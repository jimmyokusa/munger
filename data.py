"""Data fetcher module (DESIGN.md section 3.2).

Fetches per-ticker fundamentals from yfinance and returns a typed Metrics
record. This is the only module that should touch yfinance directly, so
swapping in a paid API later (Financial Modeling Prep, Polygon, Alpha
Vantage) is a one-file change.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import logging
import random
import time
from typing import Any

import pandas as pd
import yfinance as yf

import config

logger = logging.getLogger(__name__)

# yfinance's raw Ticker.info mixes units inconsistently: returnOnEquity,
# grossMargins, and operatingMargins are already decimal fractions (0.15 =
# 15%), but debtToEquity and dividendYield are raw percentage numbers
# (79.5 = 79.5%) -- confirmed live against several tickers (AAPL, KO, T,
# MSFT, JPM). Metrics normalizes both to decimal fractions so downstream
# code (config.py's thresholds are all decimal fractions) never has to
# know which raw field needed /100 and which didn't.
_PERCENT_SCALED_FIELDS = ("debtToEquity", "dividendYield")


@dataclasses.dataclass(frozen=True)
class Metrics:
    """Per-ticker fundamentals (DESIGN.md 3.2).

    Any field may be None if the provider didn't return it -- a missing
    field is not the same as a failed fetch (see fetch_metrics). Treating
    a None field as a failed check is the screener's job (M3/M4), not
    this module's.
    """

    symbol: str
    market_cap: float | None
    trailing_pe: float | None
    price_to_book: float | None
    current_ratio: float | None
    debt_to_equity: float | None
    return_on_equity: float | None
    gross_margin: float | None
    operating_margin: float | None
    free_cash_flow: float | None
    dividend_yield: float | None
    consecutive_positive_earnings_years: int | None


def _cache_raw_response(symbol: str, info: dict[str, Any], net_income: pd.Series | None) -> None:
    """Cache one ticker's raw provider response for this run, for debuggability."""
    config.DATA_RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = config.DATA_RAW_CACHE_DIR / f"{symbol}.json"
    payload = {
        "info": info,
        "net_income": {str(k): v for k, v in net_income.items()}
        if net_income is not None
        else None,
    }
    cache_path.write_text(json.dumps(payload, default=str))


def _fetch_raw(symbol: str) -> tuple[dict[str, Any], pd.Series | None]:
    """Fetch one ticker's raw info dict and net-income history.

    Raises on a failure worth retrying: a network error, or a response
    that doesn't actually match the requested symbol. yfinance does not
    raise for an invalid/delisted ticker -- it silently returns a
    near-empty info dict (confirmed live: a bogus symbol returns
    ``{"trailingPegRatio": None}``, one key, vs. ~170+ for a real ticker)
    -- so that has to be detected explicitly rather than relying on an
    exception. A near-empty dict can also happen under rate-limiting, not
    just a genuinely bad symbol, so this case is retried like any other --
    the key count is logged and the raw response is cached (even though
    it's about to be rejected) specifically so an operator can tell the
    two apart after the fact, which the exception type alone can't convey.
    """
    ticker = yf.Ticker(symbol)
    info = ticker.info
    if info.get("symbol") != symbol:
        _cache_raw_response(symbol, info, None)
        raise ValueError(
            f"no data returned for {symbol!r} (provider response didn't match; "
            f"{len(info)} keys returned, {info!r})"
        )

    net_income: pd.Series | None = None
    try:
        income_stmt = ticker.income_stmt
        if income_stmt is not None and "Net Income" in income_stmt.index:
            net_income = income_stmt.loc["Net Income"].sort_index(ascending=False)
    except Exception:
        # A ticker genuinely lacking published financials (recent IPO,
        # some ADRs) is a legitimate missing-data case, not a transient
        # failure worth retrying the whole ticker over --
        # consecutive_positive_earnings_years just comes back None below,
        # same as any other missing field.
        logger.warning("%s: income statement fetch failed", symbol, exc_info=True)

    return info, net_income


def _consecutive_positive_years(net_income: pd.Series | None) -> int | None:
    """Count consecutive positive years starting from the most recent.

    Explicitly sorts descending rather than trusting yfinance's column
    order (confirmed live to already be descending, but that's an
    assumption about a third party, not a guarantee).
    """
    if net_income is None:
        return None
    count = 0
    for value in net_income.sort_index(ascending=False):
        if pd.isna(value) or value <= 0:
            break
        count += 1
    return count


