---
name: munger-workflow
description: >-
  Engineering workflow and CI/CD for the munger project. Use whenever
  planning, writing, reviewing, testing, or deploying munger code — it
  defines the three review gates (staff-engineer for design/code,
  pm-reviewer for scope, warren-buffett for investment thesis), Google-style
  Python, end-to-end testing, the modular-monolith / distributed-v2
  architecture stance, and how the app ships to the Raspberry Pi k3s cluster
  (build → in-cluster pytest CI → CronJob + nginx CD, staging → prod).
  Invoke before starting a change, before committing/pushing, and before
  deploying.
---

# Munger engineering workflow & CI/CD

munger is a modular monolith deployed to a 4-node Raspberry Pi **k3s**
cluster. This skill is the single source of truth for *how we work* on it.
The rules below are enforced partly by git hooks (`.claude/settings.json`
+ `.claude/hooks/`) and partly by discipline — follow them even where a
hook wouldn't catch a lapse. Living session context is in `HANDOFF.md`; the
formal work log is `TASKS.md`; durable facts are in Claude memory
(`pi-k3s-cluster`, `pi-k3s-munger-deploy`).

## The team (three review gates)

Every change is reviewed along three independent axes, by three subagents:

| Reviewer | Judges | Gated on |
|----------|--------|----------|
| **staff-engineer-reviewer** | code + design: correctness, reliability, idempotency, data integrity, ops risk | `DESIGN.md` commit; any `.py` push |
| **pm-reviewer** | scope, sequencing, success criteria, scope-creep | `TASKS.md` commit |
| **warren-buffett** | the *investment thesis*: moats vs. cigar-butts, margin of safety, circle of competence, concentration, turnover | changes to the screening logic (advisory) |

They are complementary — code soundness, project scope, and investment
soundness are different questions. Details below.

## 1. Break work into small, PM-reviewed milestones

- Every change starts as a **small, independently shippable milestone** —
  one usable, inspectable slice, not a big-bang feature. This mirrors the
  M0…M14 history in `README.md`/`TASKS.md`.
- Record the milestone and its acceptance criteria in **`TASKS.md`**.
- **`TASKS.md` changes require a `pm-reviewer` pass** (scope, sequencing,
  success criteria, scope-creep risk). The commit hook
  (`check-review-markers.sh`) blocks a `git commit` that stages `TASKS.md`
  until its `sha256` matches `.claude/review-markers/tasks-pm-reviewer.sha256`.
  Flow: run the `pm-reviewer` subagent against the staged `TASKS.md`, apply
  fixes, then write `shasum -a 256 TASKS.md` into that marker file.
- Prefer the `agent-skills:plan` skill to decompose anything non-trivial
  before writing code.

## 2. Design changes → staff-engineer review

- Architecture/spec lives in **`DESIGN.md`** (v1) and
  **`DESIGN_DISTRIBUTED.md`** (v2). Changing `DESIGN.md` requires a
  **`staff-engineer-reviewer`** pass (architecture soundness, reliability,
  idempotency, data integrity, operational risk).
- Mechanism: the commit hook blocks staged `DESIGN.md` until its `sha256`
  matches `.claude/review-markers/design-staff-engineer.sha256`.
- `DESIGN_DISTRIBUTED.md` isn't hook-gated, but by convention it gets the
  same staff-engineer review (it already had one — see its §5/§10).

## 3. All Python code → staff-engineer review before push

- **Every `.py` change is reviewed by `staff-engineer-reviewer` before it
  is pushed.** The push hook (`check-code-push-review.sh`) denies
  `git push` when any `*.py` file changed between the last-reviewed commit
  (`.claude/review-markers/code-push-staff-engineer.sha`, untracked/local)
  and `HEAD`.
  Flow: run `staff-engineer-reviewer` against
  `git diff <marker_or_upstream>...HEAD`, apply fixes, then write
  `git rev-parse HEAD` into that marker file. **Never commit that marker.**
- This review earns its keep: on the M14/deploy diff it caught a real
  data-integrity bug (the daily screen archived throttled runs as clean
  history) and a node-OOM oversubscription risk, among others.
