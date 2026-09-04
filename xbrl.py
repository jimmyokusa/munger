"""SEC EDGAR XBRL companyfacts client (Design v2.2 §3.4, M36; primary as of M37).

Primary fundamentals source per the redesign -- SEC's own tagged data,
straight from filings, free, point-in-time, authoritative. yfinance is
now the fallback for gross_margin/operating_margin (see
`apply_primary_metrics`), used only when XBRL has no CIK match, no
companyfacts, or no plausible value for a ticker.

M36 built this module and proved it out against fixture data only, then
against a real full-universe shadow-mode cycle (`shadow_compare`,
xbrl_shadow.py) -- 1506 tickers, 201 disagreements, reviewed by hand
before M37's switchover. M37 is that switchover: `apply_primary_metrics`
is now called from screener.py's shared fetch path (`run_screen`) and
from evaluate.py's/bot.py's own holdings-check paths, so every gate/
score computed anywhere in the system uses XBRL first. `shadow_compare`
itself stays independent of this override (see its own docstring) --
xbrl_shadow.py can still detect a future disagreement even though
production's own Metrics no longer carry the raw yfinance value for an
overridden field.

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


@dataclasses.dataclass(frozen=True)
class CompanyFactsResult:
    """The outcome of one companyfacts fetch, with the 404 case distinguished (M37 prereq).

    `fetch_company_facts` (below) collapses every failure mode to a
    bare `None` -- correct for its own callers, where a 404 (EDGAR
    genuinely has no XBRL data for this filer) and a transient fetch
    failure (network/timeout/JSON error, worth a retry) call for the
    identical response. `xbrl_shadow.py`'s coverage report needs the
    two told apart: a hand reviewer judging how complete a shadow cycle
    was can't tell "SEC doesn't track this filer" from "SEC failed to
    answer this run" if both show up as one undifferentiated gap
    (staff-engineer-reviewer finding, `xbrl_shadow.py`'s own review).
    """

    facts: dict[str, object] | None
    not_found: bool  # True only for a confirmed 404; False for every other case


def fetch_company_facts_detailed(cik: str) -> CompanyFactsResult:
    """Raw companyfacts JSON for one CIK, with the 404-vs-other-failure distinction kept.

    Fails soft (never raises) on every path -- matching data.py's
    fetch_metrics convention for this same class of problem: a single
    ticker's fetch failing must never abort the whole screen.
    """
    try:
        body = throttled_get(_COMPANYFACTS_URL_TEMPLATE.format(cik=cik))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            logger.info("CIK %s: no XBRL companyfacts on EDGAR (404)", cik)
            return CompanyFactsResult(facts=None, not_found=True)
        logger.error("CIK %s: companyfacts fetch failed (HTTP %d)", cik, e.code)
        return CompanyFactsResult(facts=None, not_found=False)
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.error("CIK %s: companyfacts fetch failed", cik, exc_info=True)
        return CompanyFactsResult(facts=None, not_found=False)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        logger.error("CIK %s: companyfacts response was not valid JSON", cik)
        return CompanyFactsResult(facts=None, not_found=False)
    if not isinstance(parsed, dict):
        return CompanyFactsResult(facts=None, not_found=False)
    return CompanyFactsResult(facts=parsed, not_found=False)


def fetch_company_facts(cik: str) -> dict[str, object] | None:
    """Raw companyfacts JSON for one CIK, or None on any fetch failure.

    Thin wrapper over `fetch_company_facts_detailed` for every caller
    that doesn't need the 404-vs-other-failure distinction -- same
    contract, same behavior, as before that function existed. A 404
    specifically means EDGAR has no XBRL data for this filer (a foreign
    private issuer on 20-F, a very new listing that hasn't filed yet) --
    also None here, same as any other failure, since this contract's
    caller's response (fall back to yfinance, or flag as unresolved) is
    identical either way.
    """
    return fetch_company_facts_detailed(cik).facts


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


def _margin_from_xbrl(
    facts: dict[str, object], numerator_concept: str, denominator_concepts: tuple[str, ...]
) -> float | None:
    """Numerator / denominator for the most recent fiscal year both concepts plausibly agree on.

    Shared by gross_margin_from_xbrl and operating_margin_from_xbrl.
    Returns None if there's no such year (a sector where the ratio isn't
    meaningful -- financials, insurers -- or a filer that doesn't tag
    the numerator concept at all).

    Matched by fiscal year, not "most recent value per concept"
    independently -- a real bug caught against the AAPL fixture:
    GrossProfit's most recent annual value and "Revenues"' most recent
    annual value can be for entirely different fiscal years once a
    filer stops tagging one of the two concepts, silently producing a
    nonsensical ratio (a real FY2025 GrossProfit divided by a stale
    FY2018 Revenues came out to ~73%, not AAPL's real ~46% margin).

    Matching by fiscal-year-end date alone is NOT sufficient, though --
    a second real bug, caught live against real EDGAR data (not a
    fixture) for LHX/CCK/ENS: the *same* nominal fiscal-year-end can
    carry more than one dollar-scope for the *same* concept across
    different filings over time (a later filing's comparative/restated
    figure, e.g. after a divestiture reduces "continuing operations"
    revenue for a prior year that was originally filed at full
    consolidated scale). `annual_values`' own dedup already prefers the
    most-recently-filed value per concept-year (deliberately, to reflect
    the latest restatement) -- but that means a numerator concept a
    filer stopped tagging early (still its original, large, as-filed
    value) can end up paired against a denominator concept's much later,
    much smaller restated value for the nominally "same" year, producing
    a ratio like LHX's real, reproduced 1828% "gross margin." Guarded
    here by walking common years newest-to-oldest and skipping any whose
    computed ratio falls outside `config.MARGIN_PLAUSIBLE_MIN`/`_MAX` --
    cheap, general, and doesn't require modeling SEC's restatement
    semantics precisely to catch the actual observable symptom. A
    rejected newest-year and a fallback to an older one are both logged
    (staff-engineer-reviewer finding, M37 review round: the original
    version of this fix silently skipped an implausible year exactly
    the way it silently skipped a zero denominator, leaving an operator
    with no way to tell "this filer just has no plausible year" from
    "the newest year was actually rejected" without re-deriving it by
    hand).
    """
    numerator_by_year = {
        v.fiscal_year_end: v.value for v in annual_values(facts, numerator_concept)
    }
    if not numerator_by_year:
        return None

    denominator_by_year: dict[str, float] = {}
    for concept in denominator_concepts:
        for v in annual_values(facts, concept):
            denominator_by_year.setdefault(v.fiscal_year_end, v.value)

    common_years = sorted(set(numerator_by_year) & set(denominator_by_year), reverse=True)
    entity = facts.get("entityName") or facts.get("cik") or "?"
    rejected_years: list[str] = []
    for year in common_years:
        denominator = denominator_by_year[year]
        # Strictly positive, not merely nonzero (staff-engineer-reviewer
        # finding): a negative denominator paired with a negative
        # numerator produces a positive, "plausible"-looking ratio that
        # is nevertheless economically meaningless -- exactly the kind
        # of restatement/scope artifact that caused the LHX bug in the
        # first place, just landing inside the bound instead of outside
        # it.
        if denominator <= 0:
            rejected_years.append(year)
            continue
        ratio = numerator_by_year[year] / denominator
        if config.MARGIN_PLAUSIBLE_MIN <= ratio <= config.MARGIN_PLAUSIBLE_MAX:
            if rejected_years:
                logger.warning(
                    "%s: %s/%s -- rejected implausible/non-positive-denominator year(s) %s, "
                    "using %s instead (ratio %.4f)",
                    entity,
                    numerator_concept,
                    "|".join(denominator_concepts),
                    rejected_years,
                    year,
                    ratio,
                )
            return ratio
        rejected_years.append(year)
    if rejected_years:
        logger.warning(
            "%s: %s/%s -- every common year (%s) was implausible or had a "
            "non-positive denominator; no margin computed",
            entity,
            numerator_concept,
            "|".join(denominator_concepts),
            rejected_years,
        )
    return None


def gross_margin_from_xbrl(facts: dict[str, object]) -> float | None:
    """GrossProfit / Revenue for the most recent fiscal year both concepts plausibly agree on.

    See `_margin_from_xbrl`'s own docstring for the matching/plausibility
    rules this applies.
    """
    return _margin_from_xbrl(facts, "GrossProfit", _REVENUE_CONCEPTS)


def operating_margin_from_xbrl(facts: dict[str, object]) -> float | None:
    """OperatingIncomeLoss / Revenue for the most recent fiscal year both concepts agree on.

    M37 (Design v2.2 §3.4): resolves the GNTX operating-margin
    discrepancy the design doc names directly (yfinance 21.8% vs. the
    filed 18.7%) -- confirmed against real GNTX companyfacts data:
    FY2025 OperatingIncomeLoss / RevenueFromContractWithCustomerExcludingAssessedTax
    computes to 18.70%, matching the design doc's cited filed figure.
    See `_margin_from_xbrl`'s own docstring for the matching/
    plausibility rules this applies.
    """
    return _margin_from_xbrl(facts, "OperatingIncomeLoss", _REVENUE_CONCEPTS)


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


def _check_field_disagreement(
    ticker: str, field: str, xbrl_value: float | None, yfinance_value: float | None
) -> FieldDisagreement | None:
    """One field's disagreement check, or None if either value is missing or they agree."""
    if xbrl_value is None or yfinance_value is None:
        return None
    if not _disagrees(xbrl_value, yfinance_value):
        return None
    disagreement = FieldDisagreement(
        ticker=ticker, field=field, xbrl_value=xbrl_value, yfinance_value=yfinance_value
    )
    logger.warning(
        "%s: %s disagreement -- XBRL %.4f vs. yfinance %.4f (absolute diff %.4f)",
        ticker,
        field,
        xbrl_value,
        yfinance_value,
        disagreement.absolute_diff,
    )
    return disagreement


def shadow_compare(
    ticker: str, facts: dict[str, object], yf_metrics: data.Metrics
) -> list[FieldDisagreement]:
    """Compare every field this module can derive from XBRL against yfinance's own value.

    Per §3.4's shadow mode -- "both sources run in parallel...
    disagreements are logged per field."

    gross_margin and operating_margin are the two fields this milestone
    computes from XBRL (the concepts needed for the others -- ROIC,
    revenue/share -- are §3.6/§3.9's job, for later milestones). Extend
    this function as those land; the tolerance/logging mechanism itself
    is already the real, reusable piece.
    """
    checks = (
        _check_field_disagreement(
            ticker, "gross_margin", gross_margin_from_xbrl(facts), yf_metrics.gross_margin
        ),
        _check_field_disagreement(
            ticker,
            "operating_margin",
            operating_margin_from_xbrl(facts),
            yf_metrics.operating_margin,
        ),
    )
    return [d for d in checks if d is not None]


def apply_primary_metrics(
    metrics_by_symbol: dict[str, data.Metrics | None],
) -> dict[str, data.Metrics | None]:
    """XBRL primary, yfinance fallback (M37, Design v2.2 §3.4): override gross/operating margin.

    "XBRL companyfacts becomes the primary source. ... yfinance drops to
    a fallback for fields XBRL doesn't cover." Takes a yfinance-sourced
    metrics dict (as returned by data.fetch_all_metrics) and returns a
    NEW dict with gross_margin/operating_margin replaced by the
    XBRL-derived value for every ticker XBRL has usable data for --
    unchanged (still the yfinance value) for a ticker XBRL has no CIK
    match, no companyfacts, or no plausible margin for.

    Deliberately does not touch data.fetch_all_metrics/fetch_metrics
    themselves, or shadow_compare's own inputs -- callers that need
    "yfinance vs. XBRL, independently" (xbrl_shadow.py's whole reason to
    exist, both today and for whatever fields M38-M41 add later) still
    get two genuinely independent sources to compare; only callers that
    explicitly want the *screening/gating* decision (screener.py, this
    function's actual caller) get the overridden view.

    A ticker with no yfinance metrics at all (fetch_all_metrics already
    returned None for it) stays None here too -- XBRL alone can't
    substitute for a fully-missing Metrics record (market_cap,
    trailing_pe, and every other Graham-gate field this function doesn't
    touch would still be missing).

    The yfinance fallback itself is sanity-checked too, not trusted
    blindly (staff-engineer-reviewer + warren-buffett findings, M37
    review round): §1's own documented NMIH case showed yfinance can
    manufacture a nonsensical margin (an insurer's operating margin
    exceeding its own gross margin) with nothing else in this codebase
    ever catching it. That check lives in `data.validate_metrics`, not
    here -- `config.MARGIN_PLAUSIBLE_MIN`/`_MAX` applies to whichever
    value a Metrics record ends up with, XBRL-derived or yfinance-
    fallback alike, tagged `data_invalid_outlier:*` the same way
    `MAX_PLAUSIBLE_PE`/`_DEBT_TO_EQUITY` already are, so an implausible
    fallback value fails the gate honestly instead of passing through
    as if it were trustworthy. `gross_margin_from_xbrl`/
    `operating_margin_from_xbrl` never return a value outside that same
    bound in the first place (`_margin_from_xbrl`'s own plausibility
    walk), so this function only ever needs to let a value through, not
    re-check it -- `data.validate_metrics` is the single place either
    source's value gets judged.

    Sequential over tickers, same reasoning as xbrl_shadow.py's own
    run(): xbrl.throttled_get already serializes every EDGAR request
    behind one shared rate limiter, so a thread pool here would only
    queue behind the same lock, not go faster. Per-ticker exceptions are
    isolated (staff-engineer-reviewer finding: every sibling batch loop
    in this codebase -- data.fetch_all_metrics, screener.run_screen --
    already guarantees one ticker's malformed data can't abort the
    whole run; this function previously had no such guard, an
    inconsistency given it now sits on the same production gating path)
    -- a ticker whose EDGAR response trips an unexpected exception logs
    and falls back to its original yfinance metrics, the same safe
    degrade as every other failure path here, rather than crashing the
    batch.
    """
    cik_lookup = load_cik_lookup()
    result: dict[str, data.Metrics | None] = {}
    for symbol, metrics in metrics_by_symbol.items():
        if metrics is None:
            result[symbol] = None
            continue
        try:
            cik = get_cik(symbol, cik_lookup)
            xbrl_gross_margin = None
            xbrl_operating_margin = None
            if cik is not None:
                facts = fetch_company_facts(cik)
                if facts is not None:
                    xbrl_gross_margin = gross_margin_from_xbrl(facts)
                    xbrl_operating_margin = operating_margin_from_xbrl(facts)
            if xbrl_gross_margin is None and xbrl_operating_margin is None:
                result[symbol] = metrics
            else:
                result[symbol] = dataclasses.replace(
                    metrics,
                    gross_margin=(
                        xbrl_gross_margin if xbrl_gross_margin is not None else metrics.gross_margin
                    ),
                    operating_margin=(
                        xbrl_operating_margin
                        if xbrl_operating_margin is not None
                        else metrics.operating_margin
                    ),
                )
        except Exception:
            logger.error(
                "%s: apply_primary_metrics failed -- falling back to yfinance's own metrics "
                "unchanged for this ticker",
                symbol,
                exc_info=True,
            )
            result[symbol] = metrics
    return result
