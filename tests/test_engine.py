import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from novacore_scraper.models import System, Game, Artwork
from novacore_scraper.engine import ScrapeEngine
from novacore_scraper.state import State


class FakeProvider:
    workers = 2
    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
    def prepare(self, checkpoint):
        checkpoint()
    def find(self, game, checkpoint):
        checkpoint()
        self.calls.append(game.title)
        self.entered.set()
        while not self.release.wait(.01):
            checkpoint()
        if game.title == 'missing':
            return None
        if game.title == 'failed':
            raise OSError('simulated network failure')
        return Artwork('https://example.invalid/box.png', 'fixture')
    def download(self, artwork, destination, checkpoint):
        checkpoint()
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (2,2), 'red').save(destination)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'roms'/'nes'
        self.path.mkdir(parents=True)
        self.system = System('nes', 'NES', self.path, frozenset({'.nes'}))
    def game(self, title):
        p=self.path/(title+'.nes'); p.write_bytes(b'ROM unchanged')
        return Game(self.system,p,p.name,title)
    def test_download_missing_failure_and_rom_integrity(self):
        games=[self.game(x) for x in ('good','missing','failed')]
        engine=ScrapeEngine(self.root, FakeProvider(), process_running=lambda:False)
        engine.start(games)
        self.assertTrue(engine.wait(5))
        progress=engine.snapshot()
        self.assertEqual((progress.completed,progress.missing,progress.failed),(1,1,1))
        self.assertEqual(progress.pending,0)
        self.assertTrue((self.path/'gamelist.xml').is_file())
        for g in games:
            self.assertEqual(g.path.read_bytes(),b'ROM unchanged')

    def test_running_retrobat_defers_then_applies_and_repeated_run_skips(self):
        game=self.game('good')
        provider=FakeProvider()
        engine=ScrapeEngine(self.root,provider,process_running=lambda:True)
        engine.start([game]); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().completed,1)
        self.assertEqual(engine.snapshot().pending,1)
        self.assertFalse((self.path/'gamelist.xml').exists())
        engine.process_running=lambda:False
        engine.apply_pending()
        self.assertEqual(engine.snapshot().pending,0)
        self.assertTrue((self.path/'gamelist.xml').exists())
        engine.start([game]); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().skipped,1)
        self.assertEqual(provider.calls,['good'])

    def test_pause_resume_and_stop_are_cooperative(self):
        provider=FakeProvider(); provider.workers=1; provider.release.clear()
        engine=ScrapeEngine(self.root,provider,process_running=lambda:True)
        engine.start([self.game(str(n)) for n in range(10)])
        self.assertTrue(provider.entered.wait(2))
        engine.pause()
        self.assertEqual(engine.snapshot().state,'Paused')
        engine.resume()
        engine.stop()
        self.assertTrue(engine.wait(3))
        self.assertLess(len(provider.calls),10)
        self.assertEqual(engine.snapshot().state,'Stopped')
        self.assertEqual(engine.snapshot().failed,0)
        with State(self.root) as state:
            self.assertEqual(state.pending(),[])

    def test_screenshot_only_library_downloads_box_and_preserves_screenshot(self):
        from novacore_scraper.library import scan_system
        import xml.etree.ElementTree as ET
        self.game('good')
        screenshot = self.path/'images'/'good-image.png'
        screenshot.parent.mkdir()
        Image.new('RGB',(2,2),'green').save(screenshot)
        original = screenshot.read_bytes()
        (self.path/'gamelist.xml').write_text('<gameList><game><path>./good.nes</path><image>./images/good-image.png</image><favorite>true</favorite></game></gameList>')
        games = scan_system(self.system)
        self.assertIsNone(games[0].existing_artwork)
        engine=ScrapeEngine(self.root,FakeProvider(),process_running=lambda:False)
        engine.start(games); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().completed,1)
        tree=ET.parse(self.path/'gamelist.xml')
        self.assertEqual(tree.findtext('game/image'),'./images/good-image.png')
        self.assertEqual(tree.findtext('game/favorite'),'true')
        self.assertIn('/novacore/',tree.findtext('game/thumbnail'))
        self.assertEqual(screenshot.read_bytes(),original)

    def test_orphan_artwork_recovers_without_provider(self):
        from novacore_scraper.library import artwork_destination
        game=self.game('orphan')
        destination=artwork_destination(game)
        destination.parent.mkdir(parents=True)
        Image.new('RGB',(2,2)).save(destination)
        provider=FakeProvider()
        engine=ScrapeEngine(self.root,provider,process_running=lambda:False)
        engine.start([game]); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().skipped,1)
        self.assertEqual(provider.calls,[])
        self.assertTrue((self.path/'gamelist.xml').exists())

    def test_literal_tilde_path_survives_scan_download_metadata_and_repeat(self):
        from novacore_scraper.library import scan_system
        import xml.etree.ElementTree as ET
        folder = self.path/'~Homebrew'
        folder.mkdir()
        rom = folder/'~Example.nes'
        rom.write_bytes(b'ROM unchanged')
        games = scan_system(self.system)
        self.assertEqual([g.relative_path for g in games], ['~Homebrew/~Example.nes'])
        provider=FakeProvider()
        engine=ScrapeEngine(self.root, provider, process_running=lambda:False)
        engine.start(games); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().state,'Finished',engine.snapshot().message)
        self.assertEqual(engine.snapshot().completed,1)
        self.assertEqual(engine.snapshot().pending,0)
        tree = ET.parse(self.path/'gamelist.xml')
        self.assertEqual(tree.findtext('game/path'),'./~Homebrew/~Example.nes')
        engine.start(scan_system(self.system)); self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().skipped,1)
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(rom.read_bytes(),b'ROM unchanged')

    def test_job_snapshots_freeze_submitted_console_scope_and_show_provider_phases(self):
        gba_path = self.root/'roms'/'gba'
        gba_path.mkdir()
        gba = System('gba', 'Nintendo Game Boy Advance', gba_path, frozenset({'.gba'}))
        game = Game(gba, gba_path/'Advance.gba', 'Advance.gba', 'Advance')
        submitted = [game]
        provider = FakeProvider()
        provider.release.clear()
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        phases = []
        original_find, original_download = provider.find, provider.download
        def find(current, checkpoint):
            phases.append(engine.jobs_page()[0][0].status)
            return original_find(current, checkpoint)
        def download(artwork, destination, checkpoint):
            phases.append(engine.jobs_page()[0][0].status)
            return original_download(artwork, destination, checkpoint)
        provider.find, provider.download = find, download
        engine.start(submitted)
        self.addCleanup(engine.stop)
        submitted.append(self.game('unselected NES'))
        self.assertTrue(provider.entered.wait(2))
        jobs, total = engine.jobs_page(query='GameBoy Advance')
        self.assertEqual(total, 1)
        self.assertEqual((jobs[0].key, jobs[0].console, jobs[0].status),
                         ('gba/Advance.gba', 'Nintendo Game Boy Advance', 'finding'))
        with self.assertRaises(FrozenInstanceError):
            jobs[0].status = 'failed'
        revision = engine.jobs_revision
        provider.release.set()
        self.assertTrue(engine.wait(5))
        self.assertEqual(provider.calls, ['Advance'])
        self.assertEqual(phases, ['finding', 'downloading'])
        self.assertGreater(engine.jobs_revision, revision)
        self.assertEqual(engine.jobs_page(statuses={'completed'})[1], 1)
        self.assertEqual(engine.snapshot().total, 1)

    def test_cancel_queued_and_active_jobs_keeps_other_jobs_running(self):
        provider = FakeProvider()
        provider.workers = 1
        provider.release.clear()
        games = [self.game(name) for name in ('active', 'queued', 'other')]
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        engine.start(games)
        self.addCleanup(engine.stop)
        self.assertTrue(provider.entered.wait(2))
        self.assertEqual(engine.jobs_page(statuses={'queued'})[1], 2)
        engine.cancel_jobs([games[0].key, games[1].key, 'not-in-this-run'])
        provider.release.set()
        self.assertTrue(engine.wait(5))
        jobs, _ = engine.jobs_page()
        self.assertEqual([job.status for job in jobs], ['cancelled', 'cancelled', 'completed'])
        self.assertEqual(provider.calls, ['active', 'other'])
        progress = engine.snapshot()
        self.assertEqual((progress.total, progress.checked, progress.needed, progress.resolved), (3, 3, 3, 3))
        self.assertEqual((progress.cancelled, progress.completed, progress.processed), (2, 1, 3))
        self.assertEqual(progress.state, 'Finished')
        self.assertTrue(Path(jobs[-1].artwork).is_file())
        engine.cancel_jobs([games[-1].key])
        self.assertEqual(engine.jobs_page()[0][-1].status, 'completed')
        self.assertTrue(Path(jobs[-1].artwork).is_file())

    def test_cancel_during_preflight_finishes_preparation_before_other_downloads(self):
        provider = FakeProvider()
        provider.workers = 1
        provider.release.clear()
        games = [self.game(name) for name in ('first', 'cancelled before check', 'last')]
        check_entered = threading.Event()
        check_release = threading.Event()
        def existing(game):
            if game.key == games[0].key and not check_entered.is_set():
                check_entered.set()
                self.assertTrue(check_release.wait(3))
            return None
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        with patch('novacore_scraper.engine.existing_artwork', side_effect=existing):
            engine.start(games)
            self.addCleanup(engine.stop)
            self.assertTrue(check_entered.wait(2))
            engine.cancel_jobs([games[1].key])
            check_release.set()
            self.assertTrue(provider.entered.wait(2))
            progress = engine.snapshot()
            self.assertEqual((progress.checked, progress.total), (3, 3))
            self.assertEqual((progress.needed, progress.resolved, progress.cancelled), (2, 0, 1))
            provider.release.set()
            self.assertTrue(engine.wait(5))
        progress = engine.snapshot()
        self.assertEqual((progress.completed, progress.cancelled, progress.processed), (2, 1, 3))
        self.assertEqual((progress.needed, progress.resolved), (2, 2))
        self.assertNotIn(games[1].title, provider.calls)

    def test_cancel_all_resolves_unsubmitted_jobs_without_marking_failures(self):
        provider = FakeProvider()
        provider.workers = 1
        provider.release.clear()
        games = [self.game(str(index)) for index in range(12)]
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        engine.start(games)
        self.addCleanup(engine.stop)
        self.assertTrue(provider.entered.wait(2))
        engine.cancel_all()
        self.assertTrue(engine.wait(3))
        progress = engine.snapshot()
        self.assertEqual((progress.cancelled, progress.processed, progress.needed, progress.resolved), (12, 12, 12, 12))
        self.assertEqual((progress.failed, progress.state), (0, 'Stopped'))
        self.assertEqual(engine.jobs_page(statuses={'cancelled'})[1], 12)
        self.assertEqual(engine.downloaded_page(), ([], 0))

    def test_pause_blocks_publication_and_individual_cancel_wakes_paused_job(self):
        provider = FakeProvider()
        provider.workers = 1
        provider.release.clear()
        download_started = threading.Event()
        original_download = provider.download
        def download(*args):
            download_started.set()
            original_download(*args)
        provider.download = download
        games = [self.game(name) for name in ('active', 'other')]
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        engine.start(games)
        self.addCleanup(engine.stop)
        self.assertTrue(provider.entered.wait(2))
        engine.pause()
        provider.release.set()
        self.assertFalse(download_started.wait(.1))
        engine.cancel_jobs([games[0].key])
        self.assertEqual(engine.jobs_page(statuses={'cancelled'})[1], 1)
        self.assertEqual(engine.snapshot().state, 'Paused')
        engine.resume()
        self.assertTrue(engine.wait(5))
        self.assertEqual((engine.snapshot().cancelled, engine.snapshot().completed), (1, 1))

    def test_required_progress_excludes_preflight_art_but_resolves_late_art(self):
        from novacore_scraper.library import artwork_destination
        games = [self.game(name) for name in ('existing', 'late', 'good', 'missing', 'failed')]
        existing = artwork_destination(games[0])
        existing.parent.mkdir(parents=True)
        Image.new('RGB', (2, 2)).save(existing)
        provider = FakeProvider()
        original_prepare = provider.prepare
        def prepare(checkpoint):
            original_prepare(checkpoint)
            Image.new('RGB', (2, 2)).save(artwork_destination(games[1]))
        provider.prepare = prepare
        engine = ScrapeEngine(self.root, provider, process_running=lambda: False)
        engine.start(games)
        self.assertTrue(engine.wait(5))
        progress = engine.snapshot()
        self.assertEqual((progress.total, progress.checked, progress.needed, progress.resolved), (5, 5, 4, 4))
        self.assertEqual((progress.completed, progress.skipped, progress.missing, progress.failed, progress.processed), (1, 2, 1, 1, 5))
        self.assertCountEqual(provider.calls, ['good', 'missing', 'failed'])

    def test_resume_refills_queue_when_pause_and_cancel_drain_submitted_futures(self):
        provider = FakeProvider()
        provider.workers = 1
        publication_entered = threading.Event()
        publication_release = threading.Event()
        original_download = provider.download
        def download(artwork, destination, checkpoint):
            if not publication_entered.is_set():
                checkpoint()
                publication_entered.set()
                self.assertTrue(publication_release.wait(3))
                # The image can finish publication after its final checkpoint.
                original_download(artwork, destination, lambda: None)
            else:
                original_download(artwork, destination, checkpoint)
        provider.download = download
        games = [self.game(name) for name in ('publishing', 'queued', 'remaining')]
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        engine.start(games)
        self.addCleanup(engine.stop)
        self.assertTrue(publication_entered.wait(2))
        engine.pause()
        engine.cancel_jobs([games[1].key])
        publication_release.set()
        deadline = time.monotonic() + 2
        while engine.snapshot().completed != 1 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(engine.snapshot().completed, 1)
        self.assertEqual(engine.snapshot().state, 'Paused')
        engine.resume()
        self.assertTrue(engine.wait(5))
        self.assertEqual((engine.snapshot().completed, engine.snapshot().cancelled), (2, 1))

    def test_download_history_survives_engine_reopen_and_repeat_skip(self):
        game = self.game('Persisted title')
        engine = ScrapeEngine(self.root, FakeProvider(), process_running=lambda: False)
        engine.start([game])
        self.assertTrue(engine.wait(5))
        reopened = ScrapeEngine(self.root, FakeProvider(), process_running=lambda: False)
        jobs, total = reopened.downloaded_page(query='Persisted NES')
        self.assertEqual(total, 1)
        self.assertEqual((jobs[0].key, jobs[0].title, jobs[0].status), ('roms/nes/' + game.relative_path, game.title, 'completed'))
        self.assertTrue(Path(jobs[0].artwork).is_file())
        reopened.start([game])
        self.assertTrue(reopened.wait(5))
        self.assertEqual(reopened.snapshot().skipped, 1)
        self.assertEqual(reopened.downloaded_page()[1], 1)

    def test_unexpected_prepare_error_does_not_expose_provider_credentials(self):
        provider = FakeProvider()
        secret = 'devid=private&password=private'
        def prepare(checkpoint):
            raise OSError('Request failed https://example.invalid/?' + secret)
        provider.prepare = prepare
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        with self.assertLogs('novacore_scraper.engine', level='WARNING') as logs:
            engine.start([self.game('good')])
            self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().state, 'Error')
        self.assertNotIn(secret, engine.snapshot().message)
        self.assertNotIn(secret, '\n'.join(logs.output))
        self.assertEqual(engine.jobs_page(statuses={'cancelled'})[1], 1)

    def test_history_keys_distinguish_reconfigured_system_directories(self):
        old = self.game('Game')
        new_path = self.root/'roms'/'custom-nes'
        new_path.mkdir()
        new_system = System('nes', 'NES', new_path, frozenset({'.nes'}))
        new = Game(new_system, new_path/'Game.nes', 'Game.nes', 'Game')
        with State(self.root) as state:
            state.downloaded(old, self.path/'images'/'old.png')
            state.downloaded(new, new_path/'images'/'new.png')
        engine = ScrapeEngine(self.root, FakeProvider())
        rows, total = engine.downloaded_page()
        self.assertEqual(total, 2)
        self.assertEqual({row.key for row in rows}, {'roms/nes/Game.nes', 'roms/custom-nes/Game.nes'})

    def test_large_queue_pages_are_bounded_without_enqueuing_all_futures(self):
        provider = FakeProvider()
        provider.workers = 1
        provider.release.clear()
        entered = threading.Event()
        release = threading.Event()
        third_submitted = threading.Event()
        submissions = []
        class RecordingExecutor(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                submissions.append(future)
                if len(submissions) > 2:
                    third_submitted.set()
                return future
        def prepare(checkpoint):
            entered.set()
            while not release.wait(.01):
                checkpoint()
        provider.prepare = prepare
        games = [Game(self.system, self.path/f'{index}.nes', f'{index}.nes', f'Game {index}') for index in range(20000)]
        engine = ScrapeEngine(self.root, provider, process_running=lambda: True)
        with patch('novacore_scraper.engine.existing_artwork', return_value=None), \
                patch('novacore_scraper.engine.ThreadPoolExecutor', RecordingExecutor):
            engine.start(games)
            self.addCleanup(engine.stop)
            self.assertTrue(entered.wait(5))
            page, total = engine.jobs_page(offset=100, limit=10000)
            self.assertEqual((len(page), total), (200, 20000))
            self.assertEqual(page[0].title, 'Game 100')
            self.assertEqual(engine.jobs_page(query='Game 19999')[1], 1)
            self.assertEqual(provider.calls, [])
            release.set()
            self.assertTrue(provider.entered.wait(2))
            self.assertFalse(third_submitted.wait(.1))
            self.assertEqual(len(submissions), 2)
            engine.cancel_all()
            self.assertTrue(engine.wait(5))
        self.assertEqual(engine.snapshot().cancelled, 20000)
