"""Official MobyGames v1 response contracts over deterministic HTTPS transport.

The response shapes and field names come from mobygames.com/info/api/.
Artwork addresses below are fixture addresses, never a claim of a live API key
or a live authenticated download. Only network I/O and elapsed time are faked.
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

from novacore_scraper.matching import MatchPolicy
from novacore_scraper.models import Game, System
from novacore_scraper.providers import AuthenticationError, ProviderError, QuotaError


@pytest.fixture
def moby():
    assert importlib.util.find_spec('novacore_scraper.mobygames') is not None, 'MobyGames provider is not implemented'
    return importlib.import_module('novacore_scraper.mobygames')


@pytest.fixture
def transport(monkeypatch):
    routes, calls = {}, []

    def send(session, request, **kwargs):
        calls.append(request)
        parsed = urlsplit(request.url)
        route = routes.get((parsed.hostname, parsed.path))
        assert route is not None, f'unexpected fixture endpoint: {parsed.hostname}{parsed.path}'
        item = route.pop(0) if isinstance(route, list) else route
        if callable(item):
            item = item(request)
        status, body, headers = item
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content, response._content_consumed = body, True
        response.url, response.request = request.url, request
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    # Production spacing is tested separately; metadata tests must not wait ten
    # seconds between each fixture response.
    monkeypatch.setattr('novacore_scraper.providers._Provider._rate', lambda *args: None)
    return routes, calls


def game(tmp_path, title='The X-Files Game (Europe)', system='psx'):
    folder = tmp_path / system
    return Game(System(system, system, folder, frozenset({'.zip'})), folder / (title + '.zip'), title + '.zip', title)


def platforms():
    return {'platforms': [{'platform_id': 3, 'platform_name': 'Windows'},
                          {'platform_id': 6, 'platform_name': 'PlayStation'},
                          {'platform_id': 7, 'platform_name': 'PlayStation 2'}]}


def games(entries=None):
    if entries is None:
        entries = [{'alternate_titles': [{'description': 'PSX title', 'title': 'The X Files'}],
                    'description': None, 'game_id': 1, 'genres': [], 'moby_score': None,
                    'moby_url': 'https://www.mobygames.com/game/1/the-x-files-game/',
                    'num_votes': 53, 'official_url': None,
                    'platforms': [{'first_release_date': '1999', 'platform_id': 6, 'platform_name': 'PlayStation'}],
                    'sample_cover': {'image': 'https://cdn.mobygames.com/covers/wrong-platform.jpg'},
                    'sample_screenshots': [], 'title': 'The X-Files Game'}]
    return {'games': entries}


def cover(image='eu-front.jpg', scan_of='Front Cover'):
    return {'comments': None, 'description': None, 'height': 1026,
            'image': 'https://cdn.mobygames.com/covers/' + image,
            'scan_of': scan_of, 'thumbnail_image': 'https://cdn.mobygames.com/fixture.webp', 'width': 800}


def covers(groups=None):
    if groups is None:
        groups = [{'comments': None, 'countries': ['United States'], 'covers': [cover('us-front.jpg')]},
                  {'comments': None, 'countries': ['Germany'], 'covers': [cover('back.jpg', 'Back Cover'),
                    cover('disc.jpg', 'Media'), cover('poster.jpg', 'Promo'), cover()]}]
    return {'cover_groups': groups}


def configure(routes):
    routes['api.mobygames.com', '/v1/platforms'] = 200, platforms(), {}
    routes['api.mobygames.com', '/v1/games'] = 200, games(), {}
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers(), {}


def provider(moby, tmp_path, **kwargs):
    return moby.MobyGamesProvider(tmp_path / 'cache', 'fixture+secret', interval=0, **kwargs)


def test_exact_platform_and_front_cover_with_encoded_key_and_persistent_cache(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    result = provider(moby, tmp_path).find(game(tmp_path), lambda: None)
    assert result is not None and result.source == 'MobyGames'
    assert result.url == 'https://cdn.mobygames.com/covers/eu-front.jpg' and result.region == 'eu'
    assert result.provider_game_id == '1' and result.matched_title == 'The X-Files Game'
    query = parse_qs(urlsplit(calls[1].url).query)
    assert query == {'api_key': ['fixture+secret'], 'platform': ['6'], 'title': ['The X-Files Game'],
                     'format': ['normal'], 'limit': ['100'], 'offset': ['0']}
    assert moby.MobyGamesProvider(tmp_path / 'cache', 'other-key').find(game(tmp_path), lambda: None) == result
    assert len(calls) == 3
    files = list((tmp_path / 'cache').glob('*.json'))
    assert len(files) == 3
    assert all('fixture+secret' not in item.read_text() and 'api_key' not in item.read_text() for item in files)


@pytest.mark.parametrize('entry', [
    {'game_id': 2, 'title': 'The X-Files Game 2', 'platforms': [{'platform_id': 6}]},
    {'game_id': 2, 'title': 'The X-Files Game', 'platforms': [{'platform_id': 7}]},
    {'game_id': 2, 'title': 'The X-Files Game (Demo)', 'platforms': [{'platform_id': 6}]},
])
def test_sequels_wrong_platform_and_edition_never_match(moby, transport, tmp_path, entry):
    routes, calls = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is None
    assert len(calls) == 2


def test_documented_alternate_title_can_establish_exact_identity(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    result = provider(moby, tmp_path).find(game(tmp_path, 'The X Files'), lambda: None)
    assert result and result.provider_game_id == '1' and result.matched_title == 'The X Files'


def test_distinct_same_title_ids_are_ambiguous(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    entries = games()['games']
    entries.append({**entries[0], 'game_id': 2})
    routes['api.mobygames.com', '/v1/games'] = 200, games(entries), {}
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is None
    assert len(calls) == 2


def test_search_pages_are_checked_before_unique_identity_is_accepted(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    first = games()['games'] + [{'game_id': n + 20, 'title': 'Other title', 'platforms': [{'platform_id': 6}]} for n in range(99)]
    second = [{**games()['games'][0], 'game_id': 2}]
    routes['api.mobygames.com', '/v1/games'] = [(200, games(first), {}), (200, games(second), {})]
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is None
    assert parse_qs(urlsplit(calls[-1].url).query)['offset'] == ['100']


def test_strict_region_and_revision_policy_is_honored(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([
        {'countries': ['United States'], 'covers': [cover('us-front.jpg')]}]), {}
    strict = MatchPolicy(allow_other_regions=False, allow_base_cover=False)
    assert provider(moby, tmp_path, match_policy=strict).find(game(tmp_path), lambda: None) is None
    assert provider(moby, tmp_path, match_policy=strict).find(game(tmp_path, 'The X-Files Game (Disc 1)'), lambda: None) is None
    result = provider(moby, tmp_path).find(game(tmp_path, 'The X-Files Game (Europe) (Disc 1)'), lambda: None)
    assert result and 'Other-region' in result.match_note and 'packaging' in result.match_note


def test_back_media_promo_and_sample_cover_are_not_front_art(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([
        {'countries': ['Worldwide'], 'covers': [cover('back.jpg', 'Back Cover'), cover('disc.jpg', 'Media'),
                                               cover('promo.jpg', 'Promo')]}]), {}
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is None


def test_missing_key_fails_before_network(moby, transport, tmp_path):
    _, calls = transport
    with pytest.raises(AuthenticationError):
        moby.MobyGamesProvider(tmp_path, '').prepare(lambda: None)
    assert calls == []


@pytest.mark.parametrize('status,kind', [(401, AuthenticationError), (403, AuthenticationError), (429, QuotaError)])
def test_auth_and_quota_failures_stop_once_without_exposing_key(moby, transport, tmp_path, status, kind):
    routes, calls = transport
    routes['api.mobygames.com', '/v1/platforms'] = status, {'message': 'fixture+secret'}, {'Retry-After': '0'}
    p = provider(moby, tmp_path)
    for _ in range(2):
        with pytest.raises(kind) as error:
            p.prepare(lambda: None)
        assert 'fixture+secret' not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize('address', ['https://cdn.mobygames.com.evil.test/covers/front.jpg',
    'https://api.mobygames.com/v1/games?api_key=stolen', 'https://cdn.mobygames.com:444/covers/front.jpg',
    'https://member:secret@cdn.mobygames.com/covers/front.jpg', 'https://cdn.mobygames.com/user/avatar.jpg',
    'https://cdn.mobygames.com/covers/front.jpg?api_key=fixture+secret'])
def test_untrusted_artwork_addresses_are_rejected(moby, transport, tmp_path, address):
    routes, _ = transport
    configure(routes)
    image = cover()
    image['image'] = address
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([{'countries': [], 'covers': [image]}]), {}
    with pytest.raises(ProviderError):
        provider(moby, tmp_path).find(game(tmp_path), lambda: None)


def test_official_legacy_image_addresses_upgrade_to_https(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    image = cover()
    image['image'] = 'http://www.mobygames.com/images/covers/l/fixture-front.jpg'
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([{'countries': [], 'covers': [image]}]), {}
    result = provider(moby, tmp_path).find(game(tmp_path), lambda: None)
    assert result and result.url == 'https://www.mobygames.com/images/covers/l/fixture-front.jpg'


def test_download_uses_public_image_host_without_api_key_and_preserves_rom(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    output = io.BytesIO()
    Image.new('RGB', (3, 4), (12, 34, 56)).save(output, 'JPEG')
    routes['cdn.mobygames.com', '/covers/eu-front.jpg'] = 200, output.getvalue(), {}
    rom = game(tmp_path)
    rom.path.parent.mkdir()
    rom.path.write_bytes(b'unchanged ROM')
    p = provider(moby, tmp_path)
    result = p.find(rom, lambda: None)
    destination = tmp_path / 'cover.png'
    p.download(result, destination, lambda: None)
    with Image.open(destination) as picture:
        assert picture.format == 'PNG' and picture.size == (3, 4)
    assert not urlsplit(calls[-1].url).query and calls[-1].body is None
    assert rom.path.read_bytes() == b'unchanged ROM'


def test_rate_floor_covers_legacy_allowance(moby, tmp_path):
    p = moby.MobyGamesProvider(tmp_path, 'key', interval=0, workers=16)
    assert p.interval >= 10 and p.workers == 1


def test_malformed_game_rows_do_not_hide_an_unsearched_ambiguous_page(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    first = games()['games'] + [{'game_id': n + 20, 'title': 'Other title', 'platforms': [{'platform_id': 6}]} for n in range(98)]
    first.append({'game_id': 999, 'platforms': [{'platform_id': 6}]})
    routes['api.mobygames.com', '/v1/games'] = 200, games(first), {}
    with pytest.raises(ProviderError):
        provider(moby, tmp_path).find(game(tmp_path), lambda: None)


def test_null_optional_alternate_title_list_is_treated_as_empty(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    entry = games()['games'][0]
    entry['alternate_titles'] = None
    routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is not None


def test_invalid_json_never_becomes_a_missing_match_or_disk_cache(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games'] = 200, b'not json fixture+secret', {}
    with pytest.raises(ProviderError) as error:
        provider(moby, tmp_path).find(game(tmp_path), lambda: None)
    assert 'fixture+secret' not in str(error.value)
    assert len(list((tmp_path / 'cache').glob('*.json'))) == 1


def test_unknown_platform_and_oversized_title_have_bounded_search(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    p = provider(moby, tmp_path)
    assert p.find(game(tmp_path, system='not-a-real-system'), lambda: None) is None
    assert p.find(game(tmp_path, title='X' * 129), lambda: None) is None
    assert len(calls) == 1


def test_temporary_server_failure_retries_without_caching_error(moby, transport, tmp_path, monkeypatch):
    routes, calls = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/platforms'] = [(503, {'message': 'fixture+secret'}, {'Retry-After': '0'}),
                                                  (200, platforms(), {})]
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is not None
    assert len(calls) == 4


def test_plain_alternate_title_cannot_hide_a_qualified_canonical_variant(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    entry = games()['games'][0]
    entry['title'] = 'The X-Files Game (Hack)'
    entry['alternate_titles'] = [{'title': 'The X-Files Game', 'description': 'Alternate title'}]
    routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    assert provider(moby, tmp_path).find(game(tmp_path), lambda: None) is None
    assert len(calls) == 2


def test_equivalent_parenthetical_full_title_remains_an_exact_match(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    entry = games()['games'][0]
    entry['title'] = 'Adventure (Episode II)'
    routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    assert provider(moby, tmp_path).find(game(tmp_path, 'Adventure Episode II (Europe)'), lambda: None) is not None


@pytest.mark.parametrize('location', ['platform_name', 'title', 'alternate_title', 'country', 'image'])
def test_credential_echo_in_retained_catalogue_fields_is_rejected_before_cache(moby, transport, tmp_path, location):
    routes, _ = transport
    configure(routes)
    if location == 'platform_name':
        data = platforms()
        data['platforms'][0]['platform_name'] = 'Invalid fixture+secret platform'
        routes['api.mobygames.com', '/v1/platforms'] = 200, data, {}
    elif location in {'title', 'alternate_title'}:
        entry = games()['games'][0]
        if location == 'title':
            entry['title'] = 'Invalid fixture+secret title'
        else:
            entry['alternate_titles'][0]['title'] = 'Invalid fixture+secret alias'
        routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    else:
        data = covers()
        if location == 'country':
            data['cover_groups'][0]['countries'] = ['Invalid fixture+secret country']
        else:
            data['cover_groups'][1]['covers'][-1]['image'] = 'https://cdn.mobygames.com/covers/fixture+secret-front.jpg'
        routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, data, {}
    with pytest.raises(ProviderError) as error:
        provider(moby, tmp_path).find(game(tmp_path), lambda: None)
    assert 'fixture+secret' not in str(error.value)
    assert all('fixture+secret' not in path.read_text() for path in (tmp_path / 'cache').glob('*') if path.is_file())


def test_percent_encoded_credential_echo_is_rejected_before_cache(moby, transport, tmp_path):
    routes, _ = transport
    configure(routes)
    entry = games()['games'][0]
    entry['title'] = 'Invalid fixture%2Bsecret title'
    routes['api.mobygames.com', '/v1/games'] = 200, games([entry]), {}
    with pytest.raises(ProviderError) as error:
        provider(moby, tmp_path).find(game(tmp_path), lambda: None)
    assert 'secret' not in str(error.value)
    assert all('fixture%2Bsecret' not in path.read_text() for path in (tmp_path / 'cache').glob('*') if path.is_file())


def test_candidates_are_lazy_ranked_and_retain_all_allowed_fronts_for_one_id(moby, transport, tmp_path):
    routes, calls = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([
        {'countries': ['United States'], 'covers': [cover('us.jpg')]},
        {'countries': ['Germany'], 'covers': [cover('b-eu.jpg'), cover('a-eu.jpg'), cover('a-eu.jpg')]},
    ]), {}
    p = provider(moby, tmp_path)
    candidates = p.candidates(game(tmp_path), lambda: None)
    assert calls == []
    result = list(candidates)
    assert [item.url for item in result] == ['https://cdn.mobygames.com/covers/a-eu.jpg',
        'https://cdn.mobygames.com/covers/b-eu.jpg', 'https://cdn.mobygames.com/covers/us.jpg']
    assert {item.provider_game_id for item in result} == {'1'}
    assert p.find(game(tmp_path), lambda: None) == result[0]
    strict = provider(moby, tmp_path, match_policy=MatchPolicy(allow_other_regions=False))
    assert [item.region for item in strict.candidates(game(tmp_path), lambda: None)] == ['eu', 'eu']


def test_engine_tries_second_verified_moby_front_when_preferred_front_is_gone(moby, transport, tmp_path):
    from novacore_scraper.engine import ScrapeEngine

    routes, calls = transport
    configure(routes)
    routes['api.mobygames.com', '/v1/games/1/platforms/6/covers'] = 200, covers([
        {'countries': ['Germany'], 'covers': [cover('a-gone.jpg'), cover('b-working.jpg')]},
    ]), {}
    output = io.BytesIO()
    Image.new('RGB', (3, 4), (12, 34, 56)).save(output, 'JPEG')
    routes['cdn.mobygames.com', '/covers/a-gone.jpg'] = 404, b'gone', {}
    routes['cdn.mobygames.com', '/covers/b-working.jpg'] = 200, output.getvalue(), {}
    rom = game(tmp_path)
    rom.path.parent.mkdir()
    rom.path.write_bytes(b'unchanged ROM')
    engine = ScrapeEngine(tmp_path, provider(moby, tmp_path), process_running=lambda: True,
                          retry_passes=0, keep_awake=False)
    engine.start([rom])
    assert engine.wait(5)
    result = engine.snapshot()
    assert (result.completed, result.missing, result.failed) == (1, 0, 0)
    job = engine.jobs_page()[0][0]
    with Image.open(job.artwork) as image:
        assert image.format == 'PNG' and image.size == (3, 4)
    assert [urlsplit(item.url).path for item in calls[-2:]] == ['/covers/a-gone.jpg', '/covers/b-working.jpg']
    assert rom.path.read_bytes() == b'unchanged ROM'
