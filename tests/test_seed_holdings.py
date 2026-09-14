"""Unit tests for seed_holdings.py (M49)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
import execution
import journal
import seed_holdings


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")
    # Every existing test in this file passes --account ira -- default the
    # process's own account label to match, so the mismatch guard (tested
    # explicitly below) doesn't fire for every other test in this file.
    monkeypatch.setenv("MUNGER_ACCOUNT_LABEL", "ira")


class _FakeExecutionModule:
    """Stand-in for execution.ExecutionModule, monkeypatched onto execution."""

    def __init__(self, run_date: str) -> None:
        self.run_date = run_date
        self.get_current_holdings = MagicMock(return_value={})


def _patch_holdings(monkeypatch: pytest.MonkeyPatch, holdings: dict[str, float]) -> None:
    fake_exec = _FakeExecutionModule("2026-09-13")
    fake_exec.get_current_holdings.return_value = holdings
    monkeypatch.setattr(execution, "ExecutionModule", lambda run_date: fake_exec)


def test_main_seeds_every_held_symbol_not_already_in_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_holdings(monkeypatch, {"GLAD": 6915.4, "HRZN": 48475.04})

    exit_code = seed_holdings.main(["--account", "ira"])

    assert exit_code == 0
    assert journal.get_expected_holdings("ira") == {"GLAD", "HRZN"}


def test_main_records_a_seeded_reason_with_no_notional(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_holdings(monkeypatch, {"GLAD": 6915.4})
    seed_holdings.main(["--account", "ira"])

    detail = journal.get_holdings_detail("ira")
    assert len(detail) == 1
    assert detail[0]["symbol"] == "GLAD"
    assert "SEEDED" in str(detail[0]["reason"])
    assert "ira" in str(detail[0]["reason"])
    assert detail[0]["notional"] is None


def test_main_skips_a_symbol_already_expected_held(monkeypatch: pytest.MonkeyPatch) -> None:
    journal.record_order("GLAD", "buy", "an earlier real order", account="ira")
    _patch_holdings(monkeypatch, {"GLAD": 6915.4, "HRZN": 48475.04})

    seed_holdings.main(["--account", "ira"])

    # GLAD was already expected-held -- must not gain a second, SEEDED row.
    detail = journal.get_holdings_detail("ira")
    assert {d["symbol"] for d in detail} == {"GLAD", "HRZN"}
    glad_reasons = [d["reason"] for d in detail if d["symbol"] == "GLAD"]
    assert glad_reasons == ["an earlier real order"]


def test_main_is_idempotent_across_repeated_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_holdings(monkeypatch, {"GLAD": 6915.4})

    seed_holdings.main(["--account", "ira"])
    seed_holdings.main(["--account", "ira"])

    detail = journal.get_holdings_detail("ira")
    assert len(detail) == 1  # not seeded twice


def test_main_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_holdings(monkeypatch, {"GLAD": 6915.4})

    exit_code = seed_holdings.main(["--account", "ira", "--dry-run"])

    assert exit_code == 0
    # journal.get_expected_holdings() itself creates journal.db as a side
    # effect of connecting (CREATE TABLE IF NOT EXISTS), even for a pure
    # read -- the file existing is not evidence a write happened, an empty
    # expected-holdings set is.
    assert journal.get_expected_holdings("ira") == set()


def test_main_prints_what_dry_run_would_seed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_holdings(monkeypatch, {"GLAD": 6915.4})

    seed_holdings.main(["--account", "ira", "--dry-run"])

    captured = capsys.readouterr()
    assert "GLAD" in captured.out
    assert "dry-run" in captured.out


def test_main_does_nothing_when_every_holding_already_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal.record_order("GLAD", "buy", "an earlier real order", account="ira")
    _patch_holdings(monkeypatch, {"GLAD": 6915.4})

    exit_code = seed_holdings.main(["--account", "ira"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Nothing to seed" in captured.out


def test_main_rejects_an_invalid_account_choice() -> None:
    with pytest.raises(SystemExit):
        seed_holdings.main(["--account", "bogus"])


def test_main_requires_the_account_argument() -> None:
    with pytest.raises(SystemExit):
        seed_holdings.main([])


def test_main_refuses_to_seed_when_account_does_not_match_process_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # staff-engineer-reviewer finding (push review): --account must match
    # config.ACCOUNT_LABEL (this process's actual credentials), or a
    # mismatch would silently seed one real account's holdings into a
    # different account's journal partition. Overrides the autouse
    # fixture's "ira" default to "live" -- a real mismatch, not the
    # happy-path setup every other test in this file uses.
    monkeypatch.setenv("MUNGER_ACCOUNT_LABEL", "live")
    holdings_read = False

    def _fail_if_called(run_date: str) -> _FakeExecutionModule:
        nonlocal holdings_read
        holdings_read = True
        return _FakeExecutionModule(run_date)

    monkeypatch.setattr(execution, "ExecutionModule", _fail_if_called)

    exit_code = seed_holdings.main(["--account", "ira"])

    # Refuses before ever touching the broker, not merely before writing.
    assert holdings_read is False
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Refusing" in captured.err
    assert journal.get_expected_holdings("ira") == set()
