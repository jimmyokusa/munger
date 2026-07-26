# Session handoff — resume here

A running log of this work session so we can pick up cleanly after any
disconnect (including switching machines). Newest context at the top of
each section. Formal work log is in [TASKS.md](TASKS.md); durable facts are
in Claude memory (`pi-k3s-cluster`, `pi-k3s-munger-deploy`). This file is
the "what were we doing and what's next" narrative.

_Last updated: 2026-07-26 (this update)._

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
- **M15 (Report UX overhaul): all 3 slices shipped, committed, and pushed**
  (`c96eb90`, `b43f3de`, `f6214fd` — this was stale in the prior version of
  this file, which still said only slice 1 had landed). Slice 1: visual
  badges, metric tooltips, methodology drawer. Slice 2: SEC EDGAR/Finviz
  research links, Copy-JSON/Export-CSV buttons. Slice 3: `feed.json`/
  `rss.xml`. Several real bugs caught by staff-engineer-reviewer rounds
  across the three slices before push (badge logic, Graham-gate miscount,
  an XSS-adjacent unescaped `</script>` in the export JSON embed, invalid
  XML entity in `rss.xml`, redundant archive re-scans, a silent-zero data
  gap) — full detail in `TASKS.md`'s M15 rows.
- **2026-07-26: agent-run visual smoke-test done, but does NOT close the
  user-confirmation item.** Rendered a fresh local `report.generate_report()`
  and opened it in-browser (via a local static server — `file://` doesn't
  load in the browser tooling used). All three slices check out
  structurally: badges, tooltips, methodology drawer (correctly separates
  Stage 1/Stage 2), research links, export buttons, and a valid
  `feed.json`/`rss.xml` all render/parse correctly (responsive/narrow-
  viewport behavior wasn't exercised). **pm-reviewer flagged that marking
  the actual "user visual confirmation" row `done` off this would repeat
  exactly the self-certification M13 row 354 was created to prevent** —
  that row stays `todo`, and is now doubly blocked: even if the user looks
  at `gramunger.com` today, they won't see M15 (next item).
- **Real gap found in the process: `gramunger.com` (Cloud Run) is stale.**
  Its live report shows *none* of M15 — no badges, drawer, links, or export
  buttons, just bare ticker/score rows. The Cloud Run images were last
  rebuilt 2026-07-25 ~18:54 UTC (for the GCS bug fixes only), before any
  M15 commit landed. `daily-screen` has kept running successfully since,
  but it's the *old* report code regenerating output — a redeploy is
  needed to actually ship M15 to prod. See the new `TASKS.md` row.
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

1. ~~Look at the actual rendered report and confirm M15 reads and looks
   right~~ — **done 2026-07-26**, all 3 slices confirmed via a local
   render (see above). Superseded by #1 below.
1. **Rebuild + redeploy the Cloud Run images so `gramunger.com` actually
   serves M15.** The live site is running pre-M15 code (see the gap noted
   above / `TASKS.md`'s new "Cloud Run image is stale" row). This is now
   the most concrete, low-risk next action.
2. **Decide which deployment target is canonical** — k3s, Cloud Run, or
   both permanently — and write it down in `TASKS.md`. Gates further
   investment in either (deploy scripts, migrations, feed work that needs
   a stable canonical URL). *(Note: TASKS.md's Infra section already
   records a 2026-07-26 user decision — k3s dev / Cloud Run prod — so this
   may already be resolved; double check before treating it as still open.)*
3. **Confirm GCP cost/billing exposure** (Cloud Run, Artifact Registry,
   GCS, and the `gramunger.com` domain registration) is within free-tier
   limits or an accepted cost — more pressing now that `daily-screen` runs
   on an automated daily trigger, not just ad hoc.
4. **Sync to GitHub.** As of 2026-07-26 the M15 slices are already
   committed and pushed (`c96eb90`, `b43f3de`, `f6214fd`); working tree is
   clean and even with `origin/main`. This step is done for that work —
   only re-applies to whatever new changes come out of steps above (e.g.
   setting `MUNGER_REPORT_BASE_URL` on Cloud Run, a deploy script).
5. **Re-verify the k3s report still serves end-to-end** — last confirmed
   2026-07-24, not re-checked since:
   `ssh pi4 'sudo kubectl -n munger get pods'` then
   `ssh pi4 'curl -s -o /dev/null -w "%{http_code}\n" http://localhost:30080/'`.
6. **Fully automated CI/CD pipeline** (user request, 2026-07-26) — needs
   its own design pass first; see `TASKS.md`'s Infra section. **v2 build**:
   start at **D0** in `DESIGN_DISTRIBUTED.md`, whenever picked back up.

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