def _normalize_percent_field(info: dict[str, Any], key: str) -> float | None:
    """Convert one of _PERCENT_SCALED_FIELDS from a raw percentage to a decimal fraction.

    Treats a non-numeric value as missing (logs and returns None) rather
    than raising: this runs inside fetch_metrics' retry loop, and a single
    malformed field must not be indistinguishable from a network error --
    that would burn all retries and discard every other successfully-
    fetched field on this ticker over one bad value, exactly the "missing
    field vs. failed fetch" conflation this module is designed to avoid.
    """
    value: Any = info.get(key)
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        logger.warning("%s: non-numeric value %r, treating as missing", key, value)
        return None


def fetch_metrics(symbol: str) -> Metrics | None:
    """Fetch and return one ticker's fundamentals, retrying transient failures.

    Returns None only if every attempt fails outright (network error, or
    the provider not recognizing the symbol at all) -- per-field missing
    data within an otherwise-successful fetch produces a Metrics record
    with those fields set to None, not a None return. Callers (the thread
    pool in fetch_all_metrics) must tolerate a None return for any single
    ticker without aborting the run.
    """
    last_error: Exception | None = None
    for attempt in range(config.DATA_FETCH_MAX_RETRIES):
        try:
            info, net_income = _fetch_raw(symbol)
            _cache_raw_response(symbol, info, net_income)
            return Metrics(
                symbol=symbol,
                market_cap=info.get("marketCap"),
                trailing_pe=info.get("trailingPE"),
                price_to_book=info.get("priceToBook"),
                current_ratio=info.get("currentRatio"),
                debt_to_equity=_normalize_percent_field(info, "debtToEquity"),
                return_on_equity=info.get("returnOnEquity"),
                gross_margin=info.get("grossMargins"),
                operating_margin=info.get("operatingMargins"),
                free_cash_flow=info.get("freeCashflow"),
                dividend_yield=_normalize_percent_field(info, "dividendYield"),
                consecutive_positive_earnings_years=_consecutive_positive_years(net_income),
            )
        except Exception as e:
            last_error = e
            if attempt < config.DATA_FETCH_MAX_RETRIES - 1:
                # Exponential backoff with jitter, not a flat delay: with
                # config.DATA_FETCH_THREAD_POOL_WORKERS threads all hitting
                # a rate limit around the same time, a fixed delay would
                # have them all retry in near-lockstep -- recreating the
                # burst that caused the 429 instead of spreading it out.
                delay = config.DATA_FETCH_RETRY_BACKOFF_SECONDS * (2**attempt)
                time.sleep(delay + random.uniform(0, delay * 0.25))
    logger.warning(
        "%s: fetch failed after %d attempts",
        symbol,
        config.DATA_FETCH_MAX_RETRIES,
        exc_info=last_error,
    )
    return None


def fetch_all_metrics(symbols: list[str]) -> dict[str, Metrics | None]:
    """Fetch fundamentals for many tickers concurrently.

    Uses a thread pool (config.DATA_FETCH_THREAD_POOL_WORKERS) since this
    is I/O-bound network fetching, not CPU-bound work. Each ticker's
    failure is independent -- a None result for one ticker never aborts
    the batch.

    Bounded by config.DATA_FETCH_BATCH_TIMEOUT_SECONDS: if a worker
    genuinely hangs (a stalled connection, no exception ever raised),
    fetch_metrics has no per-call timeout of its own -- yfinance exposes
    no simple hook for one -- so this is the backstop that keeps the
    *logical* result bounded rather than blocking the caller forever.
    Note this cannot forcibly kill a hung worker thread (a stdlib
    ThreadPoolExecutor limitation); the process itself may still not exit
    cleanly until it finishes or an outer timeout (e.g. the scheduler
    wrapping `bot.py`) kills it. Tracked in TASKS.md as a known residual
    risk, not solved here.
    """
    results: dict[str, Metrics | None] = {}
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.DATA_FETCH_THREAD_POOL_WORKERS
    )
    future_to_symbol = {executor.submit(fetch_metrics, symbol): symbol for symbol in symbols}
    done, not_done = concurrent.futures.wait(
        future_to_symbol, timeout=config.DATA_FETCH_BATCH_TIMEOUT_SECONDS
    )
    for future in done:
        symbol = future_to_symbol[future]
        try:
            results[symbol] = future.result()
        except Exception:
            logger.error("%s: unexpected exception escaped fetch_metrics", symbol, exc_info=True)
            results[symbol] = None
    if not_done:
        stuck_symbols = [future_to_symbol[future] for future in not_done]
        logger.error(
            "Batch fetch timed out after %ds with %d ticker(s) still pending: %s",
            config.DATA_FETCH_BATCH_TIMEOUT_SECONDS,
            len(stuck_symbols),
            stuck_symbols,
        )
        for symbol in stuck_symbols:
            results[symbol] = None
    executor.shutdown(wait=False, cancel_futures=True)
    return results
