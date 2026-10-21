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
3. Arm a breakout entry for the **next candle only**: BUY at the signal candle's
   **high + 30 points**; SELL at its **low − 30 points**. Enter only when touched.
   An unfilled trigger expires at that candle's close, and that closed candle
   can generate a new signal. Gaps beyond a trigger fill at the worse opening
   price. Adverse slippage is added. One pending entry or position at a time.
4. Target **200 price points from the modeled fill**. A separate **500-point
   protective price stop** is checked on every poll: BUY stop = filled entry −500;
   SELL stop = filled entry +500. It does not wait for candle closure. The
   additional opposing-candle exit remains: exit a BUY if a completed
   candle's close minus open is **−500 points or less**; exit a SELL if it is
   **+500 points or more**. This stop uses the candle body, not a wick or cumulative
   loss from entry. Model the exit at that close with adverse slippage. A target
   reached earlier in the candle takes priority over this close-only stop.
   Monitor targets during developing candles. Otherwise exit at the closing
   price of the **fourth 15-minute candle after entry** (entry candle counts as
   candle one), adjusted for slippage.
5. Last eligible signal: **21:00–21:15 IST**. Its entry is at 21:15 and can run
   until **22:15 IST**. The service keeps running for the next day.

Example: the 17:30 candle opens at 100,000, reaches a high of 100,300 and closes
at 100,250. Arm BUY at 100,330 for the 17:45–18:00 candle. If price touches it,
default slippage models entry at 100,335 and target at 100,535. If it gaps open
at 100,400, entry instead models 100,405. If no trigger touch occurs before
18:00, no trade opens. A filled trade exits by 18:45 if neither target nor
either stop occurs first; the partially held entry candle counts as one.

Crypto weekends and US holidays are included by default. `weekdays_only` can
omit weekends. This evening window is independent of the US 09:30 stock open.

## Run locally (PowerShell)

The local environment and private `.env` are already prepared. Telegram's helper
was adapted from `Old/telegram_config.py`. For AWS/tmux, manually copy private
`config.py` beside `paper_config.json` with `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
Use `config.example.py` as a template. Actual `config.py` and `.env` are ignored
by Git. Nonempty environment/.env values take priority over `config.py`.

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

Triggered entry is a **paper model**, not a guaranteed executable live fill.
Signals must be observed within 20 seconds of the next candle opening to arm
an entry; late new signals are skipped. Once armed, the trigger can fill at any
time during that next candle. Trigger and fill/observation details are logged.
Persisted pending entries survive restarts. Recovering an already armed entry
uses historical candles, but missed signals are never armed retrospectively.
`entry_time` identifies the entry candle's opening time; an intrabar trigger's
exact touch time is unknown and is labeled as such in the trade record.

Targets use developing candle high/low. Exact touch times are unknown; exit
observation time records when polling discovered the touch. The exit alert
labels a developing candle snapshot; holding/timeout alerts contain closed
candle values. Replay discovers targets at candle closure, so it cannot model
live intrabar latency. If candle data shows both protective stop and target,
assume stop first and record the ambiguity. A pre-entry wick in a developing
entry candle is excluded from the live protective-stop test unless a later
new extreme or observed price confirms a stop crossing. Historical replay
cannot reconstruct that sequence and labels conservative entry/stop ambiguity.

Every live poll also requests the public Delta bid/ask ticker for audit evidence.
Entry records include `entry_candle_snapshot`, `entry_audit` (receipt time, quote
status, bid/ask, spread, request latency and raw exchange timestamp), fill model,
slippage, and the signed difference between the modeled fill and observed ask
for BUY or bid for SELL. Positive difference means a worse modeled fill. Quotes
are contextual evidence: entries remain modeled trigger/open plus slippage,
not actual exchange executions. Quote and candle requests are separate snapshots.
Only matching current-candle quotes received within ten seconds are attached;
exchange timestamps are preserved raw and are not a guarantee of feed freshness.
Missing/failed quotes are explicit and do not disable candle-based stop management.
Historical recovery/replay never attaches today's quote to an old fill.

Existing positions recover using intervening candle history, including target
hits during downtime. Missing bars while holding pause processing rather than
guessing an exit; errors appear in the service log. Historical candle timestamps
and the recovery observation timestamp are stored separately.

Funding, dynamic spreads, order books, liquidation and margin are not modeled.
The opposing-body stop waits for closure, while `stop_points: 500` supplies an
independent protective exit during the candle. It is a paper price trigger, not
a guaranteed 500-point loss cap: gaps, polling delays, slippage and fees can
increase losses. Current bid (long exits) or ask (short exits) is used when worse
than the protective trigger; later candle opening gaps also fill adversely.

## Telegram and logs

Alerts are sent when a trigger is armed or expires, on entry, each completed candle while holding, and exit. They
include trade ID, direction, prices, candle OHLC/body movement, IST timestamps,
and exit reason/net result when closed. Every scanned closed candle and decision
is logged. A target hit during a candle produces an immediate polling alert
with that developing candle's snapshot.

`runtime/paper_v3.sqlite3` saves positions, pending triggers, candles, events, trades and a Telegram
outbox. Temporary delivery failures retry without losing trades. Credentials
come from environment variables, `.env`, or private `config.py` (in that order). Without credentials,
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

The earlier next-open strategy (before buffered entry and the opposite-body stop)
on the supplied historical data produced **494 closed trades,
314 target hits, 64.78% net win rate, and −68,246.29 net points** (−68.2463 USD
at the modeled 0.001 BTC quantity). High target-hit frequency did not offset
large timed-exit losses and costs. This is a paper research experiment, not
evidence of a profitable strategy.

The protective-stop defaults use a fresh `runtime/paper_v3.sqlite3` database. Finish the
old experiment before starting the new one; its database remains available for
analysis with `status --database runtime/paper_v2.sqlite3`. To reproduce the earlier
entry behavior, use `entry_buffer_points: null` and
`opposite_candle_stop_points: null` and `stop_points: null` in a separate experiment configuration.

With buffered entry and opposite-body stop, before the protective-stop update, the same historical
proxy data produced **376 trades, 269 target hits, 71.54% net win rate, and
−33,877.80 net points** (−33.8778 USD at 0.001 BTC). This is still negative
after the configured costs; it is not an independently validated trading edge.

With the additional 500-point protective price stop, the same coarse historical
replay produced **393 trades, 241 target hits, and −56,282.18 net points**.
The conservative stop-first rule cannot reconstruct intrabar ordering; this
result is not evidence that the new stop improves profitability. Live quote
and entry snapshots provide evidence for evaluating fills in the next paper run.

## Source and AWS

```text
prime_time/        Source package: engine, polling, API, Telegram, SQLite, replay
tests/            Deterministic strategy, recovery, timing and cost tests
deploy/           AWS Ubuntu systemd unit and deployment instructions
docs/             Earlier opening-range baseline notes
paper_config.json Active momentum paper strategy configuration
config.json       Earlier opening-range research configuration
.env.example      Credential template; actual .env stays private
config.example.py Private config.py template; actual config.py stays private
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
