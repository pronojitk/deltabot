"""
Tkinter GUI for the Delta Exchange Alert Bot.

Run with:  py gui.py

Shows:
  • Start / Stop / Reset controls + scan status
  • Account stats (Balance, Equity, P&L, Margin, Leverage) — INR
  • Live signals table
  • Forward-test trade history (with notional, margin, ₹ P&L)
  • Watchlist (per-symbol bias from each scan)
  • Live console log
"""

import queue
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone

from bot import BotEngine
from forward_test import ForwardTester


# ---------------------------------------------------------------------------- helpers
def fmt_inr(v) -> str:
    if v is None:
        return "—"
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_num(v, sig: int = 6) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{sig}g}"
    except (TypeError, ValueError):
        return "—"


# ============================================================================ GUI
class BotGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Delta Exchange Alert Bot")
        self.root.geometry("1280x780")

        self.event_queue: "queue.Queue[dict]" = queue.Queue()
        self.engine: BotEngine | None = None
        self.send_telegram_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._poll_events()

    # ============================================================== UI
    def _build_ui(self):
        # ---- Top control bar ----------------------------------------------
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        self.start_btn = ttk.Button(top, text="▶  Start Bot", command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=4)

        self.stop_btn = ttk.Button(top, text="■  Stop", command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        ttk.Button(top, text="↺  Reset Account", command=self.reset_account)\
            .pack(side=tk.LEFT, padx=4)

        ttk.Checkbutton(top, text="Send Telegram alerts", variable=self.send_telegram_var)\
            .pack(side=tk.LEFT, padx=12)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(top, textvariable=self.status_var, foreground="#0a7",
                  font=("Segoe UI", 9, "bold"))\
            .pack(side=tk.RIGHT, padx=8)

        # ---- Stats strip ---------------------------------------------------
        stats = ttk.LabelFrame(self.root, text="Forward-Test Account (₹ INR · 25× leverage)", padding=8)
        stats.pack(fill=tk.X, padx=8, pady=4)

        self.stat_vars = {
            "balance":       tk.StringVar(value="—"),
            "equity":        tk.StringVar(value="—"),
            "total_pnl":     tk.StringVar(value="—"),
            "total_pnl_pct": tk.StringVar(value="—"),
            "margin_used":   tk.StringVar(value="—"),
            "free_margin":   tk.StringVar(value="—"),
            "leverage":      tk.StringVar(value="—"),
            "win_rate":      tk.StringVar(value="—"),
            "trades":        tk.StringVar(value="—"),
        }
        labels = [
            ("balance",       "Balance"),
            ("equity",        "Equity"),
            ("total_pnl",     "Total P&L"),
            ("total_pnl_pct", "Total P&L %"),
            ("margin_used",   "Margin Used"),
            ("free_margin",   "Free Margin"),
            ("leverage",      "Leverage"),
            ("win_rate",      "Win %"),
            ("trades",        "Trades"),
        ]
        for i, (key, label) in enumerate(labels):
            cell = ttk.Frame(stats)
            cell.grid(row=0, column=i, padx=10, sticky="w")
            ttk.Label(cell, text=label, foreground="#888", font=("Segoe UI", 8)).pack(anchor="w")
            ttk.Label(cell, textvariable=self.stat_vars[key],
                      font=("Segoe UI", 11, "bold")).pack(anchor="w")

        # ---- Notebook tabs -------------------------------------------------
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # ===== Watchlist tab
        wl_frame = ttk.Frame(nb)
        nb.add(wl_frame, text="Watchlist")
        wl_cols = ("symbol", "price", "change_24h", "bias", "trend",
                   "ema7", "ema21", "resistance", "r_dist", "support", "s_dist")
        self.wl_tree = ttk.Treeview(wl_frame, columns=wl_cols, show="headings", height=14)
        wl_widths = (110, 100, 90, 90, 70, 100, 100, 130, 80, 130, 80)
        wl_titles = ("Symbol", "Price", "24h %", "Bias", "Trend",
                     "EMA7", "EMA21", "Resistance", "R-Dist", "Support", "S-Dist")
        for c, t, w in zip(wl_cols, wl_titles, wl_widths):
            self.wl_tree.heading(c, text=t)
            self.wl_tree.column(c, width=w, anchor="center")
        self.wl_tree.tag_configure("Bullish", background="#dff5e1")
        self.wl_tree.tag_configure("Bearish", background="#fbe1e1")
        self.wl_tree.tag_configure("Neutral", background="#f4f4f4")
        wl_scroll = ttk.Scrollbar(wl_frame, orient=tk.VERTICAL, command=self.wl_tree.yview)
        self.wl_tree.configure(yscrollcommand=wl_scroll.set)
        self.wl_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        wl_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        # Track row id per symbol so we update in place
        self.wl_rows: dict[str, str] = {}

        # ===== Signals tab
        sig_frame = ttk.Frame(nb)
        nb.add(sig_frame, text="Live Signals")
        sig_cols = ("time", "symbol", "type", "level", "level_price",
                    "close", "tests", "ema7", "ema21")
        self.sig_tree = ttk.Treeview(sig_frame, columns=sig_cols, show="headings", height=14)
        for c, w in zip(sig_cols, (140, 110, 100, 80, 110, 110, 70, 100, 100)):
            self.sig_tree.heading(c, text=c.upper().replace("_", " "))
            self.sig_tree.column(c, width=w, anchor="center")
        self.sig_tree.tag_configure("BREAKOUT",  background="#dff5e1")
        self.sig_tree.tag_configure("BREAKDOWN", background="#fbe1e1")
        sig_scroll = ttk.Scrollbar(sig_frame, orient=tk.VERTICAL, command=self.sig_tree.yview)
        self.sig_tree.configure(yscrollcommand=sig_scroll.set)
        self.sig_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sig_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ===== Trades tab
        tr_frame = ttk.Frame(nb)
        nb.add(tr_frame, text="Forward-Test Trades")
        tr_cols = ("symbol", "side", "status", "entry", "sl", "tp", "exit",
                   "notional", "margin", "pnl_inr", "pnl_pct", "level", "bars")
        self.tr_tree = ttk.Treeview(tr_frame, columns=tr_cols, show="headings", height=14)
        tr_widths = (100, 60, 80, 100, 100, 100, 100, 110, 100, 110, 80, 80, 50)
        tr_titles = ("Symbol", "Side", "Status", "Entry", "SL", "TP", "Exit",
                     "Notional ₹", "Margin ₹", "P&L ₹", "P&L %", "Level", "Bars")
        for c, t, w in zip(tr_cols, tr_titles, tr_widths):
            self.tr_tree.heading(c, text=t)
            self.tr_tree.column(c, width=w, anchor="center")
        self.tr_tree.tag_configure("OPEN",    background="#fff8d6")
        self.tr_tree.tag_configure("WIN",     background="#dff5e1")
        self.tr_tree.tag_configure("LOSS",    background="#fbe1e1")
        self.tr_tree.tag_configure("TIMEOUT", background="#eeeeee")
        tr_scroll = ttk.Scrollbar(tr_frame, orient=tk.VERTICAL, command=self.tr_tree.yview)
        self.tr_tree.configure(yscrollcommand=tr_scroll.set)
        self.tr_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tr_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Button(tr_frame, text="Refresh trades", command=self._refresh_trades)\
            .pack(side=tk.BOTTOM, pady=4)

        # ===== Log tab
        log_frame = ttk.Frame(nb)
        nb.add(log_frame, text="Console Log")
        self.log_text = scrolledtext.ScrolledText(log_frame, height=20, wrap=tk.WORD,
                                                  font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Initial fill from any persisted state
        self._refresh_trades()
        self._refresh_stats()

    # ============================================================ Engine ctl
    def start_bot(self):
        if self.engine and self.engine.is_running():
            return
        self.engine = BotEngine(
            on_event=self._on_engine_event,
            send_telegram=self.send_telegram_var.get(),
        )
        self.engine.start()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Running…")
        self._log("Bot started.")

    def stop_bot(self):
        if self.engine:
            self.engine.stop()
            self.status_var.set("Stopping… (will halt after current scan)")
            self._log("Stop requested.")
        self.stop_btn.config(state=tk.DISABLED)

    def reset_account(self):
        if self.engine and self.engine.is_running():
            messagebox.showwarning("Cannot reset",
                "Stop the bot before resetting the account.")
            return
        if not messagebox.askyesno("Reset account",
                "Wipe all trades and reset balance to ₹20,000?"):
            return
        ForwardTester().reset()
        # Wipe UI tables too
        for item in self.tr_tree.get_children():
            self.tr_tree.delete(item)
        for item in self.sig_tree.get_children():
            self.sig_tree.delete(item)
        self._refresh_stats()
        self._log("Account reset to starting balance.")

    # ============================================================ Events (worker thread)
    def _on_engine_event(self, event: dict) -> None:
        """Called from BotEngine thread — push onto queue, never touch tk widgets here."""
        self.event_queue.put(event)

    # ============================================================ Polling (main thread)
    def _poll_events(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                try:
                    self._handle_event(event)
                except Exception as e:
                    self._log(f"GUI error handling {event.get('type')}: {e}")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_events)

    def _handle_event(self, event: dict):
        et = event.get("type")
        if et == "startup":
            n = len(event.get("symbols", []))
            self._log(f"Monitoring {n} perpetual contracts.")
            self.status_var.set(f"Running — {n} symbols")

        elif et == "scan_start":
            self.status_var.set(f"Scanning… (#{event['n']})")

        elif et == "scan_complete":
            self._log(
                f"Scan #{event['n']} done in {event['duration']}s | "
                f"signals={event['signals']} alerts={event['alerts']} "
                f"closed={event['trades_closed']}"
            )
            self.status_var.set(f"Idle — last scan #{event['n']} OK")
            self._update_stats(event["stats"])

        elif et == "scan_data":
            self._update_watchlist_row(event)

        elif et == "signal":
            self._add_signal(event["symbol"], event["signal"])
            sig = event["signal"]
            self._log(f"SIGNAL {sig['type']} {event['symbol']} @ {sig['close']} "
                      f"(level {sig['level_label']} tested {sig['tests']}×)")

        elif et == "trade_opened":
            t = event["trade"]
            self._add_trade(t)
            self._log(
                f"OPEN {t['side']} {t['symbol']} @ {fmt_num(t['entry_price'])} | "
                f"notional {fmt_inr(t['notional_usd'])} margin {fmt_inr(t['margin_usd'])}"
            )

        elif et == "trade_closed":
            self._refresh_trades()
            t = event["trade"]
            self._log(
                f"CLOSE {t['side']} {t['symbol']}: {t['exit_reason']} | "
                f"P&L {fmt_inr(t['pnl_usd'])} | bal {fmt_inr(t['balance_after'])}"
            )

        elif et == "alert_sent":
            self._log(f"Telegram alert sent for {event['symbol']} {event['signal']['type']}")

        elif et == "error":
            self._log(f"ERROR scanning {event.get('symbol')}: {event.get('error')}")

        elif et == "fatal":
            self._log(f"FATAL: {event.get('error')}")
            self.status_var.set("Stopped (fatal)")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

        elif et == "stopped":
            self.status_var.set("Stopped")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self._log("Bot stopped.")

    # ============================================================ UI helpers
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)

    # ---- Signals
    def _add_signal(self, symbol: str, sig: dict):
        ts = datetime.fromtimestamp(sig["time"], tz=timezone.utc).strftime("%m-%d %H:%M UTC")
        row = (
            ts, symbol, sig["type"], sig["level_label"],
            fmt_num(sig["level_price"]), fmt_num(sig["close"]),
            sig["tests"], fmt_num(sig["ema7"]), fmt_num(sig["ema21"]),
        )
        self.sig_tree.insert("", 0, values=row, tags=(sig["type"],))

    # ---- Trades
    def _add_trade(self, t: dict):
        self.tr_tree.insert("", 0, values=(
            t["symbol"], t["side"], t["status"],
            fmt_num(t["entry_price"]),
            fmt_num(t["sl"]), fmt_num(t["tp"]),
            fmt_num(t["exit_price"]) if t.get("exit_price") is not None else "—",
            fmt_inr(t.get("notional_usd")),
            fmt_inr(t.get("margin_usd")),
            fmt_inr(t.get("pnl_usd")) if t.get("pnl_usd") is not None else "—",
            fmt_pct(t.get("pnl_pct")),
            t.get("level_label") or "",
            t.get("bars_held", 0),
        ), tags=(t["status"],))

    def _refresh_trades(self):
        for item in self.tr_tree.get_children():
            self.tr_tree.delete(item)
        ft = self.engine.forward_tester if self.engine else ForwardTester()
        # get_trades returns newest-first; insert oldest-first so newest stays on top
        for t in reversed(ft.get_trades(200)):
            self._add_trade(t)

    # ---- Watchlist
    def _update_watchlist_row(self, d: dict):
        sym = d["symbol"]
        r = d.get("resistance") or {}
        s = d.get("support")    or {}
        change = d.get("change_24h", 0.0)
        values = (
            sym,
            fmt_num(d.get("price")),
            fmt_pct(change),
            d.get("bias", "—"),
            d.get("trend", "—"),
            fmt_num(d.get("ema7")),
            fmt_num(d.get("ema21")),
            f"{r.get('label','—')} @ {fmt_num(r.get('price'))}" if r else "—",
            f"{r.get('dist_pct'):+.2f}%" if r else "—",
            f"{s.get('label','—')} @ {fmt_num(s.get('price'))}" if s else "—",
            f"{s.get('dist_pct'):+.2f}%" if s else "—",
        )
        bias_tag = d.get("bias", "Neutral")
        if sym in self.wl_rows and self.wl_tree.exists(self.wl_rows[sym]):
            self.wl_tree.item(self.wl_rows[sym], values=values, tags=(bias_tag,))
        else:
            iid = self.wl_tree.insert("", "end", values=values, tags=(bias_tag,))
            self.wl_rows[sym] = iid

    # ---- Stats
    def _refresh_stats(self):
        ft = self.engine.forward_tester if self.engine else ForwardTester()
        self._update_stats(ft.get_stats())

    def _update_stats(self, stats: dict):
        self.stat_vars["balance"]      .set(fmt_inr(stats.get("balance")))
        self.stat_vars["equity"]       .set(fmt_inr(stats.get("equity")))
        self.stat_vars["total_pnl"]    .set(fmt_inr(stats.get("total_pnl")))
        self.stat_vars["total_pnl_pct"].set(fmt_pct(stats.get("total_pnl_pct")))
        self.stat_vars["margin_used"]  .set(fmt_inr(stats.get("margin_used")))
        self.stat_vars["free_margin"]  .set(fmt_inr(stats.get("free_margin")))
        self.stat_vars["leverage"]     .set(f"{stats.get('leverage', '—')}×")
        self.stat_vars["win_rate"]     .set(
            f"{stats.get('win_rate', 0)}%  ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)"
        )
        self.stat_vars["trades"]       .set(
            f"{stats.get('total_trades', 0)} ({stats.get('open', 0)} open)"
        )


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if root.tk.call("tk", "windowingsystem") == "win32" else "clam")
    except tk.TclError:
        pass
    BotGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
