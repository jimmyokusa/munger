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
holds the strike-streak counters (DESIGN.md 3.6) and, since M29c (Design
v2.2 §3.2), each held ticker's most recently classified HoldingState, so
report.py can display it -- both are cheap, small, and change together at
the same call site (process_sells), so one file continues to hold both
rather than splitting into a second persisted store for one more field.
"""

from __future__ import annotations

import datetime
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


def period_identifier(run_date: str) -> str:
    """The fiscal-period id a given run_date falls in, e.g. "2026Q3" (M35, Design v2.2 §3.1).

    Calendar-quarter based -- the natural period boundary given Evaluate
    itself now runs quarterly (M34): every Evaluate run within the same
    calendar quarter maps to the same period id, which is exactly what
    makes `add_strike`'s per-period idempotency meaningful (a
    crash-restart re-run, or bot.py's own daily fallback cadence, must
    not accumulate multiple strikes for what's really one evaluation
    period). Not tied to each individual company's own fiscal calendar
    (which varies per filer) -- the design's own §3.1 language ("two
    strikes means two consecutive quarters of failed quality") is about
    *this system's* evaluation cadence, not matching each holding's own
    10-Q schedule.
    """
    date = datetime.date.fromisoformat(run_date)
    quarter = (date.month - 1) // 3 + 1
    return f"{date.year}Q{quarter}"


class StateTracker:
    """Reads/writes the strike-streak periods and per-ticker holding state to state.json.

    Never the source of truth for current holdings -- those are always
    fetched live from the broker (DESIGN.md 3.4). Writes are atomic
    (temp file + rename) so a crash mid-write can't corrupt the file.

    Schema v2 (M35, Design v2.2 §3.1): `{"version": 2, "strikes":
    {ticker: [period_id, ...]}, "holding_states": {ticker: "healthy"|
    "deteriorating"|"unreadable"|"corporate_action", ...},
    "pending_liquidations": [ticker, ...]}`. Strikes are now recorded
    against a *fiscal period* (see `period_identifier`), not a run --
    two strikes means two distinct periods of failed quality, literally,
    regardless of how many times any workflow happens to run within one
    period. `get_strikes` still returns a plain `int` (the count of
    distinct struck periods) so every existing caller comparing against
    `config.STRIKES_TO_LIQUIDATE` needed no change.

    Every real state.json written before this schema version has no
    top-level `"version"` key at all -- either the pre-M29c legacy flat
    `{ticker: int}` shape, or the M29c-M34 wrapped-but-still-int-valued
    `{"strikes": {ticker: int}, "holding_states": {...},
    "pending_liquidations": [...]}` shape. `_load` treats the presence
    of `"version": 2` as the sole signal for the new list-valued format;
    anything else (no version key, or a version this code doesn't
    recognize) is treated as pre-v2 and migrated.

    **Migration policy, stated explicitly and followed, not left
    implicit** (§3.1's own requirement): every in-flight integer strike
    count is discarded, not converted into a synthesized placeholder
    period -- a fabricated historical period would misrepresent *when*
    the strike actually occurred, which this system's own audit
    discipline (DESIGN.md 3.6, journal.py's whole reason to exist) treats
    as worse than losing the count. Any ticker mid-streak at migration
    time starts clean and must fail quality again, under the new rule,
    to re-accumulate. `holding_states`/`pending_liquidations` are NOT
    strike data and are preserved as-is across this migration (a pre-v2
    wrapped file's own `holding_states`/`pending_liquidations`, if
    present, carry forward unchanged) -- only strikes' value type
    changes shape. `save()` always writes the current (v2) format --
    every write migrates an older file forward, matching journal.py's
    own migration precedent of writing the new shape going forward
    rather than a separate one-time migration step.

    `pending_liquidations` (M34, Design v2.2 §3.1): the handoff between
    the quarterly Evaluate cadence (which decides a ticker should be
    liquidated but never places an order) and the monthly Execute
    cadence (which does). Evaluate adds to this set; Execute reads it,
    attempts each liquidation, and removes a ticker once its liquidation
    is confirmed filled -- an unfilled/unconfirmed one stays pending for
    the next Execute window to retry, matching §3.3's "unfilled orders
    are surfaced, not forgotten" rule one level up in cadence.

    **Reverse compatibility, verified not just assumed** (staff-engineer-
    reviewer finding): state.json is shared across independently-
    deployed consumers (GitHub-Actions-triggered writers; report.py's
    separately-released Cloud Run/Pi k3s image, which only ever reads
    `all_holding_states()`, never strikes directly) that can be on
    different code versions during a rollout. A pre-M35 binary reading a
    v2 file takes its own "pre-v2 wrapped format" branch (v2's `strikes`
    is still a dict, just list-valued instead of int-valued, and that
    binary's own format check only tests for dict-ness) and then fails
    inside `int(v)` on a list value -- a `TypeError`, caught by that same
    binary's own blanket `except (..., TypeError, ...)`, degrading
    safely to empty strikes with a logged (if imprecisely worded,
    "corrupt" rather than "newer schema") error, not an uncaught crash.
    Confirmed by re-reading the exact pre-M35 `_load` shape this
    replaced, not merely assumed.
    """

    _SCHEMA_VERSION = 2

    def __init__(self, path: Path = config.STATE_FILE_PATH) -> None:
        """Load existing strike periods, holding states, and pending liquidations."""
        self._path = path
        self._strikes: dict[str, list[str]]
        self._holding_states: dict[str, str]
        self._pending_liquidations: set[str]
        self._strikes, self._holding_states, self._pending_liquidations = self._load()

    def _load(self) -> tuple[dict[str, list[str]], dict[str, str], set[str]]:
        if not self._path.exists():
            return {}, {}, set()
        try:
            loaded = json.loads(self._path.read_text())
            if loaded.get("version") == self._SCHEMA_VERSION:
                # .get(..., {}) here, not direct indexing (staff-engineer-
                # reviewer finding): a hand-edited or partially-repaired
                # state.json tagged "version": 2 but missing a key (a
                # plausible operational action -- this project's own
                # history includes a hand-reset state.json) must degrade
                # to empty for that key, the same graceful fallback every
                # other malformed shape below gets, not an uncaught
                # KeyError that crashes the whole run.
                strikes = {
                    str(k): [str(p) for p in v] for k, v in loaded.get("strikes", {}).items()
                }
                holding_states = {
                    str(k): str(v) for k, v in loaded.get("holding_states", {}).items()
                }
                raw_pending = loaded.get("pending_liquidations", [])
                pending = {str(t) for t in raw_pending} if isinstance(raw_pending, list) else set()
                return strikes, holding_states, pending
            if isinstance(loaded.get("strikes"), dict) and isinstance(
                loaded.get("holding_states"), dict
            ):
                # Pre-v2, M29c-M34 wrapped format: strikes are still
                # plain ints here. Discarded per the migration policy
                # above -- holding_states/pending_liquidations are not
                # strike data and carry forward unchanged.
                self._log_discarded_strikes(loaded["strikes"])
                holding_states = {str(k): str(v) for k, v in loaded["holding_states"].items()}
                raw_pending = loaded.get("pending_liquidations", [])
                pending = {str(t) for t in raw_pending} if isinstance(raw_pending, list) else set()
                return {}, holding_states, pending
            # Legacy pre-M29c format: the whole file is a flat
            # {ticker: strike_count} dict with no wrapper keys. Same
            # discard-the-counts migration policy.
            self._log_discarded_strikes(loaded)
            return {}, {}, set()
        except (json.JSONDecodeError, OSError, ValueError, TypeError, AttributeError, KeyError):
            logger.error(
                "%s unreadable/corrupt; starting from empty state", self._path, exc_info=True
            )
            return {}, {}, set()

    def _log_discarded_strikes(self, pre_v2_strikes: dict[str, object]) -> None:
        """Warn, naming the affected tickers, when a pre-v2 migration discards strike data.

        Staff-engineer-reviewer finding: the discard branches in `_load`
        previously returned silently -- unlike the adjacent "unreadable/
        corrupt" except-branch just below them, which does log. A ticker
        one strike away from a real quality-driven liquidation has its
        warning history erased the moment this schema ships; an operator
        reviewing the cutover run's logs should be able to see that
        happened, and to which tickers, without diffing state.json by
        hand. A no-op (not even a log line) when there was nothing to
        discard -- an empty pre-v2 file is not a noteworthy event.
        """
        if pre_v2_strikes:
            logger.warning(
                "%s: migrating to the v2 (period-based) strikes schema -- discarding "
                "in-flight integer strike counts for %s per the stated migration policy "
                "(no fabricated placeholder period; each starts clean and must fail "
                "quality again, under the new period-based rule, to re-accumulate)",
                self._path,
                sorted(pre_v2_strikes),
            )

    def get_strikes(self, ticker: str) -> int:
        """Current count of distinct struck periods for `ticker` (0 if none)."""
        return len(self._strikes.get(ticker, []))

    def add_strike(self, ticker: str, period: str) -> int:
        """Record a strike for `ticker` against `period` and return the new count.

        Idempotent per period (M35): adding the same `period` twice for
        the same ticker (a crash-restart re-run within the same
        evaluation period, or bot.py's own daily fallback cadence
        checking the same quarter repeatedly) does not double-count --
        `period_identifier` is what makes "two strikes" mean two
        genuinely distinct periods, not two runs.
        """
        periods = self._strikes.setdefault(ticker, [])
        if period not in periods:
            periods.append(period)
        return len(periods)

    def reset_strikes(self, ticker: str) -> None:
        """Clear `ticker`'s struck-period list back to empty (a clean check)."""
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

    def record_holding_state(self, ticker: str, holding_state: HoldingState) -> None:
        """Record `ticker`'s most recently classified HoldingState (M29c, §3.2)."""
        self._holding_states[ticker] = holding_state.value

    def get_holding_state(self, ticker: str) -> HoldingState | None:
        """`ticker`'s last recorded HoldingState, or None if never classified/unrecognized.

        None covers a genuinely fresh position (bought but not yet
        through a sell-evaluation pass), a state.json predating M29c,
        and (staff-engineer-reviewer finding) a value this running
        code's HoldingState enum doesn't recognize -- report.py treats
        all three the same way: no badge, not a guess, and NOT a raised
        exception. A version mismatch between the code and a persisted
        state.json (a rollback, a staged deploy reading an older/newer
        file, a hand edit) must degrade one ticker's badge, not take
        down report.py's entire `try/except: raise` in generate_report,
        which would otherwise abort index.html/tickers.html/pnl.html
        together over one bad field -- the same "one bad thing can't
        take everything else down" rule _load_pnl_snapshot already
        applies to a malformed P&L file.
        """
        raw = self._holding_states.get(ticker)
        if raw is None:
            return None
        try:
            return HoldingState(raw)
        except ValueError:
            logger.error(
                "%s: unrecognized holding state %r in state.json -- treating as unknown",
                ticker,
                raw,
            )
            return None

    def all_holding_states(self) -> dict[str, HoldingState]:
        """Every ticker with a *recognized* recorded HoldingState right now.

        A ticker with an unrecognized value (see get_holding_state) is
        silently omitted here, not raised -- report.py's read path
        (all_holding_states) must degrade one ticker's badge, never
        abort the whole report.
        """
        result: dict[str, HoldingState] = {}
        for ticker in self._holding_states:
            state = self.get_holding_state(ticker)
            if state is not None:
                result[ticker] = state
        return result

    def clear_holding_state(self, ticker: str) -> None:
        """Drop `ticker`'s recorded HoldingState (paired with reset_strikes for a sold ticker)."""
        self._holding_states.pop(ticker, None)

    def tracked_holding_state_tickers(self) -> list[str]:
        """Every ticker with a recorded HoldingState right now.

        Deliberately separate from tracked_tickers(): a HEALTHY holding
        has a recorded state but zero strikes, so it would never appear
        in tracked_tickers()'s nonzero-strikes-only list -- the caller
        (bot.run) needs this broader list too, to know which recorded
        states to reconcile against current_holdings once a HEALTHY
        position is sold.
        """
        return list(self._holding_states.keys())

    def add_pending_liquidation(self, ticker: str) -> None:
        """Mark `ticker` as decided-for-liquidation, awaiting the next Execute window (M34)."""
        self._pending_liquidations.add(ticker)

    def remove_pending_liquidation(self, ticker: str) -> None:
        """Clear `ticker`'s pending-liquidation mark, once its sell is confirmed filled."""
        self._pending_liquidations.discard(ticker)

    def pending_liquidations(self) -> list[str]:
        """Every ticker Evaluate has decided to liquidate that Execute hasn't confirmed yet."""
        return sorted(self._pending_liquidations)

    def save(self) -> None:
        """Atomically write the current state (temp file + rename), v2 schema.

        Version, strikes (as period lists), holding states, and pending
        liquidations.
        """
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(
                {
                    "version": self._SCHEMA_VERSION,
                    "strikes": {t: sorted(periods) for t, periods in self._strikes.items()},
                    "holding_states": self._holding_states,
                    "pending_liquidations": sorted(self._pending_liquidations),
                }
            )
        )
        tmp_path.replace(self._path)


def process_sells(
    current_holdings: Holdings,
    new_market_data: dict[str, data.Metrics | None],
    state: StateTracker,
    period: str,
    corporate_action_check: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Re-check every current holding against the Munger quality floors only.

    Graham's entry gates (P/E, P/E x P/B, size) deliberately do NOT apply
    here -- a stock growing out of "cheap" is success, not a sell signal
    (DESIGN.md 3.4). A hard-failing check earns a strike against `period`
    (M35, Design v2.2 §3.1 -- see `period_identifier`); a clean check
    resets the streak. `config.STRIKES_TO_LIQUIDATE` distinct struck
    periods means liquidate. Mutates and saves `state` as a side effect;
    returns `(to_liquidate, unresolved, corporate_action)`.

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

    M29c (Design v2.2 §3.2): every ticker's classified HoldingState is
    recorded via `state.record_holding_state`, regardless of which
    branch below it takes -- this is what lets report.py display all
    four states, not just the two (DETERIORATING via strikes,
    CORPORATE_ACTION via the alert) that already had some other visible
    side effect before this milestone.
    """
    to_liquidate = []
    unresolved = []
    corporate_action = []
    for ticker in current_holdings:
        metrics = new_market_data.get(ticker)
        is_corp_action = corporate_action_check(ticker) if corporate_action_check else False
        holding_state = classify_holding_state(metrics, is_corp_action)
        state.record_holding_state(ticker, holding_state)

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
            state.add_strike(ticker, period)
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
