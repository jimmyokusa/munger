"""Shared safety-critical helpers for evaluate.py and execute_trades.py (M34, Design v2.2 §3.1).

Extracted out of bot.py (the pre-M34 combined evaluate+execute loop, kept
around unmodified as a fallback -- see its own module docstring) so the
two new cadence-split workflows share one copy of kill-switch checking,
settlement reaction, and budget capping instead of two copies that could
silently drift apart on safety-critical logic. Every function here is a
direct, behavior-preserving extraction -- no logic changed in the move.
"""

from __future__ import annotations

import datetime
import logging
import sqlite3
import time
from collections.abc import Callable

from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock, Order

import config
import execution
import journal
import material_events
import settlement

# mypy strict (no_implicit_reexport): re-exported so tests can patch
# trading_common.settlement.settle_order directly -- the actual
# settlement-query seam settle_and_react calls through.
__all__ = ["settlement"]

logger = logging.getLogger(__name__)


def kill_switch_active() -> bool:
    """True if the run should be screen-only: no orders, no broker calls.

    Checked via either the config flag or the filesystem flag file
    (DESIGN.md 5) -- the file lets an operator halt live trading without
    a code/config deploy.
    """
    return config.KILL_SWITCH or config.KILL_SWITCH_FLAG_FILE_PATH.exists()


def global_kill_switch_active() -> bool:
    """True if the account-independent master kill switch is set.

    M20 (DESIGN_REAL_MONEY.md §3.2): checked before `kill_switch_active()`
    above, unconditionally, by every workflow that can touch the broker.
    Unlike that per-account flag file (DATA_DIR-relative, so scoped to
    just one workflow's own runner/checkout), this one lives at a fixed
    path in the repo checkout itself (config.BASE_DIR) -- a single commit
    adding this file on `main` is visible to every workflow's next
    `actions/checkout`, stopping every account and every cadence with one
    change, not four separate ones.
    """
    return config.GLOBAL_KILL_SWITCH_FLAG_FILE_PATH.exists()


def alert(alerts: list[str], message: str) -> None:
    """Record an alert-worthy condition immediately, not just at the end.

    Staff-engineer-reviewer finding: an alert appended to a list but only
    logged when the run finally finishes is lost if an unhandled
    exception fires first -- the operator would see only the crash
    traceback, never the earlier alert-worthy condition that was also
    true for that run. Logging and annotating at the point of discovery
    survives that.
    """
    alerts.append(message)
    logger.error("ALERT: %s", message)
    print(f"::error::{message}")


def finish(alerts: list[str]) -> int:
    """Return the process exit code for this run (0 clean, 1 alert-worthy).

    Deliberately conflates "alert-worthy" with "exit non-zero" (staff-
    engineer-reviewer, M10 review: abort paths were indistinguishable
    from success by exit code alone): a non-zero exit marks the
    scheduling workflow's run as failed, which triggers its built-in
    failure notification -- the alert delivery channel this project uses
    instead of standing up a new external notification service.
    """
    return 1 if alerts else 0


def check_data_freshness() -> int | None:
    """Hours since the last archived screen result, or None if within tolerance.

    A crude dead-man's-switch (DESIGN.md 8): if the scheduler silently
    stopped firing (or a run failed before archiving) for one or more
    cycles, the next run that does fire will see a large gap here and
    alert. Doesn't catch a total, permanent scheduler failure -- nothing
    runs this check if nothing ever runs at all -- but catches a
    resumed-after-an-outage scenario. Returns None (nothing to compare
    against) on the very first run, before any archive exists.

    Derives the age from the run_date embedded in each archive's
    filename (screen_results_{run_date}.csv), not filesystem mtime --
    staff-engineer-reviewer finding: GitHub Actions restores this
    directory from a git branch every run, and git does not preserve
    mtimes across a checkout, so every restored file would be stamped
    with "now" regardless of how old the underlying run actually was,
    silently neutralizing an mtime-based check.
    """
    if not config.SCREEN_RESULTS_ARCHIVE_DIR.exists():
        return None
    run_dates: list[datetime.date] = []
    for f in config.SCREEN_RESULTS_ARCHIVE_DIR.glob("screen_results_*.csv"):
        date_str = f.stem.removeprefix("screen_results_")
        try:
            run_dates.append(datetime.date.fromisoformat(date_str))
        except ValueError:
            continue
    if not run_dates:
        return None
    age_hours = (datetime.date.today() - max(run_dates)).days * 24
    if age_hours > config.DATA_FRESHNESS_MAX_HOURS:
        return age_hours
    return None


