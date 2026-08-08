# Design: Durable P&L history + embedded Grafana dashboards (M17)

**Status: DRAFT. Architecture settled with the user 2026-07-28; both review
gates (staff-engineer + pm) have run and their findings are folded in below.
Not TASKS.md/DESIGN.md content yet — build not started.**

_Supersedes the earlier `DESIGN_PROMETHEUS.md` draft (removed). Origin: user
request "profit and loss should be exported for collection by prometheus...
grafana in the app as a secondary page... separate tabs," refined to two
concrete graphs — (1) account P&L over time for the whole trading account,
(2) daily close price per owned ticker._

---

## 0. The Prometheus decision (evaluated, dropped)

The request named Prometheus, but through design + review it was dropped, and
this is recorded here the way M1 records declining the FMP API — a deliberate,
reasoned "no," not an oversight:

- The two requested graphs are just values plotted over time; no rate/aggregate
  math needs a metrics query engine.
- The user declined alerting — Prometheus's other real job.
- The genuine need underneath is a **durable, long-term, growing time-series
  store** for the account P&L. Prometheus is a *poor* fit for that specific
  role here: its compaction is irrelevant at ~one sample/day for ~dozens of
  series (kilobytes/year), and its local storage is explicitly *not* meant for
  durable long-term data — on a single unreplicated Pi it is **less** durable
  than the GCS bucket we already have (the exact pi-local-loss risk TASKS row
  445 flags), and it cannot run free on prod (Cloud Run scale-to-zero; §2.4).

**What replaces it:** a durable append-only series in GCS (§2.1) as the system
of record, read by Grafana's JSON/Infinity datasource. Durable (replicated),
tiny, grows forever, backfillable once from Alpaca history, and works on *both*
dev and prod with no always-on service. That is the "long-term TSDB" the user
wanted, realized as a store that's actually durable and free on prod.

---

## 1. Inherited constraints (non-negotiable)

1. **Alpaca-credential boundary.** Alpaca keys live only in GitHub Actions
   (`daily-trade.yml`); the report deployment (Cloud Run / k3s) never has them
   (M14 screen-only boundary; `pnl.py` docstring). Every component here reads an
   already-produced artifact — nothing new calls Alpaca outside GitHub Actions.
2. **The GCS bucket is the cross-system bridge.** `pnl.py` runs in GitHub
   Actions and writes to `gs://munger-503515-data`; Cloud Run FUSE-mounts it.
   The **k3s PVC does not** have `pnl.json` today (why M16 row 552 defers k3s
   P&L) — §2.3 fixes this in passing.
3. **Static-site + generated-artifact model.** `report.py` emits static HTML +
   nginx serves it; a "tab" = generated HTML + a nav link gated on a config
   constant, exactly like `REPORT_BASE_URL` gates feed links today.

---

## 2. Architecture

### 2.1 System of record: a durable P&L append-series in GCS

A new artifact `pnl_history.jsonl` in the bucket, one JSON object per day:

```json
{"date":"2026-07-28","equity":99893.02,"cash":54998.56,"last_equity":100000.0,"profit_loss":-106.96,"mode":"paper"}
```

