# Delta Bot

Multi-strategy trading dashboard:

- **Delta Exchange** crypto perpetuals — Donchian breakout (large caps on 1h, alts on 15m) + Gold SMC strategy on PAXG
- **MCX / NSE** Indian equities — Opening Range Breakout (09:15–09:20 IST) with Fibonacci-pivot SL/TP
- Live forward-tester (paper trading), Telegram alerts, web dashboard

Pure Python — no pandas in the live bot loop (yfinance only used for MCX backtest/data).

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env       # fill in Telegram token + chat id
python web_app.py
# open http://127.0.0.1:5000
```

## Files

| File | Purpose |
|---|---|
| `web_app.py` | Flask dashboard, starts both engines |
| `bot.py` | Delta Exchange scan engine |
| `mcx_bot.py` | MCX/NSE ORB engine |
| `mcx_backtest.py` | ORB + Fib-pivot backtester |
| `indicators.py` | EMA, ATR, Donchian, entropy, Gold SMC logic |
| `forward_test.py` | Paper-trade account, P&L, equity curve |
| `delta_client.py` | Delta Exchange public API client |
| `telegram_alert.py` | Telegram notifications |
| `config.py` | Strategy + account settings |

## Deploy (Oracle Cloud Free Tier)

1. Create an Ubuntu 22.04 ARM VM (always-free).
2. `git clone <repo> && cd deltabot`
3. `python3 -m venv .venv && source .venv/bin/activate`
4. `pip install -r requirements.txt`
5. `cp .env.example .env && nano .env`
6. `sudo cp deltabot.service /etc/systemd/system/`
7. `sudo systemctl enable --now deltabot`
8. Open port 5000 in the security list to access the dashboard.

## License

Personal / educational use.
