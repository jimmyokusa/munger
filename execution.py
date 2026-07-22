"""Execution module (DESIGN.md section 3.5). Thin wrapper around Alpaca.

Every order (buy or liquidation) carries a deterministic client_order_id
-- a function of run-date, ticker, and side -- so the broker itself
rejects a duplicate submission from a crashed-and-restarted run. This is
the primary idempotency guarantee, stronger than any client-side
pre-check: a client-side check of "today's open orders" alone misses the
case where a run submits an order, Alpaca fills it immediately, and the
process crashes before the journal write -- on restart the order is no
longer open, so that check alone would see nothing and double-submit.
`has_already_submitted` is the secondary guard.

Every order also carries a limit-price band (config.LIMIT_PRICE_BAND_PCT)
rather than an unconstrained market order, so a liquidation firing during
a flash crash or a thin after-hours window can't execute at an
arbitrarily bad price. alpaca-py's `close_position` doesn't accept a
limit price (confirmed against the installed SDK: `ClosePositionRequest`
only has `qty`/`percentage` fields, no order-type control) -- to get a
limit-price band on liquidations too, this module submits an explicit
sell LIMIT order for the full position quantity instead of using
`close_position`.
"""

from __future__ import annotations

import logging

from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import LimitOrderRequest

import config

logger = logging.getLogger(__name__)

Holdings = dict[str, float]

# submit_order can return HTTP 200 with an Order whose status reflects an
# immediate rejection (e.g. wash-trade prevention) rather than raising --
# these must be treated as a failed attempt, not a successful submission,
# or a caller checking only "is the return value None" would wrongly
# treat a rejected order as filled/working.
_FAILED_ORDER_STATUSES = frozenset(
    {OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.SUSPENDED}
)


def _submit_and_check(
    trading: TradingClient, request: LimitOrderRequest, symbol: str
) -> Order | None:
    order = trading.submit_order(request)
    if not isinstance(order, Order):
        return None
    if order.status in _FAILED_ORDER_STATUSES:
        logger.error("%s: order submitted but immediately %s", symbol, order.status)
        return None
    return order


