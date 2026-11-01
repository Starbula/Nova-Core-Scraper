"""Lazy, outage-aware artwork fallback without repeated ROM/library scans."""
from __future__ import annotations

import threading
from pathlib import Path

from .models import Artwork, Game
from .providers import AuthenticationError, Checkpoint, ProviderError, QuotaError


def _name(provider):
    name = getattr(provider, 'name', None)
    if name in {'Libretro', 'LaunchBox', 'ScreenScraper'}:
        return name
    names = {'LibretroProvider': 'Libretro', 'LaunchBoxProvider': 'LaunchBox', 'ScreenScraperProvider': 'ScreenScraper'}
    return names.get(type(provider).__name__, 'Artwork source')


class FallbackProvider:
    """Yield providers in order; only exhaustion turns stored lookup errors fatal.

    A successful engine download returns without resuming the iterator. If all
    sources are exhausted, any recorded outage remains a failure, not Missing.
    Authentication/quota failures disable only their source for this session.
    """
    def __init__(self, providers, workers: int = 8):
        self.providers = tuple(providers)
        self.workers = max(1, min(16, int(workers)))
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._prepared = False
        self._disabled = {}
        self._routes = {_name(provider): provider for provider in self.providers}
        self._status_callback = None

    def set_status_callback(self, callback):
        self._status_callback = callback
        for provider in self.providers:
            setter = getattr(provider, 'set_status_callback', None)
            if setter:
                setter(callback)

    def _status(self, message):
        if self._status_callback:
            self._status_callback(message)

    def _disable(self, provider):
        with self._lock:
            self._disabled[id(provider)] = _name(provider)

    def _unavailable(self, provider):
        with self._lock:
            return id(provider) in self._disabled

    def _failure(self, names):
        return ProviderError('Artwork source unavailable: ' + ', '.join(dict.fromkeys(names)) + '. Try again later.')

    def prepare(self, checkpoint: Checkpoint):
        checkpoint()
        while not self._prepare_lock.acquire(timeout=.1):
            checkpoint()
        try:
            checkpoint()
            if self._prepared:
                if not self.providers or all(self._unavailable(provider) for provider in self.providers):
                    raise self._failure([_name(provider) for provider in self.providers] or ['No configured sources'])
                return
            for provider in self.providers:
                checkpoint()
                try:
                    provider.prepare(checkpoint)
                except ProviderError:
                    self._disable(provider)
                    self._status(f'{_name(provider)} could not prepare; continuing with the other artwork sources.')
            self._prepared = True
            if not self.providers or all(self._unavailable(provider) for provider in self.providers):
                raise self._failure([_name(provider) for provider in self.providers] or ['No configured sources'])
        finally:
            self._prepare_lock.release()

    def candidates(self, game: Game, checkpoint: Checkpoint):
        self.prepare(checkpoint)
        failed = []
        for provider in self.providers:
            checkpoint()
            if self._unavailable(provider):
                failed.append(_name(provider))
                continue
            try:
                artwork = provider.find(game, checkpoint)
            except ProviderError as error:
                failed.append(_name(provider))
                if isinstance(error, (AuthenticationError, QuotaError)):
                    self._disable(provider)
                self._status(f'{_name(provider)} lookup failed; trying the next artwork source.')
                continue
            if artwork is not None:
                with self._lock:
                    previous = self._routes.get(artwork.source)
                    if previous is not None and previous is not provider:
                        raise ProviderError('The artwork sources returned conflicting source identities.')
                    self._routes[artwork.source] = provider
                yield artwork
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
        if self._unavailable(provider):
            raise self._failure([_name(provider)])
        try:
            provider.download(artwork, destination, checkpoint)
        except (AuthenticationError, QuotaError):
            self._disable(provider)
            self._status(f'{_name(provider)} is unavailable; continuing with the other artwork sources.')
            raise self._failure([_name(provider)]) from None

    def close(self):
        for provider in self.providers:
            provider.close()
