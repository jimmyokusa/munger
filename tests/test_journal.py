"""Unit tests for journal.py (DESIGN.md section 6, layer 1)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

import config
import journal


@pytest.fixture(autouse=True)
def _use_tmp_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")


def _all_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(config.JOURNAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM journal ORDER BY id").fetchall()
    finally:
        conn.close()


def test_record_order_rejects_invalid_side() -> None:
    # A typo like "Buy" or "buy " would otherwise be silently treated as
    # a sell by get_expected_holdings, which only special-cases "buy".
    with pytest.raises(ValueError):
        journal.record_order("AAPL", "Buy", "NEW_POSITION score=78.2")
    # Rejected before any write was attempted -- not even the table
    # should have been created.
    assert not config.JOURNAL_DB_PATH.exists()


def test_record_order_appends_a_row() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2", notional=500.0)

    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["side"] == "buy"
    assert rows[0]["reason"] == "NEW_POSITION score=78.2"
    assert rows[0]["notional"] == 500.0


def test_record_order_is_append_only_across_calls() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    journal.record_order("MSFT", "buy", "NEW_POSITION score=65.0")

    rows = _all_rows()
    assert len(rows) == 2
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]


def test_get_expected_holdings_reflects_most_recent_action_per_symbol() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    journal.record_order("MSFT", "buy", "NEW_POSITION score=65.0")
    journal.record_order("MSFT", "sell", "SELL strikes=2 reasons=roe_floor")

    assert journal.get_expected_holdings() == {"AAPL"}


def test_get_expected_holdings_uses_the_latest_row_not_just_any_buy() -> None:
    # AAPL bought, then sold -- must not still count as expected-held
    # just because a "buy" row exists somewhere in its history.
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    journal.record_order("AAPL", "sell", "SELL strikes=2 reasons=roe_floor")

    assert journal.get_expected_holdings() == set()


def test_check_reconciliation_no_mismatch_returns_empty() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    assert journal.check_reconciliation({"AAPL"}) == []


def test_check_reconciliation_flags_missing_expected_holding() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    warnings = journal.check_reconciliation(set())
    assert len(warnings) == 1
    assert "AAPL" in warnings[0]
    assert "expected to hold" in warnings[0]


def test_check_reconciliation_flags_unexpected_holding() -> None:
    warnings = journal.check_reconciliation({"AAPL"})
    assert len(warnings) == 1
    assert "AAPL" in warnings[0]
    assert "doesn't expect one" in warnings[0]


def test_archive_screen_results_copies_the_current_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    csv_path = tmp_path / "screen_results.csv"
    csv_path.write_text("symbol,buyable,score\nAAPL,True,90.0\n")
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", csv_path)
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", archive_dir)

    archived_path = journal.archive_screen_results("2026-07-21")

    assert archived_path == archive_dir / "screen_results_2026-07-21.csv"
    assert archived_path.read_text() == csv_path.read_text()


def test_configure_logging_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "LOG_FILE_PATH", tmp_path / "munger.log")
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    try:
        root_logger.handlers = []
        journal.configure_logging()
        handlers_after_first_call = list(root_logger.handlers)
        journal.configure_logging()
        assert root_logger.handlers == handlers_after_first_call
    finally:
        root_logger.handlers = original_handlers
