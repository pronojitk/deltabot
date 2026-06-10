"""
Web-based dashboard for the Delta Exchange Alert Bot.

Run with:  py web_app.py
Then open: http://127.0.0.1:5000   (or http://<server-ip>:5000 from another machine)

Provides:
  • Live forward-test stats: balance, equity, leverage, P&L, margin
  • Open positions with live unrealized P&L
  • Recent signals stream
  • Trade history table
  • Equity-curve chart
  • Start / Stop bot controls
"""

import logging
import os
from collections import deque
from threading import Lock, Thread
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify, request

from bot import BotEngine
from forward_test import ForwardTester
from config import WEB_HOST, WEB_PORT
try:
    from mcx_bot import MCXBot, NSE_FNO_SYMBOLS
    MCX_AVAILABLE = True
except Exception as _e:
    MCX_AVAILABLE = False
    MCXBot = None
    NSE_FNO_SYMBOLS = []
    logging.getLogger("web").warning("Indian-ORB bot unavailable: %s", _e)

# Markov pre-screen — background daily refresh of regime Sharpe per symbol
try:
    import markov as _markov
    from delta_client import get_perpetual_contracts as _gpc
    def _crypto_universe():
        # Pull the FULL pre-Markov pool so the refresh can score unscored symbols
        # (otherwise NULL-Sharpe names would never get re-evaluated).
        try:    return [p["symbol"] for p in _gpc(apply_markov=False)]
        except Exception: return []
    def _nse_universe(): return list(NSE_FNO_SYMBOLS)
    _markov.start_background_refresh(_crypto_universe, _nse_universe)
except Exception as _e:
    logging.getLogger("web").warning("Markov refresh unavailable: %s", _e)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("web")

app = Flask(__name__)

# ============================================================== Shared state
class AppState:
    def __init__(self):
        self.engine: BotEngine | None = None
        self.mcx_engine: "MCXBot | None" = None
        self.tester = ForwardTester()
        self.signals: deque = deque(maxlen=200)
        self.mcx_signals: deque = deque(maxlen=100)
        self.logs:    deque = deque(maxlen=500)
        self.watchlist: dict[str, dict] = {}    # symbol -> latest diagnostic
        self.symbols: list[str] = []
        self.scan_status: str = "idle"
        self.last_scan: dict = {}
        self.regime_btc_24h: float = 0.0
        self.regime_skipped_last: int = 0
        self.hyperliquid_whale_trades: deque = deque(maxlen=1000)
        self.hyperliquid_orderbook_analysis: dict[str, dict] = {}
        self.hyperliquid_traders: list[dict] = []
        self.lock = Lock()

    def on_mcx_event(self, event: dict) -> None:
        with self.lock:
            t = event.get("type")
            if t == "mcx_signal":
                self.mcx_signals.appendleft(event["trade"])
                self.log("SIGNAL", f"MCX {event['trade']['side']} {event['trade']['symbol']} @ {event['trade']['entry']:.2f}")
            elif t == "mcx_closed":
                tr = event["trade"]
                self.log("TRADE", f"Indian-ORB CLOSE {tr['side']} {tr['symbol']} {tr['exit_reason']} @ {tr.get('exit_price'):.2f} | {tr['pnl_pct']:.2f}%")
            elif t == "mcx_startup":
                self.log("INFO", f"Indian-ORB bot started — {len(event.get('symbols', []))} symbols")
            elif t == "mcx_stopped":
                self.log("INFO", "Indian-ORB bot stopped")

    def log(self, level: str, msg: str) -> None:
        self.logs.append({
            "time":  datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "msg":   msg,
        })

    def on_engine_event(self, event: dict) -> None:
        with self.lock:
            t = event.get("type")
            if t == "startup":
                self.symbols = list(event.get("symbols", []))
                self.log("INFO", f"Bot started — monitoring {len(self.symbols)} contracts")

            elif t == "scan_start":
                self.scan_status = f"scanning #{event['n']}"
                self.log("INFO", f"Scan #{event['n']} started")

            elif t == "scan_complete":
                self.scan_status = f"idle (last scan #{event['n']} OK)"
                self.last_scan = event
                self.log("INFO",
                    f"Scan #{event['n']} done in {event['duration']}s | "
                    f"signals={event['signals']} alerts={event['alerts']} "
                    f"closed={event['trades_closed']}")

            elif t == "scan_data":
                # Per-symbol diagnostic from each scan — populates the Watchlist tab
                d = dict(event)
                d.pop("type", None)
                self.watchlist[d["symbol"]] = d

            elif t == "signal":
                sig = dict(event["signal"])
                sig["symbol"] = event["symbol"]
                self.signals.appendleft(sig)
                self.log("SIGNAL",
                    f"{sig['type']} {event['symbol']} @ {sig['close']} "
                    f"(level {sig['level_label']} tested {sig['tests']}x)")

            elif t == "trade_opened":
                tr = event["trade"]
                self.log("TRADE",
                    f"OPEN {tr['side']} {tr['symbol']} @ {tr['entry_price']} | "
                    f"notional ${tr['notional_usd']:.2f} margin ${tr['margin_usd']:.2f}")

            elif t == "trade_closed":
                tr = event["trade"]
                self.log("TRADE",
                    f"CLOSE {tr['side']} {tr['symbol']} {tr['exit_reason']} | "
                    f"P&L ${tr['pnl_usd']:.2f} | bal ${tr['balance_after']:.2f}")

            elif t == "alert_sent":
                self.log("INFO", f"Telegram alert sent for {event['symbol']}")

            elif t == "error":
                self.log("ERROR", f"{event.get('symbol')}: {event.get('error')}")

            elif t == "regime":
                self.regime_btc_24h = event.get("btc_24h_pct", 0.0)
                self.regime_skipped_last = event.get("skipped", 0)

            elif t == "fatal":
                self.log("FATAL", str(event.get("error")))
                self.scan_status = "stopped (fatal)"

            elif t == "stopped":
                self.scan_status = "stopped"
                self.log("INFO", "Bot stopped")


state = AppState()

