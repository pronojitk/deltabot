"""
=============================================================================
                            DONCHIAN STRATEGY
=============================================================================
Trend-following with per-symbol timeframe routing.

  Large caps  (BTC/ETH/XRP)         → 4h, Donchian-20, EMA-50
  High-vol alts (SOL/DOGE/PAXG, …)  → 1h, Donchian-20, EMA-50

Entry rules:
  • LONG  when close > max(highs[-DONCHIAN_PERIOD:-1])  (new N-bar high)
            AND close > EMA(TREND_FILTER_EMA)             (uptrend filter)
            AND EMA7 > EMA21                              (momentum confirmed)
  • SHORT when close < min(lows[-DONCHIAN_PERIOD:-1])    (new N-bar low)
            AND close < EMA(TREND_FILTER_EMA)
            AND EMA7 < EMA21
  • Both gated by Shannon-entropy regime (skip when Choppy)

Exit rules (ForwardTester):
  • Initial SL = entry ∓ ATR × ATR_INITIAL_SL_MULT     (~2× ATR wide)
  • Trailing SL = high-water ∓ ATR × ATR_TRAIL_MULT    (~3× ATR ratchet)
  • Hard timeout at MAX_HOLD_BARS (10-day equivalent per timeframe)

Backtested PF ~1.30 on 1-year 6-symbol universe with realistic fees + slippage.
=============================================================================
"""

import math

from config import (
    EMA_SHORT, EMA_LONG, BREAKOUT_CONFIRM_PCT,
    ENTROPY_LOOKBACK, ENTROPY_BINS, ENTROPY_MAX,
    DONCHIAN_PERIOD, DONCHIAN_EXIT_PERIOD, ATR_PERIOD,
    ATR_INITIAL_SL_MULT, ATR_TRAIL_MULT, TREND_FILTER_EMA,
    DEFAULT_PARAMS,
)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return [0.0] * len(values)
    k = 2.0 / (period + 1)
    result = [0.0] * len(values)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def compute_emas(candles: list[dict]) -> tuple[list[float], list[float]]:
    closes = [c["close"] for c in candles]
    return ema(closes, EMA_SHORT), ema(closes, EMA_LONG)


# ---------------------------------------------------------------------------
# Average True Range (Wilder's smoothing)
# ---------------------------------------------------------------------------

def atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    """Most recent ATR value. 0.0 if not enough data."""
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    # Wilder's smoothing: first ATR = simple avg, then EMA-style with α = 1/period
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


# ---------------------------------------------------------------------------
# Donchian channels
# ---------------------------------------------------------------------------

def donchian_channel(candles: list[dict], period: int) -> tuple[float, float]:
    """
    Returns (upper, lower) of the last `period` completed bars
    (excludes the current bar so a breakout means the *current* bar punched out).
    """
    if len(candles) < period + 1:
        return 0.0, 0.0
    window = candles[-(period + 1):-1]
    upper = max(c["high"] for c in window)
    lower = min(c["low"]  for c in window)
    return upper, lower


# ---------------------------------------------------------------------------
# Shannon entropy regime detector (kept for diagnostic + optional gate)
# ---------------------------------------------------------------------------

def shannon_entropy(values: list[float], bins: int = ENTROPY_BINS) -> float:
    n = len(values)
    if n < bins: return 1.0
    lo, hi = min(values), max(values)
    if hi == lo: return 0.0
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    h = 0.0
    for c in counts:
        if c > 0:
            p = c / n
            h -= p * math.log2(p)
    return h / math.log2(bins)


def returns_entropy(candles: list[dict],
                    lookback: int = ENTROPY_LOOKBACK,
                    bins: int = ENTROPY_BINS) -> float:
    if len(candles) < lookback + 1: return 1.0
    closes = [c["close"] for c in candles[-(lookback + 1):]]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    return shannon_entropy(rets, bins) if rets else 1.0


def market_regime(entropy: float) -> str:
    if entropy <= 0.70: return "Trending"
    if entropy <= 0.85: return "Mixed"
    return "Choppy"


# ---------------------------------------------------------------------------
# Combined signal detection
# ---------------------------------------------------------------------------

