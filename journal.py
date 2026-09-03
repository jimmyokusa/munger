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

    # M26a (Design v2.2 §3.3): the single "journal" table splits into
    # "orders" (what was submitted) and "fills" (what actually happened).
    # This function's own migration precedent (the M20 account-column
    # ALTER below) is the model: PRAGMA-gated, real ALTER statements, not
    # a CREATE TABLE IF NOT EXISTS that would silently no-op against an
    # already-existing table and leave every real persisted journal.db on
    # `bot-state`/`bot-state-live` still holding a table named "journal"
    # forever.
    #
    # Migration policy, stated explicitly (staff-engineer-reviewer
    # finding on the design doc: a migration needs a stated policy for
    # existing rows, not just a schema-mechanics test) -- every existing
    # "journal" row is preserved exactly as-is under the new name. No
    # data is reinterpreted or dropped: a row that was a submission
    # record before this migration is still a submission record after
    # it, just living in a table named for what it actually represents.
    table_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "journal" in table_names and "orders" not in table_names:
        conn.execute("ALTER TABLE journal RENAME TO orders")
        table_names.discard("journal")
        table_names.add("orders")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
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
    # M20 (DESIGN_REAL_MONEY.md §3.4): CREATE TABLE IF NOT EXISTS is a
    # no-op against an already-existing journal.db from before this
    # column existed -- every persisted bot-state journal predates it, so
    # without an explicit migration the column would simply be missing on
    # every real deployment, not just fresh ones. ALTER TABLE ADD COLUMN
    # with a DEFAULT retroactively backfills existing rows as 'paper',
    # which is accurate: nothing but the paper account has ever written
    # to this table before this milestone.
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "account" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN account TEXT NOT NULL DEFAULT 'paper'")
    # M26a: qty/limit_price weren't tracked before this milestone --
    # nullable, since every pre-migration row genuinely has neither value
    # recorded (notional-only buys, and liquidate() didn't journal qty
    # either). Populating these for new orders is M26b/e's job, not this
    # one; this milestone only makes room for them.
    if "qty" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN qty REAL")
    if "limit_price" not in existing_columns:
        conn.execute("ALTER TABLE orders ADD COLUMN limit_price REAL")

    # fills: what actually happened, as opposed to what was submitted.
    # client_order_id is the primary key, not an incrementing id with a
    # unique constraint -- a settlement pass re-polling the same order
    # writes (upserts) the same row again as status changes
    # (pending -> partially_filled -> filled), rather than accumulating
    # one row per poll. That's what "idempotent re-run" (§3.3) actually
    # means against Alpaca's own Order model, which reports a single
    # order's running filled_qty/filled_avg_price, not a stream of
    # discrete fill events.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fills (
            client_order_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            filled_qty REAL,
            fill_price REAL,
            settlement_timestamp TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT 'paper'
        )
        """
    )
    return conn


def _current_account() -> str:
    """The account label for calls that don't pass one explicitly.

    "paper"/"live", derived from live config, not a module-level
    constant, so it reflects whichever process env this is running in
    (mirrors how execution.py reads config.PAPER_TRADING at call time,
    not import time).
    """
    return "paper" if config.PAPER_TRADING else "live"


def record_order(
    symbol: str,
    side: str,
    reason: str,
    notional: float | None = None,
    client_order_id: str | None = None,
    account: str | None = None,
    qty: float | None = None,
    limit_price: float | None = None,
) -> None:
    """Append one order *submission* to the orders table, with its reason string.

    E.g. reason="NEW_POSITION score=78.2" or reason="SELL strikes=2
    reasons=roe_floor,fcf_floor" (DESIGN.md 3.6). Append-only: there is
    no update/delete path -- this is an audit trail, not mutable state.

    M26a (Design v2.2 §3.3): this records that an order was *submitted*,
    not that it happened -- what actually happened lives in the `fills`
    table (see record_fill), written separately by a settlement pass.
    `qty`/`limit_price` are new, optional fields for that same split;
    existing callers that only pass `notional` (a notional-sized buy)
    leave them `None`, same as every pre-M26a row.

    Raises ValueError for anything other than exactly "buy" or "sell" --
    get_expected_holdings() depends on this value being one of exactly
    those two strings to correctly derive reconciliation state.

    `account` ("paper"/"live") defaults to whichever this process's own
    config.PAPER_TRADING says (M20, DESIGN_REAL_MONEY.md §3.4). Existing
    callers (bot.py) don't pass this explicitly and get today's exact
    behavior for the paper account; only tests and the new live workflow
    need to pass it explicitly.

    Idempotent on `client_order_id` when one is given (staff-engineer-
    reviewer finding): `execution.py`'s `market_buy`/`liquidate` return
    the *existing* broker order, not `None`, when `has_already_submitted`
    detects a crash-and-restart re-run within the same client-order-id
    window -- without a guard here, `bot.py` would then insert a second
    `orders` row for the one broker order that was actually placed,
    silently duplicating the audit trail this function's own docstring
    promises is append-only-but-accurate. A `client_order_id=None` call
    (no idempotency key available) always inserts, matching every
    pre-M25 row's behavior.
    """
    if side not in _VALID_SIDES:
        raise ValueError(f"side must be one of {_VALID_SIDES}, got {side!r}")
    account = account or _current_account()
    with _connect() as conn:
        if client_order_id is not None:
            existing = conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id = ? LIMIT 1",
                (client_order_id,),
            ).fetchone()
            if existing is not None:
                logger.info(
                    "record_order: %s already has an orders row for client_order_id=%s "
                    "-- skipping duplicate insert (crash-restart re-run)",
                    symbol,
                    client_order_id,
                )
                return
        conn.execute(
            "INSERT INTO orders "
            "(timestamp, symbol, side, notional, reason, client_order_id, account, "
            "qty, limit_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.datetime.now(datetime.UTC).isoformat(),
                symbol,
                side,
                notional,
                reason,
                client_order_id,
                account,
                qty,
                limit_price,
            ),
        )


_VALID_FILL_STATUSES = ("pending", "partially_filled", "filled", "canceled", "expired")


def record_fill(
    client_order_id: str,
    symbol: str,
    status: str,
    filled_qty: float | None = None,
    fill_price: float | None = None,
    account: str | None = None,
) -> None:
    """Upsert the current settlement outcome for one order.

    M26a (Design v2.2 §3.3): written by a settlement pass, not at
    submission time -- this is "what actually happened," and it's safe
    to call more than once for the same `client_order_id` (a re-polled
    order overwrites its own prior row rather than accumulating a new
    one), which is what makes the settlement pass idempotent against
    retries or a crash-and-restart. `status` is one of
    `_VALID_FILL_STATUSES`, matching Alpaca's own order-status vocabulary
    collapsed to the cases §3.3 actually distinguishes (a raw Alpaca
    `new`/`accepted`/`pending_new` all collapse to "pending" by the
    caller before this is invoked -- this function itself doesn't
    reinterpret Alpaca's states, it just stores what it's given).
    """
    if status not in _VALID_FILL_STATUSES:
        raise ValueError(f"status must be one of {_VALID_FILL_STATUSES}, got {status!r}")
    account = account or _current_account()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO fills "
            "(client_order_id, symbol, status, filled_qty, fill_price, "
            "settlement_timestamp, account) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "status = excluded.status, filled_qty = excluded.filled_qty, "
            "fill_price = excluded.fill_price, "
            "settlement_timestamp = excluded.settlement_timestamp",
            (
                client_order_id,
                symbol,
                status,
                filled_qty,
                fill_price,
                datetime.datetime.now(datetime.UTC).isoformat(),
                account,
            ),
        )


def get_fill(client_order_id: str) -> dict[str, object] | None:
    """The current settlement outcome for one order, or None if unsettled."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM fills WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
    return dict(row) if row else None


