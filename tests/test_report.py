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
    rows = [{"symbol": "AAPL", "reason": "</script><script>alert(1)</script>"}]

    html_out = report._render_export_controls(rows)

    assert "</script><script>alert(1)</script>" not in html_out
    assert "\\u003c/script>" in html_out
    assert "Copy JSON" in html_out
    assert "Export CSV" in html_out


def test_render_export_controls_empty_when_no_rows() -> None:
    assert report._render_export_controls([]) == ""


def test_index_page_includes_export_controls_and_feed_link_tags() -> None:
    report.generate_report()

    index_html = (config.REPORT_DIR / "index.html").read_text()
    assert 'id="picks-data"' not in index_html  # no rows: nothing embedded
    assert '<link rel="alternate" type="application/feed+json"' in index_html
    assert '<link rel="alternate" type="application/rss+xml"' in index_html


def test_recent_daily_feed_entries_reads_buyable_symbols_newest_first() -> None:
    _write_archive("2026-07-01", "AAPL,True,90.0,\nMSFT,False,10.0,graham_pe\n")
    _write_archive("2026-07-02", "AAPL,True,50.0,\nGOOG,True,95.0,\n")

    entries = report._recent_daily_feed_entries()

    assert [day.isoformat() for day, _ in entries] == ["2026-07-02", "2026-07-01"]
    # GOOG (95.0) ranks above AAPL (50.0) on the 07-02 day.
    assert entries[0][1] == ["GOOG", "AAPL"]
    assert entries[1][1] == ["AAPL"]


def test_recent_daily_feed_entries_respects_max_items_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "FEED_MAX_ITEMS", 1)
    _write_archive("2026-07-01", "AAPL,True,90.0,\n")
    _write_archive("2026-07-02", "AAPL,True,90.0,\n")

    entries = report._recent_daily_feed_entries()

    assert len(entries) == 1
    assert entries[0][0].isoformat() == "2026-07-02"


def test_recent_daily_feed_entries_skips_a_day_missing_the_score_column() -> None:
    # Real bug (staff-engineer-reviewer): a day whose archive can't be
    # re-read with the score column (legacy schema, deleted, corrupted)
    # must be skipped, not silently rendered as "0 buyable candidates" --
    # that would be indistinguishable from a real, valid empty-screen day.
    config.SCREEN_RESULTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (config.SCREEN_RESULTS_ARCHIVE_DIR / "screen_results_2026-07-01.csv").write_text(
        "symbol,buyable\nAAPL,True\n"  # no score column
    )
    _write_archive("2026-07-02", "AAPL,True,90.0,\n")

    entries = report._recent_daily_feed_entries()

    assert [day.isoformat() for day, _ in entries] == ["2026-07-02"]


def test_feed_base_url_defaults_to_relative_dot_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "REPORT_BASE_URL", "")
    assert report._feed_base_url() == "."


def test_feed_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "REPORT_BASE_URL", "https://gramunger.com/")
    assert report._feed_base_url() == "https://gramunger.com"


def test_render_feed_json_produces_valid_json_feed_with_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "REPORT_BASE_URL", "https://gramunger.com")
    _write_archive("2026-07-26", "AAPL,True,91.2,\nMSFT,True,88.0,\n")

    feed = json.loads(report._render_feed_json())

    assert feed["version"] == "https://jsonfeed.org/version/1.1"
    assert feed["home_page_url"] == "https://gramunger.com/index.html"
    assert len(feed["items"]) == 1
    item = feed["items"][0]
    assert item["id"] == "munger-daily-2026-07-26"
    assert "AAPL" in item["content_text"] and "MSFT" in item["content_text"]


def test_render_feed_rss_produces_valid_xml_with_items() -> None:
    _write_archive("2026-07-26", "AAPL,True,91.2,\n")

    rss = report._render_feed_rss()
    # Real bug (staff-engineer-reviewer): an earlier draft used the HTML5
    # named entity &mdash;, which isn't valid in bare XML (no DOCTYPE
    # declares it) and made a strict parser reject the whole document.
    # Parsing (not just substring-checking) the output is what actually
    # proves this is well-formed XML.
    root = ET.fromstring(rss)

    assert root.tag == "rss"
    assert "<rss version=\"2.0\">" in rss
    assert "munger-daily-2026-07-26" in rss
    assert "AAPL" in rss


def test_generate_report_writes_feed_json_and_rss_xml() -> None:
    _write_archive("2026-07-26", "AAPL,True,91.2,\n")

    report.generate_report()

    assert (config.REPORT_DIR / "feed.json").exists()
    assert (config.REPORT_DIR / "rss.xml").exists()
    feed = json.loads((config.REPORT_DIR / "feed.json").read_text())
    assert feed["items"][0]["id"] == "munger-daily-2026-07-26"


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
