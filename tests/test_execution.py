"""Unit tests for execution.py, mocking the Alpaca SDK (no live credentials
available in this environment -- see TASKS.md M8 for what's still blocked
on live verification)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.models.trades import Trade
from alpaca.trading.enums import AssetStatus, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import Asset, Order, Position, TradeAccount

import config
import execution


def _fake_api_error(status_code: int) -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status_code))
    # alpaca-py's APIError.__init__ has no type annotations in the SDK
    # itself, not something fixable from this side.
    return APIError(error='{"message": "not found"}', http_error=http_error)  # type: ignore[no-untyped-call]


def _fake_accepted_order() -> MagicMock:
    # pydantic v2 model fields don't appear in dir(Order), so spec=Order
    # alone doesn't let a MagicMock auto-generate `.status` on access --
    # it must be set explicitly, and to a non-failed status so
    # _submit_and_check's status check doesn't treat this as a rejection.
    order = MagicMock(spec=Order)
    order.status = OrderStatus.NEW
    return order


def _fake_trade(price: float) -> MagicMock:
    # spec=Trade, not a bare class -- staff-engineer-reviewer finding: an
    # unspec'd fake would keep passing even if the real latest-trade
    # response shape (e.g. a differently-named price field) diverged,
    # exactly the live-vs-test-green gap this project's other SDK-facing
    # tests (universe.py, data.py) are careful to avoid.
    trade = MagicMock(spec=Trade)
    trade.price = price
    return trade


def _fake_bar_set(symbol: str, volumes: list[float]) -> SimpleNamespace:
    """Stand-in for Alpaca's BarSet -- this code only ever reads
    `.data[symbol]` and each bar's `.volume`, so a bare SimpleNamespace
    is enough without pulling in the real (heavier) Bar/BarSet models."""
    bars = [SimpleNamespace(volume=v) for v in volumes]
    return SimpleNamespace(data={symbol: bars})


def _fake_position(symbol: str, market_value: float, qty: float) -> MagicMock:
    # execution.py does isinstance(p, Position) checks (needed for mypy
    # narrowing against the SDK's Position | dict return type) -- a plain
    # custom class would silently fail that check and get skipped, unlike
    # a spec'd MagicMock, which passes isinstance(mock, Position).
    position = MagicMock(spec=Position)
    position.symbol = symbol
    position.market_value = market_value
    position.qty = qty
    return position


class _Setup(NamedTuple):
    """Bundles the ExecutionModule under test with explicitly-typed mocks
    for its two Alpaca clients -- avoids reaching into module._trading/
    module._data directly, which mypy would still see as the real
    (non-mock) SDK client types even after monkeypatching the classes."""

    module: execution.ExecutionModule
    trading: MagicMock
    data: MagicMock


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> _Setup:
    trading_mock = MagicMock()
    # Default: nothing has been submitted yet under any client_order_id,
    # so market_buy/liquidate's has_already_submitted guard doesn't
    # short-circuit tests that expect a fresh submission.
    trading_mock.get_order_by_client_id.side_effect = _fake_api_error(404)
    data_mock = MagicMock()
    monkeypatch.setattr(execution, "TradingClient", lambda **kwargs: trading_mock)
    monkeypatch.setattr(execution, "StockHistoricalDataClient", lambda **kwargs: data_mock)
    module = execution.ExecutionModule(run_date="2026-07-21")
    return _Setup(module, trading_mock, data_mock)


def test_client_order_id_is_deterministic(setup: _Setup) -> None:
    assert setup.module._client_order_id("AAPL", "buy") == "paper-2026-07-21-AAPL-buy"
    assert setup.module._client_order_id("AAPL", "buy") == setup.module._client_order_id(
        "AAPL", "buy"
    )


def test_client_order_id_is_tagged_live_when_paper_trading_is_false(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # M20 (DESIGN_REAL_MONEY.md §3.5): defense-in-depth so a raw
    # client_order_id string is self-describing which account it belongs
    # to, independent of journal.py's own account column.
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    assert setup.module._client_order_id("AAPL", "buy") == "live-2026-07-21-AAPL-buy"


def test_limit_price_buy_is_above_last_trade(setup: _Setup) -> None:
    price = setup.module._limit_price("AAPL", "buy", 100.0)
    assert price == pytest.approx(100.0 * (1 + config.LIMIT_PRICE_BAND_PCT))


def test_limit_price_sell_is_below_last_trade(setup: _Setup) -> None:
    price = setup.module._limit_price("AAPL", "sell", 100.0)
    assert price == pytest.approx(100.0 * (1 - config.LIMIT_PRICE_BAND_PCT))


def test_last_trade_price_returns_the_raw_unadjusted_price(setup: _Setup) -> None:
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    assert setup.module._last_trade_price("AAPL") == pytest.approx(100.0)


def test_has_already_submitted_true_when_order_found(setup: _Setup) -> None:
    setup.trading.get_order_by_client_id.side_effect = None
    setup.trading.get_order_by_client_id.return_value = MagicMock(spec=Order)
    assert setup.module.has_already_submitted("2026-07-21-AAPL-buy") is True


def test_has_already_submitted_false_on_404(setup: _Setup) -> None:
    setup.trading.get_order_by_client_id.side_effect = _fake_api_error(404)
    assert setup.module.has_already_submitted("2026-07-21-AAPL-buy") is False


def test_has_already_submitted_reraises_on_other_errors(setup: _Setup) -> None:
    # Fail closed, per DESIGN.md 3.5: a broken pre-check query must abort
    # the run, not silently proceed as if nothing had been submitted yet.
    setup.trading.get_order_by_client_id.side_effect = _fake_api_error(500)
    with pytest.raises(APIError):
        setup.module.has_already_submitted("2026-07-21-AAPL-buy")


def test_get_current_holdings_maps_symbol_to_market_value(setup: _Setup) -> None:
    setup.trading.get_all_positions.return_value = [
        _fake_position("AAPL", 1000.0, 5),
        _fake_position("MSFT", 2000.0, 10),
    ]
    assert setup.module.get_current_holdings() == {"AAPL": 1000.0, "MSFT": 2000.0}


def test_verify_account_access_calls_get_account(setup: _Setup) -> None:
    setup.module.verify_account_access()
    setup.trading.get_account.assert_called_once()


def test_verify_account_access_reraises_on_auth_failure(setup: _Setup) -> None:
    setup.trading.get_account.side_effect = _fake_api_error(403)
    with pytest.raises(APIError):
        setup.module.verify_account_access()


def test_get_available_cash_returns_the_account_cash_balance(setup: _Setup) -> None:
    account = MagicMock(spec=TradeAccount)
    account.cash = "12345.67"
    setup.trading.get_account.return_value = account
    assert setup.module.get_available_cash() == pytest.approx(12345.67)


def test_get_available_cash_raises_when_cash_is_none(setup: _Setup) -> None:
    account = MagicMock(spec=TradeAccount)
    account.cash = None
    setup.trading.get_account.return_value = account
    with pytest.raises(ValueError):
        setup.module.get_available_cash()


def test_market_buy_submits_a_limit_order_with_the_band_and_client_order_id(
    setup: _Setup,
) -> None:
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    fake_order = _fake_accepted_order()
    setup.trading.submit_order.return_value = fake_order

    result = setup.module.market_buy("AAPL", 500.0)

    assert result is fake_order
    submitted_request = setup.trading.submit_order.call_args.args[0]
    expected_limit_price = 100.0 * (1 + config.LIMIT_PRICE_BAND_PCT)
    assert submitted_request.notional == 500.0
    assert submitted_request.side == OrderSide.BUY
    assert submitted_request.time_in_force == TimeInForce.DAY
    assert submitted_request.client_order_id == "paper-2026-07-21-AAPL-buy"
    assert submitted_request.limit_price == pytest.approx(expected_limit_price, rel=1e-4)


def test_market_buy_returns_none_on_failure_without_raising(setup: _Setup) -> None:
    setup.data.get_stock_latest_trade.side_effect = Exception("network error")
    result = setup.module.market_buy("AAPL", 500.0)
    assert result is None


def test_market_buy_returns_none_when_order_is_rejected_without_raising(
    setup: _Setup,
) -> None:
    # staff-engineer-reviewer finding: Alpaca can return HTTP 200 with a
    # rejected order (e.g. wash-trade prevention) rather than raising --
    # submit_order not raising must not be mistaken for a successful buy.
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    rejected_order = MagicMock(spec=Order)
    rejected_order.status = OrderStatus.REJECTED
    setup.trading.submit_order.return_value = rejected_order

    result = setup.module.market_buy("AAPL", 500.0)

    assert result is None


# --- M26e: average daily volume + the ADV ceiling ---


def test_average_daily_volume_averages_recent_bars(setup: _Setup) -> None:
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [100.0, 200.0, 300.0])
    assert setup.module._average_daily_volume("AAPL") == pytest.approx(200.0)


def test_average_daily_volume_returns_none_when_no_bars_at_all(setup: _Setup) -> None:
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [])
    assert setup.module._average_daily_volume("AAPL") is None


def test_average_daily_volume_requests_the_iex_feed_explicitly(setup: _Setup) -> None:
    # Real incident, 2026-09-04: an unset `feed` risks the SDK defaulting
    # to SIP, which a real account without a paid market-data
    # subscription can't query for a recent date range ("subscription
    # does not permit querying recent SIP data") -- confirmed against a
    # live paper-account run, not a hypothesis. IEX must be requested
    # explicitly, not left to an undocumented default.
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [100.0])
    setup.module._average_daily_volume("AAPL")
    request = setup.data.get_stock_bars.call_args[0][0]
    assert request.feed == DataFeed.IEX


def test_average_daily_volume_fails_open_when_the_bars_request_raises(
    setup: _Setup,
) -> None:
    # Real incident, 2026-09-04: the first live buy attempt after a
    # kill-switch/reconciliation fix hit exactly this -- get_stock_bars
    # raised APIError (a 403 on the SIP feed), which propagated straight
    # through _average_daily_volume and _exceeds_adv_ceiling into
    # market_buy's broad except, failing the ENTIRE buy rather than just
    # this secondary check. This function's own docstring already
    # promised "a missing bars response... would be a worse outcome" than
    # occasionally skipping the check -- that promise only held for an
    # empty-but-successful response before this test; an exception must
    # fail open the same way.
    setup.data.get_stock_bars.side_effect = _fake_api_error(403)
    assert setup.module._average_daily_volume("AAPL") is None


def test_exceeds_adv_ceiling_fails_open_when_the_bars_request_raises(
    setup: _Setup,
) -> None:
    setup.data.get_stock_bars.side_effect = _fake_api_error(403)
    assert setup.module._exceeds_adv_ceiling("AAPL", 1_000_000.0) is False


def test_market_buy_proceeds_when_the_adv_bars_request_raises(
    setup: _Setup,
) -> None:
    # End-to-end version of the real incident: a buy must still be
    # attempted (and reach the broker) when the ADV check's own data
    # fetch fails, not be silently dropped as "buy order failed."
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    setup.data.get_stock_bars.side_effect = _fake_api_error(403)
    setup.trading.submit_order.return_value = _fake_accepted_order()

    result = setup.module.market_buy("AAPL", 5_000.0)

    assert result is not None
    setup.trading.submit_order.assert_called_once()


def test_last_trade_price_requests_the_iex_feed_explicitly(setup: _Setup) -> None:
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    setup.module._last_trade_price("AAPL")
    request = setup.data.get_stock_latest_trade.call_args[0][0]
    assert request.feed == DataFeed.IEX


def test_exceeds_adv_ceiling_true_past_the_configured_fraction(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_ORDER_PCT_OF_ADV", 0.01)
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [1_000_000.0])
    assert setup.module._exceeds_adv_ceiling("AAPL", 10_001.0) is True  # just past 1% of 1M
    assert setup.module._exceeds_adv_ceiling("AAPL", 9_999.0) is False  # just under


def test_exceeds_adv_ceiling_fails_open_when_adv_is_unknown(setup: _Setup) -> None:
    # A missing/empty bars response must not block every order for a
    # symbol it happens to fail on -- ADV is a secondary risk control,
    # not the primary defense (see the docstring's own reasoning).
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [])
    assert setup.module._exceeds_adv_ceiling("AAPL", 1_000_000.0) is False


def test_market_buy_skipped_when_it_exceeds_the_adv_ceiling(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_ORDER_PCT_OF_ADV", 0.01)
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    # ~1% of 1,000 shares/day ADV is ~10 shares -- a $5,000 order at
    # ~$100/share (last trade, what the estimate is sized against) implies
    # ~50 shares, well past it.
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [1_000.0])

    result = setup.module.market_buy("AAPL", 5_000.0)

    assert result is None
    setup.trading.submit_order.assert_not_called()  # never even reaches the broker


def test_market_buy_adv_check_uses_last_trade_price_not_the_wider_limit_price(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Staff-engineer-reviewer finding: sizing the ADV estimate off the
    # (higher, for a buy) limit price understates the real share count
    # for any fill that lands below that ceiling -- exactly what a DAY
    # limit order routinely does. Pinned numbers: ADV=1,000 -> ceiling is
    # 10 shares. A $1,050 order at last_price=$100 implies 10.5 shares
    # (over the ceiling), but at limit_price=$105 (last_price * 1.05, the
    # default 5% band) it implies exactly 10.0 shares (not over -- the
    # check is a strict `>`). If the estimate used limit_price, this
    # order would wrongly be allowed through.
    monkeypatch.setattr(config, "MAX_ORDER_PCT_OF_ADV", 0.01)
    monkeypatch.setattr(config, "LIMIT_PRICE_BAND_PCT", 0.05)
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [1_000.0])

    result = setup.module.market_buy("AAPL", 1_050.0)

    assert result is None
    setup.trading.submit_order.assert_not_called()


def test_market_buy_proceeds_when_within_the_adv_ceiling(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_ORDER_PCT_OF_ADV", 0.01)
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    # 1% of a 10M-share ADV is 100,000 shares -- a $5,000 order (~47
    # shares) is nowhere close.
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [10_000_000.0])
    fake_order = _fake_accepted_order()
    setup.trading.submit_order.return_value = fake_order

    result = setup.module.market_buy("AAPL", 5_000.0)

    assert result is fake_order


def test_liquidate_skipped_when_it_exceeds_the_adv_ceiling(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "MAX_ORDER_PCT_OF_ADV", 0.01)
    setup.trading.get_open_position.return_value = _fake_position("AAPL", 1000.0, 500.0)
    # 1% of a 1,000-share ADV is 10 shares -- the 500-share position is
    # far past it.
    setup.data.get_stock_bars.return_value = _fake_bar_set("AAPL", [1_000.0])

    result = setup.module.liquidate("AAPL")

    assert result is None
    setup.trading.submit_order.assert_not_called()
    # The position remains open (nothing submitted) -- must NOT be
    # journaled/treated as sold; the caller (bot.run) sees None and
    # simply doesn't call journal.record_order, same as any other
    # failed liquidation attempt.


def test_liquidate_proceeds_when_the_adv_bars_request_raises(setup: _Setup) -> None:
    # Mirrors test_market_buy_proceeds_when_the_adv_bars_request_raises --
    # staff-engineer-reviewer finding: liquidate() reaches the same
    # _exceeds_adv_ceiling/_average_daily_volume helpers as market_buy,
    # via the same fail-open contract, but that guarantee wasn't
    # separately pinned for the sell side. A quality-driven liquidation
    # is exactly the case where blocking on an unrelated ADV-data outage
    # would be worst -- it would leave a deteriorating position open
    # (see liquidate's own docstring) instead of merely skipping a new
    # buy.
    setup.trading.get_open_position.return_value = _fake_position("AAPL", 1000.0, 7.5)
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    setup.data.get_stock_bars.side_effect = _fake_api_error(403)
    fake_order = _fake_accepted_order()
    setup.trading.submit_order.return_value = fake_order

    result = setup.module.liquidate("AAPL")

    assert result is fake_order
    setup.trading.submit_order.assert_called_once()


# --- M29b: is_corporate_action (Alpaca Assets API) ---


def _fake_asset(status: AssetStatus, tradable: bool) -> MagicMock:
    asset = MagicMock(spec=Asset)
    asset.status = status
    asset.tradable = tradable
    return asset


def test_is_corporate_action_false_for_a_normal_active_tradable_asset(setup: _Setup) -> None:
    setup.trading.get_asset.return_value = _fake_asset(AssetStatus.ACTIVE, True)
    assert setup.module.is_corporate_action("AAPL") is False


def test_is_corporate_action_true_when_inactive(setup: _Setup) -> None:
    setup.trading.get_asset.return_value = _fake_asset(AssetStatus.INACTIVE, False)
    assert setup.module.is_corporate_action("DELISTED") is True


def test_is_corporate_action_true_when_active_but_not_tradable(setup: _Setup) -> None:
    # A real, specific case Alpaca can report: still ACTIVE status but
    # tradable flipped False (e.g. a halted symbol pending a corporate
    # action) -- either signal alone is enough.
    setup.trading.get_asset.return_value = _fake_asset(AssetStatus.ACTIVE, False)
    assert setup.module.is_corporate_action("HALTED") is True


def test_is_corporate_action_fails_open_on_a_lookup_failure(setup: _Setup) -> None:
    # Deliberately the opposite fail direction from get_current_holdings/
    # has_already_submitted -- a broken Assets lookup for one symbol
    # must not block evaluation of every other holding.
    setup.trading.get_asset.side_effect = Exception("network error")
    assert setup.module.is_corporate_action("AAPL") is False


def test_liquidate_submits_a_sell_limit_order_for_the_full_position(setup: _Setup) -> None:
    setup.trading.get_open_position.return_value = _fake_position("AAPL", 1000.0, 7.5)
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    fake_order = _fake_accepted_order()
    setup.trading.submit_order.return_value = fake_order

    result = setup.module.liquidate("AAPL")

    assert result is fake_order
    submitted_request = setup.trading.submit_order.call_args.args[0]
    assert submitted_request.qty == 7.5
    assert submitted_request.side == OrderSide.SELL
    assert submitted_request.time_in_force == TimeInForce.DAY
    assert submitted_request.client_order_id == "paper-2026-07-21-AAPL-sell"
    assert submitted_request.limit_price == pytest.approx(
        100.0 * (1 - config.LIMIT_PRICE_BAND_PCT), rel=1e-4
    )


def test_liquidate_returns_none_on_failure_without_raising(setup: _Setup) -> None:
    setup.trading.get_open_position.side_effect = Exception("position not found")
    result = setup.module.liquidate("AAPL")
    assert result is None


def test_market_buy_recovers_existing_order_instead_of_resubmitting(setup: _Setup) -> None:
    # Simulates a crash-and-restart same-day: a previous attempt already
    # placed this exact client_order_id, so has_already_submitted must
    # short-circuit before ever building a new request, and the existing
    # order must be returned so the caller (bot.py) can still journal it.
    setup.trading.get_order_by_client_id.side_effect = None
    existing_order = _fake_accepted_order()
    setup.trading.get_order_by_client_id.return_value = existing_order

    result = setup.module.market_buy("AAPL", 500.0)

    assert result is existing_order
    setup.trading.submit_order.assert_not_called()


def test_liquidate_recovers_existing_order_instead_of_resubmitting(setup: _Setup) -> None:
    setup.trading.get_order_by_client_id.side_effect = None
    existing_order = _fake_accepted_order()
    setup.trading.get_order_by_client_id.return_value = existing_order

    result = setup.module.liquidate("AAPL")

    assert result is existing_order
    setup.trading.get_open_position.assert_not_called()
    setup.trading.submit_order.assert_not_called()


def test_market_buy_propagates_when_idempotency_check_itself_fails(setup: _Setup) -> None:
    # has_already_submitted must fail closed -- a broken pre-check query
    # aborts the run, unlike a genuine order rejection, which market_buy
    # catches and turns into a None return.
    setup.trading.get_order_by_client_id.side_effect = _fake_api_error(500)
    with pytest.raises(APIError):
        setup.module.market_buy("AAPL", 500.0)


def test_liquidate_propagates_when_idempotency_check_itself_fails(setup: _Setup) -> None:
    setup.trading.get_order_by_client_id.side_effect = _fake_api_error(500)
    with pytest.raises(APIError):
        setup.module.liquidate("AAPL")


def test_paper_trading_flag_passed_to_trading_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_trading_client(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(execution, "TradingClient", _fake_trading_client)
    monkeypatch.setattr(execution, "StockHistoricalDataClient", lambda **kwargs: MagicMock())
    monkeypatch.setattr(config, "PAPER_TRADING", True)

    execution.ExecutionModule(run_date="2026-07-21")

    assert captured["paper"] is True
