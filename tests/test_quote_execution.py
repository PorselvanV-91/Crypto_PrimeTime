import json
import unittest
from datetime import datetime, timezone
from prime_time.feed_quality import epoch_seconds, quote_quality, candle_quality
from prime_time.market_data import Bar
from prime_time.paper_config import PaperConfig
from prime_time.paper_engine import create_engine
from prime_time.paper_replay import replay
from prime_time.paper_store import Store
from prime_time.quote_engine import QuotePaperEngine
from prime_time.session import IST


class QuoteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = PaperConfig(execution_mode='quote')
        self.engine = create_engine(self.cfg, self.store)
        self.t = int(datetime(2026, 10, 21, 19, 0, tzinfo=IST).timestamp())
        self.signal = Bar(self.t, 1000, 1250, 1000, 1250)
        self.current = Bar(self.t+900, 1250, 1800, 600, 1700)

    def snapshot(self, now, bid=1289, ask=1290, **overrides):
        return dict(symbol='BTCUSD', bid=bid, ask=ask, spread_points=ask-bid,
                    observed_at=now, exchange_timestamp=now, request_latency_seconds=0.1,
                    **overrides)

    def evidence(self, now):
        return {'observed_at': now, 'request_started_at': now-0.1,
                'request_latency_seconds': 0.1, 'http_date_timestamp': now,
                'bar_timestamp_semantics': 'opening_time_not_update_time'}

    def step(self, bars, now, bid=1289, ask=1290, quote=True, evidence=True, snapshot=None):
        return self.engine.step(bars, now,
            quote_snapshot=(snapshot or self.snapshot(now, bid, ask)) if quote else None,
            candle_evidence=self.evidence(now) if evidence else None)

    def enter(self):
        return self.step([self.signal, self.current], self.t+902)

    def trade(self):
        row = self.store.db.execute('SELECT data FROM trades ORDER BY id DESC').fetchone()
        return json.loads(row[0]) if row else None

    def test_factory_and_quote_side_entry_ignore_candle_extremes(self):
        self.assertIsInstance(self.engine, QuotePaperEngine)
        state = self.enter()
        position = state['position']
        self.assertEqual(position['entry_price'], 1295)
        self.assertEqual(position['target_price'], 1495)
        self.assertEqual(position['stop_price'], 795)
        self.assertEqual(position['execution_mode'], 'quote')
        self.assertEqual(position['entry_fill_model'], 'observed_ask_plus_slippage')

    def test_past_candle_touch_does_not_trigger_entry_with_quote_below_level(self):
        state = self.step([self.signal, self.current], self.t+902, bid=1259, ask=1260)
        self.assertIsNone(state['position'])
        self.assertIsNotNone(state['pending'])

    def test_later_quote_cross_fills_current_ask_not_old_trigger(self):
        self.step([self.signal, self.current], self.t+902, bid=1259, ask=1260)
        state = self.step([self.current], self.t+910, bid=1359, ask=1360)
        self.assertEqual(state['position']['entry_price'], 1365)
        self.assertEqual(state['position']['trigger_price'], 1280)

    def test_target_requires_bid_not_ask_or_old_high(self):
        self.enter()
        state = self.step([self.current], self.t+910, bid=1490, ask=1500)
        self.assertIsNotNone(state['position'])
        self.step([self.current], self.t+920, bid=1500, ask=1501)
        trade = self.trade()
        self.assertEqual(trade['exit_reason'], 'target')
        self.assertEqual(trade['exit_price'], 1495)
        self.assertEqual(trade['exit_fill_model'], 'observed_bid_minus_slippage')

    def test_protective_stop_uses_bid_and_can_gap_beyond_level(self):
        self.enter()
        state = self.step([self.current], self.t+910, bid=780, ask=781)
        self.assertIsNone(state['position'])
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')
        self.assertEqual(self.trade()['exit_price'], 775)

    def test_short_fills_bid_and_exits_ask(self):
        signal = Bar(self.t, 1250, 1250, 1000, 1000)
        state = self.step([signal, self.current], self.t+902, bid=960, ask=961)
        self.assertEqual(state['position']['entry_price'], 955)
        self.assertEqual(state['position']['target_price'], 755)
        state = self.step([self.current], self.t+910, bid=740, ask=760)
        self.assertIsNotNone(state['position'])
        self.step([self.current], self.t+920, bid=749, ask=750)
        self.assertEqual(self.trade()['exit_price'], 755)

    def test_short_protective_stop_is_confirmed_and_filled_on_ask(self):
        signal = Bar(self.t, 1250, 1250, 1000, 1000)
        self.step([signal, self.current], self.t+902, bid=960, ask=961)
        self.step([self.current], self.t+910, bid=1440, ask=1460)
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')
        self.assertEqual(self.trade()['exit_price'], 1465)

    def test_missing_quote_never_falls_back_to_candles(self):
        state = self.step([self.signal, self.current], self.t+902, quote=False)
        self.assertIsNone(state['position'])
        state = self.step([self.current], self.t+1802, quote=False)
        self.assertEqual(state['pending']['entry_time'], self.t+1800)  # Fresh signal may arm, never fill.
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='ENTRY_EXPIRED'").fetchone()[0], 1)
        self.assertIsNone(self.trade())

    def test_stale_exchange_quote_with_fresh_receipt_is_rejected(self):
        q = self.snapshot(self.t+902)
        q['exchange_timestamp'] = self.t+880
        state = self.step([self.signal, self.current], self.t+902, snapshot=q)
        self.assertIsNone(state['position'])
        self.assertEqual(state['feed_health']['quote'], 'stale_quote')

    def test_old_quote_before_arming_cannot_create_a_fill(self):
        q = self.snapshot(self.t+902)
        q['exchange_timestamp'] = self.t+897  # Fresh, but before trigger was armed.
        state = self.step([self.signal, self.current], self.t+902, snapshot=q)
        self.assertIsNone(state['position'])

    def test_regressed_exchange_timestamp_rejected_after_restart(self):
        self.enter()
        self.engine = create_engine(self.cfg, self.store)
        q = self.snapshot(self.t+905)
        q['exchange_timestamp'] = self.t+901
        state = self.step([self.current], self.t+905, snapshot=q)
        self.assertEqual(state['feed_health']['quote'], 'exchange_timestamp_regressed')
        self.assertIsNotNone(state['position'])

    def test_unavailable_candle_update_timestamp_is_explicit(self):
        self.enter()
        audit = self.trade()['entry_audit']
        self.assertEqual(audit['candle_validation']['update_time_status'], 'not_provided_bar_open_only')
        self.assertEqual(audit['candle_validation']['status'], 'fresh_response_update_time_unknown')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM feed_snapshots').fetchone()[0], 1)

    def test_stale_candle_response_blocks_new_signal(self):
        stale = self.evidence(self.t+902)
        stale['http_date_timestamp'] = self.t+800
        state = self.engine.step([self.signal, self.current], self.t+902,
            self.snapshot(self.t+902), candle_evidence=stale)
        self.assertIsNone(state['pending'])
        self.assertEqual(state['feed_health']['candles'], 'stale_or_future_http_date')

    def test_missing_candles_cannot_block_protective_quote_exit(self):
        self.enter()
        state = self.step([], self.t+910, bid=780, ask=781, evidence=False)
        self.assertIsNone(state['position'])
        self.assertEqual(self.trade()['exit_candle_source'], 'synthetic_quote_only')
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')

    def test_timeout_waits_for_fresh_quote_and_survives_restart(self):
        self.enter()
        now = self.t+4503
        state = self.step([], now, quote=False, evidence=False)
        self.assertIsNotNone(state['position'])
        self.assertEqual(state['position']['requested_exit']['reason'], 'timeout')
        self.engine = create_engine(self.cfg, self.store)
        self.step([], now+2, bid=1200, ask=1201, evidence=False)
        self.assertEqual(self.trade()['exit_reason'], 'timeout')
        self.assertEqual(self.trade()['exit_price'], 1195)
        self.assertEqual(self.trade()['exit_fill_observed_at'], now+2)

    def test_opposite_candle_requests_exit_at_current_quote_not_close(self):
        self.enter()
        closed = Bar(self.t+900, 1250, 1800, 600, 650)
        current = Bar(self.t+1800, 1000, 1100, 900, 1000)
        self.step([closed, current], self.t+1802, bid=1000, ask=1001)
        self.assertEqual(self.trade()['exit_reason'], 'opposite_candle_stop')
        self.assertEqual(self.trade()['exit_price'], 995)

    def test_outage_does_not_recover_historical_target_as_a_fill(self):
        self.enter()
        closed = Bar(self.t+900, 1250, 1800, 600, 1400)
        current = Bar(self.t+1800, 1400, 1400, 1400, 1400)
        state = self.step([closed, current], self.t+1802, bid=1400, ask=1401)
        self.assertIsNotNone(state['position'])

    def test_history_gap_still_allows_current_quote_protection(self):
        self.enter()
        future_bar = Bar(self.t+2700, 1000, 1000, 1000, 1000)
        self.step([future_bar], self.t+2702, bid=780, ask=781)
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')

    def test_quote_execution_rejects_ohlc_only_replay(self):
        with self.assertRaisesRegex(ValueError, 'OHLC-only'):
            replay(self.cfg, self.store, [self.signal])

    def test_execution_time_is_observation_not_exchange_update_time(self):
        self.enter()
        q = self.snapshot(self.t+920, bid=1500, ask=1501)
        q['exchange_timestamp'] = self.t+918
        self.step([self.current], self.t+920, snapshot=q)
        trade = self.trade()
        self.assertEqual(trade['exit_time'], self.t+920)
        self.assertEqual(trade['exit_quote_exchange_timestamp'], self.t+918)