def detect_signals(candles: list[dict], _daily: list[dict] | None = None,
                   params: dict | None = None) -> list[dict]:
    """
    Donchian breakout + EMA trend filter + Shannon entropy regime.
    Emits at most one signal per scan.

    `params` allows per-symbol overrides of donchian_period, atr_period,
    atr_initial_sl_mult, atr_trail_mult, trend_filter_ema, max_hold_bars.
    """
    p = params or DEFAULT_PARAMS
    don_period       = p["donchian_period"]
    a_period         = p["atr_period"]
    sl_mult          = p["atr_initial_sl_mult"]
    trail_mult       = p["atr_trail_mult"]
    trend_ema_period = p["trend_filter_ema"]
    max_hold         = p["max_hold_bars"]

    needed = max(don_period, trend_ema_period, a_period) + 5
    if len(candles) < needed:
        return []

    # Entropy regime
    entropy = returns_entropy(candles)
    regime  = market_regime(entropy)
    if ENTROPY_MAX < 1.0 and entropy > ENTROPY_MAX:
        return []

    closes = [c["close"] for c in candles]
    last   = candles[-1]
    price  = last["close"]

    # Trend filter EMA (per-symbol period)
    trend_ema_series = ema(closes, trend_ema_period)
    trend_ema = trend_ema_series[-1]
    if trend_ema == 0.0:
        return []

    e7  = ema(closes, EMA_SHORT)[-1]
    e21 = ema(closes, EMA_LONG)[-1]

    upper, lower = donchian_channel(candles, don_period)
    if upper == 0.0 or lower == 0.0:
        return []

    a = atr(candles, a_period)
    if a <= 0:
        return []

    confirm = price * BREAKOUT_CONFIRM_PCT
    spread_pct = (e7 - e21) / e21 * 100.0 if e21 else 0.0

    base = {
        "ema7":           round(e7, 8),
        "ema21":          round(e21, 8),
        "ema_spread_pct": round(spread_pct, 4),
        "trend_ema":      round(trend_ema, 8),
        "entropy":        round(entropy, 3),
        "regime":         regime,
        "atr":            round(a, 8),
        "donchian_upper": round(upper, 8),
        "donchian_lower": round(lower, 8),
        "close":          price,
        "time":           last["time"],
        "tests":          0,
    }

    # LONG: new N-bar high + above trend EMA (uptrend confirmed)
    if price > upper + confirm and price > trend_ema and e7 > e21:
        sl = price - a * sl_mult
        return [{
            **base,
            "type":            "BREAKOUT",
            "level_label":     f"Donchian-{don_period}H",
            "level_price":     round(upper, 8),
            "sl":              round(sl, 8),
            "trail_distance":  round(a * trail_mult, 8),
            "use_trailing":    True,
            "max_hold_bars":   max_hold,
        }]

    # SHORT: new N-bar low + below trend EMA (downtrend confirmed)
    if price < lower - confirm and price < trend_ema and e7 < e21:
        sl = price + a * sl_mult
        return [{
            **base,
            "type":            "BREAKDOWN",
            "level_label":     f"Donchian-{don_period}L",
            "level_price":     round(lower, 8),
            "sl":              round(sl, 8),
            "trail_distance":  round(a * trail_mult, 8),
            "use_trailing":    True,
            "max_hold_bars":   max_hold,
        }]

    return []


# ===========================================================================
#                              GOLD STRATEGY
# ===========================================================================
#
# PAXGUSDT only — confluence of SMC + Fib + EMA 7/21 across two timeframes:
#
#   1h  (bias / structure):
#       • Break of Structure on 5-bar swing pivots → direction
#       • EMA 7/21 must agree with structure direction
#       • Fib retracement levels of the impulse leg
#
#   15m (entry trigger):
#       • Price inside the 1h OTE zone (0.618 – 0.786 fib)
#       • Reversal candle in bias direction (close > prev > open for longs)
#       • EMA 7/21 alignment confirms entry (e7 > e21 for longs)
#
#   Exits: ATR-based initial SL + trailing stop (same machinery as Donchian).
#
# No other indicators. No entropy. Structure + Fib + EMA confluence only.
# ===========================================================================

_OTE_LOW  = 0.618
_OTE_HIGH = 0.786


