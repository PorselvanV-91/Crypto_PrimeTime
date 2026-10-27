import json
import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from prime_time.feed_quality import quote_quality
from prime_time.hold_diagnostics import observe_position
from prime_time.market_data import Bar
from prime_time.paper_config import PaperConfig
from prime_time.paper_engine import create_engine
from prime_time.paper_store import Store
from prime_time.signal_context import session_start, signal_context
from prime_time.session import IST


class QuotePathTests(unittest.TestCase):
    def setUp(self):
        self.now = 1792757700.0
        self.cfg = PaperConfig(execution_mode='quote')
        self.position = {'entry_price': 1000, 'direction': 1, 'quantity': .1,
                         'entry_fill_observed_at': self.now, 'entry_quote_exchange_timestamp': self.now}

    def observe(self, now, bid, ask, timestamp=None):
        q = dict(symbol='BTCUSD', bid=bid, ask=ask, observed_at=now,
                 exchange_timestamp=now if timestamp is None else timestamp, request_latency_seconds=.1)
        quality = quote_quality(q, now, self.cfg)
        return observe_position(self.position, q, quality, 'live', now, self.cfg)

    def test_long_mark_includes_exit_slippage_and_both_fees(self):
        result = self.observe(self.now, 1100, 1101)
        self.assertEqual(result['mfe_points'], 100)
        self.assertEqual(result['mae_points'], 0)
        self.assertEqual(result['mark']['modeled_exit_price'], 1095)
        self.assertAlmostEqual(result['mark']['net_points'], 95-2095*.0005)
        self.assertAlmostEqual(result['mark']['net_pnl_quote'], (95-2095*.0005)*.1)

    def test_short_uses_ask_and_tracks_both_excursions(self):
        self.position['direction'] = -1
        result = self.observe(self.now, 899, 900)
        self.assertEqual(result['mfe_points'], 100)
        self.assertEqual(result['mark']['modeled_exit_price'], 905)
        result = self.observe(self.now+1, 1099, 1100)
        self.assertEqual(result['mfe_points'], 100)
        self.assertEqual(result['mae_points'], 100)

    def test_stale_or_invalid_quotes_never_update_extrema_or_mark(self):
        self.observe(self.now, 1100, 1101)
        result = self.observe(self.now+20, 1000000, 1000001, self.now)
        self.assertEqual(result['mfe_points'], 100)
        self.assertEqual(result['mae_points'], 0)
        self.assertIsNone(result['mark'])
        self.assertEqual(result['mark_status'], 'stale_quote')
        self.assertEqual(result['unavailable_quote_polls'], 1)

    def test_duplicate_calls_and_repeated_cached_quote_not_extra_samples(self):
        self.observe(self.now, 1100, 1101)
        self.observe(self.now, 1100, 1101)
        result = self.observe(self.now+1, 1100, 1101, self.now)
        self.assertEqual(result['valid_quote_polls'], 2)
        self.assertEqual(result['unique_quote_samples'], 1)

    def test_pre_entry_quote_and_unknown_prior_coverage_are_explicit(self):
        result = self.observe(self.now+5, 900, 901, self.now-5)
        self.assertEqual(result['mark_status'], 'predates_entry')
        self.assertIsNone(result['mfe_points'])
        self.assertIsNone(result['mae_points'])
        self.assertFalse(result['coverage_includes_entry'])


class HeldCandleTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = PaperConfig(execution_mode='quote')
        self.engine = create_engine(self.cfg, self.store)
        self.t = int(datetime(2026, 10, 23, 19, tzinfo=IST).timestamp())
        day = session_start(self.t, self.cfg)
        self.history = [Bar(t, 1000, 1000, 1000, 1000, 10) for t in range(day, self.t, 900)]
        self.signal = Bar(self.t, 1000, 1250, 1000, 1250, 20)
        self.current = Bar(self.t+900, 1250, 1800, 600, 1700, 1)
        self.step([self.signal, self.current], self.t+902, 1289, 1290)

    def step(self, bars, now, bid=1210, ask=1211, stale=False):
        quote = dict(symbol='BTCUSD', bid=bid, ask=ask, spread_points=ask-bid,
                     observed_at=now, exchange_timestamp=now-30 if stale else now, request_latency_seconds=.1)
        evidence = dict(observed_at=now, request_latency_seconds=.1, http_date_timestamp=now)
        return self.engine.step(self.history+bars, now, quote, candle_evidence=evidence)

    def record(self):
        return json.loads(self.store.db.execute('SELECT data FROM hold_diagnostics ORDER BY candle_time DESC').fetchone()[0])

    def test_held_record_has_closed_vwap_and_current_net_mark(self):
        self.step([self.current], self.t+910, 1330, 1331)
        self.step([self.current], self.t+920, 1200, 1201)
        held = Bar(self.t+900, 1250, 1400, 1000, 1300, 10)
        future = Bar(self.t+1800, 1e8, 1e8, 1e8, 1e8, 1e9)
        self.step([self.signal, held, future], self.t+1802)
        record = self.record()
        expected = signal_context(held, self.history+[self.signal, held], self.cfg)['session_vwap']
        self.assertEqual(record['vwap_at_candle_close'], expected)
        self.assertEqual(record['mfe_points'], 35)
        self.assertEqual(record['mae_points'], 95)
        self.assertEqual(record['unique_quote_samples'], 4)
        self.assertAlmostEqual(record['unrealized_net_points'], -91.25)
        self.assertAlmostEqual(record['unrealized_net_pnl_quote'], -.09125)
        self.assertEqual(record['observation_delay_after_close_seconds'], 2)
        self.assertEqual(record['mark_exchange_timestamp'], self.t+1802)
        message = self.store.db.execute("SELECT o.message FROM outbox o JOIN events e ON e.id=o.event_id WHERE e.kind='HOLD'").fetchone()[0]
        self.assertIn('VWAP at candle close', message)
        self.assertIn('Close-now est. net -91.25', message)
        self.assertIn('sampled quotes only', message)

    def test_stale_hold_mark_unavailable_with_previous_extrema_retained(self):
        held = Bar(self.t+900, 1250, 1400, 1000, 1300, 10)
        self.step([held], self.t+1802, bid=999999, ask=1000000, stale=True)
        record = self.record()
        self.assertIsNone(record['unrealized_net_points'])
        self.assertEqual(record['mark_status'], 'stale_quote')
        self.assertEqual(record['mfe_points'], 0)
        self.assertEqual(record['mae_points'], 6)

    def test_restart_retains_path_and_does_not_duplicate_held_row(self):
        self.step([self.current], self.t+910, 1330, 1331)
        self.engine = create_engine(self.cfg, self.store)
        held = Bar(self.t+900, 1250, 1400, 1000, 1300, 10)
        self.step([held], self.t+1802)
        self.step([held], self.t+1803)
        self.assertEqual(self.record()['mfe_points'], 35)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM hold_diagnostics').fetchone()[0], 1)
        trade = json.loads(self.store.db.execute('SELECT data FROM trades').fetchone()[0])
        self.assertEqual(trade['quote_diagnostics']['mfe_points'], 35)

    def test_final_quote_is_included_in_closed_trade_extrema(self):
        self.step([self.current], self.t+910, 1500, 1501)
        trade = json.loads(self.store.db.execute('SELECT data FROM trades').fetchone()[0])
        self.assertEqual(trade['status'], 'closed')
        self.assertEqual(trade['observed_mfe_points'], 205)
        self.assertEqual(trade['observed_mae_points'], 6)

    def test_upgrade_on_existing_position_does_not_invent_prior_excursions(self):
        state = self.store.load_state()
        state['position'].pop('quote_diagnostics')
        self.store.save_state(state)
        self.step([self.current], self.t+910, 1330, 1331)
        metrics = self.store.load_state()['position']['quote_diagnostics']
        self.assertFalse(metrics['coverage_includes_entry'])
        self.assertEqual(metrics['coverage_start_observed_at'], self.t+910)
        self.assertEqual(metrics['mfe_points'], 35)
        self.assertEqual(metrics['mae_points'], 0)

    def test_export_contains_flat_held_diagnostics(self):
        held = Bar(self.t+900, 1250, 1400, 1000, 1300, 10)
        self.step([self.signal, held], self.t+1802)
        workspace = Path.cwd().resolve()
        temporary = tempfile.TemporaryDirectory(dir=workspace)
        target = Path(temporary.name).resolve()
        self.assertTrue(target.is_relative_to(workspace))  # Check before automatic recursive cleanup.
        try:
            self.store.export(target)
            with (target/'hold_analysis.csv').open(newline='', encoding='utf-8') as file:
                row = next(csv.DictReader(file))
            self.assertAlmostEqual(float(row['unrealized_net_points']), -91.25)
            self.assertEqual(row['strategy_name'], 'baseline')
            self.assertEqual(row['mark_status'], 'fresh_observed_quote')
        finally:
            temporary.cleanup()

    def test_fourth_candle_has_diagnostics_even_when_timeout_closes_trade(self):
        for n in range(1, 5):
            bar = Bar(self.t+n*900, 1250, 1400, 1000, 1300, 10)
            next_bar = Bar(bar.time+900, 1300, 1300, 1300, 1300, 1)
            self.step([bar, next_bar], bar.time+902, bid=1300, ask=1301)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM hold_diagnostics').fetchone()[0], 4)
        trade = json.loads(self.store.db.execute('SELECT data FROM trades').fetchone()[0])
        self.assertEqual(trade['exit_reason'], 'timeout')

    def test_timeout_before_confirmation_recovers_final_diagnostic_after_restart(self):
        for n in range(1, 4):
            bar = Bar(self.t+n*900, 1250, 1400, 1000, 1300, 10)
            self.step([bar], bar.time+902, bid=1300, ask=1301)
            self.history.append(bar)
        final_bar = Bar(self.t+3600, 1300, 1400, 1200, 1310, 10)
        deadline = self.t+4500
        state = self.step([final_bar], deadline+1, bid=1300, ask=1301)
        self.assertIsNone(state['position'])
        self.assertEqual(len(state['pending_final_diagnostics']), 1)
        trade = json.loads(self.store.db.execute('SELECT data FROM trades').fetchone()[0])
        frozen = dict(trade['quote_diagnostics'])
        self.assertEqual(trade['exit_observed_at'], deadline+1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM hold_diagnostics').fetchone()[0], 3)
        self.engine = create_engine(self.cfg, self.store)
        # Missing candle response must leave the durable diagnostic pending.
        self.engine.step([], deadline+2, candle_evidence=None)
        self.assertEqual(len(self.store.load_state()['pending_final_diagnostics']), 1)
        state = self.step([final_bar], deadline+5, bid=99999, ask=100000)
        self.assertEqual(state['pending_final_diagnostics'], [])
        record = self.record()
        self.assertEqual(record['diagnostic_type'], 'post_exit_candle_confirmation')
        self.assertEqual(record['mark_observed_at'], deadline+1)
        self.assertEqual(record['diagnostic_observed_at'], deadline+5)
        self.assertEqual(record['mfe_points'], frozen['mfe_points'])
        self.assertEqual(record['mae_points'], frozen['mae_points'])
        self.assertEqual(record['unique_quote_samples'], frozen['unique_quote_samples'])
        self.assertAlmostEqual(record['unrealized_net_points'], trade['net_points'])
        self.step([final_bar], deadline+8)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM hold_diagnostics').fetchone()[0], 4)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='FINAL_HOLD_DIAGNOSTIC'").fetchone()[0], 1)

    def test_target_at_boundary_also_gets_final_diagnostic_without_delaying_exit(self):
        target = self.store.load_state()['position']['target_price']
        held = Bar(self.t+900, 1250, 1600, 1200, 1500, 10)
        state = self.step([held], self.t+1801, bid=target, ask=target+1)
        self.assertIsNone(state['position'])
        self.assertEqual(len(state['pending_final_diagnostics']), 1)
        self.step([held], self.t+1803, bid=9999, ask=10000)
        self.assertEqual(self.record()['mfe_points'], 200)
        self.assertEqual(self.record()['mark_observed_at'], self.t+1801)
