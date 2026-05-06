import requests
import time
import logging
from config import DELTA_BASE_URL, REQUEST_DELAY, CANDLE_LIMIT

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})

# Resolution string -> seconds per candle
_RES_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}


def _get(endpoint: str, params: dict = None, retries: int = 3) -> dict | None:
    url = f"{DELTA_BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") is False:
                logger.warning("API error on %s: %s", endpoint, data.get("error"))
                return None
            return data
        except requests.exceptions.RequestException as e:
            logger.warning("Request failed (%s/%s) for %s: %s", attempt + 1, retries, endpoint, e)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    return None


def get_perpetual_contracts() -> list[dict]:
    """Return all active perpetual futures contracts."""
    data = _get("/v2/products")
    if not data:
        return []
    products = data.get("result", [])
    # Delta lists tokenized US equities as perpetuals (AAPLX, TSLAX, NVDAX, ...)
    # but their /history/candles endpoint returns empty data. Skip them.
    TOKENIZED_STOCKS = {
        "AAPLXUSDT", "TSLAXUSDT", "NVDAXUSDT", "GOOGLXUSDT",
        "AMZNXUSDT", "METAXUSDT", "MSFTXUSDT", "NFLXUSDT",
        "COINXUSDT", "MSTRXUSDT", "SPYXUSDT", "QQQXUSDT",
    }
    return [
        p for p in products
        if p.get("contract_type") == "perpetual_futures"
        and p.get("state") == "live"
        and p.get("symbol") not in TOKENIZED_STOCKS
    ]


def get_daily_candles(symbol: str, limit: int = 3) -> list[dict]:
    """Fetch the last `limit` daily candles. Used for pivot level calculation."""
    return get_ohlcv(symbol, "1d", limit)


def get_ohlcv(symbol: str, resolution: str, limit: int = CANDLE_LIMIT) -> list[dict]:
    """
    Fetch the most recent `limit` OHLCV candles for a symbol.
    Delta requires explicit start/end timestamps and a string resolution.
    Returns list of dicts sorted oldest-first: {time, open, high, low, close, volume}
    """
    candle_seconds = _RES_SECONDS.get(resolution, 900)
    end = int(time.time())
    start = end - limit * candle_seconds

    data = _get(
        "/v2/history/candles",
        params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
    )
    time.sleep(REQUEST_DELAY)
    if not data:
        return []

    raw = data.get("result", [])
    # API returns list of {time, open, high, low, close, volume} objects
    candles = []
    for item in raw:
        try:
            candles.append({
                "time": int(item["time"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(candles, key=lambda x: x["time"])
