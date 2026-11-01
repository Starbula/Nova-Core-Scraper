"""Bounded background scraping; workers never touch Tk or metadata."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError
from dataclasses import dataclass, replace
from pathlib import Path
import logging
import threading
import time
import psutil
from .models import Game, System
from .library import existing_artwork, artwork_destination
from .metadata import merge_artwork, MetadataError
from .providers import Cancelled, ProviderError, AuthenticationError, QuotaError
from .state import State, InstallationLock, safe_path

LOG = logging.getLogger(__name__)


def retrobat_running() -> bool:
    """Fail closed when the process inventory itself is unavailable."""
    names = {'retrobat.exe', 'emulationstation.exe', 'emulationstation.win32.exe',
             'emulationstation', 'emulationstation-de.exe', 'es-de.exe'}
    try:
        for process in psutil.process_iter(['name']):
            if (process.info.get('name') or '').casefold() in names:
                return True
    except (psutil.Error, OSError):
        return True
    return False


@dataclass(frozen=True)
class Progress:
    total: int = 0
    completed: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    pending: int = 0
    state: str = 'Idle'
    message: str = ''

    @property
    def processed(self):
        return self.completed + self.skipped + self.missing + self.failed


class ScrapeEngine:
    def __init__(self, root: Path, provider, process_running=retrobat_running):
        self.root = Path(root).absolute()
        self.provider = provider
        self.process_running = process_running
        self._condition = threading.Condition()
        self._stop = False
        self._paused = False
        self._thread = None
        self._progress = Progress()

    @property
    def active(self):
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> Progress:
        with self._condition:
            return self._progress

    def _update(self, **values):
        with self._condition:
            self._progress = replace(self._progress, **values)

    def _count(self, field):
        with self._condition:
            self._progress = replace(self._progress, **{field: getattr(self._progress, field)+1})

    def checkpoint(self):
        with self._condition:
            while self._paused and not self._stop:
                self._condition.wait(.2)
            if self._stop:
                raise Cancelled('Stopped')

    def start(self, games):
        if self.active:
            raise RuntimeError('Scraping is already active.')
        games = list(games)
        with self._condition:
            self._stop = self._paused = False
            self._progress = Progress(total=len(games), state='Scraping', message='Checking existing artwork…')
        self._thread = threading.Thread(target=self._run, args=(games,), name='Artwork coordinator', daemon=True)
        self._thread.start()

    def pause(self):
        with self._condition:
            if self.active and not self._stop:
                self._paused = True
                self._progress = replace(self._progress, state='Paused', message='Paused; active requests pause at their next checkpoint.')

    def resume(self):
        with self._condition:
            if self.active and not self._stop:
                self._paused = False
                self._progress = replace(self._progress, state='Scraping')
                self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._stop = True
            self._paused = False
            self._progress = replace(self._progress, state='Stopping', message='Stopping current requests…')
            self._condition.notify_all()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)
        return not self.active

    def _work(self, game):
        self.checkpoint()
        art = existing_artwork(game)
        if art:
            return 'skipped', art
        self._update(message=f'{game.system.fullname}: {game.title}')
        match = self.provider.find(game, self.checkpoint)
        self.checkpoint()
        if match is None:
            return 'missing', None
        destination = safe_path(game.system.path, artwork_destination(game))
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint()
        try:
            self.provider.download(match, destination, self.checkpoint)
        except FileExistsError:
            # A completed deterministic file can be recovered without requesting it again.
            if existing_artwork(game):
                return 'skipped', destination
            raise RuntimeError('An existing artwork destination is invalid; it was preserved. See log.')
        return 'completed', destination

    def _record(self, state, game, result):
        field, art = result
        if art:
            # Existing external references are already linked; do not copy or change them.
            if art.absolute().is_relative_to(game.system.path.absolute()):
                state.downloaded(game, art)
            else:
                state.record(game, field)
        else:
            state.record(game, field)
        self._count(field)
        if field == 'missing':
            LOG.info('No unambiguous box artwork: %s', game.key)

    def _flush(self, state):
        rows = state.pending()
        self._update(pending=len(rows))
        if not rows or self.process_running():
            return
        groups = {}
        for row in rows:
            groups.setdefault((row['system_name'], row['system_path']), []).append(row)
        for (name, folder), entries in groups.items():
            try:
                system_path = safe_path(self.root, self.root/folder)
                system = System(name, name, system_path, frozenset())
                updates = {r['relative_path']: safe_path(system_path, self.root/r['artwork']) for r in entries}
                # Missing image files remain pending rather than generating broken XML references.
                updates = {k: v for k, v in updates.items() if v.is_file() and v.stat().st_size > 0}
                accepted = merge_artwork(system, updates, self.process_running)
                state.applied(r['key'] for r in entries if r['relative_path'] in accepted)
            except (MetadataError, OSError, ValueError) as exc:
                LOG.warning('Metadata remains pending for %s: %s', name, exc)
                self._update(message=f'Metadata pending for {name}: {exc}')
        self._update(pending=len(state.pending()))

    def apply_pending(self):
        """Called off the UI thread while idle; obtains the same installation lock."""
        if self.active:
            return
        try:
            with InstallationLock(self.root), State(self.root) as state:
                self._flush(state)
        except (OSError, ValueError, RuntimeError) as exc:
            self._update(message=str(exc))

    def _run(self, games):
        fatal = None
        try:
            with InstallationLock(self.root), State(self.root) as state:
                needed, seen = [], set()
                for game in games:
                    self.checkpoint()
                    # Shared system directories / duplicate gamelist entries never cause duplicate downloads.
                    identity = str(game.path.absolute()).casefold()
                    if identity in seen:
                        self._count('skipped')
                        continue
                    seen.add(identity)
                    art = existing_artwork(game)
                    if art:
                        self._record(state, game, ('skipped', art))
                    else:
                        needed.append(game)
                if needed:
                    self.provider.prepare(self.checkpoint)
                    self.checkpoint()
                    workers = max(1, min(4, int(self.provider.workers)))
                    remaining = iter(needed)
                    last_flush = time.monotonic()
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='Artwork') as pool:
                        pending = {}
                        def fill():
                            while len(pending) < workers*2 and not self._stop:
                                game = next(remaining, None)
                                if game is None:
                                    break
                                pending[pool.submit(self._work, game)] = game
                        fill()
                        while pending:
                            finished, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                            for future in finished:
                                game = pending.pop(future)
                                try:
                                    self._record(state, game, future.result())
                                except (Cancelled, CancelledError):
                                    pass
                                except (AuthenticationError, QuotaError) as exc:
                                    fatal = str(exc)
                                    LOG.warning('Provider stopped: %s', exc)
                                    self.stop()
                                except Exception as exc:
                                    # Never log request objects/tracebacks: provider query strings can contain credentials.
                                    LOG.warning('Artwork failed for %s (%s): %s', game.key, type(exc).__name__,
                                                str(exc) if isinstance(exc, ProviderError) else 'See file permissions or connection status.')
                                    state.record(game, 'failed')
                                    self._count('failed')
                                    detail = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                                    self._update(message=f'Failed: {game.title}: {detail}')
                            if self._stop:
                                for future in pending:
                                    future.cancel()
                            elif not self._paused:
                                fill()
                                if time.monotonic()-last_flush >= 5:
                                    self._flush(state)
                                    last_flush = time.monotonic()
                if not self._stop:
                    self.checkpoint()
                    self._flush(state)
                self._update(pending=len(state.pending()))
        except Cancelled:
            pass
        except (AuthenticationError, QuotaError) as exc:
            fatal = str(exc)
        except Exception as exc:
            fatal = str(exc) if isinstance(exc, (OSError, RuntimeError, ValueError)) else type(exc).__name__
            LOG.warning('Scraper stopped (%s)', type(exc).__name__)
        finally:
            p = self.snapshot()
            if fatal:
                self._update(state='Error', message=fatal)
            elif self._stop:
                self._update(state='Stopped', message='Stopped. Start again to continue; completed artwork is retained.')
            else:
                message = 'Finished.'
                if p.pending:
                    message += f' {p.pending} metadata updates pending; '
                    message += 'close RetroBat to apply them.' if self.process_running() else 'see data/logs/scraper.log for details.'
                self._update(state='Finished', message=message)
