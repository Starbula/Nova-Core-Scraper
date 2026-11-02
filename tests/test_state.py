import tempfile
import sqlite3
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from novacore_scraper.library import artwork_destination
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

    def test_recover_scanned_artwork_queues_unregistered_own_file_without_history(self):
        art = artwork_destination(self.game)
        art.parent.mkdir(parents=True)
        art.write_bytes(b'Scan-confirmed artwork')
        game = replace(self.game, existing_artwork=art)
        with State(self.root) as state:
            state.record(game, 'downloading', source='Libretro', attempts=1)
            self.assertEqual(state.recover_scanned_artwork([game, game]), 1)
        with State(self.root) as state:
            row = state.pending()[0]
            self.assertEqual(row['relative_path'], 'Game.nes')
            self.assertEqual(row['artwork'], art.relative_to(self.root).as_posix())
            self.assertEqual((row['source'], row['attempts']), ('Libretro', 1))
            self.assertEqual(state.recover_scanned_artwork([game]), 0)
        self.assertEqual(State.downloaded_page(self.root), ([], 0))
        self.assertEqual(art.read_bytes(), b'Scan-confirmed artwork')

    def test_recover_scanned_artwork_preserves_registered_pending_and_applied_rows(self):
        pending = replace(self.game, existing_artwork=artwork_destination(self.game))
        applied = replace(self.game, path=self.path/'Applied.nes', relative_path='Applied.nes', title='Applied')
        applied = replace(applied, existing_artwork=artwork_destination(applied))
        with State(self.root) as state:
            for game in (pending, applied):
                state.downloaded(game, game.existing_artwork, reason='downloaded', source='Libretro', attempts=2)
            state.applied(['roms/nes/Applied.nes'])
            before = [dict(row) for row in state.db.execute('SELECT * FROM jobs ORDER BY key')]
            self.assertEqual(state.recover_scanned_artwork([pending, applied]), 0)
            self.assertEqual([dict(row) for row in state.db.execute('SELECT * FROM jobs ORDER BY key')], before)
            self.assertEqual([row['relative_path'] for row in state.pending()], ['Game.nes'])

    def test_recover_scanned_artwork_ignores_other_existing_artwork_and_missing_games(self):
        other = replace(self.game, existing_artwork=self.path/'images'/'external.png')
        shared = replace(self.game, existing_artwork=self.root/'shared-cover.png')
        with State(self.root) as state:
            self.assertEqual(state.recover_scanned_artwork([other, shared, self.game]), 0)
            self.assertEqual(state.pending(), [])
            self.assertEqual(state.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_recover_scanned_artwork_stops_cooperatively_when_cancelled(self):
        games = [replace(self.game, path=self.path/f'{index}.nes', relative_path=f'{index}.nes') for index in range(3)]
        games = [replace(game, existing_artwork=artwork_destination(game)) for game in games]
        checks = 0
        def cancelled():
            nonlocal checks
            checks += 1
            return checks > 1
        with State(self.root) as state:
            self.assertEqual(state.recover_scanned_artwork(games, cancelled), 1)
        with State(self.root) as state:
            self.assertEqual([row['relative_path'] for row in state.pending()], ['0.nes'])
            self.assertEqual(state.recover_scanned_artwork(games, lambda:True), 0)

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
        with closing(sqlite3.connect(db_path)) as db, db:
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
            self.assertEqual(state.pending()[0]['source_trace'], '')
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

    def test_diagnostics_attempts_and_report_survive_reopen_without_touching_pending(self):
        import csv
        art = self.path/'images'/'downloaded.png'
        with State(self.root) as state:
            state.downloaded(self.game, art, source='Libretro', region='us',
                             match_note='Exact normalized title', attempts=2)
            state.record(self.game, 'failed', reason='temporary_failure',
                         detail='Connection interrupted.', retryable=True, attempts=3)
        with State(self.root) as state:
            pending = state.pending()[0]
            self.assertEqual((pending['pending'], pending['artwork']),
                             (1, 'roms/nes/images/downloaded.png'))
            self.assertEqual((pending['reason'], pending['attempts'], pending['retryable']),
                             ('temporary_failure', 3, 1))
        destination = self.root/'report.csv'
        with InstallationLock(self.root):
            self.assertEqual(State.export_report(self.root, destination), 1)
        with destination.open(newline='', encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]['title'], 'Game')
        self.assertEqual(rows[0]['reason'], 'temporary_failure')
        self.assertEqual(rows[0]['attempts'], '3')
        rows, total = State.jobs_page(self.root, statuses={'failed'})
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['detail'], 'Connection interrupted.')
        history, _ = State.downloaded_page(self.root)
        self.assertEqual((history[0]['source'], history[0]['region'], history[0]['attempts']),
                         ('Libretro', 'us', 2))

    def test_report_reads_legacy_database_without_migration_and_exports_atomically(self):
        directory = self.root/'.novacore-artwork'
        directory.mkdir()
        db_path = directory/'state.sqlite3'
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE jobs (key TEXT PRIMARY KEY,system_name TEXT,system_path TEXT,relative_path TEXT,status TEXT,artwork TEXT,pending INTEGER,updated TEXT)')
            db.execute("INSERT INTO jobs VALUES('roms/nes/Game.nes','nes','roms/nes','Game.nes','missing',NULL,0,'old')")
        destination = self.root/'report.csv'
        destination.write_text('previous report')
        self.assertEqual(State.export_report(self.root, destination), 1)
        self.assertIn('Game', destination.read_text(encoding='utf-8-sig'))
        with closing(sqlite3.connect(db_path)) as db:
            self.assertNotIn('reason', {row[1] for row in db.execute('PRAGMA table_info(jobs)')})
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_report_cannot_replace_recovery_database_or_its_sidecars(self):
        with State(self.root) as state:
            state.record(self.game, 'missing', reason='no_match')
            database = self.root/'.novacore-artwork'/'state.sqlite3'
            for target in (database, Path(str(database)+'-wal'), database.parent/'writer.lock'):
                with self.assertRaises(ValueError):
                    State.export_report(self.root, target)
            self.assertEqual(state.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_report_rejects_rom_artwork_metadata_and_csv_files_in_library_subtrees(self):
        protected = [self.game.path, self.path/'images'/'cover.png', self.path/'gamelist.xml',
                     self.path/'report.csv', self.root/'media'/'report.csv', self.root/'metadata'/'report.csv',
                     self.root/'system'/'report.csv', self.root/'emulationstation'/'report.csv',
                     self.root/'.novacore-artwork'/'report.csv']
        with State(self.root) as state:
            state.record(self.game, 'missing')
        for target in protected:
            with self.subTest(target=target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'Original library bytes')
                with self.assertRaises(ValueError):
                    State.export_report(self.root, target)
                self.assertEqual(target.read_bytes(), b'Original library bytes')

    def test_report_rejects_linked_destination_or_parent_and_keeps_targets(self):
        outside = self.root/'export-target'
        outside.mkdir()
        real = outside/'report.csv'
        real.write_bytes(b'Original destination')
        linked = self.root/'linked.csv'
        folder = self.root/'linked-folder'
        try:
            linked.symlink_to(real)
            folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        for destination in (linked, folder/'report.csv'):
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    State.export_report(self.root, destination)
                self.assertEqual(real.read_bytes(), b'Original destination')
        self.assertTrue(linked.is_symlink())

    def test_report_allows_csv_at_library_root_and_external_report_directory(self):
        external = self.root.parent/(self.root.name+'-reports')
        external.mkdir()
        self.addCleanup(lambda: external.rmdir())
        with State(self.root) as state:
            state.record(self.game, 'missing')
        for target in (self.root/'report.csv', external/'report.CSV'):
            self.assertEqual(State.export_report(self.root, target), 1)
            self.assertIn('Game', target.read_text(encoding='utf-8-sig'))
            target.unlink()

    def test_report_rejects_csv_inside_a_recorded_custom_system_directory(self):
        folder = self.root/'custom-library'
        folder.mkdir()
        custom = Game(System('custom', 'Custom', folder, frozenset({'.bin'})),
                      folder/'Game.bin', 'Game.bin', 'Custom Game')
        destination = folder/'report.csv'
        destination.write_bytes(b'Existing system data')
        with State(self.root) as state:
            state.record(custom, 'missing')
        with self.assertRaises(ValueError):
            State.export_report(self.root, destination)
        self.assertEqual(destination.read_bytes(), b'Existing system data')