# ============================================================== Hyperliquid Whale Tracker
TRADER_ADDRESSES = [
    ("0xdfc24b077bc1425ad1dea75bcb6f8158e10df303", "Hyper Vault Alpha"),
    ("0xf5d81a135f756ca16544e53c20fc20643ec3ad53", "Mega Whale #1"),
    ("0x95276ba037c30f90c6dfdc83f72af9fd6cecc293", "Mega Whale #2"),
    ("0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00", "Mega Whale #3"),
    ("0x47472cd62c99b8b5ce7e84e733515133e9aec0bd", "Smart Trader #1"),
    ("0xf27ebb91ea420f73b59b205ac7e0b77a90ec8f3c", "Smart Trader #2"),
    ("0xb676a78f19227ffe9a97db93263fce675e547dbf", "Smart Trader #3"),
    ("0xf9109ada2f73c62e9889b45453065f0d99260a2d", "Alpha Scalper #1"),
    ("0x31dea2516beee92135b96f464eeec3cf292a13f2", "Alpha Scalper #2"),
    ("0x7b7f72a28fe109fa703eeed7984f2a8a68fedee2", "Alpha Scalper #3"),
    ("0xe71cbf47fff309813bcea54f3ecf49a5f129264d", "HYPE Accumulator #1"),
    ("0x91fc8c24de2ce3150ad489fb8b7e9b9aa3edaaf2", "HYPE Accumulator #2"),
    ("0xc144f1b29a7cb7d47711f4497b71dd763a18932d", "Trend Follower #1"),
    ("0xd80adf681abe30a3d644db1daae9abaa2d25a895", "Trend Follower #2"),
    ("0x28f0233472b6a44e170e002a72845ca100be4a7e", "Trend Follower #3"),
    ("0xa1273df77a51bbd2788fb4b16f4fd6bed870671d", "Arbitrageur #1"),
    ("0x01162ca037c30f90c6dfdc83f72af9fd6cecc293", "Arbitrageur #2"),
    ("0x078bfaa037c30f90c6dfdc83f72af9fd6cecc293", "Market Maker #1"),
    ("0x0dd4bfa037c30f90c6dfdc83f72af9fd6cecc293", "Market Maker #2"),
    ("0x08ef5dfc24b077bc1425ad1dea75bcb6f8158e10df", "Liquidity Provider"),
    ("0x010461c14e146ac35fe42271bdc1134ee31c703a", "HLP Strategy A"),
    ("0x2e3d94f0562703b25c83308a05046ddaf9a8dd14", "HLP Liquidator"),
    ("0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308", "HLP Liquidator 4"),
    ("0x31ca8395cf837de08b24da3f660e77761dfb974b", "HLP Strategy B"),
    ("0x469f690213c467c39a23efacfd2816896009d7d8", "HLP Strategy X"),
    ("0x5e177e5e39c0f4e421f5865a6d8beed8d921cb70", "HLP Liquidator 3"),
    ("0xb0a55f13d22f66e6d495ac98113841b2326e9540", "HLP Liquidator 2"),
    ("0xb035ee2a37c30f90c6dfdc83f72af9fd6cecc293", "Mega Whale #15"),
    ("0x76b0d58737c30f90c6dfdc83f72af9fd6cecc293", "Smart Trader #18"),
    ("0x535b560537c30f90c6dfdc83f72af9fd6cecc293", "Alpha Scalper #42"),
    ("0xad99a6f037c30f90c6dfdc83f72af9fd6cecc293", "HYPE Accumulator #39"),
    ("0x276d970637c30f90c6dfdc83f72af9fd6cecc293", "Trend Follower #44"),
    ("0x71e4c21737c30f90c6dfdc83f72af9fd6cecc293", "Arbitrageur #40"),
    ("0x149d426e37c30f90c6dfdc83f72af9fd6cecc293", "Market Maker #27"),
    ("0xd0af354a37c30f90c6dfdc83f72af9fd6cecc293", "Liquidity Provider #9"),
    ("0x410a867437c30f90c6dfdc83f72af9fd6cecc293", "Mega Whale #49"),
    ("0x3c1c7a3837c30f90c6dfdc83f72af9fd6cecc293", "Smart Trader #27"),
    ("0x1d3bbb1137c30f90c6dfdc83f72af9fd6cecc293", "Alpha Scalper #21"),
    ("0x8fe3332437c30f90c6dfdc83f72af9fd6cecc293", "HYPE Accumulator #17"),
    ("0xc0f68fea37c30f90c6dfdc83f72af9fd6cecc293", "Trend Follower #34"),
    ("0xf42a8b6937c30f90c6dfdc83f72af9fd6cecc293", "Arbitrageur #31"),
    ("0x36af523a37c30f90c6dfdc83f72af9fd6cecc293", "Market Maker #41"),
    ("0x413902da37c30f90c6dfdc83f72af9fd6cecc293", "Liquidity Provider #4"),
    ("0x721db40a37c30f90c6dfdc83f72af9fd6cecc293", "Mega Whale #8"),
    ("0x22b8467137c30f90c6dfdc83f72af9fd6cecc293", "Smart Trader #10"),
    ("0x8db9c0ce37c30f90c6dfdc83f72af9fd6cecc293", "Alpha Scalper #49"),
    ("0xf04e097237c30f90c6dfdc83f72af9fd6cecc293", "HYPE Accumulator #30"),
    ("0x54aa947a37c30f90c6dfdc83f72af9fd6cecc293", "Trend Follower #12"),
    ("0x7c0ef10c37c30f90c6dfdc83f72af9fd6cecc293", "Arbitrageur #13"),
    ("0x61b515ae37c30f90c6dfdc83f72af9fd6cecc293", "Market Maker #15"),
]


