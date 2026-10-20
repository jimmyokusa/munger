# Archived: Raspberry Pi k3s deployment

**Retired 2026-10-20.** Kept for reference only. Nothing here is run, and
none of it is wired into CI or any deploy. See TASKS.md M52.

From July to October 2026 munger also ran on a 4-node Raspberry Pi k3s
cluster: a daily-screen CronJob, an nginx report viewer, a GCS->PVC reader
CronJob, Grafana, an in-cluster pytest Job, and the self-hosted GitHub
Actions runner that `ci.yml` used.

What replaced each piece:

| k3s piece | Now |
|---|---|
| `build-and-deploy.sh` (build, side-load, CI Job, apply) | `deploy/cloudrun/deploy.sh [daily-screen\|report-web] --verify` |
| `k8s/30-daily-screen-cronjob.yaml` | Cloud Run job `daily-screen` + Cloud Scheduler `munger-daily-screen` |
| `k8s/20-report-web.yaml` | Cloud Run service `report-web`, behind gramunger.com |
| `k8s/40-gcs-reader-cronjob.yaml` + `k8s/50-grafana.yaml` | `deploy/gce/` (Grafana VM; `pull-pnl-history.sh` does the same transform) |
| `k8s/ci-test-job.yaml` + the self-hosted runner | `.github/workflows/ci.yml` on GitHub-hosted `ubuntu-latest` |

Files were moved here with `git mv`, so `git log --follow` still shows
their full history.
