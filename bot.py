"""Orchestration entry point (DESIGN.md sections 4, 5, 8).

Ties every module together for one scheduled run: universe -> data ->
screener always run (screen-only mode never touches the broker); sells,
buys, and journaling only run if the kill switch is off and the
universe-fetch-fraction sanity check passes.
"""

from __future__ import annotations

import datetime
import logging
import sys

import pandas as pd

import config
import data
import execution
import journal
import portfolio
import screener
import universe

logger = logging.getLogger(__name__)


def _kill_switch_active() -> bool:
    """True if the run should be screen-only: no orders, no broker calls.

    Checked via either the config flag or the filesystem flag file
    (DESIGN.md 5) -- the file lets an operator halt live trading without
    a code/config deploy.
    """
    return config.KILL_SWITCH or config.KILL_SWITCH_FLAG_FILE_PATH.exists()


def _global_kill_switch_active() -> bool:
    """True if the account-independent master kill switch is set.

    M20 (DESIGN_REAL_MONEY.md §3.2): checked before `_kill_switch_active()`
    above, unconditionally, by both the paper and the live workflow. Unlike
    that per-account flag file (DATA_DIR-relative, so scoped to just one
    workflow's own runner/checkout), this one lives at a fixed path in the
    repo checkout itself (config.BASE_DIR) -- a single commit adding this
    file on `main` is visible to both workflows' next `actions/checkout`,
    regardless of which account's DATA_DIR each is otherwise scoped to.
    """
    return config.GLOBAL_KILL_SWITCH_FLAG_FILE_PATH.exists()


def _fetched_fraction(results: pd.DataFrame) -> float:
    """Fraction of screened tickers that got real data.

    Thin delegate to `screener.fetched_fraction` (the single source of
    truth) so the trading path and the screen-only daily path can't drift
    on this safety threshold. A half-empty screen would make every holding
    look delisted and trigger mass strikes (DESIGN.md §5).
    """
    return screener.fetched_fraction(results)


def _process_sells_if_data_is_healthy(
    current_holdings: portfolio.Holdings,
    holdings_metrics: dict[str, data.Metrics | None],
    state: portfolio.StateTracker,
) -> list[str]:
    """Run process_sells, unless too much of the holdings' data is missing.

    The universe-wide fetch-fraction abort (_fetched_fraction) only
    covers the buy-side screen; this second, independent fetch for
    currently-held tickers feeds process_sells's strike logic directly,
    where a missing ticker counts as a strike. A degraded fetch here
    (e.g. the same yfinance rate-limit cooldown from the first fetch
    still in effect) would otherwise silently strike real holdings
    toward liquidation with no distinguishing signal from a genuine
    quality failure (staff-engineer-reviewer finding).
    """
    if not current_holdings:
        return []
    fetched = sum(1 for m in holdings_metrics.values() if m is not None)
    fraction = fetched / len(current_holdings)
    if fraction < config.MIN_UNIVERSE_FETCH_FRACTION:
        logger.error(
            "Skipping sell evaluation: only %.1f%% of held tickers' data fetched cleanly "
            "(need >= %.1f%%) -- treating as a data problem, not a quality failure",
            fraction * 100,
            config.MIN_UNIVERSE_FETCH_FRACTION * 100,
        )
        return []
    return portfolio.process_sells(current_holdings, holdings_metrics, state)