class ExecutionModule:
    """Thin wrapper around Alpaca's paper/live trading API (DESIGN.md 3.5).

    `run_date` (e.g. "2026-07-21") is an explicit constructor argument,
    not read from the system clock internally -- keeps client_order_id
    generation a pure, testable function of its inputs rather than
    depending on wall-clock time.
    """

    def __init__(self, run_date: str) -> None:
        """Build the Alpaca trading and market-data clients for this run."""
        self._run_date = run_date
        self._trading = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )
        self._data = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY, secret_key=config.ALPACA_SECRET_KEY
        )

    def _client_order_id(self, symbol: str, side: str) -> str:
        return f"{self._run_date}-{symbol}-{side}"

    def _limit_price(self, symbol: str, side: str) -> float:
        """Last trade price adjusted by the configured band.

        Buys get a ceiling above last trade (won't pay more than that);
        sells/liquidations get a floor below it (won't accept less) --
        the band always works in the requester's favor for slippage
        protection, not against it.
        """
        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trades = self._data.get_stock_latest_trade(request)
        last_price = float(trades[symbol].price)
        if side == "buy":
            return last_price * (1 + config.LIMIT_PRICE_BAND_PCT)
        return last_price * (1 - config.LIMIT_PRICE_BAND_PCT)

    def has_already_submitted(self, client_order_id: str) -> bool:
        """Secondary idempotency guard, checked before market_buy/liquidate.

        Raises if the query itself fails, rather than catching and
        returning a default -- DESIGN.md 3.5 requires this to fail
        closed (the caller should abort the run) since submitting
        orders without knowing what already happened defeats the point
        of the check. Unlike market_buy/liquidate, which catch their own
        errors so one rejected order never aborts the run.
        """
        try:
            self._trading.get_order_by_client_id(client_order_id)
            return True
        except APIError as e:
            if e.status_code == 404:
                return False
            raise

    def get_current_holdings(self) -> Holdings:
        """Live portfolio fetch: ticker -> current market value (dollars).

        Consumed every run by portfolio.process_sells and
        portfolio.generate_buy_queue -- never read from local state
        (DESIGN.md 3.4). Alpaca's Position.market_value is typed
        optional (str | None); a position with no value is skipped with
        a warning rather than coerced to 0.0 -- an actual held position
        silently treated as worth nothing would corrupt the buy-queue's
        weight math, worse than just omitting it from this run's view.

        Deliberately fails closed: no try/except here, unlike
        market_buy/liquidate. This is the primary input to both the sell
        and buy decisions for the whole run -- returning an empty dict
        on a broker outage would look identical to "you own nothing" and
        could drive the bot to buy positions it already holds. The
        caller (bot.py, M10) must let this propagate and abort the run,
        the same fail-closed posture DESIGN.md 3.5 requires for the
        idempotency pre-check.
        """
        positions = self._trading.get_all_positions()
        holdings: Holdings = {}
        for p in positions:
            if not isinstance(p, Position):
                continue
            if p.market_value is None:
                logger.warning("%s: open position has no market_value, excluding", p.symbol)
                continue
            holdings[p.symbol] = float(p.market_value)
        return holdings

    def market_buy(self, symbol: str, notional: float) -> Order | None:
        """Submit a notional-sized buy order with a limit-price band.

        UNVERIFIED, first thing to smoke-test once an Alpaca account
        exists (TASKS.md M8): Alpaca's own documentation is genuinely
        contradictory about whether `notional` (dollar-amount) sizing is
        accepted on LIMIT orders specifically. The general API reference
        (docs.alpaca.markets/reference/postorder) states notional "can
        only work for market order types"; the dedicated fractional-
        trading page (docs.alpaca.markets/docs/fractional-trading)
        states limit orders "are supported for both fractional and
        notional orders." The installed alpaca-py SDK's own docstring
        ("Fractional qty for stocks only with market orders") reads like
        stale boilerplate shared verbatim across every order-type class,
        not a per-type constraint. This submits `notional` directly, the
        simpler reading and the one matching DESIGN.md 3.5's literal
        wording -- if wrong, the order is rejected, which is caught
        either as a raised exception or as a same-response `status` of
        REJECTED/CANCELED/EXPIRED/SUSPENDED (Alpaca can return HTTP 200
        with a rejected order rather than raising, e.g. for wash-trade
        prevention -- `_submit_and_check` inspects `order.status`
        precisely so that case isn't mistaken for a successful
        submission). Either way this method returns None like any other
        failed attempt, so getting this wrong doesn't risk an incorrect
        execution, only a silently-empty buy queue until caught by the
        smoke test.

        Returns the submitted Order, or None if the attempt failed --
        every order attempt is wrapped in error handling so one rejected
        order never aborts the run (DESIGN.md 3.5).
        """
        try:
            limit_price = self._limit_price(symbol, "buy")
            request = LimitOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=self._client_order_id(symbol, "buy"),
            )
            return _submit_and_check(self._trading, request, symbol)
        except Exception:
            logger.error("%s: buy order failed", symbol, exc_info=True)
            return None

    def liquidate(self, symbol: str) -> Order | None:
        """Close the full position in `symbol` with a limit-price band.

        Returns the submitted Order, or None if the attempt failed (same
        per-order fault tolerance as market_buy).
        """
        try:
            position = self._trading.get_open_position(symbol)
            if not isinstance(position, Position):
                raise TypeError(f"unexpected get_open_position response type: {type(position)}")
            limit_price = self._limit_price(symbol, "sell")
            request = LimitOrderRequest(
                symbol=symbol,
                qty=abs(float(position.qty)),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=self._client_order_id(symbol, "sell"),
            )
            return _submit_and_check(self._trading, request, symbol)
        except Exception:
            logger.error("%s: liquidation failed", symbol, exc_info=True)
            return None
