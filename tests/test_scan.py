"""Console scans overlap with bounded work and remain cancellable/read-only."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from novacore_scraper import scan
from novacore_scraper.library import scan_system


class ConsoleScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'retrobat.exe').write_bytes(b'fixture')
        for name, ext in (('nes','.nes'),('snes','.sfc'),('gba','.gba'),('gb','.gb')):
            folder = self.root/'roms'/name
            folder.mkdir(parents=True)
            (folder/('Game'+ext)).write_bytes(b'unchanged ROM')

    def test_two_consoles_overlap_and_progress_counts_real_completed_work(self):
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        active = 0
        maximum = 0
        calls = 0
        def measured(system, cancelled):
            nonlocal active, maximum, calls
            with guard:
                active += 1
                maximum = max(maximum, active)
                calls += 1
                number = calls
            try:
                if number <= 2:
                    barrier.wait(2)
                return scan_system(system, cancelled)
            finally:
                with guard:
                    active -= 1
        updates = []
        with patch.object(scan, 'scan_system', side_effect=measured):
            root, systems, games = scan.scan_library(self.root, progress=lambda *args: updates.append(args))
        self.assertEqual(root, self.root)
        self.assertEqual(maximum, 2)
        self.assertEqual(set(games), {'nes','snes','gb','gba'})
        self.assertTrue(all(len(value)==1 for value in games.values()))
        self.assertEqual([done for done,total,name in updates], [0,1,2,3,4])
        self.assertTrue(all(total==4 for done,total,name in updates))
        self.assertFalse((self.root/'.novacore-artwork').exists())

    def test_cancel_before_start_does_no_discovery_or_scan(self):
        with patch.object(scan, 'discover_systems') as discover:
            self.assertIsNone(scan.scan_library(self.root, cancelled=lambda:True))
        discover.assert_not_called()

    def test_stop_cancels_remaining_consoles(self):
        stop = threading.Event()
        calls = []
        def worker(system, cancelled):
            calls.append(system.name)
            stop.set()
            return []
        with patch.object(scan, 'scan_system', side_effect=worker):
            self.assertIsNone(scan.scan_library(self.root, cancelled=stop.is_set))
        self.assertLessEqual(len(calls), 2)

    def test_exception_is_reported_instead_of_returning_partial_library(self):
        with patch.object(scan, 'scan_system', side_effect=OSError('fixture unreadable')):
            with self.assertRaisesRegex(OSError,'fixture unreadable'):
                scan.scan_library(self.root)

    def test_failed_console_cancels_its_other_active_worker(self):
        barrier = threading.Barrier(2)
        cancel_seen = threading.Event()
        guard = threading.Lock()
        calls = 0
        def worker(system, cancelled):
            nonlocal calls
            with guard:
                calls += 1
                number = calls
            barrier.wait(2)
            if number == 1:
                raise OSError('fixture unreadable')
            until = time.monotonic()+.3
            while time.monotonic()<until:
                if cancelled():
                    cancel_seen.set()
                    return []
                time.sleep(.005)
            return []
        with patch.object(scan, 'scan_system', side_effect=worker):
            with self.assertRaisesRegex(OSError,'fixture unreadable'):
                scan.scan_library(self.root)
        self.assertTrue(cancel_seen.is_set(), 'A failed scan must cancel the other worker too.')
