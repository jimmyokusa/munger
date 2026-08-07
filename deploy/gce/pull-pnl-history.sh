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

# gcloud storage cp's only distinguishable "object doesn't exist yet" signal
# is its stderr text -- everything else (revoked IAM binding, network
# partition, typo'd bucket) must fail loudly instead of being swallowed into
# the same silent-success path (same distinction gcs_bridge.py makes via
# google.api_core.exceptions.NotFound; mirrored here since this script has
# no Python GCS client available, only the gcloud CLI).
cp_stderr="$(mktemp)"
if gcloud storage cp "$BUCKET/pnl_history.jsonl" "$DATA_DIR/.pnl_history.jsonl.tmp" 2>"$cp_stderr"; then
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr"
  # Transform from the just-downloaded temp file *before* committing either
  # rename, so a malformed line fails loudly with both pnl_history.jsonl and
  # pnl_history.json left on their last-good state -- not jsonl updated
  # while json (what Grafana actually reads) silently freezes.
  python3 - "$DATA_DIR/.pnl_history.jsonl.tmp" "$DATA_DIR/.pnl_history.json.tmp" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    rows = [json.loads(line) for line in f if line.strip()]
with open(dst, "w") as f:
    json.dump(rows, f)
PY
  mv -f "$DATA_DIR/.pnl_history.jsonl.tmp" "$DATA_DIR/pnl_history.jsonl"
  mv -f "$DATA_DIR/.pnl_history.json.tmp" "$DATA_DIR/pnl_history.json"
  echo "Pulled pnl_history.jsonl ($(wc -l < "$DATA_DIR/pnl_history.jsonl") rows)."
elif grep -q "matched no objects" "$cp_stderr"; then
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr"
  rm -f "$DATA_DIR/.pnl_history.jsonl.tmp"
  echo "No pnl_history.jsonl in GCS yet -- leaving any existing local copy in place."
else
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr" "$DATA_DIR/.pnl_history.jsonl.tmp"
  echo "pull-pnl-history.sh: real failure downloading pnl_history.jsonl (not a first-run 404) -- see stderr above." >&2
  exit 1
fi