def get_expected_holdings(account: str | None = None) -> set[str]:
    """Tickers the journal expects to currently be held.

    Derived from the most recent recorded action per symbol *for the
    given account*: if it was a buy, we expect to hold it; if it was a
    sell/liquidation, we don't. Used for the reconciliation check
    (DESIGN.md 3.4/3.6) against what the broker actually reports -- a
    mismatch is a signal something upstream (a failed order, a manual
    trade, a bug) needs attention, not itself an abort condition.

    Args:
      account: "paper" or "live" -- defaults to this process's own
        config.PAPER_TRADING (M20 §3.4). Filtering by account here, not
        just at write time, is the actual defense-in-depth property: even
        if a journal.db somehow ends up holding both accounts' rows (an
        isolation failure elsewhere), a paper run's reconciliation only
        ever considers paper rows and a live run only ever considers live
        rows -- one account's activity can never silently explain away
        the other's mismatch.
    """
    account = account or _current_account()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, side FROM orders WHERE account = ? AND id IN "
            "(SELECT MAX(id) FROM orders WHERE account = ? GROUP BY symbol)",
            (account, account),
        ).fetchall()
    return {symbol for symbol, side in rows if side == "buy"}


def get_holdings_detail(account: str | None = None) -> list[dict[str, object]]:
    """Most recent buy record for every symbol currently expected held.

    Same "most recent action per symbol, per account" logic as
    get_expected_holdings, but returns the full row (reason, notional,
    timestamp) rather than just the symbol set -- for display purposes
    (report.py), which needs to show *why* each current pick was bought,
    not just that it was.
    """
    account = account or _current_account()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT symbol, reason, notional, timestamp FROM orders "
            "WHERE account = ? AND id IN "
            "(SELECT MAX(id) FROM orders WHERE account = ? GROUP BY symbol) "
            "AND side = 'buy' ORDER BY symbol",
            (account, account),
        ).fetchall()
    return [dict(row) for row in rows]


def check_reconciliation(actual_holdings: set[str], account: str | None = None) -> list[str]:
    """Compare live-fetched holdings against what the journal expects.

    Returns human-readable warning strings for the caller to log
    (DESIGN.md 3.4/3.6); an empty list means no divergence. Not an abort
    condition by default -- this is the cheapest signal that something
    needs attention, not proof that something is broken (e.g. a
    legitimate corporate action could also cause a one-off mismatch).
    Whether the *caller* treats a non-empty result as fatal is a policy
    decision made by the caller. M20 (DESIGN_REAL_MONEY.md §3.3)
    originally made this abort only for the live account; M27 (Design
    v2.2 §3.3, "reconciliation is authoritative... it should have
    teeth") removed that carve-out -- bot.py now aborts on a mismatch
    for both accounts, since the real FOX/LPG bug was exactly a
    paper-account divergence this check would have caught if paper's
    warn-and-continue posture hadn't let the run proceed anyway.
    """
    expected = get_expected_holdings(account)
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
    # copyfile (content only), not copy/copy2 -- both of those also call
    # os.chmod to preserve the source file's permission bits, which GCS
    # FUSE (Cloud Run's DATA_DIR volume) rejects outright with
    # PermissionError: [Errno 1] Operation not permitted, since GCS
    # objects have no POSIX permission bits to set (real crash,
    # 2026-07-25 Cloud Run run -- this is the archive path, so on local
    # disk/k3s's PVC it worked fine and this never surfaced there).
    shutil.copyfile(config.SCREEN_RESULTS_CSV_PATH, archive_path)
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
