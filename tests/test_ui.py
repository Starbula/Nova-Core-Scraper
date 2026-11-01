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

from novacore_scraper import ui
from novacore_scraper.engine import Progress
from novacore_scraper.models import Game, System
from novacore_scraper.settings import Settings


def systems(root=Path('/fixture')):
    return [System('nes', 'Nintendo Entertainment System', root/'nes', frozenset({'.nes'})),
            System('snes', 'Super Nintendo', root/'snes', frozenset({'.sfc'})),
            System('psx', 'Sony PlayStation', root/'psx', frozenset({'.cue'}))]


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
        with patch.object(ui, 'validate_retrobat', return_value=root), \
             patch.object(ui, 'discover_systems', return_value=consoles), \
             patch.object(ui, 'scan_system', side_effect=scan), \
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
