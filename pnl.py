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
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from alpaca.trading.client import TradingClient
from alpaca.trading.models import Clock, PortfolioHistory, Position, TradeAccount
from alpaca.trading.requests import GetPortfolioHistoryRequest

import config

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _with_retry(fetch: Callable[[], _T], description: str) -> _T:
    """Calls `fetch()`, retrying once after a short delay on any failure.

    Every Alpaca read in this module is a GET (safe to retry) -- this
    exists for M19, which raises call frequency from ~1/day to ~13/day
    during market hours, meaningfully raising the odds of hitting an
    ordinary transient network blip that would otherwise fail the whole
    step and fire a false-positive alert email for a non-incident. Still
    fails closed: a second failure propagates uncaught, same posture the
    rest of this module already has (see module docstring) -- a
    persistently broken bridge must still alert, just not on one blip.
    """
    try:
        return fetch()
    except Exception:
        logger.warning(
            "%s failed once; retrying once after %.0fs.",
            description,
            config.PNL_ALPACA_RETRY_DELAY_SECONDS,
        )
        time.sleep(config.PNL_ALPACA_RETRY_DELAY_SECONDS)
        return fetch()


def market_is_open() -> bool:
    """Whether Alpaca's own market clock reports the market open right now.

    Used to gate the intraday snapshot cron (pnl-snapshot.yml, M19): its
    cron schedule is a UTC envelope wide enough to tolerate ET's DST shift
    (a fixed UTC cron can't track that shift itself), so this is the actual
    authority on "is trading happening right now," not the envelope alone.
    Builds its own TradingClient rather than sharing one with
    generate_snapshot() -- constructing a client makes no network call, so
    a second instance costs nothing, and keeping this function
    self-contained means callers (and tests) don't need to thread a client
    through just for this one read.
    """
    trading = TradingClient(
        api_key=config.ALPACA_API_KEY,
        secret_key=config.ALPACA_SECRET_KEY,
        paper=config.PAPER_TRADING,
    )
    clock = _with_retry(trading.get_clock, "get_clock")
    if not isinstance(clock, Clock):
        raise ValueError(f"unexpected get_clock() response: {clock!r}")
    return bool(clock.is_open)


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
    account = _with_retry(trading.get_account, "get_account")
    if not isinstance(account, TradeAccount):
        raise ValueError(f"unexpected get_account() response: {account!r}")

    positions = _with_retry(trading.get_all_positions, "get_all_positions")
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

    history = _with_retry(
        lambda: trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        ),
        "get_portfolio_history",
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


def write_snapshot(path: Path | None = None, snapshot: dict[str, object] | None = None) -> None:
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


def update_history_series(snapshot: dict[str, object], path: Path | None = None) -> None:
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


def _breached_positions(positions: list[dict[str, object]]) -> list[dict[str, object]]:
    """Positions at or below the loss-alert threshold.

    config.POSITION_LOSS_ALERT_THRESHOLD_PCT, user request, M20.
    """
    breached = []
    for position in positions:
        plpc = position.get("unrealized_plpc")
        if isinstance(plpc, int | float) and plpc <= config.POSITION_LOSS_ALERT_THRESHOLD_PCT:
            breached.append(position)
    return breached


def _format_loss_alert(mode: str, breached: list[dict[str, object]]) -> str:
    threshold_pct = abs(config.POSITION_LOSS_ALERT_THRESHOLD_PCT)
    lines = [
        f"\N{WARNING SIGN} **{mode.upper()} account:** {len(breached)} position(s) "
        f"down {threshold_pct:.0%} or more:"
    ]
    for position in sorted(breached, key=lambda p: str(p.get("symbol") or "")):
        symbol = position.get("symbol")
        plpc = position.get("unrealized_plpc")
        unrealized_pl = position.get("unrealized_pl")
        price = position.get("current_price")
        if (
            isinstance(plpc, int | float)
            and isinstance(unrealized_pl, int | float)
            and isinstance(price, int | float)
        ):
            # Sign before the "$", not "$-120.00" -- matches
            # report.py's own _format_dollars convention.
            pl_sign = "-" if unrealized_pl < 0 else "+" if unrealized_pl > 0 else ""
            lines.append(
                f"• **{symbol}**: {plpc:+.1%} ({pl_sign}${abs(unrealized_pl):,.2f}) @ ${price:,.2f}"
            )
        else:
            # A position with an unexpected/missing field still gets
            # flagged (it already passed the breach check above), just
            # without the numbers that field would have shown -- silently
            # dropping it from the message would hide a real loss just
            # because one of several optional fields happened to be null.
            lines.append(f"• **{symbol}**: {plpc}")
    return "\n".join(lines)


