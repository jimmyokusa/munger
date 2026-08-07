"""Unit tests for daily_screen.py -- the screen-only, no-trading daily job."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LOG_FILE_PATH", tmp_path / "munger.log")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "report")
    (tmp_path / "screen_results.csv").write_text("symbol,buyable,score,fail_reasons\n")


def test_daily_screen_never_imports_execution() -> None:
    # M14 (user request): this script must be architecturally incapable
    # of placing an order, not just configured not to -- verified here
    # by asserting execution.py is never imported as a result of
    # importing daily_screen, not just by code review.
    sys.modules.pop("daily_screen", None)
    sys.modules.pop("execution", None)

    import daily_screen  # noqa: F401

    assert "execution" not in sys.modules


def test_run_fetches_screens_and_archives_without_touching_a_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daily_screen
    import journal
    import report
    import screener
    import universe

    calls: list[str] = []
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["AAPL", "MSFT"]),
    )

    def _fake_run_screen(tickers: list[str]) -> object:
        calls.append("run_screen")

        import pandas as pd

        return pd.DataFrame(
            [
                {"symbol": "AAPL", "buyable": True, "score": 80.0, "fail_reasons": ""},
                {"symbol": "MSFT", "buyable": False, "score": 10.0, "fail_reasons": "graham_pe"},
            ]
        )

    monkeypatch.setattr(screener, "run_screen", _fake_run_screen)
    monkeypatch.setattr(journal, "archive_screen_results", lambda run_date: calls.append("archive"))
    monkeypatch.setattr(report, "generate_report", lambda: calls.append("generate_report"))

    daily_screen.run(run_date="2026-07-23")

    assert calls == ["run_screen", "archive", "generate_report"]


def test_run_skips_archive_when_fetch_quality_is_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fetch-quality gate (staff-engineer-reviewer): a throttled run that
    # fetched too little of the universe must NOT be archived, or the
    # calendar would render a degraded snapshot as if it were genuine
    # history. The live report is still regenerated.
    import daily_screen
    import journal
    import report
    import screener
    import universe

    calls: list[str] = []
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["AAPL", "MSFT", "GOOG", "AMZN"]),
    )

    def _fake_run_screen(tickers: list[str]) -> object:
        calls.append("run_screen")

        import pandas as pd

        # 3 of 4 failed to fetch -> fraction 0.25, well under the 0.90 floor.
        fetch_failed = "data_missing:fetch_failed"
        return pd.DataFrame(
            [
                {"symbol": "AAPL", "buyable": False, "score": 0.0, "fail_reasons": fetch_failed},
                {"symbol": "MSFT", "buyable": False, "score": 0.0, "fail_reasons": fetch_failed},
                {"symbol": "GOOG", "buyable": False, "score": 0.0, "fail_reasons": fetch_failed},
                {"symbol": "AMZN", "buyable": True, "score": 80.0, "fail_reasons": ""},
            ]
        )

    monkeypatch.setattr(screener, "run_screen", _fake_run_screen)
    monkeypatch.setattr(journal, "archive_screen_results", lambda run_date: calls.append("archive"))
    monkeypatch.setattr(report, "generate_report", lambda: calls.append("generate_report"))

    daily_screen.run(run_date="2026-07-23")

    assert "archive" not in calls
    assert calls == ["run_screen", "generate_report"]
