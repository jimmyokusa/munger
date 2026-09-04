"""Unit tests for evaluate.py's orchestration logic (M34, Design v2.2 §3.1).

Every external module call is mocked -- this tests evaluate.py's own
wiring, not each module's internal correctness (already covered by their
own test files). Follows the same patterns as tests/test_bot.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import config
import data
import evaluate
import execution
import journal
import portfolio
import screener
import xbrl


def _quality_metrics(**overrides: Any) -> data.Metrics:
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


class _FakeExecutionModule:
    """Stand-in for execution.ExecutionModule, monkeypatched onto execution."""

    def __init__(self, run_date: str) -> None:
        self.run_date = run_date
        self.verify_account_access = MagicMock()
        self.get_current_holdings = MagicMock(return_value={})
        self.is_corporate_action = MagicMock(return_value=False)
        # Structural safety net: evaluate.py must NEVER call these.
        self.liquidate = MagicMock(side_effect=AssertionError("evaluate.py must never liquidate"))
        self.market_buy = MagicMock(side_effect=AssertionError("evaluate.py must never buy"))


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # M37: default to XBRL-primary being a no-op passthrough -- see
    # test_bot.py's own identical fixture comment for why.
    monkeypatch.setattr(xbrl, "apply_primary_metrics", lambda metrics_by_symbol: metrics_by_symbol)
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(
        config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "GLOBAL_KILL_SWITCH"
    )
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    monkeypatch.setattr(config, "STATE_FILE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(
        config, "SETTLEMENT_BLOCKED_FLAG_FILE_PATH", tmp_path / "SETTLEMENT_BLOCKED"
    )
    monkeypatch.setattr(config, "PAPER_TRADING", True)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    # StateTracker's default path is bound at class-definition time (see
    # test_bot.py's own comment on this) -- wrap the real class so a
    # bare `portfolio.StateTracker()` call inside evaluate.run() reads
    # config.STATE_FILE_PATH at call time instead. Accepts an explicit
    # `path=` too (defaulting to the same call-time read), so a test
    # verifying results via `portfolio.StateTracker(path=...)` after
    # run() still gets the real class, not a broken stand-in.
    _real_state_tracker = portfolio.StateTracker
    monkeypatch.setattr(
        portfolio,
        "StateTracker",
        lambda path=None: _real_state_tracker(path=path or config.STATE_FILE_PATH),
    )


def test_run_screen_only_when_kill_switch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = evaluate.run(run_date="2026-07-21")

    assert construct_calls == []  # never even connects to the broker
    assert exit_code == 0


def test_run_screen_only_when_global_kill_switch_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag_path = tmp_path / "GLOBAL_KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "GLOBAL_KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    construct_calls: list[str] = []
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 0


def test_run_escalates_to_critical_when_a_stale_settlement_block_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.KILL_SWITCH_FLAG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.KILL_SWITCH_FLAG_FILE_PATH.touch()
    config.SETTLEMENT_BLOCKED_FLAG_FILE_PATH.touch()

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 1  # Critical alert is itself alert-worthy


def test_run_refuses_to_run_live_without_the_live_trading_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    monkeypatch.setattr(config, "LIVE_TRADING_ENABLED", False)
    construct_calls: list[str] = []
    monkeypatch.setattr(
        execution, "ExecutionModule", lambda run_date: construct_calls.append(run_date)
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 1


def test_run_is_a_noop_with_no_current_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 0


def test_run_aborts_on_reconciliation_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    # No journal.record_order call at all -- get_expected_holdings() is
    # empty, so AAPL being actually held is an unexpected-holding mismatch.

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_never_liquidates_or_buys_even_past_the_strike_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The structural half of "read-only": _FakeExecutionModule's
    # liquidate/market_buy both raise if called at all, so this test
    # fails loudly (not silently) if evaluate.py ever gains a write path.
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    failing = _quality_metrics(symbol="AAPL", return_on_equity=0.05)
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"AAPL": failing})
    state_path = config.STATE_FILE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # M35: v2 schema, one strike (distinct period) short of liquidation --
    # none of these collide with this run's own period (2026Q3), so its
    # check adds a genuinely new, final distinct period.
    existing_periods = [f"period-{i}" for i in range(config.STRIKES_TO_LIQUIDATE - 1)]
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "strikes": {"AAPL": existing_periods},
                "holding_states": {},
                "pending_liquidations": [],
            }
        )
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 1  # the liquidation decision is itself alert-worthy
    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.pending_liquidations() == ["AAPL"]  # recorded as pending, not acted on


def test_run_records_a_pending_liquidation_past_the_strike_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"HRMY": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("HRMY", "buy", "NEW_POSITION score=78.2")
    failing = _quality_metrics(symbol="HRMY", return_on_equity=0.05)
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"HRMY": failing})
    state_path = config.STATE_FILE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existing_periods = [f"period-{i}" for i in range(config.STRIKES_TO_LIQUIDATE - 1)]
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "strikes": {"HRMY": existing_periods},
                "holding_states": {},
                "pending_liquidations": [],
            }
        )
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 1
    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.pending_liquidations() == ["HRMY"]


def test_run_clears_a_pending_liquidation_when_the_ticker_recovers_to_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: a ticker pending from an earlier
    # quarter's liquidation decision that recovers to HEALTHY by this
    # run (fundamentals genuinely improved before Execute ever got to
    # it) must not stay pending forever -- Execute would otherwise
    # liquidate a position this run's own, fresher classification says
    # is fine.
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"HRMY": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("HRMY", "buy", "NEW_POSITION score=78.2")
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"HRMY": _quality_metrics(symbol="HRMY")},
    )
    state_path = config.STATE_FILE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"strikes": {}, "holding_states": {}, "pending_liquidations": ["HRMY"]}')

    evaluate.run(run_date="2026-07-21")

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.pending_liquidations() == []
    assert reloaded.get_holding_state("HRMY") is portfolio.HoldingState.HEALTHY


def test_run_does_not_clear_a_pending_liquidation_when_data_is_too_degraded_to_evaluate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate must apply here too, not just to process_sells itself --
    # a run whose held-ticker data was too degraded to evaluate must not
    # clear a pending liquidation off a *stale* prior classification
    # that process_sells never actually re-confirmed this run.
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"HRMY": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("HRMY", "buy", "NEW_POSITION score=78.2")
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"HRMY": None})
    state_path = config.STATE_FILE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # A stale HEALTHY classification from a prior run, still on record.
    state_path.write_text(
        '{"strikes": {}, "holding_states": {"HRMY": "healthy"}, "pending_liquidations": ["HRMY"]}'
    )

    evaluate.run(run_date="2026-07-21")

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.pending_liquidations() == ["HRMY"]  # untouched -- data was too degraded


def test_run_records_holding_state_for_a_healthy_holding_without_liquidating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"AAPL": _quality_metrics(symbol="AAPL")},
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 0
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == []
    assert state.get_holding_state("AAPL") is portfolio.HoldingState.HEALTHY


def test_run_alerts_on_corporate_action_without_liquidating_or_recording_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"MRGD": 1000.0}
    fake_exec.is_corporate_action = MagicMock(return_value=True)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("MRGD", "buy", "NEW_POSITION score=78.2")
    # Real (not None) metrics -- classify_holding_state checks
    # is_corporate_action before the metrics-is-None branch, so this is
    # the realistic shape: Alpaca's Assets API reports it inactive while
    # a market-data source still has stale figures for it. Using None
    # here instead would trip the separate "too much held-ticker data is
    # missing" gate (100% missing with only one holding) before
    # classify_holding_state ever runs.
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"MRGD": _quality_metrics(symbol="MRGD")},
    )

    exit_code = evaluate.run(run_date="2026-07-21")

    assert exit_code == 1  # alert-worthy: needs manual review
    state = portfolio.StateTracker(path=config.STATE_FILE_PATH)
    assert state.pending_liquidations() == []  # never auto-traded either direction
    assert state.get_holding_state("MRGD") is portfolio.HoldingState.CORPORATE_ACTION


def test_run_resets_stale_strikes_for_a_ticker_no_longer_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    state_path = config.STATE_FILE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"strikes": {"GONE": 5}, "holding_states": {"GONE": "deteriorating"}}')

    evaluate.run(run_date="2026-07-21")

    reloaded = portfolio.StateTracker(path=state_path)
    assert reloaded.get_strikes("GONE") == 0
    assert reloaded.get_holding_state("GONE") is None


def test_run_fetches_holdings_metrics_through_xbrl_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (peer-session review finding): the autouse
    # apply_primary_metrics no-op fixture means a test mocking only
    # data.fetch_all_metrics can't tell "correctly wired through
    # screener.fetch_metrics_with_xbrl_primary" from "reverted straight
    # to data.fetch_all_metrics" -- both would stay green. This test
    # spies on fetch_metrics_with_xbrl_primary itself, so reverting
    # evaluate.py's own call back to data.fetch_all_metrics directly
    # would correctly fail it.
    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings.return_value = {"AAPL": 1000.0}
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)
    journal.record_order("AAPL", "buy", "NEW_POSITION score=78.2")

    calls: list[tuple[list[str], str]] = []
    real_fetch = screener.fetch_metrics_with_xbrl_primary

    def _spy(tickers: list[str], phase: str = "screening") -> dict[str, data.Metrics | None]:
        calls.append((tickers, phase))
        return real_fetch(tickers, phase=phase)

    monkeypatch.setattr(screener, "fetch_metrics_with_xbrl_primary", _spy)
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {"AAPL": None})

    evaluate.run(run_date="2026-07-21")

    assert len(calls) == 1
    assert calls[0] == (["AAPL"], "holdings check")
