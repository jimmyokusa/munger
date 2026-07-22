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
— see DESIGN.md §3.1.

**Sourcing history (all resolved 2026-07-21):** three alternatives to a
pure Wikipedia scrape were evaluated for the S&P 500 leg and rejected,
in order: (1) ETF-issuer holdings files (SLY 404s, SPY's Sector column
blank); (2) Finnhub (no S&P 400/600 constituents endpoint at any price);
(3) Financial Modeling Prep's `sp500_constituent` REST API — initially
adopted as a hybrid (FMP for 500, Wikipedia for 400/600, since the user
acts on this system's output with real money and API sourcing looked
like the more reliable option for the largest slice), fully implemented
and reviewed, but then found **live** to require a paid plan: the
legacy endpoint (`/api/v3/`) was sunset 2025-08-31 (`HTTP 403`), and the
current endpoint (`/stable/sp500-constituent`) returned `HTTP 402`
("Restricted Endpoint... upgrade your plan") even with a real, valid key.
User declined to pay for FMP's Starter plan (~$19/mo) — **reverted to a
uniform Wikipedia scrape for all three indices**, the same hardening
(per-index validate-and-fallback, dedup, alert-severity tiering) applied
throughout, just without a second source technology to reconcile.

**Fallback is per-index, not all-or-nothing** (DESIGN.md §3.1): each of
the three indices is fetched/validated/falls-back independently, so the
static fallback file needs a source-index column to slice from, and each
per-index fallback gets its own alert (DESIGN.md §3.6) rather than
relying on the aggregate ticker count, which would still look healthy.
Combined results are de-duplicated by ticker (500 → 400 → 600 precedence
on conflict) to handle the rare index-reclassification window where a
ticker briefly appears in two source lists at once — confirmed live: a
real ticker (BTSG) currently appears on both the S&P 400 and 600
Wikipedia pages.

**Fully shipped 2026-07-21**, reviewed by `code-reviewer` (substituting
for `python-reviewer`, which turned out to have the same workspace-root
agent-discovery scoping issue as the hooks — copied to
`/Users/jimmyok/workspace/.claude/agents/` for next session),
`staff-engineer-reviewer`, and `pm-reviewer`, across both the original
hybrid-sourcing pass and the FMP-to-Wikipedia revert. All findings fixed
before merge (see per-row notes below); every task is unblocked and
verified live — no external dependency left to provision.

