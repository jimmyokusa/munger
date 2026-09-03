"""SEC EDGAR XBRL companyfacts client (Design v2.2 §3.4, M36).

Primary fundamentals source per the redesign -- SEC's own tagged data,
straight from filings, free, point-in-time, authoritative. Will replace
yfinance as primary once the shadow-mode comparison (see
`shadow_compare`) has run a full cycle and been reviewed by hand (M37).

M36 builds this module and proves it out against real fixture data
only -- nothing in the running bot calls it yet. No shadow comparison
has executed against a live ticker outside a test, and there is no
accumulated per-field disagreement log to review by hand. Wiring this
alongside data.py for a real full-universe cycle, and doing that
review, is M37's job, not this one's.

Two-step lookup, matching EDGAR's own API shape: a ticker maps to a CIK
(Central Index Key) via `company_tickers.json`, and *that* is the key
every other EDGAR endpoint actually wants.

Rate limiting: SEC's fair-access policy requires a real User-Agent
(config.SEC_EDGAR_USER_AGENT) and asks for no more than 10 requests/
second -- this module self-throttles at config.SEC_EDGAR_MAX_REQUESTS_
PER_SECOND (default 8, a courtesy margin under the ceiling) and requires
the User-Agent header on every request, not just a subset.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
import data

logger = logging.getLogger(__name__)

_TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
_COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Only 10-K annual figures count as "the fiscal year's number" for this
# module's purposes (§3.6's ROIC-persistence/margin-stability/revenue-
# CAGR components all want one clean value per fiscal year, not the
# 10-Q quarterly figures also present in the same response).
_ANNUAL_FORM = "10-K"
_ANNUAL_PERIOD = "FY"

# Shared across every fetch this process makes, not per-call -- the rate
# limit is a property of the requester (this process's IP), not of any
# one ticker's lookup.
_rate_limit_lock = threading.Lock()
_last_request_monotonic = 0.0


def throttled_get(url: str) -> bytes:
    """A GET request with SEC's required User-Agent.

    Also applies this module's self-imposed rate limit, shared across
    every caller in this process -- including material_events.py's 8-K
    submissions polling (M42), which hits the same SEC EDGAR host and
    must share this module's rate limiter, not run a second independent
    one that could double the real request rate against SEC's servers.
    Public (not `throttled_get`) for exactly that cross-module reuse.
    """
    global _last_request_monotonic
    min_interval = 1.0 / config.SEC_EDGAR_MAX_REQUESTS_PER_SECOND
    with _rate_limit_lock:
        wait = min_interval - (time.monotonic() - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
        _last_request_monotonic = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": config.SEC_EDGAR_USER_AGENT})
    with urllib.request.urlopen(request, timeout=config.SEC_EDGAR_REQUEST_TIMEOUT_SECONDS) as resp:
        return bytes(resp.read())


def _cik_cache_path() -> Path:
    return config.DATA_RAW_CACHE_DIR / "sec_company_tickers.json"


def load_cik_lookup(*, force_refresh: bool = False) -> dict[str, str]:
    """Ticker -> zero-padded 10-digit CIK string, for every SEC filer.

    ~10,000 tickers as of 2026. Cached to disk (config.DATA_RAW_CACHE_DIR) -- this index changes
    rarely and is ~800KB, not worth re-fetching every run. `force_refresh`
    bypasses the cache (a genuinely new listing wouldn't appear in a
    stale copy).

    Fails soft (returns {}, logs) on a fetch/parse failure -- staff-
    engineer-reviewer finding: this was previously the one uncaught
    EDGAR call in the whole module (every other fetch here already fails
    soft, matching data.py's fetch_metrics convention). It was dead code
    until M42's material_events.py became this function's first real
    caller, and on GitHub Actions' ephemeral runners
    (config.DATA_RAW_CACHE_DIR is not restored by either trading
    workflow's bot-state persistence) the disk cache is cold on every
    single scheduled run -- so an uncaught exception here would have hit
    the real fetch path daily, not as a rare edge case, and crashed the
    whole job on any transient SEC hiccup despite every doc comment in
    this codebase claiming that job can't fail on an EDGAR problem. An
    empty return degrades every caller correctly: get_cik(ticker, {})
    returns None for every ticker, and poll_holdings already treats a
    None CIK as "skip this ticker, log, keep going" per-ticker, not a
    reason to abort the whole poll.
    """
    cache_path = _cik_cache_path()
    if not force_refresh and cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text())
            return {str(v["ticker"]).upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("%s unreadable/corrupt -- refetching", cache_path, exc_info=True)

    try:
        body = throttled_get(_TICKER_INDEX_URL)
        raw = json.loads(body)
        lookup = {str(v["ticker"]).upper(): str(v["cik_str"]).zfill(10) for v in raw.values()}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError):
        logger.error("Failed to fetch/parse the SEC ticker index -- returning empty", exc_info=True)
        return {}

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp_path.write_bytes(body)
        tmp_path.replace(cache_path)
    except OSError:
        # The fetch itself succeeded -- a cache-write failure (a
        # read-only filesystem, disk full) must not discard a perfectly
        # good in-memory result just because it couldn't also be cached.
        logger.warning("Failed to write %s cache -- continuing uncached", cache_path, exc_info=True)
    return lookup


def get_cik(ticker: str, cik_lookup: dict[str, str] | None = None) -> str | None:
    """The zero-padded CIK for `ticker`, or None if EDGAR doesn't track it.

    Accepts a pre-loaded `cik_lookup` (from load_cik_lookup) so a caller
    fetching many tickers doesn't reload/reparse the ~800KB index once
    per ticker.
    """
    lookup = cik_lookup if cik_lookup is not None else load_cik_lookup()
    return lookup.get(ticker.upper())


def fetch_company_facts(cik: str) -> dict[str, object] | None:
    """Raw companyfacts JSON for one CIK, or None on any fetch failure.

    Fails soft (returns None, logs), matching data.py's fetch_metrics
    convention for this same class of problem: a single ticker's fetch
    failing must never abort the whole screen. A 404 specifically means
    EDGAR has no XBRL data for this filer (a foreign private issuer on
    20-F, a very new listing that hasn't filed yet) -- also None, same
    as any other failure, since the caller's response (fall back to
    yfinance, or flag as unresolved) is identical either way.
    """
    try:
        body = throttled_get(_COMPANYFACTS_URL_TEMPLATE.format(cik=cik))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info("CIK %s: no XBRL companyfacts on EDGAR (404)", cik)
        else:
            logger.error("CIK %s: companyfacts fetch failed (HTTP %d)", cik, e.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.error("CIK %s: companyfacts fetch failed", cik, exc_info=True)
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        logger.error("CIK %s: companyfacts response was not valid JSON", cik)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


@dataclasses.dataclass(frozen=True)
class AnnualValue:
    """One fiscal year's value for one XBRL concept."""

    fiscal_year_end: str  # ISO date, e.g. "2024-09-28"
    value: float
    filed: str  # ISO date this figure was actually filed, for dedup


def annual_values(
    facts: dict[str, object], concept: str, taxonomy: str = "us-gaap"
) -> list[AnnualValue]:
    """Every distinct fiscal year's 10-K annual value for `concept`.

    Sorted oldest to newest, one entry per fiscal-year-end. EDGAR's
    companyfacts response repeats the same fiscal year's figure
    across multiple later filings (a 10-K's own prior-year comparative
    column re-states it) -- deduped here by keeping only the most
    recently *filed* value for each distinct `end` date, which is also
    the most likely to reflect any subsequent restatement.
    """
    facts_section = facts.get("facts")
    if not isinstance(facts_section, dict):
        return []
    taxonomy_section = facts_section.get(taxonomy)
    if not isinstance(taxonomy_section, dict):
        return []
    concept_section = taxonomy_section.get(concept)
    if not isinstance(concept_section, dict):
        return []
    units = concept_section.get("units")
    if not isinstance(units, dict):
        return []
    entries = units.get("USD")
    if not isinstance(entries, list):
        return []

    best_by_end: dict[str, AnnualValue] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("form") != _ANNUAL_FORM or entry.get("fp") != _ANNUAL_PERIOD:
            continue
        end = entry.get("end")
        start = entry.get("start")
        val = entry.get("val")
        filed = entry.get("filed")
        if not isinstance(end, str) or not isinstance(filed, str):
            continue
        if not isinstance(val, (int, float)):
            continue
        # Real-data bug caught against the actual AAPL fixture, not
        # theoretical: `fp == "FY"` alone does NOT mean the period is a
        # full fiscal year -- a 10-K commonly embeds quarterly
        # sub-disclosures (a quarterly revenue breakdown table) that are
        # still tagged fp="FY" because they were *disclosed within* the
        # annual filing, not because the underlying start/end span is
        # annual. Without this check, AAPL's own "Revenues" concept
        # extracted several quarterly figures as if they were fiscal-
        # year totals. Require the span to actually be ~1 year
        # (350-380 days, a tolerance band for the +/-1 week fiscal-year
        # boundary drift real 52/53-week fiscal calendars produce).
        if isinstance(start, str):
            try:
                span_days = (
                    datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)
                ).days
            except ValueError:
                continue
            if not (350 <= span_days <= 380):
                continue
        existing = best_by_end.get(end)
        if existing is None or filed > existing.filed:
            best_by_end[end] = AnnualValue(fiscal_year_end=end, value=float(val), filed=filed)

    return sorted(best_by_end.values(), key=lambda v: v.fiscal_year_end)


def latest_annual_value(facts: dict[str, object], concept: str) -> float | None:
    """The most recent fiscal year's value for `concept`, or None."""
    values = annual_values(facts, concept)
    return values[-1].value if values else None


# Real-data finding, not theoretical: Apple's own companyfacts stops
# tagging "Revenues" after fiscal 2018 (confirmed against the real
# fixture) -- filers switched to this ASC-606 concept starting ~2018.
# Both are tried, in this order, matching whichever a given filer
# actually still uses for its most recent fiscal year.
_REVENUE_CONCEPTS = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax")


def gross_margin_from_xbrl(facts: dict[str, object]) -> float | None:
    """GrossProfit / Revenue for the most recent fiscal year BOTH concepts have a value for.

    Returns None if there's no such year (a sector without a meaningful
    gross margin -- financials, insurers -- or a filer that doesn't tag
    GrossProfit at all).

    Matched by fiscal year, not "most recent value per concept"
    independently -- a real bug caught against the AAPL fixture:
    GrossProfit's most recent annual value and "Revenues"' most recent
    annual value can be for entirely different fiscal years once a
    filer stops tagging one of the two concepts, silently producing a
    nonsensical ratio (a real FY2025 GrossProfit divided by a stale
    FY2018 Revenues came out to ~73%, not AAPL's real ~46% margin).
    """
    gross_profit_by_year = {v.fiscal_year_end: v.value for v in annual_values(facts, "GrossProfit")}
    if not gross_profit_by_year:
        return None

    revenue_by_year: dict[str, float] = {}
    for concept in _REVENUE_CONCEPTS:
        for v in annual_values(facts, concept):
            revenue_by_year.setdefault(v.fiscal_year_end, v.value)

    common_years = sorted(set(gross_profit_by_year) & set(revenue_by_year))
    if not common_years:
        return None
    latest_common_year = common_years[-1]
    revenue = revenue_by_year[latest_common_year]
    if revenue == 0:
        return None
    return gross_profit_by_year[latest_common_year] / revenue


@dataclasses.dataclass(frozen=True)
class FieldDisagreement:
    """One field where XBRL and yfinance disagree.

    The XBRL-derived value and yfinance's own value differ by more than
    the configured tolerance (§3.4, M36).
    """

    ticker: str
    field: str
    xbrl_value: float
    yfinance_value: float

    @property
    def absolute_diff(self) -> float:
        """The unsigned difference between the two values."""
        return abs(self.xbrl_value - self.yfinance_value)

    @property
    def relative_diff(self) -> float | None:
        """The unsigned difference as a fraction of the yfinance value, or None if that's zero."""
        if self.yfinance_value == 0:
            return None
        return self.absolute_diff / abs(self.yfinance_value)


def _disagrees(xbrl_value: float, yfinance_value: float) -> bool:
    """Apply §3.4's tolerance rule.

    Disagrees if the difference exceeds the LARGER of the relative or
    the absolute-percentage-point bound -- the absolute bound is what
    actually catches a real problem on a ratio field already near zero,
    where a purely relative bar is too loose (config.py's own comment on
    this, carried over verbatim from the design doc).
    """
    absolute_diff = abs(xbrl_value - yfinance_value)
    relative_bound = config.XBRL_DISAGREEMENT_RELATIVE_TOLERANCE * abs(yfinance_value)
    bound = max(relative_bound, config.XBRL_DISAGREEMENT_ABSOLUTE_TOLERANCE_PP)
    return absolute_diff > bound


def shadow_compare(
    ticker: str, facts: dict[str, object], yf_metrics: data.Metrics
) -> list[FieldDisagreement]:
    """Compare every field this module can derive from XBRL against yfinance's own value.

    Per §3.4's shadow mode -- "both sources run in parallel...
    disagreements are logged per field."

    Deliberately narrow right now: gross_margin is the one field this
    milestone computes from XBRL (the concepts needed for the others --
    operating margin, ROIC, revenue/share -- are §3.6/§3.9's job, not
    M36's). Extend this function as those land; the tolerance/logging
    mechanism itself is already the real, reusable piece.
    """
    disagreements = []
    xbrl_gross_margin = gross_margin_from_xbrl(facts)
    if (
        xbrl_gross_margin is not None
        and yf_metrics.gross_margin is not None
        and _disagrees(xbrl_gross_margin, yf_metrics.gross_margin)
    ):
        disagreement = FieldDisagreement(
            ticker=ticker,
            field="gross_margin",
            xbrl_value=xbrl_gross_margin,
            yfinance_value=yf_metrics.gross_margin,
        )
        logger.warning(
            "%s: gross_margin disagreement -- XBRL %.4f vs. yfinance %.4f (absolute diff %.4f)",
            ticker,
            xbrl_gross_margin,
            yf_metrics.gross_margin,
            disagreement.absolute_diff,
        )
        disagreements.append(disagreement)
    return disagreements
