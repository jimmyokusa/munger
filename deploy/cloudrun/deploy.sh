#!/usr/bin/env bash
# One-command build + deploy for Munger's Cloud Run prod targets.
#
# Flow:
#   1. Build the target's image via Cloud Build (`gcloud builds submit`)
#      using an inline cloudbuild.yaml, and push it to Artifact Registry.
#   2. Resolve the digest Cloud Build just produced (via
#      `gcloud artifacts docker images describe`, not by parsing build
#      log output -- more reliable) and update the Cloud Run resource to
#      pin that exact digest.
#   3. Optionally trigger a verification run and poll it to completion,
#      printing a clear pass/fail.
#
# Targets:
#   daily-screen  (default) -- Cloud Run *Job*. Built from the repo root
#                  with deploy/Dockerfile. This is the one that needs
#                  rebuilding for any report.py/template/screen-logic
#                  change.
#   report-web     -- Cloud Run *Service* (nginx, GCS-FUSE mount of the
#                  report/ dir at runtime; bakes in no report content).
#                  Only needs rebuilding for its own Dockerfile/nginx.conf.
#
# Gotcha #1 (see deploy/Dockerfile.dockerignore's own header comment):
#   the daily-screen build MUST run with BuildKit (env DOCKER_BUILDKIT=1
#   in the Cloud Build step). Without it, Docker ignores
#   deploy/Dockerfile.dockerignore and falls back to the repo-root
#   .dockerignore -- which is scoped for report.Dockerfile and excludes
#   almost everything -- and the build fails with "requirements.txt not
#   found in build context." This env var is not optional for that target.
#
# Gotcha #2: Cloud Run Jobs pin to an image *digest* at update time.
#   Re-pushing the same `:latest` tag alone does nothing until the Job is
#   explicitly updated to the new digest (some digest resolutions of a
#   bare `:latest` can silently no-op against a cached resolution) -- so
#   this script always resolves and passes an explicit `@sha256:...`
#   digest, never a bare tag.
#
# Usage:
#   deploy/cloudrun/deploy.sh [daily-screen|report-web] [--verify]
#   VERIFY=1 deploy/cloudrun/deploy.sh report-web
set -euo pipefail

# --- Config -------------------------------------------------------------
PROJECT="${PROJECT:-munger-503515}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-munger}"                # Artifact Registry repo name
AR_HOST="${REGION}-docker.pkg.dev"
VERIFY="${VERIFY:-0}"                       # trigger + poll a verification run after deploy (mirrors build-and-deploy.sh's SEED convention; default 0 here since a daily-screen execution fetches the full ticker universe and is comparatively slow/costly to run on every deploy -- opt in with --verify or VERIFY=1)
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-10}"
POLL_MAX_ATTEMPTS="${POLL_MAX_ATTEMPTS:-180}"  # 180 * 10s = 30 min ceiling

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# --- Args -----------------------------------------------------------------
TARGET="daily-screen"
for arg in "$@"; do
  case "$arg" in
    daily-screen|report-web)
      TARGET="$arg"
      ;;
    --verify)
      VERIFY=1
      ;;
    -h|--help)
      echo "Usage: $0 [daily-screen|report-web] [--verify]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [daily-screen|report-web] [--verify]" >&2
      exit 1
      ;;
  esac
done

# --- Per-target config ------------------------------------------------------
# BUILD_CONTEXT is the source root uploaded to Cloud Build; DOCKERFILE is
# the docker-build -f path, relative to BUILD_CONTEXT.
case "$TARGET" in
  daily-screen)
    BUILD_CONTEXT="."
    DOCKERFILE="deploy/Dockerfile"
    RESOURCE_KIND="job"
    ;;
  report-web)
    # This Dockerfile COPYs nginx.conf relative to its own directory, so
    # the build context must be that directory (not the repo root) --
    # unlike daily-screen, which builds from the full repo.
    BUILD_CONTEXT="deploy/cloudrun/report-web"
    DOCKERFILE="Dockerfile"
    RESOURCE_KIND="service"
    ;;
  *)
    echo "Unknown target: $TARGET" >&2
    exit 1
    ;;
esac

IMAGE="${AR_HOST}/${PROJECT}/${AR_REPO}/${TARGET}:latest"

echo "==> Target: ${TARGET} (${RESOURCE_KIND})"
echo "==> Image:  ${IMAGE}"

# --- [1/3] Build --------------------------------------------------------
CLOUDBUILD_YAML="$(mktemp -t "cloudbuild-${TARGET}.XXXXXX")"
trap 'rm -f "$CLOUDBUILD_YAML"' EXIT

cat > "$CLOUDBUILD_YAML" <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build', '-f', '${DOCKERFILE}', '-t', '${IMAGE}', '.']
  env: ['DOCKER_BUILDKIT=1']
