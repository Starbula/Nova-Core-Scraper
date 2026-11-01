"""Official, public LaunchBox metadata and front-box images; no account needed.

The large export is streamed to a bounded temporary ZIP, then SAX streams only
small scalar fields into SQLite. A complete new index atomically replaces the
old one, which remains usable when a refresh fails.
"""
from __future__ import annotations

import zlib
import unicodedata
from datetime import date
from contextlib import closing
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
import xml.sax
import zipfile
from pathlib import Path
from typing import Callable
from xml.sax.expatreader import create_parser
from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes

import requests

from .models import Artwork, Game
from .computer_titles import computer_title_variants
from .providers import CACHE_SECONDS, Cancelled, Checkpoint, ProviderError, _Provider, _region
from .matching import MatchPolicy, TitleInfo, match_note, region_rank, requested_title_variants, title_info

CATALOGUE_URL = 'https://gamesdb.launchbox-app.com/Metadata.zip'
IMAGE_BASE_URL = 'https://images.launchbox-app.com/'
MAX_CATALOGUE_BYTES = 256 * 1024 * 1024
MAX_XML_BYTES = 1024 * 1024 * 1024
MAX_FIELD_CHARS = 4096
_INDEX_VERSION = 3
_FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,199}\.(?:png|jpe?g|webp)', re.IGNORECASE)

# Exact export platform names. Aliases share a platform only when the console is
# the same; adjacent platforms such as GB/GBC and Neo Geo AES/MVS stay separate.
LAUNCHBOX_SYSTEMS = {
    'nes': 'Nintendo Entertainment System', 'famicom': 'Nintendo Entertainment System',
    'fds': 'Nintendo Famicom Disk System', 'snes': 'Super Nintendo Entertainment System',
    'snesmsu1': 'Super Nintendo Entertainment System', 'snes-msu1': 'Super Nintendo Entertainment System',
    'satellaview': 'Nintendo Satellaview',
    'n64': 'Nintendo 64', 'n64dd': 'Nintendo 64DD', 'gb': 'Nintendo Game Boy',
    'gbc': 'Nintendo Game Boy Color', 'gba': 'Nintendo Game Boy Advance',
    'nds': 'Nintendo DS', '3ds': 'Nintendo 3DS', 'n3ds': 'Nintendo 3DS',
    'gamecube': 'Nintendo GameCube', 'wii': 'Nintendo Wii', 'wiiu': 'Nintendo Wii U',
    'switch': 'Nintendo Switch', 'virtualboy': 'Nintendo Virtual Boy', 'pokemini': 'Nintendo Pokemon Mini',
    'megadrive': 'Sega Genesis', 'genesis': 'Sega Genesis', 'megadrive-msu': 'Sega Genesis',
    'mastersystem': 'Sega Master System',
    'gamegear': 'Sega Game Gear', 'sega32x': 'Sega 32X', '32x': 'Sega 32X',
    'segacd': 'Sega CD', 'megacd': 'Sega CD', 'saturn': 'Sega Saturn', 'dreamcast': 'Sega Dreamcast',
    'sg1000': 'Sega SG-1000', 'pico': 'Sega Pico', 'naomi': 'Sega Naomi', 'naomi2': 'Sega Naomi 2',
    'psx': 'Sony Playstation', 'ps1': 'Sony Playstation', 'ps2': 'Sony Playstation 2',
    'ps3': 'Sony Playstation 3', 'ps4': 'Sony Playstation 4', 'ps5': 'Sony Playstation 5',
    'psp': 'Sony PSP', 'psvita': 'Sony Playstation Vita', 'xbox': 'Microsoft Xbox',
    'xbox360': 'Microsoft Xbox 360', 'xboxone': 'Microsoft Xbox One',
    'atari2600': 'Atari 2600', 'atari5200': 'Atari 5200', 'atari7800': 'Atari 7800',
    'lynx': 'Atari Lynx', 'atarilynx': 'Atari Lynx', 'jaguar': 'Atari Jaguar',
    'atarijaguar': 'Atari Jaguar', 'jaguarcd': 'Atari Jaguar CD', 'atari800': 'Atari 800', 'atarist': 'Atari ST',
    'pcengine': 'NEC TurboGrafx-16', 'pcenginecd': 'NEC TurboGrafx-CD',
    'supergrafx': 'PC Engine SuperGrafx', 'pcfx': 'NEC PC-FX', 'pc88': 'NEC PC-8801', 'pc98': 'NEC PC-9801',
    'neogeo': 'SNK Neo Geo AES', 'neogeocd': 'SNK Neo Geo CD',
    'ngp': 'SNK Neo Geo Pocket', 'ngpc': 'SNK Neo Geo Pocket Color',
    'wonderswan': 'WonderSwan', 'wswan': 'WonderSwan',
    'wonderswancolor': 'WonderSwan Color', 'wswanc': 'WonderSwan Color',
    'colecovision': 'ColecoVision', 'intellivision': 'Mattel Intellivision',
    'odyssey2': 'Magnavox Odyssey 2', 'vectrex': 'GCE Vectrex', '3do': '3DO Interactive Multiplayer',
    'amstradcpc': 'Amstrad CPC', 'gx4000': 'Amstrad GX4000', 'c64': 'Commodore 64',
    'amiga500': 'Commodore Amiga', 'amiga1200': 'Commodore Amiga',
    'amiga4000': 'Commodore Amiga', 'amiga': 'Commodore Amiga',
    'amigacd32': 'Commodore Amiga CD32', 'amigacdtv': 'Commodore CDTV',
    'msx1': 'Microsoft MSX', 'msx': 'Microsoft MSX', 'msx2': 'Microsoft MSX2',
    'zxspectrum': 'Sinclair ZX Spectrum', 'zx81': 'Sinclair ZX-81', 'x68000': 'Sharp X68000',
    'x1': 'Sharp X1', 'mame': 'Arcade', 'arcade': 'Arcade', 'fbneo': 'Arcade',
    'cps1': 'Arcade', 'cps2': 'Arcade', 'cps3': 'Arcade', 'cave': 'Arcade',
    'segastv': 'Arcade', 'model2': 'Arcade', 'model3': 'Arcade',
    'atomiswave': 'Sammy Atomiswave', 'scummvm': 'ScummVM', 'dos': 'MS-DOS', 'supervision': 'Watara Supervision',
    'apple2': 'Apple II', 'apple2gs': 'Apple IIGS', 'bbc': 'BBC Microcomputer System',
    'bbcmicro': 'BBC Microcomputer System',
    'cdi': 'Philips CD-i', 'ngage': 'Nokia N-Gage', 'windows': 'Windows',
}


