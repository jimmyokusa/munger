"""Held-symbol daily close price module (M17 Phase 2, DESIGN_DASHBOARDS.md §3).

Feeds Graph 2 ("daily close price per owned ticker") of the embedded Grafana
dashboards. Read-only against the broker, like pnl.py -- never places or
modifies an order. Deliberately its own module for the same reason pnl.py
is: this is a display/reporting concern, not a trading decision.

Runs where ALPACA_API_KEY lives (daily-trade.yml, GitHub Actions), after
pnl.py in the same job -- it reads pnl.py's freshly-written pnl.json from
local disk for the held-symbol list rather than re-fetching positions
itself, so the two modules can never disagree about which symbols are
"currently held" on a given run. Not in report.py's own deployment
(Cloud Run/k3s), which deliberately never has Alpaca credentials
(DESIGN.md 3.5, M14's screen-only boundary).

Unlike pnl.py's pnl_history.jsonl, prices.json is NOT an append-series:
Alpaca's historical bars endpoint has no rolling-window loss the way
portfolio history does, so each run simply re-fetches config.
PRICES_LOOKBACK_DAYS of bars fresh for whichever symbols are held *today*.
"Currently held" is therefore a moving window by design -- a sold
ticker's series drops off the very next run, a new buy's appears
(DESIGN_DASHBOARDS.md §3).
"""

from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path

from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config

logger = logging.getLogger(__name__)


def held_symbols(pnl_snapshot: dict[str, object]) -> list[str]:
    """Currently-held ticker symbols from a pnl.py-shaped snapshot's positions[].

    Sorted and de-duplicated for a deterministic fetch order. A malformed
    position entry (missing/non-string symbol) is skipped rather than
    raising -- the same "don't let one bad record sink the whole snapshot"
    posture pnl.py's own _position_snapshot already assumes of its input.
    """
    positions = pnl_snapshot.get("positions") or []
    assert isinstance(positions, list)
    symbols = {
        p["symbol"]
        for p in positions
        if isinstance(p, dict) and isinstance(p.get("symbol"), str) and p["symbol"]
    }
    return sorted(symbols)


def _validate_close(symbol: str, close: float) -> list[str]:
    """Sanity-check one bar's close price, mirroring data.validate_metrics.

    Returns fail-reason codes so a bad point is a distinct, logged case,
    never a silent bad value dropped onto a public chart
    (DESIGN_DASHBOARDS.md §3 / staff-engineer-reviewer finding 9.B-3 --
    the same "no data" vs "bad data" split data.validate_metrics already
    draws for screener metrics).

    math.isfinite guards NaN/inf explicitly -- staff-engineer-reviewer
    finding: NaN compares False to every "<=`/">" comparison below, so
    without this a NaN close would have passed validation, landed in
    points, and serialized as the literal (non-standard-JSON) token `NaN`
    via json.dumps' default allow_nan=True -- breaking a strict JSON
    parser (plausibly Grafana's Infinity datasource) on the *entire* file,
    not just that one point.
    """
    if not math.isfinite(close) or close <= 0 or close > config.PRICES_MAX_PLAUSIBLE_CLOSE:
        return [f"data_invalid_outlier:{symbol}:close"]
    return []


