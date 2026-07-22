"""Orchestration entry point (DESIGN.md sections 4, 5, 8).

Ties every module together for one scheduled run: universe -> data ->
screener always run (screen-only mode never touches the broker); sells,
buys, and journaling only run if the kill switch is off and the
universe-fetch-fraction sanity check passes.
"""

from __future__ import annotations

import datetime
import logging

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


def _fetched_fraction(results: pd.DataFrame) -> float:
    """Fraction of screened tickers that got real data.

    1 - fraction tagged data_missing:fetch_failed (DESIGN.md 5). A
    half-empty screen would make every holding look delisted and
    trigger mass strikes.
    """
    if len(results) == 0:
        return 0.0
    fetch_failed = results["fail_reasons"].fillna("").str.contains("fetch_failed")
    return 1.0 - (fetch_failed.sum() / len(results))


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


def run(run_date: str | None = None) -> None:
    """Execute one full run.

    Returns normally on a clean run or a deliberate abort; does not
    raise for expected abort conditions (kill switch, budgets,
    data-fetch fraction) -- only for genuinely unexpected failures (e.g.
    a broken broker pre-check), which is left to propagate and crash the
    process per DESIGN.md's fail-closed philosophy for those specific
    checks.
    """
    journal.configure_logging()
    run_date = run_date or datetime.date.today().isoformat()
    logger.info("Starting run for %s (paper=%s)", run_date, config.PAPER_TRADING)

    tickers = universe.get_universe()
    results = screener.run_screen(tickers)
    journal.archive_screen_results(run_date)

    fetched_fraction = _fetched_fraction(results)
    if fetched_fraction < config.MIN_UNIVERSE_FETCH_FRACTION:
        logger.error(
            "Aborting: only %.1f%% of the universe fetched cleanly (need >= %.1f%%)",
            fetched_fraction * 100,
            config.MIN_UNIVERSE_FETCH_FRACTION * 100,
        )
        return

    buyable_count = int(results["buyable"].sum())
    logger.info("Screen complete: %d tickers, %d buyable.", len(results), buyable_count)

    if _kill_switch_active():
        logger.warning("KILL_SWITCH active -- screen-only run, no orders will be placed.")
        return

    exec_module = execution.ExecutionModule(run_date)
    exec_module.verify_account_access()

    current_holdings = exec_module.get_current_holdings()

    for warning in journal.check_reconciliation(set(current_holdings)):
        logger.warning("Reconciliation mismatch: %s", warning)

    holdings_metrics = data.fetch_all_metrics(list(current_holdings))
    state = portfolio.StateTracker()
    to_liquidate = _process_sells_if_data_is_healthy(current_holdings, holdings_metrics, state)

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
    planned_order_count = len(to_liquidate) + len(buy_orders)
    planned_buy_notional = sum(notional for _, notional in buy_orders)

    if planned_order_count > config.GLOBAL_ORDER_BUDGET:
        logger.error(
            "Aborting: planned %d orders exceeds GLOBAL_ORDER_BUDGET=%d",
            planned_order_count,
            config.GLOBAL_ORDER_BUDGET,
        )
        return
    notional_budget = portfolio_value * config.GLOBAL_NOTIONAL_BUDGET_PCT
    if planned_buy_notional > notional_budget:
        logger.error(
            "Aborting: planned buy notional $%.2f exceeds %.0f%% of equity ($%.2f)",
            planned_buy_notional,
            config.GLOBAL_NOTIONAL_BUDGET_PCT * 100,
            notional_budget,
        )
        return

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


if __name__ == "__main__":
    run()
