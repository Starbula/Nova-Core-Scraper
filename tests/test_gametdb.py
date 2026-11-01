"""GameTDB public catalogue and read-only disc ID artwork boundary."""
from __future__ import annotations

import importlib
import io
import os
import time
import zipfile
from pathlib import Path

import pytest
import requests

from novacore_scraper.matching import MatchPolicy
from novacore_scraper.models import Game, System
from novacore_scraper.providers import Cancelled, ProviderError


def provider_class():
    spec = importlib.util.find_spec('novacore_scraper.gametdb')
    assert spec is not None, 'GameTDB provider must be implemented'
    return importlib.import_module('novacore_scraper.gametdb').GameTDBProvider


def game(tmp_path, title='Super Mario Galaxy (USA)', system='wii', game_id=None, magic=None, extension='.iso'):
    path = tmp_path / system / (title + extension)
    path.parent.mkdir(exist_ok=True)
    if game_id is not None:
        header = bytearray(32)
        header[:6] = game_id.encode('ascii')
        offset = 0x18 if magic == 'wii' else 0x1c
        header[offset:offset + 4] = bytes.fromhex('5D1C9EA3' if magic == 'wii' else 'C2339F3D')
        path.write_bytes(header)
    return Game(System(system, system, path.parent, frozenset({extension})), path, path.name, title)


def catalogue(records=None, xml=None, member='wiitdb.xml'):
    from xml.sax.saxutils import escape
    if xml is None:
        records = records if records is not None else [('RMGE01', 'Wii', 'NTSC-U', 'Super Mario Galaxy')]
        xml = '<datafile><WiiTDB version="20261101000000"/>'
        for game_id, system, region, title in records:
            xml += '<game name="' + escape(title, {'"': '&quot;'}) + '"><id>' + game_id + '</id><type>' + system + '</type><region>' + region + '</region><locale lang="EN"><title>' + escape(title) + '</title><synopsis>Ignored synopsis</synopsis></locale></game>'
        xml += '</datafile>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(member, xml)
    return output.getvalue()


@pytest.fixture
def transport(monkeypatch):
    replies, calls = [], []
    def send(session, request, **kwargs):
        calls.append(request.url)
        assert replies, 'unexpected network request'
        body = replies.pop(0)
        status, body = body if isinstance(body, tuple) else (200, body)
        response = requests.Response()
        response.status_code, response._content, response.url = status, body, request.url
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    return replies, calls


def make_provider(tmp_path, monkeypatch, **kwargs):
    cls = provider_class()
    monkeypatch.setattr(cls, '_rate', lambda self, checkpoint: checkpoint())
    return cls(tmp_path / 'cache', **kwargs)


def test_verified_disc_id_remains_available_after_catalogue_refusal(tmp_path, monkeypatch, transport):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    statuses = []
    provider.set_status_callback(statuses.append)
    provider.prepare(lambda: None)
    assert provider.find(game(tmp_path, 'Unknown title'), lambda: None) is None
    result = provider.find(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None)
    assert result and result.url == 'https://art.gametdb.com/wii/cover/US/RMGE01.png'
    assert result.provider_game_id == 'RMGE01' and 'disc' in result.match_note.lower()
    assert len(transport[1]) == 1
    assert any('catalogue' in status.lower() and 'unavailable' in status.lower() for status in statuses)
    provider.close()


@pytest.mark.parametrize('system,magic,game_id,extension', [
    ('wii', 'gc', 'GZLE01', '.iso'), ('gamecube', 'wii', 'RMGE01', '.iso'),
    ('wii', 'wii', 'RMGE01', '.wbfs'), ('gamecube', 'gc', 'DTLX01', '.gcm'),
    ('gamecube', 'gc', 'GNHE5d', '.iso'), ('wii', 'wii', '../E01', '.iso'),
])
def test_disc_identity_rejects_other_platform_containers_and_nonunique_ids(tmp_path, monkeypatch, transport, system, magic, game_id, extension):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(game(tmp_path, 'Unidentified', system, game_id, magic, extension), lambda: None) is None
    wrong_platform = (system == 'wii' and magic == 'gc') or (system == 'gamecube' and magic == 'wii')
    assert len(transport[1]) == (0 if wrong_platform else 1)
    provider.close()