| Task | Status | Date / Notes |
|---|---|---|
| `get_universe()`: fetch, validate, exclude, normalize, combine, and de-duplicate all three indices via Wikipedia | done | 2026-07-21 — required a custom User-Agent header (default urllib UA gets HTTP 403 from Wikipedia's bot policy); found live, not theoretically. All three indices share one `_fetch_wikipedia_index(index)` function; the FMP-specific `_fetch_fmp_sp500()` path built earlier was removed on revert (see sourcing history above), returning the module to a single source technology. |
| Sector-name handling | done | 2026-07-21 — `_canonicalize_sector()` trims whitespace and tolerates non-string input (`float('nan')`) without raising, after code-reviewer found a blank/NaN sector cell would otherwise discard an entire index's live fetch over one bad row. The FMP-vs-Wikipedia alias-mapping table built during the hybrid-sourcing phase was removed on revert — unverified guesses about a vendor's field format that's no longer in use would have been dead, misleading code. |
| Live full-composite validation run (all three sources, real network) | done | 2026-07-21 — live run returns 1505 tickers (1506 combined − 1 real cross-index duplicate, BTSG), all three indices fetched live with zero fallback triggers; sector exclusion verified end-to-end (excluding Financials drops JPM, keeps AAPL, across the combined universe: 1246 tickers with the exclusion applied). |
| `config.py`: per-index validation bands, fallback file path/schema | done | 2026-07-21 — `UNIVERSE_TICKER_COUNT_BANDS` dict keyed by index ("500"/"400"/"600"). `FMP_API_KEY` was added during the hybrid-sourcing phase and removed on revert. |
| Static fallback ticker file: `data/universe_fallback.csv`, full composite with source-index column | done | 2026-07-21 — 1506 rows (503 + 400 + 603) generated from live Wikipedia scrapes of all three index pages, `source_index` column added. Confirmed live: exactly one cross-index duplicate exists in the real data today (BTSG, in both 400 and 600) — the dedup logic this milestone added is not theoretical. |
| Ticker normalization (`BRK.B` → `BRK-B`) | done | 2026-07-21 |
| Optional sector-exclusion list | done | 2026-07-21 — verified live: excluding Financials correctly drops JPM and keeps AAPL across the full combined S&P 1500 universe. `_load_static_fallback()` calls the same `_apply_sector_exclusions()` the live path uses (was a second, independently-drifting inline implementation before code-reviewer flagged it as untested), with a test proving the fallback path excludes JPM too. |
| Scrape validation (row-count/format sanity check) + fallback on validation failure | done | 2026-07-21 — validates the *raw* result before sector exclusion, not after (validating post-exclusion was a real bug found live in the single-index version: any configured exclusion would make the count fall outside the band and force fallback every run). Per-index bands and per-index fallback slicing implemented and tested (`test_validate_universe_uses_per_index_band` is a direct regression test for this). |
| Widen exception handling to cover the whole pipeline (fetch → validate → exclude → normalize), not just the fetch call | done | 2026-07-21 — found by staff-engineer-reviewer in the single-index version: a missing/renamed GICS Sector column would raise uncaught inside `_apply_sector_exclusions`, crashing instead of falling back. Covered by `test_get_universe_falls_back_on_missing_sector_column`. |
| Fallback-of-the-fallback isolation | done | 2026-07-21 — the fallback-load call was originally reachable from both the validation-failure branch *and* the exception handler, so a broken fallback file would be silently retried and its failure misattributed to "the live fetch failed twice." Split into `_fetch_and_validate_index()` (returns `None` on any live-fetch problem) and `_fetch_index()` (calls the fallback only once, outside that function's exception boundary), so a genuinely broken fallback file now fails loudly and distinctly instead. |
| Unit tests: `_apply_sector_exclusions` (pure function) | done | 2026-07-21 — in-memory DataFrame fixtures, including a whitespace-trimming case |
| Unit tests: `get_universe()` branches via monkeypatching the fetch call | done | 2026-07-21 — covers: an index raises → that index falls back while the others stay live; an index fails validation → falls back; missing sector column → falls back (regression test); all three succeed live |
| Unit tests: cross-index de-duplication | done | 2026-07-21 — a ticker shared between two fixture lists collapses to one entry in the combined result |
| `requirements.txt`: pin `lxml` | done | 2026-07-21 — critical finding, independently raised by both code-reviewer and staff-engineer-reviewer: `pd.read_html()` requires `lxml` or `html5lib`+`beautifulsoup4` as an optional pandas dependency, and neither was declared anywhere in `requirements.txt`. On a clean install this would make every Wikipedia fetch silently and permanently fall back to the static file, with only a generic "fetch failed" warning masking the real cause. |

## M2 — Data fetcher

**Environment note:** this machine's default `python3` is 3.9.6, but
`yfinance`'s pinned dependency `curl_cffi>=0.15` has no wheels for 3.9 and
DESIGN.md §7 specifies Python 3.11+ anyway. Installed Python 3.11 via
Homebrew (`/opt/homebrew/bin/python3.11`) and created a project venv at
`munger/.venv` (already gitignored from M0) — `source .venv/bin/activate`
before running `pytest`/`ruff`/`mypy` in this repo from now on.

| Task | Status | Date / Notes |
|---|---|---|
| `fetch_metrics(symbol)` via yfinance | done | 2026-07-21 — returns `Metrics \| None`: `None` only if every retry fails outright (bad/delisted symbol, network error); a successful fetch with individual fields missing returns a `Metrics` record with those fields `None`, not a failure — that distinction is what M3's `data_missing:<field>` tagging needs downstream. |
| Thread pool (~10–15 workers) | done | 2026-07-21 — `fetch_all_metrics()`, `concurrent.futures.ThreadPoolExecutor(max_workers=config.DATA_FETCH_THREAD_POOL_WORKERS)` |
| Retry-with-backoff on transient/rate-limit errors | done | 2026-07-21 — exponential backoff with jitter (`config.DATA_FETCH_RETRY_BACKOFF_SECONDS * 2**attempt`, plus up to 25% jitter), up to `config.DATA_FETCH_MAX_RETRIES`. Changed from a flat fixed delay per staff-engineer-reviewer: with 12 concurrent workers all hitting a rate limit around the same time, a fixed delay would retry them all in near-lockstep, recreating the burst that caused the 429. Verified live against a real invalid ticker (3 attempts, then `None`) — that exercises the permanent-failure path, not an actual transient/rate-limit recovery, which can't be reliably forced against a live third-party service; recovery is covered by a mocked test (`test_fetch_metrics_retries_then_succeeds`) instead. |
| Malformed field values (e.g. non-numeric `debtToEquity`) must not fail the whole ticker | done | 2026-07-21 — staff-engineer-reviewer finding: `_normalize_percent_field`'s `float()` conversion originally ran inside `fetch_metrics`'s retry-catching `try` block, so one bad field would burn all 3 retries and discard every other successfully-fetched field on that ticker. Now caught locally, logged, and treated as a missing field (`None`), not a fetch failure. |
| Batch-level timeout so a hung worker can't block the whole run forever | done | 2026-07-21 — staff-engineer-reviewer finding: neither `fetch_metrics` nor `future.result()` had any timeout, so a single stalled connection (no exception, just hangs) could block `fetch_all_metrics` indefinitely. `concurrent.futures.wait(..., timeout=config.DATA_FETCH_BATCH_TIMEOUT_SECONDS)` now bounds the wait; any still-pending ticker is reported as failed. **Known residual limitations**, not fully solved here: (1) this cannot forcibly kill an already-running worker thread — a stdlib `ThreadPoolExecutor` limitation — so the *logical* result is bounded but the OS process may not exit cleanly until the stuck thread finishes; the outer safety net for that is now its own explicit M11 task, not just a cross-reference. (2) `DATA_FETCH_BATCH_TIMEOUT_SECONDS` (600s) is one fixed ceiling regardless of ticker count — staff-engineer-reviewer noted the 400-ticker/17.5s live test below almost certainly never entered the retry/backoff path (0 failures), so it validates happy-path speed, not whether 600s is actually enough headroom for the full ~1500-ticker universe under real throttling with only 12 workers; a batch that's throttled broadly could push close to or past 600s, converting slow-but-recoverable tickers into `None` indistinguishably from a genuinely hung one. Revisit once a true full-scale (~1500-ticker) live run has been done. |
| Cache the raw response on the rejection path too, not just on success | done | 2026-07-21 — staff-engineer-reviewer finding: the one case an operator is most likely to want to inspect ("why did we reject this ticker?") was the one case that left no cached artifact. `_fetch_raw` now caches before raising on a symbol mismatch, and logs the raw key count for the same reason (see next row). |
| **Batch-scale live test** of `fetch_all_metrics()` against real concurrency | done | 2026-07-21 — a 400-ticker (27% of universe) run showed 0 failures, but a **full 1505-ticker run hit a real, significant bug**: `yfinance.exceptions.YFRateLimitError` on 991/1505 tickers (66%). The rate limit is session/IP-wide, not per-ticker, so each worker's independent backoff didn't help. Fixed with a shared cross-thread cooldown (`data._rate_limited_until`, lock-guarded): any worker hitting the error extends a shared cooldown every worker waits out before its next attempt. Retest: fetch failures dropped to 146/1505 (10%) — a ~85% reduction, and now right at `config.MIN_UNIVERSE_FETCH_FRACTION` (0.90)'s own threshold. Not fully eliminated; `config.DATA_RATE_LIMIT_COOLDOWN_SECONDS` (20s) is a first-pass value, not exhaustively tuned. **pm-reviewer flagged this has zero margin**: 10% failure sits exactly at `MIN_UNIVERSE_FETCH_FRACTION`'s 0.90 abort line, so nothing currently forces a retune before that check (M10) actually fires at full scale — treat the first real M10 abort as the signal to increase the cooldown, not a surprise. |
| **Real finding, live-verified:** a non-numeric field value crashed the entire run, not just one ticker | done | 2026-07-21 — the full-scale run above also hit `TypeError: bad operand type for abs()` mid-run and aborted completely: only the two percent-scaled fields (`debtToEquity`, `dividendYield`) had defensive type coercion (`_normalize_percent_field`); every other numeric field (`trailingPE`, `marketCap`, etc.) was trusted raw from yfinance. Generalized into `_coerce_float()`, applied to every numeric `Metrics` field. Also added defense-in-depth in `screener.run_screen()`: an unexpected exception computing one ticker's gates/score is now caught and logged (`data_invalid_outlier:unhandled`) rather than aborting the whole batch — the same per-ticker isolation DESIGN.md 3.2 requires at the fetch layer, now applied at the screening layer too. |
| Per-ticker failure tolerance (no run-abort) | done | 2026-07-21 — `fetch_all_metrics()` maps each symbol to `Metrics \| None` independently; a `None` for one ticker (or even an unexpected exception escaping `fetch_metrics`, which shouldn't happen but is caught defensively) never aborts the batch |
| Raw response caching per run | done | 2026-07-21 — `data_cache/<SYMBOL>.json` per ticker, overwritten each run (debugging aid, not a permanent audit trail — that's `journal.db`, M9); gitignored |
| Unit tests: pure functions (`_normalize_percent_field`, `_consecutive_positive_years`) | done | 2026-07-21 |
| Unit tests: `_fetch_raw` invalid-symbol detection and income-statement-failure tolerance | done | 2026-07-21 — via a fake `yf.Ticker` monkeypatched onto the `yfinance` module directly (not `data.yf`, to keep mypy's implicit-reexport check happy) |
| Unit tests: `fetch_metrics`/`fetch_all_metrics` retry, success, missing-field, and batch-tolerance branches | done | 2026-07-21 |
| **Real finding, live-verified:** yfinance's raw fields use inconsistent units | done | 2026-07-21 — `returnOnEquity`/`grossMargins`/`operatingMargins` are already decimal fractions (0.15 = 15%), but `debtToEquity`/`dividendYield` are raw percentage numbers (79.5 = 79.5%), confirmed against AAPL/KO/T/MSFT/JPM. `Metrics` normalizes both to decimal fractions so `config.py`'s all-decimal-fraction thresholds (`MAX_DEBT_TO_EQUITY = 1.0`, etc.) apply consistently — this would have been a silent, systematic Graham-gate miscalculation in M4 if not caught here. Test coverage strengthened per pm-reviewer: the original test only asserted the two converted fields, not that ROE/margins pass through *unchanged* — a regression that started dividing those by 100 too would have gone uncaught. Also confirmed `config.py`'s `MAX_PLAUSIBLE_DEBT_TO_EQUITY = 100` was always ratio-scale (matching `MAX_DEBT_TO_EQUITY = 1.0`'s units), not a stale raw-percentage assumption — added an explicit comment there so the convention isn't ambiguous for whoever implements M3. |
| **Real finding, live-verified:** yfinance doesn't raise for an invalid ticker | done | 2026-07-21 — returns a near-empty `info` dict (`{"trailingPegRatio": None}`, 1 key, vs. ~170+ for a real ticker) instead of an exception. `_fetch_raw` detects this explicitly via `info.get("symbol") != symbol` rather than relying on an exception that never comes. Staff-engineer-reviewer noted this same near-empty-dict shape can also occur under rate-limiting, not just a genuinely bad symbol, so retrying it is still correct — but the two causes were indistinguishable in logs; `_fetch_raw` now logs the raw key count and caches the raw response on rejection (see below) so an operator can tell them apart after the fact. |
| **Real finding, live-verified:** financial-sector tickers commonly lack `marketCap`/`debtToEquity` in yfinance | done | 2026-07-21 — confirmed for JPM (166 keys returned, neither present) and reproduced again in the batch-scale test below (e.g. `AAL`'s `debt_to_equity`). Not a bug — legitimate missing data, correctly surfaces as `None` fields today and will become `data_missing:market_cap`/`data_missing:debt_to_equity` once M3's `validate_metrics` lands. **Follow-up decision point flagged for M5 (pm-reviewer):** `EXCLUDED_SECTORS = ()` is the current live default, so this isn't hypothetical — financial-sector names will fail Graham gates on missing data rather than on substantive criteria unless `EXCLUDED_SECTORS` is set to exclude Financials. M5's tuning-pass task should explicitly decide (and record) whether that's the intended behavior or whether Financials should be excluded by default. |

## M3 — Data validation layer

Landed together with M4/M5 in one MVP-pace push, `data.py`/`screener.py`,
single review round (see [[munger-mvp-pacing]]).

| Task | Status | Date / Notes |
|---|---|---|
| `validate_metrics(metrics) -> list[str]` sanity/outlier checks | done | 2026-07-21 — every `Metrics` field except `symbol` checked for `None`; `trailing_pe`/`debt_to_equity` also outlier-checked (`abs(value) > MAX_PLAUSIBLE_*`) |
| Distinct `data_missing:<field>` fail code | done | 2026-07-21 |
| Distinct `data_invalid_outlier:<field>` fail code | done | 2026-07-21 |

## M4 — Graham gate logic

| Task | Status | Date / Notes |
|---|---|---|
| `pass_graham_gates(metrics)` — 7 criteria (DESIGN.md §3.3 Stage 1) | done | 2026-07-21 — starts from `validate_metrics()`'s reasons, so it's correct even called standalone |
| Returns `(passed, fail_reasons)` | done | 2026-07-21 |
| Thresholds pulled from `config.py` | done | 2026-07-21 |
| Unit tests against hand-computed fixtures (DESIGN.md §6, layer 1) | done | 2026-07-21 — one fixture per gate + combined pass/fail case |
| **Real bug found (staff-engineer-reviewer):** negative `debt_to_equity` silently passed the debt gate and scored as "better than zero debt" | done | 2026-07-21 — same trap already guarded for `trailing_pe` (negative numerically compares less than a positive max threshold) but missed for D/E. A company with negative book equity would have ranked competitively for the eventual buy queue. Fixed in both the gate and `calculate_munger_score`'s low-debt component; reviewed again to confirm no other field has the same shape (none found: ROE/margins/FCF are `< MIN` checks, which correctly fail on negative input by construction). |

## M5 — Munger scorer

| Task | Status | Date / Notes |
|---|---|---|
| `calculate_munger_score(metrics)` | done | 2026-07-21 — 0-100 weighted composite, each component clamped to [0,1] before weighting |
| `run_screen(tickers) -> DataFrame` | done | 2026-07-21 — also wraps each ticker's gate/score computation in a try/except (defense in depth, see M2's crash finding above) |
| `screen_results.csv` output | done | 2026-07-21 — key columns (symbol, buyable, score, fail_reasons) first, sorted by score |
| Unit tests for `calculate_munger_score` against hand-computed fixtures (DESIGN.md §6, layer 1) | done | 2026-07-21 — one fixture per weighted component, a clamp-above-target case, a combined-100 case, and a negative-D/E regression case |
| `screen-sanity-reviewer` subagent — reads `screen_results.csv` and flags anything that looks like a data-error artifact rather than a real high-quality compounder | todo | scoped as a plausibility/sanity checker, not stock-picking advice — same lane as the deterministic gates, formalizes the "eyeball test" DESIGN.md §6 already calls for as a manual step |
| Tune thresholds against live data (~20–50 buyable names) | todo | **First live full-universe run: 7/1505 buyable** (well below the 20-50 target band) — see DESIGN.md's own build-order note ("zero or 300 means a threshold is wrong") for why this is expected before tuning, not a bug. Failure breakdown (of 1505): `graham_pe_times_pb` 861, `munger_roe` 764, `graham_pe` 723, `graham_current_ratio` 611. Spot-checked several real tickers (CB, JNJ, KO) against known-real numbers to confirm this isn't a data bug — today's market genuinely has elevated valuations (matches the user's own earlier observation). **Not yet done**: an actual tuning pass adjusting `config.py` thresholds; this needs the user's input on how aggressive to be, since loosening Graham's valuation gates is an investment-philosophy decision, not a pure engineering one. **Explicitly decide the `EXCLUDED_SECTORS`/Financials question flagged in M2**: yfinance commonly lacks `marketCap`/`debtToEquity` for financial-sector tickers, so with the current `EXCLUDED_SECTORS = ()` default those names fail on missing data rather than substantive criteria — decide and record whether that's acceptable. |

## M6 — Strike state machine

Implemented in `portfolio.py` alongside M7, single review round (MVP pace).

| Task | Status | Date / Notes |
|---|---|---|
| `StateTracker` — strike counters only, atomic writes to `state.json` | done | 2026-07-21 — never holds current holdings; corrupt/unreadable file falls back to empty state (fail-safe, not fail-dangerous: worst case is a missed liquidation, not a wrongful one) |
| `process_sells(current_holdings, new_market_data, state)` | done | 2026-07-21 — `current_holdings: dict[str, float]` (ticker -> market value), fetched live from broker by the caller, not read from state; `new_market_data: dict[str, Metrics \| None]` |
| Two-strike rule (missing-ticker-as-strike, reset-on-clean) | done | 2026-07-21 |
| **Real bug found (staff-engineer-reviewer):** strike counters never cleared on liquidation | done | 2026-07-21 — a ticker's strike count survived leaving `current_holdings`; if ever re-bought, it would inherit the stale count and could liquidate after just one bad check instead of two. Fixed: `process_sells` now resets strikes to zero at the moment it adds a ticker to `to_liquidate`, so a future re-buy starts a fresh streak. Regression test added. |
| Reconciliation warning on live-vs-expected holdings divergence | todo | needs the trade journal (M9) to know what a prior run "expected" — not implemented yet, tracked here explicitly rather than silently skipped |
| Unit tests for the strike/reset state machine against hand-computed fixtures (DESIGN.md §6, layer 1) | done | 2026-07-21 — covers: one strike then clean (resets), two consecutive strikes (liquidates), missing ticker (counts as strike), `None` metrics (counts as strike), corrupt state file, save/reload roundtrip |

## M7 — Buy queue / target construction

| Task | Status | Date / Notes |
|---|---|---|
| `generate_buy_queue(current_holdings, screen_results, available_cash)` | done | 2026-07-21 |
| ~1/15 target weights, 12% single-name cap, 2% cash buffer | done | 2026-07-21 — per-position cap is `min(portfolio_value/TARGET_POSITION_COUNT, portfolio_value*MAX_SINGLE_POSITION_WEIGHT)`; under default config the 1/15 target (~6.7%) never actually hits the 12% cap, so a dedicated test forces a small `TARGET_POSITION_COUNT` to exercise the cap explicitly |
| Top up existing positions before opening new ones | done | 2026-07-21 |
| $50 dust filter | done | 2026-07-21 — applied to both top-up and new-position orders |
| Never sell-to-buy (no churn) | done | 2026-07-21 — trivially true by construction: this function has no sell code path at all, sells are entirely `process_sells`'s responsibility with no shared state between the two beyond `StateTracker` |
| **Live-verified** against the real M5 screen output | done | 2026-07-21 — starting from an empty portfolio with $100k cash and the real 7-buyable screen result, correctly generated 7 orders at ~$6,667 each (1/15 of equity), no cap/dust issues at this scale |

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
| Exclude `process_sells`'s `to_liquidate` tickers from `generate_buy_queue`'s `current_holdings` input | todo | added 2026-07-21 (staff-engineer-reviewer, M6/M7 review) — `generate_buy_queue` has no visibility into what `process_sells` decided to liquidate this run; without this, a position slated for sale could get topped up in the same run it's about to be closed. Documented as a caller contract in both functions' docstrings; this is where it actually needs enforcing. |
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
| Outer process-level timeout wrapping the whole `python bot.py` invocation | todo | added 2026-07-21 — M2's `fetch_all_metrics()` bounds its own *logical* wait via `concurrent.futures.wait(timeout=...)`, but can't force-kill an already-hung worker thread (stdlib `ThreadPoolExecutor` limitation, see `data.py`'s docstring); a genuinely stuck fetch could leave the OS process itself alive past its logical return. Needs an outer safety net at the scheduler layer — e.g. GitHub Actions' `timeout-minutes:`, or a shell `timeout` wrapper for a cron invocation — not solvable from inside `bot.py`. (Flagged by pm-reviewer: M2 referenced this as "tracked for M11" before this row existed — now it actually is.) |
| Same-day idempotency via `client_order_id` + today's orders/positions | todo | |
| Alert on run failure | todo | verify by forcing a failure, not just code review |
| Alert on empty universe fetch | todo | verify by forcing an empty fetch, not just code review |
| Alert on per-index universe fallback (S&P 500/400/600), severity tiered by consequence | todo | added 2026-07-21 with the S&P 1500 broadening — a fallback on the S&P 500 is high-priority (largest, most consequential slice); a fallback on the 400/600 is normal priority (DESIGN.md §3.1/§3.6); verify by forcing a fallback on each index and confirming the alert priority differs, not just code review |
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
