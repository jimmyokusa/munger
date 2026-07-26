"""Unit tests for report.py's static HTML generation."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import pandas as pd
import pytest

import config
import journal
import report


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "report")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")


def _write_screen_results(rows: str) -> None:
    header = "symbol,buyable,score,fail_reasons,market_cap,trailing_pe,return_on_equity\n"
    config.SCREEN_RESULTS_CSV_PATH.write_text(header + rows)


def test_generate_report_with_no_data_writes_empty_pages() -> None:
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    tickers_html = (config.REPORT_DIR / "tickers.html").read_text()
    assert "No current picks yet." in index_html
    assert "<table" in tickers_html


def test_index_shows_buyable_candidates_when_no_positions_are_journaled() -> None:
    # staff-engineer-reviewer / user "implement the todo": the screen-only
    # deployment has no trade journal, so the home page must show the latest
    # screen's buyable candidates (honestly labeled as not-held) instead of
    # a bare "No current picks yet".
    _write_screen_results(
        "AAPL,True,90.5,,2500000000000,28.4,1.5\n"
        "MSFT,True,88.0,,2000000000000,30.1,1.4\n"
        "XYZ,False,10.0,graham_pe,,,\n"
    )
    results = report._load_screen_results()

    html_out = report._render_index(picks=[], results=results)

    assert "No current picks yet." not in html_out
    assert "buyable candidate" in html_out
    assert "not holdings" in html_out
    assert "AAPL" in html_out and "MSFT" in html_out
    # The non-buyable name must not be surfaced as a candidate.
    assert "XYZ" not in html_out


def test_index_falls_back_to_empty_state_when_screen_finds_nothing_buyable() -> None:
    _write_screen_results("XYZ,False,10.0,graham_pe,,,\n")
    results = report._load_screen_results()

    html_out = report._render_index(picks=[], results=results)

    assert "No current picks yet." in html_out


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


def test_render_badges_shows_zero_debt_and_high_roe_as_green() -> None:
    # User request: quick-scan highlight badges. Zero debt and high ROE
    # (>= 20%) are the two strongest quality signals this system already
    # scores on (DESIGN.md 3.3) -- both can be true for the same pick.
    row = pd.Series({"debt_to_equity": 0.0, "return_on_equity": 0.25, "dividend_yield": 0.0})

    html_out = report._render_badges(row)

    assert html_out.count('class="badge badge-green"') == 2
    assert "Zero debt" in html_out
    assert "High ROE" in html_out
    assert "badge-blue" not in html_out


def test_render_badges_shows_dividend_as_blue() -> None:
    row = pd.Series({"debt_to_equity": 0.4, "return_on_equity": 0.10, "dividend_yield": 0.035})

    html_out = report._render_badges(row)

    assert 'class="badge badge-blue"' in html_out
    assert "Dividend" in html_out
    assert "badge-green" not in html_out


def test_render_badges_empty_when_nothing_qualifies() -> None:
    row = pd.Series({"debt_to_equity": 0.4, "return_on_equity": 0.10, "dividend_yield": 0.0})

    assert report._render_badges(row) == ""


def test_render_badges_does_not_award_zero_debt_for_negative_debt_to_equity() -> None:
    # Real bug (staff-engineer-reviewer): negative debt_to_equity means
    # negative book equity (liabilities exceed assets) -- a red flag, not
    # "no debt" -- and satisfies a naive "<= 0.0" check just as much as a
    # genuine zero would. screener.py's own gate/score logic already
    # guards against this exact trap; _render_badges must too.
    row = pd.Series({"debt_to_equity": -0.3, "return_on_equity": 0.10, "dividend_yield": 0.0})

    html_out = report._render_badges(row)

    assert "Zero debt" not in html_out
    assert html_out == ""


def test_render_pick_and_candidate_include_rendered_badges() -> None:
    # Integration check: the badges wiring in _render_pick/_render_candidate
    # (not just _render_badges in isolation) actually reaches the page.
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,0.30\n")
    results = report._load_screen_results()
    results["debt_to_equity"] = [0.0]
    results["dividend_yield"] = [0.0]

    pick_html = report._render_pick("AAPL", journal_row=None, results=results)
    candidate_html = report._render_candidate(results.iloc[0])

    assert 'class="badges"' in pick_html
    assert "High ROE" in pick_html
    assert 'class="badges"' in candidate_html
    assert "High ROE" in candidate_html


def test_render_badges_handles_missing_metrics_row() -> None:
    # _render_candidate/_render_pick can have no matching metrics row at
    # all (data missing that run) -- must not raise.
    assert report._render_badges(None) == ""


def test_index_page_includes_methodology_drawer() -> None:
    # User request: a collapsible "How scoring & screening works" section.
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert "How scoring &amp; screening works" in index_html
    assert "Graham entry gates" in index_html
    assert 'class="methodology glass"' in index_html


def test_metrics_table_and_tickers_header_carry_tooltip_titles() -> None:
    # User request: inline tooltips explaining each metric in a
    # Munger-style value context.
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,1.5\n")
    results = report._load_screen_results()

    pick_html = report._render_metrics_table(results.iloc[0])
    tickers_html = report._render_tickers(results, held_symbols=set())

    assert 'title="Total debt / shareholder equity' in pick_html
    assert 'title="Total market value' in tickers_html


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


def _write_archive(day: str, rows: str) -> None:
    config.SCREEN_RESULTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    header = "symbol,buyable,score,fail_reasons\n"
    (config.SCREEN_RESULTS_ARCHIVE_DIR / f"screen_results_{day}.csv").write_text(header + rows)


def test_load_daily_summaries_counts_buyable_per_day() -> None:
    _write_archive("2026-07-01", "AAPL,True,90.0,\nMSFT,False,10.0,graham_pe\n")
    _write_archive("2026-07-02", "AAPL,False,5.0,graham_pe\n")

    summaries = report._load_daily_summaries()

    assert summaries[datetime.date(2026, 7, 1)] == {"total": 2, "buyable": 1}
    assert summaries[datetime.date(2026, 7, 2)] == {"total": 1, "buyable": 0}


def test_load_daily_summaries_ignores_malformed_filenames_and_files() -> None:
    config.SCREEN_RESULTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (config.SCREEN_RESULTS_ARCHIVE_DIR / "screen_results_not-a-date.csv").write_text("junk")
    (config.SCREEN_RESULTS_ARCHIVE_DIR / "screen_results_2026-07-05.csv").write_text(
        "not,a,valid,buyable,column\n1,2,3,4,5\n"
    )

    summaries = report._load_daily_summaries()

    assert summaries == {}


def test_calendar_page_shows_a_day_with_buyable_names() -> None:
    import datetime

    today = datetime.date.today().isoformat()
    _write_archive(today, "AAPL,True,90.0,\nMSFT,False,10.0,graham_pe\n")

    report.generate_report()

    calendar_html = (config.REPORT_DIR / "calendar.html").read_text()
    assert "cal-has-buyable" in calendar_html
    assert f"screen_results_archive/screen_results_{today}.csv" in calendar_html


def test_calendar_page_never_places_orders_note_is_present() -> None:
    report.generate_report()

    calendar_html = (config.REPORT_DIR / "calendar.html").read_text()
    assert "never places orders" in calendar_html


def test_copy_archives_into_report_copies_files_and_leaves_no_tmp_artifact() -> None:
    # staff-engineer-reviewer: the new copy logic was untested. Confirm the
    # archive actually lands under REPORT_DIR (not just that the calendar
    # *links* to the expected path) and that the atomic-copy tmp file is
    # cleaned up (renamed away), not left sitting alongside the real one.
    _write_archive("2026-07-01", "AAPL,True,90.0,\n")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report._copy_archives_into_report()

    dest = config.REPORT_DIR / "screen_results_archive" / "screen_results_2026-07-01.csv"
    assert dest.exists()
    assert dest.read_text() == (config.SCREEN_RESULTS_ARCHIVE_DIR / "screen_results_2026-07-01.csv").read_text()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


def test_copy_archives_into_report_never_calls_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real bug (2026-07-25 Cloud Run crash): shutil.copy2 (unlike
    # copyfile) also calls os.chmod to preserve the source's permission
    # bits, which GCS FUSE rejects with PermissionError -- worked fine on
    # local disk/k3s's PVC, so it never surfaced there (same bug found in
    # journal.archive_screen_results). Forcing os.chmod to raise proves
    # this function no longer calls it at all.
    def _raise(*args: object, **kwargs: object) -> None:
        raise PermissionError("[Errno 1] Operation not permitted (simulated GCS FUSE)")

    monkeypatch.setattr(os, "chmod", _raise)
    _write_archive("2026-07-01", "AAPL,True,90.0,\n")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report._copy_archives_into_report()

    dest = config.REPORT_DIR / "screen_results_archive" / "screen_results_2026-07-01.csv"
    assert dest.exists()


def test_copy_archives_into_report_skips_an_unchanged_file() -> None:
    _write_archive("2026-07-01", "AAPL,True,90.0,\n")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report._copy_archives_into_report()
    dest = config.REPORT_DIR / "screen_results_archive" / "screen_results_2026-07-01.csv"
    first_copy_mtime = dest.stat().st_mtime

    # Re-running with the source unchanged must not rewrite the dest (same
    # size + not-older mtime skip condition) -- this is what keeps the copy
    # O(new files), not O(all history), on every report generation.
    report._copy_archives_into_report()

    assert dest.stat().st_mtime == first_copy_mtime


def test_copy_archives_into_report_recopies_a_changed_same_day_file() -> None:
    # A same-day re-run can overwrite today's dated archive with different
    # content -- confirm the changed-size case is actually re-copied, not
    # just skipped because the filename already exists.
    _write_archive("2026-07-01", "AAPL,True,90.0,\n")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report._copy_archives_into_report()

    _write_archive("2026-07-01", "AAPL,True,90.0,\nMSFT,False,10.0,graham_pe\n")
    report._copy_archives_into_report()

    dest = config.REPORT_DIR / "screen_results_archive" / "screen_results_2026-07-01.csv"
    assert "MSFT" in dest.read_text()
