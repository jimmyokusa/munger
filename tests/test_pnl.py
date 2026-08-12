"""Unit tests for pnl.py, mocking the Alpaca SDK (same pattern as
test_execution.py -- no live credentials available in this environment)."""

from __future__ import annotations

import datetime
import json
import time
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.models import Clock, PortfolioHistory, Position, TradeAccount

import config
import pnl


def _fake_account(**overrides: Any) -> MagicMock:
    account = MagicMock(spec=TradeAccount)
    defaults: dict[str, Any] = {
        "equity": 100_000.0,
        "cash": 25_000.0,
        "last_equity": 99_500.0,
        "portfolio_value": 100_000.0,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(account, key, value)
    return account


def _fake_position(**overrides: Any) -> MagicMock:
    position = MagicMock(spec=Position)
    defaults: dict[str, Any] = {
        "symbol": "AAPL",
        "qty": 10.0,
        "avg_entry_price": 150.0,
        "current_price": 160.0,
        "market_value": 1_600.0,
        "cost_basis": 1_500.0,
        "unrealized_pl": 100.0,
        "unrealized_plpc": 0.0667,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(position, key, value)
    return position


def _fake_history(**overrides: Any) -> MagicMock:
    history = MagicMock(spec=PortfolioHistory)
    defaults: dict[str, Any] = {
        "timestamp": [1721952000, 1722038400],
        "equity": [99_500.0, 100_000.0],
        "profit_loss": [-100.0, 400.0],
        "profit_loss_pct": [-0.001, 0.004],
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(history, key, value)
    return history


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", True)


def _patch_trading_client(monkeypatch: pytest.MonkeyPatch, trading_mock: MagicMock) -> None:
    monkeypatch.setattr(pnl, "TradingClient", lambda **kwargs: trading_mock)


def test_generate_snapshot_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_mock = MagicMock()
    trading_mock.get_account.return_value = _fake_account()
    trading_mock.get_all_positions.return_value = [_fake_position()]
    trading_mock.get_portfolio_history.return_value = _fake_history()
    _patch_trading_client(monkeypatch, trading_mock)

    snapshot = pnl.generate_snapshot()

    assert snapshot["mode"] == "paper"
    assert snapshot["account"] == {
        "equity": 100_000.0,
        "cash": 25_000.0,
        "last_equity": 99_500.0,
        "portfolio_value": 100_000.0,
    }
    assert snapshot["positions"] == [
        {
            "symbol": "AAPL",
            "qty": 10.0,
            "avg_entry_price": 150.0,
            "current_price": 160.0,
            "market_value": 1_600.0,
            "cost_basis": 1_500.0,
            "unrealized_pl": 100.0,
            "unrealized_plpc": 0.0667,
        }
    ]
    history = snapshot["history"]
    assert isinstance(history, dict)
    assert history["equity"] == [99_500.0, 100_000.0]
    assert "generated_at" in snapshot


def test_generate_snapshot_reports_live_mode_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    trading_mock = MagicMock()
    trading_mock.get_account.return_value = _fake_account()
    trading_mock.get_all_positions.return_value = []
    trading_mock.get_portfolio_history.return_value = _fake_history()
    _patch_trading_client(monkeypatch, trading_mock)

    snapshot = pnl.generate_snapshot()

    # "Extensible to real money" (user request) means this module must
    # never hardcode "paper" -- it reports whichever mode config actually
    # says, the same account-agnostic pattern execution.py already uses.
    assert snapshot["mode"] == "live"


def test_generate_snapshot_fails_closed_on_unexpected_account_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_account.return_value = {"not": "a TradeAccount"}
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ValueError, match="unexpected get_account"):
        pnl.generate_snapshot()


def test_generate_snapshot_fails_closed_on_unexpected_history_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_account.return_value = _fake_account()
    trading_mock.get_all_positions.return_value = []
    trading_mock.get_portfolio_history.return_value = {"not": "a PortfolioHistory"}
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ValueError, match="unexpected get_portfolio_history"):
        pnl.generate_snapshot()


def test_generate_snapshot_fails_closed_on_a_mixed_validity_positions_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real gap found in review: an earlier draft silently filtered out
    # any get_all_positions() entry that wasn't a real Position (e.g. a
    # schema drift or a partially malformed response), understating real
    # holdings while looking like an ordinary few-positions day with
    # nothing logged. Must abort the whole snapshot instead, same
    # fail-closed posture as the account/history checks.
    trading_mock = MagicMock()
    trading_mock.get_account.return_value = _fake_account()
    trading_mock.get_all_positions.return_value = [_fake_position(), {"not": "a Position"}]
    trading_mock.get_portfolio_history.return_value = _fake_history()
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ValueError, match="unexpected get_all_positions"):
        pnl.generate_snapshot()


