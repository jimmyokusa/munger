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
    to_liquidate, unresolved = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL")}, state
    )
    assert to_liquidate == []
    assert unresolved == []
    assert state.get_strikes("AAPL") == 0


def test_process_sells_one_strike_then_clean_resets(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)  # fails MIN_ROE
    passing = _quality_metrics(symbol="AAPL")

    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)
    assert state.get_strikes("AAPL") == 1

    state2 = portfolio.StateTracker(path=state_path)  # reload, like the next run would
    to_liquidate, unresolved = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": passing}, state2)
    assert to_liquidate == []
    assert unresolved == []
    assert state2.get_strikes("AAPL") == 0


def test_process_sells_liquidates_only_after_the_configured_streak(tmp_path: Path) -> None:
    # M24: config-driven, not a hardcoded "2", so this survives the next
    # retune of config.STRIKES_TO_LIQUIDATE (currently 10).
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    to_liquidate: list[str] = []
    for _ in range(config.STRIKES_TO_LIQUIDATE):
        state = portfolio.StateTracker(path=state_path)
        to_liquidate, unresolved = portfolio.process_sells(
            {"AAPL": 1000.0}, {"AAPL": failing}, state
        )
        assert unresolved == []

    assert to_liquidate == ["AAPL"]
    # Strikes reset to zero on liquidation, not left at the streak length
    # -- see the regression test below for why (a stale count would
    # liquidate a future re-buy after just one bad check instead of a
    # full fresh streak).
    final_state = portfolio.StateTracker(path=state_path)
    assert final_state.get_strikes("AAPL") == 0


def test_process_sells_does_not_liquidate_before_the_configured_streak(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    to_liquidate: list[str] = []
    for _ in range(config.STRIKES_TO_LIQUIDATE - 1):
        state = portfolio.StateTracker(path=state_path)
        to_liquidate, _unresolved = portfolio.process_sells(
            {"AAPL": 1000.0}, {"AAPL": failing}, state
        )

    assert to_liquidate == []
    final_state = portfolio.StateTracker(path=state_path)
    assert final_state.get_strikes("AAPL") == config.STRIKES_TO_LIQUIDATE - 1


def test_process_sells_resets_strikes_after_liquidation_for_a_future_rebuy(
    tmp_path: Path,
) -> None:
    # staff-engineer-reviewer finding: strike counters were never cleared
    # when a ticker left current_holdings. If it were ever re-bought, it
    # would inherit its stale count and could liquidate after just one
    # bad check instead of a full fresh streak -- silently violating the
    # streak contract for what is, from the position's perspective, a
    # brand new holding.
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    to_liquidate: list[str] = []
    for _ in range(config.STRIKES_TO_LIQUIDATE):
        state = portfolio.StateTracker(path=state_path)
        to_liquidate, _unresolved = portfolio.process_sells(
            {"AAPL": 1000.0}, {"AAPL": failing}, state
        )
    assert to_liquidate == ["AAPL"]  # confirms liquidation actually happened first

    # AAPL is re-bought later; its very first quality check after that
    # fails once -- this must be treated as strike 1 of a fresh streak,
    # not a continuation of the already-liquidated streak.
    state_next = portfolio.StateTracker(path=state_path)
    to_liquidate, _unresolved = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": failing}, state_next
    )

    assert to_liquidate == []
    assert state_next.get_strikes("AAPL") == 1


def test_process_sells_missing_ticker_is_unresolved_not_struck(tmp_path: Path) -> None:
    # M24 fix: fetch_metrics returns None identically for a genuine
    # quality-relevant data problem and for a delisting/symbol
    # change/acquisition close -- absence of data is not evidence of
    # failing quality, so a missing ticker is no longer struck at all.
    # (Was test_process_sells_missing_ticker_counts_as_strike, asserting
    # the buggy behavior -- inverted, not deleted.)
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate, unresolved = portfolio.process_sells({"AAPL": 1000.0}, {}, state)
    assert to_liquidate == []
    assert unresolved == ["AAPL"]
    assert state.get_strikes("AAPL") == 0


def test_process_sells_none_metrics_is_unresolved_not_struck(tmp_path: Path) -> None:
    # Was test_process_sells_none_metrics_counts_as_strike -- inverted,
    # not deleted; see the sibling test above for why.
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate, unresolved = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": None}, state)
    assert to_liquidate == []
    assert unresolved == ["AAPL"]
    assert state.get_strikes("AAPL") == 0


def test_process_sells_unreadable_check_holds_an_existing_streak_steady(tmp_path: Path) -> None:
    # An unreadable check is no evidence either way -- it must neither
    # add to nor clear an existing strike streak.
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)  # strike 1
    assert state.get_strikes("AAPL") == 1

    state2 = portfolio.StateTracker(path=state_path)
    to_liquidate, unresolved = portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": None}, state2)
    assert to_liquidate == []
    assert unresolved == ["AAPL"]
    assert state2.get_strikes("AAPL") == 1  # neither incremented nor reset

    # And a clean check after that still works normally -- the frozen
    # streak isn't stuck, just untouched by the unreadable check itself.
    state3 = portfolio.StateTracker(path=state_path)
    to_liquidate, unresolved = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL")}, state3
    )
    assert unresolved == []
    assert state3.get_strikes("AAPL") == 0


