"""Unit tests for report.py's static HTML generation."""

from __future__ import annotations

import datetime
import json
import os
import xml.etree.ElementTree as ET
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
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "pnl.json")
    monkeypatch.setattr(config, "REAL_MONEY_DATA_PATH", tmp_path / "real_money.json")
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)


def _write_screen_results(rows: str) -> None:
    header = "symbol,buyable,score,fail_reasons,market_cap,trailing_pe,return_on_equity\n"
    config.SCREEN_RESULTS_CSV_PATH.write_text(header + rows)


def test_generate_report_with_no_data_writes_empty_pages() -> None:
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    tickers_html = (config.REPORT_DIR / "tickers.html").read_text()
    pnl_html = (config.REPORT_DIR / "pnl.html").read_text()
    assert "No current picks yet." in index_html
    assert "<table" in tickers_html
    assert "No P&amp;L snapshot yet." in pnl_html


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
    assert 'title="yfinance' in html_out


def test_tickers_page_gives_each_fail_reason_its_own_tooltip() -> None:
    # User request: raw gate codes like "graham_current_ratio" don't
    # explain themselves, especially confusing on a high-scoring ticker
    # that's still non-buyable (e.g. HCI: score 97.8, buyable False,
    # fail_reasons "graham_current_ratio,graham_earnings_stability").
    _write_screen_results('HCI,False,97.8,"graham_current_ratio,graham_earnings_stability",,,\n')
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert '<span title="Current ratio below' in html_out
    assert '<span title="Fewer than Graham' in html_out
    assert "Score and Buyable are independent" in html_out


def test_tickers_page_omits_the_fetch_failed_note_when_no_row_has_it() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000.0,28.4,1.5\n")
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert "fetch_failed" not in html_out


def test_tickers_page_renders_empty_fail_reasons_as_blank_not_literal_nan() -> None:
    # Real bug found live on gramunger.com: a passing ticker's fail_reasons
    # is written to CSV as "", but pd.read_csv reads that back as NaN, and
    # `str(nan or "")` still renders "nan" (NaN is truthy) instead of "".
    _write_screen_results("AAPL,True,90.5,,2500000000.0,28.4,1.5\n")
    results = report._load_screen_results()

    html_out = report._render_tickers(results, held_symbols=set())

    assert ">nan<" not in html_out


def test_all_pages_have_a_favicon_link_and_no_generic_bot_title() -> None:
    # User request: the site had no favicon at all, and every page's
    # <title> read "Munger bot" (an internal-sounding codename) rather
    # than a public-facing name.
    report.generate_report()

    for filename in ("index.html", "tickers.html", "calendar.html"):
        html_out = (config.REPORT_DIR / filename).read_text()
        assert 'rel="icon"' in html_out
        assert "Munger bot" not in html_out
        assert "<title>Munger Screener" in html_out


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


def test_render_research_links_includes_sec_edgar_and_finviz() -> None:
    # User request: outbound research links per ticker.
    html_out = report._render_research_links("AAPL")

    assert "https://www.sec.gov/edgar/searchedgar/companysearch" in html_out
    assert "https://finviz.com/quote.ashx?t=AAPL" in html_out
    assert 'target="_blank"' in html_out
    assert 'rel="noopener noreferrer"' in html_out


def test_render_pick_and_candidate_include_research_links() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,1.5\n")
    results = report._load_screen_results()

    pick_html = report._render_pick("AAPL", journal_row=None, results=results)
    candidate_html = report._render_candidate(results.iloc[0])

    assert "https://finviz.com/quote.ashx?t=AAPL" in pick_html
    assert "https://finviz.com/quote.ashx?t=AAPL" in candidate_html


