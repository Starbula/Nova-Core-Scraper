"""Read-only RetroBat discovery, launch-file scanning and artwork lookup.

Configuration path rules are adapted from NovaCore Downloader (MIT),
Copyright (c) 2026 Lu Lu. No ROM or directory is created by this module.
"""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import string
import threading
import unicodedata
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

from .models import Game, System

# Conservative launch formats, not emulator availability or playability claims.
_COMMON = {
    'megadrive': ('Sega Mega Drive', '.md .gen .smd .sgd .68k .bin .zip .7z', '1'),
    'mastersystem': ('Sega Master System', '.sms .bin .zip .7z', '2'),
    'psx': ('Sony PlayStation', '.cue .chd .pbp .ccd .m3u .iso', '57'),
    'ps2': ('Sony PlayStation 2', '.iso .chd .cso .cue .m3u', '58'),
    'ps3': ('Sony PlayStation 3', '.iso .ps3', '59'),
    'psp': ('Sony PlayStation Portable', '.iso .cso .pbp', '61'),
    'xbox': ('Microsoft Xbox', '.iso', '32'),
    'xbox360': ('Microsoft Xbox 360', '.iso', '33'),
    'nds': ('Nintendo DS', '.nds .zip .7z', '15'),
    '3ds': ('Nintendo 3DS', '.3ds .cci .cxi .3dsx', '17'),
    'wii': ('Nintendo Wii', '.iso .rvz .wbfs .gcz .wia', '16'),
    'gamecube': ('Nintendo GameCube', '.iso .gcm .rvz .gcz .wia', '13'),
    'n64': ('Nintendo 64', '.n64 .z64 .v64 .zip .7z', '14'),
    'saturn': ('Sega Saturn', '.cue .chd .ccd .iso .m3u', '22'),
    'dreamcast': ('Sega Dreamcast', '.gdi .cdi .chd .cue .m3u', '23'),
    'gamegear': ('Sega Game Gear', '.gg .zip .7z', '21'),
    'nes': ('Nintendo Entertainment System', '.nes .unf .unif .zip .7z', '3'),
    'snes': ('Super Nintendo', '.sfc .smc .zip .7z', '4'),
    'gb': ('Nintendo Game Boy', '.gb .zip .7z', '9'),
    'gbc': ('Nintendo Game Boy Color', '.gbc .gb .zip .7z', '10'),
    'gba': ('Nintendo Game Boy Advance', '.gba .zip .7z', '12'),
    'arcade': ('Arcade', '.zip .7z', '75'),
    'mame': ('MAME', '.zip .7z', '75'),
    'fbneo': ('FinalBurn Neo', '.zip .7z', '75'),
}
_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')
_MANIFEST_EXTENSIONS = frozenset({'.m3u', '.cue', '.gdi', '.ccd'})
# Only image files are retained; a full ROM index is not kept between scans.
_ARTWORK_SNAPSHOTS: OrderedDict[str, tuple[tuple[int, int], dict[str, Path]]] = OrderedDict()
_ARTWORK_SNAPSHOT_LOCK = threading.Lock()


def _is_link(path: Path) -> bool:
    """Windows junctions/reparse points are boundaries too."""
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    except OSError:
        return False


def _has_link_ancestor(path: Path) -> bool:
    return any(_is_link(part) for part in (path, *path.parents))


def _relative(raw: str, base: PurePosixPath = PurePosixPath(), *, parent: bool = False) -> str | None:
    """Apply Windows path rules even on non-Windows fixture hosts."""
    raw = raw.strip().replace('\\', '/')
    if not raw or '\x00' in raw or raw.startswith(('/', '~')) or re.match(r'^[a-zA-Z]:', raw):
        return None
    parts = list(base.parts)
    for piece in raw.split('/'):
        if piece in ('', '.'):
            continue
        if piece == '..':
            if not parent or not parts:
                return None
            parts.pop()
        elif ':' in piece:
            return None
        else:
            parts.append(piece)
    return '/'.join(parts) if parts else None


def _safe_path(boundary: Path, relative: str) -> Path | None:
    path = boundary
    for part in PurePosixPath(relative).parts:
        path = path / part
        if _is_link(path):
            return None
    return path


def validate_retrobat(path: Path) -> Path:
    """Return an absolute local installation root, or raise ValueError."""
    candidate = Path(path).expanduser().absolute()
    try:
        if _has_link_ancestor(candidate) or not candidate.is_dir():
            raise ValueError('Select a local RetroBat installation folder.')
        root = candidate.resolve()
        roms = root / 'roms'
        if _is_link(roms) or not roms.is_dir():
            raise ValueError('The RetroBat installation must contain a local roms directory.')
        if not any(p.name.casefold() == 'retrobat.exe' and not _is_link(p) and p.is_file() for p in root.iterdir()):
            raise ValueError('retrobat.exe is missing from the selected installation folder.')
        return root
    except OSError as exc:
        raise ValueError('Cannot read the selected RetroBat installation.') from exc


