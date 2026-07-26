"""Data fetcher module (DESIGN.md section 3.2).

Fetches per-ticker fundamentals from yfinance and returns a typed Metrics
record. This is the only module that should touch yfinance directly, so
swapping in a paid API later (Financial Modeling Prep, Polygon, Alpha
Vantage) is a one-file change.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import json
import logging
import random
import threading
import time
from typing import Any

import pandas as pd
import yfinance as yf
import yfinance.exceptions

import config

logger = logging.getLogger(__name__)

# Live progress for the HTML report's progress bar (M13/report.py) --
# guarded by the same kind of lock as the rate-limit state above, since
# many worker threads update this concurrently. Written to a file inside
# config.REPORT_DIR (not just kept in memory) so a page served by nginx
# from a different process can poll it.
_progress_lock = threading.Lock()
_progress_total = 0
_progress_completed = 0
_progress_in_flight: set[str] = set()
_progress_phase = ""
# Monotonic timestamp of the last *attempted* progress-file write (success
# or failure), used to throttle the high-volume per-ticker calls -- see
# config.PROGRESS_WRITE_MIN_INTERVAL_SECONDS. Starts at 0.0 so the very
# first call, whenever it happens, is never skipped.
_progress_last_write_monotonic = 0.0


def _write_progress_file(*, force: bool = True) -> None:
    # The write+rename must happen INSIDE the lock, not just the payload
    # construction -- every worker thread shares the same tmp_path, so
    # two threads racing past an unlocked write/rename could have one
    # overwrite the other's temp file before it renames, and the loser's
    # .replace() then raises FileNotFoundError (caught live by this
    # module's own test suite, not by inspection).
    #
    # This whole function must never raise. It's called from
    # fetch_metrics's `finally` block *after* a real fetch has already
    # succeeded -- if it raised there, Python's finally-supersedes-return
    # semantics would discard that already-fetched Metrics record and
    # report the ticker as failed, purely because a cosmetic progress
    # display couldn't write a file (disk full, permissions). It's also
    # called before the real fetch even starts (_mark_ticker_started),
    # where an unguarded raise would skip fetching entirely. Staff-
    # engineer-reviewer finding: this display-only side channel must
    # never be able to affect real data-fetch outcomes.
    #
    # force=False (used only by the per-ticker start/done calls) applies
    # PROGRESS_WRITE_MIN_INTERVAL_SECONDS: on a GCS-backed deployment,
    # writing this same object on every one of ~3000 per-ticker events
    # blows through GCS's per-object mutation rate limit and the
    # resulting 429 retries starve the fetch threads themselves (real
    # 2026-07-25 Cloud Run incident -- see config.py). Skipping a throttled
    # write only delays *this* update; _start_progress/_finish_progress
    # always force a write, so the batch's start and final state are never
    # lost even if every per-ticker write in between was skipped.
    try:
        global _progress_last_write_monotonic
        with _progress_lock:
            now_monotonic = time.monotonic()
            if not force and (
                now_monotonic - _progress_last_write_monotonic
                < config.PROGRESS_WRITE_MIN_INTERVAL_SECONDS
            ):
                return
            # Record the timestamp of every write that actually proceeds
            # (force=True or a force=False call that passed the throttle),
            # not just throttled ones -- otherwise a force=True write (which
            # never updated this) would leave the *next* force=False call
            # measuring elapsed time from a stale/zero timestamp and write
            # immediately regardless of how recently the force=True write
            # just happened.
            _progress_last_write_monotonic = now_monotonic

            with _rate_limit_lock:
                rate_limited_until = _rate_limited_until
            payload = {
                "phase": _progress_phase,
                "total": _progress_total,
                "completed": _progress_completed,
                "in_flight": sorted(_progress_in_flight),
                # User-requested (2026-07-23): the report's live view should
                # say explicitly when missing data is because a shared
                # yfinance rate-limit cooldown is in effect -- not that the
                # data doesn't exist. now() < rate_limited_until means
                # every worker is currently paused waiting it out.
                "rate_limited_until": rate_limited_until,
                "now": time.time(),
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = config.PROGRESS_FILE_PATH.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload))
            tmp_path.replace(config.PROGRESS_FILE_PATH)
    except Exception:
        logger.warning("Failed to write live-progress file (cosmetic only)", exc_info=True)


def _start_progress(phase: str, total: int) -> None:
    global _progress_total, _progress_completed, _progress_phase
    with _progress_lock:
        _progress_phase = phase
        _progress_total = total
        _progress_completed = 0
        _progress_in_flight.clear()
    _write_progress_file(force=True)


def _mark_ticker_started(symbol: str) -> None:
    with _progress_lock:
        _progress_in_flight.add(symbol)
    _write_progress_file(force=False)


def _mark_ticker_done(symbol: str) -> None:
    global _progress_completed
    with _progress_lock:
        _progress_completed += 1
        _progress_in_flight.discard(symbol)
    _write_progress_file(force=False)


def _finish_progress() -> None:
    """Mark the batch complete so the report's progress bar hides itself.

    Leaves the file in place (rather than deleting it) with completed ==
    total, so a page that polls right after a run finishes sees a clean
    100% state instead of a brief 404/missing-file gap. Also clears
    in_flight explicitly, rather than assuming it's already empty --
    normally true (every completed future's `finally` already called
    _mark_ticker_done), but not if fetch_all_metrics's own documented
    hung-worker case fired (concurrent.futures.wait's `not_done` branch):
    a thread stuck past the batch timeout is still running when this is
    called, and will call _mark_ticker_done for real after this "batch
    finished" write, using this batch's counters -- or, if a new batch
    has already started by then, corrupting the following batch's
    in_flight/completed instead (staff-engineer-reviewer finding).
    Genuinely fixing that needs a per-batch generation tag, which is more
    machinery than a cosmetic progress display warrants on top of the
    pre-existing, already-accepted "can't forcibly kill a hung worker
    thread" limitation (TASKS.md M2/M11) -- not solved further here.
    """
    global _progress_completed
    with _progress_lock:
        _progress_completed = _progress_total
        _progress_in_flight.clear()
    _write_progress_file()


# yfinance's rate limit (confirmed live: fetching the full ~1500-ticker
# universe at 12 concurrent workers hit YFRateLimitError on 991/1505
# tickers) is session/IP-wide, not per-ticker -- each worker backing off
# independently doesn't help when all of them are hitting the same
# blocked state. This shared cooldown makes every worker pause together
# once any one of them sees a rate-limit response, instead of each
# burning its own retries hammering an already-limited session.
_rate_limit_lock = threading.Lock()
_rate_limited_until = 0.0


def _wait_out_shared_rate_limit() -> None:
    with _rate_limit_lock:
        until = _rate_limited_until
    remaining = until - time.time()
    if remaining > 0:
        logger.info("Waiting %.1fs for a shared yfinance rate limit to clear.", remaining)
        time.sleep(remaining)


def _register_rate_limit() -> None:
    global _rate_limited_until
    with _rate_limit_lock:
        _rate_limited_until = max(
            _rate_limited_until, time.time() + config.DATA_RATE_LIMIT_COOLDOWN_SECONDS
        )


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


def _coerce_float(info: dict[str, Any], key: str) -> float | None:
    """Safely read a numeric field from yfinance's raw info dict.

    Treats a missing OR non-numeric value as None rather than raising --
    confirmed live at full-universe scale: yfinance occasionally returns
    a non-numeric value (e.g. a string) for a field that's normally a
    float, which crashed the entire screen with an uncaught TypeError the
    first time this ran at scale (only the two _PERCENT_SCALED_FIELDS had
    this guard before; every other field was trusted raw). Every numeric
    Metrics field goes through this now, not just those two -- a
    malformed value degrading to "missing" is the same "no data, no buy"
    conservatism DESIGN.md 3.2 already calls for, applied consistently.
    """
    value: Any = info.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("%s: non-numeric value %r, treating as missing", key, value)
        return None


def _normalize_percent_field(info: dict[str, Any], key: str) -> float | None:
    """Convert one of _PERCENT_SCALED_FIELDS from a raw percentage to a decimal fraction."""
    value = _coerce_float(info, key)
    if value is None:
        return None
    return value / 100.0


def fetch_metrics(symbol: str) -> Metrics | None:
    """Fetch and return one ticker's fundamentals, retrying transient failures.

    Returns None only if every attempt fails outright (network error, or
    the provider not recognizing the symbol at all) -- per-field missing
    data within an otherwise-successful fetch produces a Metrics record
    with those fields set to None, not a None return. Callers (the thread
    pool in fetch_all_metrics) must tolerate a None return for any single
    ticker without aborting the run.

    Wraps _fetch_metrics_inner only to bracket it with the live-progress
    tracking (M13/report.py) -- a try/finally here guarantees the ticker
    is marked done regardless of which of _fetch_metrics_inner's several
    return points fires.
    """
    _mark_ticker_started(symbol)
    try:
        return _fetch_metrics_inner(symbol)
    finally:
        _mark_ticker_done(symbol)


def _fetch_metrics_inner(symbol: str) -> Metrics | None:
    last_error: Exception | None = None
    for attempt in range(config.DATA_FETCH_MAX_RETRIES):
        _wait_out_shared_rate_limit()
        try:
            info, net_income = _fetch_raw(symbol)
            _cache_raw_response(symbol, info, net_income)
            return Metrics(
                symbol=symbol,
                market_cap=_coerce_float(info, "marketCap"),
                trailing_pe=_coerce_float(info, "trailingPE"),
                price_to_book=_coerce_float(info, "priceToBook"),
                current_ratio=_coerce_float(info, "currentRatio"),
                debt_to_equity=_normalize_percent_field(info, "debtToEquity"),
                return_on_equity=_coerce_float(info, "returnOnEquity"),
                gross_margin=_coerce_float(info, "grossMargins"),
                operating_margin=_coerce_float(info, "operatingMargins"),
                free_cash_flow=_coerce_float(info, "freeCashflow"),
                dividend_yield=_normalize_percent_field(info, "dividendYield"),
                consecutive_positive_earnings_years=_consecutive_positive_years(net_income),
            )
        except yfinance.exceptions.YFRateLimitError as e:
            last_error = e
            _register_rate_limit()
            if attempt < config.DATA_FETCH_MAX_RETRIES - 1:
                _wait_out_shared_rate_limit()
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


def fetch_all_metrics(symbols: list[str], phase: str = "screening") -> dict[str, Metrics | None]:
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

    `phase` labels this batch for the HTML report's live progress bar
    (M13) -- e.g. "screening" for the full universe fetch (screener.py)
    vs. "holdings check" for bot.py's smaller post-screen fetch of just
    the currently-held tickers. Purely cosmetic; doesn't affect fetching.
    """
    _start_progress(phase, len(symbols))
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
    _finish_progress()
    return results


# Every Metrics field except symbol is required for the screener (DESIGN.md
# 3.2/3.3) -- a missing one is a failed check, not silently skipped.
# dividend_yield is excluded: yfinance returns None for it when a company
# simply doesn't pay a dividend, which is real, valid data, not a fetch
# gap -- the graham_dividend_record gate (screener.py, gated by
# config.REQUIRE_DIVIDEND_RECORD) already handles a missing/zero dividend
# correctly, so tagging it data_missing here would both duplicate that
# gate under a misleading label and wrongly fail the screen for
# non-dividend payers even when REQUIRE_DIVIDEND_RECORD is off.
_REQUIRED_METRICS_FIELDS = tuple(
    f.name for f in dataclasses.fields(Metrics) if f.name not in ("symbol", "dividend_yield")
)


def validate_metrics(metrics: Metrics) -> list[str]:
    """Sanity-check a fetched Metrics record, returning fail-reason codes.

    An empty list means the record is clean. "No data" and "bad data" are
    distinct failure classes (DESIGN.md 3.2) and must stay distinguishable
    downstream, not collapse into one boolean: a missing field is tagged
    ``data_missing:<field>``, an implausible value is tagged
    ``data_invalid_outlier:<field>`` -- both carried through to
    screen_results.csv (§3.3) so an operator can tell a transient fetch
    gap from a possible data-corruption signal.
    """
    fail_reasons: list[str] = []
    for field_name in _REQUIRED_METRICS_FIELDS:
        if getattr(metrics, field_name) is None:
            fail_reasons.append(f"data_missing:{field_name}")

    if metrics.trailing_pe is not None and abs(metrics.trailing_pe) > config.MAX_PLAUSIBLE_PE:
        fail_reasons.append("data_invalid_outlier:trailing_pe")
    if (
        metrics.debt_to_equity is not None
        and abs(metrics.debt_to_equity) > config.MAX_PLAUSIBLE_DEBT_TO_EQUITY
    ):
        fail_reasons.append("data_invalid_outlier:debt_to_equity")

    return fail_reasons
