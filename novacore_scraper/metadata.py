"""Conservative, deferred updates to EmulationStation's canonical gamelist.

Each call reads a fresh snapshot. Existing artwork wins, and an XML conflict is
reported to the caller so its durable queue can retry later. The final revision
check reduces races with other editors; no portable API can make a comparison
and rename indivisible for arbitrary, non-cooperating external writers.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import ntpath
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tempfile
from typing import Callable
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError

from .models import System


MAX_XML_BYTES = 128 * 1024 * 1024
_DECLARATION = re.compile(rb'<!\s*(?:DOCTYPE|ENTITY)\b', re.IGNORECASE)
_VARIABLE = re.compile(r'%[^%]+%|\$(?:[A-Za-z_][A-Za-z_0-9]*|\{[^}]+\})')


class MetadataError(Exception):
    """The gamelist could not safely be updated; keep its updates pending."""


class ConflictError(MetadataError):
    """Another writer changed the gamelist after it was read."""


@dataclass(frozen=True)
class _Snapshot:
    data: bytes
    revision: tuple[int, ...]
    digest: bytes
    mode: int


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _cross_api_revision(info: os.stat_result) -> tuple[int, ...]:
    # CPython 3.12+ on Windows can report creation time through lstat but
    # change time through fstat. Compare ctime only within the same API;
    # retain identity, size and mtime when checking the descriptor's path.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions and other reparse points need the same treatment as
    # symlinks. pathlib.is_symlink alone does not catch every reparse point.
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, 'st_file_attributes', 0) &
                 getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def _safe_path(root: Path, path: Path, *, missing: bool = False) -> os.stat_result | None:
    """Reject paths outside root and links in any component within root."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise MetadataError('Path is outside the system directory') from exc
    if any(part in ('.', '..') for part in relative.parts):
        raise MetadataError('Unsafe path component')
    current = root
    components = [root, *(root.joinpath(*relative.parts[:i])
                          for i in range(1, len(relative.parts) + 1))]
    info = None
    for current in components:
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise MetadataError('Required file or directory does not exist') from None
        if _is_link(info):
            raise MetadataError('Links and reparse points are not safe metadata paths')
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise MetadataError('A path component is not a directory')
    return info


def _validate_updates(root: Path, updates: dict[str, Path]) -> dict[str, str]:
    references = {}
    for key, value in updates.items():
        if (not isinstance(key, str) or not key or
                '\\' in key or ':' in key or key.startswith('/') or
                any(ord(character) < 32 for character in key) or
                any(part in ('', '.', '..') for part in key.split('/'))):
            raise MetadataError('Game keys must be safe system-relative paths')
        if not _xml_characters(key):
            raise MetadataError('The game path cannot be represented in XML')
        game_path = root.joinpath(*key.split('/'))
        game_info = _safe_path(root, game_path, missing=True)
        if (game_info is not None and not
                (stat.S_ISREG(game_info.st_mode) or stat.S_ISDIR(game_info.st_mode))):
            raise MetadataError('Game path is not a file or playable directory')
        artwork = Path(value)
        if not artwork.is_absolute() or '..' in artwork.parts:
            raise MetadataError('Artwork must have a safe absolute path')
        if not _xml_characters(str(artwork)):
            raise MetadataError('The artwork path cannot be represented in XML')
        info = _safe_path(root, artwork)
        if info is None or not stat.S_ISREG(info.st_mode):
            raise MetadataError('Artwork is not a regular file')
        references[key] = './' + artwork.relative_to(root).as_posix()
    return references


def _xml_characters(value: str) -> bool:
    return all(character in '\t\n\r' or 0x20 <= ord(character) <= 0xD7FF or
               0xE000 <= ord(character) <= 0xFFFD or 0x10000 <= ord(character) <= 0x10FFFF
               for character in value)


