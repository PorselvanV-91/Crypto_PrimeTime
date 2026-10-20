# AWS deployment (Ubuntu EC2/Lightsail, Python 3.11+)

The service only reads public Delta market data and sends Telegram messages.
No exchange API key, inbound application port, or real order capability is needed.
Allow outbound HTTPS and enable time synchronization on the server.

## tmux with your existing ~/botenv

Use your normal SSH user; no new environment or service account is needed.

```bash
cd ~
git clone https://github.com/PorselvanV-91/Crypto_PrimeTime.git
cd ~/Crypto_PrimeTime
source ~/botenv/bin/activate
python --version  # Python 3.11 or newer
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For an existing checkout, use `cd ~/Crypto_PrimeTime` then `git pull --ff-only`
instead of cloning again. Copy private `config.py` into this directory,
beside `paper_config.json`, containing:

```python
TELEGRAM_TOKEN = "your_bot_token"
TELEGRAM_CHAT_ID = "your_chat_id"
```

Or copy `config.example.py` to `config.py` and edit it. Actual `config.py` is
ignored by Git. `paper_config.json` contains strategy settings, separately.
Nonempty environment variables (including values loaded from `.env`) override
`config.py`; remove stale overrides if you want to use only your copied file.

```bash
chmod 600 config.py
tmux new -s prime-time
```

Inside the new tmux session:

```bash
cd ~/Crypto_PrimeTime
source ~/botenv/bin/activate
python -m prime_time paper
```

Startup should say `Telegram enabled`. This confirms credentials were loaded,
not that Telegram accepted a message. Alerts start with a qualifying setup;
there is no startup message. Detach with **Ctrl+B**, release, then **D**.
Disconnect SSH freely; reconnect with `tmux attach -t prime-time`.
Use **Ctrl+C inside the session** to stop gracefully. tmux survives SSH
disconnects, but does not restart the bot after a crash or server reboot.

From another SSH terminal:

```bash
cd ~/Crypto_PrimeTime
source ~/botenv/bin/activate
python -m prime_time status
python -m prime_time export
tail -f runtime/paper.log
```

Exports appear in `reports/paper/trade_analysis.csv`. Do not run systemd and tmux
against the same database simultaneously. To update, stop with Ctrl+C, run
`git pull --ff-only` and `python -m pip install -r requirements.txt`, then start
the runner again. Keep `runtime/` for recovery. Finish the old experiment before
switching to the new strategy database.

Only `config.py` needs manual copying for a fresh live paper instance. Historical
CSV files and models are not needed. Copy a database only when intentionally
migrating an experiment, after stopping the old runner.

## Alternative: systemd checkout and setup

Push this repository to your own Git remote, then use that remote URL below.
Keep `.env`, `.venv`, `runtime`, `reports`, and historical data outside Git.

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
sudo useradd --system --home /opt/prime-time --shell /usr/sbin/nologin prime-time
sudo git clone https://github.com/PorselvanV-91/Crypto_PrimeTime.git /opt/prime-time
cd /opt/prime-time
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
sudo install -d -o prime-time -g prime-time -m 700 /opt/prime-time/runtime
sudo install -m 600 .env.example /etc/prime-time.env
sudoedit /etc/prime-time.env
# Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID from your existing Telegram bot.
sudo install -m 644 deploy/prime-time.service /etc/systemd/system/prime-time.service
sudo systemctl daemon-reload
sudo systemctl enable --now prime-time
sudo systemctl status prime-time
sudo journalctl -u prime-time -f
```

If `prime-time` already exists, reuse it. Use a deploy key or another supported
Git authentication method for a private repository. The checkout can remain
root-owned; the service only needs write access to `runtime/`.

The service runs continuously, but opens trades only in the configured IST window.
All scheduling uses timezone-aware timestamps and works with an AWS host in UTC.
The default includes crypto weekends and US holidays. Set `weekdays_only` true
to omit weekends (this flag does not implement a holiday calendar).

## Inspect and export

```bash
cd /opt/prime-time
sudo -u prime-time .venv/bin/python -m prime_time status
sudo -u prime-time .venv/bin/python -m prime_time export --output runtime/export
```

`runtime/paper_v2.sqlite3` is the source of truth for the updated strategy.
It contains the position, pending trigger, completed
candles, decisions, entry/hold/exit events, trade records, and Telegram delivery
status. CSV exports include `trade_analysis.csv`, with one row per trade.
`runtime/paper.log` rotates at 5 MB with five backups; journald also captures logs.
Protect and back up the database. For a consistent backup while running, use
SQLite's backup facility, or stop the service before copying the database.
Never delete the database to update code: that loses restart recovery state.

## Update code or change an experiment

```bash
sudo systemctl stop prime-time
cd /opt/prime-time
sudo git pull --ff-only
sudo .venv/bin/python -m pip install -r requirements.txt
sudo systemctl start prime-time
```

Configuration is saved in the database. Changing strategy settings requires a
new database path in `paper_config.json`, so experiments cannot silently mix.
Finish the existing paper position before switching to another database.
Systemd's default writable path requires any new database to remain under
`/opt/prime-time/runtime/`. Set `stop_points` to a positive value only if you
explicitly want to add a fixed-price stop as a separate experiment. The updated
defaults already exit on an opposing 500-point candle body at candle closure.
Buffered entry and that close-based stop use the fresh `paper_v2.sqlite3` database;
finish the old experiment before updating so its position is not left unmanaged.

Telegram is retried from the durable outbox after temporary failures. Delivery
is at least once: a crash between Telegram accepting a message and the database
marking it sent can duplicate that alert. Each message includes a stable trade ID.
If credentials are missing, alerts remain queued until credentials are configured.
Replay databases are separate and never flushed to Telegram automatically.
