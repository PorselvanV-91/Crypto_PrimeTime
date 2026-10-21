"""Unauthenticated Delta India candles. This module has no order endpoints."""
import json
import math
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from .paper_config import BAR_SECONDS
from .feed_quality import epoch_seconds, response_evidence


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

    def __post_init__(self):
        if not isinstance(self.time, int) or self.time % BAR_SECONDS:
            raise ValueError('Candle timestamp must be aligned Unix seconds')
        if not all(math.isfinite(x) for x in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError('Non-finite candle')
        if self.low <= 0 or self.volume < 0 or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError('Invalid candle OHLCV')

    def to_dict(self):
        return asdict(self)


class DeltaCandles:
    def __init__(self, config):
        self.config = config
        self.last_candle_evidence = None

    def fetch_quote(self):
        started = time.time()
        timer = time.monotonic()
        request = Request(self.config.api_base_url.rstrip('/') + '/v2/tickers/' + quote(self.config.symbol, safe=''),
                          headers={'Accept': 'application/json', 'User-Agent': 'prime-time-paper/0.4', 'Cache-Control': 'no-cache'})
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
            timing = response_evidence(response, started, time.time(), time.monotonic()-timer)
        received = time.time()
        if data.get('success') is not True or not isinstance(data.get('result'), dict):
            raise ValueError('Delta quote request failed')
        ticker = data['result']
        if ticker.get('symbol') != self.config.symbol:
            raise ValueError('Delta quote symbol mismatch')
        prices = ticker.get('quotes') or {}
        bid, ask = float(prices['best_bid']), float(prices['best_ask'])
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or bid > ask:
            raise ValueError('Invalid bid/ask quote')
        return {'symbol': self.config.symbol, 'bid': bid, 'ask': ask, 'spread_points': ask-bid,
                'observed_at': received, 'request_started_at': started,
                **timing, 'exchange_timestamp_raw': ticker.get('timestamp'),
                'exchange_timestamp': epoch_seconds(ticker.get('timestamp')),
                'ticker_time_raw': ticker.get('time'), 'source': 'delta_rest_ticker'}

    def fetch(self, start, end):
        self.last_candle_evidence = None
        started, timer = time.time(), time.monotonic()
        query = urlencode(dict(symbol=self.config.symbol, resolution='15m',
                               start=int(start), end=int(end)))
        request = Request(self.config.api_base_url.rstrip('/') + '/v2/history/candles?' + query,
                          headers={'Accept': 'application/json', 'User-Agent': 'prime-time-paper/0.4', 'Cache-Control': 'no-cache'})
        with urlopen(request, timeout=10) as response:
            data = json.load(response)
            timing = response_evidence(response, started, time.time(), time.monotonic()-timer)
        if data.get('success') is not True or not isinstance(data.get('result'), list):
            raise ValueError('Delta candle request failed')
        bars = {}
        for row in data['result']:
            raw_time = float(row['time'])
            if not math.isfinite(raw_time) or not raw_time.is_integer():
                raise ValueError('Invalid candle time')
            bar = Bar(int(raw_time), *(float(row[k]) for k in ('open', 'high', 'low', 'close')),
                      float(row.get('volume', 0)))
            if bar.time in bars and bars[bar.time] != bar:
                raise ValueError('Conflicting duplicate candles')
            bars[bar.time] = bar
        self.last_candle_evidence = dict(timing, source='delta_rest_candles',
            exchange_update_timestamp=epoch_seconds(data.get('timestamp')),
            exchange_update_timestamp_raw=data.get('timestamp'),
            bar_timestamp_semantics='opening_time_not_update_time',
            latest_bar_open=max(bars) if bars else None,
            latest_bar_snapshot=bars[max(bars)].to_dict() if bars else None,
            requested_start=int(start), requested_end=int(end))
        return [bars[t] for t in sorted(bars)]