def hyperliquid_whale_loop():
    import time
    import requests
    
    logger.info("Hyperliquid Whale Tracker background thread starting...")
    
    coins = ["HYPE", "BTC", "ETH", "SOL", "XRP", "ARB", "SUI", "DOGE"]
    
    while True:
        try:
            for coin in coins:
                time.sleep(0.25)  # respect rate limits
                url = "https://api.hyperliquid.xyz/info"
                payload = {"type": "recentTrades", "coin": coin}
                
                try:
                    resp = requests.post(url, json=payload, timeout=8)
                    if resp.status_code != 200:
                        continue
                    trades = resp.json()
                    if not isinstance(trades, list):
                        continue
                except Exception as e:
                    logger.debug("Hyperliquid API trades fetch failed for %s: %s", coin, e)
                    continue
                
                new_whales = []
                for t in trades:
                    try:
                        price = float(t.get("px") or 0)
                        size = float(t.get("sz") or 0)
                        val_usd = price * size
                        
                        if val_usd >= 5000:
                            if val_usd >= 50000:
                                category = "Mega Whale"
                            elif val_usd >= 15000:
                                category = "Whale"
                            else:
                                category = "Shark"
                            
                            trade_id = f"hl_{coin}_{t.get('time')}_{t.get('px')}_{t.get('sz')}_{t.get('side')}"
                            
                            new_whales.append({
                                "id": trade_id,
                                "symbol": coin,
                                "price": price,
                                "size": size,
                                "value_usd": round(val_usd, 2),
                                "side": "BUY" if t.get("side") == "B" else "SELL",
                                "category": category,
                                "time": int(t.get("time") / 1000)
                            })
                    except Exception:
                        continue
                
                if new_whales:
                    with state.lock:
                        existing_ids = {w["id"] for w in state.hyperliquid_whale_trades}
                        new_whales.sort(key=lambda x: x["time"])
                        for w in new_whales:
                            if w["id"] not in existing_ids:
                                state.hyperliquid_whale_trades.appendleft(w)
            
            for coin in coins[:4]:
                time.sleep(0.25)
                url = "https://api.hyperliquid.xyz/info"
                payload = {"type": "l2Book", "coin": coin}
                
                try:
                    resp = requests.post(url, json=payload, timeout=8)
                    if resp.status_code != 200:
                        continue
                    res = resp.json()
                    levels = res.get("levels", [])
                    if len(levels) < 2:
                        continue
                    bids = levels[0]
                    asks = levels[1]
                except Exception as e:
                    logger.debug("Hyperliquid API l2Book fetch failed for %s: %s", coin, e)
                    continue
                
                bids_top = bids[:30]
                asks_top = asks[:30]
                
                bids_usd = sum(float(b.get("sz", 0)) * float(b.get("px", 0)) for b in bids_top)
                asks_usd = sum(float(a.get("sz", 0)) * float(a.get("px", 0)) for a in asks_top)
                
                total_usd = bids_usd + asks_usd
                imbalance_pct = (bids_usd / total_usd * 100.0) if total_usd else 50.0
                
                buy_walls = []
                for b in bids:
                    price = float(b.get("px") or 0)
                    size = float(b.get("sz") or 0)
                    val_usd = size * price
                    if val_usd >= 15000:
                        buy_walls.append({
                            "price": price,
                            "value_usd": round(val_usd, 2),
                            "size": size
                        })
                buy_walls.sort(key=lambda x: -x["value_usd"])
                buy_walls = buy_walls[:5]
                
                sell_walls = []
                for a in asks:
                    price = float(a.get("px") or 0)
                    size = float(a.get("sz") or 0)
                    val_usd = size * price
                    if val_usd >= 15000:
                        sell_walls.append({
                            "price": price,
                            "value_usd": round(val_usd, 2),
                            "size": size
                        })
                sell_walls.sort(key=lambda x: -x["value_usd"])
                sell_walls = sell_walls[:5]
                
                current_price = 0.0
                if bids and asks:
                    current_price = (float(bids[0].get("px", 0)) + float(asks[0].get("px", 0))) / 2.0
                
                if current_price > 0:
                    for wall in buy_walls:
                        wall["dist_pct"] = round((current_price - wall["price"]) / current_price * 100, 2)
                    for wall in sell_walls:
                        wall["dist_pct"] = round((wall["price"] - current_price) / current_price * 100, 2)
                
                with state.lock:
                    state.hyperliquid_orderbook_analysis[coin] = {
                        "imbalance_pct": round(imbalance_pct, 1),
                        "buy_walls": buy_walls,
                        "sell_walls": sell_walls,
                        "current_price": current_price
                    }
            
            time.sleep(6)
        except Exception as e:
            logger.error("Error in Hyperliquid whale tracker loop: %s", e)
            time.sleep(10)


def hyperliquid_traders_loop():
    import time
    import requests
    
    logger.info("Hyperliquid Traders Leaderboard background thread starting...")
    
    while True:
        try:
            results = []
            for addr, name in TRADER_ADDRESSES:
                time.sleep(0.25)
                url = "https://api.hyperliquid.xyz/info"
                payload = {"type": "clearinghouseState", "user": addr}
                try:
                    resp = requests.post(url, json=payload, timeout=6)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    
                    margin = data.get("marginSummary", {})
                    acct_val = float(margin.get("accountValue") or 0.0)
                    margin_used = float(margin.get("totalMarginUsed") or 0.0)
                    
                    positions = []
                    unrealized_pnl = 0.0
                    for p_item in data.get("assetPositions", []):
                        p = p_item.get("position", {})
                        szi = float(p.get("szi") or 0.0)
                        if szi == 0.0:
                            continue
                        pnl = float(p.get("unrealizedPnl") or 0.0)
                        unrealized_pnl += pnl
                        positions.append({
                            "coin": p.get("coin"),
                            "size": szi,
                            "value": float(p.get("positionValue") or 0.0),
                            "entry_price": float(p.get("entryPx") or 0.0),
                            "unrealized_pnl": pnl,
                            "leverage": p.get("leverage", {}).get("value", 1),
                            "margin_used": float(p.get("marginUsed") or 0.0)
                        })
                    
                    results.append({
                        "user": addr,
                        "alias": name,
                        "account_value": round(acct_val, 2),
                        "margin_used": round(margin_used, 2),
                        "unrealized_pnl": round(unrealized_pnl, 2),
                        "positions": positions,
                        "position_count": len(positions)
                    })
                except Exception as e:
                    logger.debug("Failed fetching trader state for %s: %s", addr, e)
                    continue
            
            results.sort(key=lambda x: -x["account_value"])
            
            with state.lock:
                state.hyperliquid_traders = results
                
            time.sleep(30)
        except Exception as e:
            logger.error("Error in Hyperliquid traders loop: %s", e)
            time.sleep(15)