def market_is_open() -> bool:
    """Whether Alpaca's own market clock reports the market open right now.

    M45 (user request, 2026-09-04): daily-trade.yml/daily-trade-live.yml
    run unconditionally every calendar day at a fixed UTC time
    (config.py's own comment on NEWS_UPDATE_DAY_OF_MONTH already noted
    this in passing) -- there was no trading-day/market-hours gate on
    the actual trading path at all, only on pnl.py's separate intraday
    snapshot job (PNL_MARKET_HOURS_ONLY). Moved here (out of pnl.py,
    where this originated as M19) so bot.py/execute_trades.py can share
    the exact same authority pnl.py already trusted, rather than each
    approximating market hours with its own UTC/weekday heuristic --
    Alpaca's own clock is the one source that already gets weekends,
    holidays, and the ET/UTC DST shift right without this project having
    to maintain a market calendar itself.

    Fails CLOSED in the sense that matters for a caller gating trading:
    an unexpected response type raises rather than silently returning
    True, so a broken clock call blocks the trade path (screen-only for
    that run) rather than risking a spurious go-ahead. A transient
    failure is retried once first (see the single retry below) before
    that raise, matching pnl.py's own established tolerance for one-off
    network blips.

    Builds its own TradingClient rather than sharing one with
    execution.ExecutionModule -- constructing a client makes no network
    call, so a second instance costs nothing, and keeping this
    self-contained means callers (and tests) don't need to thread a
    client through just for this one read.
    """
    trading = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )
    try:
        clock = trading.get_clock()
    except Exception:
        logger.warning("get_clock failed once; retrying once after a short delay.")
        time.sleep(config.ALPACA_RETRY_DELAY_SECONDS)
        clock = trading.get_clock()
    if not isinstance(clock, Clock):
        raise ValueError(f"unexpected get_clock() response: {clock!r}")
    return bool(clock.is_open)


def settle_and_react(
    exec_module: execution.ExecutionModule,
    alerts: list[str],
    symbol: str,
    order_kind: str,
    order: Order,
    on_filled: Callable[[], None] | None = None,
) -> bool:
    """Settle one just-submitted order and alert on its outcome.

    Returns True if this was a genuine settlement *query failure* (not
    merely a pending order, which is normal) -- the caller's signal to
    stop placing further orders for the rest of this run, on top of the
    kill-switch this also sets for the *next* run (staff-engineer-
    reviewer finding: setting the flag alone doesn't stop the run
    already in progress from placing more orders on the same
    now-unverifiable position picture).
    """
    # wait_for_fill (M46): this is the synchronous check fired milliseconds
    # after submission, so a "pending" result here is usually just Alpaca's
    # pending_new/accepted window, not a real non-fill -- let settle_order
    # re-poll briefly before it reports the order unconfirmed (which would
    # otherwise alert and fail the whole scheduled run over a market order
    # that fills a second later). A genuine non-fill still surfaces, just
    # a few seconds later.
    fill_status = settlement.settle_order(exec_module, order.client_order_id, wait_for_fill=True)
    if fill_status == "filled":
        if on_filled is not None:
            on_filled()
        return False
    if fill_status is None:
        alert(
            alerts,
            f"{symbol}: {order_kind} submitted but settlement query failed -- unconfirmed",
        )
        # M26d: fail closed on a genuine query failure -- set the same
        # kill-switch mechanism this and every other cadence checks,
        # reusing it rather than inventing new blocking behavior, plus a
        # second marker file recording that it was settlement (not a
        # human) that set it, so a later run can tell a stuck block apart
        # from a deliberate pause and escalate accordingly.
        config.KILL_SWITCH_FLAG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.KILL_SWITCH_FLAG_FILE_PATH.touch()
        config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.touch()
        return True
    alert(
        alerts,
        f"{symbol}: {order_kind} submitted but not yet filled "
        f"(status={fill_status}) -- unconfirmed",
    )
    return False


