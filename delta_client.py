import hashlib
import hmac
import requests
import time
import logging
from config import (
    DELTA_BASE_URL, REQUEST_DELAY, CANDLE_LIMIT,
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_REGION,
    MAX_SYMBOLS, MIN_LISTING_AGE_DAYS, FORCE_INCLUDE_SYMBOLS, SKIP_SYMBOLS,
)

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "Content-Type": "application/json",
    "User-Agent":   "deltabot-python/1.0",
})

logger.info("Delta region: %s  (%s)", DELTA_REGION, DELTA_BASE_URL)


# ─── Authenticated request signing (HMAC-SHA256) ───────────────────────────
# Delta signs: method + timestamp + path + query_string + body
def _sign(method: str, path: str, query: str = "", body: str = "") -> tuple[str, str]:
    """Returns (signature, timestamp) for a Delta authenticated request."""
    ts = str(int(time.time()))
    payload = method + ts + path + (("?" + query) if query else "") + body
    sig = hmac.new(
        DELTA_API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return sig, ts


def _auth_headers(method: str, path: str, query: str = "", body: str = "") -> dict:
    if not DELTA_API_KEY or not DELTA_API_SECRET:
        return {}
    sig, ts = _sign(method, path, query, body)
    return {
        "api-key":   DELTA_API_KEY,
        "timestamp": ts,
        "signature": sig,
    }


def _auth_get(endpoint: str, params: dict = None, retries: int = 3) -> dict | None:
    """GET to a Delta authenticated endpoint (e.g. /v2/wallet/balances)."""
    if not DELTA_API_KEY:
        logger.warning("Auth GET %s requested but DELTA_API_KEY not set", endpoint)
        return None
    qs = ""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    headers = _auth_headers("GET", endpoint, qs)
    url = f"{DELTA_BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") is False:
                logger.warning("Auth API error on %s: %s", endpoint, data.get("error"))
                return None
            return data
        except requests.exceptions.RequestException as e:
            logger.warning("Auth request failed (%s/%s) for %s: %s",
                           attempt + 1, retries, endpoint, e)
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
    return None


def get_ticker(symbol: str) -> dict | None:
    """Public ticker — funding rate, open interest, 24h volume, mark price."""
    data = _get(f"/v2/tickers/{symbol}")
    if not data: return None
    return data.get("result")


def get_wallet_balances() -> list[dict] | None:
    """Authenticated. Returns list of wallet balances. None if no creds."""
    data = _auth_get("/v2/wallet/balances")
    return data.get("result") if data else None


def auth_available() -> bool:
    return bool(DELTA_API_KEY and DELTA_API_SECRET)

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
    """Return active perpetual futures, filtered to top-N liquid + old-enough contracts.

    Filters applied in order:
      1. State must be "live", contract type must be perpetual.
      2. Exclude tokenized US equities (they return no candle data).
      3. Skip listings younger than MIN_LISTING_AGE_DAYS (whippy / unproven).
      4. Sort by 24h turnover (USD) desc, take top MAX_SYMBOLS.
    """
    from datetime import datetime, timezone
    data = _get("/v2/products")
    if not data:
        return []
    products = data.get("result", [])
    # Tokenized US equities (perpetuals on a stock) — Delta lists them as XYZXUSDT
    # or XYZXUSD. Their /history/candles endpoint returns no data, so skip.
    _TOK_STEMS = ["AAPLX", "TSLAX", "NVDAX", "GOOGLX", "AMZNX", "METAX",
                  "MSFTX", "NFLX", "COINX", "MSTRX", "SPYX", "QQQX",
                  "AAPL", "TSLA", "NVDA", "GOOGL", "AMZN", "META",
                  "MSFT", "COIN", "MSTR", "SPY", "QQQ", "CRCLX"]
    TOKENIZED_STOCKS = set()
    for stem in _TOK_STEMS:
        TOKENIZED_STOCKS.add(stem + "USDT")
        TOKENIZED_STOCKS.add(stem + "USD")

    def _launch_ts(p: dict) -> float | None:
        """Best-effort parse of the product's listing date → UTC unix seconds."""
        for k in ("launch_time", "listed_at", "created_at"):
            v = p.get(k)
            if not v: continue
            if isinstance(v, (int, float)):
                # Sometimes ms, sometimes s
                return float(v) / (1000.0 if v > 1e12 else 1.0)
            if isinstance(v, str):
                try:
                    s = v.replace("Z", "+00:00")
                    return datetime.fromisoformat(s).timestamp()
                except Exception:
                    continue
        return None

    def _turnover(p: dict) -> float:
        for k in ("turnover_usd", "turnover", "volume"):
            try:
                v = float(p.get(k) or 0)
                if v > 0: return v
            except (TypeError, ValueError):
                continue
        return 0.0

    now_ts = time.time()
    cutoff_age = MIN_LISTING_AGE_DAYS * 86400
    skip_set = set(SKIP_SYMBOLS or [])
    eligible = []
    skipped_young = 0
    skipped_black = 0
    for p in products:
        if p.get("contract_type") != "perpetual_futures": continue
        if p.get("state") != "live": continue
        sym = p.get("symbol")
        if not sym or sym in TOKENIZED_STOCKS: continue
        if sym in skip_set:
            skipped_black += 1
            continue

        launch = _launch_ts(p)
        if MIN_LISTING_AGE_DAYS > 0 and launch is not None:
            if (now_ts - launch) < cutoff_age:
                skipped_young += 1
                continue
        eligible.append(p)

    eligible.sort(key=_turnover, reverse=True)
    top = eligible[:MAX_SYMBOLS] if MAX_SYMBOLS > 0 else eligible
    top_syms = {p["symbol"] for p in top}

    # Force-include majors (their turnover_usd is None on Delta India, so they'd
    # otherwise drop to the bottom of the ranking even though they're the most liquid).
    # Blacklisted symbols are NOT force-included.
    forced = []
    for sym in FORCE_INCLUDE_SYMBOLS:
        if sym in top_syms or sym in skip_set: continue
        match = next((p for p in products
                      if p.get("symbol") == sym
                      and p.get("contract_type") == "perpetual_futures"
                      and p.get("state") == "live"), None)
        if match:
            forced.append(match)
            top_syms.add(sym)
    final = forced + top  # majors first, then volume-ranked tail

    logger.info(
        "Symbol filter: %d products → %d eligible (skipped %d young, %d blacklisted) → top %d by turnover + %d forced majors = %d total",
        len(products), len(eligible), skipped_young, skipped_black, len(top), len(forced), len(final),
    )
    return final


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