- For focused Python-quality passes, the repo also ships a `python-reviewer`
  agent; use it alongside, not instead of, the staff-engineer gate.
- Do not commit or push unless the user asks.

## 4. Investment-thesis review → warren-buffett

- Changes to **what the system decides about businesses** — `DESIGN.md`
  §1–3, `screener.py` (Graham gates / Munger score), `config.py` screening
  thresholds — should get a **`warren-buffett`** pass
  (`.claude/agents/warren-buffett.md`).
- It judges value-investing soundness: does a change buy *wonderful
  businesses at fair prices* or drift toward statistically-cheap
  "cigar-butts"? Does it preserve the margin of safety, respect the circle
  of competence, keep concentration and near-zero turnover? It is the guard
  against short-term / price-driven / sentiment-driven logic creeping into
  a long-term weighing-machine system.
- Advisory (not hook-gated), but treat it as required for thesis changes.

## 5. Google Python Style Guide

- Follow the **Google Python Style Guide**
  (https://google.github.io/styleguide/pyguide.html): imports grouped
  stdlib/third-party/local, full type annotations, Google-style docstrings
  (`Args:`/`Returns:`/`Raises:`), descriptive names, guard clauses over deep
  nesting, comments that explain *why*. Match the existing code — it already
  reads this way (see `config.py`, `report.py`).
- Every threshold/toggle is a named constant in `config.py`; nothing
  downstream hard-codes a number or flag. Writable paths derive from
  `config.DATA_DIR` (env `MUNGER_DATA_DIR`) so they can live on a PVC.

## 6. Extensive end-to-end testing

- **`pytest` is the gate for behavior.** Unit-test each module and add
  **end-to-end tests** that exercise a real flow (universe → screen →
  archive → report), mocking only the external edges (yfinance, the
  broker), never the logic under test. `tests/test_daily_screen.py` is the
  model: it asserts the *whole* `run()` sequence and even that
  `execution.py` is never imported.
- Tests must be self-contained: redirect every `config.*_PATH` /
  `DATA_RAW_CACHE_DIR` to `tmp_path` via an autouse fixture (see
  `_isolate_config`) so a test never writes into the real repo — this also
  keeps them green as a non-root container (the CI Job caught two tests that
  escaped this).
- The **same suite runs in-cluster as CI** on the arm64 image
  (`deploy/k8s/ci-test-job.yaml`) — a green local run and a green in-cluster
  run are both required before shipping.

## 7. Architecture: modular monolith; distribute only when justified

- Default is the **modular monolith**: independent modules (`universe`,
  `data`, `screener`, `portfolio`, `execution`, `journal`, `report`, `bot`)
  behind clean function boundaries, one deployable image.
- The cluster is **4× ~900MB-RAM arm64 Pis** — per-service overhead is
  expensive. **Do not split into microservices by default.** Split only
  with a concrete driver: a different runtime cadence, an independent
  scaling/failure boundary, or a hard isolation requirement. The one split
  we make is exactly that kind — the **screen-only daily job**
  (`daily_screen.py`), separate from `bot.py`'s quarterly trading because it
  must be architecturally incapable of trading (never imports `execution`).
- **v2 distribution (designed, not built):** `DESIGN_DISTRIBUTED.md`
  rearchitects only the heavy, rate-limited fetch stage as a scatter-gather
  across the 4 nodes behind a shared Redis fundamentals cache — *not* a
  microservices decomposition, and execution stays centralized. It is gated:
  **D0** (empirically confirm the yfinance rate limit is per-IP) and **D1.5**
  (measure the cache actually pays off) must pass before building the
  distributed machinery. This gate-first discipline is the model for any
  large rearchitecture here.

## 8. CI/CD on the k3s cluster

Cluster facts (memory `pi-k3s-cluster`): k3s v1.30, nodes pi1–pi4 (arm64),
control-plane **pi4** (`sudo kubectl` only works there), storage node
**pi1** (PVC + pods pinned there via `nodeSelector`; local-path is
node-local), containerd runtime — **no registry, images are side-loaded**.

### One-command pipeline

```bash
./deploy/build-and-deploy.sh
```

1. **Build** the arm64 image from `deploy/Dockerfile` (tags `latest` + git
   short-SHA). Requires the `docker-buildx` plugin so the per-Dockerfile
   `deploy/Dockerfile.dockerignore` is honored (ships source+tests, never
   `.env`/state/local report).
2. **Side-load** into pi1's containerd: `docker save … | ssh pi1 'sudo k3s
   ctr images import -'` (gzip the pipe; a full transfer to a Pi is ~5 min).
   Manifests use `imagePullPolicy: Never`.
3. **CI gate** — apply `ci-test-job.yaml` (pytest on the shipped image);
   abort unless it completes successfully.
4. **CD** — apply the PVC, nginx report `Deployment`+`Service` (NodePort
   **30080**), and the daily-screen `CronJob`; seed one screen.

Report serves at `http://<node-ip>:30080` (verified live on all four node
IPs). To regenerate just the report from existing PVC data without a full
re-screen, run a one-off Job with `command: ["python","report.py"]`.

### Deploy gotchas (learned the hard way — don't repeat)

- **Apply each manifest file separately.** Piping several YAML files into
  one `kubectl apply -f -` concatenates them **without** a `---`, silently
  merging (key-clobbering) the last doc of one file into the next — this
  dropped the Service once. `build-and-deploy.sh` now loops per-file.
- **Namespace before namespaced resources** — apply `00-namespace.yaml`
  alone first, or the PVC fails "namespace not found" in the same stream.
- **RWO + local-path** means the nginx Deployment uses `strategy: Recreate`
  and both it and the CronJob are pinned to pi1.

### Staging → prod (chosen model; not yet built)

Target: two namespaces, **`munger-staging`** and **`munger-prod`**.
`build-and-deploy.sh` deploys the CI-passed image to staging, runs a smoke
check (report serves, a screen runs), then **promotes the *same* image
digest** to prod (promote, don't rebuild). No always-on CI server / registry
— keeps footprint low on the Pis. Until this lands, the single `munger`
namespace is effectively prod.

### Safety & operations

- **Screen-only safety:** the daily CronJob mounts **no Alpaca secrets** and
  runs `daily_screen.py`, which can't import `execution.py`. `daily_screen`
  also gates archiving on `screener.fetched_fraction` ≥
  `config.MIN_UNIVERSE_FETCH_FRACTION` so a throttled run never pollutes the
  history. Keep both properties for anything scheduled here.
- **Rollback:** re-import a prior build as `:latest` (manifests reference
  the mutable tag; pin `:$TAG` if precise per-version rollback is needed).
  PVC data (archives, journal) is independent of the image.
- **Known constraints / open ops items:** full ~1500-ticker screens are
  slow + rate-limited on a 1GB Pi (lower `DATA_FETCH_THREAD_POOL_WORKERS` or
  the universe before raising memory limits); **pi4 needs a static IP** or a
  DHCP change re-breaks the agents; the local-path PVC has **no backup**
  until v2's D4 (Litestream) lands; archives grow unbounded (retention TODO).

```bash
# All kubectl runs on pi4:
ssh pi4 'sudo kubectl -n munger get pods,cronjob,pvc,svc'
ssh pi4 'sudo kubectl -n munger create job manual --from=cronjob/munger-daily-screen'
```

## 9. Definition of done for a change

1. Small milestone captured in `TASKS.md` (pm-reviewer if `TASKS.md` changed).
2. Google-style Python; constants in `config.py`.
3. Unit + end-to-end tests added; `pytest` green locally (198+ currently).
4. `staff-engineer-reviewer` pass on the `.py` diff; marker updated.
   Investment-logic changes also get a `warren-buffett` pass.
5. In-cluster CI Job green on the arm64 image.
6. Deployed via `deploy/build-and-deploy.sh`; report verified at `:30080`
   (and, once built, promoted staging → prod).