def test_export_rows_for_buyable_candidates_when_no_positions_held() -> None:
    # User request: Copy JSON / Export CSV data must mirror the page's own
    # picks-vs-candidates branch (_buyable_sorted), highest score first.
    _write_screen_results(
        "AAPL,True,90.5,,2500000000000,28.4,0.30\nMSFT,True,80.0,,2000000000000,30.0,0.25\n"
        "XYZ,False,10.0,graham_pe,,,\n"
    )
    results = report._load_screen_results()

    rows = report._export_rows(picks=[], results=results)

    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert all(r["status"] == "buyable candidate" for r in rows)
    assert rows[0]["reason"] is None
    assert rows[0]["market_cap"] == 2500000000000.0


def test_export_rows_for_held_picks_include_reason_and_notional() -> None:
    _write_screen_results("AAPL,True,90.5,,2500000000000,28.4,0.30\n")
    results = report._load_screen_results()
    picks = [
        {
            "symbol": "AAPL",
            "reason": "NEW_POSITION score=90.5",
            "timestamp": "2026-07-26T00:00:00",
            "notional": 1000.0,
        }
    ]

    rows = report._export_rows(picks, results)

    assert rows == [
        {
            "symbol": "AAPL",
            "status": "held",
            "score": 90.5,
            "reason": "NEW_POSITION score=90.5",
            "bought_at": "2026-07-26T00:00:00",
            "notional": 1000.0,
            "market_cap": 2500000000000.0,
            "trailing_pe": 28.4,
            "price_to_book": None,
            "current_ratio": None,
            "debt_to_equity": None,
            "return_on_equity": 0.30,
            "gross_margin": None,
            "operating_margin": None,
            "free_cash_flow": None,
            "dividend_yield": None,
            "consecutive_positive_earnings_years": None,
        }
    ]


def test_render_export_controls_escapes_script_closing_tag() -> None:
    # Real bug (caught by test_generate_report_escapes_html_in_reason_
    # strings failing against the first draft): a reason string containing
    # "</script>" would close the embedded <script type="application/json">
    # early, letting arbitrary markup after it render as real HTML.
    rows: list[dict[str, object]] = [
        {"symbol": "AAPL", "reason": "</script><script>alert(1)</script>"}
    ]

    html_out = report._render_export_controls(rows)

    assert "</script><script>alert(1)</script>" not in html_out
    assert "\\u003c/script>" in html_out
    assert "Copy JSON" in html_out
    assert "Export CSV" in html_out


def test_render_export_controls_empty_when_no_rows() -> None:
    assert report._render_export_controls([]) == ""


def test_index_page_includes_export_controls() -> None:
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert 'id="picks-data"' not in index_html  # no rows: nothing embedded


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

    archive_name = "screen_results_2026-07-01.csv"
    dest = config.REPORT_DIR / "screen_results_archive" / archive_name
    assert dest.exists()
    assert dest.read_text() == (config.SCREEN_RESULTS_ARCHIVE_DIR / archive_name).read_text()
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


# --- P&L page (new milestone, user request) ---


def _write_pnl_snapshot(data: dict[str, object]) -> None:
    config.PNL_DATA_PATH.write_text(json.dumps(data))


def test_load_pnl_snapshot_returns_none_when_file_is_missing() -> None:
    assert report._load_pnl_snapshot() is None


def test_load_pnl_snapshot_returns_none_and_logs_when_file_is_corrupt() -> None:
    config.PNL_DATA_PATH.write_text("not valid json{{{")
    assert report._load_pnl_snapshot() is None


def test_load_pnl_snapshot_returns_the_parsed_dict() -> None:
    _write_pnl_snapshot({"mode": "paper", "account": {"equity": 100_000.0}})
    assert report._load_pnl_snapshot() == {"mode": "paper", "account": {"equity": 100_000.0}}


def test_load_pnl_snapshot_reads_an_explicit_path(tmp_path: Path) -> None:
    # M20: real-money.html loads from config.REAL_MONEY_DATA_PATH, a
    # second, independent file -- confirms this function actually reads
    # whatever path it's given, not always config.PNL_DATA_PATH.
    other_path = tmp_path / "real_money.json"
    other_path.write_text(json.dumps({"mode": "live", "account": {"equity": 5_000.0}}))
    assert report._load_pnl_snapshot(other_path) == {
        "mode": "live",
        "account": {"equity": 5_000.0},
    }
    # The default path is untouched by this call.
    assert report._load_pnl_snapshot() is None


