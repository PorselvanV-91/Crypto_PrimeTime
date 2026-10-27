import argparse
import csv
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from .backtest import Config, load_candles, run, summary
from .session import IST, session_open


def main():
    parser = argparse.ArgumentParser(description='BTC US-open research; no order execution')
    sub = parser.add_subparsers(dest='command', required=True)
    session = sub.add_parser('session')
    session.add_argument('date', type=date.fromisoformat)
    backtest = sub.add_parser('backtest')
    backtest.add_argument('--data', type=Path, default=Path('data/BTC_5m.csv'))
    backtest.add_argument('--config', type=Path, default=Path('config.json'))
    backtest.add_argument('--output', type=Path, default=Path('reports/baseline'))
    paper = sub.add_parser('paper', help='Run persistent 15-minute momentum paper trading')
    paper.add_argument('--config', type=Path, default=Path('paper_config.json'))
    paper.add_argument('--env-file', default='.env')
    paper.add_argument('--once', action='store_true', help='One market-data poll, then exit')
    shared = sub.add_parser('paper-shared', help='Run variants against identical market observations')
    shared.add_argument('--configs', nargs='+', type=Path,
                        default=[Path('paper_config.json'), Path('paper_trend_config.json')])
    shared.add_argument('--env-file', default='.env')
    shared.add_argument('--once', action='store_true')
    replay = sub.add_parser('replay', help='Replay CSV with the paper momentum engine')
    replay.add_argument('--data', type=Path, default=Path('data/BTC_5m.csv'))
    replay.add_argument('--config', type=Path, default=Path('paper_config.json'))
    replay.add_argument('--source-minutes', type=int, default=5)
    replay.add_argument('--database', type=Path, default=Path('reports/momentum/replay.sqlite3'))
    replay.add_argument('--output', type=Path, default=Path('reports/momentum'))
    export = sub.add_parser('export', help='Export paper logs and flat trade CSV')
    export.add_argument('--database', type=Path, default=Path('runtime/paper_quote_v4.sqlite3'))
    export.add_argument('--output', type=Path, default=Path('reports/paper'))
    status = sub.add_parser('status', help='Show persisted paper position and results')
    status.add_argument('--database', type=Path, default=Path('runtime/paper_quote_v4.sqlite3'))
    args = parser.parse_args()
    if args.command == 'paper-shared':
        from .paper_config import PaperConfig
        from .shared_runner import run_shared
        run_shared([PaperConfig.load(path) for path in args.configs], once=args.once, env_file=args.env_file)
        return
    if args.command == 'paper':
        from .paper_config import PaperConfig
        from .paper_runner import run_paper
        run_paper(PaperConfig.load(args.config), once=args.once, env_file=args.env_file)
        return
    if args.command in ('replay', 'export', 'status'):
        from dataclasses import replace
        from .paper_config import PaperConfig
        from .paper_store import Store
        from .paper_replay import aggregate_csv, paper_summary, replay as run_replay
        if args.command != 'replay' and not args.database.is_file():
            parser.error('Database does not exist; run paper trading first')
        store = Store(args.database)
        try:
            if args.command == 'replay':
                cfg = replace(PaperConfig.load(args.config), database=str(args.database))
                bars = aggregate_csv(args.data, args.source_minutes)
                if not bars:
                    parser.error('No complete 15-minute candles in source CSV')
                result = run_replay(cfg, store, bars)
                result.update(data=str(args.data.resolve()), config=cfg.to_dict(),
                              source_minutes=args.source_minutes,
                              first_bar_time=bars[0].time, last_bar_time=bars[-1].time,
                              data_note='CSV replay; verify source venue matches the configured symbol')
            else:
                result = paper_summary(store)
            if args.command == 'status':
                state = store.load_state()
                result['position'] = state['position'] if state else None
                result['pending_entry'] = state.get('pending') if state else None
                result['feed_health'] = state.get('feed_health') if state else None
                result['pending_telegram'] = store.db.execute('SELECT count(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0]
            else:
                store.export(args.output)
                (args.output / 'summary.json').write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
        finally:
            store.close()
        return
    if args.command == 'session':
        opening = session_open(args.date)
        print(opening.astimezone(IST).isoformat() if opening else 'NYSE closed')
        return
    cfg = Config(**json.loads(args.config.read_text()))
    candles = load_candles(args.data, cfg.bar_minutes)
    trades, audit = run(candles, cfg)
    result = summary(trades, audit)
    result.update(config=asdict(cfg), data=str(args.data.resolve()),
                  first_bar=candles[0].timestamp.isoformat(),
                  last_bar=candles[-1].timestamp.isoformat())
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, rows in [('trades.csv', trades), ('sessions.csv', audit)]:
        with (args.output / filename).open('w', newline='') as file:
            keys = list(rows[0]) if rows else ['date']
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    (args.output / 'summary.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
