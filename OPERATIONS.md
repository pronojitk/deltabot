# Operations Guide

Day-to-day reference for running, updating, and troubleshooting Delta Bot on the Oracle Cloud VM.

> **VM**: `ubuntu@140.245.215.71`
> **Dashboard**: <http://140.245.215.71:5000>
> **Project path on VM**: `/home/ubuntu/deltabot`

---

## 1. Connecting to the VM

### From Windows PowerShell

```powershell
ssh -i $HOME\.ssh\oracle.key ubuntu@140.245.215.71
```

If you get `Permission denied`:
```powershell
icacls $HOME\.ssh\oracle.key /reset
icacls $HOME\.ssh\oracle.key /inheritance:r
icacls $HOME\.ssh\oracle.key /grant:r "$($env:COMPUTERNAME)\$($env:USERNAME):(R)"
```

### IP changed?

If the VM was stopped/restarted, the **public IP changes**. Find the new one in the Oracle Console:
**Compute → Instances → deltabot → Networking → Public IPv4 address**

---

## 2. The systemd service

The bot runs as a service called `deltabot`. systemd auto-starts it on boot and restarts it if it crashes.

### Daily commands

| Action | Command |
|---|---|
| Check status | `sudo systemctl status deltabot` |
| Start | `sudo systemctl start deltabot` |
| Stop | `sudo systemctl stop deltabot` |
| Restart (after editing code/.env) | `sudo systemctl restart deltabot` |
| Live logs (Ctrl+C to exit) | `sudo journalctl -u deltabot -f` |
| Last 100 log lines | `sudo journalctl -u deltabot -n 100 --no-pager` |
| Disable auto-start on boot | `sudo systemctl disable deltabot` |
| Re-enable auto-start | `sudo systemctl enable deltabot` |

> **Don't paste all four** "useful commands" at once — they include both `start` and `stop`, which cancel each other.

---

## 3. Updating from GitHub

```bash
cd ~/deltabot
git pull
sudo systemctl restart deltabot
```

If `git pull` says **"not a git repository"**, you're not inside the project folder — `cd ~/deltabot` first.

If the pull adds new dependencies:
```bash
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart deltabot
```

---

## 4. Editing the configuration

### `.env` (Telegram credentials only — never commit this)

```bash
cd ~/deltabot
nano .env
```

Format:
```
TELEGRAM_BOT_TOKEN=8649840050:AAH...
TELEGRAM_CHAT_ID=-1003935461524
```

After saving, **restart**:
```bash
sudo systemctl restart deltabot
```

### `config.py` (strategy parameters)

Common knobs:

| Constant | Meaning | Default |
|---|---|---|
| `STARTING_BALANCE` | USD paper-trade balance | `240.0` |
| `LEVERAGE` | Margin leverage | `25` |
| `FIXED_MARGIN_PER_TRADE` | $ margin per trade | `40.0` |
| `STOP_LOSS_PCT` | Distance from entry | `0.02` (2%) |
| `TAKE_PROFIT_PCT` | Distance from entry | `0.015` (1.5%) |
| `SCAN_INTERVAL` | Seconds between scans | `60` |
| `WEB_PORT` | Dashboard port | `5000` |

After editing: `sudo systemctl restart deltabot`.

---

## 5. The dashboard

Open <http://140.245.215.71:5000>

| Element | Purpose |
|---|---|
| **▶ START** | Launches both engines (Delta + MCX) |
| **■ STOP** | Stops both engines |
| **↺ RESET** | Wipes paper-trade history (bot must be stopped first) |
| **TELEGRAM** checkbox | Toggle Telegram alerts on/off |
| **Total card** (top-left) | Equity, P&L %, margin usage |
| **AI Screener** (left) | Filter coins by Bullish/Neutral/Bearish |
| **Strategy Performance** (left) | Per-strategy stats (Donchian / Gold / MCX-ORB) |
| **Top metrics row** | NAV, P&L, Sharpe, Vol, Drawdown, VaR |
| **Bottom panel sub-tabs** | Open Positions / Signals / Trade History / 🇮🇳 MCX (ORB) / Log |

---

## 6. Telegram

### Send a manual test

```bash
cd ~/deltabot
source .venv/bin/activate
python -c "from telegram_alert import send_startup_message; send_startup_message(99)"
```

### Common errors

| Error | Cause | Fix |
|---|---|---|
| `chat not found` | Wrong `TELEGRAM_CHAT_ID` or you never started a chat with the bot | For private chat: open the bot in Telegram → /start. For group: add bot to group, send message, find ID via `getUpdates` |
| `Unauthorized` | Wrong `TELEGRAM_BOT_TOKEN` | Verify with `https://api.telegram.org/bot<TOKEN>/getMe` |
| Messages stop arriving | Bot kicked from group, or chat ID changed when group → supergroup migration | Re-check chat ID with `getUpdates` |

### Get a chat ID quickly

