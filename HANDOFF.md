# Session handoff — resume here

A running log of this work session so we can pick up cleanly after any
disconnect (including switching machines). Newest context at the top of
each section. Formal work log is in [TASKS.md](TASKS.md); durable facts are
in Claude memory (`pi-k3s-cluster`, `pi-k3s-munger-deploy`). This file is
the "what were we doing and what's next" narrative.

_Last updated: 2026-07-26._

---

## TL;DR — current state

- **munger v1 (modular monolith): working.** Full test suite **215
  passing** locally.
- **Pi k3s deployment: working, as of the last verified state (2026-07-24).**
  Namespace `munger`: CI test Job → daily-screen CronJob + nginx report
  viewer, on a PVC. Report was confirmed serving end-to-end on all four
  node IPs. Not re-verified since.
- **Google Cloud Run deployment: now working end-to-end.** GCP project
  `munger-503515` (`us-central1`). Two real GCS-specific bugs (a
  per-object mutation rate limit on the live-progress file; `chmod` calls
  during archival that GCS FUSE rejects) were found and fixed, the images
  rebuilt and redeployed, and a daily Cloud Scheduler trigger wired up.
  **Two consecutive successful executions**: a manual run 2026-07-25, and
  the Scheduler's first automatic fire 2026-07-26 13:00 UTC — both
  archived a real ~1503-ticker screen and regenerated the report. The
  public site (`gramunger.com` + `www.gramunger.com`, both live, domain
  registered/mapped this session) is currently serving real, current data
  (7 buyable candidates as of the latest run), not stale/placeholder
  content.
- **Which deployment target is canonical is still undecided** (pm-reviewer
  flagged this as a `blocked` item in `TASKS.md` — a call only the user
  can make). Both k3s and Cloud Run now technically work; nothing
  documents whether Cloud Run replaces the Pi cluster, runs permanently
  alongside it, or was just an exploration. Don't invest further in either
  side (a migration, a Cloud Run deploy script, dual-running long-term)
  until this is settled. Two more open pm-reviewer items in the same
  section: GCP cost/billing exposure (now more pressing given the daily
  Scheduler trigger) hasn't been confirmed as free-tier/negligible, and the
  domain registration's own recurring cost isn't folded into that either.
- **M15 (Report UX overhaul) slice 1 shipped**: visual badges, metric
  tooltips, and a methodology drawer on `report.py`'s pages. Two real bugs
  a staff-engineer-reviewer round caught and fixed before push: a badge
  logic error that would have shown "Zero debt" for a *negative*
  debt/equity (negative book equity — a red flag, the same trap
  `screener.py` already guards against), and a methodology drawer that
  miscounted the Graham gates and blurred them with the separate Munger
  quality-floor stage. **Still open: nobody has looked at the actual
  rendered page yet** (same checkpoint M13 kept open even after code
  review passed) — do that before starting slice 2 (export buttons +
  outbound research links) or slice 3 (JSON/RSS feed).
- **v2 distributed architecture: designed, not built.** See
  `DESIGN_DISTRIBUTED.md`. Unchanged this session.
