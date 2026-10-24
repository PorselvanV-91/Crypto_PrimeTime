"""Observed quote excursions and hypothetical close-now P&L, without trading gates."""


def observe_position(position, quote, quality, mode, now, cfg):
    metrics = position.setdefault('quote_diagnostics', {
        'coverage_start_observed_at': now,
        'coverage_includes_entry': now == position.get('entry_fill_observed_at', position.get('entry_observed_at')),
        'valid_quote_polls': 0, 'unavailable_quote_polls': 0, 'unique_quote_samples': 0,
        'mfe_points': None, 'mae_points': None, 'mark': None,
        'definition': 'Executable quote movement from filled entry; MFE/MAE exclude exit slippage and fees'})
    if metrics.get('last_poll_observed_at') == now:
        return metrics
    metrics['last_poll_observed_at'] = now
    status = quality.get('status', 'missing_quote')
    usable = mode == 'live' and quote is not None and quality.get('accepted', False)
    entry_at = position.get('entry_fill_observed_at', position.get('entry_observed_at'))
    opening_quote = (now == entry_at and
                     quality.get('exchange_timestamp') == position.get('entry_quote_exchange_timestamp'))
    if usable and not opening_quote and quality['exchange_timestamp'] < entry_at-cfg.clock_skew_tolerance_seconds:
        usable, status = False, 'predates_entry'
    if not usable:
        metrics['unavailable_quote_polls'] += 1
        metrics['mark_status'] = status if mode == 'live' else 'no_observed_quotes_' + mode
        metrics['mark'] = None  # Never substitute an earlier quote for a current mark.
        return metrics

    metrics['valid_quote_polls'] += 1
    direction = position['direction']
    executable = quote['bid'] if direction == 1 else quote['ask']
    movement = direction*(executable-position['entry_price'])
    key = [quality['exchange_timestamp'], quote['bid'], quote['ask']]
    sample = {'executable_price': executable, 'movement_points': movement,
              'exchange_timestamp': quality['exchange_timestamp'], 'observed_at': now}
    if key != metrics.get('last_quote_key'):
        metrics['unique_quote_samples'] += 1
        metrics['last_quote_key'] = key
        if metrics['mfe_points'] is None or max(0, movement) > metrics['mfe_points']:
            metrics['mfe_points'] = max(0, movement)
            metrics['mfe_quote'] = dict(sample) if movement >= 0 else None
        if metrics['mae_points'] is None or max(0, -movement) > metrics['mae_points']:
            metrics['mae_points'] = max(0, -movement)
            metrics['mae_quote'] = dict(sample) if movement <= 0 else None
    modeled_exit = executable-direction*cfg.slippage_points_per_side
    gross = direction*(modeled_exit-position['entry_price'])
    fees = (position['entry_price']+modeled_exit)*cfg.fee_bps_per_side/10000
    metrics['mark_status'] = 'fresh_observed_quote'
    metrics['mark'] = dict(sample, bid=quote['bid'], ask=quote['ask'],
                           modeled_exit_price=modeled_exit, gross_points=gross, fee_points=fees,
                           net_points=gross-fees, net_pnl_quote=(gross-fees)*position['quantity'],
                           quote_exchange_age_seconds=quality.get('exchange_age_seconds'))
    metrics['last_valid_quote_observed_at'] = now
    return metrics


def make_hold_record(position, bar, context, now, cfg):
    metrics = position['quote_diagnostics']
    mark = metrics.get('mark')
    close_time = bar.time+900
    return {'trade_id': position['id'], 'strategy_name': position.get('strategy_name', cfg.strategy_name),
            'execution_mode': cfg.execution_mode, 'symbol': cfg.symbol, 'side': position['side'],
            'candle_open_time': bar.time, 'candle_close_time': close_time,
            'diagnostic_observed_at': now, 'observation_delay_after_close_seconds': now-close_time,
            'vwap_at_candle_close': context['session_vwap'], 'vwap_status': context['vwap_status'],
            'vwap_delta_points': context['vwap_delta_points'], 'vwap_session_timezone': cfg.vwap_session_timezone,
            'candle_close_vs_vwap_points': context['close_vs_vwap_points'],
            'mark_status': metrics['mark_status'],
            'mark_observed_at': mark['observed_at'] if mark else None,
            'mark_exchange_timestamp': mark['exchange_timestamp'] if mark else None,
            'mark_quote_age_seconds': mark['quote_exchange_age_seconds'] if mark else None,
            'mark_bid': mark['bid'] if mark else None, 'mark_ask': mark['ask'] if mark else None,
            'mark_executable_price': mark['executable_price'] if mark else None,
            'modeled_exit_price': mark['modeled_exit_price'] if mark else None,
            'unrealized_gross_points': mark['gross_points'] if mark else None,
            'unrealized_fee_points': mark['fee_points'] if mark else None,
            'unrealized_net_points': mark['net_points'] if mark else None,
            'unrealized_net_pnl_quote': mark['net_pnl_quote'] if mark else None,
            'mfe_points': metrics['mfe_points'], 'mae_points': metrics['mae_points'],
            'valid_quote_polls': metrics['valid_quote_polls'],
            'unavailable_quote_polls': metrics['unavailable_quote_polls'],
            'unique_quote_samples': metrics['unique_quote_samples'],
            'last_valid_quote_observed_at': metrics.get('last_valid_quote_observed_at'),
            'coverage_start_observed_at': metrics['coverage_start_observed_at'],
            'coverage_includes_entry': metrics['coverage_includes_entry'],
            'excursion_scope': 'Valid sampled quotes through diagnostic observation, not true intrabar extrema',
            'mark_scope': 'Close-now estimate at observation, not historical candle-close execution'}


def diagnostic_text(record):
    def number(value):
        return f'{value:+.2f}' if value is not None else 'unavailable'
    pnl = record['unrealized_net_pnl_quote']
    currency = f'{pnl:+.4f}' if pnl is not None else 'unavailable'
    return (f'VWAP at candle close {number(record["vwap_at_candle_close"])} '
            f'({record["vwap_session_timezone"]}) | {record["vwap_status"]}\n'
            f'Close-now est. net {number(record["unrealized_net_points"])} points | '
            f'{currency} USD\n'
            f'MFE {number(record["mfe_points"])} | MAE {number(record["mae_points"])} quote points\n'
            f'Mark {record["mark_status"]} | Samples {record["unique_quote_samples"]} | '
            f'Unavailable polls {record["unavailable_quote_polls"]}\n'
            f'Observation {record["observation_delay_after_close_seconds"]:.1f}s after candle close; '
            f'extrema cover sampled quotes only')
