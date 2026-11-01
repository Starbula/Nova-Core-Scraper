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
    def test_filtering_and_visible_clear_preserve_hidden_selection(self):
        selection = ui.ConsoleSelection()
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

    def test_settings_window_and_combobox_popup_use_dark_readable_colors(self):
        self.app.show_settings()
        window = self.app._settings_window
        self.assertLess(luminance(window.cget('background')), .12)

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
        self.assertEqual(self.app.tree.item('nes', 'values')[0], '[x]')
        self.assertEqual(self.app.tree.item('psx', 'values')[0], '[ ]')

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
        self.assertEqual(len(self.app.selection.games(self.app.games_by_system)), 3)
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
