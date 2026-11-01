"""Small console-level Tk interface; background work never calls Tk."""
from __future__ import annotations

from dataclasses import replace
import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .engine import ScrapeEngine
from .library import detect_installations, discover_systems, scan_system, validate_retrobat
from .models import Game, System
from .providers import LibretroProvider, ScreenScraperProvider
from .settings import Settings, data_directory, load_settings, save_settings

LOG = logging.getLogger(__name__)
_CREDENTIALS = (
    ('devid', 'Developer ID', 'NOVACORE_SS_DEVID'),
    ('devpassword', 'Developer password', 'NOVACORE_SS_DEVPASSWORD'),
    ('username', 'Account username', 'NOVACORE_SS_USERNAME'),
    ('password', 'Account password', 'NOVACORE_SS_PASSWORD'),
)


class ConsoleSelection:
    """Selection belongs to system IDs, independently of visible filtered rows."""

    def __init__(self, selected: list[str] | None = None):
        self.systems: list[System] = []
        self.selected: set[str] | None = None if selected is None else set(selected)

    def replace_systems(self, systems: list[System]) -> None:
        self.systems = list(systems)
        names = {system.name for system in systems}
        self.selected = names if self.selected is None else self.selected & names

    def visible(self, query: str = '') -> list[System]:
        query = query.strip().casefold()
        return [system for system in self.systems
                if not query or query in system.name.casefold() or query in system.fullname.casefold()]

    def toggle(self, name: str) -> None:
        if name not in {system.name for system in self.systems}:
            return
        if self.selected is None:
            self.selected = set()
        if name in self.selected:
            self.selected.remove(name)
        else:
            self.selected.add(name)

    def select_visible(self, query: str, selected: bool) -> None:
        if self.selected is None:
            self.selected = set()
        names = {system.name for system in self.visible(query)}
        if selected:
            self.selected.update(names)
        else:
            self.selected.difference_update(names)

    def games(self, games_by_system: dict[str, list[Game]]) -> list[Game]:
        return [game for system in self.systems if system.name in (self.selected or ())
                for game in games_by_system.get(system.name, ())]


