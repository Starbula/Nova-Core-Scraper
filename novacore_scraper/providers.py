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
import tempfile
import threading
import time
import unicodedata
import warnings
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from .models import Artwork, Game
from .matching import (MatchPolicy, match_note, match_title, match_titles, region_key, region_rank,
                       requested_info, title_info)

Checkpoint = Callable[[], None]
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_CACHE_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
CACHE_SECONDS = 7 * 24 * 3600


class ProviderError(RuntimeError):
    """A provider could not complete a request; message is safe for the UI."""


class ArtworkUnavailable(ProviderError):
    """A listed image is absent; trying another source/region can still succeed."""


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
    'snesmsu1': 'Nintendo - Super Nintendo Entertainment System', 'snes-msu1': 'Nintendo - Super Nintendo Entertainment System',
    'satellaview': 'Nintendo - Satellaview',
    'sufami': 'Nintendo - Sufami Turbo', 'n64': 'Nintendo - Nintendo 64', 'n64dd': 'Nintendo - Nintendo 64DD',
    'gb': 'Nintendo - Game Boy', 'gbc': 'Nintendo - Game Boy Color', 'gba': 'Nintendo - Game Boy Advance',
    'nds': 'Nintendo - Nintendo DS', '3ds': 'Nintendo - Nintendo 3DS', 'n3ds': 'Nintendo - Nintendo 3DS',
    'gamecube': 'Nintendo - GameCube', 'wii': 'Nintendo - Wii', 'wiiu': 'Nintendo - Wii U',
    'virtualboy': 'Nintendo - Virtual Boy', 'pokemini': 'Nintendo - Pokemon Mini',
    'megadrive': 'Sega - Mega Drive - Genesis', 'genesis': 'Sega - Mega Drive - Genesis',
    'megadrive-msu': 'Sega - Mega Drive - Genesis',
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
    'wswan': 'Bandai - WonderSwan', 'wswanc': 'Bandai - WonderSwan Color',
    'colecovision': 'Coleco - ColecoVision', 'intellivision': 'Mattel - Intellivision',
    'odyssey2': 'Magnavox - Odyssey2', 'vectrex': 'GCE - Vectrex', '3do': 'The 3DO Company - 3DO',
    'amstradcpc': 'Amstrad - CPC', 'gx4000': 'Amstrad - GX4000', 'c64': 'Commodore - 64',
    'amiga500': 'Commodore - Amiga', 'amiga1200': 'Commodore - Amiga', 'amiga': 'Commodore - Amiga',
    'amiga4000': 'Commodore - Amiga',
    'amigacd32': 'Commodore - CD32', 'amigacdtv': 'Commodore - CDTV',
    'msx1': 'Microsoft - MSX', 'msx': 'Microsoft - MSX', 'msx2': 'Microsoft - MSX2',
    'zxspectrum': 'Sinclair - ZX Spectrum', 'zx81': 'Sinclair - ZX 81', 'x68000': 'Sharp - X68000',
    'x1': 'Sharp - X1', 'mame': 'MAME', 'arcade': 'MAME', 'fbneo': 'FBNeo - Arcade Games',
    'cps1': 'MAME', 'cps2': 'MAME', 'cps3': 'MAME', 'cave': 'MAME',
    'segastv': 'MAME', 'model2': 'MAME', 'model3': 'MAME',
    'atomiswave': 'Atomiswave', 'scummvm': 'ScummVM', 'dos': 'DOS', 'supervision': 'Watara - Supervision',
}



