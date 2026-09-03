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

import enum
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pandas as pd

import config
import data
import screener

logger = logging.getLogger(__name__)

Holdings = dict[str, float]


class HoldingState(enum.Enum):
    """The four states a currently-held ticker can be in, per one evaluation.

    Design v2.2 §3.2, M29a. Deliberately not collapsible: RC2 (Design v2.2 §2) was that "no
    data," "bad data," and "bad business" were all treated identically
    as a quality failure, which is what let a delisting/symbol-change/
    acquisition-close strike a holding toward liquidation the same way a
    genuine quality failure would. Each state below has its own strike
    rule, enforced by classify_holding_state and process_sells together.
    """

    HEALTHY = "healthy"  # data retrieved, quality floors passed -- reset strikes
    DETERIORATING = "deteriorating"  # data retrieved, quality floors failed -- add a strike
    UNREADABLE = "unreadable"  # no data, no confirmed corporate action -- no strike, no reset
    CORPORATE_ACTION = "corporate_action"  # actively detected -- never auto-traded, no strike


def classify_holding_state(metrics: data.Metrics | None, is_corporate_action: bool) -> HoldingState:
    """Classify one currently-held ticker into exactly one HoldingState.

    Pure function, no side effects -- process_sells (below) is the
    caller that turns this classification into strike/liquidation
    decisions, and M29c's evaluate-output wiring is the caller that
    turns it into a display value. `is_corporate_action` is computed by
    the caller (execution.ExecutionModule.is_corporate_action, M29b) --
    this module stays broker-free by design (DESIGN.md 3.4), so the
    signal is passed in rather than fetched here.

    Checked before the metrics-is-None branch, not after: a ticker can
    be both data-missing AND actively confirmed as delisted/inactive at
    the broker, and CORPORATE_ACTION is the more specific, more useful
    classification of the two in that case -- UNREADABLE should mean
    "unexplained absence," not "absence with a known cause."
    """
    if is_corporate_action:
        return HoldingState.CORPORATE_ACTION
    if metrics is None:
        return HoldingState.UNREADABLE
    passed = screener.pass_munger_quality_floors(metrics)[0]
    return HoldingState.HEALTHY if passed else HoldingState.DETERIORATING


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

    def tracked_tickers(self) -> list[str]:
        """Every ticker with a nonzero strike count right now.

        M26c follow-up (staff-engineer-reviewer finding): a ticker's
        streak only ever resets on a *confirmed* fill now, not on
        submission -- correct for closing the FOX/LPG bug, but it means
        a ticker whose liquidation eventually fills asynchronously
        (after this run's own synchronous settlement check already gave
        up on a "pending" result) has nothing left to reset its stale
        count, since it's no longer in current_holdings for
        process_sells to ever look at again. The caller (bot.run) uses
        this to reconcile against the broker's own current holdings
        directly: any tracked ticker no longer actually held is real,
        broker-confirmed evidence the position is gone, whatever
        Alpaca's order-status API says about *why*.
        """
        return list(self._strikes.keys())

    def save(self) -> None:
        """Atomically write the current strike counters to disk (temp file + rename)."""
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._strikes))
        tmp_path.replace(self._path)