- **Maintained in GitHub Actions**, alongside the existing `pnl.json` write.
  Because GCS objects aren't appended in place, the daily step is
  **read-modify-write**: download the current series, **upsert today's row keyed
  by `date`** (idempotent — re-running a day overwrites, never duplicates),
  write back atomically (temp + rename, the codebase's established pattern).
  The file is tiny; whole-file rewrite is a non-issue.
- **Seeded once from Alpaca history**: on first run (empty/short series),
  backfill from the `history` array `pnl.py` already fetches. This is the piece
  Prometheus *couldn't* do (forward-only) — the curve has real past from day one
  and then accretes durably forward, no longer capped at `pnl.py`'s rolling
  `period="1M"` window (staff-eng finding 9.B-2, now resolved by design).
- **`pnl.py` stays testable/local**: it operates on a local `pnl_history.jsonl`
  (upsert + seed logic, unit-tested with fixtures); the workflow does the GCS
  download-before / upload-after, mirroring how it already handles `pnl.json`.

`pnl.json` (today's snapshot) is unchanged and still feeds `pnl.html`.

### 2.2 Grafana reads JSON directly (no Prometheus, no exporter)

One Grafana, one datasource type — the **Infinity/JSON datasource** reading:
- `pnl_history.jsonl` → **Graph 1: account P&L / equity over time** (whole
  account), backfilled + durable.
- `prices.json` (Phase 2) → **Graph 2: daily close per owned ticker.**

No `metrics_exporter`, no `prometheus_client` dependency, no Prometheus
Deployment/TSDB. This is the bulk of what the review's scope finding (both
gates) argued to cut.

### 2.3 Dev (k3s): read-only GCS→PVC bridge + Grafana

- **Bridge:** a **standalone** read-only CronJob (not an init step on
  `daily-screen`, which runs 13:00 UTC and would pull *yesterday's* 14:00
  snapshot; and `daily-screen`'s manifest is deliberately secret-free — keep it
  so). Scheduled **after 14:00 UTC**, it `gcloud storage cp`s `pnl.json`,
  `pnl_history.jsonl` (+ Phase 2 `prices.json`) from GCS into the PVC,
  **atomically** (temp path + rename, so a reader never sees a torn file). Uses
  a new read-only SA `munger-gcs-reader` (key as a k8s Secret).
  - Blast-radius note (staff-eng 9.B-7): per M16 row 548, `gcloud storage cp`
    needs bucket-level `list` — so this key is bucket-wide *read-only*, broader
    than "two files," but it is **not** Alpaca creds, so the trading boundary
    holds. It's a new read-only secret in the cluster; documented, accepted.
  - **Bonus:** this also lands `pnl.json` on the k3s PVC, closing M16 row 552
    (k3s `pnl.html` currently renders an empty P&L page).
- **Grafana:** a Deployment, **pinned off pi1** (staff-eng 9.B-6 — pi1 holds the
  PVC and is already memory-tight per `30-daily-screen-cronjob.yaml`'s own
  comment; Grafana doesn't need the PVC-host node — pin to pi2/pi3 with explicit
  memory limits), **provisioned-stateless** (dashboards + datasource as
  ConfigMap YAML/JSON), reading the bridged files via Infinity. Anonymous access
  **locked down** (9.B-8): viewer-only org, Explore disabled, only the intended
  dashboards shipped.
  - **No longer exclusively munger's (found live, 2026-08-08, see TASKS.md
    row 591):** a separate project (home-network Unifi/node-exporter
    monitoring) added its own dashboards directly onto this same k3s
    Grafana instance, outside this repo's IaC. `deploy/k8s/50-grafana.yaml`
    now *references* those foreign ConfigMaps (volume/volumeMount only, not
    their content) purely so a full `kubectl apply` of this file cannot
    silently delete them — see that file's own header comment for the
    reconciliation discipline this requires going forward (diff against
    live state before applying, don't assume this file alone is the whole
    truth). No namespace/instance split has been done yet; if one happens,
    update this section.

### 2.4 Prod (`gramunger.com`): e2-micro VM, authenticated pull, no public data

- **Host:** a free-tier GCE `e2-micro` VM running **only Grafana** (behind
  Caddy) plus a small internal-only json-server and a GCS-pull timer,
  provisioned-stateless. **As-shipped (2026-08-07, see TASKS.md row 591 —
  update this doc, not that row, if this changes again): `us-west1-b`, not
  `us-central1`** as originally planned here (`ZONE_RESOURCE_POOL_EXHAUSTED`
  on every `us-central1` zone at build time; `us-west1` is still
  free-tier-eligible, GCP's Always Free e2-micro allowance covers exactly
  `us-west1`/`us-central1`/`us-east1`). **Hostname is
  `34-82-149-71.sslip.io`, not `grafana.gramunger.com`**: new subdomains on
  the `gramunger.com` Cloudflare zone don't publish at all (API/dashboard
  accepts the record, authoritative NS returns NXDOMAIN — the same
  account/edge-level defect that already blocked `stats.`/`analytics.` in
  M18; unresolved, see TASKS.md's dedicated tracking row for it). `sslip.io`
  resolves to the VM's static IP with zero DNS config and still gets a real
  Let's Encrypt cert via HTTP-01, sidestepping the zone entirely. Cross-origin
  iframe on `gramunger.com`, so Grafana's `allow_embedding` + Caddy's
  `frame-ancestors` header must permit `https://gramunger.com` (confirmed live
  on the wire, not just in config).
