"""Read-only GCS -> PVC bridge for the k3s deployment (M17).

pnl.py and prices.py run only in GitHub Actions (that's where the Alpaca
credentials live -- M14 screen-only boundary) and write pnl.json, the
durable pnl_history.jsonl append-series, and prices.json into
config.PNL_GCS_BUCKET. The k3s report deployment deliberately has no
Alpaca keys and has no local copy of any of them -- this script pulls the
already-produced artifacts from GCS onto the shared PVC so report.py's
pnl.html gets real data and Grafana can chart the durable history and the
held-symbol daily closes.

Run as a standalone k3s CronJob (deploy/k8s/40-gcs-reader-cronjob.yaml),
inside the same `munger` image already built and side-loaded for
daily-screen/report-web -- not a third-party image, so there is no
multi-arch-availability risk on the arm64 Pi cluster (google/cloud-sdk:slim
turned out to be amd64-only despite being assumed multi-arch; found live,
not theoretically). Auth is a dedicated read-only service account key
(GOOGLE_APPLICATION_CREDENTIALS, mounted from a k8s Secret) -- not the
Alpaca credentials, so the trading boundary holds.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import storage

import config

logger = logging.getLogger(__name__)


def _download_to_temp(blob: storage.Blob, dest: Path) -> Path:
    """Downloads a blob to a temp path sibling to `dest`, without renaming it into place.

    Callers that must derive further content from the download (the JSONL ->
    JSON transform in `bridge()`) validate/transform from this temp path
    first and only rename once that succeeds -- so a parse failure never
    partially commits (see `bridge()`).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    try:
        blob.download_to_filename(str(tmp_path))
    except BaseException:
        # download_to_filename opens/truncates the destination *before*
        # issuing the GET and only self-cleans on DataCorruption -- so a
        # NotFound (the expected missing-pnl_history first-run case, caught
        # in bridge()) would otherwise leave a zero-byte .tmp orphaned.
        # Remove it on any failure so the atomic-write guarantee holds on
        # the error path too, not just the success path.
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _download_atomically(blob: storage.Blob, dest: Path) -> None:
    """Downloads a blob to `dest` via temp-file-plus-rename.

    Same atomicity pattern used throughout this codebase (pnl.py,
    screener.py) -- a reader (nginx, report.py, Grafana) must never observe
    a torn/partial file mid-download.
    """
    _download_to_temp(blob, dest).replace(dest)


def _flatten_prices(prices_snapshot: dict[str, object]) -> list[dict[str, object]]:
    """Flattens prices.py's nested-by-symbol shape into long-format rows.

    prices.json (prices.py's own output) is `{"symbols": {"AAPL": {"status":
    ..., "points": [{"date": ..., "close": ...}, ...], ...}, ...}}` -- rich
    and keyed by symbol, good for programmatic/diagnostic consumption
    (status, fail_reasons). Grafana's Infinity datasource wants a flat,
    long-format array instead (`[{"symbol": ..., "date": ..., "close": ...},
    ...]`), the same one-row-per-data-point shape Grafana's own
    "Partition by values" transform expects to split into a per-symbol
    multi-series chart -- so this is a Grafana-consumption *view*, derived
    here, not a change to prices.py's own richer contract. A symbol with no
    valid points (status "no_bars"/"no_valid_bars") simply contributes zero
    rows, which is exactly the "drops off the chart" moving-window
    semantics DESIGN_DASHBOARDS.md §3 already calls for.
    """
    symbols = prices_snapshot.get("symbols") or {}
    assert isinstance(symbols, dict)
    rows: list[dict[str, object]] = []
    for symbol, entry in symbols.items():
        if not isinstance(entry, dict):
            continue
        for point in entry.get("points") or []:
            rows.append({"symbol": symbol, "date": point["date"], "close": point["close"]})
    return rows