def _publisher_key(value: str) -> str:
    # Publisher corroboration is exact apart from Unicode form, case and
    # whitespace. Do not infer company aliases, remove suffixes or substrings.
    value = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    if value in {'', '-', '?', 'unknown', 'n/a', 'none', 'various', 'not known'}:
        return ''
    return value


def _release_year(release_date: str, release_year: str) -> int | None:
    """Require a complete known year and reject conflicting/malformed fields."""
    years = []
    if release_date:
        if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', release_date):
            try:
                years.append(date.fromisoformat(release_date).year)
            except ValueError:
                return None
        else:
            return None
    if release_year:
        if not re.fullmatch(r'[0-9]{4}', release_year):
            return None
        years.append(int(release_year))
    if not years or not 1 <= years[0] <= 9999 or len(set(years)) != 1:
        return None
    return years[0]


def _safe_filename(value: str) -> bool:
    return bool(_FILENAME.fullmatch(value)) and '..' not in value


def _image_region(value: str) -> str:
    return {'North America': 'us', 'Oceania': 'au', 'The Netherlands': 'nl',
            'Hong Kong': 'hk', 'Sweden': 'se', 'Russia': 'ru', 'Norway': 'no',
            'Finland': 'fi', 'Greece': 'gr', 'South America': 'sa'}.get(value, _region(value))


