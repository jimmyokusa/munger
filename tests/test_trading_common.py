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
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderStatus
from alpaca.trading.models import Clock, Order

import config
import journal
import material_events
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
    # M46: the SETTLEMENT_FILL_WAIT_* constants are deliberately not set
    # here -- every settle_and_react test in this file stubs
    # settlement.settle_order wholesale, so the real fill-wait loop (and
    # its time.sleep) is never reached. If a test ever un-stubs it, add
    # SETTLEMENT_FILL_WAIT_POLLS = 0 alongside that change.


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
    monkeypatch.setattr(
        trading_common.settlement, "settle_order", lambda em, cid, **kwargs: "filled"
    )
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
    monkeypatch.setattr(trading_common.settlement, "settle_order", lambda em, cid, **kwargs: None)
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
    monkeypatch.setattr(
        trading_common.settlement, "settle_order", lambda em, cid, **kwargs: "pending"
    )
    alerts: list[str] = []

    query_failed = trading_common.settle_and_react(
        exec_module, alerts, "AAPL", "buy", _fake_filled_order()
    )

    assert query_failed is False
    assert len(alerts) == 1
    assert not config.KILL_SWITCH_FLAG_FILE_PATH.exists()


def test_settle_and_react_asks_settle_order_to_wait_for_a_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M46: the synchronous post-submit check must opt into the fill wait,
    # so Alpaca's brief pending_new window isn't reported as a non-fill
    # (which alerts and fails the whole scheduled run).
    captured: dict[str, object] = {}

    def _fake_settle_order(em: object, cid: str, **kwargs: object) -> str:
        captured.update(kwargs)
        return "filled"

    monkeypatch.setattr(trading_common.settlement, "settle_order", _fake_settle_order)
    alerts: list[str] = []

    trading_common.settle_and_react(MagicMock(), alerts, "AAPL", "buy", _fake_filled_order())

    assert captured == {"wait_for_fill": True}


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


def test_cap_buy_orders_to_budget_truncates_a_near_full_equity_order_by_default() -> None:
    # Pins down the failure mode M48 fixes: at the base 25% budget, a
    # generate_buy_queue order sized close to a $100.33 account's full
    # equity would itself get deferred right back to zero here, even if
    # some future change to generate_buy_queue's own caps let it size one.
    orders = [("A", 98.32)]
    capped, deferred, bound_budgets = trading_common.cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=100.33
    )
    assert capped == []
    assert deferred == ["A"]
    assert bound_budgets == ["notional"]


def test_cap_buy_orders_to_budget_does_not_truncate_a_concentrated_mode_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M48: must read EFFECTIVE_GLOBAL_NOTIONAL_BUDGET_PCT, not the bare
    # constant -- otherwise this backstop would silently defeat
    # generate_buy_queue's own concentrated-mode sizing.
    monkeypatch.setattr(config, "SMALL_ACCOUNT_CONCENTRATED_MODE", True)
    orders = [("A", 98.32)]
    capped, deferred, bound_budgets = trading_common.cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=100.33
    )
    assert capped == orders
    assert deferred == []
    assert bound_budgets == []


# --- check_small_account_concentration_ceiling ---


def test_concentration_ceiling_check_is_a_noop_when_mode_is_off() -> None:
    assert config.SMALL_ACCOUNT_CONCENTRATED_MODE is False
    result = trading_common.check_small_account_concentration_ceiling(1_000_000.0)
    assert result is None


def test_concentration_ceiling_check_passes_under_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SMALL_ACCOUNT_CONCENTRATED_MODE", True)
    monkeypatch.setattr(config, "SMALL_ACCOUNT_CONCENTRATED_MODE_EQUITY_CEILING", 500.0)
    result = trading_common.check_small_account_concentration_ceiling(100.33)
    assert result is None


