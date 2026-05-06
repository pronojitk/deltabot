"""
Web-based dashboard for the Delta Exchange Alert Bot.

Run with:  py web_app.py
Then open: http://127.0.0.1:5000

Provides:
  • Live forward-test stats: balance, equity, leverage, P&L, margin
  • Open positions with live unrealized P&L
  • Recent signals stream
  • Trade history table
  • Equity-curve chart
  • Start / Stop bot controls
"""

import logging
from collections import deque
from threading import Lock
from datetime import datetime, timezone

from flask import Flask, render_template, jsonify, request

from bot import BotEngine
from forward_test import ForwardTester
from config import WEB_HOST, WEB_PORT
try:
    from mcx_bot import MCXBot
    MCX_AVAILABLE = True
except Exception as _e:
    MCX_AVAILABLE = False
    MCXBot = None
    logging.getLogger("web").warning("MCX bot unavailable: %s", _e)

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
        self.lock = Lock()

    def on_mcx_event(self, event: dict) -> None:
        with self.lock:
            t = event.get("type")
            if t == "mcx_signal":
                self.mcx_signals.appendleft(event["trade"])
                self.log("SIGNAL", f"MCX {event['trade']['side']} {event['trade']['symbol']} @ {event['trade']['entry']:.2f}")
            elif t == "mcx_closed":
                tr = event["trade"]
                self.log("TRADE", f"MCX CLOSE {tr['side']} {tr['symbol']} {tr['exit_reason']} @ {tr.get('exit_price'):.2f} | {tr['pnl_pct']:.2f}%")
            elif t == "mcx_startup":
                self.log("INFO", f"MCX bot started — {len(event.get('symbols', []))} symbols")
            elif t == "mcx_stopped":
                self.log("INFO", "MCX bot stopped")

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

            elif t == "fatal":
                self.log("FATAL", str(event.get("error")))
                self.scan_status = "stopped (fatal)"

            elif t == "stopped":
                self.scan_status = "stopped"
                self.log("INFO", "Bot stopped")


state = AppState()


# ============================================================== Routes
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


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
    """Per-symbol bias + overall market bias for the AI Screener panel."""
    import time as _time
    with state.lock:
        rows = list(state.watchlist.values())

    now = int(_time.time())
    enriched = []
    bullish = bearish = neutral = 0
    score_total = 0.0

    for d in rows:
        bias = d.get("bias", "Neutral")
        if   bias == "Bullish": bullish += 1
        elif bias == "Bearish": bearish += 1
        else:                    neutral += 1
        score_total += d.get("bias_score", 0.0)
        enriched.append({
            "symbol":      d["symbol"],
            "underlying":  d.get("underlying", d["symbol"]),
            "description": d.get("description", ""),
            "price":       d["price"],
            "change_24h":  d.get("change_24h", 0.0),
            "bias":        bias,
            "bias_score":  d.get("bias_score", 0.0),
            "age_seconds": max(0, now - int(d.get("time", now))),
        })

    n = len(enriched) or 1
    avg = score_total / n
    market_bias = "Bullish" if avg >= 0.2 else "Bearish" if avg <= -0.2 else "Neutral"
    # Map -1..+1 → 0..100 for the gauge (0 = far left bearish, 100 = far right bullish)
    gauge_pct = round((avg + 1) * 50, 1)

    enriched.sort(key=lambda r: -r["bias_score"])   # bullish first

    return jsonify({
        "market_bias":  market_bias,
        "gauge_pct":    gauge_pct,
        "avg_score":    round(avg, 3),
        "counts":       {"bullish": bullish, "bearish": bearish, "neutral": neutral},
        "rows":         enriched,
    })


@app.route("/api/strategies")
def api_strategies():
    """Per-strategy performance breakdown (Donchian vs Gold, etc)."""
    from config import get_params
    trades = state.tester.trades
    by_strat: dict[str, dict] = {}
    for t in trades:
        strat = get_params(t["symbol"]).get("strategy", "donchian")
        s = by_strat.setdefault(strat, {
            "strategy": strat, "trades": 0, "open": 0, "wins": 0, "losses": 0,
            "timeouts": 0, "realized_pnl": 0.0, "symbols": set(),
        })
        s["trades"] += 1
        s["symbols"].add(t["symbol"])
        if t["status"] == "OPEN":   s["open"] += 1
        elif t["status"] == "WIN":  s["wins"] += 1; s["realized_pnl"] += t.get("pnl_usd") or 0
        elif t["status"] == "LOSS": s["losses"] += 1; s["realized_pnl"] += t.get("pnl_usd") or 0
        elif t["status"] == "TIMEOUT": s["timeouts"] += 1; s["realized_pnl"] += t.get("pnl_usd") or 0

    out = []
    for s in by_strat.values():
        closed = s["wins"] + s["losses"] + s["timeouts"]
        s["win_rate"] = round(s["wins"]/closed*100, 1) if closed else 0.0
        s["realized_pnl"] = round(s["realized_pnl"], 2)
        s["symbols"] = len(s["symbols"])
        out.append(s)
    out.sort(key=lambda r: -r["realized_pnl"])
    return jsonify(out)


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
    """MCX bot state for the dashboard."""
    if not state.mcx_engine:
        return jsonify({"available": MCX_AVAILABLE, "running": False,
                        "symbols": [], "orb": {}, "open_positions": [], "history": []})
    snap = state.mcx_engine.get_state()
    snap["available"] = True
    return jsonify(snap)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    if state.engine and state.engine.is_running():
        return jsonify({"ok": False, "msg": "Stop the bot before resetting"}), 400
    state.tester.reset()
    state.signals.clear()
    state.log("INFO", "Forward-test account reset to starting balance")
    return jsonify({"ok": True})


# ============================================================== Run
if __name__ == "__main__":
    print(f"\nDelta Bot Dashboard -> http://{WEB_HOST}:{WEB_PORT}\n")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
