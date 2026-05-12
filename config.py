import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Delta Exchange API
#   "india"  → https://api.india.delta.exchange  (more INR-settled symbols)
#   "global" → https://api.delta.exchange        (default global exchange)
DELTA_REGION = os.getenv("DELTA_REGION", "india").lower()
DELTA_BASE_URL = (
    "https://api.india.delta.exchange" if DELTA_REGION == "india"
    else "https://api.delta.exchange"
)

# Optional API credentials (only needed for authenticated endpoints / live trading)
DELTA_API_KEY    = os.getenv("DELTA_API_KEY", "")
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "")

# Scanning parameters
STRATEGY_NAME = "Donchian"  # Trend-following: Donchian breakout + EMA + ATR trail + entropy

TIMEFRAME = "15m"          # default timeframe (per-symbol overrides in SYMBOL_PARAMS)
CANDLE_LIMIT = 300         # bars to fetch per scan
SCAN_INTERVAL = 60         # seconds between full scans
REQUEST_DELAY = 0.3        # seconds between API calls to avoid rate limiting

# ─── Symbol universe filters ────────────────────────────────────────────────
MAX_SYMBOLS           = 40       # keep only the top-N perpetuals by 24h volume
MIN_LISTING_AGE_DAYS  = 30       # skip newly listed contracts (whippy / unproven)
# Always include these majors, even when their turnover field is null on Delta India.
FORCE_INCLUDE_SYMBOLS = [
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "DOGEUSD",
    "ADAUSD", "LTCUSD", "BCHUSD", "AVAXUSD", "LINKUSD", "DOTUSD",
    "MATICUSD", "TRXUSD", "ATOMUSD", "NEARUSD", "PAXGUSD",
]

# EMA periods
EMA_SHORT = 7
EMA_LONG = 21

# Signal thresholds
# A level is considered "tested multiple times" if price touches it this many times
MIN_LEVEL_TESTS = 2

# Price proximity: how close to a level counts as a "touch" (as % of price)
LEVEL_PROXIMITY_PCT = 0.003   # 0.3%

# Breakout confirmation: candle close must be this far beyond level (as % of price)
BREAKOUT_CONFIRM_PCT = 0.001  # 0.1%

# Retest filter: only enter after price broke a level, retested it, and bounced.
# Looks back this many bars for the breakout+retest pattern. 0 disables (= old behavior).
RETEST_LOOKBACK = 8

# Shannon entropy regime filter — only allow signals when market is trending
# (low normalized entropy) rather than choppy (high entropy).
#   ENTROPY_LOOKBACK : how many recent 15m bars of returns to analyse
#   ENTROPY_BINS     : histogram bin count (8 ≈ standard for return distributions)
#   ENTROPY_MAX      : signals blocked when normalized entropy > this. Range [0,1].
#                      0.85 ≈ filter the choppiest 30-40% of bars. Set to 1.0 to disable.
ENTROPY_LOOKBACK = 64
ENTROPY_BINS     = 8
ENTROPY_MAX      = 0.85

# Cooldown: don't re-alert the same coin+signal within this many seconds
ALERT_COOLDOWN = 3600         # 1 hour

# ─── Forward-Test Account ──────────────────────────────────────────────────
STARTING_BALANCE  = 240.0     # USD (matches real Delta margin)
ACCOUNT_CURRENCY  = "USD"
ACCOUNT_SYMBOL    = "$"
LEVERAGE          = 25        # 25x
RISK_PER_TRADE_PCT = 1.0      # unused when FIXED_MARGIN_PER_TRADE is set
FIXED_MARGIN_PER_TRADE = 40.0 # $40 margin per trade → $1000 notional at 25x
STOP_LOSS_PCT     = 0.02      # 2%
TAKE_PROFIT_PCT   = 0.015     # 1.5%
MAX_HOLD_BARS     = 960       # extended: 960 × 15m = 10 days, gives shorts time to play out

# Real trading costs — applied per trade in ForwardTester
TAKER_FEE_PCT     = 0.0005    # 0.05% per side (Delta India taker fee)
SLIPPAGE_PCT      = 0.0003    # 0.03% adverse fill per side

# ─── Donchian Trend-Following Strategy ─────────────────────────────────────
DONCHIAN_PERIOD       = 20    # bars for breakout channel (20 × 4h = 3.3 days)
DONCHIAN_EXIT_PERIOD  = 10    # bars for tighter exit channel (faster exits)
ATR_PERIOD            = 14
ATR_INITIAL_SL_MULT   = 2.0   # initial stop = entry ∓ ATR × this
ATR_TRAIL_MULT        = 3.0   # trailing stop = high-water ∓ ATR × this
TREND_FILTER_EMA      = 50    # only LONG when close > EMA50, only SHORT when below
TRAILING_STOP_ENABLED = True  # use ATR trailing stop instead of fixed TP

# Bars per 24h, indexed by Delta resolution string
BARS_PER_DAY = {"15m": 96, "30m": 48, "1h": 24, "2h": 12, "4h": 6, "1d": 1}

# Per-symbol parameter overrides — different coins want different timeframes.
# Symbols not in this dict use DEFAULT_PARAMS.
DEFAULT_PARAMS = {
    "strategy":             "donchian",
    "timeframe":            TIMEFRAME,           # "1h" — works well for high-vol alts
    "donchian_period":      DONCHIAN_PERIOD,
    "donchian_exit_period": DONCHIAN_EXIT_PERIOD,
    "atr_period":           ATR_PERIOD,
    "atr_initial_sl_mult":  ATR_INITIAL_SL_MULT,
    "atr_trail_mult":       ATR_TRAIL_MULT,
    "trend_filter_ema":     TREND_FILTER_EMA,
    "max_hold_bars":        MAX_HOLD_BARS,
}

# Donchian — large caps on 1h
_LARGE_CAP_1H = {**DEFAULT_PARAMS, "timeframe": "1h", "max_hold_bars": 240}

# Gold strategy — pure SMC + Fib OTE, multi-timeframe.
#   • 1h: market structure (BOS) determines bias and impulse leg
#   • 15m: entry trigger when price is in the 1h OTE zone (0.618–0.786 fib)
# No EMA, no entropy filter — structure + fib only.
_GOLD_PARAMS = {
    "strategy":             "gold",
    "timeframe":            "15m",   # entry execution timeframe
    "htf_timeframe":        "1h",    # structure / bias timeframe
    "swing_bars":           5,       # bars each side that confirm a 1h pivot
    "bos_lookback":         30,      # max 1h bars since last BOS
    "atr_period":           14,
    "atr_initial_sl_mult":  2.5,     # widened: was 2.0
    "atr_trail_mult":       4.0,     # widened: was 3.0
    "max_hold_bars":        96,      # 96 × 15m = 24h
}

SYMBOL_PARAMS: dict = {
    # Donchian strategy — per-symbol routing.
    "BTCUSDT": _LARGE_CAP_1H,
    "ETHUSDT": _LARGE_CAP_1H,
    "XRPUSDT": _LARGE_CAP_1H,
    # Gold strategy — multi-timeframe entries on PAXG only.
    "PAXGUSDT": _GOLD_PARAMS,
    # SOL, DOGE, and all other unlisted symbols → DEFAULT_PARAMS (Donchian, 15m).
}


def get_params(symbol: str) -> dict:
    """Return strategy params for a symbol — falls back to DEFAULT_PARAMS."""
    return SYMBOL_PARAMS.get(symbol, DEFAULT_PARAMS)

# Web dashboard
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000