def _normal(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _region(value: str) -> str:
    return region_key(value)


def _name_info(value: str) -> tuple[str, set[str], tuple[str, ...]]:
    info = title_info(value)
    return info.key, set(info.regions), info.qualifiers + info.packaging


def _requested(game: Game) -> tuple[list[str], str, set[str], tuple[str, ...]]:
    names = list(dict.fromkeys([game.path.stem, game.title]))
    info = requested_info(game)
    return names, info.key, set(info.regions), info.qualifiers + info.packaging


def _choose_name(names: list[str], game: Game, preference: str,
                 policy: MatchPolicy | None = None) -> str | None:
    match = match_title(names, game, policy, preference)
    return match.name if match else None


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
        self.workers = max(1, min(16, int(workers)))
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
        self._image_state_lock = threading.Lock()
        self._image_locks: dict[bytes, tuple[threading.Lock, int]] = {}
        self._image_cache: OrderedDict[bytes, bytes] = OrderedDict()
        self._image_cache_bytes = 0
        self._request_stats_lock = threading.Lock()
        self._request_count = 0
        self._api_request_count = 0

    def request_stats(self) -> dict:
        """Count actual outgoing attempts, not cached lookups or queued work."""
        with self._request_stats_lock:
            return dict(requests=self._request_count, api_requests=self._api_request_count, remaining=None)

    def _note_request(self, url: str) -> None:
        # Classify only the host; credentials and URLs never enter statistics.
        host = (urlsplit(url).hostname or '').casefold()
        api = host in {'api.thegamesdb.net', 'api.igdb.com', 'api.mobygames.com',
                       'api.screenscraper.fr', 'www.screenscraper.fr', 'screenscraper.fr'}
        with self._request_stats_lock:
            self._request_count += 1
            self._api_request_count += int(api)

    def prepare(self, checkpoint: Checkpoint) -> None:
        checkpoint()
        self._check_fatal()

    def _unmatched(self, code: str, message: str):
        self._local.reason = (code, message)
        return None

    def missing_reason(self, game=None) -> tuple[str, str]:
        return getattr(self._local, 'reason', ('no_title_match', 'No matching game was found in this source.'))

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
        with self._image_state_lock:
            self._image_cache.clear()
            self._image_cache_bytes = 0

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

    def _request(self, url: str, checkpoint: Checkpoint, params=None, missing_ok=False,
                 *, method='GET', data=None, headers=None, accepted_status=()):
        method = str(method).upper()
        if method not in {'GET', 'POST'}:
            raise ProviderError('The provider requested an unsupported operation.')
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
                    try:
                        session = self._session()
                        prepared = session.prepare_request(requests.Request(method, current, params=params, data=data, headers=headers))
                        settings = session.merge_environment_settings(prepared.url, {}, True, None, None)
                        # Preparation can block or finish out of order. Reserve
                        # rate/quota only when the prepared request is ready to send.
                        self._rate(checkpoint)
                        self._before_request()
                        self._note_request(prepared.url)
                        response = session.send(prepared, timeout=(5, 20), allow_redirects=False, **settings)
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
                        target = urljoin(current, location)
                        if method != 'GET' or ((params or headers or urlsplit(current).query)
                                              and urlsplit(target).netloc != urlsplit(current).netloc):
                            raise ProviderError('The provider returned an unsafe authenticated redirect.')
                        redirects += 1
                        if not location or redirects > 4:
                            raise ProviderError('The provider returned an invalid redirect.')
                        current = target
                        params = None
                        continue
                    break
                if response is None:
                    continue
                status = response.status_code
                if status in accepted_status:
                    response._novacore_slot = self._slots
                    retained = True
                    return response
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
                    self._stop(AuthenticationError, 'The provider refused access. Check service availability and account access if required.')
                if status in (430, 431):
                    response.close()
                    self._stop(QuotaError, 'The daily provider allowance is exhausted. Resume after it resets.')
                if status == 404 and missing_ok:
                    response.close()
                    return None
                if status == 404:
                    response.close()
                    raise ArtworkUnavailable('The artwork is no longer available.')
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

    @contextmanager
    def _image_guard(self, key: bytes, checkpoint: Checkpoint):
        """Only identical URLs share a lock; waiters never consume HTTP slots."""
        with self._image_state_lock:
            lock, users = self._image_locks.get(key, (threading.Lock(), 0))
            self._image_locks[key] = lock, users + 1
        acquired = False
        try:
            while not lock.acquire(timeout=0.1):
                checkpoint()
                self._check_fatal()
            acquired = True
            checkpoint()
            self._check_fatal()
            yield
        finally:
            if acquired:
                lock.release()
            with self._image_state_lock:
                _, users = self._image_locks[key]
                if users == 1:
                    del self._image_locks[key]
                else:
                    self._image_locks[key] = lock, users - 1

    def _cached_image(self, key: bytes) -> bytes | None:
        with self._image_state_lock:
            raw = self._image_cache.get(key)
            if raw is not None:
                self._image_cache.move_to_end(key)
            return raw

    def _remember_image(self, key: bytes, raw: bytes) -> None:
        # Only decoded/verified bytes reach here. Keep neither credentials nor
        # raw artwork addresses in cache keys, and never persist image bytes.
        if len(raw) > MAX_IMAGE_CACHE_BYTES:
            return
        with self._image_state_lock:
            previous = self._image_cache.pop(key, None)
            if previous is not None:
                self._image_cache_bytes -= len(previous)
            while self._image_cache and self._image_cache_bytes + len(raw) > MAX_IMAGE_CACHE_BYTES:
                _, discarded = self._image_cache.popitem(last=False)
                self._image_cache_bytes -= len(discarded)
            self._image_cache[key] = raw
            self._image_cache_bytes += len(raw)

    def _decode_image(self, raw: bytes):
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
        return converted

    def download(self, artwork: Artwork, destination: Path, checkpoint: Checkpoint) -> None:
        destination = Path(destination)
        checkpoint()
        self._check_fatal()
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('Artwork already exists at the destination.')
        self._trusted(artwork.url)
        key = hashlib.sha256(artwork.url.encode('utf-8')).digest()
        with self._image_guard(key, checkpoint):
            # Another worker may have published this output while we waited.
            if destination.exists() or destination.is_symlink():
                raise FileExistsError('Artwork already exists at the destination.')
            raw = self._cached_image(key)
            uncached = raw is None
            if uncached:
                response = self._request(artwork.url, checkpoint)
                raw = self._read(response, checkpoint, MAX_IMAGE_BYTES)
            converted = self._decode_image(raw)
            if uncached:
                self._remember_image(key, raw)
        temporary = None
        try:
            checkpoint()
            self._check_fatal()
            if destination.exists() or destination.is_symlink():
                raise FileExistsError('Artwork already exists at the destination.')
            destination.parent.mkdir(parents=True, exist_ok=True)
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
    def __init__(self, cache_dir: Path, region: str = 'eu', interval: float = 1.0, workers: int = 2,
                 match_policy: MatchPolicy | None = None):
        super().__init__(cache_dir, region, interval, min(8, int(workers)))
        self.match_policy = match_policy or MatchPolicy()
        # NovaCore's conservative public traffic policy, not a published quota.
        self.interval = max(0.25, self.interval)
        self._catalogs: dict[str, list[str]] = {}
        self._catalog_lock = threading.Lock()
        self._indexes: dict[str, dict[str, list[str]]] = {}
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
        return next(self.candidates(game, checkpoint), None)

    def candidates(self, game: Game, checkpoint: Checkpoint):
        checkpoint()
        platform = LIBRETRO_SYSTEMS.get(game.system.name.casefold())
        if platform is None and game.system.fullname in LIBRETRO_SYSTEMS.values():
            platform = game.system.fullname
        if platform is None:
            self._unmatched('unsupported_platform', 'Libretro has no artwork catalogue for this platform.')
            return None
        names = self._catalog(platform, checkpoint)
        with self._index_lock:
            if platform not in self._indexes:
                bases: dict[str, list[str]] = {}
                for index, name in enumerate(names):
                    if index % 256 == 0:
                        checkpoint()
                    bases.setdefault(_name_info(name)[0], []).append(name)
                self._indexes[platform] = bases
            bases = self._indexes[platform]
        requested = requested_info(game)
        candidates = bases.get(requested.key, [])
        found = False
        for match in match_titles(candidates, game, self.match_policy, self.region):
            checkpoint()
            found = True
            yield Artwork('https://thumbnails.libretro.com/' + quote(platform, safe='') + '/Named_Boxarts/' + quote(match.name + '.png', safe=''),
                          'Libretro', match.region, match_note=match.note, matched_title=match.name)
        if not found:
            code = 'cover_policy' if candidates else 'no_title_match'
            self._unmatched(code, 'No exact title cover satisfies the selected region and revision policy.' if candidates
                            else 'No exact title appears in the Libretro catalogue for this platform.')
            return None


class ScreenScraperProvider(_Provider):
    """ScreenScraper API v2 using the application's legitimate developer pair."""
    _BASE = 'https://api.screenscraper.fr/api2/'

    def __init__(self, cache_dir: Path, devid: str, devpassword: str, username: str, password: str,
                 region: str = 'eu', interval: float = 1.0, workers: int = 1,
                 match_policy: MatchPolicy | None = None):
        super().__init__(cache_dir, region, interval, workers)
        self.match_policy = match_policy or MatchPolicy()
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
        return next(self.candidates(game, checkpoint), None)

    def candidates(self, game: Game, checkpoint: Checkpoint):
        self.prepare(checkpoint)
        identifier = game.system.screenscraper_id or self._system_ids.get(_normal(game.system.name)) or self._system_ids.get(_normal(game.system.fullname))
        if identifier is None or not str(identifier).isdigit():
            self._unmatched('unsupported_platform', 'ScreenScraper has no verified ID for this platform.')
            return
        requested = requested_info(game)
        data = self._json('jeuRecherche.php', checkpoint, systemeid=str(identifier), recherche=requested.title)
        games = data.get('jeux', [])
        if not isinstance(games, list):
            raise ProviderError('The provider returned an invalid game search.')
        grouped = {}
        for entry in games:
            if not isinstance(entry, dict):
                continue
            system = entry.get('systeme', {})
            if not isinstance(system, dict) or str(system.get('id', '')) != str(identifier):
                continue
            game_id = str(entry.get('id', ''))
            if not game_id.isascii() or not game_id.isdecimal():
                continue
            names, media = grouped.setdefault(game_id, ([], []))
            entry_names = entry.get('noms', [])
            if isinstance(entry_names, list):
                names.extend((str(name.get('text', '')), title_info(str(name.get('text', ''))))
                             for name in entry_names if isinstance(name, dict))
            entry_media = entry.get('medias', [])
            if isinstance(entry_media, list):
                media.extend(entry_media)
        matches = {}
        for game_id, (names, media) in grouped.items():
            checkpoint()
            # All qualified aliases must agree on the game's qualifier identity.
            # A full qualified title may differ only in parenthetical typography;
            # a separate plain alias cannot hide or combine edition qualifiers.
            qualified = [info for _, info in names if info.qualifiers]
            qualifier_sets = {frozenset(info.qualifiers) for info in qualified}
            if len(qualifier_sets) > 1:
                continue
            qualifiers = next(iter(qualifier_sets), frozenset())
            full_qualified_title = any(info.key == requested.key for info in qualified)
            if qualifiers != frozenset(requested.qualifiers) and not full_qualified_title:
                continue
            matching_names = [(info.packaging != requested.packaging, label, info)
                              for label, info in names if info.key == requested.key
                              and (self.match_policy.allow_base_cover or info.packaging == requested.packaging)]
            if matching_names:
                _, label, info = min(matching_names, key=lambda item: (item[0], item[1]))
                matches[game_id] = label, info, media
        # Resolve the game ID before considering which regional artwork exists.
        if len(matches) != 1:
            self._unmatched('ambiguous_title' if matches else 'no_title_match',
                            'Several ScreenScraper game IDs share this title; review is required.' if matches
                            else 'No exact title matches this ScreenScraper platform.')
            return
        game_id, (label, candidate, media) = next(iter(matches.items()))
        boxes = []
        for item in media:
            if not isinstance(item, dict) or item.get('type') != 'box-2D' or not item.get('url'):
                continue
            region = _region(str(item.get('region', '')))
            rank = region_rank(region, requested.regions, self.region, self.match_policy)
            if rank is not None:
                boxes.append((rank, str(item['url']), region))
        if not boxes:
            self._unmatched('no_front_cover', 'The exact ScreenScraper game has no front cover allowed by the region policy.')
            return
        seen = set()
        for _, url, region in sorted(boxes):
            checkpoint()
            if url in seen:
                continue
            seen.add(url)
            self._trusted(url)
            yield Artwork(url, 'ScreenScraper', region, match_note=match_note(requested, candidate, region),
                          matched_title=label, provider_game_id=game_id)
