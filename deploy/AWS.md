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
tail -f runtime/paper_quote_v4.log
```

Exports appear in `reports/paper/trade_analysis.csv`. Do not run systemd and tmux
against the same database simultaneously. To update, stop with Ctrl+C, run
`git pull --ff-only` and `python -m pip install -r requirements.txt`, then start
the runner again. Keep `runtime/` for recovery. Finish the old experiment before
switching to the new strategy database.

Only `config.py` needs manual copying for a fresh live paper instance. Historical
CSV files and models are not needed. Copy a database only when intentionally
migrating an experiment, after stopping the old runner.

## Quote mode and candle-touch comparison

Default `python -m prime_time paper` now runs **QUOTE** mode with
`paper_config.json` and a fresh `runtime/paper_quote_v4.sqlite3` database.
Finish any existing v3 paper position before pulling/upgrading, then stop that
runner with Ctrl+C, pull the code, and restart in the same tmux session.
No additional packages, Telegram settings or exchange keys are required.

An optional independent candle-touch comparison can run in a second tmux session:

```bash
tmux new -s prime-candle
cd ~/Crypto_PrimeTime
source ~/botenv/bin/activate
python -m prime_time paper --config paper_candle_config.json
```

It uses `runtime/paper_candle_v4.sqlite3` and `runtime/paper_candle_v4.log`.
Messages include execution mode and strategy, such as `[QUOTE/baseline]` or
`[CANDLE_TOUCH/baseline]`. Do not run both modes against
one database. Inspect/export the comparison independently:

```bash
python -m prime_time status --database runtime/paper_candle_v4.sqlite3
python -m prime_time export --database runtime/paper_candle_v4.sqlite3 --output reports/candle
```

Quote mode buys at observed ask + slippage, sells at observed bid - slippage,
and reverses sides to exit. It never executes from a candle-only touch. Stale,
future, regressing, missing or slow quotes block execution. Timeouts/opposing
candles request an exit, which waits for the next valid quote if the feed is
unavailable. Candle endpoint failure does not prevent an exit on a fresh quote.
Past unobserved target touches during downtime do not count as quote fills.

`status` includes feed health. `feed_snapshots.csv` in exports records per-poll
request/receipt times, HTTP/cache metadata, normalized quote exchange time,
candle-opening time semantics, freshness validation and rejection reasons.
Candle history has no last-update timestamp for its OHLC values; a fresh HTTP
response is labeled `fresh_response_update_time_unknown`, not proof that the
underlying candle values are current. Snapshot history grows with each poll.

Keep the server clock synchronized. Default quote/response age limits are 10s,
request latency 5s, future skew tolerance 2s. These are in both config files.
OHLC-only replay requires `--config paper_candle_config.json`; quote fills need
actual timestamped quote observations and cannot be reconstructed from OHLC.

## Optional VWAP-side experiment in tmux

The existing baseline now logs VWAP/trend and relative volume without changing
its entry/exit rules. Its v4 database accepts the added logging-only defaults on
restart. Pull the update after stopping the runner with Ctrl+C, then restart
`python -m prime_time paper` in its existing tmux session. Private `config.py`
stays unchanged. Add a separate session for the filtered experiment:

```bash
tmux new -s prime-trend
cd ~/Crypto_PrimeTime
source ~/botenv/bin/activate
python -m prime_time paper --config paper_trend_config.json
```

It uses `runtime/paper_quote_trend_v1.sqlite3` and
`runtime/paper_quote_trend_v1.log`. Baseline remains
`runtime/paper_quote_v4.sqlite3`. Telegram labels identify `QUOTE/baseline` or
`QUOTE/vwap_side`. The variant only allows BUY closes above session VWAP and
SELL closes below it; VWAP slope and relative volume are logged, not filtered.
Missing session history prevents the variant from arming entries.

```bash
python -m prime_time status --database runtime/paper_quote_trend_v1.sqlite3
python -m prime_time export --database runtime/paper_quote_trend_v1.sqlite3 --output reports/trend
```

Inspect `signal_analysis.csv` for VWAP, its delta, close-to-VWAP distance,
relative volume against the prior 20 closed candles, and rejection decisions.
Session VWAP resets at UTC midnight; change `vwap_session_timezone` only in a
new experiment if your chart uses another anchor. Check `vwap_status` and
`relative_volume_status` for warmup or missing-data limitations. The runner
requests warmup automatically; no model files or additional packages are needed.

With an optional historical CSV copied locally, reproduce the paired candle proxy:

```bash
python -m prime_time.compare_trend --data data/BTC_5m.csv --output reports/trend_comparison
```

This is not quote-mode backtesting. The historical test remained negative in
both variants, so the filter remains experimental and off in the baseline.

## Held-candle diagnostic update

No new settings, dependencies or database paths are needed. Stop each running
tmux bot with Ctrl+C, run `git pull --ff-only`, then restart it with its original
config. Open paper positions resume from the same database. New diagnostic
columns start collecting from that restart; earlier quote extrema are not
backfilled from candle data. Strategy behavior remains unchanged.

Every held-candle alert includes closed-bar VWAP, current close-now estimated
net P&L, and sampled executable-quote MFE/MAE. Long marks use bid, short marks
use ask; net marks include exit slippage and both fees. Stale/missing quotes show
the mark as unavailable. The diagnostic includes its delay after candle close;
recovery observations must not be read as historical close prices.

Use the existing export commands to obtain `hold_analysis.csv` and
`hold_diagnostics.csv`. Final trade rows retain observed excursion summaries.
MFE/MAE use only valid quotes seen while holding; quote gaps and intrabar moves
between polls are not reconstructed. Coverage is marked partial for a position
opened before diagnostic logging was installed.

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

`runtime/paper_quote_v4.sqlite3` is the source of truth for default quote mode.
It contains the position, pending trigger, completed
candles, decisions, entry/hold/exit events, trade records, and Telegram delivery
status. CSV exports include `trade_analysis.csv`, with one row per trade.
`runtime/paper_quote_v4.log` rotates at 5 MB with five backups; journald also captures logs.
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
`/opt/prime-time/runtime/`. The updated `stop_points: 500` exits on an adverse
500-point move from filled entry during the candle, alongside the existing
opposing-body stop at candle closure. Protective stops and richer fill evidence
use the fresh `paper_quote_v4.sqlite3` database; finish the old experiment before
updating so its position is not left unmanaged. Inspect the old run with
`python -m prime_time status --database runtime/paper_v3.sqlite3` before updating.
No dependency changes or extra credentials are needed. Quote evidence comes
from a public ticker endpoint; failed quote requests are logged and quote-mode
execution waits for fresh data. Export includes entry snapshots, quotes and fill
model evidence. Protective stops can fill worse during gaps or polling delays.

Telegram is retried from the durable outbox after temporary failures. Delivery
is at least once: a crash between Telegram accepting a message and the database
marking it sent can duplicate that alert. Each message includes a stable trade ID.
If credentials are missing, alerts remain queued until credentials are configured.
Replay databases are separate and never flushed to Telegram automatically.
