"""Unit tests for settlement.py (Design v2.2 §3.3, M26b)."""

from __future__ import annotations

import time
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
    # M46: default the fill-wait budget off so existing single-query tests
    # keep their pre-M46 behavior; the tests that exercise the wait opt
    # back in explicitly (setting POLLS, stubbing time.sleep). POLL_SECONDS
    # is left at its real value -- with time.sleep stubbed it costs nothing,
    # and a real value keeps settle_order's wall-clock deadline sane.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 0)


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


# --- wait_for_fill: the M46 synchronous post-submit fill wait ---


def test_settle_order_waits_out_a_briefly_pending_order_then_reports_the_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 2026-09-08 failure: a just-submitted market order sits in
    # pending_new for a beat, then fills. With wait_for_fill the settle
    # check must ride that out and report "filled," not "pending."
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 5)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    filled = _fake_order(OrderStatus.FILLED, filled_qty=3.0, fill_price=42.0)
    get_status = MagicMock(
        side_effect=[_fake_order(OrderStatus.NEW), _fake_order(OrderStatus.PENDING_NEW), filled]
    )
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status == "filled"
    assert get_status.call_count == 3  # two pending polls, then the fill
    assert len(sleeps) == 2
    fill = journal.get_fill("co-1")
    assert fill is not None
    assert fill["status"] == "filled"  # only the resolved status is journaled
    assert fill["filled_qty"] == 3.0


def test_settle_order_gives_up_after_the_fill_wait_budget_and_still_reports_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuinely stuck order must still resolve to "pending" (which the
    # caller alerts on) within a bounded number of polls, not loop.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 3)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    get_status = MagicMock(return_value=_fake_order(OrderStatus.ACCEPTED))
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status == "pending"
    assert get_status.call_count == 4  # the initial query + 3 re-polls
    assert len(sleeps) == 3
    assert journal.get_fill("co-1")["status"] == "pending"  # type: ignore[index]


def test_settle_order_stops_waiting_the_moment_a_terminal_status_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 5)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    get_status = MagicMock(
        side_effect=[_fake_order(OrderStatus.NEW), _fake_order(OrderStatus.REJECTED)]
    )
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status == "canceled"
    assert get_status.call_count == 2
    assert len(sleeps) == 1  # did not keep polling to the budget


def test_settle_order_does_not_wait_when_wait_for_fill_is_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The budget is configured, but the default (deferred settle_orders
    # pass, not a fresh submission) must not pay the wait.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 5)
    slept = MagicMock()
    monkeypatch.setattr(time, "sleep", slept)
    get_status = MagicMock(return_value=_fake_order(OrderStatus.NEW))
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1")

    assert status == "pending"
    assert get_status.call_count == 1
    slept.assert_not_called()


def test_settle_order_keeps_a_verified_pending_when_a_later_repoll_query_blips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: a first poll that successfully
    # reads "pending" then a later poll whose query fails all retries
    # must NOT escalate to the None / query-failure path (which trips the
    # caller's kill switch). The verified earlier status stands.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 2)
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    get_status = MagicMock(
        side_effect=[
            _fake_order(OrderStatus.NEW),
            ConnectionError("blip"),
            ConnectionError("blip"),
        ]
    )
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status == "pending"  # not None -- the earlier verified read stands
    assert journal.get_fill("co-1")["status"] == "pending"  # type: ignore[index]


def test_settle_order_still_reports_a_query_failure_when_no_poll_ever_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other side of the finding above: if the very first query fails
    # all retries, wait_for_fill doesn't paper over it -- still None, and
    # bailed immediately without burning the poll budget.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 5)
    monkeypatch.setattr(config, "SETTLEMENT_QUERY_RETRY_ATTEMPTS", 1)
    slept = MagicMock()
    monkeypatch.setattr(time, "sleep", slept)
    get_status = MagicMock(side_effect=ConnectionError("broker unreachable"))
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status is None
    assert get_status.call_count == 1  # no re-poll once the first read fails outright
    assert journal.get_fill("co-1") is None
    slept.assert_not_called()


def test_settle_order_wall_clock_caps_the_wait_even_with_slow_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: poll count alone doesn't bound
    # elapsed time, since each poll's own query retry can be slow. Once
    # the sleep budget (POLLS * POLL_SECONDS) of wall-clock has elapsed,
    # the loop stops regardless of how many polls are nominally left.
    # Fake clock: status queries here "cost" 10s each (a flaky endpoint),
    # so the 30s deadline is reached long before the 100-poll budget.
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLLS", 100)
    monkeypatch.setattr(config, "SETTLEMENT_FILL_WAIT_POLL_SECONDS", 0.3)  # deadline = 30s
    fake_now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(time, "sleep", lambda s: fake_now.__setitem__(0, fake_now[0] + s))

    def _slow_query(_cid: str) -> MagicMock:
        fake_now[0] += 10.0
        return _fake_order(OrderStatus.NEW)

    get_status = MagicMock(side_effect=_slow_query)
    exec_module = _fake_exec_module(get_status)

    status = settlement.settle_order(exec_module, "co-1", wait_for_fill=True)

    assert status == "pending"
    assert get_status.call_count < 10  # deadline bound it, not the 100-poll budget


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
