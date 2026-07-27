"""Unit tests for pnl.py, mocking the Alpaca SDK (same pattern as
test_execution.py -- no live credentials available in this environment)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.models import PortfolioHistory, Position, TradeAccount

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
