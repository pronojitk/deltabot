"""Telegram alert formatter — clean, level-rich trade notifications."""
import logging
import requests
from datetime import datetime, timezone

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ────────────────────────────────────────────────────────── helpers
def _fmt_price(v) -> str:
    """Compact, lossless-ish price formatter."""
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v == 0:
        return "0"
    av = abs(v)
    if av >= 1000:    return f"{v:,.2f}"
    if av >= 1:       return f"{v:.4f}".rstrip("0").rstrip(".")
    if av >= 0.01:    return f"{v:.5f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _pct_from(a, b) -> str:
    """% change b → a (signed)."""
    if not a or not b:
        return "—"
    try:
        p = (float(a) - float(b)) / float(b) * 100.0
        sign = "+" if p >= 0 else ""
        return f"{sign}{p:.2f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"


def _post(payload: dict) -> bool:
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram rejected: %s", data.get("description", data))
            return False
        return True
    except requests.exceptions.RequestException as e:
        logger.error("Telegram send failed: %s", e)
        return False


# ────────────────────────────────────────────────────────── alert
def send_alert(symbol: str, signal: dict) -> bool:
    """
    Format and send a trade-entry alert.

    Required fields in `signal`:
      type           "BREAKOUT" | "BREAKDOWN"
      close / entry  entry price
      time           unix-ts

    Optional (improves message quality):
      side, sl, tp, level_label, level_price, tests,
      ema7, ema21, entropy, regime, atr,
      strategy, timeframe, use_trailing, trail_distance,
      margin_usd, notional_usd, leverage, currency,
      orb_high, orb_low, rr
    """
    sig_type = signal.get("type", "BREAKOUT")
    side = signal.get("side") or ("LONG" if sig_type == "BREAKOUT" else "SHORT")
    entry = signal.get("entry") or signal.get("close")
    sl    = signal.get("sl")
    tp    = signal.get("tp")
    strat = signal.get("strategy", "Donchian")
    tf    = signal.get("timeframe", "")
    currency = signal.get("currency", "$")
    ts    = signal.get("time")
    dt    = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""

    # Direction visuals
    if side == "LONG":
        arrow, side_emoji, head_emoji = "🟢", "▲", "🚀"
    else:
        arrow, side_emoji, head_emoji = "🔴", "▼", "🔻"

    # Compute R:R if both SL and TP given
    rr = signal.get("rr")
    if rr is None and sl is not None and tp is not None and entry:
        try:
            risk    = abs(float(entry) - float(sl))
            reward  = abs(float(tp)    - float(entry))
            rr = (reward / risk) if risk else None
        except (TypeError, ValueError, ZeroDivisionError):
            rr = None

    sl_pct = _pct_from(sl, entry) if sl is not None else None
    tp_pct = _pct_from(tp, entry) if tp is not None else None
    use_trailing = bool(signal.get("use_trailing"))
    trail_dist = signal.get("trail_distance")

    # Per-trade $/₹ risk and reward (need notional)
    notional = signal.get("notional_usd") or signal.get("notional_inr") or 0
    sl_amount = tp_amount = None
    if notional and entry:
        try:
            entry_f = float(entry)
            if sl is not None:
                move = abs(float(sl) - entry_f) / entry_f
                sl_amount = -move * float(notional)        # always negative (loss)
            if tp is not None and not use_trailing:
                move = abs(float(tp) - entry_f) / entry_f
                tp_amount = move * float(notional)         # always positive (gain)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _amt(v):
        if v is None: return ""
        sign = "+" if v > 0 else ""
        return f"{sign}{currency}{v:,.2f}"

    # ── Build message ─────────────────────────────────────────
    lines = []
    lines.append(f"{head_emoji} *{side} ENTRY* {side_emoji} `{symbol}`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📍 *Entry* `{_fmt_price(entry)}`")

    if sl is not None:
        sl_amt = f" · {_amt(sl_amount)}" if sl_amount is not None else ""
        lines.append(f"🛑 *Stop Loss* `{_fmt_price(sl)}`   _({sl_pct}{sl_amt})_")
    if tp is not None and not use_trailing:
        tp_amt = f" · {_amt(tp_amount)}" if tp_amount is not None else ""
        lines.append(f"🎯 *Target* `{_fmt_price(tp)}`   _({tp_pct}{tp_amt})_")
    elif use_trailing:
        atr = signal.get("atr")
        mult = None
        if atr and trail_dist:
            try: mult = float(trail_dist) / float(atr)
            except Exception: mult = None
        trail_note = f"ATR × {mult:.1f}" if mult else (f"distance {_fmt_price(trail_dist)}" if trail_dist else "ATR-based")
        lines.append(f"🎯 *Target* trailing stop ({trail_note})")
    if rr is not None:
        lines.append(f"⚖️ *R:R* 1:{rr:.2f}")
    if dt:
        lines.append(f"🕒 _{dt}_")

    msg = "\n".join(lines)
    ok = _post({
        "chat_id":               TELEGRAM_CHAT_ID,
        "text":                  msg,
        "parse_mode":            "Markdown",
        "disable_web_page_preview": True,
    })
    if ok:
        logger.info("Alert sent for %s %s", symbol, side)
    return ok


# ────────────────────────────────────────────────────────── close
def send_close(symbol: str, trade: dict) -> bool:
    """Optional: notify when a trade closes."""
    side = trade.get("side", "?")
    reason = trade.get("exit_reason") or trade.get("status", "—")
    pnl_usd  = trade.get("pnl_usd")  or trade.get("pnl_inr")
    pnl_pct  = trade.get("pnl_pct")
    entry    = trade.get("entry_price") or trade.get("entry")
    exit_p   = trade.get("exit_price") or trade.get("exit")
    currency = trade.get("currency", "$")

    win = (pnl_usd or 0) > 0
    head = "✅" if win else "❌" if (pnl_usd or 0) < 0 else "⚪"
    badge = {"WIN": "🏆 WIN", "LOSS": "💀 LOSS",
             "TIMEOUT": "⏰ TIMEOUT", "STOP_LOSS": "💀 STOP",
             "TAKE_PROFIT": "🏆 TARGET", "EOD": "⏰ EOD"}.get(reason, reason)

    lines = [
        f"{head} *TRADE CLOSED* `{symbol}` _{side}_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"Result: *{badge}*",
        f"Entry → Exit: `{_fmt_price(entry)}` → `{_fmt_price(exit_p)}`",
    ]
    if pnl_usd is not None:
        sign = "+" if pnl_usd >= 0 else ""
        lines.append(f"💵 P&L: *{sign}{currency}{pnl_usd:,.2f}*"
                     + (f"  ({sign}{pnl_pct:.2f}% on margin)" if pnl_pct is not None else ""))
    return _post({
        "chat_id":     TELEGRAM_CHAT_ID,
        "text":        "\n".join(lines),
        "parse_mode":  "Markdown",
        "disable_web_page_preview": True,
    })


# ────────────────────────────────────────────────────────── startup
def send_startup_message(symbol_count: int) -> None:
    """Notify that the bot has started."""
    msg = (
        f"🤖 *Delta Bot online*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Monitoring *{symbol_count}* perpetual contracts\n"
        f"⏱ Multi-strategy: Donchian (15m/1h) + Gold (PAXG) + MCX-ORB (NSE)\n"
        f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram startup message failed: %s",
                           data.get("description", data))
    except requests.exceptions.RequestException as e:
        logger.warning("Could not send startup message: %s", e)
