"""Read-only GCS -> PVC bridge for the k3s deployment (M17).

pnl.py runs only in GitHub Actions (that's where the Alpaca credentials
live -- M14 screen-only boundary) and writes pnl.json + the durable
pnl_history.jsonl append-series into config.PNL_GCS_BUCKET. The k3s report
deployment deliberately has no Alpaca keys and has no local copy of either
file -- this script pulls the already-produced artifacts from GCS onto the
shared PVC so report.py's pnl.html gets real data and Grafana can chart the
durable history.

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


def bridge() -> None:
    """Pulls pnl.json and pnl_history.jsonl from GCS onto the local PVC."""
    client = storage.Client()
    bucket = client.bucket(config.PNL_GCS_BUCKET)

    _download_atomically(bucket.blob("pnl.json"), config.PNL_DATA_PATH)
    logger.info("Bridged pnl.json.")

    history_dest = config.REPORT_DIR / "pnl_history.jsonl"
    json_dest = config.REPORT_DIR / "pnl_history.json"
    try:
        jsonl_tmp = _download_to_temp(bucket.blob("pnl_history.jsonl"), history_dest)
    except NotFound:
        logger.info("No pnl_history.jsonl in GCS yet -- skipping (first run).")
        return

    # Transform from the temp download *before* committing either rename --
    # Grafana's Infinity datasource parses JSON, not NDJSON, so
    # pnl_history.jsonl needs a JSON-array sibling for the dashboard to
    # read. Deriving it here (rather than from the already-renamed jsonl)
    # means a malformed line fails loudly with BOTH files left on their
    # last-good state, instead of jsonl updating while json -- what Grafana
    # actually reads -- silently freezes on stale content.
    try:
        rows = [json.loads(line) for line in jsonl_tmp.read_text().splitlines() if line.strip()]
        json_tmp = json_dest.with_suffix(json_dest.suffix + ".tmp")
        json_tmp.write_text(json.dumps(rows))
    except BaseException:
        jsonl_tmp.unlink(missing_ok=True)
        raise

    jsonl_tmp.replace(history_dest)
    json_tmp.replace(json_dest)
    logger.info("Bridged pnl_history.jsonl (+ .json array for Grafana).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bridge()
