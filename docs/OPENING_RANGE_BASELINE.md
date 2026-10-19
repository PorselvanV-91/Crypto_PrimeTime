# Prime Time

Separate BTC research project created from `C:\Next Drive\Codex\Crypto\Old`.
The original directory is untouched. Only `v2/data/BTC_5m.csv` was copied;
the strategy, calendar, configuration, and tests were written fresh. Old models
were trained for different targets and are not reused. Credentials, server keys,
Telegram configuration, caches, and duplicate bot versions were not copied.

## Run in PowerShell

The local `.venv` has the required timezone database installed. No activation needed:

```powershell
.\.venv\Scripts\python.exe -m prime_time session 2026-10-19
.\.venv\Scripts\python.exe -m prime_time backtest
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For a new machine, create an environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Starting logic

- Instrument: BTCUSDT; one point means one USD of BTC price movement per BTC.
- Anchor: US cash equity open at 09:30 America/New_York, automatically converted
  using IANA timezone data. This is 19:00 IST in US daylight saving and 20:00 IST
  in standard time. 17:30 IST is not the US cash open.
- Build the first 15-minute high/low from three completed 5-minute candles.
- Skip ranges outside 50–600 points. Enter long after a later candle closes
  above the range plus 10 points, or short below the range minus 10 points.
- Execute at the next candle's opening price with adverse slippage. Entries
  must occur within 90 minutes of the US open. One attempt per session.
- Target 220 points; stop 110 points; close after at most 30 minutes.
- Fees default to 5 basis points per side; slippage to 5 points per side.
  Adjust these research assumptions in `config.json` to match the actual venue.

These are explicit starting assumptions, not fitted or validated parameters.
The 220-point target is gross price movement, not a promise of 200 net points.
At BTC prices around $100,000, the default round-trip fee alone is about 100
points. Net points include fees and slippage; account P&L also depends on quantity.

## Data and execution assumptions

CSV columns: `timestamp,open,high,low,close,volume`. Timestamps describe bar
**opening** time. Naive timestamps are interpreted as UTC, matching the original
Binance collector; offset timestamps are converted to UTC. Bars must be sorted,
unique and aligned. Each session needs the full two-hour window; sessions with
missing candles are skipped and recorded in the audit CSV.

The copied data spans July 19, 2025 to January 15, 2026. It is historical, not
current. Use `--data PATH` for another file and `--output PATH` for another run.
Use closed candles only. Binance USDT data is a proxy, not Delta contract fills.

If target and stop both appear in one bar, the backtest assumes the stop hits
first. Stop gaps fill at the worse opening price. Exact intrabar exit times
cannot be recovered from OHLC; reports record the exit bar's opening timestamp.
No funding, spread variation, liquidity limits, contract sizing, leverage, or
liquidation model is included. There is no order execution or live API client.

The opening calendar covers 2025–2028, including scheduled full-day holidays
and the January 9, 2025 closure. Unsupported years fail explicitly. It supports
only the opening window, not a full exchange calendar or unscheduled closures.
The maximum window is limited to the 3.5-hour early-close session length.
Calendar source: [NYSE holidays and hours](https://www.nyse.com/trade/hours-calendars).

## Baseline result

With the supplied data and default config: 52 trades, 14 target hits, 26.9% net
win rate, **−6,990.04 net points per BTC**, and 6,990.04 points maximum drawdown.
This baseline is losing. It establishes a reproducible starting point for logic
development; it does not establish a profitable strategy.

Reports are written to `reports/baseline/`: `summary.json`, `trades.csv`, and
`sessions.csv`. The summary includes the effective configuration and data period.
Next research should compare breakout and failed-breakout behavior on finer
data, then validate any chosen rule on a separate untouched period with actual
venue costs before adding execution.