def test_render_pnl_shows_empty_state_when_no_snapshot() -> None:
    html_out = report._render_pnl(None)
    assert "No P&amp;L snapshot yet." in html_out
    assert "<title>Munger Screener &mdash; P&amp;L</title>" in html_out


def test_render_pnl_shows_mode_badge_and_stat_tiles() -> None:
    snapshot: dict[str, object] = {
        "mode": "paper",
        "generated_at": "2026-07-27T01:27:38+00:00",
        "account": {
            "equity": 100_400.0,
            "cash": 25_000.0,
            "last_equity": 99_500.0,
            "portfolio_value": 100_400.0,
        },
        "positions": [
            {
                "symbol": "TROW",
                "qty": 41.6667,
                "avg_entry_price": 160.0,
                "current_price": 162.0,
                "market_value": 6_750.0,
                "cost_basis": 6_666.67,
                "unrealized_pl": 83.33,
                "unrealized_plpc": 0.0125,
            }
        ],
        "history": {"timestamp": [], "equity": [], "profit_loss": [], "profit_loss_pct": []},
    }

    html_out = report._render_pnl(snapshot)

    assert '<span class="mode-badge">PAPER</span>' in html_out
    assert "$100,400.00" in html_out  # equity tile
    assert "$25,000.00" in html_out  # cash tile
    assert "+$900.00" in html_out  # today's P&L: 100,400 - 99,500
    assert "TROW" in html_out
    assert "pnl-gain" in html_out  # positive P&L gets the gain class


def test_render_pnl_colors_a_loss_distinctly_from_a_gain() -> None:
    snapshot: dict[str, object] = {
        "mode": "paper",
        "account": {"equity": 99_000.0, "cash": 25_000.0, "last_equity": 99_500.0},
        "positions": [
            {
                "symbol": "HIG",
                "qty": 10.0,
                "avg_entry_price": 100.0,
                "current_price": 90.0,
                "market_value": 900.0,
                "cost_basis": 1_000.0,
                "unrealized_pl": -100.0,
                "unrealized_plpc": -0.10,
            }
        ],
    }

    html_out = report._render_pnl(snapshot)

    assert "pnl-loss" in html_out
    assert "-$500.00" in html_out  # today's P&L: 99,000 - 99,500
    assert "-$100.00" in html_out  # position's unrealized P&L


def test_render_pnl_handles_no_open_positions() -> None:
    snapshot: dict[str, object] = {
        "mode": "paper",
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot)
    assert "No open positions." in html_out


def test_render_pnl_shows_no_staleness_warning_for_a_fresh_snapshot() -> None:
    fresh = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)).isoformat()
    snapshot: dict[str, object] = {
        "mode": "paper",
        "generated_at": fresh,
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot)
    # M19: the client-side poller's JS unconditionally embeds the same
    # banner wording (so a stale-after-load page shows identical copy
    # whether server-baked or client-refreshed) -- asserting the phrase is
    # absent from the whole page would false-fail on that dead script text.
    # The real assertion is that the *rendered* wrapper is empty.
    assert '<div id="pnl-staleness-wrap"></div>' in html_out


def test_render_pnl_warns_when_snapshot_is_stale() -> None:
    # pm-reviewer finding: without a staleness check, a broken bridge (a
    # rotated key, a stopped workflow) just leaves gramunger.com showing
    # an old snapshot that looks identical to a fresh one.
    stale = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=100)).isoformat()
    snapshot: dict[str, object] = {
        "mode": "paper",
        "generated_at": stale,
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot)
    assert "bridge from the trading job" in html_out


