import json
import logging
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from prime_time.market_data import Bar
from prime_time.paper_config import PaperConfig
from prime_time.paper_store import Store
from prime_time.shared_runner import SharedPoller, TelegramWorker, validate_configs
from prime_time.signal_context import session_start, history_start
from prime_time.paper_runner import process_lock
from prime_time.session import IST


class SharedStreamTests(unittest.TestCase):
    def setUp(self):
        self.cfg = PaperConfig(execution_mode='quote', database='unused-baseline.sqlite3')
        self.trend = replace(self.cfg, database='unused-trend.sqlite3', strategy_name='vwap_side', trend_filter='vwap_side')
        self.stores = [Store(':memory:'), Store(':memory:')]
        for store in self.stores:
            self.addCleanup(store.close)
        self.t = int(datetime(2026, 10, 27, 18, tzinfo=IST).timestamp())
        self.bars = [Bar(t, 1000, 1000, 1000, 1000, 10)
                     for t in range(session_start(self.t, self.cfg), self.t, 900)]
        self.bars += [Bar(self.t, 1000, 1250, 1000, 1250, 20),
                      Bar(self.t+900, 1250, 1500, 1200, 1300, 1)]
        self.feed = Mock()
        self.feed.fetch.return_value = self.bars
        self.logger = Mock()
        self.poller = SharedPoller([self.cfg, self.trend], self.stores, self.logger, self.feed)

    def poll(self, now, bid=1289, ask=1290):
        self.feed.fetch_quote.return_value = dict(symbol='BTCUSD', bid=bid, ask=ask,
            spread_points=ask-bid, observed_at=now, exchange_timestamp=now, request_latency_seconds=.1)
        self.feed.last_candle_evidence = dict(observed_at=now, request_latency_seconds=.1, http_date_timestamp=now)
        with patch('prime_time.shared_runner.time.time', return_value=now):
            return self.poller.poll()

    def test_same_snapshot_clock_and_exact_target_produce_identical_fills(self):
        self.poll(self.t+902)
        self.assertEqual(self.feed.fetch.call_count, 1)
        self.assertEqual(self.feed.fetch_quote.call_count, 1)
        positions = [s.load_state()['position'] for s in self.stores]
        self.assertTrue(all(p is not None for p in positions))
        self.assertEqual(positions[0]['entry_price'], positions[1]['entry_price'])
        target = positions[0]['target_price']
        timing = self.poll(self.t+912, target, target+.5)
        trades = [json.loads(s.db.execute('SELECT data FROM trades').fetchone()[0]) for s in self.stores]
        for key in ('exit_time', 'exit_price', 'net_points', 'observed_mfe_points'):
            self.assertEqual(trades[0][key], trades[1][key])
        self.assertTrue(all(t['exit_reason'] == 'target' for t in trades))
        snapshots = [json.loads(s.db.execute('SELECT data FROM feed_snapshots ORDER BY id DESC').fetchone()[0]) for s in self.stores]
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0]['quote']['shared_observation']['sequence'], 1)
        self.assertEqual(len(timing['engines']), 2)
        self.assertIn('candle_fetch_seconds', timing)
        self.assertIn('quote_fetch_seconds', timing)
        self.assertIn('poll_work_seconds', timing)

    def test_candle_failure_keeps_quote_exits_running_for_both(self):
        self.poll(self.t+902)
        target = self.stores[0].load_state()['position']['target_price']
        self.feed.fetch.side_effect = RuntimeError('offline')
        self.poll(self.t+912, target, target+.5)
        self.assertTrue(all(s.load_state()['position'] is None for s in self.stores))

    def test_missing_quote_cannot_create_fills(self):
        self.feed.fetch_quote.side_effect = RuntimeError('offline')
        self.poll(self.t+902)
        self.assertTrue(all(s.load_state()['position'] is None for s in self.stores))

    def test_strategy_failure_is_visible_and_stops_comparison(self):
        self.poller.engines[0].step = Mock(side_effect=RuntimeError('failed'))
        with self.assertRaisesRegex(RuntimeError, 'Shared strategy step failed'):
            self.poll(self.t+902)
        self.assertIsNotNone(self.stores[1].load_state())

    def test_reject_shared_database_or_different_trading_rules(self):
        with self.assertRaisesRegex(ValueError, 'distinct database'):
            validate_configs([self.cfg, replace(self.trend, database=self.cfg.database)])
        with self.assertRaisesRegex(ValueError, 'may differ only'):
            validate_configs([self.cfg, replace(self.trend, target_points=250)])

    def test_pending_final_diagnostic_requests_old_session_warmup(self):
        old_bar = self.t-3*86400
        self.stores[0].save_state({'config': self.cfg.to_dict(), 'position': None,
            'pending': None, 'last_closed': self.t-900,
            'pending_final_diagnostics': [{'trade_id': 'old-trade', 'candle_time': old_bar}]})
        self.poll(self.t+902)
        self.assertEqual(self.feed.fetch.call_args.args[0], history_start(old_bar, self.cfg))
        self.assertEqual(len(self.stores[0].load_state()['pending_final_diagnostics']), 1)


class TelegramWorkerTests(unittest.TestCase):
    def test_database_lock_excludes_old_runner(self):
        root = Path.cwd().resolve()
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.assertTrue(Path(temporary.name).resolve().is_relative_to(root))
        try:
            path = Path(temporary.name)/'paper.lock'
            with process_lock(path):
                with self.assertRaises(OSError):
                    with process_lock(path):
                        self.fail('Duplicate database lock acquired')
        finally:
            temporary.cleanup()

    def test_blocked_notification_does_not_block_database_or_quote_execution(self):
        root = Path.cwd().resolve()
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.assertTrue(Path(temporary.name).resolve().is_relative_to(root))
        entered, release = threading.Event(), threading.Event()
        worker = None
        store = None
        try:
            cfg = PaperConfig(execution_mode='quote', database=str(Path(temporary.name)/'paper.sqlite3'))
            store = Store(cfg.database)
            store.event(time.time(), 'TEST', {}, message='test alert')
            telegram = Mock(enabled=True)
            def slow_send(message):
                entered.set()
                release.wait(5)
            telegram.send_telegram_alert.side_effect = slow_send
            worker = TelegramWorker(cfg.database, telegram, logging.getLogger('test'))
            worker.start()
            self.assertTrue(entered.wait(2))
            feed = Mock()
            now = time.time()
            bar_time = int(now//900)*900
            feed.fetch.return_value = [Bar(bar_time, 1000, 1000, 1000, 1000, 1)]
            feed.fetch_quote.return_value = dict(symbol='BTCUSD', bid=1000, ask=1001,
                observed_at=now, exchange_timestamp=now, request_latency_seconds=.01)
            feed.last_candle_evidence = dict(observed_at=now, request_latency_seconds=.01, http_date_timestamp=now)
            poller = SharedPoller([cfg], [store], Mock(), feed)
            poller.poll()
            self.assertEqual(store.db.execute('SELECT count(*) FROM feed_snapshots').fetchone()[0], 1)
            self.assertFalse(release.is_set())
            self.assertIsNone(store.db.execute('SELECT sent_at FROM outbox').fetchone()[0])
            before_release = time.time()
            release.set()
            worker.close()
            self.assertGreaterEqual(store.db.execute('SELECT sent_at FROM outbox').fetchone()[0], before_release)
        finally:
            release.set()
            if worker:
                worker.close()
            if store:
                store.close()
            temporary.cleanup()
