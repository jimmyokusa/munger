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
    monkeypatch.setattr(config, "REAL_MONEY_DATA_PATH", tmp_path / "real_money.json")
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
    # Any name not explicitly given defaults to "missing" (M20 finding:
    # every existing test predates real_money.json and shouldn't need to
    # start naming it explicitly just to keep passing -- a missing object
    # is bridge()'s own default-tolerated case for this file anyway).
    bucket.blob.side_effect = lambda name: blobs.get(name, _fake_blob(missing=True))
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
        b'{"date": "2026-07-28", "equity": 100000.0}\n{"date": "2026-07-29", "equity": 100500.0}\n'
    )
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 100500.0}'),
            "pnl_history.jsonl": _fake_blob(content=history_jsonl),
            "prices.json": _fake_blob(missing=True),
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


def test_bridge_writes_pnl_json_to_report_dir_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # M19 (DESIGN.md 3.8.1): k3s's nginx container has no filesystem
    # visibility outside REPORT_DIR (its volumeMount is scoped to the
    # PVC's `report` subPath) -- unlike Cloud Run, which aliases
    # DATA_DIR/pnl.json directly from its always-current FUSE mount, k3s
    # needs an actual second copy inside REPORT_DIR for pnl.html's
    # client-side poller (M19) to have anything to fetch.
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 100500.0}'),
            "pnl_history.jsonl": _fake_blob(missing=True),
            "prices.json": _fake_blob(missing=True),
        },
    )

    gcs_bridge.bridge()

    assert config.PNL_DATA_PATH.read_text() == '{"equity": 100500.0}'
    assert (config.REPORT_DIR / "pnl.json").read_text() == '{"equity": 100500.0}'


def test_bridge_downloads_real_money_json_and_writes_a_report_dir_copy_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M20 (DESIGN_REAL_MONEY.md §4): same DATA_DIR-root + REPORT_DIR-copy
    # treatment as pnl.json above, for the live account's own snapshot.
    # GCS source is "live/pnl.json" (daily-trade-live.yml's real upload
    # path), not "real_money.json" -- a real mismatch bug an earlier
    # version of this had, caught live during the k3s dev deploy (this
    # test would have kept passing throughout, since its mock used the
    # same wrong key as the buggy code -- a reminder that a test mirroring
    # a bug doesn't catch it; only the live run did). pnl.json itself is
    # required (bridge()'s always-first, fail-loud download), so it needs
    # a real entry here too even though this test is about the live
    # snapshot specifically.
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b"{}"),
            "live/pnl.json": _fake_blob(content=b'{"mode": "live", "equity": 812.34}'),
        },
    )

    gcs_bridge.bridge()

    assert config.REAL_MONEY_DATA_PATH.read_text() == '{"mode": "live", "equity": 812.34}'
    assert (
        config.REPORT_DIR / "real_money.json"
    ).read_text() == '{"mode": "live", "equity": 812.34}'


def test_bridge_tolerates_a_missing_real_money_json_as_the_expected_pre_launch_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike pnl.json (whose absence means the long-established bridge is
    # broken -- fail loud), a missing real_money.json is the default state
    # until the live trading pipeline is actually provisioned and running.
    _patch_client(monkeypatch, {"pnl.json": _fake_blob(content=b"{}")})

    gcs_bridge.bridge()  # must not raise

    assert not config.REAL_MONEY_DATA_PATH.exists()
    assert not (config.REPORT_DIR / "real_money.json").exists()


def test_bridge_tolerates_a_missing_pnl_history_as_the_expected_first_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 100000.0}'),
            "pnl_history.jsonl": _fake_blob(missing=True),
            "prices.json": _fake_blob(missing=True),
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
            "prices.json": _fake_blob(content=b'{"generated_at": "x", "symbols": {}}'),
        },
    )

    gcs_bridge.bridge()

    assert list(config.PNL_DATA_PATH.parent.glob("*.tmp")) == []
    assert list(config.REPORT_DIR.glob("*.tmp")) == []


def test_bridge_downloads_prices_json_and_publishes_a_flat_array_for_grafana(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # prices.json (M17 Phase 2) is prices.py's own nested-by-symbol shape,
    # downloaded as-is. Grafana's Infinity datasource + "Partition by
    # values" transform want a flat, long-format array instead -- derived
    # here into prices_flat.json, the same "raw + derived pair" pattern
    # pnl_history.jsonl/.json already uses.
    prices_json = (
        b'{"generated_at": "2026-08-08T00:00:00+00:00", "symbols": {'
        b'"AAPL": {"status": "ok", "points": ['
        b'{"date": "2026-08-05", "close": 200.0}, '
        b'{"date": "2026-08-06", "close": 202.5}], "fail_reasons": []}, '
        b'"ZZZZ": {"status": "no_bars", "points": [], "fail_reasons": []}}}'
    )
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b"{}"),
            "pnl_history.jsonl": _fake_blob(missing=True),
            "prices.json": _fake_blob(content=prices_json),
        },
    )

    gcs_bridge.bridge()

    assert (config.REPORT_DIR / "prices.json").read_bytes() == prices_json
    flat = json.loads((config.REPORT_DIR / "prices_flat.json").read_text())
    assert flat == [
        {"symbol": "AAPL", "date": "2026-08-05", "close": 200.0},
        {"symbol": "AAPL", "date": "2026-08-06", "close": 202.5},
    ]


def test_bridge_leaves_both_prices_files_on_last_good_state_if_prices_json_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prices_path = config.REPORT_DIR / "prices.json"
    flat_path = config.REPORT_DIR / "prices_flat.json"
    config.REPORT_DIR.mkdir(parents=True)
    prices_path.write_text('{"generated_at": "x", "symbols": {}}')
    flat_path.write_text("[]")

    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b"{}"),
            "pnl_history.jsonl": _fake_blob(missing=True),
            "prices.json": _fake_blob(content=b"not valid json"),
        },
    )

    with pytest.raises(json.JSONDecodeError):
        gcs_bridge.bridge()

    assert prices_path.read_text() == '{"generated_at": "x", "symbols": {}}'
    assert flat_path.read_text() == "[]"
    assert list(config.REPORT_DIR.glob("*.tmp")) == []


def test_bridge_tolerates_a_missing_prices_json_without_skipping_the_other_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real bug this guards against: an early `return` on a missing
    # pnl_history.jsonl (the pre-M17-Phase-2 shape of this function) would
    # have silently skipped prices.json too, and a naive re-add of
    # prices.json handling after that return could equally skip nothing
    # being downloaded at all if ordered wrong. Missing prices.json is its
    # own expected first-run/no-positions case, independent of the other
    # two files, which must still bridge normally.
    _patch_client(
        monkeypatch,
        {
            "pnl.json": _fake_blob(content=b'{"equity": 1.0}'),
            "pnl_history.jsonl": _fake_blob(content=b'{"date": "2026-08-08"}\n'),
            "prices.json": _fake_blob(missing=True),
        },
    )

    gcs_bridge.bridge()

    assert config.PNL_DATA_PATH.read_text() == '{"equity": 1.0}'
    assert (config.REPORT_DIR / "pnl_history.jsonl").exists()
    assert (config.REPORT_DIR / "pnl_history.json").exists()
    assert not (config.REPORT_DIR / "prices.json").exists()
    assert not (config.REPORT_DIR / "prices_flat.json").exists()
    assert list(config.REPORT_DIR.glob("*.tmp")) == []