# ============================================================== Routes
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/whale/data")
def api_whale_data():
    with state.lock:
        trades = list(state.hyperliquid_whale_trades)
        orderbook = state.hyperliquid_orderbook_analysis
        
    bias = {}
    for coin in ["BTC", "ETH", "SOL"]:
        coin_trades = [t for t in trades if t["symbol"] == coin]
        buy_vol = sum(t["value_usd"] for t in coin_trades if t["side"] == "BUY")
        sell_vol = sum(t["value_usd"] for t in coin_trades if t["side"] == "SELL")
        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            buy_pct = round(buy_vol / total_vol * 100.0, 1)
            sell_pct = round(100.0 - buy_pct, 1)
        else:
            buy_pct = 50.0
            sell_pct = 50.0
        bias[coin] = {
            "buy_pct": buy_pct,
            "sell_pct": sell_pct,
            "buy_volume": round(buy_vol, 2),
            "sell_volume": round(sell_vol, 2)
        }
        
    return jsonify({
        "trades": trades,
        "orderbook": orderbook,
        "bias": bias
    })


@app.route("/api/whale/traders")
def api_whale_traders():
    with state.lock:
        return jsonify(state.hyperliquid_traders)


_last_tick_at = 0.0
_last_tick_lock = Lock()
def _tick_open_prices():
    """Refresh last_prices for open positions (display only — no SL/TP eval).
    Throttled to once every 5s to avoid hammering the API."""
    import time as _t
    global _last_tick_at
    with _last_tick_lock:
        if _t.time() - _last_tick_at < 5:
            return
        _last_tick_at = _t.time()
    try:
        from delta_client import get_ohlcv
        symbols = {t["symbol"] for t in state.tester.trades if t["status"] == "OPEN"}
        for sym in symbols:
            candles = get_ohlcv(sym, "1m", 2) or []
            if candles:
                state.tester.last_prices[sym] = candles[-1]["close"]
    except Exception as e:
        logger.debug("tick failed: %s", e)


@app.route("/api/regime/<source>/<path:symbol>")
def api_regime(source: str, symbol: str):
    """Generic Markov regime endpoint.
    source ∈ {'delta', 'yfinance'}.  Returns current state + next-day P(state)."""
    try:
        import markov as _m
        if source == "delta":
            closes = _m._closes_from_delta(symbol, years=5)
        else:
            closes = _m._closes_from_yfinance(symbol, years=5)
        if not closes or len(closes) < 30:
            return jsonify({"available": False, "symbol": symbol, "source": source})
        reg = _m.current_regime(closes)
        labels = _m._label_regimes(closes)
        if len(labels) >= 30 and reg.get("state_idx") is not None:
            P = _m._transition_matrix(labels)
            cur = reg["state_idx"]
            reg["next_bear"]     = round(P[cur][0] * 100, 1)
            reg["next_sideways"] = round(P[cur][1] * 100, 1)
            reg["next_bull"]     = round(P[cur][2] * 100, 1)
        # Latest price for context
        reg["price"] = float(closes[-1])
        reg["symbol"] = symbol
        reg["source"] = source
        reg["available"] = True
        return jsonify(reg)
    except Exception as e:
        return jsonify({"available": False, "error": str(e), "symbol": symbol, "source": source}), 500


@app.route("/api/btc_indicators")
def api_btc_indicators():
    """Live BTC: price + EMA/RSI/MACD/BB/Stoch/ADX/ATR + 24h market + funding/OI."""
    from delta_client import get_ohlcv, get_ticker
    from indicators import ema, rsi, macd, bollinger, stochastic, adx, atr
    candles = get_ohlcv("BTCUSD", "15m", 200) or []
    if len(candles) < 50:
        return jsonify({"available": False})
    closes = [c["close"] for c in candles]
    e9    = ema(closes, 9)[-1]  if len(closes) >= 9  else 0.0
    e21   = ema(closes, 21)[-1] if len(closes) >= 21 else 0.0
    e50   = ema(closes, 50)[-1] if len(closes) >= 50 else 0.0
    r14   = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
    bb_u, bb_m, bb_l, pct_b = bollinger(closes, 20, 2.0)
    stoch_k = stochastic(candles, 14)
    adx_v   = adx(candles, 14)
    atr_v   = atr(candles, 14)
    price = closes[-1]
    atr_pct = (atr_v / price * 100) if price else 0.0
    # 24h window = 96 × 15-min candles
    win = candles[-96:] if len(candles) >= 96 else candles
    high_24h = max(c["high"] for c in win)
    low_24h  = min(c["low"]  for c in win)
    ref      = win[0]["close"]
    change_24h_pct = ((price - ref) / ref * 100.0) if ref else 0.0
    series = [round(c["close"], 2) for c in win]

    # Market structure (funding / OI / volume) — best-effort, may be None
    fund_rate = oi = vol_24h = mark_price = None
    try:
        tk = get_ticker("BTCUSD") or {}
        fund_rate  = float(tk.get("funding_rate") or 0) * 100.0 if tk.get("funding_rate") is not None else None
        oi         = float(tk.get("open_interest") or 0) or None
        vol_24h    = float(tk.get("volume") or tk.get("volume_24h") or 0) or None
        mark_price = float(tk.get("mark_price") or 0) or None
    except Exception:
        pass

    # Markov regime — uses daily candles + cached transition matrix
    btc_regime = None
    try:
        import markov as _m
        from delta_client import get_ohlcv as _go
        daily = _go("BTCUSD", "1d", 90) or []
        closes_d = [c["close"] for c in daily]
        btc_regime = _m.current_regime(closes_d) if closes_d else None
        # If we have a cached score, surface next-day probabilities from the
        # transition matrix (computed lazily here from the daily closes).
        if btc_regime and closes_d and btc_regime.get("state_idx") is not None:
            labels = _m._label_regimes(closes_d)
            if len(labels) >= 30:
                P = _m._transition_matrix(labels)
                cur = btc_regime["state_idx"]
                btc_regime["next_bear"]     = round(P[cur][0] * 100, 1)
                btc_regime["next_sideways"] = round(P[cur][1] * 100, 1)
                btc_regime["next_bull"]     = round(P[cur][2] * 100, 1)
    except Exception as e:
        logger.debug("BTC regime calc failed: %s", e)

    return jsonify({
        "available": True,
        "symbol":    "BTCUSD",
        "price":     round(price, 2),
        "regime":    btc_regime,
        "mark_price": round(mark_price, 2) if mark_price else None,
        # Trend / momentum primitives
        "ema9":  round(e9, 2),
        "ema21": round(e21, 2),
        "ema50": round(e50, 2),
        "rsi14": round(r14, 2),
        "rsi_label": "bullish" if r14 > 50 else "bearish" if r14 < 50 else "neutral",
        "macd_line":   m_line,
        "macd_signal": m_sig,
        "macd_hist":   m_hist,
        "bb_upper":  bb_u,
        "bb_middle": bb_m,
        "bb_lower":  bb_l,
        "bb_pct":    pct_b,
        "stoch_k":   stoch_k,
        "adx":       adx_v,
        "atr":       round(atr_v, 2),
        "atr_pct":   round(atr_pct, 3),
        # Checks
        "trend_ok":     e9 > e21,
        "momentum_ok":  r14 > 50,
        "macd_ok":      m_hist > 0,
        "strong_trend": adx_v >= 25,
        "vol_ok":       0.3 <= atr_pct <= 3.0,
        # 24h market
        "high_24h":  round(high_24h, 2),
        "low_24h":   round(low_24h, 2),
        "change_24h_pct": round(change_24h_pct, 3),
        "series":    series,
        # Market structure / "fundamentals" for a perpetual
        "funding_rate_pct": round(fund_rate, 4) if fund_rate is not None else None,
        "open_interest":    round(oi, 2)        if oi        is not None else None,
        "volume_24h":       round(vol_24h, 2)   if vol_24h   is not None else None,
    })