def test_catalogue_exact_platform_title_and_regional_id(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue([
        ('RMGE01', 'Wii', 'NTSC-U', 'Super Mario Galaxy'),
        ('RMGP01', 'Wii', 'PAL', 'Super Mario Galaxy'),
        ('GMGE01', 'GameCube', 'NTSC-U', 'Super Mario Galaxy'),
        ('RMGE02', 'Wii', 'NTSC-U', 'Super Mario Galaxy 2'),
        ('RMGE03', 'CUSTOM', 'NTSC-U', 'Super Mario Galaxy'),
    ]))
    provider = make_provider(tmp_path, monkeypatch, region='eu')
    result = provider.find(game(tmp_path), lambda: None)
    assert result and result.url == 'https://art.gametdb.com/wii/cover/US/RMGE01.png'
    assert provider.find(game(tmp_path, 'Super Mario Galaxy (Europe)'), lambda: None).provider_game_id == 'RMGP01'
    assert provider.find(game(tmp_path, 'Super Mario Galaxy (USA)', 'gamecube'), lambda: None).provider_game_id == 'GMGE01'
    assert provider.find(game(tmp_path, 'Super Mario Galaxy 3'), lambda: None) is None
    assert provider.find(game(tmp_path, 'Super Mario Galaxy (Hack)'), lambda: None) is None
    provider.close()
    again = make_provider(tmp_path, monkeypatch)
    assert again.find(game(tmp_path), lambda: None).provider_game_id == 'RMGE01'
    assert len(transport[1]) == 1
    again.close()


def test_duplicate_same_region_titles_do_not_guess_an_id(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue([('RMGE01', 'Wii', 'NTSC-U', 'Same Title'), ('RMGE02', 'Wii', 'NTSC-U', 'Same Title')]))
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(game(tmp_path, 'Same Title (USA)'), lambda: None) is None
    provider.close()


def test_other_region_and_base_cover_are_auditable_and_can_be_disabled(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue([('RMGE01', 'Wii', 'NTSC-U', 'Super Mario Galaxy')]))
    provider = make_provider(tmp_path, monkeypatch)
    variants = list(provider.candidates(game(tmp_path, 'Super Mario Galaxy (Europe) (Rev 1)'), lambda: None))
    result = next(variant for variant in variants if variant.region == 'us')
    assert result.url == 'https://art.gametdb.com/wii/cover/US/RMGE01.png'
    assert 'Other-region' in result.match_note and 'revision' in result.match_note
    provider.close()
    strict = make_provider(tmp_path, monkeypatch, match_policy=MatchPolicy(False, False))
    assert strict.find(game(tmp_path, 'Super Mario Galaxy (Europe)'), lambda: None) is None
    assert strict.find(game(tmp_path, 'Super Mario Galaxy (USA) (Rev 1)'), lambda: None) is None
    strict.close()


def test_unknown_system_does_not_prepare_or_read_network(tmp_path, monkeypatch, transport):
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(game(tmp_path, system='switch'), lambda: None) is None
    assert transport[1] == []
    provider.close()


def test_candidate_regions_stay_on_one_verified_game_and_strict_policy(tmp_path, monkeypatch, transport):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    results = list(provider.candidates(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None))
    assert results[0].region == 'us'
    assert len(results) > 1
    assert all(result.provider_game_id == 'RMGE01' and result.url.endswith('/RMGE01.png') for result in results)
    provider.close()
    strict = make_provider(tmp_path, monkeypatch, match_policy=MatchPolicy(False, True))
    transport[0].append((403, b'refused'))
    results = list(strict.candidates(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None))
    assert [result.region for result in results] == ['us']
    strict.close()


