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


def write_snapshot(path: Path | None = None) -> None:
    """Fetch a snapshot and write it atomically (temp file + rename).

    Matches this codebase's existing atomic-write pattern (StateTracker.
    save, screener._write_csv_atomically) -- a crash mid-write must never
    leave a torn/partial file for report.py or a GCS upload step to read.
    """
    target = path if path is not None else config.PNL_DATA_PATH
    snapshot = generate_snapshot()
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2))
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
    write_snapshot()
    logger.info("P&L snapshot written to %s", config.PNL_DATA_PATH)
