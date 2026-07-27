"""Unit tests for bot.py's orchestration logic (DESIGN.md sections 4, 5, 8).

Every external module call is mocked -- this tests bot.py's own wiring
(the right calls happen in the right order with the right arguments and
the right abort conditions), not each module's internal correctness,
which is already covered by their own test files. Monkeypatches the real
modules directly (not via bot.universe etc.) since bot.py imports them
by module (`import universe`), so patching the module object itself is
visible through bot.py's reference too -- and keeps mypy's implicit-
reexport check happy, the same pattern used in data.py's own tests.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

import bot
import config
import data
import execution
import journal
import portfolio
import screener
import universe


def _screen_results(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["symbol", "buyable", "score", "fail_reasons"])


def _clean_results() -> pd.DataFrame:
    return _screen_results(
        [
            {"symbol": "HIGH", "buyable": True, "score": 90.0, "fail_reasons": ""},
            {"symbol": "LOW", "buyable": True, "score": 50.0, "fail_reasons": ""},
        ]
    )


class _FakeExecutionModule:
    """Stand-in for execution.ExecutionModule, monkeypatched onto execution."""

    def __init__(self, run_date: str) -> None:
        self.run_date = run_date
        self.verify_account_access = MagicMock()
        self.get_current_holdings = MagicMock(return_value={})
        self.get_available_cash = MagicMock(return_value=100_000.0)
        self.liquidate = MagicMock(return_value=MagicMock(client_order_id="liq-id"))
        self.market_buy = MagicMock(return_value=MagicMock(client_order_id="buy-id"))


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", False)
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(config, "SCREEN_RESULTS_CSV_PATH", tmp_path / "screen_results.csv")
    (tmp_path / "screen_results.csv").write_text("symbol,buyable,score,fail_reasons\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")


def test_kill_switch_active_via_config_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    assert bot._kill_switch_active() is True


def test_kill_switch_active_via_flag_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_path = tmp_path / "KILL_SWITCH"
    flag_path.touch()
    monkeypatch.setattr(config, "KILL_SWITCH_FLAG_FILE_PATH", flag_path)
    assert bot._kill_switch_active() is True


# --- _cap_buy_orders_to_budget ---


def test_cap_buy_orders_to_budget_passes_through_when_under_budget() -> None:
    orders = [("A", 100.0), ("B", 200.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == orders
    assert deferred == []
    assert bound_budgets == []


def test_cap_buy_orders_to_budget_defers_past_the_order_count_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 2)
    orders = [("A", 100.0), ("B", 100.0), ("C", 100.0)]
    # 1 liquidation reserves a slot, leaving room for exactly 1 buy order.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=1, portfolio_value=10_000.0
    )
    assert capped == [("A", 100.0)]
    assert deferred == ["B", "C"]
    assert bound_budgets == ["order-count"]


def test_cap_buy_orders_to_budget_defers_past_the_notional_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.25)
    orders = [("A", 2_000.0), ("B", 2_000.0)]
    # Budget is 25% of $10,000 = $2,500: A fits, A+B ($4,000) doesn't.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == [("A", 2_000.0)]
    assert deferred == ["B"]
    assert bound_budgets == ["notional"]


def test_cap_buy_orders_to_budget_never_truncates_liquidations() -> None:
    # Liquidations aren't passed through this function at all -- it only
    # ever sees/returns buy orders -- confirmed here via a liquidation
    # count large enough to consume the entire order budget on its own,
    # every buy order still comes back deferred, not silently dropped.
    orders = [("A", 100.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=config.GLOBAL_ORDER_BUDGET, portfolio_value=10_000.0
    )
    assert capped == []
    assert deferred == ["A"]
    assert bound_budgets == ["order-count"]


def test_cap_buy_orders_to_budget_preserves_priority_order_past_the_notional_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Staff-engineer-reviewer finding: an earlier version used "skip and
    # continue" when an order didn't fit the notional budget, so a large
    # higher-priority order could get deferred while a smaller
    # lower-priority order after it still got bought -- silently
    # reordering execution relative to the queue's own priority (top-ups
    # before new positions, then score rank). A big order that doesn't
    # fit must defer itself *and* everything after it, not let a smaller
    # later order jump ahead of it.
    # Budget is 21% of $10,000 = $2,100: TOPUP_HIGH_PRIORITY ($2,000) fits
    # alone, but TOPUP_HIGH_PRIORITY + NEW_LOW_PRIORITY ($2,400) does not.
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.21)
    orders = [("TOPUP_HIGH_PRIORITY", 2_000.0), ("NEW_LOW_PRIORITY", 400.0)]
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=0, portfolio_value=10_000.0
    )
    assert capped == [("TOPUP_HIGH_PRIORITY", 2_000.0)]
    assert deferred == ["NEW_LOW_PRIORITY"]
    assert bound_budgets == ["notional"]


def test_cap_buy_orders_to_budget_reports_both_bound_budgets() -> None:
    orders = [("A", 100.0), ("B", 100.0), ("C", 10_000.0)]
    # liquidation_count reserves all but 2 buy slots -> order-count budget
    # excludes C before notional is even considered. Of what's left (A,
    # B), a $150 notional budget (25% of $600) admits A but not A+B ($200)
    # -> notional budget also binds, independently, on B. Both should be
    # reported.
    capped, deferred, bound_budgets = bot._cap_buy_orders_to_budget(
        orders, liquidation_count=config.GLOBAL_ORDER_BUDGET - 2, portfolio_value=600.0
    )
    assert capped == [("A", 100.0)]
    assert deferred == ["B", "C"]
    assert set(bound_budgets) == {"order-count", "notional"}


def test_kill_switch_inactive_by_default() -> None:
    assert bot._kill_switch_active() is False


def test_fetched_fraction_all_clean() -> None:
    results = _screen_results(
        [{"symbol": "A", "buyable": True, "score": 1.0, "fail_reasons": "graham_pe"}]
    )
    assert bot._fetched_fraction(results) == 1.0


def test_fetched_fraction_some_missing() -> None:
    results = _screen_results(
        [
            {
                "symbol": "A",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
            {"symbol": "B", "buyable": True, "score": 1.0, "fail_reasons": ""},
        ]
    )
    assert bot._fetched_fraction(results) == pytest.approx(0.5)


def test_fetched_fraction_empty_results() -> None:
    assert bot._fetched_fraction(_screen_results([])) == 0.0


def test_run_screen_only_when_kill_switch_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []  # never touches the broker at all
    assert exit_code == 0  # kill switch is intentional, not itself alert-worthy


def test_run_aborts_before_kill_switch_check_when_fetch_fraction_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    bad_results = _screen_results(
        [
            {
                "symbol": "A",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
            {
                "symbol": "B",
                "buyable": False,
                "score": 0.0,
                "fail_reasons": "data_missing:fetch_failed",
            },
        ]
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: bad_results)
    construct_calls: list[str] = []

    def _fake_execution_module(run_date: str) -> MagicMock:
        construct_calls.append(run_date)
        return MagicMock()

    monkeypatch.setattr(execution, "ExecutionModule", _fake_execution_module)

    exit_code = bot.run(run_date="2026-07-21")

    assert construct_calls == []
    assert exit_code == 1  # an abort is alert-worthy


def test_run_full_happy_path_places_liquidations_and_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(
        portfolio, "process_sells", lambda holdings, metrics, state: ["LIQUIDATE_ME"]
    )

    captured_buy_queue_holdings: dict[str, float] = {}

    def _fake_generate_buy_queue(
        holdings: dict[str, float], results: pd.DataFrame, cash: float
    ) -> list[tuple[str, float]]:
        captured_buy_queue_holdings.update(holdings)
        return [("HIGH", 5000.0)]

    monkeypatch.setattr(portfolio, "generate_buy_queue", _fake_generate_buy_queue)

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(
        return_value={"LIQUIDATE_ME": 1000.0, "KEEP": 2000.0}
    )
    recorded_orders: list[tuple[str, str, str]] = []

    def _fake_record_order(symbol: str, side: str, reason: str, **kwargs: Any) -> None:
        recorded_orders.append((symbol, side, reason))

    monkeypatch.setattr(journal, "record_order", _fake_record_order)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    fake_exec.verify_account_access.assert_called_once()
    fake_exec.liquidate.assert_called_once_with("LIQUIDATE_ME")
    fake_exec.market_buy.assert_called_once_with("HIGH", 5000.0)
    # Caller contract from the M6/M7 review: the liquidated ticker must
    # not appear in what's passed to generate_buy_queue.
    assert "LIQUIDATE_ME" not in captured_buy_queue_holdings
    assert "KEEP" in captured_buy_queue_holdings
    assert ("LIQUIDATE_ME", "sell", "SELL strikes=2") in recorded_orders
    assert any(symbol == "HIGH" and side == "buy" for symbol, side, _ in recorded_orders)
    assert exit_code == 1  # a liquidation occurred this run -- always alert-worthy


def test_run_defers_buy_orders_but_still_liquidates_when_order_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # User request: exceeding the order budget used to abort the entire
    # run -- correct for a one-off overage, but a deadlock under daily
    # rebalancing from a cold start (nothing bought -> next run builds
    # the identical over-budget queue -> aborts again, forever). Now the
    # buy queue is truncated (deferred to a later run) instead, while
    # liquidations -- the two-strike quality discipline, not
    # discretionary -- always still execute.
    monkeypatch.setattr(config, "GLOBAL_ORDER_BUDGET", 1)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(
        data, "fetch_all_metrics", lambda symbols, **_kwargs: {s: MagicMock() for s in symbols}
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: ["A"])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 1000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 1000.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    # 1 liquidation + 1 buy = 2 planned orders > budget of 1: the
    # liquidation still executes; the buy is deferred, not submitted.
    fake_exec.liquidate.assert_called_once_with("A")
    fake_exec.market_buy.assert_not_called()
    assert exit_code == 1


def test_run_defers_buy_order_when_notional_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "GLOBAL_NOTIONAL_BUDGET_PCT", 0.01)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 50_000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={})
    fake_exec.get_available_cash = MagicMock(return_value=100_000.0)
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    # $50k planned buy notional vs. 1% of $100k equity ($1k budget) --
    # deferred to a later run, not submitted; the run still alerts (exit 1)
    # so a human sees it was deferred rather than silently dropped.
    fake_exec.market_buy.assert_not_called()
    assert exit_code == 1


def test_run_does_not_journal_a_failed_order_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # The central fault-tolerance property this milestone guarantees:
    # execution.py returns None for a rejected/failed order, and bot.py
    # must not journal it (and must not crash processing the rest).
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(
        portfolio,
        "generate_buy_queue",
        lambda holdings, results, cash: [("HIGH", 5000.0), ("LOW", 3000.0)],
    )

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.market_buy = MagicMock(side_effect=[None, MagicMock(client_order_id="buy-id")])
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    recorded_orders: list[tuple[str, str]] = []
    monkeypatch.setattr(
        journal,
        "record_order",
        lambda symbol, side, reason, **kwargs: recorded_orders.append((symbol, side)),
    )

    exit_code = bot.run(run_date="2026-07-21")  # must not raise

    assert fake_exec.market_buy.call_count == 2
    assert recorded_orders == [("LOW", "buy")]  # HIGH's failed order never journaled
    # A single rejected order is normal, tolerated operational noise, not
    # itself one of DESIGN.md's alert categories -- no liquidations, no
    # reconciliation mismatch, no fallback, so this run is clean.
    assert exit_code == 0


def test_run_propagates_uncaught_when_verify_account_access_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # verify_account_access is deliberately fail-closed (no try/except in
    # execution.py); bot.py must not add its own handling around it --
    # this pins that contract so a future "helpful" try/except would
    # break this test instead of silently masking a real key/mode
    # mismatch.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.verify_account_access = MagicMock(side_effect=RuntimeError("key/mode mismatch"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    with pytest.raises(RuntimeError):
        bot.run(run_date="2026-07-21")


def test_run_propagates_uncaught_when_get_current_holdings_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same fail-closed contract as above: a broker outage fetching
    # holdings must abort the run, not be swallowed.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(side_effect=RuntimeError("broker outage"))
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    with pytest.raises(RuntimeError):
        bot.run(run_date="2026-07-21")


def test_run_skips_sell_evaluation_when_holdings_data_mostly_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # staff-engineer-reviewer finding: the second, holdings-only metrics
    # fetch has no fetch-fraction gate of its own, unlike the universe
    # screen -- a degraded fetch here (e.g. a still-active rate-limit
    # cooldown) must not silently strike real holdings toward
    # liquidation. process_sells should be skipped entirely, not fed
    # mostly-None data.
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(journal, "check_reconciliation", lambda holdings: [])
    # 1 of 3 holdings fetched cleanly (33%) -- well below MIN_UNIVERSE_FETCH_FRACTION.
    monkeypatch.setattr(
        data,
        "fetch_all_metrics",
        lambda symbols, **_kwargs: {"A": MagicMock(), "B": None, "C": None},
    )
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    process_sells_calls: list[dict[str, float]] = []

    def _fake_process_sells(
        holdings: dict[str, float], metrics: dict[str, Any], state: Any
    ) -> list[str]:
        process_sells_calls.append(holdings)
        return ["A"]

    monkeypatch.setattr(portfolio, "process_sells", _fake_process_sells)
    monkeypatch.setattr(portfolio, "generate_buy_queue", lambda holdings, results, cash: [])

    fake_exec = _FakeExecutionModule("2026-07-21")
    fake_exec.get_current_holdings = MagicMock(return_value={"A": 100.0, "B": 100.0, "C": 100.0})
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")

    assert process_sells_calls == []  # never called -- data too degraded to trust
    fake_exec.liquidate.assert_not_called()
    # No liquidation actually happened (sells were skipped, not decided),
    # no reconciliation mismatch, clean universe -- nothing to alert on.
    assert exit_code == 0


def test_check_data_freshness_none_when_no_archive_exists() -> None:
    assert bot._check_data_freshness() is None


def test_check_data_freshness_none_when_archive_is_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hardcoded past date would break as soon as DATA_FRESHNESS_MAX_HOURS
    # (daily-cadence tolerance, 48h) is smaller than the gap between that
    # date and whenever this test actually runs -- date it off "today"
    # instead so the test stays valid regardless of when it's run.
    recent_date = datetime.date.today() - datetime.timedelta(days=1)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / f"screen_results_{recent_date.isoformat()}.csv").write_text("symbol\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", archive_dir)
    assert bot._check_data_freshness() is None


def test_check_data_freshness_flags_a_stale_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Staff-engineer-reviewer finding: the age must come from the
    # run_date embedded in the filename, not filesystem mtime -- git
    # doesn't preserve mtimes across the checkout this project's GitHub
    # Actions workflow uses to restore this directory, which would
    # otherwise stamp every restored file with "now" and silently
    # neutralize this check. No os.utime call here on purpose: an old
    # filename alone must be enough to flag staleness.
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "screen_results_2026-01-01.csv").write_text("symbol\n")
    monkeypatch.setattr(config, "SCREEN_RESULTS_ARCHIVE_DIR", archive_dir)

    stale_hours = bot._check_data_freshness()

    assert stale_hours is not None
    assert stale_hours > config.DATA_FRESHNESS_MAX_HOURS


def test_run_alerts_on_a_stale_last_run_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missed scheduled run -- the dead-man's-switch this run's own
    # archive check is meant to catch (staff-engineer-reviewer / M11).
    monkeypatch.setattr(config, "KILL_SWITCH", True)  # keep this test broker-free
    monkeypatch.setattr(bot, "_check_data_freshness", lambda: 4000)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_alerts_on_a_universe_index_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"], fallback_indices=["500"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_alerts_on_an_empty_universe_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe, "get_universe_with_diagnostics", lambda: universe.UniverseResult(tickers=[])
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _screen_results([]))

    exit_code = bot.run(run_date="2026-07-21")

    assert exit_code == 1


def test_run_clean_when_nothing_notable_happens(monkeypatch: pytest.MonkeyPatch) -> None:
    # The converse of every alert-producing test above: a fully healthy,
    # boring run (screen-only via kill switch, fresh archive, no
    # fallback, non-empty universe) must exit 0.
    monkeypatch.setattr(config, "KILL_SWITCH", True)
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())

    assert bot.run(run_date="2026-07-21") == 0


def test_run_logs_reconciliation_warnings_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe,
        "get_universe_with_diagnostics",
        lambda: universe.UniverseResult(tickers=["HIGH", "LOW"]),
    )
    monkeypatch.setattr(screener, "run_screen", lambda tickers: _clean_results())
    monkeypatch.setattr(
        journal, "check_reconciliation", lambda holdings: ["AAPL: unexpected mismatch"]
    )
    monkeypatch.setattr(data, "fetch_all_metrics", lambda symbols, **_kwargs: {})
    monkeypatch.setattr(portfolio, "StateTracker", lambda: MagicMock())
    monkeypatch.setattr(portfolio, "process_sells", lambda holdings, metrics, state: [])
    monkeypatch.setattr(portfolio, "generate_buy_queue", lambda holdings, results, cash: [])

    fake_exec = _FakeExecutionModule("2026-07-21")
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)

    exit_code = bot.run(run_date="2026-07-21")  # must not raise despite the mismatch

    fake_exec.verify_account_access.assert_called_once()
    assert exit_code == 1  # logged, not aborted, but still alert-worthy (DESIGN.md 3.6)
