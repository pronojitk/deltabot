"""
Delta Exchange Breakout/Breakdown Alert Bot.

Two ways to use:
  1. CLI:   `py bot.py`             — runs scan loop, sends Telegram alerts
  2. GUI:   `py gui.py`             — Tkinter GUI imports BotEngine from here
"""

import time
import logging
import signal
import sys
import threading
from datetime import datetime, timezone
from typing import Callable

from config import TIMEFRAME, CANDLE_LIMIT, SCAN_INTERVAL, ALERT_COOLDOWN, STRATEGY_NAME, get_params
from delta_client import get_perpetual_contracts, get_ohlcv
from indicators import detect_signals, detect_signals_for_symbol
from telegram_alert import send_alert, send_startup_message
from forward_test import ForwardTester

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")


EventCallback = Callable[[dict], None]


class BotEngine:
    """
    Reusable scan-engine. Runs in the calling thread (blocking `run()`)
    or can be controlled via `start()`/`stop()` from another thread.
    """

    def __init__(self, on_event: EventCallback | None = None,
                 send_telegram: bool = True,
                 forward_tester: "ForwardTester | None" = None):
        self.on_event = on_event or (lambda e: None)
        self.send_telegram = send_telegram
        self.forward_tester = forward_tester or ForwardTester()
        self.alert_cooldowns: dict[str, dict[str, float]] = {}
        self.symbols: list[str] = []
        self.contract_info: dict[str, dict] = {}   # symbol -> {description, underlying}
        self.scan_count = 0
        self._running = False
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    # ----------------------------------------------------------- helpers
    def _emit(self, event_type: str, **kwargs) -> None:
        try:
            self.on_event({"type": event_type, **kwargs})
        except Exception as e:
            logger.error("Event callback error: %s", e)

    def _is_on_cooldown(self, symbol: str, sig_type: str) -> bool:
        last = self.alert_cooldowns.get(symbol, {}).get(sig_type, 0)
        return (time.time() - last) < ALERT_COOLDOWN

    def _mark_alerted(self, symbol: str, sig_type: str) -> None:
        self.alert_cooldowns.setdefault(symbol, {})[sig_type] = time.time()

    # ----------------------------------------------------------- scanning
    def scan_symbol(self, symbol: str) -> tuple[list[dict], float | None, int | None, dict | None]:
        """Returns (signals, current_close, current_time, diagnostics).
        Strategy: Donchian + EMA trend filter + ATR trail + entropy.
        Per-symbol params (timeframe, donchian_period, etc.) come from config.SYMBOL_PARAMS."""
        from indicators import (
            compute_emas, returns_entropy, market_regime,
            donchian_channel, atr, ema,
        )
        from config import get_params

        params = get_params(symbol)
        candles = get_ohlcv(symbol, params["timeframe"], CANDLE_LIMIT)
        # Gold strategy needs an additional higher-timeframe series for bias
        candles_htf: list[dict] = []
        if params.get("strategy") == "gold":
            candles_htf = get_ohlcv(symbol, params["htf_timeframe"], CANDLE_LIMIT) or []
        if not candles:
            info = self.contract_info.get(symbol, {})
            stub = {
                "symbol":      symbol,
                "underlying":  info.get("underlying", symbol),
                "description": info.get("description", symbol),
                "price":       0.0,
                "time":        int(time.time()),
                "ema7": None, "ema21": None, "trend": "—",
                "ema_spread_pct": 0.0,
                "entropy":     1.0, "regime": "Choppy",
                "change_24h":  0.0,
                "bias":        "Neutral",
                "bias_score":  0.0,
                "resistance":  None, "support": None,
                "signals":     [],
                "stale":       True,
            }
            return [], None, None, stub

        signals = detect_signals_for_symbol(candles, candles_htf, params)
        last = candles[-1]

        ema7_series, ema21_series = compute_emas(candles)
        e7  = ema7_series[-1]
        e21 = ema21_series[-1]
        price = last["close"]

        # 24h % change (96 × 15m = 24h)
        ref = candles[-96] if len(candles) >= 96 else candles[0]
        change_24h = (price - ref["close"]) / ref["close"] * 100 if ref["close"] else 0.0

        ema_spread_pct = ((e7 - e21) / e21 * 100.0) if e21 else 0.0

        # Bias score from EMA alignment + 24h change
        score = 0.0
        if e7 and e21:
            score += 1.0 if e7 > e21 else -1.0
        score += max(-1.0, min(1.0, change_24h / 5.0))
        score = max(-1.0, min(1.0, score / 2.0))
        bias = "Bullish" if score >= 0.3 else "Bearish" if score <= -0.3 else "Neutral"

        # Regime detection via Shannon entropy
        ent = returns_entropy(candles)
        regime = market_regime(ent)

        # Diagnostic indicators — pick fields based on strategy
        atr_val = atr(candles, params.get("atr_period", 14))
        closes = [c["close"] for c in candles]
        atr_pct = (atr_val / price * 100.0) if price else 0.0

        if params.get("strategy") == "gold":
            # Gold strategy: 1h bias + 15m EMA21 pullback zone
            upper, lower = 0.0, 0.0
            trend_ema = 0.0
            if candles_htf:
                closes_1h = [c["close"] for c in candles_htf]
                ema_long = params.get("ema_long", 21)
                trend_ema = ema(closes_1h, ema_long)[-1] \
                            if len(closes_1h) >= ema_long else 0.0
        else:
            # Donchian: channel + EMA trend filter
            upper, lower = donchian_channel(candles, params["donchian_period"])
            trend_ema = ema(closes, params["trend_filter_ema"])[-1] \
                        if len(closes) >= params["trend_filter_ema"] else 0.0

        # Dashboard-compatible R/S = Donchian channel boundaries
        nearest_r = ("Donchian-H", upper, 0) if upper > 0 else None
        nearest_s = ("Donchian-L", lower, 0) if lower > 0 else None

        info = self.contract_info.get(symbol, {})
        diag = {
            "symbol":      symbol,
            "underlying":  info.get("underlying", symbol),
            "description": info.get("description", symbol),
            "price":       price,
            "time":        last["time"],
            "ema7":        round(e7,  8) if e7  else None,
            "ema21":       round(e21, 8) if e21 else None,
            "trend":       "UP" if e7 and e21 and e7 > e21 else
                           "DOWN" if e7 and e21 else "—",
            "ema_spread_pct": round(ema_spread_pct, 3),
            "change_24h":  round(change_24h, 2),
            "bias":        bias,
            "bias_score":  round(score, 3),
            "entropy":     round(ent, 3),
            "regime":      regime,
            "resistance":  None if not nearest_r else {
                "label": nearest_r[0], "price": round(nearest_r[1], 8),
                "tests": nearest_r[2],
                "dist_pct": round((nearest_r[1] - price) / price * 100, 3),
            },
            "support":     None if not nearest_s else {
                "label": nearest_s[0], "price": round(nearest_s[1], 8),
                "tests": nearest_s[2],
                "dist_pct": round((price - nearest_s[1]) / price * 100, 3),
            },
            "donchian_upper": round(upper, 8) if upper > 0 else None,
            "donchian_lower": round(lower, 8) if lower > 0 else None,
            "trend_ema":      round(trend_ema, 8) if trend_ema > 0 else None,
            "atr":            round(atr_val, 8),
            "atr_pct":        round(atr_pct, 3),
            "tf":             params["timeframe"],
            "strategy":       params.get("strategy", "donchian"),
            "don_period":     params.get("donchian_period"),
            "htf":            params.get("htf_timeframe"),
            "signals":        [s["type"] for s in signals],
        }
        return signals, price, last["time"], diag

    def _scan_cycle(self) -> tuple[int, int, int]:
        """Run one full scan. Returns (signals_found, alerts_sent, trades_closed)."""
        sig_count = alert_count = closed_count = 0

        for symbol in self.symbols:
            if not self._running:
                break
            try:
                signals, last_price, last_time, diag = self.scan_symbol(symbol)

                # Emit per-symbol diagnostic info (the watchlist row)
                if diag is not None:
                    self._emit("scan_data", **diag)
                    if diag.get("stale"):
                        self._emit("error", symbol=symbol, error="No candle data (API empty/rate-limited)")

                # Forward-test: check open trades against current price
                if last_price is not None:
                    self._emit("price", symbol=symbol, price=last_price)
                    closed = self.forward_tester.update(symbol, last_price, last_time)
                    for t in closed:
                        closed_count += 1
                        self._emit("trade_closed", trade=t)

                # Process new signals
                for sig in signals:
                    sig_count += 1
                    self._emit("signal", symbol=symbol, signal=sig)

                    # Open virtual trade if not already in one for this symbol+side
                    side = "LONG" if sig["type"] == "BREAKOUT" else "SHORT"
                    if not self.forward_tester.has_open_trade(symbol, side):
                        contract_value = self.contract_info.get(symbol, {}).get("contract_value", 1.0)
                        trade = self.forward_tester.open_trade(symbol, sig, contract_value=contract_value)
                        if trade:
                            self._emit("trade_opened", trade=trade)

                    # Send Telegram alert with cooldown — enrich with sizing + strategy context
                    if self.send_telegram and not self._is_on_cooldown(symbol, sig["type"]):
                        from config import LEVERAGE, FIXED_MARGIN_PER_TRADE
                        params = get_params(symbol)
                        enriched = dict(sig)
                        enriched.setdefault("strategy", params.get("strategy", "donchian").title())
                        enriched.setdefault("timeframe", params.get("timeframe", ""))
                        enriched.setdefault("leverage",  LEVERAGE)
                        enriched.setdefault("margin_usd",   FIXED_MARGIN_PER_TRADE)
                        enriched.setdefault("notional_usd", FIXED_MARGIN_PER_TRADE * LEVERAGE)
                        enriched.setdefault("currency", "$")
                        if send_alert(symbol, enriched):
                            self._mark_alerted(symbol, sig["type"])
                            alert_count += 1
                            self._emit("alert_sent", symbol=symbol, signal=sig)

            except Exception as e:
                logger.error("Error scanning %s: %s", symbol, e)
                self._emit("error", symbol=symbol, error=str(e))

        return sig_count, alert_count, closed_count

    # ----------------------------------------------------------- main loop
    def run(self) -> None:
        logger.info("BotEngine starting…")
        contracts = get_perpetual_contracts()
        if not contracts:
            self._emit("fatal", error="Could not fetch perpetual contracts")
            self._running = False
            return

        self.symbols = [c["symbol"] for c in contracts if c.get("symbol")]
        for c in contracts:
            sym = c.get("symbol")
            if not sym:
                continue
            ua = c.get("underlying_asset")
            try:
                cv = float(c.get("contract_value") or 1.0)
            except (TypeError, ValueError):
                cv = 1.0
            self.contract_info[sym] = {
                "description":    c.get("description", ""),
                "underlying":     (ua.get("symbol") if isinstance(ua, dict) else None) or sym.replace("USDT", "").replace("USD", ""),
                "contract_value": cv,
            }
        # Summarize per-symbol timeframe routing on startup
        tf_breakdown: dict[str, int] = {}
        for s in self.symbols:
            tf_breakdown[get_params(s)["timeframe"]] = tf_breakdown.get(get_params(s)["timeframe"], 0) + 1
        tf_summary = ", ".join(f"{n}×{tf}" for tf, n in sorted(tf_breakdown.items()))
        logger.info("[%s strategy] Monitoring %d perpetual contracts (%s).",
                    STRATEGY_NAME, len(self.symbols), tf_summary)
        self._emit("startup", symbols=list(self.symbols),
                   contract_info=dict(self.contract_info),
                   strategy=STRATEGY_NAME, tf_breakdown=tf_breakdown)

        if self.send_telegram:
            send_startup_message(len(self.symbols))

        while self._running:
            self.scan_count += 1
            start = time.time()
            self._emit("scan_start", n=self.scan_count,
                       at=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
            logger.info("=== Scan #%d ===", self.scan_count)

            found, sent, closed = self._scan_cycle()
            elapsed = time.time() - start

            stats = self.forward_tester.get_stats()
            self._emit("scan_complete", n=self.scan_count, duration=round(elapsed, 1),
                       signals=found, alerts=sent, trades_closed=closed, stats=stats)
            logger.info(
                "Scan #%d done in %.1fs | signals=%d alerts=%d closed=%d | open=%d wins=%d losses=%d",
                self.scan_count, elapsed, found, sent, closed,
                stats["open"], stats["wins"], stats["losses"],
            )

            # Refresh symbols every 10 scans
            if self.scan_count % 10 == 0:
                fresh = get_perpetual_contracts()
                if fresh:
                    self.symbols = [c["symbol"] for c in fresh if c.get("symbol")]

            # Sleep until next scan, but check `_running` every second so stop is responsive
            sleep_until = time.time() + max(0, SCAN_INTERVAL - elapsed)
            while self._running and time.time() < sleep_until:
                time.sleep(1)

        logger.info("BotEngine stopped.")
        self._emit("stopped")


# ============================================================ CLI mode
def _cli_main():
    engine = BotEngine(on_event=None, send_telegram=True)

    def shutdown(*_):
        logger.info("Shutdown signal received.")
        engine.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        engine._running = True
        engine.run()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    _cli_main()
