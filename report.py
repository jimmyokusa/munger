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
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { font-size: 1.5rem; }
nav { margin-bottom: 1.5rem; }
nav a { margin-right: 1rem; }
.pick { border: 1px solid light-dark(#ddd, #444); border-radius: 8px;
  margin-bottom: 0.75rem; padding: 0.75rem 1rem; }
.pick summary { cursor: pointer; display: flex; justify-content: space-between;
  align-items: center; font-weight: 600; }
.pick summary .score { font-weight: 400; opacity: 0.7; }
.pick-body { margin-top: 0.75rem; }
table.metrics { border-collapse: collapse; margin-top: 0.5rem; font-size: 0.9rem; }
table.metrics td { padding: 0.15rem 0.75rem 0.15rem 0; }
table.metrics td:first-child { opacity: 0.7; }
.empty { opacity: 0.7; font-style: italic; }
.generated { opacity: 0.6; font-size: 0.85rem; }
#filter { padding: 0.4rem 0.6rem; width: 100%; max-width: 300px; margin-bottom: 1rem;
  box-sizing: border-box; }
table.tickers { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
table.tickers th, table.tickers td { padding: 0.4rem 0.6rem; text-align: left;
  border-bottom: 1px solid light-dark(#eee, #333); }
table.tickers th { cursor: pointer; user-select: none; position: sticky; top: 0;
  background: light-dark(#fff, #1a1a1a); }
table.tickers th:hover { opacity: 0.7; }
tr.not-buyable { opacity: 0.6; }
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

    return f"""<details class="pick">
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


def _render_index(picks: list[dict[str, object]], results: pd.DataFrame) -> str:
    if picks:
        body = "\n".join(_render_pick(str(p["symbol"]), p, results) for p in picks)
    else:
        body = '<p class="empty">No current picks yet.</p>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Munger bot &mdash; current picks</title>
<style>{_CSS}</style></head>
<body>
<h1>Current picks</h1>
<nav><a href="tickers.html">See all screened tickers &rarr;</a></nav>
{_generated_at()}
{body}
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
    for _, row in other.iterrows():
        css_class = "" if _is_buyable(row["buyable"]) else "not-buyable"
        cells = []
        for col in header_cols:
            if col in metric_cols or col == "score":
                cells.append(_metric_cell(col, row.get(col)))
            elif col == "fail_reasons":
                cells.append(f"<td>{html.escape(str(row.get('fail_reasons') or ''))}</td>")
            else:
                cells.append(f"<td>{html.escape(str(row[col]))}</td>")
        rows_html.append(f'<tr class="{css_class}">{"".join(cells)}</tr>')

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
<input id="filter" type="text" placeholder="Filter by symbol&hellip;">
<table class="tickers" id="tickers">
<thead><tr>{header_html}</tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
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
