"""Real Tk + real worker fixtures for the user's console-to-download workflow."""
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from PIL import Image

from novacore_scraper import ui
from novacore_scraper.models import Artwork
from novacore_scraper.engine import ScrapeEngine
from novacore_scraper.settings import Settings


class FixtureProvider:
    workers = 1
    def __init__(self):
        self.games = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
    def prepare(self, checkpoint):
        checkpoint()
    def find(self, game, checkpoint):
        checkpoint()
        self.games.append(game)
        self.started.set()
        while not self.release.wait(.01):
            checkpoint()
        return Artwork('https://example.invalid/fixture.png','fixture')
    def download(self, artwork, destination, checkpoint):
        checkpoint()
        destination.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(4,4),'blue').save(destination)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.window = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.window.geometry('1180x820')
        self.window._novacore_smoke_test = True
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'retrobat.exe').write_bytes(b'fixture never executed')
        for console, ext, count in (('gba','.gba',3),('nes','.nes',7),('psp','.iso',2)):
            directory=self.root/'roms'/console
            directory.mkdir(parents=True)
            for n in range(count):
                (directory/f'Game {n}{ext}').write_bytes(b'ROM unchanged')
        with patch.object(ui,'load_settings',return_value=Settings(selected_systems=['gba','nes','psp'])):
            self.app=ui.App(self.window)
        self.provider=FixtureProvider()
        self.addCleanup(self.cleanup_window)
        self.app._begin_scan(self.root)
        self.pump(lambda:not self.app.busy)

    def pump(self, predicate, timeout=8):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            self.window.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for the actual UI/worker workflow')

    def cleanup_window(self):
        self.provider.release.set()
        if self.app.engine and self.app.engine.active:
            self.app.engine.stop()
            self.app.engine.wait(5)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def start_fixture_job(self):
        with patch.object(self.app,'_make_provider',return_value=self.provider):
            self.app.start()
        self.pump(lambda:self.app.engine is not None and not self.app.engine.active)

    def test_clicking_gba_text_scrapes_only_gba_and_shows_jobs_and_history(self):
        self.assertEqual(self.app.selection.selected,set(), 'Old saved bulk selection must not auto-queue all consoles.')
        self.window.update_idletasks()
        bounds=self.app.tree.bbox('gba','console')
        self.assertTrue(bounds,'GBA row must be visible')
        x,y,w,h=bounds
        self.app.tree.event_generate('<Button-1>',x=x+min(20,w//2),y=y+h//2)
        self.window.update()
        self.assertEqual(self.app.selection.selected,{'gba'})
        self.start_fixture_job()
        self.assertEqual([g.system.name for g in self.provider.games],['gba']*3)
        rows,total=self.app.engine.jobs_page()
        self.assertEqual(total,3)
        self.assertEqual({row.status for row in rows},{'completed'})
        progress=self.app.engine.snapshot()
        self.assertEqual((progress.needed,progress.resolved,progress.completed),(3,3,3))
        history,count=self.app.engine.downloaded_page()
        self.assertEqual(count,3)
        self.assertTrue(all(Path(row.artwork).is_file() for row in history))
        self.assertEqual(len(self.app.notebook.tabs()),4)
        for name in ('nes','psp'):
            self.assertFalse((self.root/'roms'/name/'images'/'novacore').exists())

    def test_dropdown_chooses_one_console_and_settings_stays_in_notebook(self):
        self.assertIn('gba',{system.name for system in self.app.selection.systems})
        values=tuple(self.app.console_dropdown.cget('values'))
        choice=next(label for label in values if 'advance' in label.casefold())
        self.app.console_var.set(choice)
        self.app.console_dropdown.event_generate('<<ComboboxSelected>>')
        self.window.update()
        self.assertEqual(self.app.selection.selected,{'gba'})
        self.app.show_settings()
        self.window.update()
        self.assertEqual(self.app.notebook.select(),str(self.app.settings_tab))
        self.assertFalse(any(isinstance(child,tk.Toplevel) for child in self.window.winfo_children()))

    def test_missing_screenscraper_app_setup_never_starts_provider_work(self):
        self.app.select_console('gba')
        self.app.provider_setting_var.set('screenscraper')
        with patch.dict('os.environ',{},clear=True), patch.object(ui,'ScreenScraperProvider') as provider:
            self.app.start()
        provider.assert_not_called()
        self.assertFalse(self.app.engine and self.app.engine.active)
        self.assertEqual(self.app.notebook.select(),str(self.app.settings_tab))
        self.assertFalse(any(isinstance(child,tk.Toplevel) for child in self.window.winfo_children()))

    def test_downloads_cancel_selected_stops_one_game_and_keeps_other_games(self):
        self.app.select_console('gba')
        self.provider.release.clear()
        with patch.object(self.app,'_make_provider',return_value=self.provider):
            self.app.start()
        self.app.notebook.select(self.app.downloads_tab)
        self.pump(lambda:self.provider.started.is_set() and bool(self.app.jobs_tree.get_children()))
        active_key=self.app.engine.jobs_page(statuses={'finding'})[0][0].key
        self.app.jobs_tree.selection_set(active_key)
        self.app.cancel_selected()
        self.provider.release.set()
        self.pump(lambda:not self.app.engine.active)
        rows,total=self.app.engine.jobs_page()
        self.assertEqual(total,3)
        self.assertEqual(sum(row.status=='cancelled' for row in rows),1)
        self.assertEqual(sum(row.status=='completed' for row in rows),2)
        progress=self.app.engine.snapshot()
        self.assertEqual((progress.needed,progress.resolved,progress.cancelled,progress.completed),(3,3,1,2))
        self.assertEqual(self.app.engine.downloaded_page()[1],2)
