"""Configuration for the independent 15-minute paper strategy."""
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from .session import IST, NY


BAR_SECONDS = 900


@dataclass(frozen=True)
class PaperConfig:
    symbol: str = 'BTCUSD'
    api_base_url: str = 'https://api.india.delta.exchange'
    movement_points: float = 200
    target_points: float = 200
    stop_points: float | None = None
    hold_candles: int = 4
    start_new_york: str = '07:00'
    last_signal_candle_ist: str = '21:00'
    force_flat_at_cutoff: bool = False
    weekdays_only: bool = False
    quantity: float = 0.001
    fee_bps_per_side: float = 5
    slippage_points_per_side: float = 5
    poll_seconds: float = 2
    close_confirmation_seconds: float = 2
    max_entry_delay_seconds: float = 20
    database: str = 'runtime/paper.sqlite3'

    def __post_init__(self):
        for key in ('movement_points', 'target_points', 'quantity', 'poll_seconds',
                    'max_entry_delay_seconds'):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{key} must be positive and finite')
        for key in ('fee_bps_per_side', 'slippage_points_per_side', 'close_confirmation_seconds'):
            value = getattr(self, key)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{key} must be nonnegative and finite')
        if self.stop_points is not None and (not math.isfinite(self.stop_points) or self.stop_points <= 0):
            raise ValueError('stop_points must be null or positive and finite')
        if not isinstance(self.hold_candles, int) or not 1 <= self.hold_candles <= 96:
            raise ValueError('hold_candles must be between 1 and 96')
        for key in ('start_new_york', 'last_signal_candle_ist'):
            parsed = time.fromisoformat(getattr(self, key))
            if parsed.tzinfo or parsed.second or parsed.microsecond or parsed.minute % 15:
                raise ValueError(f'{key} must be an unzoned 15-minute HH:MM time')
        if self.close_confirmation_seconds >= self.max_entry_delay_seconds or self.max_entry_delay_seconds >= 900:
            raise ValueError('Require confirmation < max entry delay < 900 seconds')
        if not self.api_base_url.startswith('https://') or not self.symbol or not self.database:
            raise ValueError('Require HTTPS API URL, symbol and database path')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text(encoding='utf-8')))

    def bounds(self, timestamp):
        """Return IST start and last signal close for the timestamp's local day."""
        local = datetime.fromtimestamp(timestamp, IST)
        # 07:00 NY is 16:30 IST in daylight time, 17:30 in standard time.
        start = datetime.combine(local.date(), time.fromisoformat(self.start_new_york), NY).astimezone(IST)
        cutoff = datetime.combine(local.date(), time.fromisoformat(self.last_signal_candle_ist), IST)
        return start, cutoff + timedelta(seconds=BAR_SECONDS)

    def accepts_signal(self, candle_open):
        start, cutoff = self.bounds(candle_open)
        local = datetime.fromtimestamp(candle_open, IST)
        return (not self.weekdays_only or local.weekday() < 5) and (
            start.timestamp() <= candle_open and candle_open + BAR_SECONDS <= cutoff.timestamp())
