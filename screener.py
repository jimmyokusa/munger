"""Screener module (DESIGN.md section 3.3).

Two-stage screen: Graham entry gates (pass/fail) followed by Munger
quality floors + a 0-100 composite score, for ranking. Both stages are
independently self-contained -- each starts from data.validate_metrics()
so either can be called standalone with the same "no data, no buy"
conservatism, not just when run together in run_screen(). This matters
because the sell discipline (DESIGN.md 3.4, M6) re-checks existing
holdings against the Munger quality floors only, never the Graham gates.
"""

from __future__ import annotations

import logging

import pandas as pd

import config
import data

logger = logging.getLogger(__name__)


def pass_graham_gates(metrics: data.Metrics) -> tuple[bool, list[str]]:
    """Stage 1: Graham's defensive-investor entry gates (DESIGN.md 3.3).

    A gate whose required field is missing or an implausible outlier
    can't be meaningfully evaluated, so it's skipped -- but the
    data_missing:*/data_invalid_outlier:* reason from validate_metrics
    already makes the overall result fail (conservatism: no data, no
    buy), it just doesn't ALSO get a misleading graham_* reason for a
    value that was never there to check.
    """
    fail_reasons = list(data.validate_metrics(metrics))

    if metrics.market_cap is not None and metrics.market_cap < config.MIN_MARKET_CAP:
        fail_reasons.append("graham_size")

    if metrics.current_ratio is not None and metrics.current_ratio < config.MIN_CURRENT_RATIO:
        fail_reasons.append("graham_current_ratio")

    # Negative debt/equity means negative book equity (liabilities exceed
    # assets) -- a red flag, not "low debt". Must fail, not silently pass
    # because a negative number compares less than MAX_DEBT_TO_EQUITY (the
    # same trap already guarded against for trailing_pe below).
    if metrics.debt_to_equity is not None and (
        metrics.debt_to_equity < 0 or metrics.debt_to_equity > config.MAX_DEBT_TO_EQUITY
    ):
        fail_reasons.append("graham_debt_to_equity")

    if (
        metrics.consecutive_positive_earnings_years is not None
        and metrics.consecutive_positive_earnings_years
        < config.MIN_CONSECUTIVE_POSITIVE_EARNINGS_YEARS
    ):
        fail_reasons.append("graham_earnings_stability")

    if config.REQUIRE_DIVIDEND_RECORD and (
        metrics.dividend_yield is None or metrics.dividend_yield <= 0
    ):
        fail_reasons.append("graham_dividend_record")

    # A negative trailing P/E means negative TTM earnings, which fails
    # the spirit of a "moderate P/E" gate even though the raw number
    # isn't numerically greater than MAX_PE -- must not silently pass a
    # money-losing company just because a negative number compares less
    # than a positive threshold.
    if metrics.trailing_pe is not None and (
        metrics.trailing_pe <= 0 or metrics.trailing_pe > config.MAX_PE
    ):
        fail_reasons.append("graham_pe")

    if metrics.trailing_pe is not None and metrics.price_to_book is not None:
        combined_multiplier = metrics.trailing_pe * metrics.price_to_book
        if combined_multiplier <= 0 or combined_multiplier > config.MAX_PE_TIMES_PB:
            fail_reasons.append("graham_pe_times_pb")

    return len(fail_reasons) == 0, fail_reasons


