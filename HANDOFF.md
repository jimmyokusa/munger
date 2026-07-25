# Session handoff — resume here

A running log of this work session so we can pick up cleanly after any
disconnect. Newest context at the top of each section. Formal work log is
in [TASKS.md](TASKS.md); durable facts are in Claude memory
(`pi-k3s-cluster`, `pi-k3s-munger-deploy`). This file is the "what were we
doing and what's next" narrative.

_Last updated: 2026-07-24._

---

## TL;DR — current state

- **munger v1 (modular monolith): working.** Full test suite **195
  passing** locally and in-cluster.
- **Deployed to the Pi k3s cluster** (namespace `munger`): CI test Job →
  daily-screen CronJob + nginx report viewer, on a PVC. Running, but the
  report has **not been confirmed serving end-to-end** yet.
- **v2 distributed architecture: designed, not built.** See
  `DESIGN_DISTRIBUTED.md`. Next real build step is **D0** (validate the
  per-IP rate-limit assumption) then **D1** (fundamentals cache).
- **Nothing committed/pushed yet.** Working tree has all the changes below;
  the sync is the open action (see "Immediate next steps").

---

## What we did, in order

1. **Picked up in-progress M14 work** (daily screen + report calendar) that
   was uncommitted with 2 failing tests. Fixed the tests and a real bug
   (calendar linked to archive CSVs at a path that wouldn't resolve when
   `report/` is the web root) by making the report self-contained
   (`generate_report()` copies archives under `report/`). → `report.py`,
   `tests/test_report.py`, `daily_screen.py`, `tests/test_daily_screen.py`.

2. **Diagnosed + fixed the k3s cluster.** pi1/pi2/pi3 were `NotReady`
   because pi4's control-plane IP drifted via DHCP (.227→.204) and the
   agents still pointed at the dead IP. Repointed `K3S_URL`, cleared the
   stale agent LB cache, restarted `k3s-agent`; disabled a stray microk8s
   on pi2 holding kubelet port 10250. **All 4 nodes now `Ready`.**

3. **Built + deployed the full self-updating stack** (user chose this over
   a static viewer). Added `config.py` `MUNGER_DATA_DIR` override,
   `deploy/Dockerfile` (+ `Dockerfile.dockerignore`), `deploy/k8s/*`
   manifests, `deploy/build-and-deploy.sh`. Built the arm64 image, imported
   to pi1 containerd, ran the in-cluster CI Job — which **caught two real
   issues** (a writable path escaping `DATA_DIR`; two non-hermetic tests),
   both fixed. CI then **195 passed in-cluster**. Applied CD workloads;
   seeded the first screen.

4. **Wrote the project skill** `.claude/skills/munger-workflow/SKILL.md`:
   PM-reviewed small milestones, staff-engineer review of all `.py`,
   Google Python style, e2e tests, microservices-only-when-justified, and
   the k3s CI/CD flow.

5. **Designed the v2 distributed architecture** → `DESIGN_DISTRIBUTED.md`.
   Scatter-gather screen across the 4 nodes behind a shared Redis
   fundamentals cache; coordinator reduces + executes centrally. Milestones
   D0–D5, gated on D0 (per-IP rate-limit validation).

6. **Updated docs** (this session's ask): README (M14 + Deployment +
   v2 pointer), TASKS.md (M14, Infra, v2 sections), this HANDOFF.md, and
   Claude memory.

---

## Immediate next steps (resume here)

1. **Sync to GitHub** (open ask). We're on `main`, remote
   `github.com:jimmyokusa/munger`, HEAD even with origin. Working tree is
   uncommitted. The repo's own hooks gate this:
   - committing `TASKS.md` → requires a **pm-reviewer** pass, then update
     `.claude/review-markers/tasks-pm-reviewer.sha256` = `sha256(TASKS.md)`.
   - pushing `.py` changes → requires a **staff-engineer-reviewer** pass on
     the diff, then write `git rev-parse HEAD` to
     `.claude/review-markers/code-push-staff-engineer.sha` (untracked).
   - `DESIGN_DISTRIBUTED.md` isn't hook-gated (only `DESIGN.md` is), but per
     governance it should get a staff-engineer review too.
   Plan: branch off `main`, run the required reviewers, apply fixes, update
   markers, commit in logical chunks (M14 / deploy+skill / v2 design),
   push, open a PR.
2. **Verify the report serves end-to-end.** Saw `HTTP 000` on NodePort
   30080 during the first seed (report dir not yet populated). Re-check
   after a screen completes:
   `ssh pi4 'sudo kubectl -n munger get pods'` then
   `ssh pi4 'curl -s -o /dev/null -w "%{http_code}\n" http://localhost:30080/'`.
3. **v2 build** (when ready): start at **D0** in `DESIGN_DISTRIBUTED.md`.

---

## Key facts to resume with

- **Cluster:** k3s v1.30, arm64 Pis. `ssh pi1..pi4`. kubectl only on **pi4**
  (`sudo kubectl`). Images side-loaded to **pi1** (`sudo k3s ctr images
  import`) — no registry. local-path storage is node-local → workloads
  pinned to pi1. NodePort **30080** for the report. Full image transfer to
  a Pi ~5 min. **Durable TODO: give pi4 a static IP** or the agents fall
  off again on the next DHCP change.
- **Deploy:** `./deploy/build-and-deploy.sh` does build → import → CI gate →
  CD → seed. Manifests in `deploy/k8s/` (numbered apply order).
- **Local dev:** `.venv/bin/python -m pytest -q` (system python3 is 3.9 and
  lacks `TypeGuard`; use the venv). `MUNGER_DATA_DIR` repoints writable
  paths.
- **Governance:** review-gate hooks in `.claude/`; see the `munger-workflow`
  skill. Don't commit the `.sha` push-marker (it's gitignored/untracked).

---

## Files changed/added this session

Modified: `config.py`, `report.py`, `tests/test_report.py`,
`tests/test_data.py`.
Added: `daily_screen.py`, `tests/test_daily_screen.py`,
`.github/workflows/daily-screen.yml`, `deploy/` (Dockerfile,
Dockerfile.dockerignore, build-and-deploy.sh, k8s/*),
`.claude/skills/munger-workflow/SKILL.md`, `DESIGN_DISTRIBUTED.md`,
`HANDOFF.md`, and README/TASKS doc updates.
