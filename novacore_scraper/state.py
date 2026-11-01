"""Portable SQLite recovery state and an OS-held installation writer lock."""
from __future__ import annotations
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import stat
from .models import Game


def is_link(path: Path) -> bool:
    try:
        st = path.lstat()
        return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError:
        return False


def safe_path(root: Path, path: Path) -> Path:
    """Require lexical containment and reject links in every existing component."""
    root, path = root.absolute(), path.absolute()
    relative = path.relative_to(root)
    if '..' in relative.parts:
        raise ValueError('Path escapes its library.')
    for p in (root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts)+1))):
        if is_link(p):
            raise ValueError('Linked/reparse paths cannot be written.')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes its library.')
    return path


def workspace(root: Path) -> Path:
    path = safe_path(root, root/'.novacore-artwork')
    path.mkdir(exist_ok=True)
    return path


class InstallationLock:
    def __init__(self, root: Path):
        self.path = safe_path(root, workspace(root)/'writer.lock')
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError('Another scraper is using this RetroBat library. Close it first.') from exc
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


class State:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        path = safe_path(self.root, workspace(self.root)/'state.sqlite3')
        for suffix in ('-wal', '-shm', '-journal'):
            safe_path(self.root, Path(str(path)+suffix))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('BEGIN')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, system_name TEXT NOT NULL, system_path TEXT NOT NULL,
            relative_path TEXT NOT NULL, status TEXT NOT NULL, artwork TEXT,
            pending INTEGER NOT NULL DEFAULT 0, updated TEXT DEFAULT CURRENT_TIMESTAMP,
            title TEXT NOT NULL DEFAULT '', system_fullname TEXT NOT NULL DEFAULT '')''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)')}
        legacy = 'title' not in columns
        if legacy:
            self.db.execute("ALTER TABLE jobs ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if 'system_fullname' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN system_fullname TEXT NOT NULL DEFAULT ''")
        self.db.execute('''CREATE TABLE IF NOT EXISTS downloads (
            key TEXT PRIMARY KEY, system_name TEXT NOT NULL, system_path TEXT NOT NULL,
            relative_path TEXT NOT NULL, title TEXT NOT NULL, system_fullname TEXT NOT NULL,
            artwork TEXT NOT NULL, updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
        if legacy:
            # Earlier versions used downloaded/complete for both fresh downloads and
            # recovered covers. Preserve this available history without changing keys.
            self.db.create_function('game_title', 1, lambda value: Path(value).stem)
            self.db.execute('UPDATE jobs SET title=game_title(relative_path), system_fullname=system_name')
            self.db.execute('''INSERT OR IGNORE INTO downloads
                (key,system_name,system_path,relative_path,title,system_fullname,artwork,updated)
                SELECT key,system_name,system_path,relative_path,title,system_fullname,artwork,updated
                FROM jobs WHERE artwork IS NOT NULL AND status IN ('downloaded','complete')''')
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def record(self, game: Game, status: str, artwork: Path | None = None,
               pending: bool = False, *, history: bool = False):
        system_path = safe_path(self.root, game.system.path).relative_to(self.root).as_posix()
        if artwork is not None:
            artwork = Path(artwork).absolute()
            safe_path(game.system.path, artwork)
            art = artwork.relative_to(self.root).as_posix()
        else:
            art = None
        if game.relative_path.startswith('/') or '..' in Path(game.relative_path).parts:
            raise ValueError('Unsafe game key.')
        key = system_path + '/' + game.relative_path
        with self.db:
            self.db.execute('''INSERT INTO jobs
                (key,system_name,system_path,relative_path,status,artwork,pending,title,system_fullname)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                status=excluded.status, artwork=COALESCE(excluded.artwork,jobs.artwork),
                pending=CASE WHEN excluded.artwork IS NULL THEN jobs.pending ELSE excluded.pending END,
                title=excluded.title, system_fullname=excluded.system_fullname,
                updated=CURRENT_TIMESTAMP''',
                (key, game.system.name, system_path, game.relative_path, status, art, int(pending),
                 game.title, game.system.fullname))
            if history and art is not None:
                # History has its own artwork path, so a later skipped/cancelled
                # queue result cannot erase an earlier completed download.
                self.db.execute('''INSERT INTO downloads
                    (key,system_name,system_path,relative_path,title,system_fullname,artwork)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    title=excluded.title, system_fullname=excluded.system_fullname,
                    artwork=excluded.artwork, updated=CURRENT_TIMESTAMP''',
                    (key, game.system.name, system_path, game.relative_path,
                     game.title, game.system.fullname, art))

    def downloaded(self, game: Game, artwork: Path, *, history: bool = True):
        self.record(game, 'downloaded', artwork, True, history=history)

    def pending(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE pending=1 ORDER BY system_path,key')]

    def applied(self, keys):
        with self.db:
            self.db.executemany("UPDATE jobs SET pending=0,status='complete',updated=CURRENT_TIMESTAMP WHERE key=?", ((k,) for k in keys))

    @staticmethod
    def downloaded_page(root: Path, query: str = '', offset: int = 0,
                        limit: int = 200) -> tuple[list[dict], int]:
        """Read a bounded history page on the caller's background thread.

        The separate read-only connection can coexist with the coordinator's
        WAL writer and installation lock. Looking at an unused library does
        not create a state directory or migrate its database.
        """
        root = Path(root).absolute()
        path = safe_path(root, root/'.novacore-artwork'/'state.sqlite3')
        if not path.is_file():
            return [], 0
        for suffix in ('-wal', '-shm', '-journal'):
            safe_path(root, Path(str(path)+suffix))
        offset, limit = max(0, int(offset)), min(200, max(0, int(limit)))
        normalize = lambda value: ''.join((value or '').casefold().split())
        terms = [normalize(word) for word in query.split() if normalize(word)]
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            db.create_function('normalize_search', 1, normalize, deterministic=True)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'downloads' in tables:
                source = 'downloads'
                title, fullname = 'title', 'system_fullname'
                filters = []
            elif 'jobs' in tables:
                # Older releases can be inspected before the next scraping run
                # performs the schema migration under the installation lock.
                source = 'jobs'
                columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
                title = 'title' if 'title' in columns else 'relative_path'
                fullname = 'system_fullname' if 'system_fullname' in columns else 'system_name'
                filters = ["artwork IS NOT NULL", "status IN ('downloaded','complete')"]
            else:
                return [], 0
            search = f"normalize_search({title} || ' ' || {fullname} || ' ' || system_name || ' ' || relative_path)"
            filters.extend(f'instr({search}, ?) > 0' for _ in terms)
            where = ' WHERE ' + ' AND '.join(filters) if filters else ''
            total = db.execute(f'SELECT COUNT(*) FROM {source}{where}', terms).fetchone()[0]
            rows = db.execute(f'''SELECT key,system_name,system_path,relative_path,
                {title} AS title,{fullname} AS system_fullname,artwork
                FROM {source}{where} ORDER BY updated DESC,key LIMIT ? OFFSET ?''',
                [*terms, limit, offset]).fetchall()
            result = [dict(row) for row in rows]
            if source == 'jobs' and title == 'relative_path':
                for row in result:
                    row['title'] = Path(row['relative_path']).stem
            return result, total
