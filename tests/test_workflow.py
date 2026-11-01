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
from novacore_scraper.providers import ProviderError
from novacore_scraper.sources import FallbackProvider
from novacore_scraper.providers import ArtworkUnavailable
from novacore_scraper.state import State
import csv


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
        self.assertEqual(len(self.app.notebook.tabs()),5)
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

    def test_automatic_fallback_updates_open_library_counts_and_skips_repeat_downloads(self):
        class Source(FixtureProvider):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self.downloads = []
            def find(self, game, checkpoint):
                checkpoint()
                self.games.append(game)
                if self.name == 'Libretro' and game.title == 'Game 1':
                    return None
                return Artwork('https://example.invalid/cover.png', self.name)
            def download(self, artwork, destination, checkpoint):
                self.downloads.append(destination.name)
                if self.name == 'Libretro' and destination.name.startswith('Game 2-'):
                    raise ProviderError('A catalogue image was removed.')
                super().download(artwork, destination, checkpoint)
            def close(self):
                pass
        first, second = Source('Libretro'), Source('LaunchBox')
        chain = FallbackProvider([first, second], workers=8)
        self.app.select_console('gba')
        with patch.object(self.app, '_make_provider', return_value=chain):
            self.app.start()
        self.pump(lambda: self.app.engine is not None and not self.app.engine.active)
        self.pump(lambda: self.app._existing_counts.get('gba') == 3)
        self.assertEqual(self.app.engine.snapshot().completed, 3)
        self.assertEqual(self.app.engine.artwork_counts(), {'gba': 3})
        self.assertEqual(len(first.games), 3)
        self.assertEqual({game.title for game in second.games}, {'Game 1', 'Game 2'})
        self.assertEqual(int(self.app.tree.set('gba', 'existing')), 3)
        self.assertEqual(int(self.app.tree.set('gba', 'missing')), 0)
        self.assertEqual(int(self.app.tree.set('nes', 'missing')), 7)
        self.assertIn('0 missing', self.app.scope_var.get())
        self.assertIn('0 need artwork', self.app.start_button.cget('text'))
        self.assertEqual(self.app.engine.downloaded_page()[1], 3)
        jobs, _ = self.app.engine.jobs_page()
        self.assertEqual({job.title: job.detail for job in jobs}, {
            'Game 0': 'Source: Libretro', 'Game 1': 'Source: LaunchBox', 'Game 2': 'Source: LaunchBox'})
        with patch.object(self.app, '_make_provider', return_value=chain):
            self.app.start()
        self.pump(lambda: not self.app.engine.active)
        self.assertEqual((self.app.engine.snapshot().skipped, self.app.engine.snapshot().completed), (3, 0))
        self.assertEqual((len(first.games), len(second.games)), (3, 2))

    def test_configured_run_shows_igdb_checks_and_tgdb_quota_without_losing_fallback(self):
        from novacore_scraper.providers import QuotaError
        class Layer(FixtureProvider):
            def __init__(self, name):
                super().__init__()
                self.name = name
            def find(self, game, checkpoint):
                checkpoint()
                self.games.append(game)
                if self.name == 'TheGamesDB':
                    raise QuotaError('TheGamesDB monthly API allowance exhausted.')
                if self.name == 'IGDB' and game.title != 'Game 0' or self.name == 'MobyGames':
                    return Artwork('https://example.invalid/cover.png', self.name)
                return None
            def close(self):
                pass
        names = ('Libretro', 'LaunchBox', 'GameTDB', 'ScreenScraper', 'IGDB', 'TheGamesDB', 'MobyGames')
        layers = {name:Layer(name) for name in names}
        for key, variable in self.app._credential_vars.items():
            variable.set('fixture-' + key)
        self.app.select_console('gba')
        with patch.object(ui.auth, 'screen_scraper_app_credentials', return_value=('approved-id', 'approved-secret')), \
             patch.object(ui, 'LibretroProvider', return_value=layers['Libretro']), \
             patch.object(ui, 'LaunchBoxProvider', return_value=layers['LaunchBox']), \
             patch.object(ui, 'GameTDBProvider', return_value=layers['GameTDB']), \
             patch.object(ui, 'ScreenScraperProvider', return_value=layers['ScreenScraper']), \
             patch.object(ui, 'IGDBProvider', return_value=layers['IGDB']), \
             patch.object(ui, 'TheGamesDBProvider', return_value=layers['TheGamesDB']), \
             patch.object(ui, 'MobyGamesProvider', return_value=layers['MobyGames']):
            self.app.start()
        self.pump(lambda:self.app.engine is not None and not self.app.engine.active)
        self.app._render_sources(force=True)
        self.assertEqual([source.name for source in self.app.engine.provider.providers], list(names))
        self.assertEqual(self.app.engine.snapshot().completed, 3)
        self.assertEqual(self.app.engine.snapshot().failed, 0)
        self.assertEqual(self.app.sources_tree.set('thegamesdb', 'state'), 'Quota exhausted')
        self.assertEqual(int(self.app.sources_tree.set('igdb', 'lookups')), 3)
        self.assertEqual(int(self.app.sources_tree.set('igdb', 'downloads')), 2)
        self.assertIn('IGDB → TheGamesDB', self.app.source_chain_var.get())
        rows, _ = self.app.engine.jobs_page()
        game = next(row for row in rows if row.title == 'Game 0')
        self.app._request_page('jobs', force=True)
        self.pump(lambda:self.app.jobs_tree.exists(game.key))
        self.app.jobs_tree.selection_set(game.key)
        self.app._show_job_details('jobs')
        detail = self.app._pages['jobs']['detail_text'].get('1.0', 'end')
        for source in names:
            self.assertIn(source, detail)

    def test_unattended_seven_source_run_preserves_scope_reports_and_match_notes(self):
        names = ('Libretro', 'LaunchBox', 'GameTDB', 'TheGamesDB', 'IGDB', 'MobyGames', 'ScreenScraper')
        class Layer(FixtureProvider):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self.downloads = []
            def candidates(self, game, checkpoint):
                checkpoint()
                self.games.append(game)
                if game.title == 'Game 0' and self.name == 'ScreenScraper':
                    yield Artwork('https://example.invalid/last-source.png', self.name, 'eu', matched_title=game.title)
                elif game.title == 'Game 2' and self.name == 'Libretro':
                    yield Artwork('https://example.invalid/removed.png', self.name, 'eu')
                    yield Artwork('https://example.invalid/backup.png', self.name, 'us',
                                  match_note='Other-region cover: US; requested EU.', matched_title=game.title)
            def missing_reason(self, game):
                return ('ambiguous_title', 'Multiple verified game IDs require review.') if self.name == 'LaunchBox' else ('no_title_match', 'No matching title.')
            def download(self, artwork, destination, checkpoint):
                self.downloads.append(artwork.url)
                if artwork.url.endswith('/removed.png'):
                    raise ArtworkUnavailable('The listed artwork is absent.')
                super().download(artwork, destination, checkpoint)
            def close(self):
                pass
        layers = [Layer(name) for name in names]
        self.app.select_console('gba')
        with patch.object(self.app, '_make_provider', return_value=FallbackProvider(layers, workers=8)):
            self.app.start()
        self.pump(lambda: self.app.engine is not None and not self.app.engine.active)
        self.pump(lambda: self.app._existing_counts.get('gba') == 2)
        progress = self.app.engine.snapshot()
        self.assertEqual((progress.total, progress.completed, progress.missing, progress.failed), (3, 2, 1, 0))
        jobs, _ = self.app.engine.jobs_page()
        by_title = {job.title: job for job in jobs}
        self.assertEqual(by_title['Game 0'].source, 'ScreenScraper')
        self.assertEqual(by_title['Game 1'].reason, 'ambiguous_title')
        self.assertIn('Other-region', by_title['Game 2'].match_note)
        self.assertEqual(len(layers[0].downloads), 2)
        report = self.root/'artwork-results.csv'
        self.assertEqual(State.export_report(self.root, report), 3)
        with report.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertTrue(any('Other-region' in str(row) for row in rows))
        self.assertTrue(any('ambiguous_title' in str(row) for row in rows))
        self.assertNotIn('https://', report.read_text(encoding='utf-8-sig'))
        for console, count in (('gba', 3), ('nes', 7), ('psp', 2)):
            for game in self.app.games_by_system[console]:
                self.assertEqual(game.path.read_bytes(), b'ROM unchanged')
        for console in ('nes', 'psp'):
            self.assertFalse((self.root/'roms'/console/'images'/'novacore').exists())