def _check_data_freshness() -> int | None:
    """Hours since the last archived screen result, or None if within tolerance.

    A crude dead-man's-switch (DESIGN.md 8): if the scheduler silently
    stopped firing (or a run failed before archiving) for one or more
    cycles, the next run that does fire will see a large gap here and
    alert. Doesn't catch a total, permanent scheduler failure -- nothing
    runs this check if bot.py never runs at all -- but catches a
    resumed-after-an-outage scenario, which is the realistic failure
    mode for a quarterly cron/Action. Returns None (nothing to compare
    against) on the very first run, before any archive exists.

    Derives the age from the run_date embedded in each archive's
    filename (screen_results_{run_date}.csv), not filesystem mtime --
    staff-engineer-reviewer finding: M11's GitHub Actions workflow
    restores this directory from a git branch every run, and git does
    not preserve mtimes across a checkout, so every restored file would
    be stamped with "now" regardless of how old the underlying run
    actually was, silently neutralizing an mtime-based check.
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


def _alert(alerts: list[str], message: str) -> None:
    """Record an alert-worthy condition immediately, not just at the end.

    Staff-engineer-reviewer finding: an alert appended to a list but only
    logged when run() finally reaches _finish() is lost if an unhandled
    exception fires first (e.g. a universe fallback earlier in the same
    run that's then followed by verify_account_access raising) -- the
    operator would see only the crash traceback, never the earlier
    alert-worthy condition that was also true for that run. Logging and
    annotating at the point of discovery survives that.
    """
    alerts.append(message)
    logger.error("ALERT: %s", message)
    print(f"::error::{message}")


def _cap_buy_orders_to_budget(
    buy_orders: list[tuple[str, float]], liquidation_count: int, portfolio_value: float
) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    """Truncate the buy queue to this run's order-count and notional budgets.

    Preserves priority order, deferring the remainder to a later run
    instead of aborting the whole run.

    Previously, exceeding either budget aborted the entire run -- correct
    for a single one-off overage, but a deadlock under daily rebalancing
    from a cold start: zero orders means holdings stay at zero, so the
    next run builds the identical over-budget queue and aborts again,
    forever. generate_buy_queue already self-limits notional to
    config.GLOBAL_NOTIONAL_BUDGET_PCT (see its docstring), so in practice
    only the order-count budget should ever truncate here; the notional
    check is kept as a defense-in-depth backstop, not the active
    constraint. Liquidations are never truncated -- they're the
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
    binding is rare and alarming (it only happens with 6+ same-run
    liquidations, since TARGET_POSITION_COUNT=15 < GLOBAL_ORDER_BUDGET=20
    at today's config), while the notional budget binding is expected and
    routine during a cold start (DESIGN.md 6) -- collapsing both into one
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


def _finish(alerts: list[str]) -> int:
    """Return the process exit code for this run (0 clean, 1 alert-worthy).

    Deliberately conflates "alert-worthy" with "exit non-zero" (staff-
    engineer-reviewer, M10 review: abort paths were indistinguishable
    from success by exit code alone): a non-zero exit marks the
    scheduling workflow's run as failed, which triggers its built-in
    failure notification -- the alert delivery channel this project
    uses instead of standing up a new external notification service.
    """
    return 1 if alerts else 0


def run(run_date: str | None = None) -> int:
    """Execute one full run. Returns a process exit code (0 clean, 1 alert-worthy).

    Does not raise for expected abort/alert conditions (kill switch,
    budgets, data-fetch fraction, universe fallback, liquidations,
    reconciliation mismatches) -- those are collected and reported via
    the return code (see _finish). Only genuinely unexpected failures
    (e.g. a broken broker pre-check) are left to propagate and crash the
    process, per DESIGN.md's fail-closed philosophy for those specific
    checks.
    """
    alerts: list[str] = []
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting run for %s (paper=%s)", run_date, config.PAPER_TRADING)

    stale_hours = _check_data_freshness()
    if stale_hours is not None:
        _alert(
            alerts,
            f"No successful run archived in {stale_hours} hours "
            f"(tolerance {config.DATA_FRESHNESS_MAX_HOURS}h) -- "
            "a scheduled run may have been missed",
        )

    universe_result = universe.get_universe_with_diagnostics()
    tickers = universe_result.tickers
    if not tickers:
        _alert(alerts, "Universe fetch returned zero tickers")
    for index in universe_result.fallback_indices:
        severity = "HIGH" if index == "500" else "normal"
        _alert(alerts, f"S&P {index} universe fallback triggered (severity={severity})")

    results = screener.run_screen(tickers)
    journal.archive_screen_results(run_date)

    fetched_fraction = _fetched_fraction(results)
    if fetched_fraction < config.MIN_UNIVERSE_FETCH_FRACTION:
        _alert(
            alerts,
            f"Aborting: only {fetched_fraction:.1%} of the universe fetched cleanly "
            f"(need >= {config.MIN_UNIVERSE_FETCH_FRACTION:.1%})",
        )
        return _finish(alerts)

    buyable_count = int(results["buyable"].sum())
    logger.info("Screen complete: %d tickers, %d buyable.", len(results), buyable_count)

    if _global_kill_switch_active():
        logger.warning(
            "GLOBAL_KILL_SWITCH active -- screen-only run (all accounts), "
            "no orders will be placed."
        )
        return _finish(alerts)

    if _kill_switch_active():
        logger.warning("KILL_SWITCH active -- screen-only run, no orders will be placed.")
        return _finish(alerts)

    exec_module = execution.ExecutionModule(run_date)
    exec_module.verify_account_access()

    current_holdings = exec_module.get_current_holdings()

    reconciliation_warnings = journal.check_reconciliation(set(current_holdings))
    for warning in reconciliation_warnings:
        _alert(alerts, f"Reconciliation mismatch: {warning}")
    if reconciliation_warnings and not config.PAPER_TRADING:
        # M20 (DESIGN_REAL_MONEY.md §3.3): a mismatch against the live
        # account aborts rather than the warn-and-continue posture paper
        # keeps (M10) -- decided in design review given how much thinner
        # the rest of this system's defense-in-depth is for a genuinely
        # new account than it is for paper, which has been running
        # cleanly for months. The alerts above have already fired, so the
        # mismatch is not silent; this just stops the run from also
        # placing orders on top of a state it can't currently trust.
        _alert(alerts, "Aborting: reconciliation mismatch against the live account")
        return _finish(alerts)

    holdings_metrics = data.fetch_all_metrics(list(current_holdings), phase="holdings check")
    state = portfolio.StateTracker()
    to_liquidate = _process_sells_if_data_is_healthy(current_holdings, holdings_metrics, state)
    if to_liquidate:
        _alert(alerts, f"Liquidation(s) this run: {', '.join(sorted(to_liquidate))}")

    # Caller contract from the M6/M7 review: a ticker slated for
    # liquidation this run must not also be topped up by the buy queue.
    remaining_holdings = {t: v for t, v in current_holdings.items() if t not in to_liquidate}
    available_cash = exec_module.get_available_cash()
    buy_orders = portfolio.generate_buy_queue(remaining_holdings, results, available_cash)

    # Matches generate_buy_queue's own internal portfolio_value (computed
    # from remaining_holdings, not current_holdings) -- staff-engineer-
    # reviewer finding: using current_holdings here made this budget
    # check's denominator larger than the one that actually constrained
    # buy-order sizing, silently more permissive than intended.
    portfolio_value = available_cash + sum(remaining_holdings.values())
    buy_orders, deferred_symbols, bound_budgets = _cap_buy_orders_to_budget(
        buy_orders, len(to_liquidate), portfolio_value
    )
    if deferred_symbols:
        _alert(
            alerts,
            f"Deferred {len(deferred_symbols)} buy order(s) to a later run "
            f"({' and '.join(bound_budgets)} budget): {', '.join(deferred_symbols)}",
        )

    for symbol in to_liquidate:
        order = exec_module.liquidate(symbol)
        if order is not None:
            journal.record_order(
                symbol,
                "sell",
                f"SELL strikes={config.STRIKES_TO_LIQUIDATE}",
                client_order_id=order.client_order_id,
            )

    score_by_symbol = dict(zip(results["symbol"], results["score"], strict=True))
    for symbol, notional in buy_orders:
        order = exec_module.market_buy(symbol, notional)
        if order is not None:
            score = score_by_symbol.get(symbol, 0.0)
            journal.record_order(
                symbol,
                "buy",
                f"NEW_POSITION score={score:.1f}",
                notional=notional,
                client_order_id=order.client_order_id,
            )

    logger.info(
        "Run complete: %d liquidations, %d buys planned.", len(to_liquidate), len(buy_orders)
    )
    return _finish(alerts)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        # Python's default excepthook writes the traceback to stderr only
        # -- munger.log (the copy that actually survives to next run via
        # M11's GitHub Actions persistence) would otherwise never record
        # why a run crashed (staff-engineer-reviewer finding). Logged
        # here, then re-raised so the process still exits non-zero.
        logger.exception("Run crashed with an unhandled exception")
        raise
