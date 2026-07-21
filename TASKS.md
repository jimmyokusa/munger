# Task Tracker

Granular sub-tasks per milestone. `README.md` tracks milestone-level status;
this file is the working log underneath it — update a task's status (and
add a date/note) as it moves, rather than waiting until the whole milestone
is done. Full rationale for each item lives in `DESIGN.md`.

Status values: `todo` / `in-progress` / `done` / `blocked`.

## M0 — Project scaffolding

| Task | Status | Date / Notes |
|---|---|---|
| Repo layout per DESIGN.md §7 | todo | |
| `config.py` — every threshold/toggle as named constants | todo | |
| `requirements.txt` | todo | |
| `tests/` directory | todo | |
| `.gitignore` for `state.json`, `journal.db` | todo | |
| Alpaca paper-trading account + API key provisioning | todo | external dependency for M8–M12; do early since account approval can be slow — not on the critical path for M1–M7 but shouldn't be left until M8 either |

## M1 — Universe module

| Task | Status | Date / Notes |
|---|---|---|
| `get_universe()` — Wikipedia scrape | todo | |
| Static fallback ticker file shipped in-repo | todo | |
| Ticker normalization (`BRK.B` → `BRK-B`) | todo | |
| Optional sector-exclusion list | todo | |
| Scrape validation (row-count/format sanity check) + fallback on validation failure | todo | added in design review round 2 |

## M2 — Data fetcher

| Task | Status | Date / Notes |
|---|---|---|
| `fetch_metrics(symbol)` via yfinance | todo | |
| Thread pool (~10–15 workers) | todo | |
| Retry-with-backoff on transient/rate-limit errors | todo | added in design review round 2 |
| Per-ticker failure tolerance (no run-abort) | todo | |
| Raw response caching per run | todo | |

## M3 — Data validation layer

| Task | Status | Date / Notes |
|---|---|---|
| `validate_metrics(data)` sanity/outlier checks | todo | |
| Distinct `data_missing:<field>` fail code | todo | added in design review round 2 |
| Distinct `data_invalid_outlier:<field>` fail code | todo | added in design review round 2 |

## M4 — Graham gate logic

| Task | Status | Date / Notes |
|---|---|---|
| `pass_graham_gates(metrics)` — 7 criteria (DESIGN.md §3.3 Stage 1) | todo | |
| Returns `(passed, fail_reasons)` | todo | |
| Thresholds pulled from `config.py` | todo | |
| Unit tests against hand-computed fixtures (DESIGN.md §6, layer 1) | todo | one fixture per gate, plus a combined pass/fail case |

## M5 — Munger scorer

| Task | Status | Date / Notes |
|---|---|---|
| `calculate_munger_score(metrics)` | todo | |
| `run_screen(tickers) -> DataFrame` | todo | |
| `screen_results.csv` output | todo | make it genuinely easy to scan (sorted by score, key columns first) — feeds both the human eyeball check and the reviewer agent below |
| Unit tests for `calculate_munger_score` against hand-computed fixtures (DESIGN.md §6, layer 1) | todo | one fixture per weighted component, plus a combined score check |
| `screen-sanity-reviewer` subagent — reads `screen_results.csv` and flags anything that looks like a data-error artifact rather than a real high-quality compounder | todo | scoped as a plausibility/sanity checker, not stock-picking advice — same lane as the deterministic gates, formalizes the "eyeball test" DESIGN.md §6 already calls for as a manual step |
| Tune thresholds against live data (~20–50 buyable names) | todo | if the buyable count lands outside this band, treat it as a signal a threshold needs adjusting, not just noise — record the actual count and the resulting adjustment here; run the sanity-reviewer subagent against each tuning pass |

## M6 — Strike state machine

| Task | Status | Date / Notes |
|---|---|---|
| `StateTracker` — strike counters only, atomic writes to `state.json` | todo | never holds current holdings |
| `process_sells(current_holdings, new_market_data)` | todo | `current_holdings` fetched live from broker, not from state |
| Two-strike rule (missing-ticker-as-strike, reset-on-clean) | todo | |
| Reconciliation warning on live-vs-expected holdings divergence | todo | added in design review round 2 |
| Unit tests for the strike/reset state machine against hand-computed fixtures (DESIGN.md §6, layer 1) | todo | cover: one strike then clean (resets), two consecutive strikes (liquidates), missing ticker (counts as strike) |

