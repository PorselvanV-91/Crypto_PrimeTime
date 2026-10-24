"""Observed executable quote fills; never infer executions from candle extremes."""
from .market_data import Bar
from .paper_config import BAR_SECONDS
from .paper_engine import PaperEngine, candle_text
from .hold_diagnostics import diagnostic_text


class QuotePaperEngine(PaperEngine):
    def _can_arm(self, bar, now):
        return self._mode == 'live' and self._candle_check['accepted']

    def _entry_reference(self, pending, bar, now):
        if not self._can_arm(bar, now) or now >= pending['expires_at']:
            return None
        snapshot = self._quote_for(bar, now)
        if not snapshot or self._quote_check['exchange_timestamp'] < pending['armed_at']-self.cfg.clock_skew_tolerance_seconds:
            return None
        side = snapshot['ask'] if pending['direction'] == 1 else snapshot['bid']
        triggered = side >= pending['trigger_price'] if pending['direction'] == 1 else side <= pending['trigger_price']
        return side if triggered else None

    def _request_exit(self, position, reason, when, now, bar=None):
        if not position.get('requested_exit'):
            position['requested_exit'] = {'reason': reason, 'requested_at': when,
                                          'signal_candle': bar.to_dict() if bar else None}
            self.store.event(now, 'EXIT_REQUESTED', dict(position['requested_exit']), position['id'])

    def _exit_on_quote(self, state, now):
        position = state['position']
        if position is None:
            return
        if now >= position['deadline']:
            full_deadline = position['entry_time']+self.cfg.hold_candles*BAR_SECONDS
            self._request_exit(position, 'cutoff' if position['deadline'] < full_deadline else 'timeout', position['deadline'], now)
        if self._mode != 'live' or not self._quote_check['accepted']:
            self.store.trade(position)  # Persist any deferred exit through restart.
            return
        snapshot = self._quote
        # A quote must not predate the modeled entry observation.
        if self._quote_check['exchange_timestamp'] < position['entry_fill_observed_at']-self.cfg.clock_skew_tolerance_seconds:
            return
        direction = position['direction']
        executable = snapshot['bid'] if direction == 1 else snapshot['ask']
        stop = position['stop_price']
        reason = None
        if stop is not None and direction*(executable-stop) <= 0:
            reason = 'protective_stop'
        elif position.get('requested_exit'):
            reason = position['requested_exit']['reason']
        elif direction*(executable-position['target_price']) >= 0:
            reason = 'target'
        if reason is None:
            return
        current_time = int(now//BAR_SECONDS)*BAR_SECONDS
        bar = self._bars.get(current_time)
        position['exit_candle_source'] = 'market_candle' if bar else 'synthetic_quote_only'
        if bar is None:
            # Keep quote-based risk management active during candle endpoint failure.
            bar = Bar(current_time, executable, executable, executable, executable)
        position.update(exit_fill_model='observed_bid_minus_slippage' if direction == 1 else 'observed_ask_plus_slippage',
                        exit_quote_exchange_timestamp=self._quote_check['exchange_timestamp'],
                        exit_fill_observed_at=now)
        # Exchange timestamp describes market data, not our modeled execution time.
        self._close(state, bar, executable, reason, now, now)

    def _manage(self, state, bar, now, closed):
        position = state['position']
        if position is None or bar.time < position['entry_time']:
            return
        if closed:
            diagnostic = self._record_hold_diagnostics(position, bar, now)
            direction = position['direction']
            if (self.cfg.opposite_candle_stop_points is not None and
                    direction*(bar.close-bar.open) <= -self.cfg.opposite_candle_stop_points):
                self._request_exit(position, 'opposite_candle_stop', bar.time+BAR_SECONDS, now, bar)
            elif bar.time+BAR_SECONDS >= position['deadline']:
                full_deadline = position['entry_time']+self.cfg.hold_candles*BAR_SECONDS
                self._request_exit(position, 'cutoff' if position['deadline'] < full_deadline else 'timeout', position['deadline'], now, bar)
            elif not position.get('requested_exit'):
                held = (bar.time-position['entry_time'])//BAR_SECONDS+1
                self.store.event(now, 'HOLD', {'bar': bar.to_dict(), 'bars_held': held, 'diagnostics': diagnostic}, position['id'],
                    f'PAPER HOLD [{self.cfg.alert_label}] {self.cfg.symbol} {position["side"]}\n'
                    f'Trade {position["id"]} | Candle {held}/{self.cfg.hold_candles}\n'
                    f'Entry {position["entry_price"]:.2f} | Target {position["target_price"]:.2f}\n'
                    f'Protective stop {position["stop_price"]}\nClosed candle:\n{candle_text(bar)}\n{diagnostic_text(diagnostic)}')
        self._exit_on_quote(state, now)

    def _after_bars(self, state, now):
        pending = state.get('pending')
        if pending and now >= pending['expires_at']:
            self._expire_pending(state, now)
        self._exit_on_quote(state, now)

    def step(self, bars, now, quote_snapshot=None, mode='live', candle_evidence=None):
        if mode != 'live':
            raise ValueError('Quote execution needs timestamped live quotes; OHLC-only replay cannot reproduce it')
        self._bars = {bar.time: bar for bar in bars}
        return super().step(bars, now, quote_snapshot, mode, candle_evidence)
