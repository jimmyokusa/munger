#!/usr/bin/env bash
# Munger CI/CD to the Raspberry Pi k3s cluster.
#
# Flow:
#   1. Build the arm64 app image (deploy/Dockerfile) on this machine.
#   2. Side-load it into k3s's containerd on the storage node (no registry).
#   3. CI gate: run the full pytest suite as an in-cluster Job; abort on fail.
#   4. CD: apply namespace, PVC, nginx report Deployment, and the daily
#      screen CronJob. Optionally seed the first screen immediately.
#
# Access model (see memory: pi-k3s-cluster):
#   * kubectl works only on the control-plane node (pi4), via `sudo kubectl`.
#   * The image is imported to the node the pods are pinned to (pi1).
# Both are reached over SSH; this script runs from the dev machine.
set -euo pipefail

# --- Config -----------------------------------------------------------------
IMAGE="${IMAGE:-munger}"
STORAGE_NODE="${STORAGE_NODE:-pi1}"   # node the PVC + pods are pinned to
CONTROL_NODE="${CONTROL_NODE:-pi4}"   # node with kubectl
NAMESPACE="munger"
SEED="${SEED:-1}"                     # trigger an immediate screen after deploy
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"

kctl() { ssh "$CONTROL_NODE" "sudo kubectl $*"; }
apply() { ssh "$CONTROL_NODE" "sudo kubectl apply -f -"; }

echo "==> [1/4] Building ${IMAGE}:${TAG} (linux/arm64)"
docker build --platform linux/arm64 -f deploy/Dockerfile \
  -t "${IMAGE}:latest" -t "${IMAGE}:${TAG}" .

echo "==> [2/4] Importing image into k3s containerd on ${STORAGE_NODE}"
docker save "${IMAGE}:latest" "${IMAGE}:${TAG}" \
  | ssh "$STORAGE_NODE" "sudo k3s ctr images import -"

echo "==> [3/4] CI: running pytest as an in-cluster Job"
kctl apply -f - < deploy/k8s/00-namespace.yaml >/dev/null
kctl -n "$NAMESPACE" delete job munger-ci-test --ignore-not-found >/dev/null
apply < deploy/k8s/ci-test-job.yaml >/dev/null
# Wait for either completion or failure, whichever comes first.
if ! ssh "$CONTROL_NODE" "sudo kubectl -n $NAMESPACE wait --for=condition=complete \
      job/munger-ci-test --timeout=900s" 2>/dev/null; then
  echo "!! CI failed. Logs:"
  kctl -n "$NAMESPACE" logs job/munger-ci-test || true
  exit 1
fi
echo "   CI passed."

echo "==> [4/4] CD: applying report web + daily-screen CronJob + P&L bridge + Grafana"
# Apply each manifest separately: piping several files into one
# `kubectl apply -f -` concatenates them WITHOUT a `---` between files, so
# the last doc of one file and the first of the next merge into a single,
# key-clobbered document (this silently dropped the Service once already).
#
# 40-gcs-reader-cronjob.yaml (M17) needs the read-only GCS key Secret to
# exist first (see that file's header for the create commands):
#   sudo kubectl -n munger create secret generic munger-gcs-reader-key \
#     --from-file=key.json=/path/to/gcs-reader.json
# It's applied regardless -- a missing Secret only makes the scheduled Job
# fail, it doesn't block the rest of the deploy. 50-grafana.yaml installs the
# Infinity plugin at pod startup, so its node needs outbound internet once.
for f in deploy/k8s/10-pvc.yaml deploy/k8s/20-report-web.yaml \
         deploy/k8s/30-daily-screen-cronjob.yaml \
         deploy/k8s/40-gcs-reader-cronjob.yaml \
         deploy/k8s/50-grafana.yaml; do
  apply < "$f"
done

if [ "$SEED" = "1" ]; then
  echo "==> Seeding: triggering the first screen now (Job munger-seed)"
  kctl -n "$NAMESPACE" delete job munger-seed --ignore-not-found >/dev/null
  kctl -n "$NAMESPACE" create job munger-seed --from=cronjob/munger-daily-screen
fi

NODE_IP="$(ssh "$CONTROL_NODE" "hostname -I | awk '{print \$1}'")"
echo "==> Done. Report will be at: http://${NODE_IP}:30080 (after the first screen completes)"
echo "    Watch it:  ssh $CONTROL_NODE 'sudo kubectl -n $NAMESPACE get pods -w'"