def test_render_pnl_warns_when_generated_at_is_missing_or_unparseable() -> None:
    # A missing/unparseable timestamp is worse than a merely-old snapshot
    # (there's no age to even judge it by) -- must warn, not silently
    # pass, or a partial/buggy write looks identical to a fresh one.
    assert "unparseable timestamp" in report._pnl_staleness_note("not-a-real-timestamp")
    assert "no valid timestamp" in report._pnl_staleness_note(None)

    snapshot: dict[str, object] = {
        "mode": "paper",
        "generated_at": "not-a-real-timestamp",
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot)
    assert "unparseable timestamp" in html_out


def test_render_pnl_treats_a_timezone_naive_generated_at_as_unparseable() -> None:
    # Real bug found in review: datetime.fromisoformat happily parses a
    # string with no UTC offset into a *naive* datetime without raising,
    # but subtracting it from datetime.now(UTC) (aware) raises TypeError
    # -- which was uncaught and would have crashed the *entire* report
    # generation (not just this page) over one bad field.
    naive_timestamp = "2026-07-27T01:27:38"  # no +00:00/Z offset
    assert "unparseable timestamp" in report._pnl_staleness_note(naive_timestamp)

    snapshot: dict[str, object] = {"mode": "paper", "generated_at": naive_timestamp}
    report._render_pnl(snapshot)  # must not raise


def test_pnl_polling_script_rejects_naive_timestamps_like_the_python_side_does() -> None:
    # staff-engineer-reviewer finding: JS's `new Date()` parses a naive
    # (offset-less) ISO string "successfully" instead of raising the way
    # _pnl_staleness_note's Python-side fromisoformat check does -- the
    # client-side poller needs its own explicit offset check to treat the
    # same string as "unparseable" the way a full page reload would, or a
    # naive generated_at would silently compute a wrong age client-side
    # instead of showing the warning banner. This can't run the JS itself
    # (no node in the arm64 CI image this suite also runs in-cluster
    # against), so it locks the guard's presence in the template source --
    # a future edit removing it would fail this test even without a JS
    # runtime to execute against.
    script = report._pnl_polling_script()
    assert r"[+-]\d{2}:\d{2}" in script  # the offset-detecting regex
    assert "!hasOffset || isNaN(parsed.getTime())" in script


def test_render_pnl_includes_client_side_ids_and_poller_for_a_real_snapshot() -> None:
    # M19: pnl.html's client-side poller targets these ids by exact name --
    # if a future edit renames/removes one without updating the JS, the
    # page would silently stop refreshing that field while looking fine at
    # generation time. Locks the ids and the poller's presence together.
    snapshot: dict[str, object] = {
        "mode": "paper",
        "generated_at": "2026-07-27T01:27:38+00:00",
        "account": {"equity": 100_000.0, "cash": 5_000.0, "last_equity": 99_500.0},
        "positions": [
            {
                "symbol": "AAPL",
                "qty": 10.0,
                "avg_entry_price": 150.0,
                "current_price": 160.0,
                "market_value": 1_600.0,
                "unrealized_pl": 100.0,
                "unrealized_plpc": 0.0667,
            }
        ],
    }

    html_out = report._render_pnl(snapshot)

    for element_id in (
        'id="pnl-tile-equity"',
        'id="pnl-tile-cash"',
        'id="pnl-tile-today-value"',
        'id="pnl-tile-today-pct"',
        'id="pnl-tile-unrealized"',
        'id="pnl-generated-note"',
        'id="pnl-staleness-wrap"',
        'id="pnl-positions"',
    ):
        assert element_id in html_out
    assert "fetch('pnl.json'" in html_out
    assert "setInterval(pollPnl," in html_out
    # the poll interval/staleness threshold are baked in from config, not
    # left as literal placeholder tokens
    assert "__PNL_POLL_INTERVAL_MS__" not in html_out
    assert "__PNL_STALENESS_MAX_HOURS__" not in html_out
    assert str(config.PNL_CLIENT_POLL_INTERVAL_SECONDS * 1000) in html_out
    assert str(config.PNL_STALENESS_MAX_HOURS) in html_out


