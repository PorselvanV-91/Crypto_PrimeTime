# Prime Time — BTC 15-minute momentum paper trader

Separate paper-trading experiment using public Delta India BTCUSD candles.
The original `C:\Next Drive\Codex\Crypto\Old` directory is untouched.
This application cannot submit exchange orders.

## Strategy

1. Start checking candles **16:30 IST during US daylight saving**, **17:30 IST
   during standard time**, anchored to 07:00 `America/New_York`. Scheduling works
   on an AWS server in UTC; there are no fixed UTC offsets.
2. Evaluate **completed 15-minute candles**. Movement is close minus open,
   not high minus low. **Strictly above +200 points** signals BUY;
   **strictly below −200 points** signals SELL. Exactly ±200 does not qualify.
3. Model entry at the **next candle's reported opening price**, with adverse
   slippage. One open position at a time. Later trades may follow completed
   trades; signals while holding cannot open overlapping positions.
4. Target **200 price points from the modeled fill**. **No stop-loss**.
   Monitor targets during developing candles. Otherwise exit at the closing
   price of the **fourth 15-minute candle after entry** (entry candle counts as
   candle one), adjusted for slippage.
5. Last eligible signal: **21:00–21:15 IST**. Its entry is at 21:15 and can run
   until **22:15 IST**. The service keeps running for the next day.

Example: the 17:30 candle opens at 100,000 and closes at 100,250 at 17:45.
BUY uses the 17:45 candle's open. If that is 100,270, default slippage models
entry at 100,275 and target at 100,475. Without a target hit, exit at the 18:45
close of the 18:30–18:45 candle.

Crypto weekends and US holidays are included by default. `weekdays_only` can
omit weekends. This evening window is independent of the US 09:30 stock open.

## Run locally (PowerShell)

The local environment and private `.env` are already prepared. Telegram's helper
was adapted from `Old/telegram_config.py`; the existing token/chat ID were copied
into ignored `.env`, not Python source or Git.

```powershell
.\.venv\Scripts\python.exe -m prime_time paper
```

Keep the command running for live paper trading. Ctrl+C stops it gracefully.
Restarting resumes the saved position without inventing entries from old signals.

```powershell
.\.venv\Scripts\python.exe -m prime_time status
.\.venv\Scripts\python.exe -m prime_time export
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Fresh checkout setup (Python 3.11+):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env with your Telegram token/chat ID.
```

Settings are in `paper_config.json`. Default modeled quantity: **0.001 BTC**;
fees: **5 basis points per side**; slippage: **5 points per side**. These are
assumptions, not actual contract sizing or verified fee rates. Logged net points
and USD P&L include these costs. A 200-point target means gross price movement,
not 200 net profit points.

## Timing, fills and recovery

The runner polls every two seconds and waits two seconds after a candle boundary
for closure confirmation. It uses completed candles rather than a five-second
early prediction that could reverse before closure.

Next-open entry is a **paper model**, not a guaranteed executable live fill:
the opening print has already occurred when the previous close is confirmed.
We model that open plus slippage and record the observed entry delay. Signals
observed more than 20 seconds after the next open are skipped, including after
outages. Missed new trades are never filled retrospectively.

Targets use developing candle high/low. Exact touch times are unknown; exit
observation time records when polling discovered the touch. The exit alert
labels a developing candle snapshot; holding/timeout alerts contain closed
candle values. Replay discovers targets at candle closure, so it cannot model
live intrabar latency. Optional stops assume stop first if both levels are hit.

Existing positions recover using intervening candle history, including target
hits during downtime. Missing bars while holding pause processing rather than
guessing an exit; errors appear in the service log. Historical candle timestamps
and the recovery observation timestamp are stored separately.

Funding, dynamic spreads, order books, liquidation and margin are not modeled.
Without a stop-loss, losses can exceed 200 points before the timed exit.

## Telegram and logs

Alerts are sent on entry, each completed candle while holding, and exit. They
include trade ID, direction, prices, candle OHLC/body movement, IST timestamps,
and exit reason/net result when closed. Every scanned closed candle and decision
is logged. A target hit during a candle produces an immediate polling alert
with that developing candle's snapshot.

`runtime/paper.sqlite3` saves positions, candles, events, trades and a Telegram
outbox. Temporary delivery failures retry without losing trades. Credentials
come from `.env` or environment variables (environment wins). Without credentials,
alerts remain queued. Delivery is at least once: a crash after Telegram accepts
a message but before recording success can duplicate an alert. Trade IDs are stable.

`export` writes `reports/paper/`: `trade_analysis.csv` (flat trade rows),
`trades.csv`, `candles.csv`, `events.csv`, `outbox.csv`, and `summary.json`.
The complete configuration is saved per experiment. Use a new database path
when changing strategy settings.

## Historical replay

The local historical CSV is excluded from Git. It contains Binance BTCUSDT data
from July 2025–January 2026, a proxy for the live Delta BTCUSD contract.

```powershell
.\.venv\Scripts\python.exe -m prime_time replay --data data/BTC_5m.csv
```

Replay aggregates complete 5-minute groups into 15-minute bars and uses the same
engine. It never exposes the next candle's future OHLC at entry or sends Telegram
alerts. Replays require a fresh database; for another run:

```powershell
.\.venv\Scripts\python.exe -m prime_time replay --database reports/run2/replay.sqlite3 --output reports/run2
```

The supplied historical data with default settings produced **494 closed trades,
314 target hits, 64.78% net win rate, and −68,246.29 net points** (−68.2463 USD
at the modeled 0.001 BTC quantity). High target-hit frequency did not offset
large timed-exit losses and costs. This is a paper research experiment, not
evidence of a profitable strategy.

## Source and AWS

```text
prime_time/        Source package: engine, polling, API, Telegram, SQLite, replay
tests/            Deterministic strategy, recovery, timing and cost tests
deploy/           AWS Ubuntu systemd unit and deployment instructions
docs/             Earlier opening-range baseline notes
paper_config.json Active momentum paper strategy configuration
config.json       Earlier opening-range research configuration
.env.example      Credential template; actual .env stays private
runtime/          Database and rotating logs (ignored)
data/             Historical CSV (ignored)
reports/          Generated exports (ignored)
```

See [AWS deployment](deploy/AWS.md). The server only needs this repository,
Python dependencies and Telegram settings. No exchange API key or historical
CSV is needed for live paper trading. The earlier `backtest` and `session`
commands remain separate research tools; see [baseline notes](docs/OPENING_RANGE_BASELINE.md).

API references: [Delta candles](https://docs.delta.exchange/#get-historical-ohlc-candles),
[Telegram Bot API](https://core.telegram.org/bots/api#sendmessage).
