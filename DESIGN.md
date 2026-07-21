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
sense. It is a screener plus an executor plus a very reluctant seller. It
runs infrequently (quarterly by default), and most runs should place zero
sell orders.

## 2. High-Level Architecture

```
┌────────────────────────────────────────────────────────────┐
│                        SCHEDULER                            │
│         (cron / GitHub Actions — quarterly cadence)         │
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

Provides the list of candidate tickers. Default universe is the S&P 500,
which conveniently enforces Graham's "large, prominent, conservatively
financed" requirement and keeps the system loosely inside a circle of
competence. The module scrapes the constituent list (Wikipedia table is
acceptable for v1; a static fallback file should ship in the repo in case
the scrape fails), normalizes ticker symbols to the broker's format (e.g.,
`BRK.B` → `BRK-B`), and applies an optional configured list of excluded
sectors — Munger's circle-of-competence rule made concrete: if you don't
understand banks, exclude Financials.

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

Design requirements: fetch concurrently (thread pool, ~10–15 workers),
tolerate individual-ticker failures without aborting the run, treat missing
data as a failed check rather than a pass (conservatism: no data, no buy),
and cache raw responses per run for debuggability.

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
pass/fail bit.

**Stage 2 — Munger quality floor and score.** Hard floors that must be met:
ROE ≥ 15%, gross margin ≥ 30%, positive free cash flow. Stocks passing both
stages get a 0–100 composite quality score used for ranking:

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

The heart of the system, and where the philosophy lives. Three
responsibilities:

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

**Buy queue.** After sells settle, deployable cash = cash − buffer.
Priority order: first top up existing holdings that sit below target
weight (gap ≥ minimum order size), then open new positions from the top of
the score-ranked buyable list until the position count reaches target or
cash runs out. Orders below $50 notional are skipped as dust. The engine
never sells one holding to buy a higher-scoring one — no churn, ever.

### 3.5 Execution Module

Thin wrapper around the broker. Version 1 targets Alpaca's paper-trading
API using the `alpaca-py` SDK: notional (dollar-amount, fractional-share)
market orders, DAY time-in-force, and `close_position` for liquidations.
Every order attempt is wrapped in error handling so one rejected order
never aborts the run. The paper/live flag lives in config and defaults to
paper; the design intent is that live trading is only enabled after several
months of clean paper runs.

Interface: `market_buy(symbol, notional)`, `liquidate(symbol)`

### 3.6 State, Audit, and Observability

Three artifacts per run: `state.json` (the strike streaks — the only
mutable state the system has), `screen_results.csv` (full screen output,
timestamped copies kept), and an append-only trade journal (CSV or SQLite)
recording every order with its reason string (e.g., `NEW_POSITION
score=78.2` or `SELL strikes=2 reasons=roe_floor,fcf_floor`). Structured
logging to file and stdout. The journal is what lets you later evaluate
whether the system is actually following its own rules — an audit of
behavior, in the Graham spirit of the investor's chief problem being
himself.

## 4. Scheduling and Operations

Default cadence is quarterly, aligned to run ~2–3 weeks after quarter-end
so fresh 10-Q data has propagated into the data provider. Monthly is the
aggressive ceiling; anything faster contradicts the philosophy. Implement
as a cron job or a scheduled GitHub Action that runs `python bot.py`. The
run must be idempotent within a day (re-running after a crash must not
double-buy — check open orders and today's journal entries before
submitting).

Secrets (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) come from environment
variables, never from code or the repo.

## 5. Risk Controls

Beyond the structural controls already described (position cap, cash
buffer, paper-first, two-strike selling), the system enforces: a global
kill switch (config flag that makes every run screen-only, placing no
orders), a per-run order budget (e.g., max 20 orders per run — anything
more indicates a bug or a data-provider failure), and a sanity check that
aborts the run if fewer than a configured fraction of the universe was
successfully fetched (a half-empty screen would make every holding look
delisted and trigger mass strikes).

## 6. Testing and Validation Plan

Build confidence in three layers. First, unit tests on the pure functions:
Graham gates, Munger floors, and the score against hand-computed fixtures,
plus the strike/reset state machine of the sell discipline. Second, a
screen-only mode run repeatedly against live data to eyeball whether the
names it surfaces are sane (the top of the list should look like
high-quality compounders at reasonable multiples, not data-error
artifacts). Third, paper trading for at least one full quarter cycle —
ideally two — verifying that the journal shows the intended behavior:
near-zero sells, buys concentrated at the top of the score ranking, weights
near target.

A historical backtest is optional and explicitly a v2 concern: point-in-time
fundamental data (avoiding survivorship and look-ahead bias) requires a
paid dataset, and a naive backtest on current-constituent yfinance data
would be misleading enough to be worse than none.

## 7. Technology Stack

Python 3.11+, yfinance (data, v1), pandas (screening), alpaca-py
(execution), pytest (tests). No database in v1 — JSON + CSV state is
deliberately simple. Repo layout:

```
graham_munger_bot/
├── config.py          # every threshold and toggle, nothing hard-coded elsewhere
├── universe.py        # module 3.1
├── data.py            # module 3.2
├── screener.py        # module 3.3
├── portfolio.py       # module 3.4
├── execution.py       # module 3.5
├── journal.py         # module 3.6
├── bot.py             # orchestration entry point
├── state.json         # runtime (gitignored)
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
5. Schedule it, let it run a quarter, read the journal, adjust.

## 9. Known Limitations and v2 Directions

yfinance data is best-effort and occasionally wrong — the single most
valuable v2 upgrade is a paid fundamentals API with point-in-time data. The
earnings-stability window (4 years) is far short of Graham's 10 and should
lengthen with better data. Sector-relative thresholds (financials and REITs
fail current-ratio and margin tests structurally, so they're effectively
excluded in v1) could broaden the universe intelligently. Finally, a
"wonderful at fair price" valuation model (e.g., a simple owner-earnings
DCF with a required margin of safety) could eventually replace the blunt
P/E×P/B gate — that's the fullest expression of the Munger evolution beyond
Graham.

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
static file fallback approach for reliability).

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
metrics are physically impossible or outliers, it must return `False`.
Update the main data fetcher to use this validator before returning the
metrics object.

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
class that reads and writes to `state.json`. Implement the "two-strike"
logic: a method `process_sells(current_holdings, new_market_data)` that
checks holdings against quality floors, increments strike counts for
failures, and returns a list of tickers to liquidate if strikes reach 2.

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
Crucially, add a `get_todays_open_orders()` method. Before any order is
placed, `market_buy` must check this to ensure we don't "double-tap" or
place redundant orders if the script is re-run.

**Deliverable 4.2: Bot Orchestration (The Kill Switch)** — Putting it all
together. Implement `bot.py`. It should import all modules and run the
daily/quarterly cycle. Implement a global `KILL_SWITCH` flag from
`config.py`: if `True`, the bot must only perform the screen and print the
plan, never executing orders. Add a `GLOBAL_ORDER_BUDGET` check — if the
`portfolio.py` generated plan exceeds X orders, abort the run and log a
critical error.

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