images:
- ${IMAGE}
EOF

echo "==> [1/3] Building ${TARGET} via Cloud Build (context: ${BUILD_CONTEXT})"
gcloud builds submit \
  --project="$PROJECT" \
  --config="$CLOUDBUILD_YAML" \
  "$BUILD_CONTEXT"

# --- [2/3] Resolve digest and update the Cloud Run resource --------------
echo "==> [2/3] Resolving pushed digest for ${IMAGE}"
DIGEST="$(gcloud artifacts docker images describe "$IMAGE" \
  --project="$PROJECT" \
  --format='get(image_summary.digest)')"

if [ -z "$DIGEST" ]; then
  echo "!! Could not resolve a digest for ${IMAGE}; aborting before updating ${TARGET}." >&2
  exit 1
fi

IMAGE_AT_DIGEST="${AR_HOST}/${PROJECT}/${AR_REPO}/${TARGET}@${DIGEST}"
echo "    Resolved digest: ${DIGEST}"
echo "    Pinning ${TARGET} to: ${IMAGE_AT_DIGEST}"

if [ "$RESOURCE_KIND" = "job" ]; then
  gcloud run jobs update "$TARGET" \
    --region="$REGION" \
    --project="$PROJECT" \
    --image="$IMAGE_AT_DIGEST"
else
  gcloud run deploy "$TARGET" \
    --region="$REGION" \
    --project="$PROJECT" \
    --image="$IMAGE_AT_DIGEST"
fi
echo "    ${TARGET} updated."

# --- [3/3] Optional verification -----------------------------------------
if [ "$VERIFY" != "1" ]; then
  echo "==> [3/3] Skipping verification (pass --verify or set VERIFY=1 to run it)."
  echo "==> Done."
  exit 0
fi

if [ "$RESOURCE_KIND" != "job" ]; then
  # report-web is a Service, not a Job -- there's no "execute" to poll.
  # Do a lightweight health check against its live URL instead.
  echo "==> [3/3] Verifying ${TARGET} (Service): checking live URL"
  SERVICE_URL="$(gcloud run services describe "$TARGET" \
    --region="$REGION" \
    --project="$PROJECT" \
    --format='value(status.url)')"
  HTTP_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "$SERVICE_URL")"
  echo "    ${SERVICE_URL} -> HTTP ${HTTP_STATUS}"
  if [ "$HTTP_STATUS" = "200" ]; then
    echo "==> PASS: ${TARGET} responded 200 at ${SERVICE_URL}"
    exit 0
  else
    echo "==> FAIL: ${TARGET} responded HTTP ${HTTP_STATUS} at ${SERVICE_URL}" >&2
    exit 1
  fi
fi

echo "==> [3/3] Verifying ${TARGET} (Job): triggering an execution"
EXECUTION_NAME="$(gcloud run jobs execute "$TARGET" \
  --region="$REGION" \
  --project="$PROJECT" \
  --async \
  --format='value(metadata.name)')"
echo "    Triggered execution: ${EXECUTION_NAME}"

STATUS="Unknown"
ATTEMPT=0
while [ "$STATUS" = "Unknown" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if [ "$ATTEMPT" -gt "$POLL_MAX_ATTEMPTS" ]; then
    echo "==> FAIL: execution ${EXECUTION_NAME} still Unknown after $((POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS))s; giving up." >&2
    exit 1
  fi
  sleep "$POLL_INTERVAL_SECONDS"
  STATUS="$(gcloud run jobs executions describe "$EXECUTION_NAME" \
    --region="$REGION" \
    --project="$PROJECT" \
    --format='value(status.conditions[0].status)')"
  echo "    [poll ${ATTEMPT}/${POLL_MAX_ATTEMPTS}] status.conditions[0].status = ${STATUS}"
done

SUCCEEDED_COUNT="$(gcloud run jobs executions describe "$EXECUTION_NAME" \
  --region="$REGION" \
  --project="$PROJECT" \
  --format='value(status.succeededCount)')"
FAILED_COUNT="$(gcloud run jobs executions describe "$EXECUTION_NAME" \
  --region="$REGION" \
  --project="$PROJECT" \
  --format='value(status.failedCount)')"

echo "    succeededCount=${SUCCEEDED_COUNT:-0} failedCount=${FAILED_COUNT:-0}"

if [ "${FAILED_COUNT:-0}" = "0" ] && [ "${SUCCEEDED_COUNT:-0}" != "0" ]; then
  echo "==> PASS: execution ${EXECUTION_NAME} succeeded."
  exit 0
else
  echo "==> FAIL: execution ${EXECUTION_NAME} did not succeed (succeededCount=${SUCCEEDED_COUNT:-0}, failedCount=${FAILED_COUNT:-0})." >&2
  exit 1
fi
