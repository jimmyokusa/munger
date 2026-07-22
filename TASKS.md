# Task Tracker

Granular sub-tasks per milestone. `README.md` tracks milestone-level status;
this file is the working log underneath it — update a task's status (and
add a date/note) as it moves, rather than waiting until the whole milestone
is done. Full rationale for each item lives in `DESIGN.md`.

Status values: `todo` (not started, actionable anytime) / `in-progress` /
`done` / `blocked` (cannot be advanced by the coding agent regardless of
effort — needs the user, e.g. an external account signup).

## M0 — Project scaffolding

| Task | Status | Date / Notes |
|---|---|---|
| Repo layout per DESIGN.md §7 | done | 2026-07-21 — clarified DESIGN.md §7 during review: the tree is the `munger` repo root itself, no nested package directory |
| `config.py` — every threshold/toggle as named constants | done | 2026-07-21 — reviewed by python-reviewer + staff-engineer-reviewer; runtime paths anchored to `BASE_DIR` per review finding |
| `requirements.txt` | done | 2026-07-21 — pinned with `~=` per python-reviewer finding (was unpinned) |
| `tests/` directory | done | 2026-07-21 — `tests/test_config.py`, 7 passing tests |
| `.gitignore` for `state.json`, `journal.db`, `screen_results*.csv`, `KILL_SWITCH` flag file | done | 2026-07-21 |
| Alpaca paper-trading account + API key provisioning | blocked | needs the user to actually create the account — not something I can do. Tracked here because M0 is a natural time to knock it out early, but **does not gate M1+** — M1–M7 have no dependency on it, only M8 (execution) does. Definition of done: paper account created; `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` set as env vars (e.g. via a gitignored `.env`); confirmed the keys' reported account mode is actually `paper` (informal check now, formalized as the M10 startup assertion later). Shouldn't be left until M8 itself, since account approval can be slow. |

## M1 — Universe module

Broadened mid-milestone from S&P 500-only to the full S&P Composite 1500
(S&P 500 + S&P 400 MidCap + S&P 600 SmallCap) per user decision 2026-07-21
— see DESIGN.md §3.1. ETF-holdings-file sourcing was evaluated and
rejected (SLY 404s, SPY's Sector column is blank); Wikipedia's three
index pages share the same table structure and are used for all three.

**Fallback is per-index, not all-or-nothing** (DESIGN.md §3.1, added after
staff-engineer-reviewer + pm-reviewer both flagged this as unspecified):
each of the three indices is fetched/validated/falls-back independently,
so the static fallback file needs a source-index column to slice from,
and each per-index fallback gets its own alert (DESIGN.md §3.6) rather
than relying on the aggregate ticker count, which would still look
healthy. Combined results are de-duplicated by ticker (500 → 400 → 600
precedence on conflict) to handle the rare index-reclassification window
where a ticker briefly appears on two pages at once.

| Task | Status | Date / Notes |
|---|---|---|
| `get_universe()` — Wikipedia scrape, S&P 500 (single-index version) | done | 2026-07-21 — required a custom User-Agent header (default urllib UA gets HTTP 403 from Wikipedia's bot policy); found live, not theoretically. Superseded by the multi-index task below. |
| Generalize `get_universe()` to fetch + validate + combine all three indices (S&P 500 + 400 + 600), each with independent per-index fallback | todo | each index validated against its own row-count band (500: ~490–510; 400: ~390–410; 600: ~590–615); combine step de-duplicates by ticker (500→400→600 precedence). Done means a live run returns ~1500 tickers, verified by count and by spot-checking a few known MidCap/SmallCap names — not unit tests alone. |
| `config.py`: per-index validation bands + fallback file path/schema | todo | replace the single `UNIVERSE_MIN/MAX_TICKER_COUNT` pair with one pair per index (or a per-index dict); decide whether `STATIC_UNIVERSE_FALLBACK_PATH` is renamed or kept and just expanded — flagged by pm-reviewer as untracked |
| Static fallback ticker file: expand to full composite with source-index column | in-progress | 2026-07-21 — `data/sp500_fallback.csv` (503 tickers + sector) covers S&P 500 only so far; needs a `source_index` column (500/400/600) so per-index fallback can slice it, and needs the 400/600 rows added. Generation process: one-time manual scrape-and-save from the same Wikipedia pages, dated in a comment/commit message; revisit if validation bands start failing in production. |
| Ticker normalization (`BRK.B` → `BRK-B`) | done | 2026-07-21 |
| Optional sector-exclusion list | done | 2026-07-21 — verified live (excluding Financials correctly drops JPM, keeps AAPL); re-verify after multi-index generalization |
| Scrape validation (row-count/format sanity check) + fallback on validation failure | in-progress | 2026-07-21 — validates the *raw* scrape before sector exclusion, not after; validating post-exclusion was a real bug found live (any configured exclusion would make the count fall outside the band and force fallback every run). Downgraded from done to in-progress per pm-reviewer: per-index bands and per-index fallback slicing are not yet implemented, only the single-index version is. |
| Widen exception handling to cover the whole pipeline (fetch → validate → exclude → normalize), not just the fetch call | done | 2026-07-21 — found by staff-engineer-reviewer: a missing/renamed GICS Sector column would raise uncaught inside `_apply_sector_exclusions`, crashing instead of falling back |
| Unit tests: `_apply_sector_exclusions` (pure function) | todo | in-memory DataFrame fixture |
| Unit tests: `get_universe()` branches via monkeypatching the fetch call | todo | cover: raises → fallback; fails validation → fallback; valid + exclusion applied; missing/renamed sector column → still falls back (regression test for the bug above); one index fails/falls back while the other two succeed live, combined result is correct (the new branch the multi-index generalization introduces) |
| Unit tests: cross-index de-duplication | todo | a ticker appearing in two source lists collapses to one entry, with 500→400→600 precedence on conflicting sector data |

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
