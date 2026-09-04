"""Orchestration entry point (DESIGN.md sections 4, 5, 8).

Ties every module together for one scheduled run: universe -> data ->
screener always run (screen-only mode never touches the broker); sells,
buys, and journaling only run if the kill switch is off and the
universe-fetch-fraction sanity check passes.

M34 (Design v2.2 §3.1): this combined daily evaluate+execute loop is
superseded by the cadence-split trio -- daily_screen.py (unchanged,
already screen-only), evaluate.py (quarterly, read-only holdings
evaluation), and execute_trades.py (monthly, liquidations + buys). Kept
working and unmodified in behavior (not deleted) as a documented
fallback until the split workflows are actually cut over in production
-- see TASKS.md's M34 section for why the cutover itself is a deliberate
decision, not an automatic one, and evaluate.py's own module docstring
for the full cadence rationale. Its own kill-switch/settlement/budget
helpers now import from trading_common.py rather than duplicating them,
so a future safety-logic fix can't land in only one of bot.py/
evaluate.py/execute_trades.py by accident.
"""

from __future__ import annotations

import datetime
import functools
import logging
import sys
from collections.abc import Callable

import pandas as pd

import config
import data
import execution
import journal
import portfolio
import screener
import universe
from trading_common import alert as _alert
from trading_common import cap_buy_orders_to_budget as _cap_buy_orders_to_budget
from trading_common import check_data_freshness as _check_data_freshness
from trading_common import finish as _finish
from trading_common import global_kill_switch_active as _global_kill_switch_active
from trading_common import kill_switch_active as _kill_switch_active
from trading_common import settle_and_react as _settle_and_react

# mypy strict (no_implicit_reexport) only treats an imported name as
# explicitly re-exported via `__all__` or a same-name `as` alias -- a
# renamed alias like the ones above (kill_switch_active as
# _kill_switch_active) doesn't qualify on its own. These four are
# accessed externally as bot._name by tests that must patch bot's own
# bound reference specifically (not trading_common's), since bot.py's
# functions resolve the name in bot's namespace at call time -- patching
# trading_common's copy instead wouldn't affect bot.run() and would also
# leak into evaluate.py/execute_trades.py's unrelated tests, which import
# the same trading_common functions independently. Listed here, not
# worked around per call site, since that's the direct fix for what
# mypy is actually flagging: these names are genuinely, deliberately
# part of bot.py's externally-touched surface, even though private.
__all__ = [
    "_cap_buy_orders_to_budget",
    "_check_data_freshness",
    "_global_kill_switch_active",
    "_kill_switch_active",
]

logger = logging.getLogger(__name__)


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
    period: str,
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

    `period` (M35, Design v2.2 §3.1): bot.py runs daily (unlike
    evaluate.py's own quarterly cadence), but `portfolio.period_identifier`
    is calendar-quarter based, so repeated daily runs within the same
    quarter naturally collapse to one strike per quarter here too --
    add_strike's own per-period idempotency, not a special case bot.py
    needs to implement itself.
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
