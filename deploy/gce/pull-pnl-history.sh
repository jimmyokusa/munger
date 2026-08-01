#!/bin/bash
# Runs (via munger-gcs-pull.timer, every 15 min) as a one-shot
# google/cloud-sdk:slim container on the VM's own network namespace
# (--network=host), so it can always reach the GCE metadata server at
# 169.254.169.254 regardless of the Docker bridge's routing -- gcloud
# auto-detects the VM's attached instance service account
# (munger-grafana-vm@..., storage.objectViewer, bucket-scoped) via that
# metadata server. No key file anywhere on this VM.
#
# Same transform as deploy/k8s/40-gcs-reader-cronjob.yaml (JSONL -> JSON
# array, since Infinity parses JSON, not NDJSON) and the same
# temp-file-plus-rename atomicity so json-server's nginx never serves a
# torn file mid-write.
set -euo pipefail

BUCKET="gs://munger-503515-data"
DATA_DIR="/data"

if gcloud storage cp "$BUCKET/pnl_history.jsonl" "$DATA_DIR/.pnl_history.jsonl.tmp"; then
  mv -f "$DATA_DIR/.pnl_history.jsonl.tmp" "$DATA_DIR/pnl_history.jsonl"
  python3 - "$DATA_DIR/pnl_history.jsonl" "$DATA_DIR/.pnl_history.json.tmp" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    rows = [json.loads(line) for line in f if line.strip()]
with open(dst, "w") as f:
    json.dump(rows, f)
PY
  mv -f "$DATA_DIR/.pnl_history.json.tmp" "$DATA_DIR/pnl_history.json"
  echo "Pulled pnl_history.jsonl ($(wc -l < "$DATA_DIR/pnl_history.jsonl") rows)."
else
  echo "No pnl_history.jsonl in GCS yet -- leaving any existing local copy in place."
fi
