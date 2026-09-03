"""Unit tests for journal.py (DESIGN.md section 6, layer 1)."""

from __future__ import annotations

import logging
import os
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
        return conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
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


def test_record_order_skips_a_duplicate_client_order_id() -> None:
    # Staff-engineer-reviewer finding: execution.py's market_buy/liquidate
    # return the *existing* broker order (not None) when a crash-restart
    # re-run hits has_already_submitted -- without a guard here, bot.py
    # would insert a second orders row for the one order actually placed.
    journal.record_order(
        "AAPL", "buy", "NEW_POSITION score=78.2", client_order_id="paper-2026-01-01-AAPL-buy"
    )
    journal.record_order(
        "AAPL", "buy", "NEW_POSITION score=99.9", client_order_id="paper-2026-01-01-AAPL-buy"
    )

    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0]["reason"] == "NEW_POSITION score=78.2"  # the first (real) submission wins


def test_record_order_without_a_client_order_id_never_dedupes() -> None:
    # No idempotency key available -- matches every pre-M25 row's
    # behavior of always inserting.
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")

    assert len(_all_rows()) == 2


def test_record_order_distinct_client_order_ids_both_insert() -> None:
    journal.record_order(
        "AAPL", "buy", "NEW_POSITION score=78.2", client_order_id="paper-2026-01-01-AAPL-buy"
    )
    journal.record_order(
        "MSFT", "buy", "NEW_POSITION score=65.0", client_order_id="paper-2026-01-01-MSFT-buy"
    )

    assert len(_all_rows()) == 2


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


# --- M20: account column + migration (DESIGN_REAL_MONEY.md §3.4) ---


def test_record_order_defaults_account_from_config_paper_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    assert _all_rows()[0]["account"] == "paper"

    monkeypatch.setattr(config, "PAPER_TRADING", False)
    journal.record_order("MSFT", "buy", "NEW_POSITION score=65.0")
    assert _all_rows()[1]["account"] == "live"


def test_record_order_accepts_an_explicit_account_override() -> None:
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2", account="live")
    assert _all_rows()[0]["account"] == "live"


def test_get_expected_holdings_filters_by_account(monkeypatch: pytest.MonkeyPatch) -> None:
    # The actual defense-in-depth property (staff-engineer-reviewer
    # finding): even if a journal.db somehow held both accounts' rows, a
    # paper run's reconciliation must never be explained away by live's
    # activity, and vice versa.
    journal.record_order("AAPL", "buy", "paper pick", account="paper")
    journal.record_order("MSFT", "buy", "live pick", account="live")

    assert journal.get_expected_holdings("paper") == {"AAPL"}
    assert journal.get_expected_holdings("live") == {"MSFT"}


def test_get_expected_holdings_defaults_to_current_config_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal.record_order("AAPL", "buy", "paper pick", account="paper")
    journal.record_order("MSFT", "buy", "live pick", account="live")

    monkeypatch.setattr(config, "PAPER_TRADING", True)
    assert journal.get_expected_holdings() == {"AAPL"}
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    assert journal.get_expected_holdings() == {"MSFT"}


def test_get_holdings_detail_filters_by_account() -> None:
    journal.record_order("AAPL", "buy", "paper pick", account="paper")
    journal.record_order("MSFT", "buy", "live pick", account="live")

    paper_detail = journal.get_holdings_detail("paper")
    live_detail = journal.get_holdings_detail("live")

    assert [d["symbol"] for d in paper_detail] == ["AAPL"]
    assert [d["symbol"] for d in live_detail] == ["MSFT"]


def test_check_reconciliation_does_not_cross_contaminate_accounts() -> None:
    # A live-only mismatch must not be masked by paper's unrelated,
    # legitimate holdings, and vice versa -- this is the exact failure
    # mode named in design review: one account's activity silently
    # explaining away the other's real mismatch.
    journal.record_order("AAPL", "buy", "paper pick", account="paper")
    journal.record_order("MSFT", "buy", "live pick", account="live")

    # The live account's own broker call reports no positions at all --
    # MSFT (live's own expected holding) is genuinely missing. AAPL is a
    # *paper* journal entry with no bearing on live's broker state; it
    # must neither mask the real MSFT mismatch nor spuriously appear as
    # one of its own.
    warnings = journal.check_reconciliation(set(), account="live")

    assert len(warnings) == 1
    assert "MSFT" in warnings[0]


