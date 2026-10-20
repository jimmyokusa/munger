# munger

An automated investing system that combines Benjamin Graham's
margin-of-safety discipline (*The Intelligent Investor*) with Charlie
Munger's quality-first philosophy. It screens the S&P Composite 1500
(S&P 500 + S&P MidCap 400 + S&P SmallCap 600) for stocks that
are both cheap (Graham) and high-quality (Munger), holds a concentrated
~15-position portfolio, and sells only on a deliberate two-strike
deterioration in fundamentals — never on price movement alone. It runs
daily against Alpaca (paper trading first; see DESIGN.md §4/§1) with a
rebalancing tolerance band so trading stays low-frequency in practice, and
most runs are expected to place zero sell orders.

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
      (`.github/workflows/daily-trade.yml`, renamed 2026-07-26 from
      `quarterly-run.yml` when the cadence changed to daily) plus a daily
      `heartbeat.yml` watchdog; same-day idempotency via `client_order_id`
      + `has_already_submitted`; alerting on run failure, an empty universe
      fetch, per-index universe fallback, any liquidation event (the
      rarest/highest-signal thing this system does), or a state/broker
      reconciliation mismatch; a data-freshness dead-man's-switch. Secrets
      (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) are added as GitHub repo
      secrets and the workflow has been run manually multiple times; see
      `TASKS.md` M11/M12 for the run history.
- [ ] **M12 — Paper trading validation.** Daily cadence (2026-07-26,
      superseding the original "run a full quarter cycle" bar — see
      DESIGN.md §4/§6): observe an initial ~1-week convergence window from
      cold start, then a ≥1-month steady state where most days should
      place zero orders; audit the journal for near-zero sells, buys
      concentrated at the top of the score ranking, and weights landing
      near target. Two live attempts so far: the first aborted safely at
      the universe-fetch-fraction check (heavy yfinance rate limiting that
      day); the second reached the buy queue and safely aborted the whole
      run at the notional-budget check (a real cold-start deadlock, since
      fixed — see `TASKS.md` M12). Neither placed an order yet; the fix
      needs one real manual `workflow_dispatch` run before the daily cron
      is trusted unattended.
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
      `bot.py`'s own daily cadence via `daily-trade.yml`, a separate
      GitHub Actions workflow). `report.py` gains a calendar view
      (`calendar.html`) linking each day to its archived results, and copies
      archives under `report/` so the site is self-contained wherever it's
      served. Runs both via `.github/workflows/daily-screen.yml` and as the
      Cloud Run `daily-screen` job (see Deployment; a k3s CronJob until
      2026-10-20).

- [x] **M15 — Report UX overhaul** *(user request)*. Visual badges
      (zero-debt/high-ROE/dividend highlights), plain-language metric
      tooltips, a collapsible "how scoring & screening works" methodology
      drawer, Copy-JSON/Export-CSV buttons, per-ticker outbound research
      links (SEC EDGAR, Finviz), and a `feed.json`/`rss.xml` so new
      candidate drops are subscribable. All static/vanilla-JS, matching
      DESIGN.md §3.7's no-server constraint. Visually confirmed live on
      `gramunger.com`; the feed's absolute-URL base is set on the Cloud
      Run deployment.
- [ ] **M16 — P&L Tracking** *(user request, 2026-07-27)*. "Track profit
      and loss... for the moment just the paper trading, but extensible
      for real money in the future." New `pnl.py` fetches an account/
      positions/history snapshot straight from Alpaca (not derived from
      `journal.db`, which only records order notional, not fill price) and
      is account-agnostic — reports "paper" or "live" based on whatever
      `config.PAPER_TRADING` actually says, nothing paper-specific baked
      in. A new `pnl.html` (stat tiles, a positions table, gains/losses
      colored distinctly) is bridged from GitHub Actions (where the Alpaca
      keys live) to Cloud Run's report deployment (which deliberately
      never has those keys, DESIGN.md §3.5) via a new GCP service account
      scoped to write only the P&L snapshot into the report bucket.
      Cloud Run/`gramunger.com` only for now — the k3s dev report is
      deferred (see DESIGN.md §3.8/`TASKS.md` M16). Code written and
      tested (260/260 passing); the live bridge hasn't fired yet.

Backtesting is explicitly out of scope for v1 (see DESIGN.md §6/§9) —
point-in-time fundamental data would be needed to avoid a misleading
result.

## Deployment (Google Cloud Run)

Prod runs on Cloud Run in GCP project `munger-503515` (`us-central1`):

- **`daily-screen`**, a Cloud Run **Job** on a daily Cloud Scheduler trigger
  (`0 13 * * *`). It runs the screen-only entrypoint (`daily_screen.py`,
  built from `deploy/Dockerfile`), which never imports `execution.py`, and
  regenerates the report.
- **`report-web`**, a Cloud Run **service** (`deploy/cloudrun/report-web/`).
  It serves that report at **gramunger.com**, with Cloudflare in front and
  the real-money pages behind a login.

Writable state lives in the GCS bucket `munger-503515-data`, mounted via
`gcsfuse` at `MUNGER_DATA_DIR` (`config.py`). Deploy either target with:

```bash
deploy/cloudrun/deploy.sh [daily-screen|report-web] [--verify]
```

The script builds via Cloud Build, pins the Cloud Run resource to the exact
image digest, and with `--verify` runs a check: a full screen execution for
`daily-screen`, or an HTTP 200 check for `report-web`. Cloud Build uploads
whatever `.gitignore` doesn't exclude, so keep secrets ignored there.

Trading, P&L snapshots and alerts run in **GitHub Actions** from `main`
(`.github/workflows/`), against Alpaca. A second, independent screen-only
run also happens there daily (`daily-screen.yml`, same `0 13 * * *` time as
the Cloud Run job), archiving to its own branch. CI (`ci.yml`, on
GitHub-hosted `ubuntu-latest`) runs ruff, ruff format, mypy and pytest on
every push to `main`. The
Grafana dashboards run on a small GCE VM (`deploy/gce/`). The review gates
and workflow are documented in `.claude/skills/munger-workflow/SKILL.md`.

**History:** until 2026-10-20 munger also ran on a 4-node Raspberry Pi k3s
cluster. That setup is archived, with a map of what replaced each piece, in
[`deploy/archive/k3s/`](deploy/archive/k3s/README.md). The distributed v2
design in [DESIGN_DISTRIBUTED.md](DESIGN_DISTRIBUTED.md) targeted that
cluster and was never built.

