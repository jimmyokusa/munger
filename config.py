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
PAPER_TRADING = True  # live trading only after months of clean paper runs
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
# pm-reviewer finding: without a defined threshold, an old snapshot (the
# GCS bridge silently broke, or daily-trade.yml stopped firing) just
# looks like a normal snapshot to a viewer -- report.py flags pnl.html as
# stale past this age instead. Matches DATA_FRESHNESS_MAX_HOURS's
# daily-cadence-appropriate tolerance (one missed run's worth of slack).
PNL_STALENESS_MAX_HOURS = 48
SCREEN_RESULTS_CSV_PATH: Path = DATA_DIR / "screen_results.csv"
SCREEN_RESULTS_ARCHIVE_DIR: Path = DATA_DIR / "screen_results_archive"
LOG_FILE_PATH: Path = DATA_DIR / "munger.log"
REPORT_DIR: Path = DATA_DIR / "report"
# Inside REPORT_DIR (not BASE_DIR) so the same static server that serves
# index.html/tickers.html also serves this live-updating file -- no
# separate copy step needed for the report's progress-bar JS to poll it.
PROGRESS_FILE_PATH: Path = REPORT_DIR / "progress.json"

# Absolute origin used to build absolute links/ids in feed.json/rss.xml
# (JSON Feed / RSS both expect absolute URLs, unlike the relative links
# elsewhere in the static site). Empty by default -- unchanged local/k3s
# behavior, where there's no stable public URL to point a feed reader at
# (the k3s report is LAN-only). Set MUNGER_REPORT_BASE_URL on any
# deployment with a real public URL (e.g. https://gramunger.com on Cloud
# Run) -- report.py falls back to a relative "." base rather than
# fabricating a domain if this is unset.
REPORT_BASE_URL = os.environ.get("MUNGER_REPORT_BASE_URL", "")

# Cap on how many past days' archives feed.json/rss.xml include. Archives
# grow unbounded (TASKS.md's known retention TODO) -- without a cap the
# feed would too, costing an ever-growing per-generation read of every
# archived CSV (report._render_feed_items reads one to list that day's
# buyable tickers) for items subscribers past this window will never see
# anyway (feed readers only care about recent items).
FEED_MAX_ITEMS = 60
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
GLOBAL_ORDER_BUDGET = 20  # max orders per run
GLOBAL_NOTIONAL_BUDGET_PCT = 0.25  # max fraction of equity moved per run
MIN_UNIVERSE_FETCH_FRACTION = 0.90  # abort if fewer tickers than this fetch cleanly