def test_position_snapshot_maps_none_fields_to_none() -> None:
    # A position with a genuinely missing field (SDK types several
    # Position fields as optional) must map to JSON null, not crash or
    # silently coerce to 0 -- a real unrealized_pl of $0 and a missing
    # one are different facts.
    position = _fake_position(unrealized_pl=None, unrealized_plpc=None)

    result = pnl._position_snapshot(position)

    assert result["unrealized_pl"] is None
    assert result["unrealized_plpc"] is None
    assert result["symbol"] == "AAPL"


def test_history_snapshot_preserves_none_positionally() -> None:
    # A day with no data yet (e.g. account just opened) can appear as a
    # None entry mid-list -- it must stay at its own index, not be
    # dropped, or a consumer zipping timestamp[i] with equity[i] would
    # silently misalign every point after it.
    history = _fake_history(equity=[100_000.0, None, 100_500.0])

    result = pnl._history_snapshot(history)

    assert result["equity"] == [100_000.0, None, 100_500.0]


def test_write_snapshot_writes_atomically_with_no_leftover_tmp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixed_snapshot = {"mode": "paper", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    target = tmp_path / "pnl.json"

    pnl.write_snapshot(target)

    assert json.loads(target.read_text()) == fixed_snapshot
    assert not target.with_suffix(".json.tmp").exists()


def test_write_snapshot_uses_config_path_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "pnl.json")
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: {"mode": "paper"})

    pnl.write_snapshot()

    assert (tmp_path / "pnl.json").exists()
    assert json.loads((tmp_path / "pnl.json").read_text()) == {"mode": "paper"}


def test_write_snapshot_accepts_a_prefetched_snapshot_without_fetching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # __main__ fetches once and hands the same snapshot to both write_snapshot
    # and update_history_series -- passing it in must NOT trigger a second
    # Alpaca read.
    def _fail() -> dict[str, object]:
        pytest.fail("write_snapshot fetched instead of using the given snapshot")

    monkeypatch.setattr(pnl, "generate_snapshot", _fail)
    target = tmp_path / "pnl.json"

    pnl.write_snapshot(path=target, snapshot={"mode": "paper"})

    assert json.loads(target.read_text()) == {"mode": "paper"}


# --- M19: intraday refresh (retry, market-hours gate, snapshot-only) ---


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every retry test below deliberately triggers pnl._with_retry's sleep
    # -- patched to a no-op globally so the suite doesn't actually wait
    # config.PNL_ALPACA_RETRY_DELAY_SECONDS on every one of them.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def _fake_clock(is_open: bool) -> MagicMock:
    clock = MagicMock(spec=Clock)
    clock.is_open = is_open
    return clock


def test_with_retry_returns_first_success_without_retrying() -> None:
    calls = []

    def fetch() -> str:
        calls.append(1)
        return "ok"

    assert pnl._with_retry(fetch, "test") == "ok"
    assert len(calls) == 1


def test_with_retry_retries_once_after_a_transient_failure() -> None:
    calls = []

    def fetch() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("transient blip")
        return "ok"

    assert pnl._with_retry(fetch, "test") == "ok"
    assert len(calls) == 2


def test_with_retry_fails_closed_after_a_second_failure() -> None:
    # A persistently broken bridge must still fail loud after the retry is
    # exhausted -- this is a reduction in false-positive noise, not a
    # blanket suppression of real failures.
    def fetch() -> str:
        raise ConnectionError("still broken")

    with pytest.raises(ConnectionError, match="still broken"):
        pnl._with_retry(fetch, "test")