def test_connect_migrates_a_pre_m20_journal_db_missing_the_account_column(
    tmp_path: Path,
) -> None:
    # Simulates a real already-persisted journal.db from before this
    # milestone: created with the old schema (no account column), rows
    # already present. CREATE TABLE IF NOT EXISTS alone is a no-op against
    # an existing table -- without an explicit ALTER TABLE migration, the
    # column would simply never appear on a real deployment restoring this
    # exact file from its bot-state branch.
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            notional REAL,
            reason TEXT NOT NULL,
            client_order_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO journal (timestamp, symbol, side, notional, reason, client_order_id) "
        "VALUES ('2026-07-01T00:00:00+00:00', 'AAPL', 'buy', 500.0, 'legacy row', NULL)"
    )
    conn.commit()
    conn.close()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "JOURNAL_DB_PATH", db_path)
        # Backfilled as 'paper' -- accurate, since nothing but the paper
        # account ever wrote to this table before this migration existed.
        assert journal.get_expected_holdings("paper") == {"AAPL"}
        assert journal.get_expected_holdings("live") == set()


# --- M26a: journal -> orders/fills split (Design v2.2 §3.3) ---


def test_connect_migrates_a_pre_m26a_journal_table_to_orders(tmp_path: Path) -> None:
    # Simulates a real pre-M26a journal.db: a table literally named
    # "journal" (with the M20 account column already present, i.e. the
    # schema as it stood immediately before this migration), one row
    # already in it. A bare CREATE TABLE IF NOT EXISTS "orders" would
    # leave this untouched and simply start a second, empty table --
    # every already-persisted order on bot-state/bot-state-live would
    # silently stop being visible to get_expected_holdings/
    # get_holdings_detail/check_reconciliation, without ever raising an
    # error. The real ALTER TABLE RENAME must actually move the data.
    db_path = tmp_path / "journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            notional REAL,
            reason TEXT NOT NULL,
            client_order_id TEXT,
            account TEXT NOT NULL DEFAULT 'paper'
        )
        """
    )
    conn.execute(
        "INSERT INTO journal "
        "(timestamp, symbol, side, notional, reason, client_order_id, account) "
        "VALUES ('2026-08-08T14:47:01+00:00', 'FOX', 'sell', NULL, "
        "'SELL strikes=2', NULL, 'paper')"
    )
    conn.commit()
    conn.close()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "JOURNAL_DB_PATH", db_path)
        assert journal.get_expected_holdings("paper") == set()  # FOX was sold, not bought

    # The row itself survived the rename, unmodified -- migration policy
    # is "preserve exactly," not "reset" or "drop."
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM orders").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "FOX"
    assert rows[0]["reason"] == "SELL strikes=2"


def test_connect_migrates_the_real_bot_state_journal_db(tmp_path: Path) -> None:
    # Per this milestone's own exit criteria: tested against a real copy
    # of bot-state's journal.db, not only a synthetic fixture. This is a
    # byte-for-byte copy of the actual persisted paper-account journal
    # (fetched via `git show origin/bot-state:journal.db`, 2026-09-02),
    # containing the real FOX/LPG history referenced elsewhere in
    # TASKS.md/DESIGN_V2.md.
    real_journal_path = Path(__file__).parent / "fixtures" / "real_bot_state_journal.db"
    db_path = tmp_path / "journal.db"
    db_path.write_bytes(real_journal_path.read_bytes())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "JOURNAL_DB_PATH", db_path)
        # Both FOX and LPG's most recent recorded action is a sell --
        # migrating the table must not silently lose or reorder rows in
        # a way that flips which action is "most recent" per symbol.
        expected = journal.get_expected_holdings("paper")
        assert "FOX" not in expected
        assert "LPG" not in expected

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    orders_rows = conn.execute("SELECT * FROM orders WHERE symbol IN ('FOX', 'LPG')").fetchall()
    table_names = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert len(orders_rows) == 5  # FOX buy, LPG buy x2, FOX sell, LPG sell -- see TASKS.md
    assert "journal" not in table_names
    assert "orders" in table_names
    assert "fills" in table_names


def test_check_reconciliation_reproduces_the_real_fox_lpg_divergence(tmp_path: Path) -> None:
    # M27 (Design v2.2 §3.3, "reconciliation gets teeth"): the real
    # 2026-08-08 divergence, reproduced against the actual persisted
    # journal -- the journal recorded FOX and LPG as sold, but the
    # broker (per the real incident) still reported both as held,
    # because their DAY limit orders never actually filled. This is the
    # exact mismatch check_reconciliation exists to catch, and it must
    # keep catching it even after the M26a orders/fills schema split.
    real_journal_path = Path(__file__).parent / "fixtures" / "real_bot_state_journal.db"
    db_path = tmp_path / "journal.db"
    db_path.write_bytes(real_journal_path.read_bytes())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "JOURNAL_DB_PATH", db_path)
        # Isolate the FOX/LPG-specific mismatch: the broker agreeing
        # with the journal on every *other* symbol, but still reporting
        # FOX and LPG as held (the real incident) even though the
        # journal's most recent action for both is a sell.
        expected = journal.get_expected_holdings("paper")
        broker_reports = expected | {"FOX", "LPG"}
        warnings = journal.check_reconciliation(broker_reports, account="paper")

    assert len(warnings) == 2
    assert any("FOX" in w and "doesn't expect one" in w for w in warnings)
    assert any("LPG" in w and "doesn't expect one" in w for w in warnings)


def test_record_fill_and_get_fill_roundtrip() -> None:
    journal.record_fill(
        "paper-2026-09-02-AAPL-buy", "AAPL", "filled", filled_qty=10.0, fill_price=150.25
    )
    fill = journal.get_fill("paper-2026-09-02-AAPL-buy")
    assert fill is not None
    assert fill["status"] == "filled"
    assert fill["filled_qty"] == 10.0
    assert fill["fill_price"] == 150.25
    assert fill["account"] == "paper"


def test_get_fill_returns_none_when_unsettled() -> None:
    assert journal.get_fill("no-such-order") is None


def test_record_fill_upserts_not_duplicates() -> None:
    # The idempotency property M26b depends on: re-polling the same
    # order and writing its (possibly-updated) status must overwrite the
    # existing row, not accumulate a second one.
    journal.record_fill("paper-2026-09-02-AAPL-buy", "AAPL", "pending")
    journal.record_fill("paper-2026-09-02-AAPL-buy", "AAPL", "partially_filled", filled_qty=4.0)
    journal.record_fill(
        "paper-2026-09-02-AAPL-buy", "AAPL", "filled", filled_qty=10.0, fill_price=150.0
    )

    conn = sqlite3.connect(config.JOURNAL_DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    conn.close()

    assert count == 1
    fill = journal.get_fill("paper-2026-09-02-AAPL-buy")
    assert fill is not None
    assert fill["status"] == "filled"
    assert fill["filled_qty"] == 10.0


def test_record_fill_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        journal.record_fill("some-id", "AAPL", "bogus_status")


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


def test_archive_screen_results_never_calls_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real bug (2026-07-25 Cloud Run crash): shutil.copy (unlike copyfile)
    # also calls os.chmod to preserve the source's permission bits, which
    # GCS FUSE rejects with PermissionError -- worked fine on local disk/
    # k3s's PVC, so it never surfaced there. Forcing os.chmod to raise
    # proves archive_screen_results no longer calls it at all.
    def _raise(*args: object, **kwargs: object) -> None:
        raise PermissionError("[Errno 1] Operation not permitted (simulated GCS FUSE)")

    monkeypatch.setattr(os, "chmod", _raise)
    csv_path = tmp_path / "screen_results.csv"
    csv_path.write_text("symbol,buyable,score\nAAPL,True,90.0\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", csv_path)
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")

    archived_path = journal.archive_screen_results("2026-07-21")

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
