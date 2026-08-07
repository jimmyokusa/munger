"""Unit tests for gcs_bridge.py, mocking the google-cloud-storage client
(same pattern as test_pnl.py mocking the Alpaca SDK -- no live GCS access
in this environment)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import storage

import config
import gcs_bridge


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "PNL_DATA_PATH", tmp_path / "pnl.json")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "report")


def _fake_blob(*, content: bytes | None = None, missing: bool = False) -> MagicMock:
    blob = MagicMock()

    def _download(path: str) -> None:
        if missing:
            # google-api-core ships py.typed, but GoogleAPICallError.__init__
            # itself is untyped -- a real gap in the third-party library, not
            # ours to fix; ignore_missing_imports doesn't cover it since the
            # import resolves fine (unlike google-cloud-storage, which has no
            # py.typed at all).
            raise NotFound("no such object")  # type: ignore[no-untyped-call]
        Path(path).write_bytes(content or b"")

    blob.download_to_filename.side_effect = _download
    return blob


def _patch_client(monkeypatch: pytest.MonkeyPatch, blobs: dict[str, MagicMock]) -> None:
    bucket = MagicMock()
    bucket.blob.side_effect = lambda name: blobs[name]
    client = MagicMock()
    client.bucket.return_value = bucket
    # Patches the same cached google.cloud.storage module object gcs_bridge.py
    # imported -- not gcs_bridge.storage, which trips mypy's no_implicit_reexport
    # (gcs_bridge.py never re-exports `storage` as part of its own API).
    monkeypatch.setattr(storage, "Client", lambda: client)


def test_bridge_downloads_both_files_and_publishes_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_jsonl = (
        b'{"date": "2026-07-28", "equity": 100000.0}\n'
        b'{"date": "2026-07-29", "equity": 100500.0}\n'
    )
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 100500.0}'),
            "pnl_history.jsonl": _fake_blob(content=history_jsonl),
        },
    )

    gcs_bridge.bridge()

    assert config.PNL_DATA_PATH.read_text() == '{"equity": 100500.0}'
    history_path = config.REPORT_DIR / "pnl_history.jsonl"
    assert history_path.read_bytes() == history_jsonl
    array = json.loads((config.REPORT_DIR / "pnl_history.json").read_text())
    assert array == [
        {"date": "2026-07-28", "equity": 100000.0},
        {"date": "2026-07-29", "equity": 100500.0},
    ]


def test_bridge_tolerates_a_missing_pnl_history_as_the_expected_first_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 100000.0}'),
            "pnl_history.jsonl": _fake_blob(missing=True),
        },
    )

    gcs_bridge.bridge()

    assert config.PNL_DATA_PATH.read_text() == '{"equity": 100000.0}'
    assert not (config.REPORT_DIR / "pnl_history.jsonl").exists()
    assert not (config.REPORT_DIR / "pnl_history.json").exists()


def test_bridge_raises_when_pnl_json_itself_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike pnl_history.jsonl (tolerated as an expected first-run state),
    # pnl.json missing means the M16 bridge itself is broken -- that should
    # fail loudly (the CronJob's own failure history, not a silent no-op),
    # not be swallowed the same way.
    _patch_client(
        monkeypatch,
        {"pnl.json": _fake_blob(missing=True)},
    )

    with pytest.raises(NotFound):
        gcs_bridge.bridge()


def test_bridge_leaves_both_history_files_on_last_good_state_if_a_line_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_path = config.REPORT_DIR / "pnl_history.jsonl"
    json_path = config.REPORT_DIR / "pnl_history.json"
    config.REPORT_DIR.mkdir(parents=True)
    history_path.write_text('{"date": "2026-07-28", "equity": 100000.0}\n')
    json_path.write_text('[{"date": "2026-07-28", "equity": 100000.0}]')

    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b"{}"),
            "pnl_history.jsonl": _fake_blob(content=b"not valid json\n"),
        },
    )

    with pytest.raises(json.JSONDecodeError):
        gcs_bridge.bridge()

    # Neither file was swapped to the bad download -- both still hold
    # yesterday's last-good content, not a mismatched jsonl/json pair.
    assert history_path.read_text() == '{"date": "2026-07-28", "equity": 100000.0}\n'
    assert json_path.read_text() == '[{"date": "2026-07-28", "equity": 100000.0}]'
    assert list(config.REPORT_DIR.glob("*.tmp")) == []


def test_bridge_writes_atomically_with_no_leftover_tmp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b"{}"),
            "pnl_history.jsonl": _fake_blob(content=b'{"date": "2026-07-28"}\n'),
        },
    )

    gcs_bridge.bridge()

    assert list(config.PNL_DATA_PATH.parent.glob("*.tmp")) == []
    assert list(config.REPORT_DIR.glob("*.tmp")) == []
