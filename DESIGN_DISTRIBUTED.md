# Munger v2 — Distributed Screening Architecture

**Status:** Reviewed by staff-engineer-reviewer + pm-reviewer (2026-07-24) —
see TASKS.md's "v2" section for what each pass found and fixed. Not yet
implemented (no code exists for D1+); each D-milestone still gets its own
implementation-time review as it's built. Extends [DESIGN.md](DESIGN.md)
(v1, the modular monolith). Nothing here changes the *investment* logic —
Graham gates, Munger score, two-strike sell discipline, and centralized,
safety-gated execution are all unchanged (DESIGN.md §1, §3.3–3.5). This
document changes only *how the screen is computed and where state lives*,
to fit a 4-node Raspberry Pi k3s cluster.

---

## 1. Motivation

### 1.1 What actually hurts today
The single expensive, failure-prone stage is **Data (DESIGN.md §3.2)**:
fetching fundamentals for the ~1500-ticker S&P Composite 1500 from
yfinance. It is:

- **Rate-limited at the source, per IP/session** — the README and
  TASKS.md (M2/M13) record that yfinance's limit is session-wide, not
  per-worker, so adding threads on one machine does not help past a point,
  and a live run has already **aborted at the universe-fetch-fraction
  safety check** because too few tickers came back under heavy throttling
  (README M12).
- **Memory-heavy** — a 1500-row pandas screen is the workload most likely
  to OOM a 900 MB node.
- **Redundant across runs** — the daily screen (M14) re-fetches the entire
  universe every day, even though fundamentals only change when a new
  10-Q/10-K lands (quarterly). The daily run throws away yesterday's work.

Nothing else in the system is a bottleneck: universe scrape, Graham gates,
Munger scoring, portfolio construction, journaling, and report generation
are all cheap and either trivially parallel or trivially small.

### 1.2 Two independent levers
This design pulls two levers, in priority order:

1. **A shared, cross-run fundamentals cache (the big win).** Because
   fundamentals are quarterly, a cache with a multi-day TTL turns each
   daily screen into a mostly-cache-hit *incremental* job. Cache-miss
   count on an ordinary day drops from ~1500 to near zero (only names with
   stale/expired entries or a fresh filing). This attacks the rate limit
   at its root: the cheapest fetch is the one you never make.

2. **Sharded, one-worker-per-node fetch of the cache-miss remainder (the
   parallel win).** Each Pi has its own egress IP, hence its own
   per-IP rate-limit budget. Distributing the residual fetches across 4
   nodes gives ~4× independent throughput on exactly the work the cache
   couldn't eliminate.

Lever 1 does most of the work on a normal day; lever 2 covers cold starts,
quarterly cache turnover, and forced full refreshes. **Neither is
achievable on a single node**, which is what justifies distributing.

### 1.3 Non-goals
- **Not a microservices decomposition.** The modules stay in one codebase
  and one image. Splitting universe/screener/portfolio into always-on
  services would spend scarce RAM on runtimes and add network failure
  modes between what are today in-process calls, with no scaling driver.
  See `munger-workflow` skill §6.
- **Execution is never distributed.** Portfolio construction and Alpaca
  order placement remain a single, centralized, idempotent step with the
  only copy of the broker credentials. Distributing trading logic would
  multiply the ways money can move incorrectly.

---

## 2. Architecture overview

