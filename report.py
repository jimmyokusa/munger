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


def _load_screen_results() -> pd.DataFrame:
    if not config.SCREEN_RESULTS_CSV_PATH.exists():
        return pd.DataFrame(columns=["symbol", "buyable", "score", "fail_reasons"])
    return pd.read_csv(config.SCREEN_RESULTS_CSV_PATH)


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
    portfolio's position limits. Full detail:
    <a href="https://github.com/jimmyokusa/munger/blob/main/DESIGN.md#33-screener"
      >DESIGN.md &sect;3.3</a>.</p>
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
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Munger bot &mdash; current picks</title>
<link rel="alternate" type="application/feed+json" title="Munger daily candidates" href="feed.json">
<link rel="alternate" type="application/rss+xml" title="Munger daily candidates" href="rss.xml">
<style>{_CSS}</style></head>
<body>
<h1>Current picks</h1>
<nav><a href="tickers.html">See all screened tickers &rarr;</a>
<a href="calendar.html">Daily calendar &rarr;</a></nav>
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
                    title = (
                        "yfinance rate-limiting during this fetch, not permanently "
                        "missing data -- see the note above the table"
                    )
                    cells.append(f'<td title="{html.escape(title)}">{html.escape(reasons)}</td>')
                else:
                    cells.append(f"<td>{html.escape(reasons)}</td>")
            else:
                cells.append(f"<td>{html.escape(str(row[col]))}</td>")
        rows_html.append(f'<tr class="{css_class}">{"".join(cells)}</tr>')

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

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Munger bot &mdash; all screened tickers</title>
<style>{_CSS}</style></head>
<body>
<h1>All screened tickers</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
<a href="calendar.html">Daily calendar &rarr;</a></nav>
{_generated_at()}
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


