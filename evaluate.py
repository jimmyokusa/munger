"""Quarterly, read-only holdings evaluation (M34, Design v2.2 §3.1).

One of the three cadence-split workflows superseding bot.py's combined
daily evaluate+execute loop (see bot.py's own module docstring):

    Screen (daily, no broker)      -- daily_screen.py, unchanged
    Evaluate (quarterly, READ-ONLY) -- this module
    Execute (monthly, WRITE)        -- execute_trades.py

Evaluate's whole job is running M29a-c's holding-state machine against
currently-held tickers and deciding which ones fail badly enough to
liquidate -- it never places an order. A ticker decided for liquidation
is recorded as *pending* (portfolio.StateTracker.add_pending_liquidation)
for the next Execute window to actually act on; Evaluate's own run ends
without touching the broker's order-submission endpoints at all.

"Read-only" is a real distinction from Execute, not a rounding error
(§3.1's own staff-engineer-reviewer finding, carried forward here): this
module still calls the broker (get_current_holdings, is_corporate_action)
-- it cannot evaluate what it doesn't know is held -- but never
market_buy/liquidate. It gets its own workflow and its own account
credentials per the isolation rule below, the same as Execute; being
read-only is not an exemption from that isolation, only from the
write capability itself.

Fundamentals change quarterly, not daily -- evaluating them more often
than that manufactures noise from a signal that hasn't actually moved
(§3.1's own stated rationale for this cadence). Timed to run ~3 weeks
after quarter-end so fresh 10-Q data has had time to propagate through
data sources.

Does NOT run the universe screen (daily_screen.py already does that,
daily, and archives its own screen_results.csv) -- Evaluate only fetches
fresh metrics for tickers this account currently holds.
"""

from __future__ import annotations

import datetime
import logging
import sys
from collections.abc import Callable

import config
import data
import execution
import journal
import portfolio
import screener
import trading_common
from trading_common import alert as _alert

logger = logging.getLogger(__name__)


def _process_sells_if_data_is_healthy(
    current_holdings: portfolio.Holdings,
    holdings_metrics: dict[str, data.Metrics | None],
    state: portfolio.StateTracker,
    alerts: list[str],
    period: str,
    corporate_action_check: Callable[[str], bool] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Run process_sells, unless too much of the holdings' data is missing.

    Direct extraction from bot.py's own function of the same name/
    behavior (see that module's docstring on why bot.py itself is kept
    around, unmodified, rather than deleted) -- a badly degraded fetch
    for currently-held tickers (e.g. a yfinance rate-limit cooldown)
    would otherwise flood every holding into `unresolved` at once,
    worth aborting the whole evaluation on rather than alerting on every
    single holding.
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
        current_holdings, holdings_metrics, state, period, corporate_action_check
    )


def _reset_stale_strikes_for_tickers_no_longer_held(
    state: portfolio.StateTracker, current_holdings: portfolio.Holdings
) -> None:
    """Reset strikes and clear recorded holding state for any ticker no longer held.

    Direct extraction from bot.py's own function of the same name --
    current_holdings is always fetched live from the broker, so a
    tracked ticker's absence from it is itself real, broker-confirmed
    evidence the position is gone.
    """
    for ticker in state.tracked_tickers():
        if ticker not in current_holdings:
            state.reset_strikes(ticker)
    for ticker in state.tracked_holding_state_tickers():
        if ticker not in current_holdings:
            state.clear_holding_state(ticker)
    state.save()


