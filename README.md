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
   can generate a new signal. In default quote mode, BUY confirmation/fill uses
   the current ask; SELL uses the current bid. Adverse slippage is added.
   One pending entry or position at a time.
4. Target **200 price points from the modeled fill**. A separate **500-point
   protective price stop** is checked on every poll: BUY stop = filled entry −500;
   SELL stop = filled entry +500. It does not wait for candle closure. The
   additional opposing-candle exit remains: exit a BUY if a completed
   candle's close minus open is **−500 points or less**; exit a SELL if it is
   **+500 points or more**. This stop uses the candle body, not a wick or cumulative
   loss from entry. Request an exit at closure; quote mode executes on the next
   fresh bid/ask, rather than inventing a fill at the historical close. Targets
   executed before closure have already closed the position.
   Monitor targets during developing candles. Otherwise exit at the closing
   price of the **fourth 15-minute candle after entry** (entry candle counts as
   candle one), adjusted for slippage.
5. Last eligible signal: **21:00–21:15 IST**. Its entry is at 21:15 and can run
   until **22:15 IST**. The service keeps running for the next day.

## Execution modes

All quote-mode expiry paths use **"no fresh executable trigger observed"**.
This does not claim that an unobserved crossing was impossible during missing
or rejected quote data. Baseline trading rules remain unchanged.

The default `paper_config.json` uses **`execution_mode: "quote"`** and
`runtime/paper_quote_v4.sqlite3`. A BUY trigger is confirmed by fresh ask >=
trigger, a SELL by fresh bid <= trigger. BUY entry fills at observed ask plus
slippage, SELL at bid minus slippage. Long exits use bid minus slippage; short
exits use ask plus slippage. Profit target and protective stop are confirmed
using those executable exit sides, not the candle's earlier high/low. Modeled
extra slippage can make a target exit net less than the target distance.

For comparison, `paper_candle_config.json` selects **`candle_touch`** with
`runtime/paper_candle_v4.sqlite3`. This preserves the earlier candle-touch model,
including trigger/open fills and historical recovery. Telegram messages, trade
records, status and exports identify the execution mode. These modes represent
separate experiments and must not share a database.

## Signal evidence and optional VWAP-side variant

Every eligible closed signal candle logs session VWAP, prior VWAP, VWAP change
in points, signal close relative to VWAP, volume, and relative volume. VWAP uses
**HLC3 × volume**, resetting at **UTC midnight** by default. Configure
`vwap_session_timezone` to match your chart's actual session reset; a chart's
display timezone alone does not establish its VWAP reset. Missing bars from
session start make VWAP unavailable rather than silently calculating a partial
session. Zero volume and insufficient warmup are explicit.

`relative_volume_period: 20` means signal volume divided by the arithmetic
average of the **previous 20 completed bars**, excluding the signal candle.
Developing/future candles are excluded from all signal features. VWAP change is
current session VWAP minus its prior closed-bar value; it is logged, not gated.
Warmup requests include the full session and preceding volume lookback.

`paper_trend_config.json` runs the separate **QUOTE/vwap_side** variant using
`runtime/paper_quote_trend_v1.sqlite3`. It requires BUY signal close > session
VWAP, or SELL close < VWAP. Equality or unavailable VWAP rejects the signal with
a logged reason. Stops, target, timing, buffer and fees stay identical. Relative
volume and VWAP slope remain diagnostic only, avoiding several simultaneous
strategy changes. The default `paper_config.json` keeps `trend_filter: off`.

Armed/entered Telegram alerts include VWAP/trend/RVOL context; pending entries
and trades retain the original signal context even as later candles change.
`export` includes `signal_analysis.csv` (flat rows with decisions) and
`signal_contexts.csv`. Filter rejections are recorded as `SIGNAL_REJECTED`
events. Labels include both execution mode and strategy, for example
`QUOTE/baseline` and `QUOTE/vwap_side`.

The logging-only defaults can be added to an existing v4 baseline database on
restart. This does not rewrite existing trade snapshots or change its entry/exit
rules. Enabling the filter or changing thresholds/session settings still needs
a separate database.

Test the variants on historical OHLC proxy data:

```powershell
.\.venv\Scripts\python.exe -m prime_time.compare_trend --data data/BTC_5m.csv --output reports/trend_comparison
```

This runs matched **candle-touch** baseline and VWAP-filtered simulations and
exports separate databases, trades, features and `comparison.json`. Each run
requires a new output directory. It does not reproduce quote fills or provide
out-of-sample validation. The supplied July 2025–January 2026 proxy produced:

| Candle-touch test | Trades | Net points | Average net points/trade |
| --- | ---: | ---: | ---: |
| Baseline | 393 | -56,282.18 | -143.21 |
| VWAP-side filter | 301 | -47,380.88 | -157.41 |

Both lost after configured costs. The filtered version traded less and had a
worse per-trade average, so lower total loss is not proof of a better edge.
Compare the separate quote-mode variants prospectively before drawing conclusions.

Candle-touch example: the 17:30 candle opens at 100,000, reaches a high of 100,300 and closes
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
Persisted pending entries survive restarts. In quote mode, missed quote
crossings and historical candle touches are never recovered as executions.
An order fills only when a fresh quote is actually observed while its entry
candle is still valid. `entry_time` identifies that candle's opening;
`entry_fill_observed_at` and `entry_quote_exchange_timestamp` identify the
observation and exchange update used for execution. The partially held entry
candle still counts as the first of four candles.

