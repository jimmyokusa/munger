"""Unit tests for data.py's pure functions and fetch orchestration
(DESIGN.md section 6, layer 1)."""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yfinance as yf

import config
import data


class _FakeTicker:
    """Stand-in for yf.Ticker, monkeypatched onto the yfinance module directly
    (not via data.yf) so mypy's implicit-reexport check stays happy and the
    patch is scoped to yfinance itself, same as production code sees it."""

    def __init__(
        self,
        info: dict[str, Any] | None = None,
        income_stmt: pd.DataFrame | None = None,
        raise_on_income_stmt: bool = False,
    ) -> None:
        self._info = info if info is not None else {}
        self._income_stmt = income_stmt
        self._raise_on_income_stmt = raise_on_income_stmt

    @property
    def info(self) -> dict[str, Any]:
        return self._info

    @property
    def income_stmt(self) -> pd.DataFrame | None:
        if self._raise_on_income_stmt:
            raise RuntimeError("no financials available")
        return self._income_stmt


def test_normalize_percent_field_converts_to_decimal_fraction() -> None:
    assert data._normalize_percent_field({"debtToEquity": 79.548}, "debtToEquity") == pytest.approx(
        0.79548
    )


def test_normalize_percent_field_handles_missing_key() -> None:
    assert data._normalize_percent_field({}, "debtToEquity") is None


def test_normalize_percent_field_handles_non_numeric_value_without_raising() -> None:
    # A malformed field must degrade to "missing," not propagate an
    # exception -- this runs inside fetch_metrics' retry loop, and letting
    # it raise would burn all retries and discard the whole ticker's
    # otherwise-good data over one bad value.
    assert data._normalize_percent_field({"debtToEquity": "not a number"}, "debtToEquity") is None


def test_consecutive_positive_years_all_positive() -> None:
    net_income = pd.Series(
        {"2025": 100.0, "2024": 90.0, "2023": 95.0, "2022": 99.0},
    )
    assert data._consecutive_positive_years(net_income) == 4


def test_consecutive_positive_years_stops_at_first_non_positive() -> None:
    net_income = pd.Series({"2025": 100.0, "2024": -10.0, "2023": 95.0})
    assert data._consecutive_positive_years(net_income) == 1


def test_consecutive_positive_years_stops_at_nan() -> None:
    net_income = pd.Series({"2025": 100.0, "2024": 90.0, "2023": float("nan")})
    assert data._consecutive_positive_years(net_income) == 2


def test_consecutive_positive_years_none_input() -> None:
    assert data._consecutive_positive_years(None) is None


def test_consecutive_positive_years_sorts_regardless_of_input_order() -> None:
    # Deliberately out of chronological order -- must not trust caller's
    # ordering, since this counts consecutive years from most recent.
    net_income = pd.Series({"2023": 95.0, "2025": 100.0, "2024": -5.0})
    assert data._consecutive_positive_years(net_income) == 1


def test_fetch_raw_raises_when_symbol_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yf, "Ticker", lambda symbol: _FakeTicker(info={"trailingPegRatio": None}))
    with pytest.raises(ValueError):
        data._fetch_raw("BOGUS")


def test_fetch_raw_caches_the_raw_response_even_on_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The rejection case ("provider response didn't match") is exactly
    # the one an operator is most likely to want to inspect -- caching
    # only on success would leave no artifact behind for it.
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    monkeypatch.setattr(yf, "Ticker", lambda symbol: _FakeTicker(info={"trailingPegRatio": None}))

    with pytest.raises(ValueError):
        data._fetch_raw("BOGUS")

    cache_file = tmp_path / "BOGUS.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert payload["info"] == {"trailingPegRatio": None}


def test_fetch_raw_tolerates_income_stmt_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        yf,
        "Ticker",
        lambda symbol: _FakeTicker(
            info={"symbol": symbol, "marketCap": 1.0}, raise_on_income_stmt=True
        ),
    )
    info, net_income = data._fetch_raw("AAPL")
    assert info["symbol"] == "AAPL"
    assert net_income is None


def test_fetch_metrics_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    info = {
        "symbol": "AAPL",
        "marketCap": 4_800_000_000_000.0,
        "trailingPE": 39.7,
        "priceToBook": 45.1,
        "currentRatio": 1.07,
        "debtToEquity": 79.548,
        "returnOnEquity": 1.41,
        "grossMargins": 0.478,
        "operatingMargins": 0.322,
        "freeCashflow": 101_000_000_000.0,
        "dividendYield": 0.33,
    }
    net_income = pd.Series({"2025": 100.0, "2024": 90.0})
    monkeypatch.setattr(data, "_fetch_raw", lambda symbol: (info, net_income))

    result = data.fetch_metrics("AAPL")

    assert result is not None
    assert result.symbol == "AAPL"
    assert result.market_cap == 4_800_000_000_000.0
    assert result.debt_to_equity == pytest.approx(0.79548)
    assert result.dividend_yield == pytest.approx(0.0033)
    assert result.consecutive_positive_earnings_years == 2
    # Fields that are NOT in _PERCENT_SCALED_FIELDS must pass through
    # unchanged -- a regression that started dividing these by 100 too
    # would otherwise go uncaught (pm-reviewer finding: prior test only
    # asserted the fields that DO get converted).
    assert result.return_on_equity == 1.41
    assert result.gross_margin == 0.478
    assert result.operating_margin == 0.322


