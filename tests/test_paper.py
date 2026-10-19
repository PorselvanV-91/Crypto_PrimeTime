import json
import logging
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from prime_time.market_data import Bar
from prime_time.paper_config import PaperConfig
from prime_time.paper_engine import DataGapError, PaperEngine
from prime_time.paper_replay import replay
from prime_time.paper_store import Store
from prime_time.session import IST


def timestamp(local):
    return int(datetime.fromisoformat(local).replace(tzinfo=IST).timestamp())


class WindowTests(unittest.TestCase):
    def test_dst_and_start_boundary(self):
        cfg = PaperConfig()
        self.assertFalse(cfg.accepts_signal(timestamp('2026-10-19T16:15')))
        self.assertTrue(cfg.accepts_signal(timestamp('2026-10-19T16:30')))
        self.assertFalse(cfg.accepts_signal(timestamp('2026-11-02T17:15')))
        self.assertTrue(cfg.accepts_signal(timestamp('2026-11-02T17:30')))

    def test_cutoff_includes_nine_pm_candle(self):
        cfg = PaperConfig()
        self.assertTrue(cfg.accepts_signal(timestamp('2026-10-19T21:00')))
        self.assertFalse(cfg.accepts_signal(timestamp('2026-10-19T21:15')))

    def test_no_overnight_signals(self):
        for hour in range(16):
            self.assertFalse(PaperConfig().accepts_signal(timestamp(f'2026-10-19T{hour:02}:00')))

    def test_weekends_configurable(self):
        t = timestamp('2026-10-18T17:00')
        self.assertTrue(PaperConfig().accepts_signal(t))
        self.assertFalse(PaperConfig(weekdays_only=True).accepts_signal(t))

    def test_config_validation(self):
        for kwargs in ({'hold_candles': 0}, {'movement_points': -1}, {'target_points': float('inf')},
                       {'stop_points': -5}, {'start_new_york': '07:01'},
                       {'close_confirmation_seconds': 30}, {'max_entry_delay_seconds': 900}):
            with self.assertRaises(ValueError):
                PaperConfig(**kwargs)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = PaperConfig(fee_bps_per_side=0, slippage_points_per_side=0)
        self.engine = PaperEngine(self.cfg, self.store)
        self.t = timestamp('2026-10-19T16:30')

    def bars(self, move=250, next_open=1250):
        signal = Bar(self.t, 1000, max(1000, 1000+move), min(1000, 1000+move), 1000+move)
        next_bar = Bar(self.t+900, next_open, next_open, next_open, next_open)
        return signal, next_bar

    def enter(self, move=250, next_open=1250):
        bars = self.bars(move, next_open)
        return self.engine.step(bars, self.t+902)

    def trades(self):
        return [json.loads(r['data']) for r in self.store.db.execute('SELECT data FROM trades')]

    def test_strictly_more_than_200_body(self):
        signal = Bar(self.t, 1000, 2000, 500, 1200)
        state = self.engine.step([signal, self.bars()[1]], self.t+902)
        self.assertIsNone(state['position'])

    def test_buy_uses_next_open_not_signal_close(self):
        state = self.enter(next_open=1300)
        self.assertEqual(state['position']['entry_price'], 1300)
        self.assertEqual(state['position']['target_price'], 1500)
        self.assertIsNone(state['position']['stop_price'])

    def test_sell_and_intrabar_target(self):
        self.enter(move=-250, next_open=740)
        current = Bar(self.t+900, 740, 750, 500, 510)
        state = self.engine.step([self.bars(-250, 740)[0], current], self.t+910)
        self.assertIsNone(state['position'])
        self.assertEqual(self.trades()[0]['side'], 'SELL')
        self.assertEqual(self.trades()[0]['exit_price'], 540)
        self.assertEqual(self.trades()[0]['exit_reason'], 'target')

    def test_exact_four_candles_and_no_stop(self):
        self.enter()
        for number in range(1, 5):
            # Large adverse moves don't act as an unrequested stop-loss.
            bar = Bar(self.t+900*number, 1250, 1260, 500, 700)
            next_bar = Bar(bar.time+900, 700, 700, 700, 700)
            state = self.engine.step([bar, next_bar], bar.time+902)
            self.assertEqual(state['position'] is None, number == 4)
        trade = self.trades()[0]
        self.assertEqual(trade['exit_reason'], 'timeout')
        self.assertEqual(trade['exit_time'], self.t+4500)
        self.assertEqual(trade['net_points'], -550)

    def test_duplicate_poll_and_engine_restart(self):
        self.enter()
        self.engine.step(self.bars(), self.t+905)
        restarted = PaperEngine(self.cfg, self.store)
        state = restarted.step(self.bars(), self.t+906)
        self.assertIsNotNone(state['position'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='ENTERED'").fetchone()[0], 1)
        self.assertEqual(len(self.trades()), 1)

    def test_stale_signal_is_never_retroactively_entered(self):
        state = self.engine.step(self.bars(), self.t+925)
        self.assertIsNone(state['position'])
        decision = self.store.db.execute('SELECT decision FROM candles').fetchone()[0]
        self.assertEqual(decision, 'stale_signal_skipped')

    def test_incomplete_next_bar_retried(self):
        signal, next_bar = self.bars()
        state = self.engine.step([signal], self.t+902)
        self.assertIsNone(state['position'])
        state = self.engine.step([signal, next_bar], self.t+905)
        self.assertIsNotNone(state['position'])

    def test_wait_for_closed_bar_confirmation(self):
        state = self.engine.step(self.bars(), self.t+899)
        self.assertIsNone(state['position'])
        state = self.engine.step(self.bars(), self.t+901)
        self.assertIsNone(state['position'])
        state = self.engine.step(self.bars(), self.t+902)
        self.assertIsNotNone(state['position'])

    def test_position_blocks_overlapping_entry(self):
        self.enter()
        held = Bar(self.t+900, 1250, 1250, 900, 950)
        current = Bar(self.t+1800, 950, 950, 950, 950)
        state = self.engine.step([held, current], self.t+1802)
        self.assertEqual(len(self.trades()), 1)
        self.assertEqual(state['position']['side'], 'BUY')

    def test_data_gap_rolls_back_state(self):
        original = self.enter()
        gap_bar = Bar(self.t+1800, 1200, 1250, 1100, 1200)
        with self.assertRaises(DataGapError):
            self.engine.step([gap_bar], self.t+2702)
        self.assertEqual(self.store.load_state(), original)

    def test_config_change_rejected(self):
        self.enter()
        with self.assertRaises(ValueError):
            PaperEngine(replace(self.cfg, target_points=300), self.store).step(self.bars(), self.t+905)

    def test_final_signal_can_finish_at_2215(self):
        self.t = timestamp('2026-10-19T21:00')
        state = self.enter()
        self.assertEqual(state['position']['deadline'], timestamp('2026-10-19T22:15'))

    def test_fees_and_slippage(self):
        self.cfg = replace(self.cfg, fee_bps_per_side=5, slippage_points_per_side=5)
        self.engine = PaperEngine(self.cfg, self.store)
        self.enter()
        current = Bar(self.t+900, 1250, 1500, 1240, 1480)
        self.engine.step([self.bars()[0], current], self.t+910)
        trade = self.trades()[0]
        self.assertEqual(trade['entry_price'], 1255)
        self.assertEqual(trade['exit_price'], 1450)
        self.assertAlmostEqual(trade['net_points'], 195-(1255+1450)*0.0005)

    def test_outbox_retry_does_not_duplicate_trade(self):
        self.enter()
        telegram = Mock(enabled=True)
        telegram.send_telegram_alert.side_effect = RuntimeError('offline')
        now = self.t+905
        self.store.flush_telegram(telegram, now, logging.getLogger('test'))
        row = self.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(row['sent_at'])
        telegram.send_telegram_alert.side_effect = None
        self.store.flush_telegram(telegram, now+10, logging.getLogger('test'))
        self.assertIsNotNone(self.store.db.execute('SELECT sent_at FROM outbox').fetchone()[0])
        self.assertEqual(len(self.trades()), 1)

    def test_replay_does_not_see_future_next_candle_high(self):
        signal, _ = self.bars()
        next_bar = Bar(self.t+900, 1250, 1600, 1200, 1300)
        following = Bar(self.t+1800, 1300, 1300, 1300, 1300)
        result = replay(self.cfg, self.store, [signal, next_bar, following])
        trade = self.trades()[0]
        self.assertEqual(result['target_hits'], 1)
        self.assertEqual(trade['exit_observed_at'], self.t+1802)
        self.assertEqual(trade['entry_observed_at'], self.t+902)


if __name__ == '__main__':
    unittest.main()
