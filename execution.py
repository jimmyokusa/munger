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

import datetime
import logging

from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import Asset, Order, Position, TradeAccount
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
        # Account-tagged (M20, DESIGN_REAL_MONEY.md §3.5): not needed for
        # broker-side idempotency -- paper and live are separate Alpaca
        # endpoints reached with separate credentials, so a bare
        # "run_date-symbol-side" id was never at risk of colliding across
        # accounts there. This is independent defense-in-depth for a
        # human (or journal.py, see §3.4) reading client_order_id out of
        # context: it's self-describing which account an order belongs to
        # even from a raw id string alone. Changing this format is safe
        # for existing (paper) runs -- client_order_id only needs to be
        # unique within a single day's run for the has_already_submitted
        # crash-restart check to work, not stable across the change.
        mode = "paper" if config.PAPER_TRADING else "live"
        return f"{mode}-{self._run_date}-{symbol}-{side}"

    def _last_trade_price(self, symbol: str) -> float:
        """The symbol's last trade price, unadjusted by any band.

        Split out from _limit_price (staff-engineer-reviewer finding) so
        the ADV ceiling check can size its implied-shares estimate off
        the price a fill is actually likely to land near, not off the
        limit price -- see market_buy's own comment on why those two are
        not interchangeable for that specific estimate.
        """
        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trades = self._data.get_stock_latest_trade(request)
        return float(trades[symbol].price)

    def _limit_price(self, symbol: str, side: str, last_price: float) -> float:
        """Last trade price adjusted by the configured band.

        Buys get a ceiling above last trade (won't pay more than that);
        sells/liquidations get a floor below it (won't accept less) --
        the band always works in the requester's favor for slippage
        protection, not against it.
        """
        if side == "buy":
            return last_price * (1 + config.LIMIT_PRICE_BAND_PCT)
        return last_price * (1 - config.LIMIT_PRICE_BAND_PCT)

    def _average_daily_volume(self, symbol: str) -> float | None:
        """Mean daily volume over config.ADV_LOOKBACK_TRADING_DAYS.

        None if no bars came back (fails open on the ADV check itself --
        see _exceeds_adv_ceiling for why).

        M26e (Design v2.2 §3.3). Fetches roughly 1.5x the lookback in
        calendar days to comfortably cover weekends/holidays and still
        land at least `ADV_LOOKBACK_TRADING_DAYS` trading days, then
        averages whatever bars actually came back (not a fixed-size
        slice) -- a newly-listed name with fewer bars than the lookback
        still gets a real, if noisier, average rather than an index
        error.
        """
        end = datetime.datetime.now(datetime.UTC)
        start = end - datetime.timedelta(days=int(config.ADV_LOOKBACK_TRADING_DAYS * 1.5))
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end
        )
        bar_set = self._data.get_stock_bars(request)
        bars = bar_set.data.get(symbol, []) if hasattr(bar_set, "data") else []
        # len(bars) == 0, not "not bars" -- a falsy-vs-empty distinction
        # that only matters against an unconfigured test double (a bare
        # MagicMock is truthy by default regardless of its length), but
        # checking length explicitly is correct either way and avoids
        # depending on a mock's default __bool__ behavior.
        if len(bars) == 0:
            return None
        return sum(float(bar.volume) for bar in bars) / len(bars)

    def _exceeds_adv_ceiling(self, symbol: str, shares: float) -> bool:
        """True if `shares` exceeds the ADV ceiling for `symbol`.

        More than config.MAX_ORDER_PCT_OF_ADV of the symbol's own recent
        average daily volume. Fails open (returns False, i.e. does NOT
        block the order) when
        ADV can't be determined -- deliberately the opposite fail
        direction from this module's broker-state checks
        (get_current_holdings, has_already_submitted), which fail
        closed because they gate on facts this module cannot safely
        guess. ADV is a secondary risk control layered on top of the
        limit-price band, not the primary defense against a bad fill;
        a missing bars response (a new listing, a data-provider gap)
        blocking every order for that symbol would be a worse outcome
        than occasionally submitting one this check couldn't evaluate.
        """
        adv = self._average_daily_volume(symbol)
        if adv is None or adv <= 0:
            return False
        return shares > adv * config.MAX_ORDER_PCT_OF_ADV

    def verify_account_access(self) -> None:
        """Abort-worthy startup check: keys must match the configured mode.

        Alpaca segregates paper and live keys by endpoint, so a
        successful get_account() call under this client's configured
        `paper` flag inherently proves the keys match that mode -- a
        mismatch (e.g. live keys present while config says paper, from a
        stale .env) raises instead of silently proceeding. Deliberately
        has no try/except: the caller (bot.py) must let this abort the
        run, the same fail-closed posture as the other broker pre-checks.
        """
        self._trading.get_account()

    def get_available_cash(self) -> float:
        """Current account cash balance, for generate_buy_queue's input.

        Fails closed like get_current_holdings -- no try/except; a
        broker outage here (including a None `cash` field, which the
        SDK types as optional) must abort the run, not silently proceed
        with a wrong figure (DESIGN.md 3.4).
        """
        account = self._trading.get_account()
        if not isinstance(account, TradeAccount) or account.cash is None:
            raise ValueError(f"unexpected get_account() response: {account!r}")
        return float(account.cash)

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

    def get_order_status(self, client_order_id: str) -> Order:
        """Fetch the current broker-side status of one submitted order.

        M26b (Design v2.2 §3.3): the settlement pass's own query
        primitive -- a thin, public wrapper around the same
        `get_order_by_client_id` call `has_already_submitted` already
        makes internally, exposed here so `settlement.py` can query
        without reaching into this module's private `_trading` client.
        Deliberately has no try/except of its own: the caller
        (`settlement.py`) is the one that decides how to classify and
        retry a query failure (§3.3's "query failure vs. genuinely
        pending" distinction), which requires seeing the raised
        exception, not a swallowed `None`.
        """
        order = self._trading.get_order_by_client_id(client_order_id)
        if not isinstance(order, Order):
            raise TypeError(f"unexpected get_order_by_client_id response: {type(order)}")
        return order

    def is_corporate_action(self, symbol: str) -> bool:
        """True if `symbol` is reported inactive or untradable.

        Active detection of a delisting/symbol change/merger/spin-off,
        per Design v2.2 §3.2 (M29b), via Alpaca's own Assets API.

        RC2 (Design v2.2 §2): absence of data used to be the only signal
        a corporate action existed, indistinguishable from a transient
        fetch failure -- exactly what let a delisting strike a holding
        toward liquidation the same way a genuine quality failure would.
        This checks the fact directly instead of inferring it: Alpaca's
        `status` field goes INACTIVE and `tradable` goes False when the
        exchange delists/suspends a symbol, independent of whether
        yfinance (this system's fundamentals source) can still fetch
        anything for it.

        Fails open (returns False) on a lookup failure -- deliberately
        the same fail direction as _exceeds_adv_ceiling (a secondary
        signal, not this module's primary broker-state check) and
        deliberately the OPPOSITE of get_current_holdings/
        has_already_submitted, which fail closed because a broken check
        there would corrupt the primary buy/sell decision itself. Here,
        failing open just means a genuine corporate action gets
        classified as UNREADABLE instead of CORPORATE_ACTION for one
        run (still no strike, no auto-trade either way -- see
        portfolio.classify_holding_state) rather than blocking every
        other holding's evaluation on one broken Assets lookup.
        """
        try:
            asset = self._trading.get_asset(symbol)
        except Exception:
            logger.error("%s: corporate-action check (Assets API) failed", symbol, exc_info=True)
            return False
        if not isinstance(asset, Asset):
            logger.error("%s: unexpected get_asset response type: %s", symbol, type(asset))
            return False
        return asset.status != AssetStatus.ACTIVE or not bool(asset.tradable)

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
        order never aborts the run (DESIGN.md 3.5). The idempotency guard
        (has_already_submitted) is checked outside that error handling,
        not inside it -- it must fail closed and abort the run if the
        check itself is broken, same as get_current_holdings, rather
        than being silently swallowed as "this one order failed" like a
        genuine rejection would be.
        """
        client_order_id = self._client_order_id(symbol, "buy")
        if self.has_already_submitted(client_order_id):
            logger.warning(
                "%s: buy already submitted for this run-date -- recovering the existing "
                "order instead of resubmitting (likely a crash-and-restart)",
                symbol,
            )
            existing = self._trading.get_order_by_client_id(client_order_id)
            return existing if isinstance(existing, Order) else None
        try:
            last_price = self._last_trade_price(symbol)
            limit_price = self._limit_price(symbol, "buy", last_price)
            # M26e (Design v2.2 §3.3): notional has no share count until
            # priced -- estimate it against last trade price, not the
            # (higher, for a buy) limit price. Staff-engineer-reviewer
            # finding: dividing by the limit-price ceiling understates
            # the real share count for any fill that lands below that
            # ceiling, which a DAY limit order routinely does -- last
            # trade price is the better estimate of what a fill is
            # actually likely to cost per share, even though the order
            # itself is still submitted at the wider limit price above.
            implied_shares = notional / last_price
            if self._exceeds_adv_ceiling(symbol, implied_shares):
                logger.error(
                    "%s: buy order (~%.0f shares) exceeds %.1f%% of average daily volume -- "
                    "skipping this run, not resubmitting at a smaller size automatically",
                    symbol,
                    implied_shares,
                    config.MAX_ORDER_PCT_OF_ADV * 100,
                )
                return None
            request = LimitOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=client_order_id,
            )
            return _submit_and_check(self._trading, request, symbol)
        except Exception:
            logger.error("%s: buy order failed", symbol, exc_info=True)
            return None

    def liquidate(self, symbol: str) -> Order | None:
        """Close the full position in `symbol` with a limit-price band.

        Returns the submitted Order, or None if the attempt failed (same
        per-order fault tolerance and idempotency-guard placement as
        market_buy).
        """
        client_order_id = self._client_order_id(symbol, "sell")
        if self.has_already_submitted(client_order_id):
            logger.warning(
                "%s: liquidation already submitted for this run-date -- recovering the "
                "existing order instead of resubmitting (likely a crash-and-restart)",
                symbol,
            )
            existing = self._trading.get_order_by_client_id(client_order_id)
            return existing if isinstance(existing, Order) else None
        try:
            position = self._trading.get_open_position(symbol)
            if not isinstance(position, Position):
                raise TypeError(f"unexpected get_open_position response type: {type(position)}")
            qty = abs(float(position.qty))
            # M26e (Design v2.2 §3.3): applied to liquidations too, not
            # just buys, matching the design's own undifferentiated
            # wording -- a real tradeoff, since blocking a quality-
            # driven sell leaves a deteriorating position open rather
            # than reducing risk. Accepted anyway for consistency and
            # because the failure mode is the same one process_sells
            # already tolerates for an unconfirmed order (see M26c):
            # the position stays held, un-struck, and gets a fresh
            # look next run rather than silently vanishing from view.
            if self._exceeds_adv_ceiling(symbol, qty):
                logger.error(
                    "%s: liquidation (%.0f shares) exceeds %.1f%% of average daily volume -- "
                    "skipping this run; position remains held and will be re-evaluated next run",
                    symbol,
                    qty,
                    config.MAX_ORDER_PCT_OF_ADV * 100,
                )
                return None
            limit_price = self._limit_price(symbol, "sell", self._last_trade_price(symbol))
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=client_order_id,
            )
            return _submit_and_check(self._trading, request, symbol)
        except Exception:
            logger.error("%s: liquidation failed", symbol, exc_info=True)
            return None
