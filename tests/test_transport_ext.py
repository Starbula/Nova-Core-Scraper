import json
import threading

import pytest
import requests

from novacore_scraper import providers
from novacore_scraper.models import Artwork


class PublicProvider(providers._Provider):
    def _trusted_host(self, host):
        return host in {'source.invalid', 'cdn.source.invalid'}


@pytest.fixture
def transport(monkeypatch):
    calls, responses = [], []
    def send(session, request, **kwargs):
        calls.append(request)
        status, body, headers = responses.pop(0)
        response = requests.Response()
        response.status_code = status
        response._content = body
        response._content_consumed = True
        response.headers.update(headers)
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    return calls, responses


def test_oauth_post_and_accepted_auth_status_are_readable_and_release_slot(tmp_path, transport):
    calls, responses = transport
    responses.extend([(401, b'{"error":"invalid credentials"}', {}), (200, b'ok', {})])
    provider = PublicProvider(tmp_path, 'eu', 0, 1)
    response = provider._request('https://source.invalid/token', lambda: None,
        method='POST', data={'client_secret': 'fixture secret'}, headers={'X-Fixture': 'present'}, accepted_status=(401,))
    assert json.loads(provider._read(response, lambda: None, 1000))['error'] == 'invalid credentials'
    assert calls[0].method == 'POST'
    assert calls[0].headers['X-Fixture'] == 'present'
    assert b'client_secret=fixture+secret' in (calls[0].body.encode() if isinstance(calls[0].body, str) else calls[0].body)
    assert 'fixture' not in calls[0].url
    response = provider._request('https://source.invalid/ok', lambda: None)
    assert provider._read(response, lambda: None, 1000) == b'ok'


@pytest.mark.parametrize('post', [False, True])
def test_credentials_never_follow_cross_host_redirect(tmp_path, transport, post):
    calls, responses = transport
    responses.append((307 if post else 302, b'', {'Location': 'https://cdn.source.invalid/redirected'}))
    provider = PublicProvider(tmp_path, 'eu', 0, 1)
    kwargs = dict(method='POST', data={'client_secret': 'fixture secret'}) if post else dict(params={'apikey': 'fixture secret'})
    with pytest.raises(providers.ProviderError) as error:
        provider._request('https://source.invalid/request', lambda: None, **kwargs)
    assert len(calls) == 1
    assert 'fixture secret' not in str(error.value)


def test_missing_image_has_a_distinct_permanent_error(tmp_path, transport):
    _, responses = transport
    responses.append((404, b'not found', {}))
    provider = PublicProvider(tmp_path, 'eu', 0, 1)
    with pytest.raises(providers.ProviderError) as error:
        provider._request('https://source.invalid/cover.png', lambda: None)
    assert isinstance(error.value, providers.ArtworkUnavailable)


def test_http_attempt_counts_do_not_require_a_cover_or_count_denied_dispatch(tmp_path, transport):
    calls, responses = transport
    responses.append((404, b'not found', {}))
    provider = PublicProvider(tmp_path, 'eu', 0, 1)
    assert provider.request_stats()['requests'] == 0
    with pytest.raises(providers.ArtworkUnavailable):
        provider._request('https://source.invalid/no-cover.png', lambda: None)
    assert len(calls) == provider.request_stats()['requests'] == 1
    assert provider.request_stats()['api_requests'] == 0
    def denied():
        raise providers.QuotaError('Local request budget is exhausted.')
    provider._before_request = denied
    with pytest.raises(providers.QuotaError):
        provider._request('https://source.invalid/denied.png', lambda: None)
    assert len(calls) == provider.request_stats()['requests'] == 1


def test_missing_reasons_are_isolated_by_worker_thread(tmp_path):
    provider = PublicProvider(tmp_path, 'eu', 0, 1)
    provider._unmatched('ambiguous_title', 'Several game IDs match.')
    other = []
    thread = threading.Thread(target=lambda: other.append(provider.missing_reason()))
    thread.start()
    thread.join(2)
    assert other[0][0] == 'no_title_match'
    assert provider.missing_reason()[0] == 'ambiguous_title'


def test_artwork_carries_verified_match_provenance_without_breaking_old_calls():
    assert Artwork('https://source.invalid/old.png', 'source').region == ''
    art = Artwork('https://source.invalid/cover.png', 'source', 'us', match_note='Other region cover.',
                  matched_title='Example', provider_game_id='123')
    assert (art.match_note, art.matched_title, art.provider_game_id) == ('Other region cover.', 'Example', '123')
