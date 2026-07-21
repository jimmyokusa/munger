# munger

An automated investing system that combines Benjamin Graham's
margin-of-safety discipline (*The Intelligent Investor*) with Charlie
Munger's quality-first philosophy. It screens the S&P 500 for stocks that
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

- [ ] **M0 — Project scaffolding.** Repo layout from DESIGN.md §7;
      `config.py` with every threshold/toggle as a named constant (nothing
      hard-coded elsewhere); `requirements.txt`; `tests/`; `state.json`
      gitignored.
- [ ] **M1 — Universe module.** `get_universe()`: S&P 500 constituents via
      Wikipedia scrape with a static fallback file shipped in-repo; ticker
      normalization (`BRK.B` → `BRK-B`); optional sector exclusion list.
- [ ] **M2 — Data fetcher.** `fetch_metrics(symbol)` via yfinance; thread
      pool (~10–15 workers); per-ticker failures tolerated without
      aborting the run; raw responses cached per run.
- [ ] **M3 — Data validation layer.** `validate_metrics(data)`: sanity/
      outlier checks (e.g. P/E < 10,000, realistic debt ratios); missing or
      invalid data fails the ticker rather than passing it through.
- [ ] **M4 — Graham gate logic.** `pass_graham_gates(metrics)` implementing
      the 7 criteria in DESIGN.md §3.3 Stage 1; returns
      `(passed, fail_reasons)`; thresholds pulled from `config.py`.
- [ ] **M5 — Munger scorer.** `calculate_munger_score(metrics)` (0–100,
      weighted ROE/margins/FCF yield/debt) plus `run_screen(tickers)`
      producing the full results DataFrame and `screen_results.csv`. Tune
      thresholds against live data until the buyable list is sane
      (~20–50 names).
- [ ] **M6 — Strike state machine.** `StateTracker` reading/writing
      `state.json` (atomic: temp file + rename); `process_sells(...)`
      implementing the two-strike rule, including missing-ticker-as-strike
      and reset-on-clean-check.
- [ ] **M7 — Buy queue / target construction.** `generate_buy_queue(...)`:
      ~1/15 target weights, 12% single-name cap, 2% cash buffer, top-up
      existing positions before opening new ones, $50 dust filter, never
      sell-to-buy.
- [ ] **M8 — Execution module.** `ExecutionModule` wrapping alpaca-py:
      `market_buy(symbol, notional)`, `liquidate(symbol)`,
      `get_todays_open_orders()` checked before every order to keep the
      module idempotent against re-runs; paper by default.
- [ ] **M9 — Trade journal & logging.** Append-only journal (CSV or
      SQLite) with a reason string per order; timestamped
      `screen_results.csv` archive; structured logging to file and stdout.
- [ ] **M10 — Bot orchestration & safety controls.** `bot.py` tying every
      module together; `KILL_SWITCH` (config flag + filesystem flag file)
      forcing screen-only mode; `GLOBAL_ORDER_BUDGET` abort; universe-
      fetch-fraction sanity abort.
- [ ] **M11 — Scheduling & observability.** Cron/GitHub Action wiring for
      quarterly cadence; same-day idempotency (check open orders + today's
      journal before submitting); heartbeat/alerting on run failure or an
      empty universe fetch; data-freshness check.
- [ ] **M12 — Paper trading validation.** Run at least one full quarter
      cycle on Alpaca paper; audit the journal for near-zero sells, buys
      concentrated at the top of the score ranking, and weights landing
      near target.

Backtesting is explicitly out of scope for v1 (see DESIGN.md §6/§9) —
point-in-time fundamental data would be needed to avoid a misleading
result.