def detect_installations(extra: Iterable[Path] = (), *, first_only: bool = False, cancelled: Callable[[], bool] = lambda: False) -> list[Path]:
    """Check explicit and common portable locations without a drive-wide search."""
    candidates = list(extra)
    candidates.extend(Path(value) for key in ('RETROBAT_HOME', 'RETROBAT_PATH') if (value := os.environ.get(key)))
    candidates.extend((Path.cwd(), Path.cwd()/'RetroBat', Path.home()/'RetroBat'))
    if os.name == 'nt':
        candidates.extend(Path(drive + ':/RetroBat') for drive in string.ascii_uppercase)
        candidates.extend(Path(value)/'RetroBat' for key in ('ProgramFiles', 'ProgramFiles(x86)') if (value := os.environ.get(key)))
    found, seen = [], set()
    for candidate in candidates:
        if cancelled():
            break
        try:
            root = validate_retrobat(candidate)
        except (ValueError, OSError):
            continue
        key = os.path.normcase(str(root))
        if key not in seen:
            found.append(root)
            seen.add(key)
            if first_only:
                break
    return found


def _xml(path: Path, limit: int) -> ET.Element:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or _is_link(path) or info.st_size > limit:
        raise ValueError('XML file is nonregular, linked or too large.')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    declarations = raw.replace(b'\x00', b'').upper()
    if len(raw) > limit or b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ValueError('XML declarations or excessive input are not accepted.')
    return ET.fromstring(raw)


def _configured_path(root: Path, name: str, raw: str) -> Path | None:
    raw = raw.strip().replace('\\', '/')
    raw = re.sub('%ROMPATH%', lambda _match: (root/'roms').as_posix(), raw, flags=re.I)
    if not raw or '%' in raw or '\x00' in raw or raw.startswith('//') or (raw.startswith('~') and not raw.startswith('~/')):
        return None
    if raw.startswith('~/'):
        path = root/'emulationstation'/raw[2:]
    else:
        path = Path(raw)
        if re.match(r'^[a-zA-Z]:', raw) and not path.is_absolute():
            return None
        if not path.is_absolute():
            path = root/path
    # Standard RetroBat paths are ~\\..\\roms\\<system>. Normalize only
    # after retaining the original traversal for symlink/junction checks.
    normalized = Path(os.path.normpath(str(path)))
    try:
        relative = normalized.relative_to(root/'roms').as_posix()
    except ValueError:
        return None
    relative = _relative(relative)
    if not relative or _has_link_ancestor(path):
        return None
    safe = _safe_path(root/'roms', relative)
    return safe if safe and safe.is_dir() else None


def _config_entries(path: Path, root: Path, base: dict[str, System | None] | None = None) -> dict[str, System | None]:
    tree = _xml(path, 2 * 1024 * 1024)
    if tree.tag != 'systemList':
        raise ValueError('Unrecognised RetroBat system configuration.')
    result = {}
    for item in tree.findall('system'):
        name = (item.findtext('name') or '').strip().casefold()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name):
            continue
        previous = result.get(name, (base or {}).get(name))
        extension_node = item.find('extension')
        if extension_node is None and previous is not None:
            extensions = previous.extensions
        else:
            extensions = frozenset(e.casefold() for e in (item.findtext('extension') or '').split() if re.fullmatch(r'\.[a-zA-Z0-9]+', e) and e.casefold() != '.fake')
        path_node = item.find('path')
        if path_node is None:
            raw = str(previous.path) if previous is not None else 'roms/' + name
        else:
            raw = path_node.text or ''
        directory = _configured_path(root, name, raw)
        if not directory or not extensions:
            result[name] = None  # An unsafe/disabled custom override disables its base entry.
            continue
        screen_id = (item.findtext('screenscraper') or item.findtext('screenscraper_id') or (previous.screenscraper_id if previous is not None else '') or '').strip()
        if not screen_id.isdigit():
            screen_id = _COMMON.get(name, ('', '', None))[2]
        fullname = (item.findtext('fullname') or (previous.fullname if previous is not None else name)).strip()
        result[name] = System(name, fullname, directory, extensions, screen_id)
    return result


