"""Public GameTDB Wii/GameCube front covers, with verified disc-ID recovery.

GameTDB permits software use (https://www.gametdb.com/Main/FAQ). The catalogue
schema and cover endpoint follow current USB Loader GX and Dolphin clients.
Only catalogue IDs or verified ISO/GCM/single-disc WBFS headers identify games;
filenames never supply an ID. ROM contents are never changed or uploaded.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
import xml.sax
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from xml.sax.expatreader import create_parser
from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes

import requests

from .matching import MatchPolicy, match_note, region_rank, requested_info, title_info
from .models import Artwork, Game
from .providers import CACHE_SECONDS, Cancelled, Checkpoint, ProviderError, _Provider

CATALOGUE_URL = 'https://www.gametdb.com/wiitdb.zip'
IMAGE_BASE_URL = 'https://art.gametdb.com/wii/cover/'
GAMETDB_SYSTEMS = {'wii': 'Wii', 'gamecube': 'GameCube'}
MAX_CATALOGUE_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 128 * 1024 * 1024
MAX_FIELD_CHARS = 4096
_ID = re.compile(r'[A-Z0-9]{6}')
# Dolphin's GameTDB image-region mapping; this is a documented URL template,
# never a guessed title/ID. A missing image is handled by the provider chain.
_COVERS = {'us': 'US', 'eu': 'EN', 'jp': 'JA', 'kr': 'KO', 'de': 'DE',
           'fr': 'FR', 'es': 'ES', 'it': 'IT', 'nl': 'NL', 'tw': 'ZH'}
_REGIONS = {'NTSC-U': 'us', 'NTSC-J': 'jp', 'NTSC-K': 'kr', 'PAL': 'eu'}


class _WrongDiscPlatform(Exception):
    """A real disc header contradicts the selected console."""



def _metadata(values):
    return json.dumps(values, ensure_ascii=False, separators=(',', ':'))


class _XMLStream:
    def __init__(self, stream, checkpoint):
        self.stream, self.checkpoint, self.total, self.tail = stream, checkpoint, 0, b''

    def read(self, size=-1):
        self.checkpoint()
        data = self.stream.read(min(size, 65536) if size >= 0 else 65536)
        self.total += len(data)
        if self.total > MAX_XML_BYTES:
            raise ProviderError('The GameTDB catalogue exceeds the size limit.')
        scan = (self.tail + data).lower()
        if b'\x00' in data or b'<!doctype' in scan or b'<!entity' in scan:
            raise ProviderError('The GameTDB catalogue contains unsupported XML declarations.')
        self.tail = scan[-16:]
        return data

    def close(self):
        pass  # Owned by the enclosing ZipExtFile context.


class _CatalogueHandler(ContentHandler):
    def __init__(self, connection, checkpoint):
        super().__init__()
        self.connection, self.checkpoint = connection, checkpoint
        self.depth, self.games = 0, 0
        self.fields, self.names, self.parts = {}, [], []
        self.field, self.length = '', 0

    def startElement(self, name, attrs):
        self.depth += 1
        if self.depth == 1 and name != 'datafile':
            raise ProviderError('The GameTDB catalogue has an unsupported XML format.')
        if self.depth == 2 and name == 'game':
            self.fields = {'name': attrs.get('name', '')}
            if len(self.fields['name']) > MAX_FIELD_CHARS:
                raise ProviderError('The GameTDB catalogue has an oversized title.')
            self.names = []
        elif self.depth == 3 and self.fields and name in {'id', 'type', 'region'}:
            if name in self.fields:
                raise ProviderError('The GameTDB catalogue has duplicate identity fields.')
            self.field, self.parts, self.length = name, [], 0
        elif self.depth == 4 and self.fields and name == 'title':
            self.field, self.parts, self.length = name, [], 0
        elif self.field:
            raise ProviderError('The GameTDB catalogue has nested identity fields.')

    def characters(self, content):
        if self.field:
            self.length += len(content)
            if self.length > MAX_FIELD_CHARS:
                raise ProviderError('The GameTDB catalogue has an oversized field.')
            self.parts.append(content)

    def endElement(self, name):
        if self.field and name == self.field:
            value = ''.join(self.parts).strip()
            if self.field == 'title':
                if len(self.names) >= 64:
                    raise ProviderError('The GameTDB catalogue has too many localized titles.')
                if value:
                    self.names.append(value)
            else:
                self.fields[self.field] = value
            self.field, self.parts = '', []
        elif self.depth == 2 and name == 'game':
            self._row()
            self.fields, self.names = {}, []
            self.checkpoint()
        self.depth -= 1

    def _row(self):
        game_id = self.fields.get('id', '')
        system = self.fields.get('type') or 'Wii'
        if not _ID.fullmatch(game_id) or game_id in {'DTLX01', 'GNHE5D'} or system not in {'Wii', 'GameCube'}:
            return
        region = _REGIONS.get(self.fields.get('region', ''), '')
        if not region:
            return
        canonical = self.fields.get('name', '')
        names = list(dict.fromkeys([canonical, *self.names]))
        names = [name for name in names if name]
        if not names:
            return
        info = title_info(canonical or names[0])
        self.connection.execute('INSERT INTO games VALUES (?, ?, ?, ?, ?, ?)',
                                (game_id, system, region, info.title, _metadata(info.qualifiers), _metadata(info.packaging)))
        for name in names:
            parsed = title_info(name)
            if parsed.key and parsed.qualifiers == info.qualifiers:
                self.connection.execute('INSERT INTO names VALUES (?, ?, ?)', (game_id, parsed.key, name))
        self.games += 1
        if self.games > 100000:
            raise ProviderError('The GameTDB catalogue exceeds the record limit.')


def _check_archive(path):
    # Bound the central directory before ZipFile materializes member objects.
    with path.open('rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 65557))
        tail = handle.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if size > MAX_CATALOGUE_BYTES or position < 0 or len(tail) - position < 22:
        raise ProviderError('The GameTDB catalogue archive is invalid or oversized.')
    _, disk, directory_disk, disk_entries, entries, directory_bytes, offset, comment = struct.unpack_from('<4s4H2LH', tail, position)
    if (disk or directory_disk or disk_entries != entries or entries > 16 or directory_bytes > 16384
            or offset + directory_bytes > size or position + 22 + comment != len(tail)):
        raise ProviderError('The GameTDB catalogue archive exceeds supported directory limits.')


def _wbfs_disc_header(stream, checkpoint):
    """Read only the bounded WBFS disc table and its single copied disc header.

    Layout: Dolphin DiscIO/WbfsBlob and the original kwiirk/libwbfs format.
    No WLBA mapping, split-file search, partition extraction or ROM data reads.
    """
    checkpoint()
    prefix = stream.read(12)
    if len(prefix) != 12 or prefix[:4] != b'WBFS':
        return None
    hd_shift, wbfs_shift = prefix[8], prefix[9]
    # These explicit bounds prevent hostile shifts or an unbounded disc table.
    if not 9 <= hd_shift <= 16 or not 15 <= wbfs_shift <= 31 or wbfs_shift < hd_shift:
        return None
    hd_size, wbfs_size = 1 << hd_shift, 1 << wbfs_shift
    file_size = os.fstat(stream.fileno()).st_size
    hd_sectors = int.from_bytes(prefix[4:8], 'big')
    if not hd_sectors or hd_sectors * hd_size != file_size:
        return None
    wbfs_sectors = file_size // wbfs_size
    # WBFS stores sector addresses in unsigned 16-bit WLBA entries. A file
    # needs one metadata block and at least one possible disc-data block.
    if not 2 <= wbfs_sectors <= 65535:
        return None
    blocks_per_disc = 286864 >> (wbfs_shift - 15)
    info_size = (256 + 2 * blocks_per_disc + hd_size - 1) & ~(hd_size - 1)
    bitmap_start = ((wbfs_size - wbfs_sectors // 8) >> hd_shift) << hd_shift
    if info_size <= 0 or bitmap_start <= hd_size:
        return None
    max_slots = min((bitmap_start - hd_size) // info_size, hd_size - 12)
    checkpoint()
    table = stream.read(hd_size - 12)
    if len(table) != hd_size - 12 or any(value not in (0, 1) for value in table):
        return None
    occupied = [index for index, value in enumerate(table) if value]
    if len(occupied) != 1 or occupied[0] >= max_slots:
        return None
    offset = hd_size + occupied[0] * info_size
    # A descriptor must lie completely inside block0, before its free bitmap.
    if offset < hd_size or offset + info_size > min(bitmap_start, file_size, wbfs_size):
        return None
    checkpoint()
    stream.seek(offset)
    header = stream.read(32)
    checkpoint()
    return header if len(header) == 32 else None


class GameTDBProvider(_Provider):
    name = 'GameTDB'

    def __init__(self, cache_dir: Path, region='eu', interval=.25, workers=8, match_policy: MatchPolicy | None = None):
        super().__init__(cache_dir, region, interval, workers)
        self.interval = max(.5, self.interval)
        self.match_policy = match_policy or MatchPolicy()
        self._index = self.cache_dir / 'gametdb-v1.sqlite3'
        self._prepare_lock = threading.Lock()
        self._prepared = False
        self._catalogue_available = False
        self._catalogue_error = ''
        self._status_callback = None

    def set_status_callback(self, callback):
        self._status_callback = callback

    def _status(self, message):
        if self._status_callback:
            self._status_callback(message)

    def _trusted_host(self, host):
        return host in {'www.gametdb.com', 'art.gametdb.com'}

    def _stop(self, kind, message):
        # Public metadata and images are separate hosts. Refusing a catalogue
        # request must not disable verified local disc-ID artwork lookup.
        if getattr(self._local, 'catalogue_request', False):
            raise kind(message) from None
        super()._stop(kind, message)

    def _connect(self):
        return sqlite3.connect(self._index.resolve().as_uri() + '?mode=ro', uri=True)

    def _usable_index(self):
        try:
            with closing(self._connect()) as connection:
                return (connection.execute('PRAGMA user_version').fetchone()[0] == 1
                        and connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                        and connection.execute('SELECT game_id, system, region, title, qualifiers, packaging FROM games LIMIT 1').fetchone() is not None
                        and connection.execute('SELECT game_id, key, title FROM names LIMIT 1').fetchone() is not None)
        except (sqlite3.Error, OSError, ValueError):
            return False

    def prepare(self, checkpoint: Checkpoint):
        # Automatic runs prepare every configured provider before the selected
        # systems are known here. Public metadata is needed only for title lookup.
        checkpoint()
        self._check_fatal()

    def _prepare_catalogue(self, checkpoint: Checkpoint):
        self.prepare(checkpoint)
        while not self._prepare_lock.acquire(timeout=.1):
            checkpoint()
        try:
            if self._prepared:
                return
            usable = self._usable_index()
            if usable and time.time() - self._index.stat().st_mtime < CACHE_SECONDS:
                self._catalogue_available = self._prepared = True
                return
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._refresh(checkpoint)
                usable = True
            except Cancelled:
                raise
            except (ProviderError, OSError, sqlite3.Error):
                self._catalogue_error = 'GameTDB catalogue unavailable; verified Wii ISO, GCM or single-disc WBFS headers and GameCube ISO/GCM headers can still be used.'
                self._status('GameTDB refresh failed; using the cached catalogue.' if usable else self._catalogue_error)
            self._catalogue_available, self._prepared = usable, True
        finally:
            self._prepare_lock.release()

    def _refresh(self, checkpoint):
        archive_path = index_path = None
        try:
            self._status('GameTDB: downloading the Wii/GameCube catalogue.')
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
            for path in (archive_path, index_path):
                if path:
                    path.unlink(missing_ok=True)
                    Path(str(path) + '-journal').unlink(missing_ok=True)

    def _download_catalogue(self, output, checkpoint):
        self._local.catalogue_request = True
        try:
            response = self._request(CATALOGUE_URL, checkpoint)
        finally:
            self._local.catalogue_request = False
        try:
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_CATALOGUE_BYTES:
                raise ProviderError('The GameTDB catalogue exceeds the size limit.')
            total = 0
            for chunk in response.iter_content(65536):
                checkpoint()
                self._check_fatal()
                total += len(chunk)
                if total > MAX_CATALOGUE_BYTES:
                    raise ProviderError('The GameTDB catalogue exceeds the size limit.')
                self._throttle_bytes(len(chunk), checkpoint)
                output.write(chunk)
            output.flush()
        except (requests.RequestException, ValueError):
            raise ProviderError('The GameTDB catalogue download was interrupted or invalid.') from None
        finally:
            response.close()
            slot = getattr(response, '_novacore_slot', None)
            if slot is not None:
                del response._novacore_slot
                slot.release()

    def _index_archive(self, archive_path, index_path, checkpoint):
        self._status('GameTDB: indexing the Wii/GameCube catalogue.')
        connection = sqlite3.connect(index_path)
        try:
            connection.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;'
                'CREATE TABLE games (game_id TEXT PRIMARY KEY, system TEXT, region TEXT, title TEXT, qualifiers TEXT, packaging TEXT);'
                'CREATE TABLE names (game_id TEXT, key TEXT, title TEXT);')
            _check_archive(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                metadata = [member for member in members if member.filename == 'wiitdb.xml']
                if len(members) > 16 or len(metadata) != 1:
                    raise ProviderError('The GameTDB catalogue archive has an unsupported format.')
                member = metadata[0]
                if (member.file_size > MAX_XML_BYTES or member.flag_bits & 1
                        or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ProviderError('The GameTDB catalogue archive exceeds supported limits.')
                parser = create_parser()
                parser.setFeature(feature_external_ges, False)
                parser.setFeature(feature_external_pes, False)
                handler = _CatalogueHandler(connection, checkpoint)
                parser.setContentHandler(handler)
                with archive.open(member) as stream:
                    parser.parse(_XMLStream(stream, checkpoint))
                if not handler.games:
                    raise ProviderError('The GameTDB catalogue contains no supported game records.')
            checkpoint()
            connection.executescript('CREATE INDEX names_lookup ON names (key, game_id); PRAGMA user_version=1;')
            connection.commit()
            checkpoint()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, xml.sax.SAXException, RuntimeError, EOFError, zlib.error):
            raise ProviderError('GameTDB returned an invalid catalogue.') from None
        finally:
            connection.close()

    def _disc_identity(self, game, system, checkpoint):
        extension = game.path.suffix.casefold()
        if extension not in {'.iso', '.gcm', '.wbfs'}:
            return None
        checkpoint()
        try:
            with game.path.open('rb') as stream:
                header = _wbfs_disc_header(stream, checkpoint) if extension == '.wbfs' else stream.read(32)
        except OSError:
            return None
        checkpoint()
        if header is None or len(header) != 32:
            return None
        wii = header[0x18:0x1c] == bytes.fromhex('5D1C9EA3')
        gc = header[0x1c:0x20] == bytes.fromhex('C2339F3D')
        if extension == '.wbfs' and not wii:
            raise _WrongDiscPlatform()
        if wii == gc:
            return None
        if (system == 'Wii' and not wii) or (system == 'GameCube' and not gc):
            raise _WrongDiscPlatform()
        requested = requested_info(game)
        if requested.qualifiers or (requested.packaging and not self.match_policy.allow_base_cover):
            return None
        try:
            game_id = header[:6].decode('ascii')
        except UnicodeDecodeError:
            return None
        if not _ID.fullmatch(game_id) or game_id in {'DTLX01', 'GNHE5D'}:
            return None
        region = {'E': 'us', 'J': 'jp', 'K': 'kr', 'W': 'tw', 'P': 'eu', 'D': 'eu',
                  'F': 'eu', 'H': 'eu', 'I': 'eu', 'X': 'eu', 'Y': 'eu', 'Z': 'eu'}.get(game_id[3], '')
        origin = 'single-disc WBFS metadata' if extension == '.wbfs' else '32-byte header'
        return game_id, region, '', 'Verified local disc ID (' + game_id + '); read-only ' + origin + '.'

    def _catalogue_identity(self, game, system, checkpoint):
        if not self._catalogue_available:
            return None
        requested = requested_info(game)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute('SELECT DISTINCT game_id, region, games.title, names.title, packaging FROM games JOIN names USING(game_id) '
                    'WHERE system=? AND names.key=? AND qualifiers=? LIMIT 65',
                    (system, requested.key, _metadata(requested.qualifiers))).fetchall()
        except sqlite3.Error:
            raise ProviderError('The cached GameTDB catalogue could not be read.') from None
        checkpoint()
        if len(rows) >= 65:
            return None
        ranked = {}
        for game_id, region, title, alias, packaging in rows:
            if not isinstance(game_id, str) or not _ID.fullmatch(game_id) or game_id in {'DTLX01', 'GNHE5D'}:
                continue
            if not self.match_policy.allow_base_cover and _metadata(requested.packaging) != packaging:
                continue
            rank = region_rank(region, requested.regions, self.region, self.match_policy)
            if rank is not None:
                ranked[game_id] = (rank, region, title, alias)
        if not ranked:
            return None
        best = min(item[0] for item in ranked.values())
        matches = [(game_id, *item[1:]) for game_id, item in ranked.items() if item[0] == best]
        if len(matches) != 1:
            return None
        game_id, region, title, alias = matches[0]
        return game_id, region, alias, ''

    def candidates(self, game: Game, checkpoint: Checkpoint):
        checkpoint()
        system = GAMETDB_SYSTEMS.get(game.system.name.casefold())
        if system is None:
            self._unmatched('unsupported_system', 'GameTDB currently supports Wii and GameCube.')
            return
        self.prepare(checkpoint)
        try:
            identity = self._disc_identity(game, system, checkpoint)
        except _WrongDiscPlatform:
            self._unmatched('platform_mismatch', 'The verified disc header belongs to another console.')
            return
        if identity is None:
            self._prepare_catalogue(checkpoint)
            identity = self._catalogue_identity(game, system, checkpoint)
        if identity is None:
            self._unmatched('catalogue_unavailable' if not self._catalogue_available else 'no_match',
                            self._catalogue_error or 'GameTDB has no unique matching title or verified disc ID.')
            return
        game_id, disc_region, title, note = identity
        requested = requested_info(game)
        # A verified regional disc ID remains one game for every cover variant.
        # Explicit region and configured preferences precede fallback variants.
        regions = sorted(_COVERS, key=lambda region:
                         region_rank(region, requested.regions, self.region, self.match_policy) or (99, 99, 99, region))
        for region in regions:
            checkpoint()
            rank = region_rank(region, requested.regions, self.region, self.match_policy)
            if rank is None:
                continue
            fallback_note = match_note(requested, title_info(title or requested.title), region)
            parts = list(dict.fromkeys(part for part in (note, fallback_note) if part))
            yield Artwork(IMAGE_BASE_URL + _COVERS[region] + '/' + game_id + '.png', self.name, region,
                          match_note=' '.join(parts), matched_title=title, provider_game_id=game_id)

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        return next(self.candidates(game, checkpoint), None)