def test_market_is_open_true(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = _fake_clock(True)
    _patch_trading_client(monkeypatch, trading_mock)

    assert pnl.market_is_open() is True


def test_market_is_open_false(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = _fake_clock(False)
    _patch_trading_client(monkeypatch, trading_mock)

    assert pnl.market_is_open() is False


def test_market_is_open_fails_closed_on_unexpected_clock_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.return_value = {"not": "a Clock"}
    _patch_trading_client(monkeypatch, trading_mock)

    with pytest.raises(ValueError, match="unexpected get_clock"):
        pnl.market_is_open()


def test_market_is_open_retries_a_transient_clock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_clock.side_effect = [ConnectionError("blip"), _fake_clock(True)]
    _patch_trading_client(monkeypatch, trading_mock)

    assert pnl.market_is_open() is True
    assert trading_mock.get_clock.call_count == 2


def test_generate_snapshot_retries_a_transient_get_account_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trading_mock = MagicMock()
    trading_mock.get_account.side_effect = [ConnectionError("blip"), _fake_account()]
    trading_mock.get_all_positions.return_value = []
    trading_mock.get_portfolio_history.return_value = _fake_history()
    _patch_trading_client(monkeypatch, trading_mock)

    snapshot = pnl.generate_snapshot()

    account = snapshot["account"]
    assert isinstance(account, dict)
    assert account["equity"] == 100_000.0
    assert trading_mock.get_account.call_count == 2


def test_main_market_hours_only_skips_the_fetch_when_market_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr(pnl, "market_is_open", lambda: False)

    def _fail() -> dict[str, object]:
        pytest.fail("generate_snapshot called despite the market being closed")

    monkeypatch.setattr(pnl, "generate_snapshot", _fail)

    pnl.main()  # must return cleanly without ever calling generate_snapshot


def test_main_market_hours_only_runs_when_market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", True)
    monkeypatch.setattr(config, "PNL_SNAPSHOT_ONLY", False)
    monkeypatch.setattr(pnl, "market_is_open", lambda: True)
    fixed_snapshot = {"mode": "paper", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    write_mock = MagicMock()
    history_mock = MagicMock()
    monkeypatch.setattr(pnl, "write_snapshot", write_mock)
    monkeypatch.setattr(pnl, "update_history_series", history_mock)

    pnl.main()

    write_mock.assert_called_once_with(snapshot=fixed_snapshot)
    history_mock.assert_called_once_with(fixed_snapshot)


def test_main_snapshot_only_skips_the_durable_history_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M19's core safety property: the intraday cron must never touch
    # pnl_history.jsonl (see config.PNL_SNAPSHOT_ONLY's own comment for
    # why an unguarded upsert here would risk truncating the durable
    # multi-month series on an ephemeral GitHub Actions runner).
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", False)
    monkeypatch.setattr(config, "PNL_SNAPSHOT_ONLY", True)
    fixed_snapshot = {"mode": "paper", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    monkeypatch.setattr(pnl, "write_snapshot", MagicMock())

    def _fail(snapshot: dict[str, object]) -> None:
        pytest.fail("update_history_series called despite PNL_SNAPSHOT_ONLY")

    monkeypatch.setattr(pnl, "update_history_series", _fail)

    pnl.main()  # must not raise -- proves update_history_series was never called


def test_main_default_flags_run_the_full_flow_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # daily-trade.yml never sets either M19 env var -- both config flags
    # default False, so `main()` must behave exactly as it did before this
    # milestone: no market-hours gating, durable history always upserted.
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", False)
    monkeypatch.setattr(config, "PNL_SNAPSHOT_ONLY", False)
    fixed_snapshot = {"mode": "paper", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    write_mock = MagicMock()
    history_mock = MagicMock()
    monkeypatch.setattr(pnl, "write_snapshot", write_mock)
    monkeypatch.setattr(pnl, "update_history_series", history_mock)

    pnl.main()

    write_mock.assert_called_once_with(snapshot=fixed_snapshot)
    history_mock.assert_called_once_with(fixed_snapshot)


# --- M20: Discord loss alerts (user request) ---


def _position(symbol: str, plpc: float | None, **overrides: Any) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": symbol,
        "qty": 10.0,
        "avg_entry_price": 100.0,
        "current_price": 90.0,
        "market_value": 900.0,
        "cost_basis": 1_000.0,
        "unrealized_pl": -100.0,
        "unrealized_plpc": plpc,
    }
    base.update(overrides)
    return base


def test_breached_positions_filters_at_or_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "POSITION_LOSS_ALERT_THRESHOLD_PCT", -0.10)
    positions = [
        _position("DOWN_BAD", -0.15),
        _position("DOWN_EXACTLY", -0.10),  # at, not just past, the threshold
        _position("DOWN_MILD", -0.05),
        _position("UP", 0.05),
    ]

    breached = pnl._breached_positions(positions)

    assert {p["symbol"] for p in breached} == {"DOWN_BAD", "DOWN_EXACTLY"}


def test_breached_positions_ignores_missing_or_non_numeric_plpc() -> None:
    # A position with no unrealized_plpc yet (e.g. a corner case in the
    # snapshot) must not crash the comparison or be silently treated as a
    # breach.
    positions = [_position("NO_DATA", None)]
    assert pnl._breached_positions(positions) == []


def test_format_loss_alert_includes_symbol_pct_dollars_and_price() -> None:
    breached = [_position("AAPL", -0.12, unrealized_pl=-120.0, current_price=88.0)]
    message = pnl._format_loss_alert("live", breached)
    assert "LIVE" in message
    assert "AAPL" in message
    assert "-12.0%" in message
    assert "-$120.00" in message
    assert "$88.00" in message


def test_format_loss_alert_sorts_by_symbol() -> None:
    breached = [_position("ZZZZ", -0.20), _position("AAAA", -0.15)]
    message = pnl._format_loss_alert("paper", breached)
    assert message.index("AAAA") < message.index("ZZZZ")


def test_send_discord_alert_posts_content_to_the_configured_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    pnl._send_discord_alert("test message")

    assert captured["url"] == "https://discord.example/webhook"
    assert captured["body"] == {"content": "test message"}
    # Real bug found live (M22): Discord's API is behind Cloudflare, which
    # blocks urllib's default User-Agent as a bot signature (403, error
    # code 1010) -- silently swallowed by this function's own except
    # clause since the day it shipped. A non-default User-Agent is
    # required, not optional. staff-engineer-reviewer finding: comparing
    # only against config.DISCORD_USER_AGENT itself can't catch that
    # constant degrading to empty/default alongside a future refactor --
    # assert a hardcoded expected literal (independent of the config
    # value) and explicitly rule out urllib's own default UA string.
    sent_user_agent = captured["headers"]["User-agent"]
    assert sent_user_agent == "munger-trading-bot/1.0 (+https://gramunger.com)"
    assert not sent_user_agent.startswith("Python-urllib")


def test_send_discord_alert_swallows_a_failed_post(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Discord/network hiccup must not crash the whole P&L pipeline over
    # a missed notification -- unlike this module's core snapshot
    # generation, which does fail closed by design.
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    def _raise(*args: object, **kwargs: object) -> None:
        raise TimeoutError("simulated network blip")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    pnl._send_discord_alert("test message")  # must not raise


def test_check_position_loss_alerts_noop_when_webhook_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    sent: list[str] = []
    monkeypatch.setattr(pnl, "_send_discord_alert", lambda message: sent.append(message))

    pnl.check_position_loss_alerts({"mode": "paper", "positions": [_position("AAPL", -0.50)]})

    assert sent == []


def test_check_position_loss_alerts_noop_when_nothing_breached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    sent: list[str] = []
    monkeypatch.setattr(pnl, "_send_discord_alert", lambda message: sent.append(message))

    pnl.check_position_loss_alerts({"mode": "paper", "positions": [_position("AAPL", -0.01)]})

    assert sent == []


def test_check_position_loss_alerts_sends_when_breached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    sent: list[str] = []
    monkeypatch.setattr(pnl, "_send_discord_alert", lambda message: sent.append(message))

    pnl.check_position_loss_alerts({"mode": "live", "positions": [_position("AAPL", -0.50)]})

    assert len(sent) == 1
    assert "AAPL" in sent[0]


def test_main_skips_loss_alert_check_when_snapshot_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # M20: the intraday cron (pnl-snapshot.yml) sets PNL_SNAPSHOT_ONLY and
    # runs every 30 min for paper -- checking there too would mean up to
    # 13 Discord messages/day for one position that stays down.
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", False)
    monkeypatch.setattr(config, "PNL_SNAPSHOT_ONLY", True)
    fixed_snapshot = {"mode": "paper", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    monkeypatch.setattr(pnl, "write_snapshot", MagicMock())

    def _fail(snapshot: dict[str, object]) -> None:
        pytest.fail("check_position_loss_alerts called despite PNL_SNAPSHOT_ONLY")

    monkeypatch.setattr(pnl, "check_position_loss_alerts", _fail)

    pnl.main()  # must not raise -- proves the alert check was never called


def test_main_runs_loss_alert_check_on_canonical_daily_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PNL_MARKET_HOURS_ONLY", False)
    monkeypatch.setattr(config, "PNL_SNAPSHOT_ONLY", False)
    fixed_snapshot = {"mode": "live", "account": {}, "positions": [], "history": {}}
    monkeypatch.setattr(pnl, "generate_snapshot", lambda: fixed_snapshot)
    monkeypatch.setattr(pnl, "write_snapshot", MagicMock())
    monkeypatch.setattr(pnl, "update_history_series", MagicMock())
    checked: list[dict[str, object]] = []
    monkeypatch.setattr(pnl, "check_position_loss_alerts", checked.append)

    pnl.main()

    assert checked == [fixed_snapshot]


# --- Durable P&L append-series (M17, DESIGN_DASHBOARDS.md 2.1) ---


def _epoch(date_str: str) -> int:
    """UTC-midnight epoch seconds for an ISO date, matching Alpaca's history
    timestamp units."""
    return int(datetime.datetime.fromisoformat(date_str + "T00:00:00+00:00").timestamp())


def _series_snapshot(
    *,
    generated_at: str,
    equity: float,
    last_equity: float,
    timestamps: list[int | None],
    equities: list[float | None],
    profit_losses: list[float | None],
    mode: str = "paper",
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "mode": mode,
        "account": {
            "equity": equity,
            "cash": 0.0,
            "last_equity": last_equity,
            "portfolio_value": equity,
        },
        "positions": [],
        "history": {
            "timestamp": timestamps,
            "equity": equities,
            "profit_loss": profit_losses,
            "profit_loss_pct": [],
        },
    }


def _read_series(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_update_history_series_seeds_from_history_and_appends_today_sorted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_500.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-26"), _epoch("2026-07-27")],
        equities=[99_500.0, 100_000.0],
        profit_losses=[-100.0, 500.0],
    )

    pnl.update_history_series(snap, path)

    rows = _read_series(path)
    assert [r["date"] for r in rows] == ["2026-07-26", "2026-07-27", "2026-07-28"]
    assert rows[0] == {
        "date": "2026-07-26",
        "equity": 99_500.0,
        "profit_loss": -100.0,
        "mode": "paper",
    }
    # today's row is derived from the account snapshot (equity, and
    # profit_loss = equity - last_equity), not from the history array
    assert rows[-1] == {
        "date": "2026-07-28",
        "equity": 100_500.0,
        "profit_loss": 500.0,
        "mode": "paper",
    }


def test_update_history_series_is_idempotent_on_rerun(tmp_path: Path) -> None:
    path = tmp_path / "pnl_history.jsonl"
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_000.0,
        last_equity=99_000.0,
        timestamps=[_epoch("2026-07-27")],
        equities=[99_000.0],
        profit_losses=[0.0],
    )

    pnl.update_history_series(snap, path)
    first = path.read_text()
    pnl.update_history_series(snap, path)

    # re-running the same day upserts by date -- no duplicate rows appended
    assert path.read_text() == first


def test_update_history_series_preserves_days_outside_alpacas_window(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    # a day already recorded that Alpaca's rolling 1-month history no longer
    # returns -- the whole reason this durable file exists
    old_row = {"date": "2026-01-01", "equity": 90_000.0, "profit_loss": 0.0, "mode": "paper"}
    path.write_text(json.dumps(old_row) + "\n")
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_500.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-27")],
        equities=[100_000.0],
        profit_losses=[500.0],
    )

    pnl.update_history_series(snap, path)

    assert [r["date"] for r in _read_series(path)] == ["2026-01-01", "2026-07-27", "2026-07-28"]


def test_update_history_series_todays_row_comes_from_account_not_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    # history carries a stale/partial same-day point; the fresher account
    # value must win for today
    snap = _series_snapshot(
        generated_at="2026-07-28T20:00:00+00:00",
        equity=101_000.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-28")],
        equities=[100_200.0],
        profit_losses=[200.0],
    )

    pnl.update_history_series(snap, path)

    rows = {r["date"]: r for r in _read_series(path)}
    assert rows["2026-07-28"]["equity"] == 101_000.0
    assert rows["2026-07-28"]["profit_loss"] == 1_000.0


def test_update_history_series_skips_history_days_with_no_data(tmp_path: Path) -> None:
    path = tmp_path / "pnl_history.jsonl"
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_000.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-26"), _epoch("2026-07-27")],
        equities=[None, 100_000.0],
        profit_losses=[None, 0.0],
    )

    pnl.update_history_series(snap, path)

    # a None-equity day is skipped, never fabricated as a $0 point
    assert [r["date"] for r in _read_series(path)] == ["2026-07-27", "2026-07-28"]


def test_update_history_series_does_not_relabel_prior_days_after_a_mode_switch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    paper_snap = _series_snapshot(
        generated_at="2026-07-27T13:00:00+00:00",
        equity=100_000.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-26")],
        equities=[99_500.0],
        profit_losses=[-500.0],
        mode="paper",
    )
    pnl.update_history_series(paper_snap, path)

    # the account flips to live; a later run's snapshot reports "live" as the
    # *current* mode, but 2026-07-26/27 genuinely happened under paper -- a
    # re-run must not retroactively relabel them, or the durable history
    # would show fabricated live-trading returns for days that were paper.
    live_snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_800.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-26"), _epoch("2026-07-27")],
        equities=[99_500.0, 100_000.0],
        profit_losses=[-500.0, 0.0],
        mode="live",
    )
    pnl.update_history_series(live_snap, path)

    rows = {r["date"]: r for r in _read_series(path)}
    assert rows["2026-07-26"]["mode"] == "paper"
    assert rows["2026-07-27"]["mode"] == "paper"
    assert rows["2026-07-28"]["mode"] == "live"


def test_update_history_series_keeps_a_point_with_a_short_profit_loss_array(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    # a degraded Alpaca response one entry short on profit_loss must not drop
    # the trailing (most recent) timestamp/equity point -- it should keep the
    # point with profit_loss recorded as null, not lose it outright.
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_000.0,
        last_equity=100_000.0,
        timestamps=[_epoch("2026-07-26"), _epoch("2026-07-27")],
        equities=[99_500.0, 100_000.0],
        profit_losses=[-500.0],
    )

    pnl.update_history_series(snap, path)

    rows = {r["date"]: r for r in _read_series(path)}
    assert rows["2026-07-27"]["equity"] == 100_000.0
    assert rows["2026-07-27"]["profit_loss"] is None


def test_update_history_series_writes_atomically_with_no_leftover_tmp_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pnl_history.jsonl"
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_000.0,
        last_equity=100_000.0,
        timestamps=[],
        equities=[],
        profit_losses=[],
    )

    pnl.update_history_series(snap, path)

    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_update_history_series_uses_config_path_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "PNL_HISTORY_PATH", tmp_path / "pnl_history.jsonl")
    snap = _series_snapshot(
        generated_at="2026-07-28T13:00:00+00:00",
        equity=100_000.0,
        last_equity=100_000.0,
        timestamps=[],
        equities=[],
        profit_losses=[],
    )

    pnl.update_history_series(snap)

    assert (tmp_path / "pnl_history.jsonl").exists()
