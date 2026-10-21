"""Normalize exchange clocks without mistaking bar openings for update times."""
import math
from datetime import datetime
from email.utils import parsedate_to_datetime


def epoch_seconds(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
        if not math.isfinite(value):
            return None
        if abs(value) >= 1e17:
            value /= 1e9
        elif abs(value) >= 1e14:
            value /= 1e6
        elif abs(value) >= 1e11:
            value /= 1e3
        return value if 946684800 <= value < 4102444800 else None
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            return None


def response_evidence(response, started, received, elapsed):
    headers = response.headers
    date_raw = headers.get('Date')
    try:
        server_time = parsedate_to_datetime(date_raw).timestamp() if isinstance(date_raw, str) else None
    except (ValueError, TypeError, OverflowError):
        server_time = None
    try:
        age = float(headers.get('Age'))
        if not math.isfinite(age) or age < 0:
            age = None
    except (ValueError, TypeError):
        age = None
    return {'request_started_at': started, 'observed_at': received,
            'request_latency_seconds': elapsed, 'http_date_raw': date_raw if isinstance(date_raw, str) else None,
            'http_date_timestamp': server_time, 'http_cache_age_seconds': age,
            'cache_control': headers.get('Cache-Control') if isinstance(headers.get('Cache-Control'), str) else None}


def quote_quality(snapshot, now, cfg, previous_exchange_time=None):
    if not snapshot:
        return {'accepted': False, 'status': 'missing_quote'}
    timestamp = epoch_seconds(snapshot.get('exchange_timestamp', snapshot.get('exchange_timestamp_raw')))
    evidence = {'exchange_timestamp': timestamp,
                'exchange_age_seconds': now-timestamp if timestamp is not None else None,
                'receipt_age_seconds': None}
    status = 'fresh'
    try:
        bid, ask = float(snapshot['bid']), float(snapshot['ask'])
        receipt = float(snapshot['observed_at'])
        latency = float(snapshot['request_latency_seconds'])
        receipt_age = now-receipt
        evidence['receipt_age_seconds'] = receipt_age
        if snapshot.get('symbol') != cfg.symbol or not (0 < bid <= ask) or not all(math.isfinite(x) for x in (bid, ask, receipt, latency)):
            status = 'invalid_quote'
        elif timestamp is None:
            status = 'missing_or_invalid_exchange_timestamp'
        elif receipt_age < -cfg.clock_skew_tolerance_seconds or now-timestamp < -cfg.clock_skew_tolerance_seconds:
            status = 'future_timestamp'
        elif receipt_age > cfg.max_quote_age_seconds or now-timestamp > cfg.max_quote_age_seconds:
            status = 'stale_quote'
        elif latency < 0 or latency > cfg.max_feed_latency_seconds:
            status = 'slow_quote_request'
        elif snapshot.get('http_cache_age_seconds') is not None and snapshot['http_cache_age_seconds'] > cfg.max_quote_age_seconds:
            status = 'stale_cached_quote'
        elif snapshot.get('http_date_timestamp') is not None and (
                now-snapshot['http_date_timestamp'] > cfg.max_quote_age_seconds or
                snapshot['http_date_timestamp']-now > cfg.clock_skew_tolerance_seconds):
            status = 'stale_or_future_quote_http_date'
        elif previous_exchange_time is not None and timestamp < previous_exchange_time:
            status = 'exchange_timestamp_regressed'
    except (KeyError, ValueError, TypeError):
        status = 'invalid_quote'
    return dict(evidence, status=status, accepted=status == 'fresh')


def candle_quality(evidence, bars, now, cfg):
    result = {'accepted': False, 'status': 'missing_candle_response',
              'update_time_status': 'not_provided_bar_open_only'}
    if not evidence or not bars:
        return result
    try:
        age = now-float(evidence['observed_at'])
        latency = float(evidence['request_latency_seconds'])
        server = evidence.get('http_date_timestamp')
        cache_age = evidence.get('http_cache_age_seconds') or 0
        latest = max(bar.time for bar in bars)
        update = epoch_seconds(evidence.get('exchange_update_timestamp'))
        result.update(response_age_seconds=age, latest_bar_open=latest,
                      exchange_update_timestamp=update,
                      update_time_status='available' if update is not None else 'not_provided_bar_open_only')
        if not all(math.isfinite(x) for x in (age, latency, cache_age)):
            status = 'invalid_candle_evidence'
        elif age < -cfg.clock_skew_tolerance_seconds or latest > now+cfg.clock_skew_tolerance_seconds:
            status = 'future_candle_timestamp'
        elif latency < 0 or latency > cfg.max_feed_latency_seconds:
            status = 'slow_candle_request'
        elif age > cfg.max_candle_response_age_seconds or cache_age > cfg.max_candle_response_age_seconds:
            status = 'stale_candle_response'
        elif server is None and update is None:
            status = 'missing_candle_server_timestamp'
        elif server is not None and (now-server > cfg.max_candle_response_age_seconds or server-now > cfg.clock_skew_tolerance_seconds):
            status = 'stale_or_future_http_date'
        elif update is not None and (now-update > cfg.max_candle_response_age_seconds or update-now > cfg.clock_skew_tolerance_seconds):
            status = 'stale_or_future_candle_update'
        elif latest < int(now//900)*900-900:
            status = 'missing_recent_bar'
        else:
            status = 'fresh_response' if update is not None else 'fresh_response_update_time_unknown'
        result.update(status=status, accepted=status.startswith('fresh_response'))
    except (KeyError, ValueError, TypeError):
        result['status'] = 'invalid_candle_evidence'
    return result