def pass_munger_quality_floors(metrics: data.Metrics) -> tuple[bool, list[str]]:
    """Stage 2 floors: ROE, gross margin, positive FCF (DESIGN.md 3.3).

    Standalone-correct like pass_graham_gates -- also called on its own
    by the M6 sell discipline against existing holdings, not just as
    part of the two-stage buy screen here.
    """
    fail_reasons = list(data.validate_metrics(metrics))

    if metrics.return_on_equity is not None and metrics.return_on_equity < config.MIN_ROE:
        fail_reasons.append("munger_roe")

    if metrics.gross_margin is not None and metrics.gross_margin < config.MIN_GROSS_MARGIN:
        fail_reasons.append("munger_gross_margin")

    if (
        config.REQUIRE_POSITIVE_FCF
        and metrics.free_cash_flow is not None
        and metrics.free_cash_flow <= 0
    ):
        fail_reasons.append("munger_fcf")

    return len(fail_reasons) == 0, fail_reasons


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def calculate_munger_score(metrics: data.Metrics) -> float:
    """0-100 composite quality score (DESIGN.md 3.3 Stage 2), for ranking.

    Purely a ranking signal -- doesn't gate anything itself; callers
    should already have confirmed both stages passed. Each component is
    normalized against its configured target (a value at or beyond the
    target counts as a full 1.0 for that component, no bonus beyond it),
    then combined by config.py's SCORE_WEIGHT_* values (sum to 1.0,
    enforced by test_config.py). A None or non-computable component
    (e.g. FCF yield needs a nonzero market cap) contributes 0, not a
    crash -- this function may be called on partially-populated records.
    """
    score = 0.0

    if metrics.return_on_equity is not None:
        score += config.SCORE_WEIGHT_ROE * _clamp01(
            metrics.return_on_equity / config.SCORE_NORMALIZATION_ROE
        )
    if metrics.gross_margin is not None:
        score += config.SCORE_WEIGHT_GROSS_MARGIN * _clamp01(
            metrics.gross_margin / config.SCORE_NORMALIZATION_GROSS_MARGIN
        )
    if metrics.operating_margin is not None:
        score += config.SCORE_WEIGHT_OPERATING_MARGIN * _clamp01(
            metrics.operating_margin / config.SCORE_NORMALIZATION_OPERATING_MARGIN
        )
    if metrics.free_cash_flow is not None and metrics.market_cap:
        fcf_yield = metrics.free_cash_flow / metrics.market_cap
        score += config.SCORE_WEIGHT_FCF_YIELD * _clamp01(
            fcf_yield / config.SCORE_NORMALIZATION_FCF_YIELD
        )
    # Negative debt_to_equity (negative book equity) must not score as
    # "better than zero debt" -- 1.0 - (negative) exceeds 1.0 and would
    # otherwise clamp to the same maximal credit as genuinely zero debt.
    # This function is defense-in-depth, independent of the gate above
    # already excluding negative-D/E tickers from ever reaching "buyable"
    # (where a score is actually computed) -- it shouldn't produce a
    # misleading component value regardless of caller.
    if metrics.debt_to_equity is not None and metrics.debt_to_equity >= 0:
        score += config.SCORE_WEIGHT_LOW_DEBT * _clamp01(1.0 - metrics.debt_to_equity)

    return score * 100.0


_KEY_COLUMNS = ["symbol", "buyable", "score", "fail_reasons"]


def run_screen(tickers: list[str]) -> pd.DataFrame:
    """Fetch, gate, and score every ticker; always writes screen_results.csv.

    Every run writes the CSV, not just on request -- so there's always a
    fresh eyeball-test artifact (DESIGN.md 3.3/3.6), even for exploratory
    runs. Key columns (symbol, buyable, score, fail_reasons) come first so
    the file is easy to scan sorted by score.
    """
    fetched = data.fetch_all_metrics(tickers)
    rows: list[dict[str, object]] = []

    for symbol in tickers:
        metrics = fetched.get(symbol)
        if metrics is None:
            rows.append(
                {
                    "symbol": symbol,
                    "buyable": False,
                    "score": 0.0,
                    "fail_reasons": "data_missing:fetch_failed",
                }
            )
            continue

        try:
            graham_passed, graham_reasons = pass_graham_gates(metrics)
            quality_passed, quality_reasons = pass_munger_quality_floors(metrics)
        except Exception:
            # Defense in depth: confirmed live that a single malformed
            # field can crash gate/score computation with an uncaught
            # exception (see data._coerce_float's docstring) -- one
            # ticker's unexpected data shape must not abort the whole
            # ~1500-ticker run, the same per-ticker isolation DESIGN.md
            # 3.2 already requires at the fetch layer, applied here too.
            logger.error("%s: gate/score computation failed", symbol, exc_info=True)
            rows.append(
                {
                    "symbol": symbol,
                    "buyable": False,
                    "score": 0.0,
                    "fail_reasons": "data_invalid_outlier:unhandled",
                }
            )
            continue

        buyable = graham_passed and quality_passed
        # dict.fromkeys dedupes while preserving order -- both stages
        # independently run validate_metrics(), so a missing field common
        # to both (e.g. an outlier P/E) would otherwise appear twice.
        fail_reasons = list(dict.fromkeys(graham_reasons + quality_reasons))

        rows.append(
            {
                "symbol": symbol,
                "buyable": buyable,
                "score": calculate_munger_score(metrics) if buyable else 0.0,
                "fail_reasons": ",".join(fail_reasons),
                "market_cap": metrics.market_cap,
                "trailing_pe": metrics.trailing_pe,
                "price_to_book": metrics.price_to_book,
                "current_ratio": metrics.current_ratio,
                "debt_to_equity": metrics.debt_to_equity,
                "return_on_equity": metrics.return_on_equity,
                "gross_margin": metrics.gross_margin,
                "operating_margin": metrics.operating_margin,
                "free_cash_flow": metrics.free_cash_flow,
                "dividend_yield": metrics.dividend_yield,
                "consecutive_positive_earnings_years": metrics.consecutive_positive_earnings_years,
            }
        )

    results = pd.DataFrame(rows)
    results = results.sort_values("score", ascending=False).reset_index(drop=True)
    other_columns = [c for c in results.columns if c not in _KEY_COLUMNS]
    results = results[_KEY_COLUMNS + other_columns]
    results.to_csv(config.SCREEN_RESULTS_CSV_PATH, index=False)
    logger.info(
        "Screen complete: %d tickers, %d buyable.", len(results), int(results["buyable"].sum())
    )
    return results