def cap_buy_orders_to_budget(
    buy_orders: list[tuple[str, float]], liquidation_count: int, portfolio_value: float
) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    """Truncate the buy queue to this run's order-count and notional budgets.

    Preserves priority order, deferring the remainder to a later run
    instead of aborting the whole run.

    Previously, exceeding either budget aborted the entire run -- correct
    for a single one-off overage, but a deadlock under a cold start
    (many buyable candidates, zero holdings): zero orders means holdings
    stay at zero, so the next run builds the identical over-budget queue
    and aborts again, forever. generate_buy_queue already self-limits
    notional to config.EFFECTIVE_GLOBAL_NOTIONAL_BUDGET_PCT (see its
    docstring), so in practice only the order-count budget should ever
    truncate here; the notional check is kept as a defense-in-depth
    backstop, not the active constraint. **Must read the same
    EFFECTIVE_GLOBAL_NOTIONAL_BUDGET_PCT generate_buy_queue used to size
    its own queue** (M48 finding) -- reading the bare
    config.GLOBAL_NOTIONAL_BUDGET_PCT here instead would silently
    truncate a concentrated-mode order right back down to the
    un-overridden percentage, defeating that override entirely. Liquidations
    are never truncated -- they're the two-strike quality discipline
    (risk-reducing), not discretionary.

    Takes a strict prefix of buy_orders at each budget in turn (order
    count, then notional), rather than skipping an order that doesn't fit
    while continuing to try later, smaller ones -- staff-engineer-reviewer
    finding: an earlier version used "skip and continue" for the notional
    check, which could defer a high-priority top-up that didn't fit while
    still buying a lower-priority new position after it, silently
    breaking the priority order this function's own docstring promises.

    Also returns which budget(s) actually bound (empty if nothing was
    deferred) -- staff-engineer-reviewer finding: the order-count budget
    binding is rare and alarming, while the notional budget binding is
    expected and routine during a cold start -- collapsing both into one
    generic "order/notional budget" message made every deferral read the
    same regardless of which, much rarer, case actually occurred.
    """
    max_buy_orders = max(0, config.GLOBAL_ORDER_BUDGET - liquidation_count)
    notional_budget = portfolio_value * config.EFFECTIVE_GLOBAL_NOTIONAL_BUDGET_PCT

    count_capped = buy_orders[:max_buy_orders]
    capped: list[tuple[str, float]] = []
    running_notional = 0.0
    for symbol, notional in count_capped:
        if running_notional + notional > notional_budget:
            break
        capped.append((symbol, notional))
        running_notional += notional
    deferred = [symbol for symbol, _ in buy_orders[len(capped) :]]

    bound_budgets: list[str] = []
    if len(count_capped) < len(buy_orders):
        bound_budgets.append("order-count")
    if len(capped) < len(count_capped):
        bound_budgets.append("notional")
    return capped, deferred, bound_budgets