def _gold_swings(candles: list[dict], swing_bars: int) -> list[dict]:
    """Confirmed swing pivots, oldest-first. Each: {idx, time, price, type ('H'|'L')}."""
    n = len(candles)
    out: list[dict] = []
    for i in range(swing_bars, n - swing_bars):
        hi, lo = candles[i]["high"], candles[i]["low"]
        is_high = (all(candles[j]["high"] <= hi for j in range(i - swing_bars, i)) and
                   all(candles[j]["high"] <= hi for j in range(i + 1, i + swing_bars + 1)))
        is_low  = (all(candles[j]["low"]  >= lo for j in range(i - swing_bars, i)) and
                   all(candles[j]["low"]  >= lo for j in range(i + 1, i + swing_bars + 1)))
        if is_high:
            out.append({"idx": i, "time": candles[i]["time"], "price": hi, "type": "H"})
        if is_low:
            out.append({"idx": i, "time": candles[i]["time"], "price": lo, "type": "L"})
    return out


def _last_swing_before(swings: list[dict], idx: int, kind: str) -> dict | None:
    for sw in reversed(swings):
        if sw["idx"] < idx and sw["type"] == kind:
            return sw
    return None


def _gold_detect_bos(candles: list[dict], swings: list[dict], lookback: int) -> dict | None:
    """Most recent BOS within `lookback` bars of the latest candle."""
    if not swings or not candles:
        return None
    n = len(candles)
    cutoff = max(0, n - lookback)
    for sw in reversed(swings):
        for j in range(sw["idx"] + 1, n):
            if j < cutoff:
                continue
            close = candles[j]["close"]
            if sw["type"] == "H" and close > sw["price"]:
                prior = _last_swing_before(swings, sw["idx"], "L")
                if prior is None:
                    return None
                impulse_high = max(candles[k]["high"] for k in range(sw["idx"], j + 1))
                return {"direction": "BULL", "bos_idx": j,
                        "impulse_low": prior["price"], "impulse_high": impulse_high}
            if sw["type"] == "L" and close < sw["price"]:
                prior = _last_swing_before(swings, sw["idx"], "H")
                if prior is None:
                    return None
                impulse_low = min(candles[k]["low"] for k in range(sw["idx"], j + 1))
                return {"direction": "BEAR", "bos_idx": j,
                        "impulse_low": impulse_low, "impulse_high": prior["price"]}
    return None


def _gold_fibs(low: float, high: float, direction: str) -> dict:
    span = abs(high - low)
    if span <= 0:
        return {}
    ratios = [0.0, 0.382, 0.5, 0.618, 0.786, 1.0]
    if direction == "BULL":
        return {r: high - r * span for r in ratios}
    return {r: low + r * span for r in ratios}


def _in_ote(price: float, fibs: dict) -> bool:
    if not fibs:
        return False
    a, b = fibs[_OTE_LOW], fibs[_OTE_HIGH]
    lo, hi = min(a, b), max(a, b)
    return lo <= price <= hi


