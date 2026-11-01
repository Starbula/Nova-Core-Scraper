"""Selection/worker tests run headless; real Tk checks run where a display exists."""
from dataclasses import asdict, replace
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from novacore_scraper import ui, scan as scan_module
from novacore_scraper.engine import Progress
from novacore_scraper.models import Game, System
from novacore_scraper.settings import Settings


def systems(root=Path('/fixture')):
    return [System('nes', 'Nintendo Entertainment System', root/'nes', frozenset({'.nes'})),
            System('snes', 'Super Nintendo', root/'snes', frozenset({'.sfc'})),
            System('psx', 'Sony PlayStation', root/'psx', frozenset({'.cue'}))]


def gameboy_systems(root=Path('/fixture')):
    return systems(root) + [System('gba', 'Nintendo Game Boy Advance', root/'gba', frozenset({'.gba'}))]


def luminance(color):
    channels = [int(color[index:index+2], 16) / 255 for index in (1, 3, 5)]
    channels = [value / 12.92 if value <= .04045 else ((value+.055)/1.055)**2.4
                for value in channels]
    return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(foreground, background):
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high+.05) / (low+.05)


class DarkThemeTests(unittest.TestCase):
    def test_dark_palette_keeps_text_readable_in_selected_and_disabled_states(self):
        colors = getattr(ui, 'DARK_COLORS', {})
        required = {'background', 'surface', 'field', 'foreground', 'button', 'hover',
                    'disabled_background', 'disabled_foreground', 'selection', 'selection_foreground', 'accent'}
        self.assertTrue(required <= colors.keys(), 'The app needs an explicit dark control palette.')
        for background in ('background', 'surface', 'field', 'button', 'hover'):
            with self.subTest(background=background):
                self.assertLess(luminance(colors[background]), .12)
                self.assertGreaterEqual(contrast(colors['foreground'], colors[background]), 4.5)
        self.assertGreaterEqual(contrast(colors['disabled_foreground'], colors['disabled_background']), 4.5)
        self.assertGreaterEqual(contrast(colors['selection_foreground'], colors['selection']), 4.5)
        self.assertGreaterEqual(contrast(colors['accent'], colors['field']), 3)


