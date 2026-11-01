import tempfile
import sqlite3
import threading
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

    def test_downloaded_history_preserves_title_and_artwork_across_reopen(self):
        art = self.path/'images'/'novacore'/'game.png'
        with State(self.root) as state:
            state.downloaded(self.game, art)
            state.applied([state.pending()[0]['key']])
            state.record(self.game, 'cancelled')
        rows, total = State.downloaded_page(self.root)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['title'], 'Game')
        self.assertEqual(rows[0]['system_fullname'], 'NES')
        self.assertEqual(rows[0]['artwork'], 'roms/nes/images/novacore/game.png')
        with State(self.root) as state:
            self.assertEqual(state.pending(), [])

    def test_existing_cover_metadata_does_not_become_download_history(self):
        art = self.path/'images'/'existing.png'
        with State(self.root) as state:
            state.downloaded(self.game, art, history=False)
            self.assertEqual(len(state.pending()), 1)
        self.assertEqual(State.downloaded_page(self.root), ([], 0))

    def test_downloaded_history_pages_queries_and_caps_limit(self):
        system = System('gba', 'Nintendo Game Boy Advance', self.path, frozenset({'.gba'}))
        with State(self.root) as state:
            for index in range(225):
                game = Game(system, self.path/f'{index}.gba', f'{index}.gba', f'Adventure {index:03}')
                state.downloaded(game, self.path/'images'/f'{index}.png')
        rows, total = State.downloaded_page(self.root, 'GameBoy adventure', limit=1000)
        self.assertEqual((len(rows), total), (200, 225))
        second, total = State.downloaded_page(self.root, 'game advance', offset=200)
        self.assertEqual((len(second), total), (25, 225))
        self.assertTrue(set(row['key'] for row in rows).isdisjoint(row['key'] for row in second))
        self.assertEqual(State.downloaded_page(self.root, 'absent'), ([], 0))

    def test_history_read_works_on_background_connection_during_writer_lock(self):
        art = self.path/'images'/'game.png'
        with InstallationLock(self.root), State(self.root) as state:
            state.downloaded(self.game, art)
            result = []
            thread = threading.Thread(target=lambda: result.append(State.downloaded_page(self.root)))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result[0][1], 1)

    def test_absent_history_does_not_create_state_directory(self):
        self.assertEqual(State.downloaded_page(self.root), ([], 0))
        self.assertFalse((self.root/'.novacore-artwork').exists())

    def test_legacy_state_migration_preserves_identity_pending_and_history(self):
        directory = self.root/'.novacore-artwork'
        directory.mkdir()
        db_path = directory/'state.sqlite3'
        with sqlite3.connect(db_path) as db:
            db.execute('''CREATE TABLE jobs (
                key TEXT PRIMARY KEY, system_name TEXT NOT NULL, system_path TEXT NOT NULL,
                relative_path TEXT NOT NULL, status TEXT NOT NULL, artwork TEXT,
                pending INTEGER NOT NULL DEFAULT 0, updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
            db.execute('''INSERT INTO jobs(key,system_name,system_path,relative_path,status,artwork,pending)
                VALUES(?,?,?,?,?,?,?)''', ('roms/nes/Game.nes', 'nes', 'roms/nes', 'Game.nes',
                'downloaded', 'roms/nes/images/game.png', 1))
        # Reading an older database is safe even before a new writer migrates it.
        rows, total = State.downloaded_page(self.root)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['title'], 'Game')
        with State(self.root) as state:
            self.assertEqual(state.pending()[0]['key'], 'roms/nes/Game.nes')
            state.record(self.game, 'skipped')
            self.assertEqual(state.pending()[0]['key'], 'roms/nes/Game.nes')
        rows, total = State.downloaded_page(self.root)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['key'], 'roms/nes/Game.nes')
        self.assertEqual(rows[0]['title'], 'Game')

    def test_later_existing_cover_does_not_replace_completed_history_path(self):
        downloaded = self.path/'images'/'novacore'/'downloaded.png'
        replacement = self.path/'images'/'existing.png'
        with State(self.root) as state:
            state.downloaded(self.game, downloaded)
            state.downloaded(self.game, replacement, history=False)
        rows, total = State.downloaded_page(self.root)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['artwork'], 'roms/nes/images/novacore/downloaded.png')
        with State(self.root) as state:
            self.assertEqual(state.pending()[0]['artwork'], 'roms/nes/images/existing.png')

    def test_history_rejects_symlink_state_directory(self):
        other = self.root/'other'
        other.mkdir()
        try:
            (self.root/'.novacore-artwork').symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaises(ValueError):
            State.downloaded_page(self.root)
