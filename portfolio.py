"""Portfolio engine (DESIGN.md section 3.4). The heart of the system.

Three responsibilities: the two-strike sell discipline (StateTracker,
process_sells), target-weight buy queue construction (generate_buy_queue),
and (implicitly, by never doing it) never selling a holding to buy a
higher-scoring one -- there is no code path here that can turn a sell
decision into a buy decision or vice versa; the two are entirely separate
functions with no shared state beyond StateTracker's strike counters.

Current holdings are always represented as ticker -> current market value
(dollars), fetched live from the broker by the caller (M8) at the start of
every run -- this module never reads holdings from local state. state.json
holds only the strike-streak counters, nothing else (DESIGN.md 3.6).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

import config
import data
import screener

logger = logging.getLogger(__name__)

Holdings = dict[str, float]


class StateTracker:
    """Reads/writes only the strike-streak counters to state.json.

    Never the source of truth for current holdings -- those are always
    fetched live from the broker (DESIGN.md 3.4). Writes are atomic
    (temp file + rename) so a crash mid-write can't corrupt the file.
    """

    def __init__(self, path: Path = config.STATE_FILE_PATH) -> None:
        """Load existing strike counters from `path`, if any."""
        self._path = path
        self._strikes: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            loaded = json.loads(self._path.read_text())
            return {str(k): int(v) for k, v in loaded.items()}
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            logger.error(
                "%s unreadable/corrupt; starting from empty strike state", self._path, exc_info=True
            )
            return {}

    def get_strikes(self, ticker: str) -> int:
        """Current consecutive-strike count for `ticker` (0 if none)."""
        return self._strikes.get(ticker, 0)

    def add_strike(self, ticker: str) -> int:
        """Increment `ticker`'s strike count and return the new value."""
        self._strikes[ticker] = self._strikes.get(ticker, 0) + 1
        return self._strikes[ticker]

    def reset_strikes(self, ticker: str) -> None:
        """Clear `ticker`'s strike count back to zero (a clean check)."""
        self._strikes.pop(ticker, None)

    def save(self) -> None:
        """Atomically write the current strike counters to disk (temp file + rename)."""
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._strikes))
        tmp_path.replace(self._path)


def process_sells(
    current_holdings: Holdings, new_market_data: dict[str, data.Metrics | None], state: StateTracker
) -> list[str]:
    """Re-check every current holding against the Munger quality floors only.

    Graham's entry gates (P/E, P/E x P/B, size) deliberately do NOT apply
    here -- a stock growing out of "cheap" is success, not a sell signal
    (DESIGN.md 3.4). A hard-failing check earns a strike; a clean check
    resets the streak; a ticker missing from new_market_data (delisting,
    data failure) counts as a strike, conservatively. Two consecutive
    strikes means liquidate. Mutates and saves `state` as a side effect;
    returns the list of tickers to liquidate.

    Reconciliation against the previous run's journal-expected holdings
    (DESIGN.md 3.4/3.6) is not implemented here -- it depends on the
    trade journal, which doesn't exist yet (M9). Tracked in TASKS.md.

    Caller contract (relevant once M8 wires this together): a liquidated
    ticker's strikes are reset to zero here, on the assumption the
    position is actually closed this run. If a ticker in `to_liquidate`
    is then also passed to generate_buy_queue's `current_holdings` in
    the same run (topping it back up instead of letting the sell
    happen), that's a caller bug this function has no visibility into --
    exclude `to_liquidate` tickers from the buy queue's inputs.
    """
    to_liquidate = []
    for ticker in current_holdings:
        metrics = new_market_data.get(ticker)
        passed = metrics is not None and screener.pass_munger_quality_floors(metrics)[0]
        if passed:
            state.reset_strikes(ticker)
        else:
            state.add_strike(ticker)
        if state.get_strikes(ticker) >= config.STRIKES_TO_LIQUIDATE:
            to_liquidate.append(ticker)
            # Reset now, not just on the next clean check: the position
            # is being closed this run, so a future re-buy of the same
            # ticker must start its strike streak fresh rather than
            # inheriting a stale count and liquidating after just one
            # bad check instead of two.
            state.reset_strikes(ticker)
    state.save()
    return to_liquidate


def generate_buy_queue(
    current_holdings: Holdings, screen_results: pd.DataFrame, available_cash: float
) -> list[tuple[str, float]]:
    """Build the priority-ordered list of buy orders for this run.

    Priority order (DESIGN.md 3.4): top up existing holdings sitting
    below target weight first, then open new positions from the top of
    the score-ranked buyable list until the target position count is
    reached or cash runs out. Never sells to buy -- this function only
    ever returns buy orders. $50 (config.MIN_ORDER_NOTIONAL) dust filter
    on every order, top-up or new.

    Caller contract: this function has no visibility into what
    process_sells decided to liquidate this run. If a ticker is in this
    run's `to_liquidate` list, the caller must exclude it from
    `current_holdings` here -- otherwise a position slated for sale
    could get topped up in the same run it's about to be closed.
    """
    portfolio_value = available_cash + sum(current_holdings.values())
    buffer = portfolio_value * config.CASH_BUFFER_PCT
    deployable_cash = max(0.0, available_cash - buffer)
    target_value = portfolio_value / config.TARGET_POSITION_COUNT
    max_value = portfolio_value * config.MAX_SINGLE_POSITION_WEIGHT
    per_position_cap = min(target_value, max_value)

    orders: list[tuple[str, float]] = []

    for ticker in sorted(current_holdings):
        if deployable_cash < config.MIN_ORDER_NOTIONAL:
            break
        gap = per_position_cap - current_holdings[ticker]
        order_amount = min(gap, deployable_cash)
        if order_amount >= config.MIN_ORDER_NOTIONAL:
            orders.append((ticker, order_amount))
            deployable_cash -= order_amount

    buyable = screen_results[screen_results["buyable"]].sort_values("score", ascending=False)
    position_count = len(current_holdings)
    for _, row in buyable.iterrows():
        if deployable_cash < config.MIN_ORDER_NOTIONAL:
            break
        if position_count >= config.TARGET_POSITION_COUNT:
            break
        ticker = str(row["symbol"])
        if ticker in current_holdings:
            continue  # already considered for a top-up above, not a new position
        order_amount = min(per_position_cap, deployable_cash)
        if order_amount < config.MIN_ORDER_NOTIONAL:
            continue
        orders.append((ticker, order_amount))
        deployable_cash -= order_amount
        position_count += 1

    return orders