class App:
    """All public actions and widget updates run on the thread that owns root."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.credentials = {key: os.environ.get(env, '') for key, _label, env in _CREDENTIALS}
        self.retrobat_root: Path | None = None
        self.engine: ScrapeEngine | None = None
        self.selection = ConsoleSelection(self.settings.selected_systems)
        self.games_by_system: dict[str, list[Game]] = {}
        self._events: queue.Queue = queue.Queue(maxsize=4)
        self._scan_lock = threading.Lock()
        self._latest_scan: tuple[int, int, str] | None = None
        self._cancel_scan = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._worker_kind = ''
        self._generation = 0
        self._closing = False
        self._filter_after: str | None = None
        self._last_pending_check = 0.0
        self._previous_pending = 0
        self._settings_window: tk.Toplevel | None = None
        self._smoke_test = bool(getattr(root, '_novacore_smoke_test', False))
        self.folder_var = tk.StringVar(value=self.settings.retrobat_folder)
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value='Choose your RetroBat folder to scan its consoles.')
        self.state_var = tk.StringVar(value='Idle')
        self.progress_var = tk.StringVar(value='0 / 0')
        self.selection_var = tk.StringVar(value='0 consoles selected · 0 games')
        self.counts_var = tk.StringVar(value='Downloaded: 0   Existing: 0   Missing: 0   Failed: 0   Queued metadata: 0')
        self.provider_var = tk.StringVar()
        self._build()
        self._refresh_provider()
        self._refresh_buttons()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self._poll)
        if not self._smoke_test:
            self.root.after(150, self.autodetect)

    def _build(self) -> None:
        self.root.title('NovaCore Artwork Scraper')
        self.root.geometry('920x650')
        self.root.minsize(760, 540)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main = ttk.Frame(self.root, padding=16)
        main.grid(sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)
        ttk.Label(main, text='NovaCore Artwork Scraper', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(main, text='Fill in missing box artwork in your RetroBat library.').grid(row=1, column=0, sticky='w', pady=(3, 14))

        folder = ttk.LabelFrame(main, text='RetroBat installation', padding=10)
        folder.grid(row=2, column=0, sticky='ew')
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.folder_var, state='readonly').grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.browse_button = ttk.Button(folder, text='Browse…', command=self.browse)
        self.browse_button.grid(row=0, column=1, padx=(0, 6))
        self.detect_button = ttk.Button(folder, text='Autodetect', command=self.autodetect)
        self.detect_button.grid(row=0, column=2, padx=(0, 6))
        self.scan_button = ttk.Button(folder, text='Scan library', command=self.scan)
        self.scan_button.grid(row=0, column=3)

        tools = ttk.Frame(main)
        tools.grid(row=3, column=0, sticky='ew', pady=(14, 7))
        tools.columnconfigure(1, weight=1)
        ttk.Label(tools, text='Filter consoles:').grid(row=0, column=0, padx=(0, 8))
        self.filter_entry = ttk.Entry(tools, textvariable=self.filter_var)
        self.filter_entry.grid(row=0, column=1, sticky='ew')
        self.filter_var.trace_add('write', self._filter_changed)
        self.select_button = ttk.Button(tools, text='Select visible', command=lambda: self._select_visible(True))
        self.select_button.grid(row=0, column=2, padx=(10, 6))
        self.clear_button = ttk.Button(tools, text='Clear visible', command=lambda: self._select_visible(False))
        self.clear_button.grid(row=0, column=3)

        table_frame = ttk.Frame(main)
        table_frame.grid(row=4, column=0, sticky='nsew')
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_frame, columns=('use', 'console', 'games', 'existing', 'missing'), show='headings', selectmode='browse')
        for key, heading, width, anchor in (
            ('use', 'Use', 48, 'center'), ('console', 'Console', 360, 'w'),
            ('games', 'Games', 85, 'e'), ('existing', 'Artwork found', 115, 'e'),
            ('missing', 'Needs artwork', 115, 'e'),
        ):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=width if key != 'console' else 160,
                             stretch=key == 'console', anchor=anchor)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<Button-1>', self._click_row)
        self.tree.bind('<Double-1>', self._double_click_row)
        self.tree.bind('<space>', self._space_row)
        ttk.Label(main, textvariable=self.selection_var).grid(row=5, column=0, sticky='w', pady=(7, 12))

        actions = ttk.Frame(main)
        actions.grid(row=6, column=0, sticky='ew')
        actions.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(actions, text='Start scraping', command=self.start)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.pause_button = ttk.Button(actions, text='Pause', command=self.pause_resume)
        self.pause_button.grid(row=0, column=1, padx=(0, 6))
        self.stop_button = ttk.Button(actions, text='Stop', command=self.stop)
        self.stop_button.grid(row=0, column=2)
        ttk.Label(actions, textvariable=self.provider_var).grid(row=0, column=4, sticky='e', padx=12)
        self.settings_button = ttk.Button(actions, text='Settings…', command=self.show_settings)
        self.settings_button.grid(row=0, column=5)

        ttk.Separator(main).grid(row=7, column=0, sticky='ew', pady=12)
        progress_frame = ttk.Frame(main)
        progress_frame.grid(row=8, column=0, sticky='ew')
        progress_frame.columnconfigure(1, weight=1)
        ttk.Label(progress_frame, textvariable=self.state_var, width=13).grid(row=0, column=0, sticky='w')
        self.progress = ttk.Progressbar(progress_frame, maximum=1, value=0)
        self.progress.grid(row=0, column=1, sticky='ew')
        ttk.Label(progress_frame, textvariable=self.progress_var, width=18, anchor='e').grid(row=0, column=2, padx=(8, 0))
        ttk.Label(main, textvariable=self.counts_var).grid(row=9, column=0, sticky='w', pady=(8, 4))
        self.status_label = ttk.Label(main, textvariable=self.status_var, wraplength=850)
        self.status_label.grid(row=10, column=0, sticky='ew')
        self.root.bind('<Configure>', lambda _event: self.status_label.configure(wraplength=max(600, self.root.winfo_width()-40)))

    @property
    def busy(self) -> bool:
        # A finished worker remains busy until its result is applied on this thread.
        return bool(self._worker_kind or (self.engine and self.engine.active))

    def _refresh_buttons(self) -> None:
        available = not self.busy and not self._closing
        for button in (self.browse_button, self.detect_button, self.settings_button):
            button.configure(state='normal' if available else 'disabled')
        self.scan_button.configure(state='normal' if available and self.retrobat_root else 'disabled')
        has_games = any(self.games_by_system.get(name) for name in (self.selection.selected or ()))
        self.start_button.configure(state='normal' if available and self.retrobat_root and has_games else 'disabled')
        for button in (self.select_button, self.clear_button):
            button.configure(state='normal' if available and self.selection.systems else 'disabled')
        active = bool(self.engine and self.engine.active)
        paused = active and self.engine.snapshot().state == 'Paused'
        stopping = active and self.engine.snapshot().state == 'Stopping'
        self.pause_button.configure(text='Resume' if paused else 'Pause', state='normal' if active and not stopping and not self._closing else 'disabled')
        cancellable = active or (self.busy and self._worker_kind in ('scan', 'detect'))
        self.stop_button.configure(state='normal' if cancellable and not stopping and not self._closing else 'disabled')

    def _refresh_provider(self) -> None:
        name = 'ScreenScraper' if self.settings.provider == 'screenscraper' else 'Libretro'
        self.provider_var.set(f'{name} · {self.settings.region.upper()} · box artwork')

    def _filter_changed(self, *_args) -> None:
        if self._filter_after:
            self.root.after_cancel(self._filter_after)
        self._filter_after = self.root.after(150, self._render_systems)

    def _render_systems(self) -> None:
        self._filter_after = None
        focused = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for system in self.selection.visible(self.filter_var.get()):
            games = self.games_by_system.get(system.name, ())
            existing = sum(game.existing_artwork is not None for game in games)
            self.tree.insert('', 'end', iid=system.name,
                             values=('[x]' if system.name in (self.selection.selected or ()) else '[ ]',
                                     system.fullname, len(games), existing, len(games)-existing))
        if focused and self.tree.exists(focused):
            self.tree.focus(focused)
            self.tree.selection_set(focused)
        count = len(self.selection.selected or ())
        games = sum(len(self.games_by_system.get(name, ())) for name in (self.selection.selected or ()))
        visible = len(self.tree.get_children())
        self.selection_var.set(f'{count} consoles selected · {games:,} games · {visible} of {len(self.selection.systems)} consoles shown')
        self._refresh_buttons()

    def _toggle(self, name: str) -> None:
        if self.busy or self._closing:
            return
        self.selection.toggle(name)
        self._render_systems()

    def _click_row(self, event) -> None:
        if self.tree.identify_column(event.x) == '#1':
            name = self.tree.identify_row(event.y)
            if name:
                self._toggle(name)

    def _double_click_row(self, event) -> None:
        # The first column already toggles on its single-click binding.
        if self.tree.identify_column(event.x) != '#1':
            name = self.tree.identify_row(event.y)
            if name:
                self._toggle(name)

    def _space_row(self, _event=None) -> str:
        self._toggle(self.tree.focus())
        return 'break'

    def _select_visible(self, selected: bool) -> None:
        if not self.busy and not self._closing:
            self.selection.select_visible(self.filter_var.get(), selected)
            self._render_systems()

    def _post(self, event) -> None:
        """Only bounded result messages enter this queue; scan progress is shared latest."""
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            self._events.put_nowait(event)

    def _run_background(self, kind: str, operation) -> None:
        self._generation += 1
        generation = self._generation
        self._worker_kind = kind

        def run():
            try:
                result = operation()
                self._post((generation, kind, result, None))
            except Exception as exc:
                LOG.warning('%s failed (%s)', kind, type(exc).__name__)
                message = str(exc) if kind in ('scan', 'detect') else 'Could not apply pending metadata. Check that the installation is writable.'
                self._post((generation, kind, None, message))

        self._worker_thread = threading.Thread(target=run, name=f'NovaCore {kind}', daemon=True)
        self._worker_thread.start()
        self._refresh_buttons()

    def browse(self) -> None:
        if self.busy or self._closing:
            return
        folder = filedialog.askdirectory(parent=self.root, title='Select the folder containing retrobat.exe', initialdir=self.folder_var.get() or None, mustexist=True)
        if folder:
            self._begin_scan(Path(folder))

    def autodetect(self) -> None:
        if self.busy or self._closing or self._settings_window:
            return
        self._cancel_scan.clear()
        preferred = self.folder_var.get().strip()
        extra = [Path(preferred)] if preferred else []
        self.status_var.set('Looking for RetroBat in common installation folders…')
        self.state_var.set('Detecting')
        self._run_background('detect', lambda: detect_installations(extra))

    def scan(self) -> None:
        if self.retrobat_root and not self.busy and not self._closing:
            self._begin_scan(self.retrobat_root)

    def _begin_scan(self, folder: Path) -> None:
        if self.busy or self._closing:
            return
        same_folder = str(folder) == self.folder_var.get()
        if not same_folder:
            self.selection = ConsoleSelection()
        self.retrobat_root = None
        self.engine = None
        self.games_by_system = {}
        self.selection.systems = []
        self.folder_var.set(str(folder))
        self._cancel_scan.clear()
        with self._scan_lock:
            self._latest_scan = (0, 0, 'Reading console configuration…')
        self._render_systems()
        self.state_var.set('Scanning')
        self.status_var.set('Reading console configuration…')
        self.progress.configure(maximum=1, value=0)
        self.progress_var.set('0 / 0')
        cancelled = self._cancel_scan.is_set

        def work():
            root = validate_retrobat(folder)
            systems = discover_systems(root)
            scanned = {}
            for index, system in enumerate(systems):
                if cancelled():
                    return None
                with self._scan_lock:
                    self._latest_scan = (index, len(systems), system.fullname)
                scanned[system.name] = scan_system(system, cancelled=cancelled)
                with self._scan_lock:
                    self._latest_scan = (index+1, len(systems), system.fullname)
            return None if cancelled() else (root, systems, scanned)

        self._run_background('scan', work)

    def _make_provider(self):
        cache = data_directory() / 'cache'
        options = dict(region=self.settings.region, interval=self.settings.interval, workers=self.settings.workers)
        if self.settings.provider == 'screenscraper':
            if not all(self.credentials.values()):
                raise ValueError('Enter your ScreenScraper developer credentials and account details in Settings first.')
            return ScreenScraperProvider(cache, **self.credentials, **options)
        return LibretroProvider(cache, **options)

    def start(self) -> None:
        if self.busy or self._closing or not self.retrobat_root:
            return
        games = self.selection.games(self.games_by_system)
        if not games:
            return
        try:
            provider = self._make_provider()
            self.engine = ScrapeEngine(self.retrobat_root, provider)
            self._persist()
            self.engine.start(games)
        except ValueError as exc:
            messagebox.showerror('Cannot start scraping', str(exc), parent=self.root)
        except Exception as exc:
            LOG.warning('Could not start scraping (%s)', type(exc).__name__)
            messagebox.showerror('Cannot start scraping', 'Check your settings and that the application folder is writable.', parent=self.root)
        self._refresh_buttons()

    def pause_resume(self) -> None:
        if self.engine and self.engine.active:
            if self.engine.snapshot().state == 'Paused':
                self.engine.resume()
            else:
                self.engine.pause()
            self._refresh_buttons()

    def stop(self) -> None:
        if self.engine and self.engine.active:
            self.engine.stop()
        if self._worker_kind in ('scan', 'detect'):
            self._cancel_scan.set()
            self.status_var.set('Stopping the scan…')
        self._refresh_buttons()

    def _persist(self) -> None:
        if self._smoke_test:
            return
        self.settings = replace(self.settings, retrobat_folder=str(self.retrobat_root or self.folder_var.get()),
                                selected_systems=sorted(self.selection.selected or ()))
        try:
            save_settings(self.settings)
        except OSError:
            LOG.warning('Settings could not be saved.')
            self.status_var.set('Settings could not be saved. Use a writable application folder.')

    def _handle_result(self, event) -> None:
        generation, kind, result, error = event
        if generation != self._generation:
            return
        self._worker_thread = None
        self._worker_kind = ''
        if error:
            self.state_var.set('Error')
            self.status_var.set(error)
            return
        if kind == 'detect':
            if self._cancel_scan.is_set():
                self.state_var.set('Stopped')
                self.status_var.set('Detection stopped.')
            elif result:
                self._begin_scan(result[0])
            else:
                self.state_var.set('Idle')
                self.status_var.set('No RetroBat installation found. Use Browse to select its folder.')
        elif kind == 'scan':
            with self._scan_lock:
                self._latest_scan = None
            if result is None:
                self.state_var.set('Stopped')
                self.status_var.set('Scan stopped. Choose a folder to scan again.')
                return
            self.retrobat_root, systems, self.games_by_system = result
            self.folder_var.set(str(self.retrobat_root))
            self.selection.replace_systems(systems)
            self._render_systems()
            # Construction does not request network data. Pending XML updates need no provider access.
            self.engine = ScrapeEngine(self.retrobat_root, LibretroProvider(data_directory() / 'cache'))
            self._last_pending_check = 0
            self.state_var.set('Ready')
            total = sum(len(games) for games in self.games_by_system.values())
            self.progress.configure(maximum=1, value=0)
            self.progress_var.set('0 / 0')
            self.status_var.set(f'Scan complete: {total:,} games across {len(systems)} consoles. Select consoles and start scraping.' if systems else 'No supported consoles found in this installation. Check its system configuration and ROM folders.')
            self._persist()
        self._refresh_buttons()

    def _render_progress(self) -> None:
        if not self.engine:
            return
        progress = self.engine.snapshot()
        self.counts_var.set(f'Downloaded: {progress.completed:,}   Existing: {progress.skipped:,}   Missing: {progress.missing:,}   Failed: {progress.failed:,}   Queued metadata: {progress.pending:,}')
        if self._worker_kind not in ('scan', 'detect'):
            if progress.state != 'Idle':
                self.state_var.set(progress.state)
                self.progress.configure(maximum=max(progress.total, 1), value=progress.processed)
                self.progress_var.set(f'{progress.processed:,} / {progress.total:,}')
            if progress.pending and not self.engine.active and progress.state != 'Error':
                if progress.message.startswith('Metadata pending for '):
                    self.status_var.set(progress.message)
                else:
                    self.status_var.set(f'{progress.pending:,} metadata updates queued. Close RetroBat to apply them automatically.')
            elif self._previous_pending and not progress.pending:
                self.status_var.set('Pending metadata applied.')
            elif progress.message:
                self.status_var.set(progress.message)
        self._previous_pending = progress.pending

    def _maybe_apply_pending(self) -> None:
        if self._smoke_test or self._closing or self.busy or self._settings_window or not self.engine:
            return
        if time.monotonic() - self._last_pending_check < 5:
            return
        self._last_pending_check = time.monotonic()
        engine = self.engine
        self._run_background('pending', engine.apply_pending)

    def _poll(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if not self._closing:
                self._handle_result(event)
        if self._closing:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_kind = ''
            if not self.busy:
                self.root.destroy()
                return
        else:
            if self._worker_kind == 'scan':
                with self._scan_lock:
                    latest = self._latest_scan
                if latest:
                    done, total, name = latest
                    self.progress.configure(maximum=max(1, total), value=done)
                    self.progress_var.set(f'{done:,} / {total:,} consoles')
                    self.status_var.set(f'Scanning {name} ({done}/{total} consoles)…' if total else name)
            self._render_progress()
            self._maybe_apply_pending()
            self._refresh_buttons()
        self.root.after(100, self._poll)

    def show_settings(self) -> None:
        if self.busy or self._closing or self._settings_window:
            return
        window = self._settings_window = tk.Toplevel(self.root)
        window.title('NovaCore settings')
        window.transient(self.root)
        window.resizable(False, False)
        panel = ttk.Frame(window, padding=16)
        panel.grid(sticky='nsew')
        panel.columnconfigure(1, weight=1)
        provider = tk.StringVar(value=self.settings.provider)
        region = tk.StringVar(value=self.settings.region)
        interval = tk.StringVar(value=str(self.settings.interval))
        workers = tk.StringVar(value=str(self.settings.workers))
        credentials = {key: tk.StringVar(value=self.credentials[key]) for key, _label, _env in _CREDENTIALS}
        rows = (
            ('Artwork provider', ttk.Combobox(panel, textvariable=provider, values=('libretro', 'screenscraper'), state='readonly', width=29)),
            ('Preferred region', ttk.Combobox(panel, textvariable=region, values=('eu', 'us', 'jp', 'wor', 'fr', 'de', 'uk', 'es', 'it', 'br', 'au'), state='readonly', width=29)),
            ('Seconds between requests', ttk.Spinbox(panel, textvariable=interval, from_=0.5, to=60, increment=0.5, width=30)),
            ('Concurrent requests (1–4)', ttk.Spinbox(panel, textvariable=workers, from_=1, to=4, width=30)),
        )
        for row, (label, widget) in enumerate(rows):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 14))
            widget.grid(row=row, column=1, sticky='ew', pady=5)
        ttk.Separator(panel).grid(row=4, column=0, columnspan=2, sticky='ew', pady=10)
        ttk.Label(panel, text='ScreenScraper credentials · current session only').grid(row=5, column=0, columnspan=2, sticky='w', pady=(0, 6))
        credential_entries = []
        for row, (key, label, _env) in enumerate(_CREDENTIALS, start=6):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', pady=4)
            entry = ttk.Entry(panel, textvariable=credentials[key], show='•' if 'password' in key else '', width=33)
            entry.grid(row=row, column=1, sticky='ew', pady=4)
            credential_entries.append(entry)
        ttk.Label(panel, text='Use your own developer credentials and ScreenScraper account.\nProvider limits may reduce the requested concurrency.', wraplength=460).grid(row=10, column=0, columnspan=2, sticky='w', pady=(6, 12))
        ttk.Separator(panel).grid(row=11, column=0, columnspan=2, sticky='ew')
        media = ttk.Frame(panel)
        media.grid(row=12, column=0, columnspan=2, sticky='w', pady=10)
        window._media_vars = [tk.BooleanVar(value=True)]
        ttk.Checkbutton(media, text='Box artwork', variable=window._media_vars[0], state='disabled').grid(row=0, column=0, sticky='w')
        for column, label in enumerate(('Manuals', 'Ratings', 'Pad-to-key'), start=1):
            variable = tk.BooleanVar(value=False)
            window._media_vars.append(variable)
            ttk.Checkbutton(media, text=f'{label} (unavailable)', variable=variable, state='disabled').grid(row=column, column=0, sticky='w')

        def provider_changed(*_args):
            for entry in credential_entries:
                entry.configure(state='normal' if provider.get() == 'screenscraper' else 'disabled')

        provider.trace_add('write', provider_changed)
        provider_changed()

        def dismiss():
            self._settings_window = None
            window.destroy()

        def apply():
            try:
                new_interval = float(interval.get())
                new_workers = int(workers.get())
                if not 0.5 <= new_interval <= 60 or not 1 <= new_workers <= 4:
                    raise ValueError
            except ValueError:
                messagebox.showerror('Invalid settings', 'Use 0.5–60 seconds between requests and 1–4 concurrent requests.', parent=window)
                return
            self.settings = replace(self.settings, provider=provider.get(), region=region.get(), interval=new_interval, workers=new_workers)
            self.credentials = {key: value.get() for key, value in credentials.items()}
            self._persist()
            self._refresh_provider()
            dismiss()

        buttons = ttk.Frame(panel)
        buttons.grid(row=13, column=0, columnspan=2, sticky='e', pady=(6, 0))
        ttk.Button(buttons, text='Cancel', command=dismiss).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text='Save settings', command=apply).grid(row=0, column=1)
        window.protocol('WM_DELETE_WINDOW', dismiss)
        window.grab_set()

    def close(self) -> None:
        if self._closing:
            return
        self._persist()
        self._closing = True
        self._cancel_scan.set()
        if self.engine and self.engine.active:
            self.engine.stop()
        if self._filter_after:
            self.root.after_cancel(self._filter_after)
            self._filter_after = None
        self.status_var.set('Closing after background work stops…')
        self._refresh_buttons()
        if not self.busy:
            self.root.destroy()


def launch(smoke_test: bool = False) -> None:
    """Open the desktop app; smoke mode closes without detection, scans or network."""
    root = tk.Tk()
    root._novacore_smoke_test = smoke_test
    app = App(root)
    if smoke_test:
        root.after(300, app.close)
    root.mainloop()