def process_sells(
    current_holdings: Holdings,
    new_market_data: dict[str, data.Metrics | None],
    state: StateTracker,
    corporate_action_check: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Re-check every current holding against the Munger quality floors only.

    Graham's entry gates (P/E, P/E x P/B, size) deliberately do NOT apply
    here -- a stock growing out of "cheap" is success, not a sell signal
    (DESIGN.md 3.4). A hard-failing check earns a strike; a clean check
    resets the streak. `config.STRIKES_TO_LIQUIDATE` consecutive strikes
    means liquidate. Mutates and saves `state` as a side effect; returns
    `(to_liquidate, unresolved, corporate_action)`.

    Each ticker is classified via `classify_holding_state` (§3.2, M29a)
    into exactly one of HEALTHY/DETERIORATING/UNREADABLE/
    CORPORATE_ACTION, and only HEALTHY/DETERIORATING ever touch the
    strike counter:

    - HEALTHY resets the streak; DETERIORATING adds a strike, and
      `to_liquidate` fires past `config.STRIKES_TO_LIQUIDATE`.
    - UNREADABLE (no data, no confirmed corporate action) never strikes
      or resets -- `fetch_metrics` returns None identically for a
      genuine quality-relevant data problem and for a delisting, symbol
      change, or acquisition close (M24 fix: reproduced against a real
      acquisition-close scenario where two consecutive unreadable
      checks alone liquidated the position). Collected as `unresolved`
      for the caller to alert on.
    - CORPORATE_ACTION (M29b: actively confirmed via
      `corporate_action_check`, e.g. execution.ExecutionModule.
      is_corporate_action) never strikes or resets either, and is never
      auto-traded -- always a human decision (§3.2). Collected
      separately from `unresolved`, not folded into it: an unreadable
      check with no known cause and a confirmed delisting call for
      different next actions from a human, even though neither one
      trades automatically. `corporate_action_check` defaults to `None`
      (always CORPORATE_ACTION=False), so a caller that doesn't wire in
      a broker check gets exactly the pre-M29 UNREADABLE-only behavior.

    Reconciliation against the previous run's journal-expected holdings
    (DESIGN.md 3.4/3.6) is not implemented here -- it depends on the
    trade journal, which doesn't exist yet (M9). Tracked in TASKS.md.

    Caller contract (relevant once M8 wires this together): if a ticker
    in `to_liquidate` is then also passed to generate_buy_queue's
    `current_holdings` in the same run (topping it back up instead of
    letting the sell happen), that's a caller bug this function has no
    visibility into -- exclude `to_liquidate` tickers from the buy
    queue's inputs.

    M26c fix (Design v2.2 §3.3, RC3): strikes for a `to_liquidate`
    ticker are deliberately NOT reset here anymore. The previous
    behavior reset the streak the moment the *decision* to liquidate was
    made, before any order was even submitted -- this is the exact
    mechanism that produced the real FOX/LPG bug (the journal recorded
    both as sold and their strikes were reset on 2026-08-08, but their
    DAY limit orders never actually filled; the position was still held,
    with no memory that a liquidation was ever pending). State now
    transitions on confirmed outcome, not decision: the caller
    (`bot.run`) resets a `to_liquidate` ticker's strikes only after
    `settlement.settle_order` confirms the corresponding order actually
    filled.
    """
    to_liquidate = []
    unresolved = []
    corporate_action = []
    for ticker in current_holdings:
        metrics = new_market_data.get(ticker)
        is_corp_action = corporate_action_check(ticker) if corporate_action_check else False
        holding_state = classify_holding_state(metrics, is_corp_action)

        if holding_state is HoldingState.CORPORATE_ACTION:
            corporate_action.append(ticker)
            logger.error(
                "%s: confirmed corporate action (delisting/symbol change/merger) -- "
                "not striking, never auto-traded, needs manual review",
                ticker,
            )
            continue
        if holding_state is HoldingState.UNREADABLE:
            unresolved.append(ticker)
            logger.error(
                "%s: held position returned no data -- not striking; "
                "needs manual review (delisting/symbol change/acquisition?)",
                ticker,
            )
            continue

        if holding_state is HoldingState.HEALTHY:
            state.reset_strikes(ticker)
        else:
            state.add_strike(ticker)
        if state.get_strikes(ticker) >= config.STRIKES_TO_LIQUIDATE:
            to_liquidate.append(ticker)
            # M26c: strikes are NOT reset here anymore -- see the
            # docstring above. The caller resets them only once
            # settlement confirms the liquidation order actually filled.
    state.save()
    return to_liquidate, unresolved, corporate_action


def generate_buy_queue(
    current_holdings: Holdings,
    screen_results: pd.DataFrame,
    available_cash: float,
    exclude: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Build the priority-ordered list of buy orders for this run.

    `exclude` (M29 staff-engineer-reviewer finding): tickers that must
    never be bought this run regardless of buyability or score --
    concretely, a confirmed CORPORATE_ACTION ticker. Omitting a
    corporate-action ticker from `current_holdings` alone (the caller's
    existing pattern for `to_liquidate`) is NOT sufficient here: the
    new-position loop below only checks `ticker in current_holdings` to
    avoid double-buying an existing top-up candidate, so a ticker simply
    absent from `current_holdings` looks identical to "never held" to
    that loop -- if it also happens to be `buyable=True` in this run's
    screen (a real possibility: `is_corporate_action` is driven by
    Alpaca's Assets API, a signal independent of the yfinance-based
    fundamentals that drive `buyable`), it could be selected as a fresh
    NEW_POSITION buy for a security this same run just flagged as a
    confirmed corporate action. `exclude` closes that gap explicitly,
    checked in both loops, rather than relying on `current_holdings`
    membership to imply "eligible to buy," which is the assumption that
    broke here.

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
    exclude = exclude or set()
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
        if ticker in exclude:
            # Defense-in-depth: same rule even if a caller didn't
            # pre-filter current_holdings.
            continue
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
        if ticker in exclude:
            continue  # confirmed corporate action -- never auto-traded, in either direction
        order_amount = min(per_position_cap, deployable_cash)
        if order_amount < config.MIN_ORDER_NOTIONAL:
            continue
        orders.append((ticker, order_amount))
        deployable_cash -= order_amount
        position_count += 1

    return orders
