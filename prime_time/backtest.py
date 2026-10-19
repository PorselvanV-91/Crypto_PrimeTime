"""Closed-bar opening-range breakout with next-bar execution."""
import csv
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from .session import NY, session_open


@dataclass(frozen=True)
class Config:
    symbol: str = 'BTCUSDT'
    bar_minutes: int = 5
    opening_range_minutes: int = 15
    entry_window_minutes: int = 90
    max_hold_minutes: int = 30
    target_points: float = 220
    stop_points: float = 110
    breakout_buffer_points: float = 10
    min_range_points: float = 50
    max_range_points: float = 600
    fee_bps_per_side: float = 5
    slippage_points_per_side: float = 5

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == 'symbol':
                continue
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        for name in ('bar_minutes', 'opening_range_minutes', 'entry_window_minutes', 'max_hold_minutes'):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0 or value % self.bar_minutes:
                raise ValueError(f'{name} must be a positive whole number of bars')
        if self.entry_window_minutes <= self.opening_range_minutes:
            raise ValueError('Entry window must exceed opening range')
        if self.entry_window_minutes + self.max_hold_minutes > 210:
            raise ValueError('Window exceeds supported early-close session length')
        if self.target_points <= 0 or self.stop_points <= 0 or self.min_range_points > self.max_range_points:
            raise ValueError('Invalid target, stop, or range bounds')


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def load_candles(path, bar_minutes):
    candles = []
    with open(path, newline='', encoding='utf-8-sig') as file:
        for row in csv.DictReader(file):
            timestamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
            # Old Binance collector stores naive UTC bar-opening timestamps.
            timestamp = (timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None
                         else timestamp.astimezone(timezone.utc))
            values = [float(row[key]) for key in ('open', 'high', 'low', 'close', 'volume')]
            o, h, l, c, v = values
            if not all(math.isfinite(x) for x in values) or min(o, h, l, c) <= 0 or v < 0:
                raise ValueError(f'Invalid price or volume at {timestamp}')
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError(f'Invalid OHLC at {timestamp}')
            if timestamp.second or timestamp.microsecond or timestamp.minute % bar_minutes:
                raise ValueError(f'Misaligned candle at {timestamp}')
            if candles and timestamp <= candles[-1].timestamp:
                raise ValueError('CSV timestamps must be strictly increasing without duplicates')
            candles.append(Candle(timestamp, *values))
    if not candles:
        raise ValueError('CSV has no candles')
    return candles


def run(candles, cfg):
    groups = {}
    for candle in candles:
        groups.setdefault(candle.timestamp.astimezone(NY).date(), []).append(candle)
    trades, audit = [], []
    step = timedelta(minutes=cfg.bar_minutes)
    for day, bars in sorted(groups.items()):
        opening = session_open(day)
        if opening is None:
            audit.append({'date': str(day), 'status': 'market_closed'})
            continue
        # Require a complete window so missing bars cannot create false results.
        count = (cfg.entry_window_minutes + cfg.max_hold_minutes) // cfg.bar_minutes
        by_time = {bar.timestamp: bar for bar in bars}
        required = [opening + n * step for n in range(count)]
        if any(t not in by_time for t in required):
            audit.append({'date': str(day), 'status': 'incomplete_session'})
            continue
        window = [by_time[t] for t in required]
        range_count = cfg.opening_range_minutes // cfg.bar_minutes
        top = max(b.high for b in window[:range_count])
        bottom = min(b.low for b in window[:range_count])
        if not cfg.min_range_points <= top - bottom <= cfg.max_range_points:
            audit.append({'date': str(day), 'status': 'range_filter'})
            continue
        trade = None
        for index in range(range_count, cfg.entry_window_minutes // cfg.bar_minutes - 1):
            signal = window[index]
            direction = (1 if signal.close > top + cfg.breakout_buffer_points else
                         -1 if signal.close < bottom - cfg.breakout_buffer_points else 0)
            if not direction:
                continue
            entry_bar = window[index + 1]
            entry = entry_bar.open + direction * cfg.slippage_points_per_side
            target = entry + direction * cfg.target_points
            stop = entry - direction * cfg.stop_points
            hold_bars = cfg.max_hold_minutes // cfg.bar_minutes
            exit_price, reason, exit_bar = None, None, None
            for bar in window[index + 1:index + 1 + hold_bars]:
                stop_hit = bar.low <= stop if direction == 1 else bar.high >= stop
                target_hit = bar.high >= target if direction == 1 else bar.low <= target
                if stop_hit:
                    # Stop wins when both levels appear in one candle. Gap at worse open.
                    exit_price = min(stop, bar.open) if direction == 1 else max(stop, bar.open)
                    reason = 'stop'
                elif target_hit:
                    exit_price, reason = target, 'target'
                exit_bar = bar
                if reason:
                    break
            if reason is None:
                exit_price, reason = exit_bar.close, 'timeout'
            exit_price -= direction * cfg.slippage_points_per_side
            gross = direction * (exit_price - entry)
            fees = (entry + exit_price) * cfg.fee_bps_per_side / 10000
            trade = {
                'date': str(day), 'symbol': cfg.symbol,
                'side': 'long' if direction == 1 else 'short',
                'signal_close_time': entry_bar.timestamp.isoformat(),
                'entry_time': entry_bar.timestamp.isoformat(),
                'exit_bar_open_time': exit_bar.timestamp.isoformat(),
                'entry': entry, 'exit': exit_price, 'reason': reason,
                'gross_points_after_slippage': gross, 'fee_points': fees,
                'net_points': gross - fees,
            }
            trades.append(trade)
            break  # One attempt per NY session, including losing trades.
        audit.append({'date': str(day), 'status': 'traded' if trade else 'no_breakout'})
    return trades, audit


def summary(trades, audit):
    net = [t['net_points'] for t in trades]
    equity = peak = drawdown = 0
    for points in net:
        equity += points
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins = sum(x > 0 for x in net)
    statuses = {}
    for row in audit:
        statuses[row['status']] = statuses.get(row['status'], 0) + 1
    return {'trades': len(net), 'net_win_rate': wins / len(net) if net else 0,
            'target_hits': sum(t['reason'] == 'target' for t in trades),
            'net_points_per_unit': sum(net), 'max_drawdown_points': drawdown,
            'session_counts': statuses}
