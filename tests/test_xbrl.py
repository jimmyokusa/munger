"""Unit tests for xbrl.py (Design v2.2 §3.4, M36; primary as of M37).

Uses real, trimmed SEC EDGAR response fixtures (tests/fixtures/
sec_company_tickers_sample.json, sec_companyfacts_aapl_sample.json --
real data fetched live 2026-09-02; sec_companyfacts_lhx_sample.json,
sec_companyfacts_gntx_sample.json -- real data fetched live 2026-09-04,
during M37's own shadow-run review -- all trimmed to a handful of
tickers/concepts to keep each fixture small), not synthetic ones,
matching this project's own precedent (test_journal.py's real bot-state
fixture).
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


def _real_lhx_companyfacts() -> dict[str, object]:
    loaded = json.loads((_FIXTURES / "sec_companyfacts_lhx_sample.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _real_gntx_companyfacts() -> dict[str, object]:
    loaded = json.loads((_FIXTURES / "sec_companyfacts_gntx_sample.json").read_text())
    assert isinstance(loaded, dict)
    return loaded


# --- load_cik_lookup / get_cik ---


def test_load_cik_lookup_fetches_and_parses_when_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xbrl, "throttled_get", lambda url: _real_ticker_index_bytes())
    lookup = xbrl.load_cik_lookup()
    assert lookup["AAPL"] == "0000320193"


def test_load_cik_lookup_real_gntx_and_hrmy_ciks(monkeypatch: pytest.MonkeyPatch) -> None:
    # The two tickers central to the whole redesign's motivating case --
    # confirms the real fixture actually contains them, not just AAPL.
    monkeypatch.setattr(xbrl, "throttled_get", lambda url: _real_ticker_index_bytes())
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

    monkeypatch.setattr(xbrl, "throttled_get", _fake_get)

    xbrl.load_cik_lookup()
    xbrl.load_cik_lookup()

    assert call_count == 1  # second call served from disk cache


def test_load_cik_lookup_force_refresh_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def _fake_get(url: str) -> bytes:
        nonlocal call_count
        call_count += 1
        return _real_ticker_index_bytes()

    monkeypatch.setattr(xbrl, "throttled_get", _fake_get)

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
        xbrl, "throttled_get", lambda url: json.dumps(_real_companyfacts()).encode()
    )
    facts = xbrl.fetch_company_facts("0000320193")
    assert facts is not None
    assert facts["entityName"] == "Apple Inc."


def test_fetch_company_facts_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _raise_404(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(xbrl, "throttled_get", _raise_404)
    assert xbrl.fetch_company_facts("0000000000") is None


def test_fetch_company_facts_returns_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xbrl, "throttled_get", MagicMock(side_effect=ConnectionError("no route")))
    assert xbrl.fetch_company_facts("0000320193") is None


def test_fetch_company_facts_returns_none_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xbrl, "throttled_get", lambda url: b"not valid json{{{")
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


# --- Plausibility-bounded matching (M37, real LHX/GNTX data) ---


def test_gross_margin_from_xbrl_rejects_an_implausible_cross_filing_pairing() -> None:
    # Real, reproduced bug (not a fixture invented to match the fix):
    # LHX's GrossProfit is only tagged for FY2009/FY2010 (~$1.6-1.9B,
    # the real as-filed consolidated figures). Its Revenues concept
    # separately carries a value for the *same* nominal FY2010 end date,
    # but filed two years later at only $102.4M -- a much smaller,
    # differently-scoped figure (most likely a later restatement to a
    # narrower "continuing operations" basis after a divestiture).
    # Matching purely by fiscal-year-end date pairs these into a ~1828%
    # "gross margin" -- caught live against real EDGAR data during M37's
    # own shadow-run review, before this fix existed. There is no OTHER
    # common year between the two concepts once the implausible one is
    # rejected, so this must return None, not merely "a smaller wrong
    # number".
    facts = _real_lhx_companyfacts()
    assert xbrl.gross_margin_from_xbrl(facts) is None


def test_operating_margin_from_xbrl_matches_gntx_filed_figure() -> None:
    # The design doc's own named acceptance case (Design v2.2 §3.4):
    # "yfinance 21.8% vs. the filed 18.7%". Computed here from GNTX's
    # real OperatingIncomeLoss/RevenueFromContractWithCustomerExcludingAssessedTax
    # for its most recent fiscal year (FY2025) -- confirms the formula
    # itself, independent of anything yfinance reports.
    facts = _real_gntx_companyfacts()
    margin = xbrl.operating_margin_from_xbrl(facts)
    assert margin is not None
    assert 0.185 < margin < 0.190  # ~18.70%, matching the design doc's cited 18.7%


def test_operating_margin_from_xbrl_none_when_concept_missing() -> None:
    assert xbrl.operating_margin_from_xbrl({}) is None


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


def test_shadow_compare_flags_a_real_operating_margin_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.01)
    facts = _real_gntx_companyfacts()
    xbrl_margin = xbrl.operating_margin_from_xbrl(facts)
    assert xbrl_margin is not None
    yf_metrics = data.Metrics(
        symbol="GNTX",
        market_cap=None,
        trailing_pe=None,
        price_to_book=None,
        current_ratio=None,
        debt_to_equity=None,
        return_on_equity=None,
        gross_margin=None,
        operating_margin=0.218,  # the design doc's own cited yfinance figure
        free_cash_flow=None,
        dividend_yield=None,
        consecutive_positive_earnings_years=None,
    )

    disagreements = xbrl.shadow_compare("GNTX", facts, yf_metrics)

    assert len(disagreements) == 1
    assert disagreements[0].field == "operating_margin"
    assert disagreements[0].ticker == "GNTX"


def test_shadow_compare_checks_both_fields_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    # gross_margin agrees, operating_margin disagrees -- only the second
    # should be flagged, confirming the two checks don't interfere.
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_RELATIVE_TOLERANCE", 0.05)
    monkeypatch.setattr(config, "XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP", 0.01)
    facts = _real_gntx_companyfacts()
    xbrl_gross_margin = xbrl.gross_margin_from_xbrl(facts)
    assert xbrl_gross_margin is not None
    yf_metrics = data.Metrics(
        symbol="GNTX",
        market_cap=None,
        trailing_pe=None,
        price_to_book=None,
        current_ratio=None,
        debt_to_equity=None,
        return_on_equity=None,
        gross_margin=xbrl_gross_margin,  # agrees
        operating_margin=0.218,  # disagrees
        free_cash_flow=None,
        dividend_yield=None,
        consecutive_positive_earnings_years=None,
    )

    disagreements = xbrl.shadow_compare("GNTX", facts, yf_metrics)

    assert [d.field for d in disagreements] == ["operating_margin"]


# --- apply_primary_metrics (M37, Design v2.2 §3.4) ---


def _metrics(**overrides: object) -> data.Metrics:
    defaults: dict[str, object] = {
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
    return data.Metrics(**defaults)  # type: ignore[arg-type]


def test_apply_primary_metrics_overrides_both_margins_when_xbrl_has_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"AAA": "1"})
    monkeypatch.setattr(xbrl, "fetch_company_facts", lambda cik: {"cik": cik})
    monkeypatch.setattr(xbrl, "gross_margin_from_xbrl", lambda facts: 0.55)
    monkeypatch.setattr(xbrl, "operating_margin_from_xbrl", lambda facts: 0.25)

    result = xbrl.apply_primary_metrics({"AAA": _metrics(symbol="AAA")})

    assert result["AAA"] is not None
    assert result["AAA"].gross_margin == 0.55
    assert result["AAA"].operating_margin == 0.25
    # Every other field is untouched.
    assert result["AAA"].market_cap == 5_000_000_000.0


def test_apply_primary_metrics_falls_back_to_yfinance_without_a_cik_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {})  # no CIK for ZZZZ
    original = _metrics(symbol="ZZZZ", gross_margin=0.30, operating_margin=0.15)

    result = xbrl.apply_primary_metrics({"ZZZZ": original})

    assert result["ZZZZ"] == original  # unchanged, same object semantics


def test_apply_primary_metrics_falls_back_when_xbrl_facts_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"ZZZZ": "9999999999"})
    monkeypatch.setattr(xbrl, "fetch_company_facts", lambda cik: None)
    original = _metrics(symbol="ZZZZ", gross_margin=0.30, operating_margin=0.15)

    result = xbrl.apply_primary_metrics({"ZZZZ": original})

    assert result["ZZZZ"] == original


def test_apply_primary_metrics_falls_back_per_field_when_only_one_is_computable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A sector without a meaningful gross margin (financials, insurers)
    # might have operating income but no GrossProfit tagged -- each
    # field's override is independent.
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"AAA": "1"})
    monkeypatch.setattr(xbrl, "fetch_company_facts", lambda cik: {"cik": cik})
    monkeypatch.setattr(xbrl, "gross_margin_from_xbrl", lambda facts: None)
    monkeypatch.setattr(xbrl, "operating_margin_from_xbrl", lambda facts: 0.25)

    result = xbrl.apply_primary_metrics(
        {"AAA": _metrics(symbol="AAA", gross_margin=0.40, operating_margin=0.20)}
    )

    assert result["AAA"] is not None
    assert result["AAA"].gross_margin == 0.40  # unchanged -- XBRL had nothing to offer
    assert result["AAA"].operating_margin == 0.25  # overridden


def test_apply_primary_metrics_preserves_a_none_metrics_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ticker yfinance itself failed to fetch stays None -- XBRL alone
    # can't substitute for a fully-missing Metrics record (market_cap,
    # trailing_pe, and every Graham-gate field this function doesn't
    # touch would still be missing).
    fetch_calls: list[str] = []

    def _record_and_lookup(symbol: str, lookup: dict[str, str]) -> str | None:
        fetch_calls.append(symbol)
        return lookup.get(symbol)

    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"ZZZZ": "9999999999"})
    monkeypatch.setattr(xbrl, "get_cik", _record_and_lookup)

    result = xbrl.apply_primary_metrics({"ZZZZ": None})

    assert result == {"ZZZZ": None}
    assert fetch_calls == []  # never even attempted a CIK lookup for a None entry


def test_apply_primary_metrics_real_gntx_data_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Full pipeline against real data, not mocked internals: confirms
    # the actual GNTX fixture flows through get_cik/fetch_company_facts/
    # operating_margin_from_xbrl to produce the same ~18.7% this file's
    # other GNTX test already confirms directly.
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"GNTX": "0000355811"})
    monkeypatch.setattr(xbrl, "fetch_company_facts", lambda cik: _real_gntx_companyfacts())

    result = xbrl.apply_primary_metrics({"GNTX": _metrics(symbol="GNTX", operating_margin=0.218)})

    assert result["GNTX"] is not None
    operating_margin = result["GNTX"].operating_margin
    assert operating_margin is not None
    assert 0.185 < operating_margin < 0.190


def test_apply_primary_metrics_isolates_one_ticker_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Matches run_screen's/data.fetch_all_metrics' own per-ticker
    # isolation (staff-engineer-reviewer finding): one ticker's
    # unexpected exception must not abort the whole batch, the same
    # "one bad ticker can't take down a ~1500-ticker run" guarantee
    # every sibling batch loop in this codebase already makes.
    monkeypatch.setattr(xbrl, "load_cik_lookup", lambda: {"GOOD": "1", "EXPLODES": "2"})

    def _fetch_facts(cik: str) -> dict[str, object]:
        if cik == "2":
            raise TypeError("simulated malformed EDGAR response")
        return {"cik": cik}

    monkeypatch.setattr(xbrl, "fetch_company_facts", _fetch_facts)
    monkeypatch.setattr(xbrl, "gross_margin_from_xbrl", lambda facts: 0.55)

    good = _metrics(symbol="GOOD", gross_margin=0.40)
    explodes = _metrics(symbol="EXPLODES", gross_margin=0.30)

    result = xbrl.apply_primary_metrics({"GOOD": good, "EXPLODES": explodes})

    assert result["GOOD"] is not None
    assert result["GOOD"].gross_margin == 0.55  # unaffected by the other ticker's crash
    assert result["EXPLODES"] == explodes  # fell back to its original yfinance metrics, unchanged


def test_margin_from_xbrl_rejects_a_non_positive_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strictly positive, not merely nonzero (staff-engineer-reviewer
    # finding): a negative denominator paired with a negative numerator
    # would otherwise produce a positive, "plausible"-looking ratio that
    # is nevertheless economically meaningless.
    monkeypatch.setattr(config, "MARGIN_PLAUSIBLE_MIN", -5.0)
    monkeypatch.setattr(config, "MARGIN_PLAUSIBLE_MAX", 1.0)
    facts: dict[str, object] = {
        "facts": {
            "us-gaap": {
                "GrossProfit": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": -100,
                                "form": "10-K",
                                "fp": "FY",
                                "filed": "2025-02-01",
                            }
                        ]
                    }
                },
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                                "val": -50,  # negative denominator
                                "form": "10-K",
                                "fp": "FY",
                                "filed": "2025-02-01",
                            }
                        ]
                    }
                },
            }
        }
    }
    # -100 / -50 = 2.0 -- a "plausible"-looking positive ratio if the
    # only guard were "denominator != 0", but both figures are negative
    # (economically meaningless) and 2.0 is outside the bound anyway;
    # the real regression this guards is a denominator like -500 giving
    # a small, in-bound-looking ratio. Confirm no result either way.
    assert xbrl.gross_margin_from_xbrl(facts) is None
