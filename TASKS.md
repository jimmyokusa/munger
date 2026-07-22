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
rejected (SLY 404s, SPY's Sector column is blank).

**Sourcing is hybrid, revised again 2026-07-21:** the user clarified they
act on this system's output with real money, not paper trades, which
raises the bar on universe-module correctness. No API vendor (checked
Finnhub, FMP) offers a constituents endpoint for the S&P 400/600 at any
price point, so those two stay on the hardened Wikipedia scrape. The S&P
500 — the largest, most consequential slice — moves to **Financial
Modeling Prep's `sp500_constituent` REST API** instead, removing scraping
risk from that slice entirely. Both source types share one
validate-and-fallback contract (DESIGN.md §3.1) so the failure-handling
story doesn't fork by source technology.

**M2+ may start before FMP is provisioned** (pm-reviewer raised this as
previously implicit): the S&P 500 leg degrades safely to the static
fallback file with a high-priority log until the key exists, so nothing
downstream breaks. But per DESIGN.md's real-money framing, **the live
full-composite validation row below should be run and confirmed before
any `screen_results.csv` output is actually acted on** — until then, the
system has no way of knowing on its own whether `_fetch_fmp_sp500()`'s
field-name assumptions (`symbol`/`sector` keys) even match FMP's real
response shape, since that's only been exercised against fixtures so far.

**Fallback is per-index, not all-or-nothing** (DESIGN.md §3.1, added after
staff-engineer-reviewer + pm-reviewer both flagged this as unspecified):
each of the three indices is fetched/validated/falls-back independently,
so the static fallback file needs a source-index column to slice from,
and each per-index fallback gets its own alert (DESIGN.md §3.6) rather
than relying on the aggregate ticker count, which would still look
healthy. Combined results are de-duplicated by ticker (500 → 400 → 600
precedence on conflict) to handle the rare index-reclassification window
where a ticker briefly appears in two source lists at once.

**Code landed 2026-07-21**, reviewed by `code-reviewer` (substituting for
`python-reviewer`, which turned out to have the same workspace-root
agent-discovery scoping issue as the hooks — copied to
`/Users/jimmyok/workspace/.claude/agents/` for next session) and
`staff-engineer-reviewer` against DESIGN.md §3.1. Both reviews
independently flagged the same critical gap (missing `lxml` dependency)
plus a handful of real correctness issues, all fixed before merge — see
per-row notes below.

| Task | Status | Date / Notes |
|---|---|---|
| `get_universe()` — Wikipedia scrape, S&P 500 (single-index version) | done | 2026-07-21 — required a custom User-Agent header (default urllib UA gets HTTP 403 from Wikipedia's bot policy); found live, not theoretically. Superseded: S&P 500 moves to the FMP API task below, this code became the S&P 400/600 path instead. |
| FMP account + `FMP_API_KEY` provisioning | blocked | needs the user to sign up — not something I can do. Definition of done (mirrors the M0 Alpaca task): account created; **confirm at signup that the chosen plan actually includes the `sp500_constituent` endpoint and note the current rate limit** — don't assume free-tier coverage; `FMP_API_KEY` set as an env var (same gitignored `.env` pattern as Alpaca). Does **not** gate the rest of M1 — everything below except the two live-validation rows was built and tested without a live key. |
| `_fetch_fmp_sp500()` — S&P 500 via FMP `sp500_constituent` API | done | 2026-07-21 — relies on `urlopen`'s raise-by-default for non-2xx status (confirmed live: FMP returns a plain 401 for an invalid key) plus an explicit dict/error-body check for the "HTTP 200 with error body" case a metered API can still hit; code-commented that this depends on urllib's specific behavior, would need revisiting if ever ported to `requests`. Field-name assumption (`symbol`/`sector` keys) is untested against a real response — that's the live-validation row below, genuinely blocked on the key. |
| Sector-name normalization to a canonical GICS set, applied before `EXCLUDED_SECTORS` | done | 2026-07-21 — `_canonicalize_sector()`, with a best-effort alias map still explicitly marked unverified against live FMP data in its own code comment; also hardened to accept non-string input (`float('nan')`) without raising, after code-reviewer found a blank/NaN sector cell would otherwise discard an entire index's live fetch over one bad row. |
| Generalize `get_universe()`: build + fixture-based unit tests, combine/dedupe all three sources | done | 2026-07-21 — implemented and unit-tested against fixtures; also restructured per staff-engineer-reviewer finding: the fallback-load call was originally reachable from both the validation-failure branch *and* the exception handler, so a broken fallback file would be silently retried and its failure misattributed to "the live fetch failed twice." Split into `_fetch_and_validate_index()` (returns `None` on any live-fetch problem) and `_fetch_index()` (calls the fallback only once, outside that function's exception boundary), so a genuinely broken fallback file now fails loudly and distinctly instead. |
| Live full-composite validation run (all three sources, real network) | blocked | needs the FMP key above. Done means a live run returns ~1500 tickers total, verified by count **and** by spot-checking known names from *all three* indices (e.g. AAPL/MSFT present via FMP, count near 503; plus known MidCap/SmallCap names). Partial smoke test already done without a key: 400/600 fetch live successfully (1505 = 1506 combined − 1 real cross-index duplicate, BTSG, confirmed live in both the 400 and 600 Wikipedia pages); 500 correctly falls back with a high-priority log; sector exclusion verified end-to-end (excluding Financials drops JPM, keeps AAPL, across the combined universe). |
| One-time cross-check: FMP's S&P 500 output vs. the existing Wikipedia S&P 500 scrape, same day | blocked | needs the FMP key above (moved from `todo`, since it inherently needs live FMP data) — cheap corroboration for a brand-new dependency feeding real-money decisions, and relevant to the 500→400→600 dedup precedence rule: FMP-sourced data wins ticker/sector conflicts on the assumption it's authoritative for the 500, but it's also the *least* field-validated of the three sources until this check is run at least once. |
| `config.py`: `FMP_API_KEY`, per-index validation bands, fallback file path/schema | done | 2026-07-21 — `UNIVERSE_TICKER_COUNT_BANDS` dict keyed by index ("500"/"400"/"600"), `FMP_API_KEY` alongside the Alpaca secrets, `STATIC_UNIVERSE_FALLBACK_PATH` renamed per the row below |
| Static fallback ticker file: rename to `data/universe_fallback.csv`, expand to full composite with source-index column | done | 2026-07-21 — 1506 rows (503 + 400 + 603) generated from live Wikipedia scrapes of all three index pages, `source_index` column added. Confirmed live: exactly one cross-index duplicate exists in the real data today (BTSG, in both 400 and 600) — the dedup logic this milestone added is not theoretical. Still subject to the pm-reviewer sequencing note: the 400/600 rows and `source_index` scheme aren't final until the FMP-vs-Wikipedia cross-check (blocked row above) has run at least once. |
| Ticker normalization (`BRK.B` → `BRK-B`) | done | 2026-07-21 |
| Optional sector-exclusion list | done | 2026-07-21 — re-verified live post-generalization: excluding Financials correctly drops JPM and keeps AAPL across the full combined S&P 1500 universe (1246 tickers with the exclusion applied). `_load_static_fallback()` was refactored to call the same `_apply_sector_exclusions()` the live path uses (was a second, independently-drifting inline implementation before code-reviewer flagged it as untested), with a new test proving the fallback path excludes JPM too. |
| Scrape/fetch validation (row-count/format sanity check) + fallback on validation failure | done | 2026-07-21 — validates the *raw* result before sector exclusion, not after (validating post-exclusion was a real bug found live in the single-index version: any configured exclusion would make the count fall outside the band and force fallback every run). Per-index bands and per-index fallback slicing now implemented and tested (`test_validate_universe_uses_per_index_band` is a direct regression test for this). |
| Widen exception handling to cover the whole pipeline (fetch → validate → exclude → normalize), not just the fetch call | done | 2026-07-21 — found by staff-engineer-reviewer in the single-index version: a missing/renamed GICS Sector column would raise uncaught inside `_apply_sector_exclusions`, crashing instead of falling back. Applied to the generalized version and covered by `test_get_universe_falls_back_on_missing_sector_column`. |
| Unit tests: `_apply_sector_exclusions` (pure function) | done | 2026-07-21 — in-memory DataFrame fixtures, including a canonicalization case (FMP-style "Financial Services" alias must still be caught by a "Financials" exclusion) |
| Unit tests: `get_universe()` branches via monkeypatching each fetch call | done | 2026-07-21 — covers: FMP raises → fallback; FMP returns an error body → fallback; an index fails validation → that index falls back while the others stay live; missing sector column → fallback (regression test); all three succeed live |
| Unit tests: cross-index de-duplication | done | 2026-07-21 — a ticker shared between two fixture lists collapses to one entry in the combined result |
| `requirements.txt`: pin `lxml` | done | 2026-07-21 — critical finding, independently raised by both code-reviewer and staff-engineer-reviewer: `pd.read_html()` requires `lxml` or `html5lib`+`beautifulsoup4` as an optional pandas dependency, and neither was declared anywhere in `requirements.txt`. On a clean install this would make every Wikipedia fetch (the S&P 400/600 slice, ~1000 of ~1500 tickers) silently and permanently fall back to the static file, with only a generic "fetch failed" warning masking the real cause. |

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
| Startup check: `FMP_API_KEY` present and valid (cheap smoke request), abort if not | todo | added 2026-07-21 alongside the S&P 1500 hybrid-sourcing decision — a known-bad FMP key should abort before the run starts, same posture as the Alpaca paper/live assertion above, rather than surface for the first time as a silent mid-run universe-module fallback (DESIGN.md §4) |
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
| Alert on per-index universe fallback (S&P 500/400/600), severity tiered by consequence | todo | added 2026-07-21 with the S&P 1500 hybrid-sourcing decision — a fallback on the FMP-sourced S&P 500 is high-priority (largest, most consequential slice); a fallback on the Wikipedia-sourced 400/600 is normal priority (DESIGN.md §3.1/§3.6); verify by forcing a fallback on each source and confirming the alert priority differs, not just code review |
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
