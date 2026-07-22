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
- [ ] **M1 — Universe module.** `get_universe()`: S&P Composite 1500
      constituents (S&P 500 + S&P 400 + S&P 600), sourced hybrid — S&P 500
      via Financial Modeling Prep's REST API (no vendor offers a clean
      constituents endpoint for the 400/600, so those stay on a hardened
      Wikipedia scrape) — with a static fallback file shipped in-repo;
      ticker normalization (`BRK.B` → `BRK-B`); optional sector exclusion
      list; validate each index's result independently against its own
      row-count band and fall back to the static file on either a fetch
      exception or a validation failure — a silently-corrupted-but-
      well-formed result is as dangerous as an outright failure.
- [ ] **M2 — Data fetcher.** `fetch_metrics(symbol)` via yfinance; thread
      pool (~10–15 workers) with retry-and-backoff on transient/rate-limit
      errors; per-ticker failures tolerated without aborting the run; raw
      responses cached per run.
- [ ] **M3 — Data validation layer.** `validate_metrics(data)`: sanity/
      outlier checks (e.g. P/E < 10,000, realistic debt ratios); missing
      data fails with `data_missing:<field>`, implausible data fails with
      `data_invalid_outlier:<field>` — distinct codes, not one generic
      failure, so `screen_results.csv` shows which is which.
- [ ] **M4 — Graham gate logic.** `pass_graham_gates(metrics)` implementing
      the 7 criteria in DESIGN.md §3.3 Stage 1; returns
      `(passed, fail_reasons)`; thresholds pulled from `config.py`.
- [ ] **M5 — Munger scorer.** `calculate_munger_score(metrics)` (0–100,
      weighted ROE/margins/FCF yield/debt) plus `run_screen(tickers)`
      producing the full results DataFrame and `screen_results.csv`. Tune
      thresholds against live data until the buyable list is sane
      (~20–50 names).
- [ ] **M6 — Strike state machine.** `StateTracker` reading/writing
      **only the strike counters** to `state.json` (atomic: temp file +
      rename) — never current holdings, which are always fetched live from
      the broker; `process_sells(...)` implementing the two-strike rule,
      including missing-ticker-as-strike, reset-on-clean-check, and a
      reconciliation warning if live holdings diverge from what the last
      run's journal expected.
- [ ] **M7 — Buy queue / target construction.** `generate_buy_queue(...)`:
      ~1/15 target weights, 12% single-name cap, 2% cash buffer, top-up
      existing positions before opening new ones, $50 dust filter, never
      sell-to-buy.
- [ ] **M8 — Execution module.** `ExecutionModule` wrapping alpaca-py:
      `market_buy(symbol, notional)`, `liquidate(symbol)`, each submitted
      with a deterministic `client_order_id` (hash of run-date + ticker +
      side) so the broker itself rejects duplicate submissions from a
      crashed-and-restarted run — the primary idempotency guarantee, not
      just a client-side pre-check. `get_todays_open_orders_and_positions()`
      (covering filled orders too, not just open ones) is a secondary
      guard; if that broker query fails, raise so the caller aborts the
      run rather than proceeding blind. Every order carries a limit-price
      band (e.g. ±2%) instead of an unconstrained market order. Paper by
      default.
- [ ] **M9 — Trade journal & logging.** Append-only **SQLite** journal
      (`journal.db`) with a reason string per order — SQLite specifically,
      since idempotency (M8) depends on reading it reliably and a torn CSV
      write is a real corruption risk; timestamped `screen_results.csv`
      archive; structured logging to file and stdout.
- [ ] **M10 — Bot orchestration & safety controls.** `bot.py` tying every
      module together; startup assertion that the loaded API keys'
      account mode (paper/live) matches the configured flag, aborting on
      mismatch; `KILL_SWITCH` (config flag + filesystem flag file) forcing
      screen-only mode; `GLOBAL_ORDER_BUDGET` (max order count) **and**
      `GLOBAL_NOTIONAL_BUDGET` (max % of equity moved per run) abort —
      the notional cap bounds mistake *size*, the order-count cap only
      bounds mistake *count*; universe-fetch-fraction sanity abort.
- [ ] **M11 — Scheduling & observability.** Cron/GitHub Action wiring for
      quarterly cadence; same-day idempotency via `client_order_id` +
      today's open/filled orders and positions; heartbeat/alerting on run
      failure, an empty universe fetch, any liquidation event (the
      rarest/highest-signal thing this system does), or a state/broker
      reconciliation mismatch; data-freshness check.
- [ ] **M12 — Paper trading validation.** Run at least one full quarter
      cycle on Alpaca paper; audit the journal for near-zero sells, buys
      concentrated at the top of the score ranking, and weights landing
      near target.

Backtesting is explicitly out of scope for v1 (see DESIGN.md §6/§9) —
point-in-time fundamental data would be needed to avoid a misleading
result.