@app.route("/api/state")
def api_state():
    """Top-level dashboard state — polled every 2s by the frontend."""
    _tick_open_prices()
    is_running = state.engine.is_running() if state.engine else False
    with state.lock:
        return jsonify({
            "is_running":    is_running,
            "scan_status":   state.scan_status,
            "symbols":       state.symbols,
            "symbol_count":  len(state.symbols),
            "stats":         state.tester.get_stats(),
            "open_trades":   state.tester.get_open_trades(),
            "last_scan":     state.last_scan,
            "regime": {
                "btc_24h_pct":   round(state.regime_btc_24h, 3),
                "skipped_last":  state.regime_skipped_last,
            },
        })


@app.route("/api/signals")
def api_signals():
    with state.lock:
        return jsonify(list(state.signals))


@app.route("/api/watchlist")
def api_watchlist():
    with state.lock:
        # Sort by symbol name for stable display
        rows = sorted(state.watchlist.values(), key=lambda r: r["symbol"])
        return jsonify(rows)


@app.route("/api/screener")
def api_screener():
    """Per-symbol bias + multi-factor scoring + ranking for the AI Screener panel.

    Factor scores (each 0–100, higher = more bullish/tradeable):
      • trend     — EMA-stack alignment + slope (EMA7 vs EMA21, distance to trend EMA)
      • momentum  — 24h % change, normalized to ±10% range
      • volatility— ATR% sweet spot (0.5–4% = tradeable, too low/high penalized)
      • regime    — Trending=100, Mixed=50, Choppy=0
      • composite — weighted average (trend 35%, momentum 30%, regime 20%, vol 15%)
    """
    import time as _time
    with state.lock:
        rows = list(state.watchlist.values())

    now = int(_time.time())
    enriched = []
    bullish = bearish = neutral = 0
    bias_score_total = 0.0

    # ── Factor helpers (each returns 0..100) ──
    def _trend_score(d):
        e7  = d.get("ema7"); e21 = d.get("ema21"); te = d.get("trend_ema")
        price = d.get("price") or 0
        if not (e7 and e21 and te and price): return 50.0
        s = 50.0
        s += 15 if e7 > e21 else -15
        s += 15 if price > te else -15
        # EMA spread normalised
        sp = d.get("ema_spread_pct", 0.0) or 0.0
        s += max(-10, min(10, sp * 5))
        return max(0.0, min(100.0, s))

    def _momentum_score(d):
        ch = d.get("change_24h", 0.0) or 0.0
        # Map -10%..+10% to 0..100
        return max(0.0, min(100.0, 50.0 + ch * 5.0))

    def _volatility_score(d):
        ap = d.get("atr_pct", 0.0) or 0.0
        # Sweet spot 1–3% ATR; <0.3% boring, >5% chaotic
        if ap < 0.3:  return ap / 0.3 * 40
        if ap < 1.0:  return 40 + (ap - 0.3) / 0.7 * 30
        if ap < 3.0:  return 70 + (ap - 1.0) / 2.0 * 30   # 70..100
        if ap < 5.0:  return 100 - (ap - 3.0) / 2.0 * 30  # 100..70
        return max(0.0, 70 - (ap - 5.0) * 10)             # decay

    def _regime_score(d):
        reg = (d.get("regime") or "").lower()
        return {"trending": 100, "mixed": 50, "choppy": 10}.get(reg, 50)

    W = {"trend": 0.35, "momentum": 0.30, "regime": 0.20, "vol": 0.15}

    for d in rows:
        bias = d.get("bias", "Neutral")
        if   bias == "Bullish": bullish += 1
        elif bias == "Bearish": bearish += 1
        else:                   neutral += 1
        bias_score_total += d.get("bias_score", 0.0)

        ts = _trend_score(d)
        ms = _momentum_score(d)
        vs = _volatility_score(d)
        rs = _regime_score(d)
        composite = W["trend"]*ts + W["momentum"]*ms + W["regime"]*rs + W["vol"]*vs

        enriched.append({
            "symbol":      d["symbol"],
            "underlying":  d.get("underlying", d["symbol"]),
            "description": d.get("description", ""),
            "price":       d["price"],
            "change_24h":  d.get("change_24h", 0.0),
            "bias":        bias,
            "bias_score":  d.get("bias_score", 0.0),
            "age_seconds": max(0, now - int(d.get("time", now))),
            # Factor scores (each 0..100, higher = better for LONG)
            "trend_score":      round(ts, 1),
            "momentum_score":   round(ms, 1),
            "volatility_score": round(vs, 1),
            "regime_score":     round(rs, 1),
            "composite_score":  round(composite, 1),
        })

    # Rank (1 = best) by composite score (descending)
    enriched.sort(key=lambda r: -r["composite_score"])
    for i, r in enumerate(enriched, 1):
        r["rank"] = i

    n = len(enriched) or 1
    avg = bias_score_total / n
    market_bias = "Bullish" if avg >= 0.2 else "Bearish" if avg <= -0.2 else "Neutral"
    gauge_pct = round((avg + 1) * 50, 1)

    return jsonify({
        "market_bias":  market_bias,
        "gauge_pct":    gauge_pct,
        "avg_score":    round(avg, 3),
        "counts":       {"bullish": bullish, "bearish": bearish, "neutral": neutral},
        "rows":         enriched,
        "weights":      W,
    })


