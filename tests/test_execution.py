"""Unit tests for execution.py, mocking the Alpaca SDK (no live credentials
available in this environment -- see TASKS.md M8 for what's still blocked
on live verification)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.models.trades import Trade
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.models import Order, Position, TradeAccount

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
    assert setup.module._client_order_id("AAPL", "buy") == "2026-07-21-AAPL-buy"
    assert setup.module._client_order_id("AAPL", "buy") == setup.module._client_order_id(
        "AAPL", "buy"
    )


def test_limit_price_buy_is_above_last_trade(setup: _Setup) -> None:
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    price = setup.module._limit_price("AAPL", "buy")
    assert price == pytest.approx(100.0 * (1 + config.LIMIT_PRICE_BAND_PCT))


def test_limit_price_sell_is_below_last_trade(setup: _Setup) -> None:
    setup.data.get_stock_latest_trade.return_value = {"AAPL": _fake_trade(100.0)}
    price = setup.module._limit_price("AAPL", "sell")
    assert price == pytest.approx(100.0 * (1 - config.LIMIT_PRICE_BAND_PCT))


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
    assert submitted_request.client_order_id == "2026-07-21-AAPL-buy"
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
    assert submitted_request.client_order_id == "2026-07-21-AAPL-sell"
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