def fetch_daily_closes(symbols: list[str]) -> dict[str, object]:
    """Fetch daily close bars from Alpaca for the given (held) symbols.

    Explicitly requests config.PRICES_DATA_FEED (IEX) rather than the
    client's default -- confirmed live that this account's data
    subscription 403s on the default SIP feed for historical bars.

    A symbol with no bars at all is recorded as a distinct "no_bars"
    status, never conflated with an empty-after-validation result, so a
    consumer never renders a missing ticker as if it just happened to have
    zero valid points.

    Deliberately fails closed for the whole request, same posture as
    pnl.py's account-level reads (no try/except): a single get_stock_bars()
    call covers every held symbol, so one symbol triggering an unexpected
    response (a schema surprise, a rate limit) fails the entire run rather
    than degrading per-symbol -- staff-engineer-reviewer finding, accepted
    as this codebase's established tradeoff (see pnl.py's own docstring)
    rather than adding N per-symbol calls/try-excepts for a small,
    read-only reporting feed. Bar.close is a required (non-Optional)
    Pydantic field, so a genuinely missing close would raise inside the
    SDK's own response parsing before reaching _validate_close at all --
    the same fail-closed path, not a gap in that function.
    """
    generated_at = datetime.datetime.now(datetime.UTC).isoformat()
    if not symbols:
        return {"generated_at": generated_at, "symbols": {}}

    client = StockHistoricalDataClient(
        api_key=config.ALPACA_API_KEY, secret_key=config.ALPACA_SECRET_KEY
    )
    end = datetime.datetime.now(datetime.UTC)
    start = end - datetime.timedelta(days=config.PRICES_LOOKBACK_DAYS)
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed(config.PRICES_DATA_FEED),
    )
    bar_set = client.get_stock_bars(request)
    if not isinstance(bar_set, BarSet):
        # Same fail-closed posture as pnl.py's own get_account()/
        # get_portfolio_history() checks: a schema drift or malformed
        # response must abort this run's fetch, not silently produce an
        # empty-looking (but wrong) prices.json.
        raise ValueError(f"unexpected get_stock_bars() response: {bar_set!r}")

    result: dict[str, object] = {}
    for symbol in symbols:
        bars = bar_set.data.get(symbol) or []
        if not bars:
            result[symbol] = {"status": "no_bars", "points": [], "fail_reasons": []}
            continue
        points: list[dict[str, object]] = []
        fail_reasons: list[str] = []
        # Trusts Alpaca's bars as chronologically ordered, same posture
        # pnl.py's _history_snapshot takes toward Alpaca's parallel arrays
        # -- this is the provider's own primary sort key for the endpoint,
        # not an incidental ordering.
        for bar in bars:
            close = float(bar.close)
            reasons = _validate_close(symbol, close)
            if reasons:
                fail_reasons.extend(reasons)
                continue
            points.append({"date": bar.timestamp.date().isoformat(), "close": close})
        status = "ok" if points else "no_valid_bars"
        result[symbol] = {"status": status, "points": points, "fail_reasons": fail_reasons}
    return {"generated_at": generated_at, "symbols": result}


def generate_snapshot(pnl_snapshot: dict[str, object]) -> dict[str, object]:
    """Fetch daily closes for the symbols held in a pnl.py-shaped snapshot."""
    return fetch_daily_closes(held_symbols(pnl_snapshot))


def write_snapshot(snapshot: dict[str, object], path: Path | None = None) -> None:
    """Write a snapshot atomically (temp file + rename), matching pnl.py."""
    target = path if path is not None else config.PRICES_DATA_PATH
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2))
    tmp_path.replace(target)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Reads pnl.json rather than fetching positions itself: prices.py always
    # runs immediately after pnl.py in the same daily-trade.yml job, and
    # this way the two can never disagree about which symbols are "held"
    # on a given run. A missing pnl.json means pnl.py didn't produce one
    # this run (an already-alerted failure upstream) -- fail loudly rather
    # than silently skip and leave a stale prices.json in place.
    if not config.PNL_DATA_PATH.exists():
        raise SystemExit(
            f"{config.PNL_DATA_PATH} not found -- prices.py must run after "
            "pnl.py in the same job (it reads pnl.json for the held-symbol "
            "list)."
        )
    pnl_snapshot = json.loads(config.PNL_DATA_PATH.read_text())
    snapshot = generate_snapshot(pnl_snapshot)
    write_snapshot(snapshot)
    logger.info(
        "Price snapshot written to %s (%d symbol(s))",
        config.PRICES_DATA_PATH,
        len(snapshot["symbols"]),  # type: ignore[arg-type]
    )
