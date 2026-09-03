"""Orchestration entry point (DESIGN.md sections 4, 5, 8).

Ties every module together for one scheduled run: universe -> data ->
screener always run (screen-only mode never touches the broker); sells,
buys, and journaling only run if the kill switch is off and the
universe-fetch-fraction sanity check passes.
"""

from __future__ import annotations

import datetime
import functools
import logging
import sys
from collections.abc import Callable

import pandas as pd
from alpaca.trading.models import Order

import config
import data
import execution
import journal
import portfolio
import screener
import settlement
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
    alerts: list[str],
    corporate_action_check: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Run process_sells, unless too much of the holdings' data is missing.

    The universe-wide fetch-fraction abort (_fetched_fraction) only
    covers the buy-side screen; this second, independent fetch for
    currently-held tickers feeds process_sells's strike logic directly.
    Since M24, a missing ticker is not struck by process_sells itself
    (see its docstring), but a badly degraded fetch here (e.g. the same
    yfinance rate-limit cooldown from the first fetch still in effect)
    would otherwise flood every holding into `unresolved` at once --
    still worth aborting on rather than alerting on every single holding
    (staff-engineer-reviewer finding, pre-M24).

    M24 fix (staff-engineer-reviewer finding): this used to only
    logger.error() on the abort path, never call _alert() -- so a
    systemically degraded holdings fetch (arguably a worse signal than
    one unresolved ticker) silently exited 0 with no GH Actions
    annotation, while a single unresolved ticker (see process_sells)
    correctly alerts. Takes `alerts` now specifically to close that gap.
    """
    if not current_holdings:
        return [], [], []
    fetched = sum(1 for m in holdings_metrics.values() if m is not None)
    fraction = fetched / len(current_holdings)
    if fraction < config.MIN_UNIVERSE_FETCH_FRACTION:
        _alert(
            alerts,
            f"Skipping sell evaluation: only {fraction:.1%} of held tickers' data fetched "
            f"cleanly (need >= {config.MIN_UNIVERSE_FETCH_FRACTION:.1%}) -- treating as a "
            "data problem, not a quality failure",
        )
        return [], [], []
    return portfolio.process_sells(
        current_holdings, holdings_metrics, state, corporate_action_check
    )


def _reset_stale_strikes_for_tickers_no_longer_held(
    state: portfolio.StateTracker, current_holdings: portfolio.Holdings
) -> None:
    """Reset strikes and clear recorded holding state for any ticker no longer held.

    Staff-engineer-reviewer finding on M26c: strikes now reset only on a
    *confirmed* fill, but this run's synchronous settlement check can
    genuinely miss a fill that completes moments later -- nothing then
    ever revisits that specific order (a real deferred settlement pass
    is M34's job). Left unaddressed, the stale count sits in state.json
    forever, and if the same ticker is ever bought again, its first
    single bad quality check re-triggers immediate liquidation instead
    of a fresh streak -- silently defeating the two-strike discipline.

    The fix doesn't depend on ever resolving that specific order:
    current_holdings is always fetched live from the broker (this
    module's own long-standing rule), so a tracked ticker's absence from
    it is itself real, broker-confirmed evidence the position is gone --
    whatever Alpaca's order-status API says, or fails to say, about why.

    M29c follow-up: also clears a stale recorded HoldingState the same
    way, over the broader tracked_holding_state_tickers() list rather
    than tracked_tickers() -- a HEALTHY holding has zero strikes, so it
    would never appear in the strikes-only list, but its recorded state
    still needs clearing once sold, or report.py would keep showing a
    now-closed position as "Healthy" forever.
    """
    for ticker in state.tracked_tickers():
        if ticker not in current_holdings:
            state.reset_strikes(ticker)
    for ticker in state.tracked_holding_state_tickers():
        if ticker not in current_holdings:
            state.clear_holding_state(ticker)
    state.save()


def _reset_strikes(state: portfolio.StateTracker, symbol: str) -> None:
    """Reset one ticker's strike streak and persist immediately.

    A tiny named wrapper rather than an inline lambda body at each call
    site -- used as _settle_and_react's on_filled callback, which fires
    from inside a loop where saving right away (not batching) means a
    crash immediately after matters less: the reset that already
    happened is already durable.
    """
    state.reset_strikes(symbol)
    state.save()


def _settle_and_react(
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
        _alert(
            alerts,
            f"{symbol}: {order_kind} submitted but settlement query failed -- unconfirmed",
        )
        # M26d: fail closed on a genuine query failure -- set the same
        # kill-switch mechanism execute already checks, reusing it
        # rather than inventing new blocking behavior, plus a second
        # marker file recording that it was settlement (not a human)
        # that set it, so a later run can tell a stuck block apart from
        # a deliberate pause and escalate accordingly (see the
        # SETTLEMENT_BLOCKED check near the top of run()).
        config.KILL_SWITCH_FLAG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.KILL_SWITCH_FLAG_FILE_PATH.touch()
        config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.touch()
        return True
    _alert(
        alerts,
        f"{symbol}: {order_kind} submitted but not yet filled "
        f"(status={fill_status}) -- unconfirmed",
    )
    return False


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
            "GLOBAL_KILL_SWITCH active -- screen-only run (all accounts), no orders will be placed."
        )
        return _finish(alerts)

    if _kill_switch_active():
        # M26d (Design v2.2 §3.3): distinguish a deliberately-set kill
        # switch (routine, just a warning) from one a prior run's
        # settlement pass set after exhausting its query retries, which
        # has now survived into a new run without anyone clearing it --
        # that's a stuck block, and it gets a Critical alert every run
        # it persists, not just a routine log line, per the design's own
        # "a block that outlives one cycle is itself an alert" rule.
        if config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists():
            _alert(
                alerts,
                "CRITICAL: a settlement-pass block has persisted into this run -- a prior "
                "run's order status could not be confirmed after retrying, and this account "
                "has been screen-only ever since with nobody having cleared it. Verify the "
                "real broker state by hand, then remove both KILL_SWITCH and "
                "SETTLEMENT_BLOCKED to resume.",
            )
        else:
            logger.warning("KILL_SWITCH active -- screen-only run, no orders will be placed.")
        return _finish(alerts)

    # M24 fix: config.LIVE_TRADING_ENABLED previously gated only
    # report.py's real-money.html rendering -- it read like a live-
    # trading safety gate but wasn't one. The live workflow could place
    # real orders whenever Alpaca secrets were populated and neither
    # kill switch above was set, regardless of this flag. Paper is
    # unaffected: this only fires when PAPER_TRADING is false.
    if not config.PAPER_TRADING and not config.LIVE_TRADING_ENABLED:
        _alert(
            alerts,
            "Live mode requested but MUNGER_LIVE_TRADING_ENABLED is not set -- "
            "screen-only run, no orders placed.",
        )
        return _finish(alerts)

    exec_module = execution.ExecutionModule(run_date)
    exec_module.verify_account_access()

    current_holdings = exec_module.get_current_holdings()

    reconciliation_warnings = journal.check_reconciliation(set(current_holdings))
    for warning in reconciliation_warnings:
        _alert(alerts, f"Reconciliation mismatch: {warning}")
    if reconciliation_warnings:
        # M27 (Design v2.2 §3.3: "Reconciliation is authoritative... it
        # should have teeth") -- now aborts for BOTH accounts, not just
        # live. M20 originally carved out paper for a warn-and-continue
        # posture on the reasoning that live's defense-in-depth was
        # thinner; that reasoning didn't anticipate this being the exact
        # check that would have caught the real FOX/LPG bug (a paper-
        # account divergence) if it had been allowed to actually stop
        # the run instead of only logging. The alerts above have already
        # fired, so the mismatch is never silent either way; this just
        # stops the run from also placing orders on top of a position
        # picture it can't currently trust, for either account.
        _alert(alerts, "Aborting: reconciliation mismatch against current holdings")
        return _finish(alerts)

    holdings_metrics = data.fetch_all_metrics(list(current_holdings), phase="holdings check")
    state = portfolio.StateTracker()
    _reset_stale_strikes_for_tickers_no_longer_held(state, current_holdings)
    to_liquidate, unresolved, corporate_action = _process_sells_if_data_is_healthy(
        current_holdings, holdings_metrics, state, alerts, exec_module.is_corporate_action
    )
    if to_liquidate:
        _alert(alerts, f"Liquidation(s) this run: {', '.join(sorted(to_liquidate))}")
    if unresolved:
        # M24 fix: these holdings returned no data at all -- not struck,
        # not liquidated, but real enough to need a human look (see
        # process_sells's docstring for why data absence isn't treated
        # as a quality failure).
        _alert(
            alerts,
            f"Held position(s) returned no data, needs manual review: "
            f"{', '.join(sorted(unresolved))}",
        )
    if corporate_action:
        # M29 (Design v2.2 §3.2): actively confirmed via Alpaca's own
        # Assets API, distinct from a merely-unreadable ticker -- never
        # auto-traded, always a human decision.
        _alert(
            alerts,
            f"Held position(s) show a confirmed corporate action "
            f"(delisting/symbol change/merger?), needs manual review: "
            f"{', '.join(sorted(corporate_action))}",
        )

    # Caller contract from the M6/M7 review: a ticker slated for
    # liquidation this run must not also be topped up by the buy queue.
    # Same for a confirmed corporate action (M29): "never auto-traded"
    # applies to topping up just as much as to liquidating.
    remaining_holdings = {
        t: v
        for t, v in current_holdings.items()
        if t not in to_liquidate and t not in corporate_action
    }
    available_cash = exec_module.get_available_cash()
    # staff-engineer-reviewer finding: omitting corporate_action tickers
    # from remaining_holdings alone isn't enough -- generate_buy_queue's
    # new-position loop only checks current_holdings membership to avoid
    # a double-buy, so an absent-but-buyable corporate-action ticker
    # looked identical to "never held" and could be selected as a fresh
    # NEW_POSITION. Passed explicitly now.
    buy_orders = portfolio.generate_buy_queue(
        remaining_holdings, results, available_cash, exclude=set(corporate_action)
    )

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

    settlement_blocked = False
    for symbol in to_liquidate:
        order = exec_module.liquidate(symbol)
        if order is None:
            continue
        journal.record_order(
            symbol,
            "sell",
            f"SELL strikes={config.STRIKES_TO_LIQUIDATE}",
            client_order_id=order.client_order_id,
        )
        # M26c (Design v2.2 §3.3, RC3): the strike streak resets only on
        # a confirmed fill, never on submission alone -- the direct fix
        # for the real FOX/LPG bug (strikes were reset the moment
        # liquidation was *decided*, before the order ever confirmed
        # filling; the position was still held weeks later with no
        # memory a liquidation had ever been attempted). This checks
        # settlement synchronously, right after submission -- a
        # reasonable approximation given DAY limit orders against liquid
        # names often fill within seconds, but not a guarantee; a
        # pending/unconfirmed result here isn't lost, since the position
        # is still genuinely held and the next run's screen re-evaluates
        # it fresh. A real deferred settlement pass, decoupled from this
        # same run, is M34's job once cadences split.
        settlement_blocked = _settle_and_react(
            exec_module,
            alerts,
            symbol,
            "liquidation",
            order,
            on_filled=functools.partial(_reset_strikes, state, symbol),
        )
        if settlement_blocked:
            # staff-engineer-reviewer finding: setting the kill-switch
            # flag alone only takes effect on the *next* run -- without
            # stopping here too, the remaining liquidations and the
            # entire buy queue would still place orders in THIS run,
            # directly contradicting the reason the flag was just set
            # ("the alternative is trading on a position picture known
            # to be wrong"). One unconfirmed settlement halts everything
            # else this run was about to do.
            _alert(
                alerts,
                "Halting remaining liquidations and all buys this run -- "
                "settlement is unconfirmed, position picture can't be trusted",
            )
            break

    if not settlement_blocked:
        score_by_symbol = dict(zip(results["symbol"], results["score"], strict=True))
        for symbol, notional in buy_orders:
            order = exec_module.market_buy(symbol, notional)
            if order is None:
                continue
            score = score_by_symbol.get(symbol, 0.0)
            # M28 (Design v2.2 §3.3, RC3): derived from the actual
            # decision, not hardcoded -- every buy used to journal as
            # NEW_POSITION regardless of whether the symbol was already
            # held, which is exactly how six real top-ups (HIG x2, ASO,
            # LPG, HRMY x2) got recorded as new positions. A symbol
            # already in remaining_holdings going into
            # generate_buy_queue is a top-up by construction (that
            # function only ever tops up an existing holding or opens a
            # position for a symbol that wasn't one) -- checking
            # membership here doesn't require generate_buy_queue itself
            # to change its return shape.
            reason = (
                f"TOP_UP score={score:.1f}"
                if symbol in remaining_holdings
                else f"NEW_POSITION score={score:.1f}"
            )
            journal.record_order(
                symbol,
                "buy",
                reason,
                notional=notional,
                client_order_id=order.client_order_id,
            )
            # staff-engineer-reviewer finding: this used to discard
            # settle_order's return value entirely -- a query failure on
            # a buy's settlement check raised no alert, set no kill
            # switch, and left `fills` silently incomplete for that
            # client_order_id forever. Same treatment as the liquidation
            # side now: a genuine query failure halts the rest of this
            # run's buys too.
            if _settle_and_react(exec_module, alerts, symbol, "buy", order):
                _alert(
                    alerts,
                    "Halting remaining buys this run -- settlement is unconfirmed, "
                    "position picture can't be trusted",
                )
                break

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