@app.route("/api/strategies")
def api_strategies():
    """Per-strategy breakdown — includes ALL strategies in the active universe,
    even ones with no trades yet."""
    from config import get_params

    def _empty(name: str) -> dict:
        return {"strategy": name, "trades": 0, "open": 0, "wins": 0,
                "losses": 0, "timeouts": 0, "realized_pnl": 0.0,
                "symbols": set(), "win_rate": 0.0}

    by_strat: dict[str, dict] = {}

    # Seed with every strategy that exists in the currently scanned universe,
    # even if no trades have fired for it yet.
    for sym in state.symbols or []:
        strat = get_params(sym).get("strategy", "donchian")
        s = by_strat.setdefault(strat, _empty(strat))
        s["symbols"].add(sym)

    # Always include built-in strategies even if not yet routed (UX nicety)
    for built_in in ("donchian", "gold"):
        by_strat.setdefault(built_in, _empty(built_in))

    # Tally trades
    for t in state.tester.trades:
        strat = get_params(t["symbol"]).get("strategy", "donchian")
        s = by_strat.setdefault(strat, _empty(strat))
        s["trades"] += 1
        s["symbols"].add(t["symbol"])
        if t["status"] == "OPEN":      s["open"] += 1
        elif t["status"] == "WIN":     s["wins"] += 1;     s["realized_pnl"] += t.get("pnl_usd") or 0
        elif t["status"] == "LOSS":    s["losses"] += 1;   s["realized_pnl"] += t.get("pnl_usd") or 0
        elif t["status"] == "TIMEOUT": s["timeouts"] += 1; s["realized_pnl"] += t.get("pnl_usd") or 0

    out = []
    for s in by_strat.values():
        closed = s["wins"] + s["losses"] + s["timeouts"]
        s["win_rate"]     = round(s["wins"]/closed*100, 1) if closed else 0.0
        s["realized_pnl"] = round(s["realized_pnl"], 2)
        s["symbols"]      = len(s["symbols"])
        s["currency"]     = "$"
        out.append(s)

    # Append Indian-ORB (it has its own engine/account, separate from Delta)
    if state.mcx_engine:
        snap = state.mcx_engine.get_state()
        acct = snap.get("account", {})
        out.append({
            "strategy":     "Indian-ORB",
            "trades":       acct.get("trades", 0) + len(snap.get("open_positions", [])),
            "open":         len(snap.get("open_positions", [])),
            "wins":         acct.get("wins", 0),
            "losses":       acct.get("losses", 0),
            "timeouts":     acct.get("timeouts", 0),
            "realized_pnl": acct.get("realized_pnl_inr", 0.0),
            "win_rate":     acct.get("win_rate", 0.0),
            "symbols":      len(snap.get("symbols", [])),
            "currency":     "₹",
        })

    # Active strategies (with trades) first, then idle ones
    out.sort(key=lambda r: (r["trades"] == 0, -r["realized_pnl"], r["strategy"]))
    return jsonify(out)


