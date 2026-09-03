"""Unit tests for portfolio.py against hand-computed fixtures
(DESIGN.md section 6, layer 1)."""

from __future__ import annotations

import json
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


def test_state_tracker_tracked_tickers_lists_only_nonzero_strikes(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.add_strike("AAPL")
    tracker.add_strike("MSFT")
    tracker.reset_strikes("MSFT")

    assert tracker.tracked_tickers() == ["AAPL"]


# --- StateTracker: holding_states (Design v2.2 §3.2, M29c) ---


def test_state_tracker_get_holding_state_defaults_to_none(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    assert tracker.get_holding_state("AAPL") is None


def test_state_tracker_record_and_get_holding_state_roundtrip(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.DETERIORATING)
    assert tracker.get_holding_state("AAPL") is portfolio.HoldingState.DETERIORATING


def test_state_tracker_all_holding_states_returns_every_recorded_ticker(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.HEALTHY)
    tracker.record_holding_state("MSFT", portfolio.HoldingState.CORPORATE_ACTION)

    assert tracker.all_holding_states() == {
        "AAPL": portfolio.HoldingState.HEALTHY,
        "MSFT": portfolio.HoldingState.CORPORATE_ACTION,
    }


def test_state_tracker_clear_holding_state_removes_it(tmp_path: Path) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.HEALTHY)
    tracker.clear_holding_state("AAPL")
    assert tracker.get_holding_state("AAPL") is None


def test_state_tracker_clear_holding_state_of_an_untracked_ticker_is_a_no_op(
    tmp_path: Path,
) -> None:
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.clear_holding_state("AAPL")  # never recorded -- must not raise
    assert tracker.get_holding_state("AAPL") is None


def test_state_tracker_tracked_holding_state_tickers_includes_healthy(tmp_path: Path) -> None:
    # Deliberately distinct from tracked_tickers() (nonzero strikes only)
    # -- a HEALTHY holding has zero strikes but must still show up here,
    # or bot.py's stale-state reconciliation would never clear it once
    # sold.
    tracker = portfolio.StateTracker(path=tmp_path / "state.json")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.HEALTHY)

    assert tracker.tracked_holding_state_tickers() == ["AAPL"]
    assert tracker.tracked_tickers() == []  # zero strikes, so absent from this one


def test_state_tracker_save_and_reload_roundtrips_holding_states(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tracker = portfolio.StateTracker(path=state_path)
    tracker.add_strike("AAPL")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.DETERIORATING)
    tracker.record_holding_state("MSFT", portfolio.HoldingState.HEALTHY)
    tracker.save()

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.get_strikes("AAPL") == 1
    assert reloaded.get_holding_state("AAPL") is portfolio.HoldingState.DETERIORATING
    assert reloaded.get_holding_state("MSFT") is portfolio.HoldingState.HEALTHY


