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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _download_atomically(blob: storage.Blob, dest: Path) -> None:
    """Downloads a blob to `dest` via temp-file-plus-rename.

    Same atomicity pattern used throughout this codebase (pnl.py,
    screener.py) -- a reader (nginx, report.py, Grafana) must never observe
    a torn/partial file mid-download.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    blob.download_to_filename(str(tmp_path))
    tmp_path.replace(dest)


def _write_json_array_from_jsonl(jsonl_path: Path, json_path: Path) -> None:
    """Publishes a JSON-array rendition of a JSONL file.

    Grafana's Infinity datasource parses JSON, not NDJSON, so
    pnl_history.jsonl (one JSON object per line) needs a JSON-array sibling
    for the dashboard to read.
    """
    rows = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]
    tmp_path = json_path.with_suffix(json_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(rows))
    tmp_path.replace(json_path)


def bridge() -> None:
    """Pulls pnl.json and pnl_history.jsonl from GCS onto the local PVC."""
    client = storage.Client()
    bucket = client.bucket(config.PNL_GCS_BUCKET)

    _download_atomically(bucket.blob("pnl.json"), config.PNL_DATA_PATH)
    logger.info("Bridged pnl.json.")

    history_dest = config.REPORT_DIR / "pnl_history.jsonl"
    try:
        _download_atomically(bucket.blob("pnl_history.jsonl"), history_dest)
    except NotFound:
        logger.info("No pnl_history.jsonl in GCS yet -- skipping (first run).")
        return

    _write_json_array_from_jsonl(history_dest, config.REPORT_DIR / "pnl_history.json")
    logger.info("Bridged pnl_history.jsonl (+ .json array for Grafana).")


if __name__ == "__main__":
    bridge()
