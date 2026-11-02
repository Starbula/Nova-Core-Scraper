"""Small console-level Tk interface; background work never calls Tk."""
from __future__ import annotations

from dataclasses import replace
from itertools import chain
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import auth
from .credentials import (configured_credentials, save_credentials, clear_credentials,
                          can_remember_credentials)
from .engine import ScrapeEngine, recover_scanned_artwork
from .library import detect_installations
from .scan import scan_library
from .models import Game, System
from .providers import LibretroProvider, ScreenScraperProvider, Cancelled
from .launchbox import LaunchBoxProvider
from .gametdb import GameTDBProvider
from .api_sources import TheGamesDBProvider, IGDBProvider
from .mobygames import MobyGamesProvider
from .matching import MatchPolicy
from .sources import FallbackProvider
from .state import State
from .settings import Settings, data_directory, load_settings, save_settings

LOG = logging.getLogger(__name__)
DARK_COLORS = {
    'background': '#181c22',
    'surface': '#232a33',
    'field': '#12171e',
    'foreground': '#edf2f7',
    'button': '#313c49',
    'hover': '#405166',
    'disabled_background': '#272f39',
    'disabled_foreground': '#b1bdcc',
    'border': '#536173',
    'accent': '#8cbcff',
    'selection': '#285880',
    'selection_foreground': '#ffffff',
}
_CREDENTIALS = (
    ('username', 'ScreenScraper username', 'NOVACORE_SS_USERNAME'),
    ('password', 'ScreenScraper password', 'NOVACORE_SS_PASSWORD'),
    ('tgdb_api_key', 'TheGamesDB API key', 'NOVACORE_TGDB_API_KEY'),
    ('igdb_client_id', 'IGDB client ID', 'NOVACORE_IGDB_CLIENT_ID'),
    ('igdb_client_secret', 'IGDB client secret', 'NOVACORE_IGDB_CLIENT_SECRET'),
    ('mobygames_api_key', 'MobyGames API key', 'NOVACORE_MOBYGAMES_API_KEY'),
)
PROVIDERS = ('automatic', 'libretro', 'launchbox', 'gametdb', 'thegamesdb', 'igdb', 'mobygames', 'screenscraper')
SOURCE_NAMES = {'libretro': 'Libretro', 'launchbox': 'LaunchBox', 'gametdb': 'GameTDB',
                'screenscraper': 'ScreenScraper', 'thegamesdb': 'TheGamesDB',
                'igdb': 'IGDB', 'mobygames': 'MobyGames'}
CREDENTIALS_LOADING = 'Loading saved provider credentials…'
SOURCE_ORDER = ('libretro', 'launchbox', 'gametdb', 'screenscraper', 'igdb', 'thegamesdb', 'mobygames')
SOURCE_STATES = {'waiting': 'Waiting', 'preparing': 'Preparing', 'prepared': 'Prepared',
                 'searching': 'Searching', 'no_match': 'No match', 'matched': 'Matched',
                 'downloaded': 'Downloaded', 'quota_exhausted': 'Quota exhausted',
                 'authentication': 'Authentication rejected', 'temporary_failure': 'Temporary failure',
                 'access_refused': 'Access refused',
                 'source_failure': 'Source failure', 'invalid_response': 'Invalid response',
                 'local_error': 'Local error', 'no_artwork': 'No front cover',
                 'allowance_unconfirmed': 'Budget unconfirmed'}


def _dark_window(window: tk.Tk | tk.Toplevel) -> None:
    window.configure(background=DARK_COLORS['background'])
    if os.name != 'nt':
        return

    def titlebar():
        # Windows 10/11 may support either attribute; older versions retain their title bar.
        try:
            import ctypes
            from ctypes import wintypes
            get_parent = ctypes.windll.user32.GetParent
            get_parent.argtypes = (wintypes.HWND,)
            get_parent.restype = wintypes.HWND
            hwnd = get_parent(window.winfo_id()) or window.winfo_id()
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            set_attribute.restype = ctypes.c_long
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):
                if set_attribute(hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) >= 0:
                    break
        except (AttributeError, OSError, tk.TclError):
            pass

    window.after_idle(titlebar)


def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    """Use drawable ttk controls so Windows native colors cannot hide dark text."""
    c = DARK_COLORS
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=c['background'], foreground=c['foreground'],
                    bordercolor=c['border'], lightcolor=c['surface'], darkcolor=c['field'],
                    troughcolor=c['field'], focuscolor=c['accent'],
                    selectbackground=c['selection'], selectforeground=c['selection_foreground'])
    style.map('.', background=[('disabled', c['disabled_background']), ('active', c['surface'])],
              foreground=[('disabled', c['disabled_foreground'])],
              selectbackground=[('!focus', c['selection'])],
              selectforeground=[('!focus', c['selection_foreground'])])
    for name in ('TFrame', 'TLabel', 'TLabelframe', 'TLabelframe.Label'):
        style.configure(name, background=c['background'], foreground=c['foreground'])
    style.configure('TButton', background=c['button'], foreground=c['foreground'],
                    lightcolor=c['button'], darkcolor=c['button'])
    style.map('TButton', background=[('disabled', c['disabled_background']),
                                    ('pressed', c['surface']), ('active', c['hover'])],
              foreground=[('disabled', c['disabled_foreground'])],
              lightcolor=[('pressed', c['surface'])], darkcolor=[('pressed', c['surface'])],
              bordercolor=[('focus', c['accent'])])
    for name in ('TEntry', 'TSpinbox', 'TCombobox'):
        style.configure(name, foreground=c['foreground'], background=c['surface'],
                        fieldbackground=c['field'], insertcolor=c['foreground'],
                        arrowcolor=c['foreground'], lightcolor=c['border'], darkcolor=c['border'],
                        selectbackground=c['selection'], selectforeground=c['selection_foreground'])
        style.map(name, foreground=[('disabled', c['disabled_foreground']), ('readonly', c['foreground'])],
                  fieldbackground=[('disabled', c['disabled_background']), ('readonly', c['field'])],
                  background=[('disabled', c['disabled_background']), ('pressed', c['hover']),
                              ('active', c['hover']), ('readonly', c['surface'])],
                  arrowcolor=[('disabled', c['disabled_foreground'])],
                  bordercolor=[('focus', c['accent'])], lightcolor=[('focus', c['accent'])])
    style.configure('TCheckbutton', background=c['background'], foreground=c['foreground'],
                    indicatorbackground=c['field'], indicatorforeground=c['foreground'])
    style.map('TCheckbutton', background=[('disabled', c['background']), ('active', c['background'])],
              foreground=[('disabled', c['disabled_foreground'])],
              indicatorbackground=[('disabled', c['disabled_background']), ('pressed', c['surface'])],
              indicatorforeground=[('disabled', c['disabled_foreground'])])
    style.configure('Treeview', background=c['field'], fieldbackground=c['field'],
                    stripedbackground=c['surface'], foreground=c['foreground'])
    style.map('Treeview', background=[('disabled', c['disabled_background']),
                                     ('selected', c['selection'])],
              foreground=[('disabled', c['disabled_foreground']),
                          ('selected', c['selection_foreground'])],
              bordercolor=[('focus', c['accent'])])
    style.configure('Treeview.Heading', background=c['surface'], foreground=c['foreground'],
                    lightcolor=c['surface'], darkcolor=c['surface'])
    style.map('Treeview.Heading', background=[('active', c['hover']), ('pressed', c['button'])],
              foreground=[('disabled', c['disabled_foreground'])])
    style.configure('TScrollbar', background=c['button'], troughcolor=c['field'],
                    arrowcolor=c['foreground'], lightcolor=c['button'], darkcolor=c['button'])
    style.map('TScrollbar', background=[('disabled', c['disabled_background']),
                                       ('pressed', c['selection']), ('active', c['hover'])],
              arrowcolor=[('disabled', c['disabled_foreground'])])
    style.configure('TProgressbar', background=c['accent'], troughcolor=c['field'],
                    lightcolor=c['accent'], darkcolor=c['accent'])
    style.configure('TSeparator', background=c['border'])
    style.configure('ComboboxPopdownFrame', background=c['field'], bordercolor=c['border'])
    # The combobox popup is a classic Tk Listbox rather than a ttk widget.
    for option, value in (('background', c['field']), ('foreground', c['foreground']),
                          ('selectBackground', c['selection']), ('selectForeground', c['selection_foreground']),
                          ('highlightBackground', c['border']), ('highlightColor', c['accent'])):
        root.option_add(f'*Listbox.{option}', value)
    _dark_window(root)
    return style



def _search_text(value: str) -> str:
    return ''.join(character for character in value.casefold() if character.isalnum())


class ConsoleSelection:
    """A checked scope is separate from the filter and the highlighted table row."""

    def __init__(self, selected: list[str] | None = None):
        self.systems: list[System] = []
        self.selected: set[str] = set(selected or ())

    def replace_systems(self, systems: list[System]) -> None:
        self.systems = list(systems)
        self.selected.intersection_update(system.name for system in systems)

    def visible(self, query: str = '') -> list[System]:
        tokens = [_search_text(word) for word in query.split() if _search_text(word)]
        return [system for system in self.systems
                if all(token in _search_text(system.fullname + ' ' + system.name) for token in tokens)]

    def choose(self, name: str) -> None:
        self.selected = {name} if any(system.name == name for system in self.systems) else set()

    def toggle(self, name: str) -> None:
        if not any(system.name == name for system in self.systems):
            return
        if name in self.selected:
            self.selected.remove(name)
        else:
            self.selected.add(name)

    def select_all(self, selected: bool) -> None:
        self.selected = {system.name for system in self.systems} if selected else set()

    def select_visible(self, query: str, selected: bool) -> None:
        names = {system.name for system in self.visible(query)}
        if selected:
            self.selected.update(names)
        else:
            self.selected.difference_update(names)

    def games(self, games_by_system: dict[str, list[Game]]) -> list[Game]:
        return [game for system in self.systems if system.name in self.selected
                for game in games_by_system.get(system.name, ())]