def test_cancelled_refresh_preserves_index_and_cleans_temporary_files(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    provider.find(game(tmp_path), lambda: None)
    provider.close()
    index = next((tmp_path / 'cache').glob('*.sqlite3'))
    before = index.read_bytes()
    os.utime(index, (time.time() - 8 * 86400,) * 2)
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    def cancel():
        if len(transport[1]) > 1:
            raise Cancelled()
    with pytest.raises(Cancelled):
        provider.find(game(tmp_path), cancel)
    assert index.read_bytes() == before
    assert list(index.parent.iterdir()) == [index]
    provider.close()


@pytest.mark.parametrize('payload', [b'not a zip', catalogue(xml='<bad/>'), catalogue(xml='<!DOCTYPE datafile [<!ENTITY x "a">]><datafile/>'), catalogue(member='../wiitdb.xml')])
def test_invalid_catalogue_is_not_published_and_header_recovery_still_works(tmp_path, monkeypatch, transport, payload):
    transport[0].append(payload)
    provider = make_provider(tmp_path, monkeypatch)
    provider.find(game(tmp_path), lambda: None)
    assert not list((tmp_path / 'cache').iterdir())
    assert provider.find(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None)
    provider.close()


def test_trusted_hosts_and_conservative_rate_floor(tmp_path, monkeypatch, transport):
    provider = make_provider(tmp_path, monkeypatch, interval=0)
    assert provider.interval >= .5
    for url in ('https://art.gametdb.com.evil.test/wii/cover/US/RMGE01.png', 'http://art.gametdb.com/wii/cover/US/RMGE01.png'):
        with pytest.raises(ProviderError):
            provider._trusted(url)
    assert not transport[1]
    provider.close()


def test_header_recovery_does_not_hide_a_hack_or_claim_revision_specific_art(tmp_path, monkeypatch, transport):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch, match_policy=MatchPolicy(True, False))
    assert provider.find(game(tmp_path, 'Super Mario Galaxy (USA) (Rev 1)', game_id='RMGE01', magic='wii'), lambda: None) is None
    assert provider.find(game(tmp_path, 'Super Mario Galaxy (USA) (Hack)', game_id='RMGE01', magic='wii'), lambda: None) is None
    provider.close()


def test_cover_candidates_honor_explicit_region_before_disc_region(tmp_path, monkeypatch, transport):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    result = provider.find(game(tmp_path, 'Super Mario Galaxy (Europe)', game_id='RMGE01', magic='wii'), lambda: None)
    assert result and result.url == 'https://art.gametdb.com/wii/cover/EN/RMGE01.png'
    provider.close()


@pytest.mark.parametrize('title', ['Super Mario Galaxy', 'Help Wanted (Demo)'])
def test_verified_other_platform_header_blocks_title_based_catalogue_matching(tmp_path, monkeypatch, transport, title):
    transport[0].append(catalogue([('RMGE01', 'Wii', 'NTSC-U', title)]))
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(game(tmp_path, title + ' (USA)', game_id='GZLE01', magic='gc'), lambda: None) is None
    provider.close()


