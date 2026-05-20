"""
Markov regime scoring for the bot's symbol universe.

Lifted from the markov-hedge-fund-method skill (Roan @RohOnChain framework,
installed as a Claude Code skill). Vendored here so deltabot has zero
runtime dependency on the user's `~/.claude/skills/` install.

What it does
============
For each symbol, fetch ~5 years of daily candles, label every day as
Bear / Sideways / Bull from a 20-day rolling return, build the 3×3
transition matrix, solve the stationary distribution, and run a
walk-forward backtest (no lookahead). Returns:

    sharpe              annualised Sharpe of the walk-forward strategy
    max_dd              max drawdown of that equity curve
    bull_persist        P(Bull → Bull) — how sticky uptrends are
    bear_persist        P(Bear → Bear)
    stationary_bull     long-run fraction of days in Bull regime
    n_rows              data length

Scores are persisted to a SQLite table (`markov_scores`) so we don't
recompute on every scan. Background refresh runs once per day.
"""

from __future__ import annotations

import math
import sqlite3
import time
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("markov")

# Use the same SQLite DB as everything else in deltabot
DB_FILE = Path(__file__).parent / "ft_state.db"

# Background refresh cadence (seconds). 24h is plenty — regime drift is slow.
REFRESH_INTERVAL_SEC = 24 * 3600


# ─────────────────────────────────────────── regime math (pure python) ──
def _rolling_return(closes: list[float], window: int) -> list[float]:
    """closes[i] / closes[i-window] - 1, NaN for the first `window` rows."""
    out = [float("nan")] * len(closes)
    for i in range(window, len(closes)):
        prev = closes[i - window]
        if prev:
            out[i] = closes[i] / prev - 1.0
    return out


def _label_regimes(closes: list[float], window: int = 20, threshold: float = 0.02) -> list[int]:
    """0=Bear, 1=Sideways, 2=Bull. Returns labels at indices ≥ window."""
    rr = _rolling_return(closes, window)
    labels: list[int] = []
    for v in rr:
        if math.isnan(v): continue
        if   v >  threshold: labels.append(2)
        elif v < -threshold: labels.append(0)
        else:                labels.append(1)
    return labels


def _transition_matrix(labels: list[int]) -> list[list[float]]:
    """3×3 MLE transition matrix from a label sequence."""
    n = 3
    counts = [[0.0]*n for _ in range(n)]
    for i in range(len(labels) - 1):
        counts[labels[i]][labels[i+1]] += 1
    P = [[0.0]*n for _ in range(n)]
    for r in range(n):
        s = sum(counts[r]) or 1.0
        for c in range(n):
            P[r][c] = counts[r][c] / s
    return P


def _stationary(P: list[list[float]], iters: int = 200) -> list[float]:
    """Solve via power iteration (no numpy needed)."""
    n = len(P)
    v = [1.0 / n] * n
    for _ in range(iters):
        nxt = [0.0] * n
        for j in range(n):
            for i in range(n):
                nxt[j] += v[i] * P[i][j]
        s = sum(nxt) or 1.0
        v = [x / s for x in nxt]
    return v


def _signal(P: list[list[float]], current: int) -> int:
    """Sign of P(next=Bull|current) - P(next=Bear|current). +1 / 0 / -1."""
    diff = P[current][2] - P[current][0]
    return 1 if diff > 0 else -1 if diff < 0 else 0


def _walk_forward(closes: list[float], labels: list[int], min_train: int = 252) -> tuple[float, float, int]:
    """Returns (sharpe_annualised, max_drawdown, n_trades)."""
    # Need len(closes) == len(labels) + window. Align: labels start at index `window`.
    offset = len(closes) - len(labels)
    if offset < 0: return float("nan"), float("nan"), 0
    # Pre-compute daily returns aligned to labels
    daily_returns: list[float] = []
    for i in range(1, len(labels)):
        ci = offset + i
        prev = closes[ci - 1]
        daily_returns.append((closes[ci] / prev - 1.0) if prev else 0.0)
    # daily_returns[t] = return realised AFTER labels[t]

    if len(labels) < min_train + 30: return float("nan"), float("nan"), 0

    strat_rets: list[float] = []
    for t in range(min_train, len(labels) - 1):
        P_t = _transition_matrix(labels[:t])
        side = _signal(P_t, labels[t])
        strat_rets.append(side * daily_returns[t])

    if not strat_rets: return float("nan"), float("nan"), 0

    n = len(strat_rets)
    mean = sum(strat_rets) / n
    var  = sum((x - mean)**2 for x in strat_rets) / max(1, n - 1)
    std  = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(252)) if std > 0 else float("nan")

    # Drawdown
    equity = 1.0
    peak   = 1.0
    max_dd = 0.0
    for r in strat_rets:
        equity *= (1.0 + r)
        if equity > peak: peak = equity
        dd = (equity - peak) / peak if peak else 0.0
        if dd < max_dd: max_dd = dd

    return sharpe, max_dd, n


def score_series(closes: list[float], window: int = 20, threshold: float = 0.02) -> dict:
    """Score a single price series. Returns a dict ready to insert into DB."""
    if not closes or len(closes) < 300:
        return {"sharpe": float("nan"), "max_dd": float("nan"),
                "bull_persist": float("nan"), "bear_persist": float("nan"),
                "stationary_bull": float("nan"), "n_rows": len(closes)}
    labels = _label_regimes(closes, window=window, threshold=threshold)
    if len(labels) < 100:
        return {"sharpe": float("nan"), "max_dd": float("nan"),
                "bull_persist": float("nan"), "bear_persist": float("nan"),
                "stationary_bull": float("nan"), "n_rows": len(closes)}
    P = _transition_matrix(labels)
    pi = _stationary(P)
    sharpe, max_dd, _ = _walk_forward(closes, labels)
    return {
        "sharpe":          float(sharpe) if not math.isnan(sharpe) else None,
        "max_dd":          float(max_dd) if not math.isnan(max_dd) else None,
        "bull_persist":    P[2][2],
        "bear_persist":    P[0][0],
        "stationary_bull": pi[2],
        "n_rows":          len(closes),
    }


