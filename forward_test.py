"""
Forward-tester with realistic margin trading account.

Account model:
  • Starting balance: $240 USD
  • Leverage: 25x
  • Risk per trade: 2% of current balance (sizing derived from SL distance)
  • SL: 0.5% adverse, TP: 1.5% favourable (3:1 R:R)
  • Max hold: 8 hours (32 × 15m bars), auto-closed at market

P&L math (linear perpetual, INR-denominated account):
  notional   = position_size_in_quote   ($ size of position)
  margin     = notional / leverage      ($ collateral locked)
  P&L($)     = notional × ((exit-entry)/entry) × side_multiplier

Sizing math:
  risk_amount       = balance × RISK_PER_TRADE_PCT / 100
  notional          = risk_amount / STOP_LOSS_PCT
  margin            = notional / leverage
  capped at:        balance × leverage  (max possible notional)
  capped at:        balance             (max margin = whole account)
"""

import json
import sqlite3
import time
import logging
from pathlib import Path
from threading import Lock

from config import (
    STARTING_BALANCE, LEVERAGE, RISK_PER_TRADE_PCT, FIXED_MARGIN_PER_TRADE,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLD_BARS,
    TAKER_FEE_PCT, SLIPPAGE_PCT, TRAILING_STOP_ENABLED,
)

logger = logging.getLogger(__name__)

STATE_FILE  = Path(__file__).parent / "ft_state.json"   # legacy / migration source
DB_FILE     = Path(__file__).parent / "ft_state.db"