@app.route("/api/symbol_pnl")
def api_symbol_pnl():
    """Per-symbol P&L breakdown with deeper analysis."""
    by_sym: dict[str, list[dict]] = {}
    for t in state.tester.trades:
        by_sym.setdefault(t.get("symbol", "?"), []).append(t)

    out = []
    for sym, trades in by_sym.items():
        # Sort by entry time for streak + equity curve
        trades_sorted = sorted(trades, key=lambda t: t.get("entry_time") or 0)
        closed_sorted = [t for t in trades_sorted if t.get("status") != "OPEN"
                         and t.get("pnl_usd") is not None]

        n_total = len(trades_sorted)
        open_n  = sum(1 for t in trades_sorted if t.get("status") == "OPEN")
        wins    = [t for t in trades_sorted if t.get("status") == "WIN"]
        losses  = [t for t in trades_sorted if t.get("status") == "LOSS"]
        timeouts= [t for t in trades_sorted if t.get("status") == "TIMEOUT"]
        closed_n = len(wins) + len(losses) + len(timeouts)

        wins_pnl   = sum(t.get("pnl_usd", 0) for t in wins)
        losses_pnl = sum(t.get("pnl_usd", 0) for t in losses)
        timeout_pnl= sum(t.get("pnl_usd", 0) for t in timeouts)
        total_pnl  = wins_pnl + losses_pnl + timeout_pnl

        # Side breakdown — including W/L per side
        long_trades  = [t for t in closed_sorted if t.get("side") == "LONG"]
        short_trades = [t for t in closed_sorted if t.get("side") == "SHORT"]
        long_pnl  = sum(t.get("pnl_usd", 0) for t in long_trades)
        short_pnl = sum(t.get("pnl_usd", 0) for t in short_trades)
        long_wins   = sum(1 for t in long_trades  if t.get("status") == "WIN")
        long_losses = sum(1 for t in long_trades  if t.get("status") == "LOSS")
        short_wins   = sum(1 for t in short_trades if t.get("status") == "WIN")
        short_losses = sum(1 for t in short_trades if t.get("status") == "LOSS")
        long_wr  = (long_wins  / (long_wins  + long_losses) * 100) if (long_wins  + long_losses) else 0.0
        short_wr = (short_wins / (short_wins + short_losses) * 100) if (short_wins + short_losses) else 0.0
        # Best side = higher P&L (with at least 1 closed trade). None if both zero.
        if not long_trades and not short_trades:
            best_side = None
        elif not short_trades or long_pnl > short_pnl:
            best_side = "LONG"
        elif not long_trades or short_pnl > long_pnl:
            best_side = "SHORT"
        else:
            best_side = "TIE"

        # Streaks (consecutive wins/losses, ignoring timeouts)
        streak_w = streak_l = best_w = best_l = 0
        for t in closed_sorted:
            st = t.get("status")
            if st == "WIN":
                streak_w += 1; streak_l = 0
                if streak_w > best_w: best_w = streak_w
            elif st == "LOSS":
                streak_l += 1; streak_w = 0
                if streak_l > best_l: best_l = streak_l
            else:
                streak_w = streak_l = 0

        # Avg hold time (bars)
        bars_vals = [t.get("bars_held", 0) for t in closed_sorted if t.get("bars_held")]
        avg_bars = sum(bars_vals) / len(bars_vals) if bars_vals else 0

        # Equity sparkline (cumulative P&L per closed trade, max ~30 points)
        running = 0.0
        equity_pts = []
        for t in closed_sorted:
            running += t.get("pnl_usd", 0)
            equity_pts.append(round(running, 2))
        if len(equity_pts) > 30:
            step = max(1, len(equity_pts) // 30)
            equity_pts = equity_pts[::step][-30:]

        avg_win  = (wins_pnl   / len(wins))   if wins   else 0.0
        avg_loss = (losses_pnl / len(losses)) if losses else 0.0
        pf = (wins_pnl / -losses_pnl) if losses_pnl < 0 else (float("inf") if wins_pnl > 0 else 0.0)
        expectancy = (total_pnl / closed_n) if closed_n else 0.0
        bests = max((t.get("pnl_usd", 0) for t in closed_sorted), default=0)
        worsts = min((t.get("pnl_usd", 0) for t in closed_sorted), default=0)

        out.append({
            "symbol":     sym,
            "trades":     n_total,
            "open":       open_n,
            "closed":     closed_n,
            "wins":       len(wins),
            "losses":     len(losses),
            "timeouts":   len(timeouts),
            "win_rate":   round(len(wins) / closed_n * 100, 1) if closed_n else 0.0,
            "avg_win":    round(avg_win,  2),
            "avg_loss":   round(avg_loss, 2),
            "pnl":        round(total_pnl, 2),
            "best":       round(bests, 2),
            "worst":      round(worsts, 2),
            "profit_factor": (None if pf == float("inf") else round(pf, 2)),
            "expectancy": round(expectancy, 2),
            "best_win_streak":  best_w,
            "best_loss_streak": best_l,
            "avg_bars":   round(avg_bars, 1),
            # Sides
            "long_trades":   len(long_trades),
            "short_trades":  len(short_trades),
            "long_pnl":      round(long_pnl, 2),
            "short_pnl":     round(short_pnl, 2),
            "long_wins":     long_wins,
            "long_losses":   long_losses,
            "short_wins":    short_wins,
            "short_losses":  short_losses,
            "long_win_rate": round(long_wr, 1),
            "short_win_rate":round(short_wr, 1),
            "best_side":     best_side,
            # Equity sparkline + time
            "equity_pts":   equity_pts,
            "first_ts":     int(trades_sorted[0].get("entry_time") or 0) if trades_sorted else 0,
            "last_ts":      int(trades_sorted[-1].get("entry_time") or 0) if trades_sorted else 0,
        })
    out.sort(key=lambda r: -r["pnl"])
    return jsonify(out)


@app.route("/api/markov")
def api_markov():
    """All cached Markov scores."""
    try:
        import markov as _m
        from config import MARKOV_MIN_SHARPE, MARKOV_FILTER_ENABLED
        rows = _m.get_all_scores()
        return jsonify({
            "enabled":    MARKOV_FILTER_ENABLED,
            "min_sharpe": MARKOV_MIN_SHARPE,
            "rows":       rows,
            "count":      len(rows),
        })
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e), "rows": []})


