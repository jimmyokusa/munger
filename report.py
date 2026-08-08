"""Static HTML report: what the bot picked, and why (DESIGN.md 3.6).

Reads screen_results.csv and journal.db and writes a small static site to
config.REPORT_DIR -- no server, no build step, just HTML files a browser
opens directly. Regenerate anytime with `python report.py`; bot.py does
not call this automatically (it's a display concern, not part of the
trading run itself).

index.html: current picks (from the journal), each with an expandable
panel showing the recorded buy reason and the metrics that drove its
score. tickers.html: every other screened ticker, in a sortable,
filterable table, so "why wasn't X picked" is answerable too.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import math
import shutil
import urllib.parse
from pathlib import Path
from typing import TypeGuard

import numpy as np
import pandas as pd

import config
import journal

logger = logging.getLogger(__name__)

_METRIC_LABELS: dict[str, str] = {
    "market_cap": "Market Cap",
    "trailing_pe": "Trailing P/E",
    "price_to_book": "Price / Book",
    "current_ratio": "Current Ratio",
    "debt_to_equity": "Debt / Equity",
    "return_on_equity": "Return on Equity",
    "gross_margin": "Gross Margin",
    "operating_margin": "Operating Margin",
    "free_cash_flow": "Free Cash Flow",
    "dividend_yield": "Dividend Yield",
    "consecutive_positive_earnings_years": "Consecutive Positive-Earnings Years",
}

_PERCENT_METRICS = frozenset(
    {"return_on_equity", "gross_margin", "operating_margin", "dividend_yield"}
)
_DOLLAR_METRICS = frozenset({"market_cap", "free_cash_flow"})

# Plain-language, Munger/Graham-style explanations shown as a title
# tooltip on each metric label (user request). Kept short -- this is a
# hover hint, not the methodology drawer (_render_methodology_drawer),
# which has the full gate/score writeup.
_METRIC_TOOLTIPS: dict[str, str] = {
    "market_cap": "Total market value of the company's shares. Graham's gate 1 "
    "requires at least $2B, avoiding fragile small caps.",
    "trailing_pe": "Price / trailing 12-month earnings. Lower means you're paying "
    "less per dollar of current profit. Graham's gate 6 caps this at 20.",
    "price_to_book": "Price / book (accounting net worth) value. Combined with "
    "P/E in Graham's gate 7 (P/E x P/B <= 30) as a margin-of-safety check.",
    "current_ratio": "Current assets / current liabilities. A rough near-term "
    "solvency check -- Graham's gate 2 requires at least 1.5.",
    "debt_to_equity": "Total debt / shareholder equity. Lower is safer. Graham's "
    "gate 3 caps this at 1.0; the Munger score also rewards low debt directly.",
    "return_on_equity": "Net income / shareholder equity -- how efficiently the "
    "business turns owners' capital into profit. Munger's quality floor "
    "requires at least 15%; it's the single heaviest-weighted score component.",
    "gross_margin": "Gross profit / revenue. A proxy for pricing power / moat "
    "strength. Munger's quality floor requires at least 30%.",
    "operating_margin": "Operating income / revenue -- profitability after "
    "operating costs, before interest and taxes.",
    "free_cash_flow": "Cash generated after the capital spending needed to run "
    "the business. Must be positive to clear Munger's quality floor.",
    "dividend_yield": "Annual dividend / share price. Not required (Graham's "
    "gate 5 is an optional toggle, off by default) -- a quality overlay "
    "substitutes for it in this system.",
    "consecutive_positive_earnings_years": "How many years running the company "
    "posted positive net income -- a v1 stand-in for Graham's original "
    "10-year earnings-stability test (data availability limit).",
}

# Plain-language explanations for each Graham/Munger gate fail-reason code,
# shown as a hover tooltip on the Fail reasons cell (user request: a raw
# code like "graham_current_ratio" doesn't explain itself, and a ticker can
# have a high score yet still fail one gate -- this is where a reader finds
# out which one and why).
_GATE_REASON_TOOLTIPS: dict[str, str] = {
    "graham_size": "Market cap below Graham's gate 1 minimum "
    f"(${config.MIN_MARKET_CAP / 1e9:.0f}B).",
    "graham_current_ratio": "Current ratio below Graham's gate 2 minimum "
    f"({config.MIN_CURRENT_RATIO}).",
    "graham_debt_to_equity": "Debt/equity above Graham's gate 3 maximum "
    f"({config.MAX_DEBT_TO_EQUITY}), or negative (liabilities exceed assets).",
    "graham_earnings_stability": "Fewer than Graham's gate 4 minimum "
    f"({config.MIN_CONSECUTIVE_POSITIVE_EARNINGS_YEARS}) consecutive years of "
    "positive net income.",
    "graham_dividend_record": "No qualifying dividend -- Graham's gate 5, "
    "only checked when the REQUIRE_DIVIDEND_RECORD toggle is enabled.",
    "graham_pe": f"P/E above Graham's gate 6 maximum ({config.MAX_PE}), or "
    "zero/negative (unprofitable).",
    "graham_pe_times_pb": "Combined P/E x P/B above Graham's gate 7 "
    f"maximum ({config.MAX_PE_TIMES_PB}), or non-positive.",
    "munger_roe": "Return on equity below Munger's quality floor "
    f"({config.MIN_ROE:.0%}).",
    "munger_gross_margin": "Gross margin below Munger's quality floor "
    f"({config.MIN_GROSS_MARGIN:.0%}).",
    "munger_fcf": "Free cash flow is not positive -- only checked when the "
    "REQUIRE_POSITIVE_FCF toggle is enabled (on by default).",
    "data_missing:fetch_failed": "yfinance's shared rate limit was hit during "
    "this run and the fetch didn't complete in time -- not that the data "
    "doesn't exist. Usually recoverable on the next run.",
    "data_invalid_outlier:unhandled": "An unexpected error occurred computing "
    "this ticker's gates/score; treated as a fail, not silently skipped.",
}


def _fail_reason_tooltip(token: str) -> str:
    """Plain-language explanation for one fail_reasons token (user request).

    Static gate codes look up directly; data_missing:<field> and
    data_invalid_outlier:<field> carry a dynamic field name suffix (see
    data.validate_metrics), so those are built from _METRIC_LABELS instead
    of needing one dict entry per field.
    """
    if token in _GATE_REASON_TOOLTIPS:
        return _GATE_REASON_TOOLTIPS[token]
    if ":" in token:
        prefix, field = token.split(":", 1)
        label = _METRIC_LABELS.get(field, field)
        if prefix == "data_missing":
            return f"{label} wasn't returned by the data provider for this fetch."
        if prefix == "data_invalid_outlier":
            return f"{label}'s value was an implausible outlier and excluded."
    return ""

# A rounded-square "M" monogram in the site's own gradient (matching the
# body background below) -- inlined as a data URI so the favicon needs no
# extra file to host or build step, just this one string.
_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="#60a5fa"/>
<stop offset="50%" stop-color="#a78bfa"/>
<stop offset="100%" stop-color="#f472b6"/>
</linearGradient></defs>
<rect width="64" height="64" rx="14" fill="url(#g)"/>
<text x="32" y="46" font-family="-apple-system,Segoe UI,sans-serif" font-size="36" \
font-weight="700" fill="#1e1b2e" text-anchor="middle">M</text>
</svg>"""
_FAVICON_HREF = "data:image/svg+xml," + urllib.parse.quote(_FAVICON_SVG)
_FAVICON_LINK = f'<link rel="icon" type="image/svg+xml" href="{_FAVICON_HREF}">'

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 900px; margin: 0 auto; padding: 2rem 1rem 4rem; line-height: 1.5;
  min-height: 100vh;
  color: light-dark(#1e1b2e, #e5e7eb);
  background: light-dark(
    linear-gradient(135deg, #dbeafe 0%, #ede9fe 45%, #fce7f3 100%),
    linear-gradient(135deg, #0b1120 0%, #1e1b4b 55%, #1a1030 100%)
  );
  background-attachment: fixed;
}
h1 { font-size: 1.6rem; font-weight: 700; margin-bottom: 0.5rem; }
nav { margin-bottom: 1.5rem; }
nav a {
  margin-right: 1rem; text-decoration: none; font-weight: 600;
  color: light-dark(#4338ca, #a5b4fc);
}
nav a:hover { text-decoration: underline; }

/* Glass card: translucent + blurred, needs the gradient body background
   behind it to actually read as "glass" -- a flat backdrop makes blur
   nearly invisible. */
.glass {
  background: light-dark(rgba(255, 255, 255, 0.55), rgba(30, 27, 55, 0.45));
  backdrop-filter: blur(18px) saturate(160%);
  -webkit-backdrop-filter: blur(18px) saturate(160%);
  border: 1px solid light-dark(rgba(255, 255, 255, 0.6), rgba(255, 255, 255, 0.08));
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(31, 38, 135, 0.12);
}

.pick { padding: 1rem 1.25rem; margin-bottom: 0.85rem; }
.pick summary {
  cursor: pointer; display: flex; justify-content: space-between;
  align-items: center; font-weight: 600; list-style: none;
}
.pick summary::-webkit-details-marker { display: none; }
.pick summary .score {
  font-weight: 600; font-size: 0.85rem; padding: 0.2rem 0.65rem; border-radius: 999px;
  background: light-dark(rgba(99, 102, 241, 0.15), rgba(165, 180, 252, 0.2));
}
.pick-body { margin-top: 0.85rem; }
/* overflow-x: auto, not the table itself, so a long metric value (or a
   narrow phone viewport) scrolls its own row instead of breaking the
   page layout -- same pattern as .table-wrap around table.tickers. */
.metrics-wrap { overflow-x: auto; margin-top: 0.5rem; }
table.metrics { border-collapse: collapse; font-size: 0.9rem; width: 100%; }
table.metrics td { padding: 0.25rem 0.75rem 0.25rem 0; }
table.metrics td:first-child { opacity: 0.65; cursor: help; }

/* Visual badges (user request): quick-scan highlights on a pick, ranked
   green > blue > neutral by how directly they signal quality vs. just
   informational. Kept to text + background, no icons, so they read fine
   at the tiny sizes a badge needs. */
.badge {
  display: inline-block; font-size: 0.72rem; font-weight: 600;
  padding: 0.15rem 0.55rem; border-radius: 999px; margin: 0 0.3rem 0.3rem 0;
  white-space: nowrap;
}
.badge-green {
  color: light-dark(#166534, #86efac);
  background: light-dark(rgba(34, 197, 94, 0.15), rgba(34, 197, 94, 0.2));
}
.badge-blue {
  color: light-dark(#1e40af, #93c5fd);
  background: light-dark(rgba(59, 130, 246, 0.15), rgba(59, 130, 246, 0.2));
}
/* Provisioned per the user's spec (a 3rd, non-quality-signal tier) but
   not yet emitted by any renderer -- _render_badges only produces
   green/blue today. Kept defined so a future informational badge (e.g. a
   "held position" tag) doesn't need a new CSS pass. */
.badge-neutral {
  color: light-dark(#3f3f46, #d4d4d8);
  background: light-dark(rgba(113, 113, 122, 0.15), rgba(161, 161, 170, 0.2));
}

/* P&L page (user request): gain/loss color convention, reusing .badge-
   green's exact color for gains rather than inventing a second green. */
.pnl-gain { color: light-dark(#166534, #86efac); }
.pnl-loss { color: light-dark(#991b1b, #fca5a5); }
.pnl-flat { opacity: 0.7; }
.stat-tiles {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.75rem; margin-bottom: 1.25rem;
}
.stat-tile { padding: 0.9rem 1.1rem; }
.stat-tile .stat-label {
  font-size: 0.75rem; opacity: 0.65; text-transform: uppercase; letter-spacing: 0.03em;
}
.stat-tile .stat-value { font-size: 1.4rem; font-weight: 700; margin-top: 0.15rem; }
.stat-tile .stat-subvalue { font-size: 0.85rem; margin-top: 0.1rem; }
.mode-badge {
  display: inline-block; font-size: 0.75rem; font-weight: 600;
  padding: 0.15rem 0.6rem; border-radius: 999px; margin-left: 0.5rem;
  vertical-align: middle;
  color: light-dark(#92400e, #fcd34d);
  background: light-dark(rgba(245, 158, 11, 0.15), rgba(245, 158, 11, 0.2));
}

/* Methodology drawer: same <details>/glass pattern as .pick, so it costs
   no new CSS beyond spacing -- just needs to read as a document, not a
   card. */
.methodology { padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.methodology summary { cursor: pointer; font-weight: 600; }
.methodology .methodology-body { margin-top: 0.85rem; font-size: 0.9rem; }
.methodology h3 { font-size: 0.95rem; margin: 0.9rem 0 0.3rem; }
.methodology h3:first-child { margin-top: 0; }
.methodology ul { margin: 0.2rem 0; padding-left: 1.2rem; }
.methodology li { margin-bottom: 0.15rem; }

.empty { opacity: 0.7; font-style: italic; padding: 1.5rem; text-align: center; }
.generated { opacity: 0.55; font-size: 0.8rem; margin-bottom: 1rem; }

/* Outbound research links + export controls (user request). */
.research-links { margin-top: 0.6rem; font-size: 0.82rem; opacity: 0.75; }
.research-links a { color: inherit; }
.export-controls { margin-bottom: 1rem; display: flex; gap: 0.6rem; flex-wrap: wrap; }
.export-controls button {
  font: inherit; font-size: 0.82rem; font-weight: 600; cursor: pointer;
  padding: 0.4rem 0.85rem; border-radius: 10px; color: inherit;
  border: 1px solid light-dark(rgba(0, 0, 0, 0.15), rgba(255, 255, 255, 0.15));
  background: light-dark(rgba(255, 255, 255, 0.6), rgba(30, 27, 55, 0.5));
}
.export-controls button:hover { opacity: 0.85; }
.export-controls .export-status {
  font-size: 0.8rem; opacity: 0.65; align-self: center;
}

.progress-banner { display: none; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.progress-banner.active { display: block; }
.progress-banner .phase { font-weight: 600; margin-bottom: 0.6rem; }
.progress-bar-track {
  background: light-dark(rgba(0, 0, 0, 0.08), rgba(255, 255, 255, 0.1));
  border-radius: 999px; height: 10px; overflow: hidden; margin-bottom: 0.5rem;
}
.progress-bar-fill {
  height: 100%; border-radius: 999px; width: 0%;
  background: linear-gradient(90deg, #6366f1, #ec4899);
  transition: width 0.4s ease;
}
.progress-detail { font-size: 0.85rem; opacity: 0.75; }

#filter {
  padding: 0.55rem 0.85rem; width: 100%; max-width: 320px; margin-bottom: 1rem;
  border-radius: 10px; color: inherit;
  border: 1px solid light-dark(rgba(0, 0, 0, 0.15), rgba(255, 255, 255, 0.15));
  background: light-dark(rgba(255, 255, 255, 0.6), rgba(30, 27, 55, 0.5));
}
.table-wrap { padding: 0.5rem; overflow-x: auto; }
table.tickers { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
table.tickers th, table.tickers td { padding: 0.5rem 0.7rem; text-align: left;
  border-bottom: 1px solid light-dark(rgba(0, 0, 0, 0.08), rgba(255, 255, 255, 0.08)); }
table.tickers th { cursor: pointer; user-select: none; position: sticky; top: 0;
  background: light-dark(rgba(255, 255, 255, 0.85), rgba(20, 18, 38, 0.85)); }
table.tickers th:hover { opacity: 0.7; }
tr.not-buyable { opacity: 0.55; }

.cal-month { padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.cal-month h2 { font-size: 1.1rem; margin: 0 0 0.75rem; }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 0.35rem; }
.cal-weekday { font-size: 0.75rem; opacity: 0.6; text-align: center; padding-bottom: 0.25rem; }
.cal-cell, a.cal-cell {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  aspect-ratio: 1; border-radius: 8px; text-decoration: none; color: inherit;
  font-size: 0.8rem;
}
.cal-blank { visibility: hidden; }
.cal-empty { opacity: 0.35; }
.cal-has-data {
  background: light-dark(rgba(99, 102, 241, 0.12), rgba(99, 102, 241, 0.15));
}
.cal-has-buyable {
  background: light-dark(rgba(236, 72, 153, 0.2), rgba(236, 72, 153, 0.25));
  font-weight: 600;
}
a.cal-cell:hover { filter: brightness(0.95); }
.cal-day { line-height: 1.3; }
.cal-count { font-size: 0.7rem; opacity: 0.8; }
.cal-legend { font-size: 0.8rem; opacity: 0.75; margin-bottom: 1rem; }
.cal-legend-swatch {
  display: inline-block; width: 0.8rem; height: 0.8rem; border-radius: 3px;
  vertical-align: middle; margin-right: 0.25rem;
}
"""


def _analytics_snippet() -> str:
    """The GoatCounter count.js <script>, or "" when analytics is unconfigured.

    Read from config live (not a module constant) so tests can monkeypatch
    config.ANALYTICS_URL, matching how GRAFANA_BASE_URL is read per-call. The
    beacon is cookieless and same-origin (loaded from our own stats subdomain),
    so no consent banner is required (DESIGN_WEB_ANALYTICS_SEO.md §2.1).
    """
    endpoint = config.ANALYTICS_URL
    if not endpoint:
        return ""
    host = urllib.parse.urlsplit(endpoint).netloc
    return (
        f'<script data-goatcounter="{html.escape(endpoint, quote=True)}" '
        f'async src="//{html.escape(host, quote=True)}/count.js"></script>'
    )


def _seo_head(title: str, description: str, path: str) -> str:
    """Shared <!doctype>..</head> block: title, SEO meta, favicon, analytics, CSS.

    `title` is the full <title> text (kept starting with "Munger Screener" for
    consistency across pages); `description` is a <=155-char page summary; `path`
    is the page's filename (e.g. "index.html") for canonical/OG URLs.

    Canonical, Open Graph, and an explicit index,follow are emitted only when
    config.SITE_BASE_URL is set -- the env-gated pattern shared with
    GRAFANA_BASE_URL. On dev/local (unset) the page is marked noindex,nofollow
    and carries no absolute URLs, so a dev deployment is never indexed and never
    advertises gramunger.com links (DESIGN_WEB_ANALYTICS_SEO.md §1 constraint 3).
    og:image is intentionally omitted until a real preview asset exists -- an
    og:image pointing at a 404 is worse than none (§3.3).
    """
    # title/description are static, author-controlled page chrome that already
    # carry intentional HTML entities (e.g. "&mdash;", "P&amp;L") -- the same
    # pre-escaped strings the inline <head>s embedded before M18. They contain
    # no raw double-quotes, so they drop straight into content="..." attributes;
    # re-escaping here would double-encode the entities into visible "&amp;mdash;".
    social: list[str] = []
    if config.SITE_BASE_URL:
        canonical = f"{config.SITE_BASE_URL}/{path}"
        esc_url = html.escape(canonical, quote=True)
        social = [
            '<meta name="robots" content="index,follow">',
            f'<link rel="canonical" href="{esc_url}">',
            '<meta property="og:type" content="website">',
            '<meta property="og:site_name" content="Munger Screener">',
            f'<meta property="og:title" content="{title}">',
            f'<meta property="og:description" content="{description}">',
            f'<meta property="og:url" content="{esc_url}">',
            '<meta name="twitter:card" content="summary">',
            f'<meta name="twitter:title" content="{title}">',
            f'<meta name="twitter:description" content="{description}">',
        ]
    else:
        social = ['<meta name="robots" content="noindex,nofollow">']
    head_lines = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        f'<meta name="description" content="{description}">',
        *social,
        _FAVICON_LINK,
        _analytics_snippet(),
        f"<style>{_CSS}</style></head>",
    ]
    # Drop the analytics line when unconfigured so no stray blank line appears.
    return "\n".join(line for line in head_lines if line)


# Public pages that belong in robots.txt / sitemap.xml. Operational artifacts
# (progress.json, the archive CSVs) are deliberately excluded -- they are not
# content and shouldn't be indexed. Which history page exists depends on the
# GRAFANA_BASE_URL fork (dashboards.html vs calendar.html); resolved at
# generation time in _sitemap_pages() rather than hard-coded here.
_ALWAYS_INDEXED_PAGES = ("index.html", "tickers.html", "pnl.html")


def _sitemap_pages() -> tuple[str, ...]:
    history_page = "dashboards.html" if config.GRAFANA_BASE_URL else "calendar.html"
    return (*_ALWAYS_INDEXED_PAGES, history_page)


def _render_robots_txt() -> str:
    """robots.txt written into REPORT_DIR (must be at the served web root).

    Prod (SITE_BASE_URL set): allow the real pages, keep operational artifacts
    out, and advertise the sitemap. Dev (unset): Disallow: / -- belt-and-
    suspenders so a dev deploy that somehow becomes reachable is never indexed
    (DESIGN_WEB_ANALYTICS_SEO.md §3.2).
    """
    if not config.SITE_BASE_URL:
        return "User-agent: *\nDisallow: /\n"
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /progress.json\n"
        "Disallow: /screen_results_archive/\n"
        f"Sitemap: {config.SITE_BASE_URL}/sitemap.xml\n"
    )


def _render_sitemap_xml() -> str:
    """sitemap.xml listing the canonical pages generated this run.

    <lastmod> is today's UTC date -- the site regenerates daily, so every page's
    last-modified is the generation date. Only called when SITE_BASE_URL is set
    (a sitemap of relative URLs is meaningless), so callers gate on it.
    """
    lastmod = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    urls = "".join(
        f"  <url><loc>{html.escape(config.SITE_BASE_URL + '/' + page, quote=True)}</loc>"
        f"<lastmod>{lastmod}</lastmod></url>\n"
        for page in _sitemap_pages()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        "</urlset>\n"
    )


def _load_screen_results() -> pd.DataFrame:
    if not config.SCREEN_RESULTS_CSV_PATH.exists():
        return pd.DataFrame(columns=["symbol", "buyable", "score", "fail_reasons"])
    return pd.read_csv(config.SCREEN_RESULTS_CSV_PATH)


def _load_pnl_snapshot() -> dict[str, object] | None:
    """The latest P&L snapshot pnl.py wrote, or None if it doesn't exist yet.

    pnl.py runs where the Alpaca keys live (daily-trade.yml, GitHub
    Actions) and pushes its output into the same GCS bucket this
    deployment mounts at config.DATA_DIR -- report.py itself never talks
    to Alpaca (DESIGN.md 3.5/M14's screen-only boundary). A missing or
    malformed file is a normal, expected state (first run before the
    bridge has fired even once, or a transient upload gap), not a report-
    generation failure -- returns None rather than raising, so one bad
    P&L file can't take down index.html/tickers.html too.
    """
    if not config.PNL_DATA_PATH.exists():
        return None
    try:
        data = json.loads(config.PNL_DATA_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        logger.error("%s unreadable/corrupt; omitting the P&L page", config.PNL_DATA_PATH)
        return None
    return data if isinstance(data, dict) else None


def _format_metric(name: str, value: object) -> str:
    # numpy's int64/float64 (what pandas actually hands back from a CSV
    # column) don't reliably subclass Python's int/float across numpy
    # versions -- np.int64 in particular does not -- so isinstance
    # against the builtins alone silently dropped every whole-number
    # column (e.g. market_cap) to "—"; found live via this module's own
    # tests, not by inspection. _is_numeric handles both.
    if not _is_numeric(value):
        return "—"
    numeric = float(value)
    if name in _DOLLAR_METRICS:
        return f"${numeric / 1e9:.2f}B" if abs(numeric) >= 1e9 else f"${numeric / 1e6:.1f}M"
    if name in _PERCENT_METRICS:
        return f"{numeric * 100:.1f}%"
    if name == "consecutive_positive_earnings_years":
        return str(int(numeric))
    return f"{numeric:.2f}"


def _is_numeric(value: object) -> TypeGuard[int | float]:
    """True for Python or numpy int/float, false for anything else (incl. NaN)."""
    if not isinstance(value, (int, float, np.integer, np.floating)):
        return False
    return not math.isnan(float(value))


def _is_buyable(value: object) -> bool:
    """Safe parse of the buyable column: True only for an actual True.

    A clean CSV gives pandas a real bool dtype, where bool(value) works
    fine -- but a column with any missing/blank cell can't hold NaN in a
    bool dtype, so pandas silently falls back to object dtype and the
    clean cells become the literal strings "True"/"False". bool("False")
    is True (non-empty string), which would have shown a row whose own
    displayed text says "False" as buyable and un-dimmed -- a directly
    visible inconsistency (staff-engineer-reviewer finding). Treats
    anything not unambiguously true as not-buyable, the conservative
    default this project uses elsewhere for missing/malformed data.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _metric_cell(name: str, value: object) -> str:
    """A <td> for a numeric column, carrying the raw value for JS sorting.

    tickers.html's sort comparator parses displayed text -- but
    _format_metric renders dollar figures with a leading "$"
    (e.g. "$2.50B"), and JS's parseFloat("$2.50B") is NaN, so those
    columns silently fell back to string-sorting text ("$2.50B" sorted
    before "$500.0M" despite being the larger figure) -- staff-engineer-
    reviewer finding. data-sort carries the real underlying number so the
    comparator has something reliable to parse regardless of formatting.
    """
    if not _is_numeric(value):
        return "<td>—</td>"
    if name == "score":
        return f'<td data-sort="{float(value)}">{float(value):.1f}</td>'
    return f'<td data-sort="{float(value)}">{_format_metric(name, value)}</td>'


def _render_metrics_table(metrics_row: pd.Series) -> str:
    rows = "".join(
        f'<tr><td title="{html.escape(_METRIC_TOOLTIPS.get(key, ""))}">{html.escape(label)}</td>'
        f"<td>{_format_metric(key, metrics_row.get(key))}</td></tr>"
        for key, label in _METRIC_LABELS.items()
    )
    return f'<div class="metrics-wrap"><table class="metrics">{rows}</table></div>'


# Thresholds are informational display copy, not new screening logic --
# they mirror DESIGN.md 3.3's existing gate/score constants (Graham gate 3
# "debt/equity <= 1.0", Munger's ROE >= 15% floor, dividend gate 5) and are
# intentionally not read from config.py: these are round, human-facing
# "what counts as a highlight" numbers for the report, not tunable
# screening parameters, so keeping them here (not config.py) avoids
# implying they gate buyability the way the real thresholds do.
_BADGE_ZERO_DEBT_MAX = 0.0
_BADGE_HIGH_ROE_MIN = 0.20
_BADGE_HIGH_DIVIDEND_YIELD_MIN = 0.03


def _render_badges(metrics_row: pd.Series | None) -> str:
    """Quick-scan highlight badges for a pick (user request).

    green: zero debt or high ROE (the two strongest quality signals this
    system already scores on -- DESIGN.md 3.3's low-debt and ROE score
    components). blue: a meaningful dividend, informational only (v1's
    dividend gate is an explicit optional toggle, off by default, so this
    is not a buy signal, just a highlight). Silent (empty string) when
    nothing qualifies or there's no metrics row at all -- a badge is a
    bonus signal, never a placeholder.
    """
    if metrics_row is None:
        return ""
    badges: list[str] = []
    debt_to_equity = metrics_row.get("debt_to_equity")
    roe = metrics_row.get("return_on_equity")
    dividend_yield = metrics_row.get("dividend_yield")
    # >= 0 guard, not just <= _BADGE_ZERO_DEBT_MAX: a negative debt/equity
    # means negative book equity (liabilities exceed assets) -- a red flag,
    # not "no debt" -- and would otherwise satisfy "<= 0.0" too. Same trap
    # screener.py's own gate/score logic already guards against (see its
    # comments on negative debt_to_equity); staff-engineer-reviewer finding.
    if (
        _is_numeric(debt_to_equity)
        and 0.0 <= float(debt_to_equity) <= _BADGE_ZERO_DEBT_MAX
    ):
        badges.append('<span class="badge badge-green">Zero debt</span>')
    if _is_numeric(roe) and float(roe) >= _BADGE_HIGH_ROE_MIN:
        badges.append('<span class="badge badge-green">High ROE</span>')
    if _is_numeric(dividend_yield) and float(dividend_yield) >= _BADGE_HIGH_DIVIDEND_YIELD_MIN:
        badges.append('<span class="badge badge-blue">Dividend</span>')
    return "".join(badges)


def _render_methodology_drawer() -> str:
    """"How scoring & screening works" section (user request).

    Collapsed by default via native <details> -- same zero-JS pattern as
    each pick's own expandable panel. Content mirrors DESIGN.md 3.3
    exactly, keeping its Stage 1 (7 Graham gates, pass/fail) vs. Stage 2
    (Munger quality floor + score) split explicit rather than flattening
    both into one undifferentiated list -- staff-engineer-reviewer finding
    on an earlier draft that miscounted the gates and blurred the two
    stages together. Keep the two in sync if the gates/weights ever change.
    """
    return """<details class="methodology glass">
  <summary>How scoring &amp; screening works</summary>
  <div class="methodology-body">
    <p><strong>Stage 1:</strong> every ticker must first clear all
    <strong>7 Graham entry gates</strong> (pass/fail) to even be
    considered:</p>
    <h3>Valuation</h3>
    <ul>
      <li>Trailing P/E &le; 20</li>
      <li>P/E &times; Price/Book &le; 30 (Munger's twist on Graham's own
      margin-of-safety check &mdash; paying a fair price for a wonderful
      business, not just a statistically cheap one)</li>
    </ul>
    <h3>Financial health</h3>
    <ul>
      <li>Market cap &ge; $2B (avoids fragile small caps)</li>
      <li>Current ratio &ge; 1.5</li>
      <li>Debt / equity &le; 1.0</li>
    </ul>
    <h3>Profitability</h3>
    <ul>
      <li>4 consecutive years of positive net income</li>
      <li>Dividend record: an optional gate, off by default in this
      system &mdash; a quality overlay substitutes for it</li>
    </ul>
    <p><strong>Stage 2:</strong> stocks clearing Stage 1 must also meet a
    Munger quality floor before they're eligible for a score:</p>
    <ul>
      <li><strong>Profitability</strong> &mdash; return on equity &ge; 15%,
      gross margin &ge; 30%</li>
      <li><strong>Cash flow</strong> &mdash; positive free cash flow</li>
    </ul>
    <p>Everything clearing both stages gets a <strong>0&ndash;100 Munger
    score</strong>, weighted: return on equity 30%, gross margin 20%, FCF
    yield 20%, low debt 15%, operating margin 15%. Ranking, not a pass/fail
    bar &mdash; the buy queue takes the top-scoring names within the
    portfolio's position limits.</p>
  </div>
</details>"""


def _render_research_links(symbol: str) -> str:
    """Outbound research links for a ticker (user request).

    SEC EDGAR's company-search page isn't parameterizable by a documented
    query string (unlike Finviz's quote page), so it links to the generic
    search entry point rather than guessing at undocumented URL params --
    the user still has to type the ticker there. urllib.parse.quote guards
    the symbol in the Finviz URL even though real tickers are always
    plain uppercase letters/dots/dashes (data.py normalizes them) --
    cheap insurance against a malformed symbol ever reaching this far.
    """
    esc_symbol = html.escape(symbol)
    finviz_url = f"https://finviz.com/quote.ashx?t={urllib.parse.quote(symbol)}"
    return (
        '<p class="research-links">'
        '<a href="https://www.sec.gov/edgar/searchedgar/companysearch" '
        f'target="_blank" rel="noopener noreferrer">SEC EDGAR &#8599;</a> &middot; '
        f'<a href="{html.escape(finviz_url)}" target="_blank" '
        f'rel="noopener noreferrer">Finviz ({esc_symbol}) &#8599;</a>'
        "</p>"
    )


def _render_pick(symbol: str, journal_row: dict[str, object] | None, results: pd.DataFrame) -> str:
    esc_symbol = html.escape(symbol)
    matches = results[results["symbol"] == symbol]
    metrics_row = matches.iloc[0] if len(matches) > 0 else None

    reason = html.escape(str(journal_row["reason"])) if journal_row else "no journal record"
    timestamp = html.escape(str(journal_row["timestamp"])) if journal_row else "unknown"
    notional = journal_row.get("notional") if journal_row else None
    notional_str = f"${float(notional):,.2f}" if _is_numeric(notional) else "—"
    score_str = (
        f"{float(metrics_row['score']):.1f}"
        if metrics_row is not None and _is_numeric(metrics_row["score"])
        else "—"
    )
    metrics_html = _render_metrics_table(metrics_row) if metrics_row is not None else ""
    badges_html = _render_badges(metrics_row)

    return f"""<details class="pick glass">
  <summary><span class="symbol">{esc_symbol}</span>
    <span class="score">score {score_str}</span></summary>
  <div class="pick-body">
    {f'<div class="badges">{badges_html}</div>' if badges_html else ""}
    <p><strong>Reason:</strong> {reason}</p>
    <p><strong>Bought:</strong> {timestamp} &middot; <strong>Notional:</strong> {notional_str}</p>
    {metrics_html}
    {_render_research_links(symbol)}
  </div>
</details>"""


def _generated_at() -> str:
    """A small "generated at" line.

    This is a static file with no server and no expiry, so it's the
    only signal an operator opening a bookmarked report days or weeks
    later has that it might be stale (staff-engineer-reviewer finding).
    """
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f'<p class="generated">Generated {html.escape(now)}</p>'


def _disclaimer_banner() -> str:
    """Standing "not financial advice" banner (user request).

    Unlike _pnl_staleness_note (data-freshness warning, conditional on
    age), this is unconditional -- shown on every page, every run. Borrows
    that function's visual pattern (a `glass`-styled inline note) for
    consistency rather than introducing a second banner style.
    """
    return (
        '<div class="glass" style="padding: 0.75rem 1rem; margin-bottom: 1rem; '
        'font-size: 0.85rem;">'
        "&#9432; This is a personal, automated research project, not "
        "investment advice. Nothing on this site is a recommendation to "
        "buy or sell any security &mdash; it publishes the output of one "
        "rules-based paper-trading strategy for its own record."
        "</div>"
    )


def _progress_polling_script() -> str:
    """JS polling progress.json for a live progress bar (M13, user request).

    index.html is generated once and doesn't change while a screen is
    running -- this polls a SEPARATE file (progress.json, written
    directly by data.py on every ticker completion, not regenerated by
    report.py) so the static page can still reflect a live-in-progress
    run without needing report.py to be re-run. Fails quietly (hides the
    banner) if the file is missing or malformed -- no run in progress is
    the common, expected case, not an error.
    """
    return """
<script>
async function pollProgress() {
  const banner = document.getElementById('progress-banner');
  try {
    const res = await fetch('progress.json', {cache: 'no-store'});
    if (!res.ok) throw new Error('no progress file');
    const data = await res.json();
    if (data.total > 0 && data.completed < data.total) {
      banner.classList.add('active');
      const pct = Math.round((data.completed / data.total) * 100);
      document.getElementById('progress-bar-fill').style.width = pct + '%';
      const current = (data.in_flight && data.in_flight.length > 0) ? data.in_flight[0] : '';
      // User request (2026-07-23): make it explicit in the UI when
      // missing/slow data is because every worker is waiting out a
      // shared yfinance rate-limit cooldown, not because the data
      // doesn't exist -- this is the single biggest cause of tickers
      // showing data_missing:fetch_failed on tickers.html.
      const rateLimited = data.rate_limited_until && data.now && data.rate_limited_until > data.now;
      if (rateLimited) {
        const waitSecs = Math.max(0, Math.round(data.rate_limited_until - data.now));
        document.getElementById('progress-phase').textContent =
          `Screening in progress: ${data.phase} \\u2014 paused, waiting out a shared ` +
          `yfinance rate limit (~${waitSecs}s)`;
        document.getElementById('progress-detail').textContent =
          `${data.completed}/${data.total} tickers (${pct}%) \\u2014 ` +
          `this is expected under yfinance's rate limiting, not a permanent gap; ` +
          `slowly gathering the rest as the limit clears.`;
      } else {
        document.getElementById('progress-phase').textContent =
          `Screening in progress: ${data.phase}`;
        document.getElementById('progress-detail').textContent =
          `${data.completed}/${data.total} tickers (${pct}%)` +
          (current ? ` \\u2014 currently checking: ${current}` : '');
      }
    } else {
      banner.classList.remove('active');
    }
  } catch (e) {
    banner.classList.remove('active');
  }
}
pollProgress();
setInterval(pollProgress, 2000);
</script>
"""


def _render_candidate(metrics_row: pd.Series) -> str:
    """A buyable-candidate card from the latest screen (not a held position).

    Same expandable metrics layout as `_render_pick`, but honestly labeled:
    it deliberately does not render "Reason/Bought/Notional", which would
    imply a position the screen-only path never took.
    """
    raw_symbol = str(metrics_row["symbol"])
    symbol = html.escape(raw_symbol)
    score_str = (
        f"{float(metrics_row['score']):.1f}" if _is_numeric(metrics_row.get("score")) else "—"
    )
    metrics_html = _render_metrics_table(metrics_row)
    badges_html = _render_badges(metrics_row)
    return f"""<details class="pick glass">
  <summary><span class="symbol">{symbol}</span>
    <span class="score">score {score_str}</span></summary>
  <div class="pick-body">
    {f'<div class="badges">{badges_html}</div>' if badges_html else ""}
    <p><strong>Status:</strong> buyable in the latest screen &mdash; not a held position</p>
    {metrics_html}
    {_render_research_links(raw_symbol)}
  </div>
</details>"""


def _buyable_sorted(results: pd.DataFrame) -> pd.DataFrame:
    """Buyable rows from a screen result, ranked by score (highest first).

    Shared by `_render_candidates_or_empty` and the export-button data
    (`_export_rows`) so "what's shown on the page" and "what a Copy
    JSON/Export CSV click produces" can never silently diverge.
    """
    if len(results) == 0 or "buyable" not in results.columns:
        return pd.DataFrame()
    buyable = results[results["buyable"].apply(_is_buyable)]
    if "score" in buyable.columns:
        buyable = buyable.sort_values("score", ascending=False)
    return buyable


def _render_candidates_or_empty(results: pd.DataFrame) -> str:
    """Home-page body when no positions are currently held.

    Scopes the page to what's actually known (staff-engineer-reviewer):
    rather than a bare "No current picks yet", show the latest screen's
    buyable candidates, clearly marked as screen output, not holdings.
    Falls back to the original empty state (exact string preserved) when
    the screen found nothing buyable. Deliberately doesn't claim *why*
    nothing is held -- that's true both for a screen-only deployment with
    no trade journal at all, and for a trading deployment mid-cycle between
    a full liquidation and the next buy (which does have a journal, just no
    currently open position) -- the candidate label is accurate either way.
    """
    buyable = _buyable_sorted(results)
    if len(buyable) == 0:
        return '<div class="glass"><p class="empty">No current picks yet.</p></div>'
    intro = (
        '<div class="glass" style="padding: 0.75rem 1rem; margin-bottom: 1rem; '
        'font-size: 0.9rem;">No positions are currently held. Showing the latest '
        f"screen&rsquo;s <strong>{len(buyable)}</strong> buyable candidate(s), ranked "
        "by score &mdash; these passed every gate but are screen output, not holdings."
        "</div>"
    )
    cards = "\n".join(_render_candidate(row) for _, row in buyable.iterrows())
    return f"{intro}\n{cards}"


def _json_safe_metric(value: object) -> float | None:
    """A metric value coerced to a plain float for JSON, or None.

    Mirrors `_is_numeric`'s definition of "real data" (excludes NaN) so
    the exported JSON/CSV agrees with what the page itself renders as
    "—"/missing -- raw numpy float64 isn't JSON-serializable as-is,
    so this can't just be `metrics_row.get(key)`.
    """
    return float(value) if _is_numeric(value) else None


def _export_rows(picks: list[dict[str, object]], results: pd.DataFrame) -> list[dict[str, object]]:
    """The data behind the Copy JSON / Export CSV buttons (user request).

    Deliberately mirrors _render_index's own picks-vs-candidates branch
    (same `if picks: ... else: _buyable_sorted(...)` split) so an export
    can never show something different from what's actually on the page
    at the moment it's clicked.
    """
    rows: list[dict[str, object]] = []
    if picks:
        for p in picks:
            symbol = str(p["symbol"])
            matches = results[results["symbol"] == symbol]
            metrics_row = matches.iloc[0] if len(matches) > 0 else None
            score = _json_safe_metric(metrics_row["score"]) if metrics_row is not None else None
            row: dict[str, object] = {
                "symbol": symbol,
                "status": "held",
                "score": score,
                "reason": str(p["reason"]) if p.get("reason") is not None else None,
                "bought_at": str(p["timestamp"]) if p.get("timestamp") is not None else None,
                "notional": _json_safe_metric(p.get("notional")),
            }
            for key in _METRIC_LABELS:
                metric_value = metrics_row.get(key) if metrics_row is not None else None
                row[key] = _json_safe_metric(metric_value)
            rows.append(row)
        return rows
    for _, metrics_row in _buyable_sorted(results).iterrows():
        row = {
            "symbol": str(metrics_row["symbol"]),
            "status": "buyable candidate",
            "score": _json_safe_metric(metrics_row.get("score")),
            "reason": None,
            "bought_at": None,
            "notional": None,
        }
        for key in _METRIC_LABELS:
            row[key] = _json_safe_metric(metrics_row.get(key))
        rows.append(row)
    return rows


def _render_export_controls(rows: list[dict[str, object]]) -> str:
    """Copy-JSON / Export-CSV buttons near the picks header (user request).

    The row data is embedded as a `<script type="application/json">` --
    inert to the HTML/JS parser (unlike a plain `<script>` block, this
    type never executes), so no escaping beyond what `json.dumps` already
    does is needed to make it safe to embed. Both buttons then just read
    that same embedded data client-side; nothing is re-fetched or
    re-derived, so the export can never disagree with what's on the page.
    """
    if not rows:
        return ""
    # A reason string (bot-generated today, but not something this
    # rendering layer should trust) could contain "</script>" -- the HTML
    # parser closes the tag right there regardless of type="application/
    # json" not executing as script, which can inject real markup after
    # it. Escaping "<" as its JSON unicode form is the standard fix (valid
    # JSON either way; JSON.parse reads < back as "<") -- confirmed
    # by this module's own test_generate_report_escapes_html_in_reason_
    # strings, which failed against the unescaped version.
    data_json = json.dumps(rows).replace("<", "\\u003c")
    return f"""<div class="export-controls">
  <script type="application/json" id="picks-data">{data_json}</script>
  <button type="button" id="copy-json-btn">Copy JSON</button>
  <button type="button" id="export-csv-btn">Export CSV</button>
  <span class="export-status" id="export-status" aria-live="polite"></span>
</div>
<script>
(function() {{
  const rows = JSON.parse(document.getElementById('picks-data').textContent);
  const status = document.getElementById('export-status');
  function flash(msg) {{
    status.textContent = msg;
    setTimeout(() => {{ status.textContent = ''; }}, 2500);
  }}
  document.getElementById('copy-json-btn').addEventListener('click', async () => {{
    try {{
      await navigator.clipboard.writeText(JSON.stringify(rows, null, 2));
      flash('Copied ' + rows.length + ' row(s) to clipboard.');
    }} catch (e) {{
      flash('Copy failed -- clipboard access was denied.');
    }}
  }});
  document.getElementById('export-csv-btn').addEventListener('click', () => {{
    const cols = Object.keys(rows[0]);
    const escape = (v) => {{
      if (v === null || v === undefined) return '';
      const s = String(v);
      return /[",\\r\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }};
    const lines = [cols.join(',')].concat(
      rows.map((r) => cols.map((c) => escape(r[c])).join(','))
    );
    const blob = new Blob([lines.join('\\n')], {{type: 'text/csv'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'munger_picks.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }});
}})();
</script>"""


def _render_index(picks: list[dict[str, object]], results: pd.DataFrame) -> str:
    if picks:
        body = "\n".join(_render_pick(str(p["symbol"]), p, results) for p in picks)
    else:
        body = _render_candidates_or_empty(results)
    export_controls = _render_export_controls(_export_rows(picks, results))
    head = _seo_head(
        "Munger Screener &mdash; current picks",
        "Daily quality-value stock screen: today's candidate picks with the metrics and "
        "reasoning behind each score. Paper-trading research, not investment advice.",
        "index.html",
    )
    return f"""{head}
<body>
<h1>Current picks</h1>
<nav><a href="tickers.html">See all screened tickers &rarr;</a>
{_history_nav_link()}
<a href="pnl.html">Paper trading P&amp;L &rarr;</a></nav>
{_disclaimer_banner()}
{_render_methodology_drawer()}
<div class="progress-banner glass" id="progress-banner">
  <div class="phase" id="progress-phase"></div>
  <div class="progress-bar-track"><div class="progress-bar-fill" id="progress-bar-fill"></div></div>
  <div class="progress-detail" id="progress-detail"></div>
</div>
{_generated_at()}
{export_controls}
{body}
{_progress_polling_script()}
</body></html>
"""


def _render_tickers(results: pd.DataFrame, held_symbols: set[str]) -> str:
    other = results[~results["symbol"].isin(held_symbols)].sort_values("score", ascending=False)
    metric_cols = [c for c in _METRIC_LABELS if c in other.columns]
    header_cols = ["symbol", "buyable", "score", "fail_reasons", *metric_cols]
    labels = {
        "symbol": "Symbol",
        "buyable": "Buyable",
        "score": "Score",
        "fail_reasons": "Fail reasons",
    }
    labels.update(_METRIC_LABELS)

    def _header_cell(i: int, col: str) -> str:
        label = html.escape(labels[col])
        if col not in _METRIC_TOOLTIPS:
            return f'<th data-col="{i}">{label}</th>'
        tooltip = html.escape(_METRIC_TOOLTIPS[col])
        return f'<th data-col="{i}" title="{tooltip}">{label}</th>'

    header_html = "".join(_header_cell(i, c) for i, c in enumerate(header_cols))

    rows_html = []
    any_fetch_failed = False
    for _, row in other.iterrows():
        css_class = "" if _is_buyable(row["buyable"]) else "not-buyable"
        cells = []
        for col in header_cols:
            if col in metric_cols or col == "score":
                cells.append(_metric_cell(col, row.get(col)))
            elif col == "fail_reasons":
                raw_reasons = row.get("fail_reasons")
                # A ticker that passes every gate has an empty fail_reasons
                # string, but the CSV round-trip (write "" -> read back)
                # turns that into a pandas NaN, not "" -- `NaN or ""` is
                # still NaN (NaN is truthy), so a naive `str(x or "")`
                # rendered the literal text "nan" in the table for every
                # passing ticker. Must check isna() explicitly.
                reasons = "" if pd.isna(raw_reasons) else str(raw_reasons)
                if "fetch_failed" in reasons:
                    any_fetch_failed = True
                # Each comma-separated token gets its own hover tooltip
                # (user request: a code like "graham_current_ratio" doesn't
                # explain itself) rather than one title on the whole cell.
                tokens = [t for t in reasons.split(",") if t]
                spans = ", ".join(
                    f'<span title="{html.escape(_fail_reason_tooltip(t))}">{html.escape(t)}</span>'
                    for t in tokens
                )
                cells.append(f"<td>{spans}</td>")
            else:
                cells.append(f"<td>{html.escape(str(row[col]))}</td>")
        rows_html.append(f'<tr class="{css_class}">{"".join(cells)}</tr>')

    score_buyable_note = (
        '<div class="glass" style="padding: 0.75rem 1rem; margin-bottom: 1rem; '
        'font-size: 0.85rem;">'
        "&#9432; Score and Buyable are independent: Score ranks quality across "
        "every screened ticker, while Buyable requires passing <em>every</em> "
        "Graham/Munger gate. A high score can still be non-buyable &mdash; "
        "hover a Fail reasons code to see which gate it failed and why."
        "</div>"
    )

    fetch_failed_note = (
        '<div class="glass" style="padding: 0.75rem 1rem; margin-bottom: 1rem; '
        'font-size: 0.85rem;">'
        "&#9432; Some rows show <code>data_missing:fetch_failed</code> &mdash; this means "
        "yfinance's shared rate limit was hit during that run and the fetch didn't complete "
        "in time, not that the data doesn't exist. It's usually recoverable on the next run."
        "</div>"
        if any_fetch_failed
        else ""
    )

    script = """
<script>
const input = document.getElementById('filter');
const table = document.getElementById('tickers');
const rows = Array.from(table.tBodies[0].rows);
input.addEventListener('input', () => {
  const q = input.value.trim().toUpperCase();
  for (const r of rows) {
    r.style.display = r.cells[0].textContent.toUpperCase().includes(q) ? '' : 'none';
  }
});
let sortDir = {};
for (const th of table.tHead.rows[0].cells) {
  th.addEventListener('click', () => {
    const col = parseInt(th.dataset.col, 10);
    const dir = sortDir[col] = !sortDir[col];
    const body = table.tBodies[0];
    const sorted = rows.slice().sort((a, b) => {
      const ac = a.cells[col], bc = b.cells[col];
      // data-sort carries the raw numeric value for columns whose
      // displayed text isn't directly parseable (e.g. "$2.50B") --
      // prefer it over parsing the formatted text.
      const av = ac.dataset.sort ?? ac.textContent, bv = bc.dataset.sort ?? bc.textContent;
      const an = parseFloat(av), bn = parseFloat(bv);
      const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv);
      return dir ? cmp : -cmp;
    });
    for (const r of sorted) body.appendChild(r);
  });
}
</script>
"""

    head = _seo_head(
        "Munger Screener &mdash; all screened tickers",
        "Every ticker the daily quality-value screen evaluated, in a sortable, filterable "
        "table: see why any stock was or wasn't picked.",
        "tickers.html",
    )
    return f"""{head}
<body>
<h1>All screened tickers</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
{_history_nav_link()}
<a href="pnl.html">Paper trading P&amp;L &rarr;</a></nav>
{_disclaimer_banner()}
{_generated_at()}
{score_buyable_note}
{fetch_failed_note}
<input id="filter" type="text" placeholder="Filter by symbol&hellip;">
<div class="glass table-wrap">
<table class="tickers" id="tickers">
<thead><tr>{header_html}</tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
</div>
{script}
</body></html>
"""


_CALENDAR_MONTHS_SHOWN = 3


def _load_daily_summaries() -> dict[datetime.date, dict[str, int]]:
    """One summary (total, buyable) per archived day, keyed by date.

    Reads only the `symbol`/`buyable` columns (pandas `usecols`), not the
    whole CSV -- this can scale to a year+ of daily archives without a real
    cost, unlike loading every metric column for every historical day.
    Requiring both columns also means a file that isn't a real screen-
    results archive (wrong schema) is skipped rather than miscounted.
    """
    summaries: dict[datetime.date, dict[str, int]] = {}
    if not config.SCREEN_RESULTS_ARCHIVE_DIR.exists():
        return summaries
    for path in config.SCREEN_RESULTS_ARCHIVE_DIR.glob("screen_results_*.csv"):
        date_str = path.stem.removeprefix("screen_results_")
        try:
            day = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        try:
            frame = pd.read_csv(path, usecols=["symbol", "buyable"])
        except (ValueError, KeyError):
            continue
        summaries[day] = {
            "total": len(frame),
            "buyable": sum(1 for v in frame["buyable"] if _is_buyable(v)),
        }
    return summaries


def _render_month(year: int, month: int, summaries: dict[datetime.date, dict[str, int]]) -> str:
    first_of_month = datetime.date(year, month, 1)
    # Monday-first grid, padded with blank cells so the 1st lines up
    # under the correct weekday column.
    lead_blanks = first_of_month.weekday()
    days_in_month = (
        (datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1))
        - first_of_month
    ).days

    cells = ['<div class="cal-cell cal-blank"></div>' for _ in range(lead_blanks)]
    for day_num in range(1, days_in_month + 1):
        day = datetime.date(year, month, day_num)
        summary = summaries.get(day)
        if summary is None:
            cells.append(
                f'<div class="cal-cell cal-empty"><span class="cal-day">{day_num}</span></div>'
            )
            continue
        has_buyable = summary["buyable"] > 0
        css_class = "cal-cell cal-has-data" + (" cal-has-buyable" if has_buyable else "")
        # Link within REPORT_DIR: generate_report() copies each referenced
        # archive CSV into REPORT_DIR/screen_results_archive/ so the report
        # is self-contained no matter where report/ is served as web root
        # (local nginx, k8s, or the GH archive branch) -- see _copy_archives.
        archive_href = f"screen_results_archive/screen_results_{day.isoformat()}.csv"
        cells.append(
            f'<a class="{css_class}" href="{html.escape(archive_href)}" '
            f'title="{summary["total"]} tickers, {summary["buyable"]} buyable">'
            f'<span class="cal-day">{day_num}</span>'
            f'<span class="cal-count">{summary["buyable"]}</span>'
            f"</a>"
        )

    month_name = first_of_month.strftime("%B %Y")
    weekday_headers = "".join(
        f'<div class="cal-weekday">{d}</div>' for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    )
    return f"""<div class="glass cal-month">
  <h2>{html.escape(month_name)}</h2>
  <div class="cal-grid">{weekday_headers}{"".join(cells)}</div>
</div>"""


def _pnl_class(value: float | None) -> str:
    if value is None or math.isnan(value):
        return ""
    if value > 0:
        return "pnl-gain"
    if value < 0:
        return "pnl-loss"
    return "pnl-flat"


def _format_dollars(value: object) -> str:
    """Exact dollar figure with a sign (e.g. "+$1,234.56", "-$50.00").

    Deliberately not _format_metric's B/M-scaled style -- that's tuned
    for screening metrics like market cap, not an account/position
    dollar figure where the exact number is the point.
    """
    if not _is_numeric(value):
        return "—"
    numeric = float(value)
    sign = "-" if numeric < 0 else "+" if numeric > 0 else ""
    return f"{sign}${abs(numeric):,.2f}"


def _format_pct_signed(value: object) -> str:
    if not _is_numeric(value):
        return "—"
    numeric = float(value) * 100
    sign = "-" if numeric < 0 else "+" if numeric > 0 else ""
    return f"{sign}{abs(numeric):.2f}%"


def _render_pnl_positions_table(positions: list[dict[str, object]]) -> str:
    if not positions:
        return '<p style="opacity: 0.7;">No open positions.</p>'
    header_cols = [
        "Symbol",
        "Qty",
        "Avg Entry",
        "Current Price",
        "Market Value",
        "Unrealized P&amp;L",
        "Unrealized %",
    ]
    header_html = "".join(f"<th>{c}</th>" for c in header_cols)
    rows_html = []
    for p in sorted(positions, key=lambda p: str(p.get("symbol") or "")):
        unrealized_pl = p.get("unrealized_pl")
        unrealized_plpc = p.get("unrealized_plpc")
        qty = p.get("qty")
        pl_value = float(unrealized_pl) if _is_numeric(unrealized_pl) else None
        qty_cell = f"<td>{qty:,.4f}</td>" if _is_numeric(qty) else "<td>—</td>"
        pl_pct_cell = (
            f'<td class="{_pnl_class(pl_value)}">{_format_pct_signed(unrealized_plpc)}</td>'
        )
        cells = [
            f"<td>{html.escape(str(p.get('symbol') or '—'))}</td>",
            qty_cell,
            f"<td>{_format_dollars(p.get('avg_entry_price')).lstrip('+')}</td>",
            f"<td>{_format_dollars(p.get('current_price')).lstrip('+')}</td>",
            f"<td>{_format_dollars(p.get('market_value')).lstrip('+')}</td>",
            f'<td class="{_pnl_class(pl_value)}">{_format_dollars(unrealized_pl)}</td>',
            pl_pct_cell,
        ]
        rows_html.append(f"<tr>{''.join(cells)}</tr>")
    return f"""<div class="glass table-wrap">
<table class="tickers">
<thead><tr>{header_html}</tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
</div>"""


def _pnl_staleness_note(generated_at: object) -> str:
    """A warning banner if the P&L snapshot is too old to trust.

    Also covers a missing/unparseable timestamp -- pm-reviewer finding on
    an earlier draft: that case must warn too, not silently pass, or a
    snapshot from a partial/buggy write (no valid generated_at at all)
    looks identical to a normal fresh one -- worse than a merely-old
    snapshot, since there's no age to even judge it by.
    """
    banner_body: str
    if not isinstance(generated_at, str):
        banner_body = "has no valid timestamp"
    else:
        try:
            generated = datetime.datetime.fromisoformat(generated_at)
            # staff-engineer-reviewer finding: fromisoformat happily
            # parses a string with no UTC offset into a *naive*
            # datetime without raising -- subtracting a naive value
            # from datetime.now(UTC) (aware) then raises TypeError,
            # uncaught here, which would crash the *entire* report
            # generation (not just this page) over one bad field in a
            # bridged-across-two-systems file. A missing offset is
            # exactly the same "can't confirm freshness" case as an
            # unparseable string, not a crash.
            if generated.tzinfo is None:
                raise ValueError("naive timestamp, no UTC offset")
        except ValueError:
            banner_body = "has an unparseable timestamp"
        else:
            age_hours = (datetime.datetime.now(datetime.UTC) - generated).total_seconds() / 3600
            if age_hours <= config.PNL_STALENESS_MAX_HOURS:
                return ""
            banner_body = (
                f"is {age_hours:.0f} hours old (expected within "
                f"{config.PNL_STALENESS_MAX_HOURS}h)"
            )
    return (
        '<div class="glass" style="padding: 0.75rem 1rem; margin-bottom: 1rem; '
        'font-size: 0.85rem;">'
        f"&#9432; This snapshot {banner_body} &mdash; the daily bridge from the "
        "trading job to this site may have stopped running or be misconfigured. "
        "Numbers below may not reflect the current account."
        "</div>"
    )


def _render_pnl(snapshot: dict[str, object] | None) -> str:
    """Renders pnl.html (new milestone, user request: track P&L).

    Reads a snapshot pnl.py already computed and wrote to disk -- this
    function never talks to Alpaca itself (see _load_pnl_snapshot).
    """
    mode_badge = ""
    body: str
    if snapshot is None:
        body = (
            '<div class="glass" style="padding: 1.25rem;">'
            "No P&amp;L snapshot yet. This page is populated by "
            "<code>pnl.py</code>, which runs alongside the daily trading "
            "job and needs at least one completed run before there's "
            "anything to show here."
            "</div>"
        )
    else:
        mode = str(snapshot.get("mode") or "paper")
        mode_badge = f'<span class="mode-badge">{html.escape(mode.upper())}</span>'
        account = snapshot.get("account")
        account = account if isinstance(account, dict) else {}
        equity = account.get("equity")
        last_equity = account.get("last_equity")
        cash = account.get("cash")
        today_pl = (
            float(equity) - float(last_equity)
            if _is_numeric(equity) and _is_numeric(last_equity)
            else None
        )
        today_pl_pct = (
            today_pl / float(last_equity)
            if today_pl is not None and _is_numeric(last_equity) and float(last_equity) != 0
            else None
        )
        positions_raw = snapshot.get("positions")
        positions = [p for p in positions_raw if isinstance(p, dict)] if isinstance(
            positions_raw, list
        ) else []
        total_unrealized = sum(
            float(p["unrealized_pl"])
            for p in positions
            if _is_numeric(p.get("unrealized_pl"))
        )

        tiles = f"""<div class="stat-tiles">
<div class="glass stat-tile">
  <div class="stat-label">Equity</div>
  <div class="stat-value">{_format_dollars(equity).lstrip('+')}</div>
</div>
<div class="glass stat-tile">
  <div class="stat-label">Cash</div>
  <div class="stat-value">{_format_dollars(cash).lstrip('+')}</div>
</div>
<div class="glass stat-tile">
  <div class="stat-label">Today&rsquo;s P&amp;L</div>
  <div class="stat-value {_pnl_class(today_pl)}">{_format_dollars(today_pl)}</div>
  <div class="stat-subvalue {_pnl_class(today_pl)}">{_format_pct_signed(today_pl_pct)}</div>
</div>
<div class="glass stat-tile">
  <div class="stat-label">Unrealized P&amp;L</div>
  <div class="stat-value {_pnl_class(total_unrealized)}">{_format_dollars(total_unrealized)}</div>
</div>
</div>"""

        generated_at = snapshot.get("generated_at")
        generated_note = (
            f'<p style="font-size: 0.8rem; opacity: 0.65;">Snapshot generated '
            f"{html.escape(str(generated_at))}</p>"
            if generated_at
            else ""
        )
        staleness_note = _pnl_staleness_note(generated_at)

        body = (
            staleness_note
            + tiles
            + generated_note
            + "<h2>Open positions</h2>"
            + _render_pnl_positions_table(positions)
        )

    head = _seo_head(
        "Munger Screener &mdash; P&amp;L",
        "Paper-trading profit and loss for the daily quality-value screen: account equity "
        "and open positions. Paper money only, not investment advice.",
        "pnl.html",
    )
    return f"""{head}
<body>
<h1>Paper trading P&amp;L{mode_badge}</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
<a href="tickers.html">See all screened tickers &rarr;</a>
{_history_nav_link()}</nav>
{_disclaimer_banner()}
<p style="font-size: 0.85rem; opacity: 0.7;">
  This account trades on the same daily cadence and rules as the
  screener above &mdash; paper money only, not investment advice.
</p>
{body}
{_generated_at()}
</body></html>
"""


def _render_calendar(
    summaries: dict[datetime.date, dict[str, int]] | None = None,
) -> str:
    """Renders calendar.html, the pre-M17 history view.

    Still generated on deployments without Grafana configured -- see
    generate_report.

    Args:
      summaries: Reuse an already-loaded dict rather than re-globbing/
        re-reading every archive CSV -- a caller with the summaries already
        in hand avoids re-scanning the whole (unbounded-growth) archive
        directory.
    """
    if summaries is None:
        summaries = _load_daily_summaries()
    today = datetime.date.today()
    months_html = []
    year, month = today.year, today.month
    for _ in range(_CALENDAR_MONTHS_SHOWN):
        months_html.append(_render_month(year, month, summaries))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    # Most recent month first.
    body = "\n".join(months_html)

    legend = (
        '<div class="cal-legend">'
        '<span class="cal-legend-swatch cal-has-buyable"></span> buyable names found &nbsp;'
        '<span class="cal-legend-swatch cal-has-data"></span> screened, none buyable &nbsp;'
        '<span class="cal-legend-swatch cal-empty"></span> no run that day'
        "</div>"
    )

    head = _seo_head(
        "Munger Screener &mdash; calendar",
        "A day-by-day calendar of the daily quality-value screen: which days found buyable "
        "names, and each day's full results.",
        "calendar.html",
    )
    return f"""{head}
<body>
<h1>Daily screen calendar</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
<a href="tickers.html">See all screened tickers &rarr;</a>
<a href="pnl.html">Paper trading P&amp;L &rarr;</a></nav>
{_disclaimer_banner()}
{_generated_at()}
<p style="font-size: 0.85rem; opacity: 0.75; margin-bottom: 1rem;">
  Each day's screen is informational only &mdash; daily_screen.py never places orders;
  trading decisions stay on the quarterly cadence. Click a day to see its full results.
</p>
{legend}
{body}
</body></html>
"""


def _copy_archives_into_report() -> None:
    """Mirror the daily archive CSVs into REPORT_DIR/screen_results_archive/.

    The calendar links to these files with a report-relative path, so they
    must live under REPORT_DIR itself -- otherwise a browser serving report/
    as its web root (local nginx, the k8s deployment, or the GH archive
    branch) cannot reach the real archive dir, which is REPORT_DIR's sibling
    under BASE_DIR. Copying keeps the whole report self-contained.
    """
    if not config.SCREEN_RESULTS_ARCHIVE_DIR.exists():
        return
    dest_dir = config.REPORT_DIR / "screen_results_archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in config.SCREEN_RESULTS_ARCHIVE_DIR.glob("screen_results_*.csv"):
        dest = dest_dir / src.name
        # Incremental: past days' archives are immutable once written, so
        # only copy a file that's new or changed (today's dated archive can
        # be overwritten by a same-day re-run -> newer mtime/different size).
        # This keeps the per-run copy O(new files), not O(all history), and
        # avoids needlessly rewriting a file nginx may be serving.
        src_stat = src.stat()
        if dest.exists():
            dest_stat = dest.stat()
            if dest_stat.st_size == src_stat.st_size and dest_stat.st_mtime >= src_stat.st_mtime:
                continue
        # Copy to a temp file + rename (staff-engineer-reviewer), matching
        # this module's own _write_text_atomically/screener's
        # _write_csv_atomically pattern -- a crash mid-copy must not leave a
        # truncated CSV live at the name nginx is already serving.
        #
        # copyfile (content only), not copy2 -- copy2 also calls os.chmod
        # to preserve the source file's permission bits, which GCS FUSE
        # (Cloud Run's DATA_DIR volume) rejects with PermissionError:
        # [Errno 1] Operation not permitted (same real crash found in
        # journal.archive_screen_results, 2026-07-25 Cloud Run run).
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copyfile(src, tmp_dest)
        tmp_dest.replace(dest)


def _history_nav_link() -> str:
    """The "history over time" nav entry.

    The Grafana "Dashboards" tab where a Grafana URL is configured
    (config.GRAFANA_BASE_URL), otherwise the legacy "Daily calendar" page.
    M17 retires the calendar in favor of the embedded dashboards, but the
    swap is gated per environment so a deployment without Grafana yet keeps a
    history view rather than none (DESIGN_DASHBOARDS.md 4).
    """
    if config.GRAFANA_BASE_URL:
        return '<a href="dashboards.html">Dashboards &rarr;</a>'
    return '<a href="calendar.html">Daily calendar &rarr;</a>'


def _render_dashboards() -> str:
    """Renders dashboards.html -- embeds the Grafana dashboards (M17).

    Generated only when config.GRAFANA_BASE_URL is set (see generate_report);
    the nav swaps the calendar link for this tab in the same case. The
    <iframe> shows the live Grafana; the always-visible fallback link lets a
    visitor open Grafana directly if the embed can't load (e.g. the prod VM
    is down), degrading to a link rather than a blank frame
    (DESIGN_DASHBOARDS.md 4, staff-engineer-reviewer finding).
    """
    url = html.escape(config.GRAFANA_BASE_URL, quote=True)
    head = _seo_head(
        "Munger Screener &mdash; dashboards",
        "Account P&amp;L over time and daily closing prices for held positions, from the "
        "daily quality-value screen's paper-trading account.",
        "dashboards.html",
    )
    return f"""{head}
<body>
<h1>Dashboards</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
<a href="tickers.html">See all screened tickers &rarr;</a>
<a href="pnl.html">Paper trading P&amp;L &rarr;</a></nav>
{_disclaimer_banner()}
<p style="font-size: 0.85rem; opacity: 0.75;">
  Account P&amp;L over time and daily closing prices for held positions.
  If the charts don't load,
  <a href="{url}" target="_blank" rel="noopener">open them directly &rarr;</a>.
</p>
<iframe src="{url}" title="Munger Grafana dashboards"
  style="width: 100%; height: 80vh; border: 0;" loading="lazy"
  referrerpolicy="no-referrer"></iframe>
{_generated_at()}
</body></html>
"""


def _write_text_atomically(path: Path, text: str) -> None:
    """Write via temp-file + rename, matching StateTracker's pattern.

    A crash mid-write (OOM, disk-full, kill) between opening the file
    and finishing the write would otherwise leave a truncated HTML file
    on disk indefinitely -- a browser opening it later renders a
    silently cut-off page with no sign anything is wrong, and nothing
    regenerates it until the next manual run (staff-engineer-reviewer
    finding).
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text)
    tmp_path.replace(path)


def generate_report() -> None:
    """Write index.html and tickers.html to config.REPORT_DIR.

    Not synchronized with bot.py's own schedule -- report.py can be run
    standalone at any time, including while a scheduled run is mid-write
    to screen_results.csv/journal.db. Logs (rather than silently
    crashing with a bare traceback) so a concurrent-access failure at
    least leaves a trace in munger.log, consistent with how bot.py
    itself surfaces unexpected failures.
    """
    journal.configure_logging()
    try:
        results = _load_screen_results()
        picks = journal.get_holdings_detail()
        held_symbols = {str(p["symbol"]) for p in picks}

        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(config.REPORT_DIR / "index.html", _render_index(picks, results))
        _write_text_atomically(
            config.REPORT_DIR / "tickers.html", _render_tickers(results, held_symbols)
        )
        # M17 calendar->dashboards swap, gated on GRAFANA_BASE_URL: where
        # Grafana is configured, emit the embedded-dashboards tab; otherwise
        # keep generating the calendar (an environment without Grafana still
        # needs a history view). The JSON/RSS feed was retired alongside the
        # calendar (user request, M17) -- feed.json/rss.xml are no longer
        # written at all.
        if config.GRAFANA_BASE_URL:
            _write_text_atomically(
                config.REPORT_DIR / "dashboards.html", _render_dashboards()
            )
        else:
            _write_text_atomically(config.REPORT_DIR / "calendar.html", _render_calendar())
        _write_text_atomically(config.REPORT_DIR / "pnl.html", _render_pnl(_load_pnl_snapshot()))
        # SEO artifacts (M18): robots.txt always (dev emits Disallow: / to stay
        # unindexed); sitemap.xml only on prod, where SITE_BASE_URL makes its
        # absolute URLs meaningful. Both must live in REPORT_DIR to be served
        # from the web root, same as the archive CSVs below.
        _write_text_atomically(config.REPORT_DIR / "robots.txt", _render_robots_txt())
        if config.SITE_BASE_URL:
            _write_text_atomically(config.REPORT_DIR / "sitemap.xml", _render_sitemap_xml())
        _copy_archives_into_report()
    except Exception:
        logger.exception("report.py failed to generate the report")
        raise


if __name__ == "__main__":
    generate_report()
    print(f"Report written to {config.REPORT_DIR}/index.html")
