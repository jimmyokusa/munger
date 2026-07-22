"""Unit tests for portfolio.py against hand-computed fixtures
(DESIGN.md section 6, layer 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import config
import data
import portfolio


def _quality_metrics(**overrides: Any) -> data.Metrics:
    """A Metrics record that cleanly passes the Munger quality floors --
    the baseline every strike-triggering fixture mutates away from."""
    defaults: dict[str, Any] = {
        "symbol": "TEST",
        "market_cap": 5_000_000_000.0,
        "trailing_pe": 15.0,
        "price_to_book": 1.5,
        "current_ratio": 2.0,
        "debt_to_equity": 0.5,
        "return_on_equity": 0.20,
        "gross_margin": 0.40,
        "operating_margin": 0.20,
        "free_cash_flow": 500_000_000.0,
        "dividend_yield": 0.02,
        "consecutive_positive_earnings_years": 5,
    }
    defaults.update(overrides)
    return data.Metrics(**defaults)


def _screen_results(rows: list[dict[str, Any]]) -> pd.DataFrame:
    # A real run_screen() output always has these columns, with proper
    # dtypes, even with zero buyable rows (every ticker in the universe
    # gets a row). An empty `rows` list alone produces a columnless
    # DataFrame, and even with columns= specified, an empty frame's
    # columns default to object dtype -- boolean-indexing an empty
    # object-dtype "buyable" column collapses to zero *columns*, not
    # just zero rows (a real pandas quirk, reproduced live while writing
    # this fixture). Explicit dtypes avoid it, matching what a real
    # DataFrame of actual booleans/floats/strings would have.
    if not rows:
        return pd.DataFrame(
            {
                "symbol": pd.Series(dtype="str"),
                "buyable": pd.Series(dtype="bool"),
                "score": pd.Series(dtype="float"),
            }
        )
    return pd.DataFrame(rows, columns=["symbol", "buyable", "score"])


# --- StateTracker ---


def test_state_tracker_get_strikes_defaults_to_zero(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    assert tracker.get_strikes("AAPL") == 0


def test_state_tracker_add_strike_increments(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.add_strike("AAPL")
    assert tracker.get_strikes("AAPL") == 1
    tracker.add_strike("AAPL")
    assert tracker.get_strikes("AAPL") == 2


def test_state_tracker_reset_strikes_clears(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.add_strike("AAPL")
    tracker.reset_strikes("AAPL")
    assert tracker.get_strikes("AAPL") == 0


def test_state_tracker_save_and_reload_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tracker = portfolio.StateTracker(path=state_path)
    tracker.add_strike("AAPL")
    tracker.save()

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.get_strikes("AAPL") == 1


def test_state_tracker_corrupt_file_falls_back_to_empty(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not valid json{{{")

    tracker = portfolio.StateTracker(path=state_path)

    assert tracker.get_strikes("AAPL") == 0


# --- process_sells ---


def test_process_sells_clean_check_no_strike(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL")}, state
    )
    assert to_liquidate == []
    assert state.get_strikes("AAPL") == 0


def test_process_sells_one_strike_then_clean_resets(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)  # fails MIN_ROE
    passing = _quality_metrics(symbol="AAPL")

    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)
    assert state.get_strikes("AAPL") == 1

    state2 = portfolio.StateTracker(path=state_path)  # reload, like the next run would
    to_liquidate = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": passing}, state2)
    assert to_liquidate == []
    assert state2.get_strikes("AAPL") == 0


def test_process_sells_two_consecutive_strikes_liquidates(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)

    state2 = portfolio.StateTracker(path=state_path)
    to_liquidate = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state2)

    assert to_liquidate == ["AAPL"]
    # Strikes reset to zero on liquidation, not left at 2 -- see the
    # regression test below for why (a stale count would liquidate a
    # future re-buy after just one bad check instead of two).
    assert state2.get_strikes("AAPL") == 0


def test_process_sells_resets_strikes_after_liquidation_for_a_future_rebuy(
    tmp_path: Path,
) -> None:
    # staff-engineer-reviewer finding: strike counters were never cleared
    # when a ticker left current_holdings. If it were ever re-bought, it
    # would inherit its stale count and could liquidate after just one
    # bad check instead of two -- silently violating the two-strike
    # contract for what is, from the position's perspective, a brand new
    # holding.
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)  # strike 1
    state2 = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state2)  # strike 2, liquidated

    # AAPL is re-bought later; its very first quality check after that
    # fails once -- this must be treated as strike 1 of a fresh streak,
    # not strike 3 (which would already be past the liquidation point).
    state3 = portfolio.StateTracker(path=state_path)
    to_liquidate = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state3)

    assert to_liquidate == []
    assert state3.get_strikes("AAPL") == 1


def test_process_sells_missing_ticker_counts_as_strike(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate = portfolio.process_sells({"AAPL": 1000.0}, {}, state)
    assert to_liquidate == []
    assert state.get_strikes("AAPL") == 1


def test_process_sells_none_metrics_counts_as_strike(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": None}, state)
    assert state.get_strikes("AAPL") == 1


def test_process_sells_persists_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL", return_on_equity=0.05)}, state
    )
    assert state_path.exists()


# --- generate_buy_queue ---


def test_generate_buy_queue_tops_up_existing_holding_below_target() -> None:
    orders = portfolio.generate_buy_queue({"AAPL": 100.0}, _screen_results([]), 10_000.0)
    portfolio_value = 10_000.0 + 100.0
    target = portfolio_value / config.TARGET_POSITION_COUNT
    assert len(orders) == 1
    assert orders[0][0] == "AAPL"
    assert orders[0][1] == pytest.approx(target - 100.0)


def test_generate_buy_queue_opens_new_positions_by_score_rank() -> None:
    screen_results = _screen_results(
        [
            {"symbol": "LOW", "buyable": True, "score": 50.0},
            {"symbol": "HIGH", "buyable": True, "score": 90.0},
            {"symbol": "BAD", "buyable": False, "score": 0.0},
        ]
    )
    orders = portfolio.generate_buy_queue({}, screen_results, 10_000.0)
    assert [ticker for ticker, _ in orders] == ["HIGH", "LOW"]


def test_generate_buy_queue_never_buys_a_non_buyable_ticker() -> None:
    screen_results = _screen_results([{"symbol": "BAD", "buyable": False, "score": 99.0}])
    orders = portfolio.generate_buy_queue({}, screen_results, 10_000.0)
    assert orders == []


def test_generate_buy_queue_skips_dust_orders() -> None:
    # Available cash net of the 2% buffer is well under MIN_ORDER_NOTIONAL.
    orders = portfolio.generate_buy_queue({}, _screen_results([]), 30.0)
    assert orders == []


def test_generate_buy_queue_does_not_top_up_a_holding_already_at_target() -> None:
    portfolio_value_estimate = 10_000.0
    target = portfolio_value_estimate / config.TARGET_POSITION_COUNT
    orders = portfolio.generate_buy_queue({"AAPL": target * 2}, _screen_results([]), 10_000.0)
    assert orders == []


def test_generate_buy_queue_stops_at_target_position_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "TARGET_POSITION_COUNT", 2)
    screen_results = _screen_results(
        [
            {"symbol": "A", "buyable": True, "score": 90.0},
            {"symbol": "B", "buyable": True, "score": 80.0},
        ]
    )
    orders = portfolio.generate_buy_queue({"EXISTING": 1.0}, screen_results, 100_000.0)
    # 1 existing position + target of 2 total -> exactly one new position opened.
    new_position_orders = [o for o in orders if o[0] != "EXISTING"]
    assert len(new_position_orders) == 1
    assert new_position_orders[0][0] == "A"  # higher score


def test_generate_buy_queue_respects_max_single_position_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With few target positions, 1/N of equity exceeds the 12% cap --
    # the cap, not the 1/N target, must bind.
    monkeypatch.setattr(config, "TARGET_POSITION_COUNT", 2)
    screen_results = _screen_results([{"symbol": "A", "buyable": True, "score": 90.0}])
    orders = portfolio.generate_buy_queue({}, screen_results, 100_000.0)
    portfolio_value = 100_000.0
    max_value = portfolio_value * config.MAX_SINGLE_POSITION_WEIGHT
    assert len(orders) == 1
    assert orders[0][0] == "A"
    assert orders[0][1] == pytest.approx(max_value)