def test_render_pnl_omits_the_poller_when_there_is_no_snapshot() -> None:
    # The empty state renders none of the tile/table ids the poller
    # targets -- including the script there would just throw on every
    # document.getElementById(...) call (guarded against in the JS, but
    # there's no reason to ship a script with nothing to do).
    html_out = report._render_pnl(None)
    assert "pollPnl" not in html_out


def test_disclaimer_banner_appears_on_every_page() -> None:
    # staff-engineer-reviewer finding: nothing previously locked the "not
    # investment advice" banner into all 5 pages -- a future refactor of
    # any one _render_* function could silently drop the call and nothing
    # would fail CI. Anchored on wording unique to the banner itself, not
    # the pre-existing "not investment advice" phrasing that also appears
    # elsewhere (SEO meta descriptions, pnl.html's own prose).
    marker = "This is a personal, automated research project"

    results = report._load_screen_results()
    assert marker in report._render_index(picks=[], results=results)
    assert marker in report._render_tickers(results, held_symbols=set())
    assert marker in report._render_pnl(None)
    # M20: real-money.html reuses _render_pnl with overridden kwargs, not
    # a separate render function -- confirms the banner survives that
    # call shape too, not just the default (paper) one.
    assert marker in report._render_pnl(None, heading="Real-money trading P&amp;L")
    assert marker in report._render_calendar(summaries={})
    assert marker in report._render_dashboards()


def test_dashboards_omits_prices_section_when_prices_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GRAFANA_PRICES_URL", "")

    html_out = report._render_dashboards()

    assert "Held-symbol daily close prices" not in html_out


def test_dashboards_shows_prices_section_when_prices_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: the Prices Grafana dashboard was
    # fully provisioned in k3s with no way for a visitor to ever reach it
    # from the site -- dashboards.html only ever embedded GRAFANA_BASE_URL
    # (the P&L dashboard). This locks in that a second, independently
    # configured URL renders its own iframe.
    monkeypatch.setattr(config, "GRAFANA_PRICES_URL", "https://grafana.example/d/munger-prices")

    html_out = report._render_dashboards()

    assert "Held-symbol daily close prices" in html_out
    assert 'src="https://grafana.example/d/munger-prices"' in html_out


def test_generate_report_writes_pnl_html_from_a_real_snapshot() -> None:
    _write_pnl_snapshot({"mode": "live", "account": {"equity": 5_000.0}, "positions": []})
    report.generate_report()
    pnl_html = (config.REPORT_DIR / "pnl.html").read_text()
    assert '<span class="mode-badge">LIVE</span>' in pnl_html


# --- M20: real-money.html (a second, parameterized _render_pnl call) --------


def test_render_pnl_default_kwargs_are_unchanged_from_pnl_htmls_original_output() -> None:
    # Every keyword argument must default to pnl.html's exact original
    # values -- this is what lets the existing call site stay untouched.
    snapshot: dict[str, object] = {
        "mode": "paper",
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot)
    assert "<h1>Paper trading P&amp;L" in html_out
    assert "paper money only, not investment advice" in html_out
    assert "<title>Munger Screener &mdash; P&amp;L</title>" in html_out
    assert "fetch('pnl.json'" in html_out


def test_render_pnl_overridden_kwargs_produce_a_distinct_live_page() -> None:
    snapshot: dict[str, object] = {"mode": "live", "account": {"equity": 5_000.0}, "positions": []}
    html_out = report._render_pnl(
        snapshot,
        expected_mode="live",
        heading="Real-money trading P&amp;L",
        account_note_html="the user's own real capital, not investment advice",
        seo_title="Munger Screener &mdash; Real-Money Trading",
        seo_description="Real-money description",
        seo_slug="real-money.html",
        snapshot_url="real_money.json",
        nav_html='<nav><a href="pnl.html">Paper trading P&amp;L &rarr;</a></nav>',
    )
    assert "<h1>Real-money trading P&amp;L" in html_out
    assert "the user's own real capital" in html_out
    assert "<title>Munger Screener &mdash; Real-Money Trading</title>" in html_out
    assert "fetch('real_money.json'" in html_out
    assert '<a href="pnl.html">Paper trading P&amp;L' in html_out
    # Proves this isn't just appending new content on top of the old --
    # the paper-specific strings are genuinely gone from this page.
    assert "Paper trading P&amp;L</h1>" not in html_out
    assert "paper money only" not in html_out


