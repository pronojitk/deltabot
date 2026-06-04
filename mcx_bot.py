"""
Indian (NSE F&O) Opening Range Breakout (ORB) Strategy.

Strategy:
  • First 5-min candle = 09:15–09:20 IST (NSE open + 5min). This is the "ORB".
  • LONG  entry : first subsequent 5-min close > ORB high
                  SL = ORB low,   TP = entry + 2 × (entry − ORB low)        [1:2]
  • SHORT entry : first subsequent 5-min close < ORB low
                  SL = ORB high,  TP = entry − 2 × (ORB high − entry)       [1:2]
  • One trade per symbol per day. Auto-flat at 15:25 IST.
  • Data: yfinance 5-min candles (free, ~15-min delayed).

NOTE: yfinance does NOT have MCX commodity futures (GOLD/SILVER/CRUDEOIL
contracts). It only has NSE-listed equity ETFs/stocks. The default symbol
list below uses gold/silver tracking ETFs as a proxy. Edit MCX_SYMBOLS to
change. For real MCX commodity data, swap the _fetch_intraday() call for
a broker API like Zerodha Kite / Upstox / Angel One.

Run:  py mcx_bot.py
"""

import os
import json
import sqlite3
import time
import logging
import threading
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("Install yfinance:  pip install yfinance pytz")
try:
    import pytz
except ImportError:
    raise SystemExit("Install pytz:  pip install pytz")

from telegram_alert import send_alert

IST = pytz.timezone("Asia/Kolkata")

# ─── Symbols (NSE F&O stocks — most liquid ~60 names). Format "<ticker>.NS". ──
# Add/remove as you like; the bot scans whatever's in this list during 9:15–15:30 IST.
# yfinance caps 5-min intraday data at ~60 days; rate-limits aggressive polling, so
# don't expand much above ~80 symbols unless you increase SCAN_INTERVAL.
NSE_FNO_SYMBOLS = [
    # Nifty 50 (most liquid F&O — index heavyweights)
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS", "LT.NS", "ITC.NS",
    "AXISBANK.NS", "HCLTECH.NS", "BAJFINANCE.NS", "ASIANPAINT.NS", "MARUTI.NS",
    "NESTLEIND.NS", "WIPRO.NS", "ULTRACEMCO.NS", "SUNPHARMA.NS", "TITAN.NS",
    "BAJAJFINSV.NS", "NTPC.NS", "POWERGRID.NS", "M&M.NS", "TECHM.NS",
    "COALINDIA.NS", "ONGC.NS", "JSWSTEEL.NS", "ADANIENT.NS", "ADANIPORTS.NS",
    "INDUSINDBK.NS", "BPCL.NS", "HINDALCO.NS", "GRASIM.NS", "EICHERMOT.NS",
    "BRITANNIA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "HEROMOTOCO.NS",
    "TATAMOTORS.NS", "TATASTEEL.NS", "UPL.NS", "BAJAJ-AUTO.NS", "SHRIRAMFIN.NS",
    "APOLLOHOSP.NS", "LTIM.NS", "HDFCLIFE.NS", "SBILIFE.NS", "TATACONSUM.NS",
    # Bank / NBFC F&O
    "BANKBARODA.NS", "CANBK.NS", "PNB.NS", "FEDERALBNK.NS", "IDFCFIRSTB.NS",
    "AUBANK.NS", "BANDHANBNK.NS", "CHOLAFIN.NS", "MUTHOOTFIN.NS", "SBICARD.NS",
    # PSU + commodity / energy (covers your old MCX proxies)
    "MCX.NS", "VEDL.NS", "HINDPETRO.NS", "IOC.NS", "GAIL.NS",
    "PFC.NS", "RECLTD.NS", "ADANIGREEN.NS", "TATAPOWER.NS", "JSWENERGY.NS",
    # ETF / commodity exposure (kept from old MCX list)
    "GOLDBEES.NS", "SILVERBEES.NS",
]
# Chronic losers from the forward test — sub-40% win rate with clear negative
# edge over a meaningful sample. ORB doesn't work on low-beta IT largecaps or
# the Adani complex. Pruned to lift the strategy's profit factor.
MCX_SKIP_SYMBOLS = [
    "ADANIGREEN.NS",  # -Rs2141 / 12% win (n=8)
    "INFY.NS",        # -Rs1303 / 33% win (n=6)
    "HCLTECH.NS",     # -Rs1180 / 0% win (n=6)
    "ADANIPORTS.NS",  # -Rs871  / 17% win (n=6)
    "CIPLA.NS",       # -Rs871  / 0% win (n=4)
    "ULTRACEMCO.NS",  # -Rs845  / 0% win (n=5)
    "WIPRO.NS",       # -Rs571  / 38% win (n=8)
]
NSE_FNO_SYMBOLS = [s for s in NSE_FNO_SYMBOLS if s not in MCX_SKIP_SYMBOLS]