def test_failed_refresh_uses_previous_complete_catalogue(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    expected = provider.find(game(tmp_path), lambda: None)
    provider.close()
    index = next((tmp_path / 'cache').glob('*.sqlite3'))
    before = index.read_bytes()
    os.utime(index, (time.time() - 8 * 86400,) * 2)
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    statuses = []
    provider.set_status_callback(statuses.append)
    assert provider.find(game(tmp_path), lambda: None) == expected
    assert index.read_bytes() == before and list(index.parent.iterdir()) == [index]
    assert any('cached' in status.lower() for status in statuses)
    provider.close()


@pytest.mark.parametrize('limit', ['MAX_CATALOGUE_BYTES', 'MAX_XML_BYTES', 'MAX_FIELD_CHARS'])
def test_catalogue_bounds_reject_before_publication(tmp_path, monkeypatch, transport, limit):
    cls = provider_class()
    module = importlib.import_module(cls.__module__)
    monkeypatch.setattr(module, limit, 8)
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    provider.find(game(tmp_path), lambda: None)
    assert list((tmp_path / 'cache').iterdir()) == []
    assert provider.find(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None)
    provider.close()


def test_oversized_zip_directory_is_rejected_before_zipfile_allocates_members(tmp_path, monkeypatch, transport):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as handle:
        handle.writestr('wiitdb.xml', '<datafile/>')
        for number in range(32):
            handle.writestr(str(number), b'')
    transport[0].append(raw.getvalue())
    provider = make_provider(tmp_path, monkeypatch)
    provider.find(game(tmp_path), lambda: None)
    assert not list((tmp_path / 'cache').iterdir())
    provider.close()


def test_cached_catalogue_cannot_supply_an_unsafe_game_id(tmp_path, monkeypatch, transport):
    import sqlite3
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    provider.find(game(tmp_path), lambda: None)
    provider.close()
    index = next((tmp_path / 'cache').glob('*.sqlite3'))
    with sqlite3.connect(index) as connection:
        connection.execute("UPDATE games SET game_id='../E01'")
        connection.execute("UPDATE names SET game_id='../E01'")
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(game(tmp_path), lambda: None) is None
    provider.close()


def test_automatic_chain_preparation_does_not_fetch_unused_gametdb_catalogue(tmp_path, monkeypatch, transport):
    from novacore_scraper.sources import FallbackProvider
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    chain = FallbackProvider([provider])
    chain.prepare(lambda: None)
    assert chain.find(game(tmp_path, 'Super Mario Land', system='gb'), lambda: None) is None
    assert transport[1] == []
    chain.close()


def test_verified_header_lookup_does_not_fetch_catalogue(tmp_path, monkeypatch, transport):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    result = provider.find(game(tmp_path, game_id='RMGE01', magic='wii'), lambda: None)
    assert result and result.provider_game_id == 'RMGE01'
    assert transport[1] == []
    provider.close()


def test_title_only_lookup_prepares_catalogue_lazily(tmp_path, monkeypatch, transport):
    transport[0].append(catalogue())
    provider = make_provider(tmp_path, monkeypatch)
    provider.prepare(lambda: None)
    assert transport[1] == []
    assert provider.find(game(tmp_path), lambda: None).provider_game_id == 'RMGE01'
    assert transport[1] == ['https://www.gametdb.com/wiitdb.zip']
    provider.close()


def wbfs_game(tmp_path, *, game_id='RMGE01', system='wii', slot=0, hd_shift=9,
              wbfs_shift=21, file_size=4 * 1024 * 1024, magic='wii'):
    """Sparse, hand-positioned single-disc fixtures; offsets are format literals."""
    result = game(tmp_path, 'Super Mario Galaxy (USA)', system=system, extension='.wbfs')
    hd_size = 1 << hd_shift
    # Verified values from the original WBFS formula, independent of our parser.
    info_size = {(9, 21): 9728, (12, 21): 12288, (16, 21): 65536,
                 (9, 22): 5120}[hd_shift, wbfs_shift]
    header = bytearray(hd_size)
    header[:4] = b'WBFS'
    header[4:8] = (file_size // hd_size).to_bytes(4, 'big')
    header[8], header[9], header[12 + slot] = hd_shift, wbfs_shift, 1
    disc = bytearray(32)
    disc[:6] = game_id.encode('ascii')
    offset = 0x18 if magic == 'wii' else 0x1c
    disc[offset:offset + 4] = bytes.fromhex('5D1C9EA3' if magic == 'wii' else 'C2339F3D')
    with result.path.open('wb') as handle:
        handle.truncate(file_size)
        handle.write(header)
        handle.seek(hd_size + slot * info_size)
        handle.write(disc)
    return result


@pytest.mark.parametrize('slot,hd_shift,wbfs_shift,file_size', [
    (0, 9, 21, 4194304), (3, 9, 21, 4194304), (1, 12, 21, 4194304),
    (0, 16, 21, 4194304), (2, 9, 22, 8388608),
])
def test_single_disc_wbfs_recovers_verified_id_without_catalogue(tmp_path, monkeypatch, transport, slot, hd_shift, wbfs_shift, file_size):
    transport[0].append((403, b'refused'))
    item = wbfs_game(tmp_path, slot=slot, hd_shift=hd_shift, wbfs_shift=wbfs_shift, file_size=file_size)
    before = item.path.stat()
    provider = make_provider(tmp_path, monkeypatch)
    result = provider.find(item, lambda: None)
    assert result and result.provider_game_id == 'RMGE01'
    assert result.url == 'https://art.gametdb.com/wii/cover/US/RMGE01.png'
    assert 'WBFS' in result.match_note and transport[1] == []
    after = item.path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    provider.close()


@pytest.mark.parametrize('mutation', ['magic', 'hd_shift_small', 'hd_shift_large', 'hd_shift_huge',
    'wbfs_shift_small', 'wbfs_shift_huge', 'zero_sector_count', 'size_mismatch',
    'empty_disc_table', 'multiple_discs', 'unknown_slot_flag', 'outside_metadata_slot',
    'truncated_file', 'bad_wii_magic', 'unsafe_id', 'nonunique_id'])
def test_wbfs_invalid_or_ambiguous_metadata_never_supplies_an_id(tmp_path, monkeypatch, transport, mutation):
    transport[0].append((403, b'refused'))
    item = wbfs_game(tmp_path)
    with item.path.open('r+b') as handle:
        if mutation == 'magic':
            handle.write(b'BAD!')
        elif mutation.startswith('hd_shift'):
            handle.seek(8)
            handle.write(bytes([{'hd_shift_small': 8, 'hd_shift_large': 17, 'hd_shift_huge': 255}[mutation]]))
        elif mutation.startswith('wbfs_shift'):
            handle.seek(9)
            handle.write(bytes([14 if mutation.endswith('small') else 255]))
        elif mutation == 'zero_sector_count':
            handle.seek(4)
            handle.write(b'\0' * 4)
        elif mutation == 'size_mismatch':
            handle.seek(4)
            handle.write((8193).to_bytes(4, 'big'))
        elif mutation == 'empty_disc_table':
            handle.seek(12)
            handle.write(b'\0')
        elif mutation == 'multiple_discs':
            handle.seek(13)
            handle.write(b'\1')
        elif mutation == 'unknown_slot_flag':
            handle.seek(12)
            handle.write(b'\2')
        elif mutation == 'outside_metadata_slot':
            handle.seek(12)
            handle.write(b'\0')
            handle.seek(12 + 215)  # Beyond the last full disc-info record in block0.
            handle.write(b'\1')
        elif mutation == 'truncated_file':
            handle.truncate(520)
        elif mutation == 'bad_wii_magic':
            handle.seek(512 + 24)
            handle.write(b'\0' * 4)
        elif mutation == 'unsafe_id':
            handle.seek(512)
            handle.write(b'../E01')
        elif mutation == 'nonunique_id':
            handle.seek(512)
            handle.write(b'DTLX01')
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(item, lambda: None) is None
    provider.close()


@pytest.mark.parametrize('system,magic', [('wii', 'gc'), ('gamecube', 'wii'), ('gamecube', 'gc')])
def test_wbfs_recovery_requires_wii_platform_magic(tmp_path, monkeypatch, transport, system, magic):
    transport[0].append((403, b'refused'))
    provider = make_provider(tmp_path, monkeypatch)
    assert provider.find(wbfs_game(tmp_path, system=system, magic=magic), lambda: None) is None
    provider.close()


def test_wbfs_identification_reads_only_bounded_table_and_disc_header(tmp_path, monkeypatch):
    item = wbfs_game(tmp_path, hd_shift=16)
    original_open = Path.open
    reads = []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, count=-1):
            reads.append(count)
            return self.stream.read(count)
        def seek(self, *args):
            return self.stream.seek(*args)
        def fileno(self):
            return self.stream.fileno()
    def open_read_only(path, mode='r', *args, **kwargs):
        assert mode == 'rb'
        return Reader(original_open(path, mode, *args, **kwargs))
    monkeypatch.setattr(Path, 'open', open_read_only)
    provider = make_provider(tmp_path, monkeypatch)
    result = provider._disc_identity(item, 'Wii', lambda: None)
    assert result and result[0] == 'RMGE01'
    assert sum(reads) <= 65568 and max(reads) <= 65536
    assert all(count > 0 for count in reads)
    provider.close()


def test_wbfs_identification_can_be_cancelled_before_disc_header_read(tmp_path, monkeypatch, transport):
    item = wbfs_game(tmp_path)
    provider = make_provider(tmp_path, monkeypatch)
    count = 0
    def checkpoint():
        nonlocal count
        count += 1
        if count == 4:
            raise Cancelled()
    with pytest.raises(Cancelled):
        provider._disc_identity(item, 'Wii', checkpoint)
    assert transport[1] == []
    provider.close()
