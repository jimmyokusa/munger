# munger

An automated investing system that combines Benjamin Graham's
margin-of-safety discipline (*The Intelligent Investor*) with Charlie
Munger's quality-first philosophy. It screens the S&P Composite 1500
(S&P 500 + S&P MidCap 400 + S&P SmallCap 600) for stocks that
are both cheap (Graham) and high-quality (Munger), holds a concentrated
~15-position portfolio, and sells only on a deliberate two-strike
deterioration in fundamentals — never on price movement alone. It runs
quarterly against Alpaca (paper trading first), and most runs are expected
to place zero sell orders.

Full specification: [DESIGN.md](DESIGN.md).

## Tech stack

Python 3.11+, yfinance, pandas, alpaca-py, pytest. Repo layout per
[DESIGN.md §7](DESIGN.md#7-technology-stack).

## Milestones

Built and worked through one at a time, in order. Each should be usable and
inspectable on its own before moving to the next.

- [x] **M0 — Project scaffolding.** Repo layout from DESIGN.md §7;
      `config.py` with every threshold/toggle as a named constant (nothing
      hard-coded elsewhere); `requirements.txt`; `tests/`; `.gitignore` for
      `state.json`, `journal.db`, `screen_results*.csv`, and the
      `KILL_SWITCH` flag file. (Alpaca account provisioning is tracked in
      `TASKS.md` but doesn't gate this milestone or M1+ — see there.)
- [x] **M1 — Universe module.** `get_universe()`: S&P Composite 1500
      constituents (S&P 500 + S&P 400 + S&P 600) via a hardened Wikipedia
      scrape of all three (a paid Financial Modeling Prep API was
      evaluated for the S&P 500 leg and declined — not worth the ongoing
      cost) — with a static fallback file shipped in-repo; ticker
      normalization (`BRK.B` → `BRK-B`); optional sector exclusion
      list; validate each index's result independently against its own
      row-count band and fall back to the static file on either a fetch
      exception or a validation failure — a silently-corrupted-but-
      well-formed result is as dangerous as an outright failure.
- [x] **M2 — Data fetcher.** `fetch_metrics(symbol)` via yfinance; thread
      pool with retry-and-backoff on transient/rate-limit errors; per-ticker
      failures tolerated without aborting the run; raw responses cached per
      run. yfinance's rate limit is session-wide, not per-worker, so this
      has been tuned more than once (see `TASKS.md` M2/M13) and remains a
      known, imperfect constraint against an undocumented provider limit.
- [x] **M3 — Data validation layer.** `validate_metrics(data)`: sanity/
      outlier checks (e.g. P/E < 10,000, realistic debt ratios); missing
      data fails with `data_missing:<field>`, implausible data fails with
      `data_invalid_outlier:<field>` — distinct codes, not one generic
      failure, so `screen_results.csv` shows which is which.
- [x] **M4 — Graham gate logic.** `pass_graham_gates(metrics)` implementing
      the 7 criteria in DESIGN.md §3.3 Stage 1; returns
      `(passed, fail_reasons)`; thresholds pulled from `config.py`.
- [x] **M5 — Munger scorer.** `calculate_munger_score(metrics)` (0–100,
      weighted ROE/margins/FCF yield/debt) plus `run_screen(tickers)`
      producing the full results DataFrame and `screen_results.csv`. Live
      data currently yields a very tight buyable list (single digits, not
      the ~20–50 originally targeted) — today's elevated market valuations,
      confirmed against real tickers, not a data bug; see `TASKS.md` M5.
- [x] **M6 — Strike state machine.** `StateTracker` reading/writing
      **only the strike counters** to `state.json` (atomic: temp file +
      rename) — never current holdings, which are always fetched live from
      the broker; `process_sells(...)` implementing the two-strike rule,
      including missing-ticker-as-strike, reset-on-clean-check, and a
      reconciliation warning if live holdings diverge from what the last
      run's journal expected.
- [x] **M7 — Buy queue / target construction.** `generate_buy_queue(...)`:
      ~1/15 target weights, 12% single-name cap, 2% cash buffer, top-up
      existing positions before opening new ones, $50 dust filter, never
      sell-to-buy.
- [x] **M8 — Execution module.** `ExecutionModule` wrapping alpaca-py:
      `market_buy(symbol, notional)`, `liquidate(symbol)`, each submitted
      with a deterministic `client_order_id` (run-date + ticker + side) so
      the broker itself rejects duplicate submissions from a crashed-and-
      restarted run — the primary idempotency guarantee, not just a
      client-side pre-check; `has_already_submitted(client_order_id)` is
      the secondary guard, and if that broker query fails, it raises so the
      caller aborts the run rather than proceeding blind. Every order
      carries a limit-price band (±2%) instead of an unconstrained market
      order. Paper by default — and now live-verified: a real `market_buy`/
      `liquidate` round trip against Alpaca's paper API confirmed `notional`
      sizing works on limit orders (previously ambiguous in Alpaca's own
      docs) and that the crash-recovery idempotency path works for real,
      not just in mocks.
- [x] **M9 — Trade journal & logging.** Append-only **SQLite** journal
      (`journal.db`) with a reason string per order — SQLite specifically,
      since idempotency (M8) depends on reading it reliably and a torn CSV
      write is a real corruption risk; timestamped `screen_results.csv`
      archive; structured logging to file and stdout.
- [x] **M10 — Bot orchestration & safety controls.** `bot.py` tying every
      module together; startup assertion that the loaded API keys'
      account mode (paper/live) matches the configured flag, aborting on
      mismatch; `KILL_SWITCH` (config flag + filesystem flag file) forcing
      screen-only mode; `GLOBAL_ORDER_BUDGET` (max order count) **and**
      `GLOBAL_NOTIONAL_BUDGET_PCT` (max % of equity moved per run) abort —
      the notional cap bounds mistake *size*, the order-count cap only
      bounds mistake *count*; universe-fetch-fraction sanity abort.
- [x] **M11 — Scheduling & observability.** Cron/GitHub Action wiring
      (`.github/workflows/quarterly-run.yml`) for quarterly cadence, plus a
      monthly `heartbeat.yml` watchdog; same-day idempotency via
      `client_order_id` + `has_already_submitted`; alerting on run failure,
      an empty universe fetch, per-index universe fallback, any liquidation
      event (the rarest/highest-signal thing this system does), or a
      state/broker reconciliation mismatch; a data-freshness dead-man's-
      switch. **Code-complete but not yet verified against real GitHub
      Actions** — the state-persistence and heartbeat mechanisms need the
      user to add `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` as GitHub repo
      secrets and manually run `workflow_dispatch` at least once; see
      `TASKS.md` M11 for the exact sequence.
- [ ] **M12 — Paper trading validation.** Run at least one full quarter
      cycle on Alpaca paper; audit the journal for near-zero sells, buys
      concentrated at the top of the score ranking, and weights landing
      near target. One live attempt so far aborted safely at the
      universe-fetch-fraction check (heavy yfinance rate limiting that day)
      before any trading decision — doesn't count toward the quarter-cycle
      requirement; genuinely blocked on ~3 months of real elapsed time once
      M11's scheduling is verified and running.
- [x] **M13 — HTML report** *(added after v1's original build order; user
      request).* Static site (`report.py`, no server) showing current picks
      with an expandable reason/metrics panel per ticker, a sortable/
      filterable table of every other screened ticker, a live progress bar
      while a screen is running, and a glass visual style. See
      [DESIGN.md §3.7](DESIGN.md#37-html-report).
- [x] **M14 — Daily screen-only snapshot + report calendar** *(user
      request).* `daily_screen.py`: a read-only daily job that fetches the
      universe, screens it, archives the result, and regenerates the report
      — deliberately never importing `execution.py`, so it is
      architecturally incapable of placing an order (trading stays on
      `bot.py`'s quarterly cadence). `report.py` gains a calendar view
      (`calendar.html`) linking each day to its archived results, and copies
      archives under `report/` so the site is self-contained wherever it's
      served. Runs both via `.github/workflows/daily-screen.yml` and as a
      k3s CronJob (see Deployment).

Backtesting is explicitly out of scope for v1 (see DESIGN.md §6/§9) —
point-in-time fundamental data would be needed to avoid a misleading
result.

## Deployment (Raspberry Pi k3s cluster)

munger runs on a 4-node Raspberry Pi **k3s** cluster (arm64). One command,
`deploy/build-and-deploy.sh`, builds the arm64 image (`deploy/Dockerfile`),
side-loads it into the cluster's containerd (no registry), runs the full
test suite as an **in-cluster CI Job**, and — only if CI is green — applies
the CD workloads: an nginx **Deployment/Service** (report viewer, NodePort
30080) and the daily-screen **CronJob**, both backed by a PVC. Writable
paths are relocatable onto that PVC via the `MUNGER_DATA_DIR` env var
(`config.py`). Manifests live in `deploy/k8s/`; the end-to-end workflow,
review gates, and CI/CD flow are documented in the project skill,
`.claude/skills/munger-workflow/SKILL.md`.

A distributed **v2 architecture** — sharding the rate-limited data-fetch
stage across all four nodes behind a shared fundamentals cache — is drafted
in [DESIGN_DISTRIBUTED.md](DESIGN_DISTRIBUTED.md) (not yet built; gated on
empirically confirming the yfinance rate limit is per-IP).