```
                    ┌──────────────── CronJob (daily / quarterly) ────────────────┐
                    │ creates a per-run Coordinator Job with a unique run_id       │
                    └───────────────────────────────┬─────────────────────────────┘
                                                     ▼
   ┌─────────────────────────────── COORDINATOR (one Job pod) ───────────────────────────────┐
   │  1. Acquire run-lock (Redis SETNX run:{date})       ── idempotency: one run per day      │
   │  2. Universe scrape → ticker list → snapshot to Redis (universe:{run_id}, versioned)     │
   │  3. Split cache-hit vs cache-miss using the fundamentals cache (fund:{ticker})           │
   │  4. Launch WORKER Indexed Job (completions=N, parallelism=N, pod anti-affinity)          │
   │  5. Collect partials (HTTP POST from workers) until all shards report or deadline        │
   │  6. REDUCE: assemble metrics → Graham gates → Munger score → rank                        │
   │  7. Portfolio engine → (quarterly) Execution via Alpaca  ── broker creds ONLY here       │
   │  8. Journal (SQLite, durable via Litestream) + archive + HTML report                     │
   │  9. Release run-lock                                                                     │
   └───────▲───────────────────────────────────────────────────────────────────┬────────────┘
           │ HTTP POST /partials/{run_id}/{shard}                                │ writes
           │                                                                     ▼
   ┌───────┴──────── WORKER Indexed Job (N pods, 1 per node — own IP each) ─┐   report/ + journal.db
   │ shard = JOB_COMPLETION_INDEX                                           │   (coordinator's PVC
   │ read universe:{run_id} from Redis → take this shard's slice           │    + Litestream → S3)
   │ for each ticker: cache-get fund:{ticker}; on miss → yfinance fetch,    │
   │   validate, cache-set with TTL                                         │        ┌──────────┐
   │ score locally → POST partial results back to coordinator              │        │  nginx   │ serves
   └───────────────────────────────────────────────────────────────────────┘        │ (report) │ report/
                                                                                     └──────────┘
                    ┌──────── REDIS (StatefulSet, 1 replica) ────────┐
                    │  fundamentals cache · run-lock · universe       │
                    │  snapshot · live progress · rate-limit circuit  │
                    └─────────────────────────────────────────────────┘
```

### 2.1 Components and why each exists
| Component | k8s object | Lifetime | Rationale |
|-----------|-----------|----------|-----------|
| **Coordinator** | Job (created by CronJob) | per run | Owns universe scrape, reduce, portfolio, execution, journal, report. Single point of trading authority; holds broker creds. |
| **Workers** | Indexed Job | per run (ephemeral) | Parallel, sharded fetch+validate+score across nodes/IPs. Consume no RAM between runs. |
| **Redis** | StatefulSet (1) | always-on | Cross-run fundamentals cache + coordination (lock, snapshot, progress, rate-limit signal). Small, arm64-native, ~64 MB. |
| **Report web** | Deployment + Service | always-on | Stock nginx serving the coordinator's `report/` (already built). |
| **Durable store** | Litestream sidecar → S3 | continuous | Streams `journal.db` + `state.json` off-node so a node loss can't destroy the audit trail. |

Between runs only Redis + nginx (+ their tiny footprints) are resident.
The heavy Python/pandas pods (coordinator, workers) exist only during a run
— this is what keeps a distributed design inside a 4×900 MB budget.

---

## 3. The message-passing contract (worker → coordinator)

Per the chosen coordination model: **Indexed Job + HTTP POST to the
coordinator, no shared filesystem.** Redis is the *coordination and cache*
substrate, not the result-transport path — results flow directly worker→
coordinator so the reduce step has a simple, synchronous "have all shards
reported?" signal.

- The coordinator pod is fronted by a headless-or-ClusterIP **Service**
  (`munger-coordinator`) so workers have a stable DNS name to POST to even
  though the coordinator is a Job pod.
- Worker → `POST /partials/{run_id}/{shard_index}` with a JSON body:
  `{ "shard": i, "run_id": …, "results": [ {symbol, metrics, valid,
  fail_code, graham_pass, munger_score}, … ], "fetch_stats": {hits,
  misses, failures} }`.
- The endpoint is **idempotent per (run_id, shard)** — a retried worker
  (Job backoff) overwrites, never appends. The coordinator considers the
  scatter complete when it holds a partial for every shard `0..N-1`, or
  when `SHARD_DEADLINE` elapses.
