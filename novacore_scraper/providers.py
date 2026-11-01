"""Conservative box-art providers with shared throttling and safe image publication.

No provider edits ROMs or metadata. Network exceptions intentionally omit remote
bodies and URLs: ScreenScraper puts account credentials in query strings.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import warnings
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from .models import Artwork, Game

Checkpoint = Callable[[], None]
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
CACHE_SECONDS = 7 * 24 * 3600


class ProviderError(RuntimeError):
    """A provider could not complete a request; message is safe for the UI."""


class AuthenticationError(ProviderError):
    """Account or application credentials must be corrected."""


class QuotaError(ProviderError):
    """A session-wide provider allowance has been exhausted."""


class Cancelled(Exception):
    """Raised by an engine checkpoint to stop work cooperatively."""


# These are public catalog directory names, not guessed URLs for games.
LIBRETRO_SYSTEMS = {
    'nes': 'Nintendo - Nintendo Entertainment System', 'famicom': 'Nintendo - Nintendo Entertainment System',
    'fds': 'Nintendo - Family Computer Disk System', 'snes': 'Nintendo - Super Nintendo Entertainment System',
    'snesmsu1': 'Nintendo - Super Nintendo Entertainment System', 'satellaview': 'Nintendo - Satellaview',
    'sufami': 'Nintendo - Sufami Turbo', 'n64': 'Nintendo - Nintendo 64', 'n64dd': 'Nintendo - Nintendo 64DD',
    'gb': 'Nintendo - Game Boy', 'gbc': 'Nintendo - Game Boy Color', 'gba': 'Nintendo - Game Boy Advance',
    'nds': 'Nintendo - Nintendo DS', '3ds': 'Nintendo - Nintendo 3DS', 'n3ds': 'Nintendo - Nintendo 3DS',
    'gamecube': 'Nintendo - GameCube', 'wii': 'Nintendo - Wii', 'wiiu': 'Nintendo - Wii U',
    'virtualboy': 'Nintendo - Virtual Boy', 'pokemini': 'Nintendo - Pokemon Mini',
    'megadrive': 'Sega - Mega Drive - Genesis', 'genesis': 'Sega - Mega Drive - Genesis',
    'mastersystem': 'Sega - Master System - Mark III', 'gamegear': 'Sega - Game Gear',
    'sega32x': 'Sega - 32X', '32x': 'Sega - 32X', 'segacd': 'Sega - Mega-CD - Sega CD',
    'megacd': 'Sega - Mega-CD - Sega CD', 'saturn': 'Sega - Saturn', 'dreamcast': 'Sega - Dreamcast',
    'sg1000': 'Sega - SG-1000', 'pico': 'Sega - PICO', 'naomi': 'Sega - Naomi', 'naomi2': 'Sega - Naomi 2',
    'psx': 'Sony - PlayStation', 'ps1': 'Sony - PlayStation', 'ps2': 'Sony - PlayStation 2',
    'ps3': 'Sony - PlayStation 3', 'ps4': 'Sony - PlayStation 4', 'psp': 'Sony - PlayStation Portable',
    'psvita': 'Sony - PlayStation Vita', 'xbox': 'Microsoft - Xbox', 'xbox360': 'Microsoft - Xbox 360',
    'atari2600': 'Atari - 2600', 'atari5200': 'Atari - 5200', 'atari7800': 'Atari - 7800',
    'lynx': 'Atari - Lynx', 'atarilynx': 'Atari - Lynx', 'jaguar': 'Atari - Jaguar',
    'atarijaguar': 'Atari - Jaguar', 'atari800': 'Atari - 8-bit', 'atarist': 'Atari - ST',
    'pcengine': 'NEC - PC Engine - TurboGrafx 16', 'pcenginecd': 'NEC - PC Engine CD - TurboGrafx-CD',
    'supergrafx': 'NEC - PC Engine SuperGrafx', 'pcfx': 'NEC - PC-FX', 'pc88': 'NEC - PC-88', 'pc98': 'NEC - PC-98',
    'neogeo': 'SNK - Neo Geo', 'neogeocd': 'SNK - Neo Geo CD',
    'ngp': 'SNK - Neo Geo Pocket', 'ngpc': 'SNK - Neo Geo Pocket Color',
    'wonderswan': 'Bandai - WonderSwan', 'wonderswancolor': 'Bandai - WonderSwan Color',
    'colecovision': 'Coleco - ColecoVision', 'intellivision': 'Mattel - Intellivision',
    'odyssey2': 'Magnavox - Odyssey2', 'vectrex': 'GCE - Vectrex', '3do': 'The 3DO Company - 3DO',
    'amstradcpc': 'Amstrad - CPC', 'gx4000': 'Amstrad - GX4000', 'c64': 'Commodore - 64',
    'amiga500': 'Commodore - Amiga', 'amiga1200': 'Commodore - Amiga', 'amiga': 'Commodore - Amiga',
    'amigacd32': 'Commodore - CD32', 'amigacdtv': 'Commodore - CDTV',
    'msx1': 'Microsoft - MSX', 'msx': 'Microsoft - MSX', 'msx2': 'Microsoft - MSX2',
    'zxspectrum': 'Sinclair - ZX Spectrum', 'zx81': 'Sinclair - ZX 81', 'x68000': 'Sharp - X68000',
    'x1': 'Sharp - X1', 'mame': 'MAME', 'arcade': 'MAME', 'fbneo': 'FBNeo - Arcade Games',
    'atomiswave': 'Atomiswave', 'scummvm': 'ScummVM', 'dos': 'DOS', 'supervision': 'Watara - Supervision',
}

_REGION_ALIASES = {
    'eu': 'eu', 'europe': 'eu', 'european': 'eu', 'pal': 'eu',
    'us': 'us', 'usa': 'us', 'united states': 'us', 'ntsc-u': 'us',
    'jp': 'jp', 'japan': 'jp', 'japanese': 'jp', 'ntsc-j': 'jp',
    'wor': 'wor', 'world': 'wor', 'fr': 'fr', 'france': 'fr', 'de': 'de', 'germany': 'de',
    'es': 'es', 'spain': 'es', 'it': 'it', 'italy': 'it', 'uk': 'uk', 'united kingdom': 'uk',
    'au': 'au', 'australia': 'au', 'ca': 'ca', 'canada': 'ca', 'br': 'br', 'brazil': 'br',
    'kr': 'kr', 'korea': 'kr', 'cn': 'cn', 'china': 'cn', 'tw': 'tw', 'taiwan': 'tw', 'asia': 'asi', 'asi': 'asi',
}
_TAGS = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]')


def _normal(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _region(value: str) -> str:
    return _REGION_ALIASES.get(_normal(value), _normal(value))


def _name_info(value: str) -> tuple[str, set[str], tuple[str, ...]]:
    regions: set[str] = set()
    qualifiers = []
    for match in _TAGS.finditer(value):
        tag = _normal(match.group(1) or match.group(2))
        parts = [part.strip() for part in re.split(r'[,/]', tag)]
        if parts and all(part in _REGION_ALIASES for part in parts):
            regions.update(_REGION_ALIASES[part] for part in parts)
        elif re.fullmatch(r'[a-z]{2}(?:,[a-z]{2})*', tag) and all(part in {'en', 'fr', 'de', 'es', 'it', 'ja', 'pt', 'nl', 'sv', 'zh', 'ko'} for part in parts):
            continue
        else:
            qualifiers.append(tag)
    return _normal(_TAGS.sub('', value)), regions, tuple(sorted(qualifiers))


def _requested(game: Game) -> tuple[list[str], str, set[str], tuple[str, ...]]:
    names = list(dict.fromkeys([game.path.stem, game.title]))
    base, _, title_qualifiers = _name_info(game.title)
    _, file_regions, file_qualifiers = _name_info(game.path.stem)
    _, title_regions, _ = _name_info(game.title)
    return names, base, file_regions or title_regions, file_qualifiers or title_qualifiers


def _choose_name(names: list[str], game: Game, preference: str) -> str | None:
    wanted, base, explicit_regions, qualifiers = _requested(game)
    for exact in wanted:
        matches = [name for name in names if _normal(name) == _normal(exact)]
        if len(matches) == 1:
            _, regions, candidate_qualifiers = _name_info(matches[0])
            if candidate_qualifiers == qualifiers and (not explicit_regions or regions & explicit_regions):
                return matches[0]
    matches = []
    for name in names:
        candidate_base, regions, candidate_qualifiers = _name_info(name)
        if candidate_base != base or candidate_qualifiers != qualifiers:
            continue
        if explicit_regions and not regions & explicit_regions:
            continue
        matches.append(name)
    preferred = [name for name in matches if preference in _name_info(name)[1]]
    if len(preferred) == 1:
        return preferred[0]
    if len(matches) == 1:
        return matches[0]
    return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        href = dict(attrs).get('href', '')
        parsed = urlsplit(href)
        name = unquote(parsed.path)
        if not parsed.scheme and not parsed.netloc and '/' not in name and '\\' not in name and name.casefold().endswith('.png'):
            self.names.append(name[:-4])


class _Provider:
    def __init__(self, cache_dir: Path, region: str, interval: float, workers: int):
        self.cache_dir = Path(cache_dir)
        self.region = _region(region)
        self.workers = max(1, min(4, int(workers)))
        self.interval = max(0.0, float(interval)) if math.isfinite(float(interval)) else 1.0
        self._slots = threading.BoundedSemaphore(self.workers)
        self._rate_lock = threading.Lock()
        self._next_request = 0.0
        self._bandwidth_lock = threading.Lock()
        self._next_bytes = 0.0
        self._download_speed = 0.0
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._fatal: tuple[type[ProviderError], str] | None = None

    def prepare(self, checkpoint: Checkpoint) -> None:
        checkpoint()
        self._check_fatal()

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def _session(self):
        if not hasattr(self._local, 'session'):
            session = requests.Session()
            session.headers['User-Agent'] = 'NovaCoreArtworkScraper/1.0'
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return self._local.session

    def _check_fatal(self):
        if self._fatal:
            kind, message = self._fatal
            raise kind(message) from None

    def _stop(self, kind: type[ProviderError], message: str):
        self._fatal = (kind, message)
        raise kind(message) from None

    def _trusted(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower()
            valid = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                     and parsed.port in (None, 443) and self._trusted_host(host))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ProviderError('The provider returned an untrusted artwork address.')

    def _trusted_host(self, host: str) -> bool:
        raise NotImplementedError

    def _wait(self, seconds: float, checkpoint: Checkpoint):
        until = time.monotonic() + seconds
        while True:
            checkpoint()
            self._check_fatal()
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _rate(self, checkpoint: Checkpoint):
        while True:
            checkpoint()
            self._check_fatal()
            with self._rate_lock:
                now = time.monotonic()
                delay = self._next_request - now
                if delay <= 0:
                    self._next_request = now + self.interval
                    return
            self._wait(min(delay, 0.1), checkpoint)

    def _before_request(self):
        self._check_fatal()

    def _throttle_bytes(self, count: int, checkpoint: Checkpoint):
        if self._download_speed <= 0 or count <= 0:
            return
        with self._bandwidth_lock:
            now = time.monotonic()
            self._next_bytes = max(self._next_bytes, now) + count / self._download_speed
            delay = self._next_bytes - now
        self._wait(delay, checkpoint)

    def _retry_delay(self, response: requests.Response, attempt: int):
        value = response.headers.get('Retry-After', '')
        try:
            delay = float(value)
            if not math.isfinite(delay):
                raise ValueError
            return max(0.0, delay)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return 0.5 * (2 ** attempt)

    def _request(self, url: str, checkpoint: Checkpoint, params=None, missing_ok=False):
        self._trusted(url)
        checkpoint()
        self._check_fatal()
        while not self._slots.acquire(timeout=0.1):
            checkpoint()
            self._check_fatal()
        retained = False
        try:
            current = url
            redirects = 0
            for attempt in range(4):
                while True:
                    self._trusted(current)
                    self._rate(checkpoint)
                    self._before_request()
                    try:
                        response = self._session().get(current, params=params, timeout=(5, 20), stream=True, allow_redirects=False)
                    except requests.RequestException:
                        if attempt == 3:
                            raise ProviderError('Network request failed after several attempts. Check your connection and try again.') from None
                        self._wait(0.5 * (2 ** attempt), checkpoint)
                        response = None
                        break
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location', '')
                        response.close()
                        redirects += 1
                        if not location or redirects > 4:
                            raise ProviderError('The provider returned an invalid redirect.')
                        current = urljoin(current, location)
                        params = None
                        continue
                    break
                if response is None:
                    continue
                status = response.status_code
                if status == 429 or 500 <= status <= 599:
                    delay = self._retry_delay(response, attempt)
                    response.close()
                    if attempt == 3:
                        raise ProviderError('The provider is busy. Try again later.')
                    with self._rate_lock:
                        self._next_request = max(self._next_request, time.monotonic() + delay)
                    self._wait(delay, checkpoint)
                    continue
                if status == 403:
                    response.close()
                    self._stop(AuthenticationError, 'The provider rejected the credentials. Check both developer and user account details.')
                if status in (430, 431):
                    response.close()
                    self._stop(QuotaError, 'The daily provider allowance is exhausted. Resume after it resets.')
                if status == 404 and missing_ok:
                    response.close()
                    return None
                if status != 200:
                    response.close()
                    messages = {401: 'The provider is temporarily closed to this account.', 423: 'The provider is temporarily unavailable.',
                                426: 'The provider does not currently accept this application version.', 404: 'The artwork is no longer available.'}
                    raise ProviderError(messages.get(status, 'The provider rejected the request.'))
                # Streaming responses remain active requests until fully read.
                response._novacore_slot = self._slots
                retained = True
                return response
            raise ProviderError('The provider could not complete the request.')
        finally:
            if not retained:
                self._slots.release()

    def _read(self, response, checkpoint: Checkpoint, maximum: int) -> bytes:
        try:
            length = response.headers.get('Content-Length')
            if length and int(length) > maximum:
                raise ProviderError('The provider response exceeds the size limit.')
            parts = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                checkpoint()
                self._check_fatal()
                total += len(chunk)
                if total > maximum:
                    raise ProviderError('The provider response exceeds the size limit.')
                self._throttle_bytes(len(chunk), checkpoint)
                parts.append(chunk)
            return b''.join(parts)
        except (requests.RequestException, ValueError):
            raise ProviderError('The provider response was interrupted or invalid.') from None
        finally:
            response.close()
            slot = getattr(response, '_novacore_slot', None)
            if slot is not None:
                del response._novacore_slot
                slot.release()

    def download(self, artwork: Artwork, destination: Path, checkpoint: Checkpoint) -> None:
        destination = Path(destination)
        checkpoint()
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Artwork already exists at the destination.')
        response = self._request(artwork.url, checkpoint)
        raw = self._read(response, checkpoint, MAX_IMAGE_BYTES)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as picture:
                    width, height = picture.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise ProviderError('The artwork exceeds the pixel limit.')
                    picture.verify()
                with Image.open(io.BytesIO(raw)) as picture:
                    picture.load()
                    converted = picture.convert('RGBA' if 'A' in picture.getbands() or 'transparency' in picture.info else 'RGB')
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ProviderError('The provider returned an invalid or oversized image.') from None
        checkpoint()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.novacore-', suffix='.tmp', dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                converted.save(handle, format='PNG')
                handle.flush()
                os.fsync(handle.fileno())
            checkpoint()
            self._check_fatal()
            # Windows rename refuses an existing target. POSIX rename overwrites;
            # hard-link publication instead atomically requires a vacant name.
            if os.name == 'nt':
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        except FileExistsError:
            raise FileExistsError('Artwork already exists at the destination.') from None
        finally:
            converted.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class LibretroProvider(_Provider):
    """Public, filename-based Named_Boxarts; no account or API key needed."""
    def __init__(self, cache_dir: Path, region: str = 'eu', interval: float = 1.0, workers: int = 2):
        super().__init__(cache_dir, region, interval, workers)
        self._catalogs: dict[str, list[str]] = {}
        self._catalog_lock = threading.Lock()
        self._indexes: dict[str, tuple[dict[str, list[str]], dict[str, list[str]]]] = {}
        self._index_lock = threading.Lock()

    def _trusted_host(self, host):
        return host == 'thumbnails.libretro.com'

    def _catalog(self, platform: str, checkpoint: Checkpoint):
        with self._catalog_lock:
            checkpoint()
            if platform in self._catalogs:
                return self._catalogs[platform]
            filename = self.cache_dir / ('libretro-' + hashlib.sha256(platform.encode()).hexdigest()[:20] + '.json')
            try:
                if time.time() - filename.stat().st_mtime < CACHE_SECONDS:
                    names = json.loads(filename.read_text(encoding='utf-8'))
                    if isinstance(names, list) and all(isinstance(name, str) and '/' not in name and '\\' not in name for name in names):
                        self._catalogs[platform] = names
                        return names
            except (OSError, ValueError):
                pass
            url = 'https://thumbnails.libretro.com/' + quote(platform, safe='') + '/Named_Boxarts/'
            response = self._request(url, checkpoint, missing_ok=True)
            if response is None:
                self._catalogs[platform] = []
                return []
            body = self._read(response, checkpoint, MAX_TEXT_BYTES).decode('utf-8', errors='replace')
            parser = _Links()
            parser.feed(body)
            names = sorted(set(parser.names))
            if not names and 'index of ' not in body.casefold():
                raise ProviderError('The provider returned an invalid artwork catalog. Try again later.')
            self._catalogs[platform] = names
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_dir, suffix='.tmp', delete=False) as handle:
                    temp = Path(handle.name)
                    json.dump(names, handle)
                try:
                    os.replace(temp, filename)
                finally:
                    temp.unlink(missing_ok=True)
            except OSError:
                pass  # A read-only cache must not prevent scraping.
            return names

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        checkpoint()
        platform = LIBRETRO_SYSTEMS.get(game.system.name.casefold())
        if platform is None and game.system.fullname in LIBRETRO_SYSTEMS.values():
            platform = game.system.fullname
        if platform is None:
            return None
        names = self._catalog(platform, checkpoint)
        with self._index_lock:
            if platform not in self._indexes:
                exact: dict[str, list[str]] = {}
                bases: dict[str, list[str]] = {}
                for index, name in enumerate(names):
                    if index % 256 == 0:
                        checkpoint()
                    exact.setdefault(_normal(name), []).append(name)
                    bases.setdefault(_name_info(name)[0], []).append(name)
                self._indexes[platform] = exact, bases
            exact, bases = self._indexes[platform]
        wanted, base, _, _ = _requested(game)
        candidates = list(bases.get(base, []))
        for name in wanted:
            candidates.extend(exact.get(_normal(name), []))
        match = _choose_name(list(dict.fromkeys(candidates)), game, self.region)
        if match is None:
            return None
        regions = _name_info(match)[1]
        region = self.region if self.region in regions else next(iter(sorted(regions)), '')
        return Artwork('https://thumbnails.libretro.com/' + quote(platform, safe='') + '/Named_Boxarts/' + quote(match + '.png', safe=''), 'Libretro', region)


class ScreenScraperProvider(_Provider):
    """ScreenScraper API v2 using the application's legitimate developer pair."""
    _BASE = 'https://api.screenscraper.fr/api2/'

    def __init__(self, cache_dir: Path, devid: str, devpassword: str, username: str, password: str,
                 region: str = 'eu', interval: float = 1.0, workers: int = 1):
        super().__init__(cache_dir, region, interval, workers)
        self._credentials = {'devid': devid, 'devpassword': devpassword, 'ssid': username, 'sspassword': password,
                             'softname': 'NovaCoreArtworkScraper', 'output': 'json'}
        self._prepared = False
        self._prepare_lock = threading.Lock()
        self._quota_lock = threading.Lock()
        self._max_daily = self._max_failures = 0
        self._requests_today = self._failures_today = 0
        self._quota_known = False
        self._system_ids: dict[str, str] = {}

    def _trusted_host(self, host):
        return host == 'screenscraper.fr' or host.endswith('.screenscraper.fr')

    def _before_request(self):
        super()._before_request()
        with self._quota_lock:
            if self._quota_known:
                if self._requests_today >= self._max_daily or self._failures_today >= self._max_failures:
                    self._stop(QuotaError, 'The daily provider allowance is exhausted. Resume after it resets.')
                self._requests_today += 1

    def _json(self, endpoint: str, checkpoint: Checkpoint, **extra):
        response = self._request(self._BASE + endpoint, checkpoint, params={**self._credentials, **extra}, missing_ok=True)
        if response is None:
            with self._quota_lock:
                self._failures_today += 1
            return {}
        raw = self._read(response, checkpoint, MAX_TEXT_BYTES)
        try:
            data = json.loads(raw)
            value = data.get('response', {})
            if not isinstance(value, dict):
                raise ValueError
            # Other software may share this account; use newer server counters
            # without letting out-of-order replies reduce our local reservations.
            user = value.get('ssuser', {})
            if self._quota_known and isinstance(user, dict):
                with self._quota_lock:
                    self._requests_today = max(self._requests_today, int(user.get('requeststoday', 0)))
                    self._failures_today = max(self._failures_today, int(user.get('requestskotoday', 0)))
            return value
        except (ValueError, TypeError, AttributeError):
            raise ProviderError('The provider returned invalid account or catalog data.') from None

    def prepare(self, checkpoint: Checkpoint) -> None:
        checkpoint()
        self._check_fatal()
        with self._prepare_lock:
            if self._prepared:
                return
            if not all(self._credentials[key].strip() for key in ('devid', 'devpassword', 'ssid', 'sspassword')):
                self._stop(AuthenticationError, 'Enter your own ScreenScraper developer and user credentials.')
            user = self._json('ssuserInfos.php', checkpoint).get('ssuser', {})
            try:
                maximum = int(user['maxthreads'])
                per_minute = int(user['maxrequestspermin'])
                per_day = int(user['maxrequestsperday'])
                failures = int(user['maxrequestskoperday'])
                used = int(user['requeststoday'])
                failed = int(user['requestskotoday'])
                # API documents Ko/s. Use decimal 1000 bytes, the conservative
                # limit even when an upstream server interprets kilobytes as 1024.
                speed = float(user.get('maxdownloadspeed', 0)) * 1000
                if maximum < 1 or per_minute < 1 or min(per_day, failures, used, failed) < 0:
                    raise ValueError
                if speed < 0 or not math.isfinite(speed):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ProviderError('The provider did not return valid thread and quota permissions.') from None
            self.workers = min(self.workers, maximum)
            self._slots = threading.BoundedSemaphore(self.workers)
            self.interval = max(self.interval, 60.0 / per_minute)
            with self._rate_lock:
                self._next_request = max(self._next_request, time.monotonic() + self.interval)
            self._download_speed = speed
            with self._quota_lock:
                self._max_daily, self._max_failures = per_day, failures
                self._requests_today, self._failures_today = used, failed
                self._quota_known = True
            if used >= per_day or failed >= failures:
                self._stop(QuotaError, 'The daily provider allowance is exhausted. Resume after it resets.')
            systems = self._json('systemesListe.php', checkpoint).get('systemes', [])
            if isinstance(systems, dict):
                systems = list(systems.values())
            if not isinstance(systems, list):
                raise ProviderError('The provider returned an invalid systems catalog.')
            for system in systems:
                if not isinstance(system, dict):
                    continue
                identifier = str(system.get('id', ''))
                if not identifier.isdigit():
                    continue
                names = system.get('noms', {})
                if isinstance(names, dict):
                    aliases = names.values()
                elif isinstance(names, list):
                    aliases = [item.get('text', '') for item in names if isinstance(item, dict)]
                else:
                    continue
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip():
                        for name in alias.split(','):
                            self._system_ids[_normal(name)] = identifier
            self._prepared = True

    def find(self, game: Game, checkpoint: Checkpoint) -> Artwork | None:
        self.prepare(checkpoint)
        identifier = game.system.screenscraper_id or self._system_ids.get(_normal(game.system.name)) or self._system_ids.get(_normal(game.system.fullname))
        if identifier is None or not str(identifier).isdigit():
            return None
        _, base, explicit_regions, qualifiers = _requested(game)
        # Remove region/language metadata while preserving meaningful title text.
        def title_tag(match):
            _, regions, tags = _name_info(match.group(0))
            return match.group(0) if tags and not regions else ''
        query = ' '.join(_TAGS.sub(title_tag, game.title).split())
        data = self._json('jeuRecherche.php', checkpoint, systemeid=str(identifier), recherche=query)
        games = data.get('jeux', [])
        if not isinstance(games, list):
            raise ProviderError('The provider returned an invalid game search.')
        matches = []
        for entry in games:
            if not isinstance(entry, dict):
                continue
            system = entry.get('systeme', {})
            if not isinstance(system, dict) or str(system.get('id', '')) != str(identifier):
                continue
            names = entry.get('noms', [])
            if not isinstance(names, list) or not any(
                isinstance(name, dict)
                and _name_info(str(name.get('text', '')))[0] == base
                and _name_info(str(name.get('text', '')))[2] == qualifiers
                for name in names
            ):
                continue
            matches.append(entry)
        # More than one distinct game with an identical name is review work.
        if len(matches) != 1:
            return None
        media = matches[0].get('medias', [])
        if not isinstance(media, list):
            return None
        boxes = [item for item in media if isinstance(item, dict) and item.get('type') == 'box-2D' and item.get('url')]
        if explicit_regions:
            boxes = [item for item in boxes if _region(str(item.get('region', ''))) in explicit_regions]
        preference = [self.region, 'wor', 'us', 'eu', 'jp', '']
        for region in dict.fromkeys(preference):
            regional = [item for item in boxes if _region(str(item.get('region', ''))) == region]
            if len(regional) == 1:
                url = str(regional[0]['url'])
                self._trusted(url)
                return Artwork(url, 'ScreenScraper', region)
        if len(boxes) == 1:
            url = str(boxes[0]['url'])
            self._trusted(url)
            return Artwork(url, 'ScreenScraper', _region(str(boxes[0].get('region', ''))))
        return None