In Telegram, search `@userinfobot` and start it — it replies with your user ID.

For a group: add the bot, send any message, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id` (group IDs are negative, supergroups start with `-100`).

---

## 7. Backtesting

### MCX ORB + Fib pivot

```bash
cd ~/deltabot
source .venv/bin/activate
python mcx_backtest.py MCX.NS
```

Multi-symbol:
```bash
python mcx_backtest.py MCX.NS GOLDBEES.NS SILVERBEES.NS
```

> yfinance caps 5-min data at ~60 days — for longer history you'd need a paid feed.

### Tuning knobs (top of `mcx_backtest.py`)

```python
MIN_RISK_PCT  = 0.003   # skip if SL too close
MAX_RISK_PCT  = 0.010   # skip if SL too wide
TP_NTH        = 1       # 1=nearest pivot as TP, 2=second-nearest
TREND_FILTER  = False   # only trade in direction of prev-day close vs PP
TRAIL_TO_BE   = False   # move SL to breakeven when intermediate pivot hits
```

The current best config: `MIN_RISK_PCT=0.003`, `MAX_RISK_PCT=0.010`, all others off.

---

## 8. Troubleshooting

### Service shows `inactive (dead)` in status

You probably ran `systemctl stop`. Just restart:
```bash
sudo systemctl start deltabot
```

### Can't reach dashboard from browser

Run on VM:
```bash
sudo systemctl status deltabot
curl -I http://localhost:5000/
```

If localhost works but public IP doesn't:
1. Check Oracle Console → **Networking → VCN → Subnet → Security List → Ingress Rules** has TCP port `5000` for `0.0.0.0/0`
2. Check VM iptables:
   ```bash
   sudo iptables -L INPUT -n --line-numbers
   ```
   Port 5000 ACCEPT must come **before** the REJECT rule.

### Bot opens trades but no Telegram messages

- Was the **TELEGRAM** checkbox checked when you clicked START? Restart bot from dashboard with it checked.
- Run the manual Telegram test (Section 6).

### Stale data in dashboard

Hard-refresh browser: `Ctrl+F5` (Windows) / `Cmd+Shift+R` (Mac).

### Reset the paper-trade account

```bash
sudo systemctl stop deltabot
rm ~/deltabot/ft_state.json
sudo systemctl start deltabot
```

Or click **↺ RESET** on the dashboard (bot must be stopped first via ■ STOP).

### Out of disk

```bash
df -h
sudo journalctl --vacuum-time=7d   # keep last 7 days of logs
sudo apt clean
```

### Out of RAM (E2.1.Micro is only 1 GB)

Monitor:
```bash
free -h
top
```

If memory pressure is high, either upgrade shape or reduce concurrency. The bot itself uses ~80 MB.

---

## 9. Stopping / starting the VM

### Web Console

**Compute → Instances → deltabot → Stop / Start**

⚠️ When stopped + restarted, the **ephemeral public IP changes**. Reconnect with the new IP.

To prevent this, in **Networking → IPv4 Addresses → Edit primary IP**, change the public IP type from "Ephemeral" to **"Reserved"** — this gives you a permanent free IP (one is always free per tenancy).

---

## 10. Backup & restore

### Backup

```bash
cd ~/deltabot
tar czf ~/deltabot-backup-$(date +%F).tgz .env ft_state.json bt_state.json
```

Copy off the VM:
```powershell
# from your Windows PC
scp -i $HOME\.ssh\oracle.key ubuntu@140.245.215.71:/home/ubuntu/deltabot-backup-*.tgz .
```

### Restore

Upload the tarball, then:
```bash
cd ~/deltabot
tar xzf ~/deltabot-backup-YYYY-MM-DD.tgz
sudo systemctl restart deltabot
```

---

## 11. Security checklist

- [ ] **Never commit `.env`** to GitHub. Confirm with `git check-ignore .env`.
- [ ] **Rotate the Telegram token** if it was ever public (BotFather → `/revoke`).
- [ ] Keep the SSH key private (`oracle.key`). If lost, generate a new one in Oracle Console.
- [ ] Repo can stay private on GitHub for safety even though no secrets are inside.
- [ ] Open only the ports you need (currently 22 SSH + 5000 Dashboard).

---

## 12. Going further

- **HTTPS for the dashboard** — front the Flask app with nginx + Let's Encrypt (`sudo apt install nginx certbot python3-certbot-nginx`).
- **Reserved IP** — so the dashboard URL never changes.
- **Production WSGI** — replace Flask's dev server with `gunicorn`:
  ```bash
  pip install gunicorn
  # change ExecStart in /etc/systemd/system/deltabot.service to:
  # ExecStart=/home/ubuntu/deltabot/.venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 web_app:app
  sudo systemctl daemon-reload
  sudo systemctl restart deltabot
  ```
- **Cloudflare Tunnel** — expose the dashboard with a free `*.trycloudflare.com` URL without opening any ports.