def bridge() -> None:
    """Pulls pnl.json, pnl_history.jsonl, and prices.json from GCS onto the local PVC."""
    client = storage.Client()
    bucket = client.bucket(config.PNL_GCS_BUCKET)

    pnl_blob = bucket.blob("pnl.json")
    _download_atomically(pnl_blob, config.PNL_DATA_PATH)
    # M19 (DESIGN.md 3.8.1): a second copy inside REPORT_DIR, the only tree
    # k3s's nginx container can see at all (its volumeMount is scoped to
    # the PVC's `report` subPath -- unlike Cloud Run's FUSE mount, there is
    # no filesystem visibility outside it to alias from). Cloud Run instead
    # serves DATA_DIR/pnl.json directly via an nginx exact-match location,
    # since its mount already covers both paths; k3s has no such option, so
    # this is a second real download, not a symlink or an alias.
    _download_atomically(pnl_blob, config.REPORT_DIR / "pnl.json")
    logger.info("Bridged pnl.json (+ REPORT_DIR copy for the report-web nginx to serve).")

    # M20 (DESIGN_REAL_MONEY.md §4): the live account's own snapshot, same
    # DATA_DIR-root + REPORT_DIR-copy treatment as pnl.json above. Unlike
    # pnl.json (whose absence means the core, long-established bridge is
    # broken and should fail loud), a missing real_money.json is the
    # expected default state until the live trading pipeline is actually
    # provisioned and running -- tolerated the same way pnl_history.jsonl/
    # prices.json's "not deployed yet" case already is below.
    try:
        real_money_blob = bucket.blob("real_money.json")
        _download_atomically(real_money_blob, config.REAL_MONEY_DATA_PATH)
        _download_atomically(real_money_blob, config.REPORT_DIR / "real_money.json")
    except NotFound:
        logger.info("No real_money.json in GCS yet -- skipping (live pipeline not live yet).")
    else:
        logger.info("Bridged real_money.json (+ REPORT_DIR copy for the report-web nginx).")

    history_dest = config.REPORT_DIR / "pnl_history.jsonl"
    json_dest = config.REPORT_DIR / "pnl_history.json"
    try:
        jsonl_tmp = _download_to_temp(bucket.blob("pnl_history.jsonl"), history_dest)
    except NotFound:
        logger.info("No pnl_history.jsonl in GCS yet -- skipping (first run).")
    else:
        # Transform from the temp download *before* committing either
        # rename -- Grafana's Infinity datasource parses JSON, not NDJSON,
        # so pnl_history.jsonl needs a JSON-array sibling for the dashboard
        # to read. Deriving it here (rather than from the already-renamed
        # jsonl) means a malformed line fails loudly with BOTH files left
        # on their last-good state, instead of jsonl updating while json --
        # what Grafana actually reads -- silently freezes on stale content.
        try:
            rows = [
                json.loads(line) for line in jsonl_tmp.read_text().splitlines() if line.strip()
            ]
            json_tmp = json_dest.with_suffix(json_dest.suffix + ".tmp")
            json_tmp.write_text(json.dumps(rows))
        except BaseException:
            jsonl_tmp.unlink(missing_ok=True)
            raise

        # staff-engineer-reviewer finding: these two renames are sequential,
        # not transactional -- a crash between them (OOM, node reboot)
        # leaves history_dest advanced while json_dest (what Grafana
        # actually reads) stays one day stale. Accepted: this CronJob
        # re-derives both files from scratch every run, so the exposure
        # window is bounded to at most one day and self-heals on the next
        # tick -- not worth a two-phase-commit for a read-only reporting
        # pipeline. Same shape/tradeoff as the prices.json block below.
        jsonl_tmp.replace(history_dest)
        json_tmp.replace(json_dest)
        logger.info("Bridged pnl_history.jsonl (+ .json array for Grafana).")

    # prices.json (M17 Phase 2, DESIGN_DASHBOARDS.md §3). Missing is
    # tolerated the same way pnl_history.jsonl's absence is (an early,
    # `else`-skipped state above): Phase 2 not deployed yet, or no
    # positions currently held, are both expected, not a broken bridge --
    # this must NOT `return` early like the pre-refactor pnl_history
    # handling used to, or a missing prices.json would silently also skip
    # pnl_history.jsonl on a run where it exists. (pnl.json itself
    # downloads first and unconditionally, above, with no NotFound
    # handling -- its absence still aborts bridge() entirely, same as
    # before this refactor; only pnl_history.jsonl and prices.json are
    # independent of each other.)
    prices_dest = config.REPORT_DIR / "prices.json"
    flat_dest = config.REPORT_DIR / "prices_flat.json"
    try:
        prices_tmp = _download_to_temp(bucket.blob("prices.json"), prices_dest)
    except NotFound:
        logger.info("No prices.json in GCS yet -- skipping.")
    else:
        # Same "transform from the temp download before committing either
        # rename" reasoning as the pnl_history block above: a malformed
        # prices.json fails loudly with both files left on their last-good
        # state, instead of prices.json updating while prices_flat.json --
        # what Grafana actually reads -- silently freezes on stale content.
        try:
            snapshot = json.loads(prices_tmp.read_text())
            rows = _flatten_prices(snapshot)
            flat_tmp = flat_dest.with_suffix(flat_dest.suffix + ".tmp")
            flat_tmp.write_text(json.dumps(rows))
        except BaseException:
            prices_tmp.unlink(missing_ok=True)
            raise

        prices_tmp.replace(prices_dest)
        flat_tmp.replace(flat_dest)
        logger.info("Bridged prices.json (+ flattened prices_flat.json for Grafana).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bridge()
