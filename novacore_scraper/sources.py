"""Lazy artwork fallback with isolated credentials, bounded outage cooldowns."""
from __future__ import annotations

import re
from dataclasses import dataclass
import threading
import time
from pathlib import Path

from .models import Artwork, Game
from .providers import AuthenticationError, Cancelled, Checkpoint, ProviderError, QuotaError

_NAMES = {'Libretro', 'LaunchBox', 'GameTDB', 'TheGamesDB', 'IGDB', 'MobyGames', 'ScreenScraper'}


def _name(provider):
    name = getattr(provider, 'name', None)
    if isinstance(name, str) and name in _NAMES:
        return name
    return {name+'Provider': name for name in _NAMES}.get(type(provider).__name__, 'Artwork source')


def safe_detail(value, fallback='The artwork source could not complete this request.'):
    """Only provider-authored text belongs in reports; never persist remote URLs."""
    value = str(value)
    if re.search(r'https?://|[?&](?:key|token|password|secret)|(?:password|authorization|api[_ -]?key)\s*[:=]', value, re.I):
        return fallback
    return ' '.join(value.split())[:600] or fallback


def error_diagnostic(error):
    """A narrow retry policy: malformed data and local I/O need review, not loops."""
    if isinstance(error, AuthenticationError):
        if getattr(error, 'reason', '') == 'access_refused':
            return 'access_refused', safe_detail(error), False
        return 'authentication', 'The source credentials are missing or were rejected.', False
    if isinstance(error, QuotaError):
        if getattr(error, 'reason', '') == 'allowance_unconfirmed':
            return 'allowance_unconfirmed', safe_detail(error), False
        return 'quota_exhausted', 'The source allowance is exhausted; resume after it resets.', False
    if type(error).__name__ == 'ArtworkUnavailable':
        return 'no_artwork', 'The matched artwork is no longer available (HTTP 404).', False
    if isinstance(error, ProviderError):
        reason = getattr(error, 'reason', '')
        retryable = getattr(error, 'retryable', None)
        detail = safe_detail(error)
        if reason and retryable is not None:
            return reason, detail, bool(retryable)
        lowered = detail.casefold()
        if any(word in lowered for word in ('network request failed', 'connection', 'timed out', 'timeout',
                                            'provider is busy', 'temporarily', 'temporary', 'interrupted')):
            return 'temporary_failure', detail, True
        if any(word in lowered for word in ('invalid', 'oversized', 'untrusted', 'size limit', 'pixel limit')):
            return 'invalid_response', detail, False
        return 'source_failure', detail, False
    return 'local_error', 'Could not save or process artwork. Check file permissions and the scraper log.', False


@dataclass(frozen=True)
class SourceSnapshot:
    name: str
    state: str = 'waiting'
    detail: str = 'Awaiting the first lookup.'
    lookups: int = 0
    matches: int = 0
    downloads: int = 0
    requests: int = 0
    api_requests: int = 0
    remaining: int | None = None


class _SourceFailure(ProviderError):
    def __init__(self, failures, trace=''):
        names = ', '.join(dict.fromkeys(name for name, _ in failures)) or 'No configured sources'
        self.retryable = any(info[2] for _, info in failures)
        reasons = {info[0] for _, info in failures}
        self.source = names
        self.reason = ('temporary_failure' if self.retryable else
                       next(iter(reasons)) if len(reasons) == 1 else 'source_failure')
        summary = '; '.join(name+': '+info[0].replace('_', ' ') for name, info in failures)
        super().__init__('Artwork source unavailable: '+(summary or names)+'. '+
                         ('Temporary failures will be retried.' if self.retryable else
                          'Review source settings or the unresolved results.')+
                         (' Sources tried: '+trace if trace else ''))


