"""Monthly, order-placing trade execution (M34, Design v2.2 §3.1).

One of the three cadence-split workflows superseding bot.py's combined
daily evaluate+execute loop (see bot.py's own module docstring):

    Screen (daily, no broker)      -- daily_screen.py, unchanged
    Evaluate (quarterly, read-only) -- evaluate.py
    Execute (monthly, WRITE)        -- this module

Execute is the only cadence that can submit an order. Two independent
jobs each run:

1. **Liquidate whatever Evaluate has decided.** Reads
   portfolio.StateTracker.pending_liquidations() -- tickers Evaluate
   classified as failing the quality floor two consecutive periods,
   awaiting an actual sell order. A ticker stays pending across Execute
   windows until its liquidation is confirmed filled (matching §3.3's
   "unfilled orders are surfaced, not forgotten" rule one level up in
   cadence) -- Execute does not re-run the holding-state machine itself;
   that judgment already happened in Evaluate.

2. **Build and place the buy queue.** Runs its own universe screen (this
   workflow's own GitHub Actions runner has no access to daily_screen.py's
   separately-deployed Cloud Run/k3s filesystem, so it screens fresh here,
   the same way bot.py always has -- daily_screen.py's daily screen exists
   to feed the site, not to hand a file to this workflow) and constructs
   the buy queue from the result, same priority/budget rules bot.py
   always used.

Bounded, reviewable, infrequent -- batching real capital moves into one
monthly window rather than a running daily stream, per §3.1's own
rationale (real turnover discipline over an always-on rebalancer).
"""

from __future__ import annotations

import datetime
import logging
import sys

import pandas as pd

import config
import execution
import journal
import portfolio
import screener
import trading_common
import universe
from trading_common import alert as _alert

logger = logging.getLogger(__name__)


def _fetched_fraction(results: pd.DataFrame) -> float:
    """Thin delegate to screener.fetched_fraction (single source of truth).

    Direct extraction from bot.py's own function of the same name/
    behavior -- keeps this workflow's fetch-quality gate from drifting
    against the screen-only daily path's identical check.
    """
    return screener.fetched_fraction(results)


def _reset_strikes(state: portfolio.StateTracker, symbol: str) -> None:
    """Reset one ticker's strike streak and persist immediately.

    Direct extraction from bot.py's own function of the same name --
    used as trading_common.settle_and_react's on_filled callback.
    """
    state.reset_strikes(symbol)
    state.save()


