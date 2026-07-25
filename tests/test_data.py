"""Unit tests for data.py's pure functions and fetch orchestration
(DESIGN.md section 6, layer 1)."""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yfinance as yf
import yfinance.exceptions

import config
import data


@pytest.fixture(autouse=True)
def _reset_shared_rate_limit_state() -> Generator[None, None, None]:
    # _rate_limited_until is module-level shared state (deliberately, so
    # every worker thread sees the same cooldown) -- reset it around each
    # test so tests can't leak a cooldown into each other.
    data._rate_limited_until = 0.0
    yield
    data._rate_limited_until = 0.0


@pytest.fixture(autouse=True)
def _isolate_progress_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # fetch_all_metrics/fetch_metrics now write live progress (M13) to
    # config.PROGRESS_FILE_PATH on every call -- without this, tests would
    # write into this repo's real report/ directory instead of an
    # isolated tmp_path.
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "report")
    monkeypatch.setattr(config, "PROGRESS_FILE_PATH", tmp_path / "report" / "progress.json")
    # The fetch path also writes a raw-response cache to DATA_RAW_CACHE_DIR.
    # Isolate it here so every test in this module is hermetic -- otherwise
    # tests that don't redirect it individually write into the real cache
    # dir, which fails outright when it isn't writable (e.g. the non-root
    # CI container's read-only /app). Individual per-test overrides below
    # still win where a test needs to inspect the cache.
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path / "data_cache")


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


def test_coerce_float_returns_numeric_value() -> None:
    assert data._coerce_float({"trailingPE": 15.5}, "trailingPE") == 15.5


def test_coerce_float_handles_missing_key() -> None:
    assert data._coerce_float({}, "trailingPE") is None


def test_coerce_float_handles_non_numeric_value_without_raising() -> None:
    # Regression test for a real bug: a full-universe live run crashed
    # entirely (not just one ticker) on an uncaught TypeError when
    # yfinance returned a non-numeric value for trailingPE -- only the
    # two percent-scaled fields had this guard before, every other
    # numeric field was trusted raw. Every Metrics field goes through
    # this now.
    assert data._coerce_float({"trailingPE": "not a number"}, "trailingPE") is None


def test_coerce_float_accepts_numeric_string() -> None:
    # yfinance sometimes returns numeric values as strings -- these are
    # legitimately convertible, not malformed.
    assert data._coerce_float({"trailingPE": "15.5"}, "trailingPE") == 15.5


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


def test_fetch_metrics_malformed_field_is_none_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Regression test for the real full-universe crash: a non-numeric
    # value in a field that isn't one of the two percent-scaled fields
    # (trailingPE here) must not propagate an exception out of
    # fetch_metrics -- it should degrade to a missing field like any
    # other unusable value.
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    info = {"symbol": "WEIRD", "trailingPE": "N/A", "marketCap": 1_000_000_000.0}
    monkeypatch.setattr(data, "_fetch_raw", lambda symbol: (info, None))

    result = data.fetch_metrics("WEIRD")

    assert result is not None
    assert result.trailing_pe is None
    assert result.market_cap == 1_000_000_000.0


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


def test_register_rate_limit_sets_a_future_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_RATE_LIMIT_COOLDOWN_SECONDS", 10.0)
    before = time.time()
    data._register_rate_limit()
    assert data._rate_limited_until >= before + 10.0


def test_wait_out_shared_rate_limit_sleeps_for_remaining_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data._rate_limited_until = time.time() + 5.0
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: delays.append(seconds))

    data._wait_out_shared_rate_limit()

    assert len(delays) == 1
    assert 0 < delays[0] <= 5.0


def test_wait_out_shared_rate_limit_noop_when_no_cooldown_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: delays.append(seconds))

    data._wait_out_shared_rate_limit()

    assert delays == []


def test_fetch_metrics_registers_shared_cooldown_on_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    attempts = {"count": 0}

    def _rate_limited_then_succeeds(symbol: str) -> tuple[dict[str, object], None]:
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise yfinance.exceptions.YFRateLimitError()
        return {"symbol": symbol}, None

    monkeypatch.setattr(data, "_fetch_raw", _rate_limited_then_succeeds)

    result = data.fetch_metrics("AAPL")

    assert result is not None
    assert attempts["count"] == 2
    assert data._rate_limited_until > 0


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


def test_progress_tracking_reflects_in_flight_and_completed_tickers() -> None:
    data._start_progress("screening", 3)
    data._mark_ticker_started("AAPL")
    data._mark_ticker_started("MSFT")

    mid_payload = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert mid_payload["total"] == 3
    assert mid_payload["completed"] == 0
    assert set(mid_payload["in_flight"]) == {"AAPL", "MSFT"}

    data._mark_ticker_done("AAPL")

    after_one_done = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert after_one_done["completed"] == 1
    assert after_one_done["in_flight"] == ["MSFT"]

    data._finish_progress()

    final_payload = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert final_payload["completed"] == final_payload["total"] == 3


def test_progress_file_reports_an_active_rate_limit_cooldown() -> None:
    # User request (2026-07-23): the report's live view needs to tell
    # "waiting out a shared rate limit" apart from "genuinely missing."
    data._register_rate_limit()
    data._write_progress_file()

    payload = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert payload["rate_limited_until"] > payload["now"]


