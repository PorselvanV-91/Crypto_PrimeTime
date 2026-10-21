"""Deterministic momentum paper engine shared by polling and replay."""
from datetime import datetime
from .market_data import Bar
from .paper_config import BAR_SECONDS
from .session import IST
from .feed_quality import quote_quality, candle_quality


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
        if type(self) is PaperEngine and config.execution_mode == 'quote':
            raise ValueError('Use create_engine() for quote execution mode')
        self.cfg, self.store = config, store
        self._quote = None
        self._mode = 'candle_model'
        self._quote_check = {'accepted': False, 'status': 'missing_quote'}
        self._candle_evidence = None
        self._candle_check = {}

    def _quote_for(self, bar, now):
        snapshot = self._quote
        if (self._mode == 'live' and snapshot and
                bar.time == int(now // BAR_SECONDS)*BAR_SECONDS and
                self._quote_check['accepted']):
            return snapshot
        return None

    def _audit(self, bar, now):
        snapshot = self._quote_for(bar, now)
        if self._mode != 'live':
            status = 'not_collected_' + self._mode
        elif bar.time + BAR_SECONDS <= now:
            status = 'historical_candle_no_matching_quote'
        else:
            status = 'available' if snapshot else 'missing_or_stale'
        return {'candle_snapshot': bar.to_dict(), 'snapshot_observed_at': now,
                'quote': snapshot, 'quote_status': status, 'execution_origin': self._mode,
                'quote_validation': dict(self._quote_check), 'candle_validation': dict(self._candle_check),
                'candle_response_evidence': self._candle_evidence,
                'execution_mode': self.cfg.execution_mode}

    def _can_arm(self, bar, now):
        return True

    def _after_bars(self, state, now):
        pass

    def _entry_reference(self, pending, bar, now):
        direction, trigger = pending['direction'], pending['trigger_price']
        hit = bar.high >= trigger if direction == 1 else bar.low <= trigger
        if not hit:
            return None
        return max(trigger, bar.open) if direction == 1 else min(trigger, bar.open)

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
        audit = self._audit(bar, now)
        position.update(exit_audit=audit, exit_reference_price=raw_exit,
                        exit_slippage_points=self.cfg.slippage_points_per_side,
                        exit_bid=audit['quote']['bid'] if audit['quote'] else None,
                        exit_ask=audit['quote']['ask'] if audit['quote'] else None)
        position['execution_mode'] = self.cfg.execution_mode
        self.store.trade(position)
        message = (f'PAPER CLOSED [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {position["side"]}\n'
                   f'Trade {position["id"]}\n{reason.upper()} | '
                   f'Entry {position["entry_price"]:.2f} → Exit {exit_price:.2f}\n'
                   f'Gross {gross:+.2f} | Fees {fees:.2f} | Net {gross-fees:+.2f} points\n'
                   f'Exit quote {audit["quote_status"]} | Bid/ask {position["exit_bid"]}/{position["exit_ask"]}\n'
                   f'Quote check {self._quote_check["status"]} | Exchange age '
                   f'{self._quote_check.get("exchange_age_seconds")}s\n'
                   f'P&L {position["net_pnl_quote"]:+.4f} USD at {position["quantity"]:g} BTC\n'
                   f'Observed {clock(now)}\n'
                   f'Candle source {position.get("exit_candle_source", "market_candle")}\n'
                   f'{"Closed candle" if bar.time + BAR_SECONDS <= now else "Developing candle snapshot"}:\n'
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
        quote_snapshot = self._quote_for(bar, now)
        observed_exit = (quote_snapshot['bid'] if direction == 1 else quote_snapshot['ask']) if quote_snapshot else None
        # A wick seen before an intrabar entry is not a post-entry stop hit.
        baseline = position.get('entry_candle_snapshot')
        if (stop is not None and baseline and position.get('entry_execution_origin') == 'live' and
                position.get('entry_timing') == 'intrabar_touch_time_unknown' and
                bar.time == position['entry_time']):
            new_extreme = bar.low < baseline['low'] if direction == 1 else bar.high > baseline['high']
            close_beyond = bar.close <= stop if direction == 1 else bar.close >= stop
            stop_hit = (new_extreme and stop_hit) or close_beyond
        if stop is not None and observed_exit is not None:
            stop_hit = stop_hit or (observed_exit <= stop if direction == 1 else observed_exit >= stop)
        target_hit = bar.high >= position['target_price'] if direction == 1 else bar.low <= position['target_price']
        if stop_hit:
            raw = stop
            if bar.time > position['entry_time'] or position.get('entry_timing') == 'bar_open':
                raw = min(raw, bar.open) if direction == 1 else max(raw, bar.open)
            if observed_exit is not None:
                raw = min(raw, observed_exit) if direction == 1 else max(raw, observed_exit)
            elif self._mode == 'live' and not closed:
                raw = min(raw, bar.close) if direction == 1 else max(raw, bar.close)
            position['exit_both_levels_in_candle'] = bool(target_hit)
            position['exit_ordering_assumption'] = 'stop_first_ohlc_order_unknown' if target_hit else None
            if (not target_hit and bar.time == position['entry_time'] and
                    position.get('entry_timing') == 'intrabar_touch_time_unknown' and
                    position.get('entry_execution_origin') != 'live'):
                position['exit_ordering_assumption'] = 'entry_and_stop_order_unknown_conservative_stop'
            self._close(state, bar, raw, 'protective_stop', now)
        elif target_hit:
            self._close(state, bar, position['target_price'], 'target', now)
        elif closed:
            bars_held = (bar.time - position['entry_time']) // BAR_SECONDS + 1
            if (self.cfg.opposite_candle_stop_points is not None and
                    direction * (bar.close - bar.open) <= -self.cfg.opposite_candle_stop_points):
                self._close(state, bar, bar.close, 'opposite_candle_stop', now, bar.time + BAR_SECONDS)
            elif bar.time + BAR_SECONDS >= position['deadline']:
                reason = 'cutoff' if position['deadline'] < position['entry_time'] + self.cfg.hold_candles*BAR_SECONDS else 'timeout'
                self._close(state, bar, bar.close, reason, now, bar.time + BAR_SECONDS)
            else:
                message = (f'PAPER HOLD [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {position["side"]}\n'
                           f'Trade {position["id"]} | Candle {bars_held}/{self.cfg.hold_candles}\n'
                           f'Entry {position["entry_price"]:.2f} | Target {position["target_price"]:.2f}\n'
                           f'Closed candle:\n{candle_text(bar)}')
                self.store.event(now, 'HOLD', {'bar': bar.to_dict(), 'bars_held': bars_held},
                                 position['id'], message)

    def _fill_pending(self, state, bar, now):
        pending = state.get('pending')
        if not pending or bar.time != pending['entry_time']:
            return False
        direction = pending['direction']
        trigger = pending['trigger_price']
        raw = self._entry_reference(pending, bar, now)
        if raw is None:
            return False
        # Stop-entry gaps fill at the worse opening price, never at a better stale trigger.
        entry = raw + direction*self.cfg.slippage_points_per_side
        deadline = bar.time + self.cfg.hold_candles*BAR_SECONDS
        if self.cfg.force_flat_at_cutoff:
            deadline = min(deadline, int(self.cfg.bounds(pending['signal_candle']['time'])[1].timestamp()))
        position = dict(pending)
        position.update(status='open', entry_observed_at=now, entry_price=entry,
                        reference_next_open=bar.open, reference_fill_price=raw,
                        entry_delay_seconds=now-bar.time,
                        entry_timing='bar_open' if raw == bar.open else 'intrabar_touch_time_unknown',
                        target_price=entry + direction*self.cfg.target_points,
                        stop_price=None if self.cfg.stop_points is None else entry-direction*self.cfg.stop_points,
                        deadline=deadline, quantity=self.cfg.quantity, config=self.cfg.to_dict())
        audit = self._audit(bar, now)
        snapshot = audit['quote']
        side_quote = (snapshot['ask'] if direction == 1 else snapshot['bid']) if snapshot else None
        position.update(entry_candle_snapshot=bar.to_dict(), entry_audit=audit,
                        execution_mode=self.cfg.execution_mode,
                        entry_execution_origin='historical_recovery' if self._mode == 'live' and bar.time+BAR_SECONDS <= now else self._mode,
                        entry_fill_model='gap_open_plus_slippage' if raw == bar.open else 'trigger_plus_slippage',
                        entry_slippage_points=self.cfg.slippage_points_per_side,
                        entry_bid=snapshot['bid'] if snapshot else None,
                        entry_ask=snapshot['ask'] if snapshot else None,
                        entry_spread_points=snapshot['spread_points'] if snapshot else None,
                        entry_vs_observed_side_points=direction*(entry-side_quote) if side_quote is not None else None)
        if self.cfg.execution_mode == 'quote':
            position.update(entry_fill_model='observed_ask_plus_slippage' if direction == 1 else 'observed_bid_minus_slippage',
                            entry_timing='observed_quote', entry_quote_exchange_timestamp=self._quote_check['exchange_timestamp'],
                            entry_fill_observed_at=now)
        state['position'], state['pending'] = position, None
        self.store.trade(position)
        signal = pending['signal_candle']
        message = (f'PAPER ENTERED [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {position["side"]}\n'
                   f'Trade {position["id"]}\nTrigger {trigger:.2f} | Fill {entry:.2f} (includes slippage)\n'
                   f'Target {position["target_price"]:.2f} | Protective stop {position["stop_price"]}\n'
                   f'Fill model {position["entry_fill_model"]} | Quote {audit["quote_status"]}\n'
                   f'Quote check {self._quote_check["status"]} | Exchange age '
                   f'{self._quote_check.get("exchange_age_seconds")}s\n'
                   f'Observed bid/ask {position["entry_bid"]}/{position["entry_ask"]}\n'
                   f'Spread {position["entry_spread_points"]} | Fill vs observed side '
                   f'{position["entry_vs_observed_side_points"]} points\n'
                   f'Opposite candle stop '
                   f'{self.cfg.opposite_candle_stop_points} points at close\n'
                   f'Entry candle {clock(bar.time)} | Observed {clock(now)}\n'
                   f'Exit by {clock(deadline)}\nEntry observation candle snapshot:\n{candle_text(bar)}\n'
                   f'Signal closed candle:\n{candle_text(Bar(**signal))}')
        self.store.event(now, 'ENTERED', dict(position), position['id'], message)
        return True

    def step(self, bars, now, quote_snapshot=None, mode='candle_model', candle_evidence=None):
        if mode not in ('live', 'replay', 'candle_model'):
            raise ValueError('Unknown execution evidence mode')
        self._quote, self._mode = quote_snapshot, mode
        self._candle_evidence = candle_evidence
        self._candle_check = candle_quality(candle_evidence, bars, now, self.cfg)
        by_time = {bar.time: bar for bar in bars}
        if len(by_time) != len(bars):
            raise ValueError('Duplicate bars passed to engine')
        with self.store.transaction():
            state = self.store.load_state()
            if state is None:
                state = {'config': self.cfg.to_dict(), 'position': None, 'pending': None,
                         'last_closed': int(now // BAR_SECONDS)*BAR_SECONDS - 2*BAR_SECONDS}
                self.store.event(now, 'STARTED', {'config': self.cfg.to_dict()})
            elif state['config'] != self.cfg.to_dict():
                raise ValueError('Configuration changed for this database; use a new database for a new experiment')
            self._quote_check = quote_quality(quote_snapshot, now, self.cfg, state.get('last_quote_exchange_timestamp'))
            if self._quote_check['accepted']:
                state['last_quote_exchange_timestamp'] = self._quote_check['exchange_timestamp']
            health = {'quote': self._quote_check['status'], 'candles': self._candle_check['status']}
            if mode == 'live' and health != state.get('feed_health'):
                self.store.event(now, 'FEED_HEALTH', health)
                state['feed_health'] = health
            if mode == 'live':
                self.store.feed_snapshot(now, self.cfg.execution_mode, {
                    'quote': quote_snapshot, 'quote_validation': self._quote_check,
                    'candle_response': candle_evidence, 'candle_validation': self._candle_check})

            closed_bars = [bar for t, bar in sorted(by_time.items())
                           if state['last_closed'] < t and t + BAR_SECONDS + self.cfg.close_confirmation_seconds <= now]
            for bar in closed_bars:
                # Never jump over missing data while a position is open.
                if (state['position'] or state.get('pending')) and bar.time != state['last_closed'] + BAR_SECONDS:
                    if self.cfg.execution_mode != 'quote':
                        raise DataGapError('Missing candle while managing paper position; waiting for complete history')
                    if not state.get('data_gap_reported'):
                        self.store.event(now, 'CANDLE_HISTORY_GAP', {'expected': state['last_closed']+BAR_SECONDS, 'received': bar.time})
                        state['data_gap_reported'] = True
                    break  # Still manage fresh executable quotes in _after_bars.
                state.pop('data_gap_reported', None)
                had_pending = state.get('pending') is not None
                if had_pending:
                    self._fill_pending(state, bar, now)
                    if state.get('pending'):
                        pending = state['pending']
                        self.store.event(now, 'ENTRY_EXPIRED', dict(pending), pending['id'],
                            f'PAPER ENTRY EXPIRED [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {pending["side"]}\n'
                            f'Trade setup {pending["id"]} | Trigger {pending["trigger_price"]:.2f}\n'
                            f'No fill during the next candle:\n{candle_text(bar)}')
                        state['pending'] = None
                was_open = state['position'] is not None
                if was_open:
                    self._manage(state, bar, now, closed=True)
                    decision = 'position_managed'
                else:
                    decision = 'outside_window'
                    if self.cfg.accepts_signal(bar.time) and self._can_arm(bar, now):
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
                            elif self.cfg.entry_buffer_points is not None:
                                direction = 1 if move > 0 else -1
                                trigger = (bar.high + self.cfg.entry_buffer_points if direction == 1
                                           else bar.low - self.cfg.entry_buffer_points)
                                if trigger <= 0:
                                    raise ValueError('Nonpositive SELL trigger; check instrument and buffer')
                                pending = {
                                    'id': f'{self.cfg.symbol}-{entry_time}', 'symbol': self.cfg.symbol,
                                    'direction': direction, 'side': 'BUY' if direction == 1 else 'SELL',
                                    'signal_candle': bar.to_dict(), 'signal_move': move,
                                    'entry_time': entry_time, 'trigger_price': trigger,
                                    'armed_at': now, 'expires_at': entry_time+BAR_SECONDS,
                                }
                                state['pending'] = pending
                                self.store.event(now, 'ENTRY_ARMED', pending, pending['id'],
                                    f'PAPER ENTRY ARMED [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {pending["side"]}\n'
                                    f'Trigger {trigger:.2f} | Valid until {clock(pending["expires_at"])}\n'
                                    f'Signal closed candle:\n{candle_text(bar)}')
                                decision = 'entry_armed'
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
                                    'execution_mode': self.cfg.execution_mode,
                                }
                                audit = self._audit(next_bar, now)
                                position.update(entry_audit=audit, entry_candle_snapshot=next_bar.to_dict(),
                                                entry_execution_origin=self._mode, entry_timing='bar_open',
                                                entry_fill_model='next_open_plus_slippage',
                                                entry_slippage_points=self.cfg.slippage_points_per_side,
                                                entry_bid=audit['quote']['bid'] if audit['quote'] else None,
                                                entry_ask=audit['quote']['ask'] if audit['quote'] else None,
                                                entry_spread_points=audit['quote']['spread_points'] if audit['quote'] else None)
                                state['position'] = position
                                self.store.trade(position)
                                message = (f'PAPER ENTERED [{self.cfg.execution_mode.upper()}] {self.cfg.symbol} {position["side"]}\n'
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
            if state['position'] or state.get('pending'):
                expected = state['last_closed'] + BAR_SECONDS
                if expected < current_time and expected not in by_time:
                    if self.cfg.execution_mode != 'quote':
                        raise DataGapError('Candle history missing for open paper position')
                # During confirmation delay the previous bar may just have ended.
                # It will be processed as a closed candle after confirmation.
                current = by_time.get(current_time)
                if current is not None and expected == current_time:
                    self._fill_pending(state, current, now)
                    self._manage(state, current, now, closed=False)
            self._after_bars(state, now)
            self.store.save_state(state)
        return self.store.load_state()


def create_engine(config, store):
    if config.execution_mode == 'quote':
        from .quote_engine import QuotePaperEngine
        return QuotePaperEngine(config, store)
    return PaperEngine(config, store)
