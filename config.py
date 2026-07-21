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
EXCLUDED_SECTORS: tuple[str, ...] = ()
UNIVERSE_MIN_TICKER_COUNT = 490
UNIVERSE_MAX_TICKER_COUNT = 510
STATIC_UNIVERSE_FALLBACK_PATH = "data/sp500_fallback.csv"

# --- Data module (DESIGN.md 3.2) ---
DATA_FETCH_THREAD_POOL_WORKERS = 12
DATA_FETCH_MAX_RETRIES = 3
DATA_FETCH_RETRY_BACKOFF_SECONDS = 2.0

# Sanity bounds for validate_metrics (DESIGN.md 3.2, Deliverable 1.3)
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
DATA_FRESHNESS_MAX_HOURS = 24

# --- Risk controls (DESIGN.md 5) ---
KILL_SWITCH = False
KILL_SWITCH_FLAG_FILE_PATH: Path = BASE_DIR / "KILL_SWITCH"
GLOBAL_ORDER_BUDGET = 20  # max orders per run
GLOBAL_NOTIONAL_BUDGET_PCT = 0.25  # max fraction of equity moved per run
MIN_UNIVERSE_FETCH_FRACTION = 0.90  # abort if fewer tickers than this fetch cleanly
