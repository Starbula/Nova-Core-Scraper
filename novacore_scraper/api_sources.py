"""Opt-in official artwork APIs; credentials and OAuth tokens stay in memory.

TheGamesDB uses its documented per-account API key. IGDB uses the user's Twitch
application and client-credentials flow. Only public platform/title metadata and
front-cover addresses enter the atomic disk cache, never raw API envelopes.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import threading
import time
from urllib.parse import quote

from .launchbox import LAUNCHBOX_SYSTEMS
from .matching import REGION_ALIASES, MatchPolicy, match_note, match_title, region_key, region_rank, requested_info, title_info, title_key
from .models import Artwork, Game
from .providers import AuthenticationError, CACHE_SECONDS, Checkpoint, MAX_TEXT_BYTES, ProviderError, QuotaError, _Provider


# Synonyms refer to the same machine. Family names never combine GB/GBC,
# Mega Drive/Master System, PlayStation generations, or Neo Geo AES/MVS.
_PLATFORM_SYNONYMS = {
    'nes': ('Nintendo Entertainment System', 'NES', 'Famicom'),
    'fds': ('Family Computer Disk System', 'Famicom Disk System'),
    'snes': ('Super Nintendo Entertainment System', 'SNES', 'Super Famicom'),
    'gb': ('Game Boy',), 'gbc': ('Game Boy Color',), 'gba': ('Game Boy Advance',),
    'nds': ('Nintendo DS',), '3ds': ('Nintendo 3DS',), 'gamecube': ('GameCube',),
    'wii': ('Wii',), 'wiiu': ('Wii U',), 'switch': ('Nintendo Switch',),
    'n64': ('Nintendo 64',), 'n64dd': ('Nintendo 64DD',), 'virtualboy': ('Virtual Boy',),
    'pokemini': ('Pokemon Mini', 'Pokémon mini'),
    'megadrive': ('Sega Mega Drive/Genesis', 'Sega Mega Drive', 'Sega Genesis', 'Genesis', 'Mega Drive'),
    'mastersystem': ('Sega Master System', 'Master System'), 'gamegear': ('Game Gear',),
    'sega32x': ('Sega 32X', '32X'), 'segacd': ('Sega CD', 'Sega Mega-CD', 'Sega CD/Mega-CD'),
    'saturn': ('Sega Saturn', 'Saturn'), 'dreamcast': ('Dreamcast', 'Sega Dreamcast'),
    'sg1000': ('SG-1000', 'Sega SG-1000'), 'pico': ('Sega Pico',),
    'psx': ('PlayStation', 'Sony PlayStation', 'PS1', 'PSX'),
    'ps2': ('PlayStation 2', 'PS2'), 'ps3': ('PlayStation 3', 'PS3'),
    'ps4': ('PlayStation 4', 'PS4'), 'ps5': ('PlayStation 5', 'PS5'),
    'psp': ('PlayStation Portable', 'PSP'), 'psvita': ('PlayStation Vita', 'PS Vita'),
    'xbox': ('Xbox',), 'xbox360': ('Xbox 360',), 'xboxone': ('Xbox One',),
    'atarilynx': ('Atari Lynx',), 'atarijaguar': ('Atari Jaguar',),
    'atari800': ('Atari 8-bit', 'Atari 8-bit family', 'Atari 800'),
    'pcengine': ('TurboGrafx-16', 'PC Engine', 'TurboGrafx-16/PC Engine'),
    'pcenginecd': ('TurboGrafx-CD', 'PC Engine CD', 'TurboGrafx-16/PC Engine CD'),
    'supergrafx': ('SuperGrafx', 'PC Engine SuperGrafx'), 'pcfx': ('PC-FX',),
    'pc88': ('PC-8800 Series', 'PC-8801'), 'pc98': ('PC-9800 Series', 'PC-9801'),
    'neogeo': ('Neo Geo AES',), 'neogeocd': ('Neo Geo CD',),
    'ngp': ('Neo Geo Pocket',), 'ngpc': ('Neo Geo Pocket Color',),
    'wonderswan': ('WonderSwan',), 'wonderswancolor': ('WonderSwan Color',),
    'colecovision': ('ColecoVision',), 'intellivision': ('Intellivision',),
    'odyssey2': ('Odyssey 2', 'Odyssey2', 'Philips Videopac G7000'),
    'vectrex': ('Vectrex',), '3do': ('3DO Interactive Multiplayer', '3DO'),
    'c64': ('Commodore C64/128/MAX', 'Commodore 64'),
    'amiga': ('Amiga', 'Commodore Amiga'), 'amigacd32': ('Amiga CD32',),
    'amigacdtv': ('Commodore CDTV', 'CDTV'), 'msx': ('MSX',), 'msx2': ('MSX2',),
    'zxspectrum': ('ZX Spectrum', 'Sinclair ZX Spectrum'), 'zx81': ('ZX81', 'Sinclair ZX81'),
    'mame': ('Arcade',), 'dos': ('DOS', 'MS-DOS'),
    'windows': ('PC (Microsoft Windows)', 'Windows'), 'apple2': ('Apple II',),
    'apple2gs': ('Apple IIGS',), 'bbc': ('BBC Micro',), 'cdi': ('Philips CD-i',),
    'ngage': ('N-Gage', 'Nokia N-Gage'), 'supervision': ('Watara Supervision',),
}
_SYSTEM_EQUIVALENTS = {
    'genesis': 'megadrive', 'famicom': 'nes', 'snesmsu1': 'snes', 'n3ds': '3ds',
    '32x': 'sega32x', 'megacd': 'segacd', 'ps1': 'psx', 'lynx': 'atarilynx',
    'jaguar': 'atarijaguar', 'amiga500': 'amiga', 'amiga1200': 'amiga',
    'msx1': 'msx', 'arcade': 'mame', 'fbneo': 'mame',
}
_CACHE_VERSION = 2
_MEMORY_ENTRIES = 512
_PAGE_LIMIT = 10
_IMAGE_ID = re.compile(r'[A-Za-z0-9_-]{1,200}')
_IMAGE_FILE = re.compile(r'[A-Za-z0-9_./ -]{1,500}\.(?:png|jpe?g|webp)', re.IGNORECASE)
_KNOWN_REGIONS = set(REGION_ALIASES.values()) | {
    'nl', 'ru', 'se', 'no', 'fi', 'dk', 'pl', 'cz', 'pt', 'gr', 'hk', 'za', 'nz', 'mx', 'ar', 'cl', 'sa',
}


def _identifier(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if result > 0 and str(value).isdigit() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value):
    return value if isinstance(value, str) and len(value) <= 4096 else ''


def _platform_id(platforms, game):
    system = game.system.name.casefold()
    canonical = _SYSTEM_EQUIVALENTS.get(system, system)
    aliases = {system, canonical, game.system.fullname, LAUNCHBOX_SYSTEMS.get(system, ''),
               LAUNCHBOX_SYSTEMS.get(canonical, ''), *_PLATFORM_SYNONYMS.get(canonical, ())}
    wanted = {title_key(name) for name in aliases if name}
    matches = {row['id'] for row in platforms if any(title_key(name) in wanted for name in row['names'])}
    return next(iter(matches)) if len(matches) == 1 else None


def _geographic_region(value):
    value = _text(value)
    normalized = region_key(value)
    normalized = {'north-america': 'us', 'north america': 'us', 'worldwide': 'wor',
                  'united-states': 'us', 'jpn': 'jp', 'eur': 'eu'}.get(normalized, normalized)
    return normalized if normalized in _KNOWN_REGIONS else ''


def _region(record):
    """A language alone proves no country, including ambiguous fr/de/es codes."""
    if not isinstance(record, dict):
        return ''
    category = _text(record.get('category')).casefold()
    identifier = _text(record.get('identifier')).replace('_', '-')
    locale = re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{4})?-([A-Za-z]{2})', identifier)
    if category == 'locale':
        return _geographic_region(locale.group(1)) if locale else ''
    if locale:
        return _geographic_region(locale.group(1))
    if category in {'country', 'continent'}:
        return _geographic_region(identifier) or _geographic_region(record.get('name'))
    # Older responses may omit category. Require an explicit geographic name;
    # capitalizing a bare language identifier does not establish a country.
    name = _text(record.get('name')).strip()
    return _geographic_region(name) if len(name) > 3 or name.casefold() in {'usa', 'uk'} else ''


class _APIProvider(_Provider):
    _COVER_NOTE = ''

    def __init__(self, cache_dir, region, interval, workers, match_policy):
        super().__init__(cache_dir, region, interval, min(8, int(workers)))
        self.interval = max(.25, self.interval)
        self.match_policy = match_policy or MatchPolicy()
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._platforms = None

    def _json_response(self, response, checkpoint):
        raw = self._read(response, checkpoint, MAX_TEXT_BYTES)
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise ProviderError('The provider returned invalid catalog data.') from None

    def _valid_rows(self, rows, search):
        if not isinstance(rows, list):
            return False
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
                    or not isinstance(row.get('names'), list) or not row['names']
                    or not all(_text(name) for name in row['names'])):
                return False
            if search:
                if (not isinstance(row.get('platforms'), list) or not row['platforms']
                        or not all(type(pid) is int and pid > 0 for pid in row['platforms'])
                        or not isinstance(row.get('images'), list)):
                    return False
                for entry in row['images']:
                    if (not isinstance(entry, dict) or not _text(entry.get('url'))
                            or not isinstance(entry.get('region'), str) or len(entry['region']) > 100):
                        return False
                    try:
                        self._trusted(entry['url'])
                    except ProviderError:
                        return False
        return True

    def _cached(self, identity, loader, checkpoint):
        digest = hashlib.sha256((str(_CACHE_VERSION) + ':' + identity).encode()).hexdigest()
        filename = self.cache_dir / (self.name.lower() + '-' + digest + '.json')
        with self._image_guard(('catalog:' + digest).encode(), checkpoint):
            with self._cache_lock:
                if digest in self._cache:
                    self._cache.move_to_end(digest)
                    return self._cache[digest]
            rows = None
            try:
                info = filename.stat()
                if info.st_size <= MAX_TEXT_BYTES and 0 <= time.time() - info.st_mtime < CACHE_SECONDS:
                    saved = json.loads(filename.read_text(encoding='utf-8'))
                    if (saved.get('version') == _CACHE_VERSION and saved.get('identity') == digest
                            and self._valid_rows(saved.get('rows'), identity.startswith('search:'))):
                        rows = saved['rows']
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if rows is None:
                rows = loader()
                body = json.dumps({'version': _CACHE_VERSION, 'identity': digest, 'rows': rows},
                                  ensure_ascii=False, separators=(',', ':'))
                # Even an unexpected upstream echo must not persist a secret.
                secrets = getattr(self, '_secrets', ())
                if len(body.encode()) <= MAX_TEXT_BYTES and not any(secret and secret in body for secret in secrets):
                    temporary = None
                    try:
                        self.cache_dir.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir,
                                                         prefix='.novacore-api-', suffix='.tmp', delete=False) as handle:
                            temporary = Path(handle.name)
                            handle.write(body)
                            handle.flush()
                            os.fsync(handle.fileno())
                        checkpoint()
                        os.replace(temporary, filename)
                    except OSError:
                        pass  # A read-only cache never blocks the lookup.
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
            with self._cache_lock:
                self._cache[digest] = rows
                self._cache.move_to_end(digest)
                while len(self._cache) > _MEMORY_ENTRIES:
                    self._cache.popitem(last=False)
            return rows

    def _select(self, records, platform_id, game, checkpoint):
        matches = {}
        wanted = requested_info(game)
        # API aliases identify a game; physical cover regions are ranked below.
        # A canonical title with no region tag can still have a proven EU cover.
        identity_policy = MatchPolicy(allow_other_regions=True,
                                      allow_base_cover=self.match_policy.allow_base_cover)
        for row in records:
            if platform_id not in row.get('platforms', []):
                continue
            canonical = title_info(row['names'][0])
            if canonical.qualifiers != wanted.qualifiers and canonical.key != wanted.key:
                continue
            matched = match_title(row['names'], game, identity_policy, self.region)
            if matched is not None:
                matches[row['id']] = row, matched
        if len(matches) != 1:
            self._unmatched('ambiguous_match' if matches else 'no_match',
                            'Several games share this title on the selected platform.' if matches else 'No matching title on this platform.')
            return None
        row, matched = next(iter(matches.values()))
        choices = []
        for entry in row.get('images', []):
            if not isinstance(entry, dict):
                continue
            url, region = _text(entry.get('url')), _text(entry.get('region'))
            if not url:
                continue
            self._trusted(url)
            rank = region_rank(region, wanted.regions, self.region, self.match_policy)
            if rank is not None:
                choices.append((rank, url, region))
        if not choices:
            self._unmatched('no_cover', 'The matching game has no eligible front cover.')
            return None
        seen = set()
        for _, url, region in sorted(choices):
            checkpoint()
            if url in seen:
                continue
            seen.add(url)
            note = ' '.join(value for value in (match_note(wanted, title_info(matched.name), region), self._COVER_NOTE) if value)
            yield Artwork(url, self.name, region, match_note=note,
                          matched_title=matched.name, provider_game_id=str(row['id']))

    def candidates(self, game: Game, checkpoint: Checkpoint):
        """Rank every eligible cover only after the complete search proves identity."""
        self.prepare(checkpoint)
        platform_id = _platform_id(self._platforms, game)
        if platform_id is None:
            self._unmatched('unsupported_platform', 'The API has no unambiguous match for this platform.')
            return None
        query = requested_info(game).title
        if not query or len(query) > 4096:
            return None
        records = self._cached('search:' + str(platform_id) + ':' + query,
                               lambda: self._search(platform_id, query, checkpoint), checkpoint)
        yield from self._select(records, platform_id, game, checkpoint)

    def find(self, game: Game, checkpoint: Checkpoint):
        candidates = self.candidates(game, checkpoint)
        try:
            return next(candidates, None)
        finally:
            candidates.close()

    def close(self):
        with self._cache_lock:
            self._cache.clear()
        super().close()


class TheGamesDBProvider(_APIProvider):
    name = 'TheGamesDB'
    _BASE = 'https://api.thegamesdb.net/'

    def __init__(self, cache_dir: Path, api_key: str, region='eu', interval=.25, workers=8, match_policy=None):
        super().__init__(cache_dir, region, interval, workers, match_policy)
        self._api_key = api_key.strip()
        self._secrets = (self._api_key,)
        self._quota_lock = threading.Lock()
        self._remaining = None

    def _trusted_host(self, host):
        return host in {'api.thegamesdb.net', 'cdn.thegamesdb.net'}

    def _before_request(self):
        super()._before_request()
        if getattr(self._local, 'tgdb_api', False):
            with self._quota_lock:
                if self._remaining is not None:
                    if self._remaining <= 0:
                        self._stop(QuotaError, 'TheGamesDB API allowance is exhausted. Resume after it resets.')
                    self._remaining -= 1

    def _json(self, endpoint, checkpoint, **params):
        self._local.tgdb_api = True
        try:
            response = self._request(self._BASE + endpoint, checkpoint,
                                     params={'apikey': self._api_key, **params}, accepted_status=(401, 403))
        finally:
            self._local.tgdb_api = False
        status = response.status_code
        if status in (401, 403):
            self._read(response, checkpoint, MAX_TEXT_BYTES)
            self._stop(AuthenticationError, 'TheGamesDB refused access. Check your API key and API allowance.')
        data = self._json_response(response, checkpoint)
        if not isinstance(data, dict):
            raise ProviderError('TheGamesDB returned invalid catalog data.')
        code = data.get('code', 200)
        if code in (401, 403):
            self._stop(AuthenticationError, 'TheGamesDB refused access. Check your API key and API allowance.')
        if code != 200 or not isinstance(data.get('data'), dict):
            raise ProviderError('TheGamesDB returned invalid catalog data.')
        try:
            remaining = int(data['remaining_monthly_allowance']) + int(data.get('extra_allowance', 0))
            if remaining < 0:
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ProviderError('TheGamesDB returned invalid API allowance data.') from None
        with self._quota_lock:
            self._remaining = remaining if self._remaining is None else min(self._remaining, remaining)
        return data

    def prepare(self, checkpoint: Checkpoint):
        super().prepare(checkpoint)
        with self._prepare_lock:
            checkpoint()
            if not self._api_key:
                self._stop(AuthenticationError, 'Enter your own TheGamesDB API key to enable this source.')
            if self._platforms is None:
                self._platforms = self._cached('platforms', lambda: self._load_platforms(checkpoint), checkpoint)

    def _load_platforms(self, checkpoint):
        result = self._json('v1/Platforms', checkpoint)['data'].get('platforms')
        if not isinstance(result, dict):
            raise ProviderError('TheGamesDB returned an invalid platform catalog.')
        return [{'id': ident, 'names': names} for row in result.values() if isinstance(row, dict)
                and (ident := _identifier(row.get('id'))) and
                (names := [name for key in ('name', 'alias') if (name := _text(row.get(key)))])]

    def _images(self, section, ident):
        if not isinstance(section, dict):
            return []
        base = section.get('base_url', {})
        files = section.get('data', section.get('images', {}))
        if not isinstance(base, dict) or not isinstance(files, dict):
            return []
        origin = _text(base.get('original'))
        if not origin:
            return []
        self._trusted(origin)
        result = []
        entries = files.get(str(ident), [])
        if not isinstance(entries, list):
            return []
        for row in entries:
            if not isinstance(row, dict) or row.get('type') != 'boxart' or row.get('side') != 'front':
                continue
            filename = _text(row.get('filename'))
            if (not _IMAGE_FILE.fullmatch(filename) or filename.startswith('/')
                    or '..' in PurePosixPath(filename).parts or '\\' in filename):
                continue
            url = origin.rstrip('/') + '/' + quote(filename, safe='/')
            self._trusted(url)
            result.append({'url': url, 'region': ''})  # API GameImage has no documented regional field.
        return result

    def _search(self, platform_id, query, checkpoint):
        records = {}
        for page in range(1, _PAGE_LIMIT + 1):
            payload = self._json('v1.1/Games/ByGameName', checkpoint, name=query,
                                 **{'filter[platform]': str(platform_id), 'fields': 'alternates', 'include': 'boxart', 'page': page})
            games = payload['data'].get('games')
            if not isinstance(games, list):
                raise ProviderError('TheGamesDB returned invalid search data.')
            included = payload.get('include', {})
            section = included.get('boxart', {}) if isinstance(included, dict) else {}
            for row in games:
                if not isinstance(row, dict) or not (ident := _identifier(row.get('id'))):
                    continue
                pid = _identifier(row.get('platform'))
                names = [_text(row.get('game_title'))]
                alternates = row.get('alternates', [])
                if isinstance(alternates, list):
                    names.extend(_text(name) for name in alternates)
                names = list(dict.fromkeys(name for name in names if name))
                if not names or pid is None:
                    continue
                records[ident] = {'id': ident, 'names': names, 'platforms': [pid], 'images': self._images(section, ident)}
            pages = payload.get('pages', {})
            if not isinstance(pages, dict) or not pages.get('next'):
                return list(records.values())
            # Reconstruct pagination locally. Upstream next links contain the
            # account key and must never be followed, cached or logged.
        raise ProviderError('TheGamesDB search is too broad to establish a unique match.')

    def close(self):
        self._api_key, self._secrets = '', ()
        super().close()


class IGDBProvider(_APIProvider):
    name = 'IGDB'
    _COVER_NOTE = 'Game-level cover; platform-specific packaging is unverified.'
    _BASE = 'https://api.igdb.com/v4/'
    _TOKEN = 'https://id.twitch.tv/oauth2/token'

    def __init__(self, cache_dir: Path, client_id: str, client_secret: str, region='eu', interval=.25,
                 workers=8, match_policy=None):
        super().__init__(cache_dir, region, interval, workers, match_policy)
        self._client_id, self._client_secret = client_id.strip(), client_secret.strip()
        self._secrets = (self._client_secret,)
        self._token_value, self._token_expires = '', 0.0
        self._token_lock = threading.Lock()

    def _trusted_host(self, host):
        return host in {'api.igdb.com', 'images.igdb.com', 'id.twitch.tv'}

    def _token(self, checkpoint, rejected=None):
        while not self._token_lock.acquire(timeout=.1):
            checkpoint()
            self._check_fatal()
        try:
            checkpoint()
            self._check_fatal()
            if self._token_value and time.monotonic() < self._token_expires and self._token_value != rejected:
                return self._token_value
            response = self._request(self._TOKEN, checkpoint, method='POST',
                                     data={'client_id': self._client_id, 'client_secret': self._client_secret,
                                           'grant_type': 'client_credentials'}, accepted_status=(400, 401, 403))
            status = response.status_code
            if status in (400, 401, 403):
                self._read(response, checkpoint, MAX_TEXT_BYTES)
                self._stop(AuthenticationError, 'IGDB authentication failed. Check your Twitch application client ID and secret.')
            data = self._json_response(response, checkpoint)
            try:
                token, lifetime = data['access_token'], float(data['expires_in'])
                if (not isinstance(token, str) or not token or len(token) > 4096 or '\r' in token or '\n' in token
                        or not math.isfinite(lifetime) or lifetime <= 0 or data.get('token_type', '').lower() != 'bearer'):
                    raise ValueError
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ProviderError('IGDB returned invalid authentication data.') from None
            self._token_value = token
            self._secrets = (self._client_secret, token)
            self._token_expires = time.monotonic() + max(.1, lifetime - min(60, lifetime * .1))
            return token
        finally:
            self._token_lock.release()

    def _json(self, endpoint, body, checkpoint):
        token = self._token(checkpoint)
        for attempt in range(2):
            response = self._request(self._BASE + endpoint, checkpoint, method='POST', data=body,
                                     headers={'Client-ID': self._client_id, 'Authorization': 'Bearer ' + token,
                                              'Accept': 'application/json', 'Content-Type': 'text/plain'},
                                     accepted_status=(401,))
            status = response.status_code
            if status == 401:
                self._read(response, checkpoint, MAX_TEXT_BYTES)
                if attempt:
                    self._stop(AuthenticationError, 'IGDB rejected the refreshed token. Check your Twitch application credentials.')
                token = self._token(checkpoint, rejected=token)
                continue
            data = self._json_response(response, checkpoint)
            if not isinstance(data, list):
                raise ProviderError('IGDB returned invalid catalog data.')
            return data

    def prepare(self, checkpoint: Checkpoint):
        super().prepare(checkpoint)
        with self._prepare_lock:
            checkpoint()
            if not self._client_id or not self._client_secret:
                self._stop(AuthenticationError, 'Enter your own Twitch application client ID and secret to enable IGDB.')
            if self._platforms is None:
                self._platforms = self._cached('platforms', lambda: self._load_platforms(checkpoint), checkpoint)

    def _load_platforms(self, checkpoint):
        platforms = {}
        for page in range(_PAGE_LIMIT):
            rows = self._json('platforms', 'fields name,alternative_name,abbreviation,slug; limit 500; offset ' + str(page * 500) + ';', checkpoint)
            for row in rows:
                if not isinstance(row, dict) or not (ident := _identifier(row.get('id'))):
                    continue
                names = [name for key in ('name', 'alternative_name', 'abbreviation', 'slug') if (name := _text(row.get(key)))]
                if names:
                    platforms[ident] = {'id': ident, 'names': names}
            if len(rows) < 500:
                return list(platforms.values())
        raise ProviderError('IGDB returned an oversized platform catalog.')

    def _cover(self, cover, region=''):
        if not isinstance(cover, dict) or cover.get('animated'):
            return None
        ident = _text(cover.get('image_id'))
        if not _IMAGE_ID.fullmatch(ident):
            return None
        return {'url': 'https://images.igdb.com/igdb/image/upload/t_cover_big_2x/' + ident + '.jpg', 'region': region}

    def _search(self, platform_id, query, checkpoint):
        records = {}
        for page in range(_PAGE_LIMIT):
            body = ('search ' + json.dumps(query) + '; fields name,alternative_names.name,platforms,cover.image_id,cover.animated,'
                    'game_localizations.name,game_localizations.cover.image_id,game_localizations.cover.animated,'
                    'game_localizations.region.name,game_localizations.region.identifier,game_localizations.region.category; where platforms = ' + str(platform_id) +
                    '; limit 50; offset ' + str(page * 50) + ';')
            rows = self._json('games', body, checkpoint)
            for row in rows:
                if not isinstance(row, dict) or not (ident := _identifier(row.get('id'))):
                    continue
                names = [_text(row.get('name'))]
                for alias in row.get('alternative_names', []) if isinstance(row.get('alternative_names', []), list) else []:
                    if isinstance(alias, dict):
                        names.append(_text(alias.get('name')))
                platforms = row.get('platforms', [])
                if not isinstance(platforms, list):
                    continue
                images = []
                if cover := self._cover(row.get('cover')):
                    images.append(cover)
                localizations = row.get('game_localizations', [])
                if isinstance(localizations, list):
                    for localization in localizations:
                        if not isinstance(localization, dict):
                            continue
                        names.append(_text(localization.get('name')))
                        region = localization.get('region', {})
                        if not isinstance(region, dict):
                            continue
                        region = _region(region)
                        if cover := self._cover(localization.get('cover'), region):
                            images.append(cover)
                names = list(dict.fromkeys(name for name in names if name))
                if names:
                    records[ident] = {'id': ident, 'names': names,
                                      'platforms': [pid for value in platforms if (pid := _identifier(value))], 'images': images}
            if len(rows) < 50:
                return list(records.values())
        raise ProviderError('IGDB search is too broad to establish a unique match.')

    def close(self):
        with self._token_lock:
            self._token_value, self._token_expires = '', 0.0
            self._client_id, self._client_secret, self._secrets = '', '', ()
        super().close()
