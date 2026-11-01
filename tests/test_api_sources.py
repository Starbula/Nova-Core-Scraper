"""Official API response shapes through a deterministic requests transport.

Fixtures reflect TheGamesDB OpenAPI spec and api-docs.igdb.com. They make no
claim that a live credential or an artwork download has been exercised.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from PIL import Image

from novacore_scraper.models import Game, System
from novacore_scraper.providers import AuthenticationError, ProviderError, QuotaError


@pytest.fixture
def api():
    assert importlib.util.find_spec('novacore_scraper.api_sources') is not None, 'API providers are not implemented'
    return importlib.import_module('novacore_scraper.api_sources')


@pytest.fixture
def transport(monkeypatch):
    routes, calls = {}, []

    def send(session, request, **kwargs):
        calls.append(request)
        endpoint = urlsplit(request.url).path
        route = routes.get(endpoint)
        assert route is not None, f'unexpected endpoint: {endpoint}'
        item = route.pop(0) if isinstance(route, list) else route
        if callable(item):
            item = item(request)
        status, body, headers = item
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body
        response._content_consumed = True
        response.url, response.request = request.url, request
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    return routes, calls


def game(tmp_path, title='Sonic the Hedgehog', system='megadrive'):
    folder = tmp_path / system
    return Game(System(system, system, folder, frozenset({'.zip'})), folder / (title + '.zip'), title + '.zip', title)


def tgdb(data, **extra):
    return {'code': 200, 'status': 'Success', 'remaining_monthly_allowance': 100,
            'extra_allowance': 0, 'data': data, **extra}


def tg_platforms():
    return tgdb({'count': 2, 'platforms': {'18': {'id': 18, 'name': 'Sega Genesis', 'alias': 'sega-genesis'},
                                        '12': {'id': 12, 'name': 'Sega Master System', 'alias': 'sega-master-system'}}})


def tg_games(games=None, images=None, **extra):
    if games is None:
        games = [{'id': 92, 'game_title': 'Sonic the Hedgehog', 'platform': 18, 'alternates': []}]
    if images is None:
        images = [{'id': 1, 'type': 'boxart', 'side': 'back', 'filename': 'boxart/back/92-1.jpg'},
                  {'id': 2, 'type': 'boxart', 'side': 'front', 'filename': 'boxart/front/92-1.jpg'}]
    return tgdb({'count': len(games), 'games': games},
                include={'boxart': {'base_url': {'original': 'https://cdn.thegamesdb.net/images/original/'},
                                    'data': {'92': images}}},
                pages={'current': 'https://api.thegamesdb.net/?apikey=fixture-secret', 'next': ''}, **extra)


def ig_platforms():
    return [{'id': 29, 'name': 'Sega Mega Drive/Genesis', 'alternative_name': 'Sega Genesis', 'abbreviation': 'SMD', 'slug': 'genesis'},
            {'id': 64, 'name': 'Sega Master System', 'abbreviation': 'SMS', 'slug': 'sms'}]


def ig_games():
    return [{'id': 3192, 'name': 'Sonic the Hedgehog', 'platforms': [29],
             'alternative_names': [], 'cover': {'id': 834, 'image_id': 'co1abc',
             'url': '//images.igdb.com/igdb/image/upload/t_thumb/co1abc.jpg'}}]


def configure_ig(routes):
    routes['/oauth2/token'] = 200, {'access_token': 'fixture-token', 'expires_in': 3600, 'token_type': 'bearer'}, {}
    routes['/v4/platforms'] = 200, ig_platforms(), {}
    routes['/v4/games'] = 200, ig_games(), {}


def test_tgdb_uses_platform_filter_front_art_and_secret_free_persistent_cache(api, transport, tmp_path):
    routes, calls = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games(), {}
    provider = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    result = provider.find(game(tmp_path), lambda: None)
    assert result is not None and result.source == 'TheGamesDB'
    assert result.url == 'https://cdn.thegamesdb.net/images/original/boxart/front/92-1.jpg'
    query = parse_qs(urlsplit(calls[-1].url).query)
    assert query['filter[platform]'] == ['18']
    assert query['fields'] == ['alternates']
    assert query['apikey'] == ['fixture-secret']
    again = api.TheGamesDBProvider(tmp_path / 'cache', 'different-secret', interval=0)
    assert again.find(game(tmp_path), lambda: None) == result
    assert len(calls) == 2
    files = list((tmp_path / 'cache').glob('*.json'))
    assert len(files) >= 2
    assert all('fixture-secret' not in path.read_text() and 'apikey' not in path.read_text() for path in files)


@pytest.mark.parametrize('candidate', [
    {'id': 92, 'game_title': 'Sonic the Hedgehog 2', 'platform': 18},
    {'id': 92, 'game_title': 'Sonic the Hedgehog', 'platform': 12},
])
def test_tgdb_never_crosses_platforms_or_selects_sequels(api, transport, tmp_path, candidate):
    routes, _ = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games([candidate]), {}
    assert api.TheGamesDBProvider(tmp_path / 'cache', 'key', interval=0).find(game(tmp_path), lambda: None) is None


def test_tgdb_uses_documented_alternate_titles(api, transport, tmp_path):
    routes, _ = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games([{'id': 92, 'game_title': 'Regional title', 'platform': 18,
                                                                   'alternates': ['Sonic the Hedgehog']}]), {}
    assert api.TheGamesDBProvider(tmp_path / 'cache', 'key', interval=0).find(game(tmp_path), lambda: None) is not None


def test_tgdb_ambiguous_same_title_ids_are_skipped(api, transport, tmp_path):
    routes, _ = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games([
        {'id': 92, 'game_title': 'Sonic the Hedgehog', 'platform': 18},
        {'id': 93, 'game_title': 'Sonic the Hedgehog', 'platform': 18}]), {}
    assert api.TheGamesDBProvider(tmp_path / 'cache', 'key', interval=0).find(game(tmp_path), lambda: None) is None


def test_tgdb_exhausted_allowance_stops_future_requests(api, transport, tmp_path):
    routes, calls = transport
    response = tg_platforms()
    response['remaining_monthly_allowance'] = 0
    routes['/v1/Platforms'] = 200, response, {}
    provider = api.TheGamesDBProvider(tmp_path / 'cache', 'key', interval=0)
    with pytest.raises(QuotaError):
        provider.find(game(tmp_path), lambda: None)
    assert len(calls) == 1


def test_igdb_oauth_form_post_scoped_query_cover_and_token_reuse(api, transport, tmp_path):
    routes, calls = transport
    configure_ig(routes)
    provider = api.IGDBProvider(tmp_path / 'cache', 'fixture-client', 'fixture-secret', interval=0)
    result = provider.find(game(tmp_path), lambda: None)
    assert result is not None and result.source == 'IGDB'
    assert result.url == 'https://images.igdb.com/igdb/image/upload/t_cover_big_2x/co1abc.jpg'
    assert calls[0].method == 'POST' and not urlsplit(calls[0].url).query
    assert parse_qs(calls[0].body) == {'client_id': ['fixture-client'], 'client_secret': ['fixture-secret'], 'grant_type': ['client_credentials']}
    query = calls[-1].body.decode() if isinstance(calls[-1].body, bytes) else calls[-1].body
    assert calls[-1].method == 'POST' and 'platforms = 29' in query
    assert calls[-1].headers['Client-ID'] == 'fixture-client'
    assert calls[-1].headers['Authorization'] == 'Bearer fixture-token'
    assert provider.find(game(tmp_path), lambda: None) == result
    assert len(calls) == 3
    assert all('fixture-token' not in path.read_text() and 'fixture-secret' not in path.read_text()
               for path in (tmp_path / 'cache').glob('*.json'))


def test_igdb_refreshes_rejected_token_once(api, transport, tmp_path):
    routes, calls = transport
    configure_ig(routes)
    routes['/oauth2/token'] = [routes['/oauth2/token'], (200, {'access_token': 'refreshed', 'expires_in': 3600, 'token_type': 'bearer'}, {})]
    routes['/v4/games'] = [(401, {'message': 'fixture-token'}, {}), (200, ig_games(), {})]
    provider = api.IGDBProvider(tmp_path / 'cache', 'fixture-client', 'fixture-secret', interval=0)
    assert provider.find(game(tmp_path), lambda: None) is not None
    assert len([r for r in calls if urlsplit(r.url).path == '/oauth2/token']) == 2
    assert calls[-1].headers['Authorization'] == 'Bearer refreshed'


@pytest.mark.parametrize('candidate', [
    {'id': 3192, 'name': 'Sonic the Hedgehog 2', 'platforms': [29], 'cover': {'image_id': 'co1abc'}},
    {'id': 3192, 'name': 'Sonic the Hedgehog', 'platforms': [64], 'cover': {'image_id': 'co1abc'}},
])
def test_igdb_never_crosses_platforms_or_selects_sequels(api, transport, tmp_path, candidate):
    routes, _ = transport
    configure_ig(routes)
    routes['/v4/games'] = 200, [candidate], {}
    assert api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0).find(game(tmp_path), lambda: None) is None


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_missing_credentials_fail_before_network(api, transport, tmp_path, kind):
    _, calls = transport
    provider = (api.TheGamesDBProvider(tmp_path, '') if kind == 'tgdb' else api.IGDBProvider(tmp_path, '', ''))
    with pytest.raises(AuthenticationError):
        provider.prepare(lambda: None)
    assert calls == []


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_provider_auth_failures_never_expose_remote_body_or_credentials(api, transport, tmp_path, kind):
    routes, calls = transport
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 403, {'error': 'fixture-secret https://host/?apikey=fixture-secret'}, {}
        provider = api.TheGamesDBProvider(tmp_path, 'fixture-secret', interval=0)
    else:
        routes['/oauth2/token'] = 400, {'message': 'fixture-secret'}, {}
        provider = api.IGDBProvider(tmp_path, 'client', 'fixture-secret', interval=0)
    for _ in range(2):
        with pytest.raises(AuthenticationError) as error:
            provider.prepare(lambda: None)
        assert 'fixture-secret' not in str(error.value) and 'https://' not in str(error.value)
    assert len(calls) == 1


def test_igdb_observes_official_rate_and_open_request_limits(api, tmp_path):
    provider = api.IGDBProvider(tmp_path, 'client', 'secret', interval=0, workers=16)
    assert provider.interval >= .25 and provider.workers <= 8


def test_tgdb_only_front_cover_assets_are_eligible(api, transport, tmp_path):
    routes, _ = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games(images=[
        {'type': 'boxart', 'side': 'back', 'filename': 'boxart/back/92.jpg'},
        {'type': 'screenshot', 'side': 'front', 'filename': 'screenshots/92.jpg'},
        {'type': 'boxart', 'side': 'front', 'filename': '../escape.jpg'},
    ]), {}
    assert api.TheGamesDBProvider(tmp_path / 'cache', 'key', interval=0).find(game(tmp_path), lambda: None) is None


def test_igdb_localized_cover_respects_explicit_region_and_records_fallback_note(api, transport, tmp_path):
    from novacore_scraper.matching import MatchPolicy
    routes, _ = transport
    configure_ig(routes)
    payload = ig_games()
    payload[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_eu'}, 'region': {'name': 'Europe', 'identifier': 'EU'}},
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_us'}, 'region': {'name': 'North America', 'identifier': 'US'}},
    ]
    routes['/v4/games'] = 200, payload, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', region='us', interval=0)
    result = provider.find(game(tmp_path, 'Sonic the Hedgehog (Europe) (Rev 1)'), lambda: None)
    assert result is not None and result.region == 'eu' and result.url.endswith('/co_eu.jpg')
    assert 'revision/disc' in result.match_note
    strict = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                              match_policy=MatchPolicy(allow_other_regions=False, allow_base_cover=False))
    assert strict.find(game(tmp_path, 'Sonic the Hedgehog (Europe) (Rev 1)'), lambda: None) is None


def test_igdb_ambiguous_games_are_not_resolved_by_cover_availability(api, transport, tmp_path):
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows.append({'id': 3193, 'name': 'Sonic the Hedgehog', 'platforms': [29]})
    routes['/v4/games'] = 200, rows, {}
    assert api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0).find(game(tmp_path), lambda: None) is None


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_html_auth_failure_is_latched_and_sanitized(api, transport, tmp_path, kind):
    routes, calls = transport
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 403, b'<html>fixture-secret</html>', {}
        provider = api.TheGamesDBProvider(tmp_path, 'fixture-secret', interval=0)
    else:
        routes['/oauth2/token'] = 400, b'<html>fixture-secret</html>', {}
        provider = api.IGDBProvider(tmp_path, 'client', 'fixture-secret', interval=0)
    for _ in range(2):
        with pytest.raises(AuthenticationError) as error:
            provider.prepare(lambda: None)
        assert 'fixture-secret' not in str(error.value)
    assert len(calls) == 1


def test_malformed_cached_records_are_refetched(api, transport, tmp_path):
    routes, calls = transport
    routes['/v1/Platforms'] = 200, tg_platforms(), {}
    routes['/v1.1/Games/ByGameName'] = 200, tg_games(), {}
    first = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    assert first.find(game(tmp_path), lambda: None) is not None
    for path in (tmp_path / 'cache').glob('*.json'):
        data = json.loads(path.read_text())
        if data['rows'] and 'platforms' in data['rows'][0]:
            data['rows'][0]['platforms'] = 'malformed'
            path.write_text(json.dumps(data))
    second = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    assert second.find(game(tmp_path), lambda: None) is not None
    assert len(calls) == 3


def test_strict_regional_policy_accepts_proven_igdb_localized_cover(api, transport, tmp_path):
    from novacore_scraper.matching import MatchPolicy
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_eu'}, 'region': {'name': 'Europe', 'identifier': 'EU'}},
    ]
    routes['/v4/games'] = 200, rows, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    result = provider.find(game(tmp_path, 'Sonic the Hedgehog (Europe)'), lambda: None)
    assert result is not None and result.region == 'eu'


def test_igdb_country_locale_identifier_overrides_unrecognized_display_name(api, transport, tmp_path):
    from novacore_scraper.matching import MatchPolicy
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_br'},
         'region': {'name': 'Português', 'identifier': 'pt-BR'}},
    ]
    routes['/v4/games'] = 200, rows, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    result = provider.find(game(tmp_path, 'Sonic the Hedgehog (Brazil)'), lambda: None)
    assert result is not None and result.region == 'br' and result.url.endswith('/co_br.jpg')


def test_igdb_language_only_identifier_does_not_invent_a_cover_region(api, transport, tmp_path):
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0].pop('cover')
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_en'}, 'region': {'identifier': 'en'}},
    ]
    routes['/v4/games'] = 200, rows, {}
    result = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0).find(game(tmp_path), lambda: None)
    assert result is not None and result.region == ''


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_cover_download_keeps_api_credentials_off_image_request(api, transport, tmp_path, kind):
    routes, calls = transport
    image = io.BytesIO()
    Image.new('RGB', (2, 3), 'green').save(image, 'PNG')
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 200, tg_platforms(), {}
        routes['/v1.1/Games/ByGameName'] = 200, tg_games(), {}
        routes['/images/original/boxart/front/92-1.jpg'] = 200, image.getvalue(), {}
        provider = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    else:
        configure_ig(routes)
        routes['/igdb/image/upload/t_cover_big_2x/co1abc.jpg'] = 200, image.getvalue(), {}
        provider = api.IGDBProvider(tmp_path / 'cache', 'fixture-client', 'fixture-secret', interval=0)
    artwork = provider.find(game(tmp_path), lambda: None)
    destination = tmp_path / 'front.png'
    provider.download(artwork, destination, lambda: None)
    with Image.open(destination) as decoded:
        assert decoded.size == (2, 3) and decoded.format == 'PNG'
    request = calls[-1]
    assert request.method == 'GET' and not urlsplit(request.url).query
    assert 'Authorization' not in request.headers and 'Client-ID' not in request.headers
    assert b'fixture-secret' not in destination.read_bytes()


def test_igdb_repeated_token_rejection_disables_source_without_retry_loop(api, transport, tmp_path):
    routes, calls = transport
    configure_ig(routes)
    routes['/v4/games'] = 401, {'message': 'fixture-token'}, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0)
    for _ in range(2):
        with pytest.raises(AuthenticationError) as error:
            provider.find(game(tmp_path), lambda: None)
        assert 'fixture-token' not in str(error.value)
    assert len([r for r in calls if urlsplit(r.url).path == '/v4/games']) == 2
    assert len([r for r in calls if urlsplit(r.url).path == '/oauth2/token']) == 2


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_api_alias_never_erases_canonical_content_qualifier(api, transport, tmp_path, kind):
    routes, _ = transport
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 200, tg_platforms(), {}
        routes['/v1.1/Games/ByGameName'] = 200, tg_games([
            {'id': 92, 'game_title': 'Sonic the Hedgehog (Hack)', 'platform': 18, 'alternates': ['Sonic the Hedgehog']}]), {}
        provider = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    else:
        configure_ig(routes)
        rows = ig_games()
        rows[0]['name'] = 'Sonic the Hedgehog (Hack)'
        rows[0]['alternative_names'] = [{'name': 'Sonic the Hedgehog'}]
        routes['/v4/games'] = 200, rows, {}
        provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0)
    assert provider.find(game(tmp_path), lambda: None) is None


def test_equivalent_full_title_can_include_parenthesized_subtitle(api, transport, tmp_path):
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0]['name'] = 'Adventure (Episode II)'
    routes['/v4/games'] = 200, rows, {}
    result = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0).find(game(tmp_path, 'Adventure Episode II'), lambda: None)
    assert result is not None


@pytest.mark.parametrize('language,country', [
    ('fr', 'France'), ('de', 'Germany'), ('es', 'Spain'), ('it', 'Italy'),
    ('pt', 'Portugal'), ('nl', 'Netherlands'),
])
def test_igdb_language_only_locale_cannot_satisfy_strict_country_policy(api, transport, tmp_path, language, country):
    from novacore_scraper.matching import MatchPolicy
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0].pop('cover')
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_language'},
         'region': {'category': 'locale', 'name': 'Localized language', 'identifier': language}},
    ]
    routes['/v4/games'] = 200, rows, {}
    requested = game(tmp_path, f'Sonic the Hedgehog ({country})')
    # These country codes need no title-tag assumptions: the strict match must
    # never obtain country metadata from a language-only localization record.
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    assert provider.find(requested, lambda: None) is None
    permissive = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0)
    result = permissive.find(game(tmp_path), lambda: None)
    assert result is not None and result.region == ''


@pytest.mark.parametrize('region,requested,expected', [
    ({'category': 'locale', 'name': 'Português', 'identifier': 'pt-BR'}, 'Brazil', 'br'),
    ({'category': 'locale', 'name': 'Français', 'identifier': 'fr-FR'}, 'France', 'fr'),
    ({'category': 'country', 'name': 'Français', 'identifier': 'fr'}, 'France', 'fr'),
])
def test_igdb_explicit_country_metadata_remains_eligible(api, transport, tmp_path, region, requested, expected):
    from novacore_scraper.matching import MatchPolicy
    routes, calls = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0].pop('cover')
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_country'}, 'region': region},
    ]
    routes['/v4/games'] = 200, rows, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    result = provider.find(game(tmp_path, f'Sonic the Hedgehog ({requested})'), lambda: None)
    assert result is not None and result.region == expected
    body = calls[-1].body.decode() if isinstance(calls[-1].body, bytes) else calls[-1].body
    assert 'game_localizations.region.category' in body


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_same_game_ranked_front_covers_continue_after_first_http_404(api, transport, tmp_path, kind):
    from novacore_scraper.sources import FallbackProvider
    routes, calls = transport
    image = io.BytesIO()
    Image.new('RGB', (3, 4), 'blue').save(image, 'PNG')
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 200, tg_platforms(), {}
        routes['/v1.1/Games/ByGameName'] = 200, tg_games(images=[
            {'type': 'boxart', 'side': 'front', 'filename': 'boxart/front/92-a.jpg'},
            {'type': 'boxart', 'side': 'front', 'filename': 'boxart/front/92-b.jpg'},
        ]), {}
        first, second = '/images/original/boxart/front/92-a.jpg', '/images/original/boxart/front/92-b.jpg'
        provider = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    else:
        configure_ig(routes)
        rows = ig_games()
        rows[0].pop('cover')
        rows[0]['game_localizations'] = [
            {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_a'}, 'region': {'category': 'continent', 'name': 'Europe', 'identifier': 'EU'}},
            {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_b'}, 'region': {'category': 'continent', 'name': 'North America', 'identifier': 'US'}},
        ]
        routes['/v4/games'] = 200, rows, {}
        first, second = '/igdb/image/upload/t_cover_big_2x/co_a.jpg', '/igdb/image/upload/t_cover_big_2x/co_b.jpg'
        provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', region='eu', interval=0)
    routes[first] = 404, b'missing', {}
    routes[second] = 200, image.getvalue(), {}
    chain = FallbackProvider([provider])
    iterator = chain.candidates(game(tmp_path), lambda: None)
    assert calls == []  # Constructing candidates cannot eagerly search/download.
    destination = tmp_path / 'front.png'
    first_art = next(iterator)
    assert urlsplit(first_art.url).path == first
    with pytest.raises(ProviderError):
        chain.download(first_art, destination, lambda: None)
    second_art = next(iterator, None)
    assert second_art is not None and second_art.provider_game_id == first_art.provider_game_id
    chain.download(second_art, destination, lambda: None)
    iterator.close()
    with Image.open(destination) as decoded:
        assert decoded.size == (3, 4)
    assert [urlsplit(request.url).path for request in calls if urlsplit(request.url).path in {first, second}] == [first, second]


def test_igdb_strict_policy_never_yields_disallowed_backup_cover(api, transport, tmp_path):
    from novacore_scraper.matching import MatchPolicy
    routes, calls = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_eu'}, 'region': {'category': 'continent', 'name': 'Europe', 'identifier': 'EU'}},
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_us'}, 'region': {'category': 'continent', 'name': 'North America', 'identifier': 'US'}},
    ]
    routes['/v4/games'] = 200, rows, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    candidates = list(provider.candidates(game(tmp_path, 'Sonic the Hedgehog (Europe)'), lambda: None))
    assert len(candidates) == 1 and candidates[0].url.endswith('/co_eu.jpg')
    assert all(urlsplit(request.url).path in {'/oauth2/token', '/v4/platforms', '/v4/games'} for request in calls)


@pytest.mark.parametrize('kind', ['tgdb', 'igdb'])
def test_capped_search_cannot_yield_false_unique_game(api, transport, tmp_path, monkeypatch, kind):
    routes, calls = transport
    monkeypatch.setattr(api, '_PAGE_LIMIT', 1)
    if kind == 'tgdb':
        routes['/v1/Platforms'] = 200, tg_platforms(), {}
        payload = tg_games()
        payload['pages']['next'] = 'https://api.thegamesdb.net/?apikey=fixture-secret&page=2'
        routes['/v1.1/Games/ByGameName'] = 200, payload, {}
        provider = api.TheGamesDBProvider(tmp_path / 'cache', 'fixture-secret', interval=0)
    else:
        configure_ig(routes)
        rows = ig_games() + [{'id': 1000 + number, 'name': f'Other title {number}', 'platforms': [29]} for number in range(49)]
        routes['/v4/games'] = 200, rows, {}
        provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0)
    with pytest.raises(ProviderError, match='too broad'):
        next(provider.candidates(game(tmp_path), lambda: None))
    assert not any('/image/' in urlsplit(request.url).path or '/images/' in urlsplit(request.url).path for request in calls)


def test_igdb_cover_note_explains_platform_packaging_limit(api, transport, tmp_path):
    routes, _ = transport
    configure_ig(routes)
    artwork = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0).find(game(tmp_path), lambda: None)
    assert artwork is not None
    assert 'Game-level cover; platform-specific packaging is unverified.' in artwork.match_note


def test_igdb_uppercase_language_without_category_does_not_prove_country(api, transport, tmp_path):
    from novacore_scraper.matching import MatchPolicy
    routes, _ = transport
    configure_ig(routes)
    rows = ig_games()
    rows[0].pop('cover')
    rows[0]['game_localizations'] = [
        {'name': 'Sonic the Hedgehog', 'cover': {'image_id': 'co_fr'},
         'region': {'name': 'Français', 'identifier': 'FR'}},
    ]
    routes['/v4/games'] = 200, rows, {}
    provider = api.IGDBProvider(tmp_path / 'cache', 'client', 'secret', interval=0,
                               match_policy=MatchPolicy(allow_other_regions=False))
    assert provider.find(game(tmp_path, 'Sonic the Hedgehog (France)'), lambda: None) is None