def _send_discord_alert(message: str) -> None:
    """POSTs `message` to config.DISCORD_WEBHOOK_URL.

    Failure here (a network blip, Discord being down, a bad webhook URL)
    is logged and swallowed, not raised -- unlike this module's core
    snapshot generation (which fails closed by design, see the module
    docstring), a missed Discord notification is not worth failing the
    whole P&L pipeline over.
    """
    body = json.dumps({"content": message}).encode("utf-8")
    request = urllib.request.Request(
        config.DISCORD_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Discord loss alert failed to send: %s", exc)


def check_position_loss_alerts(snapshot: dict[str, object]) -> None:
    """Sends a Discord alert if any held position is down too far.

    Past config.POSITION_LOSS_ALERT_THRESHOLD_PCT (user request, M20).
    No-op if config.DISCORD_WEBHOOK_URL is unset -- config-gated, the same
    shape as every other optional integration in this codebase
    (GRAFANA_BASE_URL, ANALYTICS_URL, etc.).
    """
    if not config.DISCORD_WEBHOOK_URL:
        return
    positions_raw = snapshot.get("positions")
    positions = (
        [p for p in positions_raw if isinstance(p, dict)] if isinstance(positions_raw, list) else []
    )
    breached = _breached_positions(positions)
    if not breached:
        return
    mode = str(snapshot.get("mode") or "paper")
    _send_discord_alert(_format_loss_alert(mode, breached))


def main() -> None:
    """Entry point invoked by `__main__`.

    Pulled out to a plain function (staff-engineer-reviewer convention,
    M19) so the market-hours/
    snapshot-only branching added for the intraday cron is directly unit-
    testable, rather than locked inside an `if __name__ == "__main__":`
    block a test can only reach via subprocess.

    Deliberately NOT swallowed into a guaranteed exit 0 -- pm-reviewer
    finding (M16): a "P&L is a display concern" argument justifies not
    aborting the trading run over this (it's a separate, later step in
    daily-trade.yml either way), but does not justify silence. This
    step's own exit code is the only thing that surfaces a snapshot
    failure through this project's existing alert channel (a failed
    step fails the job, which triggers the standard GitHub Actions
    failure email, same channel as every other alert here) -- catching
    and logging without re-raising would mean a broken bridge (a
    rotated key, a changed SDK response shape, a GCS outage) fails
    silently and gramunger.com just shows an increasingly stale
    snapshot with nothing paging anyone. market_is_open() shares this
    same fail-loud posture: an unreachable clock must not quietly
    resolve to "assume closed, skip silently."
    """
    if config.PNL_MARKET_HOURS_ONLY and not market_is_open():
        # M19: pnl-snapshot.yml's own cron envelope is wider than real
        # market hours (a fixed UTC cron can't track ET's DST shift) --
        # this is the actual authority on whether to spend an Alpaca call
        # and a GCS upload on this tick.
        logger.info("Market is closed; skipping this intraday P&L tick.")
        return

    # One Alpaca read feeds both the current-snapshot JSON and (unless
    # PNL_SNAPSHOT_ONLY) the durable append-series, so the two can never
    # disagree about the same run.
    snapshot = generate_snapshot()
    write_snapshot(snapshot=snapshot)
    if config.PNL_SNAPSHOT_ONLY:
        # M19: the durable series is daily-trade.yml's exclusive
        # responsibility (see config.PNL_SNAPSHOT_ONLY's own comment for
        # why an intraday upsert here would be unsafe) -- only the
        # current-state file refreshes on this cadence.
        logger.info("PNL_SNAPSHOT_ONLY set; skipping the durable history upsert.")
    else:
        update_history_series(snapshot)
        # M20 (user request): gated on the same PNL_SNAPSHOT_ONLY check as
        # the durable-history upsert above, for the same reason -- the
        # intraday cron (pnl-snapshot.yml) runs every 30 min for paper;
        # checking here too would mean up to 13 Discord messages/day for a
        # single position that stays down, not one. Only the canonical
        # once-daily runs (daily-trade.yml, daily-trade-live.yml) alert.
        check_position_loss_alerts(snapshot)
    history_note = (
        "" if config.PNL_SNAPSHOT_ONLY else f"; history series updated at {config.PNL_HISTORY_PATH}"
    )
    logger.info("P&L snapshot written to %s%s", config.PNL_DATA_PATH, history_note)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
