"""Portable SQLite recovery state and an OS-held installation writer lock."""
from __future__ import annotations
from pathlib import Path
from contextlib import closing, contextmanager, nullcontext
import csv
import tempfile
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


def _comparison_path(path: Path) -> Path:
    """Compare equivalent Windows extended DOS/UNC paths with one anchor."""
    drive = path.drive
    if drive[:8].casefold() == '\\\\?\\unc\\':
        return type(path)('\\\\' + str(path)[8:])
    if drive.startswith('\\\\?\\') and len(drive) == 6 and drive.endswith(':'):
        return type(path)(str(path)[4:])
    return path


def safe_path(root: Path, path: Path) -> Path:
    """Require lexical containment and reject links in every existing component."""
    root, path = root.absolute(), path.absolute()
    relative = path.relative_to(root)
    if '..' in relative.parts:
        raise ValueError('Path escapes its library.')
    for p in (root, *(root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts)+1))):
        if is_link(p):
            raise ValueError('Linked/reparse paths cannot be written.')
    # Concurrent parent creation can change Windows' missing-path error during
    # resolve(), leaving a \\?\ prefix on only one result. Normalize comparison
    # anchors only; preserve the original path for every filesystem operation.
    if not _comparison_path(path.resolve()).is_relative_to(_comparison_path(root.resolve())):
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


_DIAGNOSTICS = {
    'reason': "TEXT NOT NULL DEFAULT ''", 'detail': "TEXT NOT NULL DEFAULT ''",
    'source': "TEXT NOT NULL DEFAULT ''", 'region': "TEXT NOT NULL DEFAULT ''",
    'match_note': "TEXT NOT NULL DEFAULT ''", 'attempts': 'INTEGER NOT NULL DEFAULT 0',
    'retryable': 'INTEGER NOT NULL DEFAULT 0',
    'source_trace': "TEXT NOT NULL DEFAULT ''",
}


