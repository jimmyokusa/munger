"""Unit tests for report.py's static HTML generation."""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import journal
import report


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "report")


def _write_screen_results(rows: str) -> None:
    header = "symbol,buyable,score,fail_reasons,market_cap,trailing_pe,return_on_equity\n"
    config.SCREEN_RESULTS_CSV_PATH.write_text(header + rows)


def test_generate_report_with_no_data_writes_empty_pages() -> None:
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    tickers_html = (config.REPORT_DIR / "tickers.html").read_text()
    assert "No current picks yet." in index_html
    assert "<table" in tickers_html


def test_index_page_includes_a_live_progress_banner_and_polling_script() -> None:
    # User request: a progress bar showing which ticker it's working on
    # while a screen is running. index.html is generated once, so the
    # banner markup + polling JS must always be present (hidden by CSS
    # until progress.json shows an in-progress batch) -- it can't depend
    # on report.py knowing at generation time whether a run is active.
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert 'id="progress-banner"' in index_html
    assert 'id="progress-bar-fill"' in index_html
    assert "fetch('progress.json'" in index_html
    assert "setInterval(pollProgress" in index_html
    # User request (2026-07-23): the live view must say explicitly when
    # missing data is due to a shared rate-limit cooldown, not a
    # permanent gap.
    assert "rate_limited_until" in index_html
    assert "waiting out a shared" in index_html


def test_tickers_page_shows_a_note_when_any_row_has_fetch_failed() -> None:
    # User request (2026-07-23): data_missing:fetch_failed must read as
    # "yfinance rate-limited this run," not "this data doesn't exist."
    _write_screen_results(
        "AAPL,True,90.5,,2500000000.0,28.4,1.5\nXYZ,False,0.0,data_missing:fetch_failed,,,\n"
    )
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert "not that the data doesn't exist" in html_out
    assert 'title="yfinance rate-limiting' in html_out


def test_tickers_page_omits_the_fetch_failed_note_when_no_row_has_it() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000.0,28.4,1.5\n")
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert "fetch_failed" not in html_out


def test_generate_report_shows_a_pick_with_its_reason_and_metrics() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,1.5\n")
    journal.record_order("AAPL", "buy", "NEW_POSITION score=90.5", notional=1000.0)

    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert "AAPL" in index_html
    assert "NEW_POSITION score=90.5" in index_html
    assert "$1,000.00" in index_html
    assert "90.5" in index_html
    assert "$2500.00B" in index_html  # no dedicated trillion tier, just a large $B figure


def test_generate_report_excludes_held_tickers_from_the_other_tickers_page() -> None:
    _write_screen_results(
        "AAPL,True,90.5,,2500000000000,28.4,1.5\nXYZ,False,10.0,graham_pe,500000000,50.0,0.05\n"
    )
    journal.record_order("AAPL", "buy", "NEW_POSITION score=90.5", notional=1000.0)

    report.generate_report()

    tickers_html = (config.REPORT_DIR / "tickers.html").read_text()
    assert "XYZ" in tickers_html
    assert "AAPL" not in tickers_html  # held tickers only appear on index.html


def test_generate_report_escapes_html_in_reason_strings() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,1.5\n")
    journal.record_order("AAPL", "buy", "<script>alert(1)</script>", notional=1000.0)

    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert "<script>alert(1)</script>" not in index_html
    assert "&lt;script&gt;" in index_html


def test_format_metric_handles_missing_and_nan_values() -> None:
    assert report._format_metric("market_cap", None) == "—"
    assert report._format_metric("market_cap", float("nan")) == "—"


def test_format_metric_formats_dollar_and_percent_metrics() -> None:
    assert report._format_metric("market_cap", 2_500_000_000.0) == "$2.50B"
    assert report._format_metric("free_cash_flow", 500_000.0) == "$0.5M"
    assert report._format_metric("return_on_equity", 0.185) == "18.5%"
    assert report._format_metric("consecutive_positive_earnings_years", 4.0) == "4"


def test_is_buyable_handles_real_bools_and_pandas_object_dtype_strings() -> None:
    # Staff-engineer-reviewer finding: a column with any missing/blank
    # cell can't stay bool dtype in pandas, so clean cells fall back to
    # the literal strings "True"/"False" -- bool("False") is True (a
    # non-empty string), which would have shown a row whose own text
    # says "False" as buyable and un-dimmed.
    assert report._is_buyable(True) is True
    assert report._is_buyable(False) is False
    assert report._is_buyable("True") is True
    assert report._is_buyable("False") is False
    assert report._is_buyable(float("nan")) is False
    assert report._is_buyable(None) is False


def test_tickers_page_marks_a_string_valued_false_row_as_not_buyable(
    tmp_path: Path,
) -> None:
    # Reproduces the object-dtype scenario directly: a blank cell
    # anywhere in the buyable column forces the whole column to object
    # dtype, so even the clean rows arrive as literal strings.
    csv_path = tmp_path / "screen_results.csv"
    csv_path.write_text(
        "symbol,buyable,score,fail_reasons,market_cap,trailing_pe,return_on_equity\n"
        "GOOD,True,90.0,,1000000000,20.0,0.15\n"
        "BAD,False,5.0,graham_pe,1000000000,20.0,0.15\n"
        "BLANK,,0.0,data_missing:fetch_failed,,,\n"
    )
    import pandas as pd

    results = pd.read_csv(csv_path)
    assert results["buyable"].dtype == object  # confirms the fallback actually triggered

    html_out = report._render_tickers(results, held_symbols=set())

    assert '<tr class="not-buyable">' in html_out
    # BAD's row must carry the not-buyable class, not the un-dimmed default.
    bad_row_start = html_out.index("BAD")
    row_open_tag = html_out.rfind("<tr", 0, bad_row_start)
    assert 'class="not-buyable"' in html_out[row_open_tag : bad_row_start + 10]


def test_tickers_page_carries_raw_numeric_value_for_js_sorting() -> None:
    # Staff-engineer-reviewer finding: parseFloat("$2.50B") is NaN in JS,
    # so dollar-formatted columns silently sorted as text without a
    # data-sort attribute carrying the real underlying number.
    _write_screen_results("AAPL,True,90.5,,2500000000.0,28.4,1.5\n")
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert 'data-sort="2500000000.0"' in html_out
