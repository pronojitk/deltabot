"""
MCX ORB + Fibonacci-Pivot Strategy Backtest.

Entry (Opening Range Breakout):
  • ORB candle  : 09:15–09:20 IST (first 5-min after NSE open)
  • LONG entry  : first 5-min CLOSE > ORB high
  • SHORT entry : first 5-min CLOSE < ORB low

Exits (Fibonacci Pivot Points — computed from PREVIOUS day's H/L/C):
  PP  = (H + L + C) / 3
  R1  = PP + 0.382 × (H − L)
  R2  = PP + 0.618 × (H − L)
  R3  = PP + 1.000 × (H − L)
  S1  = PP − 0.382 × (H − L)
  S2  = PP − 0.618 × (H − L)
  S3  = PP − 1.000 × (H − L)

  • LONG  : SL = nearest pivot LEVEL BELOW entry (PP / S1 / S2 / S3)
            TP = nearest pivot LEVEL ABOVE entry (R1 / R2 / R3)
  • SHORT : SL = nearest pivot LEVEL ABOVE entry (PP / R1 / R2 / R3)
            TP = nearest pivot LEVEL BELOW entry (S1 / S2 / S3)
  • Auto-flat at 15:25 IST (EOD).
  • One trade per side per symbol per day.

Data: yfinance 5-min candles (capped at last ~60 days by Yahoo).

Run:
    py mcx_backtest.py                # default: MCX.NS, last 60 days
    py mcx_backtest.py SYM            # single symbol
    py mcx_backtest.py SYM1 SYM2 ...  # multiple
"""

import sys
import logging
from datetime import time as dtime
from collections import defaultdict

try:
    import yfinance as yf
    import pytz
except ImportError:
    raise SystemExit("Install deps:  pip install yfinance pytz")

IST = pytz.timezone("Asia/Kolkata")

ORB_OPEN      = dtime(9, 15)
ORB_CLOSE     = dtime(9, 20)
EOD_FLAT_TIME = dtime(15, 25)
# Fibonacci pivot ratios (Wilder/Tom DeMark style)
FIB_RATIOS    = (0.382, 0.618, 1.000)
# ── Filters / improvements ──
MIN_RISK_PCT  = 0.003   # entry→SL distance must be ≥ 0.3%
MAX_RISK_PCT  = 0.010   # entry→SL distance must be ≤ 1.0% (skip wide-SL trades)
TP_NTH        = 1       # nearest pivot as TP (#3 disabled — original behavior)
TREND_FILTER  = False   # #4 disabled
TRAIL_TO_BE   = False   # #5 disabled

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger("mcx-bt")