Targets, stops and all other quote-mode exits use a fresh executable quote.
Timeout and opposing-candle signals create a durable exit request. If quotes
are missing/stale, that request waits across polls/restarts; it fills using the
next valid observed quote. There is no fallback to candle close or a historical
target. Thus an exit can occur after its planned deadline during a feed outage.
Quote stops remain active when the candle endpoint fails; those exits explicitly
label a quote-only synthetic candle snapshot instead of inventing market OHLC.

In candle-touch mode, exits still use candle high/low and historical bar closes.
If a candle contains both target and protective stop, assume stop first and
record the ambiguity. Pre-entry wicks are excluded from live intrabar stop
checks when a later price/extreme is needed to prove a new crossing. Coarse
replay cannot reconstruct that sequence and records conservative assumptions.

Both HTTP feeds record request start, receipt, monotonic request duration,
HTTP server Date, cache Age, and any exchange update timestamp. Ticker timestamps
are normalized from seconds/milliseconds/microseconds/nanoseconds or zoned ISO
while preserving raw values. Quote validation checks symbol/prices, exchange
age, receipt age, future clock skew, latency, cache age, and timestamp regression
across restarts. Defaults: maximum quote age 10 seconds, request latency 5 seconds,
future clock tolerance 2 seconds. These thresholds are configurable.

Candle history supplies **bar-opening time**, not the last update time of its
values. We validate alignment, future/missing recent bars, request/receipt age
and HTTP/cache timestamps, and any separate update timestamp actually supplied.
A normal response is labeled `fresh_response_update_time_unknown`; it does not
prove the exchange's underlying candle values are current. Missing candle
server/update timestamps block new quote-mode entries. Never infer feed latency
by subtracting a candle's opening time from now. Current API headers can be cached;
requesting no-cache does not guarantee the server honors it.

Entry/exit audits include validation decisions, normalized exchange time, receipt
time, bid/ask/spread, latency, candle evidence, fill model and slippage. Every
live poll is saved in `feed_snapshots`, including rejected/missing feeds. Telegram
and the rotating log identify execution mode and feed-health transitions. Feed
snapshot history grows over time; retain/export/archive the experiment database
according to your disk capacity. No historical replay quote is invented.

Funding, market depth, liquidation and margin are not modeled. Observed top-of-book
quotes are still paper fills and do not guarantee available size or real fills.
The 500-point protective level is not a guaranteed loss cap: gaps, polling/feed
outages, slippage and fees can increase losses. In quote mode a stale feed pauses
execution rather than claiming an unavailable protective fill.

## Telegram and logs

Alerts are sent when a trigger is armed or expires, on entry, each completed candle while holding, and exit. They
include trade ID, direction, prices, candle OHLC/body movement, IST timestamps,
and exit reason/net result when closed. Every scanned closed candle and decision
is logged. A target hit during a candle produces an immediate polling alert
with that developing candle's snapshot.

`runtime/paper_quote_v4.sqlite3` saves quote-mode positions, pending triggers, candles, events, trades and a Telegram
outbox. Temporary delivery failures retry without losing trades. Credentials
come from environment variables, `.env`, or private `config.py` (in that order). Without credentials,
alerts remain queued. Delivery is at least once: a crash after Telegram accepts
a message but before recording success can duplicate an alert. Trade IDs are stable.

`export` writes `reports/paper/`: `trade_analysis.csv` (flat trade rows),
`trades.csv`, `candles.csv`, `events.csv`, `outbox.csv`, `feed_snapshots.csv`, and `summary.json`.
The complete configuration is saved per experiment. Use a new database path
when changing strategy settings.

## Historical replay

The local historical CSV is excluded from Git. It contains Binance BTCUSDT data
from July 2025–January 2026, a proxy for the live Delta BTCUSD contract.

```powershell
.\.venv\Scripts\python.exe -m prime_time replay --config paper_candle_config.json --data data/BTC_5m.csv
```

OHLC-only replay is supported only for candle-touch mode, not quote execution.
Replay aggregates complete 5-minute groups into 15-minute bars and uses the same
engine. It never exposes the next candle's future OHLC at entry or sends Telegram
alerts. Replays require a fresh database; for another run:

```powershell
.\.venv\Scripts\python.exe -m prime_time replay --config paper_candle_config.json --database reports/run2/replay.sqlite3 --output reports/run2
```

The earlier next-open strategy (before buffered entry and the opposite-body stop)
on the supplied historical data produced **494 closed trades,
314 target hits, 64.78% net win rate, and −68,246.29 net points** (−68.2463 USD
at the modeled 0.001 BTC quantity). High target-hit frequency did not offset
large timed-exit losses and costs. This is a paper research experiment, not
evidence of a profitable strategy.

The quote-mode defaults use a fresh `runtime/paper_quote_v4.sqlite3` database. Finish the
old experiment before starting the new one; its database remains available for
analysis with `status --database runtime/paper_v3.sqlite3`. To reproduce the earlier
entry behavior, use `entry_buffer_points: null` and
`opposite_candle_stop_points: null` and `stop_points: null` in a separate experiment configuration.

With buffered entry and opposite-body stop, before the protective-stop update, the same historical
proxy data produced **376 trades, 269 target hits, 71.54% net win rate, and
−33,877.80 net points** (−33.8778 USD at 0.001 BTC). This is still negative
after the configured costs; it is not an independently validated trading edge.

In candle-touch mode with the 500-point protective price stop, the same coarse historical
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
paper_config.json Quote-mode configuration
paper_candle_config.json Candle-touch comparison configuration
paper_trend_config.json Optional VWAP-side quote variant
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