class State:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        path = safe_path(self.root, workspace(self.root)/'state.sqlite3')
        for suffix in ('-wal', '-shm', '-journal'):
            safe_path(self.root, Path(str(path)+suffix))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self._batch_depth = 0
        try:
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
            for name in ('title', 'system_fullname'):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            self.db.execute('''CREATE TABLE IF NOT EXISTS downloads (
                key TEXT PRIMARY KEY, system_name TEXT NOT NULL, system_path TEXT NOT NULL,
                relative_path TEXT NOT NULL, title TEXT NOT NULL, system_fullname TEXT NOT NULL,
                artwork TEXT NOT NULL, updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
            for table in ('jobs', 'downloads'):
                columns = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
                for name, definition in _DIAGNOSTICS.items():
                    if name not in columns:
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            if legacy:
                self.db.create_function('game_title', 1, lambda value: Path(value).stem)
                self.db.execute('UPDATE jobs SET title=game_title(relative_path), system_fullname=system_name')
                self.db.execute('''INSERT OR IGNORE INTO downloads
                    (key,system_name,system_path,relative_path,title,system_fullname,artwork,updated)
                    SELECT key,system_name,system_path,relative_path,title,system_fullname,artwork,updated
                    FROM jobs WHERE artwork IS NOT NULL AND status IN ('downloaded','complete')''')
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def batch(self):
        """Commit a coordinator's bounded group of state changes together."""
        self._batch_depth += 1
        try:
            with self.db:
                yield
        finally:
            self._batch_depth -= 1

    def record(self, game: Game, status: str, artwork: Path | None = None,
               pending: bool = False, *, history: bool = False, reason=None, detail=None,
               source=None, region=None, match_note=None, attempts=None, retryable=None, source_trace=None):
        system_path = safe_path(self.root, game.system.path).relative_to(self.root).as_posix()
        if artwork is not None:
            artwork = Path(artwork).absolute()
            safe_path(game.system.path, artwork)
            art = artwork.relative_to(self.root).as_posix()
        else:
            art = None
        if game.relative_path.startswith('/') or '..' in Path(game.relative_path).parts:
            raise ValueError('Unsafe game key.')
        values = dict(key=system_path+'/'+game.relative_path, system_name=game.system.name,
                      system_path=system_path, relative_path=game.relative_path, status=status,
                      artwork=art, pending=int(pending), title=game.title,
                      system_fullname=game.system.fullname, reason=reason, detail=detail,
                      source=source, region=region, match_note=match_note, attempts=attempts,
                      source_trace=source_trace,
                      retryable=None if retryable is None else int(retryable))
        extra = ','.join(_DIAGNOSTICS)
        defaults = {name: ('0' if name in {'attempts', 'retryable'} else "''") for name in _DIAGNOSTICS}
        bound = ','.join(f'COALESCE(:{name}, {default})' for name, default in defaults.items())
        updates = ','.join(f'{name}=COALESCE(:{name},jobs.{name})' for name in _DIAGNOSTICS)
        with (self.db if not self._batch_depth else nullcontext()):
            self.db.execute(f'''INSERT INTO jobs
                (key,system_name,system_path,relative_path,status,artwork,pending,title,system_fullname,{extra})
                VALUES(:key,:system_name,:system_path,:relative_path,:status,:artwork,:pending,:title,:system_fullname,{bound})
                ON CONFLICT(key) DO UPDATE SET status=excluded.status,
                artwork=COALESCE(excluded.artwork,jobs.artwork),
                pending=CASE WHEN excluded.artwork IS NULL THEN jobs.pending ELSE excluded.pending END,
                title=excluded.title, system_fullname=excluded.system_fullname,
                {updates},updated=CURRENT_TIMESTAMP''', values)
            if history and art is not None:
                history_updates = ','.join(f'{name}=COALESCE(:{name},downloads.{name})' for name in _DIAGNOSTICS)
                self.db.execute(f'''INSERT INTO downloads
                    (key,system_name,system_path,relative_path,title,system_fullname,artwork,{extra})
                    VALUES(:key,:system_name,:system_path,:relative_path,:title,:system_fullname,:artwork,{bound})
                    ON CONFLICT(key) DO UPDATE SET title=excluded.title,
                    system_fullname=excluded.system_fullname,artwork=excluded.artwork,
                    {history_updates},updated=CURRENT_TIMESTAMP''', values)

    def downloaded(self, game: Game, artwork: Path, *, history: bool = True, **diagnostics):
        self.record(game, 'downloaded', artwork, True, history=history, **diagnostics)

    def previous(self, game: Game):
        system_path = safe_path(self.root, game.system.path).relative_to(self.root).as_posix()
        row = self.db.execute('SELECT * FROM jobs WHERE key=?',
                              (system_path+'/'+game.relative_path,)).fetchone()
        return dict(row) if row is not None else None

    def pending(self) -> list[dict]:
        return [dict(row) for row in self.db.execute('SELECT * FROM jobs WHERE pending=1 ORDER BY system_path,key')]

    def applied(self, keys):
        with self.db:
            self.db.executemany('''UPDATE jobs SET pending=0,
                status=CASE WHEN status='downloaded' THEN 'complete' ELSE status END,
                updated=CURRENT_TIMESTAMP WHERE key=?''', ((k,) for k in keys))

    @staticmethod
    def _database(root):
        root = Path(root).absolute()
        path = safe_path(root, root/'.novacore-artwork'/'state.sqlite3')
        for suffix in ('-wal', '-shm', '-journal'):
            safe_path(root, Path(str(path)+suffix))
        return path

    @staticmethod
    def _select(db, table):
        """Project current and legacy schemas into one read-only report contract."""
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        fields = ['key', 'system_name', 'system_path', 'relative_path', 'artwork', 'updated']
        for name, fallback in [('title', 'relative_path'), ('system_fullname', 'system_name')]:
            fields.append(f'{name if name in columns else fallback} AS {name}')
        if table == 'downloads':
            fields.append("'completed' AS status")
        else:
            fields.append("CASE WHEN status IN ('downloaded','complete') THEN 'completed' ELSE status END AS status")
        for name in _DIAGNOSTICS:
            fallback = '0' if name in {'attempts', 'retryable'} else "''"
            fields.append(f'{name if name in columns else fallback} AS {name}')
        return f'SELECT {",".join(fields)} FROM {table}', 'title' not in columns

    @staticmethod
    def _page(root, query, offset, limit, *, history, statuses=None):
        path = State._database(root)
        if not path.is_file():
            return [], 0
        offset, limit = max(0, int(offset)), min(200, max(0, int(limit)))
        normalize = lambda value: ''.join((value or '').casefold().split())
        terms = [normalize(word) for word in query.split() if normalize(word)]
        if isinstance(statuses, str):
            statuses = {statuses}
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            db.create_function('normalize_search', 1, normalize, deterministic=True)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = 'downloads' if history and 'downloads' in tables else 'jobs'
            if table not in tables:
                return [], 0
            select, legacy = State._select(db, table)
            search = "normalize_search(title || ' ' || system_fullname || ' ' || system_name || ' ' || relative_path || ' ' || reason || ' ' || detail || ' ' || source)"
            filters = [f'instr({search}, ?) > 0' for _ in terms]
            params = list(terms)
            if history and table == 'jobs':
                filters += ["artwork IS NOT NULL", "status='completed'"]
            if statuses is not None:
                statuses = sorted(set(statuses))
                if not statuses:
                    return [], 0
                filters.append('status IN ('+','.join('?' for _ in statuses)+')')
                params.extend(statuses)
            where = ' WHERE '+' AND '.join(filters) if filters else ''
            source = f'({select})'
            total = db.execute(f'SELECT COUNT(*) FROM {source}{where}', params).fetchone()[0]
            rows = [dict(row) for row in db.execute(f'SELECT * FROM {source}{where} ORDER BY updated DESC,key LIMIT ? OFFSET ?', [*params, limit, offset])]
            if legacy:
                for row in rows:
                    row['title'] = Path(row['relative_path']).stem
            return rows, total

    @staticmethod
    def downloaded_page(root: Path, query: str = '', offset: int = 0, limit: int = 200):
        """Read bounded completed-download history without opening a writer."""
        return State._page(root, query, offset, limit, history=True)

    @staticmethod
    def jobs_page(root: Path, query: str = '', offset: int = 0, limit: int = 200, *, statuses=None):
        return State._page(root, query, offset, limit, history=False, statuses=statuses)

    @staticmethod
    def _report_destination(root: Path, destination: Path, db=None):
        """A report may replace a CSV report, never installation/library data."""
        root, destination = Path(root).absolute(), Path(destination).absolute()
        if destination.suffix.casefold() != '.csv':
            raise ValueError('Choose a CSV report filename ending in .csv.')
        # Reject links/reparse points in all destination ancestors, including
        # external export directories. Keep the original path for publication.
        safe_path(Path(destination.anchor), destination)
        compared = _comparison_path(destination.resolve())
        compared_root = _comparison_path(root.resolve())
        if compared.is_relative_to(compared_root):
            relative = compared.relative_to(compared_root)
            protected = {'roms', 'system', 'emulationstation', '.novacore-artwork',
                         'media', 'metadata', 'images', 'videos', 'manuals', 'screenshots', 'gamelists'}
            if any(part.casefold() in protected for part in relative.parts[:-1]):
                raise ValueError('Choose a report destination outside ROM, media, metadata and system folders.')
        if db is not None:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('jobs', 'downloads'):
                if table not in tables:
                    continue
                for row in db.execute(f'SELECT DISTINCT system_path FROM {table}'):
                    system = safe_path(root, root/row[0])
                    if compared.is_relative_to(_comparison_path(system.resolve())):
                        raise ValueError('Choose a report destination outside game library folders.')
        return destination

    @staticmethod
    def export_report(root: Path, destination: Path) -> int:
        """Stream a consistent read-only snapshot to an atomically replaced CSV."""
        path = State._database(root)
        destination = State._report_destination(root, destination)
        fields = ['title', 'console', 'status', 'reason', 'detail', 'source', 'region',
                  'match_note', 'artwork', 'attempts', 'source_trace']
        temporary = None
        count = 0
        reader = (closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10))
                  if path.is_file() else nullcontext(None))
        try:
            with reader as db:
                if db is not None:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA query_only=ON')
                    db.execute('BEGIN')
                    State._report_destination(root, destination, db)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                        prefix='.'+destination.name+'.', suffix='.tmp', dir=destination.parent,
                        delete=False) as handle:
                    temporary = Path(handle.name)
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    if db is not None:
                        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if 'jobs' in tables:
                            select, legacy = State._select(db, 'jobs')
                            for row in db.execute(select+' ORDER BY system_name,key'):
                                values = {name: row[name] if row[name] is not None else '' for name in fields if name != 'console'}
                                values['console'] = row['system_fullname'] or row['system_name']
                                if legacy:
                                    values['title'] = Path(row['relative_path']).stem
                                writer.writerow(values)
                                count += 1
                    handle.flush()
                    os.fsync(handle.fileno())
                State._report_destination(root, destination, db)
                os.replace(temporary, destination)
            return count
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