def test_concentration_ceiling_check_refuses_once_equity_exceeds_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M48 (staff-engineer-reviewer finding): the structural guard against
    # a forgotten flag going all-in at a much larger equity than approved.
    monkeypatch.setattr(config, "SMALL_ACCOUNT_CONCENTRATED_MODE", True)
    monkeypatch.setattr(config, "SMALL_ACCOUNT_CONCENTRATED_MODE_EQUITY_CEILING", 500.0)
    result = trading_common.check_small_account_concentration_ceiling(50_000.0)
    assert result is not None
    assert "50,000.00" in result
    assert "500.00" in result


# --- market_is_open (moved from pnl.py, M45: now shared with bot.py/execute_trades.py) ---


def _fake_clock(is_open: bool) -> MagicMock:
    clock = MagicMock(spec=Clock)
    clock.is_open = is_open
    return clock


def _patch_trading_client(monkeypatch: pytest.MonkeyPatch, trading_mock: MagicMock) -> None:
    monkeypatch.setattr(trading_common, "TradingClient", lambda **kwargs: trading_mock)


def test_market_is_open_true(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = _fake_clock(True)
    _patch_trading_client(monkeypatch, trading_mock)

    assert trading_common.market_is_open() is True


def test_market_is_open_false(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = _fake_clock(False)
    _patch_trading_client(monkeypatch, trading_mock)

    assert trading_common.market_is_open() is False


def test_market_is_open_fails_closed_on_unexpected_clock_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = {"not": "a Clock"}
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ValueError, match="unexpected get_clock"):
        trading_common.market_is_open()


def test_market_is_open_retries_a_transient_clock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    trading_mock = MagicMock()
    trading_mock.get_clock.side_effect = [ConnectionError("blip"), _fake_clock(True)]
    _patch_trading_client(monkeypatch, trading_mock)

    assert trading_common.market_is_open() is True
    assert trading_mock.get_clock.call_count == 2


def test_market_is_open_fails_closed_after_a_second_clock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A persistently broken clock call must still fail loud after the
    # retry is exhausted, same posture as pnl._with_retry's own
    # equivalent test -- this is a reduction in false-positive noise on
    # one-off blips, not a blanket suppression of real failures.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    trading_mock = MagicMock()
    trading_mock.get_clock.side_effect = ConnectionError("still broken")
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ConnectionError, match="still broken"):
        trading_common.market_is_open()


# --- check_material_event_buy_blocks (M50a: paced alerts, block always) ---


def _block(accession_number: str = "acc-1") -> material_events.BuyBlock:
    return material_events.BuyBlock(
        accession_number=accession_number,
        reason="Critical 8-K filed 2026-04-29 (Item 4.02, within 365-day cooldown)",
    )


def test_material_event_buy_blocks_alerts_the_first_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})
    alerts: list[str] = []

    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1
    assert "GRBK" in alerts[0]


def test_material_event_buy_blocks_does_not_realert_within_the_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The core M50a fix: the same block on a later run still excludes the
    # symbol but no longer alerts (which would fail the run and email the
    # operator, every single day, for a known condition).
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}  # still blocked -- the exclusion never lapses
    assert alerts == []  # but no longer alert-worthy


def test_material_event_buy_blocks_realerts_once_the_interval_elapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A months-long block must not go permanently silent after one alert
    # (pm-reviewer finding) -- it resurfaces on the re-alert interval.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})
    monkeypatch.setattr(config, "MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS", 30)

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    # Backdate the stored alert past the interval, rather than sleeping.
    stale = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=31)).isoformat()
    with journal._connect() as conn:
        conn.execute("UPDATE material_event_buy_blocks SET last_alerted_timestamp = ?", (stale,))

    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1
    assert "still blocked" in alerts[0]  # distinguishable from a first-time discovery


