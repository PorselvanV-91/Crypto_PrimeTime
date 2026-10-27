"""One ordered REST observation stream, isolated strategy ledgers and alert workers."""
import copy
import json
import logging
import signal
import threading
import time
import uuid
from contextlib import ExitStack
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .market_data import DeltaCandles
from .paper_config import BAR_SECONDS
from .paper_engine import create_engine
from .paper_runner import process_lock
from .paper_store import Store
from .signal_context import history_start
from .telegram import Telegram, load_env


def validate_configs(configs):
    if not configs:
        raise ValueError('At least one paper configuration is required')
    paths = [Path(c.database).resolve() for c in configs]
    if len(set(paths)) != len(paths):
        raise ValueError('Each strategy must use a distinct database')
    if len(configs) > 1:
        if any(c.execution_mode != 'quote' for c in configs):
            raise ValueError('Shared comparison requires quote execution')
        def common(c):
            return {k: v for k, v in c.to_dict().items()
                    if k not in ('database', 'strategy_name', 'trend_filter')}
        if any(common(c) != common(configs[0]) for c in configs[1:]):
            raise ValueError('Shared configurations may differ only in database, strategy_name and trend_filter')


class TelegramWorker:
    """Own connection; no SQLite transaction or engine lock during network I/O."""
    def __init__(self, database, telegram, logger):
        self.database, self.telegram = database, telegram
        self.logger = logger.getChild(Path(database).stem)
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name='telegram-'+Path(database).stem, daemon=True)

    def start(self):
        self.thread.start()

    def notify(self):
        self.wake.set()

    def _run(self):
        store = None
        try:
            store = Store(self.database)
            while not self.stopped.is_set():
                self.wake.clear()
                try:
                    store.flush_telegram(self.telegram, time.time(), self.logger, self.stopped)
                except Exception as error:
                    self.logger.error('Telegram worker database failure (%s); outbox retained', type(error).__name__)
                self.wake.wait(1)
        except Exception as error:
            self.logger.error('Telegram worker stopped (%s); outbox retained', type(error).__name__)
        finally:
            if store:
                store.close()

    def close(self):
        self.stopped.set()
        self.wake.set()
        # Only an in-flight request can remain; queued alerts survive restart.
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            self.logger.error('Telegram worker shutdown timed out; delivery may be retried on restart')


