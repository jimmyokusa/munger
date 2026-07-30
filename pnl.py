"""P&L snapshot module.

New milestone (user request: "track profit and loss... for the moment
just the paper trading, but extensible for real money in the future").
Read-only against the broker -- never places or modifies an order.
Deliberately its own module, not a method on execution.ExecutionModule:
generating a P&L snapshot is a display/reporting concern, not a trading
decision, the same distinction report.py already draws (DESIGN.md 3.7:
"not wired into bot.py's own run... generating a report is a display
concern, not a trading decision"). "Extensible to real money" falls out
for free here -- this reads whichever account config.PAPER_TRADING/
ALPACA_API_KEY actually point at and labels the snapshot accordingly,
the same account-agnostic pattern execution.py already uses; nothing
here is paper-specific.

Runs where ALPACA_API_KEY lives (daily-trade.yml, GitHub Actions) -- not
in report.py's own deployment (Cloud Run/k3s), which deliberately never
has Alpaca credentials (DESIGN.md 3.5, M14's screen-only boundary). The
resulting snapshot is written to config.PNL_DATA_PATH and, in CI, pushed
to the same GCS bucket report.py's deployment reads from, since that's
the only bridge between the two systems.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.models import PortfolioHistory, Position, TradeAccount
from alpaca.trading.requests import GetPortfolioHistoryRequest

import config

logger = logging.getLogger(__name__)


def _float_or_none(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _position_snapshot(position: Position) -> dict[str, object]:
    return {
        "symbol": position.symbol,
        "qty": _float_or_none(position.qty),
        "avg_entry_price": _float_or_none(position.avg_entry_price),
        "current_price": _float_or_none(position.current_price),
        "market_value": _float_or_none(position.market_value),
        "cost_basis": _float_or_none(position.cost_basis),
        "unrealized_pl": _float_or_none(position.unrealized_pl),
        "unrealized_plpc": _float_or_none(position.unrealized_plpc),
    }


def _history_snapshot(history: PortfolioHistory) -> dict[str, object]:
    # Alpaca returns parallel lists (timestamp[i] pairs with equity[i]
    # etc.), typed as optional -- None entries (e.g. a day with no data
    # yet) are preserved positionally rather than dropped, so a consumer
    # zipping the lists doesn't silently misalign the remaining points.
    return {
        "timestamp": list(history.timestamp) if history.timestamp else [],
        "equity": [_float_or_none(v) for v in (history.equity or [])],
        "profit_loss": [_float_or_none(v) for v in (history.profit_loss or [])],
        "profit_loss_pct": [_float_or_none(v) for v in (history.profit_loss_pct or [])],
    }


def generate_snapshot() -> dict[str, object]:
    """Fetch one point-in-time P&L snapshot from Alpaca as a JSON-safe dict.

    Fails closed (no try/except) like execution.py's own account/holdings
    reads -- a broker outage here must surface as a real error, not a
    silently empty or stale-looking snapshot that report.py would render
    as if it were current.
    """
    trading = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )
    account = trading.get_account()
    if not isinstance(account, TradeAccount):
        raise ValueError(f"unexpected get_account() response: {account!r}")

    positions = trading.get_all_positions()
    if not all(isinstance(p, Position) for p in positions):
        # staff-engineer-reviewer finding: silently filtering non-Position
        # entries (as an earlier draft did) is the one place this
        # function's own "fail closed, not silently empty" promise
        # (see docstring above) wasn't actually enforced -- a schema
        # drift or a partially malformed response would understate real
        # holdings while looking like an ordinary few-positions day,
        # with nothing logged. Any unexpected entry aborts the whole
        # snapshot instead, same posture as account/history above.
        raise ValueError(f"unexpected get_all_positions() response: {positions!r}")
    position_snapshots = [_position_snapshot(p) for p in positions if isinstance(p, Position)]

    history = trading.get_portfolio_history(
        GetPortfolioHistoryRequest(period="1M", timeframe="1D")
    )
    if not isinstance(history, PortfolioHistory):
        raise ValueError(f"unexpected get_portfolio_history() response: {history!r}")

    return {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "mode": "paper" if config.PAPER_TRADING else "live",
        "account": {
            "equity": _float_or_none(account.equity),
            "cash": _float_or_none(account.cash),
            "last_equity": _float_or_none(account.last_equity),
            "portfolio_value": _float_or_none(account.portfolio_value),
        },
        "positions": position_snapshots,
        "history": _history_snapshot(history),
    }


def write_snapshot(
    path: Path | None = None, snapshot: dict[str, object] | None = None
) -> None:
    """Write a snapshot atomically (temp file + rename).

    Matches this codebase's existing atomic-write pattern (StateTracker.
    save, screener._write_csv_atomically) -- a crash mid-write must never
    leave a torn/partial file for report.py or a GCS upload step to read.

    Fetches a fresh snapshot if one isn't supplied; __main__ passes the
    snapshot it already fetched so the append-series (update_history_series)
    and the JSON snapshot come from the *same* Alpaca read, not two.
    """
    target = path if path is not None else config.PNL_DATA_PATH
    if snapshot is None:
        snapshot = generate_snapshot()
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2))
    tmp_path.replace(target)


def _snapshot_date(generated_at: str) -> str:
    """ISO date (UTC) a snapshot belongs to, from its generated_at string.

    A generated_at with no offset is treated as UTC rather than assumed
    local -- the same "a naive timestamp is not silently reinterpreted"
    care report.py's _pnl_staleness_note learned the hard way (M16).
    """
    dt = datetime.datetime.fromisoformat(generated_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC).date().isoformat()


def _series_rows_from_snapshot(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    """Per-day {date, equity, profit_loss, mode} rows derived from a snapshot.

    Past days come from Alpaca's portfolio history (end-of-day, authoritative);
    today is overlaid from the account snapshot (the freshest value, which
    overwrites history's partial same-day point). Keyed by ISO date so the
    caller can upsert idempotently. Days Alpaca reports with no data (None
    timestamp or equity) are skipped, not fabricated as a zero point.
    """
    mode = snapshot.get("mode")
    rows: dict[str, dict[str, object]] = {}

    history = snapshot.get("history") or {}
    assert isinstance(history, dict)
    timestamps = history.get("timestamp") or []
    equities = history.get("equity") or []
    profit_losses = history.get("profit_loss") or []
    # Indexed, not zip(strict=False): if Alpaca returns parallel arrays of
    # unequal length (a degraded response), zip's truncation would silently
    # drop the *trailing* (most recent) timestamp/equity points whenever
    # profit_loss is the short array, rather than keeping them with a null
    # profit_loss -- this is a display/history path, not a trade, so a
    # missing profit_loss shouldn't cost the whole day's point.
    n = max(len(timestamps), len(equities), len(profit_losses))
    for i in range(n):
        ts = timestamps[i] if i < len(timestamps) else None
        equity = equities[i] if i < len(equities) else None
        profit_loss = profit_losses[i] if i < len(profit_losses) else None
        if ts is None or equity is None:
            continue
        date = datetime.datetime.fromtimestamp(ts, datetime.UTC).date().isoformat()
        rows[date] = {"date": date, "equity": equity, "profit_loss": profit_loss, "mode": mode}

    account = snapshot.get("account") or {}
    assert isinstance(account, dict)
    equity = account.get("equity")
    generated_at = snapshot.get("generated_at")
    if equity is not None and isinstance(generated_at, str):
        today = _snapshot_date(generated_at)
        last_equity = account.get("last_equity")
        profit_loss = None if last_equity is None else equity - last_equity
        rows[today] = {"date": today, "equity": equity, "profit_loss": profit_loss, "mode": mode}

    return rows


def update_history_series(
    snapshot: dict[str, object], path: Path | None = None
) -> None:
    """Upsert a snapshot's daily points into the durable append-series file.

    Read-modify-write (GCS objects can't be appended in place; the file is
    tiny -- one row/day). Idempotent by date: re-running a day overwrites its
    row, never appends a duplicate. Seeds the past from the snapshot's Alpaca
    history on first run, and -- the whole reason this file exists --
    *preserves* days already recorded that have since scrolled out of Alpaca's
    rolling history window. Atomic write (temp + rename), like write_snapshot.
    See DESIGN_DASHBOARDS.md 2.1.
    """
    target = path if path is not None else config.PNL_HISTORY_PATH

    existing: dict[str, dict[str, object]] = {}
    if target.exists():
        for line in target.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            existing[row["date"]] = row

    # mode is not refreshed on an already-recorded date: it records which
    # regime (paper/live) was actually in effect *that day*, so once
    # config.PAPER_TRADING ever flips, a later run must not relabel prior
    # paper-mode history as live just because the account's *current* mode
    # changed. Only a genuinely new date takes its mode from this snapshot.
    for date, row in _series_rows_from_snapshot(snapshot).items():
        if date in existing:
            row["mode"] = existing[date]["mode"]
        existing[date] = row
    ordered = [existing[date] for date in sorted(existing)]

    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text("".join(json.dumps(row) + "\n" for row in ordered))
    tmp_path.replace(target)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Deliberately NOT swallowed into a guaranteed exit 0 -- pm-reviewer
    # finding: a "P&L is a display concern" argument justifies not
    # aborting the trading run over this (it's a separate, later step in
    # daily-trade.yml either way), but does not justify silence. This
    # step's own exit code is the only thing that surfaces a snapshot
    # failure through this project's existing alert channel (a failed
    # step fails the job, which triggers the standard GitHub Actions
    # failure email, same channel as every other alert here) -- catching
    # and logging without re-raising would mean a broken bridge (a
    # rotated key, a changed SDK response shape, a GCS outage) fails
    # silently and gramunger.com just shows an increasingly stale
    # snapshot with nothing paging anyone.
    # One Alpaca read feeds both the current-snapshot JSON and the durable
    # append-series, so the two can never disagree about the same run.
    snapshot = generate_snapshot()
    write_snapshot(snapshot=snapshot)
    update_history_series(snapshot)
    logger.info(
        "P&L snapshot written to %s; history series updated at %s",
        config.PNL_DATA_PATH,
        config.PNL_HISTORY_PATH,
    )