- **Data access is authenticated, nothing public** (staff-eng security finding):
  the VM pulls `pnl_history.jsonl`/`prices.json` from GCS using its **instance
  service account** (read-only, via GCE metadata — no key file) into local disk;
  Grafana reads local files. The naive "public JSON URL" alternative was wrong —
  `pnl.json` is outside the nginx web root today, and a public bucket would leak
  `state.json`/`journal.db`/archives.
- **Cost confirmed (TASKS.md row 510, 2026-07-30):** the account-wide
  one-e2-micro-free-tier allowance was unused before this VM, and it's the
  only Compute Engine instance on the account — the ~1GB/mo egress cap is a
  low-traffic personal dashboard, immaterial in practice (see row 510's
  `report-web` comparison, ~134MB/mo for a comparably-sized workload).

---

## 3. Data details

- **`pnl_history.jsonl`** (§2.1) — account-level daily series; the durable
  record for Graph 1.
- **`prices.json`** (Phase 2) — daily close bars for **currently-held** symbols
  (`pnl.json` `positions[]`), fetched via Alpaca `StockHistoricalDataClient`.
  - **Confirm the account's data tier returns daily historical bars for held
    symbols before committing** (pm 9.B-8 / staff — `execution.py` uses this
    client for *latest-trade* pricing, not historical bars; free-tier feeds have
    lookback limits).
  - **Needs a validation/sanity layer** mirroring `data.validate_metrics`:
    reject zero/outlier/missing closes; treat "no bars for symbol" as a distinct
    handled case, never a silent bad point on a public chart (staff 9.B-3).
  - **Moving-window semantic, named** (staff 9.B-4): "owned" = *currently*
    owned. A sold ticker's series drops off the dashboard; a new buy appears.
    Acceptable per the user's "ticker that I own" framing, but stated, not
    silent. (Moving averages: dropped from the ask; trivial Grafana overlay if
    ever wanted.)

---

## 4. Site tabs

- **Calendar → Dashboards swap, config-gated** on `config.GRAFANA_BASE_URL`
  (env `MUNGER_GRAFANA_URL`, default `""`). Where set: nav shows **Dashboards**,
  omits **Calendar**, and `calendar.html` need not be generated. Where unset:
  nav keeps Calendar (so no environment is empty-handed mid-rollout). End state
  once both envs have Grafana: calendar retired everywhere.
  - **Implementation reality** (staff 9.B-10): the `calendar.html` link is
    hardcoded in **four** nav blocks (`_render_index`, `_render_tickers`,
    `_render_pnl`, `_render_calendar`) — all four must be conditionally
    rewritten, or pages ship dead links. This is a multi-site edit, not a
    one-file add.
- **`pnl.html` stays** — current-snapshot view; complements the over-time
  dashboard; and it's the only P&L surface that works on prod without Grafana.
- **Feed deleted** (user: "delete the feed") — `feed.json`/`rss.xml` retire,
  `<head>` `<link rel="alternate">` tags removed, `config.FEED_MAX_ITEMS` +
  feed helpers/tests removed. **Reverses M15 slice 3**; tracked as its own
  destructive item (pm 9.B-17), archive data untouched.
- **Embed = iframe** (user choice). **Graceful degradation** (staff 9.B-9): the
  prod VM is a single point of failure and the calendar it replaces is gone —
  if the VM is down, the tab must degrade to a plain link / "temporarily
  unavailable" state, never a broken empty iframe. A genuine *inability* to
  embed (CSP/proxy) goes back to the user — the link is only the degraded
  runtime state, not a silent build-time substitution. Ship a rebuild runbook
  for the hand-built VM/TLS/DNS.

