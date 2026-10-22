"""Paired OHLC proxy benchmark; never claim to reproduce observed quote fills."""
import argparse
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from .paper_config import PaperConfig
from .paper_replay import aggregate_csv, replay
from .paper_store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data/BTC_5m.csv'))
    parser.add_argument('--source-minutes', type=int, default=5)
    parser.add_argument('--config', type=Path, default=Path('paper_candle_config.json'))
    parser.add_argument('--output', type=Path, default=Path('reports/trend_comparison'))
    args = parser.parse_args()
    base = PaperConfig.load(args.config)
    if base.execution_mode != 'candle_touch':
        parser.error('Comparison needs candle-touch configuration; CSV cannot reproduce quote execution')
    bars = aggregate_csv(args.data, args.source_minutes)
    if not bars:
        parser.error('No complete 15-minute candles')
    output = {'data': str(args.data.resolve()), 'bar_count': len(bars),
              'first_bar_time': bars[0].time, 'last_bar_time': bars[-1].time,
              'limitation': 'OHLC candle-touch proxy; not actual quote-mode performance or out-of-sample validation',
              'runs': {}}
    # Reject existing experiments before modifying either one.
    for name in ('baseline', 'vwap_side'):
        if (args.output/name/'replay.sqlite3').exists():
            parser.error('Comparison output already exists; select a new --output directory')
    for name, gate in [('baseline', 'off'), ('vwap_side', 'vwap_side')]:
        folder = args.output/name
        cfg = replace(base, strategy_name=name, trend_filter=gate, database=str(folder/'replay.sqlite3'))
        store = Store(cfg.database)
        try:
            result = replay(cfg, store, bars)
            result['average_net_points'] = result['net_points']/result['closed_trades'] if result['closed_trades'] else None
            reasons = Counter(json.loads(row['data'])['reason'] for row in
                              store.db.execute("SELECT data FROM events WHERE kind='SIGNAL_REJECTED'"))
            result.update(config=cfg.to_dict(), rejected_signals=dict(reasons))
            store.export(folder)
            (folder/'summary.json').write_text(json.dumps(result, indent=2))
            output['runs'][name] = result
        finally:
            store.close()
    (args.output/'comparison.json').write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