class SharedPoller:
    def __init__(self, configs, stores, logger, feed=None):
        validate_configs(configs)
        if len(stores) != len(configs):
            raise ValueError('One store is required per configuration')
        self.configs, self.stores, self.logger = configs, stores, logger
        for cfg, store in zip(configs, stores):
            saved = store.load_state()
            if saved and not cfg.matches_saved(saved['config']):
                raise ValueError('Configuration differs from persisted run; choose another database')
        self.engines = [create_engine(c, s) for c, s in zip(configs, stores)]
        self.feed = feed if feed is not None else DeltaCandles(configs[0])
        self.stream_id, self.sequence = uuid.uuid4().hex, 0
        self.previous_start = None
        self.last_health = [None]*len(configs)

    def poll(self):
        timer, wall = time.monotonic(), time.time()
        timing = {'stream_id': self.stream_id, 'sequence': self.sequence,
                  'poll_started_at': wall,
                  'poll_interval_seconds': None if self.previous_start is None else timer-self.previous_start}
        self.previous_start = timer
        self.sequence += 1
        try:
            starts = []
            for cfg, store in zip(self.configs, self.stores):
                state = store.load_state()
                if state and not cfg.matches_saved(state['config']):
                    raise ValueError('Configuration differs from persisted run')
                start = int(wall//BAR_SECONDS)*BAR_SECONDS-2*BAR_SECONDS
                if state and (state['position'] or state.get('pending')):
                    start = state['last_closed']+BAR_SECONDS
                start = min(start, history_start(wall, cfg))
                for item in (state or {}).get('pending_final_diagnostics', []):
                    start = min(start, history_start(item['candle_time'], cfg))
                starts.append(start)
            start = min(starts)
            fetch_timer = time.monotonic()
            try:
                bars = self.feed.fetch(start, min(int(wall), start+1000*BAR_SECONDS))
                if not bars:
                    raise RuntimeError('No candle data returned')
            except Exception as error:
                if self.configs[0].execution_mode != 'quote':
                    raise
                bars = []
                self.logger.warning('Candle feed unavailable (%s); fresh quote exits remain active', type(error).__name__)
            timing['candle_fetch_seconds'] = time.monotonic()-fetch_timer
            fetch_timer = time.monotonic()
            try:
                quote = self.feed.fetch_quote()
            except Exception as error:
                quote = None
                self.logger.warning('Quote unavailable (%s); waiting for fresh data', type(error).__name__)
            timing['quote_fetch_seconds'] = time.monotonic()-fetch_timer
            # One observation clock, not one clock read per strategy. Sequence is
            # recorded in both feed ledgers for exact cross-strategy matching.
            observed_at = time.time()
            observation_timer = time.monotonic()
            identity = dict(stream_id=self.stream_id, sequence=timing['sequence'], observed_at=observed_at)
            if quote is not None:
                quote = dict(quote, shared_observation=identity)
            evidence = dict(self.feed.last_candle_evidence or {}, shared_observation=identity)
            timing['observation_at'] = observed_at
            timing['engines'] = []
            failures = []
            for index, (cfg, engine) in enumerate(zip(self.configs, self.engines)):
                engine_timer = time.monotonic()
                try:
                    state = engine.step(bars, observed_at, quote_snapshot=copy.deepcopy(quote),
                                        mode='live', candle_evidence=copy.deepcopy(evidence))
                    health = state.get('feed_health')
                    if health != self.last_health[index]:
                        self.logger.info('Feed validation [%s]: %s', cfg.alert_label, health)
                        self.last_health[index] = health
                except Exception as error:
                    failures.append(type(error).__name__)
                    self.logger.error('Strategy step failed [%s] (%s)', cfg.alert_label, type(error).__name__)
                finally:
                    timing['engines'].append({'strategy': cfg.strategy_name,
                        'dispatch_delay_seconds': engine_timer-timer,
                        'observation_to_step_seconds': engine_timer-observation_timer,
                        'step_seconds': time.monotonic()-engine_timer})
            if failures:
                # Never silently continue a comparison with one failed ledger.
                raise RuntimeError('Shared strategy step failed; inspect logs before restarting')
            return timing
        finally:
            timing['poll_work_seconds'] = time.monotonic()-timer
            self.logger.info('POLL_TIMING %s', json.dumps(timing, separators=(',', ':')))


def run_shared(configs, once=False, env_file='.env'):
    validate_configs(configs)
    load_env(env_file)
    telegram = Telegram()
    directory = Path(configs[0].database).parent
    directory.mkdir(parents=True, exist_ok=True)
    log_name = 'paper_shared' if len(configs) > 1 else Path(configs[0].database).stem
    logger = logging.getLogger('prime_time.'+log_name)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(name)s %(message)s')
    formatter.converter = time.gmtime
    handlers = [logging.StreamHandler(), RotatingFileHandler(directory/(log_name+'.log'),
                 maxBytes=5_000_000, backupCount=5, encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    stopped = threading.Event()
    previous = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: stopped.set())
        with ExitStack() as resources:
            # Lock every existing database before starting any worker or polling.
            for cfg in sorted(configs, key=lambda c: str(Path(c.database).resolve())):
                resources.enter_context(process_lock(str(cfg.database)+'.lock'))
            stores = []
            for cfg in configs:
                store = Store(cfg.database)
                resources.callback(store.close)
                stores.append(store)
            poller = SharedPoller(configs, stores, logger)
            workers = []
            if telegram.enabled:
                for cfg in configs:
                    worker = TelegramWorker(cfg.database, telegram, logger)
                    worker.start()
                    resources.callback(worker.close)
                    workers.append(worker)
            logger.info('Paper stream started: %s; Telegram %s',
                        ', '.join(c.alert_label for c in configs), 'background workers' if workers else 'disabled; outbox retained')
            while not stopped.is_set():
                poller.poll()
                for worker in workers:
                    worker.notify()
                if once:
                    break
                # Preserve the existing configured wait; measured intervals also
                # include fetch, processing and scheduling time.
                stopped.wait(configs[0].poll_seconds)
    finally:
        logger.info('Paper stream stopped')
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