def discover_systems(root: Path) -> list[System]:
    root = validate_retrobat(root)
    candidates = [root/'emulationstation'/'.emulationstation'/'es_systems.cfg', root/'emulationstation'/'es_systems.cfg', root/'system'/'templates'/'emulationstation'/'es_systems.cfg']
    try:
        primary = next((path for path in candidates if path.exists()), None)
        if primary is None:
            return [System(name, fullname, directory, frozenset(extensions.split()), sid) for name, (fullname, extensions, sid) in sorted(_COMMON.items()) if (directory := _configured_path(root, name, 'roms/' + name))]
        if _has_link_ancestor(primary):
            return []
        systems = _config_entries(primary, root)
        # RetroBat also creates per-system es_systems_<name>.cfg overlays.
        # Inherit omitted fields so emulator-only overrides keep ROM formats.
        for custom in sorted(primary.parent.glob('es_systems_*.cfg'), key=lambda path: path.name.casefold()):
            if _has_link_ancestor(custom):
                return []
            systems.update(_config_entries(custom, root, systems))
        return [system for _, system in sorted(systems.items()) if system is not None]
    except (OSError, ValueError, ET.ParseError):
        # A broken present config must not silently enable unrelated fallback systems.
        return []


def _box_image_hint(raw: str) -> bool:
    """RetroBat image normally means screenshot; only reuse explicit box paths."""
    path = raw.strip().replace('\\', '/').casefold()
    return (bool(re.search(r'(?<![a-z0-9])(?:thumb(?:nails?)?|box(?:2d|3d|arts?)?|covers?)(?![a-z0-9])', path))
            or '/images/novacore/' in '/' + path)


def _read_gamelist(root: Path) -> dict[str, tuple[str, list[str]]]:
    try:
        tree = _xml(root/'gamelist.xml', 64 * 1024 * 1024)
        if tree.tag != 'gameList':
            return {}
    except (OSError, ValueError, ET.ParseError):
        return {}
    entries = {}
    for item in tree.findall('game'):
        relative = _relative(item.findtext('path') or '')
        if not relative:
            continue
        title = (item.findtext('name') or '').strip()
        # RetroBat uses thumbnail for box art and image for screenshots.
        images = [raw] if (raw := item.findtext('thumbnail')) else []
        if (raw := item.findtext('image')) and _box_image_hint(raw):
            images.append(raw)
        entries[relative.casefold()] = (title, images)
    return entries



@lru_cache(maxsize=8)
def _cached_gamelist(root: str, signature: tuple[int, int, int, int]) -> dict[str, tuple[str, list[str]]]:
    return _read_gamelist(Path(root))


def _gamelist(root: Path) -> dict[str, tuple[str, list[str]]]:
    # A direct artwork recheck per game must not reparse large XML per game.
    try:
        path = root/'gamelist.xml'
        if _is_link(path):
            return {}
        info = path.stat()
        signature = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return _cached_gamelist(str(root), signature)
    except OSError:
        return {}


def artwork_destination(game: Game) -> Path:
    """Stable path across drive moves, distinct for identical nested ROM names."""
    relative = _relative(game.relative_path)
    if not relative:
        raise ValueError('Game path must be system-relative.')
    stem = PurePosixPath(relative).stem
    stem = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode('ascii')
    stem = re.sub(r'[^A-Za-z0-9 _-]', '_', stem).strip(' ._-')[:48] or 'game'
    digest = hashlib.sha256((game.system.name + '/' + relative).encode('utf-8')).hexdigest()[:16]
    return game.system.path/'images'/'novacore'/(stem + '-' + digest + '.png')


def _artwork_candidates(game: Game) -> Iterable[str]:
    relative = PurePosixPath(game.relative_path)
    bases = list(dict.fromkeys((relative.with_suffix('').as_posix(), relative.stem)))
    for folder in ('images', 'downloaded_images', 'media/box2d'):
        for base in bases:
            for suffix in ('-thumb', ''):
                for extension in _IMAGE_EXTENSIONS:
                    yield folder + '/' + base + suffix + extension
    # Some layouts keep their images beside each nested ROM directory.
    if relative.parent != PurePosixPath('.'):
        for folder in ('images', 'downloaded_images', 'media/box2d'):
            for suffix in ('-thumb', ''):
                for extension in _IMAGE_EXTENSIONS:
                    yield (relative.parent/folder/(relative.stem + suffix + extension)).as_posix()


def _checked_artwork(path: Path) -> Path | None:
    # Missing images need one stat, not an lstat for every ancestor.
    try:
        return path if path.is_file() and not _has_link_ancestor(path) else None
    except OSError:
        return None


