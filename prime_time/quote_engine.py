"""Observed executable quote fills; never infer executions from candle extremes."""
from .market_data import Bar
from .paper_config import BAR_SECONDS
from .paper_engine import PaperEngine, candle_text
from .hold_diagnostics import diagnostic_text
from .hold_diagnostics import make_hold_record
from .signal_context import signal_context
from .paper_engine import clock
import json


class QuotePaperEngine(PaperEngine):
    def _close(self, state, bar, raw_exit, reason, now, exact_time=None):
        position = state['position']
        super()._close(state, bar, raw_exit, reason, now, exact_time)
        # An exit may precede candle confirmation or arrive during a history gap.
        # Queue only candles actually completed while this position was held.
        pending = state.setdefault('pending_final_diagnostics', [])
        for candle_time in range(position['entry_time'], int(now//BAR_SECONDS)*BAR_SECONDS, BAR_SECONDS):
            exists = self.store.db.execute(
                'SELECT 1 FROM hold_diagnostics WHERE trade_id=? AND candle_time=?',
                (position['id'], candle_time)).fetchone()
            if not exists:
                pending.append({'trade_id': position['id'], 'candle_time': candle_time})

    def _complete_final_diagnostics(self, state, now):
        pending = state.get('pending_final_diagnostics', [])
        if not pending or not self._candle_check.get('accepted'):
            return
        remaining = []
        for item in pending:
            bar = self._bars.get(item['candle_time'])
            if bar is None or bar.time+BAR_SECONDS+self.cfg.close_confirmation_seconds > now:
                remaining.append(item)
                continue
            row = self.store.db.execute('SELECT data FROM trades WHERE id=?', (item['trade_id'],)).fetchone()
            if row is None:
                raise RuntimeError('Missing closed trade for pending final diagnostic')
            position = json.loads(row[0])
            context = signal_context(bar, self._diagnostic_bars, self.cfg)
            # Never observe a new quote on a closed position: its mark and
            # excursions stay frozen at execution, even on delayed recovery.
            record = make_hold_record(position, bar, context, now, self.cfg)
            record.update(diagnostic_type='post_exit_candle_confirmation',
                          trade_exit_observed_at=position['exit_observed_at'],
                          realized_net_points=position['net_points'],
                          mark_scope='Frozen exit-time quote estimate; not a later unrealized mark',
                          excursion_scope='Valid sampled quotes through exit only',
                          vwap_response_status=self._candle_check.get('status'),
                          vwap_response_accepted=True)
            self.store.hold_diagnostic(record)
            position['last_hold_diagnostics'] = record
            self.store.trade(position)
            vwap = record['vwap_at_candle_close']
            value = f'{vwap:.2f}' if vwap is not None else 'unavailable'
            self.store.event(now, 'FINAL_HOLD_DIAGNOSTIC', record, position['id'],
                f'PAPER FINAL DIAGNOSTIC [{self.cfg.alert_label}] {self.cfg.symbol} {position["side"]}\n'
                f'Trade {position["id"]} | Candle closed {clock(bar.time+BAR_SECONDS)}\n'
                f'VWAP {value} ({self.cfg.vwap_session_timezone}) | {record["vwap_status"]}\n'
                f'Exit-time net {position["net_points"]:+.2f} points | Exit {clock(position["exit_observed_at"])}\n'
                f'Observed quote MFE {record["mfe_points"]} | MAE {record["mae_points"]} points\n'
                f'Candle context confirmed {clock(now)}; quote mark and extrema frozen at exit')
        state['pending_final_diagnostics'] = remaining

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
        self._complete_final_diagnostics(state, now)

    def step(self, bars, now, quote_snapshot=None, mode='live', candle_evidence=None):
        if mode != 'live':
            raise ValueError('Quote execution needs timestamped live quotes; OHLC-only replay cannot reproduce it')
        self._bars = {bar.time: bar for bar in bars}
        return super().step(bars, now, quote_snapshot, mode, candle_evidence)