def fetch_history(symbol: str, period="60d", interval="5m"):
    """Fetch OHLC. Returns DataFrame indexed in IST.
    Yahoo limits: 1m=7d, 5m/15m/30m=60d, 1h=730d, 1d=any."""
    df = yf.download(symbol, period=period, interval=interval,
                     progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert(IST)
    return df


def fib_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Fibonacci pivot points based on previous day's H/L/C."""
    pp = (prev_high + prev_low + prev_close) / 3.0
    rng = prev_high - prev_low
    return {
        "PP": pp,
        "R1": pp + FIB_RATIOS[0] * rng,
        "R2": pp + FIB_RATIOS[1] * rng,
        "R3": pp + FIB_RATIOS[2] * rng,
        "S1": pp - FIB_RATIOS[0] * rng,
        "S2": pp - FIB_RATIOS[1] * rng,
        "S3": pp - FIB_RATIOS[2] * rng,
    }


def nth_level(pivots: dict, price: float, side: str, n: int = 1):
    """Return (label, price) of the n-th nearest pivot level on the given side
    of `price`. side='above' or 'below'. n=1 → nearest, n=2 → second-nearest."""
    if side == "above":
        cands = sorted([(k, v) for k, v in pivots.items() if v > price],
                       key=lambda kv: kv[1])  # ascending
    else:
        cands = sorted([(k, v) for k, v in pivots.items() if v < price],
                       key=lambda kv: kv[1], reverse=True)  # descending (closest first)
    if not cands or n > len(cands): return None
    return cands[n-1]


def nearest_level(pivots, price, side):
    return nth_level(pivots, price, side, 1)


def backtest_symbol(symbol: str):
    """Run ORB + Fib-pivot backtest. Returns (trades, summary_dict)."""
    df = fetch_history(symbol)
    if df is None or df.empty:
        print(f"  ⚠  No data for {symbol}")
        return [], None

    # Group bars by date
    by_day = defaultdict(list)
    for ts, row in df.iterrows():
        by_day[ts.date()].append((ts, row))

    # Pre-compute each day's H/L/C for pivot calc
    day_hlc = {}
    for d, bars in by_day.items():
        highs = [float(r["High"]) for _, r in bars]
        lows  = [float(r["Low"])  for _, r in bars]
        closes= [float(r["Close"]) for _, r in bars]
        day_hlc[d] = (max(highs), min(lows), closes[-1])

    sorted_days = sorted(by_day.keys())
    trades = []
    skipped = {"min_risk":0, "max_risk":0, "trend":0, "no_pivot":0}
    for i, day in enumerate(sorted_days):
        bars = by_day[day]
        # Need previous trading day for pivots
        if i == 0:
            continue
        prev_day = sorted_days[i - 1]
        ph, pl, pc = day_hlc[prev_day]
        pivots = fib_pivots(ph, pl, pc)
        prev_pp = pivots["PP"]
        # #4 trend filter: prev close vs prev PP
        if TREND_FILTER:
            if   pc > prev_pp: allowed_sides = ("LONG",)
            elif pc < prev_pp: allowed_sides = ("SHORT",)
            else:              allowed_sides = ("LONG","SHORT")
        else:
            allowed_sides = ("LONG","SHORT")
        # Identify ORB candle (start time == 09:15)
        orb = None
        for ts, row in bars:
            if ts.time() == ORB_OPEN:
                orb = {
                    "high": float(row["High"]),
                    "low":  float(row["Low"]),
                    "ts":   ts,
                }
                break
        if not orb:
            continue

        # Walk forward through post-ORB bars looking for breakout/breakdown
        post = [(ts, row) for ts, row in bars if ts > orb["ts"]]
        if not post:
            continue

        for kind in ("LONG", "SHORT"):
            if kind not in allowed_sides:
                skipped["trend"] += 1
                continue
            already_traded = False
            entry_ts = entry = sl = tp = None
            sl_label = tp_label = None
            interm_label = None; interm_price = None  # #5 trail trigger
            trailed = False
            risk = 0.0
            exit_ts = exit_price = exit_reason = None

            for ts, row in post:
                close = float(row["Close"])
                if not already_traded:
                    triggered = (kind == "LONG"  and close > orb["high"]) or \
                                (kind == "SHORT" and close < orb["low"])
                    if not triggered: continue
                    entry = close; entry_ts = ts
                    if kind == "LONG":
                        sl_kv = nearest_level(pivots, entry, "below")
                        tp_kv = nth_level(pivots, entry, "above", TP_NTH) \
                                or nearest_level(pivots, entry, "above")
                        interm_kv = nearest_level(pivots, entry, "above") if TP_NTH > 1 else None
                    else:
                        sl_kv = nearest_level(pivots, entry, "above")
                        tp_kv = nth_level(pivots, entry, "below", TP_NTH) \
                                or nearest_level(pivots, entry, "below")
                        interm_kv = nearest_level(pivots, entry, "below") if TP_NTH > 1 else None
                    if not sl_kv or not tp_kv:
                        skipped["no_pivot"] += 1; break
                    sl_label, sl = sl_kv
                    tp_label, tp = tp_kv
                    risk = abs(entry - sl)
                    if interm_kv: interm_label, interm_price = interm_kv
                    # #2 min-risk filter
                    if entry > 0 and (risk / entry) < MIN_RISK_PCT:
                        skipped["min_risk"] += 1
                        already_traded = False; entry = None; break
                    # max-risk filter
                    if entry > 0 and (risk / entry) > MAX_RISK_PCT:
                        skipped["max_risk"] += 1
                        already_traded = False; entry = None; break
                    already_traded = True
                else:
                    hi, lo = float(row["High"]), float(row["Low"])
                    # #5 trailing: when price first reaches intermediate level, move SL → BE
                    if TRAIL_TO_BE and not trailed and interm_price is not None:
                        if (kind == "LONG"  and hi >= interm_price) or \
                           (kind == "SHORT" and lo <= interm_price):
                            sl = entry; sl_label = "BE"; trailed = True
                    if kind == "LONG":
                        if lo <= sl: exit_price = sl; exit_reason = "SL" if not trailed else "BE"
                        elif hi >= tp: exit_price = tp; exit_reason = "TP"
                    else:
                        if hi >= sl: exit_price = sl; exit_reason = "SL" if not trailed else "BE"
                        elif lo <= tp: exit_price = tp; exit_reason = "TP"
                    if exit_price is not None:
                        exit_ts = ts
                        break
                    if ts.time() >= EOD_FLAT_TIME:
                        exit_price = close; exit_reason = "EOD"; exit_ts = ts
                        break

            if not already_traded:
                continue
            if exit_price is None:
                exit_ts, last_row = post[-1]
                exit_price = float(last_row["Close"])
                exit_reason = "EOD"

            pnl_pct = ((exit_price - entry) / entry * 100) * (1 if kind == "LONG" else -1)
            r_multiple = pnl_pct / ((risk / entry) * 100) if risk and entry else 0.0
            trades.append({
                "date": day, "symbol": symbol, "side": kind,
                "entry": entry, "exit": exit_price, "sl": sl, "tp": tp,
                "sl_label": sl_label, "tp_label": tp_label,
                "orb_high": orb["high"], "orb_low": orb["low"],
                "entry_time": entry_ts.strftime("%H:%M"),
                "exit_time":  exit_ts.strftime("%H:%M"),
                "exit_reason": exit_reason,
                "pnl_pct": pnl_pct, "r": r_multiple,
            })

    # Summary
    if not trades:
        return trades, None
    wins  = [t for t in trades if t["exit_reason"] == "TP"]
    losses = [t for t in trades if t["exit_reason"] == "SL"]
    eods  = [t for t in trades if t["exit_reason"] in ("EOD", "BE")]
    closed = wins + losses + eods
    total_pnl_pct = sum(t["pnl_pct"] for t in closed)
    total_r = sum(t["r"] for t in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    avg_win  = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    expectancy = total_pnl_pct / len(closed) if closed else 0

    return trades, {
        "symbol": symbol,
        "trades": len(closed), "wins": len(wins), "losses": len(losses),
        "eods": len(eods), "win_rate": win_rate,
        "total_pnl_pct": total_pnl_pct, "total_r": total_r,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": expectancy,
        "longs": len([t for t in closed if t["side"] == "LONG"]),
        "shorts": len([t for t in closed if t["side"] == "SHORT"]),
        "skipped": skipped,
    }


def print_trades(trades):
    print(f"\n{'Date':<12}{'Symbol':<12}{'Side':<6}{'Entry':>8} {'SL':>8}({'lbl':>3})"
          f"  {'TP':>8}({'lbl':>3})  {'Exit':>8}{'Why':>5}{'P&L %':>8}{'R':>6}")
    print("-" * 100)
    for t in trades:
        c = "✓" if t["exit_reason"] == "TP" else "✗" if t["exit_reason"] == "SL" else "·"
        print(f"{str(t['date']):<12}{t['symbol']:<12}{t['side']:<6}"
              f"{t['entry']:>8.2f} {t['sl']:>8.2f}({t.get('sl_label','—'):>3})  "
              f"{t['tp']:>8.2f}({t.get('tp_label','—'):>3})  "
              f"{t['exit']:>8.2f}{t['exit_reason']:>5}"
              f"{t['pnl_pct']:>7.2f}% {c}{t['r']:>5.2f}")


def print_summary(summary):
    s = summary
    print(f"\n┌─ {s['symbol']} ─────────────────────────────────────")
    print(f"│ Trades        : {s['trades']}  ({s['longs']}L / {s['shorts']}S)")
    print(f"│ Wins          : {s['wins']}    ({s['win_rate']:.1f}%)")
    print(f"│ Losses        : {s['losses']}")
    print(f"│ EOD exits     : {s['eods']}")
    print(f"│ Total P&L     : {s['total_pnl_pct']:+.2f}%   ({s['total_r']:+.2f}R)")
    print(f"│ Avg win       : {s['avg_win']:+.2f}%")
    print(f"│ Avg loss      : {s['avg_loss']:+.2f}%")
    print(f"│ Expectancy    : {s['expectancy']:+.3f}% / trade")
    sk = s.get("skipped", {})
    if sk:
        print(f"│ Skipped       : trend={sk.get('trend',0)} "
              f"min_risk={sk.get('min_risk',0)} max_risk={sk.get('max_risk',0)} "
              f"no_pivot={sk.get('no_pivot',0)}")
    print( "└─────────────────────────────────────────────────────")


def main():
    args = sys.argv[1:] or ["MCX.NS"]
    print(f"Backtesting {len(args)} symbol(s) on 5-min candles.")
    print(f"⚠ yfinance caps 5-min history at ~60 days — a true 1-year 5m backtest is not")
    print(f"  possible with this data source. Use a paid feed for longer history.\n")
    print(f"Strategy: ORB 09:15–09:20 IST + Fibonacci pivot SL/TP (prev-day H/L/C).")
    print(f"          SL = nearest pivot opposing trade. TP = {TP_NTH}-nd nearest in trade dir.")
    print(f"          Risk band: {MIN_RISK_PCT*100:.2f}% – {MAX_RISK_PCT*100:.2f}%.   Trend filter: {TREND_FILTER}.")
    print(f"          Trail SL → BE when intermediate pivot tagged: {TRAIL_TO_BE}.")
    print(f"          Auto-flat 15:25 IST.\n")

    all_trades = []
    summaries = []
    for sym in args:
        print(f"⏳ {sym} …")
        trades, summary = backtest_symbol(sym)
        if summary:
            all_trades.extend(trades)
            summaries.append(summary)
            print_trades(trades)
            print_summary(summary)
        else:
            print(f"  No trades for {sym}.")

    if len(summaries) > 1:
        print("\n" + "=" * 60)
        print("AGGREGATE")
        agg_trades  = sum(s["trades"] for s in summaries)
        agg_wins    = sum(s["wins"]   for s in summaries)
        agg_pnl     = sum(s["total_pnl_pct"] for s in summaries)
        agg_r       = sum(s["total_r"] for s in summaries)
        wr = agg_wins / agg_trades * 100 if agg_trades else 0
        print(f"  Symbols       : {len(summaries)}")
        print(f"  Trades        : {agg_trades}")
        print(f"  Win rate      : {wr:.1f}%")
        print(f"  Total P&L     : {agg_pnl:+.2f}% ({agg_r:+.2f}R)")
        print("=" * 60)


if __name__ == "__main__":
    main()