def test_render_pnl_warns_when_snapshot_mode_disagrees_with_expected_mode() -> None:
    # staff-engineer-reviewer finding (code-push review): journal.py's
    # account column (§3.4) defends against two accounts' *trade* data
    # mixing up; this is the equivalent defense at the *display* layer --
    # a snapshot uploaded to the wrong GCS path/page must not silently
    # render under the wrong mode badge with nothing to catch it.
    snapshot: dict[str, object] = {
        "mode": "paper",
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot, expected_mode="live")
    assert "reports mode PAPER" in html_out
    assert "expects LIVE" in html_out


def test_render_pnl_shows_no_mismatch_warning_when_modes_agree() -> None:
    snapshot: dict[str, object] = {
        "mode": "paper",
        "account": {"equity": 100_000.0},
        "positions": [],
    }
    html_out = report._render_pnl(snapshot, expected_mode="paper")
    assert "reports mode" not in html_out


def test_real_money_nav_link_is_empty_when_live_trading_disabled() -> None:
    assert report._real_money_nav_link() == ""


def test_real_money_nav_link_appears_when_live_trading_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    assert 'href="real-money.html"' in report._real_money_nav_link()


def test_generate_report_omits_real_money_html_by_default() -> None:
    report.generate_report()
    assert not (config.REPORT_DIR / "real-money.html").exists()
    # ...and pnl.html itself carries no dead link to a page that doesn't exist.
    assert 'href="real-money.html"' not in (config.REPORT_DIR / "pnl.html").read_text()


def test_generate_report_writes_real_money_html_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    config.REAL_MONEY_DATA_PATH.write_text(
        json.dumps({"mode": "live", "account": {"equity": 5_000.0}, "positions": []})
    )
    # A separate paper snapshot proves real-money.html reads its own file,
    # not pnl.json.
    _write_pnl_snapshot({"mode": "paper", "account": {"equity": 100_000.0}, "positions": []})

    report.generate_report()

    real_money_html = (config.REPORT_DIR / "real-money.html").read_text()
    assert '<span class="mode-badge">LIVE</span>' in real_money_html
    assert "$5,000.00" in real_money_html
    assert "$100,000.00" not in real_money_html
    # pnl.html links forward to it, once it exists.
    assert 'href="real-money.html"' in (config.REPORT_DIR / "pnl.html").read_text()


def test_generate_report_links_to_real_money_html_from_every_existing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding (code-push review): an earlier draft
    # only wired the new nav link into pnl.html/real-money.html's own
    # mutual link, leaving index.html/tickers.html/calendar.html with no
    # path to the live page at all -- the M16 precedent for pnl.html
    # itself (DESIGN.md §3.8 "a new nav link to it on each of
    # index.html/tickers.html/calendar.html") was every page, not one.
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    config.REAL_MONEY_DATA_PATH.write_text(
        json.dumps({"mode": "live", "account": {"equity": 5_000.0}, "positions": []})
    )
    report.generate_report()
    for filename in ("index.html", "tickers.html", "calendar.html"):
        assert 'href="real-money.html"' in (config.REPORT_DIR / filename).read_text(), (
            f"{filename} has no link to real-money.html"
        )


def test_sitemap_pages_omits_real_money_html_by_default() -> None:
    assert "real-money.html" not in report._sitemap_pages()


def test_sitemap_pages_includes_real_money_html_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", True)
    assert "real-money.html" in report._sitemap_pages()


# --- M18: SEO meta, robots.txt, sitemap.xml, analytics -----------------------

_HTML_PAGES = ("index.html", "tickers.html", "pnl.html", "calendar.html")


