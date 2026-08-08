"""Unit tests for prices.py, mocking the Alpaca SDK (same pattern as
test_pnl.py -- no live credentials available in this environment)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.models import Bar, BarSet

import config
import prices


def _fake_bar(date: str, close: float) -> MagicMock:
    bar = MagicMock(spec=Bar)
    bar.timestamp = datetime.datetime.fromisoformat(f"{date}T00:00:00+00:00")
    bar.close = close
    return bar


def _fake_bar_set(data: dict[str, list[MagicMock]]) -> MagicMock:
    bar_set = MagicMock(spec=BarSet)
    bar_set.data = data
    return bar_set


def _patch_data_client(monkeypatch: pytest.MonkeyPatch, client_mock: MagicMock) -> None:
    monkeypatch.setattr(prices, "StockHistoricalDataClient", lambda **kwargs: client_mock)


def _symbol_entry(result: dict[str, object], symbol: str) -> dict[str, object]:
    """Narrows fetch_daily_closes()'s result["symbols"][symbol] for assertions.

    result is typed dict[str, object] (JSON-safe, matching pnl.py's own
    snapshot convention) -- this indexes the shape prices.py actually
    constructs, which unlike arbitrary external JSON is a known, trusted
    contract here.
    """
    symbols = result["symbols"]
    assert isinstance(symbols, dict)
    entry = symbols[symbol]
    assert isinstance(entry, dict)
    return entry


def test_held_symbols_extracts_sorts_and_dedupes() -> None:
    snapshot: dict[str, object] = {
        "positions": [
            {"symbol": "MSFT", "qty": 1.0},
            {"symbol": "AAPL", "qty": 2.0},
            {"symbol": "AAPL", "qty": 2.0},
        ]
    }

    assert prices.held_symbols(snapshot) == ["AAPL", "MSFT"]


def test_held_symbols_handles_no_positions() -> None:
    assert prices.held_symbols({}) == []
    assert prices.held_symbols({"positions": []}) == []


def test_held_symbols_skips_malformed_entries() -> None:
    # A schema drift or partial record shouldn't sink the whole list --
    # matches pnl.py's own tolerance for a single bad position record.
    snapshot: dict[str, object] = {
        "positions": [
            {"symbol": "AAPL"},
            {"qty": 5.0},  # missing symbol entirely
            {"symbol": None},
            {"symbol": ""},
            "not-even-a-dict",
        ]
    }

    assert prices.held_symbols(snapshot) == ["AAPL"]


def test_fetch_daily_closes_with_no_symbols_returns_empty_without_calling_alpaca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(**kwargs: Any) -> MagicMock:
        pytest.fail("StockHistoricalDataClient constructed with no symbols to fetch")

    monkeypatch.setattr(prices, "StockHistoricalDataClient", _fail)

    result = prices.fetch_daily_closes([])

    assert result["symbols"] == {}
    assert "generated_at" in result


def test_fetch_daily_closes_builds_points_and_requests_the_configured_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set(
        {"AAPL": [_fake_bar("2026-08-05", 200.0), _fake_bar("2026-08-06", 202.5)]}
    )
    _patch_data_client(monkeypatch, client_mock)
    monkeypatch.setattr(config, "PRICES_DATA_FEED", "iex")

    result = prices.fetch_daily_closes(["AAPL"])

    assert _symbol_entry(result, "AAPL") == {
        "status": "ok",
        "points": [
            {"date": "2026-08-05", "close": 200.0},
            {"date": "2026-08-06", "close": 202.5},
        ],
        "fail_reasons": [],
    }
    request = client_mock.get_stock_bars.call_args.args[0]
    assert request.feed == DataFeed.IEX


def test_fetch_daily_closes_marks_a_symbol_with_no_bars_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set({"ZZZZ": []})
    _patch_data_client(monkeypatch, client_mock)

    result = prices.fetch_daily_closes(["ZZZZ"])

    assert _symbol_entry(result, "ZZZZ") == {"status": "no_bars", "points": [], "fail_reasons": []}


def test_fetch_daily_closes_rejects_nan_and_inf_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real bug found in review: NaN compares False to every <=/> comparison,
    # so a plain `close <= 0 or close > ceiling` check silently passes a
    # NaN through -- which then serializes as the literal (non-standard-
    # JSON) token `NaN` via json.dumps' default allow_nan=True, breaking a
    # strict JSON parser (plausibly Grafana's Infinity datasource) on the
    # *entire* file, not just that one point.
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set(
        {
            "AAPL": [
                _fake_bar("2026-08-05", float("nan")),
                _fake_bar("2026-08-06", float("inf")),
                _fake_bar("2026-08-07", 200.0),
            ]
        }
    )
    _patch_data_client(monkeypatch, client_mock)

    result = prices.fetch_daily_closes(["AAPL"])

    entry = _symbol_entry(result, "AAPL")
    assert entry["points"] == [{"date": "2026-08-07", "close": 200.0}]
    assert entry["fail_reasons"] == [
        "data_invalid_outlier:AAPL:close",
        "data_invalid_outlier:AAPL:close",
    ]
    # The bug's actual symptom: a NaN/inf point that slipped through would
    # make the whole snapshot fail strict JSON serialization once written.
    json.dumps(result, allow_nan=False)


def test_fetch_daily_closes_rejects_a_zero_or_negative_close_as_outlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set(
        {
            "AAPL": [
                _fake_bar("2026-08-05", 0.0),
                _fake_bar("2026-08-06", -5.0),
                _fake_bar("2026-08-07", 200.0),
            ]
        }
    )
    _patch_data_client(monkeypatch, client_mock)

    result = prices.fetch_daily_closes(["AAPL"])

    entry = _symbol_entry(result, "AAPL")
    assert entry["status"] == "ok"
    assert entry["points"] == [{"date": "2026-08-07", "close": 200.0}]
    assert entry["fail_reasons"] == [
        "data_invalid_outlier:AAPL:close",
        "data_invalid_outlier:AAPL:close",
    ]


def test_fetch_daily_closes_rejects_an_implausibly_high_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set(
        {"AAPL": [_fake_bar("2026-08-05", config.PRICES_MAX_PLAUSIBLE_CLOSE + 1)]}
    )
    _patch_data_client(monkeypatch, client_mock)

    result = prices.fetch_daily_closes(["AAPL"])

    assert _symbol_entry(result, "AAPL")["fail_reasons"] == ["data_invalid_outlier:AAPL:close"]


def test_fetch_daily_closes_all_points_invalid_yields_no_valid_bars_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_mock = MagicMock()
    client_mock.get_stock_bars.return_value = _fake_bar_set(
        {"AAPL": [_fake_bar("2026-08-05", 0.0)]}
    )
    _patch_data_client(monkeypatch, client_mock)

    result = prices.fetch_daily_closes(["AAPL"])

    entry = _symbol_entry(result, "AAPL")
    assert entry["status"] == "no_valid_bars"
    assert entry["points"] == []


def test_generate_snapshot_fetches_only_held_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    pnl_snapshot: dict[str, object] = {"positions": [{"symbol": "TSLA"}]}
    monkeypatch.setattr(
        prices,
        "fetch_daily_closes",
        lambda symbols: {"generated_at": "x", "symbols": dict.fromkeys(symbols, "stub")},
    )

    result = prices.generate_snapshot(pnl_snapshot)

    assert result["symbols"] == {"TSLA": "stub"}


def test_write_snapshot_writes_atomically_with_no_leftover_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "prices.json"
    fixed_snapshot: dict[str, object] = {"generated_at": "x", "symbols": {}}

    prices.write_snapshot(fixed_snapshot, path=target)

    assert json.loads(target.read_text()) == fixed_snapshot
    assert not target.with_suffix(".json.tmp").exists()


def test_write_snapshot_uses_config_path_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "PRICES_DATA_PATH", tmp_path / "prices.json")

    prices.write_snapshot({"generated_at": "x", "symbols": {}})

    assert (tmp_path / "prices.json").exists()