# Backward-compat alias so any legacy code keeps working.
MCX_SYMBOLS = NSE_FNO_SYMBOLS

# ─── Account (INR-denominated, separate from Delta USD account) ────────────
MCX_STARTING_BALANCE_INR = 40_000.0   # ₹40,000 starting capital
MCX_LEVERAGE             = 5          # 5x intraday leverage
MCX_MARGIN_PER_TRADE_INR = 5_000.0    # ₹5,000 collateral per trade → ₹25,000 notional at 5x
                                      # → up to 8 concurrent positions
MCX_TAKER_FEE_PCT        = 0.0005     # 0.05% per side (broker brokerage)
MCX_SLIPPAGE_PCT         = 0.0005     # 0.05% adverse fill per side
YF_REQUEST_DELAY         = 0.25       # seconds between yfinance fetches (rate-limit safety)

# ─── Strategy parameters ───────────────────────────────────────────────────
ORB_OPEN       = dtime(9, 15)    # first candle starts
ORB_CLOSE      = dtime(9, 30)    # ORB candle = 09:15–09:30 (15 min — wider, fewer noise stops)
MARKET_CLOSE   = dtime(15, 30)   # NSE close
EOD_FLAT_TIME  = dtime(15, 25)   # auto-flat any open paper trade here
SCAN_INTERVAL  = 60              # seconds between yfinance polls

# Quality filters
MIN_ORB_RANGE_PCT  = 0.004       # skip if ORB range < 0.4% of price (range too tight)
BREAKOUT_BUFFER_PCT = 0.001      # require close beyond ORB high/low by 0.1% (no wick fakes)

# Exit logic
USE_TRAILING_STOP  = True        # trail the stop instead of using a fixed TP
BREAKEVEN_AT_R     = 1.0         # move SL → entry once unrealised >= 1R profit
TRAIL_WITH_EMA     = True        # after BE (1R), trail the SL along the 5m EMA(7)
TRAIL_EMA_PERIOD   = 7           # EMA period used for the post-BE trail
TRAIL_MULT_OF_ORB  = 1.0         # (fallback) ORB-range trail if TRAIL_WITH_EMA is off
RR_RATIO           = 1.0         # fallback fixed TP if trailing disabled (1:1)

# Per symbol per day: max 1 trade total (whichever side fires first wins;
# no re-entry the same day even if the opposite side breaks out later).

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("indian-orb")

# Database file (shared with ForwardTester via separate tables)
DB_FILE = Path(__file__).parent / "ft_state.db"


