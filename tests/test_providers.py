"""Provider boundary tests: deterministic HTTPS transport, actual PNG decoding and files."""
from __future__ import annotations

import importlib
import io
import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from PIL import Image

from novacore_scraper.models import Artwork, Game, System


@pytest.fixture
def provider_module():
    return importlib.import_module('novacore_scraper.providers')


@pytest.fixture
def transport(monkeypatch):
    routes = {}
    calls = []
    lock = threading.Lock()

    def send(session, request, **kwargs):
        parsed = urlsplit(request.url)
        with lock:
            calls.append((request.url, time.monotonic()))
            endpoint = parsed.path
            route = routes.get(endpoint)
            assert route is not None, f'unexpected fixture endpoint {endpoint}'
            item = route.pop(0) if isinstance(route, list) else route
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response._content = body
        response._content_consumed = True
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    return routes, calls


def game(tmp_path, title='Sonic the Hedgehog (Europe)', system='megadrive', ssid=None):
    folder = tmp_path / system
    folder.mkdir(exist_ok=True)
    rom = folder / (title + '.zip')
    rom.write_bytes(b'rom')
    return Game(System(system, system, folder, frozenset({'.zip'}), ssid), rom, rom.name, title)


def png_bytes():
    output = io.BytesIO()
    Image.new('RGB', (3, 4), (12, 34, 56)).save(output, 'PNG')
    return output.getvalue()


def ss_user(maxthreads='2', **extra):
    return {'response': {'ssuser': {'id': 'member', 'maxthreads': maxthreads,
        'maxrequestspermin': '60', 'maxrequestsperday': '1000', 'maxrequestskoperday': '100',
        'requeststoday': '0', 'requestskotoday': '0', **extra}}}


def ss_provider(module, tmp_path, **kwargs):
    return module.ScreenScraperProvider(tmp_path / 'cache', 'developer', 'devsecret', 'member', 'usersecret', interval=0, **kwargs)


