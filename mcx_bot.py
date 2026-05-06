"""
MCX Opening Range Breakout (ORB) Strategy.

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
import time
import logging
import threading
from datetime import datetime, time as dtime, timezone, timedelta

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

# ─── Symbols (yfinance tickers — NSE ETF proxies for MCX commodities) ──────
# Edit this list as needed. Format: "<ticker>.NS" for NSE.
MCX_SYMBOLS = [
    "MCX.NS",           # MCX Ltd (the exchange company itself)
    "GOLDBEES.NS",      # Nippon India Gold ETF (gold proxy)
    "SILVERBEES.NS",    # Nippon India Silver ETF
    "GOLDIETF.NS",      # ICICI Gold ETF
    "HINDPETRO.NS",     # Crude oil proxy (refiner)
    "ONGC.NS",          # Crude oil proxy (producer)
    "VEDL.NS",          # Base metals (zinc/copper/aluminium)
]

# ─── Strategy parameters ───────────────────────────────────────────────────
ORB_OPEN       = dtime(9, 15)    # first candle starts
ORB_CLOSE      = dtime(9, 20)    # first 5-min candle closes here → ORB defined
MARKET_CLOSE   = dtime(15, 30)   # NSE close
EOD_FLAT_TIME  = dtime(15, 25)   # auto-flat any open paper trade here
RR_RATIO       = 2.0             # target = entry + 2 × risk  (1:2)
SCAN_INTERVAL  = 60              # seconds between yfinance polls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mcx-orb")


class MCXBot:
    def __init__(self, symbols=None, on_event=None, send_telegram=True):
        self.symbols     = symbols or MCX_SYMBOLS
        self.orb         : dict[str, dict] = {}    # sym -> ORB dict
        self.positions   : dict[str, dict] = {}    # sym -> open paper trade
        self.history     : list[dict]      = []
        self.alerted_today: set[str]       = set() # (sym, side) keys
        self._day        : str | None      = None
        self.on_event    = on_event or (lambda e: None)
        self.send_telegram = send_telegram
        self._running    = False
        self._thread     : threading.Thread | None = None

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
        """After the ORB candle, return first {side, entry, sl, tp, ts} where
        a 5-min candle CLOSES beyond the ORB range. None if no breakout yet."""
        df_today = df[df.index.date == today]
        # Skip candles up to and including the ORB candle (09:15–09:20 → close at 09:20)
        post = df_today[df_today.index > orb["ts"]]
        for ts, row in post.iterrows():
            close = float(row["Close"])
            if close > orb["high"]:
                entry = close
                sl    = orb["low"]
                risk  = entry - sl
                tp    = entry + RR_RATIO * risk
                return {"side":"LONG", "entry":entry, "sl":sl, "tp":tp, "ts":ts, "risk":risk}
            if close < orb["low"]:
                entry = close
                sl    = orb["high"]
                risk  = sl - entry
                tp    = entry - RR_RATIO * risk
                return {"side":"SHORT", "entry":entry, "sl":sl, "tp":tp, "ts":ts, "risk":risk}
        return None

    # ─────────────────────────────────────────────────────────── trade mgmt
    def _check_open_trade(self, symbol, df, now):
        """Check SL/TP/EOD on any open paper trade for this symbol."""
        if symbol not in self.positions:
            return
        t = self.positions[symbol]
        last = float(df.iloc[-1]["Close"])
        hit = None
        if t["side"] == "LONG":
            if last <= t["sl"]: hit = ("SL", t["sl"])
            elif last >= t["tp"]: hit = ("TP", t["tp"])
        else:
            if last >= t["sl"]: hit = ("SL", t["sl"])
            elif last <= t["tp"]: hit = ("TP", t["tp"])
        if not hit and now.time() >= EOD_FLAT_TIME:
            hit = ("EOD", last)
        if hit:
            reason, exit_price = hit
            pnl_pct = ((exit_price - t["entry"]) / t["entry"] * 100) \
                      * (1 if t["side"] == "LONG" else -1)
            t.update({"exit_price": exit_price, "exit_reason": reason,
                      "pnl_pct": pnl_pct, "exit_time": now,
                      "status": "WIN" if reason == "TP" else "LOSS" if reason == "SL" else "TIMEOUT"})
            logger.info("CLOSE %s %s @ %.2f reason=%s P&L=%.2f%%",
                        symbol, t["side"], exit_price, reason, pnl_pct)
            self.history.append(dict(t))
            self.positions.pop(symbol, None)
            self._emit("mcx_closed", trade=dict(t))

    # ─────────────────────────────────────────────────────────── scan
    def _reset_for_new_day(self, today_str):
        if self._day != today_str:
            self._day = today_str
            self.orb.clear()
            self.positions.clear()
            self.alerted_today.clear()
            logger.info("=== New trading day: %s ===", today_str)

    def get_state(self) -> dict:
        """Snapshot for the web UI."""
        return {
            "running":        self._running,
            "symbols":        list(self.symbols),
            "orb":            {k: {kk:(vv.isoformat() if hasattr(vv,'isoformat') else vv)
                                   for kk,vv in v.items()} for k,v in self.orb.items()},
            "open_positions": [self._serialize(t) for t in self.positions.values()],
            "history":        [self._serialize(t) for t in self.history[-50:]],
            "today":          self._day,
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

            # Already alerted both sides? Skip.
            already = (sym, "LONG") in self.alerted_today and (sym, "SHORT") in self.alerted_today
            if already or sym in self.positions:
                continue

            sig = self._check_signal(df, orb, today)
            if not sig: continue
            key = (sym, sig["side"])
            if key in self.alerted_today:
                continue

            self.alerted_today.add(key)
            self._open_trade(sym, sig, orb)

    # ─────────────────────────────────────────────────────────── alert + paper-trade
    def _open_trade(self, symbol, sig, orb):
        trade = {
            "symbol":     symbol,
            "side":       sig["side"],
            "entry":      sig["entry"],
            "entry_time": sig["ts"],
            "sl":         sig["sl"],
            "tp":         sig["tp"],
            "risk":       sig["risk"],
            "rr":         RR_RATIO,
            "orb_high":   orb["high"],
            "orb_low":    orb["low"],
        }
        self.positions[symbol] = trade

        logger.info("SIGNAL %s %s entry=%.2f SL=%.2f TP=%.2f (risk=%.2f, R:R=1:%.0f)",
                    symbol, sig["side"], sig["entry"], sig["sl"], sig["tp"],
                    sig["risk"], RR_RATIO)

        self._emit("mcx_signal", trade=self._serialize(trade))

        # Telegram alert — reuse existing send_alert(symbol, signal_dict)
        if not self.send_telegram:
            return
        try:
            send_alert(symbol, {
                "type":        "BREAKOUT" if sig["side"] == "LONG" else "BREAKDOWN",
                "close":       sig["entry"],
                "level_label": f"ORB-{'High' if sig['side']=='LONG' else 'Low'}",
                "level_price": orb["high"] if sig["side"] == "LONG" else orb["low"],
                "tests":       1,
                "ema7":        None, "ema21": None,
                "entropy":     None, "regime": "ORB",
                "time":        int(sig["ts"].timestamp()),
                "sl":          sig["sl"],
                "tp":          sig["tp"],
            })
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)

    # ─────────────────────────────────────────────────────────── main loop
    def run(self):
        logger.info("MCX-ORB bot starting. Symbols: %s", ", ".join(self.symbols))
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