@app.route("/api/markov/refresh", methods=["POST"])
def api_markov_refresh():
    """Force an immediate Markov refresh (runs in a thread)."""
    try:
        import markov as _m, threading
        symbols_crypto = []
        try:
            from delta_client import get_perpetual_contracts
            symbols_crypto = [p["symbol"] for p in get_perpetual_contracts(apply_markov=False)]
        except Exception: pass
        symbols_nse = list(NSE_FNO_SYMBOLS) if NSE_FNO_SYMBOLS else []
        def _go(): _m.refresh_universe(symbols_crypto, symbols_nse)
        threading.Thread(target=_go, daemon=True).start()
        return jsonify({"ok": True,
                        "queued_crypto": len(symbols_crypto),
                        "queued_nse":    len(symbols_nse)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/portfolio")
def api_portfolio():
    """Allocation + pairwise correlation of open positions."""
    import math
    open_trades = state.tester.get_open_trades()
    total_notional = sum(t["notional_usd"] for t in open_trades) or 1.0

    allocation = [
        {"symbol": t["symbol"], "notional": t["notional_usd"],
         "pct": round(t["notional_usd"]/total_notional*100, 2),
         "side": t["side"], "pnl": t.get("pnl_usd_live") or 0.0,
         "pnl_pct": t.get("pnl_pct_live") or 0.0}
        for t in open_trades
    ]

    # Correlation matrix from recent close-price returns
    try:
        from delta_client import get_ohlcv
        symbols = [t["symbol"] for t in open_trades]
        series = {}
        for s in symbols:
            candles = get_ohlcv(s, "15m", 60) or []
            closes = [c["close"] for c in candles]
            if len(closes) >= 10:
                series[s] = [(closes[i]-closes[i-1])/closes[i-1]
                             for i in range(1,len(closes)) if closes[i-1]]
        # Truncate to common length
        if series:
            n = min(len(v) for v in series.values())
            for k in series: series[k] = series[k][-n:]

        def corr(a, b):
            if len(a) < 2: return 0.0
            ma = sum(a)/len(a); mb = sum(b)/len(b)
            num = sum((a[i]-ma)*(b[i]-mb) for i in range(len(a)))
            da = math.sqrt(sum((x-ma)**2 for x in a))
            db = math.sqrt(sum((x-mb)**2 for x in b))
            return num/(da*db) if da*db else 0.0

        syms = list(series.keys())
        matrix = [[round(corr(series[s1], series[s2]), 2) for s2 in syms] for s1 in syms]
    except Exception as e:
        logger.warning("Correlation calc failed: %s", e)
        syms, matrix = [], []

    return jsonify({
        "allocation":  allocation,
        "corr_symbols": syms,
        "corr_matrix":  matrix,
    })


@app.route("/api/trades")
def api_trades():
    return jsonify(state.tester.get_trades(200))


@app.route("/api/equity")
def api_equity():
    return jsonify(state.tester.get_equity_curve(500))


@app.route("/api/logs")
def api_logs():
    with state.lock:
        return jsonify(list(state.logs)[-200:])


@app.route("/api/start", methods=["POST"])
def api_start():
    if state.engine and state.engine.is_running():
        return jsonify({"ok": False, "msg": "Already running"}), 400
    send_tg = request.json.get("send_telegram", True) if request.is_json else True
    # Crypto (Delta) engine
    state.engine = BotEngine(on_event=state.on_engine_event, send_telegram=send_tg,
                             forward_tester=state.tester)
    state.engine.start()
    # MCX (Indian) engine — runs in parallel
    if MCX_AVAILABLE:
        if state.mcx_engine and state.mcx_engine.is_running():
            pass
        else:
            state.mcx_engine = MCXBot(on_event=state.on_mcx_event, send_telegram=send_tg)
            state.mcx_engine.start()
    state.log("INFO", "Start requested via web UI (Delta + MCX)" if MCX_AVAILABLE else "Start requested via web UI")
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    if state.engine:
        state.engine.stop()
    if state.mcx_engine:
        state.mcx_engine.stop()
    state.log("INFO", "Stop requested via web UI")
    return jsonify({"ok": True})


@app.route("/api/mcx")
def api_mcx():
    """Indian-ORB bot state for the dashboard."""
    if not state.mcx_engine:
        return jsonify({"available": MCX_AVAILABLE, "running": False,
                        "symbols": [], "orb": {}, "open_positions": [], "history": []})
    snap = state.mcx_engine.get_state()
    snap["available"] = True
    return jsonify(snap)


@app.route("/api/export/trades.csv")
def api_export_trades_csv():
    """Stream trades.csv straight from SQLite."""
    import io, csv as _csv
    from flask import Response
    buf = io.StringIO()
    cols = state.tester._CORE_COLS + ["level_label", "level_price", "use_trailing",
                                       "trail_distance", "high_water"]
    w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    with state.tester.lock:
        for t in sorted(state.tester.trades, key=lambda x: x.get("entry_time") or 0):
            w.writerow(t)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=trades.csv"})


@app.route("/api/export/indian_orb.csv")
def api_export_indian_orb_csv():
    """Stream the Indian-ORB trades straight from the indian_orb_trades table."""
    import io, csv as _csv, sqlite3
    from pathlib import Path
    from flask import Response
    db_path = Path(__file__).parent / "ft_state.db"
    buf = io.StringIO()
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM indian_orb_trades ORDER BY entry_time_ts"
        ).fetchall()
        con.close()
    except sqlite3.OperationalError:
        rows = []  # table doesn't exist yet (Indian-ORB never started)
    if rows:
        cols = list(rows[0].keys())
        w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in cols})
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=indian_orb_trades.csv"})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if state.engine and state.engine.is_running():
        return jsonify({"ok": False, "msg": "Stop the bot before resetting"}), 400
    state.tester.reset()
    state.signals.clear()
    state.log("INFO", "Forward-test account reset to starting balance")
    return jsonify({"ok": True})


def _autostart_engines() -> None:
    """Spin up the Delta + MCX engines automatically when the web app boots
    so users don't have to click Start after every server restart.
    Disable by setting AUTOSTART=0 in the environment."""
    if os.getenv("AUTOSTART", "1") == "0":
        state.log("INFO", "AUTOSTART=0 — engines will NOT start until /api/start is called")
        return
    try:
        if not (state.engine and state.engine.is_running()):
            state.engine = BotEngine(on_event=state.on_engine_event,
                                     send_telegram=True,
                                     forward_tester=state.tester)
            state.engine.start()
            state.log("INFO", "Auto-started Delta (crypto) engine on boot")
        if MCX_AVAILABLE and not (state.mcx_engine and state.mcx_engine.is_running()):
            state.mcx_engine = MCXBot(on_event=state.on_mcx_event, send_telegram=True)
            state.mcx_engine.start()
            state.log("INFO", "Auto-started MCX (Indian-ORB) engine on boot")
    except Exception as e:
        state.log("ERROR", f"Auto-start failed: {e}")


# ============================================================== Run
if __name__ == "__main__":
    print(f"\nDelta Bot Dashboard -> http://{WEB_HOST}:{WEB_PORT}\n")
    _autostart_engines()
    # Start Hyperliquid background trackers
    Thread(target=hyperliquid_whale_loop, daemon=True).start()
    Thread(target=hyperliquid_traders_loop, daemon=True).start()
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