def _check_archive_directory(path):
    # ZipFile materializes the central directory on opening. Bound it first so
    # thousands of tiny unrelated members cannot consume unbounded RAM.
    with Path(path).open('rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size > MAX_CATALOGUE_BYTES:
            raise ProviderError('The LaunchBox catalogue archive exceeds the size limit.')
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or len(tail) - position < 22:
        raise ProviderError('The LaunchBox catalogue archive has an invalid directory.')
    _, disk, directory_disk, disk_entries, entries, directory_bytes, offset, comment = struct.unpack_from('<4s4H2LH', tail, position)
    if (disk or directory_disk or disk_entries != entries or entries > 32
            or directory_bytes > 64 * 1024 or offset + directory_bytes > size
            or position + 22 + comment != len(tail)):
        raise ProviderError('The LaunchBox catalogue archive exceeds the supported directory limits.')


class _XMLStream:
    """Bound decompressed input and reject DTDs before the XML parser sees them."""
    def __init__(self, stream, checkpoint: Checkpoint):
        self.stream, self.checkpoint = stream, checkpoint
        self.total = 0
        self.tail = b''

    def read(self, size=-1):
        self.checkpoint()
        # SAX requests fixed-size chunks; cap any other caller as well.
        data = self.stream.read(min(size, 64 * 1024) if size >= 0 else 64 * 1024)
        self.total += len(data)
        if self.total > MAX_XML_BYTES:
            raise ProviderError('The LaunchBox catalogue exceeds the size limit.')
        scan = (self.tail + data).lower()
        if b'\x00' in data or b'<!doctype' in scan or b'<!entity' in scan:
            raise ProviderError('The LaunchBox catalogue contains unsupported XML declarations.')
        self.tail = scan[-16:]
        return data

    def close(self):
        pass  # The ZipExtFile is owned by the surrounding context manager.


class _CatalogueHandler(ContentHandler):
    FIELDS = {
        'Game': {'DatabaseID', 'Name', 'Platform', 'Publisher', 'ReleaseDate', 'ReleaseYear'},
        'GameAlternateName': {'DatabaseID', 'AlternateName'},
        'GameImage': {'DatabaseID', 'FileName', 'Type', 'Region'},
    }

    def __init__(self, connection, checkpoint, status):
        super().__init__()
        self.connection, self.checkpoint, self.status = connection, checkpoint, status
        self.depth = 0
        self.kind = self.field = ''
        self.fields = {}
        self.parts = []
        self.length = 0
        self.rows = self.games = 0

    def startElement(self, name, attrs):
        self.depth += 1
        if self.depth == 1 and name != 'LaunchBox':
            raise ProviderError('The LaunchBox catalogue has an unsupported XML format.')
        if self.depth == 2:
            self.kind = name if name in self.FIELDS else ''
            self.fields = {}
        elif self.depth == 3 and self.kind and name in self.FIELDS[self.kind]:
            if name in self.fields:
                raise ProviderError('The LaunchBox catalogue contains duplicate record fields.')
            self.field, self.parts, self.length = name, [], 0
        elif self.depth > 3 and self.field:
            raise ProviderError('The LaunchBox catalogue contains invalid record fields.')

    def characters(self, content):
        if self.field:
            self.length += len(content)
            if self.length > MAX_FIELD_CHARS:
                raise ProviderError('The LaunchBox catalogue contains an oversized record field.')
            self.parts.append(content)

    def endElement(self, name):
        if self.depth == 3 and self.field:
            self.fields[self.field] = ''.join(self.parts).strip()
            self.field = ''
            self.parts = []
        elif self.depth == 2:
            if self.kind:
                self._row()
            self.rows += 1
            if self.rows % 2048 == 0:
                self.checkpoint()
            if self.rows % 50000 == 0:
                self.status(f'LaunchBox: indexing catalogue ({self.rows:,} records).')
            self.kind = ''
            self.fields = {}
        self.depth -= 1

    def _row(self):
        data = self.fields
        database_id = data.get('DatabaseID', '')
        if not database_id.isascii() or not database_id.isdecimal() or not 0 < len(database_id) <= 12:
            return
        if self.kind == 'Game':
            name, platform = data.get('Name', ''), data.get('Platform', '')
            if name and platform:
                self.connection.execute('INSERT INTO games VALUES (?, ?, ?, ?, ?, ?)',
                                        (database_id, platform, name, data.get('Publisher', ''),
                                         data.get('ReleaseDate', ''), data.get('ReleaseYear', '')))
                self._name(database_id, name)
                self.games += 1
        elif self.kind == 'GameAlternateName' and data.get('AlternateName'):
            self._name(database_id, data['AlternateName'])
        elif self.kind == 'GameImage' and data.get('Type') == 'Box - Front':
            filename = data.get('FileName', '')
            if _safe_filename(filename):
                self.connection.execute('INSERT INTO images VALUES (?, ?, ?)',
                                        (database_id, filename, _image_region(data.get('Region', ''))))

    def _name(self, database_id, name):
        info = title_info(name)
        if info.key:
            self.connection.execute('INSERT INTO names VALUES (?, ?, ?)', (database_id, info.key, name))


class LaunchBoxProvider(_Provider):
    name = 'LaunchBox'

    def __init__(self, cache_dir: Path, region: str = 'eu', interval: float = .25, workers: int = 8,
                 match_policy: MatchPolicy | None = None):
        super().__init__(cache_dir, region, interval, workers)
        self.match_policy = match_policy or MatchPolicy()
        self.interval = max(.5, self.interval)
        self._index = self.cache_dir / 'launchbox-v1.sqlite3'
        self._prepare_lock = threading.Lock()
        self._prepared = False
        self._catalogue_refusal = None
        self._status_callback: Callable[[str], None] | None = None

    def set_status_callback(self, callback):
        self._status_callback = callback

    def _status(self, message):
        if self._status_callback:
            self._status_callback(message)

    def _stop(self, kind, message):
        # Metadata and artwork use separate public hosts. A rejected catalogue
        # refresh must not poison an already usable cache or the artwork host.
        # Keep the refusal latched for the catalogue itself for this session.
        if getattr(self._local, 'catalogue_request', False):
            self._catalogue_refusal = (kind, message)
            raise kind(message) from None
        super()._stop(kind, message)

    def _trusted_host(self, host):
        return host in {'gamesdb.launchbox-app.com', 'images.launchbox-app.com'}

    def _connect(self):
        return sqlite3.connect(self._index.resolve().as_uri() + '?mode=ro', uri=True)

    def _usable_index(self):
        try:
            with closing(self._connect()) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != _INDEX_VERSION:
                    return False
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    return False
                connection.execute('SELECT game_id, base, name FROM names LIMIT 1').fetchone()
                connection.execute('SELECT game_id, filename, region FROM images LIMIT 1').fetchone()
                return connection.execute('SELECT name, publisher, release_date, release_year FROM games LIMIT 1').fetchone() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

    def prepare(self, checkpoint: Checkpoint) -> None:
        checkpoint()
        self._check_fatal()
        while not self._prepare_lock.acquire(timeout=.1):
            checkpoint()
        try:
            checkpoint()
            if self._prepared:
                return
            usable = self._usable_index()
            if usable and time.time() - self._index.stat().st_mtime < CACHE_SECONDS:
                self._prepared = True
                return
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._refresh(checkpoint)
            except Cancelled:
                raise
            except (ProviderError, OSError, sqlite3.Error):
                if not usable or self._fatal:
                    raise ProviderError('LaunchBox catalogue preparation failed. Check the connection and try again.') from None
                self._status('LaunchBox refresh failed; using the cached catalogue.')
            self._prepared = True
        finally:
            self._prepare_lock.release()

    def _refresh(self, checkpoint):
        archive_path = index_path = None
        try:
            self._status('LaunchBox: downloading the initial artwork catalogue (about 110 MB).')
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix='.tmp', delete=False) as output:
                archive_path = Path(output.name)
                self._download_catalogue(output, checkpoint)
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix='.tmp', delete=False) as output:
                index_path = Path(output.name)
            self._index_archive(archive_path, index_path, checkpoint)
            checkpoint()
            with index_path.open('r+b') as handle:
                os.fsync(handle.fileno())
            os.replace(index_path, self._index)
        finally:
            if archive_path:
                archive_path.unlink(missing_ok=True)
            if index_path:
                index_path.unlink(missing_ok=True)
                Path(str(index_path) + '-journal').unlink(missing_ok=True)

    def _download_catalogue(self, output, checkpoint):
        if self._catalogue_refusal:
            kind, message = self._catalogue_refusal
            raise kind(message) from None
        self._local.catalogue_request = True
        try:
            response = self._request(CATALOGUE_URL, checkpoint)
        finally:
            self._local.catalogue_request = False
        try:
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_CATALOGUE_BYTES:
                raise ProviderError('The LaunchBox catalogue exceeds the size limit.')
            total = reported = 0
            for chunk in response.iter_content(64 * 1024):
                checkpoint()
                self._check_fatal()
                total += len(chunk)
                if total > MAX_CATALOGUE_BYTES:
                    raise ProviderError('The LaunchBox catalogue exceeds the size limit.')
                self._throttle_bytes(len(chunk), checkpoint)
                output.write(chunk)
                if total - reported >= 4 * 1024 * 1024:
                    self._status(f'LaunchBox: downloading catalogue ({total // (1024 * 1024)} MB).')
                    reported = total
            output.flush()
        except (requests.RequestException, ValueError):
            raise ProviderError('The LaunchBox catalogue download was interrupted or invalid.') from None
        finally:
            response.close()
            slot = getattr(response, '_novacore_slot', None)
            if slot is not None:
                del response._novacore_slot
                slot.release()

    def _index_archive(self, archive_path, index_path, checkpoint):
        self._status('LaunchBox: indexing the artwork catalogue. This may take a moment.')
        connection = sqlite3.connect(index_path)
        try:
            connection.execute('PRAGMA journal_mode=OFF')
            connection.execute('PRAGMA synchronous=OFF')
            connection.execute('PRAGMA temp_store=FILE')
            connection.executescript('CREATE TABLE games (game_id INTEGER PRIMARY KEY, platform TEXT NOT NULL, name TEXT NOT NULL, '
                                     'publisher TEXT NOT NULL, release_date TEXT NOT NULL, release_year TEXT NOT NULL);'
                                     'CREATE TABLE names (game_id INTEGER, base TEXT, name TEXT);'
                                     'CREATE TABLE images (game_id INTEGER, filename TEXT, region TEXT);')
            _check_archive_directory(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                metadata = [member for member in members if member.filename == 'Metadata.xml']
                if len(members) > 32 or len(metadata) != 1:
                    raise ProviderError('The LaunchBox catalogue archive has an unsupported format.')
                member = metadata[0]
                if (member.file_size > MAX_XML_BYTES or member.flag_bits & 1
                        or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ProviderError('The LaunchBox catalogue archive exceeds the supported limits.')
                parser = create_parser()
                parser.setFeature(feature_external_ges, False)
                parser.setFeature(feature_external_pes, False)
                handler = _CatalogueHandler(connection, checkpoint, self._status)
                parser.setContentHandler(handler)
                with archive.open(member) as stream:
                    parser.parse(_XMLStream(stream, checkpoint))
                if not handler.games:
                    raise ProviderError('The LaunchBox catalogue contains no game records.')
            checkpoint()
            # SQLite's progress hook also permits cancellation while building indexes.
            cancelled = []
            def progress():
                try:
                    checkpoint()
                except Cancelled as error:
                    cancelled.append(error)
                    return 1
                return 0
            connection.set_progress_handler(progress, 10000)
            try:
                connection.executescript('CREATE INDEX names_lookup ON names (base, game_id);'
                                         'CREATE INDEX images_lookup ON images (game_id);'
                                         'CREATE INDEX games_platform ON games (platform, game_id);')
            except sqlite3.Error:
                if cancelled:
                    raise cancelled[0]
                raise
            connection.execute(f'PRAGMA user_version={_INDEX_VERSION}')
            connection.commit()
            checkpoint()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, xml.sax.SAXException, RuntimeError, EOFError, zlib.error):
            raise ProviderError('LaunchBox returned an invalid artwork catalogue.') from None
        finally:
            connection.close()

    def _title_matches(self, connection, platform, requested, publisher=None, year=None, note=''):
        candidates = connection.execute(
            'SELECT DISTINCT games.game_id, games.name, names.name, games.publisher, '
            'games.release_date, games.release_year FROM names JOIN games USING (game_id) '
            'WHERE games.platform=? AND names.base=?', (platform, requested.key)).fetchall()
        matches = {}
        for game_id, canonical_name, alias, official_publisher, release_date, release_year in candidates:
            canonical, alternate = title_info(canonical_name), title_info(alias)
            # A verified alias cannot erase a hack, prototype, edition, or
            # other meaningful qualifier on its canonical game.
            if canonical.key != requested.key and canonical.qualifiers != requested.qualifiers:
                continue
            if publisher is not None and (
                    _publisher_key(official_publisher) != publisher
                    or _release_year(release_date, release_year) != year):
                continue
            if not self.match_policy.allow_base_cover and (
                    canonical.packaging != requested.packaging or alternate.packaging != requested.packaging):
                continue
            choice = (alternate.packaging != requested.packaging, alias, canonical, requested, note)
            if game_id not in matches or choice[:2] < matches[game_id][:2]:
                matches[game_id] = choice
        return matches

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        return next(self.candidates(game, checkpoint), None)

    def candidates(self, game: Game, checkpoint: Checkpoint):
        checkpoint()
        platform = LAUNCHBOX_SYSTEMS.get(game.system.name.casefold())
        if platform is None and game.system.fullname in LAUNCHBOX_SYSTEMS.values():
            platform = game.system.fullname
        if platform is None:
            self._unmatched('unsupported_platform', 'LaunchBox has no verified platform mapping for this system.')
            return None
        self.prepare(checkpoint)
        variants = requested_title_variants(game)
        requested = variants[0].info
        try:
            with closing(self._connect()) as connection:
                matches = self._title_matches(connection, platform, requested)
                if not matches:
                    # Publisher-preserving TOSEC and selected-system labels
                    # keep the normal exact title/alias uniqueness checks.
                    for alternate in variants[1:]:
                        for game_id, choice in self._title_matches(
                                connection, platform, alternate.info, note=alternate.note).items():
                            if game_id not in matches or choice[:2] < matches[game_id][:2]:
                                matches[game_id] = choice
                if not matches:
                    # TOSEC's year and publisher may be omitted from the lookup
                    # only when the official record corroborates both. The
                    # parser preserves every meaningful/unknown modifier.
                    for variant in computer_title_variants(game):
                        # The helper validates the complete TOSEC date. Its
                        # month/day may be unknown; the year must be known.
                        year = _release_year('', variant.date.split('-', 1)[0])
                        publisher = _publisher_key(variant.publisher)
                        if year is None or not publisher or not variant.base_title:
                            continue
                        info = title_info(variant.base_title)
                        effective = TitleInfo(info.key, variants[-1].info.regions,
                                              info.qualifiers, variants[-1].info.packaging or info.packaging, info.title)
                        note = ('TOSEC title confirmed by official publisher and release year '
                                + str(year) + '. ' + variant.note).strip()
                        for game_id, choice in self._title_matches(
                                connection, platform, effective, publisher, year, note).items():
                            if game_id not in matches or choice[:2] < matches[game_id][:2]:
                                matches[game_id] = choice
                # Identity is settled before inspecting image availability or region.
                if len(matches) != 1:
                    self._unmatched('ambiguous_title' if matches else 'no_title_match',
                                    'Several LaunchBox game IDs share this title; review is required.' if matches
                                    else 'No exact title or verified alias matches this LaunchBox platform.')
                    return None
                game_id, (_, alias, candidate, requested, identity_note) = next(iter(matches.items()))
                rows = connection.execute('SELECT filename, region FROM images WHERE game_id=? ORDER BY filename', (game_id,)).fetchall()
        except sqlite3.Error:
            raise ProviderError('The cached LaunchBox catalogue could not be read. Try again.') from None
        checkpoint()
        ranked = []
        for filename, region in rows:
            rank = region_rank(region, requested.regions, self.region, self.match_policy)
            if rank is not None:
                ranked.append((rank, filename, region))
        if not ranked:
            self._unmatched('no_front_cover', 'The exact LaunchBox game has no front cover allowed by the region policy.')
            return None
        # Multiple covers are safe variants once the canonical game ID is unique.
        seen = set()
        for _, filename, region in sorted(ranked):
            checkpoint()
            if filename in seen:
                continue
            seen.add(filename)
            if not _safe_filename(filename):
                raise ProviderError('The cached LaunchBox catalogue contains an unsafe artwork filename.')
            yield Artwork(IMAGE_BASE_URL + filename.replace(' ', '%20'), self.name, region,
                          match_note=' '.join(filter(None, (identity_note, match_note(requested, candidate, region)))), matched_title=alias,
                          provider_game_id=str(game_id))
