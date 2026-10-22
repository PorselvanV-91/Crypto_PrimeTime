"""Closed-bar HLC3 session VWAP and relative volume; never use future bars."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
from .paper_config import BAR_SECONDS


def session_start(timestamp, cfg):
    zone = ZoneInfo(cfg.vwap_session_timezone)
    local = datetime.fromtimestamp(timestamp, zone)
    return int(datetime.combine(local.date(), time(), zone).timestamp())


def history_start(timestamp, cfg):
    return min(session_start(timestamp, cfg), int(timestamp//BAR_SECONDS)*BAR_SECONDS-cfg.relative_volume_period*BAR_SECONDS)


def signal_context(signal, bars, cfg):
    # A response can include developing/future bars; explicitly discard them.
    known = {bar.time: bar for bar in bars if bar.time <= signal.time}
    known[signal.time] = signal
    opening = session_start(signal.time, cfg)
    expected = range(opening, signal.time+BAR_SECONDS, BAR_SECONDS)
    missing = sum(t not in known for t in expected)
    session = [known[t] for t in expected if t in known]
    volume = sum(bar.volume for bar in session)
    vwap = delta = previous = None
    if missing:
        status = 'incomplete_session_history'
    elif volume <= 0:
        status = 'zero_session_volume'
    else:
        weighted = sum((bar.high+bar.low+bar.close)/3*bar.volume for bar in session)
        vwap = weighted/volume
        prev_volume = volume-signal.volume
        if prev_volume > 0:
            previous = (weighted-(signal.high+signal.low+signal.close)/3*signal.volume)/prev_volume
            delta = vwap-previous
        status = 'ready'
    prior_times = range(signal.time-cfg.relative_volume_period*BAR_SECONDS, signal.time, BAR_SECONDS)
    prior = [known[t] for t in prior_times if t in known]
    average = sum(bar.volume for bar in prior)/len(prior) if len(prior) == cfg.relative_volume_period else None
    relative = signal.volume/average if average and average > 0 else None
    trend = ('above_vwap' if signal.close > vwap else 'below_vwap' if signal.close < vwap else 'at_vwap') if vwap is not None else 'unknown'
    direction = 1 if signal.close > signal.open else -1 if signal.close < signal.open else 0
    aligned = vwap is not None and direction*(signal.close-vwap) > 0
    return {'signal_time': signal.time, 'signal_open': signal.open, 'signal_high': signal.high,
            'signal_low': signal.low, 'signal_close': signal.close,
            'signal_body_points': signal.close-signal.open,
            'movement_candidate': abs(signal.close-signal.open) > cfg.movement_points,
            'vwap_session_start': opening,
            'vwap_session_timezone': cfg.vwap_session_timezone, 'vwap_price_basis': 'HLC3',
            'vwap_status': status, 'vwap_missing_bars': missing, 'session_vwap': vwap,
            'previous_session_vwap': previous, 'vwap_delta_points': delta,
            'close_vs_vwap_points': signal.close-vwap if vwap is not None else None,
            'trend': trend, 'vwap_side_pass': bool(aligned),
            'signal_volume': signal.volume, 'relative_volume_period': cfg.relative_volume_period,
            'prior_volume_mean': average, 'relative_volume': relative,
            'relative_volume_status': 'ready' if relative is not None else 'insufficient_or_zero_prior_volume',
            'trend_filter': cfg.trend_filter, 'strategy_name': cfg.strategy_name}


def context_text(context):
    def number(value):
        return f'{value:.2f}' if value is not None else 'unavailable'
    return (f'VWAP {number(context["session_vwap"])} ({context["vwap_session_timezone"]} midnight/HLC3) | '
            f'{context["trend"]}\nVWAP delta {number(context["vwap_delta_points"])} | '
            f'RVOL({context["relative_volume_period"]}) {number(context["relative_volume"])}\n'
            f'Context {context["vwap_status"]} | Filter {context["trend_filter"]}')
