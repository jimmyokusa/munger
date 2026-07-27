# Graham–Munger Buy-and-Hold Bot — System Design

A specification for an automated investing system that combines Benjamin
Graham's margin-of-safety discipline from *The Intelligent Investor* with
Charlie Munger's quality-first philosophy. This document is
implementation-ready: hand it to a coding agent and build module by module.

## 1. Investment Philosophy (the "why" behind every design decision)

The system encodes two thinkers whose ideas complement each other:

Graham supplies the entry discipline. A stock may only be bought when it is
quantitatively cheap and financially sound: adequate size, strong balance
sheet, a multi-year record of positive earnings, and a price that is
moderate relative to earnings and book value. Graham's Mr. Market allegory
also dictates a core rule: price movements are never signals. A holding that
has fallen is not a sell; a holding that has risen is not a sell. Only
deteriorating business fundamentals are.

Munger supplies the quality filter and the temperament. "A wonderful company
at a fair price beats a fair company at a wonderful price." Among stocks
that pass Graham's cheapness tests, the system prefers businesses with high
returns on capital, fat gross margins (a proxy for pricing power and moats),
positive free cash flow, and low debt. Munger's second contribution is
behavioral: concentration over di-worse-ification (~15 positions, not 50),
and a heavy bias toward inaction — "the big money is not in the buying and
selling, but in the waiting." Turnover is treated as a tax on compounding.

The resulting system is therefore not a trading bot in the conventional
sense. It is a screener plus an executor plus a very reluctant seller. The
orchestration itself now runs daily (§4) so the portfolio checks its
current results against fresh data every day, but a rebalancing tolerance
band (§3.4) means the *decision to trade* stays rare in practice: Graham's
gates and Munger's floors are anchored to quarterly-cadence fundamentals
(10-Qs), not daily price action, so a holding's target weight barely moves
day to day, and most runs should still place zero orders of either kind —
checking daily does not mean trading daily.

## 2. High-Level Architecture

```
┌────────────────────────────────────────────────────────────┐
│                        SCHEDULER                            │
│         (cron / GitHub Actions — daily cadence)             │
└───────────────────────────┬──────────────────────────────────┘
                            ▼
┌────────────────────────────────────────────────────────────┐
│  1. UNIVERSE MODULE      S&P 500 constituents,               │
│                          optional sector exclusions          │
├────────────────────────────────────────────────────────────┤
│  2. DATA MODULE          fundamentals per ticker              │
│                          (yfinance → later: paid API)        │
├────────────────────────────────────────────────────────────┤
│  3. SCREENER             Graham pass/fail gates               │
│                          + Munger 0–100 quality score         │
├────────────────────────────────────────────────────────────┤
│  4. PORTFOLIO ENGINE     target weights, sell discipline,     │
│                          buy queue                            │
├────────────────────────────────────────────────────────────┤
│  5. EXECUTION MODULE     Alpaca API (paper first),             │
│                          fractional-share market orders       │
├────────────────────────────────────────────────────────────┤
│  6. STATE & AUDIT        state.json, screen_results.csv,       │
│                          append-only trade journal, logs      │
└────────────────────────────────────────────────────────────┘
```

Each module has a single responsibility and a clean interface, so each can
be built and tested independently.

## 3. Module Specifications

### 3.1 Universe Module

Provides the list of candidate tickers. **Default universe is the S&P
Composite 1500** (S&P 500 + S&P MidCap 400 + S&P SmallCap 600) — broadened
from a large-cap-only S&P 500 during M1 implementation, once it was clear
that restricting to the 500 leaves real bargains on the table: S&P 500
membership is an index-committee/market-cap-rank decision, not a cheapness
signal, and the $2B–$18B range already inside this system's own
`MIN_MARKET_CAP` gate (§3.3) is mostly *outside* the S&P 500 but squarely
inside the MidCap 400's territory. Graham's "large, prominent,
conservatively financed" requirement and Munger's circle-of-competence
rule are both still enforced structurally — not by index membership, but
by `MIN_MARKET_CAP` staying a hard gate (so true micro-caps are excluded
regardless of which index list they came from) and by all three sources
being maintained, liquid, analyst-covered indices rather than an
unfiltered exchange-wide scan.