def test_fetch_all_metrics_writes_live_progress_for_the_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M13: report.py's progress bar polls this file while a batch is in
    # flight. Patches _fetch_metrics_inner (not fetch_metrics itself) so
    # the real fetch_metrics wrapper's start/done tracking actually runs.
    def _fake_inner(symbol: str) -> data.Metrics | None:
        return None

    monkeypatch.setattr(data, "_fetch_metrics_inner", _fake_inner)

    data.fetch_all_metrics(["AAPL", "MSFT"], phase="screening")

    payload = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert payload["phase"] == "screening"
    assert payload["total"] == 2
    assert payload["completed"] == 2  # _finish_progress marks the batch done
    assert payload["in_flight"] == []


def test_fetch_all_metrics_survives_sustained_thread_contention_on_progress_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: the earlier test only submits 2
    # symbols against a 12-worker pool, with no forced synchronization --
    # whether two threads actually collide on _write_progress_file's
    # shared temp filename is left to OS/GIL scheduling, so it could pass
    # even if the lock scope regressed. Enough symbols to keep every
    # worker thread hammering _write_progress_file concurrently for the
    # whole batch gives the race a real chance to reproduce if reintroduced.
    def _fake_inner(symbol: str) -> data.Metrics | None:
        return None

    monkeypatch.setattr(data, "_fetch_metrics_inner", _fake_inner)
    symbols = [f"SYM{i}" for i in range(200)]

    results = data.fetch_all_metrics(symbols, phase="screening")

    assert len(results) == 200
    payload = json.loads(config.PROGRESS_FILE_PATH.read_text())
    assert payload["completed"] == payload["total"] == 200
    assert payload["in_flight"] == []


def test_progress_write_failure_never_discards_a_successful_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: _mark_ticker_done runs in
    # fetch_metrics's `finally` block, AFTER a real fetch has already
    # succeeded. If _write_progress_file raised there, Python's
    # finally-supersedes-return semantics would discard the already-
    # fetched Metrics record -- a cosmetic display feature silently
    # corrupting real trading data. Forces _write_progress_file's guarded
    # body to raise (simulating a disk-full/permissions fault) and
    # confirms fetch_metrics still returns the real result.
    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("disk full (simulated)")

    # Path is effectively immutable/slotted -- patch write_text at the
    # class level (auto-reverted by monkeypatch after the test) rather
    # than trying to override an instance attribute.
    monkeypatch.setattr(Path, "write_text", _raise)

    expected = data.Metrics(
        symbol="AAPL",
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
    monkeypatch.setattr(data, "_fetch_metrics_inner", lambda symbol: expected)

    result = data.fetch_metrics("AAPL")

    assert result is expected  # not None -- the real fetch must survive


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


def _clean_metrics(**overrides: Any) -> data.Metrics:
    """A fully-populated, plausible Metrics record; override individual
    fields per test to exercise a specific missing/outlier case."""
    defaults: dict[str, Any] = {
        "symbol": "AAPL",
        "market_cap": 4_800_000_000_000.0,
        "trailing_pe": 39.7,
        "price_to_book": 45.1,
        "current_ratio": 1.07,
        "debt_to_equity": 0.795,
        "return_on_equity": 1.41,
        "gross_margin": 0.478,
        "operating_margin": 0.322,
        "free_cash_flow": 101_000_000_000.0,
        "dividend_yield": 0.0033,
        "consecutive_positive_earnings_years": 4,
    }
    defaults.update(overrides)
    return data.Metrics(**defaults)


def test_validate_metrics_clean_record_has_no_fail_reasons() -> None:
    assert data.validate_metrics(_clean_metrics()) == []


def test_validate_metrics_flags_each_missing_field_distinctly() -> None:
    metrics = _clean_metrics(market_cap=None, dividend_yield=None)
    reasons = data.validate_metrics(metrics)
    assert "data_missing:market_cap" in reasons
    assert "data_missing:dividend_yield" in reasons
    assert len(reasons) == 2


def test_validate_metrics_never_flags_symbol_as_missing() -> None:
    # symbol is always present (it's the fetch key, not fetched data) --
    # regression guard against _REQUIRED_METRICS_FIELDS accidentally
    # including it.
    assert "data_missing:symbol" not in data.validate_metrics(_clean_metrics())


def test_validate_metrics_flags_implausible_pe_as_outlier_not_missing() -> None:
    metrics = _clean_metrics(trailing_pe=50_000.0)
    assert data.validate_metrics(metrics) == ["data_invalid_outlier:trailing_pe"]


def test_validate_metrics_flags_implausible_negative_pe() -> None:
    # A negative P/E is a legitimate, common signal (a loss-making year) --
    # only an extreme magnitude should read as corrupted data, not the sign.
    metrics = _clean_metrics(trailing_pe=-50_000.0)
    assert data.validate_metrics(metrics) == ["data_invalid_outlier:trailing_pe"]


def test_validate_metrics_accepts_legitimate_negative_pe() -> None:
    metrics = _clean_metrics(trailing_pe=-12.5)
    assert data.validate_metrics(metrics) == []


def test_validate_metrics_flags_implausible_debt_to_equity() -> None:
    metrics = _clean_metrics(debt_to_equity=500.0)
    assert data.validate_metrics(metrics) == ["data_invalid_outlier:debt_to_equity"]


def test_validate_metrics_combines_missing_and_outlier_reasons() -> None:
    metrics = _clean_metrics(market_cap=None, trailing_pe=50_000.0)
    reasons = data.validate_metrics(metrics)
    assert set(reasons) == {"data_missing:market_cap", "data_invalid_outlier:trailing_pe"}
