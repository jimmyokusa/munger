"""Unit tests for execute_trades.py's orchestration logic (M34, Design v2.2 §3.1).

Every external module call is mocked -- this tests execute_trades.py's
own wiring, not each module's internal correctness (already covered by
their own test files). Follows the same patterns as tests/test_bot.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Order

import config
import execute_trades
import execution
import journal
import portfolio
import screener
import trading_common
import universe


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
        self.get_order_status = MagicMock(return_value=_fake_filled_order())
        self.is_corporate_action = MagicMock(return_value=False)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # M45: default to market-open so existing tests keep exercising the
    # trading path without reaching a real TradingClient -- tests that
    # want to exercise the closed-market path override this locally.
    monkeypatch.setattr(trading_common, "market_is_open", lambda: True)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(
        config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "GLOBAL_KILL_SWITCH"
    )
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    (tmp_path / "screen_results.csv").write_text("symbol,buyable,score,fail_reasons\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "STATE_FILE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(
        config, "SETTLEMENT_BLOCKED_FLAG_FILE_PATH", tmp_path / "SETTLEMENT_BLOCKED"
    )
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    # StateTracker's default path is bound at class-definition time --
    # wrap the real class so both a bare call inside execute_trades.run()
    # and an explicit `path=` from a test's own verification both read/
    # write the same tmp_path file.
    _real_state_tracker = portfolio.StateTracker
    monkeypatch.setattr(
        portfolio,
        "StateTracker",
        lambda path=None: _real_state_tracker(path=path or config.STATE_FILE_PATH),
    )


def _seed_pending_liquidation(ticker: str) -> None:
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    state.add_pending_liquidation(ticker)
    state.save()


def test_run_screen_only_when_kill_switch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert construct_calls == []  # never touches the broker at all
    assert exit_code == 0


def test_run_screen_only_when_global_kill_switch_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag_path = tmp_path / "GLOBAL_KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    construct_calls: list[str] = []
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 0


def test_run_aborts_before_kill_switch_check_when_fetch_fraction_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 1


def test_run_refuses_to_run_live_without_the_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    construct_calls: list[str] = []
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 1


def test_run_refuses_to_trade_when_the_market_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # M45: same gate as bot.py's own -- expected/routine (weekends,
    # holidays), so NOT alert-worthy, unlike the live-trading-flag test
    # above.
    monkeypatch.setattr(trading_common, "market_is_open", lambda: False)
    construct_calls: list[str] = []
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 0


def test_run_aborts_on_reconciliation_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert exit_code == 1
    fake_exec.liquidate.assert_not_called()
    fake_exec.market_buy.assert_not_called()


def test_run_liquidates_a_pending_ticker_and_clears_it_on_confirmed_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"HRMY": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("HRMY", "buy", "NEW_POSITION score=78.2")
    _seed_pending_liquidation("HRMY")

    exit_code = execute_trades.run(run_date="2026-07-21")

    fake_exec.liquidate.assert_called_once_with("HRMY")
    assert exit_code == 1  # a liquidation is always alert-worthy
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == []  # cleared once confirmed filled
    assert state.get_strikes("HRMY") == 0  # reset alongside


def test_run_clears_a_stale_pending_liquidation_no_longer_actually_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {}  # HRMY isn't held anymore
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    _seed_pending_liquidation("HRMY")

    execute_trades.run(run_date="2026-07-21")

    fake_exec.liquidate.assert_not_called()  # never attempts a sell against nothing
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == []


def test_run_leaves_an_unconfirmed_liquidation_pending_for_the_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"HRMY": 1000.0}
    fake_exec.get_order_status = MagicMock(side_effect=RuntimeError("broker unreachable"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("HRMY", "buy", "NEW_POSITION score=78.2")
    _seed_pending_liquidation("HRMY")
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_ATTEMPTS", 1)

    execute_trades.run(run_date="2026-07-21")

    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == ["HRMY"]  # still pending, not lost
    assert config.KILL_SWITCH_FLAG_FILE_PATH.exists()


def test_run_excludes_a_live_corporate_action_ticker_from_the_buy_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"MRGD": 1000.0}
    fake_exec.is_corporate_action = MagicMock(return_value=True)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("MRGD", "buy", "NEW_POSITION score=78.2")
    captured_exclude: set[str] = set()

    def _fake_generate_buy_queue(
        holdings: dict[str, float],
        results: pd.DataFrame,
        cash: float,
        exclude: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        captured_exclude.update(exclude or set())
        return []

    monkeypatch.setattr(portfolio, "generate_buy_queue", _fake_generate_buy_queue)

    exit_code = execute_trades.run(run_date="2026-07-21")

    assert captured_exclude == {"MRGD"}
    assert exit_code == 1  # confirmed corporate action needs manual review


def test_run_defers_a_pending_liquidation_for_a_ticker_now_in_corporate_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: a ticker Evaluate already decided
    # to liquidate can enter a corporate action before Execute ever gets
    # to it -- must never sell it this run (CORPORATE_ACTION is always a
    # human decision, per §3.2), and the pending mark must survive so a
    # human can still act on the original liquidation reason once the
    # corporate action resolves.
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"MRGD": 1000.0}
    fake_exec.is_corporate_action = MagicMock(return_value=True)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("MRGD", "buy", "NEW_POSITION score=78.2")
    _seed_pending_liquidation("MRGD")

    exit_code = execute_trades.run(run_date="2026-07-21")

    fake_exec.liquidate.assert_not_called()
    assert exit_code == 1
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == ["MRGD"]  # stays pending, not cleared or acted on


def test_run_full_happy_path_places_liquidations_and_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"LIQUIDATE_ME": 1000.0, "KEEP": 2000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("LIQUIDATE_ME", "buy", "NEW_POSITION score=1.0")
    journal.record_order("KEEP", "buy", "NEW_POSITION score=2.0")
    _seed_pending_liquidation("LIQUIDATE_ME")

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
    recorded_orders: list[tuple[str, str, str]] = []

    def _fake_record_order(symbol: str, side: str, reason: str, **kwargs: Any) -> None:
        recorded_orders.append((symbol, side, reason))

    monkeypatch.setattr(journal, "record_order", _fake_record_order)

    exit_code = execute_trades.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    fake_exec.liquidate.assert_called_once_with("LIQUIDATE_ME")
    fake_exec.market_buy.assert_called_once_with("HIGH", 5000.0)
    assert "LIQUIDATE_ME" not in captured_buy_queue_holdings
    assert "KEEP" in captured_buy_queue_holdings
    assert (
        "LIQUIDATE_ME",
        "sell",
        f"SELL strikes={config.STRIKES_TO_LIQUIDATE}",
    ) in recorded_orders
    assert ("HIGH", "buy", "NEW_POSITION score=90.0") in recorded_orders
    assert exit_code == 1


def test_run_journals_a_top_up_buy_distinctly_from_a_new_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen_results(
        [
            {"symbol": "KEEP", "buyable": True, "score": 70.0, "fail_reasons": ""},
            {"symbol": "NEW", "buyable": True, "score": 85.0, "fail_reasons": ""},
        ]
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: screen)
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"KEEP": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("KEEP", "buy", "NEW_POSITION score=1.0")

    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("KEEP", 500.0), ("NEW", 500.0)],
    )
    recorded_orders: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        journal,
        "record_order",
        lambda symbol, side, reason, **kwargs: recorded_orders.append((symbol, side, reason)),
    )

    execute_trades.run(run_date="2026-07-21")

    assert ("KEEP", "buy", "TOP_UP score=70.0") in recorded_orders
    assert ("NEW", "buy", "NEW_POSITION score=85.0") in recorded_orders


def test_run_halts_remaining_buys_after_a_buy_settlement_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {}
    fake_exec.get_order_status = MagicMock(side_effect=RuntimeError("broker unreachable"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash, exclude=None: [("HIGH", 1000.0), ("LOW", 1000.0)],
    )

    execute_trades.run(run_date="2026-07-21")

    fake_exec.market_buy.assert_called_once()  # never attempted the second buy
    assert config.KILL_SWITCH_FLAG_FILE_PATH.exists()
    assert config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists()
