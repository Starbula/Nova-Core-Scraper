import tempfile
import threading
import unittest
from pathlib import Path
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