# ─────────────────────────────────────────── SQLite cache ──
def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_FILE, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def _init_schema():
    db = _connect()
    db.execute("""
      CREATE TABLE IF NOT EXISTS markov_scores (
        symbol           TEXT PRIMARY KEY,
        source           TEXT,
        sharpe           REAL,
        max_dd           REAL,
        bull_persist     REAL,
        bear_persist     REAL,
        stationary_bull  REAL,
        n_rows           INTEGER,
        last_updated     INTEGER
      )
    """)
    db.close()


def save_score(symbol: str, source: str, score: dict):
    _init_schema()
    db = _connect()
    db.execute("""INSERT OR REPLACE INTO markov_scores
        (symbol, source, sharpe, max_dd, bull_persist, bear_persist,
         stationary_bull, n_rows, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?)""", (
        symbol, source,
        score.get("sharpe"), score.get("max_dd"),
        score.get("bull_persist"), score.get("bear_persist"),
        score.get("stationary_bull"), score.get("n_rows"),
        int(time.time()),
    ))
    db.close()


def get_score(symbol: str) -> dict | None:
    _init_schema()
    db = _connect()
    row = db.execute("SELECT * FROM markov_scores WHERE symbol=?", (symbol,)).fetchone()
    db.close()
    return dict(row) if row else None


def get_all_scores() -> list[dict]:
    _init_schema()
    db = _connect()
    rows = db.execute("SELECT * FROM markov_scores ORDER BY sharpe DESC NULLS LAST").fetchall()
    db.close()
    return [dict(r) for r in rows]


def is_tradeable(symbol: str, min_sharpe: float) -> bool:
    """True if symbol has Markov Sharpe >= min_sharpe. If no score exists,
    we allow it (don't block trades on unscored symbols)."""
    s = get_score(symbol)
    if s is None or s.get("sharpe") is None:
        return True
    return s["sharpe"] >= min_sharpe


# ─────────────────────────────────────────── fetchers per source ──
def _closes_from_delta(symbol: str, years: int = 5) -> list[float]:
    """Daily closes from Delta Exchange (~1825 daily candles for 5y)."""
    from delta_client import get_ohlcv
    bars = years * 365 + 50  # buffer
    candles = get_ohlcv(symbol, "1d", bars) or []
    return [c["close"] for c in candles]


def _closes_from_yfinance(symbol: str, years: int = 5) -> list[float]:
    """Daily closes via yfinance (for .NS symbols)."""
    try:
        import yfinance as yf
        import pandas as pd
        end = pd.Timestamp.utcnow().normalize()
        start = end - pd.DateOffset(years=years)
        df = yf.download(symbol, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         progress=False, auto_adjust=True, threads=False)
        if df is None or df.empty: return []
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        return df["Close"].dropna().tolist()
    except Exception as e:
        logger.debug("yfinance fetch failed for %s: %s", symbol, e)
        return []


# ─────────────────────────────────────────── batch refresh ──
def refresh_universe(symbols_crypto: list[str], symbols_nse: list[str]):
    """Recompute scores for both universes. Runs sequentially with backoff."""
    _init_schema()
    logger.info("Markov refresh: %d crypto + %d nse symbols",
                len(symbols_crypto), len(symbols_nse))
    done = 0
    for sym in symbols_crypto:
        try:
            closes = _closes_from_delta(sym, years=5)
            score = score_series(closes)
            save_score(sym, "delta", score)
            done += 1
        except Exception as e:
            logger.warning("Markov %s: %s", sym, e)
        time.sleep(0.4)   # gentle on the API
    for sym in symbols_nse:
        try:
            closes = _closes_from_yfinance(sym, years=5)
            score = score_series(closes)
            save_score(sym, "yfinance", score)
            done += 1
        except Exception as e:
            logger.warning("Markov %s: %s", sym, e)
        time.sleep(0.5)
    logger.info("Markov refresh complete: %d symbols scored", done)


# ─────────────────────────────────────────── background thread ──
_refresh_thread: threading.Thread | None = None
_refresh_stop = threading.Event()


def start_background_refresh(symbols_crypto_fn, symbols_nse_fn):
    """Start a daemon thread that refreshes scores once now and then every
    REFRESH_INTERVAL_SEC. Pass thunks so we re-read the universe each cycle
    (which can change when new contracts list / delist)."""
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive(): return

    def _loop():
        # Short delay so the engine boots cleanly before we start hammering
        time.sleep(15)
        while not _refresh_stop.is_set():
            try:
                refresh_universe(symbols_crypto_fn() or [], symbols_nse_fn() or [])
            except Exception as e:
                logger.error("Markov refresh error: %s", e)
            # Sleep in chunks so stop is responsive
            for _ in range(REFRESH_INTERVAL_SEC):
                if _refresh_stop.is_set(): return
                time.sleep(1)

    _refresh_thread = threading.Thread(target=_loop, daemon=True, name="markov-refresh")
    _refresh_thread.start()
    logger.info("Markov background refresh thread started")


def stop_background_refresh():
    _refresh_stop.set()
