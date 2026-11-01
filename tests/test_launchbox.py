"""LaunchBox's public XML boundary, catalogue cache, and exact cover matching."""
from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests

from novacore_scraper.models import Game, System
from novacore_scraper.matching import MatchPolicy
from novacore_scraper.providers import Cancelled, ProviderError
from novacore_scraper.launchbox import LaunchBoxProvider


def game(tmp_path, title='Super Mario Land (Europe)', system='gb'):
    path = tmp_path / system / (title + '.zip')
    return Game(System(system, system, path.parent, frozenset({'.zip'})), path, path.name, title)


def catalogue(games=None, aliases=None, images=None):
    games = games if games is not None else [('1', 'Super Mario Land', 'Nintendo Game Boy')]
    images = images if images is not None else [('1', 'eu.jpg', 'Box - Front', 'Europe'), ('1', 'us.png', 'Box - Front', 'North America')]
    from xml.sax.saxutils import escape
    def row(tag, fields):
        return '<' + tag + '>' + ''.join('<' + key + '>' + escape(value) + '</' + key + '>' for key, value in fields) + '</' + tag + '>'
    xml = '<LaunchBox>' + ''.join(row('Game', zip(('DatabaseID', 'Name', 'Platform'), item)) for item in games)
    xml += ''.join(row('GameAlternateName', zip(('DatabaseID', 'AlternateName'), item)) for item in (aliases or []))
    xml += ''.join(row('GameImage', zip(('DatabaseID', 'FileName', 'Type', 'Region'), item)) for item in images) + '</LaunchBox>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as handle:
        handle.writestr('Metadata.xml', xml)
    return output.getvalue()


@pytest.fixture
def transport(monkeypatch):
    replies, calls = [], []
    def send(session, request, **kwargs):
        calls.append(request.url)
        assert replies, 'unexpected network request'
        body = replies.pop(0)
        if isinstance(body, Exception):
            raise body
        response = requests.Response()
        if isinstance(body, tuple):
            status, body = body
        else:
            status = 200
        response.status_code = status
        response._content = body
        response._content_consumed = True
        response.url = request.url
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    monkeypatch.setattr(LaunchBoxProvider, '_rate', lambda self, checkpoint: checkpoint())
    return replies, calls


def test_exact_platform_region_front_box_and_reusable_cache(tmp_path, transport):
    replies, calls = transport
    replies.append(catalogue(games=[('1', 'Super Mario Land', 'Nintendo Game Boy'), ('2', 'Super Mario Land', 'Nintendo Game Boy Color')], images=[('1', 'back.png', 'Box - Back', 'Europe'), ('2', 'wrong.png', 'Box - Front', 'Europe'), ('1', 'us.jpg', 'Box - Front', 'North America'), ('1', 'eu.jpg', 'Box - Front', 'Europe')]))
    p = LaunchBoxProvider(tmp_path / 'cache', region='us')
    messages = []
    p.set_status_callback(messages.append)
    p.prepare(lambda: None)
    result = p.find(game(tmp_path), lambda: None)
    assert result and result.source == 'LaunchBox' and result.region == 'eu'
    assert result.url == 'https://images.launchbox-app.com/eu.jpg'
    assert p.find(game(tmp_path, 'Super Mario Land'), lambda: None).region == 'us'
    p.close()
    other = LaunchBoxProvider(tmp_path / 'cache')
    assert other.find(game(tmp_path), lambda: None) == result
    other.close()
    assert len(calls) == 1
    assert any('download' in message.lower() for message in messages)
    assert any('index' in message.lower() for message in messages)


def test_verified_alias_and_conservative_qualifiers(tmp_path, transport):
    transport[0].append(catalogue(aliases=[('1', 'Mario Land')]))
    p = LaunchBoxProvider(tmp_path / 'cache', match_policy=MatchPolicy(False, False))
    assert p.find(game(tmp_path, 'Mario Land (Europe)'), lambda: None)
    for title in ('Super Mario Land 2 (Europe)', 'Super Mario Land (Europe) (Hack)', 'Mario Land (USA) (Rev 2)'):
        assert p.find(game(tmp_path, title), lambda: None) is None
    assert p.find(game(tmp_path, system='unusual'), lambda: None) is None
    p.close()


