"""Trade journal and observability (DESIGN.md section 3.6).

Three responsibilities: an append-only SQLite journal of every order with
a reason string (SQLite, not CSV -- a torn CSV append from a crash
mid-write is exactly the corruption risk this format avoids); timestamped
screen_results.csv archiving; and structured logging setup to file and
stdout.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import sqlite3
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# get_expected_holdings() treats "buy" as the only held-signaling value --
# an unvalidated typo (e.g. "Buy", "buy ") would silently be treated as a
# sell for reconciliation purposes with no error.
_VALID_SIDES = ("buy", "sell")


def _connect() -> sqlite3.Connection:
    # timeout=5.0: report.py (M13) can run standalone at any time, so a
    # read here may genuinely race a concurrent bot.py write -- the
    # default 5s SQLite busy-timeout is 0 (fail immediately with
    # "database is locked"); waiting a few seconds lets the other
    # transaction finish instead of crashing (staff-engineer-reviewer
    # finding). record_order's transactions are short (a single INSERT),
    # so this should almost never actually wait the full timeout.
    conn = sqlite3.connect(config.JOURNAL_DB_PATH, timeout=5.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journal (
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
    return conn


def record_order(
    symbol: str,
    side: str,
    reason: str,
    notional: float | None = None,
    client_order_id: str | None = None,
) -> None:
    """Append one order attempt to the journal, with its reason string.

    E.g. reason="NEW_POSITION score=78.2" or reason="SELL strikes=2
    reasons=roe_floor,fcf_floor" (DESIGN.md 3.6). Append-only: there is
    no update/delete path -- this is an audit trail, not mutable state.

    Raises ValueError for anything other than exactly "buy" or "sell" --
    get_expected_holdings() depends on this value being one of exactly
    those two strings to correctly derive reconciliation state.
    """
    if side not in _VALID_SIDES:
        raise ValueError(f"side must be one of {_VALID_SIDES}, got {side!r}")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO journal (timestamp, symbol, side, notional, reason, client_order_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.datetime.now(datetime.UTC).isoformat(),
                symbol,
                side,
                notional,
                reason,
                client_order_id,
            ),
        )


def get_expected_holdings() -> set[str]:
    """Tickers the journal expects to currently be held.

    Derived from the most recent recorded action per symbol: if it was a
    buy, we expect to hold it; if it was a sell/liquidation, we don't.
    Used for the reconciliation check (DESIGN.md 3.4/3.6) against what
    the broker actually reports -- a mismatch is a signal something
    upstream (a failed order, a manual trade, a bug) needs attention, not
    itself an abort condition.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, side FROM journal WHERE id IN "
            "(SELECT MAX(id) FROM journal GROUP BY symbol)"
        ).fetchall()
    return {symbol for symbol, side in rows if side == "buy"}


def get_holdings_detail() -> list[dict[str, object]]:
    """Most recent buy record for every symbol currently expected held.

    Same "most recent action per symbol" logic as get_expected_holdings,
    but returns the full row (reason, notional, timestamp) rather than
    just the symbol set -- for display purposes (report.py), which needs
    to show *why* each current pick was bought, not just that it was.
    """
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, reason, notional, timestamp FROM journal "
            "WHERE id IN (SELECT MAX(id) FROM journal GROUP BY symbol) "
            "AND side = 'buy' ORDER BY symbol"
        ).fetchall()
    return [dict(row) for row in rows]


def check_reconciliation(actual_holdings: set[str]) -> list[str]:
    """Compare live-fetched holdings against what the journal expects.

    Returns human-readable warning strings for the caller to log
    (DESIGN.md 3.4/3.6); an empty list means no divergence. Not an abort
    condition -- this is the cheapest signal that something needs
    attention, not proof that something is broken (e.g. a legitimate
    corporate action could also cause a one-off mismatch).
    """
    expected = get_expected_holdings()
    warnings = [
        f"{ticker}: expected to hold (per journal) but broker reports no position"
        for ticker in sorted(expected - actual_holdings)
    ]
    warnings += [
        f"{ticker}: broker reports a position but journal doesn't expect one"
        for ticker in sorted(actual_holdings - expected)
    ]
    return warnings


def archive_screen_results(run_date: str) -> Path:
    """Copy the current screen_results.csv to a timestamped archive file.

    DESIGN.md 3.6 requires timestamped copies be kept, distinct from the
    main file (which is overwritten every run by screener.run_screen).
    """
    config.SCREEN_RESULTS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = config.SCREEN_RESULTS_ARCHIVE_DIR / f"screen_results_{run_date}.csv"
    shutil.copy(config.SCREEN_RESULTS_CSV_PATH, archive_path)
    return archive_path


def configure_logging() -> None:
    """Structured logging to file and stdout (DESIGN.md 3.6).

    Idempotent: safe to call more than once (e.g. from tests) without
    duplicating handlers on the root logger.
    """
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(config.LOG_FILE_PATH)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
