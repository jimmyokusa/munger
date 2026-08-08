#!/bin/bash
# Runs (via munger-gcs-pull.timer, every 15 min) as a one-shot
# google/cloud-sdk:slim container on the VM's own network namespace
# (--network=host), so it can always reach the GCE metadata server at
# 169.254.169.254 regardless of the Docker bridge's routing -- gcloud
# auto-detects the VM's attached instance service account
# (munger-grafana-vm@..., storage.objectViewer, bucket-wide) via that
# metadata server. No key file anywhere on this VM.
#
# Also pulls prices.json (M17 Phase 2) as of 2026-08-08 -- filename kept
# as pull-pnl-history.sh despite pulling more than that now, matching this
# project's own established "don't rename a working thing, just document
# it" convention (see the k3s "traefik" job-name precedent).
#
# Same transform as deploy/k8s/40-gcs-reader-cronjob.yaml/gcs_bridge.py
# (pnl_history.jsonl -> .json array since Infinity parses JSON not NDJSON;
# prices.json's nested-by-symbol shape -> a flat {symbol,date,close} array
# since that's what Grafana's "Partition by values" transform needs) and
# the same temp-file-plus-rename atomicity so json-server's nginx never
# serves a torn file mid-write.
set -euo pipefail

BUCKET="gs://munger-503515-data"
DATA_DIR="/data"

# staff-engineer-reviewer finding: a transform failure (set -e) exits mid-
# block, before the raw *.tmp is renamed into place -- unlike
# gcs_bridge.py's _download_to_temp, which explicitly unlinks its temp file
# on any exception. Harmless in practice (the next successful 15-minute
# tick's gcloud storage cp overwrites the same path), but this EXIT trap
# closes the gap the same way: it only ever removes leftover *.tmp files
# (the success path has already mv -f'd them to their final names by the
# time this runs), so it is a safe no-op on the happy path.
trap 'rm -f "$DATA_DIR"/.*.tmp' EXIT

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

# prices.json (M17 Phase 2, DESIGN_DASHBOARDS.md §3): pulled as-is (the
# nested-by-symbol source of truth) and flattened into prices_flat.json
# (one row per {symbol, date, close}) for Grafana -- same derivation as
# gcs_bridge.py's _flatten_prices(), reimplemented here since this script
# has no import path to that module (it runs in a bare cloud-sdk container,
# not the munger image). Missing is tolerated the same way pnl_history.jsonl's
# absence is above: an expected state (no positions currently held, or this
# object hasn't been produced by a run yet), not a broken pull.
cp_stderr="$(mktemp)"
if gcloud storage cp "$BUCKET/prices.json" "$DATA_DIR/.prices.json.tmp" 2>"$cp_stderr"; then
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr"
  python3 - "$DATA_DIR/.prices.json.tmp" "$DATA_DIR/.prices_flat.json.tmp" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    snapshot = json.load(f)
symbols = snapshot.get("symbols") or {}
assert isinstance(symbols, dict), f"expected symbols to be a dict, got {type(symbols)}"
rows = []
for symbol, entry in symbols.items():
    if not isinstance(entry, dict):
        continue
    for point in entry.get("points") or []:
        rows.append({"symbol": symbol, "date": point["date"], "close": point["close"]})
with open(dst, "w") as f:
    json.dump(rows, f)
PY
  mv -f "$DATA_DIR/.prices.json.tmp" "$DATA_DIR/prices.json"
  mv -f "$DATA_DIR/.prices_flat.json.tmp" "$DATA_DIR/prices_flat.json"
  echo "Pulled prices.json (+ flattened prices_flat.json for Grafana)."
elif grep -q "matched no objects" "$cp_stderr"; then
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr"
  rm -f "$DATA_DIR/.prices.json.tmp"
  echo "No prices.json in GCS yet -- leaving any existing local copy in place."
else
  cat "$cp_stderr" >&2
  rm -f "$cp_stderr" "$DATA_DIR/.prices.json.tmp"
  echo "pull-pnl-history.sh: real failure downloading prices.json (not a first-run 404) -- see stderr above." >&2
  exit 1
fi
