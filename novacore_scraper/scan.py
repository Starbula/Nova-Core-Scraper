"""Bounded console-level scanning; progress is emitted by its coordinator only."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
import threading
from typing import Callable
from .library import discover_systems, scan_system, validate_retrobat
from .models import Game, System

# Two concurrent enumerations overlap slow filesystem calls without flooding a
# portable hard drive with a worker for every configured system.
CONSOLE_WORKERS = 2


def scan_library(root: Path, *, cancelled: Callable[[], bool] = lambda: False,
                 progress: Callable[[int, int, str], None] = lambda *args: None
                 ) -> tuple[Path, list[System], dict[str, list[Game]]] | None:
    if cancelled():
        return None
    root = validate_retrobat(root)
    systems = discover_systems(root)
    if cancelled():
        return None
    progress(0, len(systems), 'Reading game libraries…')
    games = {}
    stopped = threading.Event()
    def should_stop():
        return stopped.is_set() or cancelled()
    remaining = iter(systems)
    with ThreadPoolExecutor(max_workers=CONSOLE_WORKERS, thread_name_prefix='Console scan') as pool:
        pending = {}
        def fill():
            while len(pending) < CONSOLE_WORKERS and not cancelled():
                system = next(remaining, None)
                if system is None:
                    break
                pending[pool.submit(scan_system, system, cancelled=should_stop)] = system
        try:
            fill()
            while pending:
                if cancelled():
                    return None
                finished, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
                for future in finished:
                    system = pending.pop(future)
                    result = future.result()
                    if cancelled():
                        return None
                    games[system.name] = result
                    progress(len(games), len(systems), system.fullname)
                fill()
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
    return None if cancelled() else (root, systems, games)