class TimestampTests(unittest.TestCase):
    def setUp(self):
        self.now = 1792593900.25
        self.cfg = PaperConfig(execution_mode='quote')

    def test_seconds_milliseconds_microseconds_nanoseconds_iso(self):
        for raw in (self.now, self.now*1e3, self.now*1e6, self.now*1e9,
                    datetime.fromtimestamp(self.now, timezone.utc).isoformat()):
            self.assertAlmostEqual(epoch_seconds(raw), self.now, places=5)
        for raw in (None, 'not-a-time', float('nan'), True, 123):
            self.assertIsNone(epoch_seconds(raw))

    def test_quote_clock_latency_and_timestamp_validation(self):
        base = dict(symbol='BTCUSD', bid=100, ask=101, observed_at=self.now,
                    exchange_timestamp=self.now, request_latency_seconds=0.1)
        cases = [({'exchange_timestamp': self.now+3}, 'future_timestamp'),
                 ({'exchange_timestamp': None}, 'missing_or_invalid_exchange_timestamp'),
                 ({'request_latency_seconds': 6}, 'slow_quote_request'),
                 ({'http_cache_age_seconds': 20}, 'stale_cached_quote'),
                 ({'http_date_timestamp': self.now-20}, 'stale_or_future_quote_http_date')]
        for updates, status in cases:
            self.assertEqual(quote_quality(dict(base, **updates), self.now, self.cfg)['status'], status)

    def test_bar_open_is_not_treated_as_exchange_update(self):
        opening = int(self.now//900)*900
        bars = [Bar(opening, 100, 110, 90, 100)]
        evidence = {'observed_at': self.now, 'request_latency_seconds': .1,
                    'http_date_timestamp': self.now}
        check = candle_quality(evidence, bars, self.now, self.cfg)
        self.assertTrue(check['accepted'])
        self.assertIsNone(check['exchange_update_timestamp'])
