"""Bounded background scraping; workers never touch Tk or metadata."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError
from dataclasses import dataclass, replace
from pathlib import Path
import logging
import math
import re
import threading
import time
import psutil
from .models import Game, System
from .library import existing_artwork, artwork_destination
from .metadata import merge_artwork, MetadataError
from .providers import Cancelled, ProviderError
from .state import State, InstallationLock, safe_path
from .power import keep_system_awake
from .sources import error_diagnostic, safe_detail

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
    checked: int = 0
    needed: int = 0
    resolved: int = 0
    cancelled: int = 0
    stage: str = ''
    retry_pass: int = 0
    retry_wait: int = 0

    @property
    def processed(self):
        return self.completed + self.skipped + self.missing + self.failed + self.cancelled


@dataclass(frozen=True)
class JobSnapshot:
    key: str
    title: str
    console: str
    status: str
    artwork: str = ''
    detail: str = ''
    reason: str = ''
    source: str = ''
    region: str = ''
    match_note: str = ''
    attempts: int = 0
    retryable: bool = False


_TERMINAL = frozenset({'completed', 'skipped', 'missing', 'failed', 'cancelled'})


def _search_text(value: str) -> str:
    return ''.join(character for character in value.casefold() if character.isalnum())


class ScrapeEngine:
    def __init__(self, root: Path, provider, process_running=retrobat_running, *,
                 retry_passes=2, retry_delay=30, keep_awake=True):
        self.root = Path(root).absolute()
        self.provider = provider
        self.process_running = process_running
        self.retry_passes = max(0, min(2, int(retry_passes)))
        self.retry_delay = max(0.0, float(retry_delay))
        self.keep_awake = bool(keep_awake)
        self._condition = threading.Condition()
        self._stop = False
        self._paused = False
        self._thread = None
        self._progress = Progress()
        self._jobs: dict[str, JobSnapshot] = {}
        self._search: dict[str, str] = {}
        self._job_systems: dict[str, str] = {}
        self._covered_jobs: set[str] = set()
        self._artwork_counts: dict[str, int] = {}
        self._needed: set[str] = set()
        self._cancelled: set[str] = set()
        self._jobs_revision = 0
        self._dirty_jobs = set()
        self._games = {}

    @property
    def active(self):
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> Progress:
        with self._condition:
            return self._progress

    @property
    def jobs_revision(self) -> int:
        with self._condition:
            return self._jobs_revision

    def artwork_counts(self) -> dict[str, int]:
        """Covered games in this run, by console; no filesystem work on Tk's thread."""
        with self._condition:
            return dict(self._artwork_counts)

    def _cover_game(self, key: str) -> None:
        with self._condition:
            if key not in self._covered_jobs:
                self._covered_jobs.add(key)
                system = self._job_systems[key]
                self._artwork_counts[system] = self._artwork_counts.get(system, 0)+1

    def jobs_page(self, query='', statuses=None, offset=0, limit=200) -> tuple[list[JobSnapshot], int]:
        """Read the submitted queue without filesystem I/O or copying the full queue."""
        offset, limit = max(0, int(offset)), min(200, max(0, int(limit)))
        terms = [_search_text(word) for word in re.findall(r'\w+', query) if _search_text(word)]
        if isinstance(statuses, str):
            statuses = {statuses}
        elif statuses is not None:
            statuses = frozenset(statuses)
        rows, total = [], 0
        with self._condition:
            for key, job in self._jobs.items():
                if statuses is not None and job.status not in statuses:
                    continue
                if terms and not all(term in self._search[key] for term in terms):
                    continue
                if offset <= total < offset + limit:
                    rows.append(job)
                total += 1
        return rows, total

    def downloaded_page(self, query='', offset=0, limit=200, *, statuses=None) -> tuple[list[JobSnapshot], int]:
        """Read persistent history/results off the UI thread with bounded pages."""
        if statuses is None:
            rows, total = State.downloaded_page(self.root, query, offset, limit)
        else:
            rows, total = State.jobs_page(self.root, query, offset, limit, statuses=statuses)
        return [JobSnapshot(
            row['key'], row['title'], row['system_fullname'] or row['system_name'],
            row['status'], str(self.root/row['artwork']) if row['artwork'] else '',
            row['detail'], row['reason'], row['source'], row['region'], row['match_note'],
            row['attempts'], bool(row['retryable'])) for row in rows], total

    def _update(self, **values):
        with self._condition:
            self._progress = replace(self._progress, **values)

    def _count(self, field):
        with self._condition:
            self._progress = replace(self._progress, **{field: getattr(self._progress, field)+1})

    def checkpoint(self, key=None):
        with self._condition:
            while self._paused and not self._stop and key not in self._cancelled:
                self._condition.wait(.2)
            if self._stop or key in self._cancelled:
                raise Cancelled('Cancelled')

    def _transition(self, key, status, **values):
        with self._condition:
            job = self._jobs[key]
            if job.status in _TERMINAL:
                return
            updated = replace(job, status=status, **values)
            if updated != job:
                self._jobs[key] = updated
                self._jobs_revision += 1
                self._dirty_jobs.add(key)

    def _finish(self, key, status, artwork='', detail='', **values):
        with self._condition:
            job = self._jobs[key]
            counts = {}
            if job.status in _TERMINAL:
                # Publication can win a cancellation race. Keep the finished
                # file and record its actual result rather than losing history.
                if job.status != 'cancelled' or status not in {'completed', 'skipped'} or not artwork:
                    return
                counts['cancelled'] = self._progress.cancelled - 1
            elif key in self._needed:
                counts['resolved'] = self._progress.resolved + 1
            counts[status] = getattr(self._progress, status) + 1
            self._progress = replace(self._progress, **counts)
            self._jobs[key] = replace(job, status=status, artwork=str(artwork or ''),
                                      detail=detail or (job.detail if status == 'completed' else ''), **values)
            if artwork and status in {'completed', 'skipped'}:
                self._cover_game(key)
            self._jobs_revision += 1
            self._dirty_jobs.add(key)

    def start(self, games):
        # Game/System are frozen dataclasses. Copy the caller's selection once;
        # later changes in the Library tab cannot expand an active run.
        games = tuple({game.key: game for game in games}.values())
        with self._condition:
            if self.active:
                raise RuntimeError('Scraping is already active.')
            self._stop = self._paused = False
            self._needed.clear()
            self._cancelled.clear()
            self._covered_jobs.clear()
            self._dirty_jobs.clear()
            self._games = {game.key: game for game in games}
            self._artwork_counts.clear()
            self._jobs = {game.key: JobSnapshot(game.key, game.title, game.system.fullname, 'checking') for game in games}
            self._job_systems = {game.key: game.system.name for game in games}
            self._search = {game.key: _search_text(' '.join((game.title, game.system.fullname, game.key))) for game in games}
            self._jobs_revision += 1
            self._progress = Progress(total=len(games), state='Scraping', stage='checking', message='Checking existing artwork…')
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
            if not self.active:
                return
            self._stop = True
            self._paused = False
            self.cancel_jobs(tuple(self._jobs))
            self._progress = replace(self._progress, state='Stopping', message='Stopping current requests…')
            self._condition.notify_all()

    def cancel_jobs(self, keys):
        with self._condition:
            for key in keys:
                job = self._jobs.get(key)
                if job is not None and job.status not in _TERMINAL:
                    self._cancelled.add(key)
                    self._finish(key, 'cancelled', detail='Cancelled by user.', reason='cancelled', retryable=False)
            self._condition.notify_all()

    def cancel_all(self):
        self.stop()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)
        return not self.active

    def _work(self, game):
        checkpoint = lambda: self.checkpoint(game.key)
        checkpoint()
        art = existing_artwork(game)
        if art:
            self._transition(game.key, 'checking', reason='existing_artwork')
            return 'skipped', art
        checkpoint()
        self._transition(game.key, 'finding')
        self._update(message=f'{game.system.fullname}: {game.title}', stage='scraping')
        candidate_source = getattr(self.provider, 'candidates', None)
        chained = callable(candidate_source)
        if chained:
            matches = iter(candidate_source(game, checkpoint))
        else:
            match = self.provider.find(game, checkpoint)
            matches = iter(()) if match is None else iter((match,))
        failures, failed_sources = [], []
        try:
            try:
                for match in matches:
                    checkpoint()
                    destination = safe_path(game.system.path, artwork_destination(game))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._transition(game.key, 'downloading', detail=f'Source: {safe_detail(match.source)}',
                                     source=safe_detail(match.source, 'Artwork source'),
                                     region=safe_detail(match.region, '') if match.region else '',
                                     match_note=safe_detail(getattr(match, 'match_note', ''), ''))
                    try:
                        self.provider.download(match, destination, checkpoint)
                        self._transition(game.key, 'downloading', reason='downloaded', retryable=False)
                        return 'completed', destination
                    except FileExistsError:
                        if existing_artwork(game):
                            self._transition(game.key, 'checking', reason='existing_artwork', retryable=False)
                            return 'skipped', destination
                        raise RuntimeError('An existing artwork destination is invalid; it was preserved. See log.')
                    except ProviderError as error:
                        if not chained:
                            if error_diagnostic(error)[0] == 'no_artwork':
                                self._transition(game.key, 'finding', reason='no_artwork', detail=error_diagnostic(error)[1])
                                return 'missing', None
                            raise
                        failures.append(error_diagnostic(error))
                        failed_sources.append(safe_detail(match.source, 'Artwork source'))
                        self._transition(game.key, 'finding', detail=f'{safe_detail(match.source)} download failed; trying the next source.')
                        LOG.warning('Artwork source download failed for %s (%s); trying fallback.', game.key, safe_detail(match.source))
            except ProviderError as error:
                if not failures:
                    raise
                failures.append(error_diagnostic(error))
                source = getattr(error, 'source', '')
                if source:
                    failed_sources.append(safe_detail(source, 'Artwork source'))
            if failures:
                info = next((info for info in failures if info[2]), failures[-1])
                if all(info[0] == 'no_artwork' for info in failures):
                    self._transition(game.key, 'finding', reason='no_artwork', detail=info[1], retryable=False)
                    return 'missing', None
                detail = '; '.join(dict.fromkeys(item[1] for item in failures))
                error = ProviderError(safe_detail(detail))
                error.reason, error.retryable = info[0], info[2]
                error.source = ', '.join(dict.fromkeys(failed_sources))
                raise error
            reporter = getattr(self.provider, 'missing_reason', None)
            code, detail = reporter(game) if callable(reporter) else ('no_match', 'No unambiguous front cover was found.')
            self._transition(game.key, 'finding', reason=str(code), detail=safe_detail(detail), retryable=False)
            return 'missing', None
        finally:
            close = getattr(matches, 'close', None)
            if callable(close):
                close()

    def _diagnostics(self, job):
        return dict(reason=job.reason, detail=job.detail, source=job.source or None,
                    region=job.region or None, match_note=job.match_note or None,
                    attempts=job.attempts, retryable=job.retryable)

    def _persist_dirty(self, state):
        with self._condition:
            jobs = [(self._games[key], self._jobs[key]) for key in self._dirty_jobs]
            self._dirty_jobs.clear()
        with state.batch():
            for game, job in jobs:
                state.record(game, job.status, **self._diagnostics(job))

    def _record(self, state, game, result):
        field, art = result
        with self._condition:
            job = self._jobs[game.key]
        info = self._diagnostics(job)
        if field == 'skipped':
            info.update(reason='existing_artwork', detail='Existing artwork retained.', retryable=False)
        elif field == 'completed':
            info.update(reason='downloaded', retryable=False)
        if art and art.absolute().is_relative_to(game.system.path.absolute()):
            state.downloaded(game, art, history=field == 'completed', **info)
        else:
            state.record(game, field, **info)
        self._finish(game.key, field, art or '', **{key: value for key, value in info.items()
                     if key not in {'source','region','match_note','attempts'}})
        if field == 'missing':
            LOG.info('No unambiguous box artwork: %s', game.key)

    def _failed_job(self, state, game, error):
        reason, detail, retryable = error_diagnostic(error)
        source = getattr(error, 'source', '')
        self._finish(game.key, 'failed', detail=detail, reason=reason, retryable=retryable,
                     **({'source': safe_detail(source)} if source else {}))
        self._persist_dirty(state)
        LOG.warning('Artwork failed for %s (%s): %s', game.key, type(error).__name__, detail)
        self._update(message=f'Failed: {game.title}: {detail}')

    def _requeue(self, games):
        with self._condition:
            if self._stop:
                return
            for game in games:
                job = self._jobs[game.key]
                if job.status != 'failed' or not job.retryable or game.key in self._cancelled:
                    continue
                self._progress = replace(self._progress, failed=self._progress.failed-1,
                                         resolved=self._progress.resolved-1)
                self._jobs[game.key] = replace(job, status='retry_wait')
                self._dirty_jobs.add(game.key)
                self._jobs_revision += 1

    def _retry_wait(self, games, number, state=None):
        remaining = self.retry_delay
        last = time.monotonic()
        while True:
            if state is not None:
                self._persist_dirty(state)
            with self._condition:
                if self._stop:
                    raise Cancelled('Cancelled')
                if not any(self._jobs[game.key].status == 'retry_wait' for game in games):
                    break
                if self._paused:
                    self._condition.wait(.2)
                    last = time.monotonic()
                    continue
                now = time.monotonic()
                remaining = max(0, remaining-(now-last))
                last = now
                seconds = math.ceil(remaining)
                self._progress = replace(self._progress, stage='retry_wait', retry_pass=number,
                    retry_wait=seconds, message=f'Retry pass {number}/{self.retry_passes} in {seconds}s; temporary source failures only.')
                if remaining <= 0:
                    break
                self._condition.wait(min(.2, remaining))
        self._update(retry_wait=0)

    def _run_pass(self, state, games):
        self.checkpoint()
        self._update(stage='preparing', message='Preparing artwork sources; the first catalogue download may take a moment…')
        try:
            self.provider.prepare(self.checkpoint)
        except Cancelled:
            raise
        except ProviderError as error:
            for game in games:
                with self._condition:
                    job = self._jobs[game.key]
                    if job.status in _TERMINAL:
                        continue
                    self._transition(game.key, 'queued', attempts=job.attempts+1)
                self._failed_job(state, game, error)
            return
        except Exception:
            raise RuntimeError('The artwork provider could not be prepared. Check connection and settings.') from None
        self.checkpoint()
        workers = max(1, min(16, int(self.provider.workers)))
        remaining, exhausted = iter(games), False
        last_flush = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='Artwork') as pool:
            pending = {}
            def fill():
                nonlocal exhausted
                while len(pending) < workers*2 and not self._stop:
                    game = next(remaining, None)
                    if game is None:
                        exhausted = True
                        break
                    with self._condition:
                        if game.key in self._cancelled:
                            continue
                        job = self._jobs[game.key]
                        self._transition(game.key, 'queued', attempts=job.attempts+1)
                    pending[pool.submit(self._work, game)] = game
                self._persist_dirty(state)
            fill()
            while pending or not exhausted:
                if not pending:
                    self.checkpoint()
                    fill()
                    if not pending:
                        break
                finished, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                for future in finished:
                    game = pending.pop(future)
                    try:
                        self._record(state, game, future.result())
                    except (Cancelled, CancelledError):
                        self._finish(game.key, 'cancelled', detail='Cancelled by user.', reason='cancelled', retryable=False)
                    except Exception as error:
                        self._failed_job(state, game, error)
                self._persist_dirty(state)
                if self._stop:
                    for future in pending:
                        future.cancel()
                    exhausted = True
                elif not self._paused:
                    fill()
                    if time.monotonic()-last_flush >= 5:
                        self._flush(state)
                        last_flush = time.monotonic()

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

    def _coordinate(self, state, games):
        needed, seen = [], set()
        for game in games:
            self.checkpoint()
            previous = state.previous(game)
            with self._condition:
                if previous is not None:
                    job = self._jobs[game.key]
                    self._jobs[game.key] = replace(job, attempts=previous['attempts'],
                        source=previous['source'], region=previous['region'], match_note=previous['match_note'])
                cancelled = game.key in self._cancelled
            if cancelled:
                if existing_artwork(game):
                    self._cover_game(game.key)
                self._count('checked')
                continue
            identity = str(game.path.absolute()).casefold()
            if identity in seen:
                self._count('checked')
                self._finish(game.key, 'skipped', detail='Duplicate library entry.', reason='duplicate_entry')
                continue
            seen.add(identity)
            art = existing_artwork(game)
            self._count('checked')
            if art:
                self._record(state, game, ('skipped', art))
            else:
                with self._condition:
                    if game.key in self._cancelled:
                        continue
                    self._needed.add(game.key)
                    self._progress = replace(self._progress, needed=self._progress.needed+1)
                    self._transition(game.key, 'queued')
                    needed.append(game)
        self._persist_dirty(state)
        report = getattr(self.provider, 'set_status_callback', None)
        if callable(report):
            report(lambda message: self._update(message=safe_detail(message)))
        batch = needed
        for number in range(self.retry_passes+1):
            if not batch:
                break
            if number:
                self._requeue(batch)
                self._persist_dirty(state)
                self._retry_wait(batch, number, state)
                self._persist_dirty(state)
                batch = [game for game in batch if self._jobs[game.key].status == 'retry_wait']
                if not batch:
                    break
            self._run_pass(state, batch)
            batch = [game for game in batch if self._jobs[game.key].status == 'failed'
                     and self._jobs[game.key].retryable]
        if not self._stop:
            self.checkpoint()
            self._flush(state)

    def _run(self, games):
        fatal = None
        try:
            with keep_system_awake(self.keep_awake), InstallationLock(self.root), State(self.root) as state:
                try:
                    self._coordinate(state, games)
                except Cancelled:
                    pass
                except Exception as error:
                    fatal = safe_detail(error) if isinstance(error, (ProviderError, RuntimeError)) else 'The scraper could not complete this run. Check file permissions and source settings.'
                    LOG.warning('Scraper stopped (%s)', type(error).__name__)
                finally:
                    if fatal or self._stop:
                        self.cancel_jobs(tuple(self._jobs))
                    self._persist_dirty(state)
                    self._update(pending=len(state.pending()))
        except Exception as error:
            fatal = safe_detail(error) if isinstance(error, (RuntimeError, ValueError)) else 'The scraper could not open its recovery state. Check library permissions.'
            self.cancel_jobs(tuple(self._jobs))
            LOG.warning('Scraper stopped (%s)', type(error).__name__)
        finally:
            close = getattr(self.provider, 'close', None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    LOG.warning('Could not close artwork source (%s).', type(error).__name__)
            p = self.snapshot()
            if fatal:
                self._update(state='Error', stage='finished', retry_wait=0, message=fatal)
            elif self._stop:
                self._update(state='Stopped', stage='finished', retry_wait=0, message='Stopped. Start again to continue; completed artwork is retained.')
            else:
                message = 'Finished.'
                if p.missing or p.failed:
                    message += f' Review {p.missing} missing and {p.failed} failed results in Downloads or export the report.'
                if p.pending:
                    message += f' {p.pending} metadata updates pending; '
                    message += 'close RetroBat to apply them.' if self.process_running() else 'see data/logs/scraper.log for details.'
                self._update(state='Finished', stage='finished', retry_wait=0, message=message)
