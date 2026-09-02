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
) -> tuple[list[str], list[str]]:
    """Re-check every current holding against the Munger quality floors only.

    Graham's entry gates (P/E, P/E x P/B, size) deliberately do NOT apply
    here -- a stock growing out of "cheap" is success, not a sell signal
    (DESIGN.md 3.4). A hard-failing check earns a strike; a clean check
    resets the streak. `config.STRIKES_TO_LIQUIDATE` consecutive strikes
    means liquidate. Mutates and saves `state` as a side effect; returns
    `(to_liquidate, unresolved)`.

    A ticker missing from `new_market_data` is NOT struck. `fetch_metrics`
    returns None identically for a genuine quality-relevant data problem
    and for a delisting, symbol change, or acquisition close -- absence of
    data is not evidence of failing quality, and striking it as if it were
    can liquidate a position that was never actually re-evaluated (M24
    fix: reproduced against a real acquisition-close scenario, where two
    consecutive unreadable checks alone -- no quality read at all --
    liquidated the position). Unreadable holdings are collected separately
    as `unresolved`; their strike streak is held steady (neither
    incremented nor reset -- an unreadable check is no evidence either
    way), and the caller is expected to alert on them for manual review.

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
    unresolved = []
    for ticker in current_holdings:
        metrics = new_market_data.get(ticker)
        if metrics is None:
            unresolved.append(ticker)
            logger.error(
                "%s: held position returned no data -- not striking; "
                "needs manual review (delisting/symbol change/acquisition?)",
                ticker,
            )
            continue
        passed = screener.pass_munger_quality_floors(metrics)[0]
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
    return to_liquidate, unresolved


def generate_buy_queue(
    current_holdings: Holdings, screen_results: pd.DataFrame, available_cash: float
) -> list[tuple[str, float]]:
    """Build the priority-ordered list of buy orders for this run.

    Priority order (DESIGN.md 3.4): top up existing holdings sitting
    below target weight first, then open new positions from the top of
    the score-ranked buyable list until the target position count is
    reached or cash runs out. Never sells to buy -- this function only
    ever returns buy orders. $50 (config.MIN_ORDER_NOTIONAL) dust filter
    on every order, top-up or new; a top-up additionally only fires past
    config.REBALANCE_DRIFT_BAND_PCT below target (daily-cadence tolerance
    band -- see its definition).

    Self-limits total notional to config.GLOBAL_NOTIONAL_BUDGET_PCT of
    portfolio value, same as the run-level budget bot.py enforces as a
    backstop. Without this, a cold start (many buyable candidates, zero
    current holdings) would build a queue bot.py's budget check rejects
    wholesale -- capping deployable_cash here instead means a cold start
    fills as much of the target portfolio as fits this run's budget and
    ramps the rest in over subsequent runs, rather than deadlocking (the
    same buy queue, and the same rejection, every run, forever, since
    nothing ever gets bought).

    Caller contract: this function has no visibility into what
    process_sells decided to liquidate this run. If a ticker is in this
    run's `to_liquidate` list, the caller must exclude it from
    `current_holdings` here -- otherwise a position slated for sale
    could get topped up in the same run it's about to be closed.

    A top-up is a purchase, not a hold: unlike process_sells (where
    Graham's entry gates deliberately don't apply -- a stock growing out
    of "cheap" is success, not a sell signal), a top-up must still pass
    the current screen's buyability gate. Without this, the loop below
    would add to a position the current screen explicitly refuses to
    open, compounding into averaging down into a deteriorating position
    at any price under a daily cadence -- the exact behavior the
    margin-of-safety gates exist to prevent (M24 fix, reproduced against
    the real function: a holding failing graham_pe/graham_pe_times_pb
    still received a top-up order before this check existed). A holding
    absent from screen_results entirely (delisting, symbol change) has no
    row and is therefore also not in buyable_now -- conservatively not
    topped up, since buyability can't be confirmed for a ticker the
    screen can't see.
    """
    buyable_now = set(screen_results.loc[screen_results["buyable"], "symbol"].astype(str))
    portfolio_value = available_cash + sum(current_holdings.values())
    buffer = portfolio_value * config.CASH_BUFFER_PCT
    deployable_cash = max(0.0, available_cash - buffer)
    run_notional_budget = portfolio_value * config.GLOBAL_NOTIONAL_BUDGET_PCT
    deployable_cash = min(deployable_cash, run_notional_budget)
    target_value = portfolio_value / config.TARGET_POSITION_COUNT
    max_value = portfolio_value * config.MAX_SINGLE_POSITION_WEIGHT
    per_position_cap = min(target_value, max_value)
    drift_threshold = per_position_cap * config.REBALANCE_DRIFT_BAND_PCT

    orders: list[tuple[str, float]] = []

    for ticker in sorted(current_holdings):
        if deployable_cash < config.MIN_ORDER_NOTIONAL:
            break
        if ticker not in buyable_now:
            logger.info(
                "%s: holding not buyable in the current screen -- holding, not topping up",
                ticker,
            )
            continue
        gap = per_position_cap - current_holdings[ticker]
        if gap < drift_threshold:
            continue  # within the daily-noise tolerance band, not real drift
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
