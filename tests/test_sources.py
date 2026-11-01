"""A source outage must not prevent a healthy fallback or become false Missing."""
from pathlib import Path

import pytest

from novacore_scraper.models import Artwork
from novacore_scraper.providers import AuthenticationError, Cancelled, ProviderError, QuotaError
from novacore_scraper.sources import FallbackProvider


class Stub:
    def __init__(self, name, result=None, prepare_error=None, find_error=None):
        self.name = name
        self.result = result
        self.prepare_error = prepare_error
        self.find_error = find_error
        self.lookups = 0
        self.downloads = []
        self.closed = False
        self.callback = None

    def prepare(self, checkpoint):
        checkpoint()
        if self.prepare_error:
            raise self.prepare_error

    def find(self, game, checkpoint):
        checkpoint()
        self.lookups += 1
        if self.find_error:
            raise self.find_error
        return self.result

    def download(self, artwork, destination, checkpoint):
        checkpoint()
        self.downloads.append((artwork, destination))

    def set_status_callback(self, callback):
        self.callback = callback

    def close(self):
        self.closed = True


def test_candidates_are_lazy_and_route_each_download_to_its_source(tmp_path):
    first = Stub('Libretro', Artwork('https://example/1.png', 'Libretro'))
    second = Stub('LaunchBox', Artwork('https://example/2.png', 'LaunchBox'))
    chain = FallbackProvider([first, second], workers=8)
    matches = chain.candidates(object(), lambda: None)
    one = next(matches)
    assert first.lookups == 1 and second.lookups == 0
    chain.download(one, tmp_path / 'one.png', lambda: None)
    two = next(matches)
    chain.download(two, tmp_path / 'two.png', lambda: None)
    assert list(matches) == []
    assert first.downloads == [(one, tmp_path / 'one.png')]
    assert second.downloads == [(two, tmp_path / 'two.png')]
    assert chain.workers == 8
    chain.close()
    assert first.closed and second.closed


def test_find_stops_at_first_match():
    first = Stub('Libretro', Artwork('https://example/1.png', 'Libretro'))
    second = Stub('LaunchBox', find_error=ProviderError('unavailable'))
    chain = FallbackProvider([first, second])
    assert chain.find(object(), lambda: None) == first.result
    assert second.lookups == 0


@pytest.mark.parametrize('failure', [ProviderError('https://secret/'), AuthenticationError('private credential'), QuotaError('private account')])
def test_lookup_failure_is_deferred_until_healthy_candidate_is_consumed(failure):
    first = Stub('Libretro', find_error=failure)
    second = Stub('LaunchBox', Artwork('https://example/2.png', 'LaunchBox'))
    chain = FallbackProvider([first, second])
    matches = chain.candidates(object(), lambda: None)
    assert next(matches) == second.result
    with pytest.raises(ProviderError) as error:
        next(matches)
    assert 'https://' not in str(error.value) and 'private' not in str(error.value)
    assert chain.find(object(), lambda: None) == second.result
    assert first.lookups == (2 if type(failure) is ProviderError else 1)


def test_all_missing_is_distinct_from_partial_outage():
    chain = FallbackProvider([Stub('Libretro'), Stub('LaunchBox')])
    assert list(chain.candidates(object(), lambda: None)) == []
    chain = FallbackProvider([Stub('Libretro', find_error=ProviderError('bad')), Stub('LaunchBox')])
    with pytest.raises(ProviderError):
        list(chain.candidates(object(), lambda: None))


def test_prepare_continues_after_one_source_fails_and_announces_status():
    first = Stub('Libretro', prepare_error=ProviderError('secret'))
    second = Stub('LaunchBox', Artwork('https://example/2.png', 'LaunchBox'))
    chain = FallbackProvider([first, second])
    messages = []
    chain.set_status_callback(messages.append)
    chain.prepare(lambda: None)
    assert chain.find(object(), lambda: None) == second.result
    assert first.lookups == 0
    assert messages and all('secret' not in message for message in messages)
    assert second.callback is not None


def test_all_prepare_failures_remain_visible():
    chain = FallbackProvider([Stub('Libretro', prepare_error=ProviderError('bad')), Stub('LaunchBox', prepare_error=ProviderError('bad'))])
    with pytest.raises(ProviderError):
        chain.prepare(lambda: None)


def test_cancellation_does_not_become_source_failure():
    chain = FallbackProvider([Stub('Libretro', find_error=Cancelled()), Stub('LaunchBox')])
    with pytest.raises(Cancelled):
        list(chain.candidates(object(), lambda: None))


def test_download_fatal_disables_only_that_source(tmp_path):
    first = Stub('Libretro', Artwork('https://example/1.png', 'Libretro'))
    second = Stub('LaunchBox', Artwork('https://example/2.png', 'LaunchBox'))
    def denied(*args):
        raise AuthenticationError('private')
    first.download = denied
    chain = FallbackProvider([first, second])
    candidates = chain.candidates(object(), lambda: None)
    with pytest.raises(ProviderError):
        chain.download(next(candidates), tmp_path / 'out.png', lambda: None)
    assert next(candidates) == second.result
    assert chain.find(object(), lambda: None) == second.result
    assert first.lookups == 1


def test_unrecognized_source_cannot_route_to_an_arbitrary_provider(tmp_path):
    chain = FallbackProvider([Stub('Libretro')])
    with pytest.raises(ProviderError):
        chain.download(Artwork('https://evil/x.png', 'unknown'), tmp_path / 'out.png', lambda: None)


def test_repeated_failed_prepare_and_empty_chain_still_raise():
    for providers in ([], [Stub('Libretro', prepare_error=ProviderError('bad'))]):
        chain = FallbackProvider(providers)
        for _ in range(2):
            with pytest.raises(ProviderError):
                chain.prepare(lambda: None)


def test_prepare_cancellation_keeps_other_source_untouched():
    first = Stub('Libretro', prepare_error=Cancelled())
    second = Stub('LaunchBox')
    chain = FallbackProvider([first, second])
    with pytest.raises(Cancelled):
        chain.prepare(lambda: None)
    assert not chain._prepared
    assert not chain._disabled