def _local_metadata_path(root: Path, raw: str) -> Path | None:
    """Read-only media may be shared; ROM paths still use strict _relative."""
    raw = raw.strip().replace('\\', '/')
    if not raw or '\x00' in raw or raw.startswith('//') or '://' in raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        # A rooted path with no drive is ambiguous on Windows.
        if os.name == 'nt' and not path.drive:
            return None
    else:
        if re.match(r'^[a-zA-Z]:', raw) or raw.startswith(('~', '/')):
            return None
        # Parent-relative media must remain in this RetroBat installation.
        roms = next((part for part in (root, *root.parents) if part.name.casefold() == 'roms'), None)
        boundary = roms.parent if roms is not None else root
        try:
            base = PurePosixPath(root.relative_to(boundary).as_posix())
        except ValueError:
            return None
        relative = _relative(raw, base, parent=True)
        if not relative:
            return None
        path = boundary/relative
    if any(':' in part for part in path.parts[1:]):
        return None
    if '..' in path.parts and _has_link_ancestor(path):
        return None
    path = Path(os.path.normpath(str(path)))
    if os.name == 'nt':
        # UNC and mapped network artwork can hang a scan on unavailable hosts.
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            return None
    return path


def _remember_artwork(root: Path, files: dict[str, Path]) -> None:
    try:
        info = root.stat()
    except OSError:
        return
    key = str(root)
    images = {key: path for key, path in files.items() if path.suffix.casefold() in _IMAGE_EXTENSIONS}
    with _ARTWORK_SNAPSHOT_LOCK:
        _ARTWORK_SNAPSHOTS[key] = ((info.st_dev, info.st_ino), images)
        _ARTWORK_SNAPSHOTS.move_to_end(key)
        while len(_ARTWORK_SNAPSHOTS) > 256:
            _ARTWORK_SNAPSHOTS.popitem(last=False)


def _artwork_snapshot(root: Path) -> dict[str, Path] | None:
    key = str(root)
    try:
        info = root.stat()
    except OSError:
        return None
    with _ARTWORK_SNAPSHOT_LOCK:
        record = _ARTWORK_SNAPSHOTS.get(key)
        if record is None:
            return None
        if record[0] != (info.st_dev, info.st_ino):
            _ARTWORK_SNAPSHOTS.pop(key, None)
            return None
        _ARTWORK_SNAPSHOTS.move_to_end(key)
        return record[1]


def _find_artwork(game: Game, images: Iterable[str], index: dict[str, Path] | None = None, *, live_destination: bool = False, fresh_index: bool = False) -> Path | None:
    # Fresh explicit XML takes priority over a former scan result.
    for raw in images:
        # The current scan already checked these entries and their parents.
        # Avoid rebuilding/inspecting the same path for every XML thumbnail.
        relative = _relative(raw)
        if index is not None and relative is not None:
            known = index.get(relative.casefold())
            if known is not None:
                if fresh_index:
                    return known
                if (checked := _checked_artwork(known)) is not None:
                    return checked
        path = _local_metadata_path(game.system.path, raw)
        if path is not None:
            if index is not None:
                try:
                    path = index.get(path.relative_to(game.system.path).as_posix().casefold(), path)
                except ValueError:
                    pass
            if fresh_index and index is not None:
                try:
                    known = index.get(path.relative_to(game.system.path).as_posix().casefold())
                except ValueError:
                    known = None
                if known is not None:
                    return known
            checked = _checked_artwork(path)
            if checked is not None:
                return checked
    if game.existing_artwork:
        path = _local_metadata_path(game.system.path, str(game.existing_artwork))
        if path is not None and (checked := _checked_artwork(path)) is not None:
            return checked
    destination = artwork_destination(game)
    if index is None or live_destination:
        if (checked := _checked_artwork(destination)) is not None:
            return checked
    elif (known := index.get(destination.relative_to(game.system.path).as_posix().casefold())) is not None:
        if fresh_index:
            return known
        if (checked := _checked_artwork(known)) is not None:
            return checked
    for relative in _artwork_candidates(game):
        path = index.get(relative.casefold()) if index is not None else game.system.path/relative
        if path is not None:
            if fresh_index:
                return path
            if (checked := _checked_artwork(path)) is not None:
                return checked
    return None


def existing_artwork(game: Game) -> Path | None:
    """Recheck box thumbnails/explicit cover images, then scan-indexed media.

    Conventional media newly added during a run is discovered on the next
    scan. Explicit metadata and new downloads are checked immediately.
    """
    if _has_link_ancestor(game.system.path):
        return None
    relative = _relative(game.relative_path)
    if not relative:
        return None
    _, images = _gamelist(game.system.path).get(relative.casefold(), ('', []))
    return _find_artwork(game, images, _artwork_snapshot(game.system.path), live_destination=True)


