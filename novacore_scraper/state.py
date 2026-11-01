"""Portable SQLite recovery state and an OS-held installation writer lock."""
from __future__ import annotations
from pathlib import Path
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
        self.root = root.absolute()
        path = safe_path(root, workspace(root)/'state.sqlite3')
        for suffix in ('-wal', '-shm', '-journal'):
            safe_path(root, Path(str(path)+suffix))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, system_name TEXT NOT NULL, system_path TEXT NOT NULL,
            relative_path TEXT NOT NULL, status TEXT NOT NULL, artwork TEXT,
            pending INTEGER NOT NULL DEFAULT 0, updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def record(self, game: Game, status: str, artwork: Path | None = None, pending: bool = False):
        system_path = safe_path(self.root, game.system.path).relative_to(self.root).as_posix()
        if artwork is not None:
            safe_path(game.system.path, artwork)
            art = artwork.relative_to(self.root).as_posix()
        else:
            art = None
        if game.relative_path.startswith('/') or '..' in Path(game.relative_path).parts:
            raise ValueError('Unsafe game key.')
        key = system_path + '/' + game.relative_path
        with self.db:
            self.db.execute('''INSERT INTO jobs(key,system_name,system_path,relative_path,status,artwork,pending)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                status=excluded.status, artwork=COALESCE(excluded.artwork,jobs.artwork),
                pending=CASE WHEN excluded.artwork IS NULL THEN jobs.pending ELSE excluded.pending END,
                updated=CURRENT_TIMESTAMP''',
                (key, game.system.name, system_path, game.relative_path, status, art, int(pending)))

    def downloaded(self, game: Game, artwork: Path):
        self.record(game, 'downloaded', artwork, True)

    def pending(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE pending=1 ORDER BY system_path,key')]

    def applied(self, keys):
        with self.db:
            self.db.executemany("UPDATE jobs SET pending=0,status='complete',updated=CURRENT_TIMESTAMP WHERE key=?", ((k,) for k in keys))
