import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from prime_time.market_data import Bar
from prime_time.paper_config import PaperConfig
from prime_time.paper_engine import create_engine
from prime_time.paper_store import Store
from prime_time.signal_context import signal_context, session_start
from prime_time.session import IST


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = PaperConfig(relative_volume_period=3)
        self.t = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp())
        self.prior = [Bar(self.t+n*900, 10, 10, 10, 10, 10) for n in range(3)]
        self.signal = Bar(self.t+2700, 20, 20, 20, 20, 20)

    def test_weighted_hlc3_and_excluded_signal_volume_baseline(self):
        context = signal_context(self.signal, self.prior+[self.signal], self.cfg)
        self.assertEqual(context['session_vwap'], 14)
        self.assertEqual(context['previous_session_vwap'], 10)
        self.assertEqual(context['vwap_delta_points'], 4)
        self.assertEqual(context['relative_volume'], 2)
        self.assertEqual(context['prior_volume_mean'], 10)

    def test_future_candle_does_not_change_any_signal_feature(self):
        future = Bar(self.signal.time+900, 1e9, 1e9, 1e9, 1e9, 1e9)
        before = signal_context(self.signal, self.prior+[self.signal], self.cfg)
        after = signal_context(self.signal, self.prior+[self.signal, future], self.cfg)
        self.assertEqual(before, after)

    def test_missing_session_bars_are_not_silently_partial_vwap(self):
        context = signal_context(self.signal, [self.prior[0], self.signal], self.cfg)
        self.assertIsNone(context['session_vwap'])
        self.assertEqual(context['vwap_status'], 'incomplete_session_history')
        self.assertFalse(context['vwap_side_pass'])

    def test_day_reset_excludes_previous_day_from_vwap_but_not_rvol(self):
        prior = [Bar(self.t-n*900, 100, 100, 100, 100, 10) for n in (3, 2, 1)]
        signal = Bar(self.t, 20, 20, 20, 20, 20)
        context = signal_context(signal, prior+[signal], self.cfg)
        self.assertEqual(context['session_vwap'], 20)
        self.assertEqual(context['relative_volume'], 2)
        self.assertIsNone(context['vwap_delta_points'])

    def test_zero_volume_and_warmup_are_explicit(self):
        zero = [Bar(self.t+n*900, 10, 10, 10, 10, 0) for n in range(4)]
        context = signal_context(zero[-1], zero, self.cfg)
        self.assertIsNone(context['session_vwap'])
        self.assertIsNone(context['relative_volume'])
        self.assertEqual(context['vwap_status'], 'zero_session_volume')

    def test_session_timezone_is_configurable(self):
        cfg = replace(self.cfg, vwap_session_timezone='Asia/Kolkata')
        midnight = int(datetime(2026, 10, 22, tzinfo=IST).timestamp())
        self.assertEqual(session_start(midnight+3600, cfg), midnight)


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.t = int(datetime(2026, 10, 22, 19, tzinfo=IST).timestamp())
        day = session_start(self.t, PaperConfig())
        self.history = [Bar(t, 2000, 2000, 2000, 2000, 10) for t in range(day, self.t, 900)]
        self.signal = Bar(self.t, 1000, 1250, 1000, 1250, 20)
        self.current = Bar(self.t+900, 1250, 1300, 1240, 1290, 1)

    def run_engine(self, gate='off', signal=None, history=None, bid=1289, ask=1290):
        cfg = PaperConfig(execution_mode='quote', trend_filter=gate, strategy_name='vwap_side' if gate != 'off' else 'baseline')
        store = Store(':memory:')
        self.addCleanup(store.close)
        engine = create_engine(cfg, store)
        now = self.t+902
        quote = dict(symbol='BTCUSD', bid=bid, ask=ask, spread_points=ask-bid,
                     exchange_timestamp=now, observed_at=now, request_latency_seconds=.1)
        evidence = dict(observed_at=now, request_latency_seconds=.1, http_date_timestamp=now)
        bars = (self.history if history is None else history)+[signal or self.signal, self.current]
        state = engine.step(bars, now, quote, candle_evidence=evidence)
        return state, store, engine, quote, evidence

    def test_baseline_logs_countertrend_signal_without_restricting_it(self):
        state, store, *_ = self.run_engine()
        self.assertIsNotNone(state['position'])
        context = state['position']['signal_context']
        self.assertEqual(context['trend'], 'below_vwap')
        self.assertFalse(context['vwap_side_pass'])
        self.assertEqual(context['relative_volume'], 2)
        self.assertEqual(store.db.execute('SELECT count(*) FROM signal_contexts').fetchone()[0], 1)

    def test_filtered_variant_rejects_buy_below_vwap_with_reason(self):
        state, store, *_ = self.run_engine(gate='vwap_side')
        self.assertIsNone(state['position'])
        self.assertIsNone(state['pending'])
        reason = json.loads(store.db.execute("SELECT data FROM events WHERE kind='SIGNAL_REJECTED'").fetchone()[0])
        self.assertEqual(reason['reason'], 'vwap_side_not_aligned')

    def test_filtered_variant_allows_sell_below_vwap(self):
        signal = Bar(self.t, 1250, 1250, 1000, 1000, 20)
        state, *_ = self.run_engine(gate='vwap_side', signal=signal, bid=960, ask=961)
        self.assertEqual(state['position']['side'], 'SELL')

    def test_filtered_variant_allows_buy_above_vwap(self):
        history = [Bar(b.time, 900, 900, 900, 900, 10) for b in self.history]
        state, *_ = self.run_engine(gate='vwap_side', history=history)
        self.assertEqual(state['position']['side'], 'BUY')

    def test_filtered_variant_blocks_missing_vwap_history(self):
        state, store, *_ = self.run_engine(gate='vwap_side', history=[])
        self.assertIsNone(state['position'])
        reason = json.loads(store.db.execute("SELECT data FROM events WHERE kind='SIGNAL_REJECTED'").fetchone()[0])
        self.assertEqual(reason['reason'], 'vwap_unavailable')

    def test_expiry_wording_identical_with_closed_candle_and_wallclock_paths(self):
        for closed_path in (True, False):
            state, store, engine, quote, evidence = self.run_engine(bid=1250, ask=1251)
            self.assertIsNotNone(state['pending'])
            now = self.t+1802
            # No qualifying body for a second signal.
            closed = Bar(self.t+900, 1250, 1270, 1240, 1250, 1)
            engine.step([closed] if closed_path else [], now, None, candle_evidence=None)
            message = store.db.execute("SELECT o.message FROM outbox o JOIN events e ON e.id=o.event_id WHERE e.kind='ENTRY_EXPIRED'").fetchone()[0]
            self.assertIn('no fresh executable trigger observed', message)
            self.assertNotIn('No fill during', message)

    def test_only_logging_defaults_can_migrate_existing_baseline(self):
        cfg = PaperConfig(execution_mode='quote')
        saved = cfg.to_dict()
        for key in ('strategy_name', 'trend_filter', 'vwap_session_timezone', 'relative_volume_period'):
            saved.pop(key)
        self.assertTrue(cfg.matches_saved(saved))
        self.assertFalse(replace(cfg, trend_filter='vwap_side').matches_saved(saved))
        self.assertFalse(replace(cfg, stop_points=400).matches_saved(saved))

    def test_existing_baseline_position_survives_logging_schema_upgrade(self):
        state, store, engine, quote, evidence = self.run_engine()
        identity = state['position']['id']
        for key in ('strategy_name', 'trend_filter', 'vwap_session_timezone', 'relative_volume_period'):
            state['config'].pop(key)
        store.save_state(state)
        now = self.t+905
        quote.update(observed_at=now, exchange_timestamp=now)
        evidence.update(observed_at=now, http_date_timestamp=now)
        recovered = engine.step([self.current], now, quote, candle_evidence=evidence)
        self.assertEqual(recovered['position']['id'], identity)
        self.assertEqual(recovered['config']['trend_filter'], 'off')