def _read_snapshot(root: Path, path: Path) -> _Snapshot | None:
    initial = _safe_path(root, path, missing=True)
    if initial is not None and not stat.S_ISREG(initial.st_mode):
        raise MetadataError('The gamelist is not a regular file')
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
             getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if initial is not None:
            raise ConflictError('The gamelist disappeared while it was being read') from None
        return None
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        before = os.fstat(stream.fileno())
        if _is_link(before) or not stat.S_ISREG(before.st_mode):
            raise MetadataError('The gamelist is not a regular file')
        if before.st_size > MAX_XML_BYTES:
            raise MetadataError('The gamelist exceeds the XML size limit')
        data = stream.read(MAX_XML_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_XML_BYTES:
        raise MetadataError('The gamelist exceeds the XML size limit')
    current = _safe_path(root, path, missing=True)
    if (initial is None or current is None or
            _revision(initial) != _revision(current) or
            _revision(before) != _revision(after) or
            _cross_api_revision(initial) != _cross_api_revision(before) or
            _cross_api_revision(after) != _cross_api_revision(current) or
            len(data) != after.st_size):
        raise ConflictError('The gamelist changed while it was being read')
    return _Snapshot(data, _revision(current), hashlib.sha256(data).digest(),
                     stat.S_IMODE(after.st_mode))


def _assert_current(root: Path, path: Path, original: _Snapshot | None) -> None:
    current = _read_snapshot(root, path)
    if original is None:
        unchanged = current is None
    else:
        unchanged = (current is not None and original.revision == current.revision and
                     original.digest == current.digest)
    if not unchanged:
        raise ConflictError('Another writer changed the gamelist; updates remain pending')


def _parse(data: bytes | None) -> minidom.Document:
    if data is None:
        document = minidom.Document()
        document.appendChild(document.createElement('gameList'))
        return document
    # Removing zero bytes also catches UTF-16/32 declaration spelling before
    # Expat sees it. XML's other supported encodings preserve ASCII markup.
    if _DECLARATION.search(data.replace(b'\x00', b'')):
        raise MetadataError('DOCTYPE and entity declarations are not permitted')
    try:
        document = minidom.parseString(data)
    except (ExpatError, ValueError, LookupError) as exc:
        raise MetadataError('The gamelist XML is malformed') from exc
    if document.documentElement.tagName != 'gameList':
        document.unlink()
        raise MetadataError('Expected a gameList XML root')
    return document


def _children(element: minidom.Element, tag: str) -> list[minidom.Element]:
    return [child for child in element.childNodes
            if child.nodeType == Node.ELEMENT_NODE and child.tagName == tag]


def _text(element: minidom.Element) -> str:
    return ''.join(child.data for child in element.childNodes
                   if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE)).strip()


def _game_key(reference: str, root: Path) -> str | None:
    reference = reference.replace('\\', '/')
    if not reference:
        return None
    path = Path(reference)
    if path.is_absolute():
        try:
            reference = path.relative_to(root).as_posix()
        except ValueError:
            return None
    elif ntpath.isabs(reference):
        return None
    key = posixpath.normpath(reference)
    if key == '..' or key.startswith('../') or key.startswith('/') or ':' in key:
        return None
    return key


def _reference_is_valid(element: minidom.Element, root: Path) -> bool:
    # Nested/custom syntax is unknown to this application and must survive.
    if any(child.nodeType == Node.ELEMENT_NODE for child in element.childNodes):
        return True
    reference = _text(element)
    if not reference:
        return False
    # Windows expanduser can invent a sibling profile for an unknown explicit
    # username. That is not proof the user's original reference is broken.
    if reference.startswith('~') and len(reference) > 1 and reference[1] not in '/\\':
        return True
    expanded = os.path.expanduser(os.path.expandvars(reference))
    # Do not destroy remote paths, foreign drive letters, or unavailable custom
    # variable conventions simply because this host cannot resolve them.
    if ((re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', expanded) and
         not ntpath.splitdrive(expanded)[0]) or
            expanded.startswith('~') or _VARIABLE.search(expanded) or '\x00' in expanded or
            (ntpath.splitdrive(expanded)[0] and not Path(expanded).is_absolute()) or
            (ntpath.isabs(expanded) and not Path(expanded).is_absolute())):
        return True
    path = Path(expanded.replace('\\', os.sep))
    if not path.is_absolute():
        path = root / path
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        # A missing file on an available drive is broken. An unavailable
        # custom drive or UNC share cannot establish that the art is missing.
        if path.is_absolute() and path.anchor:
            try:
                Path(path.anchor).stat()
            except (OSError, ValueError):
                return True
        return False
    except (OSError, ValueError):
        # Permission errors, unavailable network shares and unfamiliar path
        # syntax are not proof that the user's reference is broken.
        return True


def _has_valid_field(game: minidom.Element, root: Path, tag: str) -> bool:
    return any(_reference_is_valid(element, root)
               for element in _children(game, tag))


def _set_field(document: minidom.Document, game: minidom.Element,
               tag: str, value: str) -> None:
    existing = _children(game, tag)
    if existing:
        element = existing[0]
        for child in list(element.childNodes):
            if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
                element.removeChild(child)
                child.unlink()
    else:
        element = document.createElement(tag)
        game.appendChild(element)
    element.appendChild(document.createTextNode(value))


