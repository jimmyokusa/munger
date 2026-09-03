"""Unit tests for settlement.py (Design v2.2 §3.3, M26b)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Order

import config
import execution
import journal
import settlement


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    # Fast retries in tests -- no real backoff wait.
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_BACKOFF_SECONDS", 0.0)


def _fake_order(
    status: OrderStatus,
    symbol: str = "AAPL",
    filled_qty: float | None = None,
    fill_price: float | None = None,
) -> MagicMock:
    order = MagicMock(spec=Order)
    order.status = status
    order.symbol = symbol
    order.filled_qty = filled_qty
    order.filled_avg_price = fill_price
    return order


def _fake_exec_module(get_order_status: MagicMock) -> MagicMock:
    exec_module = MagicMock(spec=execution.ExecutionModule)
    exec_module.get_order_status = get_order_status
    return exec_module


# --- Classification (the four §3.3 cases) ---


def test_settle_order_classifies_a_full_fill() -> None:
    order = _fake_order(OrderStatus.FILLED, filled_qty=10.0, fill_price=150.25)
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "filled"
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["status"] == "filled"
    assert fill["filled_qty"] == 10.0
    assert fill["fill_price"] == 150.25


def test_settle_order_classifies_a_partial_fill() -> None:
    order = _fake_order(OrderStatus.PARTIALLY_FILLED, filled_qty=4.0, fill_price=150.0)
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "partially_filled"
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["filled_qty"] == 4.0  # the real filled quantity, not discarded


def test_settle_order_classifies_genuinely_pending_as_pending_not_a_failure() -> None:
    # A successful query reporting the order still open is not an error
    # -- distinct from a query failure below.
    order = _fake_order(OrderStatus.NEW)
    get_status = MagicMock(return_value=order)
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "pending"
    assert get_status.call_count == 1  # no retry -- the query itself succeeded
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["status"] == "pending"


def test_settle_order_classifies_expired_distinctly_from_canceled() -> None:
    order = _fake_order(OrderStatus.EXPIRED)
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    assert settlement.settle_order(exec_module, "co-1") == "expired"


def test_settle_order_classifies_rejected_as_canceled() -> None:
    order = _fake_order(OrderStatus.REJECTED)
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    assert settlement.settle_order(exec_module, "co-1") == "canceled"


def test_settle_order_survives_malformed_filled_qty_without_crashing() -> None:
    # staff-engineer-reviewer finding: a genuinely malformed numeric
    # field in an otherwise-successful query response used to raise
    # unguarded, propagating out of bot.run()'s order loops as an
    # unhandled crash instead of going through the structured
    # kill-switch path -- a successful query with bad data is a
    # data-integrity problem, not a broker-communication one.
    order = _fake_order(
        OrderStatus.FILLED,
        filled_qty="not-a-number",  # type: ignore[arg-type]  # deliberately malformed
        fill_price=150.0,
    )
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "filled"  # classification itself is unaffected
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["filled_qty"] is None  # recorded as unknown, not crashed or guessed
    assert fill["fill_price"] == 150.0  # the other, valid field is unaffected


def test_settle_order_survives_malformed_fill_price_without_crashing() -> None:
    order = _fake_order(
        OrderStatus.FILLED,
        filled_qty=10.0,
        fill_price="also-not-a-number",  # type: ignore[arg-type]  # deliberately malformed
    )
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "filled"
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["filled_qty"] == 10.0
    assert fill["fill_price"] is None


def test_settle_order_query_failure_retries_then_returns_none() -> None:
    get_status = MagicMock(side_effect=ConnectionError("broker unreachable"))
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1")

    assert status is None  # None means "couldn't find out," not "no fill"
    assert get_status.call_count == config.SETTLEMENT_QUERY_RETRY_ATTEMPTS
    assert journal.get_fill("co-1") is None  # nothing written on a query failure


def test_settle_order_recovers_after_a_transient_query_failure() -> None:
    # Fails once, then succeeds -- must not treat one bad attempt as a
    # permanent query failure when a retry would have resolved it.
    order = _fake_order(OrderStatus.FILLED, filled_qty=10.0, fill_price=100.0)
    get_status = MagicMock(side_effect=[ConnectionError("blip"), order])
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "filled"
    assert get_status.call_count == 2


# --- settle_orders (multi-order pass) ---


def test_settle_orders_isolates_one_query_failure_from_the_rest() -> None:
    filled_order = _fake_order(OrderStatus.FILLED, symbol="MSFT", filled_qty=5.0, fill_price=300.0)

    def _side_effect(client_order_id: str) -> Order:
        if client_order_id == "co-bad":
            raise ConnectionError("broker unreachable")
        return filled_order

    exec_module = _fake_exec_module(MagicMock(side_effect=_side_effect))

    result = settlement.settle_orders(exec_module, ["co-good", "co-bad"])

    assert result.settled == ["co-good"]
    assert result.query_failed == ["co-bad"]
    assert result.all_settled is False


def test_settlement_result_all_settled_true_when_nothing_failed() -> None:
    order = _fake_order(OrderStatus.FILLED, filled_qty=1.0, fill_price=1.0)
    exec_module = _fake_exec_module(MagicMock(return_value=order))

    result = settlement.settle_orders(exec_module, ["co-1", "co-2"])

    assert result.all_settled is True
    assert result.query_failed == []


# --- Idempotency: re-running settlement over the same order set ---


def test_settle_order_is_idempotent_on_rerun_after_status_changes() -> None:
    # Simulates re-running the whole settlement pass after a crash mid-
    # pass, where the underlying order has since progressed from pending
    # to filled -- must converge to the final status, not duplicate rows
    # or get stuck on the stale one.
    pending_order = _fake_order(OrderStatus.NEW)
    exec_module = _fake_exec_module(MagicMock(return_value=pending_order))
    settlement.settle_order(exec_module, "co-1")
    assert journal.get_fill("co-1")["status"] == "pending"  # type: ignore[index]

    filled_order = _fake_order(OrderStatus.FILLED, filled_qty=10.0, fill_price=100.0)
    exec_module.get_order_status = MagicMock(return_value=filled_order)
    settlement.settle_order(exec_module, "co-1")

    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["status"] == "filled"
    conn_rows = journal._connect().execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert conn_rows == 1  # upserted, not accumulated