def _render_calendar(
    summaries: dict[datetime.date, dict[str, int]] | None = None,
) -> str:
    """Renders calendar.html.

    Pass `summaries` to reuse an already-loaded dict (generate_report()
    loads it once and shares it with the feed renderers) rather than
    re-globbing/re-reading every archive CSV per caller --
    staff-engineer-reviewer finding on the first draft, which had
    calendar.html, feed.json, and rss.xml each independently re-scanning
    the whole (unbounded-growth) archive directory.
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

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Munger bot &mdash; calendar</title>
<style>{_CSS}</style></head>
<body>
<h1>Daily screen calendar</h1>
<nav><a href="index.html">&larr; Back to current picks</a>
<a href="tickers.html">See all screened tickers &rarr;</a></nav>
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


def _recent_daily_feed_entries(
    summaries: dict[datetime.date, dict[str, int]] | None = None,
) -> list[tuple[datetime.date, list[str]]]:
    """Up to config.FEED_MAX_ITEMS most recent archived days, newest first.

    One (day, buyable_symbols) pair per day -- a subscriber wants to see
    *which* names newly qualified, not just the count
    `_load_daily_summaries` returns. Pass `summaries` to reuse an
    already-loaded dict (see `_render_calendar`'s docstring) instead of
    re-scanning the whole archive directory; reads each day's archive CSV
    with the same narrow `usecols` as `_load_daily_summaries` for the same
    reason: this can scale to a year+ of history without loading full
    metric columns for days that will be immediately discarded by the
    FEED_MAX_ITEMS cap.
    """
    if summaries is None:
        summaries = _load_daily_summaries()
    recent_days = sorted(summaries, reverse=True)[: config.FEED_MAX_ITEMS]
    entries: list[tuple[datetime.date, list[str]]] = []
    for day in recent_days:
        archive_path = config.SCREEN_RESULTS_ARCHIVE_DIR / f"screen_results_{day.isoformat()}.csv"
        try:
            frame = pd.read_csv(archive_path, usecols=["symbol", "buyable", "score"])
        except (OSError, ValueError, KeyError):
            # _load_daily_summaries already read this same file successfully
            # (with a 2-column subset) to know `day` belongs in `summaries`
            # at all -- a failure here means something changed since (the
            # file lost its `score` column, was deleted, or a concurrent
            # write raced this read). Skip the day rather than fabricate a
            # "0 buyable" entry indistinguishable from a real empty-but-
            # valid screen day -- staff-engineer-reviewer finding: silently
            # coercing "unreadable" to "empty" would conflate the two for
            # every feed subscriber, with no way to tell them apart.
            continue
        symbols: list[str] = []
        if len(frame) > 0:
            buyable = frame[frame["buyable"].apply(_is_buyable)].sort_values(
                "score", ascending=False
            )
            symbols = [str(s) for s in buyable["symbol"]]
        entries.append((day, symbols))
    return entries


def _feed_base_url() -> str:
    """Absolute origin for feed links, or "." if none is configured.

    "." (not "") so a relative href built as f"{base}/x.html" still forms
    a valid relative reference ("./x.html") instead of an absolute-looking
    "/x.html" that would resolve against the domain root rather than
    wherever the feed itself happens to be served from.
    """
    return config.REPORT_BASE_URL.rstrip("/") if config.REPORT_BASE_URL else "."


def _render_feed_json(entries: list[tuple[datetime.date, list[str]]] | None = None) -> str:
    """feed.json in JSON Feed 1.1 format (https://jsonfeed.org/version/1.1).

    One item per recently-archived day (see _recent_daily_feed_entries),
    so subscribing surfaces new candidate drops as they're archived --
    not a snapshot of today's picks alone, which a feed reader would only
    ever show once and then never update meaningfully. Pass `entries` to
    reuse an already-computed list (generate_report() computes it once
    and shares it with _render_feed_rss) instead of each format
    independently re-reading the same archive CSVs.
    """
    if entries is None:
        entries = _recent_daily_feed_entries()
    base = _feed_base_url()
    items = []
    for day, symbols in entries:
        summary = (
            f"{len(symbols)} buyable candidate(s): {', '.join(symbols)}"
            if symbols
            else "No buyable candidates this run."
        )
        items.append(
            {
                "id": f"munger-daily-{day.isoformat()}",
                "url": f"{base}/calendar.html",
                "title": f"{day.isoformat()}: {len(symbols)} buyable candidate(s)",
                "content_text": summary,
                "date_published": f"{day.isoformat()}T00:00:00Z",
            }
        )
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Munger bot — daily candidates",
        "home_page_url": f"{base}/index.html",
        "feed_url": f"{base}/feed.json",
        "description": "New buyable candidates from each archived daily screen.",
        "items": items,
    }
    return json.dumps(feed, indent=2)


def _render_feed_rss(entries: list[tuple[datetime.date, list[str]]] | None = None) -> str:
    """rss.xml, the same items as feed.json in RSS 2.0 format.

    Two formats rather than one because feed readers split roughly evenly
    on which they support -- both are generated from the same
    _recent_daily_feed_entries() so they can't drift apart in content.
    Pass `entries` for the same reuse reason as `_render_feed_json`.
    """
    if entries is None:
        entries = _recent_daily_feed_entries()
    base = _feed_base_url()
    items_xml = []
    for day, symbols in entries:
        summary = (
            f"{len(symbols)} buyable candidate(s): {html.escape(', '.join(symbols))}"
            if symbols
            else "No buyable candidates this run."
        )
        pub_date = datetime.datetime.combine(
            day, datetime.time.min, tzinfo=datetime.UTC
        ).strftime("%a, %d %b %Y %H:%M:%S %z")
        items_xml.append(
            f"<item><title>{html.escape(day.isoformat())}: {len(symbols)} buyable "
            f"candidate(s)</title><link>{html.escape(base)}/calendar.html</link>"
            f"<guid isPermaLink=\"false\">munger-daily-{day.isoformat()}</guid>"
            f"<pubDate>{pub_date}</pubDate><description>{summary}</description></item>"
        )
    # &mdash; (an HTML5 named entity) is NOT valid in bare XML -- only
    # &amp;/&lt;/&gt;/&quot;/&apos; and numeric entities are, and this
    # document has no DOCTYPE to declare it. A strict XML parser (many
    # feed readers, xml.etree.ElementTree) rejects the whole file on an
    # undefined-entity error. &#8212; is the numeric (always-valid) form
    # of the same em dash character -- real bug, staff-engineer-reviewer
    # finding, caught because the existing test only substring-checked
    # the output instead of parsing it as XML.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Munger bot &#8212; daily candidates</title>
<link>{html.escape(base)}/index.html</link>
<description>New buyable candidates from each archived daily screen.</description>
{"".join(items_xml)}
</channel></rss>
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
        # Loaded once and shared across calendar.html/feed.json/rss.xml --
        # each independently re-globbing and re-reading every archive CSV
        # (an unbounded, ever-growing directory) on every single
        # generate_report() call was a real, avoidable scaling cost
        # (staff-engineer-reviewer finding).
        daily_summaries = _load_daily_summaries()
        feed_entries = _recent_daily_feed_entries(daily_summaries)
        _write_text_atomically(
            config.REPORT_DIR / "calendar.html", _render_calendar(daily_summaries)
        )
        _write_text_atomically(
            config.REPORT_DIR / "feed.json", _render_feed_json(feed_entries)
        )
        _write_text_atomically(config.REPORT_DIR / "rss.xml", _render_feed_rss(feed_entries))
        _copy_archives_into_report()
    except Exception:
        logger.exception("report.py failed to generate the report")
        raise


if __name__ == "__main__":
    generate_report()
    print(f"Report written to {config.REPORT_DIR}/index.html")
