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
import time
from collections.abc import Callable

from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock, Order

import config
import execution
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
    fill_status = settlement.settle_order(exec_module, order.client_order_id)
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
    notional to config.GLOBAL_NOTIONAL_BUDGET_PCT (see its docstring), so
    in practice only the order-count budget should ever truncate here;
    the notional check is kept as a defense-in-depth backstop, not the
    active constraint. Liquidations are never truncated -- they're the
    two-strike quality discipline (risk-reducing), not discretionary.

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
    notional_budget = portfolio_value * config.GLOBAL_NOTIONAL_BUDGET_PCT

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