- **Auth/isolation:** cluster-internal only (NetworkPolicy restricting the
  coordinator's ingress to worker pods in the namespace); the body carries
  a per-run shared token the coordinator generates and passes to workers
  via the Job spec, rejecting POSTs with the wrong token.

### 3.1 Determinism & the universe snapshot
All workers must shard the **same** ticker list. The coordinator writes the
scraped, normalized, sorted list once to `universe:{run_id}` in Redis;
workers read that exact key. Shard `i` = `tickers[i::N]` (stride
partitioning, so a contiguous alphabetical block never lands entirely on
one worker — smooths any per-exchange fetch skew). N is fixed per run and
embedded in the Job.

---

## 4. The fundamentals cache

The highest-leverage piece. Keyed `fund:{ticker}`; value = the validated
metrics blob + `fetched_at` + the source filing period. TTL and
invalidation:

- **TTL:** default multi-day (e.g. `FUND_CACHE_TTL_HOURS`, default ~72 h)
  so a daily screen re-uses fundamentals but a stale entry can't persist
  indefinitely. All thresholds stay in `config.py` (DESIGN.md discipline).
- **Explicit refresh:** a run flag (`--refresh` / quarterly runs) bypasses
  the cache to force a full re-fetch, so the quarterly *trading* run always
  scores on fresh data — we never place trades on cached fundamentals older
  than the run's own freshness bound.
- **Negative caching:** a fetch that fails validation is cached briefly
  (short TTL) so 1500 workers don't all retry a known-bad ticker every run;
  distinct from a transient fetch error, which is not cached.
- **Warm-once, read-many:** the *first* daily run after a cold Redis (or a
  quarterly turnover) pays the full fetch cost, sharded across 4 IPs;
  subsequent daily runs are near-instant. This is the incremental-screen
  payoff.

Cache correctness note: fundamentals are point-in-time. The cache stores
"the latest fundamentals yfinance reported at `fetched_at`," which is
exactly what a live screen uses today — the cache does not introduce
look-ahead bias (that concern is about *backtesting*, still out of scope,
DESIGN.md §6/§9).

---

## 5. Reliability & failure modes

Distribution adds failure modes; the design's job is to make each one
**degrade safely**, and the good news is v1's safety controls already
compose correctly with it.

| Failure | Behavior | Safety net |
|---------|----------|-----------|
| A worker (node) dies mid-shard | Indexed Job retries that index (`backoffLimit`); if it exhausts, that shard's tickers are simply missing from the merge | The **universe-fetch-fraction abort** (DESIGN.md §5) already aborts *trading* if too few tickers were fetched — so a lost shard degrades to *screen-only, no trades*, never to bad trades. |
| Coordinator dies mid-run | The next CronJob-created coordinator can retry once the lock is free. No partial trades because execution is the last step and is itself idempotent (`client_order_id`, `has_already_submitted`, DESIGN.md §3.5). | Existing idempotency + the fenced run-lock below. |
| Run legitimately outlives a static lock TTL | **This is the trap** (staff-engineer-reviewer): a cold-cache full sharded fetch under throttling can run long. A plain TTL could expire *while the first coordinator is still working*, letting a second acquire the lock and both reach execution. So the lock must **not** be a fixed short TTL: the coordinator **renews (heartbeats) the lock while alive** and uses a **fencing token** the reduce/execute step checks, or the TTL is provably longer than max run duration. | Fencing token + broker `has_already_submitted` as the independent backstop. |
| Redis unavailable | **Not "slower but correct" — the run cannot proceed.** The universe snapshot workers shard from lives in Redis (§3.1), so without Redis there is nothing to shard and no lock to take; the coordinator aborts cleanly rather than running blind or risking a double-run. (Redis loss *between* runs only means a cold cache next time — see §6.) | Fail-closed abort; broker `has_already_submitted` still guards against any double-submit. |
| yfinance throttles a worker despite its own IP | Per-worker backoff + a shared **rate-limit circuit-breaker** flag in Redis lets one throttled worker warn the others to slow down | Same fetch-fraction abort covers the worst case. |
| Two coordinators race (CronJob overlap) | `SETNX run:{date}` — only one acquires the lock; the other exits | `concurrencyPolicy: Forbid` on the CronJob is the first line; the lock is defense in depth for multi-object races. |
| Node running Redis dies | Cache is cold on restart (rebuildable); run-lock lost (acceptable — broker idempotency covers it). Journal/state are NOT on Redis — they're durable via Litestream. | Only *rebuildable* state lives in Redis by design. |

**Invariant:** the only irreversible action (placing orders) happens once,
centrally, last, behind the same limit-price band, order-count budget,
notional budget, and kill switch as v1 (DESIGN.md §5). Distribution cannot
create a trade the monolith wouldn't have.

---

## 6. State & durability

Three tiers, by how much we care if we lose it:

1. **Rebuildable (Redis):** fundamentals cache, universe snapshot, progress,
   locks. Loss = a slower next run. No replication needed; single Redis.
2. **Served, regenerable (coordinator local-path PVC):** `report/`,
   `screen_results.csv`, archives. Regenerated every run; node-local RWO is
   fine.
3. **Irreplaceable audit trail (`journal.db`, `state.json`):** the strike
   history and trade journal. **This must survive a node loss.** Chosen
   mechanism: **Litestream** sidecar on the coordinator, streaming the
   SQLite WAL continuously to an **S3-compatible bucket hosted off the Pi
   cluster** (e.g. MinIO on the existing Unraid box, or a cloud bucket).
   Rationale over Longhorn: Litestream is ~megabytes of RAM vs Longhorn's
   per-node manager+engine overhead, keeps the heaviest storage *off* the
   900 MB nodes entirely, and matches an append-only SQLite audit file
   perfectly. Longhorn (replica=2) is the fallback if fully-in-cluster HA
   is preferred over an external dependency.

---

## 7. Infrastructure choices (arm64 / 900 MB rationale)

| Need | Choice | Why this one on this hardware |
|------|--------|-------------------------------|
| KV / cache / coordination | **Redis** (single StatefulSet) | Mature arm64 image, ~10–20 MB resident, TTL + `SETNX` + Lua for atomic ops + pub/sub for progress — one component covers cache, lock, snapshot, and progress. Dragonfly wants more RAM; etcd (already in k3s) is for k8s control-plane, not app cache. |
| Result transport | **Direct HTTP** (worker→coordinator) | Chosen model; no broker to run; synchronous "all shards in?" is trivial. |
| Durable audit trail | **Litestream → external S3/MinIO** | Keeps heavy, replicated storage off the tiny nodes; near-zero RAM. |
| Parallel fan-out | **k8s Indexed Job** | Native static sharding, per-index retry, anti-affinity for 1-per-node IP spread. No queue/broker needed. |
| Report serving | **nginx Deployment** (built) | Static files, ~32 MB. |

### 7.1 Memory budget (worst case, during a run)
- System per node (kubelet + containerd + flannel): ~250 MB (fixed).
- Redis node: +~20 MB (Redis) +~32 MB (nginx) = ~300 MB total → headroom.
- Each worker node during a run: +~350 MB (python+pandas, ~375 tickers).
  ~600 MB on a 900 MB node — fits, with anti-affinity ensuring **one**
  heavy pod per node.
- Coordinator during reduce: +~400 MB on its node; keep the coordinator's
  node from also hosting a worker via anti-affinity where the schedule
  allows (4 workers + 1 coordinator on 4 nodes means one node hosts both —
  assign the coordinator to the Redis/nginx node, whose baseline is
  lightest, or accept a brief squeeze during reduce, which happens *after*
  that node's worker has exited).

---

## 8. Security

- **Broker credentials (`ALPACA_*`) are mounted only on the coordinator**,
  never on workers or Redis — workers are screen-only by construction and
  cannot place orders (they don't import `execution.py`; same guarantee the
  daily screen already enforces, M14).
- **NetworkPolicy**: coordinator ingress restricted to worker pods; Redis
  ingress restricted to coordinator + workers; nothing in the namespace is
  exposed off-cluster except the report NodePort.
- Per-run **shared token** authenticates worker POSTs.
- Litestream's S3 credentials are a separate Secret, coordinator-only.

---

## 9. Observability

- Structured logs to stdout per pod (existing `journal.configure_logging`);
  `kubectl logs -l` aggregates by run.
- **Live progress** moves from the per-file `progress.json` to a Redis
  key/pub-sub the coordinator merges across shards, so the report's
  progress bar reflects the *distributed* run (`342/1504, 4 workers`).
- Per-run summary line: cache hit-rate, per-shard fetch/miss/failure
  counts, wall-clock — the hit-rate is the health metric that tells us the
  cache is doing its job.
- Alerting reuses v1's channels (run failure, empty universe, fetch-
  fraction fallback, any liquidation, reconciliation mismatch — DESIGN.md
  §3.6/M11), plus a new alert on **cache hit-rate collapse** (would signal
  Redis trouble or a mass cache invalidation).

---

## 10. Migration plan (small, independently shippable milestones)

Each milestone is usable and reviewable on its own and preserves a working
system (the v1 monolith keeps running until the last step flips the
default). Ordered so the highest-value, lowest-risk pieces land first.

- **D0 — Validate the load-bearing assumption.** Empirically confirm
  yfinance's rate limit is per-IP: run identical fetch bursts from two Pis
  simultaneously and measure whether they throttle independently. If they
  *share* a limit, lever 2 (sharding) collapses and we ship only lever 1
  (the cache) on a single node. **Gate for everything *past the cache*
  (D2+)** — D1 ships regardless of D0's outcome and can even proceed in
  parallel with it.
- **D1 — Fundamentals cache, single-node.** Add a cache abstraction
  (`cache.py`) with a Redis backend and an in-process/no-op fallback; wire
  `data.fetch_metrics` through it (get→miss→fetch→set+TTL). Deploy Redis.
  Ship on the *existing* single-node CronJob first — this delivers the
  biggest win (incremental daily screen) with zero distribution risk.
- **D1.5 — Measure the cache payoff (gate).** Before building any of D2+,
  observe the realized cache hit-rate over several ordinary daily runs and
  confirm cache-miss count actually drops to near zero (§9). This is the
  second load-bearing gate: if D1 alone makes daily screens near-instant,
  the marginal value of the whole distributed build (D2–D5) is only a
  handful of cold-start/quarterly-turnover events, and we should stop here.
  Distribution proceeds only if the measured residual fetch volume is large
  enough to justify it.
- **D2 — Worker entrypoint + partial-result contract.** A `worker.py` that
  reads a universe snapshot, takes its shard, fetches (through the D1
  cache), scores, and POSTs partials. Unit + e2e tested against a fake
  coordinator; not yet wired to real distribution.
- **D3a — Coordinator happy-path (manual trigger), execution disabled.**
  A `coordinator.py` that scrapes the universe, writes the snapshot,
  launches the worker Indexed Job via the k8s API (service account + RBAC),
  collects partials to a deadline, and runs the existing reduce/portfolio
  path — run by hand, no locking yet. **Execution is hard-disabled for this
  milestone** (no Alpaca credentials mounted on the D3a coordinator, same
  pattern `daily_screen.py` already uses for screen-only safety): the whole
  point of §5's failure-mode table is that execution is safe to run
  centrally *once* the run-lock/token hardening in D3b exists, and D3a is
  precisely the period before that hardening exists. This is the first
  end-to-end scatter-gather; keeping it separate isolates the k8s-API/RBAC
  integration risk from the locking/auth risk.
- **D3b — Correctness hardening.** Add the Redis run-lock (fenced/renewed,
  per §5), the per-run token auth, and the fetch-fraction abort. Only once
  D3b is in place and reviewed does the coordinator get Alpaca credentials
  and reach the portfolio→execution step for real. Each piece testable on
  its own against the D3a happy path.
- **D4 — Durability.** Litestream sidecar streaming `journal.db`/
  `state.json` to external S3/MinIO; restore-on-start verified.
- **D5a — Apply & validate hardening.** NetworkPolicies (coordinator/Redis
  ingress isolation, §8), anti-affinity, per-run-token enforcement, and the
  cache-hit-rate alert — applied and exercised *while the monolith is still
  the default*, so isolation is proven before it's load-bearing.
- **D5b — Cut over.** Flip the daily/quarterly CronJobs from the monolith
  path to the coordinator path; keep the monolith runnable as the
  documented rollback.

Rollback at any point: the monolith `bot.py`/`daily_screen.py` paths remain
intact and runnable; D5b is the only step that changes the default, and it's
a one-line CronJob image/command change to revert. (Note: because manifests
reference the mutable `munger:latest`, a rollback to a *prior code build*
means re-importing that older build as `:latest`, not editing a manifest to
a pinned tag — pin `:$TAG` in the manifests if precise, per-version
rollback matters.)

---

## 11. When this is *not* worth it (honest trade-off)

If D0 shows the rate limit is **not** per-IP, the parallel lever is worth
little and the added operational surface (Redis, coordinator/worker split,
Litestream, NetworkPolicies) is not justified by memory-spreading alone —
in that case ship **only D1** (the cache) on the single-node monolith and
stop. The cache is valuable regardless of distribution; the distribution is
valuable only if the per-IP assumption holds. This document should not be
implemented past D1 until D0 says so.