---

## 5. Freshness (no alerts)

- No alerting (user q4). So the freshness indicator is the *only* defense
  against a confidently-wrong chart — make it **loud** (banner semantics like
  `pnl.html`'s `_pnl_staleness_note`), not a small tile (staff 9.B-11).
- Staleness = age of the latest `pnl_history.jsonl` row / `pnl.json`
  `generated_at`, judged against the single existing
  `config.PNL_STALENESS_MAX_HOURS` (48h) on *both* `pnl.html` and Grafana, so
  the two surfaces can't disagree.
- A failed bridge CronJob only accumulates in k8s job history (no email/page,
  unlike `daily-trade.yml`'s GH-failure-email). Accepted given "no alerts," but
  recorded as a known risk.

---

## 6. Phasing

- **Phase 1 (dev, from existing data):** the durable `pnl_history.jsonl`
  append-series (`pnl.py` upsert + seed + tests; workflow GCS wiring), the
  read-only GCS→PVC bridge (closes M16 row 552), dev Grafana (pinned off pi1,
  provisioned-stateless, locked-down anonymous), the **account-P&L-over-time**
  dashboard + Dashboards tab, the calendar→dashboards nav swap, and the feed
  deletion. **Prerequisite:** verify the P&L numbers against Alpaca's dashboard
  (M16 row 551 — currently `todo`); Graph 1 must not be built on unverified
  data (pm 9.B-13). Note the paper account is young, so the curve may be short
  initially even after the history-seed.
- **Phase 2 (dev):** `prices.py` (held-symbol daily close + validation),
  `prices.json`, the Prices dashboard/tab. Gated on confirming Alpaca's data
  tier returns the bars (§3).
- **Phase 3 (prod):** its own milestone (pm 9.B-15 — prod was the least-planned,
  highest-risk part): the e2-micro VM, authenticated GCS pull, cross-origin
  embed, graceful degradation, **and the row-510 cost confirmation** — before
  flipping `MUNGER_GRAFANA_URL` on prod (which is also what retires the prod
  calendar).

---

## 7. Non-goals (explicit)

No alerting/paging. No Prometheus (§0). No GitHub-Actions→LAN push (the bridge
is a cluster/VM-initiated *pull*). No writes from the report deployment to the
bucket (read-only everywhere outside GitHub Actions). No moving averages
(deferred). No touching `screen_results_archive/` data (only the calendar/feed
*views* over it are removed). No exposing any bucket object publicly.

---

## 8. Testing + the user-confirmation gate

- **Unit tests** (pytest, like the rest of the repo): `pnl.py`'s append-series
  upsert (idempotent re-run, seed-from-history, atomic write); `report.py` nav
  (tab shown/hidden by `GRAFANA_BASE_URL`, no dead calendar links in any of the
  four nav blocks, feed artifacts gone); Phase-2 `prices.py` validation
  (rejects zero/outlier/missing, handles "no bars").
- **Manifest/provision sanity**: k8s manifests + Grafana provisioning apply
  cleanly (extend `deploy/build-and-deploy.sh`).
- **User-confirmation gate (not agent self-certification)** — M13 row 354
  precedent, reaffirmed M15/M16: this is a user-facing display feature, so
  "done" requires **the user** looking at the live dashboards and confirming
  the two graphs show the right data. An agent smoke-test does not satisfy this;
  it is a distinct, un-self-certifiable milestone row.

---

## 9. Prerequisites / dependencies

- **M16 row 551** (verify P&L numbers vs Alpaca dashboard) — prerequisite for
  Phase 1 Graph 1.
- **TASKS row 510** (GCP cost/billing) — must confirm the e2-micro + egress
  stays free before Phase 3.
- **Alpaca data-tier check** — historical daily bars for held symbols, before
  Phase 2.
