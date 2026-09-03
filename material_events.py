"""8-K material-event polling and alerting (Design v2.2 §3.8 Tier 1, M42).

Deterministic, no model: polls SEC EDGAR's own submissions index for
each held ticker, classifies any new 8-K filing by item number against
a fixed, closed taxonomy, and alerts on the ones that matter. This is
"the highest value-per-unit-complexity item in the entire v2 design"
per the design doc's own words -- structured data straight from SEC,
free, and available within days of the actual event.

The hard constraint, enforced structurally rather than by convention:
alert-only, never trade-triggering. This module never imports
execution.py or portfolio.py, and no function here returns anything an
order, a score, or a strike could be derived from -- see
tests/test_material_events.py's own "no code path" test for the
regression check.

Tier 2 (model-assisted third-party event extraction) and Tier 3
(everything else, filtered out) are explicitly out of scope here --
they are Tranche 4's optional qualitative layer (M44-M49), gated far
later. This module is Tier 1 only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import config
import journal
import xbrl

# mypy strict (no_implicit_reexport): xbrl/urllib are re-exported so
# tests can patch material_events.xbrl.throttled_get and
# material_events.urllib.request.urlopen directly -- the actual network/
# XBRL-fetch seams this module calls through, not incidental attributes.
__all__ = ["urllib", "xbrl"]

logger = logging.getLogger(__name__)

_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"

# The closed taxonomy from Design v2.2 §3.8's own table. An item number
# not in this dict is not classified and never alerts -- silently
# ignored, matching the design's "closed enumeration... an unmatched
# record is discarded, not downgraded" principle (stated there for Tier
# 2, applied here identically for Tier 1's own taxonomy). "9.01"
# (Financial Statements and Exhibits) is deliberately absent: it's
# boilerplate present on nearly every 8-K regardless of what triggered
# it, carries no severity information on its own, and including it
# would alert on almost every single 8-K filed.
_ITEM_SEVERITY: dict[str, tuple[str, str]] = {
    "4.02": ("Non-reliance on previously issued financials", "Critical"),
    "1.03": ("Bankruptcy or receivership", "Critical"),
    "4.01": ("Auditor change", "High"),
    "5.02": ("Departure of principal officers", "Medium"),
    "2.01": ("Asset disposition", "Medium"),
    "2.05": ("Exit or disposal costs", "Medium"),
    "2.06": ("Material impairment", "Medium"),
    "2.02": ("Results of operations", "Low"),
}
# Item 2.02 (quarterly/annual results) is routine and files on every
# earnings date for every holding -- suppressed by default per the
# design doc's own table ("Low -- routine, usually suppressed"), so it's
# classified (for completeness/audit) but never sent to Discord.
_SUPPRESSED_ITEMS = frozenset({"2.02"})


def classify_items(items_field: str) -> list[tuple[str, str, str]]:
    """Match an 8-K's comma-separated item-number string against the closed taxonomy.

    Returns a list of (item, meaning, severity) tuples for every item
    number this taxonomy recognizes -- an unrecognized item number
    (SEC's submissions API reports many item numbers this table doesn't
    care about, e.g. 7.01/8.01/5.07/9.01) is silently skipped, not
    guessed at. An empty `items_field` (some 8-Ks, and every non-8-K
    form) returns an empty list.
    """
    if not items_field:
        return []
    matches = []
    for item in items_field.split(","):
        item = item.strip()
        classification = _ITEM_SEVERITY.get(item)
        if classification is not None:
            meaning, severity = classification
            matches.append((item, meaning, severity))
    return matches


def fetch_recent_8k_filings(cik: str) -> list[dict[str, str]]:
    """The most recent 8-K filings for `cik`, from SEC EDGAR's submissions index.

    Returns a list of dicts with accession_number/filing_date/items,
    newest first (matching the submissions API's own order). Fails soft
    (returns [], logs) on any fetch/parse error -- one ticker's transient
    EDGAR failure must never abort polling the rest of the holdings,
    same posture as xbrl.fetch_company_facts for the identical class of
    problem.

    Only the "recent" page of the submissions index is read (not the
    older `files` pagination some filers' full histories spill into) --
    sufficient for polling, whose whole point is catching *new* filings
    since the last run, never a full historical backfill.
    """
    url = _SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
    try:
        body = xbrl.throttled_get(url)
    except urllib.error.HTTPError as e:
        logger.error("CIK %s: submissions fetch failed (HTTP %d)", cik, e.code)
        return []
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.error("CIK %s: submissions fetch failed", cik, exc_info=True)
        return []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        logger.error("CIK %s: submissions response was not valid JSON", cik)
        return []
    recent = parsed.get("filings", {}).get("recent", {}) if isinstance(parsed, dict) else {}
    forms = recent.get("form", [])
    if not isinstance(forms, list):
        return []
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    items = recent.get("items", [])
    filings = []
    for i, form in enumerate(forms):
        # "8-K/A" (an amendment) matters just as much as the original --
        # staff-engineer-reviewer finding: an amendment can carry
        # corrected/restated item information, including exactly the
        # Critical-severity 4.02 (non-reliance on previously issued
        # financials) case this taxonomy exists to catch, and excluding
        # it silently was a real coverage gap, demonstrated live against
        # this module's own fixture data (a real AAPL 8-K/A with a
        # taxonomy-matching item).
        if form not in ("8-K", "8-K/A"):
            continue
        if i >= len(accessions) or i >= len(filing_dates):
            continue  # malformed/misaligned response -- skip this entry, not the whole batch
        filings.append(
            {
                "accession_number": str(accessions[i]),
                "filing_date": str(filing_dates[i]),
                "items": str(items[i]) if i < len(items) else "",
            }
        )
    return filings


def _send_discord_alert(message: str) -> None:
    """POSTs `message` to config.DISCORD_MATERIAL_EVENT_WEBHOOK_URL.

    Same fail-soft posture and explicit-User-Agent requirement as
    pnl.py's _send_discord_alert/news_update.py's _post_discord_message
    -- Discord's Cloudflare front end blocks urllib's default
    User-Agent, and a missed material-event notification, while
    high-signal, is still not worth failing the whole poll over (the
    event is durably recorded in material_events regardless of whether
    this POST succeeds; a failed alert loses timeliness, not the record
    itself).
    """
    if not config.DISCORD_MATERIAL_EVENT_WEBHOOK_URL:
        return
    body = json.dumps({"content": message}).encode("utf-8")
    request = urllib.request.Request(
        config.DISCORD_MATERIAL_EVENT_WEBHOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": config.DISCORD_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"::warning::Material-event Discord alert failed to send: {exc}")
        logger.warning("Material-event Discord alert failed to send: %s", exc)


def poll_ticker(ticker: str, cik: str) -> list[dict[str, object]]:
    """Poll one ticker's recent 8-Ks, alert on new taxonomy matches, return them.

    Idempotent across runs via journal.has_alerted_on_filing --
    accession_number is SEC's own globally-unique id per filing, so a
    filing already recorded in a prior run's material_events table is
    never re-alerted. A filing with no taxonomy-matching items (or only
    suppressed ones) is neither alerted nor recorded -- material_events
    is an alerted-events log, not a full 8-K mirror; classify_items'
    return value alone is enough for a caller to recompute those if ever
    needed.
    """
    new_events: list[dict[str, object]] = []
    for filing in fetch_recent_8k_filings(cik):
        accession_number = filing["accession_number"]
        if journal.has_alerted_on_filing(accession_number):
            continue
        matches = classify_items(filing["items"])
        alertable = [m for m in matches if m[0] not in _SUPPRESSED_ITEMS]
        if not alertable:
            continue
        severities = [severity for _, _, severity in alertable]
        primary_severity = next(
            (s for s in ("Critical", "High", "Medium", "Low") if s in severities), "Low"
        )
        journal.record_material_event(
            accession_number, ticker, filing["filing_date"], filing["items"], primary_severity
        )
        descriptions = "; ".join(f"Item {item} ({meaning})" for item, meaning, _ in alertable)
        _send_discord_alert(
            f"[{primary_severity}] {ticker}: 8-K filed {filing['filing_date']} -- {descriptions}\n"
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&"
            f"accession_number={accession_number}"
        )
        logger.error("%s: material event alert (%s) -- %s", ticker, primary_severity, descriptions)
        new_events.append(
            {
                "ticker": ticker,
                "accession_number": accession_number,
                "filing_date": filing["filing_date"],
                "severity": primary_severity,
                "items": alertable,
            }
        )
    return new_events


def poll_holdings(tickers: list[str]) -> list[dict[str, object]]:
    """Poll every held ticker for new material 8-K events; returns all new alerts.

    A ticker EDGAR doesn't recognize (get_cik returns None -- a very
    thin foreign issuer, or a data lag right after a fresh listing) is
    skipped, logged, and does not abort polling the rest of the
    holdings -- same per-item fault tolerance this codebase applies
    everywhere else (market_buy/liquidate's own per-order try/except is
    the model).
    """
    cik_lookup = xbrl.load_cik_lookup()
    all_new_events: list[dict[str, object]] = []
    for ticker in tickers:
        cik = xbrl.get_cik(ticker, cik_lookup)
        if cik is None:
            logger.warning("%s: no CIK found on EDGAR -- skipping material-event poll", ticker)
            continue
        all_new_events.extend(poll_ticker(ticker, cik))
    return all_new_events


def _held_symbols_from_pnl_snapshot() -> list[str]:
    """Held tickers from pnl.json, same read-only source news_update.py already uses.

    Not execution.get_current_holdings() -- this module must never
    import execution.py at all (the structural half of "alert-only,
    never trade-triggering": it can't gain trading capability by
    accident if it never has a handle on anything that can place an
    order). pnl.json is written by pnl.py, which runs in the same
    trading-workflow job where Alpaca credentials already live.
    """
    if not config.PNL_DATA_PATH.exists():
        logger.warning("%s not found -- skipping material-event poll", config.PNL_DATA_PATH)
        return []
    try:
        snapshot = json.loads(config.PNL_DATA_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("%s unreadable -- skipping material-event poll", config.PNL_DATA_PATH)
        return []
    positions = snapshot.get("positions", [])
    if not isinstance(positions, list):
        return []
    symbols = sorted(
        {
            str(p["symbol"])
            for p in positions
            if isinstance(p, dict) and isinstance(p.get("symbol"), str) and p["symbol"]
        }
    )
    return symbols


def run() -> None:
    """Poll every currently held ticker for new material events (entry point).

    Deliberately fails soft end to end: an exception anywhere in a
    single ticker's poll is caught inside poll_ticker/fetch_recent_8k_
    filings and logged, never raised out of this function, so a broken
    EDGAR response for one ticker can't take down the trading run this
    is invoked alongside (news_update.py's own module docstring states
    the identical reasoning for the identical reason: this is a display/
    monitoring feature layered on top of the trading run, not something
    that should ever fail the job).
    """
    journal.configure_logging()
    tickers = _held_symbols_from_pnl_snapshot()
    if not tickers:
        logger.info("material_events.run: no held tickers -- nothing to poll")
        return
    events = poll_holdings(tickers)
    logger.info(
        "material_events.run: %d new alert(s) across %d ticker(s)", len(events), len(tickers)
    )


if __name__ == "__main__":
    run()