def check_small_account_concentration_ceiling(portfolio_value: float) -> str | None:
    """None if fine to proceed; otherwise an alert message and the caller must abort.

    M48 (staff-engineer-reviewer finding, push review):
    config.SMALL_ACCOUNT_CONCENTRATED_MODE is a static, equity-unaware
    override -- nothing else in this codebase ties it to the account's
    actual current equity. Without this check, a deposit that grew the
    account well past the ~$100 it was scoped and user-approved for would
    silently place an all-in single-stock order at whatever the new,
    larger equity is, if a human simply forgot to remove the one workflow
    env var line first. This is the structural guard, called from
    bot.py/execute_trades.py right before generate_buy_queue: an
    alert-worthy abort (the same "screen-only, no orders placed" posture
    every other pre-trade gate in those callers uses), not a silent skip
    or a quietly-resized order.

    No-op (returns None immediately) when the flag itself is off --
    every other account is entirely unaffected by this check existing.
    """
    if not config.SMALL_ACCOUNT_CONCENTRATED_MODE:
        return None
    ceiling = config.SMALL_ACCOUNT_CONCENTRATED_MODE_EQUITY_CEILING
    if portfolio_value <= ceiling:
        return None
    return (
        f"MUNGER_SMALL_ACCOUNT_CONCENTRATED_MODE is set but equity (${portfolio_value:,.2f}) "
        f"exceeds its ${ceiling:,.2f} ceiling -- refusing to place an all-in single-stock "
        "order at this size. Remove MUNGER_SMALL_ACCOUNT_CONCENTRATED_MODE (the account no "
        "longer needs it) or deliberately raise the ceiling, but do not let this pass "
        "silently."
    )


def check_material_event_buy_blocks(candidates: list[str], alerts: list[str]) -> set[str]:
    """Symbols to keep out of this run's buy queue; alerts into `alerts` as needed.

    Returns the FULL current block set, re-derived live from EDGAR every
    run and returned unconditionally -- the exclusion never depends on
    journal state, so a journal that fails to *persist* can only ever cost
    an operator a duplicate notification, never let a blocked symbol
    through. (A journal that raises is different: that propagates and
    fails the run closed, before any order -- see the per-ticker
    exception handling below for why that can't happen on this path.)

    M50a (2026-10-01, found by reading the first real runs after M50
    shipped, not in review): M50 as first built alerted on every block on
    every run, which marks the whole workflow run failed (non-zero exit,
    per finish's own "alert-worthy == exit non-zero" contract) and emails
    the operator. The real GRBK block correctly fired -- and then kept
    firing daily, for a condition already known, for as long as the
    filing stayed inside its cooldown (~7 more months, since GRBK sits
    below target weight and so comes up for a top-up every run). That is
    precisely the alert-fatigue failure mode Design v2.2 §3.8 names as the
    thing to design against ("an alert channel that fires weekly gets
    ignored, which is worse than not having one, because it produces false
    confidence that something is watching") -- and worse here, since an
    indistinguishable red X every day trains an operator to ignore exactly
    the channel a genuinely new Critical filing would arrive on.

    So: alert when a block is first seen, then again only once every
    config.MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS while it stays active. A
    genuinely NEW filing (different accession number, or the same filing
    on a different account) is a different key and alerts immediately.
    Every block, new or not, is logged at WARNING every run, so the
    current state is always visible in the run log.

    **Alerts are emitted here, per ticker, immediately BEFORE recording
    that ticker (staff-engineer-reviewer finding on the first M50a
    draft, which had this exactly backwards and would have been a real
    silent-failure bug).** That draft recorded inside this loop but
    returned one aggregated message for the caller to alert with
    afterwards, reasoning that nothing after the record could fail. Wrong:
    alert() runs in the caller, after this function returns, and each
    record commits immediately -- so a crash, a sqlite lock timeout on the
    next ticker, or the job's own 45-minute timeout landing mid-loop would
    commit a dedup row whose alert was never emitted, and (because the
    workflows persist journal.db with `if: always()`, even on the failure
    path) permanently suppress the one notification the entire gate exists
    to produce. Alerting first bounds the worst case at one duplicate
    alert for one ticker, which is the cost already accepted elsewhere
    here, instead of indefinite silence on a real Critical filing.
    """
    try:
        blocks = material_events.check_buy_candidates(candidates)
    except material_events.MaterialEventCheckUnavailableError as exc:
        # The gate could not look at anything (SEC ticker index down). Loud,
        # because silence here is indistinguishable from a clean run now that
        # suppression is the steady state -- see the exception's own docstring.
        # Deliberately does NOT abort the run: that would be a real change to
        # trading behavior (an EDGAR hiccup would stop all buying for the day),
        # which is a decision for the user, not a side effect of a milestone
        # about alert volume. Tracked as an open question in TASKS.md's M50a
        # section instead.
        alert(
            alerts,
            f"Material-event buy gate did not run this cycle: {exc}. No candidate was "
            "checked, so buys this run are UNGATED for Critical-severity filings.",
        )
        return set()
    for ticker, block in sorted(blocks.items()):
        # Per-ticker fault isolation (same staff-engineer finding): one
        # ticker's journal failure must not swallow another ticker's alert,
        # and an unreadable journal must fail toward telling the operator,
        # never toward silence -- the block applies either way, so the only
        # thing at stake in these except branches is the notification.
        try:
            last_alerted = journal.get_buy_block_last_alerted(ticker, block.accession_number)
            read_failed = False
        except sqlite3.Error:
            logger.exception(
                "%s: buy-block alert state unreadable -- alerting rather than risking silence",
                ticker,
            )
            last_alerted = None
            read_failed = True
        if last_alerted is not None and not _realert_interval_elapsed(last_alerted):
            logger.warning(
                "%s: still blocked from a buy (%s) -- already alerted %s, not re-alerting",
                ticker,
                block.reason,
                last_alerted,
            )
            continue
        # staff-engineer-reviewer (2nd pass): a persistently broken journal
        # would otherwise re-alert every run with the first-discovery wording,
        # i.e. look exactly like a genuinely new Critical filing -- the one
        # case where the operator most needs the two told apart. Say so.
        if read_failed:
            prefix = "blocked (alert history unreadable, so this may be a repeat)"
        elif last_alerted:
            prefix = "still blocked"
        else:
            prefix = "blocked"
        alert(
            alerts,
            f"New-buy candidate {prefix} by an unresolved Critical-severity material event: "
            f"{ticker} ({block.reason})",
        )
        try:
            journal.record_buy_block_alert(ticker, block.accession_number)
        except sqlite3.Error:
            logger.exception(
                "%s: buy-block alert recorded nowhere -- may re-alert next run", ticker
            )
    return set(blocks)