- **Nothing committed/pushed since 2026-07-24** (the last PR, #1, merged
  the prior session's k3s/M14/v2-design work). This session's Cloud Run
  fix + M15 slice 1 diff is still uncommitted — get it reviewed, committed,
  pushed.

---

## What we did, in order (2026-07-25 → 2026-07-26)

1. **Picked up mid-flight, undocumented Cloud Run work.** On resuming, the
   working tree already had a Cloud Run migration in progress that wasn't
   captured anywhere: `deploy/cloudrun/report-web/{Dockerfile,nginx.conf}`
   (new), plus a fix for a GCS per-object mutation rate limit that starved
   fetch threads on the first Cloud Run seed run. GCP infra (bucket,
   Artifact Registry, the two Cloud Run resources) already existed,
   provisioned by hand.

2. **Investigated why the most recent execution still failed** even with
   the rate-limit fix deployed via env var. Root cause: GCS FUSE rejects
   `chmod`, and both `journal.archive_screen_results` (`shutil.copy`) and
   `report._copy_archives_into_report` (`shutil.copy2`) call it internally.
   Fixed both to `shutil.copyfile`; added tests proving neither calls
   `chmod` anymore. `staff-engineer-reviewer` passed this diff.

3. **Registered and domain-mapped `gramunger.com`** to the `report-web`
   Cloud Run service (user decision) — both apex and `www` now live.

4. **Rebuilt and redeployed the Cloud Run images with both fixes**, and a
   Cloud Scheduler trigger (`munger-daily-screen`, daily 13:00 UTC) was
   wired up. Result: the manual post-redeploy run and the Scheduler's first
   automatic fire the next day both succeeded end-to-end — the first two
   successful executions this target has ever had.

5. **Shipped M15 slice 1** (user request, split into 3 milestones at the
   user's direction): badges, metric tooltips, and a methodology drawer in
   `report.py`. `staff-engineer-reviewer` round 2 caught and fixed two real
   bugs (see TL;DR) before this was considered push-ready.

6. **Updated docs throughout**: `TASKS.md` (Cloud Run Infra section + new
   M15 section), `README.md`, this file, Claude memory — corrected more
   than once as the actual Cloud Run state kept turning out better than
   what had just been written down (see "a note on staleness" below).

7. **Ran the required review gates** on both diffs (Cloud Run fixes;
   M15 slice 1) — `pm-reviewer` on the `TASKS.md` changes,
   `staff-engineer-reviewer` on the `.py` diffs — and applied every fix
   both requested before considering either ready to commit.

### A note on staleness (why this file keeps getting corrected)

Twice this session, TASKS.md/HANDOFF.md/README.md were written to say the
Cloud Run target "hasn't succeeded yet" at the exact moment it was actually
mid-redeploy or had just started succeeding — the docs were accurate when
written but stale within the hour because real infrastructure (a rebuild,
a Scheduler trigger, a fresh execution) kept moving underneath them. If
you're resuming this and something here looks off, **re-verify against
live `gcloud`/`curl` state before trusting this file** — it's a snapshot,
not a live view, and this specific deployment target has proven to change
faster than the doc update cycle so far.

---

## Immediate next steps (resume here)

1. **Look at the actual rendered report** (`gramunger.com` or a local
   `report.generate_report()` run) and confirm M15 slice 1 (badges,
   tooltips, methodology drawer) reads and looks right. This is the one
   open item blocking slice 1 from being fully closed out (M13 precedent).
2. **Decide which deployment target is canonical** — k3s, Cloud Run, or
   both permanently — and write it down in `TASKS.md`. Gates further
   investment in either (deploy scripts, migrations, feed work that needs
   a stable canonical URL).
3. **Confirm GCP cost/billing exposure** (Cloud Run, Artifact Registry,
   GCS, and the `gramunger.com` domain registration) is within free-tier
   limits or an accepted cost — more pressing now that `daily-screen` runs
   on an automated daily trigger, not just ad hoc.
4. **Sync to GitHub.** We're on `main`, remote `github.com:jimmyokusa/munger`,
   HEAD even with origin. Both review gates already ran (see above) —
   remaining mechanics: update
   `.claude/review-markers/tasks-pm-reviewer.sha256` = `sha256(TASKS.md)`
   (final content, post-fixes) before committing, and write
   `git rev-parse HEAD` to `.claude/review-markers/code-push-staff-engineer.sha`
   (untracked) before pushing. Commit in logical chunks (Cloud Run bug
   fixes, M15 slice 1, docs), push, open a PR.
5. **Re-verify the k3s report still serves end-to-end** — last confirmed
   2026-07-24, not re-checked since:
   `ssh pi4 'sudo kubectl -n munger get pods'` then
   `ssh pi4 'curl -s -o /dev/null -w "%{http_code}\n" http://localhost:30080/'`.
6. **M15 slice 2** (export buttons + outbound research links) once slice 1
   is confirmed (#1 above). **v2 build**: start at **D0** in
   `DESIGN_DISTRIBUTED.md`, whenever picked back up.

---

## Key facts to resume with

- **Cloud Run:** GCP project `munger-503515`, region `us-central1`.
  `gcloud` auth: account `jimmyokusa@gmail.com` (already configured on this
  machine — **if resuming from a different machine, `gcloud auth login` +
  `gcloud config set project munger-503515` first**). Bucket
  `munger-503515-data` (GCS FUSE-mounted at `/mnt/data`). Artifact
  Registry repo `munger` (Docker, `us-central1`). Service `report-web`
  (live at `gramunger.com`/`www.gramunger.com`, both HTTP 200). Job
  `daily-screen` (2 consecutive successful executions as of 2026-07-26;
  see the staleness note above before trusting that count). Cloud
  Scheduler `munger-daily-screen` (daily 13:00 UTC, `ENABLED`). Check job
  history: `gcloud run jobs executions list --job=daily-screen
  --region=us-central1`; tail logs: `gcloud logging read
  'resource.type="cloud_run_job" resource.labels.job_name="daily-screen"
  labels."run.googleapis.com/execution_name"="<name>"'`.
- **Cluster (k3s):** k3s v1.30, arm64 Pis. `ssh pi1..pi4`. kubectl only on
  **pi4** (`sudo kubectl`). Images side-loaded to **pi1** (`sudo k3s ctr
  images import`) — no registry. NodePort **30080** for the report. Full
  image transfer to a Pi ~5 min. **Durable TODO: give pi4 a static IP** or
  the agents fall off again on the next DHCP change.
- **Local dev:** `.venv/bin/python -m pytest -q` (system python3 is 3.9 and
  lacks `TypeGuard`; use the venv). `MUNGER_DATA_DIR` repoints writable
  paths; `MUNGER_PROGRESS_WRITE_MIN_INTERVAL_SECONDS` throttles the
  per-ticker progress write (needed on GCS FUSE, not on local disk/PVC).
- **Governance:** review-gate hooks in `.claude/`; see the `munger-workflow`
  skill. Don't commit the `.sha` push-marker (it's gitignored/untracked).

---

## Files changed/added this session (2026-07-25 → 2026-07-26)

Modified: `config.py`, `data.py`, `journal.py`, `report.py`,
`tests/test_data.py`, `tests/test_journal.py`, `tests/test_report.py`,
`TASKS.md`, `README.md`, this file.
Added: `deploy/cloudrun/report-web/{Dockerfile,nginx.conf}`.

_(Prior session's — 2026-07-24 — uncommitted changes were already merged
via PR #1 before this session started; only the work listed above remains
uncommitted.)_
