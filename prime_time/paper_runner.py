"""Foreground paper runner and cross-platform database process lock."""
import os
from contextlib import contextmanager
from pathlib import Path


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
    # Single-strategy users also get asynchronous delivery and timing logs.
    from .shared_runner import run_shared
    return run_shared([config], once=once, env_file=env_file)
