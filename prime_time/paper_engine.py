"""Deterministic momentum paper engine shared by polling and replay."""
from datetime import datetime
from .paper_config import BAR_SECONDS
from .session import IST


def clock(timestamp):
    return datetime.fromtimestamp(timestamp, IST).isoformat(timespec='seconds')


def candle_text(bar):
    return (f'{clock(bar.time)} → {clock(bar.time + BAR_SECONDS)}\n'
            f'O {bar.open:g} H {bar.high:g} L {bar.low:g} C {bar.close:g}\n'
            f'Body move {bar.close - bar.open:+.2f} points')


class DataGapError(RuntimeError):
    pass


class PaperEngine:
    def __init__(self, config, store):
        self.cfg, self.store = config, store

    def _close(self, state, bar, raw_exit, reason, now, exact_time=None):
        position = state['position']
        direction = position['direction']
        exit_price = raw_exit - direction * self.cfg.slippage_points_per_side
        gross = direction * (exit_price - position['entry_price'])
        fees = (position['entry_price'] + exit_price) * self.cfg.fee_bps_per_side / 10000
        position.update(status='closed', exit_price=exit_price, exit_reason=reason,
                        exit_observed_at=now, exit_bar_time=bar.time,
                        exit_time=exact_time,
                        gross_points=gross, fee_points=fees, net_points=gross-fees,
                        net_pnl_quote=(gross-fees)*position['quantity'],
                        exit_candle=bar.to_dict())
        self.store.trade(position)
        message = (f'PAPER CLOSED {self.cfg.symbol} {position["side"]}\n'
                   f'Trade {position["id"]}\n{reason.upper()} | '
                   f'Entry {position["entry_price"]:.2f} → Exit {exit_price:.2f}\n'
                   f'Net {gross-fees:+.2f} points | '
                   f'P&L {position["net_pnl_quote"]:+.4f} USD at {position["quantity"]:g} BTC\n'
                   f'Observed {clock(now)}\n'
                   f'{"Closed candle" if bar.time + BAR_SECONDS <= now else "Developing candle (target/stop observed)"}:\n'
                   f'{candle_text(bar)}')
        self.store.event(now, 'CLOSED', dict(position), position['id'], message)
        state['position'] = None

    def _manage(self, state, bar, now, closed):
        position = state['position']
        if position is None or bar.time < position['entry_time']:
            return
        direction = position['direction']
        stop = position['stop_price']
        stop_hit = stop is not None and (bar.low <= stop if direction == 1 else bar.high >= stop)
        target_hit = bar.high >= position['target_price'] if direction == 1 else bar.low <= position['target_price']
        if stop_hit:
            raw = min(stop, bar.open) if direction == 1 else max(stop, bar.open)
            self._close(state, bar, raw, 'stop', now)
        elif target_hit:
            self._close(state, bar, position['target_price'], 'target', now)
        elif closed:
            bars_held = (bar.time - position['entry_time']) // BAR_SECONDS + 1
            if bar.time + BAR_SECONDS >= position['deadline']:
                reason = 'cutoff' if position['deadline'] < position['entry_time'] + self.cfg.hold_candles*BAR_SECONDS else 'timeout'
                self._close(state, bar, bar.close, reason, now, bar.time + BAR_SECONDS)
            else:
                message = (f'PAPER HOLD {self.cfg.symbol} {position["side"]}\n'
                           f'Trade {position["id"]} | Candle {bars_held}/{self.cfg.hold_candles}\n'
                           f'Entry {position["entry_price"]:.2f} | Target {position["target_price"]:.2f}\n'
                           f'Closed candle:\n{candle_text(bar)}')
                self.store.event(now, 'HOLD', {'bar': bar.to_dict(), 'bars_held': bars_held},
                                 position['id'], message)

    def step(self, bars, now):
        by_time = {bar.time: bar for bar in bars}
        if len(by_time) != len(bars):
            raise ValueError('Duplicate bars passed to engine')
        with self.store.transaction():
            state = self.store.load_state()
            if state is None:
                state = {'config': self.cfg.to_dict(), 'position': None,
                         'last_closed': int(now // BAR_SECONDS)*BAR_SECONDS - 2*BAR_SECONDS}
                self.store.event(now, 'STARTED', {'config': self.cfg.to_dict()})
            elif state['config'] != self.cfg.to_dict():
                raise ValueError('Configuration changed for this database; use a new database for a new experiment')

            closed_bars = [bar for t, bar in sorted(by_time.items())
                           if state['last_closed'] < t and t + BAR_SECONDS + self.cfg.close_confirmation_seconds <= now]
            for bar in closed_bars:
                # Never jump over missing data while a position is open.
                if state['position'] and bar.time != state['last_closed'] + BAR_SECONDS:
                    raise DataGapError('Missing candle while managing paper position; waiting for complete history')
                was_open = state['position'] is not None
                if was_open:
                    self._manage(state, bar, now, closed=True)
                    decision = 'position_managed'
                else:
                    decision = 'outside_window'
                    if self.cfg.accepts_signal(bar.time):
                        move = bar.close - bar.open
                        decision = 'below_threshold'
                        if abs(move) > self.cfg.movement_points:
                            entry_time = bar.time + BAR_SECONDS
                            delay = now - entry_time
                            next_bar = by_time.get(entry_time)
                            if delay > self.cfg.max_entry_delay_seconds:
                                decision = 'stale_signal_skipped'
                            elif self.cfg.force_flat_at_cutoff and entry_time >= self.cfg.bounds(bar.time)[1].timestamp():
                                decision = 'cutoff_entry_skipped'
                            elif next_bar is None:
                                # Next-bar open is not available yet; retry until delay limit.
                                break
                            else:
                                direction = 1 if move > 0 else -1
                                entry = next_bar.open + direction*self.cfg.slippage_points_per_side
                                deadline = entry_time + self.cfg.hold_candles*BAR_SECONDS
                                if self.cfg.force_flat_at_cutoff:
                                    deadline = min(deadline, int(self.cfg.bounds(bar.time)[1].timestamp()))
                                position = {
                                    'id': f'{self.cfg.symbol}-{entry_time}', 'symbol': self.cfg.symbol,
                                    'status': 'open', 'side': 'BUY' if direction == 1 else 'SELL',
                                    'direction': direction, 'signal_candle': bar.to_dict(),
                                    'signal_move': move, 'entry_time': entry_time,
                                    'entry_observed_at': now, 'entry_delay_seconds': delay,
                                    'reference_next_open': next_bar.open, 'entry_price': entry,
                                    'target_price': entry + direction*self.cfg.target_points,
                                    'stop_price': None if self.cfg.stop_points is None else entry-direction*self.cfg.stop_points,
                                    'deadline': deadline, 'quantity': self.cfg.quantity,
                                    'config': self.cfg.to_dict(),
                                }
                                state['position'] = position
                                self.store.trade(position)
                                message = (f'PAPER ENTERED {self.cfg.symbol} {position["side"]}\n'
                                           f'Trade {position["id"]}\n'
                                           f'Next open {next_bar.open:.2f} | Fill {entry:.2f} (includes slippage)\n'
                                           f'Target {position["target_price"]:.2f} | Stop {position["stop_price"]}\n'
                                           f'Entry {clock(entry_time)} | Observed delay {delay:.1f}s\n'
                                           f'Exit by {clock(deadline)}\nSignal closed candle:\n{candle_text(bar)}')
                                self.store.event(now, 'ENTERED', dict(position), position['id'], message)
                                decision = 'entered'
                self.store.candle(self.cfg.symbol, bar, decision)
                state['last_closed'] = bar.time

            # Targets are monitored throughout the developing candle, not only at close.
            current_time = int(now // BAR_SECONDS)*BAR_SECONDS
            if state['position']:
                expected = state['last_closed'] + BAR_SECONDS
                if expected < current_time and expected not in by_time:
                    raise DataGapError('Candle history missing for open paper position')
                # During confirmation delay the previous bar may just have ended.
                # It will be processed as a closed candle after confirmation.
                current = by_time.get(current_time)
                if current is not None and expected == current_time:
                    self._manage(state, current, now, closed=False)
            self.store.save_state(state)
        return self.store.load_state()