def test_libretro_preserves_explicit_region_and_uses_cached_catalog(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic%20the%20Hedgehog%20(Europe).png">EU</a><a href="Sonic%20the%20Hedgehog%20(USA).png">US</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', region='us', interval=0)
    result = p.find(game(tmp_path), lambda: None)
    assert result and result.url.endswith('Sonic%20the%20Hedgehog%20%28Europe%29.png')
    assert result.region == 'eu'
    other = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    assert other.find(game(tmp_path), lambda: None) == result
    assert len(calls) == 1  # Cached index survives a new provider instance.


def test_libretro_will_not_cross_regions_or_guess_a_sequel(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (USA).png">US</a><a href="Sonic the Hedgehog 2 (Europe).png">2</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    assert p.find(game(tmp_path), lambda: None) is None
    assert p.find(game(tmp_path, 'Sonic'), lambda: None) is None


def test_libretro_uses_region_preference_only_with_unambiguous_title(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (USA).png">US</a><a href="Sonic the Hedgehog (Europe).png">EU</a>', {})
    result = provider_module.LibretroProvider(tmp_path / 'cache', region='eu', interval=0).find(
        game(tmp_path, 'Sonic the Hedgehog'), lambda: None)
    assert result and result.region == 'eu'


def test_libretro_never_matches_a_different_disc_or_revision(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sony%20-%20PlayStation/Named_Boxarts/'] = (200,
        '<a href="Final Fantasy VII (Europe) (Disc 2).png">2</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    assert p.find(game(tmp_path, 'Final Fantasy VII (Europe) (Disc 1)', 'psx'), lambda: None) is None


def test_unknown_system_is_unmatched_without_network(provider_module, transport, tmp_path):
    _, calls = transport
    assert provider_module.LibretroProvider(tmp_path / 'cache').find(game(tmp_path, system='unusual'), lambda: None) is None
    assert calls == []


def test_download_decodes_and_atomically_normalizes_png(provider_module, transport, tmp_path):
    routes, _ = transport
    output = io.BytesIO()
    Image.new('RGB', (3, 4), (12, 34, 56)).save(output, 'JPEG')
    routes['/cover.jpg'] = (200, output.getvalue(), {'Content-Type': 'image/jpeg'})
    destination = tmp_path / 'art' / 'cover.png'
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    p.download(Artwork('https://thumbnails.libretro.com/cover.jpg', 'Libretro'), destination, lambda: None)
    with Image.open(destination) as result:
        assert result.format == 'PNG'
        assert result.size == (3, 4)
    assert list(destination.parent.iterdir()) == [destination]


def test_download_does_not_overwrite_existing_or_racing_file(provider_module, transport, tmp_path):
    routes, calls = transport
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    destination = tmp_path / 'cover.png'
    destination.write_bytes(b'keep')
    art = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    with pytest.raises(FileExistsError):
        p.download(art, destination, lambda: None)
    assert calls == []
    destination.unlink()
    def race(request):
        destination.write_bytes(b'other writer')
        return 200, png_bytes(), {}
    routes['/cover.png'] = race
    with pytest.raises(FileExistsError):
        p.download(art, destination, lambda: None)
    assert destination.read_bytes() == b'other writer'
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('body', [b'NOMEDIA', b'<html>busy</html>', b'\x89PNG\r\n\x1a\nbroken'])
def test_bad_media_never_becomes_an_output_file(provider_module, transport, tmp_path, body):
    routes, _ = transport
    routes['/cover.png'] = (200, body, {})
    output = tmp_path / 'cover.png'
    with pytest.raises(provider_module.ProviderError):
        provider_module.LibretroProvider(tmp_path / 'cache', interval=0).download(
            Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro'), output, lambda: None)
    assert not output.exists()


@pytest.mark.parametrize('url', ['http://thumbnails.libretro.com/x.png', 'https://evil.example/x.png',
    'https://thumbnails.libretro.com.evil.example/x.png', 'https://user:password@thumbnails.libretro.com/x.png'])
def test_download_rejects_untrusted_url_before_request(provider_module, transport, tmp_path, url):
    _, calls = transport
    with pytest.raises(provider_module.ProviderError):
        provider_module.LibretroProvider(tmp_path / 'cache', interval=0).download(Artwork(url, 'Libretro'), tmp_path / 'out.png', lambda: None)
    assert calls == []


def test_redirect_is_validated_before_following(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/cover.png'] = (302, b'', {'Location': 'http://evil.example/cover.png'})
    with pytest.raises(provider_module.ProviderError):
        provider_module.LibretroProvider(tmp_path / 'cache', interval=0).download(
            Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro'), tmp_path / 'out.png', lambda: None)
    assert len(calls) == 1


def test_retry_transient_http_and_cancellation_interrupts_backoff(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/cover.png'] = [(503, b'', {'Retry-After': '0'}), (200, png_bytes(), {})]
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    art = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    p.download(art, tmp_path / 'out.png', lambda: None)
    assert len(calls) == 2
    routes['/again.png'] = (429, b'', {'Retry-After': '30'})
    def cancel():
        if any('/again.png' in item[0] for item in calls):
            raise provider_module.Cancelled()
    started = time.monotonic()
    with pytest.raises(provider_module.Cancelled):
        p.download(Artwork('https://thumbnails.libretro.com/again.png', 'Libretro'), tmp_path / 'again.png', cancel)
    assert time.monotonic() - started < 1
    assert not (tmp_path / 'again.png').exists()


def test_oversized_media_is_rejected_before_creating_output(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/cover.png'] = (200, png_bytes(), {'Content-Length': str(33 * 1024 * 1024)})
    with pytest.raises(provider_module.ProviderError):
        provider_module.LibretroProvider(tmp_path / 'cache', interval=0).download(
            Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro'), tmp_path / 'out.png', lambda: None)
    assert not (tmp_path / 'out.png').exists()


def test_screenscraper_prepares_permissions_and_matches_exact_title(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {'systemes': [{'id': '1', 'noms': {'nom_recalbox': 'megadrive'}}]}}, {})
    def search(request):
        params = parse_qs(urlsplit(request.url).query)
        assert params['systemeid'] == ['1']
        assert params['recherche'] == ['Sonic the Hedgehog']
        return 200, {'response': {'jeux': [
            {'id': '22', 'systeme': {'id': '1'}, 'noms': [{'text': 'Sonic the Hedgehog 2', 'region': 'eu'}], 'medias': []},
            {'id': '11', 'systeme': {'id': '1'}, 'noms': [{'text': 'Sonic the Hedgehog', 'region': 'eu'}],
                'medias': [{'type': 'box-2D', 'region': 'eu', 'url': 'https://api.screenscraper.fr/api2/mediaJeu.php?jeuid=11&media=box-2D(eu)'}]},
        ]}}, {}
    routes['/api2/jeuRecherche.php'] = search
    p = ss_provider(provider_module, tmp_path, workers=4)
    p.prepare(lambda: None)
    assert p.workers == 2
    result = p.find(game(tmp_path), lambda: None)
    assert result and 'jeuid=11' in result.url
    assert result.region == 'eu'
    assert len(calls) == 3


def test_screenscraper_explicit_id_override_and_region_do_not_guess(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {'systemes': []}}, {})
    routes['/api2/jeuRecherche.php'] = (200, {'response': {'jeux': [{'id': '11', 'systeme': {'id': '99'},
        'noms': [{'text': 'Sonic the Hedgehog', 'region': 'us'}],
        'medias': [{'type': 'box-2D', 'region': 'us', 'url': 'https://api.screenscraper.fr/a.png'}]}]}}, {})
    p = ss_provider(provider_module, tmp_path)
    assert p.find(game(tmp_path, system='custom', ssid='99'), lambda: None) is None


@pytest.mark.parametrize('status,exception_name', [(403, 'AuthenticationError'), (430, 'QuotaError'), (431, 'QuotaError')])
def test_fatal_errors_are_sanitized_and_latched(provider_module, transport, tmp_path, status, exception_name):
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = (status, 'devsecret usersecret https://secret.url/', {})
    p = ss_provider(provider_module, tmp_path)
    error_type = getattr(provider_module, exception_name)
    for _ in range(2):
        with pytest.raises(error_type) as error:
            p.prepare(lambda: None)
        assert not any(secret in str(error.value) for secret in ('devsecret', 'usersecret', 'https://'))
    assert len(calls) == 1


def test_screenscraper_stops_before_search_when_daily_quota_exhausted(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(requeststoday='1000'), {})
    with pytest.raises(provider_module.QuotaError):
        ss_provider(provider_module, tmp_path).find(game(tmp_path), lambda: None)
    assert len(calls) == 1


def test_network_exception_is_sanitized(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/api2/ssuserInfos.php'] = requests.ConnectionError('https://host/?sspassword=usersecret&devpassword=devsecret')
    with pytest.raises(provider_module.ProviderError) as error:
        ss_provider(provider_module, tmp_path).prepare(lambda: None)
    assert 'secret' not in str(error.value)
    assert 'https://' not in str(error.value)


def test_shared_request_spacing_and_worker_clamp(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/a.png'] = routes['/b.png'] = (200, png_bytes(), {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0.04, workers=99)
    assert p.workers == 8
    failures = []
    def run(name):
        try:
            p.download(Artwork('https://thumbnails.libretro.com/' + name + '.png', 'Libretro'), tmp_path / (name + '.png'), lambda: None)
        except Exception as exc:
            failures.append(exc)
    threads = [threading.Thread(target=run, args=(name,)) for name in ('a', 'b')]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not failures
    assert calls[1][1] - calls[0][1] >= 0.23


def test_streaming_bodies_share_provider_concurrency_limit(provider_module, transport, tmp_path, monkeypatch):
    routes, _ = transport
    routes['/a.png'] = routes['/b.png'] = (200, png_bytes(), {})
    original = requests.Response.iter_content
    lock = threading.Lock()
    active = 0
    maximum = 0
    def read(response, *args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.04)
            yield from original(response, *args, **kwargs)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(requests.Response, 'iter_content', read)
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0, workers=1)
    failures = []
    def run(name):
        try:
            p.download(Artwork('https://thumbnails.libretro.com/' + name + '.png', 'Libretro'), tmp_path / (name + '.png'), lambda: None)
        except Exception as error:
            failures.append(error)
    threads = [threading.Thread(target=run, args=(name,)) for name in ('a', 'b')]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not failures
    assert maximum == 1


def test_server_quota_updates_stop_the_next_request(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {
        'ssuser': ss_user(requeststoday='1000')['response']['ssuser'],
        'systemes': [{'id': '1', 'noms': {'nom_recalbox': 'megadrive'}}]}}, {})
    with pytest.raises(provider_module.QuotaError):
        ss_provider(provider_module, tmp_path).find(game(tmp_path), lambda: None)
    assert len(calls) == 2


def test_large_catalog_is_indexed_once_for_repeated_game_matching(provider_module, transport, tmp_path, monkeypatch):
    routes, calls = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        ''.join(f'<a href="Game {index} (Europe).png">cover</a>' for index in range(10000)), {})
    original = provider_module._name_info
    parses = 0
    def count(name):
        nonlocal parses
        parses += 1
        return original(name)
    monkeypatch.setattr(provider_module, '_name_info', count)
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    system = System('megadrive', 'megadrive', tmp_path, frozenset({'.zip'}))
    def item(index):
        title = f'Game {index}'
        return Game(system, tmp_path / (title + '.zip'), title + '.zip', title)
    for index in range(3):
        assert p.find(item(index), lambda: None) is not None
    assert parses < 20000, 'matching rescans the entire catalog for each game'
    started = time.monotonic()
    for index in range(1000):
        assert p.find(item(index), lambda: None) is not None
    assert time.monotonic() - started < 5
    assert len(calls) == 1


def test_pixel_limit_is_checked_before_png_publication(provider_module, transport, tmp_path, monkeypatch):
    routes, _ = transport
    routes['/cover.png'] = (200, png_bytes(), {})
    monkeypatch.setattr(provider_module, 'MAX_IMAGE_PIXELS', 10)
    with pytest.raises(provider_module.ProviderError):
        provider_module.LibretroProvider(tmp_path / 'cache', interval=0).download(
            Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro'), tmp_path / 'out.png', lambda: None)
    assert not (tmp_path / 'out.png').exists()


def test_screenscraper_does_not_collapse_meaningful_parenthetical_titles(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {'systemes': [{'id': '1', 'noms': {'nom_recalbox': 'megadrive'}}]}}, {})
    routes['/api2/jeuRecherche.php'] = (200, {'response': {'jeux': [{'id': '11', 'systeme': {'id': '1'},
        'noms': [{'text': 'Adventure (Episode II)', 'region': 'eu'}],
        'medias': [{'type': 'box-2D', 'region': 'eu', 'url': 'https://api.screenscraper.fr/box.png'}]}]}}, {})
    p = ss_provider(provider_module, tmp_path)
    assert p.find(game(tmp_path, 'Adventure (Episode I)'), lambda: None) is None


def test_screenscraper_server_download_speed_is_shared_across_streams(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(maxdownloadspeed='1', maxrequestspermin='1000000000'), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {'systemes': []}}, {})
    payload = png_bytes()
    routes['/a.png'] = routes['/b.png'] = (200, payload, {})
    p = ss_provider(provider_module, tmp_path, workers=2)
    p.prepare(lambda: None)
    failures = []
    def run(name):
        try:
            p.download(Artwork('https://api.screenscraper.fr/' + name + '.png', 'ScreenScraper'), tmp_path / (name + '.png'), lambda: None)
        except Exception as error:
            failures.append(error)
    threads = [threading.Thread(target=run, args=(name,)) for name in ('a', 'b')]
    started = time.monotonic()
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not failures
    assert time.monotonic() - started >= (2 * len(payload) / 1000) * 0.9


def test_libretro_does_not_cache_an_error_page_as_an_empty_catalog(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = [
        (200, '<html>Temporarily busy</html>', {}),
        (200, '<a href="Sonic the Hedgehog (Europe).png">box</a>', {}),
    ]
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    with pytest.raises(provider_module.ProviderError):
        p.find(game(tmp_path), lambda: None)
    assert p.find(game(tmp_path), lambda: None) is not None
    assert len(calls) == 2


def test_missing_libretro_catalog_is_requested_once_per_session(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (404, b'', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    item = game(tmp_path)
    for _ in range(10):
        assert p.find(item, lambda: None) is None
    assert len(calls) == 1


def test_screenscraper_new_rate_permission_applies_to_next_request(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = (200, ss_user(maxrequestspermin='1500'), {})
    routes['/api2/systemesListe.php'] = (200, {'response': {'systemes': []}}, {})
    ss_provider(provider_module, tmp_path).prepare(lambda: None)
    assert calls[1][1] - calls[0][1] >= 0.035


def test_libretro_plain_title_does_not_select_a_hack_or_different_edition(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (Europe) (Hack).png">hack</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    assert p.find(game(tmp_path, 'Sonic the Hedgehog'), lambda: None) is None


def test_libretro_display_title_does_not_erase_explicit_file_qualifiers(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (Europe).png">plain</a>', {})
    item = game(tmp_path, 'Sonic the Hedgehog (Europe) (Hack)')
    item = Game(item.system, item.path, item.relative_path, 'Sonic the Hedgehog (Europe)')
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    assert p.find(item, lambda: None) is None


def test_same_url_concurrent_downloads_share_one_validated_response(provider_module, transport, tmp_path):
    """Removing coalescing or successful-image reuse repeats network transfers."""
    from concurrent.futures import ThreadPoolExecutor
    routes, calls = transport
    started = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(8)
    def response(request):
        started.set()
        assert release.wait(3)
        return 200, png_bytes(), {}
    routes['/shared.png'] = response
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0, workers=8)
    artwork = Artwork('https://thumbnails.libretro.com/shared.png', 'Libretro')
    def download(index):
        barrier.wait(timeout=3)
        p.download(artwork, tmp_path / f'cover-{index}.png', lambda: None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(download, index) for index in range(8)]
        try:
            assert started.wait(3)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    p.download(artwork, tmp_path / 'another.png', lambda: None)
    assert len(calls) == 1
    for index in range(8):
        with Image.open(tmp_path / f'cover-{index}.png') as image:
            assert image.format == 'PNG' and image.size == (3, 4)
    assert not (tmp_path / 'cache').exists(), 'image cache must remain in memory'


def test_distinct_urls_fill_all_sixteen_shared_provider_slots(provider_module, transport, tmp_path):
    """A global download lock or the old four-worker clamp stalls distinct URLs."""
    from concurrent.futures import ThreadPoolExecutor
    routes, calls = transport
    class PublicProvider(provider_module._Provider):
        def _trusted_host(self, host):
            return host == 'thumbnails.libretro.com'
    p = PublicProvider(tmp_path / 'cache', 'eu', 0, 99)
    release = threading.Event()
    all_entered = threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    def response(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 16:
                all_entered.set()
        try:
            assert release.wait(4)
            return 200, png_bytes(), {}
        finally:
            with lock:
                active -= 1
    for index in range(16):
        routes[f'/{index}.png'] = response
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(p.download, Artwork(f'https://thumbnails.libretro.com/{index}.png', 'Test'),
            tmp_path / f'{index}.png', lambda: None) for index in range(16)]
        try:
            progressed = all_entered.wait(2)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    assert progressed, f'only {maximum} distinct requests progressed together'
    assert maximum == 16
    assert len(calls) == 16


def test_image_cache_evicts_least_recently_used_bytes(provider_module, transport, tmp_path, monkeypatch):
    """The byte budget must evict cold images while a cache hit refreshes recency."""
    routes, calls = transport
    raw = png_bytes()
    monkeypatch.setattr(provider_module, 'MAX_IMAGE_CACHE_BYTES', len(raw) * 2, raising=False)
    for name in ('a', 'b', 'c'):
        routes[f'/{name}.png'] = 200, raw, {}
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    for index, name in enumerate(('a', 'b', 'a', 'c', 'b')):
        p.download(Artwork(f'https://thumbnails.libretro.com/{name}.png', 'Libretro'),
            tmp_path / f'{index}.png', lambda: None)
    assert [urlsplit(url).path for url, _ in calls] == ['/a.png', '/b.png', '/c.png', '/b.png']


def test_image_larger_than_cache_budget_is_valid_but_requested_again(provider_module, transport, tmp_path, monkeypatch):
    routes, calls = transport
    raw = png_bytes()
    monkeypatch.setattr(provider_module, 'MAX_IMAGE_CACHE_BYTES', len(raw) - 1, raising=False)
    routes['/large.png'] = 200, raw, {}
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    for index in range(2):
        p.download(Artwork('https://thumbnails.libretro.com/large.png', 'Libretro'),
            tmp_path / f'{index}.png', lambda: None)
    assert len(calls) == 2
    assert (tmp_path / '0.png').read_bytes() == (tmp_path / '1.png').read_bytes()


def test_invalid_image_does_not_poison_shared_url_cache(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/cover.png'] = [(200, b'broken image', {}), (200, png_bytes(), {}), (200, png_bytes(), {})]
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    artwork = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    with pytest.raises(provider_module.ProviderError):
        p.download(artwork, tmp_path / 'broken.png', lambda: None)
    p.download(artwork, tmp_path / 'good.png', lambda: None)
    p.download(artwork, tmp_path / 'cached.png', lambda: None)
    assert len(calls) == 2
    assert not (tmp_path / 'broken.png').exists()
    assert (tmp_path / 'good.png').read_bytes() == (tmp_path / 'cached.png').read_bytes()


def test_cancelled_same_url_waiter_does_not_request_or_block_owner(provider_module, transport, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    routes, calls = transport
    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    waiter_seen = threading.Event()
    def response(request):
        started.set()
        assert release.wait(3)
        return 200, png_bytes(), {}
    routes['/cover.png'] = response
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0, workers=2)
    artwork = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    checkpoints = 0
    def checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints >= 3:
            waiter_seen.set()
        if cancelled.is_set():
            raise provider_module.Cancelled()
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(p.download, artwork, tmp_path / 'owner.png', lambda: None)
        try:
            assert started.wait(2)
            waiter = pool.submit(p.download, artwork, tmp_path / 'cancelled.png', checkpoint)
            assert waiter_seen.wait(2)
            cancelled.set()
            with pytest.raises(provider_module.Cancelled):
                waiter.result(timeout=0.5)
            assert len(calls) == 1
        finally:
            release.set()
        owner.result(timeout=3)
    assert (tmp_path / 'owner.png').exists()
    assert not (tmp_path / 'cancelled.png').exists()


def test_destination_created_while_coalescing_is_preserved(provider_module, transport, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    routes, calls = transport
    started = threading.Event()
    release = threading.Event()
    waiter_seen = threading.Event()
    destination = tmp_path / 'racing.png'
    def response(request):
        started.set()
        assert release.wait(3)
        return 200, png_bytes(), {}
    routes['/cover.png'] = response
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0, workers=2)
    artwork = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    checkpoints = 0
    def checkpoint():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints >= 3:
            waiter_seen.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(p.download, artwork, tmp_path / 'owner.png', lambda: None)
        try:
            assert started.wait(2)
            waiter = pool.submit(p.download, artwork, destination, checkpoint)
            assert waiter_seen.wait(2)
            destination.write_bytes(b'other writer')
        finally:
            release.set()
        owner.result(timeout=3)
        with pytest.raises(FileExistsError):
            waiter.result(timeout=3)
    assert destination.read_bytes() == b'other writer'
    assert len(calls) == 1
    assert not list(tmp_path.glob('.novacore-*.tmp'))


def test_cached_response_still_observes_destination_and_cancellation(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/cover.png'] = 200, png_bytes(), {}
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    artwork = Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro')
    destination = tmp_path / 'existing.png'
    p.download(artwork, destination, lambda: None)
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        p.download(artwork, destination, lambda: None)
    def cancel():
        raise provider_module.Cancelled()
    with pytest.raises(provider_module.Cancelled):
        p.download(artwork, tmp_path / 'cancelled.png', cancel)
    assert len(calls) == 1
    assert destination.read_bytes() == original
    assert not (tmp_path / 'cancelled.png').exists()


def test_image_cache_budget_counts_bytes_across_unequal_images(provider_module, transport, tmp_path, monkeypatch):
    """A larger validated response must evict enough bytes, not just one entry."""
    routes, calls = transport
    raw = png_bytes()
    monkeypatch.setattr(provider_module, 'MAX_IMAGE_CACHE_BYTES', len(raw) * 3)
    for name in ('a', 'b', 'c'):
        routes[f'/{name}.png'] = 200, raw, {}
    # A PNG may contain trailing data. The full verified response still counts
    # against the raw-byte budget rather than its small decoded dimensions.
    routes['/larger.png'] = 200, raw + b'\x00' * len(raw), {}
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    for index, name in enumerate(('a', 'b', 'c', 'larger', 'c', 'b')):
        p.download(Artwork(f'https://thumbnails.libretro.com/{name}.png', 'Libretro'),
            tmp_path / f'{index}.png', lambda: None)
    assert [urlsplit(url).path for url, _ in calls] == [
        '/a.png', '/b.png', '/c.png', '/larger.png', '/b.png']


def test_screenscraper_account_threads_still_limit_distinct_downloads(provider_module, transport, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    routes, calls = transport
    routes['/api2/ssuserInfos.php'] = 200, ss_user(maxthreads='2', maxrequestspermin='1000000000'), {}
    routes['/api2/systemesListe.php'] = 200, {'response': {'systemes': []}}, {}
    p = ss_provider(provider_module, tmp_path, workers=16)
    p.prepare(lambda: None)
    release = threading.Event()
    two_entered = threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    def response(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
        try:
            assert release.wait(3)
            return 200, png_bytes(), {}
        finally:
            with lock:
                active -= 1
    for index in range(4):
        routes[f'/{index}.png'] = response
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(p.download, Artwork(f'https://api.screenscraper.fr/{index}.png', 'ScreenScraper'),
            tmp_path / f'{index}.png', lambda: None) for index in range(4)]
        try:
            assert two_entered.wait(2)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=4)
    assert maximum == 2
    assert len(calls) == 6  # Two account/catalog calls plus four images.


@pytest.mark.parametrize('tag,expected_region', [('U', 'us'), ('E', 'eu'), ('J', 'jp'), ('UE', 'eu'), ('JUE', 'eu')])
def test_libretro_recognizes_goodtools_region_and_good_dump_metadata(provider_module, transport, tmp_path, tag, expected_region):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (USA).png">US</a>'
        '<a href="Sonic the Hedgehog (Europe).png">EU</a>'
        '<a href="Sonic the Hedgehog (Japan).png">JP</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', region='eu', interval=0)
    result = p.find(game(tmp_path, f'Sonic the Hedgehog ({tag}) [!]'), lambda: None)
    assert result is not None
    assert result.region == expected_region


@pytest.mark.parametrize('qualifier', ['Hack', 'Disc 2', 'Rev 1', 'unknown metadata'])
def test_goodtools_metadata_never_erases_content_qualifiers(provider_module, transport, tmp_path, qualifier):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (USA).png">US</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', region='eu', interval=0)
    assert p.find(game(tmp_path, f'Sonic the Hedgehog (U) ({qualifier}) [!]'), lambda: None) is None


def test_libretro_goodtools_combined_regions_do_not_allow_other_regions(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog (Japan).png">JP</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', region='jp', interval=0)
    assert p.find(game(tmp_path, 'Sonic the Hedgehog (UE) [!]'), lambda: None) is None


def test_libretro_ignores_empty_metadata_tags_in_real_catalogue_names(provider_module, transport, tmp_path):
    routes, _ = transport
    routes['/Sega%20-%20Mega%20Drive%20-%20Genesis/Named_Boxarts/'] = (200,
        '<a href="Sonic the Hedgehog () [] (Europe).png">EU</a>', {})
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    result = p.find(game(tmp_path, 'Sonic the Hedgehog (Europe)'), lambda: None)
    assert result is not None and result.region == 'eu'


def test_public_source_access_refusal_avoids_developer_setup_instructions(provider_module, transport, tmp_path):
    routes, calls = transport
    routes['/cover.png'] = 403, b'remote error body', {}
    p = provider_module.LibretroProvider(tmp_path / 'cache', interval=0)
    for index in range(2):
        with pytest.raises(provider_module.AuthenticationError) as error:
            p.download(Artwork('https://thumbnails.libretro.com/cover.png', 'Libretro'),
                tmp_path / f'{index}.png', lambda: None)
        assert 'developer' not in str(error.value).lower()
        assert 'remote error body' not in str(error.value)
    assert len(calls) == 1
