"""Optional MobyGames subscription API v1; actual platform front-cover scans.

Official contracts: https://www.mobygames.com/info/api/ and
https://www.mobygames.com/api/subscribe/. Requires the user's own API key.
Data by MobyGames.com. Do not redistribute/repackage the database.

Ten seconds between requests observes even the documented legacy 360/hour
allowance. New subscriptions may allow more; NovaCore deliberately uses the
common conservative floor because no account-quota endpoint is documented.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

from .matching import MatchPolicy, match_note, region_key, region_rank, requested_info, title_info
from .models import Artwork, Game
from .providers import (AuthenticationError, CACHE_SECONDS, Checkpoint, MAX_TEXT_BYTES,
                        ProviderError, QuotaError, _Provider)

_BASE = 'https://api.mobygames.com/v1/'
_PAGE_LIMIT = 100
_MAX_SEARCH_PAGES = 10
_CACHE_VERSION = 2

# Provider platform IDs come exclusively from /platforms. Only exact console
# aliases are expanded; a neighbouring console is never used as a fallback.
_SYSTEM_NAMES = {
    'nes': ('NES',), 'famicom': ('NES',), 'fds': ('Famicom Disk System',),
    'snes': ('SNES',), 'snesmsu1': ('SNES',), 'n64': ('Nintendo 64',),
    'gb': ('Game Boy',), 'gbc': ('Game Boy Color',), 'gba': ('Game Boy Advance',),
    'nds': ('Nintendo DS',), '3ds': ('Nintendo 3DS',), 'n3ds': ('Nintendo 3DS',),
    'gamecube': ('GameCube',), 'wii': ('Wii',), 'wiiu': ('Wii U',),
    'switch': ('Nintendo Switch',), 'virtualboy': ('Virtual Boy',),
    'pokemini': ('Pokémon Mini',), 'megadrive': ('Genesis',), 'genesis': ('Genesis',),
    'mastersystem': ('SEGA Master System',), 'gamegear': ('Game Gear',),
    'sega32x': ('SEGA 32X',), '32x': ('SEGA 32X',),
    'segacd': ('SEGA CD',), 'megacd': ('SEGA CD',), 'saturn': ('SEGA Saturn',),
    'dreamcast': ('Dreamcast',), 'sg1000': ('SG-1000',), 'pico': ('Pico',),
    'psx': ('PlayStation',), 'ps1': ('PlayStation',), 'ps2': ('PlayStation 2',),
    'ps3': ('PlayStation 3',), 'ps4': ('PlayStation 4',), 'ps5': ('PlayStation 5',),
    'psp': ('PSP',), 'psvita': ('PS Vita',), 'xbox': ('Xbox',),
    'xbox360': ('Xbox 360',), 'xboxone': ('Xbox One',),
    'atari2600': ('Atari 2600',), 'atari5200': ('Atari 5200',), 'atari7800': ('Atari 7800',),
    'lynx': ('Lynx',), 'atarilynx': ('Lynx',), 'jaguar': ('Jaguar',),
    'atarijaguar': ('Jaguar',), 'jaguarcd': ('Jaguar CD',),
    'atari800': ('Atari 8-bit',), 'atarist': ('Atari ST',),
    'pcengine': ('TurboGrafx-16',), 'pcenginecd': ('TurboGrafx CD',),
    'supergrafx': ('SuperGrafx',), 'pcfx': ('PC-FX',), 'pc88': ('PC-88',), 'pc98': ('PC-98',),
    'neogeo': ('Neo Geo',), 'neogeocd': ('Neo Geo CD',),
    'ngp': ('Neo Geo Pocket',), 'ngpc': ('Neo Geo Pocket Color',),
    'wonderswan': ('WonderSwan',), 'wonderswancolor': ('WonderSwan Color',),
    'colecovision': ('ColecoVision',), 'intellivision': ('Intellivision',),
    'odyssey2': ('Odyssey 2',), 'vectrex': ('Vectrex',), '3do': ('3DO',),
    'amstradcpc': ('Amstrad CPC',), 'gx4000': ('Amstrad GX4000',),
    'c64': ('Commodore 64',), 'amiga500': ('Amiga',), 'amiga1200': ('Amiga',),
    'amiga': ('Amiga',), 'amigacd32': ('Amiga CD32',), 'amigacdtv': ('CDTV',),
    'msx1': ('MSX',), 'msx': ('MSX',), 'msx2': ('MSX2',),
    'zxspectrum': ('ZX Spectrum',), 'zx81': ('ZX81',), 'x68000': ('Sharp X68000',),
    'x1': ('Sharp X1',), 'dos': ('DOS',), 'supervision': ('Supervision',),
    'apple2': ('Apple II',), 'apple2gs': ('Apple IIgs',), 'bbc': ('BBC Micro',),
    'cdi': ('CD-i',), 'ngage': ('N-Gage',), 'windows': ('Windows',),
}
_EUROPE = frozenset({'austria', 'belgium', 'bulgaria', 'croatia', 'cyprus', 'czech republic',
    'czechia', 'denmark', 'estonia', 'finland', 'france', 'germany', 'greece', 'hungary',
    'iceland', 'ireland', 'italy', 'latvia', 'lithuania', 'luxembourg', 'malta', 'netherlands',
    'the netherlands', 'norway', 'poland', 'portugal', 'romania', 'slovakia', 'slovenia',
    'spain', 'sweden', 'switzerland', 'united kingdom'})
_COUNTRIES = {'worldwide': 'wor', 'the netherlands': 'nl', 'netherlands': 'nl',
    'south korea': 'kr', 'republic of korea': 'kr', 'sweden': 'se', 'norway': 'no',
    'finland': 'fi', 'denmark': 'dk', 'portugal': 'pt', 'poland': 'pl',
    'russian federation': 'ru', 'russia': 'ru'}


def _identifier(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _countries(values):
    result = set()
    for value in values:
        if not isinstance(value, str):
            continue
        country = ' '.join(value.casefold().split())
        if not country:
            continue
        result.add(_COUNTRIES.get(country, region_key(country)))
        if country in _EUROPE:
            result.add('eu')
    return result or {''}


class MobyGamesProvider(_Provider):
    name = 'MobyGames'
    attribution = 'Data by MobyGames.com'

    def __init__(self, cache_dir: Path, api_key: str, region: str = 'eu', interval: float = .25,
                 workers: int = 8, match_policy: MatchPolicy | None = None):
        super().__init__(cache_dir, region, interval, 1)
        self.interval = max(10.0, self.interval)
        self.match_policy = match_policy or MatchPolicy()
        self._api_key = api_key
        self._records = {}
        self._records_lock = threading.RLock()
        self._platforms = None

    def _trusted_host(self, host):
        return host in {'api.mobygames.com', 'www.mobygames.com', 'cdn.mobygames.com'}

    def _trusted(self, url):
        super()._trusted(url)
        parsed = urlsplit(url)
        if parsed.hostname == 'api.mobygames.com':
            valid = parsed.path.startswith('/v1/') and not parsed.fragment
        else:
            valid = (not parsed.query and not parsed.fragment and (
                parsed.hostname == 'cdn.mobygames.com' and parsed.path.startswith('/covers/') or
                parsed.hostname == 'www.mobygames.com' and parsed.path.startswith('/images/covers/')))
        if not valid:
            raise ProviderError('MobyGames returned an untrusted artwork or API address.')

    def _image_url(self, value):
        if not isinstance(value, str):
            raise ProviderError('MobyGames returned invalid front-cover data.')
        # v1 docs retain the historical HTTP URL. Upgrade that exact owned host
        # and cover path; do not derive or guess image IDs or original scans.
        if value.startswith('http://www.mobygames.com/images/covers/'):
            value = 'https://' + value[len('http://'):]
        self._trusted(value)
        if urlsplit(value).hostname == 'api.mobygames.com':
            raise ProviderError('MobyGames returned an untrusted artwork address.')
        return value

    def _reject_credentials(self, data):
        # Even a normally public field can contain a malformed server echo.
        # Inspect decoded JSON values before retaining any of them in memory
        # or on disk, including escaped and URL-encoded representations.
        secrets = {self._api_key, quote(self._api_key, safe=''), quote_plus(self._api_key, safe='')}
        secrets.discard('')
        values = [data]
        while values:
            value = values.pop()
            if isinstance(value, str):
                if any(secret in value for secret in secrets):
                    raise ProviderError('MobyGames returned invalid catalogue data.')
            elif isinstance(value, dict):
                values.extend(value.keys())
                values.extend(value.values())
            elif isinstance(value, list):
                values.extend(value)

    def _clean(self, kind, data):
        self._reject_credentials(data)
        if not isinstance(data, dict):
            raise ProviderError('MobyGames returned invalid catalogue data.')
        field = {'platforms': 'platforms', 'games': 'games', 'covers': 'cover_groups'}[kind]
        entries = data.get(field)
        if not isinstance(entries, list):
            raise ProviderError('MobyGames returned invalid catalogue data.')
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ProviderError('MobyGames returned invalid catalogue records.')
            if kind == 'platforms':
                identifier, name = _identifier(entry.get('platform_id')), entry.get('platform_name')
                if identifier is not None and isinstance(name, str) and name.strip():
                    result.append({'platform_id': identifier, 'platform_name': name})
            elif kind == 'games':
                identifier, title = _identifier(entry.get('game_id')), entry.get('title')
                if identifier is None or not isinstance(title, str) or not title.strip():
                    # Dropping a malformed row could shorten a full page and
                    # hide a later conflicting ID from the uniqueness check.
                    raise ProviderError('MobyGames returned invalid game identity data.')
                platforms, alternates = entry.get('platforms'), entry.get('alternate_titles')
                if alternates is None:
                    alternates = []
                if not isinstance(platforms, list) or not isinstance(alternates, list):
                    raise ProviderError('MobyGames returned invalid game identity data.')
                result.append({'game_id': identifier, 'title': title,
                    'platforms': [{'platform_id': platform['platform_id']} for platform in platforms
                        if isinstance(platform, dict) and _identifier(platform.get('platform_id')) is not None],
                    'alternate_titles': [{'title': alternate['title']} for alternate in alternates
                        if isinstance(alternate, dict) and isinstance(alternate.get('title'), str)]})
            else:
                countries, covers = entry.get('countries', []), entry.get('covers')
                if countries is None:
                    countries = []
                if not isinstance(countries, list) or not isinstance(covers, list):
                    raise ProviderError('MobyGames returned invalid cover-group data.')
                fronts = []
                for cover in covers:
                    if not isinstance(cover, dict) or cover.get('scan_of') != 'Front Cover':
                        continue
                    fronts.append({'scan_of': 'Front Cover', 'image': self._image_url(cover.get('image'))})
                result.append({'countries': [country for country in countries if isinstance(country, str)], 'covers': fronts})
        return {field: result}

    def _json(self, endpoint, kind, checkpoint, **params):
        # The key is added only at send time, never to a filename or cache body.
        identity = json.dumps([endpoint, params], sort_keys=True, ensure_ascii=False)
        token = hashlib.sha256(identity.encode()).hexdigest()
        with self._records_lock:
            checkpoint()
            self._check_fatal()
            if token in self._records:
                return self._records[token]
            filename = self.cache_dir / ('mobygames-' + token + '.json')
            try:
                if filename.stat().st_size <= MAX_TEXT_BYTES and time.time() - filename.stat().st_mtime < CACHE_SECONDS:
                    cached = json.loads(filename.read_text(encoding='utf-8'))
                    if isinstance(cached, dict) and cached.get('version') == _CACHE_VERSION:
                        data = self._clean(kind, cached.get('data'))
                        self._records[token] = data
                        return data
            except (OSError, ValueError, TypeError, ProviderError):
                pass
            response = self._request(_BASE + endpoint, checkpoint,
                params={'api_key': self._api_key, **params}, missing_ok=True, accepted_status={401, 429})
            if response is None:
                data = {'platforms': [], 'games': [], 'cover_groups': []}
            else:
                status = response.status_code
                raw = self._read(response, checkpoint, MAX_TEXT_BYTES)
                if status == 401:
                    self._stop(AuthenticationError, 'MobyGames rejected the API key. Enter your own active subscription API key.')
                if status == 429:
                    self._stop(QuotaError, 'The MobyGames API allowance is exhausted or rate limited. Resume after its reset.')
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    raise ProviderError('MobyGames returned invalid JSON catalogue data.') from None
            data = self._clean(kind, data)
            self._records[token] = data
            temporary = None
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir,
                                                 suffix='.tmp', delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump({'version': _CACHE_VERSION, 'data': data}, handle, ensure_ascii=False)
                os.replace(temporary, filename)
            except OSError:
                pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return data

    def prepare(self, checkpoint: Checkpoint) -> None:
        super().prepare(checkpoint)
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            self._stop(AuthenticationError, 'Enter your own MobyGames subscription API key to enable this source.')
        with self._records_lock:
            if self._platforms is None:
                self._platforms = self._json('platforms', 'platforms', checkpoint)['platforms']

    def _platform(self, game):
        names = {game.system.name, game.system.fullname}
        names.update(_SYSTEM_NAMES.get(game.system.name.casefold(), ()))
        keys = {title_info(name).key for name in names}
        matches = {entry['platform_id'] for entry in self._platforms if title_info(entry['platform_name']).key in keys}
        return next(iter(matches)) if len(matches) == 1 else None

    def candidates(self, game: Game, checkpoint: Checkpoint):
        """Yield all allowed fronts for one proven game, in preference order."""
        self.prepare(checkpoint)
        identifier = self._platform(game)
        if identifier is None:
            self._unmatched('unsupported_platform', 'MobyGames has no unambiguous exact platform mapping for this system.')
            return None
        requested = requested_info(game)
        if not requested.title or len(requested.title) > 128:
            self._unmatched('no_title_match', 'The game title is empty or exceeds the documented MobyGames search limit.')
            return None
        matches = {}
        for page in range(_MAX_SEARCH_PAGES):
            entries = self._json('games', 'games', checkpoint, platform=identifier, title=requested.title,
                                 format='normal', limit=_PAGE_LIMIT, offset=page * _PAGE_LIMIT)['games']
            for entry in entries:
                checkpoint()
                if not any(platform['platform_id'] == identifier for platform in entry['platforms']):
                    continue
                canonical = title_info(entry['title'])
                if (canonical.qualifiers and canonical.qualifiers != requested.qualifiers
                        and canonical.key != requested.key):
                    # A generic alias must not erase a canonical hack/demo/
                    # edition qualifier. Equivalent full text still matches
                    # when its only difference is parentheses or punctuation.
                    continue
                names = [entry['title']] + [alternate['title'] for alternate in entry['alternate_titles']]
                for name in names:
                    candidate = title_info(name)
                    if candidate.key != requested.key:
                        continue
                    if not self.match_policy.allow_base_cover and candidate.packaging != requested.packaging:
                        continue
                    matches.setdefault(entry['game_id'], (name, candidate))
            if len(matches) > 1:
                self._unmatched('ambiguous_title', 'More than one MobyGames game has this exact title on the selected platform.')
                return None
            if len(entries) < _PAGE_LIMIT:
                break
        else:
            self._unmatched('search_limit', 'The MobyGames search is too broad to prove one exact game safely.')
            return None
        if len(matches) != 1:
            self._unmatched('no_title_match', 'No exact title or verified alternate title was found for this MobyGames platform.')
            return None
        game_id, (matched_title, candidate) = next(iter(matches.items()))
        groups = self._json(f'games/{game_id}/platforms/{identifier}/covers', 'covers', checkpoint)['cover_groups']
        choices = []
        for group in groups:
            ranked = [(region_rank(region, requested.regions, self.region, self.match_policy), region)
                      for region in _countries(group['countries'])]
            ranked = [(rank, region) for rank, region in ranked if rank is not None]
            if not ranked:
                continue
            rank, region = min(ranked)
            choices.extend((rank, cover['image'], region) for cover in group['covers'])
        if not choices:
            self._unmatched('no_front_cover', 'No actual MobyGames front cover satisfies the selected region policy.')
            return None
        seen = set()
        for _, url, region in sorted(choices):
            checkpoint()
            if url in seen:
                continue
            seen.add(url)
            yield Artwork(url, self.name, region, match_note=match_note(requested, candidate, region),
                          matched_title=matched_title, provider_game_id=str(game_id))

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        candidates = self.candidates(game, checkpoint)
        try:
            return next(candidates, None)
        finally:
            candidates.close()