def detect_gold_signal(candles_15m: list[dict],
                       candles_1h:  list[dict],
                       params: dict | None = None) -> list[dict]:
    """
    PAXG: SMC + Fib OTE + EMA 7/21 confluence, 1h bias / 15m entry.
    Returns at most one signal per call. No EMA filter on price-cross — EMA 7/21
    alignment is required on BOTH timeframes as a confluence filter only.
    """
    p = params or {}
    es           = p.get("ema_short", 7)
    el           = p.get("ema_long", 21)
    swing_bars   = p.get("swing_bars", 5)
    bos_lookback = p.get("bos_lookback", 30)
    a_period    = p.get("atr_period", 14)
    sl_mult     = p.get("atr_initial_sl_mult", 2.0)
    trail_mult  = p.get("atr_trail_mult", 3.0)
    max_hold    = p.get("max_hold_bars", 96)

    if len(candles_1h) < max(swing_bars * 2 + 5, el + 2):
        return []
    if len(candles_15m) < max(a_period + 3, el + 2):
        return []

    # ─── 1h bias: SMC structure + EMA 7/21 must agree ─────────────────────
    swings = _gold_swings(candles_1h, swing_bars)
    bos    = _gold_detect_bos(candles_1h, swings, bos_lookback)
    if bos is None:
        return []

    closes_1h = [c["close"] for c in candles_1h]
    e7_1h  = ema(closes_1h, es)[-1]
    e21_1h = ema(closes_1h, el)[-1]
    if e7_1h == 0.0 or e21_1h == 0.0:
        return []

    if bos["direction"] == "BULL" and not (e7_1h > e21_1h):
        return []                       # structure says up but EMA disagrees → skip
    if bos["direction"] == "BEAR" and not (e7_1h < e21_1h):
        return []

    # ─── 1h fib levels of the impulse leg ─────────────────────────────────
    fibs = _gold_fibs(bos["impulse_low"], bos["impulse_high"], bos["direction"])
    if not fibs:
        return []

    # ─── 15m entry: price in OTE + reversal candle + EMA 7/21 aligned ─────
    last  = candles_15m[-1]
    prev  = candles_15m[-2]
    price = last["close"]

    if not _in_ote(price, fibs):
        return []

    closes_15m = [c["close"] for c in candles_15m]
    e7_15  = ema(closes_15m, es)[-1]
    e21_15 = ema(closes_15m, el)[-1]
    if e7_15 == 0.0 or e21_15 == 0.0:
        return []

    a = atr(candles_15m, a_period)
    if a <= 0:
        return []

    base = {
        "ema7":         round(e7_15, 8),
        "ema21":        round(e21_15, 8),
        "ema7_1h":      round(e7_1h, 8),
        "ema21_1h":     round(e21_1h, 8),
        "atr":          round(a, 8),
        "smc_dir":      bos["direction"],
        "impulse_low":  round(bos["impulse_low"], 8),
        "impulse_high": round(bos["impulse_high"], 8),
        "fib_0_618":    round(fibs[0.618], 8),
        "fib_0_786":    round(fibs[0.786], 8),
        "close":        price,
        "time":         last["time"],
        "tests":        0,
    }

    # LONG: 1h bull BOS + 1h EMA bullish + 15m EMA bullish + OTE + reversal
    if (bos["direction"] == "BULL"
            and e7_15 > e21_15
            and last["close"] > prev["close"]
            and last["close"] > last["open"]):
        sl = price - a * sl_mult
        return [{
            **base,
            "type":           "BREAKOUT",
            "level_label":    "OTE-Long",
            "level_price":    round(fibs[0.618], 8),
            "sl":             round(sl, 8),
            "trail_distance": round(a * trail_mult, 8),
            "use_trailing":   True,
            "max_hold_bars":  max_hold,
        }]

    # SHORT: 1h bear BOS + 1h EMA bearish + 15m EMA bearish + OTE + reversal
    if (bos["direction"] == "BEAR"
            and e7_15 < e21_15
            and last["close"] < prev["close"]
            and last["close"] < last["open"]):
        sl = price + a * sl_mult
        return [{
            **base,
            "type":           "BREAKDOWN",
            "level_label":    "OTE-Short",
            "level_price":    round(fibs[0.618], 8),
            "sl":             round(sl, 8),
            "trail_distance": round(a * trail_mult, 8),
            "use_trailing":   True,
            "max_hold_bars":  max_hold,
        }]

    return []


# ---------------------------------------------------------------------------
# Strategy dispatcher — picks Donchian or Gold based on params["strategy"]
# ---------------------------------------------------------------------------

def detect_signals_for_symbol(candles_entry: list[dict],
                              candles_htf:   list[dict] | None,
                              params: dict) -> list[dict]:
    """
    Single entry-point used by bot.py and backtest.py.
    Routes to detect_gold_signal or detect_signals depending on params["strategy"].
    """
    strategy = params.get("strategy", "donchian")
    if strategy == "gold":
        return detect_gold_signal(candles_entry, candles_htf or [], params=params)
    return detect_signals(candles_entry, params=params)


# ---------------------------------------------------------------------------
# Compatibility shims
# ---------------------------------------------------------------------------

def get_all_levels(candles: list[dict], _daily=None) -> list[tuple[str, float]]:
    """For dashboard context: Donchian channel + EMA trend filter."""
    if len(candles) < DONCHIAN_PERIOD + 1:
        return []
    upper, lower = donchian_channel(candles, DONCHIAN_PERIOD)
    closes = [c["close"] for c in candles]
    tema = ema(closes, TREND_FILTER_EMA)[-1] if len(closes) >= TREND_FILTER_EMA else 0.0
    out = [("Donchian-H", upper), ("Donchian-L", lower)]
    if tema > 0:
        out.append((f"EMA{TREND_FILTER_EMA}", tema))
    return out


def count_level_tests(*_args, **_kwargs) -> int:
    return 0
