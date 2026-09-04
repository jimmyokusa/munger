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
from pathlib import Path

import pandas as pd

import config
import data
import xbrl

logger = logging.getLogger(__name__)


def _write_csv_atomically(results: pd.DataFrame, path: Path) -> None:
    """Write the results CSV via temp-file + rename, not a direct write.

    A direct write leaves a window where a concurrent reader (report.py,
    run standalone and not synchronized with bot.py's own schedule) can
    see a torn/partial file mid-write and crash on `pd.read_csv` or worse,
    silently parse a malformed row -- staff-engineer-reviewer finding.
    Same atomicity pattern StateTracker already uses for state.json.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    results.to_csv(tmp_path, index=False)
    tmp_path.replace(path)


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


def fetched_fraction(results: pd.DataFrame) -> float:
    """Fraction of screened tickers that got real data (not fetch_failed).

    `1 - fraction tagged data_missing:fetch_failed` (DESIGN.md §5). A
    half-empty screen would make every holding look delisted (trading
    path) or archive a throttled snapshot as if it were clean history
    (daily-screen path) -- both callers gate on this against
    `config.MIN_UNIVERSE_FETCH_FRACTION`. Lives here (not in bot.py) so
    the screen-only `daily_screen` can use it without importing the
    trading/execution path.
    """
    if len(results) == 0:
        return 0.0
    fetch_failed = results["fail_reasons"].fillna("").str.contains("fetch_failed")
    return 1.0 - (float(fetch_failed.sum()) / len(results))


def fetch_metrics_with_xbrl_primary(
    tickers: list[str], phase: str = "screening"
) -> dict[str, data.Metrics | None]:
    """Fetch fundamentals, XBRL primary with yfinance fallback (M37, Design v2.2 §3.4).

    The one shared entry point every gate/score computation in this
    system should fetch through -- `run_screen` (below) uses it, and
    `evaluate.py`/`bot.py` call it directly for their own holdings-check
    fetches (they don't go through `run_screen`, since they already
    have a fixed ticker list and don't need a fresh universe screen).
    Centralizing here, not in `data.py` itself, keeps `data.fetch_all_metrics`
    a pure yfinance fetch -- xbrl_shadow.py's own shadow-comparison
    still needs two genuinely independent sources to compare, which a
    silent override baked into `data.py` would break.
    """
    return xbrl.apply_primary_metrics(data.fetch_all_metrics(tickers, phase=phase))


def run_screen(tickers: list[str]) -> pd.DataFrame:
    """Fetch, gate, and score every ticker; always writes screen_results.csv.

    Every run writes the CSV, not just on request -- so there's always a
    fresh eyeball-test artifact (DESIGN.md 3.3/3.6), even for exploratory
    runs. Key columns (symbol, buyable, score, fail_reasons) come first so
    the file is easy to scan sorted by score.
    """
    if not tickers:
        # pd.DataFrame([]) has zero columns, not just zero rows -- the
        # same empty-DataFrame quirk fixed in M6/M7's test_portfolio.py --
        # so sort_values("score", ...) below would raise KeyError on an
        # empty universe fetch (found live while testing M11's new
        # empty-universe alert path, which calls run_screen([]) exactly
        # this way).
        results = pd.DataFrame(columns=_KEY_COLUMNS)
        _write_csv_atomically(results, config.SCREEN_RESULTS_CSV_PATH)
        logger.info("Screen complete: 0 tickers, 0 buyable.")
        return results

    fetched = fetch_metrics_with_xbrl_primary(tickers)
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
                # Scored unconditionally, not just when buyable -- score is
                # a continuous quality signal across the whole universe
                # (DESIGN.md 3.3), independent of whether a ticker cleared
                # every hard Graham/Munger gate. calculate_munger_score
                # already tolerates partial metrics (each component
                # contributes 0 if its field is None), so this is safe even
                # for a ticker that failed a gate on missing/outlier data.
                "score": calculate_munger_score(metrics),
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
    _write_csv_atomically(results, config.SCREEN_RESULTS_CSV_PATH)
    logger.info(
        "Screen complete: %d tickers, %d buyable.", len(results), int(results["buyable"].sum())
    )
    return results