class FallbackProvider:
    """Try healthy sources in order and preserve every provider's candidate list.

    Offline-host circuits have bounded cooldowns; unsupported platforms and
    missing game images never open those circuits. Credential/quota failures
    disable only their source until a new run creates the next chain.
    """
    def __init__(self, providers, workers: int = 8, *, cooldown=30, clock=time.monotonic):
        self.providers = tuple(providers)
        self.workers = max(1, min(16, int(workers)))
        self.cooldown = max(1.0, min(300.0, float(cooldown)))
        self._clock = clock
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._prepared = False
        self._ready = set()
        self._disabled = {}
        self._errors = {}
        self._cooldowns = {}
        self._routes = {_name(provider): provider for provider in self.providers}
        self._status_callback = None
        self._local = threading.local()
        self._stats = {id(provider): dict(state='waiting', detail='Awaiting the first lookup.',
                       lookups=0, matches=0, downloads=0) for provider in self.providers}

    def _activity(self, provider, state, detail='', **counts):
        with self._lock:
            stats = self._stats[id(provider)]
            # A paid request can finish after another worker exhausts quota.
            # Retain the source suspension while still counting its result.
            if id(provider) not in self._disabled:
                stats.update(state=state, detail=safe_detail(detail) if detail else state.replace('_', ' '))
            for key, value in counts.items():
                stats[key] += value

    def _trace(self, provider, code):
        outcomes = getattr(self._local, 'outcomes', None)
        if outcomes is None:
            outcomes = self._local.outcomes = {}
        # Known service names and normalized result codes only, never remote text.
        code = re.sub(r'[^a-z0-9_ ]', '', str(code).casefold())[:50] or 'source_failure'
        outcomes[_name(provider)] = code.replace('_', ' ')

    def attempt_summary(self) -> str:
        return '; '.join(name+': '+result for name, result in getattr(self._local, 'outcomes', {}).items())[:1000]

    def source_statuses(self) -> tuple[SourceSnapshot, ...]:
        with self._lock:
            copied = [(provider, dict(self._stats[id(provider)])) for provider in self.providers]
        rows = []
        for provider, stats in copied:
            reader = getattr(provider, 'request_stats', None)
            network = reader() if callable(reader) else {}
            remaining = network.get('remaining')
            rows.append(SourceSnapshot(_name(provider), **stats,
                requests=network.get('requests', 0), api_requests=network.get('api_requests', 0),
                remaining=remaining if isinstance(remaining, int) and not isinstance(remaining, bool) else None))
        return tuple(rows)

    def check_sources(self, checkpoint: Checkpoint):
        """Explicit idle diagnostics: API adapters bypass cached authentication."""
        for provider in self.providers:
            checkpoint()
            self._activity(provider, 'checking', 'Checking source access…')
            try:
                check = getattr(provider, 'check_connection', None)
                if callable(check):
                    detail = check(checkpoint)
                    self._activity(provider, 'checked', detail)
                else:
                    provider.prepare(checkpoint)
                    self._activity(provider, 'prepared', 'Catalogue preparation succeeded. This may use cached data; no account authentication is implied.')
                self._ready.add(id(provider))
            except Cancelled:
                raise
            except Exception as error:
                self._failed(provider, error, preparing=True)

    def set_status_callback(self, callback):
        self._status_callback = callback
        for provider in self.providers:
            setter = getattr(provider, 'set_status_callback', None)
            if callable(setter):
                setter(callback)

    def _status(self, message):
        if self._status_callback:
            self._status_callback(message)

    def _unavailable(self, provider):
        with self._lock:
            identifier = id(provider)
            return identifier in self._disabled or self._cooldowns.get(identifier, 0) > self._clock()

    def _failed(self, provider, error, *, preparing=False):
        info = error_diagnostic(error)
        with self._lock:
            identifier = id(provider)
            previous = self._errors.get(identifier)
            severity = {'authentication': 3, 'access_refused': 3, 'quota_exhausted': 2, 'allowance_unconfirmed': 1}
            preserve = (identifier in self._disabled and previous is not None
                        and severity.get(previous[0], 0) > severity.get(info[0], 0))
            if not preserve:
                self._errors[identifier] = info
                self._stats[identifier].update(state=info[0], detail=info[1])
            if isinstance(error, (AuthenticationError, QuotaError)):
                self._disabled[identifier] = _name(provider)
            elif info[2] or preparing:
                self._cooldowns[identifier] = self._clock()+self.cooldown
        return info

    def _failure(self, providers):
        with self._lock:
            failures = [(_name(provider), self._errors.get(id(provider),
                         ('source_failure', 'The source is unavailable.', False))) for provider in providers]
        return _SourceFailure(failures, self.attempt_summary())

    def prepare(self, checkpoint: Checkpoint):
        checkpoint()
        while not self._prepare_lock.acquire(timeout=.1):
            checkpoint()
        try:
            checkpoint()
            for provider in self.providers:
                if id(provider) in self._ready or self._unavailable(provider):
                    continue
                checkpoint()
                self._activity(provider, 'preparing', 'Preparing the source catalogue…')
                try:
                    provider.prepare(checkpoint)
                except Cancelled:
                    raise
                except Exception as error:
                    self._failed(provider, error, preparing=True)
                    self._status(f'{_name(provider)} could not prepare; continuing with the other artwork sources.')
                else:
                    self._ready.add(id(provider))
                    self._activity(provider, 'prepared', 'Catalogue ready. Cached preparation does not verify account credentials.')
                    with self._lock:
                        self._cooldowns.pop(id(provider), None)
            self._prepared = True
            if not self.providers or all(self._unavailable(provider) for provider in self.providers):
                raise self._failure(self.providers)
        finally:
            self._prepare_lock.release()

    def missing_reason(self, game=None):
        return getattr(self._local, 'missing', ('no_match', 'No unambiguous front cover was found in the available sources.'))

    def candidates(self, game: Game, checkpoint: Checkpoint):
        self._local.missing = ('no_match', 'No unambiguous front cover was found in the available sources.')
        self._local.outcomes = {}
        try:
            self.prepare(checkpoint)
        except ProviderError:
            for provider in self.providers:
                if self._unavailable(provider):
                    self._trace(provider, self._errors.get(id(provider), ('source_failure',))[0])
            raise self._failure(self.providers) from None
        failed, unmatched = [], []
        for provider in self.providers:
            checkpoint()
            if self._unavailable(provider):
                failed.append(provider)
                self._trace(provider, self._errors.get(id(provider), ('source_failure',))[0])
                continue
            matches = None
            try:
                self._activity(provider, 'searching', 'Checking the game in this source.', lookups=1)
                self._trace(provider, 'searching')
                nested = getattr(provider, 'candidates', None)
                if callable(nested):
                    matches = iter(nested(game, checkpoint))
                else:
                    found = provider.find(game, checkpoint)
                    matches = iter(()) if found is None else iter((found,))
                found_any = False
                for artwork in matches:
                    checkpoint()
                    if artwork is None:
                        continue
                    found_any = True
                    self._activity(provider, 'matched', 'A matching cover candidate was found.', matches=1)
                    self._trace(provider, 'matched')
                    with self._lock:
                        previous = self._routes.get(artwork.source)
                        if previous is not None and previous is not provider:
                            raise ProviderError('The artwork sources returned conflicting source identities.')
                        self._routes[artwork.source] = provider
                    yield artwork
                if not found_any:
                    reporter = getattr(provider, 'missing_reason', None)
                    code, detail = reporter(game) if callable(reporter) else ('no_match', 'No unambiguous front cover was found.')
                    unmatched.append((str(code), f'{_name(provider)}: {safe_detail(detail)}'))
                    self._activity(provider, str(code), detail)
                    self._trace(provider, code)
            except Cancelled:
                raise
            except Exception as error:
                info = self._failed(provider, error)
                self._trace(provider, info[0])
                failed.append(provider)
                self._status(f'{_name(provider)} lookup failed; trying the next artwork source.')
            finally:
                close = getattr(matches, 'close', None)
                if callable(close):
                    close()
        if unmatched:
            def importance(item):
                code = item[0]
                return (4 if 'ambiguous' in code else 3 if any(word in code for word in ('region', 'revision', 'edition', 'policy'))
                        else 2 if 'cover' in code or 'artwork' in code else 0 if 'unsupported' in code else 1)
            self._local.missing = max(unmatched, key=importance)
            code, detail = self._local.missing
            self._local.missing = (code, safe_detail(detail+' Sources tried: '+self.attempt_summary()))
        if failed:
            raise self._failure(failed)

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        candidates = self.candidates(game, checkpoint)
        try:
            return next(candidates, None)
        finally:
            candidates.close()

    def download(self, artwork: Artwork, destination: Path, checkpoint: Checkpoint):
        checkpoint()
        with self._lock:
            provider = self._routes.get(artwork.source)
        if provider is None:
            raise ProviderError('The artwork source could not be identified.')
        with self._lock:
            quota_only = self._errors.get(id(provider), ('',))[0] in {'quota_exhausted', 'allowance_unconfirmed'}
        if self._unavailable(provider) and not quota_only:
            self._trace(provider, self._errors.get(id(provider), ('source_failure',))[0])
            raise self._failure([provider])
        try:
            provider.download(artwork, destination, checkpoint)
            self._activity(provider, 'downloaded', 'Artwork saved successfully.', downloads=1)
            self._trace(provider, 'downloaded')
        except (Cancelled, FileExistsError):
            # Publication races are local recovery, not a remote source outage.
            raise
        except Exception as error:
            info = self._failed(provider, error)
            self._trace(provider, info[0])
            if isinstance(error, (AuthenticationError, QuotaError)):
                self._status(f'{_name(provider)} is unavailable; continuing with the other artwork sources.')
                raise self._failure([provider]) from None
            if not isinstance(error, ProviderError):
                raise ProviderError('The artwork source returned an unexpected response.') from None
            raise

    def close(self):
        for provider in self.providers:
            close = getattr(provider, 'close', None)
            if callable(close):
                close()
