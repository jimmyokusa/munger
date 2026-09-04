"""Unit tests for bot.py's orchestration logic (DESIGN.md sections 4, 5, 8).

Every external module call is mocked -- this tests bot.py's own wiring
(the right calls happen in the right order with the right arguments and
the right abort conditions), not each module's internal correctness,
which is already covered by their own test files. Monkeypatches the real
modules directly (not via bot.universe etc.) since bot.py imports them
by module (`import universe`), so patching the module object itself is
visible through bot.py's reference too -- and keeps mypy's implicit-
reexport check happy, the same pattern used in data.py's own tests.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Order

import bot
import config
import data
import execution
import journal
import portfolio
import screener
import universe
import xbrl


def _fake_filled_order(symbol: str = "AAPL") -> MagicMock:
    order = MagicMock(spec=Order)
    order.status = OrderStatus.FILLED
    order.symbol = symbol
    order.filled_qty = 1.0
    order.filled_avg_price = 100.0
    return order


def _screen_results(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "buyable", "score", "fail_reasons"])


def _clean_results() -> pd.DataFrame:
    return _screen_results(
        [
            {"symbol": "HIGH", "buyable": True, "score": 90.0, "fail_reasons": ""},
            {"symbol": "LOW", "buyable": True, "score": 50.0, "fail_reasons": ""},
        ]
    )


class _FakeExecutionModule:
    """Stand-in for execution.ExecutionModule, monkeypatched onto execution."""

    def __init__(self, run_date: str) -> None:
        self.run_date = run_date
        self.verify_account_access = MagicMock()
        self.get_current_holdings = MagicMock(return_value={})
        self.get_available_cash = MagicMock(return_value=100_000.0)
        self.liquidate = MagicMock(return_value=MagicMock(client_order_id="liq-id"))
        self.market_buy = MagicMock(return_value=MagicMock(client_order_id="buy-id"))
        # M26c: settlement.settle_order calls this after every liquidate/
        # market_buy -- defaults to an immediate clean fill so existing
        # tests exercise the intended "confirmed fill" path, not an
        # AttributeError-as-query-failure (which settle_order's broad
        # except Exception would otherwise silently swallow as a real
        # broker failure, burning real retry-backoff sleep time).
        self.get_order_status = MagicMock(return_value=_fake_filled_order())
        # M29b: passed to process_sells as corporate_action_check --
        # defaults to "never a corporate action" so existing tests get
        # the pre-M29 behavior unless a test explicitly overrides it.
        self.is_corporate_action = MagicMock(return_value=False)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # M37: default to XBRL-primary being a no-op passthrough -- without
    # this, every screener.run_screen/fetch_metrics_with_xbrl_primary
    # call in this file would fall through to a real
    # xbrl.apply_primary_metrics, which attempts a real network call to
    # SEC EDGAR (degrades safely on failure, but still reaches out
    # during a test run, which this project's testing discipline
    # forbids). Tests that want to exercise the override itself
    # monkeypatch xbrl.apply_primary_metrics again, locally.
    monkeypatch.setattr(xbrl, "apply_primary_metrics", lambda metrics_by_symbol: metrics_by_symbol)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    # Without this, tests would check the *real* repo-root path -- which
    # doesn't have this file today, so it happens to read False, but any
    # test run from a checkout that ever did create one (or a future
    # regression) would otherwise silently flip every test in this file
    # to a screen-only run instead of the behavior each test expects.
    monkeypatch.setattr(
        config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "GLOBAL_KILL_SWITCH"
    )
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    (tmp_path / "screen_results.csv").write_text("symbol,buyable,score,fail_reasons\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    # M26b: no real sleeping on a settlement query retry in tests.
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_BACKOFF_SECONDS", 0.0)
    # M26d: same reasoning as KILL_SWITCH_FLAG_FILE_PATH above -- without
    # this, a test that genuinely exercises the settlement query-failure
    # path would touch a real file in the real repo-root-relative
    # DATA_DIR, not a throwaway tmp_path.
    monkeypatch.setattr(
        config, "SETTLEMENT_BLOCKED_FLAG_FILE_PATH", tmp_path / "SETTLEMENT_BLOCKED"
    )


def test_kill_switch_active_via_config_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    assert bot._kill_switch_active() is True


def test_kill_switch_active_via_flag_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_path = tmp_path / "KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert bot._kill_switch_active() is True


def test_global_kill_switch_inactive_by_default() -> None:
    assert bot._global_kill_switch_active() is False


def test_global_kill_switch_active_via_flag_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag_path = tmp_path / "GLOBAL_KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert bot._global_kill_switch_active() is True


# --- _cap_buy_orders_to_budget ---


def test_cap_buy_orders_to_budget_passes_through_when_under_budget() -> None:
    orders = [("A", 100.0), ("B", 200.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == orders
    assert deferred == []
    assert bound_budgets == []


def test_cap_buy_orders_to_budget_defers_past_the_order_count_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 2)
    orders = [("A", 100.0), ("B", 100.0), ("C", 100.0)]
    # 1 liquidation reserves a slot, leaving room for exactly 1 buy order.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=1, portfolio_value=10_000.0
    )
    assert capped == [("A", 100.0)]
    assert deferred == ["B", "C"]
    assert bound_budgets == ["order-count"]


def test_cap_buy_orders_to_budget_defers_past_the_notional_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.25)
    orders = [("A", 2_000.0), ("B", 2_000.0)]
    # Budget is 25% of $10,000 = $2,500: A fits, A+B ($4,000) doesn't.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == [("A", 2_000.0)]
    assert deferred == ["B"]
    assert bound_budgets == ["notional"]


def test_cap_buy_orders_to_budget_never_truncates_liquidations() -> None:
    # Liquidations aren't passed through this function at all -- it only
    # ever sees/returns buy orders -- confirmed here via a liquidation
    # count large enough to consume the entire order budget on its own,
    # every buy order still comes back deferred, not silently dropped.
    orders = [("A", 100.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=config.GLOBAL_ORDER_BUDGET, portfolio_value=10_000.0
    )
    assert capped == []
    assert deferred == ["A"]
    assert bound_budgets == ["order-count"]


def test_cap_buy_orders_to_budget_preserves_priority_order_past_the_notional_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: an earlier version used "skip and
    # continue" when an order didn't fit the notional budget, so a large
    # higher-priority order could get deferred while a smaller
    # lower-priority order after it still got bought -- silently
    # reordering execution relative to the queue's own priority (top-ups
    # before new positions, then score rank). A big order that doesn't
    # fit must defer itself *and* everything after it, not let a smaller
    # later order jump ahead of it.
    # Budget is 21% of $10,000 = $2,100: TOPUP_HIGH_PRIORITY ($2,000) fits
    # alone, but TOPUP_HIGH_PRIORITY + NEW_LOW_PRIORITY ($2,400) does not.
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.21)
    orders = [("TOPUP_HIGH_PRIORITY", 2_000.0), ("NEW_LOW_PRIORITY", 400.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == [("TOPUP_HIGH_PRIORITY", 2_000.0)]
    assert deferred == ["NEW_LOW_PRIORITY"]
    assert bound_budgets == ["notional"]


def test_cap_buy_orders_to_budget_reports_both_bound_budgets() -> None:
    orders = [("A", 100.0), ("B", 100.0), ("C", 10_000.0)]
    # liquidation_count reserves all but 2 buy slots -> order-count budget
    # excludes C before notional is even considered. Of what's left (A,
    # B), a $150 notional budget (25% of $600) admits A but not A+B ($200)
    # -> notional budget also binds, independently, on B. Both should be
    # reported.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=config.GLOBAL_ORDER_BUDGET - 2, portfolio_value=600.0
    )
    assert capped == [("A", 100.0)]
    assert deferred == ["B", "C"]
    assert set(bound_budgets) == {"order-count", "notional"}


def test_kill_switch_inactive_by_default() -> None:
    assert bot._kill_switch_active() is False


def test_fetched_fraction_all_clean() -> None:
    results = _screen_results(
        [{"symbol": "A", "buyable": True, "score": 1.0, "fail_reasons": "graham_pe"}]
    )
    assert bot._fetched_fraction(results) == 1.0


def test_fetched_fraction_some_missing() -> None:
    results = _screen_results(
        [
            {
                "symbol": "A",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
            {"symbol": "B", "buyable": True, "score": 1.0, "fail_reasons": ""},
        ]
    )
    assert bot._fetched_fraction(results) == pytest.approx(0.5)


def test_fetched_fraction_empty_results() -> None:
    assert bot._fetched_fraction(_screen_results([])) == 0.0


def test_run_screen_only_when_kill_switch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []  # never touches the broker at all
    assert exit_code == 0  # kill switch is intentional, not itself alert-worthy


def test_run_screen_only_when_global_kill_switch_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M20 (DESIGN_REAL_MONEY.md §3.2): checked before the per-account
    # switch, so this must halt the run even with the per-account switch
    # left off (config.KILL_SWITCH is already False via the autouse
    # fixture, unlike the per-account test above).
    flag_path = tmp_path / "GLOBAL_KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []  # never touches the broker at all
    assert exit_code == 0  # kill switch is intentional, not itself alert-worthy


def test_run_aborts_before_kill_switch_check_when_fetch_fraction_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    bad_results = _screen_results(
        [
            {
                "symbol": "A",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
            {
                "symbol": "B",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
        ]
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: bad_results)
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 1  # an abort is alert-worthy


def test_run_full_happy_path_places_liquidations_and_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: (["LIQUIDATE_ME"], [], []),
    )

    captured_buy_queue_holdings: dict[str, float] = {}

    def _fake_generate_buy_queue(
        holdings: dict[str, float],
        results: pd.DataFrame,
        cash: float,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        captured_buy_queue_holdings.update(holdings)
        return [("HIGH", 5000.0)]

    monkeypatch.setattr(portfolio, "generate_buy_queue", _fake_generate_buy_queue)

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(
        return_value={"LIQUIDATE_ME": 1000.0, "KEEP": 2000.0}
    )
    recorded_orders: list[tuple[str, str, str]] = []

    def _fake_record_order(symbol: str, side: str, reason: str, **kwargs: Any) -> None:
        recorded_orders.append((symbol, side, reason))

    monkeypatch.setattr(journal, "record_order", _fake_record_order)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    fake_exec.liquidate.assert_called_once_with("LIQUIDATE_ME")
    fake_exec.market_buy.assert_called_once_with("HIGH", 5000.0)
    # Caller contract from the M6/M7 review: the liquidated ticker must
    # not appear in what's passed to generate_buy_queue.
    assert "LIQUIDATE_ME" not in captured_buy_queue_holdings
    assert "KEEP" in captured_buy_queue_holdings
    assert (
        "LIQUIDATE_ME",
        "sell",
        f"SELL strikes={config.STRIKES_TO_LIQUIDATE}",
    ) in recorded_orders
    assert ("HIGH", "buy", "NEW_POSITION score=90.0") in recorded_orders  # genuinely new
    assert exit_code == 1  # a liquidation occurred this run -- always alert-worthy


def test_run_journals_a_top_up_buy_distinctly_from_a_new_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M28 (Design v2.2 §3.3, RC3): the real bug -- every buy journaled
    # as NEW_POSITION regardless of whether the symbol was already
    # held, which is how six real top-ups got recorded as new
    # positions. "KEEP" is already held going into this run and gets
    # topped up; "NEW" is opened fresh -- the journal must distinguish
    # them.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["KEEP", "NEW"]),
    )
    screen = _screen_results(
        [
            {"symbol": "KEEP", "buyable": True, "score": 70.0, "fail_reasons": ""},
            {"symbol": "NEW", "buyable": True, "score": 85.0, "fail_reasons": ""},
        ]
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: screen)
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("KEEP", 500.0), ("NEW", 3000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"KEEP": 2000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    recorded_orders: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        journal,
        "record_order",
        lambda symbol, side, reason, **kwargs: recorded_orders.append((symbol, side, reason)),
    )

    bot.run(run_date="2026-07-21")

    assert ("KEEP", "buy", "TOP_UP score=70.0") in recorded_orders
    assert ("NEW", "buy", "NEW_POSITION score=85.0") in recorded_orders
    # Never the old hardcoded behavior for the top-up.
    assert not any(
        symbol == "KEEP" and "NEW_POSITION" in reason for symbol, _, reason in recorded_orders
    )


def test_run_alerts_on_unreadable_holdings_without_selling_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M24 fix: a held position that returned no data must be surfaced
    # for manual review, not silently struck toward liquidation or
    # silently ignored either.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], ["ACQD"], []),
    )
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"ACQD": 1000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.liquidate.assert_not_called()
    assert exit_code == 1  # alert-worthy, even though nothing was sold


def test_run_alerts_on_corporate_action_without_selling_or_topping_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M29 (Design v2.2 §3.2): a confirmed corporate action is never
    # auto-traded, in either direction -- not liquidated (same as
    # UNREADABLE) but also not topped up (unlike UNREADABLE, since a
    # confirmed corporate action is a stronger, more specific signal
    # than a merely-unreadable ticker, distinct enough that this test
    # exists separately from the unresolved-holdings test above).
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], ["MRGD"]),
    )
    captured_buy_queue_holdings: dict[str, float] = {}
    captured_exclude: set[str] | None = None

    def _fake_generate_buy_queue(
        holdings: dict[str, float],
        results: pd.DataFrame,
        cash: float,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        nonlocal captured_exclude
        captured_buy_queue_holdings.update(holdings)
        captured_exclude = exclude
        return []

    monkeypatch.setattr(portfolio, "generate_buy_queue", _fake_generate_buy_queue)

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"MRGD": 1000.0, "KEEP": 2000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.liquidate.assert_not_called()
    fake_exec.market_buy.assert_not_called()  # nothing to buy in this test, but confirms no crash
    assert "MRGD" not in captured_buy_queue_holdings  # never topped up either
    assert "KEEP" in captured_buy_queue_holdings  # an unrelated holding is unaffected
    # staff-engineer-reviewer finding: omission from current_holdings
    # alone isn't enough -- generate_buy_queue must also receive an
    # explicit exclude set, or a corporate-action ticker that's also
    # `buyable=True` in this run's screen could be selected as a fresh
    # NEW_POSITION purchase.
    assert captured_exclude == {"MRGD"}
    assert exit_code == 1  # alert-worthy


def test_run_reproduces_the_fox_lpg_shape_and_does_not_reset_strikes_on_an_unfilled_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # M26c regression test (Design v2.2 §3.3, RC3): reproduces the exact
    # real FOX/LPG failure shape end to end, with the REAL
    # portfolio.process_sells and portfolio.StateTracker (not mocked, as
    # every other test in this file uses) -- an order is submitted and
    # journaled, but the broker never confirms it filled. Before this
    # fix, strikes were reset the moment liquidation was *decided*
    # (inside process_sells), so the position's state.json entry was
    # wiped even though the order never actually filled -- exactly what
    # happened for real on 2026-08-08.
    state_path = tmp_path / "state.json"
    # StateTracker's default path is bound at class-definition time
    # (config.STATE_FILE_PATH read once, not re-read per call), so
    # redirecting it for this test means replacing the constructor
    # bot.run() calls, not patching the config value it already
    # captured -- the same reason every other test in this file that
    # wants the real StateTracker's behavior does this via
    # portfolio.StateTracker, never via config.STATE_FILE_PATH alone.
    _real_state_tracker = portfolio.StateTracker  # captured before patching -- avoid self-reference
    monkeypatch.setattr(portfolio, "StateTracker", lambda: _real_state_tracker(path=state_path))
    # One strike short of the threshold -- this run's failing check
    # pushes FOX over the line, so process_sells decides to liquidate.
    # M35: v2 schema, strikes as a list of distinct periods (not an int
    # count) -- config.STRIKES_TO_LIQUIDATE - 1 already-struck periods,
    # none of them the one this run's own period_identifier(run_date)
    # will compute, so this run's strike is a genuinely new period.
    existing_periods = [f"period-{i}" for i in range(config.STRIKES_TO_LIQUIDATE - 1)]
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "strikes": {"FOX": existing_periods},
                "holding_states": {},
                "pending_liquidations": [],
            }
        )
    )

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["FOX"])
    )
    monkeypatch.setattr(
        screener,
        "run_screen",
        lambda tickers: _screen_results(
            [{"symbol": "FOX", "buyable": False, "score": 0.0, "fail_reasons": ""}]
        ),
    )
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    failing_metrics = data.Metrics(
        symbol="FOX",
        market_cap=1_000_000_000.0,
        trailing_pe=10.0,
        price_to_book=1.0,
        current_ratio=2.0,
        debt_to_equity=0.5,
        return_on_equity=0.02,  # fails MIN_ROE -- a real quality floor failure
        gross_margin=0.3,
        operating_margin=0.1,
        free_cash_flow=1_000_000.0,
        dividend_yield=0.0,
        consecutive_positive_earnings_years=3,
    )
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"FOX": failing_metrics}
    )
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    fake_exec = _FakeExecutionModule("2026-08-08")
    fake_exec.get_current_holdings = MagicMock(return_value={"FOX": 6667.16})
    # The real FOX order: submitted, but the broker never confirms a
    # fill -- reported as still open (NEW) every time settlement polls
    # it, the same as a DAY limit order that's genuinely still pending.
    unfilled_order = MagicMock(spec=Order)
    unfilled_order.status = OrderStatus.NEW
    unfilled_order.symbol = "FOX"
    unfilled_order.filled_qty = None
    unfilled_order.filled_avg_price = None
    fake_exec.get_order_status = MagicMock(return_value=unfilled_order)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    recorded_orders: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        journal,
        "record_order",
        lambda symbol, side, reason, **kwargs: recorded_orders.append((symbol, side, reason)),
    )

    exit_code = bot.run(run_date="2026-08-08")

    # The order was submitted and journaled -- this part of the old
    # behavior was already correct and stays correct.
    fake_exec.liquidate.assert_called_once_with("FOX")
    assert any(symbol == "FOX" and side == "sell" for symbol, side, _ in recorded_orders)

    # The bug, fixed: strikes must NOT be reset just because liquidation
    # was decided and an order submitted -- the order never confirmed
    # filling. A future run must still see FOX at (or past) the
    # threshold, not a wiped-clean streak that would let a genuinely
    # still-held, still-failing position quietly stop being evaluated
    # as urgent.
    final_state = _real_state_tracker(path=state_path)
    assert final_state.get_strikes("FOX") >= config.STRIKES_TO_LIQUIDATE

    # And it's alert-worthy -- an unconfirmed liquidation is exactly the
    # kind of thing this fix exists to surface, not silently swallow.
    assert exit_code == 1


def test_run_sets_kill_switch_and_settlement_blocked_on_a_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M26d (Design v2.2 §3.3): a genuine settlement query failure (not
    # merely a pending order) fails closed -- both the existing
    # KILL_SWITCH mechanism (so the *next* run doesn't place orders on a
    # position picture this run couldn't verify) and a second marker
    # file recording that it was settlement, not a human, that set it.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: (["A"], [], []),
    )
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 1000.0})
    fake_exec.get_order_status = MagicMock(side_effect=ConnectionError("broker unreachable"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1
    assert config.KILL_SWITCH_FLAG_FILE_PATH.exists()
    assert config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists()


def test_run_halts_remaining_liquidations_and_all_buys_after_a_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: setting the kill-switch flag alone
    # only takes effect on the *next* run -- without stopping the rest
    # of THIS run too, a query failure on the first of several
    # liquidations would still let the remaining liquidations and the
    # entire buy queue place orders in the same run, directly
    # contradicting the reason the flag was just set.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: (["A", "B"], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 1000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 1000.0, "B": 1000.0})
    # A's settlement query fails every retry; B's would succeed if
    # reached, and so would HIGH's buy -- neither should be reached.
    fake_exec.get_order_status = MagicMock(side_effect=ConnectionError("broker unreachable"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1
    # Both liquidations were attempted (A submitted, failed settlement,
    # loop broke before B) -- exactly one liquidate() call, not two.
    fake_exec.liquidate.assert_called_once_with("A")
    fake_exec.market_buy.assert_not_called()


def test_run_halts_remaining_buys_after_a_buy_settlement_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: the buy-side settlement call used
    # to discard its return value entirely -- a query failure raised no
    # alert, set no kill switch, and silently left `fills` incomplete.
    # Same fail-closed treatment as the liquidation side now.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 1000.0), ("LOW", 500.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_order_status = MagicMock(side_effect=ConnectionError("broker unreachable"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1
    assert config.KILL_SWITCH_FLAG_FILE_PATH.exists()
    assert config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists()
    # HIGH was attempted (submitted, failed settlement, loop broke
    # before LOW) -- exactly one market_buy() call, not two.
    fake_exec.market_buy.assert_called_once_with("HIGH", 1000.0)


def test_reset_stale_strikes_clears_a_tracked_ticker_no_longer_held(tmp_path: Path) -> None:
    # staff-engineer-reviewer finding on M26c: a ticker's liquidation
    # settling *after* this run's own synchronous check (never revisited
    # -- a real deferred settlement pass is M34) leaves a stale nonzero
    # strike count in state.json forever, poisoning a future rebuy's
    # first bad check into instant re-liquidation. Fixed by reconciling
    # tracked tickers against the broker's own current holdings directly
    # -- absence from current_holdings is real, broker-confirmed
    # evidence the position is gone, independent of whether settlement
    # ever resolved that specific order.
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "strikes": {"GONE": ["p1", "p2"], "STILL_HELD": ["p1", "p2", "p3"]},
                "holding_states": {},
                "pending_liquidations": [],
            }
        )
    )
    state = portfolio.StateTracker(path=state_path)

    bot._reset_stale_strikes_for_tickers_no_longer_held(state, {"STILL_HELD": 1000.0})

    assert state.get_strikes("GONE") == 0
    assert state.get_strikes("STILL_HELD") == 3  # untouched -- still genuinely held

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.get_strikes("GONE") == 0  # persisted, not just in-memory


def test_reset_stale_strikes_also_clears_a_healthy_holding_state_no_longer_held(
    tmp_path: Path,
) -> None:
    # M29c follow-up: a HEALTHY holding has zero strikes, so it never
    # appears in tracked_tickers() -- but its recorded HoldingState still
    # needs clearing once sold, or report.py would keep showing a
    # now-closed position as "Healthy" forever. Uses
    # tracked_holding_state_tickers() instead, which the strikes-only
    # reconciliation above doesn't touch.
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    state.record_holding_state("SOLD_OFF", portfolio.HoldingState.HEALTHY)
    state.record_holding_state("STILL_HELD", portfolio.HoldingState.DETERIORATING)
    state.save()

    bot._reset_stale_strikes_for_tickers_no_longer_held(state, {"STILL_HELD": 1000.0})

    assert state.get_holding_state("SOLD_OFF") is None
    assert state.get_holding_state("STILL_HELD") is portfolio.HoldingState.DETERIORATING


def test_run_escalates_to_critical_when_a_stale_settlement_block_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The converse of the routine kill-switch test above: when
    # SETTLEMENT_BLOCKED is also present (meaning it was a prior run's
    # unresolved settlement failure, not a deliberate pause), the run
    # must escalate loudly (alert-worthy) rather than log the same quiet
    # routine message a deliberate pause gets.
    config.KILL_SWITCH_FLAG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.KILL_SWITCH_FLAG_FILE_PATH.touch()
    config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.touch()
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []  # still never touches the broker
    assert exit_code == 1  # unlike a routine, deliberate pause -- this is alert-worthy


def test_run_defers_buy_orders_but_still_liquidates_when_order_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User request: exceeding the order budget used to abort the entire
    # run -- correct for a one-off overage, but a deadlock under daily
    # rebalancing from a cold start (nothing bought -> next run builds
    # the identical over-budget queue -> aborts again, forever). Now the
    # buy queue is truncated (deferred to a later run) instead, while
    # liquidations -- the two-strike quality discipline, not
    # discretionary -- always still execute.
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 1)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: (["A"], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 1000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 1000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    # 1 liquidation + 1 buy = 2 planned orders > budget of 1: the
    # liquidation still executes; the buy is deferred, not submitted.
    fake_exec.liquidate.assert_called_once_with("A")
    fake_exec.market_buy.assert_not_called()
    assert exit_code == 1


def test_run_defers_buy_order_when_notional_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.01)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 50_000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={})
    fake_exec.get_available_cash = MagicMock(return_value=100_000.0)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    # $50k planned buy notional vs. 1% of $100k equity ($1k budget) --
    # deferred to a later run, not submitted; the run still alerts (exit 1)
    # so a human sees it was deferred rather than silently dropped.
    fake_exec.market_buy.assert_not_called()
    assert exit_code == 1


def test_run_does_not_journal_a_failed_order_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # The central fault-tolerance property this milestone guarantees:
    # execution.py returns None for a rejected/failed order, and bot.py
    # must not journal it (and must not crash processing the rest).
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 5000.0), ("LOW", 3000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.market_buy = MagicMock(side_effect=[None, MagicMock(client_order_id="buy-id")])
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    recorded_orders: list[tuple[str, str]] = []
    monkeypatch.setattr(
        journal,
        "record_order",
        lambda symbol, side, reason, **kwargs: recorded_orders.append((symbol, side)),
    )

    exit_code = bot.run(run_date="2026-07-21")  # must not raise

    assert fake_exec.market_buy.call_count == 2
    assert recorded_orders == [("LOW", "buy")]  # HIGH's failed order never journaled
    # A single rejected order is normal, tolerated operational noise, not
    # itself one of DESIGN.md's alert categories -- no liquidations, no
    # reconciliation mismatch, no fallback, so this run is clean.
    assert exit_code == 0


def test_run_propagates_uncaught_when_verify_account_access_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # verify_account_access is deliberately fail-closed (no try/except in
    # execution.py); bot.py must not add its own handling around it --
    # this pins that contract so a future "helpful" try/except would
    # break this test instead of silently masking a real key/mode
    # mismatch.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.verify_account_access = MagicMock(side_effect=RuntimeError("key/mode mismatch"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    with pytest.raises(RuntimeError):
        bot.run(run_date="2026-07-21")


def test_run_propagates_uncaught_when_get_current_holdings_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same fail-closed contract as above: a broker outage fetching
    # holdings must abort the run, not be swallowed.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(side_effect=RuntimeError("broker outage"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    with pytest.raises(RuntimeError):
        bot.run(run_date="2026-07-21")


def test_run_skips_sell_evaluation_when_holdings_data_mostly_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: the second, holdings-only metrics
    # fetch has no fetch-fraction gate of its own, unlike the universe
    # screen -- a degraded fetch here (e.g. a still-active rate-limit
    # cooldown) must not silently strike real holdings toward
    # liquidation. process_sells should be skipped entirely, not fed
    # mostly-None data. M24 fix (staff-engineer-reviewer finding): this
    # abort now also alerts (exit 1), not just logs -- it used to be the
    # one degraded-data path in this file that silently exited 0.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    # 1 of 3 holdings fetched cleanly (33%) -- well below MIN_UNIVERSE_FETCH_FRACTION.
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"A": MagicMock(), "B": None, "C": None},
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    process_sells_calls: list[dict[str, float]] = []

    def _fake_process_sells(
        holdings: dict[str, float],
        metrics: dict[str, Any],
        state: Any,
        period: str,
        corp_check: Any = None,
    ) -> tuple[list[str], list[str], list[str]]:
        process_sells_calls.append(holdings)
        return ["A"], [], []

    monkeypatch.setattr(portfolio, "process_sells", _fake_process_sells)
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 100.0, "B": 100.0, "C": 100.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    assert process_sells_calls == []  # never called -- data too degraded to trust
    fake_exec.liquidate.assert_not_called()
    # M24: the degraded-fetch skip is itself alert-worthy now -- an
    # operator should see this, not just find it in the log if they go
    # looking.
    assert exit_code == 1


def test_check_data_freshness_none_when_no_archive_exists() -> None:
    assert bot._check_data_freshness() is None


def test_check_data_freshness_none_when_archive_is_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hardcoded past date would break as soon as DATA_FRESHNESS_MAX_HOURS
    # (daily-cadence tolerance, 48h) is smaller than the gap between that
    # date and whenever this test actually runs -- date it off "today"
    # instead so the test stays valid regardless of when it's run.
    recent_date = datetime.date.today() - datetime.timedelta(days=1)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / f"screen_results_{recent_date.isoformat()}.csv").write_text("symbol\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", archive_dir)
    assert bot._check_data_freshness() is None


def test_check_data_freshness_flags_a_stale_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Staff-engineer-reviewer finding: the age must come from the
    # run_date embedded in the filename, not filesystem mtime -- git
    # doesn't preserve mtimes across the checkout this project's GitHub
    # Actions workflow uses to restore this directory, which would
    # otherwise stamp every restored file with "now" and silently
    # neutralize this check. No os.utime call here on purpose: an old
    # filename alone must be enough to flag staleness.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "screen_results_2026-01-01.csv").write_text("symbol\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", archive_dir)

    stale_hours = bot._check_data_freshness()

    assert stale_hours is not None
    assert stale_hours > config.DATA_FRESHNESS_MAX_HOURS


def test_run_alerts_on_a_stale_last_run_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missed scheduled run -- the dead-man's-switch this run's own
    # archive check is meant to catch (staff-engineer-reviewer / M11).
    monkeypatch.setattr(config, "KILL_SWITCH", True)  # keep this test broker-free
    monkeypatch.setattr(bot, "_check_data_freshness", lambda: 4000)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_alerts_on_a_universe_index_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"], fallback_indices=["500"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_alerts_on_an_empty_universe_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=[])
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _screen_results([]))

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_clean_when_nothing_notable_happens(monkeypatch: pytest.MonkeyPatch) -> None:
    # The converse of every alert-producing test above: a fully healthy,
    # boring run (screen-only via kill switch, fresh archive, no
    # fallback, non-empty universe) must exit 0.
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    assert bot.run(run_date="2026-07-21") == 0


def test_run_aborts_on_reconciliation_mismatch_for_the_paper_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config.PAPER_TRADING is left at its real default (True) here.
    #
    # M27 update (Design v2.2 §3.3: "Reconciliation is authoritative...
    # it should have teeth"): this used to be the paper-account posture
    # that warned and continued (M10, unchanged by M20's live-only
    # abort) -- was
    # test_run_logs_reconciliation_warnings_without_aborting, asserting
    # the old behavior. Inverted, not deleted: the real FOX/LPG bug was
    # exactly a paper-account divergence this check would have caught
    # if it had been allowed to actually stop the run instead of only
    # logging, so the warn-and-continue carve-out for paper no longer
    # holds.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    fetch_calls: list[list[str]] = []
    monkeypatch.setattr(
        journal, "check_reconciliation", lambda holdings: ["AAPL: unexpected mismatch"]
    )

    def _fake_fetch_all_metrics(symbols: list[str], **_kwargs: Any) -> dict[str, MagicMock]:
        fetch_calls.append(symbols)
        return {}

    monkeypatch.setattr(data, "fetch_all_metrics", _fake_fetch_all_metrics)

    fake_exec = _FakeExecutionModule("2026-07-21")
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    assert exit_code == 1
    # Never got as far as evaluating holdings for sells/buys -- this is
    # what distinguishes "aborted" from "warned and continued."
    assert fetch_calls == []
    fake_exec.liquidate.assert_not_called()
    fake_exec.market_buy.assert_not_called()


def test_run_aborts_on_reconciliation_mismatch_for_the_live_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M20 originally gave the live account a stricter posture than paper
    # here (abort vs. warn-and-continue); M27 (Design v2.2 §3.3) removed
    # that distinction -- both accounts abort now, see the sibling
    # paper-account test above for why. This test confirms live keeps
    # working the same way, not that it's uniquely strict anymore.
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    # M24: LIVE_TRADING_ENABLED must be set for a live-account run to get
    # this far at all now (Fix 4) -- set True here since this test is
    # specifically exercising the reconciliation-abort path, not Fix 4's
    # own gate (that has its own dedicated test below).
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(
        journal, "check_reconciliation", lambda holdings: ["AAPL: unexpected mismatch"]
    )
    fetch_calls: list[list[str]] = []

    def _fake_fetch_all_metrics(symbols: list[str], **_kwargs: Any) -> dict[str, MagicMock]:
        fetch_calls.append(symbols)
        return {}

    monkeypatch.setattr(data, "fetch_all_metrics", _fake_fetch_all_metrics)

    fake_exec = _FakeExecutionModule("2026-07-21")
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    assert exit_code == 1
    # Never got as far as evaluating holdings for sells/buys, let alone
    # placing an order -- this is what distinguishes "aborted" from
    # "warned and continued."
    assert fetch_calls == []
    fake_exec.liquidate.assert_not_called()
    fake_exec.market_buy.assert_not_called()


def test_run_refuses_to_trade_live_without_the_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M24 fix: config.LIVE_TRADING_ENABLED previously gated only
    # report.py's real-money.html rendering -- the live workflow could
    # place real orders whenever Alpaca secrets were populated and
    # neither kill switch was set, regardless of this flag. Asserts the
    # broker client is never even constructed, not merely that no orders
    # were placed -- a stronger guarantee than "orders happened to be
    # zero."
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    exec_constructed = False

    def _fail_if_constructed(run_date: str) -> _FakeExecutionModule:
        nonlocal exec_constructed
        exec_constructed = True
        return _FakeExecutionModule(run_date)

    monkeypatch.setattr(execution, "ExecutionModule", _fail_if_constructed)

    exit_code = bot.run(run_date="2026-07-21")

    assert exec_constructed is False
    assert exit_code == 1


def test_run_trades_live_when_the_live_trading_flag_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The converse of the test above -- paper trading must remain
    # completely unaffected by this flag either way (checked via the
    # existing paper-account tests, which never set LIVE_TRADING_ENABLED
    # and still pass), and a live run WITH the flag set must proceed
    # exactly as before this fix.
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    assert exit_code == 0


def test_run_fetches_holdings_metrics_through_xbrl_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (peer-session review finding): the autouse
    # apply_primary_metrics no-op fixture means a test mocking only
    # data.fetch_all_metrics can't tell "correctly wired through
    # screener.fetch_metrics_with_xbrl_primary" from "reverted straight
    # to data.fetch_all_metrics" -- both would stay green. This test
    # spies on fetch_metrics_with_xbrl_primary itself, so reverting
    # bot.py's own call back to data.fetch_all_metrics directly would
    # correctly fail it.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio,
        "process_sells",
        lambda holdings, metrics, state, period, corp_check=None: ([], [], []),
    )
    monkeypatch.setattr(
        portfolio, "generate_buy_queue", lambda holdings, results, cash, exclude=None: []
    )

    calls: list[tuple[list[str], str]] = []
    real_fetch = screener.fetch_metrics_with_xbrl_primary

    def _spy(tickers: list[str], phase: str = "screening") -> dict[str, data.Metrics | None]:
        calls.append((tickers, phase))
        return real_fetch(tickers, phase=phase)

    monkeypatch.setattr(screener, "fetch_metrics_with_xbrl_primary", _spy)
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"AAPL": None})

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    bot.run(run_date="2026-07-21")

    assert len(calls) == 1
    assert calls[0] == (["AAPL"], "holdings check")