def _realert_interval_elapsed(last_alerted: str) -> bool:
    """True if config.MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS has passed since `last_alerted`.

    An unusable stored timestamp resolves to True (re-alert) rather than
    False: the same fail-toward-telling-the-operator posture the caller's
    own except branches use, since the alternative is silently never
    mentioning an active block again because of a bad string.

    Catches TypeError alongside ValueError (pm-reviewer finding on an
    earlier draft that claimed this posture while only catching the
    latter): a timezone-NAIVE stored string parses fine and then raises
    TypeError on the subtraction below. Unreachable today -- every write
    goes through record_buy_block_alert's datetime.now(UTC).isoformat() --
    but the guarantee this docstring makes should actually hold rather
    than depend on that staying true.
    """
    try:
        previous = datetime.datetime.fromisoformat(last_alerted)
        elapsed = datetime.datetime.now(datetime.UTC) - previous
    except (ValueError, TypeError):
        logger.warning("Unusable buy-block alert timestamp %r -- re-alerting", last_alerted)
        return True
    if elapsed < datetime.timedelta(0):
        # A future timestamp would otherwise suppress the alert until
        # wall-clock time caught up -- silently, for however long it is set
        # ahead (staff-engineer-reviewer, 2nd pass). Reachable: hand-editing
        # this column forward is the most obvious ad-hoc snooze an operator
        # has today, and the intuitive direction to edit it.
        logger.warning(
            "Buy-block alert timestamp %r is in the future -- re-alerting rather than "
            "going silent until it elapses",
            last_alerted,
        )
        return True
    return elapsed >= datetime.timedelta(days=config.MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS)
