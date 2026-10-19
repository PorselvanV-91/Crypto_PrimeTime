from io import StringIO
from unittest.mock import patch
import unittest
from datetime import date, timedelta
from prime_time.backtest import Candle, Config, load_candles, run
from prime_time.session import IST, session_open


class SessionTests(unittest.TestCase):
    def test_dst_boundaries(self):
        for day, expected in [('2026-03-06', 20), ('2026-03-09', 19),
                              ('2026-10-30', 19), ('2026-11-02', 20)]:
            opening = session_open(date.fromisoformat(day)).astimezone(IST)
            self.assertEqual((opening.hour, opening.minute), (expected, 0))

    def test_closures_and_new_year_exception(self):
        for day in ['2026-07-03', '2026-04-03', '2026-10-18', '2025-01-09']:
            self.assertIsNone(session_open(date.fromisoformat(day)))
        self.assertIsNotNone(session_open(date(2027, 12, 31)))
        with self.assertRaises(ValueError):
            session_open(date(2029, 1, 2))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(min_range_points=0, max_range_points=1000,
                          fee_bps_per_side=0, slippage_points_per_side=0)
        opening = session_open(date(2026, 10, 19))
        self.bars = [Candle(opening + timedelta(minutes=5*n), 1000, 1020, 980, 1000, 1)
                     for n in range(24)]

    def replace(self, n, o, h, l, c):
        self.bars[n] = Candle(self.bars[n].timestamp, o, h, l, c, 1)

    def test_next_bar_entry_and_ambiguous_stop(self):
        self.replace(3, 1000, 1045, 980, 1040)
        self.replace(4, 1100, 1350, 900, 1200)
        trades, _ = run(self.bars, self.cfg)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['entry'], 1100)
        self.assertEqual(trades[0]['reason'], 'stop')
        self.assertEqual(trades[0]['net_points'], -110)
        self.assertEqual(trades[0]['entry_time'], self.bars[4].timestamp.isoformat())

    def test_short_target(self):
        self.replace(3, 1000, 1020, 950, 960)
        self.replace(4, 960, 970, 700, 720)
        trades, _ = run(self.bars, self.cfg)
        self.assertEqual(trades[0]['side'], 'short')
        self.assertEqual(trades[0]['net_points'], 220)

    def test_gap_stop_fills_at_worse_open(self):
        self.replace(3, 1000, 1050, 980, 1040)
        self.replace(4, 1040, 1050, 1000, 1020)
        self.replace(5, 800, 850, 750, 800)
        trades, _ = run(self.bars, self.cfg)
        self.assertEqual(trades[0]['exit'], 800)

    def test_missing_bar_skips_session(self):
        trades, audit = run(self.bars[:-1], self.cfg)
        self.assertEqual(trades, [])
        self.assertEqual(audit[0]['status'], 'incomplete_session')

    def test_timeout_and_costs(self):
        self.replace(3, 1000, 1050, 980, 1040)
        trades, _ = run(self.bars, Config(min_range_points=0))
        trade = trades[0]
        self.assertEqual(trade['reason'], 'timeout')
        self.assertAlmostEqual(trade['net_points'], -10 - (1005 + 995)*0.0005)
        self.assertEqual(trade['exit_bar_open_time'], self.bars[9].timestamp.isoformat())

    def test_no_future_range_leak(self):
        self.replace(4, 1000, 1500, 900, 1000)
        trades, _ = run(self.bars, self.cfg)
        self.assertEqual(trades, [])  # A wick alone is not a closed-bar breakout.

    def test_duplicate_csv_rejected(self):
        row = '2026-10-19 13:30:00,100,110,90,100,1\n'
        with patch('builtins.open', return_value=StringIO(
                'timestamp,open,high,low,close,volume\n' + row + row)):
            with self.assertRaises(ValueError):
                load_candles('bars.csv', 5)

    def test_bad_config_rejected(self):
        for kwargs in [{'bar_minutes': 0}, {'target_points': -1},
                       {'max_hold_minutes': 7}, {'fee_bps_per_side': float('nan')}]:
            with self.assertRaises(ValueError):
                Config(**kwargs)


if __name__ == '__main__':
    unittest.main()
