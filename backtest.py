"""
Backtest the Delta Bot signal/forward-test logic on historical candles.

Replays bar-by-bar using the exact same detect_signals() and ForwardTester
classes the live bot uses, so results are directly comparable.

Usage:
    py backtest.py                       # all perpetuals, ~21 days of 15m
    py backtest.py --symbols BTCUSDT,ETHUSDT
    py backtest.py --bars 4000           # ~42 days (limited by Delta API window)
    py backtest.py --top 20              # only the 20 highest-volume contracts
"""

import argparse
import time
import sys
from pathlib import Path

from delta_client import get_perpetual_contracts, get_ohlcv, SESSION, _RES_SECONDS
from indicators import detect_signals, detect_signals_for_symbol
from forward_test import ForwardTester
from config import (
    TIMEFRAME, CANDLE_LIMIT, MAX_HOLD_BARS, LEVERAGE, RISK_PER_TRADE_PCT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, STARTING_BALANCE, ACCOUNT_SYMBOL,
    DELTA_BASE_URL, REQUEST_DELAY, ATR_INITIAL_SL_MULT,
    BARS_PER_DAY, get_params, STRATEGY_NAME,
)


# A huge working balance so the balance-cap never blocks signals during a
# full-year run. P&L is reported relative to STARTING_BALANCE, not this buffer.
BACKTEST_BUFFER = 100_000_000.0   # ₹10 crore "test capital"


class BacktestTester(ForwardTester):
    """Tester variant for backtests: truly fixed sizing, no balance cap.
    Sizing scales with actual stop distance so ATR-based stops keep ₹ risk constant."""
    def _size_position(self, stop_distance_pct: float | None = None):
        risk_usd = STARTING_BALANCE * (RISK_PER_TRADE_PCT / 100.0)
        sl_pct = stop_distance_pct if (stop_distance_pct and stop_distance_pct > 0) \
                                   else STOP_LOSS_PCT
        notional = risk_usd / sl_pct
        margin   = notional / LEVERAGE
        return round(notional, 2), round(margin, 2), round(risk_usd, 2)


# --------------------------------------------------------------------- helpers
CHUNK_BARS = 1500   # Delta /v2/history/candles caps a single response at ~2000

