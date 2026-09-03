"""Unit tests for record_override.py (Design v2.2 §3.8, M43)."""

from __future__ import annotations

from pathlib import Path

import pytest

import config
import journal
import record_override


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOURNAL_DB_PATH", tmp_path / "journal.db")


def test_main_records_an_override_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = record_override.main(["HRMY", "Litigation ruling reviewed; holding anyway"])

    assert exit_code == 0
    assert journal.get_manual_override_count(ticker="HRMY") == 1
    captured = capsys.readouterr()
    assert "HRMY" in captured.out
    assert "Litigation ruling reviewed" in captured.out


def test_main_uppercases_the_ticker() -> None:
    record_override.main(["hrmy", "a reason"])
    assert journal.get_manual_override_count(ticker="HRMY") == 1


def test_main_defaults_account_from_config_paper_trading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PAPER_TRADING", False)
    record_override.main(["HRMY", "a reason"])
    assert journal.get_manual_override_count(account="live") == 1
    assert journal.get_manual_override_count(account="paper") == 0


def test_main_accepts_an_explicit_account_override() -> None:
    monkeypatch_account = "live"
    record_override.main(["HRMY", "a reason", "--account", monkeypatch_account])
    assert journal.get_manual_override_count(account="live") == 1


def test_main_rejects_an_empty_reason(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = record_override.main(["HRMY", "   "])

    assert exit_code == 1
    assert journal.get_manual_override_count() == 0
    captured = capsys.readouterr()
    assert "empty reason" in captured.err


def test_main_rejects_an_invalid_account_choice() -> None:
    with pytest.raises(SystemExit):
        record_override.main(["HRMY", "a reason", "--account", "bogus"])
