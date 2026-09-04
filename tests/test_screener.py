"""Unit tests for screener.py against hand-computed fixtures
(DESIGN.md section 6, layer 1)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

import config
import data
import screener
import xbrl


@pytest.fixture(autouse=True)
def _no_real_xbrl_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to XBRL-primary being a no-op passthrough (M37).

    Without this, every `run_screen`/`fetch_metrics_with_xbrl_primary`
    call in this file would fall through to a real
    `xbrl.apply_primary_metrics`, which attempts a real network call to
    SEC EDGAR (it degrades safely -- an empty CIK lookup on failure --
    but still reaches out over the network during a test run, which
    this project's own testing discipline forbids). Tests that actually
    want to exercise the XBRL-primary override behavior itself
    monkeypatch `xbrl.apply_primary_metrics` again, locally, to override
    this default.
    """
    monkeypatch.setattr(xbrl, "apply_primary_metrics", lambda metrics_by_symbol: metrics_by_symbol)


def _passing_metrics(**overrides: Any) -> data.Metrics:
    """A Metrics record that cleanly passes every Graham gate and every
    Munger quality floor at default config thresholds -- the baseline
    every single-gate-failure fixture mutates one field away from."""
    defaults: dict[str, Any] = {
        "symbol": "TEST",
        "market_cap": 5_000_000_000.0,
        "trailing_pe": 15.0,
        "price_to_book": 1.5,  # pe * pb = 22.5, under MAX_PE_TIMES_PB (30)
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


# --- pass_graham_gates: one fixture per gate ---


def test_graham_gates_baseline_passes_cleanly() -> None:
    passed, reasons = screener.pass_graham_gates(_passing_metrics())
    assert passed is True
    assert reasons == []


def test_graham_gate_size_fails_below_min_market_cap() -> None:
    metrics = _passing_metrics(market_cap=1_000_000_000.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_size"]


def test_graham_gate_current_ratio_fails_below_minimum() -> None:
    metrics = _passing_metrics(current_ratio=1.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_current_ratio"]


def test_graham_gate_debt_to_equity_fails_above_maximum() -> None:
    metrics = _passing_metrics(debt_to_equity=1.5)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_debt_to_equity"]


def test_graham_gate_debt_to_equity_fails_when_negative() -> None:
    # Negative D/E means negative book equity (liabilities > assets) --
    # a real red flag, not "even less debt than zero". Must fail, not
    # silently pass because a negative number compares less than the
    # maximum -- the same trap already guarded for trailing_pe.
    metrics = _passing_metrics(debt_to_equity=-10.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_debt_to_equity"]


def test_graham_gate_earnings_stability_fails_below_minimum_years() -> None:
    metrics = _passing_metrics(consecutive_positive_earnings_years=2)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_earnings_stability"]


def test_graham_gate_dividend_record_off_by_default() -> None:
    # REQUIRE_DIVIDEND_RECORD defaults to False -- no dividend shouldn't
    # fail anything unless explicitly turned on. Previously this still
    # failed via data_missing:dividend_yield (a bug: validate_metrics
    # treated "no dividend" as missing data) -- fixed by excluding
    # dividend_yield from data._REQUIRED_METRICS_FIELDS.
    metrics = _passing_metrics(dividend_yield=None)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is True
    assert reasons == []


def test_graham_gate_dividend_record_fails_when_required_and_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "REQUIRE_DIVIDEND_RECORD", True)
    metrics = _passing_metrics(dividend_yield=0.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_dividend_record"]


def test_graham_gate_pe_fails_above_maximum() -> None:
    metrics = _passing_metrics(trailing_pe=25.0, price_to_book=1.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert "graham_pe" in reasons


def test_graham_gate_pe_fails_when_negative() -> None:
    # A negative P/E means negative TTM earnings -- must fail, not
    # silently pass because a negative number compares less than MAX_PE.
    metrics = _passing_metrics(trailing_pe=-5.0, price_to_book=1.0)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert "graham_pe" in reasons


def test_graham_gate_pe_times_pb_fails_above_maximum() -> None:
    # trailing_pe alone (15) stays under MAX_PE (20), isolating this gate.
    metrics = _passing_metrics(trailing_pe=15.0, price_to_book=3.0)  # 45 > 30
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["graham_pe_times_pb"]


def test_graham_gates_combined_failure_reports_every_failing_gate() -> None:
    metrics = _passing_metrics(
        market_cap=1_000_000_000.0,
        current_ratio=1.0,
        debt_to_equity=1.5,
    )
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert set(reasons) == {"graham_size", "graham_current_ratio", "graham_debt_to_equity"}


def test_graham_gates_missing_data_fails_conservatively() -> None:
    metrics = _passing_metrics(market_cap=None)
    passed, reasons = screener.pass_graham_gates(metrics)
    assert passed is False
    assert reasons == ["data_missing:market_cap"]


# --- pass_munger_quality_floors ---


def test_munger_floors_baseline_passes_cleanly() -> None:
    passed, reasons = screener.pass_munger_quality_floors(_passing_metrics())
    assert passed is True
    assert reasons == []


def test_munger_floor_roe_fails_below_minimum() -> None:
    metrics = _passing_metrics(return_on_equity=0.10)
    passed, reasons = screener.pass_munger_quality_floors(metrics)
    assert passed is False
    assert reasons == ["munger_roe"]


def test_munger_floor_gross_margin_fails_below_minimum() -> None:
    metrics = _passing_metrics(gross_margin=0.20)
    passed, reasons = screener.pass_munger_quality_floors(metrics)
    assert passed is False
    assert reasons == ["munger_gross_margin"]


def test_munger_floor_fcf_fails_when_negative() -> None:
    metrics = _passing_metrics(free_cash_flow=-100.0)
    passed, reasons = screener.pass_munger_quality_floors(metrics)
    assert passed is False
    assert reasons == ["munger_fcf"]


def test_munger_floors_standalone_correct_without_graham() -> None:
    # This function is called on its own by the M6 sell discipline, so it
    # must independently detect missing data, not rely on
    # pass_graham_gates having been called first.
    metrics = _passing_metrics(return_on_equity=None)
    passed, reasons = screener.pass_munger_quality_floors(metrics)
    assert passed is False
    assert reasons == ["data_missing:return_on_equity"]


# --- calculate_munger_score: one fixture per weighted component ---


def test_score_roe_component_at_normalization_target() -> None:
    metrics = _passing_metrics(
        return_on_equity=config.SCORE_NORMALIZATION_ROE,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=None,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(config.SCORE_WEIGHT_ROE * 100)


def test_score_gross_margin_component_at_normalization_target() -> None:
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=config.SCORE_NORMALIZATION_GROSS_MARGIN,
        operating_margin=None,
        free_cash_flow=None,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(
        config.SCORE_WEIGHT_GROSS_MARGIN * 100
    )


def test_score_operating_margin_component_at_normalization_target() -> None:
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=None,
        operating_margin=config.SCORE_NORMALIZATION_OPERATING_MARGIN,
        free_cash_flow=None,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(
        config.SCORE_WEIGHT_OPERATING_MARGIN * 100
    )


def test_score_fcf_yield_component_at_normalization_target() -> None:
    market_cap = 1_000.0
    fcf = market_cap * config.SCORE_NORMALIZATION_FCF_YIELD
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=fcf,
        market_cap=market_cap,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(
        config.SCORE_WEIGHT_FCF_YIELD * 100
    )


def test_score_low_debt_component_negative_debt_scores_zero_not_maximal() -> None:
    # 1.0 - (negative) exceeds 1.0 and would otherwise clamp to the same
    # maximal credit as genuinely zero debt -- negative book equity must
    # not score as "better than zero debt".
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=None,
        debt_to_equity=-10.0,
    )
    assert screener.calculate_munger_score(metrics) == 0.0


def test_score_low_debt_component_at_zero_debt() -> None:
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=None,
        debt_to_equity=0.0,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(
        config.SCORE_WEIGHT_LOW_DEBT * 100
    )


def test_score_clamps_above_normalization_target() -> None:
    # Double the ROE normalization target must not exceed that
    # component's weight -- no bonus for going beyond the target.
    metrics = _passing_metrics(
        return_on_equity=config.SCORE_NORMALIZATION_ROE * 2,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=None,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(config.SCORE_WEIGHT_ROE * 100)


def test_score_combined_all_components_at_target_is_100() -> None:
    market_cap = 1_000.0
    metrics = _passing_metrics(
        return_on_equity=config.SCORE_NORMALIZATION_ROE,
        gross_margin=config.SCORE_NORMALIZATION_GROSS_MARGIN,
        operating_margin=config.SCORE_NORMALIZATION_OPERATING_MARGIN,
        free_cash_flow=market_cap * config.SCORE_NORMALIZATION_FCF_YIELD,
        market_cap=market_cap,
        debt_to_equity=0.0,
    )
    assert screener.calculate_munger_score(metrics) == pytest.approx(100.0)


def test_score_missing_fields_contribute_zero_not_crash() -> None:
    metrics = _passing_metrics(
        return_on_equity=None,
        gross_margin=None,
        operating_margin=None,
        free_cash_flow=None,
        market_cap=None,
        debt_to_equity=None,
    )
    assert screener.calculate_munger_score(metrics) == 0.0


# --- run_screen ---


def test_run_screen_writes_csv_and_sorts_by_score(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    csv_path = tmp_path / "screen_results.csv"
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", csv_path)

    high_score = _passing_metrics(symbol="HIGH", return_on_equity=config.SCORE_NORMALIZATION_ROE)
    low_score = _passing_metrics(symbol="LOW", return_on_equity=config.MIN_ROE)
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"HIGH": high_score, "LOW": low_score}
    )

    results = screener.run_screen(["HIGH", "LOW"])

    assert csv_path.exists()
    assert list(results["symbol"]) == ["HIGH", "LOW"]  # sorted by score descending
    assert bool(results.iloc[0]["buyable"]) is True
    assert list(results.columns[:4]) == ["symbol", "buyable", "score", "fail_reasons"]


def test_run_screen_scores_a_ticker_that_fails_a_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # User request: score is a continuous quality signal across the whole
    # universe, not just among buyable survivors -- previously every
    # non-buyable ticker was forced to score=0.0 regardless of its actual
    # quality metrics, which made "most tickers score 0" look like a data
    # problem when it was really this gating.
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    failing_gate_metrics = _passing_metrics(symbol="SMALLCAP", market_cap=1_000_000_000.0)
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"SMALLCAP": failing_gate_metrics}
    )

    results = screener.run_screen(["SMALLCAP"])

    row = results.iloc[0]
    assert bool(row["buyable"]) is False
    assert row["fail_reasons"] == "graham_size"
    assert row["score"] == pytest.approx(screener.calculate_munger_score(failing_gate_metrics))
    assert row["score"] > 0.0


def test_run_screen_handles_an_empty_ticker_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # pd.DataFrame([]) has zero columns, not just zero rows -- found live
    # while testing M11's empty-universe alert path, which calls
    # run_screen([]) exactly this way; sort_values("score", ...) would
    # otherwise raise KeyError on a genuinely empty universe fetch.
    csv_path = tmp_path / "screen_results.csv"
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", csv_path)

    results = screener.run_screen([])

    assert csv_path.exists()
    assert len(results) == 0
    assert list(results.columns) == ["symbol", "buyable", "score", "fail_reasons"]


def test_run_screen_handles_total_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    ticker_metrics = {"GOOD": _passing_metrics(symbol="GOOD"), "MISSING": None}
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: ticker_metrics)

    results = screener.run_screen(["GOOD", "MISSING"])

    missing_row = results[results["symbol"] == "MISSING"].iloc[0]
    assert bool(missing_row["buyable"]) is False
    assert missing_row["fail_reasons"] == "data_missing:fetch_failed"


def test_run_screen_isolates_a_single_ticker_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Confirmed live: an unexpected crash computing gates/score for one
    # ticker must not abort the whole run -- the same per-ticker
    # isolation the fetch layer already has, applied at the screening
    # layer too.
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    ticker_metrics = {
        "GOOD": _passing_metrics(symbol="GOOD"),
        "EXPLODES": _passing_metrics(symbol="EXPLODES"),
    }
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: ticker_metrics)

    original_pass_graham_gates = screener.pass_graham_gates

    def _raising_pass_graham_gates(metrics: data.Metrics) -> tuple[bool, list[str]]:
        if metrics.symbol == "EXPLODES":
            raise TypeError("simulated unexpected crash")
        return original_pass_graham_gates(metrics)

    monkeypatch.setattr(screener, "pass_graham_gates", _raising_pass_graham_gates)

    results = screener.run_screen(["GOOD", "EXPLODES"])

    exploded_row = results[results["symbol"] == "EXPLODES"].iloc[0]
    good_row = results[results["symbol"] == "GOOD"].iloc[0]
    assert bool(exploded_row["buyable"]) is False
    assert exploded_row["fail_reasons"] == "data_invalid_outlier:unhandled"
    assert bool(good_row["buyable"]) is True


def test_fetched_fraction_all_clean() -> None:
    # Direct test of the single source of truth (staff-engineer-reviewer):
    # bot._fetched_fraction now delegates here, and daily_screen.py's
    # archive gate calls this directly -- both share this exact function.
    results = pd.DataFrame(
        [{"symbol": "A", "buyable": True, "score": 1.0, "fail_reasons": "graham_pe"}]
    )
    assert screener.fetched_fraction(results) == 1.0


def test_fetched_fraction_some_missing() -> None:
    results = pd.DataFrame(
        [
            {
                "symbol": "A",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
            {"symbol": "B", "buyable": True, "score": 1.0, "fail_reasons": ""},
        ]
    )
    assert screener.fetched_fraction(results) == pytest.approx(0.5)


def test_fetched_fraction_empty_results() -> None:
    assert screener.fetched_fraction(pd.DataFrame(columns=["symbol", "fail_reasons"])) == 0.0


# --- fetch_metrics_with_xbrl_primary / run_screen's XBRL-primary wiring (M37) ---


def test_fetch_metrics_with_xbrl_primary_composes_fetch_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"AAA": _passing_metrics(symbol="AAA")},
    )
    monkeypatch.setattr(
        xbrl,
        "apply_primary_metrics",
        lambda metrics_by_symbol: {
            s: (m.__class__(**{**m.__dict__, "gross_margin": 0.99}) if m else None)
            for s, m in metrics_by_symbol.items()
        },
    )

    result = screener.fetch_metrics_with_xbrl_primary(["AAA"])

    assert result["AAA"] is not None
    assert result["AAA"].gross_margin == 0.99


def test_run_screen_gates_on_the_xbrl_overridden_value_not_yfinances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ticker that passes on yfinance's own gross_margin but XBRL
    # overrides to below the floor must show up as not-buyable -- proves
    # run_screen actually gates on the post-override value, not a stale
    # yfinance-only figure.
    failing_via_xbrl = _passing_metrics(symbol="AAA", gross_margin=0.40)  # would pass as-is
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {"AAA": failing_via_xbrl}
    )
    monkeypatch.setattr(
        xbrl,
        "apply_primary_metrics",
        lambda metrics_by_symbol: {
            s: (
                data.Metrics(**{**m.__dict__, "gross_margin": 0.05})  # below MIN_GROSS_MARGIN
                if m
                else None
            )
            for s, m in metrics_by_symbol.items()
        },
    )

    results = screener.run_screen(["AAA"])

    row = results[results["symbol"] == "AAA"].iloc[0]
    assert bool(row["buyable"]) is False
    assert "munger_gross_margin" in row["fail_reasons"]
    assert row["gross_margin"] == 0.05
