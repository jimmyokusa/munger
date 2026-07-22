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

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

import bot
import config
import data
import execution
import journal
import portfolio
import screener
import universe


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


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    (tmp_path / "screen_results.csv").write_text("symbol,buyable,score,fail_reasons\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")


def test_kill_switch_active_via_config_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    assert bot._kill_switch_active() is True


def test_kill_switch_active_via_flag_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_path = tmp_path / "KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert bot._kill_switch_active() is True


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
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    bot.run(run_date="2026-07-21")

    assert construct_calls == []  # never touches the broker at all


def test_run_aborts_before_kill_switch_check_when_fetch_fraction_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
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

    bot.run(run_date="2026-07-21")

    assert construct_calls == []


def test_run_full_happy_path_places_liquidations_and_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio, "process_sells", lambda holdings, metrics, state: ["LIQUIDATE_ME"]
    )

    captured_buy_queue_holdings: dict[str, float] = {}

    def _fake_generate_buy_queue(
        holdings: dict[str, float], results: pd.DataFrame, cash: float
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

    bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    fake_exec.liquidate.assert_called_once_with("LIQUIDATE_ME")
    fake_exec.market_buy.assert_called_once_with("HIGH", 5000.0)
    # Caller contract from the M6/M7 review: the liquidated ticker must
    # not appear in what's passed to generate_buy_queue.
    assert "LIQUIDATE_ME" not in captured_buy_queue_holdings
    assert "KEEP" in captured_buy_queue_holdings
    assert ("LIQUIDATE_ME", "sell", "SELL strikes=2") in recorded_orders
    assert any(symbol == "HIGH" and side == "buy" for symbol, side, _ in recorded_orders)


def test_run_aborts_before_any_orders_when_order_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 1)
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: ["A"])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 1000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 1000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    bot.run(run_date="2026-07-21")

    # 1 liquidation + 1 buy = 2 planned orders > budget of 1 -- neither
    # should actually be submitted.
    fake_exec.liquidate.assert_not_called()
    fake_exec.market_buy.assert_not_called()


def test_run_aborts_before_any_orders_when_notional_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.01)
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 50_000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={})
    fake_exec.get_available_cash = MagicMock(return_value=100_000.0)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    bot.run(run_date="2026-07-21")

    # $50k planned buy notional vs. 1% of $100k equity ($1k budget).
    fake_exec.market_buy.assert_not_called()


def test_run_does_not_journal_a_failed_order_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # The central fault-tolerance property this milestone guarantees:
    # execution.py returns None for a rejected/failed order, and bot.py
    # must not journal it (and must not crash processing the rest).
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 5000.0), ("LOW", 3000.0)],
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

    bot.run(run_date="2026-07-21")  # must not raise

    assert fake_exec.market_buy.call_count == 2
    assert recorded_orders == [("LOW", "buy")]  # HIGH's failed order never journaled


def test_run_propagates_uncaught_when_verify_account_access_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # verify_account_access is deliberately fail-closed (no try/except in
    # execution.py); bot.py must not add its own handling around it --
    # this pins that contract so a future "helpful" try/except would
    # break this test instead of silently masking a real key/mode
    # mismatch.
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
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
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
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
    # mostly-None data.
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    # 1 of 3 holdings fetched cleanly (33%) -- well below MIN_UNIVERSE_FETCH_FRACTION.
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols: {"A": MagicMock(), "B": None, "C": None}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    process_sells_calls: list[dict[str, float]] = []

    def _fake_process_sells(
        holdings: dict[str, float], metrics: dict[str, Any], state: Any
    ) -> list[str]:
        process_sells_calls.append(holdings)
        return ["A"]

    monkeypatch.setattr(portfolio, "process_sells", _fake_process_sells)
    monkeypatch.setattr(portfolio, "generate_buy_queue", lambda holdings, results, cash: [])

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 100.0, "B": 100.0, "C": 100.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    bot.run(run_date="2026-07-21")

    assert process_sells_calls == []  # never called -- data too degraded to trust
    fake_exec.liquidate.assert_not_called()


def test_run_logs_reconciliation_warnings_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(universe, "get_universe", lambda: ["HIGH", "LOW"])
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(
        journal, "check_reconciliation", lambda holdings: ["AAPL: unexpected mismatch"]
    )
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(portfolio, "generate_buy_queue", lambda holdings, results, cash: [])

    fake_exec = _FakeExecutionModule("2026-07-21")
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    bot.run(run_date="2026-07-21")  # must not raise despite the mismatch

    fake_exec.verify_account_access.assert_called_once()
