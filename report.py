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
import logging
import math
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
table.metrics { border-collapse: collapse; margin-top: 0.5rem; font-size: 0.9rem; width: 100%; }
table.metrics td { padding: 0.25rem 0.75rem 0.25rem 0; }
table.metrics td:first-child { opacity: 0.65; }

.empty { opacity: 0.7; font-style: italic; padding: 1.5rem; text-align: center; }
.generated { opacity: 0.55; font-size: 0.8rem; margin-bottom: 1rem; }

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
        f"<tr><td>{html.escape(label)}</td>"
        f"<td>{_format_metric(key, metrics_row.get(key))}</td></tr>"
        for key, label in _METRIC_LABELS.items()
    )
    return f'<table class="metrics">{rows}</table>'


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

    return f"""<details class="pick glass">
  <summary><span class="symbol">{esc_symbol}</span>
    <span class="score">score {score_str}</span></summary>
  <div class="pick-body">
    <p><strong>Reason:</strong> {reason}</p>
    <p><strong>Bought:</strong> {timestamp} &middot; <strong>Notional:</strong> {notional_str}</p>
    {metrics_html}
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


def _render_index(picks: list[dict[str, object]], results: pd.DataFrame) -> str:
    if picks:
        body = "\n".join(_render_pick(str(p["symbol"]), p, results) for p in picks)
    else:
        body = '<div class="glass"><p class="empty">No current picks yet.</p></div>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Munger bot &mdash; current picks</title>
<style>{_CSS}</style></head>
<body>
<h1>Current picks</h1>
<nav><a href="tickers.html">See all screened tickers &rarr;</a></nav>
<div class="progress-banner glass" id="progress-banner">
  <div class="phase" id="progress-phase"></div>
  <div class="progress-bar-track"><div class="progress-bar-fill" id="progress-bar-fill"></div></div>
  <div class="progress-detail" id="progress-detail"></div>
</div>
{_generated_at()}
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

    header_html = "".join(
        f'<th data-col="{i}">{html.escape(labels[c])}</th>' for i, c in enumerate(header_cols)
    )

    rows_html = []
    any_fetch_failed = False
    for _, row in other.iterrows():
        css_class = "" if _is_buyable(row["buyable"]) else "not-buyable"
        cells = []
        for col in header_cols:
            if col in metric_cols or col == "score":
                cells.append(_metric_cell(col, row.get(col)))
            elif col == "fail_reasons":
                reasons = str(row.get("fail_reasons") or "")
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
<nav><a href="index.html">&larr; Back to current picks</a></nav>
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
    except Exception:
        logger.exception("report.py failed to generate the report")
        raise


if __name__ == "__main__":
    generate_report()
    print(f"Report written to {config.REPORT_DIR}/index.html")