def test_material_event_buy_blocks_realert_resets_the_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard for an earlier draft's INSERT OR IGNORE: if the
    # re-alert didn't update last_alerted_timestamp, the interval would
    # elapse once and then fire on EVERY subsequent run, quietly restoring
    # the every-run spam this milestone exists to remove.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})
    monkeypatch.setattr(config, "MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS", 30)

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    stale = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=31)).isoformat()
    with journal._connect() as conn:
        conn.execute("UPDATE material_event_buy_blocks SET last_alerted_timestamp = ?", (stale,))
    trading_common.check_material_event_buy_blocks(["GRBK"], [])  # re-alerts, resets the clock

    alerts: list[str] = []
    trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert alerts == []  # silent again, not spamming every run


def test_material_event_buy_blocks_preserves_first_blocked_timestamp_across_realerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # first_blocked_timestamp is the only record of how long a block has
    # actually been standing -- a re-alert must not overwrite it.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})
    monkeypatch.setattr(config, "MATERIAL_EVENT_BUY_BLOCK_REALERT_DAYS", 0)

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    with journal._connect() as conn:
        first = conn.execute(
            "SELECT first_blocked_timestamp FROM material_event_buy_blocks"
        ).fetchone()[0]
    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    with journal._connect() as conn:
        row = conn.execute(
            "SELECT first_blocked_timestamp, last_alerted_timestamp FROM material_event_buy_blocks"
        ).fetchone()

    assert row[0] == first  # unchanged
    assert row[1] >= first  # moved forward


def test_material_event_buy_blocks_realerts_on_a_genuinely_new_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A different accession number is a different operator-facing fact,
    # even on the same ticker -- it must alert immediately, not wait out
    # the re-alert interval.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(
        material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block("acc-1")}
    )
    trading_common.check_material_event_buy_blocks(["GRBK"], [])

    monkeypatch.setattr(
        material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block("acc-2")}
    )
    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1


def test_material_event_buy_blocks_alerts_only_for_the_new_ticker_in_a_mixed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(
        material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block("acc-1")}
    )
    trading_common.check_material_event_buy_blocks(["GRBK"], [])

    monkeypatch.setattr(
        material_events,
        "check_buy_candidates",
        lambda tickers: {"GRBK": _block("acc-1"), "NEWCO": _block("acc-2")},
    )
    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK", "NEWCO"], alerts)

    assert blocked == {"GRBK", "NEWCO"}  # both still excluded
    assert len(alerts) == 1
    assert "NEWCO" in alerts[0]
    assert "GRBK" not in alerts[0]  # already reported, not repeated


def test_material_event_buy_blocks_is_a_noop_when_nothing_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {})
    alerts: list[str] = []

    assert trading_common.check_material_event_buy_blocks(["AAPL"], alerts) == set()
    assert alerts == []


def test_material_event_buy_blocks_dedup_is_per_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same filing blocking the same ticker on a DIFFERENT account is a
    # separate operator-facing fact, so it alerts again rather than being
    # suppressed by another account's earlier alert.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    monkeypatch.setattr(config, "ACCOUNT_LABEL", "paper")
    first: list[str] = []
    trading_common.check_material_event_buy_blocks(["GRBK"], first)
    monkeypatch.setattr(config, "ACCOUNT_LABEL", "live")
    second: list[str] = []
    trading_common.check_material_event_buy_blocks(["GRBK"], second)

    assert len(first) == 1
    assert len(second) == 1


def test_material_event_buy_blocks_still_alerts_when_the_journal_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # staff-engineer finding: an unreadable journal must fail toward
    # telling the operator, never toward silence -- the block applies
    # either way, so only the notification is at stake.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    def _boom(*_args: object, **_kwargs: object) -> str | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal, "get_buy_block_last_alerted", _boom)
    alerts: list[str] = []

    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1