**Sourcing is uniform: all three indices are scraped from Wikipedia.**
Two API alternatives were evaluated and rejected for the S&P 500 leg
specifically (the largest, most consequential slice, so it's the one
worth the most scrutiny): **Financial Modeling Prep's `sp500_constituent`
REST API** looked promising — a real, versioned, authenticated endpoint,
no HTML parsing — but live testing found the endpoint gated behind a
paid plan (`HTTP 402`) on both the legacy (`/api/v3/`, sunset 2025-08-31,
`HTTP 403`) and current (`/stable/`) paths, not included in the free
tier the project uses; **Finnhub** and **ETF-issuer holdings files**
(e.g. State Street's SPY/MDY/SLY daily holdings) don't offer a
constituents endpoint for the S&P 400/600 at any price point checked
either, and the ETF files specifically had two more problems: not every
fund publishes at a predictable URL (SLY's file could not be located),
and the ones that did publish (SPY) left the `Sector` column blank for
every row. Paying for FMP's Starter plan was considered and declined —
not worth the ongoing cost for a personal project when the Wikipedia
path, once hardened, is adequate. This is revisited if a suitable free
or low-cost vendor is found later; until then, the risk on all three
indices is bounded and made visible (see fallback/alerting below) rather
than eliminated.

The module scrapes each of the three constituent lists from Wikipedia
(same table structure and `GICS Sector` column across all three pages —
`List of S&P 500 companies`, `List of S&P 400 companies`, `List of S&P
600 companies` — confirmed live), normalizes ticker symbols to the
broker's format (e.g., `BRK.B` → `BRK-B`), and applies an optional
configured list of excluded sectors — Munger's circle-of-competence rule
made concrete: if you don't understand banks, exclude Financials. The
scrape requires a descriptive User-Agent header — the default
`urllib`/`pandas.read_html` User-Agent gets an HTTP 403 from Wikipedia's
bot policy; a request identifying the bot (not spoofing a browser) is
both required to get past it and the compliant way to do so.

The scrape can fail two different ways: it can raise (network error,
Wikipedia down), or it can *succeed with a corrupted result* — a page
restructure that shifts which column holds the ticker symbol (or
renames/drops the sector column entirely) produces a result that parses
cleanly but is garbage, or crashes sector-filtering downstream. Only the
first case is caught by falling back on exception alone — the whole
pipeline (fetch, validate, apply exclusions, normalize) must share one
failure boundary, not just the fetch call, or a sector-column failure
crashes the process instead of falling back. Validate the parsed result
before accepting it — row count within a sane band per index (S&P 500:
~490–510; S&P 400: ~390–410; S&P 600: ~590–615) and every entry matching
a plausible ticker pattern — and fall back to the static file on either
failure mode, not just an outright exception. Validation runs against
each index's *raw* result, before sector exclusions are applied:
exclusions are a legitimate filter that can validly shrink a list below
its sane band, so validating post-filter would make any configured
exclusion look like a fetch failure and force a fallback every run.

**Fallback is per-index, not all-or-nothing.** Each of the three indices
is fetched and validated independently, and each falls back to *its own*
slice of the static file independently — a validation failure on the
SmallCap 600 page does not discard an otherwise-healthy scrape of the 500
or the 400. The single static fallback file therefore needs a
source-index column (not just symbol/sector) so a per-index fallback can
select the right slice. The alternative — one combined static file used
whenever any of the three fails — was considered and rejected: it turns a
single-source hiccup (routine, given three independent scrapes) into
full-composite staleness.

Because a static fallback still returns a plausible-looking, correctly-
sized list, a persistent fallback on just one of the three sources
wouldn't trip the aggregate empty-universe/`MIN_UNIVERSE_FETCH_FRACTION`
check in §5 — the total count still looks healthy. Each per-index
fallback event must therefore be logged and alerted on individually (§3.6/
§5), not folded into the aggregate check alone, or an operator could run
for months on a stale list without any signal. **Alert severity tracks
consequence, not just occurrence:** a fallback on the S&P 500 — the
largest, most heavily-weighted slice — is a high-priority alert
(investigate before the next run acts on it), while a fallback on the
400/600 is logged and alerted at normal priority. This is independent of
which vendor happens to source each index today — severity tracks
portfolio-weight consequence, not sourcing technology.

**Combining and de-duplicating.** S&P 500/400/600 are disjoint by S&P's
own index methodology, but membership changes can lag between the three
Wikipedia pages during a reclassification (e.g., a name moving from
SmallCap 600 to MidCap 400), producing a brief window where a ticker
appears on two pages at once. After validating and normalizing each
index's list independently, `get_universe()` de-duplicates the combined
result by ticker, keeping the first occurrence in 500 → 400 → 600 order
(so the large-cap page's data wins a conflict, including which sector it
reports) rather than fetching or scoring a ticker twice.

Interface: `get_universe() -> list[str]`

### 3.2 Data Module

Fetches per-ticker fundamentals and returns a typed `Metrics` record.
Version 1 uses yfinance (free, best-effort quality). The module must be the
only place that touches the data provider, so swapping in a paid API later
(Financial Modeling Prep, Polygon, Alpha Vantage) is a one-file change.

Required fields per ticker: market cap, trailing P/E, price-to-book,
current ratio, debt-to-equity, return on equity, gross margin, operating
margin, free cash flow, dividend yield, and a count of consecutive years of
positive net income (from annual income statements; yfinance provides
roughly four years).

Design requirements: fetch concurrently (thread pool, ~10–15 workers) with
retry-and-backoff on transient/rate-limit errors before giving up on a
ticker, tolerate individual-ticker failures without aborting the run, treat
missing data as a failed check rather than a pass (conservatism: no data,
no buy), and cache raw responses per run for debuggability.

"No data" and "bad data" are distinct failure classes and must stay
distinguishable downstream, not collapse into the same boolean. A ticker
with no P/E because the provider returned nothing (transient hiccup) and a
ticker with a P/E of 50,000 (implausible value, possibly a data-corruption
signal worth investigating) both end up `buyable=False`, but an operator
reading `screen_results.csv` needs to tell them apart. Tag each with a
distinct fail-reason code — e.g. `data_missing:pe` vs.
`data_invalid_outlier:pe` — carried through to the screener's output (see
§3.3) rather than a single generic "failed validation" flag.

Interface: `fetch_metrics(symbol) -> Metrics | None`

### 3.3 Screener

Two stages, run in order.

**Stage 1 — Graham entry gates** (pass/fail, all required to buy). Adapted
from the defensive-investor criteria in Chapter 14:

| # | Criterion | Graham original | v1 threshold | Rationale |
|---|-----------|------------------|---------------|-----------|
| 1 | Adequate size | large companies | market cap ≥ $2B | avoids fragile small caps |
| 2 | Financial strength | current ratio ≥ 2.0 | ≥ 1.5 | modern balance sheets run leaner |
| 3 | Debt discipline | LT debt ≤ working capital | debt/equity ≤ 1.0 | simpler proxy, same intent |
| 4 | Earnings stability | 10 yrs positive EPS | 4 yrs positive net income | data availability limit in v1 |
| 5 | Dividend record | 20 yrs uninterrupted | optional toggle, off by default | quality overlay substitutes |
| 6 | Moderate P/E | ≤ 15 | ≤ 20 | relaxed because of the quality tilt |
| 7 | Combined multiplier | P/E × P/B ≤ 22.5 | ≤ 30 | Munger pays fair prices for wonderful businesses |

All thresholds live in a single config file. The screener records which
gate failed for every stock — this audit trail matters more than the
pass/fail bit. Fail reasons originating from the data layer (§3.2) keep
their `data_missing:*` / `data_invalid_outlier:*` distinction rather than
being flattened into a generic gate-failure code, so `screen_results.csv`
always shows whether a ticker failed on valuation or on data quality. An
absent dividend is *not* `data_missing` — a `None` `dividend_yield` means
the company simply doesn't pay one, which is valid data, not a fetch gap;
the dedicated `graham_dividend_record` gate (row 5, off by default) is the
only place that judges it.

**Stage 2 — Munger quality floor and score.** Hard floors that must be met
to be `buyable`: ROE ≥ 15%, gross margin ≥ 30%, positive free cash flow.
Every ticker with at least partial fundamentals gets a 0–100 composite
quality score, whether or not it's buyable — score and buyable are
independent signals (score ranks quality across the whole screened
universe; buyable is the pass/fail gate on top of it), not one gating the
other. A ticker that fails a Graham gate or a quality floor can still carry
a real, informative score computed from whichever of its metrics are
present. `score=0.0` is still a real, distinct value, but now means only
"total fetch failure" (`data_missing:fetch_failed`) or an unhandled
computation error (`data_invalid_outlier:unhandled`) — not "didn't pass
the gates." A missing component contributes 0 to the weighted sum rather
than being excluded/renormalized among the components that *are* present,
so a low score can mean either low quality or incomplete data — the
`fail_reasons` column (not the score) is the source of truth for which:

| Component | Weight | Normalization (→ 1.0 at) |
|-----------|--------|--------------------------|
| Return on equity | 30% | 40% ROE |
| Gross margin | 20% | 60% |
| Operating margin | 15% | 35% |
| FCF yield (FCF / market cap) | 20% | 8% |
| Low debt (1 − D/E, clamped) | 15% | zero debt |

Output: a DataFrame of every fetched ticker with `buyable` flag, `score`,
`fail_reasons`, and raw metrics, persisted to `screen_results.csv` on every
run.

Interface: `run_screen(tickers) -> DataFrame`

### 3.4 Portfolio Engine

The heart of the system, and where the philosophy lives. `state.json` holds
only the strike-streak counters — the *only* mutable state the system has
(see §3.6). Current holdings are never read from local state; they are
fetched live from the broker at the start of every run. This keeps the
two-strike logic honest against what is actually held rather than a
potentially stale local copy, and it means a partial fill, a manual
intervention, or a corporate action is reflected automatically on the next
run instead of silently drifting out of sync with a cached snapshot. If the
broker's reported holdings ever diverge from what the previous run's
journal expected to be true, log a reconciliation warning (see §3.6) — this
is the cheapest signal that something upstream (a failed order, a manual
trade, a bug) needs attention.

Three responsibilities:

**Target construction.** Roughly 15 positions, equal-weighted at ~1/15 of
equity each, with a hard cap of 12% in any single name and a 2% cash buffer
left undeployed.

**Sell discipline — the two-strike rule.** On each run, every current
holding is re-checked against the Munger quality floors only. Graham's
entry gates (P/E, P/E×P/B, size) explicitly do NOT apply to holdings — a
stock growing out of "cheap" is success, not a sell signal, and applying
valuation gates to holdings would force the system to sell its winners, the
classic error Munger warns against. A holding that hard-fails quality (ROE,
gross margin, or FCF floor) earns a strike, tracked in `state.json`. One
strike is noise (a bad quarter); a second consecutive strike means the
thesis is broken and the position is liquidated. Any clean check resets the
streak. A ticker missing from the screen (delisting, data failure) counts
as a strike, conservatively.

**Buy queue.** After sells settle, deployable cash = cash − buffer, further
capped at this run's global notional budget (see §5) so a cold start
(zero holdings, many buyable candidates) fills as much of the target
portfolio as the run's budget allows instead of building a queue too large
for any single run to execute — the rest rides in on later runs rather
than the whole run being rejected. Priority order: first top up existing
holdings that sit below target weight, then open new positions from the
top of the score-ranked buyable list until the position count reaches
target or cash runs out. A top-up only fires once a holding has drifted
more than a tolerance band (`REBALANCE_DRIFT_BAND_PCT`, default 10%) below
its own target dollar value — with daily runs, a holding's value moves
with its price every day, and without this band ordinary price noise
alone would trigger a top-up trade most days even though nothing about
the position's standing actually changed. Orders below $50 notional are
skipped as dust regardless. A position that's rallied past target is
never trimmed to rebalance down — the engine never sells one holding to
buy another, and a rally is success, not a sell signal (§1) — the drift
band and the dust filter both apply to buys only.

### 3.5 Execution Module

Thin wrapper around the broker. Version 1 targets Alpaca's paper-trading
API using the `alpaca-py` SDK: notional (dollar-amount, fractional-share)
market orders with a limit-price band, DAY time-in-force, and
`close_position` for liquidations. Every order attempt is wrapped in error
handling so one rejected order never aborts the run. The paper/live flag
lives in config and defaults to paper; the design intent is that live
trading is only enabled after several months of clean paper runs.

Idempotency is enforced primarily by a **deterministic `client_order_id`**
on every submitted order — a hash of run-date + ticker + side (e.g.
`2026q3-AAPL-buy`) — so the broker itself rejects a duplicate submission
from a crashed-and-restarted run. This is a stronger guarantee than a
client-side pre-check alone: checking only "today's open orders" before
submitting misses the case where a run submits a buy, Alpaca fills it
immediately, and the process crashes before the journal write — on restart
the order is no longer open, so a pre-check sees nothing and would
double-buy. The pre-check (today's open orders, today's filled orders, and
current positions) remains as a secondary guard, but `client_order_id` is
the guarantee that actually holds under that failure mode. If the pre-check
query to the broker itself fails or times out, the run **aborts** rather
than proceeding blind — this call must fail closed, since submitting orders
without knowing current state defeats the whole point of the check.

Every order — buys and liquidations alike — carries a limit-price band
(e.g. ±2% of last trade) rather than an unconstrained market order, so a
liquidation firing during a flash-crash or a thin after-hours window can't
execute at an arbitrarily bad price.

Interface: `market_buy(symbol, notional)`, `liquidate(symbol)`

### 3.6 State, Audit, and Observability

Three artifacts per run: `state.json` (the strike streaks — the only
mutable state the system has), `screen_results.csv` (full screen output,
timestamped copies kept), and an append-only trade journal recording every
order with its reason string (e.g., `NEW_POSITION score=78.2` or `SELL
strikes=2 reasons=roe_floor,fcf_floor`). The journal is **SQLite**, not
CSV — idempotency (§3.5) now depends on reading it reliably to determine
what already happened today, and a CSV append torn by a crash mid-write
(truncated last line) is exactly the kind of corruption that would make
that check silently unreliable. Structured logging to file and stdout.

Beyond passive logging, active alerts fire rather than waiting to be
discovered by reading the journal: any `SELL`/liquidation event (the
system is designed to place near-zero sells, so one firing is the rarest
and highest-signal thing it does — see §1); a holdings-reconciliation
mismatch (§3.4) between what the broker reports and what the previous
run's journal expected; and, per-index, whenever the universe module
(§3.1) falls back to its static file for the S&P 500, 400, or 600 slice —
a per-index fallback keeps the aggregate ticker count looking healthy, so
without its own distinct alert a stale slice could persist for months
unnoticed. The journal is what lets you later evaluate
whether the system is actually following its own rules — an audit of
behavior, in the Graham spirit of the investor's chief problem being
himself.

### 3.7 HTML Report

A small, static reporting layer (`report.py`) on top of §3.6's own
artifacts — reads `screen_results.csv` and the journal, writes plain
HTML files (`index.html`, `tickers.html`) to a `report/` directory; no
server, no build step, no new dependency to run or maintain, matching
this project's low-operational-overhead philosophy for something that's
only regenerated a handful of times a year. `index.html` shows the
current picks (the journal's most-recent-buy-per-symbol), each with an
expandable panel (native HTML `<details>`, no JS needed for that part)
showing the recorded reason and the metrics that drove its score.
`tickers.html` shows every other screened ticker in a sortable,
filterable table (vanilla JS, no framework), so "why wasn't X picked" is
answerable directly from the same run's data. Not wired into `bot.py`'s
own run — generating a report is a display concern, not a trading
decision, and re-running it costs nothing if the underlying CSV/journal
haven't changed.

### 3.8 P&L Tracking (2026-07-27, user request)

"Track profit and loss... for the moment just the paper trading, but
extensible for real money in the future." A new module, `pnl.py`,
read-only against the broker (never places or modifies an order) — fetches
one point-in-time snapshot (account equity/cash, every open position's
unrealized P&L, a portfolio-value history) directly from Alpaca's own
account API rather than computing P&L from `journal.db`, since the journal
records order *notional* (what was requested), not fill price or current
market value — Alpaca's own numbers are the source of truth here, not a
derived approximation. "Extensible to real money" falls out for free:
`pnl.py` reads whichever account `config.PAPER_TRADING`/`ALPACA_API_KEY`
actually point at and labels the snapshot `"paper"` or `"live"`
accordingly — the same account-agnostic pattern `execution.py` already
uses, nothing paper-specific baked in.

**The cross-system bridge this needs, and why:** `report.py`'s own
deployment (Cloud Run/k3s) deliberately never has Alpaca credentials
(§3.5, M14's screen-only boundary) — that's an intentional security
property, not an oversight, so P&L data can't be fetched from where the
public report is generated. `pnl.py` instead runs in `daily-trade.yml`
(GitHub Actions, where the Alpaca keys already live) immediately after
`bot.py`, writes its snapshot to `config.PNL_DATA_PATH`, then a dedicated
GCP service account (`munger-pnl-writer`) uploads it to that same
bucket — the one Cloud Run's `report-web`/`daily-screen` already mount
at `DATA_DIR`. Scoping this took two attempts, both grounded in what was
actually tested live, not assumed: `roles/storage.objectCreator` alone
(bucket-scoped, no delete) looked sufficient on paper but a real run
proved otherwise — `gcloud storage cp` overwriting an *existing* object
needs `storage.objects.delete` too (confirmed by the exact 403 in a real
failed GitHub Actions run), not just create. Granting bucket-wide
`objectAdmin` would satisfy that but reintroduces the original blast-
radius problem (delete on `state.json`, `journal.db`,
`screen_results*.csv`, the whole `report/` tree). The actual fix: a
single **IAM Condition** — `roles/storage.objectAdmin`, but only where
`resource.name == ".../objects/pnl.json"` — grants the create+get+delete
this account genuinely needs, confined to that one object. Verified live
by impersonating the service account directly: an overwrite of
`pnl.json` succeeds; a write attempt to any other object name in the
same bucket is denied with the same permission error as before. `report.py`
(running later, in `daily-screen`,
which still never touches Alpaca) reads the snapshot from that mount and
renders `pnl.html`: account equity/cash, today's P&L, total unrealized
P&L, and a per-position table, with gains/losses colored distinctly. A
missing or malformed snapshot renders an explicit empty state, not an
error — this is a display concern with the same non-critical posture
`report.py` already has for its other pages.

**Scoped to Cloud Run/`gramunger.com` only, not the k3s dev report** (user
decision): GitHub Actions can't reach the home LAN the k3s cluster runs
on without new tunnel infrastructure — the same reachability gap already
named in `DESIGN_CD_PIPELINE.md` — so wiring P&L into the k3s report is
deferred, not solved here.

**Raises the stakes of an existing open decision (pm-reviewer finding):**
`TASKS.md` already carries a `done`-but-conditional row (M13's public-
exposure question) stating the public report "must stay screen-only,
paper-account data" and must be revisited "before any paper→live go/no-go
... since the same report would then show real position sizes." This
milestone adds account-level equity, cash, and aggregate unrealized P&L to
that same unauthenticated page — a materially fuller financial picture
than the per-order notional the report already showed. `pnl.py`'s
account-agnostic design (reports whichever mode is configured) means that
if `config.PAPER_TRADING` is ever flipped to live with no further changes,
this exact pipeline starts pushing real account balances to an
unauthenticated public site with zero additional gating. **That flip must
not happen without first re-deciding this**, the same precondition M13
already named for the report generally, now concretely bound to what M16
actually renders.

**Staleness and failure visibility:** `pnl.html` flags itself as stale (a
visible warning banner, `config.PNL_STALENESS_MAX_HOURS`, default 48) if
its own `generated_at` is older than that tolerance or missing/
unparseable — otherwise an old snapshot from a silently broken bridge
looks identical to a fresh one. `pnl.py`'s `__main__` deliberately does
not catch its own exceptions (pm-reviewer finding, reversing an earlier
draft that did): a broken bridge (a rotated key, a changed SDK response
shape, a GCS outage) must fail its GitHub Actions step visibly, which
fails the job, which triggers the same GH-failure-email channel every
other alert in this system already uses — not disappear into a caught-
and-logged no-op that only `munger.log` (which nobody is watching for
this specific pipeline) would ever show.

**Rollback:** read-only against the broker. Mostly additive against
`report.py`'s output — a new page (`pnl.html`), plus a new nav link to it
on each of `index.html`/`tickers.html`/`calendar.html`, which is the one
change to existing pages this milestone makes; nothing else about their
content or behavior changed. To disable, stop running the "Generate/
Upload P&L snapshot" steps in `daily-trade.yml`; `report.py` already
renders an explicit empty state when `pnl.json` doesn't exist, so no data
migration or cleanup is needed on either side. Stopping the upload steps
alone leaves the dead nav link pointing at that empty state rather than
removing it — acceptable for a quick rollback, but reverting `report.py`
itself is a second step if the link should disappear too.

## 4. Scheduling and Operations

**Cadence is daily** (2026-07-26 decision, superseding the original
quarterly-only default): the orchestration runs `python bot.py` once a
day via a scheduled GitHub Action. The original rationale for a slow
cadence — Graham/Munger gates are anchored to quarterly fundamentals
(10-Qs), and "anything faster contradicts the philosophy" — is preserved
not by running rarely, but by the rebalancing tolerance band (§3.4): the
*check* runs daily, but the target weights it checks against barely move
between quarters, so actual order activity stays low-frequency in
practice even though the process itself fires every day. A daily cadence
also converges a cold start (e.g. this system's own first paper-trading
run) to its target allocation over roughly a week, rather than needing to
wait a full quarter for the run-level notional budget (§5) to allow it.
The run must be idempotent within a day (re-running after a crash must not
double-buy) — enforced primarily by the deterministic `client_order_id` on
every order (§3.5), with a secondary pre-check against today's open orders,
filled orders, and current positions; if that broker query itself fails,
the run aborts rather than proceeding blind.

**Rollback:** if daily rebalancing doesn't behave as this section assumes
— e.g. the drift band fails to damp trading and orders keep firing past
the initial convergence window (§6), not just during it — the fallback is
either the `KILL_SWITCH` (immediate, screen-only, no code change, no
un-schedule needed) for a full stop, or reverting `daily-trade.yml`'s cron
back toward a slower cadence for a partial one. Both are operational
changes, not data migrations — no rollback of `state.json`/`journal.db`
is needed, since neither's schema changed.

Secrets (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) come from environment
variables, never from code or the repo. At startup, assert that the
account mode reported by the loaded API keys (paper vs. live) matches the
configured `paper`/`live` flag, and abort if they disagree — a mismatch
here (e.g. live keys present while config says paper, from a stale `.env`)
is exactly the kind of silent misconfiguration that only matters once,
catastrophically.

## 5. Risk Controls

Beyond the structural controls already described (position cap, cash
buffer, paper-first, two-strike selling), the system enforces: a global
kill switch (config flag that makes every run screen-only, placing no
orders), a per-run order budget (e.g., max 20 orders per run — anything
more indicates a bug or a data-provider failure), and a sanity check that
aborts the run if fewer than a configured fraction of the universe was
successfully fetched (a half-empty screen would make every holding look
delisted and trigger mass strikes).

The order-count budget bounds the number of mistakes a single run can make,
not their size — a bug in the target-weight or notional calculation (e.g.
using gross buying power instead of net liquidation value) could still
deploy a large fraction of the account in well under 20 orders. A second,
independent cap limits **total notional deployed in a single run** (e.g.,
no more than a configured percentage of equity moved per run). Combined
with the per-order limit-price band (§3.5), this bounds both how many
things can go wrong and how badly any one of them can.

**2026-07-26 revision:** either budget being exceeded used to abort the
*entire* run — correct for a one-off overage, but under a daily cadence
(§4) it's a deadlock from a cold start: an aborted run buys nothing, so
holdings stay at zero, so the next run builds the identical over-budget
queue and aborts again, forever. The buy queue now self-limits its own
notional to the run budget as it's built (§3.4) and, as a backstop, the
orchestration truncates any excess (preserving priority order) and defers
it to a later run rather than rejecting the whole run — an operator still
sees an alert naming exactly which symbols were deferred and which
budget(s) actually bound (the order-count budget binding is rare and
alarming — it only happens with 6+ same-run liquidations at today's
config — while the notional budget binding is expected and routine during
a cold start, so collapsing both into one generic message would have
made every deferral read the same regardless of which, much rarer, case
occurred). The truncation takes a strict prefix at each budget in turn
(order-count, then notional) rather than skipping an order that doesn't
fit while still trying smaller ones after it — a large higher-priority
order must defer itself and everything behind it, never let a smaller
lower-priority order execute ahead of one that didn't fit. Liquidations are
never truncated or deferred by either budget — the two-strike quality
discipline is risk-reducing, not discretionary, and always executes in
full; only the order-count budget's *remaining* room (after reserving a
slot per liquidation) applies to the buy side.

## 6. Testing and Validation Plan

Build confidence in three layers. First, unit tests on the pure functions:
Graham gates, Munger floors, and the score against hand-computed fixtures,
plus the strike/reset state machine of the sell discipline. Second, a
screen-only mode run repeatedly against live data to eyeball whether the
names it surfaces are sane (the top of the list should look like
high-quality compounders at reasonable multiples, not data-error
artifacts). Third, paper trading, observed closely at two distinct stages
given the daily cadence (§4): an initial convergence window of roughly a
week from a cold start, where deferred-order alerts on most runs are
*expected* (the run-level notional budget spreading a cold start's full
target allocation across several days, §5) and not themselves a signal
something's wrong; then an ongoing steady state, verified over at least a
full month, where the journal should show the originally-intended
behavior: near-zero sells, buys concentrated at the top of the score
ranking, weights near target, and — this is the falsifiable check for
whether the drift band (§3.4) is actually damping daily-noise trades as
intended, not just during the cold start — most days placing zero orders
of either kind once converged. The same symbol being deferred on many
consecutive days *after* that initial window would indicate the budget
genuinely isn't converging (a config or sizing bug), not healthy ramp-up,
and is the concrete signal to investigate rather than assume benign.

A historical backtest is optional and explicitly a v2 concern: point-in-time
fundamental data (avoiding survivorship and look-ahead bias) requires a
paid dataset, and a naive backtest on current-constituent yfinance data
would be misleading enough to be worse than none.

## 7. Technology Stack

Python 3.11+, yfinance (data, v1), pandas (screening), alpaca-py
(execution), pytest (tests). State is deliberately simple: `state.json` for
the strike counters, SQLite (via the standard-library `sqlite3`, no extra
dependency) for the trade journal — SQLite rather than CSV because
idempotency (§3.5) depends on reading the journal reliably, and a torn CSV
write is a real corruption risk once it's load-bearing rather than just an
audit log. Repo layout — this tree describes the `munger` repo root
directly, not a nested package directory:

```
munger/               # this repo's root
├── config.py          # every threshold and toggle, nothing hard-coded elsewhere
├── universe.py        # module 3.1
├── data.py            # module 3.2
├── screener.py        # module 3.3
├── portfolio.py       # module 3.4
├── execution.py       # module 3.5
├── journal.py         # module 3.6
├── bot.py             # orchestration entry point
├── state.json         # runtime (gitignored) — strike counters only
├── journal.db         # runtime (gitignored) — SQLite trade journal
├── tests/
└── requirements.txt
```

## 8. Build Order (suggested milestones for implementation)

1. `config.py` + `universe.py` + `data.py` — prove you can fetch clean
   metrics for the full S&P 500.
2. `screener.py` — run screen-only, inspect `screen_results.csv`, tune
   thresholds until the buyable list looks sensible (expect roughly 20–50
   names; zero or 300 means a threshold is wrong).
3. `portfolio.py` with the state machine + unit tests for the two-strike
   logic.
4. `execution.py` + `bot.py` against Alpaca paper, with the kill switch and
   order budget in place.
5. Schedule it (daily as of the 2026-07-26 cadence revision, §4), read the
   journal through the initial convergence window and then on an ongoing
   basis, adjust (§6).

## 9. Known Limitations and v2 Directions

yfinance data is best-effort and occasionally wrong — the single most
valuable v2 upgrade is a paid fundamentals API with point-in-time data.
The 2026-07-26 cadence revision (§4) means `daily-trade.yml` now runs its
own full-universe screen (via `bot.py` -> `screener.run_screen`) every
day, on top of the pre-existing separate daily screen for the public
report (`daily-screen.yml`, a different deployment entirely) -- an
accepted, not yet addressed, doubling of daily load against the same
already-best-effort/rate-limit-fragile yfinance/Wikipedia sources. The
earnings-stability window (4 years) is far short of Graham's 10 and should
lengthen with better data. Sector-relative thresholds (financials and REITs
fail current-ratio and margin tests structurally, so they're effectively
excluded in v1) could broaden the universe intelligently. Finally, a
"wonderful at fair price" valuation model (e.g., a simple owner-earnings
DCF with a required margin of safety) could eventually replace the blunt
P/E×P/B gate — that's the fullest expression of the Munger evolution beyond
Graham. A CFP-certified financial-planner Managed Agent (Anthropic API) is
also worth exploring as a v2 input to stock analysis or as a sanity check
alongside `screen-sanity-reviewer` (M5) — noted here as a future direction,
not scoped or built in v1.

This system automates a philosophy, not a prediction. Nothing in this
document is financial advice; the strategy can underperform for long
stretches, and any live deployment is at the operator's own risk.

## Implementation Roadmap

This section details the project breakdown and specific instructions for
an AI coding agent to implement the system module by module.

### Epic 1: Data Foundations & Ingestion

Goal: Ensure we have reliable, validated data before building any business
logic.

**Deliverable 1.1: Project Setup & Universe** — Create the config,
directory structure, and the basic ticker fetcher. Create a Python project
structure as defined in the Technology Stack section. Implement `config.py`
with placeholders for thresholds. Implement `universe.py` with a
`get_universe()` function that returns a list of S&P 500 tickers (use a
static file fallback approach for reliability). Validate the scraped
result before accepting it — row count in a sane band and every entry
matching a plausible ticker pattern — and fall back to the static file on
either a scrape exception or a validation failure, not just the former.

**Deliverable 1.2: Data Fetcher** — Build the `data.py` module to fetch
metrics using yfinance. Create a `fetch_metrics(symbol)` function that
retrieves the required financial metrics using yfinance. Use a thread pool
to handle concurrent requests (~10 workers). Implement a robust error
handler that returns `None` for tickers with missing data, ensuring the
process does not abort.

**Deliverable 1.3: Data Validation Layer** — Prevent bad data from
entering the logic. Add a `validate_metrics(data)` function to `data.py`.
This function should perform sanity checks: ensure numerical values are
within realistic ranges (e.g., P/E < 10,000, reasonable debt ratios). If
metrics are physically impossible or outliers, it must fail the ticker with
a `data_invalid_outlier:<field>` reason — distinct from a
`data_missing:<field>` reason for fields the provider returned nothing for.
Update the main data fetcher to use this validator before returning the
metrics object, and add retry-with-backoff around the underlying yfinance
calls for transient/rate-limit errors before giving up on a ticker.

### Epic 2: The Screener (Graham & Munger Logic)

Goal: Decouple the philosophy from the execution.

**Deliverable 2.1: Graham Gate Logic** — The binary pass/fail criteria.
Implement `screener.py`. Create a `pass_graham_gates(metrics)` function
that evaluates the ticker against the hard "pass/fail" criteria outlined in
Section 3.3 (Stage 1). The function must return a tuple: `(bool passed,
list of fail_reasons)`. Ensure all thresholds are pulled from `config.py`.

**Deliverable 2.2: Munger Scorer** — The quality scoring (0–100). Continue
`screener.py`. Implement `calculate_munger_score(metrics)` that computes a
0–100 score based on the weighted components (ROE, gross margin, operating
margin, FCF yield, debt). Include a `run_screen(tickers)` function that
coordinates both the Graham gates and the Munger score, returning a pandas
DataFrame with all results.

### Epic 3: Portfolio Engine & State Management

Goal: Implement the "two-strike" rule and target weight logic.

**Deliverable 3.1: Strike State Machine** — Keeping track of holdings over
time. Implement `portfolio.py` and `journal.py`. Create the `StateTracker`
class that reads and writes **only the strike counters** to `state.json`
(atomic writes: temp file + rename) — it is never the source of truth for
current holdings. `process_sells(current_holdings, new_market_data)` takes
`current_holdings` as fetched live from the broker at the start of the run
(see §3.4), checks them against quality floors, increments strike counts
for failures, and returns a list of tickers to liquidate if strikes reach
2. If the live holdings diverge from what the previous run's journal
expected, log a reconciliation warning.

**Deliverable 3.2: Target Construction** — Buying and weighting logic.
Update `portfolio.py`. Implement `generate_buy_queue(current_holdings,
screen_results, available_cash)`. This should calculate target weights
(~1/15th of equity), account for the cash buffer, and generate a list of
orders to "top up" existing holdings or open new positions based on the
Munger score, ensuring we ignore "dust" orders below $50.

### Epic 4: Execution & Orchestration

Goal: Final integration and safety controls.

**Deliverable 4.1: Execution Module (Idempotency)** — Safe, reliable
orders. Implement `execution.py` using `alpaca-py`. Create a class
`ExecutionModule` with `market_buy(symbol, amount)` and `liquidate(symbol)`.
Every order is submitted with a deterministic `client_order_id` (hash of
run-date + ticker + side) so Alpaca itself rejects a duplicate submission
from a crashed-and-restarted run — this is the primary idempotency
guarantee, not just a nice-to-have. Add a
`get_todays_open_orders_and_positions()` method that also checks today's
*filled* orders and current positions (not open orders alone, which misses
an order that filled before a crash); `market_buy` and `liquidate` check
this as a secondary guard before submitting. If this broker query itself
fails, `ExecutionModule` must raise rather than let the caller proceed
blind — `bot.py` treats that as an abort-the-run condition. Apply a
limit-price band (e.g. ±2% of last trade) to every order, buys and
liquidations alike, instead of an unconstrained market order.

**Deliverable 4.2: Bot Orchestration (The Kill Switch)** — Putting it all
together. Implement `bot.py`. It should import all modules and run the
daily/quarterly cycle. At startup, assert the loaded API keys' account mode
(paper/live) matches the configured flag and abort on mismatch. Implement a
global `KILL_SWITCH` flag from `config.py`: if `True`, the bot must only
perform the screen and print the plan, never executing orders. Add a
`GLOBAL_ORDER_BUDGET` check (max order count) **and** a
`GLOBAL_NOTIONAL_BUDGET` check (max total notional deployed as a percentage
of equity) — either being exceeded aborts the run and logs a critical
error; the order-count budget alone bounds how many things can go wrong,
not how much any one of them can move.

### PM Recommendations for Success

- Test the Screener First: Before building the Alpaca integration, run the
  screener against the full S&P 500 and manually check the
  `screen_results.csv` output.
- Audit the Journal: Spend a full two weeks reviewing the `journal.py`
  outputs before enabling real-money trading to ensure the "two-strike"
  sell logic behaves as expected.

## Design Review (Staff Engineer Feedback)

- **Observability & Alerting:** Beyond structured logs, consider
  implementing heartbeat monitoring. If the cron job fails to run or the
  universe fetcher returns an empty list, you need immediate alerts. Define
  what "healthy" looks like — e.g., alerting if data freshness is > 24
  hours.
- **Execution Reliability (Idempotency):** Your Execution Module must be
  strictly idempotent. If the script restarts mid-process, it should query
  the broker for "today's open orders" before attempting any new buys.
  Avoid "double-tapping" a buy signal.
- **Data Integrity (The Outlier Problem):** You're relying on free-tier
  APIs (yfinance). Add a "Data Sanity Check" layer. If a fundamental metric
  (e.g., P/E) is physically impossible or an extreme outlier (e.g.,
  > 10,000), the system must treat this as a "data error" and fail the
  ticker rather than processing it.
- **State Management:** While `state.json` works for a prototype, treat it
  with care. Ensure atomic writes (write to temp file, then
  rename/overwrite) to prevent state corruption during a crash.
- **Security:** If this grows beyond a personal tool, move secrets out of
  environment variables and into a proper secret management service.
  Ensure your "kill switch" is easily accessible (e.g., a simple
  file-based flag on the filesystem) in case you need to intervene
  manually without access to the code.

## Design Review — Round 2 (Independent Staff-Engineer Pass)

A second, independent staff-engineer review (2026-07-21) went further than
the round above on two of its points and surfaced several gaps it didn't
cover. All of the following are now incorporated into the relevant sections
above (§3.1–§3.6, §4, §5, and the Epic 3/4 deliverables):

- **The Round 1 idempotency fix is necessary but insufficient.** Checking
  only "today's open orders" misses an order that filled and then crashed
  before the journal write — on restart it's no longer "open," so the
  check sees nothing and double-buys. Fixed via a deterministic
  `client_order_id` per order (§3.5), with the open-orders check widened
  to also cover filled orders and current positions.
- **The Round 1 state-management fix (atomic writes) doesn't address what
  `state.json` should be authoritative for.** It's now explicit: strike
  counters only. Current holdings are always fetched live from the broker,
  never read from local state (§3.4).
- **Universe scrape validation only covered outright failure, not silent
  corruption** — a Wikipedia table restructure that shifts columns
  produces a garbage-but-well-formed list. Fixed with a row-count/format
  sanity check before accepting a scrape (§3.1).
- **"No data" and "bad data" collapsed into one boolean.** Fixed with
  distinct fail-reason codes (`data_missing:*` vs.
  `data_invalid_outlier:*`) carried through to `screen_results.csv`
  (§3.2/§3.3).
- **The order-count budget bounded mistake count, not mistake size.**
  Fixed with an independent notional-cap budget and a limit-price band on
  every order (§3.5/§5).
- **No alerting specifically on liquidations or on a state/broker
  reconciliation mismatch**, despite these being the highest-signal events
  the system produces. Fixed (§3.4/§3.6).
- **No safeguard against a paper/live API key mismatch.** Fixed with a
  startup assertion (§4).
- **No retry/backoff strategy for yfinance rate-limiting**, and the
  journal format was left undecided as "CSV or SQLite" despite becoming
  load-bearing for idempotency. Resolved: retry-with-backoff in the data
  layer (§3.2), and the journal is SQLite (§3.6/§7).

Not changed: the review found nothing wrong with the observability and
security recommendations from Round 1 as scoped, and confirmed the
position-cap/cash-buffer/paper-first/two-strike structural controls
already in place.
