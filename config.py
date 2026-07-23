"""Every threshold and toggle for the Graham-Munger bot.

Nothing downstream hard-codes a number or flag — it imports from here.
See DESIGN.md for the rationale behind each value.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

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
DATA_RAW_CACHE_DIR: Path = BASE_DIR / "data_cache"

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
STRIKES_TO_LIQUIDATE = 2

# --- Execution module (DESIGN.md 3.5) ---
PAPER_TRADING = True  # live trading only after months of clean paper runs
LIMIT_PRICE_BAND_PCT = 0.02  # +/-2% of last trade, applied to every order

# --- State, audit, and observability (DESIGN.md 3.6) ---
# Anchored to BASE_DIR (not bare relative filenames) so a scheduler
# invoking bot.py from a different working directory than an interactive
# run still reads/writes the same state.json instead of silently starting
# from a fresh, empty strike history.
STATE_FILE_PATH: Path = BASE_DIR / "state.json"
JOURNAL_DB_PATH: Path = BASE_DIR / "journal.db"
SCREEN_RESULTS_CSV_PATH: Path = BASE_DIR / "screen_results.csv"
SCREEN_RESULTS_ARCHIVE_DIR: Path = BASE_DIR / "screen_results_archive"
LOG_FILE_PATH: Path = BASE_DIR / "munger.log"
REPORT_DIR: Path = BASE_DIR / "report"
# Inside REPORT_DIR (not BASE_DIR) so the same static server that serves
# index.html/tickers.html also serves this live-updating file -- no
# separate copy step needed for the report's progress-bar JS to poll it.
PROGRESS_FILE_PATH: Path = REPORT_DIR / "progress.json"
# DESIGN.md's PM-recommendations narrative illustrates this as "> 24
# hours," written before the quarterly cadence was settled (M1) -- a
# literal 24-hour threshold would fire on every single healthy quarterly
# gap. Set instead as a dead-man's-switch tolerance around the actual
# cadence: quarterly (~91 days) plus the "2-3 weeks after quarter-end"
# scheduling offset (DESIGN.md 4), plus buffer, so it only fires if a
# scheduled run was actually missed, not on ordinary cadence drift.
DATA_FRESHNESS_MAX_HOURS = 130 * 24

# --- Risk controls (DESIGN.md 5) ---
KILL_SWITCH = False
KILL_SWITCH_FLAG_FILE_PATH: Path = BASE_DIR / "KILL_SWITCH"
GLOBAL_ORDER_BUDGET = 20  # max orders per run
GLOBAL_NOTIONAL_BUDGET_PCT = 0.25  # max fraction of equity moved per run
MIN_UNIVERSE_FETCH_FRACTION = 0.90  # abort if fewer tickers than this fetch cleanly