def test_material_event_buy_blocks_one_tickers_journal_failure_does_not_swallow_another_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # staff-engineer finding: per-ticker fault isolation. A write failure
    # on the first ticker must not prevent the second ticker's alert.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(
        material_events,
        "check_buy_candidates",
        lambda tickers: {"AAA": _block("acc-1"), "BBB": _block("acc-2")},
    )

    def _boom_on_aaa(ticker: str, accession_number: str, account: str | None = None) -> None:
        if ticker == "AAA":
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal, "record_buy_block_alert", _boom_on_aaa)
    alerts: list[str] = []

    blocked = trading_common.check_material_event_buy_blocks(["AAA", "BBB"], alerts)

    assert blocked == {"AAA", "BBB"}
    assert len(alerts) == 2  # both alerted despite AAA's write failing


def test_material_event_buy_blocks_realerts_on_an_unusable_stored_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pm-reviewer finding: _realert_interval_elapsed claimed a
    # fail-toward-alerting posture but only caught ValueError, so a
    # timezone-NAIVE stored string (parses fine, then raises TypeError on
    # the subtraction) would have failed the run instead of re-alerting.
    # Unreachable via the normal write path; pinned here so the documented
    # guarantee doesn't silently depend on that staying true.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    with journal._connect() as conn:
        conn.execute(
            "UPDATE material_event_buy_blocks SET last_alerted_timestamp = ?",
            ("2026-01-01T00:00:00",),  # naive -- no tzinfo
        )

    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1  # re-alerted, did not raise

    with journal._connect() as conn:
        conn.execute(
            "UPDATE material_event_buy_blocks SET last_alerted_timestamp = ?", ("not-a-date",)
        )
    more_alerts: list[str] = []
    assert trading_common.check_material_event_buy_blocks(["GRBK"], more_alerts) == {"GRBK"}
    assert len(more_alerts) == 1


def test_material_event_buy_blocks_alerts_loudly_when_the_check_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # staff-engineer-reviewer (2nd pass): the gate's single fail-open point.
    # A SEC ticker-index outage resolves zero CIKs, which would otherwise
    # look identical to a clean run now that suppression is the steady
    # state. Must alert, and must not be mistaken for "nothing blocked".
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")

    def _unavailable(tickers: list[str]) -> dict[str, material_events.BuyBlock]:
        raise material_events.MaterialEventCheckUnavailableError("no CIK resolved for any of 3")

    monkeypatch.setattr(material_events, "check_buy_candidates", _unavailable)
    alerts: list[str] = []

    blocked = trading_common.check_material_event_buy_blocks(["A", "B", "C"], alerts)

    assert blocked == set()
    assert len(alerts) == 1
    assert "UNGATED" in alerts[0]  # says plainly that buys were not screened


def test_material_event_buy_blocks_realerts_on_a_future_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # staff-engineer-reviewer (2nd pass): a future last_alerted would
    # otherwise suppress silently until wall-clock caught up -- the same
    # outcome as the silent-failure bug, by a different door. Reachable by
    # hand-editing the column, the obvious ad-hoc snooze available today.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    trading_common.check_material_event_buy_blocks(["GRBK"], [])
    future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=200)).isoformat()
    with journal._connect() as conn:
        conn.execute("UPDATE material_event_buy_blocks SET last_alerted_timestamp = ?", (future,))

    alerts: list[str] = []
    blocked = trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert blocked == {"GRBK"}
    assert len(alerts) == 1  # re-alerted rather than going silent for 200 days


def test_material_event_buy_blocks_says_so_when_the_alert_history_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # staff-engineer-reviewer (2nd pass): a persistently broken journal
    # re-alerts every run, which is the safe direction -- but it must not
    # use the first-discovery wording, or it reads as a genuinely new
    # Critical filing every single time.
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(material_events, "check_buy_candidates", lambda tickers: {"GRBK": _block()})

    def _boom(*_args: object, **_kwargs: object) -> str | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal, "get_buy_block_last_alerted", _boom)
    alerts: list[str] = []

    trading_common.check_material_event_buy_blocks(["GRBK"], alerts)

    assert len(alerts) == 1
    assert "unreadable" in alerts[0]  # distinguishable from a real first discovery