def test_fetch_metrics_missing_field_is_none_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    info = {"symbol": "THIN"}  # everything else missing
    monkeypatch.setattr(data, "_fetch_raw", lambda symbol: (info, None))

    result = data.fetch_metrics("THIN")

    assert result is not None
    assert result.market_cap is None
    assert result.consecutive_positive_earnings_years is None


def test_fetch_metrics_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    attempts = {"count": 0}

    def _flaky_fetch(symbol: str) -> tuple[dict[str, object], None]:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise ConnectionError("transient")
        return {"symbol": symbol}, None

    monkeypatch.setattr(data, "_fetch_raw", _flaky_fetch)

    result = data.fetch_metrics("AAPL")

    assert result is not None
    assert attempts["count"] == 2


def test_fetch_metrics_returns_none_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    attempts = {"count": 0}

    def _always_fails(symbol: str) -> tuple[dict[str, object], None]:
        attempts["count"] += 1
        raise ConnectionError("still down")

    monkeypatch.setattr(data, "_fetch_raw", _always_fails)

    result = data.fetch_metrics("AAPL")

    assert result is None
    assert attempts["count"] == config.DATA_FETCH_MAX_RETRIES


def test_fetch_metrics_backoff_grows_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression test for a real bug: backoff was originally a flat delay
    # despite being called "retry-with-backoff", which would retry all
    # DATA_FETCH_THREAD_POOL_WORKERS concurrent workers in near-lockstep
    # under real rate-limiting -- recreating the burst that caused it.
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: delays.append(seconds))

    def _always_fails(symbol: str) -> tuple[dict[str, object], None]:
        raise ConnectionError("still down")

    monkeypatch.setattr(data, "_fetch_raw", _always_fails)

    data.fetch_metrics("AAPL")

    assert len(delays) == config.DATA_FETCH_MAX_RETRIES - 1
    assert all(earlier < later for earlier, later in itertools.pairwise(delays))


def test_fetch_all_metrics_maps_each_symbol_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_fetch(symbol: str) -> data.Metrics | None:
        if symbol == "BAD":
            return None
        return data.Metrics(
            symbol=symbol,
            market_cap=1.0,
            trailing_pe=None,
            price_to_book=None,
            current_ratio=None,
            debt_to_equity=None,
            return_on_equity=None,
            gross_margin=None,
            operating_margin=None,
            free_cash_flow=None,
            dividend_yield=None,
            consecutive_positive_earnings_years=None,
        )

    monkeypatch.setattr(data, "fetch_metrics", _fake_fetch)

    results = data.fetch_all_metrics(["AAPL", "BAD", "MSFT"])

    assert results["BAD"] is None
    assert results["AAPL"] is not None
    assert results["MSFT"] is not None
    assert len(results) == 3


def test_fetch_all_metrics_tolerates_an_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raising_fetch(symbol: str) -> data.Metrics | None:
        if symbol == "EXPLODES":
            raise RuntimeError("should never happen, but must not take down the batch")
        return None

    monkeypatch.setattr(data, "fetch_metrics", _raising_fetch)

    results = data.fetch_all_metrics(["EXPLODES", "FINE"])

    assert results["EXPLODES"] is None
    assert results["FINE"] is None


def test_fetch_all_metrics_bounds_batch_wait_on_a_hung_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A worker that never returns (no exception, no timeout of its own --
    # yfinance exposes no per-call timeout hook) must not block the whole
    # batch forever; fetch_all_metrics bounds the wait and reports that
    # ticker as failed rather than hanging. The thread itself keeps
    # running in the background (a stdlib ThreadPoolExecutor limitation,
    # documented in the function's docstring) -- kept short (0.3s) so it
    # doesn't meaningfully slow down the suite.
    monkeypatch.setattr(config, "DATA_FETCH_BATCH_TIMEOUT_SECONDS", 0.05)

    def _fake_fetch(symbol: str) -> data.Metrics | None:
        if symbol == "STUCK":
            time.sleep(0.3)
            return None
        return data.Metrics(
            symbol=symbol,
            market_cap=1.0,
            trailing_pe=None,
            price_to_book=None,
            current_ratio=None,
            debt_to_equity=None,
            return_on_equity=None,
            gross_margin=None,
            operating_margin=None,
            free_cash_flow=None,
            dividend_yield=None,
            consecutive_positive_earnings_years=None,
        )

    monkeypatch.setattr(data, "fetch_metrics", _fake_fetch)

    results = data.fetch_all_metrics(["FAST", "STUCK"])

    assert results["FAST"] is not None
    assert results["STUCK"] is None


def test_cache_raw_response_writes_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    net_income = pd.Series({"2025": 100.0})

    data._cache_raw_response("AAPL", {"symbol": "AAPL", "marketCap": 1.0}, net_income)

    cache_file = tmp_path / "AAPL.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert payload["info"]["symbol"] == "AAPL"
    assert payload["net_income"]["2025"] == 100.0