class ForwardTester:
    """SQLite-backed paper-trade ledger.

    Tables:
      account     (key TEXT PRIMARY KEY, value REAL)        - balance, etc.
      trades      (id INTEGER PK, ... full trade dict columns + extras_json TEXT)
      equity      (time INTEGER PK, balance REAL, equity REAL)

    The in-memory `self.trades` list mirrors the SQLite `trades` table for fast
    iteration. Every mutation goes through INSERT/UPDATE so the DB is always the
    source of truth and any crash leaves it consistent.
    """

    # All "core" trade fields that get their own typed column. Anything else
    # goes into `extras_json` (e.g. level_label, trail_distance, high_water).
    _CORE_COLS = [
        "id", "symbol", "side", "status", "leverage",
        "entry_price", "entry_time", "sl", "tp",
        "notional_usd", "margin_usd", "risk_usd",
        "qty", "lots", "contract_value",
        "exit_price", "exit_time", "exit_reason",
        "pnl_usd", "pnl_pct", "pnl_price_pct",
        "fees_usd", "gross_pnl_usd", "balance_after",
        "bars_held", "max_hold_bars",
    ]

    def __init__(self, file: Path = DB_FILE):
        # `file` may be passed as a .json path (e.g. by backtest.py). Force .db.
        self.file = file if str(file).endswith(".db") else DB_FILE
        self.lock = Lock()
        self.db = sqlite3.connect(self.file, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate_json_if_needed()

        # Hydrate in-memory mirror
        self.trades        : list[dict] = self._load_trades()
        self.equity_curve  : list[dict] = self._load_equity()
        self.balance       : float      = self._load_balance()
        self.last_prices   : dict[str, float] = {}

    # ============================================================ DB setup
    def _init_schema(self) -> None:
        c = self.db.cursor()
        cols_sql = ",\n  ".join([
            "id INTEGER PRIMARY KEY",
            "symbol TEXT",
            "side TEXT",
            "status TEXT",
            "leverage REAL",
            "entry_price REAL", "entry_time INTEGER",
            "sl REAL", "tp REAL",
            "notional_usd REAL", "margin_usd REAL", "risk_usd REAL",
            "qty REAL", "lots REAL", "contract_value REAL",
            "exit_price REAL", "exit_time INTEGER", "exit_reason TEXT",
            "pnl_usd REAL", "pnl_pct REAL", "pnl_price_pct REAL",
            "fees_usd REAL", "gross_pnl_usd REAL", "balance_after REAL",
            "bars_held INTEGER", "max_hold_bars INTEGER",
            "extras_json TEXT",
        ])
        c.execute(f"CREATE TABLE IF NOT EXISTS trades (\n  {cols_sql}\n)")
        c.execute("CREATE TABLE IF NOT EXISTS account (key TEXT PRIMARY KEY, value REAL)")
        c.execute("""CREATE TABLE IF NOT EXISTS equity (
                       time INTEGER PRIMARY KEY,
                       balance REAL,
                       equity REAL
                     )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")

    def _migrate_json_if_needed(self) -> None:
        """Import legacy ft_state.json on first run if the DB is empty."""
        c = self.db.cursor()
        n = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        if n > 0: return
        if not STATE_FILE.exists(): return
        try:
            data = json.loads(STATE_FILE.read_text())
        except Exception as e:
            logger.warning("Could not read legacy ft_state.json: %s", e)
            return
        if not isinstance(data, dict): return

        for t in data.get("trades", []):
            self._insert_trade(t, commit=False)
        for p in data.get("equity_curve", []):
            try:
                c.execute("INSERT OR REPLACE INTO equity(time,balance,equity) VALUES (?,?,?)",
                          (int(p["time"]), float(p["balance"]), float(p["equity"])))
            except (KeyError, ValueError, TypeError):
                continue
        if "balance" in data:
            c.execute("INSERT OR REPLACE INTO account(key,value) VALUES ('balance',?)",
                      (float(data["balance"]),))
        logger.info("Migrated ft_state.json → SQLite (%d trades, %d equity rows)",
                    len(data.get("trades", [])), len(data.get("equity_curve", [])))
        # Rename so we don't re-import on next boot
        try:
            STATE_FILE.rename(STATE_FILE.with_suffix(".json.migrated"))
        except OSError:
            pass

    # ============================================================ Hydration
    def _row_to_trade(self, r: sqlite3.Row) -> dict:
        t = {k: r[k] for k in r.keys() if k != "extras_json"}
        if r["extras_json"]:
            try: t.update(json.loads(r["extras_json"]))
            except Exception: pass
        return t

    def _load_trades(self) -> list[dict]:
        c = self.db.cursor()
        rows = c.execute("SELECT * FROM trades ORDER BY entry_time ASC").fetchall()
        return [self._row_to_trade(r) for r in rows]

    def _load_equity(self) -> list[dict]:
        c = self.db.cursor()
        rows = c.execute("SELECT time,balance,equity FROM equity ORDER BY time ASC").fetchall()
        return [dict(r) for r in rows]

    def _load_balance(self) -> float:
        c = self.db.cursor()
        r = c.execute("SELECT value FROM account WHERE key='balance'").fetchone()
        return float(r[0]) if r else STARTING_BALANCE

    # ============================================================ Write helpers
    def _split_trade(self, t: dict) -> tuple[dict, dict]:
        """Returns (core_cols_dict, extras_dict)."""
        core = {k: t.get(k) for k in self._CORE_COLS}
        extras = {k: v for k, v in t.items() if k not in self._CORE_COLS}
        return core, extras

    def _insert_trade(self, t: dict, commit: bool = True) -> None:
        core, extras = self._split_trade(t)
        placeholders = ",".join("?" for _ in self._CORE_COLS) + ",?"
        cols = ",".join(self._CORE_COLS) + ",extras_json"
        vals = [core.get(k) for k in self._CORE_COLS] + [json.dumps(extras)]
        self.db.execute(f"INSERT OR REPLACE INTO trades({cols}) VALUES ({placeholders})", vals)

    def _update_trade(self, t: dict) -> None:
        self._insert_trade(t)   # PRIMARY KEY id → INSERT OR REPLACE works as upsert

    def _save_balance(self) -> None:
        self.db.execute("INSERT OR REPLACE INTO account(key,value) VALUES ('balance',?)",
                        (float(self.balance),))

    def _save_equity_point(self, ts: int, bal: float, eq: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO equity(time,balance,equity) VALUES (?,?,?)",
                        (int(ts), float(bal), float(eq)))

    def _save(self) -> None:
        """Full sync — kept for compatibility. Writes balance + every trade."""
        self._save_balance()
        for t in self.trades:
            self._update_trade(t)

    def reset(self) -> None:
        """Wipe history and restore starting balance."""
        with self.lock:
            self.balance = STARTING_BALANCE
            self.trades.clear()
            self.equity_curve.clear()
            self.last_prices.clear()
            self.db.execute("DELETE FROM trades")
            self.db.execute("DELETE FROM equity")
            self._save_balance()
        logger.info("Forward tester reset to $%.2f", STARTING_BALANCE)

    # ============================================================ Sizing
    def _size_position(self, stop_distance_pct: float | None = None) -> tuple[float, float, float]:
        """
        Returns (notional_usd, margin_usd, risk_usd).
        Fixed-margin sizing: each trade uses FIXED_MARGIN_PER_TRADE as collateral,
        notional = margin × LEVERAGE. Capped by available balance.
        """
        sl_pct   = stop_distance_pct if (stop_distance_pct and stop_distance_pct > 0) \
                                     else STOP_LOSS_PCT
        margin   = min(FIXED_MARGIN_PER_TRADE, max(0.0, self.balance))
        notional = margin * LEVERAGE
        risk_usd = notional * sl_pct
        return round(notional, 2), round(margin, 2), round(risk_usd, 2)

    # ============================================================ Trades
    def has_open_trade(self, symbol: str, side: str | None = None) -> bool:
        with self.lock:
            return any(
                t["symbol"] == symbol and t["status"] == "OPEN"
                and (side is None or t["side"] == side)
                for t in self.trades
            )

    def open_trade(self, symbol: str, signal: dict, contract_value: float = 1.0) -> dict | None:
        side = "LONG" if signal["type"] == "BREAKOUT" else "SHORT"
        signal_price = float(signal["close"])

        # Slippage: longs fill ABOVE signal price, shorts fill BELOW
        if side == "LONG":
            entry = signal_price * (1 + SLIPPAGE_PCT)
        else:
            entry = signal_price * (1 - SLIPPAGE_PCT)

        # If signal supplies an ATR-based SL, use it. Otherwise fall back to %-SL.
        if "sl" in signal and signal["sl"]:
            sl = float(signal["sl"])
        elif side == "LONG":
            sl = entry * (1 - STOP_LOSS_PCT)
        else:
            sl = entry * (1 + STOP_LOSS_PCT)

        # TP is far away (effectively disabled) when trailing is active
        use_trailing = bool(signal.get("use_trailing", False)) and TRAILING_STOP_ENABLED
        if use_trailing:
            tp = entry * 100.0 if side == "LONG" else entry * 0.01   # never hit
        else:
            tp = entry * (1 + TAKE_PROFIT_PCT) if side == "LONG" else entry * (1 - TAKE_PROFIT_PCT)

        # Size based on the actual stop distance (so ATR-based stops respect risk)
        sl_pct = abs(entry - sl) / entry if entry else STOP_LOSS_PCT
        notional, margin, risk_usd = self._size_position(stop_distance_pct=sl_pct)
        if margin <= 0 or self.balance <= 0:
            logger.warning("Insufficient balance for new trade on %s", symbol)
            return None

        # Quantity in base asset, and number of contracts (lots)
        qty   = notional / entry if entry else 0.0
        cv    = contract_value if contract_value and contract_value > 0 else 1.0
        lots  = qty / cv

        trade = {
            "id":             int(time.time() * 1000),
            "symbol":         symbol,
            "side":           side,
            "leverage":       LEVERAGE,
            "entry_price":    entry,
            "entry_time":     int(signal["time"]),
            "sl":             round(sl, 8),
            "tp":             round(tp, 8),
            "notional_usd":   notional,
            "margin_usd":     margin,
            "risk_usd":       risk_usd,
            "qty":            round(qty, 8),
            "lots":           round(lots, 4),
            "contract_value": cv,
            "level_label":    signal.get("level_label"),
            "level_price":    signal.get("level_price"),
            "use_trailing":   use_trailing,
            "trail_distance": float(signal.get("trail_distance", 0)) if use_trailing else 0.0,
            "high_water":     entry,    # peak favorable price seen so far
            "max_hold_bars":  int(signal.get("max_hold_bars", MAX_HOLD_BARS)),
            "status":         "OPEN",
            "exit_price":     None,
            "exit_time":      None,
            "exit_reason":    None,
            "pnl_usd":        None,
            "pnl_pct":        None,    # % of entry (price move)
            "balance_after":  None,
            "bars_held":      0,
        }
        with self.lock:
            self.trades.append(trade)
            self._insert_trade(trade)
            self._save_balance()
        logger.info(
            "OPEN %s %s @ %g | notional=$%.2f margin=$%.2f risk=$%.2f | SL %g TP %g",
            side, symbol, entry, notional, margin, risk_usd, sl, tp,
        )
        return trade

    def update(self, symbol: str, current_price: float, current_time: int) -> list[dict]:
        """Re-price open trades for this symbol. Returns newly closed trades."""
        self.last_prices[symbol] = current_price
        closed = []
        with self.lock:
            for t in self.trades:
                if t["symbol"] != symbol or t["status"] != "OPEN":
                    continue
                t["bars_held"] += 1

                # Trailing-stop ratchet: tighten SL as price moves favorably
                if t.get("use_trailing") and t.get("trail_distance", 0) > 0:
                    if t["side"] == "LONG" and current_price > t["high_water"]:
                        t["high_water"] = current_price
                        new_sl = current_price - t["trail_distance"]
                        if new_sl > t["sl"]:
                            t["sl"] = round(new_sl, 8)
                    elif t["side"] == "SHORT" and current_price < t["high_water"]:
                        t["high_water"] = current_price
                        new_sl = current_price + t["trail_distance"]
                        if new_sl < t["sl"]:
                            t["sl"] = round(new_sl, 8)

                hit_status, hit_price = self._check_exit(t, current_price)
                if hit_status:
                    self._close_trade(t, hit_status, hit_price, current_time)
                    self._update_trade(t)
                    closed.append(dict(t))
                elif t["bars_held"] >= t.get("max_hold_bars", MAX_HOLD_BARS):
                    self._close_trade(t, "TIMEOUT", current_price, current_time)
                    self._update_trade(t)
                    closed.append(dict(t))
                else:
                    # Update bars_held / trailing SL in DB
                    self._update_trade(t)
            if closed:
                self._snapshot_equity(current_time)
                self._save_balance()

        for t in closed:
            logger.info(
                "CLOSE %s %s: %s @ %g | P&L $%.2f (%.2f%%) | bal $%.2f",
                t["side"], t["symbol"], t["exit_reason"], t["exit_price"],
                t["pnl_usd"], t["pnl_pct"], t["balance_after"],
            )
        return closed

    def _check_exit(self, trade: dict, price: float) -> tuple[str | None, float | None]:
        """Returns (status_or_None, fill_price_or_None) - fills at SL/TP exactly.
        For trailing-stop trades, SL hits below entry are LOSS, above entry are WIN
        (because the stop ratcheted in profit)."""
        side, sl, tp = trade["side"], trade["sl"], trade["tp"]
        entry = trade["entry_price"]
        if side == "LONG":
            if price <= sl:
                status = "WIN" if sl > entry else "LOSS"
                return status, sl
            if price >= tp: return "WIN", tp
        else:
            if price >= sl:
                status = "WIN" if sl < entry else "LOSS"
                return status, sl
            if price <= tp: return "WIN", tp
        return None, None

    def _close_trade(self, trade: dict, status: str, exit_price: float, exit_time: int) -> None:
        entry = trade["entry_price"]
        # Slippage on exit: longs exit BELOW the trigger price, shorts exit ABOVE
        if trade["side"] == "LONG":
            fill_price = exit_price * (1 - SLIPPAGE_PCT)
            move_pct = (fill_price - entry) / entry
        else:
            fill_price = exit_price * (1 + SLIPPAGE_PCT)
            move_pct = (entry - fill_price) / entry

        gross_pnl = trade["notional_usd"] * move_pct
        # Round-trip taker fees on entry + exit notional
        fees = trade["notional_usd"] * TAKER_FEE_PCT * 2
        pnl_usd = gross_pnl - fees
        self.balance += pnl_usd

        trade["status"]        = status
        trade["exit_reason"]   = "STOP_LOSS"   if status == "LOSS" else \
                                 "TAKE_PROFIT" if status == "WIN"  else "TIMEOUT"
        trade["exit_price"]    = round(fill_price, 8)
        trade["exit_time"]     = exit_time
        trade["fees_usd"]      = round(fees, 2)
        trade["gross_pnl_usd"] = round(gross_pnl, 2)
        trade["pnl_usd"]       = round(pnl_usd, 2)
        # P&L % is now return on margin (collateral), not raw price move.
        # With leverage, a 1% price move on $1000 notional with $40 margin = 25% margin return.
        margin = trade.get("margin_usd") or 0.0
        trade["pnl_pct"]       = round((pnl_usd / margin * 100) if margin else 0.0, 4)
        trade["pnl_price_pct"] = round(move_pct * 100, 4)   # raw price-move % (kept for reference)
        trade["balance_after"] = round(self.balance, 2)

    # ============================================================ Equity / stats
    def _snapshot_equity(self, ts: int) -> None:
        bal = round(self.balance, 2)
        eq  = round(self.equity(), 2)
        self.equity_curve.append({"time": ts, "balance": bal, "equity": eq})
        self._save_equity_point(ts, bal, eq)

    def unrealized_pnl(self) -> float:
        total = 0.0
        for t in self.trades:
            if t["status"] != "OPEN":
                continue
            price = self.last_prices.get(t["symbol"])
            if price is None:
                continue
            entry = t["entry_price"]
            if t["side"] == "LONG":
                move = (price - entry) / entry
            else:
                move = (entry - price) / entry
            total += t["notional_usd"] * move
        return total

    def equity(self) -> float:
        return self.balance + self.unrealized_pnl()

    def margin_used(self) -> float:
        return sum(t["margin_usd"] for t in self.trades if t["status"] == "OPEN")

    def _risk_metrics(self) -> dict:
        """Sharpe / Volatility / MaxDD / VaR / Day-change from equity_curve."""
        import math
        ec = self.equity_curve
        if len(ec) < 2:
            return {"sharpe":0.0, "volatility":0.0, "max_drawdown":0.0,
                    "var95":0.0, "day_change":0.0, "day_change_pct":0.0}

        equities = [p["equity"] for p in ec]
        rets = [(equities[i]-equities[i-1])/equities[i-1]
                for i in range(1,len(equities)) if equities[i-1]]

        # Volatility & Sharpe (annualized assuming ~scan-per-15min ≈ 35040/yr)
        if rets:
            mean_r = sum(rets)/len(rets)
            var_r  = sum((r-mean_r)**2 for r in rets)/len(rets)
            std_r  = math.sqrt(var_r)
            ann    = math.sqrt(35040)
            vol    = std_r * ann * 100
            sharpe = (mean_r/std_r * ann) if std_r > 0 else 0.0
            sorted_r = sorted(rets)
            var95 = -sorted_r[max(0, int(len(sorted_r)*0.05))] * equities[-1]
        else:
            vol = sharpe = var95 = 0.0

        # Max drawdown
        peak = equities[0]; max_dd = 0.0
        for e in equities:
            if e > peak: peak = e
            dd = (peak - e)/peak * 100 if peak else 0
            if dd > max_dd: max_dd = dd

        # Day change — equity now vs equity ~24h ago
        now_ts = ec[-1]["time"]
        cutoff = now_ts - 24*3600
        prior  = next((p for p in ec if p["time"] >= cutoff), ec[0])
        day_change = equities[-1] - prior["equity"]
        day_pct = (day_change / prior["equity"] * 100) if prior["equity"] else 0.0

        return {
            "sharpe":         round(sharpe, 2),
            "volatility":     round(vol, 1),
            "max_drawdown":   round(max_dd, 2),
            "var95":          round(var95, 2),
            "day_change":     round(day_change, 2),
            "day_change_pct": round(day_pct, 2),
        }

    def get_stats(self) -> dict:
        with self.lock:
            wins   = [t for t in self.trades if t["status"] == "WIN"]
            losses = [t for t in self.trades if t["status"] == "LOSS"]
            timeouts = [t for t in self.trades if t["status"] == "TIMEOUT"]
            open_  = [t for t in self.trades if t["status"] == "OPEN"]
            closed = wins + losses + timeouts

            realized_pnl = sum(t["pnl_usd"] for t in closed if t["pnl_usd"] is not None)
            unreal       = self.unrealized_pnl()
            equity       = self.balance + unreal
            margin_used  = self.margin_used()
            free_margin  = self.balance - margin_used

            avg_win  = (sum(t["pnl_usd"] for t in wins) / len(wins))     if wins   else 0
            avg_loss = (sum(t["pnl_usd"] for t in losses) / len(losses)) if losses else 0
            risk = self._risk_metrics()

        return {
            "starting_balance": STARTING_BALANCE,
            **risk,
            "balance":          round(self.balance, 2),
            "equity":           round(equity, 2),
            "leverage":         LEVERAGE,
            "margin_used":      round(margin_used, 2),
            "free_margin":      round(free_margin, 2),
            "unrealized_pnl":   round(unreal, 2),
            "realized_pnl":     round(realized_pnl, 2),
            "total_pnl":        round(realized_pnl + unreal, 2),
            "total_pnl_pct":    round((equity - STARTING_BALANCE) / STARTING_BALANCE * 100, 2),
            "total_trades":     len(self.trades),
            "open":             len(open_),
            "wins":             len(wins),
            "losses":           len(losses),
            "timeouts":         len(timeouts),
            "win_rate":         round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "avg_win":          round(avg_win, 2),
            "avg_loss":         round(avg_loss, 2),
            "profit_factor":    round(
                                    abs(sum(t["pnl_usd"] for t in wins) /
                                        sum(t["pnl_usd"] for t in losses))
                                    if losses and sum(t["pnl_usd"] for t in losses) != 0 else 0,
                                    2),
        }

    def _enrich_live(self, t: dict) -> dict:
        """Add current_price + live unrealized P&L for OPEN trades."""
        t = dict(t)
        if t["status"] == "OPEN":
            price = self.last_prices.get(t["symbol"])
            if price is not None:
                entry = t["entry_price"]
                if t["side"] == "LONG":
                    move = (price - entry) / entry if entry else 0
                else:
                    move = (entry - price) / entry if entry else 0
                pnl_live = t["notional_usd"] * move
                margin = t.get("margin_usd") or 0.0
                t["current_price"]    = round(price, 8)
                t["pnl_usd_live"]     = round(pnl_live, 2)
                # P&L % = return on margin (with leverage), not raw price move
                t["pnl_pct_live"]     = round((pnl_live / margin * 100) if margin else 0.0, 4)
                t["pnl_price_pct_live"] = round(move * 100, 4)   # raw % (kept for reference)
            else:
                t["current_price"]  = None
                t["pnl_usd_live"]   = None
                t["pnl_pct_live"]   = None
        return t

    def get_trades(self, limit: int = 200) -> list[dict]:
        with self.lock:
            return [self._enrich_live(t) for t in reversed(self.trades[-limit:])]

    def get_open_trades(self) -> list[dict]:
        with self.lock:
            return [self._enrich_live(t) for t in self.trades if t["status"] == "OPEN"]

    def get_equity_curve(self, limit: int = 500) -> list[dict]:
        with self.lock:
            return list(self.equity_curve[-limit:])

    # ============================================================ Export
    def export_csv(self, csv_path: Path | str = "trades_export.csv") -> Path:
        """Dump all trades to CSV (closed + open). Returns path written."""
        import csv
        path = Path(csv_path)
        cols = self._CORE_COLS + ["level_label", "level_price", "use_trailing",
                                   "trail_distance", "high_water"]
        with self.lock, path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for t in sorted(self.trades, key=lambda x: x.get("entry_time") or 0):
                w.writerow(t)
        logger.info("Exported %d trades → %s", len(self.trades), path)
        return path