def test_state_tracker_save_writes_the_new_wrapped_schema(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tracker = portfolio.StateTracker(path=state_path)
    tracker.add_strike("AAPL")
    tracker.record_holding_state("AAPL", portfolio.HoldingState.HEALTHY)
    tracker.save()

    on_disk = json.loads(state_path.read_text())
    assert on_disk == {"strikes": {"AAPL": 1}, "holding_states": {"AAPL": "healthy"}}


def test_state_tracker_loads_a_legacy_flat_strikes_file(tmp_path: Path) -> None:
    # Every real state.json written before M29c is exactly this shape --
    # a bare {ticker: strike_count} dict, no wrapper keys at all. Must
    # keep loading correctly, or every real deployed state.json becomes
    # unreadable the moment this code ships.
    state_path = tmp_path / "state.json"
    state_path.write_text('{"FOX": 1, "LPG": 2}')

    tracker = portfolio.StateTracker(path=state_path)

    assert tracker.get_strikes("FOX") == 1
    assert tracker.get_strikes("LPG") == 2
    assert tracker.all_holding_states() == {}  # no holding-state data in a legacy file


def test_state_tracker_a_legacy_ticker_literally_named_strikes_does_not_misread_as_new_format(
    tmp_path: Path,
) -> None:
    # The new-format detection requires BOTH "strikes" and "holding_states"
    # keys present AND both mapped to dicts -- not just "strikes" present
    # -- specifically so a legacy file with an (unlikely but possible)
    # real ticker literally named "STRIKES" doesn't get misread as the
    # new wrapped format and silently lose every other ticker's count.
    # Uppercase here since that's what a real ticker looks like; the
    # check itself is case-sensitive against the lowercase wrapper key,
    # so this also confirms there's no accidental case-insensitive
    # collision.
    state_path = tmp_path / "state.json"
    state_path.write_text('{"STRIKES": 3, "AAPL": 1}')

    tracker = portfolio.StateTracker(path=state_path)

    assert tracker.get_strikes("STRIKES") == 3
    assert tracker.get_strikes("AAPL") == 1


def test_state_tracker_get_holding_state_ignores_an_unrecognized_value(tmp_path: Path) -> None:
    # Staff-engineer-reviewer finding: a version mismatch between the
    # running code's HoldingState enum and a persisted state.json (a
    # rollback, a staged deploy reading an older/newer file, a hand
    # edit) must not raise -- get_holding_state degrades to None, not an
    # unhandled ValueError that would abort report.py's entire report
    # generation over one ticker's stale/unknown value.
    state_path = tmp_path / "state.json"
    state_path.write_text('{"strikes": {}, "holding_states": {"AAPL": "some_future_state"}}')

    tracker = portfolio.StateTracker(path=state_path)

    assert tracker.get_holding_state("AAPL") is None


def test_state_tracker_all_holding_states_omits_an_unrecognized_value(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        '{"strikes": {}, "holding_states": {"AAPL": "some_future_state", "MSFT": "healthy"}}'
    )

    tracker = portfolio.StateTracker(path=state_path)

    # AAPL silently omitted, not raised; MSFT (a recognized value) is
    # still returned correctly -- one bad ticker doesn't take the rest
    # down with it.
    assert tracker.all_holding_states() == {"MSFT": portfolio.HoldingState.HEALTHY}


def test_state_tracker_migrates_a_legacy_file_to_the_new_schema_on_save(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"FOX": 1}')

    tracker = portfolio.StateTracker(path=state_path)
    tracker.record_holding_state("FOX", portfolio.HoldingState.DETERIORATING)
    tracker.save()

    on_disk = json.loads(state_path.read_text())
    assert on_disk == {"strikes": {"FOX": 1}, "holding_states": {"FOX": "deteriorating"}}


# --- HoldingState / classify_holding_state (Design v2.2 §3.2, M29a) ---


def test_classify_holding_state_healthy_when_data_passes_quality_floors() -> None:
    assert (
        portfolio.classify_holding_state(_quality_metrics(), is_corporate_action=False)
        is portfolio.HoldingState.HEALTHY
    )


def test_classify_holding_state_deteriorating_when_data_fails_quality_floors() -> None:
    failing = _quality_metrics(return_on_equity=0.02)  # fails MIN_ROE
    assert (
        portfolio.classify_holding_state(failing, is_corporate_action=False)
        is portfolio.HoldingState.DETERIORATING
    )


def test_classify_holding_state_unreadable_when_no_data_and_no_corporate_action() -> None:
    assert (
        portfolio.classify_holding_state(None, is_corporate_action=False)
        is portfolio.HoldingState.UNREADABLE
    )


def test_classify_holding_state_corporate_action_when_actively_detected() -> None:
    assert (
        portfolio.classify_holding_state(None, is_corporate_action=True)
        is portfolio.HoldingState.CORPORATE_ACTION
    )


def test_classify_holding_state_corporate_action_takes_priority_over_data_presence() -> None:
    # A ticker can have both real (stale) metrics AND a confirmed
    # corporate action -- CORPORATE_ACTION is the more specific, more
    # useful classification of the two either way (§3.2's own reasoning:
    # UNREADABLE should mean "unexplained absence," not "absence with a
    # known cause" -- and the same logic extends to "presence with a
    # known cause").
    assert (
        portfolio.classify_holding_state(_quality_metrics(), is_corporate_action=True)
        is portfolio.HoldingState.CORPORATE_ACTION
    )


# --- process_sells ---


def test_process_sells_clean_check_no_strike(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
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
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": passing}, state2
    )
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
        to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
            {"AAPL": 1000.0}, {"AAPL": failing}, state
        )
        assert unresolved == []

    assert to_liquidate == ["AAPL"]
    # M26c (Design v2.2 §3.3): process_sells no longer resets strikes
    # the moment liquidation is *decided* -- that used to be exactly the
    # mechanism behind the real FOX/LPG bug (reset before the order ever
    # confirmed filling). The streak is left exactly where it was;
    # resetting it is now the caller's job (bot.run), and only once
    # settlement confirms the order actually filled.
    final_state = portfolio.StateTracker(path=state_path)
    assert final_state.get_strikes("AAPL") == config.STRIKES_TO_LIQUIDATE