def _fetch_chunk(symbol: str, resolution: str, start: int, end: int) -> list[dict]:
    """Single API call for a candle window."""
    try:
        r = SESSION.get(
            f"{DELTA_BASE_URL}/v2/history/candles",
            params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    if not data or data.get("success") is False:
        return []
    out = []
    for it in data.get("result", []):
        try:
            out.append({
                "time":  int(it["time"]),
                "open":  float(it["open"]),
                "high":  float(it["high"]),
                "low":   float(it["low"]),
                "close": float(it["close"]),
                "volume": float(it["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_history(symbol: str, bars: int, timeframe: str = TIMEFRAME) -> list[dict]:
    """
    Pull `bars` of `timeframe` candles for one symbol. Paginates if needed.
    Returns oldest-first deduped list.
    """
    if bars <= CHUNK_BARS:
        return get_ohlcv(symbol, timeframe, bars)

    candle_seconds = _RES_SECONDS.get(timeframe, 900)
    end = int(time.time())
    remaining = bars
    all_candles: list[dict] = []
    while remaining > 0:
        n = min(CHUNK_BARS, remaining)
        start = end - n * candle_seconds
        chunk = _fetch_chunk(symbol, timeframe, start, end)
        time.sleep(REQUEST_DELAY)
        if not chunk:
            break
        all_candles.extend(chunk)
        end = min(c["time"] for c in chunk) - 1
        remaining -= n
    seen = set(); unique = []
    for c in sorted(all_candles, key=lambda x: x["time"]):
        if c["time"] in seen:
            continue
        seen.add(c["time"]); unique.append(c)
    return unique


def fetch_daily(symbol: str, days: int = 30) -> list[dict]:
    if days <= CHUNK_BARS:
        return get_ohlcv(symbol, "1d", days)
    end = int(time.time())
    remaining = days
    out: list[dict] = []
    while remaining > 0:
        n = min(CHUNK_BARS, remaining)
        start = end - n * 86400
        chunk = _fetch_chunk(symbol, "1d", start, end)
        time.sleep(REQUEST_DELAY)
        if not chunk:
            break
        out.extend(chunk)
        end = min(c["time"] for c in chunk) - 1
        remaining -= n
    seen = set(); unique = []
    for c in sorted(out, key=lambda x: x["time"]):
        if c["time"] in seen:
            continue
        seen.add(c["time"]); unique.append(c)
    return unique


def daily_window_for(daily: list[dict], bar_time: int) -> list[dict]:
    """Slice of daily candles strictly up to (but not including) the current bar's day."""
    return [d for d in daily if d["time"] <= bar_time]


def check_intrabar_exit(trade: dict, bar: dict) -> tuple[str | None, float | None]:
    """
    Check if SL/TP was hit during this bar using high/low.
    Conservative: if both could fire, assume the SL fired first.
    For trailing-stop trades, an SL hit above entry counts as a WIN
    (the stop ratcheted into profit before reversing).
    """
    sl, tp, side, entry = trade["sl"], trade["tp"], trade["side"], trade["entry_price"]
    hi, lo = bar["high"], bar["low"]
    if side == "LONG":
        sl_hit = lo <= sl
        tp_hit = hi >= tp
        if sl_hit and tp_hit:
            status = "WIN" if sl > entry else "LOSS"
            return status, sl
        if sl_hit:
            status = "WIN" if sl > entry else "LOSS"
            return status, sl
        if tp_hit: return "WIN", tp
    else:
        sl_hit = hi >= sl
        tp_hit = lo <= tp
        if sl_hit and tp_hit:
            status = "WIN" if sl < entry else "LOSS"
            return status, sl
        if sl_hit:
            status = "WIN" if sl < entry else "LOSS"
            return status, sl
        if tp_hit: return "WIN", tp
    return None, None


# --------------------------------------------------------------------- engine
def backtest_symbol(symbol: str, candles: list[dict], daily: list[dict],
                    tester: ForwardTester, contract_value: float,
                    params: dict | None = None,
                    htf_candles: list[dict] | None = None,
                    warmup: int = 100) -> int:
    """Walk forward through candles. Returns number of signals fired.
    `htf_candles` is required for multi-timeframe strategies (e.g. Gold)."""
    if len(candles) <= warmup:
        return 0

    if params is None:
        params = get_params(symbol)
    max_hold = params["max_hold_bars"]
    htf_candles = htf_candles or []

    sig_count = 0
    for i in range(warmup, len(candles)):
        window = candles[max(0, i - CANDLE_LIMIT + 1): i + 1]
        bar    = candles[i]

        # 1) Update open trades using THIS bar's high/low (intrabar fills)
        for t in list(tester.trades):
            if t["symbol"] != symbol or t["status"] != "OPEN":
                continue
            t["bars_held"] += 1

            if t.get("use_trailing") and t.get("trail_distance", 0) > 0:
                if t["side"] == "LONG" and bar["high"] > t["high_water"]:
                    t["high_water"] = bar["high"]
                    new_sl = bar["high"] - t["trail_distance"]
                    if new_sl > t["sl"]:
                        t["sl"] = round(new_sl, 8)
                elif t["side"] == "SHORT" and bar["low"] < t["high_water"]:
                    t["high_water"] = bar["low"]
                    new_sl = bar["low"] + t["trail_distance"]
                    if new_sl < t["sl"]:
                        t["sl"] = round(new_sl, 8)

            status, fill = check_intrabar_exit(t, bar)
            if status:
                tester._close_trade(t, status, fill, bar["time"])
            elif t["bars_held"] >= t.get("max_hold_bars", max_hold):
                tester._close_trade(t, "TIMEOUT", bar["close"], bar["time"])

        # 2) Detect new signals using per-symbol params and dispatcher
        if htf_candles:
            # Slice HTF candles up to (and including) any whose close-time ≤ current bar
            htf_window = [h for h in htf_candles if h["time"] <= bar["time"]]
            signals = detect_signals_for_symbol(window, htf_window, params)
        else:
            signals = detect_signals_for_symbol(window, None, params)

        for sig in signals:
            sig_count += 1
            side = "LONG" if sig["type"] == "BREAKOUT" else "SHORT"
            if not tester.has_open_trade(symbol, side):
                tester.open_trade(symbol, sig, contract_value=contract_value)

    # 3) Snapshot final equity for this symbol's run
    if candles:
        tester._snapshot_equity(candles[-1]["time"])

    return sig_count


# --------------------------------------------------------------------- runner
def run(symbols: list[str], days: int, contract_info: dict[str, dict],
        daily_days: int = 30) -> None:
    # Use a separate state file so we don't clobber ft_state.json
    bt_file = Path(__file__).parent / "bt_state.json"
    if bt_file.exists():
        bt_file.unlink()

    tester = BacktestTester(file=bt_file)
    tester.balance = BACKTEST_BUFFER

    print(f"\n{STRATEGY_NAME.upper()} STRATEGY — backtesting {len(symbols)} symbols × {days} days")
    print(f"  Per-symbol timeframe routing: BTC/ETH/XRP→4h, SOL/DOGE/PAXG→1h")
    print(f"  Starting balance: {ACCOUNT_SYMBOL}{STARTING_BALANCE:,.0f}  "
          f"(Donchian breakout + EMA trend + ATR trail + entropy)\n")

    per_symbol_stats = []
    total_signals = 0

    for idx, sym in enumerate(symbols, 1):
        params = get_params(sym)
        tf = params["timeframe"]
        bars = days * BARS_PER_DAY.get(tf, 24)
        strategy = params.get("strategy", "donchian")
        label = (f"Gold,{tf}+{params['htf_timeframe']}" if strategy == "gold"
                 else f"{tf:<3}, Don-{params['donchian_period']}")
        sys.stdout.write(f"  [{idx:>3}/{len(symbols)}] {sym:<12} ({label}) fetching…")
        sys.stdout.flush()
        candles = fetch_history(sym, bars, timeframe=tf)
        if not candles or len(candles) < 110:
            print(" — skipped (no data)")
            continue

        # Gold strategy needs higher-timeframe candles (1h) for bias
        htf_candles: list[dict] = []
        if strategy == "gold":
            htf_tf  = params["htf_timeframe"]
            htf_bars = days * BARS_PER_DAY.get(htf_tf, 24)
            htf_candles = fetch_history(sym, htf_bars, timeframe=htf_tf) or []
            print(f" got {len(candles)} {tf} + {len(htf_candles)} {htf_tf} bars…", end="")
        else:
            print(f" got {len(candles)} bars…", end="")
        sys.stdout.flush()

        daily: list[dict] = []
        opens_before = sum(1 for t in tester.trades if t["status"] != "OPEN")
        bal_before  = tester.balance
        cv = contract_info.get(sym, {}).get("contract_value", 1.0)

        sigs = backtest_symbol(sym, candles, daily, tester,
                               contract_value=cv, params=params,
                               htf_candles=htf_candles)
        total_signals += sigs

        closed_now = [t for t in tester.trades
                      if t["status"] != "OPEN"
                      and t["symbol"] == sym]
        wins   = sum(1 for t in closed_now if t["status"] == "WIN")
        losses = sum(1 for t in closed_now if t["status"] == "LOSS")
        timeouts = sum(1 for t in closed_now if t["status"] == "TIMEOUT")
        pnl = sum(t["pnl_usd"] for t in closed_now if t["pnl_usd"] is not None)

        per_symbol_stats.append({
            "symbol": sym, "signals": sigs, "trades": len(closed_now),
            "wins": wins, "losses": losses, "timeouts": timeouts, "pnl": pnl,
        })
        print(f" {sigs} signals  {len(closed_now)} trades  "
              f"W{wins}/L{losses}/T{timeouts}  P&L {ACCOUNT_SYMBOL}{pnl:+,.2f}")

    # ============================================================ Summary
    stats = tester.get_stats()
    print("\n" + "=" * 78)
    print("BACKTEST SUMMARY".center(78))
    print("=" * 78)
    print(f"  Symbols tested      : {len(per_symbol_stats)}")
    print(f"  Total signals fired : {total_signals}")
    print(f"  Total trades        : {stats['total_trades']}  "
          f"(open={stats['open']} won={stats['wins']} lost={stats['losses']} timeout={stats['timeouts']})")
    print(f"  Win rate            : {stats['win_rate']}%")
    print(f"  Avg win / loss      : {ACCOUNT_SYMBOL}{stats['avg_win']:+,.2f} / {ACCOUNT_SYMBOL}{stats['avg_loss']:+,.2f}")
    print(f"  Profit factor       : {stats['profit_factor']}")
    print(f"  Realized P&L        : {ACCOUNT_SYMBOL}{stats['realized_pnl']:+,.2f}")
    # Report relative to the *real* starting balance, ignoring the test buffer
    real_final = STARTING_BALANCE + stats['realized_pnl']
    real_pct   = stats['realized_pnl'] / STARTING_BALANCE * 100
    total_fees = sum(t.get('fees_usd', 0) for t in tester.trades if t.get('fees_usd'))
    print(f"  Total fees paid     : {ACCOUNT_SYMBOL}{total_fees:+,.2f}")
    print(f"  Final balance       : {ACCOUNT_SYMBOL}{real_final:,.2f}  "
          f"({real_pct:+.2f}% on {ACCOUNT_SYMBOL}{STARTING_BALANCE:,.0f})")
    print("=" * 78)

    # Per-symbol leaderboard
    per_symbol_stats.sort(key=lambda r: -r["pnl"])
    print("\nTOP 10 SYMBOLS BY P&L:")
    print(f"  {'Symbol':<14} {'Sigs':>5} {'Trades':>7} {'W':>4} {'L':>4} {'T':>4} {'P&L':>14}")
    for r in per_symbol_stats[:10]:
        print(f"  {r['symbol']:<14} {r['signals']:>5} {r['trades']:>7} "
              f"{r['wins']:>4} {r['losses']:>4} {r['timeouts']:>4} "
              f"{ACCOUNT_SYMBOL}{r['pnl']:>+13,.2f}")
    if len(per_symbol_stats) > 10:
        print("\nWORST 5 SYMBOLS BY P&L:")
        for r in per_symbol_stats[-5:]:
            print(f"  {r['symbol']:<14} {r['signals']:>5} {r['trades']:>7} "
                  f"{r['wins']:>4} {r['losses']:>4} {r['timeouts']:>4} "
                  f"{ACCOUNT_SYMBOL}{r['pnl']:>+13,.2f}")
    print(f"\nDetailed trade log saved to: {bt_file}")


# --------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated list (default: all perpetuals)")
    ap.add_argument("--bars", type=int, default=2000,
                    help="number of 15m candles per symbol (default 2000 ≈ 21 days)")
    ap.add_argument("--days", type=int, default=0,
                    help="convenience: backtest N days of 15m data (overrides --bars)")
    ap.add_argument("--top", type=int, default=0,
                    help="limit to N highest-volume contracts (default: all)")
    args = ap.parse_args()

    print("Fetching contract list…")
    contracts = get_perpetual_contracts()
    if not contracts:
        print("FAILED: could not fetch contracts"); return

    # Build symbol → contract_value map
    info = {}
    for c in contracts:
        sym = c.get("symbol")
        if not sym: continue
        try:
            cv = float(c.get("contract_value") or 1.0)
        except (TypeError, ValueError):
            cv = 1.0
        info[sym] = {"contract_value": cv}

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = [c["symbol"] for c in contracts if c.get("symbol")]
        if args.top:
            # Sort by 24h notional volume if available
            ranked = sorted(contracts,
                            key=lambda c: float(c.get("turnover_usd") or c.get("volume") or 0),
                            reverse=True)
            symbols = [c["symbol"] for c in ranked[:args.top] if c.get("symbol")]

    # Each symbol now uses its own timeframe (per-symbol routing). The runner
    # converts days → bars per symbol internally based on its config.
    if args.days > 0:
        days = args.days
    else:
        # Backward compat with --bars: assume 1h timeframe for the conversion
        days = max(1, args.bars // 24)
    daily_days = days + 30

    t0 = time.time()
    run(symbols, days, info, daily_days=daily_days)
    print(f"\nFinished in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