def run(run_date: str | None = None) -> int:
    """Execute one liquidate+buy window. Returns a process exit code (0 clean, 1 alert-worthy).

    Does not raise for expected abort/alert conditions (kill switch,
    budgets, data-fetch fraction, universe fallback, reconciliation
    mismatches) -- those are collected and reported via the return code.
    Only genuinely unexpected failures propagate and crash the process.
    """
    alerts: list[str] = []
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting execute run for %s (paper=%s)", run_date, config.PAPER_TRADING)

    stale_hours = trading_common.check_data_freshness()
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
        return trading_common.finish(alerts)

    buyable_count = int(results["buyable"].sum())
    logger.info("Screen complete: %d tickers, %d buyable.", len(results), buyable_count)

    if trading_common.global_kill_switch_active():
        logger.warning(
            "GLOBAL_KILL_SWITCH active -- screen-only run (all accounts), no orders will be placed."
        )
        return trading_common.finish(alerts)

    if trading_common.kill_switch_active():
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
        return trading_common.finish(alerts)

    if not config.PAPER_TRADING and not config.LIVE_TRADING_ENABLED:
        _alert(
            alerts,
            "Live mode requested but MUNGER_LIVE_TRADING_ENABLED is not set -- "
            "screen-only run, no orders placed.",
        )
        return trading_common.finish(alerts)

    exec_module = execution.ExecutionModule(run_date)
    exec_module.verify_account_access()

    current_holdings = exec_module.get_current_holdings()

    reconciliation_warnings = journal.check_reconciliation(set(current_holdings))
    for warning in reconciliation_warnings:
        _alert(alerts, f"Reconciliation mismatch: {warning}")
    if reconciliation_warnings:
        # M27 (Design v2.2 §3.3: "Reconciliation is authoritative") --
        # aborts both accounts, same as bot.py's own version of this
        # check. The alerts above have already fired, so the mismatch is
        # never silent either way; this just stops the run from also
        # placing orders on top of a position picture it can't currently
        # trust.
        _alert(alerts, "Aborting: reconciliation mismatch against current holdings")
        return trading_common.finish(alerts)

    # Independent, live safety net (§3.2): a holding can enter a
    # corporate action at any point between Evaluate windows -- Execute
    # re-checks every current holding immediately before deciding what's
    # eligible for a sell OR a top-up, rather than trusting Evaluate's
    # potentially-months-stale classification for this one purpose.
    # Never auto-traded either direction, same rule as Evaluate's own.
    # Computed BEFORE to_liquidate (staff-engineer-reviewer finding: an
    # earlier version only excluded corporate_action from the *buy*
    # side, so a ticker Evaluate had already queued for liquidation
    # before it entered a corporate action would still get sold here --
    # directly violating "never auto-traded, always a human decision").
    corporate_action = sorted(t for t in current_holdings if exec_module.is_corporate_action(t))
    if corporate_action:
        _alert(
            alerts,
            f"Held position(s) show a confirmed corporate action "
            f"(delisting/symbol change/merger?), needs manual review: "
            f"{', '.join(corporate_action)}",
        )

    state = portfolio.StateTracker()
    pending = set(state.pending_liquidations())
    to_liquidate = sorted(t for t in pending if t in current_holdings and t not in corporate_action)
    deferred_for_corporate_action = pending & set(corporate_action)
    if deferred_for_corporate_action:
        # Stays pending, not cleared -- the quality-failure reason
        # Evaluate originally decided this on may well still be valid
        # once the corporate action resolves; this run just isn't the
        # one that gets to act on it.
        _alert(
            alerts,
            f"Liquidation deferred (confirmed corporate action, needs a human decision "
            f"first): {', '.join(sorted(deferred_for_corporate_action))}",
        )
    stale_pending = pending - current_holdings.keys()
    if stale_pending:
        # A pending liquidation for a ticker no longer actually held --
        # it must have sold, delisted, or otherwise left the portfolio
        # by some path this run didn't cause. Clear it rather than retry
        # a sell against a position that doesn't exist.
        for ticker in stale_pending:
            state.remove_pending_liquidation(ticker)
        state.save()
        logger.info(
            "Cleared %d stale pending liquidation(s) no longer actually held: %s",
            len(stale_pending),
            ", ".join(sorted(stale_pending)),
        )

    if to_liquidate:
        _alert(alerts, f"Liquidation(s) this run: {', '.join(to_liquidate)}")

    remaining_holdings = {
        t: v
        for t, v in current_holdings.items()
        if t not in to_liquidate and t not in corporate_action
    }
    available_cash = exec_module.get_available_cash()
    buy_orders = portfolio.generate_buy_queue(
        remaining_holdings, results, available_cash, exclude=set(corporate_action)
    )

    portfolio_value = available_cash + sum(remaining_holdings.values())
    buy_orders, deferred_symbols, bound_budgets = trading_common.cap_buy_orders_to_budget(
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

        def _on_filled(sym: str = symbol) -> None:
            _reset_strikes(state, sym)
            state.remove_pending_liquidation(sym)
            state.save()

        settlement_blocked = trading_common.settle_and_react(
            exec_module, alerts, symbol, "liquidation", order, on_filled=_on_filled
        )
        if settlement_blocked:
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
            if trading_common.settle_and_react(exec_module, alerts, symbol, "buy", order):
                _alert(
                    alerts,
                    "Halting remaining buys this run -- settlement is unconfirmed, "
                    "position picture can't be trusted",
                )
                break

    logger.info(
        "Run complete: %d liquidations, %d buys planned, %d still pending for next window.",
        len(to_liquidate),
        len(buy_orders),
        len(state.pending_liquidations()),
    )
    return trading_common.finish(alerts)


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception:
        logger.exception("Execute run crashed with an unhandled exception")
        raise