class ConsoleSelectionTests(unittest.TestCase):
    def test_default_selection_is_empty_and_flexible_search_finds_game_boy_advance(self):
        selection = ui.ConsoleSelection()
        selection.replace_systems(gameboy_systems())
        self.assertEqual(selection.selected, set())
        self.assertEqual([system.name for system in selection.visible('GameBoy')], ['gba'])
        self.assertEqual([system.name for system in selection.visible('Game Advance')], ['gba'])
        self.assertEqual([system.name for system in selection.visible('boy adv')], ['gba'])

    def test_single_console_choice_replaces_bulk_selection_and_global_clear_ignores_filter(self):
        selection = ui.ConsoleSelection(['nes', 'snes', 'psx'])
        selection.replace_systems(gameboy_systems())
        self.assertTrue(callable(getattr(selection, 'choose', None)))
        selection.choose('gba')
        self.assertEqual(selection.selected, {'gba'})
        self.assertTrue(callable(getattr(selection, 'select_all', None)))
        selection.select_all(True)
        selection.visible('GameBoy')
        selection.select_all(False)
        self.assertEqual(selection.selected, set())

    def test_filtering_and_visible_clear_preserve_hidden_selection(self):
        selection = ui.ConsoleSelection(['nes', 'snes', 'psx'])
        selection.replace_systems(systems())
        self.assertEqual(selection.selected, {'nes', 'snes', 'psx'})
        self.assertEqual([s.name for s in selection.visible(' NINTENDO ')], ['nes', 'snes'])
        selection.select_visible('Nintendo', False)
        self.assertEqual(selection.selected, {'psx'})
        self.assertEqual(len(selection.visible('')), 3)
        selection.select_visible('snes', True)
        self.assertEqual(selection.selected, {'snes', 'psx'})

    def test_rescan_retains_empty_selection_and_removes_disappeared_systems(self):
        empty = ui.ConsoleSelection([])
        empty.replace_systems(systems())
        self.assertEqual(empty.selected, set())
        selection = ui.ConsoleSelection(['nes', 'obsolete'])
        selection.replace_systems(systems())
        self.assertEqual(selection.selected, {'nes'})
        selection.toggle('nes')
        selection.replace_systems(systems())
        self.assertEqual(selection.selected, set())
        selection.toggle('unknown')
        self.assertEqual(selection.selected, set())

    def test_start_uses_selected_scanned_games_including_hidden_consoles(self):
        selection = ui.ConsoleSelection(['nes', 'psx'])
        consoles = systems()
        selection.replace_systems(consoles)
        games = {system.name: [Game(system, system.path/'game.rom', 'game.rom', 'Game')]
                 for system in consoles}
        selection.visible('Sony')
        self.assertEqual([game.system.name for game in selection.games(games)], ['nes', 'psx'])

    def test_background_operation_only_queues_results(self):
        app = ui.App.__new__(ui.App)
        app._generation = 0
        app._worker_kind = ''
        app._events = queue.Queue(maxsize=4)
        owner = threading.get_ident()
        refresh_threads = []
        app._refresh_buttons = lambda: refresh_threads.append(threading.get_ident())
        worker_threads = []

        def operation():
            worker_threads.append(threading.get_ident())
            return 'scanned'

        app._run_background('scan', operation)
        app._worker_thread.join(2)
        self.assertFalse(app._worker_thread.is_alive())
        self.assertTrue(app.busy, 'Controls stay locked until the UI applies the result.')
        self.assertNotEqual(worker_threads, [owner])
        self.assertEqual(refresh_threads, [owner])
        self.assertEqual(app._events.get_nowait(), (1, 'scan', 'scanned', None))

    def test_autodetect_uses_first_match_and_exposes_cancellation(self):
        from types import SimpleNamespace
        app = ui.App.__new__(ui.App)
        app._worker_kind = ''
        app.engine = None
        app._closing = False
        app._settings_window = None
        app._cancel_scan = threading.Event()
        app.folder_var = SimpleNamespace(get=lambda: '/fixture')
        app.status_var = app.state_var = SimpleNamespace(set=lambda value: None)
        app._run_background = lambda kind, operation: operation()
        with patch.object(ui, 'detect_installations', return_value=[]) as detect:
            app.autodetect()
        self.assertTrue(detect.call_args.kwargs.get('first_only'))
        checkpoint = detect.call_args.kwargs['cancelled']
        self.assertFalse(checkpoint())
        app._cancel_scan.set()
        self.assertTrue(checkpoint())

    def test_result_mailbox_is_bounded_and_retains_latest_result(self):
        app = ui.App.__new__(ui.App)
        app._events = queue.Queue(maxsize=4)
        for index in range(100):
            app._post(index)
        self.assertEqual(app._events.qsize(), 4)
        self.assertEqual([app._events.get_nowait() for _ in range(4)], [96, 97, 98, 99])


class TkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.root.withdraw()
        self.root._novacore_smoke_test = True
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(ui, 'load_settings', return_value=Settings()):
            self.app = ui.App(self.root)
        self.addCleanup(self.destroy_root)

    def destroy_root(self):
        try:
            for handle in self.root.tk.call('after', 'info'):
                self.root.after_cancel(handle)
            self.root.destroy()
        except tk.TclError:
            pass

    def pump_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.005)
        self.fail('Timed out waiting for the Tk event loop.')

    def assert_readable_colors(self, foreground, background):
        self.assertTrue(foreground, 'The foreground must be explicitly configured.')
        self.assertTrue(background, 'The background must be explicitly configured.')

        def hex_color(value):
            return '#' + ''.join(f'{channel // 257:02x}' for channel in self.root.winfo_rgb(value))

        foreground, background = hex_color(foreground), hex_color(background)
        self.assertLess(luminance(background), .12)
        self.assertGreaterEqual(contrast(foreground, background), 4.5)

    def test_dark_styles_cover_normal_readonly_disabled_and_selected_controls(self):
        style = ui.ttk.Style(self.root)
        self.assertEqual(style.theme_use(), 'clam', 'Native Windows themes must not replace dark control colors.')
        self.assertLess(luminance(self.root.cget('background')), .12)
        cases = (
            ('TLabel', (), 'background'), ('TButton', (), 'background'),
            ('TButton', ('active',), 'background'), ('TButton', ('pressed',), 'background'),
            ('TButton', ('disabled',), 'background'), ('TEntry', (), 'fieldbackground'),
            ('TEntry', ('readonly',), 'fieldbackground'), ('TEntry', ('disabled',), 'fieldbackground'),
            ('TSpinbox', (), 'fieldbackground'), ('TSpinbox', ('disabled',), 'fieldbackground'),
            ('TCombobox', ('readonly',), 'fieldbackground'), ('TCombobox', ('disabled',), 'fieldbackground'),
            ('Treeview', (), 'background'), ('Treeview', ('selected',), 'background'),
            ('Treeview', ('disabled',), 'background'), ('Treeview.Heading', (), 'background'),
            ('Treeview.Heading', ('active',), 'background'),
            ('TCheckbutton', (), 'background'), ('TCheckbutton', ('disabled',), 'background'),
        )
        for name, state, background in cases:
            with self.subTest(style=name, state=state):
                self.assert_readable_colors(style.lookup(name, 'foreground', state),
                                            style.lookup(name, background, state))

    def test_settings_tab_and_combobox_popup_use_dark_readable_colors(self):
        self.app.show_settings()
        self.assertEqual(self.app.notebook.select(), str(self.app.settings_tab))
        self.assertIsNone(self.app._settings_window)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()))
        window = self.app.settings_tab

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        combobox = next(widget for widget in descendants(window) if isinstance(widget, ui.ttk.Combobox))
        popup = str(self.root.tk.call('ttk::combobox::PopdownWindow', str(combobox)))
        listbox = popup + '.f.l'
        self.assert_readable_colors(self.root.tk.call(listbox, 'cget', '-foreground'),
                                    self.root.tk.call(listbox, 'cget', '-background'))
        self.assert_readable_colors(self.root.tk.call(listbox, 'cget', '-selectforeground'),
                                    self.root.tk.call(listbox, 'cget', '-selectbackground'))

    def test_filter_render_preserves_selection(self):
        self.app.selection.replace_systems(systems())
        self.app._render_systems()
        self.assertEqual(self.app.selection.selected, set())
        self.app._select_visible(True)
        self.app.filter_var.set('Sony')
        self.app._render_systems()
        self.assertEqual(self.app.tree.get_children(), ('psx',))
        self.app._select_visible(False)
        self.app.filter_var.set('')
        self.app._render_systems()
        self.assertEqual(self.app.selection.selected, {'nes', 'snes'})
        self.assertEqual(self.app.tree.item('nes', 'image')[0], str(self.app._checkbox_images[True]))
        self.assertEqual(self.app.tree.item('psx', 'image')[0], str(self.app._checkbox_images[False]))

    def test_scan_delivers_results_on_tk_thread_and_locks_folder_controls(self):
        root = Path(self.temp.name)
        consoles = systems(root)
        owner = threading.get_ident()
        scanner_threads = []
        render_threads = []
        original_render = self.app._render_systems

        def render():
            render_threads.append(threading.get_ident())
            original_render()

        def scan(system, cancelled):
            scanner_threads.append(threading.get_ident())
            return [Game(system, system.path/'game.rom', 'game.rom', 'Game')]

        self.app._render_systems = render
        with patch.object(scan_module, 'validate_retrobat', return_value=root), \
             patch.object(scan_module, 'discover_systems', return_value=consoles), \
             patch.object(scan_module, 'scan_system', side_effect=scan), \
             patch.object(ui, 'LibretroProvider'):
            self.app._begin_scan(root)
            self.assertTrue(self.app.browse_button.instate(['disabled']))
            self.assertTrue(self.app.start_button.instate(['disabled']))
            self.pump_until(lambda: not self.app.busy)
        self.assertEqual(set(render_threads), {owner})
        self.assertTrue(scanner_threads)
        self.assertNotIn(owner, scanner_threads)
        self.assertEqual(sum(map(len, self.app.games_by_system.values())), 3)
        self.assertEqual(self.app.selection.selected, set())
        self.assertTrue(self.app.start_button.instate(['disabled']))
        self.app.select_console('nes')
        self.assertTrue(self.app.start_button.instate(['!disabled']))
        self.assertTrue(self.app.browse_button.instate(['!disabled']))

    def test_pause_resume_and_stopping_button_states(self):
        class Engine:
            active = True
            progress = Progress(total=2, state='Scraping')

            def snapshot(self):
                return self.progress

            def pause(self):
                self.progress = replace(self.progress, state='Paused')

            def resume(self):
                self.progress = replace(self.progress, state='Scraping')

            def stop(self):
                self.progress = replace(self.progress, state='Stopping')

        self.app.engine = Engine()
        self.app._refresh_buttons()
        self.assertTrue(self.app.settings_button.instate(['disabled']))
        self.assertTrue(self.app.pause_button.instate(['!disabled']))
        self.app.pause_resume()
        self.assertEqual(self.app.pause_button.cget('text'), 'Resume')
        self.app.pause_resume()
        self.assertEqual(self.app.pause_button.cget('text'), 'Pause')
        self.app.stop()
        self.assertTrue(self.app.pause_button.instate(['disabled']))
        self.assertTrue(self.app.stop_button.instate(['disabled']))

    def install_console_fixture(self):
        consoles = gameboy_systems(Path(self.temp.name))
        self.app.retrobat_root = Path(self.temp.name)
        self.app.selection.replace_systems(consoles)
        self.app.games_by_system = {
            system.name: [Game(system, system.path/'game.rom', 'game.rom', 'Game')]
            for system in consoles
        }
        self.app._render_systems()

    def test_row_text_and_dropdown_each_choose_exactly_one_console(self):
        self.install_console_fixture()
        self.app.select_all()
        self.root.deiconify()
        self.root.update()
        x, y, width, height = self.app.tree.bbox('gba', '#2')
        self.app.tree.event_generate('<Button-1>', x=x+min(20, width//2), y=y+height//2)
        self.root.update()
        self.assertEqual(self.app.selection.selected, {'gba'})
        self.assertIn('Game Boy Advance', self.app.console_var.get())
        self.assertIn('1 games', self.app.start_button.cget('text'))
        self.app.select_all()
        label = next(value for value in self.app.console_dropdown.cget('values') if '(gba)' in value)
        self.app.console_var.set(label)
        self.app.console_dropdown.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertEqual(self.app.selection.selected, {'gba'})
        self.assertEqual([game.system.name for game in self.app.selection.games(self.app.games_by_system)], ['gba'])

    def test_checkbox_column_uses_drawn_images_and_explicit_multi_selection(self):
        self.install_console_fixture()
        self.root.deiconify()
        self.root.update()
        unchecked = self.app.tree.item('nes', 'image')
        self.assertTrue(unchecked, 'Checkboxes must be drawn independently of font glyph support.')
        self.assertGreaterEqual(self.app._checkbox_images[False].width(), 20)
        self.assertGreaterEqual(self.app._checkbox_images[False].height(), 20)
        self.assertFalse(self.app._checkbox_images[False].transparency_get(2, 2))
        for name in ('nes', 'gba'):
            x, y, width, height = self.app.tree.bbox(name, '#0')
            self.app.tree.event_generate('<Button-1>', x=x+width//2, y=y+height//2)
            self.root.update()
        self.assertEqual(self.app.selection.selected, {'nes', 'gba'})
        self.assertNotEqual(self.app.tree.item('nes', 'image'), unchecked)

    def test_dropdown_reveals_console_hidden_by_search_and_scrolls_to_its_row(self):
        self.install_console_fixture()
        consoles = list(self.app.selection.systems)
        consoles.extend(System(f'fixture{index}', f'Fixture console {index}', Path(self.temp.name)/str(index), frozenset())
                        for index in range(100))
        consoles.append(System('psp', 'Sony PlayStation Portable', Path(self.temp.name)/'psp', frozenset({'.iso'})))
        self.app.selection.replace_systems(consoles)
        self.app.filter_var.set('GameBoy')
        self.app._render_systems()
        self.assertEqual(self.app.tree.get_children(), ('gba',))
        self.root.deiconify()
        self.root.update()
        choice = next(label for label in self.app.console_dropdown.cget('values') if '(psp)' in label)
        self.app.console_var.set(choice)
        self.app.console_dropdown.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertEqual(self.app.filter_var.get(), '')
        self.assertEqual(self.app.selection.selected, {'psp'})
        self.assertEqual(self.app.tree.focus(), 'psp')
        self.assertTrue(self.app.tree.bbox('psp'), 'The selected console must be scrolled into view.')

    def test_deselect_all_clears_hidden_consoles_and_tabs_stay_in_one_window(self):
        self.install_console_fixture()
        self.app.select_button.invoke()
        self.app.filter_var.set('GameBoy')
        self.app._render_systems()
        self.assertEqual(self.app.tree.get_children(), ('gba',))
        self.app.clear_button.invoke()
        self.assertEqual(self.app.selection.selected, set())
        self.assertTrue(self.app.start_button.instate(['disabled']))
        self.assertEqual([self.app.notebook.tab(tab, 'text') for tab in self.app.notebook.tabs()],
                         ['Library', 'Downloads', 'Downloaded', 'Settings'])
        self.app.show_settings()
        self.assertEqual(self.app.notebook.select(), str(self.app.settings_tab))
        self.assertIsNone(self.app._settings_window)

    def test_progress_separates_preparation_existing_and_missing_artwork(self):
        from types import SimpleNamespace
        progress = Progress(total=100, checked=40, state='Scraping')
        self.app.engine = SimpleNamespace(active=False, snapshot=lambda: progress)
        self.app._render_progress()
        self.assertEqual(float(self.app.progress.cget('maximum')), 100)
        self.assertEqual(float(self.app.progress.cget('value')), 40)
        self.assertIn('Checking 40 / 100', self.app.progress_var.get())
        progress = replace(progress, state='Paused')
        self.app._render_progress()
        self.assertEqual(self.app.state_var.get(), 'Paused')
        self.assertEqual(float(self.app.progress.cget('value')), 40)
        progress = Progress(total=100, checked=100, skipped=80, needed=20, resolved=5,
                            completed=5, state='Scraping')
        self.app._render_progress()
        self.assertEqual(float(self.app.progress.cget('maximum')), 20)
        self.assertEqual(float(self.app.progress.cget('value')), 5)
        self.assertIn('5 / 20 artwork jobs processed', self.app.progress_var.get())
        self.assertIn('Existing: 80', self.app.counts_var.get())

    def test_unavailable_application_auth_blocks_start_and_offers_automatic_sources(self):
        self.install_console_fixture()
        self.app.select_console('gba')
        with patch.object(ui.auth, 'screen_scraper_app_credentials', return_value=None):
            self.app.provider_setting_var.set('screenscraper')
            self.app.account_username_var.set('account')
            self.app.account_password_var.set('password')
            self.assertTrue(self.app.start_button.instate(['disabled']))
            self.assertIn('unavailable', self.app.auth_status_var.get())
            self.assertIn('NovaCore', self.app.auth_status_var.get())
            self.assertEqual(len(self.app._account_entries), 2)
            self.assertTrue(self.app.use_automatic_button.instate(['!disabled']))
            self.app.use_automatic_button.invoke()
        self.assertEqual(self.app.provider_setting_var.get(), 'automatic')
        self.assertTrue(self.app.start_button.instate(['!disabled']))

    def test_settings_offer_sources_and_fast_preset_with_new_limits(self):
        provider_widget = self.app._settings_controls[0][0]
        self.assertEqual(tuple(provider_widget.cget('values')), ('automatic','libretro','launchbox','screenscraper'))
        self.assertEqual(self.app.provider_setting_var.get(), 'automatic')
        self.assertIn('Libretro', self.app.provider_var.get())
        self.assertIn('LaunchBox', self.app.provider_var.get())
        self.app.interval_setting_var.set('2')
        self.app.workers_setting_var.set('3')
        self.app.fast_preset_button.invoke()
        self.assertTrue(self.app._apply_settings())
        self.assertEqual((self.app.settings.workers,self.app.settings.interval),(8,.25))
        self.app.workers_setting_var.set('16')
        self.assertTrue(self.app._apply_settings())
        self.app.workers_setting_var.set('17')
        self.assertFalse(self.app._apply_settings())
        self.app.workers_setting_var.set('8')
        self.app.interval_setting_var.set('.24')
        self.assertFalse(self.app._apply_settings())

    def test_automatic_provider_uses_ordered_real_source_chain(self):
        with patch.object(ui, 'data_directory', return_value=Path(self.temp.name)):
            provider = self.app._make_provider()
        self.addCleanup(provider.close)
        self.assertIsInstance(provider, ui.FallbackProvider)
        self.assertEqual([type(source) for source in provider.providers], [ui.LibretroProvider,ui.LaunchBoxProvider])
        self.assertEqual(provider.workers,8)
        self.app.settings = replace(self.app.settings,provider='launchbox')
        with patch.object(ui, 'data_directory', return_value=Path(self.temp.name)):
            standalone = self.app._make_provider()
        self.addCleanup(standalone.close)
        self.assertIsInstance(standalone,ui.LaunchBoxProvider)

    def test_poll_updates_only_reported_console_covers_without_rescanning_games(self):
        from types import SimpleNamespace
        consoles = [system for system in gameboy_systems(Path(self.temp.name)) if system.name in ('nes','gba')]
        self.app.retrobat_root = Path(self.temp.name)
        self.app.selection.replace_systems(consoles)
        self.app.games_by_system = {
            system.name: [Game(system,system.path/f'{index}.rom',f'{index}.rom',f'Game {index}',
                               system.path/'cover.png' if index == 0 else None) for index in range(4)]
            for system in consoles
        }
        self.app._render_systems()
        self.app.select_console('gba')
        self.assertIn('3 missing covers',self.app.scope_var.get())

        class NoGameScan(list):
            def __iter__(self):
                raise AssertionError('Live counts must not scan games on the Tk thread.')

        self.app.games_by_system = {name:NoGameScan(games) for name,games in self.app.games_by_system.items()}
        coverage = {'nes':3,'gba':0}  # GBA preflight has not checked its existing cover yet.
        self.app.engine = SimpleNamespace(active=False,snapshot=lambda:Progress(completed=2,state='Finished'),
                                         artwork_counts=lambda:dict(coverage))
        self.app._poll()
        self.assertEqual(tuple(map(int,self.app.tree.item('nes','values')[2:])),(3,1))
        self.assertEqual(tuple(map(int,self.app.tree.item('gba','values')[2:])),(1,3))
        self.assertIn('3 missing covers',self.app.scope_var.get())
        coverage['gba'] = 3
        self.pump_until(lambda:tuple(map(int,self.app.tree.item('gba','values')[2:])) == (3,1))
        self.assertIn('3 existing covers · 1 missing covers',self.app.scope_var.get())
        self.assertIn('1 need artwork',self.app.start_button.cget('text'))
        self.assertEqual(self.app._existing_counts,{'nes':3,'gba':3})

        # A new run reports partial preflight counts; retained totals cannot regress.
        self.app.engine = SimpleNamespace(active=False,snapshot=lambda:Progress(pending=2),
                                         artwork_counts=lambda:{'gba':0})
        self.app._last_artwork_check = 0
        self.app._poll()
        self.assertEqual(tuple(map(int,self.app.tree.item('gba','values')[2:])),(3,1))
        self.assertEqual(tuple(map(int,self.app.tree.item('nes','values')[2:])),(3,1))

    def test_start_retains_latest_coverage_before_replacing_engine(self):
        from types import SimpleNamespace
        self.install_console_fixture()
        self.app.select_console('gba')
        self.app.engine = SimpleNamespace(active=False,snapshot=lambda:Progress(),artwork_counts=lambda:{'gba':1})
        class NewEngine:
            active = False
            def __init__(self,*args):
                pass
            def start(self,games):
                pass
        with patch.object(ui,'ScrapeEngine',NewEngine), patch.object(self.app,'_make_provider',return_value=object()), \
             patch.object(self.app,'_request_page'):
            self.app.start()
        self.assertEqual(self.app._existing_counts['gba'],1)
        self.assertIn('0 need artwork',self.app.start_button.cget('text'))

    def test_real_engine_covers_update_console_rows_while_metadata_is_pending(self):
        from PIL import Image
        from novacore_scraper.library import artwork_destination
        from novacore_scraper.models import Artwork
        root = Path(self.temp.name)
        consoles = [system for system in gameboy_systems(root/'roms') if system.name in ('nes','gba')]
        games = []
        for system in consoles:
            system.path.mkdir(parents=True)
            for title in (('Existing','New') if system.name == 'nes' else ('New',)):
                game = Game(system,system.path/f'{title}.rom',f'{title}.rom',title)
                game.path.write_bytes(b'ROM unchanged')
                if title == 'Existing':
                    cover = artwork_destination(game)
                    cover.parent.mkdir(parents=True)
                    Image.new('RGB',(2,2),'blue').save(cover)
                    game = replace(game,existing_artwork=cover)
                games.append(game)
        entered, release = threading.Event(), threading.Event()

        class Provider:
            workers = 1
            def prepare(self,checkpoint):
                checkpoint()
            def find(self,game,checkpoint):
                if game.system.name == 'gba':
                    entered.set()
                    while not release.wait(.01):
                        checkpoint()
                return Artwork('https://fixture.invalid/cover.png','fixture')
            def download(self,artwork,destination,checkpoint):
                checkpoint()
                Image.new('RGB',(2,2),'red').save(destination)

        engine = ui.ScrapeEngine(root,Provider(),process_running=lambda:True)
        def finish_engine():
            release.set()
            engine.wait(5)
        self.addCleanup(finish_engine)
        self.app.retrobat_root = root
        self.app.selection.replace_systems(consoles)
        self.app.games_by_system = {system.name:[game for game in games if game.system == system] for system in consoles}
        self.app._render_systems()
        self.app.select_console('gba')
        self.app.engine = engine
        engine.start(games)
        self.pump_until(entered.is_set)
        self.pump_until(lambda:int(self.app.tree.set('nes','existing')) == 2)
        self.assertEqual(int(self.app.tree.set('gba','existing')),0)
        self.assertIn('1 missing covers',self.app.scope_var.get())
        release.set()
        self.pump_until(lambda:not engine.active and int(self.app.tree.set('gba','existing')) == 1)
        self.assertEqual(engine.artwork_counts(),{'nes':2,'gba':1})
        self.assertEqual(engine.snapshot().pending,3)
        self.assertIn('1 existing covers · 0 missing covers',self.app.scope_var.get())
        self.assertIn('0 need artwork',self.app.start_button.cget('text'))
        engine.process_running = lambda:False
        engine.apply_pending()
        self.app._render_progress()
        self.assertEqual(engine.snapshot().pending,0)
        self.assertEqual(self.app._existing_counts,{'nes':2,'gba':1})

    def test_history_pages_load_off_tk_thread_and_render_at_most_200_rows(self):
        from types import SimpleNamespace
        from novacore_scraper.engine import JobSnapshot
        owner = threading.get_ident()
        calls = []
        rows = [JobSnapshot(f'gba/{index}', f'Game {index}', 'GBA', 'completed', f'/covers/{index}.png')
                for index in range(450)]

        def history(**options):
            calls.append((threading.get_ident(), options))
            offset = options['offset']
            return rows[offset:offset+options['limit']], len(rows)

        self.app.engine = SimpleNamespace(active=False, jobs_revision=1, downloaded_page=history,
                                         snapshot=lambda: Progress())
        self.app._request_page('downloaded')
        self.pump_until(lambda: self.app._pages['downloaded']['total'] == 450)
        self.assertEqual(len(self.app.downloaded_tree.get_children()), 200)
        self.assertNotEqual(calls[0][0], owner)
        self.app._change_page('downloaded', 1)
        self.pump_until(lambda: self.app.downloaded_tree.exists('gba/200'))
        self.assertEqual(calls[-1][1]['offset'], 200)
        self.assertEqual(len(self.app.downloaded_tree.get_children()), 200)

    def test_switching_tabs_keeps_valid_offscreen_results_available(self):
        from types import SimpleNamespace
        from novacore_scraper.engine import JobSnapshot
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def jobs(**options):
            entered.set()
            release.wait(3)
            return [JobSnapshot('gba/job', 'Queued game', 'GBA', 'queued')], 1

        self.app.engine = SimpleNamespace(active=False, jobs_revision=1, snapshot=lambda: Progress(),
            jobs_page=jobs,
            downloaded_page=lambda **options: ([JobSnapshot('gba/history', 'Cover', 'GBA', 'completed')], 1))
        self.app._request_page('jobs')
        self.pump_until(entered.is_set)
        self.app._request_page('downloaded')
        release.set()
        self.pump_until(lambda: self.app.downloaded_tree.exists('gba/history'))
        self.app._request_page('jobs')
        self.pump_until(lambda: self.app.jobs_tree.exists('gba/job'))
        self.assertNotIn('Loading', self.app._pages['jobs']['label'].get())

    def test_latest_query_for_each_tab_survives_other_tab_pending_request(self):
        from types import SimpleNamespace
        from novacore_scraper.engine import JobSnapshot
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        queries = []

        def jobs(**options):
            query = options['query']
            queries.append(query)
            if not query:
                entered.set()
                release.wait(3)
            return [JobSnapshot('gba/'+(query or 'initial'), query or 'Initial', 'GBA', 'queued')], 1

        self.app.engine = SimpleNamespace(active=False, jobs_revision=1, snapshot=lambda: Progress(),
            jobs_page=jobs,
            downloaded_page=lambda **options: ([JobSnapshot('gba/history', 'Cover', 'GBA', 'completed')], 1))
        self.app._request_page('jobs')
        self.pump_until(entered.is_set)
        self.app._pages['jobs']['query'].set('superseded')
        self.app._request_page('jobs')
        self.app._pages['jobs']['query'].set('latest')
        self.app._request_page('jobs')
        self.app._request_page('downloaded')
        release.set()
        self.pump_until(lambda: self.app.downloaded_tree.exists('gba/history'))
        self.app._request_page('jobs')
        self.pump_until(lambda: self.app.jobs_tree.exists('gba/latest'))
        self.assertNotIn('superseded', queries)
        self.assertFalse(self.app.jobs_tree.exists('gba/initial'))

    def test_search_resets_page_before_an_immediate_refresh(self):
        from types import SimpleNamespace
        from novacore_scraper.engine import JobSnapshot
        all_rows = [JobSnapshot(f'gba/{index}', f'Game {index}', 'GBA', 'completed') for index in range(450)]
        rare = JobSnapshot('gba/rare', 'Rare game', 'GBA', 'completed')

        def history(**options):
            rows = [rare] if options['query'] else all_rows
            return rows[options['offset']:options['offset']+options['limit']], len(rows)

        self.app.engine = SimpleNamespace(active=False, jobs_revision=1, snapshot=lambda: Progress(), downloaded_page=history)
        page = self.app._pages['downloaded']
        page['offset'] = 200
        self.app._request_page('downloaded')
        self.pump_until(lambda: self.app.downloaded_tree.exists('gba/200'))
        page['query'].set('Rare')
        self.assertEqual(page['offset'], 0, 'Search must reset the page before its debounce or an automatic refresh.')
        self.app._request_page('downloaded', force=True)
        self.pump_until(lambda: self.app.downloaded_tree.exists('gba/rare'))
        self.assertEqual(page['label'].get().split(' · ')[0], '1–1 of 1 games')

    def test_shrinking_active_filter_clamps_and_reloads_current_page(self):
        from types import SimpleNamespace
        from novacore_scraper.engine import JobSnapshot
        remaining = [450]
        rows = [JobSnapshot(f'gba/{index}', f'Game {index}', 'GBA', 'queued') for index in range(450)]

        def jobs(**options):
            active = rows[:remaining[0]]
            return active[options['offset']:options['offset']+options['limit']], len(active)

        self.app.engine = SimpleNamespace(active=False, jobs_revision=1, snapshot=lambda: Progress(), jobs_page=jobs)
        page = self.app._pages['jobs']
        page['status'].set('Active')
        page['offset'] = 200
        self.app._request_page('jobs')
        self.pump_until(lambda: self.app.jobs_tree.exists('gba/200'))
        remaining[0] = 3
        self.app.engine.jobs_revision = 2
        self.app._request_page('jobs', force=True)
        self.pump_until(lambda: page['offset'] == 0 and len(self.app.jobs_tree.get_children()) == 3)
        self.assertEqual(page['label'].get().split(' · ')[0], '1–3 of 3 games')

    def test_cancel_buttons_dispatch_only_selected_keys_and_then_cancel_all(self):
        from types import SimpleNamespace
        cancelled = []
        all_cancelled = []
        self.app.engine = SimpleNamespace(active=True, jobs_revision=1,
            snapshot=lambda: Progress(state='Scraping'),
            cancel_jobs=lambda keys: cancelled.append(tuple(keys)),
            cancel_all=lambda: all_cancelled.append(True),
            jobs_page=lambda **options: ([], 0))
        self.app.jobs_tree.insert('', 'end', iid='gba/one.gba', values=('One', 'GBA', 'Queued', ''))
        self.app.jobs_tree.insert('', 'end', iid='nes/two.nes', values=('Two', 'NES', 'Queued', ''))
        self.app.jobs_tree.selection_set('gba/one.gba')
        self.app._refresh_buttons()
        self.app.cancel_selected_button.invoke()
        self.app.cancel_all_button.invoke()
        self.assertEqual(cancelled, [('gba/one.gba',)])
        self.assertEqual(all_cancelled, [True])

    def test_close_waits_for_background_work_without_blocking_tk(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.app._run_background('scan', lambda: release.wait(3))
        started = time.monotonic()
        self.app.close()
        self.assertLess(time.monotonic()-started, .2)
        self.assertTrue(self.root.winfo_exists())
        release.set()
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            try:
                self.root.update()
                if not self.root.winfo_exists():
                    return
            except tk.TclError:
                return
            time.sleep(.005)
        self.fail('Window did not close after the worker stopped.')

    def test_credentials_never_enter_persisted_settings(self):
        self.app._smoke_test = False
        self.app.credentials = {key: 'SESSION-SECRET' for key, _label, _env in ui._CREDENTIALS}
        with patch.object(ui, 'save_settings') as save:
            self.app._persist()
        self.assertNotIn('SESSION-SECRET', repr(asdict(save.call_args.args[0])))


if __name__ == '__main__':
    unittest.main()
