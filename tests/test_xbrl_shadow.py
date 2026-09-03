"""Unit tests for xbrl_shadow.py -- the full-universe shadow comparison run (M37 prereq).

Every external module call is mocked -- this tests xbrl_shadow.py's own
orchestration and report-writing, not xbrl.py's/data.py's own internal
correctness (already covered by their own test files). Follows the same
pattern as tests/test_evaluate.py/tests/test_daily_screen.py.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import pytest

import config
import data
import universe
import xbrl


def _metrics(**overrides: Any) -> data.Metrics:
    defaults: dict[str, Any] = {
        "symbol": "TEST",
        "market_cap": 5_000_000_000.0,
        "trailing_pe": 15.0,
        "price_to_book": 1.5,
        "current_ratio": 2.0,
        "debt_to_equity": 0.5,
        "return_on_equity": 0.20,
        "gross_margin": 0.40,
        "operating_margin": 0.20,
        "free_cash_flow": 500_000_000.0,
        "dividend_yield": 0.02,
        "consecutive_positive_earnings_years": 5,
    }
    defaults.update(overrides)
    return data.Metrics(**defaults)


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LOG_FILE_PATH", tmp_path / "munger.log")
    monkeypatch.setattr(config, "XBRL_SHADOW_REPORT_PATH", tmp_path / "xbrl_shadow_report.csv")


def test_xbrl_shadow_never_imports_execution() -> None:
    # Same architectural guarantee as daily_screen.py (M14): this is a
    # read-only diagnostic comparison run, never a trading path.
    sys.modules.pop("xbrl_shadow", None)
    sys.modules.pop("execution", None)

    import xbrl_shadow  # noqa: F401

    assert "execution" not in sys.modules


def test_run_writes_disagreements_worst_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["AAA", "BBB"]),
    )
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {
            "AAA": _metrics(symbol="AAA", gross_margin=0.40),
            "BBB": _metrics(symbol="BBB", gross_margin=0.40),
        },
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"AAA": "0000000001", "BBB": "0000000002"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts={"cik": cik}, not_found=False),
    )

    def _fake_gross_margin(facts: dict[str, object]) -> float | None:
        return {"0000000001": 0.55, "0000000002": 0.40}[str(facts["cik"])]

    monkeypatch.setattr(xbrl, "gross_margin_from_xbrl", _fake_gross_margin)

    def _fake_shadow_compare(
        ticker: str, facts: dict[str, object], yf_metrics: data.Metrics
    ) -> list[xbrl.FieldDisagreement]:
        xbrl_value = _fake_gross_margin(facts)
        yfinance_value = yf_metrics.gross_margin
        assert xbrl_value is not None
        assert yfinance_value is not None
        if abs(xbrl_value - yfinance_value) < 1e-9:
            return []
        return [
            xbrl.FieldDisagreement(
                ticker=ticker,
                field="gross_margin",
                xbrl_value=xbrl_value,
                yfinance_value=yfinance_value,
            )
        ]

    monkeypatch.setattr(xbrl, "shadow_compare", _fake_shadow_compare)

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.universe_size == 2
    assert summary.yfinance_fetched == 2
    assert summary.cik_matched == 2
    assert summary.xbrl_facts_fetched == 2
    assert summary.comparable == 2
    assert summary.disagreements == 1

    rows = list(csv.DictReader(config.XBRL_SHADOW_REPORT_PATH.open()))
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["field"] == "gross_margin"
    assert float(rows[0]["xbrl_value"]) == 0.55
    assert float(rows[0]["yfinance_value"]) == 0.40


def test_run_sorts_multiple_disagreements_worst_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["SMALL_DIFF", "BIG_DIFF"]),
    )
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {
            "SMALL_DIFF": _metrics(symbol="SMALL_DIFF", gross_margin=0.40),
            "BIG_DIFF": _metrics(symbol="BIG_DIFF", gross_margin=0.40),
        },
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"SMALL_DIFF": "1", "BIG_DIFF": "2"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts={"cik": cik}, not_found=False),
    )
    monkeypatch.setattr(
        xbrl, "gross_margin_from_xbrl", lambda facts: {"1": 0.45, "2": 0.80}[str(facts["cik"])]
    )

    def _fake_shadow_compare(
        ticker: str, facts: dict[str, object], yf_metrics: data.Metrics
    ) -> list[xbrl.FieldDisagreement]:
        xbrl_value = {"1": 0.45, "2": 0.80}[str(facts["cik"])]
        yfinance_value = yf_metrics.gross_margin
        assert yfinance_value is not None
        return [
            xbrl.FieldDisagreement(
                ticker=ticker,
                field="gross_margin",
                xbrl_value=xbrl_value,
                yfinance_value=yfinance_value,
            )
        ]

    monkeypatch.setattr(xbrl, "shadow_compare", _fake_shadow_compare)

    xbrl_shadow.run(run_date="2026-09-03")

    rows = list(csv.DictReader(config.XBRL_SHADOW_REPORT_PATH.open()))
    assert [r["ticker"] for r in rows] == ["BIG_DIFF", "SMALL_DIFF"]


def test_run_counts_a_ticker_with_no_cik_match_as_uncovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["ZZZZ"])
    )
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"ZZZZ": _metrics(symbol="ZZZZ")}
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {})  # no CIK for ZZZZ
    fetch_calls: list[str] = []

    def _record_and_fetch(cik: str) -> xbrl.CompanyFactsResult:
        fetch_calls.append(cik)
        return xbrl.CompanyFactsResult(facts={}, not_found=False)

    monkeypatch.setattr(xbrl, "fetch_company_facts_detailed", _record_and_fetch)

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.cik_matched == 0
    assert summary.xbrl_facts_fetched == 0
    assert summary.xbrl_not_found == 0
    assert summary.xbrl_fetch_failed == 0
    assert summary.comparable == 0
    assert summary.disagreements == 0
    assert fetch_calls == []  # never even attempted a companyfacts fetch without a CIK


def test_run_counts_a_confirmed_404_as_not_found_not_fetch_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["ZZZZ"])
    )
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"ZZZZ": _metrics(symbol="ZZZZ")}
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"ZZZZ": "9999999999"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts=None, not_found=True),
    )

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.cik_matched == 1
    assert summary.xbrl_facts_fetched == 0
    assert summary.xbrl_not_found == 1
    assert summary.xbrl_fetch_failed == 0
    assert summary.comparable == 0
    assert summary.disagreements == 0


def test_run_counts_a_network_failure_as_fetch_failed_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["ZZZZ"])
    )
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"ZZZZ": _metrics(symbol="ZZZZ")}
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"ZZZZ": "9999999999"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts=None, not_found=False),
    )

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.cik_matched == 1
    assert summary.xbrl_facts_fetched == 0
    assert summary.xbrl_not_found == 0
    assert summary.xbrl_fetch_failed == 1
    assert summary.comparable == 0
    assert summary.disagreements == 0


def test_run_does_not_crash_when_yfinance_metrics_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ticker yfinance failed to fetch this run still gets an XBRL
    # lookup attempted -- coverage counts are about EDGAR's own coverage,
    # independent of yfinance's own success this run.
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["ZZZZ"])
    )
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"ZZZZ": None})
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"ZZZZ": "9999999999"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts={"cik": cik}, not_found=False),
    )
    monkeypatch.setattr(xbrl, "gross_margin_from_xbrl", lambda facts: 0.50)

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.yfinance_fetched == 0
    assert summary.cik_matched == 1
    assert summary.xbrl_facts_fetched == 1
    assert summary.comparable == 0  # no yfinance value to compare against
    assert summary.degraded is True  # 0/1 yfinance-fetched is well under the coverage floor
    assert summary.disagreements == 0


def test_run_handles_an_empty_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=[])
    )
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {})

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.universe_size == 0
    assert summary.disagreements == 0
    # A zero-length universe (every S&P index scrape and the static
    # fallback failing at once) is the one scenario this coverage gate
    # exists to catch -- degraded, not trivially "healthy."
    assert summary.degraded is True
    rows = list(csv.DictReader(config.XBRL_SHADOW_REPORT_PATH.open()))
    assert rows == []


def test_run_not_degraded_when_coverage_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=["AAA"])
    )
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"AAA": _metrics(symbol="AAA")}
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"AAA": "1"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts=None, not_found=True),  # no GrossProfit tagged
    )

    summary = xbrl_shadow.run(run_date="2026-09-03")

    # Full yfinance + CIK coverage, but zero XBRL-comparable data -- this
    # is expected structural non-coverage, not a degraded run (see
    # ShadowRunSummary's own docstring on why comparable/not_found don't
    # feed the degraded check).
    assert summary.degraded is False


def test_run_degraded_when_cik_matching_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    import xbrl_shadow

    tickers = [f"T{i}" for i in range(10)]
    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=tickers)
    )
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {t: _metrics(symbol=t) for t in tickers},
    )
    # Only 1 of 10 tickers has a matched CIK -- well under the 90% floor,
    # even though yfinance's own fetch was fully healthy.
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"T0": "1"})
    monkeypatch.setattr(
        xbrl,
        "fetch_company_facts_detailed",
        lambda cik: xbrl.CompanyFactsResult(facts=None, not_found=True),
    )

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.yfinance_fetched == 10
    assert summary.cik_matched == 1
    assert summary.degraded is True


def test_run_degraded_when_edgar_fetches_mostly_fail_not_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # yfinance and CIK matching are both fully healthy, but EDGAR itself
    # is having a bad day on the companyfacts endpoint (an outage,
    # sustained throttling) -- comparable/xbrl_facts_fetched alone would
    # miss this (they're expected to be low on a healthy run too), so
    # this must be caught via the EDGAR-resolved fraction specifically,
    # not lumped in with genuine not-found non-coverage.
    import xbrl_shadow

    tickers = [f"T{i}" for i in range(10)]
    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=tickers)
    )
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {t: _metrics(symbol=t) for t in tickers},
    )
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {t: str(i) for i, t in enumerate(tickers)})

    def _mostly_fetch_failures(cik: str) -> xbrl.CompanyFactsResult:
        # Only CIK "0" resolves (a confirmed 404); the other 9 fail to
        # fetch at all -- 1/10 resolved is well under the 90% floor.
        if cik == "0":
            return xbrl.CompanyFactsResult(facts=None, not_found=True)
        return xbrl.CompanyFactsResult(facts=None, not_found=False)

    monkeypatch.setattr(xbrl, "fetch_company_facts_detailed", _mostly_fetch_failures)

    summary = xbrl_shadow.run(run_date="2026-09-03")

    assert summary.yfinance_fetched == 10
    assert summary.cik_matched == 10
    assert summary.xbrl_not_found == 1
    assert summary.xbrl_fetch_failed == 9
    assert summary.degraded is True


def test_run_writes_report_atomically_no_tmp_file_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xbrl_shadow

    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=[])
    )
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {})

    xbrl_shadow.run(run_date="2026-09-03")

    assert config.XBRL_SHADOW_REPORT_PATH.exists()
    tmp_path = config.XBRL_SHADOW_REPORT_PATH.with_suffix(
        config.XBRL_SHADOW_REPORT_PATH.suffix + ".tmp"
    )
    assert not tmp_path.exists()