def test_duplicate_game_ids_and_cross_region_never_guess(tmp_path, transport):
    transport[0].append(catalogue(games=[('1', 'Super Mario Land', 'Nintendo Game Boy'), ('2', 'Super Mario Land', 'Nintendo Game Boy')], images=[('1', 'eu.jpg', 'Box - Front', 'Europe'), ('2', 'us.jpg', 'Box - Front', 'North America')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path), lambda: None) is None
    assert p.find(game(tmp_path, 'Super Mario Land'), lambda: None) is None
    p.close()


@pytest.mark.parametrize('filename', ['../evil.jpg', 'https://evil.example/x.png', 'folder/a.jpg', r'folder\a.jpg', '%2e%2e.png', 'cover.jpg?x=1', 'cover.svg'])
def test_unsafe_image_filename_is_never_used(tmp_path, transport, filename):
    transport[0].append(catalogue(images=[('1', filename, 'Box - Front', 'Europe')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path), lambda: None) is None
    p.close()


@pytest.mark.parametrize('body', [b'not a zip', b'PKbroken'])
def test_invalid_catalogue_is_a_sanitized_provider_failure(tmp_path, transport, body):
    transport[0].append(body)
    p = LaunchBoxProvider(tmp_path / 'cache')
    with pytest.raises(ProviderError) as error:
        p.prepare(lambda: None)
    assert 'https://' not in str(error.value)
    assert not list((tmp_path / 'cache').glob('*.tmp'))
    p.close()


def test_wrong_zip_member_and_invalid_xml_are_rejected(tmp_path, transport):
    for member, body in (('../Metadata.xml', '<LaunchBox/>'), ('Metadata.xml', '<broken>'), ('Metadata.xml', '<!DOCTYPE LaunchBox [<!ENTITY x "bad">]><LaunchBox><Game><Name>&x;</Name></Game></LaunchBox>')):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as handle:
            handle.writestr(member, body)
        transport[0].append(raw.getvalue())
        p = LaunchBoxProvider(tmp_path / ('cache' + str(len(transport[1]))))
        with pytest.raises(ProviderError):
            p.prepare(lambda: None)
        p.close()


def test_cancelled_refresh_preserves_previous_index_and_cleans_temporary_files(tmp_path, transport):
    replies, calls = transport
    replies.append(catalogue())
    cache = tmp_path / 'cache'
    p = LaunchBoxProvider(cache)
    p.prepare(lambda: None)
    expected = p.find(game(tmp_path), lambda: None)
    p.close()
    index = next(cache.glob('*.sqlite3'))
    old = index.read_bytes()
    os.utime(index, (time.time() - 8 * 86400,) * 2)
    replies.append(catalogue())
    p = LaunchBoxProvider(cache)
    def cancel():
        if len(calls) > 1:
            raise Cancelled()
    with pytest.raises(Cancelled):
        p.prepare(cancel)
    assert index.read_bytes() == old
    assert list(cache.iterdir()) == [index]
    p.close()
    # A failed refresh also leaves the previous valid catalogue available.
    replies.append(b'invalid zip')
    p = LaunchBoxProvider(cache)
    messages = []
    p.set_status_callback(messages.append)
    assert p.find(game(tmp_path), lambda: None) == expected
    assert any('cached' in message.lower() for message in messages)
    assert index.read_bytes() == old
    p.close()


def test_provider_floor_and_untrusted_download_host(tmp_path, transport):
    p = LaunchBoxProvider(tmp_path / 'cache', interval=0, workers=8)
    assert p.interval >= .5
    with pytest.raises(ProviderError):
        p._trusted('https://images.launchbox-app.com.evil.example/x.png')
    assert not transport[1]
    p.close()


def test_unqualified_alias_cannot_hide_a_hack_or_other_edition(tmp_path, transport):
    transport[0].append(catalogue(games=[('1', 'Super Mario Land (Hack)', 'Nintendo Game Boy')],
                                  aliases=[('1', 'Super Mario Land')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path), lambda: None) is None
    assert p.find(game(tmp_path, 'Super Mario Land (Europe) (Hack)'), lambda: None)
    p.close()


def test_explicit_region_does_not_choose_an_unrelated_regional_cover(tmp_path, transport):
    transport[0].append(catalogue(images=[('1', 'us.jpg', 'Box - Front', 'North America')]))
    p = LaunchBoxProvider(tmp_path / 'cache', match_policy=MatchPolicy(False, False))
    assert p.find(game(tmp_path), lambda: None) is None
    assert p.find(game(tmp_path, 'Super Mario Land (USA)'), lambda: None).region == 'us'
    p.close()


def test_world_cover_can_serve_an_explicit_region(tmp_path, transport):
    transport[0].append(catalogue(images=[('1', 'world.jpg', 'Box - Front', 'World')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path), lambda: None).region == 'wor'
    p.close()


def test_archive_and_xml_limits_are_enforced_without_partial_index(tmp_path, transport, monkeypatch):
    import novacore_scraper.launchbox as module
    raw = catalogue()
    monkeypatch.setattr(module, 'MAX_CATALOGUE_BYTES', len(raw) - 1)
    transport[0].append(raw)
    p = LaunchBoxProvider(tmp_path / 'cache')
    with pytest.raises(ProviderError):
        p.prepare(lambda: None)
    assert list((tmp_path / 'cache').iterdir()) == []
    monkeypatch.setattr(module, 'MAX_CATALOGUE_BYTES', len(raw) + 1)
    monkeypatch.setattr(module, 'MAX_XML_BYTES', 32)
    transport[0].append(raw)
    with pytest.raises(ProviderError):
        p.prepare(lambda: None)
    assert list((tmp_path / 'cache').iterdir()) == []
    p.close()


def test_xml_indexing_can_be_cancelled_without_publishing_partial_catalogue(tmp_path, transport):
    transport[0].append(catalogue(games=[(str(index), 'Name ' + str(index), 'Nintendo Game Boy') for index in range(1, 8000)]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    indexing = False
    checks = 0
    def status(message):
        nonlocal indexing
        indexing |= 'indexing' in message
    def checkpoint():
        nonlocal checks
        if indexing:
            checks += 1
            if checks > 8:
                raise Cancelled()
    p.set_status_callback(status)
    with pytest.raises(Cancelled):
        p.prepare(checkpoint)
    assert checks > 8
    assert list((tmp_path / 'cache').iterdir()) == []
    p.close()


def test_refresh_replaces_index_only_after_new_catalogue_is_complete(tmp_path, transport):
    transport[0].append(catalogue())
    cache = tmp_path / 'cache'
    p = LaunchBoxProvider(cache)
    p.prepare(lambda: None)
    p.close()
    index = next(cache.glob('*.sqlite3'))
    os.utime(index, (time.time() - 8 * 86400,) * 2)
    transport[0].append(catalogue(games=[('1', 'New Game', 'Nintendo Game Boy')]))
    p = LaunchBoxProvider(cache)
    assert p.find(game(tmp_path, 'New Game (Europe)'), lambda: None)
    assert p.find(game(tmp_path), lambda: None) is None
    assert list(cache.iterdir()) == [index]
    p.close()


def test_utf16_dtd_and_oversized_scalar_fields_are_rejected(tmp_path, transport):
    bodies = [
        '<!DOCTYPE LaunchBox [<!ENTITY x "bad">]><LaunchBox><Game><Name>&x;</Name></Game></LaunchBox>'.encode('utf-16'),
        ('<LaunchBox><Game><DatabaseID>1</DatabaseID><Name>' + 'x' * 5000 + '</Name><Platform>Nintendo Game Boy</Platform></Game></LaunchBox>').encode(),
    ]
    for number, body in enumerate(bodies):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as handle:
            handle.writestr('Metadata.xml', body)
        transport[0].append(raw.getvalue())
        p = LaunchBoxProvider(tmp_path / str(number))
        with pytest.raises(ProviderError):
            p.prepare(lambda: None)
        p.close()


def test_zip_directory_is_bounded_before_opening_and_other_members_are_not_extracted(tmp_path, transport):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as handle:
        handle.writestr('Metadata.xml', '<LaunchBox/>')
        for number in range(32):
            handle.writestr(str(number), b'')
    transport[0].append(raw.getvalue())
    p = LaunchBoxProvider(tmp_path / 'cache')
    with pytest.raises(ProviderError):
        p.prepare(lambda: None)
    assert list((tmp_path / 'cache').iterdir()) == []
    p.close()
    # A non-selected traversal name is never extracted or opened.
    with zipfile.ZipFile(io.BytesIO(catalogue())) as original:
        xml = original.read('Metadata.xml')
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as handle:
        handle.writestr('Metadata.xml', xml)
        handle.writestr('../../evil.txt', b'evil')
    transport[0].append(raw.getvalue())
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path), lambda: None)
    assert not (tmp_path / 'evil.txt').exists()
    assert len(list((tmp_path / 'cache').iterdir())) == 1
    p.close()


@pytest.mark.parametrize('status', [403, 430, 431])
def test_stale_cache_survives_catalogue_only_refusal_but_image_refusal_stays_fatal(tmp_path, transport, status):
    from novacore_scraper.models import Artwork
    from novacore_scraper.providers import AuthenticationError, QuotaError
    from PIL import Image
    replies, calls = transport
    replies.append(catalogue())
    cache = tmp_path / 'cache'
    p = LaunchBoxProvider(cache)
    expected = p.find(game(tmp_path), lambda: None)
    p.close()
    index = next(cache.glob('*.sqlite3'))
    old = index.read_bytes()
    os.utime(index, (time.time() - 8 * 86400,) * 2)
    replies.append((status, b'refused catalogue'))
    p = LaunchBoxProvider(cache)
    messages = []
    p.set_status_callback(messages.append)
    p.prepare(lambda: None)
    assert any('cached' in message.lower() for message in messages)
    assert p.find(game(tmp_path), lambda: None) == expected
    p.prepare(lambda: None)
    # Only the rejected catalogue endpoint is suppressed; legitimately available
    # cached metadata can still direct requests to the separate public image host.
    raw = io.BytesIO()
    Image.new('RGB', (3, 4)).save(raw, 'PNG')
    replies.append(raw.getvalue())
    p.download(expected, tmp_path / 'cached-cover.png', lambda: None)
    assert (tmp_path / 'cached-cover.png').exists()
    assert index.read_bytes() == old
    assert sum(url.endswith('/Metadata.zip') for url in calls) == 2
    # A subsequent image-host refusal retains the shared provider's fatal latch.
    replies.append((status, b'refused image'))
    denied = Artwork('https://images.launchbox-app.com/denied.png', 'LaunchBox')
    error_type = AuthenticationError if status == 403 else QuotaError
    with pytest.raises(error_type):
        p.download(denied, tmp_path / 'denied.png', lambda: None)
    request_count = len(calls)
    with pytest.raises(error_type):
        p.download(denied, tmp_path / 'denied-again.png', lambda: None)
    assert len(calls) == request_count
    assert not (tmp_path / 'denied.png').exists()
    p.close()


def test_refused_catalogue_is_not_retried_within_a_session_without_a_cache(tmp_path, transport):
    transport[0].append((403, b'refused'))
    p = LaunchBoxProvider(tmp_path / 'cache')
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.prepare(lambda: None)
    assert len(transport[1]) == 1
    assert list((tmp_path / 'cache').iterdir()) == []
    p.close()


def test_normalized_alias_revision_and_region_fallback_is_annotated(tmp_path, transport):
    transport[0].append(catalogue(games=[('1', 'Pokémon - Red Version', 'Nintendo Game Boy')],
        aliases=[('1', 'PocketMonstersRed')], images=[('1', 'us.jpg', 'Box - Front', 'North America')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    result = p.find(game(tmp_path, 'PokemonRedVersion (Europe) (Rev 1)'), lambda: None)
    assert result and result.region == 'us' and result.provider_game_id == '1'
    assert 'base' in result.match_note.lower() and 'region' in result.match_note.lower()
    assert p.find(game(tmp_path, 'Pocket Monsters Red (Europe)'), lambda: None)
    p.close()


def test_normalization_does_not_hide_duplicate_canonical_ids(tmp_path, transport):
    transport[0].append(catalogue(games=[('1', 'Pokemon - Red Version', 'Nintendo Game Boy'),
        ('2', 'PokémonRedVersion', 'Nintendo Game Boy')],
        images=[('1', 'eu.jpg', 'Box - Front', 'Europe')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path, 'Pokemon Red Version (Europe)'), lambda: None) is None
    p.close()


def test_parenthetical_title_content_matches_only_equivalent_full_text(tmp_path, transport):
    transport[0].append(catalogue(games=[('1', 'Adventure (Episode II)', 'Nintendo Game Boy')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    assert p.find(game(tmp_path, 'Adventure Episode II (Europe)'), lambda: None)
    assert p.find(game(tmp_path, 'Adventure (Europe)'), lambda: None) is None
    p.close()


def test_launchbox_candidates_are_lazy_ranked_and_keep_unique_identity(tmp_path, transport):
    replies, calls = transport
    replies.append(catalogue(images=[('1', 'eu-b.jpg', 'Box - Front', 'Europe'),
        ('1', 'eu-a.jpg', 'Box - Front', 'Europe'), ('1', 'us.jpg', 'Box - Front', 'North America')]))
    p = LaunchBoxProvider(tmp_path / 'cache')
    item = game(tmp_path)
    candidates = p.candidates(item, lambda: None)
    assert calls == []
    results = list(candidates)
    assert [result.url.rsplit('/', 1)[1] for result in results] == ['eu-a.jpg', 'eu-b.jpg', 'us.jpg']
    assert {result.provider_game_id for result in results} == {'1'}
    assert p.find(item, lambda: None) == results[0]
    strict = LaunchBoxProvider(tmp_path / 'cache', match_policy=MatchPolicy(False, False))
    assert [result.region for result in strict.candidates(item, lambda: None)] == ['eu', 'eu']
    strict.close()
    p.close()


def test_engine_tries_another_launchbox_cover_after_404(tmp_path, transport):
    from PIL import Image
    from novacore_scraper.engine import ScrapeEngine
    replies, calls = transport
    replies.append(catalogue(games=[('1', 'Super Mario Land', 'Nintendo Game Boy'),
        ('2', 'Super Mario Land 2', 'Nintendo Game Boy')],
        images=[('1', 'eu.jpg', 'Box - Front', 'Europe'), ('1', 'us.png', 'Box - Front', 'North America'),
                ('2', 'wrong.png', 'Box - Front', 'Europe')]))
    raw = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(raw, 'PNG')
    replies.extend([(404, b'missing image'), raw.getvalue()])
    item = game(tmp_path)
    item.path.parent.mkdir(parents=True)
    item.path.write_bytes(b'ROM unchanged')
    p = LaunchBoxProvider(tmp_path / 'cache')
    engine = ScrapeEngine(tmp_path, p, process_running=lambda: True, retry_passes=0, keep_awake=False)
    engine.start([item])
    assert engine.wait(5)
    assert (engine.snapshot().completed, engine.snapshot().missing, engine.snapshot().failed) == (1, 0, 0)
    job = engine.jobs_page()[0][0]
    assert job.region == 'us' and 'region' in job.match_note.lower() and Path(job.artwork).is_file()
    assert calls[-2:] == ['https://images.launchbox-app.com/eu.jpg', 'https://images.launchbox-app.com/us.png']