def _temporary(root: Path, data: bytes, mode: int | None = None) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix='.novacore-', suffix='.tmp', dir=root)
    path = Path(filename)
    try:
        try:
            stream = os.fdopen(descriptor, 'wb')
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            if mode is not None:
                os.chmod(path, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _sync_directory(root: Path) -> None:
    # Windows does not expose portable directory fsync. On filesystems that
    # support it, persist the directory entry after the atomic rename/link.
    descriptor = None
    try:
        descriptor = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _backup(root: Path, path: Path, snapshot: _Snapshot) -> None:
    backup = path.with_name(path.name + '.novacore.bak')
    existing = _safe_path(root, backup, missing=True)
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode):
            raise MetadataError('The gamelist backup is not a regular file')
        return
    temporary = _temporary(root, snapshot.data, snapshot.mode)
    try:
        try:
            # A hard link publishes the already fsynced complete backup, and
            # fails atomically if an original backup already exists.
            os.link(temporary, backup)
        except FileExistsError:
            _safe_path(root, backup)
        except OSError as exc:
            # Windows rename refuses to overwrite an existing destination,
            # unlike POSIX rename, and supports drives without hard links.
            if os.name != 'nt' or exc.errno not in (errno.EPERM, errno.EACCES,
                                                   errno.ENOTSUP, errno.EXDEV,
                                                   errno.EINVAL, errno.ENOSYS):
                raise
            try:
                os.rename(temporary, backup)
            except FileExistsError:
                _safe_path(root, backup)
        _sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def merge_artwork(system: System, updates: dict[str, Path],
                  running: Callable[[], bool]) -> set[str]:
    """Fill missing artwork, returning keys that may leave the pending queue.

    Keys are slash-separated system-relative ROM paths without ``./``. Artwork
    is an existing absolute regular file within the system directory. Valid
    existing thumbnails are accepted without any file write. Valid images
    (the RetroBat screenshot slot) survive while a missing thumbnail is added.
    A process gate returns an empty set; any error, including a revision
    conflict, leaves the caller responsible for retaining the whole batch.
    """
    if running() or not updates:
        return set()
    temporary = None
    document = None
    try:
        root = Path(system.path)
        if not root.is_absolute() or '..' in root.parts:
            raise MetadataError('The system directory must be a safe absolute path')
        info = _safe_path(root, root)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise MetadataError('The system directory does not exist')
        references = _validate_updates(root, updates)
        path = root / 'gamelist.xml'
        original = _read_snapshot(root, path)
        document = _parse(original.data if original is not None else None)
        container = document.documentElement
        index: dict[str, list[minidom.Element]] = {}
        for game in _children(container, 'game'):
            paths = _children(game, 'path')
            if paths:
                key = _game_key(_text(paths[0]), root)
                if key is not None:
                    index.setdefault(key.casefold(), []).append(game)
        accepted = set()
        changed = False
        for key, reference in references.items():
            games = index.get(key.casefold(), [])
            if any(_has_valid_field(game, root, 'thumbnail') for game in games):
                accepted.add(key)
                continue
            if not games:
                game = document.createElement('game')
                container.appendChild(game)
                _set_field(document, game, 'path', './' + key)
                game_path = root.joinpath(*key.split('/'))
                name = PurePosixPath(key).name if game_path.is_dir() else PurePosixPath(key).stem
                _set_field(document, game, 'name', name)
                games = [game]
                index[key.casefold()] = games
            for game in games:
                _set_field(document, game, 'thumbnail', reference)
                if not _has_valid_field(game, root, 'image'):
                    _set_field(document, game, 'image', reference)
            accepted.add(key)
            changed = True
        if not changed:
            return accepted
        data = document.toxml(encoding='utf-8')
        if len(data) > MAX_XML_BYTES:
            raise MetadataError('The updated gamelist exceeds the XML size limit')
        temporary = _temporary(root, data, original.mode if original else None)
        if running():
            return set()
        _assert_current(root, path, original)
        if original is not None:
            _backup(root, path, original)
        # Backups can take time for large gamelists; check both owners again
        # immediately before replacing the canonical metadata.
        if running():
            return set()
        _assert_current(root, path, original)
        os.replace(temporary, path)
        _sync_directory(root)
        return accepted
    except MetadataError:
        raise
    except (OSError, ValueError) as exc:
        raise MetadataError('Unable to safely update the gamelist') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if document is not None:
            document.unlink()
