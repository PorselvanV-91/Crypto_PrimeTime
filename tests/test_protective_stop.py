import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime
from prime_time.market_data import Bar, DeltaCandles
from prime_time.paper_config import PaperConfig
from prime_time.paper_engine import PaperEngine
from prime_time.paper_store import Store
from prime_time.session import IST


class ProtectiveStopTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = PaperConfig(fee_bps_per_side=0, slippage_points_per_side=0)
        self.engine = PaperEngine(self.cfg, self.store)
        self.t = int(datetime(2026, 10, 20, 19, 0, tzinfo=IST).timestamp())

    def quote(self, now, bid=1399, ask=1401):
        return {'symbol': 'BTCUSD', 'bid': bid, 'ask': ask, 'spread_points': ask-bid,
                'observed_at': now, 'exchange_timestamp': now, 'request_latency_seconds': 0,
                'source': 'test'}

    def enter(self, short=False):
        signal = Bar(self.t, 1250, 1250, 1000, 1000) if short else Bar(self.t, 1000, 1250, 1000, 1250)
        opening = 700 if short else 1400
        current = Bar(self.t+900, opening, opening, opening, opening)
        return self.engine.step([signal, current], self.t+902,
                               quote_snapshot=self.quote(self.t+902, opening-1, opening+1), mode='live')

    def trade(self):
        return json.loads(self.store.db.execute('SELECT data FROM trades').fetchone()[0])

    def test_default_stop_is_entry_relative_500_both_directions(self):
        state = self.enter()
        self.assertEqual(state['position']['stop_price'], 900)
        self.assertEqual(state['position']['target_price'], 1600)

    def test_long_exits_intrabar_without_waiting_for_close(self):
        self.enter()
        current = Bar(self.t+900, 1400, 1410, 890, 895)
        state = self.engine.step([current], self.t+1000, self.quote(self.t+1000, 894, 896), 'live')
        self.assertIsNone(state['position'])
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')
        self.assertEqual(self.trade()['exit_price'], 894)
        self.assertEqual(self.trade()['exit_bid'], 894)

    def test_short_stop_intrabar(self):
        self.enter(short=True)
        self.assertEqual(self.trade()['stop_price'], 1200)
        current = Bar(self.t+900, 700, 1220, 690, 1210)
        self.engine.step([current], self.t+1000, self.quote(self.t+1000, 1209, 1211), 'live')
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')
        self.assertEqual(self.trade()['exit_price'], 1211)

    def test_gap_after_entry_fills_worse_than_stop(self):
        self.enter()
        held = Bar(self.t+900, 1400, 1450, 1300, 1350)
        current = Bar(self.t+1800, 800, 820, 790, 810)
        self.engine.step([held, current], self.t+1802, mode='live')
        self.assertEqual(self.trade()['exit_price'], 800)

    def test_same_candle_historical_ambiguity_is_stop_first_and_labeled(self):
        self.enter()
        bar = Bar(self.t+1800, 1400, 1700, 800, 1300)
        held = Bar(self.t+900, 1400, 1450, 1300, 1400)
        self.engine.step([held, bar], self.t+2702, mode='replay')
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')
        self.assertTrue(self.trade()['exit_both_levels_in_candle'])
        self.assertEqual(self.trade()['exit_ordering_assumption'], 'stop_first_ohlc_order_unknown')

    def test_pre_entry_wick_is_not_immediate_protective_stop(self):
        signal = Bar(self.t, 1000, 1250, 1000, 1250)
        initial = Bar(self.t+900, 700, 700, 600, 700)
        self.engine.step([signal, initial], self.t+902, mode='live')
        # Pre-entry low already below eventual stop 780; current price is above trigger.
        current = Bar(self.t+900, 700, 1300, 600, 1290)
        state = self.engine.step([current], self.t+950,
                               self.quote(self.t+950, 1289, 1291), 'live')
        self.assertIsNotNone(state['position'])
        self.assertEqual(state['position']['stop_price'], 780)
        # The prior low persists, but no new low or current stop crossing occurs.
        state = self.engine.step([current], self.t+960, self.quote(self.t+960, 1289, 1291), 'live')
        self.assertIsNotNone(state['position'])
        lower = Bar(current.time, 700, 1300, 590, 620)
        self.engine.step([lower], self.t+970, self.quote(self.t+970, 619, 621), 'live')
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')

    def test_snapshot_quote_fill_comparison_and_restart_persist(self):
        state = self.enter()
        position = state['position']
        self.assertEqual(position['entry_bid'], 1399)
        self.assertEqual(position['entry_ask'], 1401)
        self.assertEqual(position['entry_spread_points'], 2)
        self.assertEqual(position['entry_vs_observed_side_points'], -1)
        self.assertEqual(position['entry_candle_snapshot']['open'], 1400)
        self.assertEqual(position['entry_audit']['quote_status'], 'available')
        PaperEngine(self.cfg, self.store).step([], self.t+905, mode='live')
        self.assertEqual(self.store.load_state()['position']['entry_audit'], position['entry_audit'])

    def test_missing_or_stale_quote_is_explicit_not_invented(self):
        signal = Bar(self.t, 1000, 1250, 1000, 1250)
        current = Bar(self.t+900, 1400, 1400, 1400, 1400)
        state = self.engine.step([signal, current], self.t+902, self.quote(self.t+880), 'live')
        self.assertEqual(state['position']['entry_audit']['quote_status'], 'missing_or_stale')
        self.assertIsNone(state['position']['entry_bid'])

    def test_missing_quote_audit_does_not_disable_protection(self):
        signal = Bar(self.t, 1000, 1250, 1000, 1250)
        current = Bar(self.t+900, 1400, 1400, 1400, 1400)
        state = self.engine.step([signal, current], self.t+902, mode='live')
        self.assertEqual(state['position']['entry_audit']['quote_status'], 'missing_or_stale')
        current = Bar(self.t+900, 1400, 1400, 850, 860)
        self.engine.step([current], self.t+910, mode='live')
        self.assertEqual(self.trade()['exit_reason'], 'protective_stop')

    def test_replay_never_associates_current_quote_with_history(self):
        self.enter()
        held = Bar(self.t+900, 1400, 1450, 1300, 1400)
        bad = Bar(self.t+1800, 1400, 1400, 800, 850)
        self.engine.step([held, bad], self.t+5000, self.quote(self.t+5000), 'live')
        self.assertEqual(self.trade()['exit_audit']['quote_status'], 'historical_candle_no_matching_quote')
        self.assertIsNone(self.trade()['exit_audit']['quote'])

    def test_cost_breakdown_in_exit_alert(self):
        self.enter()
        current = Bar(self.t+900, 1400, 1410, 890, 895)
        self.engine.step([current], self.t+1000, mode='live')
        message = self.store.db.execute('SELECT message FROM outbox ORDER BY event_id DESC').fetchone()[0]
        self.assertIn('Gross ', message)
        self.assertIn('Fees ', message)
        self.assertIn('Net ', message)

    def test_protective_stop_includes_slippage_and_fees(self):
        self.cfg = replace(self.cfg, slippage_points_per_side=5, fee_bps_per_side=5)
        self.engine = PaperEngine(self.cfg, self.store)
        self.enter()
        current = Bar(self.t+900, 1400, 1410, 900, 910)
        self.engine.step([current], self.t+1000, self.quote(self.t+1000, 909, 911), 'live')
        trade = self.trade()
        self.assertEqual(trade['entry_price'], 1405)
        self.assertEqual(trade['stop_price'], 905)
        self.assertEqual(trade['exit_price'], 900)
        self.assertAlmostEqual(trade['net_points'], -505-(1405+900)*0.0005)

    def test_opposing_candle_exit_still_operates_before_price_stop(self):
        self.enter()
        held = Bar(self.t+900, 1400, 1450, 1300, 1400)
        opposing = Bar(self.t+1800, 1550, 1550, 1000, 1050)
        self.engine.step([held, opposing], self.t+2702, mode='replay')
        self.assertEqual(self.trade()['exit_reason'], 'opposite_candle_stop')
        self.assertEqual(self.trade()['exit_price'], 1050)


class QuoteAdapterTests(unittest.TestCase):
    def test_parse_quote_evidence(self):
        data = {'success': True, 'result': {'symbol': 'BTCUSD', 'timestamp': 123,
                'quotes': {'best_bid': '100', 'best_ask': '101'}}}
        with patch('prime_time.market_data.urlopen'), patch('prime_time.market_data.json.load', return_value=data):
            snapshot = DeltaCandles(PaperConfig()).fetch_quote()
        self.assertEqual(snapshot['bid'], 100)
        self.assertEqual(snapshot['spread_points'], 1)
        self.assertEqual(snapshot['exchange_timestamp_raw'], 123)

    def test_crossed_or_wrong_symbol_quote_rejected(self):
        for symbol, bid, ask in [('OTHER', '100', '101'), ('BTCUSD', '102', '101'), ('BTCUSD', 'nan', '101')]:
            data = {'success': True, 'result': {'symbol': symbol, 'quotes': {'best_bid': bid, 'best_ask': ask}}}
            with patch('prime_time.market_data.urlopen'), patch('prime_time.market_data.json.load', return_value=data):
                with self.assertRaises(ValueError):
                    DeltaCandles(PaperConfig()).fetch_quote()