def test_process_sells_persists_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = portfolio.StateTracker(path=state_path)
    portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL", return_on_equity=0.05)}, state
    )
    assert state_path.exists()


# --- generate_buy_queue ---


def test_generate_buy_queue_tops_up_existing_holding_below_target() -> None:
    screen_results = _screen_results([{"symbol": "AAPL", "buyable": True, "score": 50.0}])
    orders = portfolio.generate_buy_queue({"AAPL": 100.0}, screen_results, 10_000.0)
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


def test_generate_buy_queue_ignores_a_top_up_gap_within_the_drift_band() -> None:
    # User request: daily rebalancing needs a tolerance band, or a
    # holding's dollar value drifting with ordinary daily price noise
    # would trigger a top-up trade most days even with nothing wrong.
    available_cash = 10_000.0
    holding_value = 650.0
    portfolio_value = available_cash + holding_value  # 10,650
    target = portfolio_value / config.TARGET_POSITION_COUNT  # 710.0
    gap = target - holding_value  # 60.0
    assert gap < target * config.REBALANCE_DRIFT_BAND_PCT  # 60 < 71: inside the band
    orders = portfolio.generate_buy_queue(
        {"AAPL": holding_value}, _screen_results([]), available_cash
    )
    assert orders == []


def test_generate_buy_queue_still_tops_up_past_the_drift_band() -> None:
    available_cash = 10_000.0
    holding_value = 500.0
    portfolio_value = available_cash + holding_value  # 10,500
    target = portfolio_value / config.TARGET_POSITION_COUNT  # 700.0
    gap = target - holding_value  # 200.0
    assert gap > target * config.REBALANCE_DRIFT_BAND_PCT  # 200 > 70: past the band
    screen_results = _screen_results([{"symbol": "AAPL", "buyable": True, "score": 50.0}])
    orders = portfolio.generate_buy_queue({"AAPL": holding_value}, screen_results, available_cash)
    assert len(orders) == 1
    assert orders[0][0] == "AAPL"
    assert orders[0][1] == pytest.approx(gap)


def test_generate_buy_queue_does_not_top_up_a_holding_that_fails_the_screen() -> None:
    # M24 fix: real bug found live -- a holding whose fail_reasons were
    # graham_pe/graham_pe_times_pb (buyable=False) still received a
    # top-up order before this check existed. A top-up is a purchase,
    # not a hold, so it must still pass the current screen's buyability
    # gate even though process_sells deliberately ignores it.
    available_cash = 10_000.0
    holding_value = 500.0  # well past the drift band, so only the buyable gate can be stopping it
    screen_results = _screen_results([{"symbol": "AAPL", "buyable": False, "score": 50.0}])
    orders = portfolio.generate_buy_queue({"AAPL": holding_value}, screen_results, available_cash)
    assert orders == []


def test_generate_buy_queue_tops_up_only_the_still_buyable_holding() -> None:
    available_cash = 10_000.0
    holding_value = 500.0
    screen_results = _screen_results(
        [
            {"symbol": "GOOD", "buyable": True, "score": 50.0},
            {"symbol": "BAD", "buyable": False, "score": 50.0},
        ]
    )
    orders = portfolio.generate_buy_queue(
        {"GOOD": holding_value, "BAD": holding_value}, screen_results, available_cash
    )
    assert [ticker for ticker, _ in orders] == ["GOOD"]


def test_generate_buy_queue_holding_absent_from_screen_is_not_topped_up() -> None:
    # A holding with no row at all in screen_results (delisting, symbol
    # change) can't have its buyability confirmed -- conservatively not
    # topped up, same as an explicit buyable=False.
    available_cash = 10_000.0
    holding_value = 500.0
    orders = portfolio.generate_buy_queue(
        {"AAPL": holding_value}, _screen_results([]), available_cash
    )
    assert orders == []


def test_generate_buy_queue_self_limits_to_the_notional_budget_on_a_cold_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real bug found live: starting from zero holdings with several
    # buyable candidates, the queue used to request far more than
    # GLOBAL_NOTIONAL_BUDGET_PCT of equity in one run (e.g. 7 candidates
    # x ~$6,667 target each = ~$46,667 vs. a 25%-of-$100k ~$25,000
    # budget) -- bot.py then aborted the *entire* run, and since nothing
    # was ever bought, the next run built the identical over-budget
    # queue and aborted again, forever. generate_buy_queue must now cap
    # its own total notional to the run budget so a cold start fills as
    # much as fits and lets later runs ramp in the rest.
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.25)
    screen_results = _screen_results(
        [{"symbol": f"T{i}", "buyable": True, "score": 100.0 - i} for i in range(7)]
    )
    available_cash = 100_000.0
    orders = portfolio.generate_buy_queue({}, screen_results, available_cash)
    total_notional = sum(notional for _, notional in orders)
    assert total_notional <= available_cash * config.GLOBAL_NOTIONAL_BUDGET_PCT + 1e-3
    # Confirms the cap actually bound (not merely happened to be under
    # it) -- the unrestricted 1/15th-of-equity target per position would
    # otherwise have wanted every one of the 7 candidates.
    assert len(orders) < 7