PAGE_SIZE = 200
JOB_FILTERS = ('All statuses', 'Active', 'checking', 'queued', 'finding', 'downloading',
               'retry_wait', 'completed', 'skipped', 'missing', 'failed', 'cancelled')
ACTIVE_JOB_STATES = ('checking', 'queued', 'finding', 'downloading', 'retry_wait')


class App:
    """Tk owns selection and rendering; scanners and history readers only publish data."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.style = apply_dark_theme(root)
        self.settings = load_settings()
        # A previous bulk selection must never become the default scope of a new session.
        self.selection = ConsoleSelection()
        self.credentials = {key: os.environ.get(env, '') for key, _label, env in _CREDENTIALS}
        self.retrobat_root: Path | None = None
        self.engine: ScrapeEngine | None = None
        self.games_by_system: dict[str, list[Game]] = {}
        self._existing_counts: dict[str, int] = {}
        self._covered_game_keys: set[str] = set()
        self._last_artwork_check = 0.0
        self._selected_games = 0
        self._selected_existing = 0
        self._run_scope = ''
        self._events: queue.Queue = queue.Queue(maxsize=4)
        self._scan_lock = threading.Lock()
        self._latest_scan = None
        self._cancel_scan = threading.Event()
        self._worker_thread = None
        self._worker_kind = ''
        self._background_threads = {}
        self._background_pending = {}
        self._generation = 0
        self._closing = False
        self._filter_after = None
        self._poll_after = None
        self._startup_after = None
        self._last_pending_check = 0.0
        self._previous_pending = 0
        self._settings_window = None  # Compatibility: Settings never creates a Toplevel.
        self._smoke_test = bool(getattr(root, '_novacore_smoke_test', False))
        self._page_thread = None
        self._page_events: queue.Queue = queue.Queue(maxsize=4)
        self._page_ticket = 0
        self._active_page_request = None
        self._pending_pages = {}
        self._last_page_check = 0.0
        self._pages = {}
        self._console_names = {}
        self._last_source_check = 0.0
        self._source_details = {}
        self._diagnostic_provider = None
        self._diagnostic_cancel = threading.Event()
        self.folder_var = tk.StringVar(value=self.settings.retrobat_folder)
        self.console_var = tk.StringVar(value='Choose one console')
        self.filter_var = tk.StringVar()
        self.status_var = tk.StringVar(value='Choose a RetroBat folder, then choose a console to scrape.')
        self.state_var = tk.StringVar(value='Idle')
        self.progress_var = tk.StringVar(value='0 / 0 artwork jobs processed')
        self.selection_var = tk.StringVar(value='Selected consoles: none')
        self.scope_var = tk.StringVar(value='0 games selected · 0 existing covers · 0 missing covers')
        self.counts_var = tk.StringVar(value='Existing: 0   Downloaded: 0   Missing: 0   Failed: 0   Cancelled: 0   Queued metadata: 0')
        self.provider_var = tk.StringVar()
        self.auth_status_var = tk.StringVar()
        self.sources_status_var = tk.StringVar()
        self.source_chain_var = tk.StringVar()
        self.settings_status_var = tk.StringVar()
        self.provider_setting_var = tk.StringVar(value=self.settings.provider)
        self.region_setting_var = tk.StringVar(value=self.settings.region)
        self.interval_setting_var = tk.StringVar(value=str(self.settings.interval))
        self.workers_setting_var = tk.StringVar(value=str(self.settings.workers))
        self.account_username_var = tk.StringVar(value=self.credentials['username'])
        self.account_password_var = tk.StringVar(value=self.credentials['password'])
        self.tgdb_key_var = tk.StringVar(value=self.credentials['tgdb_api_key'])
        self.igdb_id_var = tk.StringVar(value=self.credentials['igdb_client_id'])
        self.igdb_secret_var = tk.StringVar(value=self.credentials['igdb_client_secret'])
        self.moby_key_var = tk.StringVar(value=self.credentials['mobygames_api_key'])
        self._credential_vars = dict(zip((item[0] for item in _CREDENTIALS),
            (self.account_username_var, self.account_password_var, self.tgdb_key_var,
             self.igdb_id_var, self.igdb_secret_var, self.moby_key_var)))
        self._initial_credentials = dict(self.credentials)
        self._credential_edited = set()
        self._remember_available = can_remember_credentials()
        self.remember_credentials_var = tk.BooleanVar(value=self.settings.remember_credentials and self._remember_available)
        self.allow_other_regions_var = tk.BooleanVar(value=self.settings.allow_other_regions)
        self.allow_base_cover_var = tk.BooleanVar(value=self.settings.allow_base_cover)
        self.retry_failed_var = tk.BooleanVar(value=self.settings.retry_failed)
        self.keep_awake_var = tk.BooleanVar(value=self.settings.keep_awake)
        self._build()
        self.provider_setting_var.trace_add('write', self._provider_changed)
        for key, variable in self._credential_vars.items():
            variable.trace_add('write', lambda *_args, key=key:self._credential_changed(key))
        self._refresh_provider()
        self._refresh_buttons()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self._poll_after = self.root.after(100, self._poll)
        if not self._smoke_test:
            self._run_background('credentials-load', configured_credentials, independent=True)
            self._startup_after = self.root.after(150, self.autodetect)

    def _build(self) -> None:
        self.root.title('NovaCore Artwork Scraper')
        self.root.geometry('1080x780')
        self.root.minsize(920, 660)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.style.configure('TNotebook', background=DARK_COLORS['background'], bordercolor=DARK_COLORS['border'])
        self.style.configure('TNotebook.Tab', background=DARK_COLORS['button'], foreground=DARK_COLORS['foreground'], padding=(18, 8))
        self.style.map('TNotebook.Tab', background=[('selected', DARK_COLORS['background']), ('active', DARK_COLORS['hover'])],
                       foreground=[('disabled', DARK_COLORS['disabled_foreground'])])
        self.style.configure('Console.Treeview', rowheight=30)
        main = ttk.Frame(self.root, padding=14)
        main.grid(sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        ttk.Label(main, text='NovaCore Artwork Scraper', font=('Segoe UI', 17, 'bold')).grid(row=0, column=0, sticky='w')
        provider_line = ttk.Frame(main)
        provider_line.grid(row=1, column=0, sticky='ew', pady=(5, 10))
        provider_line.columnconfigure(0, weight=1)
        ttk.Label(provider_line, textvariable=self.provider_var, wraplength=850).grid(row=0, column=0, sticky='w')
        self.use_automatic_button = ttk.Button(provider_line, text='Use automatic sources', command=self.use_automatic)
        self.use_automatic_button.grid(row=0, column=1, padx=(10, 0))
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=2, column=0, sticky='nsew')
        self.library_tab = ttk.Frame(self.notebook, padding=12)
        self.downloads_tab = ttk.Frame(self.notebook, padding=12)
        self.downloaded_tab = ttk.Frame(self.notebook, padding=12)
        self.sources_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        for tab, label in ((self.library_tab, 'Library'), (self.downloads_tab, 'Downloads'),
                           (self.downloaded_tab, 'Downloaded'), (self.sources_tab, 'Sources'), (self.settings_tab, 'Settings')):
            self.notebook.add(tab, text=label)
        self._build_library()
        self._build_job_page('jobs', self.downloads_tab)
        self._build_job_page('downloaded', self.downloaded_tab)
        self.jobs_tree = self._pages['jobs']['tree']
        self.downloaded_tree = self._pages['downloaded']['tree']
        self._build_sources()
        self._build_settings()
        self.notebook.bind('<<NotebookTabChanged>>', self._tab_changed)
        actions = ttk.Frame(main)
        actions.grid(row=3, column=0, sticky='ew', pady=(12, 8))
        actions.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(actions, text='Start scraping · 0 games', command=self.start)
        self.start_button.grid(row=0, column=0, sticky='w')
        self.pause_button = ttk.Button(actions, text='Pause', command=self.pause_resume)
        self.pause_button.grid(row=0, column=1, padx=6)
        self.stop_button = ttk.Button(actions, text='Stop', command=self.stop)
        self.stop_button.grid(row=0, column=2)
        ttk.Separator(main).grid(row=4, column=0, sticky='ew', pady=(0, 8))
        progress_frame = ttk.Frame(main)
        progress_frame.grid(row=5, column=0, sticky='ew')
        progress_frame.columnconfigure(1, weight=1)
        ttk.Label(progress_frame, textvariable=self.state_var, width=13).grid(row=0, column=0, sticky='w')
        self.progress = ttk.Progressbar(progress_frame, maximum=1, value=0)
        self.progress.grid(row=0, column=1, sticky='ew')
        ttk.Label(progress_frame, textvariable=self.progress_var, width=38, anchor='e').grid(row=0, column=2, padx=(8, 0))
        ttk.Label(main, textvariable=self.counts_var).grid(row=6, column=0, sticky='w', pady=(7, 3))
        self.status_label = ttk.Label(main, textvariable=self.status_var, wraplength=1020)
        self.status_label.grid(row=7, column=0, sticky='ew')

    def _build_library(self) -> None:
        tab = self.library_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(5, weight=1)
        folder = ttk.LabelFrame(tab, text='RetroBat installation', padding=8)
        folder.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.folder_var, state='readonly').grid(row=0, column=0, sticky='ew', padx=(0, 8))
        self.browse_button = ttk.Button(folder, text='Browse…', command=self.browse)
        self.browse_button.grid(row=0, column=1, padx=(0, 6))
        self.detect_button = ttk.Button(folder, text='Autodetect', command=self.autodetect)
        self.detect_button.grid(row=0, column=2, padx=(0, 6))
        self.scan_button = ttk.Button(folder, text='Scan library', command=self.scan)
        self.scan_button.grid(row=0, column=3)
        picker = ttk.Frame(tab)
        picker.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        picker.columnconfigure(1, weight=1)
        ttk.Label(picker, text='Choose one console:').grid(row=0, column=0, padx=(0, 8))
        self.console_dropdown = ttk.Combobox(picker, textvariable=self.console_var, state='readonly')
        self.console_dropdown.grid(row=0, column=1, sticky='ew')
        self.console_dropdown.bind('<<ComboboxSelected>>', self._console_chosen)
        self.select_button = ttk.Button(picker, text='Select All', command=lambda: self.select_all(True))
        self.select_button.grid(row=0, column=2, padx=(10, 6))
        self.clear_button = ttk.Button(picker, text='Deselect All', command=lambda: self.select_all(False))
        self.clear_button.grid(row=0, column=3)
        search = ttk.Frame(tab)
        search.grid(row=2, column=0, sticky='ew', pady=(0, 8))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text='Find consoles:').grid(row=0, column=0, padx=(0, 8))
        self.filter_entry = ttk.Entry(search, textvariable=self.filter_var)
        self.filter_entry.grid(row=0, column=1, sticky='ew')
        self.filter_var.trace_add('write', self._filter_changed)
        ttk.Label(tab, textvariable=self.selection_var, font=('Segoe UI', 10, 'bold'), wraplength=990).grid(row=3, column=0, sticky='w')
        ttk.Label(tab, textvariable=self.scope_var).grid(row=4, column=0, sticky='w', pady=(4, 8))
        table = ttk.Frame(tab)
        table.grid(row=5, column=0, sticky='nsew')
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self._checkbox_images = {}
        for checked in (False, True):
            image = tk.PhotoImage(master=self.root, width=24, height=24)
            outline = DARK_COLORS['accent'] if checked else DARK_COLORS['disabled_foreground']
            image.put(outline, to=(2, 2, 22, 22))
            image.put(DARK_COLORS['accent'] if checked else DARK_COLORS['field'], to=(4, 4, 20, 20))
            if checked:
                for x, y in ((6, 11), (7, 12), (8, 13), (9, 14), (10, 14), (11, 13),
                             (12, 12), (13, 11), (14, 10), (15, 9), (16, 8)):
                    image.put(DARK_COLORS['field'], to=(x, y, x+3, y+3))
            self._checkbox_images[checked] = image
        self.tree = ttk.Treeview(table, columns=('console', 'games', 'existing', 'missing'),
                                 show='tree headings', selectmode='browse', style='Console.Treeview')
        self.tree.heading('#0', text='Select')
        self.tree.column('#0', width=86, minwidth=86, stretch=False, anchor='center')
        self.tree.tag_configure('checked', background=DARK_COLORS['surface'])
        for key, heading, width, anchor in (
            ('console', 'Console', 360, 'w'), ('games', 'Games', 90, 'e'),
            ('existing', 'Existing covers', 125, 'e'), ('missing', 'Missing covers', 125, 'e')):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=width if key != 'console' else 150,
                             stretch=key == 'console', anchor=anchor)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<Button-1>', self._click_row)
        self.tree.bind('<space>', self._space_row)
        ttk.Label(tab, text='Click a console name to select only that console. Use the checkbox column to add or remove consoles.').grid(row=6, column=0, sticky='w', pady=(6, 0))

    def _build_job_page(self, name, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        controls.columnconfigure(1, weight=1)
        query = tk.StringVar()
        status = tk.StringVar(value='All statuses')
        ttk.Label(controls, text='Search games:').grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(controls, textvariable=query).grid(row=0, column=1, sticky='ew')
        if name == 'jobs':
            combo = ttk.Combobox(controls, textvariable=status, values=JOB_FILTERS, state='readonly', width=16)
            combo.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(controls, text='Refresh', command=lambda: self._request_page(name, force=True)).grid(row=0, column=3, padx=(8, 0))
        table = ttk.Frame(tab)
        table.grid(row=1, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        tree = ttk.Treeview(table, columns=('title', 'console', 'status', 'detail'), show='headings', selectmode='extended')
        for key, heading, width in (('title', 'Game', 300), ('console', 'Console', 190), ('status', 'Status', 110),
                                    ('detail', 'Artwork file' if name == 'downloaded' else 'Details', 350)):
            tree.heading(key, text=heading)
            tree.column(key, width=width, minwidth=80, stretch=key in ('title', 'detail'))
        tree.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        tree.bind('<<TreeviewSelect>>', lambda _event: self._job_selected(name))
        tree.bind('<Double-1>', lambda _event:self._show_job_details(name))
        footer = ttk.Frame(tab)
        footer.grid(row=2, column=0, sticky='ew', pady=(8, 0))
        footer.columnconfigure(1, weight=1)
        previous = ttk.Button(footer, text='Previous', command=lambda: self._change_page(name, -1))
        previous.grid(row=0, column=0)
        label = tk.StringVar(value='No games to show · 200 rows per page')
        ttk.Label(footer, textvariable=label, anchor='center').grid(row=0, column=1, sticky='ew')
        next_button = ttk.Button(footer, text='Next', command=lambda: self._change_page(name, 1))
        next_button.grid(row=0, column=2)
        ttk.Button(footer, text='Show details', command=lambda:self._show_job_details(name)).grid(row=0, column=3, padx=(8, 0))
        if name == 'jobs':
            cancel = ttk.Frame(tab)
            cancel.grid(row=3, column=0, sticky='ew', pady=(10, 0))
            self.cancel_selected_button = ttk.Button(cancel, text='Cancel selected', command=self.cancel_selected)
            self.cancel_selected_button.grid(row=0, column=0, padx=(0, 6))
            self.cancel_all_button = ttk.Button(cancel, text='Cancel all', command=self.cancel_all)
            self.cancel_all_button.grid(row=0, column=1)
            ttk.Label(cancel, text='Completed artwork files are kept.').grid(row=0, column=2, padx=12)
            self.export_results_button = ttk.Button(cancel, text='Export results…', command=self.export_results)
            self.export_results_button.grid(row=0, column=3, padx=(12, 0))
            ttk.Label(tab, text='Filter Missing or Failed to review unresolved games. Export includes all results and match notes.').grid(row=4, column=0, sticky='w', pady=(8, 0))
        details = ttk.LabelFrame(tab, text='Selected game · source attempts and result', padding=8)
        details.grid(row=5, column=0, sticky='ew', pady=(8, 0))
        details.columnconfigure(0, weight=1)
        detail_text = tk.Text(details, height=6, wrap='word', background=DARK_COLORS['field'],
            foreground=DARK_COLORS['foreground'], insertbackground=DARK_COLORS['foreground'],
            selectbackground=DARK_COLORS['selection'], relief='flat', state='disabled')
        detail_text.grid(row=0, column=0, sticky='ew')
        detail_scroll = ttk.Scrollbar(details, orient='vertical', command=detail_text.yview)
        detail_scroll.grid(row=0, column=1, sticky='ns')
        detail_text.configure(yscrollcommand=detail_scroll.set)
        ttk.Button(details, text='Hide details', command=details.grid_remove).grid(row=1, column=0, sticky='e', pady=(6, 0))
        details.grid_remove()
        self._pages[name] = dict(tree=tree, query=query, status=status, offset=0, total=0, label=label,
                                 previous=previous, next=next_button, after=None, requested=None,
                                 applied=None, ticket=0, rows={}, detail_frame=details, detail_text=detail_text)
        query.trace_add('write', lambda *_args: self._page_search_changed(name))
        status.trace_add('write', lambda *_args: self._page_status_changed(name))

    @staticmethod
    def _set_readonly_text(widget, value) -> None:
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def _job_selected(self, name) -> None:
        self._refresh_buttons()
        page = self._pages[name]
        selected = page['tree'].selection()
        row = page['rows'].get(selected[0]) if selected else None
        if name == 'jobs' and selected:
            current = getattr(self.engine, 'job_snapshot', None)
            fresh = current(selected[0]) if callable(current) else None
            if fresh is not None:
                row = fresh
                page['rows'][selected[0]] = fresh
        if row is None:
            self._set_readonly_text(page['detail_text'], 'Select a game to inspect its result and source attempts.')
            return
        lines = [f'{row.title} · {row.console}', f'Status: {row.status.replace("_", " ")}', row.detail]
        for field, label in (('reason', 'Reason'), ('source', 'Source'), ('region', 'Region'),
                             ('match_note', 'Match note'), ('artwork', 'Artwork'), ('attempts', 'Attempts')):
            value = getattr(row, field, '')
            if value:
                lines.append(f'{label}: {value}')
        trace = getattr(row, 'source_trace', '')
        lines.append('Source attempts:\n' + trace if trace else 'No source-attempt trace is recorded for this result.')
        self._set_readonly_text(page['detail_text'], '\n'.join(line for line in lines if line))

    def _show_job_details(self, name) -> None:
        self._pages[name]['detail_frame'].grid()
        self._job_selected(name)

    def _build_sources(self) -> None:
        tab = self.sources_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        controls.columnconfigure(0, weight=1)
        header = ttk.Label(controls, textvariable=self.source_chain_var, wraplength=700)
        header.grid(row=0, column=0, sticky='ew', padx=(0, 10))
        self.check_sources_button = ttk.Button(controls, text='Check sources', command=self.check_sources)
        self.check_sources_button.grid(row=0, column=1, sticky='e')
        table = ttk.Frame(tab)
        table.grid(row=1, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ('source', 'state', 'lookups', 'matches', 'downloads', 'api_requests', 'requests', 'remaining')
        self.sources_tree = ttk.Treeview(table, columns=columns, show='headings', height=7, selectmode='browse')
        for key, heading, width in (
            ('source', 'Source', 135), ('state', 'Status', 155), ('lookups', 'Game checks', 90),
            ('matches', 'Matches', 65), ('downloads', 'Covers saved', 90), ('api_requests', 'API requests', 90),
            ('requests', 'HTTP attempts', 95), ('remaining', 'Last quota', 95)):
            self.sources_tree.heading(key, text=heading)
            self.sources_tree.column(key, width=width, minwidth=55, stretch=key in ('source', 'state'),
                                     anchor='w' if key in ('source', 'state') else 'e')
        self.sources_tree.grid(row=0, column=0, sticky='nsew')
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.sources_tree.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.sources_tree.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        self.sources_tree.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.sources_tree.bind('<<TreeviewSelect>>', self._source_selected)
        details = ttk.LabelFrame(tab, text='Selected source · access and latest result', padding=8)
        details.grid(row=2, column=0, sticky='ew', pady=(10, 8))
        details.columnconfigure(0, weight=1)
        self.source_detail_text = tk.Text(details, height=4, wrap='word', background=DARK_COLORS['field'],
            foreground=DARK_COLORS['foreground'], selectbackground=DARK_COLORS['selection'],
            relief='flat', state='disabled')
        self.source_detail_text.grid(row=0, column=0, sticky='ew')
        scroll = ttk.Scrollbar(details, orient='vertical', command=self.source_detail_text.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.source_detail_text.configure(yscrollcommand=scroll.set)
        footer = ttk.Label(tab, text='Game checks are source lookups. API requests count service API calls; HTTP attempts include catalogue and image requests. '
            'Neither is a count of covers saved. Cached results may need zero network requests. Each game tries sources in the displayed order; parallel games may use different sources at the same time. '
            'Check sources sends real service requests and may use API quota. Server-reported quota may lag in-flight requests. '
            'Configured credentials are unverified until the service accepts an actual request. Data by MobyGames.com.', wraplength=850)
        footer.grid(row=3, column=0, sticky='ew')
        tab.bind('<Configure>', lambda event:(header.configure(wraplength=max(240, event.width-150)),
                                            footer.configure(wraplength=max(240, event.width-20))))
        self._render_sources(force=True)

    def _idle_source(self, key):
        if key in ('libretro', 'launchbox', 'gametdb'):
            return 'Available', ('No account required. Access and artwork availability are checked during a run.'
                                 + (' GameTDB covers verified Wii and GameCube games.' if key == 'gametdb' else ''))
        if key == 'screenscraper' and not auth.screen_scraper_app_credentials():
            return 'Blocked', ('ScreenScraper requires approved NovaCore application credentials as well as your account. '
                               'An entered username and password alone cannot enable this source.')
        if self._source_ready(key):
            return 'Configured, unverified', 'Credentials are configured. This does not prove service access; authentication and quota are checked on an actual request.'
        return 'Unconfigured', 'Enter your authorized account or API credentials in Settings to enable this optional source.'

    def _display_source_provider(self):
        if self.engine and self.engine.active:
            return getattr(self.engine, 'provider', None)
        return self._diagnostic_provider or getattr(self.engine, 'provider', None)

    def _render_sources(self, *, force=False) -> None:
        now = time.monotonic()
        if not force and now-self._last_source_check < .5:
            return
        self._last_source_check = now
        provider = self._display_source_provider()
        getter = getattr(provider, 'source_statuses', None)
        actual = callable(getter)
        try:
            snapshots = tuple(getter()) if actual else ()
        except Exception as exc:
            LOG.warning('Could not read source status (%s)', type(exc).__name__)
            snapshots = ()
        names = {''.join(character for character in name.casefold() if character.isalnum()):key
                 for key, name in SOURCE_NAMES.items()}
        by_key = {names.get(''.join(character for character in row.name.casefold() if character.isalnum())):row
                  for row in snapshots}
        chain = ' → '.join(row.name for row in snapshots)
        if actual:
            active = bool(self.engine and self.engine.active)
            prefix = 'Source check order: ' if provider is self._diagnostic_provider and not active else 'Active run order: ' if active else 'Last run order: '
            self.source_chain_var.set(prefix + (chain or 'Status unavailable.'))
            if active and chain:
                self.provider_var.set('Running · ' + chain + '. See Sources for access, requests and results.')
        else:
            configured = [key for key in SOURCE_ORDER if self._source_ready(key)]
            selected = self.provider_setting_var.get()
            enabled = configured if selected == 'automatic' else [selected] if self._source_ready(selected) else []
            self.source_chain_var.set('Next run order (access unverified): ' + (' → '.join(SOURCE_NAMES[key] for key in enabled) or 'Choose and configure a source in Settings.'))
        selected = self.sources_tree.selection()
        order = [key for key in by_key if key in SOURCE_NAMES] + [key for key in SOURCE_ORDER if key not in by_key]
        for index, key in enumerate(order):
            row = by_key.get(key)
            if row is not None:
                state = SOURCE_STATES.get(row.state, row.state.replace('_', ' ').capitalize())
                detail = row.detail or 'No additional result is available yet.'
                metrics = (row.lookups, row.matches, row.downloads, row.api_requests, row.requests,
                           row.remaining if row.remaining is not None else '—')
            else:
                state, detail = self._idle_source(key)
                if actual:
                    state = 'Not enabled in this run'
                    detail = 'Not enabled in this run. ' + detail + ' Configuration changes apply to the next run.'
                metrics = (0, 0, 0, 0, 0, '—')
            self._source_details[key] = f'{SOURCE_NAMES[key]} · {state}\n{detail}'
            values = (SOURCE_NAMES[key], state, *metrics)
            if self.sources_tree.exists(key):
                self.sources_tree.item(key, values=values)
                self.sources_tree.move(key, '', index)
            else:
                self.sources_tree.insert('', 'end', iid=key, values=values)
        if not selected and order:
            self.sources_tree.selection_set(order[0])
        self._source_selected()

    def _source_selected(self, _event=None) -> None:
        selected = self.sources_tree.selection()
        self._set_readonly_text(self.source_detail_text, self._source_details.get(selected[0], '') if selected else 'Select a source for access and result details.')

    def _build_settings(self) -> None:
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(self.settings_tab, background=DARK_COLORS['background'],
                                        highlightthickness=0, borderwidth=0)
        self.settings_canvas.grid(row=0, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(self.settings_tab, orient='vertical', command=self.settings_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        tab = ttk.Frame(self.settings_canvas, padding=(0, 0, 16, 12))
        tab.columnconfigure(1, weight=1)
        window = self.settings_canvas.create_window((0, 0), window=tab, anchor='nw')
        tab.bind('<Configure>', lambda _event:self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox('all')))
        def resize(event):
            self.settings_canvas.itemconfigure(window, width=event.width)
            pending = list(tab.winfo_children())
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if isinstance(widget, ttk.Label) and int(widget.cget('wraplength') or 0):
                    widget.configure(wraplength=max(240, event.width-48))
        self.settings_canvas.bind('<Configure>', resize)
        self.root.bind('<MouseWheel>', self._scroll_settings, add='+')
        self.root.bind('<Button-4>', self._scroll_settings, add='+')
        self.root.bind('<Button-5>', self._scroll_settings, add='+')
        self._settings_controls = []
        fields = (
            ('Artwork provider', self.provider_setting_var, PROVIDERS),
            ('Preferred region', self.region_setting_var, ('eu', 'us', 'jp', 'wor', 'fr', 'de', 'uk', 'es', 'it', 'br', 'au')),
        )
        for row, (label, variable, values) in enumerate(fields):
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=5)
            widget = ttk.Combobox(tab, textvariable=variable, values=values, state='readonly', width=30)
            widget.grid(row=row, column=1, sticky='w', pady=5)
            self._settings_controls.append((widget, 'readonly'))
        ttk.Label(tab, textvariable=self.auth_status_var, wraplength=900).grid(row=2, column=0, columnspan=2, sticky='w', pady=(6, 8))
        ttk.Label(tab, textvariable=self.sources_status_var, wraplength=900).grid(row=3, column=0, columnspan=2, sticky='w', pady=(0, 12))
        account = ttk.LabelFrame(tab, text='Optional accounts · automatic mode also uses configured accounts', padding=10)
        account.grid(row=4, column=0, columnspan=2, sticky='ew')
        account.columnconfigure(1, weight=1)
        self._account_entries = []
        self._credential_entries = []
        for row, (key, label, _env) in enumerate(_CREDENTIALS):
            ttk.Label(account, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=4)
            widget = ttk.Entry(account, textvariable=self._credential_vars[key],
                               show='' if key in ('username', 'igdb_client_id') else '•', width=42)
            widget.grid(row=row, column=1, sticky='ew', pady=4)
            self._credential_entries.append(widget)
            if key in ('username', 'password'):
                self._account_entries.append(widget)
        links = ttk.Frame(account)
        links.grid(row=6, column=0, columnspan=2, sticky='w', pady=(8, 4))
        for column, (label, url) in enumerate((
            ('TheGamesDB API setup', 'https://api.thegamesdb.net/key.php'),
            ('IGDB API setup', 'https://api-docs.igdb.com/'),
            ('MobyGames API subscription', 'https://www.mobygames.com/api/subscribe/'))):
            ttk.Button(links, text=label, command=lambda url=url:webbrowser.open(url)).grid(row=0, column=column, padx=(0, 8))
        ttk.Label(account, text='Data by MobyGames.com', foreground=DARK_COLORS['accent']).grid(row=7, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.remember_credentials_button = ttk.Checkbutton(account, text='Remember credentials for this Windows account',
                                                           variable=self.remember_credentials_var)
        self.remember_credentials_button.grid(row=8, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Label(account, text=('Saved credentials are tied to this Windows account; other accounts or PCs require entry again. Environment variables override saved values.'
                  if self._remember_available else 'Credentials stay in this session. Remembering credentials requires Windows account protection.'),
                  wraplength=860).grid(row=9, column=0, columnspan=2, sticky='w', pady=(4, 0))
        rate = ttk.Frame(tab)
        rate.grid(row=5, column=0, columnspan=2, sticky='w', pady=12)
        for column, (label, variable, minimum, maximum, increment) in enumerate((
            ('Seconds between requests', self.interval_setting_var, .25, 60, .25),
            ('Concurrent requests (1–16)', self.workers_setting_var, 1, 16, 1))):
            ttk.Label(rate, text=label).grid(row=0, column=column*2, padx=(0, 8))
            widget = ttk.Spinbox(rate, textvariable=variable, from_=minimum, to=maximum, increment=increment, width=8)
            widget.grid(row=0, column=column*2+1, padx=(0, 16))
            self._settings_controls.append((widget, 'normal'))
        self.fast_preset_button = ttk.Button(rate, text='Fast preset · 8 / 0.25 s', command=self.use_fast_preset)
        self.fast_preset_button.grid(row=0, column=4)
        self._settings_controls.append((self.fast_preset_button, 'normal'))
        ttk.Label(tab, text='Per-provider request limits and account quotas may reduce concurrency.', wraplength=900).grid(row=6, column=0, columnspan=2, sticky='w')
        options = ttk.LabelFrame(tab, text='Unattended coverage', padding=10)
        options.grid(row=7, column=0, columnspan=2, sticky='ew', pady=12)
        for row, (label, variable) in enumerate((
            ('Use another-region cover for the same game', self.allow_other_regions_var),
            ('Use base-game cover for revisions/discs', self.allow_base_cover_var),
            ('Retry temporary failures in up to two delayed passes', self.retry_failed_var),
            ('Keep PC awake while scraping (Windows)', self.keep_awake_var))):
            widget = ttk.Checkbutton(options, text=label, variable=variable)
            widget.grid(row=row, column=0, sticky='w', pady=3)
            self._settings_controls.append((widget, 'normal'))
        ttk.Label(options, text='Fallback choices are recorded in results. Ambiguous games remain unresolved.', wraplength=880).grid(row=4, column=0, sticky='w', pady=(6, 0))
        media = ttk.Frame(tab)
        media.grid(row=8, column=0, columnspan=2, sticky='w', pady=(0, 12))
        self._media_vars = [tk.BooleanVar(value=value) for value in (True, False, False, False)]
        for row, (label, variable) in enumerate(zip(('Box artwork', 'Manuals (unavailable)', 'Ratings (unavailable)', 'Pad-to-key (unavailable)'), self._media_vars)):
            ttk.Checkbutton(media, text=label, variable=variable, state='disabled').grid(row=row, column=0, sticky='w')
        self.settings_save_button = ttk.Button(tab, text='Save settings', command=self._apply_settings)
        self.settings_save_button.grid(row=9, column=0, sticky='w', pady=(4, 8))
        self.settings_button = self.settings_save_button
        ttk.Label(tab, textvariable=self.settings_status_var, wraplength=900).grid(row=10, column=0, columnspan=2, sticky='w')

    def _scroll_settings(self, event):
        if self.notebook.select() != str(self.settings_tab):
            return
        amount = -1 if getattr(event, 'num', 0) == 4 else 1 if getattr(event, 'num', 0) == 5 else (-1 if event.delta > 0 else 1)
        self.settings_canvas.yview_scroll(amount * 3, 'units')
        return 'break'

    @property
    def busy(self) -> bool:
        return bool(self._worker_kind or (self.engine and self.engine.active))

    def _provider_ready(self) -> bool:
        return ('credentials-load' not in self._background_threads
                and 'sources-check' not in self._background_threads
                and self._source_ready(self.provider_setting_var.get()))

    def _source_ready(self, name) -> bool:
        if name == 'screenscraper':
            return bool(auth.screen_scraper_app_credentials() and self.account_username_var.get().strip()
                        and self.account_password_var.get().strip())
        if name == 'thegamesdb':
            return bool(self.tgdb_key_var.get().strip())
        if name == 'igdb':
            return bool(self.igdb_id_var.get().strip() and self.igdb_secret_var.get().strip())
        if name == 'mobygames':
            return bool(self.moby_key_var.get().strip())
        return name in ('automatic', 'libretro', 'launchbox', 'gametdb')

    def _refresh_buttons(self) -> None:
        available = not self.busy and not self._closing and 'sources-check' not in self._background_threads
        for button in (self.browse_button, self.detect_button, self.settings_save_button):
            button.configure(state='normal' if available else 'disabled')
        self.scan_button.configure(state='normal' if available and self.retrobat_root else 'disabled')
        has_games = any(self.games_by_system.get(name) for name in self.selection.selected)
        self.start_button.configure(state='normal' if available and self.retrobat_root and has_games and self._provider_ready() else 'disabled')
        for button in (self.select_button, self.clear_button):
            button.configure(state='normal' if available and self.selection.systems else 'disabled')
        self.console_dropdown.configure(state='readonly' if available and self.selection.systems else 'disabled')
        for widget, state in self._settings_controls:
            widget.configure(state=state if available else 'disabled')
        for widget in self._credential_entries:
            widget.configure(state='normal' if available else 'disabled')
        self.remember_credentials_button.configure(state='normal' if available and self._remember_available else 'disabled')
        self.use_automatic_button.configure(state='normal' if available and self.provider_setting_var.get() != 'automatic' else 'disabled')
        active = bool(self.engine and self.engine.active)
        state = self.engine.snapshot().state if active else ''
        self.pause_button.configure(text='Resume' if state == 'Paused' else 'Pause', state='normal' if active and state != 'Stopping' and not self._closing else 'disabled')
        checking_sources = 'sources-check' in self._background_threads and not self._diagnostic_cancel.is_set()
        self.stop_button.configure(state='normal' if not self._closing and (active and state != 'Stopping' or self._worker_kind in ('scan', 'detect') or checking_sources) else 'disabled')
        self.cancel_selected_button.configure(state='normal' if active and self.jobs_tree.selection() and not self._closing else 'disabled')
        self.cancel_all_button.configure(state='normal' if active and not self._closing else 'disabled')
        self.export_results_button.configure(state='normal' if self.retrobat_root and not self._closing
                                             and 'export' not in self._background_threads else 'disabled')
        self.check_sources_button.configure(state='normal' if available and self._provider_ready() else 'disabled')
        for page in self._pages.values():
            page['previous'].configure(state='normal' if page['offset'] > 0 and not self._closing else 'disabled')
            page['next'].configure(state='normal' if page['offset']+PAGE_SIZE < page['total'] and not self._closing else 'disabled')

    def _provider_changed(self, *_args) -> None:
        self._refresh_provider()
        self._refresh_buttons()
        if hasattr(self, 'sources_tree'):
            provider = self._display_source_provider()
            self._render_sources(force=not callable(getattr(provider, 'source_statuses', None)))

    def _credential_changed(self, key) -> None:
        self._credential_edited.add(key)
        self.credentials[key] = self._credential_vars[key].get()
        self._provider_changed()

    def _refresh_provider(self) -> None:
        name = self.provider_setting_var.get()
        configured = [SOURCE_NAMES[source] for source in ('screenscraper', 'igdb', 'thegamesdb', 'mobygames') if self._source_ready(source)]
        unconfigured = [SOURCE_NAMES[source] for source in ('screenscraper', 'igdb', 'thegamesdb', 'mobygames') if not self._source_ready(source)]
        self.sources_status_var.set('Available without accounts: Libretro, LaunchBox, GameTDB (Wii/GameCube). '
            + ('Configured, unverified accounts: ' + ', '.join(configured) + '. ' if configured else 'Configured accounts: none. ')
            + ('Unconfigured optional sources: ' + ', '.join(unconfigured) + '.' if unconfigured else 'All optional accounts configured.')
            + ' Actual service requests verify access; cached preparation alone does not. See Sources to check access and activity.')
        if name == 'screenscraper':
            self.auth_status_var.set(auth.screen_scraper_status())
            if not auth.screen_scraper_app_credentials():
                self.provider_var.set(auth.screen_scraper_status())
            elif not self.account_username_var.get().strip() or not self.account_password_var.get().strip():
                self.provider_var.set('ScreenScraper: enter your username and password in Settings before starting.')
            else:
                self.provider_var.set('ScreenScraper account configured, unverified · Actual service requests verify access.')
        elif name == 'libretro':
            self.provider_var.set('Libretro · Ready to download box artwork without an account.')
        elif name == 'launchbox':
            self.provider_var.set('LaunchBox · No account needed. First use downloads a catalogue of about 110 MB; later runs use its cache.')
        elif name == 'gametdb':
            self.provider_var.set('GameTDB · No account needed. Verified Wii and GameCube front covers.')
        elif name in ('thegamesdb', 'igdb', 'mobygames'):
            self.provider_var.set(f'{SOURCE_NAMES[name]} · Credentials configured, unverified; actual service requests verify access.'
                                  if self._source_ready(name) else f'{SOURCE_NAMES[name]} · Unconfigured. Add your authorized API credentials in Settings.')
        else:
            chain = ['Libretro', 'LaunchBox', 'GameTDB'] + configured
            self.provider_var.set('Automatic · ' + ' → '.join(chain) + '. Public sources work without an account; GameTDB covers Wii/GameCube.')
        if 'credentials-load' in self._background_threads:
            self.provider_var.set(CREDENTIALS_LOADING + ' Start will be ready when they are loaded.')
        if name != 'screenscraper':
            self.auth_status_var.set(self.provider_var.get())

    def use_automatic(self) -> None:
        if self.busy or self._closing:
            return
        self.provider_setting_var.set('automatic')
        self.settings = replace(self.settings, provider='automatic')
        self._persist()
        self.status_var.set('Automatic sources selected: public sources, then configured accounts. Choose a console and start scraping.')

    def use_fast_preset(self) -> None:
        if self.busy or self._closing:
            return
        self.interval_setting_var.set('0.25')
        self.workers_setting_var.set('8')
        self.settings_status_var.set('Fast preset selected: 8 workers / 0.25 seconds. Per-provider limits still apply. Save settings or start to use it.')

    def use_libretro(self) -> None:
        if self.busy or self._closing:
            return
        self.provider_setting_var.set('libretro')
        self.settings = replace(self.settings, provider='libretro')
        self._persist()
        self.status_var.set('Libretro selected. Choose a console and start scraping.')

    def _filter_changed(self, *_args) -> None:
        if self._filter_after:
            self.root.after_cancel(self._filter_after)
        self._filter_after = self.root.after(150, self._render_systems)

    def _render_systems(self) -> None:
        if self._filter_after:
            self.root.after_cancel(self._filter_after)
        self._filter_after = None
        focused = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for system in self.selection.visible(self.filter_var.get()):
            games = self.games_by_system.get(system.name, ())
            if system.name not in self._existing_counts:
                covered = {game.key for game in games if game.existing_artwork is not None}
                self._covered_game_keys.update(covered)
                self._existing_counts[system.name] = len(covered)
            existing = self._existing_counts[system.name]
            checked = system.name in self.selection.selected
            self.tree.insert('', 'end', iid=system.name, image=self._checkbox_images[checked],
                             tags=('checked',) if checked else (),
                             values=(system.fullname, len(games), existing, len(games)-existing))
        if focused and self.tree.exists(focused):
            self.tree.focus(focused)
            self.tree.selection_set(focused)
        self._console_names = {}
        labels = []
        for system in self.selection.systems:
            label = f'{system.fullname} ({system.name})'
            labels.append(label)
            self._console_names[label] = system.name
        self.console_dropdown.configure(values=labels)
        selected_systems = [system for system in self.selection.systems if system.name in self.selection.selected]
        if len(selected_systems) == 1:
            system = selected_systems[0]
            self.console_var.set(f'{system.fullname} ({system.name})')
        else:
            self.console_var.set(f'{len(selected_systems)} consoles selected' if selected_systems else 'Choose one console')
        names = [system.fullname for system in selected_systems]
        summary = ', '.join(names[:4])
        if len(names) > 4:
            summary += f' · +{len(names)-4} more'
        self.selection_var.set(f'Selected consoles ({len(names)}): {summary}' if names else 'Selected consoles: none')
        self._refresh_scope_counts()
        self._refresh_buttons()

    def _refresh_scope_counts(self) -> None:
        selected_systems = [system for system in self.selection.systems if system.name in self.selection.selected]
        self._selected_games = sum(len(self.games_by_system.get(system.name, ())) for system in selected_systems)
        self._selected_existing = sum(self._existing_counts.get(system.name,0) for system in selected_systems)
        missing = self._selected_games-self._selected_existing
        self.scope_var.set(f'{self._selected_games:,} games selected · {self._selected_existing:,} existing covers · {missing:,} missing covers')
        self.start_button.configure(text=f'Start scraping · {self._selected_games:,} games · {missing:,} need artwork')

    def _refresh_artwork_counts(self, *, force=False) -> None:
        updates = getattr(self.engine, 'take_artwork_updates', None)
        getter = updates if callable(updates) else getattr(self.engine, 'artwork_counts', None)
        if not callable(getter):
            return
        now = time.monotonic()
        if not force and now-self._last_artwork_check < .5:
            return
        self._last_artwork_check = now
        counts = getter()
        if callable(updates):
            counts_by_system = {}
            for key, name in counts.items():
                if name not in self.games_by_system or key in self._covered_game_keys:
                    continue
                self._covered_game_keys.add(key)
                counts_by_system[name] = counts_by_system.get(name,self._existing_counts.get(name,0))+1
            counts = counts_by_system
        changed = False
        for name, covered in counts.items():
            games = self.games_by_system.get(name)
            if games is None:
                continue
            # Aggregate-only engines may still report partial preflight counts.
            existing = max(self._existing_counts.get(name,0), min(len(games),covered))
            if existing == self._existing_counts.get(name,0):
                continue
            self._existing_counts[name] = existing
            changed = True
            if self.tree.exists(name):
                self.tree.set(name, 'existing', existing)
                self.tree.set(name, 'missing', len(games)-existing)
        if changed:
            self._refresh_scope_counts()

    def select_console(self, name: str) -> None:
        if self.busy or self._closing:
            return
        name = self._console_names.get(name, name)
        self.selection.choose(name)
        if name in self.selection.selected and not any(
                system.name == name for system in self.selection.visible(self.filter_var.get())):
            self.filter_var.set('')
        self._render_systems()
        if self.tree.exists(name):
            self.tree.focus(name)
            self.tree.selection_set(name)
            self.tree.see(name)

    def _console_chosen(self, _event=None) -> None:
        self.select_console(self._console_names.get(self.console_var.get(), self.console_var.get()))

    def select_all(self, selected=True) -> None:
        if not self.busy and not self._closing:
            self.selection.select_all(selected)
            self._render_systems()

    def _toggle(self, name: str) -> None:
        if not self.busy and not self._closing:
            self.selection.toggle(name)
            self._render_systems()

    def _click_row(self, event):
        name = self.tree.identify_row(event.y)
        if name:
            if self.tree.identify_column(event.x) == '#0':
                self._toggle(name)
            else:
                self.select_console(name)
            return 'break'

    def _space_row(self, _event=None) -> str:
        self._toggle(self.tree.focus())
        return 'break'

    def _select_visible(self, selected: bool) -> None:
        if not self.busy and not self._closing:
            self.selection.select_visible(self.filter_var.get(), selected)
            self._render_systems()

    def _post(self, event) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            self._events.put_nowait(event)

    def _run_background(self, kind: str, operation, *, independent=False) -> None:
        if independent:
            if kind in self._background_threads:
                self._background_pending[kind] = operation
                return
            generation = None
        else:
            self._generation += 1
            generation = self._generation
            self._worker_kind = kind
        def run():
            try:
                self._post((generation, kind, operation(), None))
            except Exception as exc:
                LOG.warning('%s failed (%s)', kind, type(exc).__name__)
                message = (str(exc) if kind in ('scan', 'detect') else
                           'Could not export results. Choose a writable destination and try again.' if kind == 'export' else
                           'Source check stopped.' if kind == 'sources-check' and isinstance(exc, Cancelled) else
                           'Could not finish the source check. Review source details and try again.' if kind == 'sources-check' else
                           'Could not remember credentials. They still work for this session.' if kind == 'credentials-save' else
                           'Could not load saved credentials. Enter them in Settings for this session.' if kind == 'credentials-load' else
                           'Could not apply pending metadata. Check installation write permissions.')
                self._post((generation, kind, None, message))
        thread = threading.Thread(target=run, name=f'NovaCore {kind}', daemon=True)
        if independent:
            self._background_threads[kind] = thread
            if kind == 'credentials-load':
                self.status_var.set(CREDENTIALS_LOADING)
                self._refresh_provider()
        else:
            self._worker_thread = thread
        thread.start()
        self._refresh_buttons()

    def browse(self) -> None:
        if self.busy or self._closing:
            return
        folder = filedialog.askdirectory(parent=self.root, title='Select the folder containing retrobat.exe', initialdir=self.folder_var.get() or None, mustexist=True)
        if folder:
            self._begin_scan(Path(folder))

    def autodetect(self) -> None:
        self._startup_after = None
        if self.busy or self._closing:
            return
        self._cancel_scan.clear()
        preferred = self.folder_var.get().strip()
        extra = [Path(preferred)] if preferred else []
        self.status_var.set('Looking for RetroBat in common installation folders…')
        self.state_var.set('Detecting')
        self._run_background('detect', lambda: detect_installations(extra, first_only=True, cancelled=self._cancel_scan.is_set))

    def scan(self) -> None:
        if self.retrobat_root and not self.busy and not self._closing:
            self._begin_scan(self.retrobat_root)

    def _invalidate_pages(self) -> None:
        self._pending_pages.clear()
        for page in self._pages.values():
            self._page_ticket += 1
            page['ticket'] = self._page_ticket
            page['offset'] = page['total'] = 0
            page['requested'] = page['applied'] = None
            page['tree'].delete(*page['tree'].get_children())
            page['rows'] = {}
            self._set_readonly_text(page['detail_text'], 'Select a game to inspect its result and source attempts.')
            page['label'].set('No games to show · 200 rows per page')

    def _begin_scan(self, folder: Path) -> None:
        if self.busy or self._closing:
            return
        if str(folder) != self.folder_var.get():
            self.selection = ConsoleSelection()
        self.retrobat_root = None
        self.engine = None
        self.games_by_system = {}
        self._existing_counts = {}
        self._covered_game_keys = set()
        self.selection.systems = []
        self.folder_var.set(str(folder))
        self._invalidate_pages()
        self._cancel_scan.clear()
        with self._scan_lock:
            self._latest_scan = (0, 0, 'Reading console configuration…')
        self._render_systems()
        self.state_var.set('Scanning')
        self.status_var.set('Reading console configuration…')
        self.progress.configure(maximum=1, value=0)
        self.progress_var.set('0 / 0 consoles scanned')
        self.counts_var.set('Existing: 0   Downloaded: 0   Missing: 0   Failed: 0   Cancelled: 0   Queued metadata: 0')
        def progress(done, total, name):
            with self._scan_lock:
                self._latest_scan = (done, total, name)
        def operation():
            result = scan_library(folder, cancelled=self._cancel_scan.is_set, progress=progress)
            if result is not None and not self._cancel_scan.is_set():
                root, _systems, games = result
                try:
                    recover_scanned_artwork(root, chain.from_iterable(games.values()), cancelled=self._cancel_scan.is_set)
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                    LOG.warning('Artwork recovery deferred (%s).', type(exc).__name__)
            return None if self._cancel_scan.is_set() else result
        self._run_background('scan', operation)

    def _make_provider(self):
        cache = data_directory() / 'cache'
        options = dict(region=self.settings.region, interval=self.settings.interval, workers=self.settings.workers,
                       match_policy=MatchPolicy(allow_other_regions=self.settings.allow_other_regions,
                                                allow_base_cover=self.settings.allow_base_cover))
        credentials = self.credentials
        def make(name):
            if name == 'screenscraper':
                application = auth.screen_scraper_app_credentials()
                if application is None:
                    raise ValueError(auth.screen_scraper_status())
                if not credentials['username'].strip() or not credentials['password'].strip():
                    raise ValueError('Enter your ScreenScraper username and password in Settings first.')
                return ScreenScraperProvider(cache, devid=application[0], devpassword=application[1],
                    username=credentials['username'], password=credentials['password'], **options)
            if name == 'thegamesdb':
                return TheGamesDBProvider(cache, api_key=credentials['tgdb_api_key'], **options)
            if name == 'igdb':
                return IGDBProvider(cache, client_id=credentials['igdb_client_id'],
                                    client_secret=credentials['igdb_client_secret'], **options)
            if name == 'mobygames':
                return MobyGamesProvider(cache, api_key=credentials['mobygames_api_key'], **options)
            return {'libretro':LibretroProvider, 'launchbox':LaunchBoxProvider, 'gametdb':GameTDBProvider}[name](cache, **options)
        if self.settings.provider == 'automatic':
            names = ['libretro', 'launchbox', 'gametdb']
            if auth.screen_scraper_app_credentials() and credentials['username'].strip() and credentials['password'].strip():
                names.append('screenscraper')
            if credentials['igdb_client_id'].strip() and credentials['igdb_client_secret'].strip():
                names.append('igdb')
            if credentials['tgdb_api_key'].strip():
                names.append('thegamesdb')
            if credentials['mobygames_api_key'].strip():
                names.append('mobygames')
            return FallbackProvider([make(name) for name in names], workers=self.settings.workers)
        if not self._source_ready(self.settings.provider):
            raise ValueError(f'{SOURCE_NAMES.get(self.settings.provider, self.settings.provider)} is unconfigured. Enter authorized credentials in Settings first.')
        return FallbackProvider([make(self.settings.provider)], workers=self.settings.workers)

    def start(self) -> None:
        if self.busy or self._closing or not self.retrobat_root:
            return
        games = self.selection.games(self.games_by_system)
        if not games:
            self.status_var.set('Choose a console or check consoles before starting.')
            return
        self._refresh_artwork_counts(force=True)
        games = [game for game in games if game.existing_artwork is None
                 and game.key not in self._covered_game_keys]
        if not games:
            self.status_var.set('No missing artwork in the selected consoles. Rescan to check for library changes.')
            return
        if 'credentials-load' in self._background_threads:
            self.status_var.set(CREDENTIALS_LOADING)
            return
        if 'sources-check' in self._background_threads:
            self.status_var.set('Checking source access… Wait for the check to finish before starting.')
            return
        if not self._apply_settings():
            self.show_settings()
            return
        try:
            provider = self._make_provider()
            self._diagnostic_provider = None
            self.engine = ScrapeEngine(self.retrobat_root, provider,
                                       retry_passes=2 if self.settings.retry_failed else 0,
                                       keep_awake=self.settings.keep_awake)
            self._run_scope = self.selection_var.get() + ' · ' + self.scope_var.get()
            self.engine.start(games)
            self._render_sources(force=True)
            self._invalidate_pages()
            self.notebook.select(self.downloads_tab)
            self._request_page('jobs', force=True)
        except ValueError as exc:
            self.status_var.set(str(exc))
            self.show_settings()
        except Exception as exc:
            LOG.warning('Could not start scraping (%s)', type(exc).__name__)
            self.status_var.set('Could not start scraping. Check settings and application write permissions.')
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
        if 'sources-check' in self._background_threads:
            self._diagnostic_cancel.set()
            self.status_var.set('Stopping the source check…')
        if self._worker_kind in ('scan', 'detect'):
            self._cancel_scan.set()
            self.status_var.set('Stopping the scan…')
        self._refresh_buttons()

    def cancel_selected(self) -> None:
        if self.engine and self.engine.active:
            keys = self.jobs_tree.selection()
            if keys:
                self.engine.cancel_jobs(keys)
                self.status_var.set(f'Cancellation requested for {len(keys)} selected games. Completed files are kept.')
                self._request_page('jobs', force=True)

    def cancel_all(self) -> None:
        if self.engine and self.engine.active:
            self.engine.cancel_all()
            self.status_var.set('Cancelling queued and active work. Completed artwork files are kept.')
            self._refresh_buttons()
            self._request_page('jobs', force=True)

    def export_results(self) -> None:
        if self._closing or not self.retrobat_root or 'export' in self._background_threads:
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title='Choose a CSV report destination',
            defaultextension='.csv', initialfile='novacore-artwork-results.csv',
            filetypes=(('CSV report', '*.csv'),))
        if filename:
            root, destination = self.retrobat_root, Path(filename)
            self._export_destination = destination
            self.status_var.set('Exporting all artwork results…')
            self._run_background('export', lambda:State.export_report(root, destination), independent=True)

    def check_sources(self) -> None:
        if self.busy or self._closing or not self._provider_ready():
            return
        if not self._apply_settings():
            self.show_settings()
            return
        try:
            provider = self._make_provider()
        except Exception as exc:
            LOG.warning('Could not configure source check (%s)', type(exc).__name__)
            self.status_var.set('Could not configure the source check. Review Settings and source access requirements.')
            return
        self._diagnostic_provider = provider
        self._diagnostic_cancel.clear()
        self.notebook.select(self.sources_tab)
        self.status_var.set('Checking sources with actual service requests… Review statuses and request counts below.')
        self._render_sources(force=True)
        def checkpoint():
            if self._diagnostic_cancel.is_set():
                raise Cancelled()
        def check():
            try:
                provider.check_sources(checkpoint)
                return 'Source check finished. Review each source status and detail; cover availability is checked per game.'
            finally:
                provider.close()
        self._run_background('sources-check', check, independent=True)

    def _persist(self) -> None:
        if self._smoke_test:
            return
        self.settings = replace(self.settings, retrobat_folder=str(self.retrobat_root or self.folder_var.get()),
                                selected_systems=sorted(self.selection.selected))
        try:
            save_settings(self.settings)
        except OSError:
            LOG.warning('Settings could not be saved.')
            self.status_var.set('Settings could not be saved. Use a writable application folder.')

    def _apply_settings(self) -> bool:
        if self.busy or self._closing:
            return False
        try:
            interval = float(self.interval_setting_var.get())
            workers = int(self.workers_setting_var.get())
            if not .25 <= interval <= 60 or not 1 <= workers <= 16:
                raise ValueError
        except ValueError:
            self.settings_status_var.set('Use 0.25–60 seconds between requests and 1–16 concurrent requests.')
            self.status_var.set(self.settings_status_var.get())
            return False
        self.settings = replace(self.settings, provider=self.provider_setting_var.get(), region=self.region_setting_var.get(),
            interval=interval, workers=workers, allow_other_regions=self.allow_other_regions_var.get(),
            allow_base_cover=self.allow_base_cover_var.get(), retry_failed=self.retry_failed_var.get(),
            keep_awake=self.keep_awake_var.get(), remember_credentials=self.remember_credentials_var.get())
        self.credentials = {key:variable.get() for key, variable in self._credential_vars.items()}
        self._persist()
        self.settings_status_var.set('Settings saved. Credentials work for this session.')
        if not self._smoke_test and self._remember_available:
            values = dict(self.credentials)
            remember = self.remember_credentials_var.get()
            loading = 'credentials-load' in self._background_threads
            changed = set(self._credential_edited)
            changed.update(key for key, value in self._initial_credentials.items() if value.strip())
            def store():
                if remember:
                    if loading:
                        # A quick Start must not erase keys whose startup read
                        # has not reached Tk yet. Typed values still take priority.
                        merged = configured_credentials()
                        merged.update({key:value for key, value in values.items() if key in changed})
                        save_credentials(merged)
                    else:
                        save_credentials(values)
                    return 'Settings and credentials saved for this Windows account.'
                clear_credentials()
                return 'Settings saved. Remembered credentials cleared; current values stay in this session.'
            self._run_background('credentials-save', store, independent=True)
        self._refresh_provider()
        return True

    def show_settings(self) -> None:
        self.notebook.select(self.settings_tab)

    def _handle_result(self, event) -> None:
        generation, kind, result, error = event
        if generation is None:
            self._background_threads.pop(kind, None)
            if kind == 'credentials-load':
                if not error:
                    for key, variable in self._credential_vars.items():
                        if key not in self._credential_edited and variable.get() == self._initial_credentials[key]:
                            value = os.environ.get(next(env for name, _label, env in _CREDENTIALS if name == key), '')
                            variable.set(value if value.strip() else result.get(key, ''))
                elif error:
                    self.settings_status_var.set(error)
                self._refresh_provider()
                if self.status_var.get() == CREDENTIALS_LOADING:
                    self.status_var.set(error or 'Provider credentials loaded. Choose a console and start scraping.')
            elif kind == 'credentials-save':
                self.settings_status_var.set(error or result)
            elif kind == 'export':
                self.status_var.set(error or f'Exported {result:,} artwork results to {self._export_destination}.')
            elif kind == 'sources-check':
                self.status_var.set(error or result)
                self._render_sources(force=True)
            pending = self._background_pending.pop(kind, None)
            if pending is not None and (not self._closing or kind == 'credentials-save'):
                self._run_background(kind, pending, independent=True)
            self._refresh_buttons()
            return
        if generation != self._generation:
            return
        self._worker_thread = None
        self._worker_kind = ''
        if error:
            self.state_var.set('Error')
            self.status_var.set(error)
            self._refresh_buttons()
            return
        if kind == 'detect':
            if self._cancel_scan.is_set():
                self.state_var.set('Stopped')
                self.status_var.set('Detection stopped.')
            elif result:
                self._begin_scan(result[0])
            else:
                self.state_var.set('Idle')
                self.status_var.set('No RetroBat installation found. Use Browse to choose its folder.')
        elif kind == 'scan':
            with self._scan_lock:
                self._latest_scan = None
            if result is None:
                self.state_var.set('Stopped')
                self.status_var.set('Scan stopped. Choose a folder to scan again.')
                self._refresh_buttons()
                return
            self.retrobat_root, systems, self.games_by_system = result
            self.folder_var.set(str(self.retrobat_root))
            self.selection.replace_systems(systems)
            self._covered_game_keys = {game.key for games in self.games_by_system.values() for game in games
                                       if game.existing_artwork is not None}
            self._existing_counts = {name: sum(game.existing_artwork is not None for game in games)
                                     for name, games in self.games_by_system.items()}
            self.engine = ScrapeEngine(self.retrobat_root, LibretroProvider(data_directory() / 'cache'))
            self._render_systems()
            self._last_pending_check = 0
            self.state_var.set('Ready')
            self.progress.configure(maximum=1, value=0)
            self.progress_var.set('0 / 0 artwork jobs processed')
            total = sum(len(games) for games in self.games_by_system.values())
            self.status_var.set(f'Scan complete: {total:,} games in {len(systems)} consoles. Choose exactly one console or check a custom selection.' if systems else 'No supported consoles found. Check the installation system configuration and ROM folders.')
            self._persist()
        self._refresh_buttons()

    def _render_progress(self) -> None:
        if (self._diagnostic_provider is not None and not (self.engine and self.engine.active)
                and self._worker_kind not in ('scan', 'detect')):
            checking = 'sources-check' in self._background_threads
            stopped = self._diagnostic_cancel.is_set()
            self.state_var.set('Checking' if checking else 'Stopped' if stopped else 'Finished')
            self.progress.configure(maximum=1, value=0 if checking else 1)
            self.progress_var.set('Checking source access' if checking else 'Source check stopped' if stopped else 'Check finished · review Sources')
            return
        if not self.engine:
            return
        self._refresh_artwork_counts()
        p = self.engine.snapshot()
        checked = getattr(p, 'checked', 0)
        needed = getattr(p, 'needed', 0)
        resolved = getattr(p, 'resolved', 0)
        cancelled = getattr(p, 'cancelled', 0)
        self.counts_var.set(f'Existing: {p.skipped:,}   Downloaded: {p.completed:,}   Missing: {p.missing:,}   Failed: {p.failed:,}   Cancelled: {cancelled:,}   Queued metadata: {p.pending:,}')
        if self._worker_kind not in ('scan', 'detect'):
            if p.state != 'Idle':
                preparing = checked < p.total and p.state not in ('Error', 'Stopped', 'Finished')
                self.state_var.set('Preparing' if preparing and p.state not in ('Paused', 'Stopping') else p.state)
                self.progress.configure(maximum=max(1, p.total if preparing else needed), value=checked if preparing else resolved)
                self.progress_var.set(f'Checking {checked:,} / {p.total:,} selected games' if preparing else f'{resolved:,} / {needed:,} artwork jobs processed')
            if p.pending and not self.engine.active and p.state != 'Error':
                self.status_var.set(p.message if p.message.startswith('Metadata pending for ') else f'{p.pending:,} metadata updates queued. Close RetroBat to apply them automatically.')
            elif self._previous_pending and not p.pending:
                self.status_var.set('Pending metadata applied.')
            elif p.message:
                self.status_var.set(p.message)
        self._previous_pending = p.pending

    def _maybe_apply_pending(self) -> None:
        if self._smoke_test or self._closing or self.busy or not self.engine or 'sources-check' in self._background_threads:
            return
        if time.monotonic()-self._last_pending_check < 5:
            return
        self._last_pending_check = time.monotonic()
        self._run_background('pending', self.engine.apply_pending)

    def _tab_changed(self, _event=None) -> None:
        if self._closing:
            return
        selected = self.notebook.select()
        if selected == str(self.downloads_tab):
            self._request_page('jobs')
        elif selected == str(self.downloaded_tab):
            self._request_page('downloaded')

    def _page_search_changed(self, name) -> None:
        page = self._pages[name]
        if page['after']:
            self.root.after_cancel(page['after'])
        self._reset_page_query(name)
        page['after'] = self.root.after(250, lambda: self._request_page(name))

    def _reset_page_query(self, name) -> None:
        """Invalidate old query results immediately, before its debounce can run."""
        page = self._pages[name]
        self._page_ticket += 1
        page['ticket'] = self._page_ticket
        page['offset'] = 0
        page['requested'] = page['applied'] = None
        self._pending_pages.pop(name, None)

    def _page_status_changed(self, name) -> None:
        self._reset_page_query(name)
        self._request_page(name)

    def _change_page(self, name, direction) -> None:
        page = self._pages[name]
        page['offset'] = max(0, min(page['offset']+direction*PAGE_SIZE, max(0, ((page['total']-1)//PAGE_SIZE)*PAGE_SIZE)))
        self._request_page(name, force=True)

    def _request_page(self, name, *, reset=False, force=False) -> None:
        page = self._pages[name]
        if page['after']:
            self.root.after_cancel(page['after'])
        page['after'] = None
        if self._closing:
            return
        if reset:
            page['offset'] = 0
        if not self.engine:
            page['label'].set('Scan a RetroBat library to view downloads.')
            return
        query = page['query'].get().strip()
        status = page['status'].get()
        statuses = None if status == 'All statuses' else ACTIVE_JOB_STATES if status == 'Active' else (status,)
        revision = getattr(self.engine, 'jobs_revision', 0)
        signature = (id(self.engine), revision, query, statuses, page['offset'])
        if not force and signature == page['requested']:
            active = self._active_page_request
            pending = self._pending_pages.get(name)
            if (signature == page['applied'] or
                    active and active[0] == page['ticket'] or
                    pending and pending[0] == page['ticket']):
                return
        self._page_ticket += 1
        page['ticket'] = self._page_ticket
        page['requested'] = signature
        request = (page['ticket'], name, self.engine, query, statuses, page['offset'], signature)
        page['label'].set('Loading… · 200 rows per page')
        if self._page_thread is not None:
            # At most one pending request per tab. New searches replace that tab only.
            self._pending_pages[name] = request
        else:
            self._start_page_reader(request)

    def _start_page_reader(self, request) -> None:
        ticket, name, engine, query, statuses, offset, signature = request
        self._active_page_request = request
        def read():
            try:
                if name == 'jobs':
                    rows, total = engine.jobs_page(query=query, statuses=statuses, offset=offset, limit=PAGE_SIZE)
                else:
                    rows, total = engine.downloaded_page(query=query, offset=offset, limit=PAGE_SIZE)
                result = (ticket, name, engine, signature, offset, list(rows)[:PAGE_SIZE], total, None)
            except Exception as exc:
                LOG.warning('Could not read %s (%s)', name, type(exc).__name__)
                result = (ticket, name, engine, signature, offset, [], 0, 'Could not load this page. Try Refresh.')
            self._page_events.put_nowait(result)
        self._page_thread = threading.Thread(target=read, name='NovaCore page reader', daemon=True)
        self._page_thread.start()

    def _drain_pages(self) -> None:
        try:
            ticket, name, engine, signature, offset, rows, total, error = self._page_events.get_nowait()
        except queue.Empty:
            return
        self._page_thread = None
        self._active_page_request = None
        page = self._pages[name]
        if not self._closing and ticket == page['ticket'] and engine is self.engine:
            page['total'] = total
            if not error and offset and offset >= total:
                # Active queues and filtered history can shrink while a page is open.
                page['offset'] = max(0, ((total-1)//PAGE_SIZE)*PAGE_SIZE)
                page['requested'] = None
                self._request_page(name, force=True)
            else:
                selected = set(page['tree'].selection())
                page['rows'] = {row.key:row for row in rows}
                page['tree'].delete(*page['tree'].get_children())
                for row in rows:
                    parts = [row.artwork if name == 'downloaded' else row.detail or row.artwork]
                    parts.extend(str(getattr(row, field, '')) for field in ('source', 'region', 'reason', 'match_note'))
                    if getattr(row, 'attempts', 0) > 1:
                        parts.append(f'{row.attempts} attempts')
                    detail = ' · '.join(dict.fromkeys(part for part in parts if part))
                    page['tree'].insert('', 'end', iid=row.key, values=(row.title, row.console, row.status.replace('_', ' ').capitalize(), detail))
                selected.intersection_update(page['tree'].get_children())
                if selected:
                    page['tree'].selection_set(*selected)
                self._job_selected(name)
                page['applied'] = signature
                first = offset+1 if total else 0
                last = min(offset+len(rows), total)
                page['label'].set(error or f'{first:,}–{last:,} of {total:,} games · 200 rows per page')
                self._refresh_buttons()
        if not self._closing and self._page_thread is None:
            while self._pending_pages:
                pending_name = next(iter(self._pending_pages))
                request = self._pending_pages.pop(pending_name)
                if request[0] == self._pages[pending_name]['ticket'] and request[2] is self.engine:
                    self._start_page_reader(request)
                    break

    def _maybe_page_refresh(self) -> None:
        if time.monotonic()-self._last_page_check < .5:
            return
        self._last_page_check = time.monotonic()
        self._tab_changed()

    def _poll(self) -> None:
        self._poll_after = None
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if not self._closing or event[0] is None:
                self._handle_result(event)
        self._drain_pages()
        if self._closing:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_kind = ''
            if not self.busy and (self._page_thread is None or not self._page_thread.is_alive()) and not any(
                    thread.is_alive() for thread in self._background_threads.values()) and not self._background_pending:
                self.root.destroy()
                return
        else:
            if self._worker_kind == 'scan':
                with self._scan_lock:
                    latest = self._latest_scan
                if latest:
                    done, total, name = latest
                    self.progress.configure(maximum=max(1, total), value=done)
                    self.progress_var.set(f'{done:,} / {total:,} consoles scanned')
                    self.status_var.set(f'Scanning {name} ({done}/{total} consoles)…' if total else name)
            self._render_progress()
            self._render_sources()
            self._maybe_apply_pending()
            self._maybe_page_refresh()
            self._refresh_buttons()
        self._poll_after = self.root.after(100, self._poll)

    def close(self) -> None:
        if self._closing:
            return
        self._persist()
        self._closing = True
        self._diagnostic_cancel.set()
        self._cancel_scan.set()
        self._pending_pages.clear()
        self._background_pending = {kind:operation for kind, operation in self._background_pending.items()
                                    if kind == 'credentials-save'}
        if self.engine and self.engine.active:
            self.engine.stop()
        for handle in (self._filter_after, self._startup_after, *(page['after'] for page in self._pages.values())):
            if handle:
                self.root.after_cancel(handle)
        self.status_var.set('Closing after background work stops…')
        self._refresh_buttons()
        if not self.busy and (self._page_thread is None or not self._page_thread.is_alive()) and not any(
                thread.is_alive() for thread in self._background_threads.values()) and not self._background_pending:
            if self._poll_after:
                self.root.after_cancel(self._poll_after)
            self.root.destroy()


def launch(smoke_test: bool = False) -> None:
    """Open the desktop app; smoke mode never detects, scans or contacts a provider."""
    root = tk.Tk()
    root._novacore_smoke_test = smoke_test
    app = App(root)
    if smoke_test:
        root.after(300, app.close)
    root.mainloop()
