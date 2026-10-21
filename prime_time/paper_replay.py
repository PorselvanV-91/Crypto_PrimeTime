"""Replay historical 5m/15m CSV through the same persistent paper engine."""
from .backtest import load_candles
from .market_data import Bar
from .paper_config import BAR_SECONDS
from .paper_engine import PaperEngine


def aggregate_csv(path, source_minutes=5):
    if source_minutes not in (1, 3, 5, 15):
        raise ValueError('Source interval must divide 15 minutes')
    source = load_candles(path, source_minutes)
    grouped = {}
    for candle in source:
        timestamp = int(candle.timestamp.timestamp())
        grouped.setdefault(timestamp // BAR_SECONDS * BAR_SECONDS, []).append(candle)
    result = []
    for timestamp, group in sorted(grouped.items()):
        expected = [timestamp + n*source_minutes*60 for n in range(15//source_minutes)]
        if [int(c.timestamp.timestamp()) for c in group] != expected:
            continue
        result.append(Bar(timestamp, group[0].open, max(c.high for c in group),
                          min(c.low for c in group), group[-1].close,
                          sum(c.volume for c in group)))
    return result


def replay(config, store, bars):
    if config.execution_mode == 'quote':
        raise ValueError('OHLC-only replay cannot reproduce quote fills; use --config paper_candle_config.json')
    if store.load_state() is not None:
        raise ValueError('Replay requires a new database; choose another --database path')
    engine = PaperEngine(config, store)
    for index, bar in enumerate(bars):
        snapshot = [bar]
        next_bar = bars[index+1] if index+1 < len(bars) else None
        now = bar.time + BAR_SECONDS + config.close_confirmation_seconds
        if next_bar and next_bar.time == bar.time + BAR_SECONDS:
            # Only the next open is visible here. Never expose its future OHLC.
            snapshot.append(Bar(next_bar.time, next_bar.open, next_bar.open,
                                next_bar.open, next_bar.open, 0))
        else:
            # No available next-open fill; don't invent an entry at end of data.
            now += config.max_entry_delay_seconds
        engine.step(snapshot, now, mode='replay')
    return paper_summary(store)


def paper_summary(store):
    import json
    rows = [json.loads(r['data']) for r in store.db.execute('SELECT data FROM trades ORDER BY id')]
    state = store.load_state()
    closed = [r for r in rows if r['status'] == 'closed']
    net = [r['net_points'] for r in closed]
    equity = peak = drawdown = 0
    for points in net:
        equity += points
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    return {'closed_trades': len(closed), 'open_trades': len(rows)-len(closed),
            'execution_mode': state['config'].get('execution_mode', 'legacy_candle_model') if state else None,
            'execution_modes': sorted({r.get('execution_mode', 'legacy_candle_model') for r in rows}),
            'target_hits': sum(r['exit_reason'] == 'target' for r in closed),
            'net_win_rate': sum(x > 0 for x in net)/len(net) if net else 0,
            'net_points': sum(net), 'net_pnl_quote': sum(r['net_pnl_quote'] for r in closed),
            'max_drawdown_points': drawdown}
