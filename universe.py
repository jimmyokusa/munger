"""S&P Composite 1500 universe module (DESIGN.md section 3.1).

Provides the list of candidate tickers for the screener. Sourcing is
hybrid: the S&P 500 comes from Financial Modeling Prep's REST API, while
the S&P 400 and S&P 600 come from a Wikipedia scrape -- no vendor offers a
constituents API for the 400/600 at any price point checked. Each of the
three indices is fetched, validated, and falls back to its own slice of
the static file independently if the fetch either raises or returns a
result that fails a sanity check.
"""

from __future__ import annotations

import io
import json
import logging
import re
import urllib.request

import pandas as pd

import config

logger = logging.getLogger(__name__)

_WIKIPEDIA_URLS = {
    "400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}
_FMP_SP500_URL = "https://financialmodelingprep.com/api/v3/sp500_constituent"
_TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")
# Wikipedia's bot policy rejects the default urllib/pandas User-Agent with a
# 403; a descriptive UA identifying the bot is both required to get past it
# and the compliant way to do so (vs. spoofing a browser).
_REQUEST_HEADERS = {"User-Agent": "graham-munger-bot/0.1 (personal project; contact via GitHub)"}

# Best-effort mapping from vendor-specific sector labels to a canonical GICS
# name, so EXCLUDED_SECTORS filtering doesn't silently depend on which
# vendor happened to source a given ticker (e.g. FMP's "Financial Services"
# vs. Wikipedia's "Financials"). Wikipedia's GICS Sector column already uses
# canonical names; this exists mainly to normalize FMP's. NOT YET VERIFIED
# against live FMP data (needs an API key) -- see TASKS.md M1.
_SECTOR_CANONICAL_MAP: dict[str, str] = {
    "financial services": "Financials",
    "healthcare": "Health Care",
    "technology": "Information Technology",
    "consumer cyclical": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "basic materials": "Materials",
}


def normalize_ticker(ticker: str) -> str:
    """Normalize a ticker to the broker's format, e.g. ``BRK.B`` -> ``BRK-B``."""
    return ticker.strip().upper().replace(".", "-")


def _canonicalize_sector(sector: object) -> str:
    """Map a vendor-specific sector label to a canonical GICS name.

    Accepts ``object``, not just ``str``: a blank/NaN sector cell in a live
    fetch (a stub row, a mid-edit Wikipedia page) arrives as a float, and
    letting that raise here would discard an entire index's live fetch --
    hundreds of otherwise-good rows -- over one bad cell.
    """
    if not isinstance(sector, str):
        return str(sector)
    return _SECTOR_CANONICAL_MAP.get(sector.strip().lower(), sector.strip())


def _is_plausible_ticker(ticker: str) -> bool:
    return bool(_TICKER_PATTERN.match(ticker))


def validate_universe(tickers: list[str], index: str) -> bool:
    """Sanity-check one index's fetched ticker list before accepting it.

    Catches the case where a fetch succeeds but returns a corrupted result
    (e.g. a Wikipedia page restructure shifting which column holds the
    ticker symbol, or a malformed FMP response) -- an exception handler
    alone would miss this, since the fetch itself didn't raise.
    """
    min_count, max_count = config.UNIVERSE_TICKER_COUNT_BANDS[index]
    if not (min_count <= len(tickers) <= max_count):
        return False
    return all(_is_plausible_ticker(t) for t in tickers)


def _fetch_wikipedia_index(index: str) -> pd.DataFrame:
    request = urllib.request.Request(_WIKIPEDIA_URLS[index], headers=_REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=15) as response:
        html = response.read()
    table = pd.read_html(io.StringIO(html.decode("utf-8")))[0]
    return table.rename(columns={"Symbol": "symbol", "GICS Sector": "sector"})


def _fetch_fmp_sp500() -> pd.DataFrame:
    url = f"{_FMP_SP500_URL}?apikey={config.FMP_API_KEY}"
    request = urllib.request.Request(url, headers=_REQUEST_HEADERS)
    # urlopen raises urllib.error.HTTPError for any non-2xx status by
    # default (confirmed live: FMP returns a plain 401 for an invalid/
    # missing key) -- this is what satisfies DESIGN.md 3.1's requirement to
    # treat 401/403/429 as failures. This is an implicit property of
    # urllib, not an explicit status check: a future switch to a client
    # that doesn't raise-by-default (e.g. requests, without
    # .raise_for_status()) would silently drop this guarantee.
    with urllib.request.urlopen(request, timeout=15) as response:
        body = response.read()
    data = json.loads(body)
    # Second line of defense for the case a metered API can still return
    # HTTP 200 with an error body under other conditions (see DESIGN.md
    # 3.1): a dict, rather than the expected list of per-ticker records, is
    # that shape.
    if isinstance(data, dict):
        raise ValueError(f"FMP sp500_constituent returned an error body: {data}")
    return pd.DataFrame(data)


def _apply_sector_exclusions(table: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose canonicalized sector is in config.EXCLUDED_SECTORS.

    Munger's circle-of-competence rule made concrete: if you don't
    understand banks, exclude Financials.
    """
    if not config.EXCLUDED_SECTORS:
        return table
    canonical_sector = table["sector"].apply(_canonicalize_sector)
    return table[~canonical_sector.isin(config.EXCLUDED_SECTORS)]


def _load_static_fallback(index: str) -> list[str]:
    """Load one index's slice of the static fallback file shipped in the repo.

    Reuses _apply_sector_exclusions rather than re-implementing the
    exclusion predicate inline, so the live-fetch path and the fallback
    path can't silently drift apart on Munger's circle-of-competence rule.
    """
    fallback_path = config.BASE_DIR / config.STATIC_UNIVERSE_FALLBACK_PATH
    table = pd.read_csv(fallback_path, dtype={"source_index": str})
    table = table[table["source_index"] == index]
    table = _apply_sector_exclusions(table)
    return [normalize_ticker(t) for t in table["symbol"]]


def _fetch_and_validate_index(index: str) -> list[str] | None:
    """Fetch, validate, exclude, and normalize one index's live ticker list.

    Returns None (rather than raising) on either a fetch exception or a
    validation failure, signaling the caller to fall back -- a
    page/response restructure can produce a well-formed-but-garbage result
    that only the sanity check in validate_universe catches, not an
    exception handler alone. The whole live-fetch pipeline (fetch,
    validate, exclude, normalize) shares one failure boundary, since a
    missing/renamed sector column can raise downstream of the fetch call
    just as easily as the fetch itself failing. A failure on the S&P 500
    (FMP) is logged at a higher priority than the 400/600 (Wikipedia),
    since it's the largest, most consequential slice (DESIGN.md 3.1/3.6).
    """
    priority = logging.ERROR if index == "500" else logging.WARNING
    try:
        table = _fetch_fmp_sp500() if index == "500" else _fetch_wikipedia_index(index)
        raw_tickers = [normalize_ticker(t) for t in table["symbol"]]
        if not validate_universe(raw_tickers, index):
            logger.log(
                priority,
                "S&P %s fetch returned %d tickers, which failed the sanity "
                "check; using static fallback instead.",
                index,
                len(raw_tickers),
            )
            return None
        table = _apply_sector_exclusions(table)
        return [normalize_ticker(t) for t in table["symbol"]]
    except Exception:
        logger.log(priority, "S&P %s fetch failed; using static fallback.", index, exc_info=True)
        return None


def _fetch_index(index: str) -> list[str]:
    """Return one index's ticker list, live if possible, else its fallback slice.

    The fallback load deliberately sits outside the live fetch's exception
    boundary above: if the fallback file itself is broken (missing,
    malformed), that's a distinct, more severe failure than "the live
    source is down" and should propagate as its own error rather than be
    silently retried or misattributed to the live fetch having failed
    twice.
    """
    tickers = _fetch_and_validate_index(index)
    if tickers is None:
        return _load_static_fallback(index)
    return tickers


def get_universe() -> list[str]:
    """Return the combined S&P Composite 1500 candidate ticker list.

    Each of the S&P 500 (via FMP), S&P 400, and S&P 600 (via Wikipedia) is
    fetched and falls back independently -- a bad SmallCap 600 fetch
    doesn't discard two otherwise-healthy results (DESIGN.md 3.1). The
    combined result is de-duplicated by ticker with 500 -> 400 -> 600
    precedence, since index-reclassification lag can briefly put the same
    ticker in two source lists at once (confirmed live: BTSG currently
    appears in both the S&P 400 and S&P 600 Wikipedia pages).
    """
    seen: set[str] = set()
    combined: list[str] = []
    for index in ("500", "400", "600"):
        for ticker in _fetch_index(index):
            if ticker not in seen:
                seen.add(ticker)
                combined.append(ticker)
    return combined
