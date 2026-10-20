"""Foreground polling runner for tmux or systemd on AWS."""
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .market_data import DeltaCandles
from .paper_config import BAR_SECONDS
from .paper_engine import PaperEngine
from .paper_store import Store
from .telegram import Telegram, load_env


@contextmanager
def process_lock(path):
    """OS releases this lock even if the process crashes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        file.seek(0)
        file.write(b'0')
        file.flush()
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def run_paper(config, once=False, env_file='.env'):
    load_env(env_file)
    telegram = Telegram()
    log_directory = Path(config.database).parent
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('prime_time.paper')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s')
    formatter.converter = time.gmtime
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        log_directory / 'paper.log', maxBytes=5_000_000, backupCount=5, encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    stopped = threading.Event()
    previous_signals = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_signals[sig] = signal.signal(sig, lambda *_: stopped.set())
    try:
        with process_lock(str(config.database) + '.lock'):
            store = Store(config.database)
            try:
                saved = store.load_state()
                if saved and saved['config'] != config.to_dict():
                    raise ValueError('Configuration differs from persisted run; choose another database')
                engine = PaperEngine(config, store)
                feed = DeltaCandles(config)
                logger.info('Paper service started: %s; Telegram %s', config.symbol,
                            'enabled' if telegram.enabled else 'disabled (outbox retained)')
                failures = 0
                while not stopped.is_set():
                    try:
                        now = time.time()
                        state = store.load_state()
                        if state and state['config'] != config.to_dict():
                            raise ValueError('Configuration differs from persisted run; choose another database')
                        start = int(now // BAR_SECONDS)*BAR_SECONDS - 2*BAR_SECONDS
                        if state and (state['position'] or state.get('pending')):
                            start = state['last_closed'] + BAR_SECONDS
                        # Cap below Delta's 2000-bar limit; catch up in successive polls.
                        end = min(int(now), start + 1000*BAR_SECONDS)
                        bars = feed.fetch(start, end)
                        if not bars:
                            raise RuntimeError('No candle data returned')
                        engine.step(bars, time.time())
                        failures = 0
                    except (ValueError, RuntimeError) as error:
                        # These messages are application controlled; no credential URLs.
                        logger.error('%s', error)
                        failures += 1
                        if once:
                            raise
                    except Exception as error:
                        logger.error('Market polling failed (%s); retrying', type(error).__name__)
                        failures += 1
                        if once:
                            raise RuntimeError('Market polling failed') from None
                    store.flush_telegram(telegram, time.time(), logger)
                    if once:
                        break
                    stopped.wait(min(60, config.poll_seconds * 2**min(failures, 5)))
            finally:
                store.close()
                logger.info('Paper service stopped')
    finally:
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
