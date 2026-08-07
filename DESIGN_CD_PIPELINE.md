# Design: Automated CD Pipeline — k3s (dev) → 24h soak → Cloud Run (prod)

**Status: DRAFT for human/pm review. Not implemented. Not TASKS.md/DESIGN.md
content yet — do not treat anything below as decided.**

_Written 2026-07-26, in response to the TASKS.md Infra row "Fully automated
CI/CD for the dev→prod pipeline (user request, 2026-07-26)". That row asked
for exactly this design pass before any implementation starts, given "the
operational risk of a bad build auto-promoting to `gramunger.com`" — a real,
public, unauthenticated site with actual visitors._

---

## 0. Why this needs a design pass, not just a script

The user-decided pipeline (TASKS.md Infra section, 2026-07-26) is: **k3s is
dev, Cloud Run is prod; ship to k3s first, run it 24h as a dev soak, then
promote the same build to Cloud Run.** Nothing enforces this today. Every
deploy this session was done by hand:

- k3s side: `deploy/build-and-deploy.sh` — builds locally, side-loads via
  `k3s ctr images import` (no registry at all), runs pytest as an in-cluster
  Job, applies manifests.
- Cloud Run side: `gcloud builds submit` with an inline `cloudbuild` config,
  then `gcloud run jobs update --image=...@sha256:...` to repoint the Job at
  the new digest, then a manual verification execution.

These are two **independent build pipelines** for what is supposed to be one
promoted artifact. There is no shared tag, no automated soak check, and —
most importantly — this session shipped at least two real bugs straight to
`gramunger.com` that had passing tests and a passing `staff-engineer-reviewer`
pass on the code review:

- `6412d72` — "Fail reasons column showed literal `nan` for passing tickers."
  `pd.read_csv` turns an empty `fail_reasons` string back into `NaN`, and
  `str(x or "")` still rendered `"nan"` because `NaN` is truthy in Python.
  **Found live on `gramunger.com/tickers.html`.**
- `8ed812b` — "Score every ticker independent of buyable; stop mislabeling
  no-dividend as `data_missing`." ~99% of tickers showed `score=0` because
  score was only computed for tickers that passed every gate; separately,
  `dividend_yield` was wrongly listed as a required metric, so non-dividend
  payers (a legitimate outcome, not a fetch gap) got tagged `data_missing`
  and silently failed the screen even with the dividend gate disabled.
  **Found live on `gramunger.com`.**

Both bugs are semantic/output-correctness bugs, not crashes — pytest was
green and mypy/ruff were clean for both. This is the central design problem:
**"tests pass" is a necessary gate but was already proven insufficient** to
catch the two real bugs that actually reached the public site this session.
Any pipeline design that stops at "run pytest, then promote" reproduces
exactly the gap that let these ship.

---

## 1. Pipeline stages

### 1.1 What triggers a build

Proposed: **push to `main`** (after CI passes on the PR, same as this repo's
existing GitHub Actions workflows for the trading-bot side), plus
`workflow_dispatch` for a manual trigger — mirroring the pattern already used
by `daily-screen.yml`/`daily-trade.yml`/`heartbeat.yml`. Every push to
`main` that touches app code (not docs) kicks off stage 1 (build + tag +
push to a registry + deploy to k3s dev). Promotion to Cloud Run is a
**separate, later** trigger (see 1.4) — it does not fire on the same push.

Open design choice: should build even trigger automatically at all, or should
building be gated behind a manual `workflow_dispatch` too, given the
Raspberry Pi cluster has limited resources and a bad automatic deploy to dev
already has *some* cost (breaks the dev soak signal, page 1 of TASKS.md shows
several sessions where hand-driven redeploys were the norm)? Recommendation:
auto-build+deploy-to-dev on push to `main` is low-risk (dev-only, no public
exposure) and is the whole point of having a dev environment — keep it
automatic. Promotion to prod is the step that needs a gate (§2).

### 1.2 One shared image/tag, not two independent builds