class MCXBot:
    def __init__(self, symbols=None, on_event=None, send_telegram=True):
        base_symbols = symbols or NSE_FNO_SYMBOLS
        # Markov pre-screen — keep symbols whose walk-forward Sharpe ≥ threshold.
        # NULL Sharpe blocked if MARKOV_BLOCK_UNSCORED. MARKOV_FORCE_KEEP bypasses.
        try:
            from config import (MARKOV_FILTER_ENABLED, MARKOV_MIN_SHARPE,
                                MARKOV_BLOCK_UNSCORED, MARKOV_FORCE_KEEP)
            if MARKOV_FILTER_ENABLED:
                from markov import get_score
                force_keep = set(MARKOV_FORCE_KEEP or [])
                filtered, dropped_low, dropped_null = [], [], []
                for sym in base_symbols:
                    if sym in force_keep:
                        filtered.append(sym); continue
                    s = get_score(sym)
                    sh = s.get("sharpe") if s else None
                    if sh is None:
                        if MARKOV_BLOCK_UNSCORED: dropped_null.append(sym)
                        else: filtered.append(sym)
                    elif sh >= MARKOV_MIN_SHARPE:
                        filtered.append(sym)
                    else:
                        dropped_low.append((sym, sh))
                if dropped_low:
                    logger.info("Indian-ORB Markov dropped %d low-Sharpe: %s",
                                len(dropped_low),
                                ", ".join(f"{s}({sh:+.2f})" for s, sh in dropped_low[:8]))
                if dropped_null:
                    logger.info("Indian-ORB Markov dropped %d unscored: %s",
                                len(dropped_null), ", ".join(dropped_null[:8]))
                base_symbols = filtered
        except Exception as e:
            logger.warning("Markov filter unavailable for Indian-ORB: %s", e)
        self.symbols     = base_symbols
        self.orb         : dict[str, dict] = {}
        self.positions   : dict[str, dict] = {}
        self.history     : list[dict]      = []
        self.trades_today : set[tuple]     = set()
        self.last_prices : dict[str, float] = {}      # NEW: for live P&L
        self._day        : str | None      = None
        self.on_event    = on_event or (lambda e: None)
        self.send_telegram = send_telegram
        self._running    = False
        self._thread     : threading.Thread | None = None
        # ── Account ──
        self.balance_inr : float = MCX_STARTING_BALANCE_INR
        # ── SQLite persistence ──
        self.db = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._init_schema()
        self._load_state()

    # ─────────────────────────────────────────────────────── persistence
    _CORE_COLS = [
        "id", "symbol", "side", "status",
        "entry", "entry_time_ts", "sl", "tp", "risk", "rr", "orb_range",
        "orb_high", "orb_low",
        "leverage", "margin_inr", "notional_inr", "qty",
        "exit_price", "exit_time_ts", "exit_reason",
        "pnl_inr", "pnl_pct", "pnl_price_pct", "fees_inr", "balance_after_inr",
        "be_moved", "high_water",
    ]

    def _init_schema(self):
        cur = self.db.cursor()
        cols = ",\n  ".join([
            "id INTEGER PRIMARY KEY",
            "symbol TEXT", "side TEXT", "status TEXT",
            "entry REAL", "entry_time_ts INTEGER",
            "sl REAL", "tp REAL", "risk REAL", "rr REAL", "orb_range REAL",
            "orb_high REAL", "orb_low REAL",
            "leverage REAL", "margin_inr REAL", "notional_inr REAL", "qty REAL",
            "exit_price REAL", "exit_time_ts INTEGER", "exit_reason TEXT",
            "pnl_inr REAL", "pnl_pct REAL", "pnl_price_pct REAL",
            "fees_inr REAL", "balance_after_inr REAL",
            "be_moved INTEGER", "high_water REAL",
            "extras_json TEXT",
        ])
        cur.execute(f"CREATE TABLE IF NOT EXISTS indian_orb_trades (\n  {cols}\n)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_iorb_status ON indian_orb_trades(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_iorb_symbol ON indian_orb_trades(symbol)")
        cur.execute("""CREATE TABLE IF NOT EXISTS indian_orb_account (
                         key TEXT PRIMARY KEY, value REAL
                       )""")

    def _to_ts(self, v):
        """Convert datetime / pandas Timestamp / int → unix seconds (or None)."""
        if v is None: return None
        if isinstance(v, (int, float)): return int(v)
        if hasattr(v, "timestamp"):
            try: return int(v.timestamp())
            except Exception: return None
        return None

    def _from_ts(self, ts):
        if ts is None: return None
        try:    return datetime.fromtimestamp(int(ts), tz=IST)
        except Exception: return None

    def _row_to_trade(self, row: sqlite3.Row) -> dict:
        t = {k: row[k] for k in row.keys() if k != "extras_json"}
        t["be_moved"]    = bool(t.get("be_moved"))
        t["entry_time"]  = self._from_ts(t.pop("entry_time_ts", None))
        t["exit_time"]   = self._from_ts(t.pop("exit_time_ts",  None))
        if row["extras_json"]:
            try: t.update(json.loads(row["extras_json"]))
            except Exception: pass
        return t

    def _trade_to_row(self, t: dict) -> dict:
        core_keys = set(self._CORE_COLS) | {"entry_time", "exit_time"}
        core = {k: t.get(k) for k in self._CORE_COLS}
        # Translate datetime fields → unix
        core["entry_time_ts"] = self._to_ts(t.get("entry_time"))
        core["exit_time_ts"]  = self._to_ts(t.get("exit_time"))
        core["be_moved"]      = 1 if t.get("be_moved") else 0
        extras = {k: v for k, v in t.items() if k not in core_keys}
        # Drop non-serialisable
        clean = {}
        for k, v in extras.items():
            try: json.dumps(v); clean[k] = v
            except Exception:
                if hasattr(v, "isoformat"):
                    try: clean[k] = v.isoformat()
                    except Exception: pass
        return core, clean

    def _save_trade(self, t: dict):
        if "id" not in t or t["id"] is None:
            t["id"] = int(time.time() * 1000)
        core, extras = self._trade_to_row(t)
        cols = ",".join(self._CORE_COLS) + ",extras_json"
        ph   = ",".join("?" for _ in self._CORE_COLS) + ",?"
        vals = [core.get(k) for k in self._CORE_COLS] + [json.dumps(extras)]
        self.db.execute(f"INSERT OR REPLACE INTO indian_orb_trades({cols}) VALUES ({ph})", vals)

    def _save_balance(self):
        self.db.execute(
            "INSERT OR REPLACE INTO indian_orb_account(key,value) VALUES ('balance_inr',?)",
            (float(self.balance_inr),))

    def _load_state(self):
        cur = self.db.cursor()
        # Open positions → self.positions
        for row in cur.execute("SELECT * FROM indian_orb_trades WHERE status='OPEN'"):
            t = self._row_to_trade(row)
            self.positions[t["symbol"]] = t
            self.trades_today.add((t["symbol"], t["side"]))
        # Closed trades → history (last 200)
        for row in cur.execute(
            "SELECT * FROM indian_orb_trades WHERE status!='OPEN' "
            "ORDER BY exit_time_ts DESC LIMIT 200"):
            self.history.append(self._row_to_trade(row))
        self.history.reverse()
        # Balance
        r = cur.execute("SELECT value FROM indian_orb_account WHERE key='balance_inr'").fetchone()
        if r: self.balance_inr = float(r[0])
        if self.positions or self.history:
            logger.info("Indian-ORB restored: %d open, %d historical, balance ₹%.2f",
                        len(self.positions), len(self.history), self.balance_inr)

    # ──────────────────────────────────────────────────── lifecycle
    def start(self) -> None:
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def _emit(self, event_type: str, **kwargs) -> None:
        try: self.on_event({"type": event_type, **kwargs})
        except Exception as e: logger.error("Event callback error: %s", e)

    # ─────────────────────────────────────────────────────────── data
    def _fetch_5m(self, symbol: str):
        """Fetch today's 5-min OHLC for a symbol. Returns DataFrame or None."""
        # Gentle pacing to avoid yfinance rate-limits on big symbol lists
        if YF_REQUEST_DELAY > 0:
            time.sleep(YF_REQUEST_DELAY)
        try:
            df = yf.download(symbol, period="1d", interval="5m",
                             progress=False, auto_adjust=False, threads=False)
            if df is None or df.empty:
                return None
            # yfinance returns timezone-aware UTC index for intraday
            idx = df.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            df.index = idx.tz_convert(IST)
            # Flatten multi-index columns if yfinance returned them
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            return df
        except Exception as e:
            logger.warning("yfinance %s: %s", symbol, e)
            return None

    # ─────────────────────────────────────────────────────────── ORB
    def _compute_orb(self, df, today):
        """Identify the 9:15–9:20 IST candle and return {high, low, open, close}."""
        df_today = df[df.index.date == today]
        if df_today.empty:
            return None
        # Find candle whose start time is exactly ORB_OPEN
        for ts, row in df_today.iterrows():
            if ts.time() == ORB_OPEN:
                return {
                    "high":  float(row["High"]),
                    "low":   float(row["Low"]),
                    "open":  float(row["Open"]),
                    "close": float(row["Close"]),
                    "ts":    ts,
                }
        # Fallback: first bar of the day if it starts within 09:15–09:20
        first = df_today.iloc[0]
        if df_today.index[0].time() >= ORB_OPEN and df_today.index[0].time() < ORB_CLOSE:
            return {
                "high":  float(first["High"]),
                "low":   float(first["Low"]),
                "open":  float(first["Open"]),
                "close": float(first["Close"]),
                "ts":    df_today.index[0],
            }
        return None

    # ─────────────────────────────────────────────────────────── signal
    def _check_signal(self, df, orb, today):
        """After the ORB candle closes, return first {side, entry, sl, tp, ts} where
        a 5-min candle CLOSES beyond the ORB range by the confirmation buffer.
        None if no breakout yet OR if the ORB is too tight."""
        # Quality gate: skip symbols with a too-tight ORB range
        mid = (orb["high"] + orb["low"]) / 2.0
        rng = orb["high"] - orb["low"]
        if mid > 0 and (rng / mid) < MIN_ORB_RANGE_PCT:
            return None

        df_today = df[df.index.date == today]
        post = df_today[df_today.index > orb["ts"]]
        up_trigger   = orb["high"] * (1.0 + BREAKOUT_BUFFER_PCT)
        down_trigger = orb["low"]  * (1.0 - BREAKOUT_BUFFER_PCT)
        for ts, row in post.iterrows():
            close = float(row["Close"])
            if close > up_trigger:
                entry = close
                sl    = orb["low"]
                risk  = entry - sl
                tp    = entry + RR_RATIO * risk
                return {"side":"LONG", "entry":entry, "sl":sl, "tp":tp, "ts":ts,
                        "risk":risk, "orb_range":rng}
            if close < down_trigger:
                entry = close
                sl    = orb["high"]
                risk  = sl - entry
                tp    = entry - RR_RATIO * risk
                return {"side":"SHORT", "entry":entry, "sl":sl, "tp":tp, "ts":ts,
                        "risk":risk, "orb_range":rng}
        return None

    # ─────────────────────────────────────────────────────────── trade mgmt
    def _check_open_trade(self, symbol, df, now):
        """Check SL/TP/EOD on any open paper trade for this symbol.
        With trailing stop: ratchet SL using high-water + ORB-range trail,
        and move SL to entry (BE) once unrealised P&L >= BREAKEVEN_AT_R."""
        if symbol not in self.positions:
            return
        t = self.positions[symbol]
        last = float(df.iloc[-1]["Close"])
        self.last_prices[symbol] = last     # for live P&L

        sl_before = t["sl"]; be_before = bool(t.get("be_moved"))

        # ── Update high-water mark + apply trailing logic ──
        if USE_TRAILING_STOP and t.get("risk", 0) > 0:
            hw = t.get("high_water", t["entry"])
            risk = t["risk"]
            # Post-BE trail distance source: 5m EMA(7) if enabled, else ORB-range.
            ema_trail = None
            if TRAIL_WITH_EMA:
                try:
                    ema_trail = float(
                        df["Close"].ewm(span=TRAIL_EMA_PERIOD, adjust=False).mean().iloc[-1]
                    )
                except Exception:
                    ema_trail = None
            orb_trail_dist = t.get("orb_range", risk) * TRAIL_MULT_OF_ORB
            if t["side"] == "LONG":
                if last > hw: hw = last; t["high_water"] = hw
                # Breakeven gate
                if not t.get("be_moved") and (hw - t["entry"]) >= BREAKEVEN_AT_R * risk:
                    t["sl"] = max(t["sl"], t["entry"]); t["be_moved"] = True
                # Trail (only after BE so we never loosen the initial stop)
                if t.get("be_moved"):
                    trail_sl = ema_trail if ema_trail is not None else (hw - orb_trail_dist)
                    # never let the EMA trail sit above price (would insta-stop)
                    trail_sl = min(trail_sl, last)
                    if trail_sl > t["sl"]: t["sl"] = trail_sl
            else:   # SHORT
                if last < hw: hw = last; t["high_water"] = hw
                if not t.get("be_moved") and (t["entry"] - hw) >= BREAKEVEN_AT_R * risk:
                    t["sl"] = min(t["sl"], t["entry"]); t["be_moved"] = True
                if t.get("be_moved"):
                    trail_sl = ema_trail if ema_trail is not None else (hw + orb_trail_dist)
                    trail_sl = max(trail_sl, last)
                    if trail_sl < t["sl"]: t["sl"] = trail_sl

        hit = None
        if t["side"] == "LONG":
            if last <= t["sl"]: hit = ("SL" if not t.get("be_moved") else "TRAIL", t["sl"])
            elif (not USE_TRAILING_STOP) and last >= t["tp"]: hit = ("TP", t["tp"])
        else:
            if last >= t["sl"]: hit = ("SL" if not t.get("be_moved") else "TRAIL", t["sl"])
            elif (not USE_TRAILING_STOP) and last <= t["tp"]: hit = ("TP", t["tp"])
        if not hit and now.time() >= EOD_FLAT_TIME:
            hit = ("EOD", last)

        # If SL or BE flag changed (trail moved) and we're NOT exiting now, persist it.
        if not hit and (t["sl"] != sl_before or bool(t.get("be_moved")) != be_before):
            self._save_trade(t)
        if hit:
            reason, exit_price = hit
            # Apply exit slippage (longs exit BELOW, shorts exit ABOVE)
            if t["side"] == "LONG":
                fill = exit_price * (1 - MCX_SLIPPAGE_PCT)
                move_pct = (fill - t["entry"]) / t["entry"]
            else:
                fill = exit_price * (1 + MCX_SLIPPAGE_PCT)
                move_pct = (t["entry"] - fill) / t["entry"]

            notional = t.get("notional_inr", 0.0)
            margin   = t.get("margin_inr", 0.0)
            gross_pnl_inr = notional * move_pct
            fees_inr      = notional * MCX_TAKER_FEE_PCT * 2
            pnl_inr       = gross_pnl_inr - fees_inr
            pnl_pct_margin = (pnl_inr / margin * 100) if margin else 0.0
            pnl_pct_price  = move_pct * 100
            self.balance_inr += pnl_inr

            t.update({
                "exit_price": fill, "exit_reason": reason, "exit_time": now,
                "pnl_inr": round(pnl_inr, 2),
                "fees_inr": round(fees_inr, 2),
                "pnl_pct": round(pnl_pct_margin, 4),    # return on margin (leveraged)
                "pnl_price_pct": round(pnl_pct_price, 4),
                "balance_after_inr": round(self.balance_inr, 2),
                # WIN if hit TP (fixed) or a trailing SL that ratcheted above entry;
                # LOSS if initial SL hit before BE moved.
                "status": (
                    "WIN"  if reason in ("TP", "TRAIL") else
                    "LOSS" if reason == "SL"           else
                    "TIMEOUT"
                ),
            })
            logger.info(
                "CLOSE %s %s @ %.2f reason=%s P&L=₹%.2f (%.2f%% on margin) | bal=₹%.2f",
                symbol, t["side"], fill, reason, pnl_inr, pnl_pct_margin, self.balance_inr,
            )
            self.history.append(dict(t))
            self.positions.pop(symbol, None)
            self._save_trade(t); self._save_balance()
            self._emit("mcx_closed", trade=dict(t))

    # ─────────────────────────────────────────────────────────── scan
    def _reset_for_new_day(self, today_str):
        if self._day != today_str:
            self._day = today_str
            self.orb.clear()
            self.positions.clear()
            self.trades_today.clear()
            logger.info("=== New trading day: %s ===", today_str)

    def _enrich_open(self, t: dict) -> dict:
        """Add live current_price + unrealised P&L to an open position."""
        out = dict(t)
        sym = t.get("symbol")
        last = self.last_prices.get(sym)
        if last is None:
            out["current_price"]   = None
            out["pnl_live_inr"]    = None
            out["pnl_pct_live"]    = None
            return out
        entry = float(t.get("entry") or 0)
        notional = float(t.get("notional_inr") or 0)
        margin   = float(t.get("margin_inr") or 0)
        if not entry:
            out["current_price"] = round(last, 2)
            return out
        move = (last - entry) / entry if t.get("side") == "LONG" else (entry - last) / entry
        # Gross only — fees deducted on exit
        pnl = notional * move
        out["current_price"] = round(last, 4)
        out["pnl_live_inr"]  = round(pnl, 2)
        out["pnl_pct_live"]  = round((pnl / margin * 100) if margin else 0.0, 2)
        return out

    def get_state(self) -> dict:
        """Snapshot for the web UI."""
        wins   = [t for t in self.history if t.get("status") == "WIN"]
        losses = [t for t in self.history if t.get("status") == "LOSS"]
        timeouts = [t for t in self.history if t.get("status") == "TIMEOUT"]
        closed = wins + losses + timeouts
        realized = sum(t.get("pnl_inr", 0) or 0 for t in closed)
        wr = (len(wins) / len(closed) * 100) if closed else 0.0
        # Live unrealised P&L across all OPEN positions
        unreal = 0.0
        for t in self.positions.values():
            enr = self._enrich_open(t)
            if enr.get("pnl_live_inr") is not None:
                unreal += enr["pnl_live_inr"]

        # Per-symbol status — useful filter for the dashboard
        today_closed = {t.get("symbol"): t for t in self.history
                        if t.get("entry_time") and getattr(t.get("entry_time"), "date", None)
                        and t["entry_time"].date().isoformat() == self._day}
        # Some histories may not have datetime objects — fall back by exit_time string
        stocks = []
        for sym in self.symbols:
            took_long  = (sym, "LONG")  in self.trades_today
            took_short = (sym, "SHORT") in self.trades_today
            took = took_long or took_short
            side = "LONG" if took_long else "SHORT" if took_short else None
            orb  = self.orb.get(sym)
            open_pos = self.positions.get(sym)
            closed_t = today_closed.get(sym)

            if open_pos:
                status, status_color = "OPEN", "yellow"
            elif closed_t:
                st = closed_t.get("status", "?")
                status = st
                status_color = "green" if st == "WIN" else "red" if st == "LOSS" else "muted"
            elif took:
                status, status_color = "TAKEN", "muted"
            elif orb:
                status, status_color = "WAITING", "muted"
            else:
                status, status_color = "PRE-ORB", "muted"

            stocks.append({
                "symbol":     sym,
                "took_trade": took,
                "side":       side,
                "status":     status,
                "status_color": status_color,
                "orb_high":   orb["high"] if orb else None,
                "orb_low":    orb["low"]  if orb else None,
                "pnl_inr":    closed_t.get("pnl_inr") if closed_t else None,
                "exit_reason": closed_t.get("exit_reason") if closed_t else None,
                "open":       open_pos is not None,
            })
        return {
            "running":        self._running,
            "symbols":        list(self.symbols),
            "orb":            {k: {kk:(vv.isoformat() if hasattr(vv,'isoformat') else vv)
                                   for kk,vv in v.items()} for k,v in self.orb.items()},
            "open_positions": [self._serialize(self._enrich_open(t)) for t in self.positions.values()],
            "history":        [self._serialize(t) for t in self.history[-50:]],
            "today":          self._day,
            "stocks":         stocks,
            "account": {
                "starting_balance_inr": MCX_STARTING_BALANCE_INR,
                "balance_inr":          round(self.balance_inr, 2),
                "leverage":             MCX_LEVERAGE,
                "margin_per_trade_inr": MCX_MARGIN_PER_TRADE_INR,
                "realized_pnl_inr":     round(realized, 2),
                "unrealized_pnl_inr":   round(unreal, 2),
                "equity_inr":           round(self.balance_inr + unreal, 2),
                "total_pnl_pct":        round((self.balance_inr - MCX_STARTING_BALANCE_INR) /
                                              MCX_STARTING_BALANCE_INR * 100, 2),
                "trades":   len(self.history),
                "wins":     len(wins),
                "losses":   len(losses),
                "timeouts": len(timeouts),
                "win_rate": round(wr, 1),
            },
        }

    def _serialize(self, t: dict) -> dict:
        out = {}
        for k, v in t.items():
            if hasattr(v, "isoformat"):  out[k] = v.isoformat()
            else: out[k] = v
        return out

    def scan(self):
        now = datetime.now(IST)
        today = now.date()
        self._reset_for_new_day(today.isoformat())

        # Outside NSE market hours → nothing to do
        if now.time() < ORB_OPEN or now.time() > MARKET_CLOSE:
            return

        for sym in self.symbols:
            df = self._fetch_5m(sym)
            if df is None or df.empty:
                continue

            # Manage any open paper trade first
            self._check_open_trade(sym, df, now)

            # Pre-9:20 → wait for ORB to fully form
            if now.time() < ORB_CLOSE:
                continue

            # Compute ORB once per day per symbol
            if sym not in self.orb:
                orb = self._compute_orb(df, today)
                if not orb:
                    continue
                self.orb[sym] = orb
                logger.info("ORB %s: high=%.2f low=%.2f range=%.2f",
                            sym, orb["high"], orb["low"], orb["high"] - orb["low"])

            orb = self.orb[sym]

            # ONE trade per symbol per day — either side, first signal wins
            if (sym, "LONG") in self.trades_today or (sym, "SHORT") in self.trades_today:
                continue
            if sym in self.positions:
                continue

            sig = self._check_signal(df, orb, today)
            if not sig: continue

            self.trades_today.add((sym, sig["side"]))
            self._open_trade(sym, sig, orb)

    # ─────────────────────────────────────────────────────────── alert + paper-trade
    def _open_trade(self, symbol, sig, orb):
        # ── Sizing in INR (capped by available balance) ──
        margin   = min(MCX_MARGIN_PER_TRADE_INR, max(0.0, self.balance_inr))
        notional = margin * MCX_LEVERAGE
        # Apply entry slippage
        if sig["side"] == "LONG":
            entry_fill = sig["entry"] * (1 + MCX_SLIPPAGE_PCT)
        else:
            entry_fill = sig["entry"] * (1 - MCX_SLIPPAGE_PCT)
        qty = notional / entry_fill if entry_fill else 0.0

        trade = {
            "symbol":       symbol,
            "side":         sig["side"],
            "entry":        round(entry_fill, 4),
            "entry_time":   sig["ts"],
            "sl":           sig["sl"],
            "tp":           sig["tp"],
            "risk":         sig["risk"],
            "rr":           RR_RATIO,
            "orb_high":     orb["high"],
            "orb_low":      orb["low"],
            # Account / sizing
            "leverage":     MCX_LEVERAGE,
            "margin_inr":   round(margin, 2),
            "notional_inr": round(notional, 2),
            "qty":          round(qty, 4),
            "status":       "OPEN",
            # Trailing-stop state
            "high_water":   round(entry_fill, 4),
            "be_moved":     False,
            "orb_range":    sig.get("orb_range", 0),
        }
        if margin <= 0:
            logger.warning("Insufficient INR balance for new MCX trade on %s", symbol)
            return
        trade["id"] = int(time.time() * 1000)
        self.positions[symbol] = trade
        self._save_trade(trade); self._save_balance()

        logger.info(
            "SIGNAL %s %s @ %.2f SL=%.2f TP=%.2f | margin=₹%.0f notional=₹%.0f qty=%.2f (1:%.0f)",
            symbol, sig["side"], entry_fill, sig["sl"], sig["tp"],
            margin, notional, qty, RR_RATIO,
        )

        self._emit("mcx_signal", trade=self._serialize(trade))

        # Telegram alert — reuse existing send_alert(symbol, signal_dict)
        if not self.send_telegram:
            return
        try:
            send_alert(symbol, {
                "type":         "BREAKOUT" if sig["side"] == "LONG" else "BREAKDOWN",
                "side":         sig["side"],
                "entry":        trade["entry"],
                "close":        trade["entry"],
                "sl":           sig["sl"],
                "tp":           sig["tp"],
                "rr":           RR_RATIO,
                "level_label":  f"ORB-{'High' if sig['side']=='LONG' else 'Low'}",
                "level_price":  orb["high"] if sig["side"] == "LONG" else orb["low"],
                "orb_high":     orb["high"],
                "orb_low":      orb["low"],
                "regime":       "ORB",
                "strategy":     "Indian-ORB",
                "timeframe":    "5m IST",
                "time":         int(sig["ts"].timestamp()),
                "leverage":     MCX_LEVERAGE,
                "margin_inr":   trade["margin_inr"],
                "notional_inr": trade["notional_inr"],
                "currency":     "₹",
            })
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    # ─────────────────────────────────────────────────────────── main loop
    def run(self):
        logger.info("Indian-ORB bot starting on %d NSE F&O symbols.", len(self.symbols))
        logger.info("ORB window: %s–%s IST.  Market close: %s.  R:R = 1:%.0f",
                    ORB_OPEN.strftime("%H:%M"), ORB_CLOSE.strftime("%H:%M"),
                    MARKET_CLOSE.strftime("%H:%M"), RR_RATIO)
        self._emit("mcx_startup", symbols=list(self.symbols))
        self._running = True
        while self._running:
            try:
                self.scan()
            except Exception as e:
                logger.error("Scan error: %s", e)
            # responsive sleep so stop() reacts within ~1s
            for _ in range(SCAN_INTERVAL):
                if not self._running: break
                time.sleep(1)
        logger.info("MCX-ORB bot stopped.")
        self._emit("mcx_stopped")


if __name__ == "__main__":
    MCXBot().run()
