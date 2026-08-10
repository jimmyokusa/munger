"""Every threshold and toggle for the Graham-Munger bot.

Nothing downstream hard-codes a number or flag — it imports from here.
See DESIGN.md for the rationale behind each value.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Writable runtime artifacts (report/, archives, journal.db, the raw-
# response cache, logs, state) live under DATA_DIR, which defaults to the
# code directory (unchanged local/test behavior) but can be repointed at a
# mounted volume via MUNGER_DATA_DIR -- e.g. a Kubernetes PersistentVolume
# so the daily CronJob's output survives pod restarts and is shared with
# the nginx pod that serves it. Read-only inputs (STATIC_UNIVERSE_FALLBACK
# etc.) stay under BASE_DIR since they ship inside the image.
DATA_DIR = Path(os.environ.get("MUNGER_DATA_DIR", str(BASE_DIR)))

# --- Secrets (env vars only, never hard-coded; see DESIGN.md section 4) ---
# Empty-string default is intentional here: this layer only reads config,
# it doesn't validate it. The paper/live startup assertion in bot.py (M10)
# is where a missing key should fail fast, not here.
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# --- Universe module (DESIGN.md 3.1) ---
# All three indices (S&P 500/400/600) are scraped from Wikipedia. Each is
# fetched, validated, and falls back to its own slice of
# STATIC_UNIVERSE_FALLBACK_PATH independently -- see DESIGN.md 3.1.
EXCLUDED_SECTORS: tuple[str, ...] = ()
UNIVERSE_TICKER_COUNT_BANDS: dict[str, tuple[int, int]] = {
    "500": (490, 510),
    "400": (390, 410),
    "600": (590, 615),
}
STATIC_UNIVERSE_FALLBACK_PATH = "data/universe_fallback.csv"

# --- Data module (DESIGN.md 3.2) ---
# Reduced from 12 -- user-reported observation (2026-07-23) that a live
# run only fetched 80.5% cleanly (worse than M2's 90% retest). Since
# yfinance's rate limit is session/IP-wide, not per-worker (see below),
# more concurrent workers doesn't buy more real throughput once the
# limit is hit -- it just means more threads simultaneously trigger it.
# Fewer workers should reduce how often that first 429 fires at all.
# Best-effort tuning against an undocumented, opaque provider limit, not
# a guaranteed fix -- revisit against another live run.
DATA_FETCH_THREAD_POOL_WORKERS = 6
DATA_FETCH_MAX_RETRIES = 4
# Base delay for exponential backoff (delay = this * 2**attempt, plus
# jitter) between retries on a single ticker, not a flat per-retry sleep.
DATA_FETCH_RETRY_BACKOFF_SECONDS = 2.0
# Upper bound on how long fetch_all_metrics waits for the whole batch --
# see its docstring for why this can't fully protect against a hung
# worker thread, only bound the caller's logical wait. Doubled from 600s
# (2026-07-23): this is a quarterly batch job with no minute-level time
# pressure, so giving a rate-limited run more real wall-clock time to
# cycle through cooldowns and actually finish is a free way to improve
# the completion rate, unlike anything that costs more requests.
DATA_FETCH_BATCH_TIMEOUT_SECONDS = 1200.0
# yfinance's rate limit is session/IP-wide, not per-ticker (confirmed
# live: a full ~1500-ticker universe fetch at 12 workers hit
# YFRateLimitError on 991/1505 tickers) -- when hit, every worker pauses
# for this long before its next attempt, rather than each burning its
# own retries hammering an already-limited session independently.
# Increased from 20s (2026-07-23): a live run still saw heavy repeated
# rate-limiting at 20s, suggesting the backend needs longer to actually
# reset before it's safe to resume hammering it.
DATA_RATE_LIMIT_COOLDOWN_SECONDS = 45.0
# Raw per-ticker provider responses, overwritten each run -- a debugging
# aid, not a permanent audit trail (that's journal.db, DESIGN.md 3.6).
DATA_RAW_CACHE_DIR: Path = DATA_DIR / "data_cache"

# Throttle for the per-ticker live-progress file write (data.py's
# _mark_ticker_started/_mark_ticker_done). Zero (the default) writes on
# every single call, unchanged from the original local-disk/k3s-PVC
# behavior, where a real block device has no per-object write-rate limit.
# Cloud Run's GCS-backed volume does: a full universe run
# (DATA_FETCH_THREAD_POOL_WORKERS concurrent threads, ~3000 progress
# writes) blew through GCS's per-object mutation rate limit, and the
# resulting 429/stale-file-handle retries starved the fetch threads badly
# enough that 489/1503 tickers timed out (first Cloud Run seed run,
# 2026-07-25). Set via MUNGER_PROGRESS_WRITE_MIN_INTERVAL_SECONDS on any
# GCS-backed deployment; _start_progress/_finish_progress always write
# immediately regardless of this value -- only the high-volume per-ticker
# calls are throttled.
PROGRESS_WRITE_MIN_INTERVAL_SECONDS = float(
    os.environ.get("MUNGER_PROGRESS_WRITE_MIN_INTERVAL_SECONDS", "0")
)

# Sanity bounds for validate_metrics (DESIGN.md 3.2, Deliverable 1.3).
# Same ratio/decimal-fraction units as the corresponding Metrics field and
# gate threshold (e.g. MAX_DEBT_TO_EQUITY above) -- data.py normalizes
# yfinance's raw percentage-scaled fields (debtToEquity, dividendYield)
# before they ever reach here, so these bounds are never raw-percentage
# scale even though the provider's own field happens to be.
MAX_PLAUSIBLE_PE = 10_000
MAX_PLAUSIBLE_DEBT_TO_EQUITY = 100

# --- Screener Stage 1: Graham entry gates (DESIGN.md 3.3) ---
MIN_MARKET_CAP = 2_000_000_000  # $2B
MIN_CURRENT_RATIO = 1.5
MAX_DEBT_TO_EQUITY = 1.0
MIN_CONSECUTIVE_POSITIVE_EARNINGS_YEARS = 4
REQUIRE_DIVIDEND_RECORD = False  # optional toggle, off by default
MAX_PE = 20
MAX_PE_TIMES_PB = 30

# --- Screener Stage 2: Munger quality floors (DESIGN.md 3.3) ---
MIN_ROE = 0.15
MIN_GROSS_MARGIN = 0.30
REQUIRE_POSITIVE_FCF = True

# Munger score weights and normalization targets (sum of weights == 1.0)
SCORE_WEIGHT_ROE = 0.30
SCORE_NORMALIZATION_ROE = 0.40
SCORE_WEIGHT_GROSS_MARGIN = 0.20
SCORE_NORMALIZATION_GROSS_MARGIN = 0.60
SCORE_WEIGHT_OPERATING_MARGIN = 0.15
SCORE_NORMALIZATION_OPERATING_MARGIN = 0.35
SCORE_WEIGHT_FCF_YIELD = 0.20
SCORE_NORMALIZATION_FCF_YIELD = 0.08
SCORE_WEIGHT_LOW_DEBT = 0.15  # normalized as (1 - D/E), clamped to [0, 1]

# --- Portfolio engine (DESIGN.md 3.4) ---
TARGET_POSITION_COUNT = 15
MAX_SINGLE_POSITION_WEIGHT = 0.12
CASH_BUFFER_PCT = 0.02
MIN_ORDER_NOTIONAL = 50.0  # orders below this are skipped as dust
# Under daily rebalancing, a holding's dollar value drifts with price
# every single run -- without a tolerance band, any drift past
# MIN_ORDER_NOTIONAL alone would trigger a top-up trade most days on
# noise, not real signal. Only top up a holding once it's fallen this
# fraction below its own target dollar value; a stock that ran up past
# target is never trimmed to rebalance down (DESIGN.md 3.4: this module
# never sells to buy, and a rally is success, not a sell signal).
REBALANCE_DRIFT_BAND_PCT = 0.10
STRIKES_TO_LIQUIDATE = 2

# --- Execution module (DESIGN.md 3.5) ---
# M20 (DESIGN_REAL_MONEY.md §3.1): env-driven, not a bare literal, so the
# exact same bot.py/execution.py/pnl.py binaries can run a second time
# against a real live account (daily-trade-live.yml) with nothing but a
# different environment -- the "one binary, env decides behavior" idiom
# this codebase already uses everywhere else (MUNGER_DATA_DIR,
# MUNGER_GRAFANA_URL). Defaults to "true" so daily-trade.yml's existing
# invocation (which never sets this) is byte-for-byte unchanged behavior;
# only the new live workflow sets MUNGER_PAPER_TRADING=false. Every read
# site in this codebase does `import config; ...config.PAPER_TRADING`,
# never `from config import PAPER_TRADING`, so there's no import-time
# literal-bool binding anywhere this env-string parse would break
# (verified in the M20 design's staff-engineer-reviewer pass, not
# assumed).
#
# Parses as "anything other than exactly 'false' means paper" (staff-
# engineer-reviewer finding, code-push review), not "anything other than
# exactly 'true' means live" -- the original version of this line failed
# toward the *more* dangerous mode on ambiguous input: a stray typo,
# extra whitespace from a copy-paste into workflow YAML, or an
# accidentally-empty value would all have silently enabled live trading.
# This matches DESIGN.md §4's own precedent for a "matters once,
# catastrophically" flag (the paper/live API-key mismatch assertion):
# fail toward the safe default, not away from it.
PAPER_TRADING = os.environ.get("MUNGER_PAPER_TRADING", "true").strip().lower() != "false"
LIMIT_PRICE_BAND_PCT = 0.02  # +/-2% of last trade, applied to every order

# --- State, audit, and observability (DESIGN.md 3.6) ---
# Anchored to BASE_DIR (not bare relative filenames) so a scheduler
# invoking bot.py from a different working directory than an interactive
# run still reads/writes the same state.json instead of silently starting
# from a fresh, empty strike history.
STATE_FILE_PATH: Path = DATA_DIR / "state.json"
JOURNAL_DB_PATH: Path = DATA_DIR / "journal.db"
# Written by pnl.py (which runs where ALPACA_API_KEY lives -- daily-trade.yml,
# GitHub Actions) directly to the same GCS bucket report-web/daily-screen
# mount at DATA_DIR, since report.py's own deployment (Cloud Run/k3s)
# deliberately never has Alpaca credentials (DESIGN.md 3.5/M14's screen-
# only boundary) and so can never fetch this data itself.
PNL_DATA_PATH: Path = DATA_DIR / "pnl.json"
# M20 (DESIGN_REAL_MONEY.md §4): the live account's own current-state
# snapshot, written by the exact same pnl.py binary as PNL_DATA_PATH
# above, just invoked from the separate daily-trade-live.yml workflow
# with live credentials -- deliberately a second, independent path, not a
# parameter that could accidentally make the two accounts' snapshots
# overwrite each other.
REAL_MONEY_DATA_PATH: Path = DATA_DIR / "real_money.json"
# Durable, append-only daily P&L series (M17) -- the *system of record* for the
# "account P&L over time" dashboard, maintained by pnl.py in GitHub Actions.
# pnl.json is a rolling ~1-month snapshot Alpaca overwrites daily; this file
# instead accretes one row/day forever (seeded once from Alpaca's history, then
# grown), so the long-term equity curve survives past Alpaca's rolling window.
# Lives in the same GCS bucket as pnl.json; read (never written) by the report
# deployment. See DESIGN_DASHBOARDS.md 2.1.
PNL_HISTORY_PATH: Path = DATA_DIR / "pnl_history.jsonl"
# The GCS bucket pnl.py (GitHub Actions) writes pnl.json/pnl_history.jsonl
# into. Read by gcs_bridge.py (k3s's read-only GCS->PVC CronJob, M17
# DESIGN_DASHBOARDS.md 2.3) -- deliberately a plain constant, not env-gated,
# since it names a specific real bucket rather than toggling a deployment
# behavior.
PNL_GCS_BUCKET = "munger-503515-data"
# pm-reviewer finding: without a defined threshold, an old snapshot (the
# GCS bridge silently broke, or daily-trade.yml stopped firing) just
# looks like a normal snapshot to a viewer -- report.py flags pnl.html as
# stale past this age instead. Matches DATA_FRESHNESS_MAX_HOURS's
# daily-cadence-appropriate tolerance (one missed run's worth of slack).
PNL_STALENESS_MAX_HOURS = 48
# M19: intraday P&L refresh. `pnl-snapshot.yml` sets both to opt into
# behavior `daily-trade.yml`'s once/day `pnl.py` invocation must NOT get --
# left unset there (both default false), that call stays exactly the full
# snapshot-plus-durable-history run it always was.
# MARKET_HOURS_ONLY: the intraday cron's own UTC envelope is deliberately
# wider than real market hours (a fixed cron can't track ET's DST shift),
# so __main__ asks Alpaca's own clock and skips the fetch/upload entirely
# on a tick that lands inside the envelope but outside real trading hours.
PNL_MARKET_HOURS_ONLY = os.environ.get("MUNGER_PNL_MARKET_HOURS_ONLY", "") == "1"
# SNAPSHOT_ONLY: skips update_history_series() (the pnl_history.jsonl
# read-modify-write upsert). That file is a one-row-per-day durable series
# by design -- upserting it 13x/day during market hours adds no value
# (same day's row, a fresher same-day equity value each tick) while
# reintroducing real risk, since GitHub Actions runners are ephemeral and
# only daily-trade.yml's own workflow restores the prior series from GCS
# before upserting. Without that restore, an upsert reseeds from only
# Alpaca's rolling ~30-day history and permanently truncates the durable
# record on re-upload. The durable series stays daily-trade.yml's
# exclusive responsibility; the intraday cron never touches it.
PNL_SNAPSHOT_ONLY = os.environ.get("MUNGER_PNL_SNAPSHOT_ONLY", "") == "1"
# A single short retry before letting an Alpaca read failure propagate
# (fail closed, same as before -- see pnl.py's module docstring). Added
# because M19 raises Alpaca call frequency from ~1/day to ~13/day during
# market hours, meaningfully raising the odds of an ordinary transient
# blip firing a false-positive failure-alert email for a non-incident; a
# persistently broken bridge still fails loud after this is exhausted.
PNL_ALPACA_RETRY_DELAY_SECONDS = 5.0
# Client-side poll interval for pnl.html's live refresh (M19) -- a few
# times tighter than the data's own ~30-min update cadence (a left-open
# tab picks up a new snapshot within minutes of it landing, not up to 30),
# but nowhere near _progress_polling_script's 2s interval (tuned for a
# screening run users watch complete in minutes); reusing that cadence
# here would generate ~1,800 requests/hour per open tab for data that only
# changes twice an hour (staff-engineer-reviewer finding, DESIGN.md 3.8.1).
PNL_CLIENT_POLL_INTERVAL_SECONDS = 300
# Currently-held-symbol daily close prices (M17 Phase 2, DESIGN_DASHBOARDS.md
# §3) -- feeds Graph 2 ("daily close price per owned ticker"). Written by
# prices.py in the same GitHub Actions job as pnl.py (same Alpaca-credential
# boundary), reading pnl.json's positions[] for which symbols to fetch.
# Unlike PNL_HISTORY_PATH this is NOT an append-series: Alpaca's historical
# bars endpoint has no rolling-window loss the way portfolio history does,
# so each run simply re-fetches PRICES_LOOKBACK_DAYS of bars fresh.
# "Currently held" is a moving window by design -- a sold ticker's series
# drops off next run, a new buy's appears (DESIGN_DASHBOARDS.md §3).
PRICES_DATA_PATH: Path = DATA_DIR / "prices.json"
PRICES_LOOKBACK_DAYS = 365
# Sanity ceiling for a single daily close price, mirroring
# MAX_PLAUSIBLE_PE/MAX_PLAUSIBLE_DEBT_TO_EQUITY above. A close <= 0 or
# beyond this is a data_invalid_outlier point, never silently plotted.
PRICES_MAX_PLAUSIBLE_CLOSE = 100_000.0
# Alpaca market-data feed for prices.py's historical bars. Confirmed live
# (2026-08-07): this account's free-tier subscription 403s on the client's
# default (SIP) feed -- "subscription does not permit querying recent SIP
# data" -- while IEX succeeds; IEX is what the free tier actually grants
# (DESIGN_DASHBOARDS.md §3 prerequisite). A future paid-tier upgrade would
# just change this one constant. Plain string, not the alpaca-py DataFeed
# enum, so config.py stays free of a provider-SDK import like every other
# constant here.
PRICES_DATA_FEED = "iex"
SCREEN_RESULTS_CSV_PATH: Path = DATA_DIR / "screen_results.csv"
SCREEN_RESULTS_ARCHIVE_DIR: Path = DATA_DIR / "screen_results_archive"
LOG_FILE_PATH: Path = DATA_DIR / "munger.log"
REPORT_DIR: Path = DATA_DIR / "report"
# Inside REPORT_DIR (not BASE_DIR) so the same static server that serves
# index.html/tickers.html also serves this live-updating file -- no
# separate copy step needed for the report's progress-bar JS to poll it.
PROGRESS_FILE_PATH: Path = REPORT_DIR / "progress.json"

# URL of the embedded Grafana dashboards (M17). When set, report.py renders
# a "Dashboards" tab (dashboards.html iframing this URL) and drops the legacy
# "Daily calendar" nav link -- the M17 calendar->dashboards swap, gated per
# environment so a deployment without Grafana yet keeps the calendar rather
# than showing no history view at all (DESIGN_DASHBOARDS.md 4). Empty by
# default (unchanged local/k3s-without-Grafana behavior). Set
# MUNGER_GRAFANA_URL to the dashboards' embeddable URL on any deployment that
# has Grafana (k3s dev; grafana.gramunger.com on prod).
GRAFANA_BASE_URL = os.environ.get("MUNGER_GRAFANA_URL", "")
# URL of the embedded "Prices" (Graph 2, held-symbol daily close) Grafana
# dashboard -- M17 Phase 2, DESIGN_DASHBOARDS.md §3. Deliberately a
# separate constant, not a second URL derived from GRAFANA_BASE_URL: the
# two dashboards can ship on different schedules (P&L before Prices, as
# happened this session), so dashboards.html renders whichever iframes are
# actually configured rather than assuming both always exist together.
# Same env-gated pattern; empty by default so dashboards.html doesn't
# advertise a "Prices" panel that isn't deployed yet (real bug found in
# review: an earlier version of this feature provisioned the Grafana
# dashboard but had no way for a user to reach it from the site at all).
GRAFANA_PRICES_URL = os.environ.get("MUNGER_GRAFANA_PRICES_URL", "")
# Gates whether report.py renders real-money.html/its nav link at all
# (M20, DESIGN_REAL_MONEY.md §4). Same config-gated-off-by-default shape
# as GRAFANA_BASE_URL above -- a deployment without the live pipeline
# provisioned yet renders nothing new. Set MUNGER_LIVE_TRADING_ENABLED=1
# on the daily-screen Job once the live account/workflow are ready.
# Deliberately a manually-set flag, not literally "derived from whether
# ALPACA_LIVE_API_KEY is configured" as an earlier design draft phrased
# it: report.py's own deployment (Cloud Run/k3s) must never hold, read,
# or even reference the name of an Alpaca credential at all (§1.2/M14's
# screen-only boundary) -- that invariant is easiest to keep true, and
# easiest to audit, if this file simply never names ALPACA_LIVE_API_KEY,
# not even to check whether it's set.
LIVE_TRADING_ENABLED = os.environ.get("MUNGER_LIVE_TRADING_ENABLED", "") == "1"

# --- Public site metadata + SEO (M18, DESIGN_WEB_ANALYTICS_SEO.md) ---
# Absolute origin of the public site (e.g. https://gramunger.com). One
# constant gates every output that needs an absolute URL: <link rel=canonical>,
# Open Graph tags, sitemap.xml, and the robots.txt "index me" policy. Empty by
# default -- the same env-gated pattern as GRAFANA_BASE_URL above -- so local
# and k3s-dev runs emit noindex, no sitemap, and no gramunger.com URLs, and are
# never pulled into a search index. Set MUNGER_SITE_URL only on the prod
# daily-screen Job (report.py bakes these into the HTML at generation time, so
# the env var lives on the Job that runs report.py, not the nginx service).
SITE_BASE_URL = os.environ.get("MUNGER_SITE_URL", "").rstrip("/")
# GoatCounter beacon endpoint (e.g. https://stats.gramunger.com/count) for the
# self-hosted, cookieless visitor analytics (M18 §2). When set, report.py
# injects the ~3KB async count.js snippet into every page's <head>; empty by
# default so dev/local traffic is never reported to prod stats. Also set only
# on the prod daily-screen Job.
ANALYTICS_URL = os.environ.get("MUNGER_ANALYTICS_URL", "")
# DESIGN.md's PM-recommendations narrative illustrates this as "> 24
# hours," written before the quarterly cadence was settled (M1) -- a
# literal 24-hour threshold would have fired on every single healthy
# quarterly gap back then. 2026-07-26: cadence is now daily (DESIGN.md
# 4), so shrunk from 130*24 (quarterly: ~91 days + scheduling offset +
# buffer) down to a tolerance around the actual daily cadence -- one
# missed/delayed run's worth of slack, not 130 days of it, or this
# dead-man's-switch would no longer fire within any useful window of an
# actual missed run.
DATA_FRESHNESS_MAX_HOURS = 48

# --- Risk controls (DESIGN.md 5) ---
KILL_SWITCH = False
KILL_SWITCH_FLAG_FILE_PATH: Path = DATA_DIR / "KILL_SWITCH"
# M20 (DESIGN_REAL_MONEY.md §3.2): an account-independent master switch,
# checked before either account's own per-account KILL_SWITCH above.
# Deliberately anchored at BASE_DIR (the checkout root), not DATA_DIR --
# paper and live now run as genuinely separate GitHub Actions workflows
# with separate checkouts/runners (§3.1), so a file written into one
# run's local DATA_DIR is invisible to the other's. A file *committed to
# the repo itself* (on `main`) is the one thing both workflows' fresh
# `actions/checkout` will always see identically -- one commit stops both
# accounts; removing it resumes both. The per-account flags above remain
# for stopping just one account without touching the other.
GLOBAL_KILL_SWITCH_FLAG_FILE_PATH: Path = BASE_DIR / "GLOBAL_KILL_SWITCH"
GLOBAL_ORDER_BUDGET = 20  # max orders per run
GLOBAL_NOTIONAL_BUDGET_PCT = 0.25  # max fraction of equity moved per run
MIN_UNIVERSE_FETCH_FRACTION = 0.90  # abort if fewer tickers than this fetch cleanly