## M7 — Buy queue / target construction

| Task | Status | Date / Notes |
|---|---|---|
| `generate_buy_queue(current_holdings, screen_results, available_cash)` | todo | |
| ~1/15 target weights, 12% single-name cap, 2% cash buffer | todo | |
| Top up existing positions before opening new ones | todo | |
| $50 dust filter | todo | |
| Never sell-to-buy (no churn) | todo | |

## M8 — Execution module

| Task | Status | Date / Notes |
|---|---|---|
| `ExecutionModule` wrapping alpaca-py | todo | |
| `market_buy(symbol, notional)`, `liquidate(symbol)` | todo | |
| `get_current_holdings()` — live portfolio fetch from the broker | todo | consumed every run by `process_sells` (M6) and `generate_buy_queue` (M7); distinct from the idempotency pre-check below — this is "what do we hold," not "did we already order today" |
| Deterministic `client_order_id` (hash of run-date + ticker + side) | todo | primary idempotency guarantee, added in design review round 2 |
| `get_todays_open_orders_and_positions()` (open + filled + positions) | todo | secondary idempotency guard (has today's run already submitted this order), widened in design review round 2 |
| Raise (abort run) if the broker pre-check query itself fails | todo | added in design review round 2 |
| Limit-price band (±2%) on every order, including liquidations | todo | added in design review round 2 |
| Paper mode by default | todo | |

## M9 — Trade journal & logging

| Task | Status | Date / Notes |
|---|---|---|
| Append-only journal as **SQLite** (`journal.db`), not CSV | todo | changed in design review round 2 |
| Reason string per order | todo | |
| Timestamped `screen_results.csv` archive | todo | |
| Structured logging to file and stdout | todo | |

## M10 — Bot orchestration & safety controls

| Task | Status | Date / Notes |
|---|---|---|
| `bot.py` — ties all modules together | todo | |
| Startup assertion: API key account mode matches configured paper/live flag | todo | added in design review round 2 |
| `KILL_SWITCH` (config flag + filesystem flag file) | todo | |
| `GLOBAL_ORDER_BUDGET` abort (max order count) | todo | |
| `GLOBAL_NOTIONAL_BUDGET` abort (max % equity moved per run) | todo | added in design review round 2 |
| Universe-fetch-fraction sanity abort | todo | |
| End-to-end smoke test: `bot.py` runs cleanly with `KILL_SWITCH` on (screen-only) against live data | todo | checkpoint before wiring up scheduling in M11 — every earlier milestone has its own inspectable artifact; this is the equivalent for the assembled orchestration |

## M11 — Scheduling & observability

| Task | Status | Date / Notes |
|---|---|---|
| Cron / GitHub Action wiring for quarterly cadence | todo | |
| Same-day idempotency via `client_order_id` + today's orders/positions | todo | |
| Alert on run failure | todo | verify by forcing a failure, not just code review |
| Alert on empty universe fetch | todo | verify by forcing an empty fetch, not just code review |
| Alert on any liquidation event | todo | added in design review round 2; verify by forcing a two-strike liquidation in paper, not just code review |
| Alert on state/broker reconciliation mismatch | todo | added in design review round 2; verify by forcing a mismatch, not just code review |
| Data-freshness check | todo | alert if data is >24 hours stale (threshold from DESIGN.md's round-1 staff-engineer review) |

## M12 — Paper trading validation

| Task | Status | Date / Notes |
|---|---|---|
| Run at least one full quarter cycle on Alpaca paper | todo | |
| Audit journal: near-zero sells | todo | pass criterion: 0–1 sell orders in the quarter; any more requires a documented two-strike justification per ticker in the journal. Record actual count + pass/fail here. |
| Audit journal: buys concentrated at top of score ranking | todo | pass criterion: new-position buys are drawn from the top half of that run's buyable score ranking, with any exception explained. Record actual result + pass/fail here. |
| Audit journal: weights landing near target | todo | pass criterion: each position's weight within ±2% of its 1/15 target after the run's buys settle. Record actual result + pass/fail here. |
| **Go/no-go: enable live trading** | todo | per DESIGN.md's PM Recommendations — at least two weeks reviewing `journal.db` output before flipping `paper`→`live`. Record the decision, date, and reasoning here; if any M12 audit above failed, this defaults to no-go until re-run for another quarter. |