def test_every_page_has_viewport_lang_and_a_description() -> None:
    # Baseline SEO/mobile hygiene: before M18 no page had a viewport meta,
    # a lang attribute, or a description -- the biggest single gap.
    report.generate_report()
    for filename in _HTML_PAGES:
        html_out = (config.REPORT_DIR / filename).read_text()
        assert '<html lang="en">' in html_out
        assert 'name="viewport"' in html_out
        assert '<meta name="description" content="' in html_out


def test_pages_are_noindex_with_no_canonical_when_site_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    report.generate_report()
    for filename in _HTML_PAGES:
        html_out = (config.REPORT_DIR / filename).read_text()
        assert 'content="noindex,nofollow"' in html_out
        assert 'rel="canonical"' not in html_out
        assert "og:title" not in html_out


def test_pages_carry_canonical_and_open_graph_when_site_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://gramunger.com")
    report.generate_report()
    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert 'content="index,follow"' in index_html
    assert '<link rel="canonical" href="https://gramunger.com/index.html">' in index_html
    assert 'property="og:url" content="https://gramunger.com/index.html"' in index_html
    assert 'property="og:site_name" content="Munger Screener"' in index_html
    assert "noindex" not in index_html


def test_site_url_trailing_slash_does_not_double_up_in_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # config.SITE_BASE_URL is .rstrip("/")-ed at load; guard the join anyway.
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://gramunger.com")
    head = report._seo_head("T", "D", "pnl.html")
    assert "https://gramunger.com/pnl.html" in head
    assert "gramunger.com//pnl.html" not in head


def test_robots_txt_disallows_everything_on_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "")
    report.generate_report()
    robots = (config.REPORT_DIR / "robots.txt").read_text()
    assert "Disallow: /" in robots
    assert "Sitemap:" not in robots
    assert not (config.REPORT_DIR / "sitemap.xml").exists()


def test_robots_txt_allows_pages_and_advertises_sitemap_on_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://gramunger.com")
    report.generate_report()
    robots = (config.REPORT_DIR / "robots.txt").read_text()
    assert "Allow: /" in robots
    assert "Disallow: /progress.json" in robots
    assert "Sitemap: https://gramunger.com/sitemap.xml" in robots


def test_sitemap_lists_calendar_when_grafana_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://gramunger.com")
    monkeypatch.setattr(config, "GRAFANA_BASE_URL", "")
    report.generate_report()
    sitemap = (config.REPORT_DIR / "sitemap.xml").read_text()
    root = ET.fromstring(sitemap)  # must be well-formed XML
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = {el.text for el in root.iter(f"{ns}loc")}
    assert "https://gramunger.com/index.html" in locs
    assert "https://gramunger.com/calendar.html" in locs
    assert "https://gramunger.com/dashboards.html" not in locs


def test_sitemap_lists_dashboards_when_grafana_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "SITE_BASE_URL", "https://gramunger.com")
    monkeypatch.setattr(config, "GRAFANA_BASE_URL", "https://grafana.gramunger.com/d/x")
    report.generate_report()
    sitemap = (config.REPORT_DIR / "sitemap.xml").read_text()
    locs = {el.text for el in ET.fromstring(sitemap).iter()}
    assert "https://gramunger.com/dashboards.html" in locs
    assert "https://gramunger.com/calendar.html" not in locs


def test_analytics_snippet_present_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "ANALYTICS_URL", "")
    assert report._analytics_snippet() == ""
    report.generate_report()
    assert "goatcounter" not in (config.REPORT_DIR / "index.html").read_text()

    monkeypatch.setattr(config, "ANALYTICS_URL", "https://stats.gramunger.com/count")
    snippet = report._analytics_snippet()
    assert 'data-goatcounter="https://stats.gramunger.com/count"' in snippet
    assert 'src="//stats.gramunger.com/count.js"' in snippet
    report.generate_report()
    assert snippet in (config.REPORT_DIR / "index.html").read_text()
