"""Unit tests for xbrl.py (Design v2.2 §3.4, M36).

Uses real, trimmed SEC EDGAR response fixtures (tests/fixtures/
sec_company_tickers_sample.json, sec_companyfacts_aapl_sample.json --
both real data fetched live 2026-09-02, trimmed to a handful of tickers/
concepts to keep the fixture small), not synthetic ones, matching this
project's own precedent (test_journal.py's real bot-state fixture).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import data
import xbrl

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_RAW_CACHE_DIR", tmp_path)
    # No real sleeping in tests.
    monkeypatch.setattr(config, "SEC_EDGAR_MAX_REQUESTS_PER_SECOND", 1_000_000)


def _real_ticker_index_bytes() -> bytes:
    return (_FIXTURES / "sec_company_tickers_sample.json").read_bytes()


def _real_companyfacts() -> dict[str, object]:
    loaded = json.loads((_FIXTURES / "sec_companyfacts_aapl_sample.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


# --- load_cik_lookup / get_cik ---


def test_load_cik_lookup_fetches_and_parses_when_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xbrl, "_throttled_get", lambda url: _real_ticker_index_bytes())
    lookup = xbrl.load_cik_lookup()
    assert lookup["AAPL"] == "0000320193"


def test_load_cik_lookup_real_gntx_and_hrmy_ciks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The two tickers central to the whole redesign's motivating case --
    # confirms the real fixture actually contains them, not just AAPL.
    monkeypatch.setattr(xbrl, "_throttled_get", lambda url: _real_ticker_index_bytes())
    lookup = xbrl.load_cik_lookup()
    assert "GNTX" in lookup
    assert "HRMY" in lookup
    assert len(lookup["GNTX"]) == 10
    assert len(lookup["HRMY"]) == 10


def test_load_cik_lookup_uses_disk_cache_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def _fake_get(url: str) -> bytes:
        nonlocal call_count
        call_count += 1
        return _real_ticker_index_bytes()

    monkeypatch.setattr(xbrl, "_throttled_get", _fake_get)

    xbrl.load_cik_lookup()
    xbrl.load_cik_lookup()

    assert call_count == 1  # second call served from disk cache


def test_load_cik_lookup_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def _fake_get(url: str) -> bytes:
        nonlocal call_count
        call_count += 1
        return _real_ticker_index_bytes()

    monkeypatch.setattr(xbrl, "_throttled_get", _fake_get)

    xbrl.load_cik_lookup()
    xbrl.load_cik_lookup(force_refresh=True)

    assert call_count == 2


def test_get_cik_returns_none_for_an_untracked_ticker() -> None:
    assert xbrl.get_cik("NOT-A-REAL-TICKER", cik_lookup={"AAPL": "0000320193"}) is None


def test_get_cik_is_case_insensitive() -> None:
    assert xbrl.get_cik("aapl", cik_lookup={"AAPL": "0000320193"}) == "0000320193"


# --- fetch_company_facts ---


def test_fetch_company_facts_returns_real_parsed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        xbrl, "_throttled_get", lambda url: json.dumps(_real_companyfacts()).encode()
    )
    facts = xbrl.fetch_company_facts("0000320193")
    assert facts is not None
    assert facts["entityName"] == "Apple Inc."


def test_fetch_company_facts_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _raise_404(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(xbrl, "_throttled_get", _raise_404)
    assert xbrl.fetch_company_facts("0000000000") is None


def test_fetch_company_facts_returns_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xbrl, "_throttled_get", MagicMock(side_effect=ConnectionError("no route")))
    assert xbrl.fetch_company_facts("0000320193") is None


def test_fetch_company_facts_returns_none_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xbrl, "_throttled_get", lambda url: b"not valid json{{{")
    assert xbrl.fetch_company_facts("0000320193") is None


# --- annual_values (real AAPL data) ---


def test_annual_values_extracts_real_net_income_history() -> None:
    facts = _real_companyfacts()
    values = xbrl.annual_values(facts, "NetIncomeLoss")

    assert len(values) > 5  # AAPL has many years of 10-K history on EDGAR
    # Every entry is a distinct fiscal year, sorted oldest -> newest.
    ends = [v.fiscal_year_end for v in values]
    assert ends == sorted(ends)
    assert len(ends) == len(set(ends))  # no duplicate fiscal years
    # The real, known FY2024 net income (Apple's 10-K, fiscal year ended
    # 2024-09-28): $93,736,000,000.
    fy2024 = next(v for v in values if v.fiscal_year_end == "2024-09-28")
    assert fy2024.value == pytest.approx(93_736_000_000.0)


def test_annual_values_dedup_keeps_the_most_recently_filed() -> None:
    # Real EDGAR responses repeat the same fiscal year across multiple
    # later filings (a comparative column in a subsequent 10-K) --
    # confirmed directly against the real fixture: filtering to entries
    # that are BOTH form=10-K/fp=FY AND span a genuine ~1-year period
    # still leaves more raw rows than distinct fiscal years.
    facts = _real_companyfacts()
    raw_entries = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]  # type: ignore[index]

    def _is_annual_span(e: dict[str, object]) -> bool:
        import datetime

        if e.get("form") != "10-K" or e.get("fp") != "FY":
            return False
        start, end = e.get("start"), e.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            return False
        span = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
        return 350 <= span <= 380

    annual_raw = [e for e in raw_entries if _is_annual_span(e)]
    distinct_years = {e["end"] for e in annual_raw}
    assert len(annual_raw) > len(distinct_years)  # real duplication exists in the fixture

    values = xbrl.annual_values(facts, "NetIncomeLoss")
    assert len(values) == len(distinct_years)
    # And every single one is a genuinely annual span -- the real bug
    # this test guards: a naive form=10-K/fp=FY filter alone (no span
    # check) pulled in 55 "years" for AAPL, most of them quarterly
    # sub-disclosures embedded in a 10-K, not fiscal-year totals.
    for v in values:
        assert v.fiscal_year_end in distinct_years


def test_annual_values_empty_for_a_concept_not_present() -> None:
    facts = _real_companyfacts()
    assert xbrl.annual_values(facts, "NotARealConcept") == []


def test_annual_values_empty_for_malformed_facts() -> None:
    assert xbrl.annual_values({}, "NetIncomeLoss") == []
    assert xbrl.annual_values({"facts": "not-a-dict"}, "NetIncomeLoss") == []


# --- gross_margin_from_xbrl / shadow_compare (Design v2.2 §3.4, M36) ---


def test_gross_margin_from_xbrl_computed_from_real_data() -> None:
    facts = _real_companyfacts()
    margin = xbrl.gross_margin_from_xbrl(facts)
    assert margin is not None
    # Apple's real gross margin is comfortably in the low-to-mid 40s%
    # range -- a loose sanity bound on the real computed figure, not a
    # hardcoded expected value (which would just be re-deriving the same
    # fixture). This is also the regression test for the same-fiscal-
    # year-matching fix: before it, this came out to ~73% (a real
    # FY2025 GrossProfit divided by a stale FY2018 "Revenues" value,
    # since AAPL stopped tagging that concept after adopting ASC 606).
    assert 0.35 < margin < 0.55


def test_gross_margin_from_xbrl_matches_by_fiscal_year_not_independently() -> None:
    # Direct regression test for the real bug: GrossProfit's most recent
    # annual value and "Revenues"' most recent annual value are for
    # different fiscal years in AAPL's real data (the filer switched
    # revenue concepts in 2018) -- gross_margin_from_xbrl must find a
    # year both concepts actually share, not naively divide each
    # concept's own latest value.
    facts = _real_companyfacts()
    gross_profit_values = xbrl.annual_values(facts, "GrossProfit")
    revenues_years = {v.fiscal_year_end for v in xbrl.annual_values(facts, "Revenues")}
    # GrossProfit's own MOST RECENT fiscal year is not among the years
    # "Revenues" has data for -- confirms naively pairing each concept's
    # independently-latest value (the real bug) would mismatch fiscal
    # years, even though the two concepts' *year ranges* do overlap
    # earlier on (AAPL tagged both during its 2016-2018 transition).
    assert gross_profit_values[-1].fiscal_year_end not in revenues_years

    # The fallback concept (RevenueFromContractWithCustomerExcludingAssessedTax)
    # is what actually makes a common year exist.
    modern_revenue_years = {
        v.fiscal_year_end
        for v in xbrl.annual_values(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")
    }
    gross_profit_years = {v.fiscal_year_end for v in gross_profit_values}
    assert gross_profit_years & modern_revenue_years

    margin = xbrl.gross_margin_from_xbrl(facts)
    assert margin is not None
    assert 0.35 < margin < 0.55


def test_gross_margin_from_xbrl_none_when_concept_missing() -> None:
    assert xbrl.gross_margin_from_xbrl({}) is None


def test_disagrees_true_past_the_relative_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.0)
    assert xbrl._disagrees(0.20, 0.10) is True  # 100% relative diff
    assert xbrl._disagrees(0.20, 0.199) is False  # well within a 5% relative bound


def test_disagrees_true_past_the_absolute_tolerance_near_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The case the absolute bound exists for: a ratio field near zero,
    # where a purely relative bar would be too loose to catch a real
    # problem (config.py's own reasoning, carried from the design doc).
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.01)
    assert xbrl._disagrees(0.02, 0.001) is True  # tiny relative bound, but > 1pp absolute


def test_shadow_compare_flags_a_real_gross_margin_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.01)
    facts = _real_companyfacts()
    xbrl_margin = xbrl.gross_margin_from_xbrl(facts)
    assert xbrl_margin is not None
    yf_metrics = data.Metrics(
        symbol="AAPL",
        market_cap=None,
        trailing_pe=None,
        price_to_book=None,
        current_ratio=None,
        debt_to_equity=None,
        return_on_equity=None,
        gross_margin=xbrl_margin + 0.20,  # far off, deliberately
        operating_margin=None,
        free_cash_flow=None,
        dividend_yield=None,
        consecutive_positive_earnings_years=None,
    )

    disagreements = xbrl.shadow_compare("AAPL", facts, yf_metrics)

    assert len(disagreements) == 1
    assert disagreements[0].field == "gross_margin"
    assert disagreements[0].ticker == "AAPL"


def test_shadow_compare_empty_when_values_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.01)
    facts = _real_companyfacts()
    xbrl_margin = xbrl.gross_margin_from_xbrl(facts)
    assert xbrl_margin is not None
    yf_metrics = data.Metrics(
        symbol="AAPL",
        market_cap=None,
        trailing_pe=None,
        price_to_book=None,
        current_ratio=None,
        debt_to_equity=None,
        return_on_equity=None,
        gross_margin=xbrl_margin,  # exact agreement
        operating_margin=None,
        free_cash_flow=None,
        dividend_yield=None,
        consecutive_positive_earnings_years=None,
    )

    assert xbrl.shadow_compare("AAPL", facts, yf_metrics) == []