Today there is no shared identity between the k3s image and the Cloud Run
image — they're built by two different toolchains (`docker build --platform
linux/arm64` on a dev machine vs. `gcloud builds submit` in GCP) from
possibly-different working-tree states, and neither one's tag has any
relationship to the other's. This must be fixed as the foundation of any
promotion pipeline: **you cannot "promote" a build you didn't build once.**

Proposed scheme:

1. Build **one multi-arch image** per commit, tagged by commit SHA:
   `<registry>/munger:<git-sha>`. Multi-arch (`linux/amd64` + `linux/arm64`
   via `docker buildx build --platform linux/amd64,linux/arm64`) so the same
   manifest list serves both the Pi cluster (arm64) and Cloud Run (amd64 —
   Cloud Run does not run arm64 as of this writing; confirm before finalizing).
2. Push that one image to **one registry both targets pull from** — Artifact
   Registry (already exists: `us-central1-docker.pkg.dev/munger-503515/munger`
   per the Cloud Run side), reachable from both GCP and the Pi cluster over
   the internet.
3. **k3s side must change from side-loading to pulling from Artifact
   Registry.** This is the biggest structural change this design implies:
   `deploy/build-and-deploy.sh`'s `docker save | ssh ... k3s ctr images
   import` step goes away; the CronJob/Deployment manifests switch
   `imagePullPolicy: Never` → `IfNotPresent`/`Always` and `image: munger:latest`
   → `image: us-central1-docker.pkg.dev/munger-503515/munger:<sha>`, and the
   Pis need `imagePullSecrets` (or a public/read-only-authenticated repo) to
   auth to Artifact Registry. This trades away the current "no registry
   needed, no internet dependency for deploys" property of the k3s side —
   worth flagging explicitly as a real tradeoff, not a free change (see Open
   Questions).
4. Cloud Run promotion (§1.4) then becomes: **point the existing Cloud Run
   Job/Service at the exact same `<sha>` digest** already sitting in Artifact
   Registry — no second build. This directly answers "build once, deploy to
   both, or build twice from the same commit SHA": **build once**. Building
   twice (separately for arm64 and amd64, from the same source but through
   two toolchains) is exactly the setup that already let the two targets
   drift this session (Cloud Run ran stale pre-M15 code for a day because
   nobody rebuilt it) and gives no correctness guarantee that both images
   actually contain the same code.

### 1.3 The 24h dev soak — an automatable, concrete gate

"Run it for 24 hours" needs an operational definition, not a wall-clock
timer alone. Proposed concrete signal, built from artifacts that already
exist:

- **CronJob execution outcome**, not just "the pod started." k3s's
  `munger-daily-screen` CronJob runs once daily at 13:00 UTC; a single 24h
  window realistically captures **at most one** scheduled execution.
  `daily_screen.py` already exits non-zero on a degraded run (fetch quality
  below `MIN_UNIVERSE_FETCH_FRACTION = 0.90`) or a crash — `kubectl get jobs`
  / the Job's `status.succeeded`/`status.failed` field is a real pass/fail
  signal, already load-bearing, not something new to build.
- **Report reachability + content sanity**, not just HTTP 200. `curl -s
  http://<pi-ip>:30080/` returning 200 would *not* have caught either of
  this session's two real bugs — both rendered a fully valid HTML page,
  just with wrong content. The gate needs to assert on actual values, e.g.:
  - `tickers.html` — no row's `fail_reasons` column renders the literal
    string `nan` (would have caught `6412d72`).
  - `index.html`/the underlying `screen_results.csv` — score is not `0` (or
    not-fetch-failed) for some minimum fraction of buyable/non-buyable rows,
    not just for the handful that passed every gate (would have caught
    `8ed812b`'s ~99%-zero-score regression).
  - `feed.json`/`rss.xml` parse as valid JSON/XML (this session already had
    an invalid-XML bug, `&mdash;` in bare XML, caught by review before push
    but the same *class* of bug that unit tests alone have already slipped
    through once).
  These are exactly the kind of assertions this repo already writes as
  pytest tests against `report.py`'s output — the soak gate's real job is
  running a **subset of those same assertions against the live rendered
  output on the dev cluster**, not reinventing new checks. Concretely: a
  small script (or a pytest module tagged e.g. `@pytest.mark.smoke`) that
  fetches `http://<pi-ip>:30080/{index,tickers,feed.json,rss.xml}` and
  reruns a handful of the existing content-sanity assertions against the
  live bytes, not against an in-process `generate_report()` call.
- **`journal.db` / archive freshness**: confirm the CronJob actually wrote a
  new row into `screen_results_archive/` for the soak window's date, not
  just that the pod exited 0 — a job can exit 0 while having skipped
  archiving (see `daily_screen.py`'s own degraded-run branch, which is a
  *correct*, intentional 0-archive-but-still-exit-1 path, but a promotion
  gate should treat "archived" as the real signal, not "exited 0" alone,
  since a false-positive succeeded-but-skipped state is exactly the kind of
  thing this session's bugs teach us to distrust).
- **No new error-log signal since the image was deployed** — grep
  `munger.log` (or `kubectl logs`) on the Pi for `logger.exception`/ERROR
  lines timestamped after the new image's rollout.

Proposed gate (all must hold before promotion is even offered):
1. At least one CronJob execution completed with `status.succeeded == 1`
   since the new image was deployed to k3s.
2. That execution's archive write is confirmed present (freshness check on
   `screen_results_archive/`).
3. Live-fetched `index.html`/`tickers.html`/`feed.json`/`rss.xml` from the
   k3s NodePort pass the smoke-assertions above.
4. No new ERROR/exception lines in `munger.log` since deploy.
5. Wall-clock ≥ 24h since the image was deployed to k3s (still needed as a
   floor — a single successful execution 5 minutes after deploy is not the
   same evidence as one that's been serving traffic/re-running for a full
   day; this is also the "give the humans a day to notice something's
   wrong" property of a soak, which a single automated pass-fail check does
   not substitute for).

This is deliberately a **partial** answer to "what would have caught this
session's bugs" — see §2 for why some of it (fail_reasons rendering) is
straightforward to automate as a content assertion, but the `score=0`
mislabeling was actually reported by the *user looking at the live site*,
not by any tool. An automated gate built purely from "does the output look
structurally sane" would likely have caught the `nan` bug and might have
caught the score-distribution bug (99% zero is a detectable statistical
anomaly: assert e.g. "fraction of non-fetch-failed rows with score exactly
0.0 is below some threshold like 10%") but would *not* reliably catch a
subtler mislabeling like the `dividend_yield` one, which required a human
noticing the screen result didn't match domain expectations (Graham's
criteria). This is a real limit of automation here, not a gap in this
design — flagged explicitly rather than glossed over.

### 1.4 Promotion step to Cloud Run

Given §1.2 (one image, one registry), promotion is: repoint each Cloud Run
resource at the soaked SHA's digest, not a new build:

```
DIGEST=$(gcloud artifacts docker images describe \
  us-central1-docker.pkg.dev/munger-503515/munger:<sha> \
  --format='get(image_summary.digest)')

gcloud run jobs update daily-screen \
  --image="us-central1-docker.pkg.dev/munger-503515/munger@${DIGEST}" \
  --region=us-central1

gcloud run deploy report-web \
  --image="us-central1-docker.pkg.dev/munger-503515/munger@${DIGEST}" \
  --region=us-central1
```
(Exact image split for `report-web` needs reconciling — today it's a
*separate* Dockerfile, `deploy/cloudrun/report-web/Dockerfile`, an
nginx-only image with no application code baked in, deliberately never
needing a rebuild for report-content-only changes per the existing comment
in that file. If `report.py`/template logic changes are part of the change
being promoted, `report-web`'s image needs its own promoted build too — this
design's "one image" claim in §1.2 is really "one *app* image," and
`report-web`'s nginx wrapper is a second, much-lower-churn artifact that
still needs its own place in the pipeline, just gated less strictly since it
carries no business logic.)

Then run **one verification execution** of the `daily-screen` Job
immediately after the digest update (same as the manual pattern used this
session) before declaring promotion complete, and re-run the same
smoke-assertions from §1.3 against the *Cloud Run* URLs
(`gramunger.com`/`gramunger.com/tickers.html`/etc.) rather than assuming
"it worked on k3s so it'll work on Cloud Run" — the two runtimes have already
diverged once this session (GCS FUSE rejects `chmod`; the k3s PVC didn't
care) and could again.

---

## 2. Safety gates before touching prod — and the automation/approval tension

Tests passing (pytest + ruff/mypy) is already proven insufficient on its
own — see §0. The soak gate in §1.3 adds real signal (content assertions
against live output, not just exit codes) but is explicitly incomplete
against the class of bug a human domain-review catches (the
`dividend_yield` mislabeling was corrected because Graham's own criteria are
domain knowledge a smoke-test doesn't encode).

This surfaces a genuine tension the user asked to be named rather than
silently resolved:

- **A human-approval gate before Cloud Run promotion** (e.g. a GitHub
  Actions `environment: production` with required reviewers, or a manual
  `workflow_dispatch`-only promotion job) is the most direct way to add the
  kind of judgment that caught `8ed812b` and `6412d72`'s user-facing symptom
  in the first place — both were found by someone actually *looking* at
  `gramunger.com`, not by a tool. This is very plausibly the right call
  given a bad promote goes to a real public domain with real visitors.
- But a human-approval gate means the pipeline is **not** "fully automated
  end-to-end" as literally requested in TASKS.md's "Fully automated CI/CD
  for the dev→prod pipeline" row — it becomes "automated up to a checkpoint,
  human-gated after." That may be exactly the right tradeoff, but it is a
  different thing than what was asked for, and this document is not
  choosing silently between them.

Proposed middle ground, stated explicitly as a design option rather than a
decision: automate everything through §1.3's soak gate passing, then require
one human `Approve` click (a GitHub Actions manual-approval environment gate
is the natural mechanism, reusing this repo's existing GitHub Actions
patterns) before the promotion job in §1.4 runs. This keeps 95% of the toil
(building, tagging, deploying to dev, running the soak checks, preparing the
exact promotion command) automated, while keeping the one step that this
session's evidence says actually needs a human — looking at the result and
judging whether it's *right*, not just whether it *ran* — a human action.
Whether that satisfies "fully automated" is a call for the user, not this
document (see Open Questions).

Additional gates worth adding regardless of the approval question, since
they're cheap and directly targeted at this session's actual failure modes:
- A content-assertion smoke-test suite (§1.3) run against the **rendered
  live output**, not just unit tests against `generate_report()` in
  isolation — this is the single most direct answer to "what would have
  caught this session's bugs," since both bugs were rendering/labeling bugs
  that only became visible in the actual generated HTML/CSV, not in any
  function's return value in isolation.
- A statistical sanity check on score/buyable distribution (e.g. "fraction
  of rows with score exactly 0.0" or "fraction of rows tagged
  data_missing:<field> for any single field" should fall within some
  historical band) — would have flagged `8ed812b`'s ~99%-zero-score anomaly
  and might catch a similar future regression before a human notices.
- Diffing the new build's rendered output against the previous day's, and
  surfacing (in the approval-gate PR/notification, not blocking on it) any
  metric whose aggregate shape changed sharply — cheap to compute, doesn't
  need a hard threshold to be useful as a human's approval-time context.

---

## 3. Rollback

Because Cloud Run Jobs/Services pin to a **digest**, not a mutable tag,
rollback is mechanically simple and should be scripted regardless of
anything else in this design:

```
gcloud run jobs update daily-screen \
  --image="us-central1-docker.pkg.dev/munger-503515/munger@<previous-good-digest>" \
  --region=us-central1
gcloud run deploy report-web \
  --image="us-central1-docker.pkg.dev/munger-503515/munger@<previous-good-digest>" \
  --region=us-central1
```

This requires **recording the previous digest before each promotion** —
today nothing does this; the promotion script should write the outgoing
digest to a small durable log (a file in the GCS bucket already mounted, or
a git-tracked `deploy/cloudrun/PROMOTED.log` committed alongside the
promotion) before repointing, so "rollback to previous" is a one-command,
no-lookup operation rather than requiring someone to dig through
`gcloud artifacts docker images list` / Cloud Build history under pressure.
`report-web` being a live-serving Service (not a Job) means a bad promote
there is user-visible immediately — its rollback path is the more
time-sensitive of the two and should be the one drilled/scripted first.

Nothing about k3s dev needs a rollback story with the same urgency — it has
no public audience and the same `build-and-deploy.sh apply` mechanism can
simply be re-run against an older SHA's manifests/image if needed.

---

## 4. Where this should live

The repo already runs GitHub Actions for the trading-bot side
(`.github/workflows/{daily-screen,heartbeat,daily-trade}.yml`) but has
**no CI workflow at all for the screener/report/deploy side** — no
`.github/workflows/ci.yml` runs pytest on PRs today; the only place pytest
currently runs against this code is inside the k3s CI Job
(`deploy/k8s/ci-test-job.yaml`), which only happens at deploy time, on
whichever machine happens to run `build-and-deploy.sh`. That's itself a gap
worth closing independent of this design (tests should run on every PR, not
just at deploy time).

Given the two very different deploy mechanisms (SSH+`k3s ctr images import`
into a home LAN cluster vs. `gcloud`/Artifact Registry), proposed split:

- **GitHub Actions** as the orchestrator/trigger layer and the home for the
  parts that don't need LAN access: building the multi-arch image, pushing
  to Artifact Registry, running the soak-gate's *readable* checks (anything
  reachable over the public internet — Cloud Run's own URLs; the k3s
  NodePort is **not** publicly reachable, see below), the manual-approval
  gate (GitHub's `environment` protection rules are a natural fit and need
  no new tooling), and the promotion step itself (`gcloud run jobs/deploy
  update`, from a GCP service-account key stored as a repo secret — this
  already has to exist for Cloud Run access from CI regardless of this
  design).
- **A self-hosted runner or an SSH-driven script step, not plain GitHub
  Actions**, for anything that touches the k3s cluster directly — GitHub's
  hosted runners cannot reach `pi1`/`pi4` on the home LAN without a tunnel
  (e.g. a self-hosted Actions runner living on the LAN, or Tailscale/similar
  connecting the GitHub-hosted runner to the Pi network). This is the same
  SSH-based approach `build-and-deploy.sh` already uses locally, just
  invoked from CI instead of a dev machine's shell — either keep
  `build-and-deploy.sh` as the actual mechanism and have a self-hosted
  runner execute it, or port its logic into workflow steps. Given the
  script already encodes real hard-won knowledge (the `kubectl apply`
  per-file gotcha comment, the CI-job wait/log pattern), **wrapping it
  rather than rewriting it** is the lower-risk path.
- The soak-gate check against the k3s report specifically needs to run
  *from somewhere that can reach the LAN* — either the same self-hosted
  runner, or a small in-cluster CronJob/Job (mirroring `ci-test-job.yaml`'s
  existing pattern) that writes its pass/fail result somewhere the
  GitHub-hosted side can poll (e.g. a status file on the shared PVC, read
  back over SSH; or simplest, the self-hosted runner just does the whole
  soak check itself on a timer/second workflow run 24h after deploy).

Net proposal: **one GitHub Actions workflow, split into jobs**, with the
k3s-touching jobs pinned to a self-hosted runner (`runs-on: [self-hosted,
pi-lan]` or similar) and the Cloud Run–touching jobs on GitHub-hosted
runners, connected by artifacts/outputs (the git SHA / digest) passed
between jobs — not two separate systems. This keeps a single audit trail
(one Actions run per promotion) instead of a GitHub Actions run for one half
and a manually-invoked script for the other, which is closer to today's
"nobody can tell if TASKS.md's `done` reflects what's actually deployed"
problem (explicitly named in TASKS.md's M15 "Cloud Run image is stale" row)
that this whole design exists to prevent from recurring.

---

## 5. Open questions for the user

These are decisions this document deliberately does not make:

1. **Does a human-approval gate before Cloud Run promotion satisfy "fully
   automated CI/CD"?** §2 names this tension explicitly. If the answer is
   "no, it must be zero-touch," the soak gate in §1.3 needs to be trusted as
   the *sole* safety net, and its known blind spot (domain-knowledge bugs
   like the `dividend_yield` mislabeling, which a human caught by inspection
   and no automated check in this design would have caught) becomes an
   accepted risk, not a solved problem.
2. **Is 24 hours the right soak duration, and is "one CronJob execution"
   enough evidence?** The daily cadence means a 24h window captures at most
   one scheduled run. A shorter interim CronJob schedule during soak (e.g.
   hourly for the soak window only, reverting to daily after promotion)
   would give more data points per soak but adds real complexity — worth an
   explicit decision rather than defaulting to whichever is easier to build.
3. **Is trading the k3s side's "no registry, no internet dependency" deploy
   property for a shared Artifact Registry image acceptable?** §1.2 flags
   this as a real tradeoff of the "one shared image" requirement. An
   alternative that preserves the current side-load mechanism (build twice,
   promote by re-running the same source through both toolchains and trusting
   they produce equivalent output) avoids this tradeoff but reintroduces the
   "two independent builds can silently diverge" risk this design is trying
   to remove — the user should pick which risk is more acceptable.
4. **Does a self-hosted GitHub Actions runner get installed on the Pi LAN
   (or a tunnel like Tailscale set up), and who maintains it?** §4's
   proposal depends on this; it's new operational surface (a persistent
   process on the Pi cluster, or another service to keep patched) that
   isn't free, distinct from the workflow-design question itself.
5. **What is the acceptable false-positive/false-negative rate for the
   statistical sanity checks in §2** (score-distribution, fail-reason-code
   distribution)? These need a "normal" baseline to compare against, and
   the current archive history (a handful of days) may be too short to set
   a confident threshold yet — worth deciding whether to ship the pipeline
   without this check initially and add it once more history accumulates,
   vs. blocking the pipeline on having it from day one.
6. **Who gets paged/notified on a soak-gate failure, a promotion-gate
   rejection, or a rollback?** Today failures surface only as a red X in
   whichever CI system ran (GitHub Actions email, or nothing at all for the
   hand-run k3s script) — no alerting channel (Slack, PagerDuty, even just a
   personal email rule) is defined anywhere in this repo. Worth deciding
   before this pipeline goes live, since a failed promotion of a public site
   is exactly the kind of failure that shouldn't be discovered by a user.

**2026-08-07 addendum — a concrete new fact for whoever resolves Q3/Q4:**
a self-hosted GitHub Actions runner already exists (`actions-runner/munger-ci-runner`,
a bare pod pinned to `pi4`, `myoung34/github-runner:latest`, default
in-cluster ServiceAccount with no RBAC bindings) — so Q4 ("does a runner get
installed, who maintains it") is already partially answered: yes, one
exists, unmaintained/unmonitored beyond existing. More importantly, a
concrete gap Q3 didn't anticipate: **no node in the k3s cluster has a Docker
daemon at all**, and the runner pod has no Docker socket mounted (`docker
info` fails inside it — confirmed live). Every build to date, including
this pipeline's own manual runs, has happened on a dev machine, not
in-cluster. This means Q3's "one shared image" tradeoff isn't the only
open fork — there's a prior question: **how does an in-cluster CI runner
build an image at all**, given neither a host Docker socket (simplest, but
grants anything that compromises the runner pod root-equivalent control of
`pi4` — meaningful given the pod also used to trigger on `pull_request`)
nor installing Docker on a Pi (new persistent service/patch surface on home
infra) is free. A daemonless in-cluster builder (kaniko, run as a
Kubernetes Job) avoids both but needs its own new machinery: RBAC to launch
the Job, and — for the side-load-into-containerd step this repo's k3s side
depends on (§1.2 already flags the alternative, pulling from a registry
instead) — a hostPath mount of the target node's containerd socket into
whichever Job does the import, which is itself node-scoped privileged
access, just narrower than a full Docker socket. **2026-08-07 interim
decision** (see `TASKS.md` row 509): none of the above was granted yet;
`.github/workflows/ci.yml` runs only lint/typecheck/test on push to `main`,
no build/push/deploy, until this question gets an actual answer.