def run(run_date: str | None = None) -> int:
    """Evaluate current holdings against the quality floor. Never places an order.

    Returns a process exit code (0 clean, 1 alert-worthy), matching
    bot.py's own return-code convention -- a non-zero exit marks the
    scheduling workflow's run as failed, the alert-delivery channel this
    project uses. Does not raise for expected abort/alert conditions
    (kill switch, reconciliation mismatch); only genuinely unexpected
    failures (e.g. a broken broker pre-check) propagate and crash the
    process.
    """
    alerts: list[str] = []
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting evaluate run for %s (paper=%s)", run_date, config.PAPER_TRADING)

    if trading_common.global_kill_switch_active():
        logger.warning(
            "GLOBAL_KILL_SWITCH active -- evaluate skipped (all accounts). "
            "Read-only, but the broker connection itself is withheld while paused."
        )
        return trading_common.finish(alerts)

    if trading_common.kill_switch_active():
        if config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists():
            _alert(
                alerts,
                "CRITICAL: a settlement-pass block has persisted into this run -- a prior "
                "Execute run's order status could not be confirmed after retrying. Verify the "
                "real broker state by hand, then remove both KILL_SWITCH and "
                "SETTLEMENT_BLOCKED to resume.",
            )
        else:
            logger.warning("KILL_SWITCH active -- evaluate skipped.")
        return trading_common.finish(alerts)

    if not config.PAPER_TRADING and not config.LIVE_TRADING_ENABLED:
        _alert(
            alerts,
            "Live mode requested but MUNGER_LIVE_TRADING_ENABLED is not set -- evaluate skipped.",
        )
        return trading_common.finish(alerts)

    exec_module = execution.ExecutionModule(run_date)
    exec_module.verify_account_access()

    # Deliberately no "if not current_holdings: return early" shortcut
    # here (a real bug caught by this module's own tests): an empty
    # holdings dict is a legitimate, common state -- e.g. right after a
    # full liquidation -- and reconciliation plus the stale-strike/
    # holding-state reset below still need to run against it, or a
    # ticker that just sold off keeps a stale nonzero strike count and
    # recorded HoldingState forever. process_sells itself already
    # no-ops cleanly on an empty current_holdings (its `for ticker in
    # current_holdings` loop simply doesn't iterate), so there's no
    # separate fast path needed for this case.
    current_holdings = exec_module.get_current_holdings()

    reconciliation_warnings = journal.check_reconciliation(set(current_holdings))
    for warning in reconciliation_warnings:
        _alert(alerts, f"Reconciliation mismatch: {warning}")
    if reconciliation_warnings:
        # Same "reconciliation is authoritative" rule as Execute (§3.3) --
        # Evaluate places no orders, so the risk isn't trading on a wrong
        # position picture, but classifying holdings that don't actually
        # match what the broker reports is meaningless and could record
        # a strike (or a pending liquidation) against a state that isn't
        # real.
        _alert(alerts, "Aborting evaluation: reconciliation mismatch against current holdings")
        return trading_common.finish(alerts)

    holdings_metrics = screener.fetch_metrics_with_xbrl_primary(
        list(current_holdings), phase="holdings check"
    )
    state = portfolio.StateTracker()
    _reset_stale_strikes_for_tickers_no_longer_held(state, current_holdings)
    period = portfolio.period_identifier(run_date)
    to_liquidate, unresolved, corporate_action = _process_sells_if_data_is_healthy(
        current_holdings,
        holdings_metrics,
        state,
        alerts,
        period,
        exec_module.is_corporate_action,
    )

    # The handoff to Execute (M34, §3.1): Evaluate decides, Execute acts.
    # Additive, not a replacement -- a ticker already pending from a
    # prior Evaluate run that Execute hasn't gotten to yet stays pending
    # alongside any newly-decided ones this run.
    for ticker in to_liquidate:
        state.add_pending_liquidation(ticker)

    # Staff-engineer-reviewer finding: the reverse direction was missing
    # -- a ticker pending from an earlier quarter's liquidation decision
    # that recovers to HEALTHY by this run (fundamentals genuinely
    # improved before Execute ever got to it) stayed pending forever,
    # since only additions existed, never a removal on reclassification.
    # Execute would then liquidate a position this run's own, fresher
    # classification says is fine.
    #
    # Gated on the identical fetch-fraction check
    # _process_sells_if_data_is_healthy applies internally, not just "did
    # process_sells run at all" -- when that gate skips evaluation for
    # bad data, state.get_holding_state() would otherwise reflect a
    # *prior* run's classification (process_sells never touched it this
    # run), and clearing a pending liquidation off stale data would be
    # exactly the kind of "absence of fresh evidence treated as a
    # trading signal" this whole state machine exists to prevent.
    if current_holdings:
        fetched = sum(1 for m in holdings_metrics.values() if m is not None)
        fraction = fetched / len(current_holdings)
        if fraction >= config.MIN_UNIVERSE_FETCH_FRACTION:
            for ticker in current_holdings:
                if state.get_holding_state(ticker) is portfolio.HoldingState.HEALTHY:
                    state.remove_pending_liquidation(ticker)
    state.save()

    if to_liquidate:
        _alert(
            alerts,
            f"Decided to liquidate (awaiting the next Execute window): "
            f"{', '.join(sorted(to_liquidate))}",
        )
    if unresolved:
        _alert(
            alerts,
            f"Held position(s) returned no data, needs manual review: "
            f"{', '.join(sorted(unresolved))}",
        )
    if corporate_action:
        _alert(
            alerts,
            f"Held position(s) show a confirmed corporate action "
            f"(delisting/symbol change/merger?), needs manual review: "
            f"{', '.join(sorted(corporate_action))}",
        )

    logger.info(
        "Evaluate complete: %d ticker(s) newly decided for liquidation, %d pending overall.",
        len(to_liquidate),
        len(state.pending_liquidations()),
    )
    return trading_common.finish(alerts)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        logger.exception("Evaluate run crashed with an unhandled exception")
        raise