def _manifest_references(path: Path) -> list[str]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or _is_link(path) or info.st_size > 4 * 1024 * 1024:
            return []
        raw = path.read_bytes()
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = raw.decode('utf-16')
        else:
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('cp1252')
    except (OSError, UnicodeError):
        return []
    ext = path.suffix.casefold()
    if ext == '.cue':
        return [quoted or plain for quoted, plain in re.findall(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+\S+', text, re.I | re.M)]
    if ext == '.m3u':
        return [line.strip().strip('"') for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if ext == '.ccd':
        return [path.with_suffix('.img').name, path.with_suffix('.sub').name]
    refs = []
    try:
        lines = text.splitlines()
        count = int(lines[0].strip())
        for line in lines[1:]:
            if not line.strip():
                continue
            lexer = shlex.shlex(line, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ''
            lexer.escape = ''
            track = list(lexer)
            if len(track) < 6:
                return []
            refs.append(track[4])
        return refs if 0 < count == len(refs) else []
    except (ValueError, IndexError):
        return []


def _entry_linked(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    # Windows supplies these attributes in scandir's cached find information.
    # POSIX d_type handles links without adding a stat for every regular file.
    if os.name == 'nt':
        info = entry.stat(follow_symlinks=False)
        return bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    return False


def scan_system(system: System, cancelled: Callable[[], bool] = lambda: False) -> list[Game]:
    """Read each relevant directory once and retain its safe file information."""
    root = system.path
    if cancelled() or _has_link_ancestor(root) or not root.is_dir():
        return []
    metadata = _gamelist(root)
    files: dict[str, Path] = {}
    candidates: dict[str, tuple[Path, str]] = {}
    manifests: list[tuple[Path, str]] = []
    # kind is rom, images, or media (where only box2d is conventional artwork).
    stack = [(root, '', 'rom')]
    while stack:
        if cancelled():
            return []
        directory, prefix, kind = stack.pop()
        # A removable drive or another process may replace a queued directory.
        # Recheck only directory ancestry, never every indexed file's ancestry.
        if _has_link_ancestor(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancelled():
                        return []
                    name = entry.name
                    if name.startswith('.'):
                        continue
                    folded = name.casefold()
                    relative = prefix + name
                    key = relative.casefold()
                    extension = '.' + folded.rsplit('.', 1)[1] if '.' in folded else ''
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if _entry_linked(entry):
                                continue
                            if kind == 'rom' and (key in metadata or extension in system.extensions):
                                candidates[key] = (Path(entry.path), relative)
                                continue
                            if kind == 'media':
                                if folded != 'box2d':
                                    continue
                                next_kind = 'images'
                            elif folded in {'videos', 'manuals', 'marquees'}:
                                continue
                            elif folded == 'media':
                                next_kind = 'media'
                            elif kind == 'images' or folded in {'images', 'downloaded_images'}:
                                next_kind = 'images'
                            else:
                                next_kind = 'rom'
                            stack.append((Path(entry.path), relative + '/', next_kind))
                            continue
                        if kind == 'media':
                            continue
                        if kind == 'images':
                            relevant = extension in _IMAGE_EXTENSIONS
                        else:
                            relevant = extension in system.extensions or extension in _MANIFEST_EXTENSIONS or extension in _IMAGE_EXTENSIONS
                        if not relevant or not entry.is_file(follow_symlinks=False) or _entry_linked(entry):
                            continue
                    except OSError:
                        # Unreadable or concurrently removed entries are not games.
                        continue
                    path = Path(entry.path)
                    files[key] = path
                    if kind == 'rom':
                        if extension in system.extensions:
                            candidates[key] = (path, relative)
                        if extension in _MANIFEST_EXTENSIONS:
                            manifests.append((path, relative))
        except OSError:
            continue
    referenced = set()
    for path, relative in manifests:
        if cancelled():
            return []
        parent = PurePosixPath(relative).parent
        for raw in _manifest_references(path):
            reference = _relative(raw, parent, parent=True)
            if reference and reference.casefold() in files:
                referenced.add(reference.casefold())
    games = []
    for key, (path, relative) in candidates.items():
        if cancelled():
            return []
        if key in referenced:
            continue
        title, images = metadata.get(key, ('', []))
        game = Game(system, path, relative, title or path.stem)
        games.append(Game(system, path, relative, game.title, _find_artwork(game, images, files, fresh_index=True)))
    _remember_artwork(root, files)
    return sorted(games, key=lambda game: game.relative_path.casefold())
