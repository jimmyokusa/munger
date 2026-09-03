"""Unit tests for trading_common.py (M34, Design v2.2 §3.1).

Extracted out of bot.py's own tests -- the underlying logic was already
verified behavior-preserving by tests/test_bot.py's 45 unmodified passes
after the extraction (bot.py imports these via aliases, so its existing
tests exercise this module transparently too). This file adds direct,
module-level coverage that doesn't imply "bot-specific" the way testing
only through bot.py's aliases would -- evaluate.py/execute_trades.py
both depend on this module being correct on its own.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Order

import config
import trading_common


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(
        config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "GLOBAL_KILL_SWITCH"
    )
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(
        config, "SETTLEMENT_BLOCKED_FLAG_FILE_PATH", tmp_path / "SETTLEMENT_BLOCKED"
    )
    monkeypatch.setattr(config, "DATA_FRESHNESS_MAX_HOURS", 48)
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 20)
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.25)


def _fake_filled_order(symbol: str = "AAPL") -> MagicMock:
    order = MagicMock(spec=Order)
    order.status = OrderStatus.FILLED
    order.client_order_id = f"paper-2026-01-01-{symbol}-buy"
    return order


# --- kill_switch_active / global_kill_switch_active ---


def test_kill_switch_active_via_config_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    assert trading_common.kill_switch_active() is True


def test_kill_switch_active_via_flag_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_path = tmp_path / "KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert trading_common.kill_switch_active() is True


def test_kill_switch_active_false_by_default() -> None:
    assert trading_common.kill_switch_active() is False


def test_global_kill_switch_active_false_by_default() -> None:
    assert trading_common.global_kill_switch_active() is False


def test_global_kill_switch_active_via_flag_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag_path = tmp_path / "GLOBAL_KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert trading_common.global_kill_switch_active() is True


# --- alert / finish ---


def test_alert_appends_and_returns_none() -> None:
    alerts: list[str] = []
    trading_common.alert(alerts, "something happened")
    assert alerts == ["something happened"]


def test_finish_returns_zero_with_no_alerts() -> None:
    assert trading_common.finish([]) == 0


def test_finish_returns_one_with_alerts() -> None:
    assert trading_common.finish(["something happened"]) == 1


# --- check_data_freshness ---


def test_check_data_freshness_none_when_no_archive_exists() -> None:
    assert trading_common.check_data_freshness() is None


def test_check_data_freshness_none_when_archive_is_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    archive_dir = config.SCREEN_RESULTS_ARCHIVE_DIR
    archive_dir.mkdir(parents=True)
    today = datetime.date.today().isoformat()
    (archive_dir / f"screen_results_{today}.csv").touch()
    assert trading_common.check_data_freshness() is None


def test_check_data_freshness_flags_a_stale_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    archive_dir = config.SCREEN_RESULTS_ARCHIVE_DIR
    archive_dir.mkdir(parents=True)
    old_date = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    (archive_dir / f"screen_results_{old_date}.csv").touch()
    stale_hours = trading_common.check_data_freshness()
    assert stale_hours is not None
    assert stale_hours >= 48


# --- settle_and_react ---


def test_settle_and_react_calls_on_filled_when_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    exec_module = MagicMock()
    monkeypatch.setattr(trading_common.settlement, "settle_order", lambda em, cid: "filled")
    on_filled = MagicMock()
    alerts: list[str] = []

    query_failed = trading_common.settle_and_react(
        exec_module, alerts, "AAPL", "buy", _fake_filled_order(), on_filled=on_filled
    )

    assert query_failed is False
    on_filled.assert_called_once()
    assert alerts == []


def test_settle_and_react_alerts_and_sets_kill_switch_on_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_module = MagicMock()
    monkeypatch.setattr(trading_common.settlement, "settle_order", lambda em, cid: None)
    alerts: list[str] = []

    query_failed = trading_common.settle_and_react(
        exec_module, alerts, "AAPL", "buy", _fake_filled_order()
    )

    assert query_failed is True
    assert len(alerts) == 1
    assert config.KILL_SWITCH_FLAG_FILE_PATH.exists()
    assert config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.exists()


def test_settle_and_react_alerts_without_kill_switch_when_genuinely_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_module = MagicMock()
    monkeypatch.setattr(trading_common.settlement, "settle_order", lambda em, cid: "pending")
    alerts: list[str] = []

    query_failed = trading_common.settle_and_react(
        exec_module, alerts, "AAPL", "buy", _fake_filled_order()
    )

    assert query_failed is False
    assert len(alerts) == 1
    assert not config.KILL_SWITCH_FLAG_FILE_PATH.exists()


# --- cap_buy_orders_to_budget ---


def test_cap_buy_orders_to_budget_passes_through_when_under_budget() -> None:
    orders = [("AAPL", 100.0), ("MSFT", 100.0)]
    capped, deferred, bound_budgets = trading_common.cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == orders
    assert deferred == []
    assert bound_budgets == []


def test_cap_buy_orders_to_budget_defers_past_the_order_count_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 2)
    orders = [("AAPL", 100.0), ("MSFT", 100.0), ("GOOG", 100.0)]
    capped, deferred, bound_budgets = trading_common.cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert [s for s, _ in capped] == ["AAPL", "MSFT"]
    assert deferred == ["GOOG"]
    assert bound_budgets == ["order-count"]


def test_cap_buy_orders_to_budget_never_truncates_liquidations() -> None:
    orders = [("AAPL", 100.0)]
    capped, deferred, _bound_budgets = trading_common.cap_buy_orders_to_budget(
        orders, liquidation_count=config.GLOBAL_ORDER_BUDGET, portfolio_value=10_000.0
    )
    # max_buy_orders = max(0, 20 - 20) = 0, so the one buy order is fully deferred,
    # but the liquidation count itself was never touched by this function.
    assert capped == []
    assert deferred == ["AAPL"]
