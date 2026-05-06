import requests
import logging
from datetime import datetime, timezone
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_alert(symbol: str, signal: dict) -> bool:
    """Format and send a breakout/breakdown alert to Telegram."""
    signal_type = signal["type"]
    level_label = signal["level_label"]
    level_price = signal["level_price"]
    tests = signal["tests"]
    close = signal["close"]
    ema7 = signal["ema7"]
    ema21 = signal["ema21"]
    ts = signal["time"]

    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    emoji = "🚀" if signal_type == "BREAKOUT" else "🔻"
    direction = "above" if signal_type == "BREAKOUT" else "below"

    msg = (
        f"{emoji} *{signal_type}* — `{symbol}`\n"
        f"────────────────────\n"
        f"📍 Level: *{level_label}* @ `{level_price:.6g}`\n"
        f"🔁 Tests before break: *{tests}*\n"
        f"💰 Close: `{close:.6g}` ({direction} level)\n"
        f"📈 EMA7: `{ema7:.6g}` | EMA21: `{ema21:.6g}`\n"
        f"⏰ Time: {dt}\n"
        f"⏱ Timeframe: 15m"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram rejected alert: %s", data.get("description", data))
            return False
        logger.info("Alert sent for %s %s", symbol, signal_type)
        return True
    except requests.exceptions.RequestException as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False


def send_startup_message(symbol_count: int) -> None:
    """Notify that the bot has started."""
    msg = (
        f"✅ *Delta Exchange Alert Bot started*\n"
        f"Monitoring *{symbol_count}* perpetual contracts\n"
        f"Timeframe: 15m | EMA7/21 + Pivot Levels"
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram startup message failed: %s", data.get("description", data))
    except requests.exceptions.RequestException as e:
        logger.warning("Could not send startup message: %s", e)
