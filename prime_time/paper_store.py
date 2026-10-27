"""SQLite transaction log, persisted position, and durable Telegram outbox."""
import csv
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT, time INTEGER, data TEXT NOT NULL, decision TEXT NOT NULL,
                PRIMARY KEY(symbol,time));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL,
                kind TEXT NOT NULL, trade_id TEXT, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                event_id INTEGER PRIMARY KEY, message TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                sent_at REAL);
            CREATE TABLE IF NOT EXISTS feed_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL,
                execution_mode TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signal_contexts (
                symbol TEXT, time INTEGER, strategy_name TEXT, data TEXT NOT NULL,
                PRIMARY KEY(symbol,time,strategy_name));
            CREATE TABLE IF NOT EXISTS hold_diagnostics (
                trade_id TEXT, candle_time INTEGER, data TEXT NOT NULL,
                PRIMARY KEY(trade_id,candle_time));
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def load_state(self):
        row = self.db.execute('SELECT data FROM state WHERE id=1').fetchone()
        return json.loads(row['data']) if row else None

    def save_state(self, state):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(state),))

    def event(self, now, kind, data, trade_id=None, message=None):
        cursor = self.db.execute('INSERT INTO events(observed_at,kind,trade_id,data) VALUES(?,?,?,?)',
                                 (now, kind, trade_id, json.dumps(data)))
        if message:
            self.db.execute('INSERT INTO outbox(event_id,message) VALUES(?,?)', (cursor.lastrowid, message))

    def candle(self, symbol, bar, decision):
        self.db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?)',
                        (symbol, bar.time, json.dumps(bar.to_dict()), decision))

    def trade(self, trade):
        self.db.execute('INSERT OR REPLACE INTO trades VALUES(?,?,?)',
                        (trade['id'], trade['status'], json.dumps(trade)))

    def feed_snapshot(self, now, execution_mode, data):
        self.db.execute('INSERT INTO feed_snapshots(observed_at,execution_mode,data) VALUES(?,?,?)',
                        (now, execution_mode, json.dumps(data)))

    def signal_context(self, symbol, bar, context):
        self.db.execute('INSERT OR REPLACE INTO signal_contexts VALUES(?,?,?,?)',
                        (symbol, bar.time, context['strategy_name'], json.dumps(context)))

    def hold_diagnostic(self, record):
        self.db.execute('INSERT OR REPLACE INTO hold_diagnostics VALUES(?,?,?)',
                        (record['trade_id'], record['candle_open_time'], json.dumps(record)))

    def flush_telegram(self, telegram, now, logger, stop_event=None):
        if not telegram.enabled:
            return
        rows = self.db.execute('SELECT * FROM outbox WHERE sent_at IS NULL AND next_attempt<=? '
                               'ORDER BY event_id LIMIT 5', (now,)).fetchall()
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                break
            started = time.monotonic()
            try:
                telegram.send_telegram_alert(row['message'])
            except Exception:
                attempts = row['attempts'] + 1
                self.db.execute('UPDATE outbox SET attempts=?,next_attempt=? WHERE event_id=?',
                                (attempts, now + min(300, 2 ** min(attempts, 8)), row['event_id']))
                logger.warning('Telegram delivery failed for event %s; will retry', row['event_id'])
                logger.info('TELEGRAM_TIMING event_id=%s send_seconds=%.6f result=failed',
                            row['event_id'], time.monotonic()-started)
                break
            else:
                completed = time.time()
                send_seconds = time.monotonic()-started
                ack_started = time.monotonic()
                self.db.execute('UPDATE outbox SET sent_at=? WHERE event_id=?', (completed, row['event_id']))
                logger.info('TELEGRAM_TIMING event_id=%s send_seconds=%.6f ack_seconds=%.6f result=sent',
                            row['event_id'], send_seconds, time.monotonic()-ack_started)

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for table in ('trades', 'events', 'candles', 'outbox', 'feed_snapshots', 'signal_contexts', 'hold_diagnostics'):
            rows = self.db.execute(f'SELECT * FROM {table}').fetchall()
            with (directory / f'{table}.csv').open('w', newline='', encoding='utf-8') as file:
                keys = [r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]
                writer = csv.DictWriter(file, fieldnames=keys)
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
        trades = [json.loads(r['data']) for r in self.db.execute('SELECT data FROM trades')]
        # A flat trade export avoids requiring JSON parsing for later analysis.
        keys = sorted({key for trade in trades for key in trade}) or ['id', 'status']
        with (directory / 'trade_analysis.csv').open('w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            for trade in trades:
                writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                 for k, v in trade.items()})
        contexts = []
        for row in self.db.execute('SELECT s.symbol,s.data,c.decision FROM signal_contexts s '
                                   'LEFT JOIN candles c ON c.symbol=s.symbol AND c.time=s.time ORDER BY s.time'):
            contexts.append(dict(json.loads(row['data']), symbol=row['symbol'], decision=row['decision']))
        keys = sorted({key for row in contexts for key in row}) or ['signal_time', 'strategy_name']
        with (directory / 'signal_analysis.csv').open('w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            writer.writerows(contexts)
        held = [json.loads(row['data']) for row in self.db.execute('SELECT data FROM hold_diagnostics ORDER BY candle_time,trade_id')]
        keys = sorted({key for row in held for key in row}) or ['trade_id', 'candle_open_time']
        with (directory / 'hold_analysis.csv').open('w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            writer.writerows(held)

    def close(self):
        self.db.close()