def test_process_sells_does_not_liquidate_before_the_configured_streak(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    to_liquidate: list[str] = []
    for _ in range(config.STRIKES_TO_LIQUIDATE - 1):
        state = portfolio.StateTracker(path=state_path)
        to_liquidate, _unresolved, _corporate_action = portfolio.process_sells(
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
    #
    # M26c update: process_sells itself no longer performs this reset
    # (see the sibling test above) -- it's now bot.run's job, done only
    # after settlement confirms the liquidation filled. This test now
    # exercises the same underlying contract one level out: once the
    # caller has reset the streak (simulating a confirmed fill), a
    # future re-buy's first bad check must start a fresh streak, not
    # inherit the pre-liquidation count.
    state_path = tmp_path / "state.json"
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)

    to_liquidate: list[str] = []
    for _ in range(config.STRIKES_TO_LIQUIDATE):
        state = portfolio.StateTracker(path=state_path)
        to_liquidate, _unresolved, _corporate_action = portfolio.process_sells(
            {"AAPL": 1000.0}, {"AAPL": failing}, state
        )
    assert to_liquidate == ["AAPL"]  # confirms liquidation actually happened first
    state.reset_strikes("AAPL")  # simulates bot.run's post-confirmed-fill reset
    state.save()

    # AAPL is re-bought later; its very first quality check after that
    # fails once -- this must be treated as strike 1 of a fresh streak,
    # not a continuation of the already-liquidated streak.
    state_next = portfolio.StateTracker(path=state_path)
    to_liquidate, _unresolved, _corporate_action = portfolio.process_sells(
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
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
        {"AAPL": 1000.0}, {}, state
    )
    assert to_liquidate == []
    assert unresolved == ["AAPL"]
    assert state.get_strikes("AAPL") == 0


def test_process_sells_none_metrics_is_unresolved_not_struck(tmp_path: Path) -> None:
    # Was test_process_sells_none_metrics_counts_as_strike -- inverted,
    # not deleted; see the sibling test above for why.
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": None}, state
    )
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
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": None}, state2
    )
    assert to_liquidate == []
    assert unresolved == ["AAPL"]
    assert state2.get_strikes("AAPL") == 1  # neither incremented nor reset

    # And a clean check after that still works normally -- the frozen
    # streak isn't stuck, just untouched by the unreadable check itself.
    state3 = portfolio.StateTracker(path=state_path)
    to_liquidate, unresolved, _corporate_action = portfolio.process_sells(
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


# --- process_sells: records HoldingState for every branch (M29c) ---


def test_process_sells_records_healthy_holding_state(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": _quality_metrics(symbol="AAPL")}, state)
    assert state.get_holding_state("AAPL") is portfolio.HoldingState.HEALTHY


def test_process_sells_records_deteriorating_holding_state(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": failing}, state)
    assert state.get_holding_state("AAPL") is portfolio.HoldingState.DETERIORATING


def test_process_sells_records_unreadable_holding_state(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    portfolio.process_sells({"AAPL": 1000.0}, {"AAPL": None}, state)
    assert state.get_holding_state("AAPL") is portfolio.HoldingState.UNREADABLE


def test_process_sells_records_corporate_action_holding_state(tmp_path: Path) -> None:
    state = portfolio.StateTracker(path=tmp_path / "state.json")
    portfolio.process_sells(
        {"AAPL": 1000.0}, {"AAPL": None}, state, corporate_action_check=lambda t: True
    )
    assert state.get_holding_state("AAPL") is portfolio.HoldingState.CORPORATE_ACTION


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


def test_generate_buy_queue_exclude_blocks_a_buyable_new_position() -> None:
    # M29 staff-engineer-reviewer finding: the real bug -- a confirmed
    # corporate-action ticker that's *also* buyable=True in the same
    # run's screen (a real possibility, since is_corporate_action is
    # driven by Alpaca's Assets API, independent of the yfinance-based
    # fundamentals that drive buyable) must not be selected as a fresh
    # NEW_POSITION purchase just because it's absent from
    # current_holdings. `exclude` is the explicit fix.
    screen_results = _screen_results([{"symbol": "MRGD", "buyable": True, "score": 99.0}])
    orders = portfolio.generate_buy_queue({}, screen_results, 10_000.0, exclude={"MRGD"})
    assert orders == []


def test_generate_buy_queue_exclude_also_blocks_a_top_up() -> None:
    # Defense-in-depth: the same rule applies even if a caller didn't
    # pre-filter current_holdings (the top-up loop's own membership
    # check alone would otherwise still top it up).
    screen_results = _screen_results([{"symbol": "MRGD", "buyable": True, "score": 50.0}])
    orders = portfolio.generate_buy_queue(
        {"MRGD": 100.0}, screen_results, 10_000.0, exclude={"MRGD"}
    )
    assert orders == []


def test_generate_buy_queue_exclude_does_not_affect_other_tickers() -> None:
    screen_results = _screen_results(
        [
            {"symbol": "MRGD", "buyable": True, "score": 99.0},
            {"symbol": "GOOD", "buyable": True, "score": 50.0},
        ]
    )
    orders = portfolio.generate_buy_queue({}, screen_results, 10_000.0, exclude={"MRGD"})
    assert [ticker for ticker, _ in orders] == ["GOOD"]


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
