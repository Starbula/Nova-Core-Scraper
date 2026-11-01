import tempfile
import unittest
from pathlib import Path
from novacore_scraper.models import System, Game
from novacore_scraper.state import State, InstallationLock


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'roms'/'nes'
        self.path.mkdir(parents=True)
        self.game = Game(System('nes', 'NES', self.path, frozenset({'.nes'})), self.path/'Game.nes', 'Game.nes', 'Game')

    def test_download_and_pending_survive_reopen_and_drive_move(self):
        art = self.path/'images'/'novacore'/'game.png'
        with State(self.root) as state:
            state.downloaded(self.game, art)
        with State(self.root) as state:
            row = state.pending()[0]
            self.assertEqual(row['relative_path'], 'Game.nes')
            self.assertEqual(row['artwork'], 'roms/nes/images/novacore/game.png')
            state.applied([row['key']])
            self.assertEqual(state.pending(), [])

    def test_installation_lock_excludes_second_copy(self):
        with InstallationLock(self.root):
            with self.assertRaises(RuntimeError):
                with InstallationLock(self.root):
                    pass
        with InstallationLock(self.root):
            pass

    def test_rejects_artwork_outside_system(self):
        with State(self.root) as state:
            with self.assertRaises(ValueError):
                state.downloaded(self.game, self.root/'outside.png')

    def test_symlink_state_directory_rejected(self):
        other = self.root/'other'
        other.mkdir()
        try:
            (self.root/'.novacore-artwork').symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError):
            State(self.root)
