import stat
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch
from novacore_scraper import paths, state


class XMLPathTextTests(unittest.TestCase):
    def test_literal_spaces_remain_distinct_even_for_missing_entries(self):
        for value in (' Game.nes', 'Game.nes ', './ Game.nes ', ' covers/box.png', '~Game.nes', './~Game.nes'):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value)

    def test_explicit_xml_line_formatting_is_removed(self):
        for value in ('\n ./Game.nes\n', '\r\n  ./folder/Game.nes\r\n ', '\t./images/box.png\t', '  ./images/box.png  ', '  ../shared/box.png  '):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value.strip())

    def test_home_absolute_and_parent_syntax_remains_for_boundary_validation(self):
        for value in ('~/file', '/outside/file', '../file', 'C:/file', '//server/file'):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value)


class SafePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root/'images'/'novacore'/'game.png'

    def test_missing_destination_accepts_equivalent_windows_extended_anchors(self):
        # On Windows, concurrent creation of images/novacore can change the
        # missing-file error during resolve() from WinError 3 to WinError 2.
        # CPython then retains the extended prefix on only one resolved path.
        for root in (r'C:\Users\runneradmin\Temp\library',
                     r'\\server\share\library'):
            extended = ('\\\\?\\UNC\\' + root[2:] if root.startswith('\\\\')
                        else '\\\\?\\' + root)
            for resolved_root, resolved_destination in (
                    (root, extended + r'\images\novacore\game.png'),
                    (extended, root + r'\images\novacore\game.png')):
                with self.subTest(root=resolved_root, destination=resolved_destination):
                    resolved = {self.root: PureWindowsPath(resolved_root),
                                self.destination: PureWindowsPath(resolved_destination)}
                    with patch.object(Path, 'resolve', autospec=True,
                                      side_effect=lambda path: resolved[path]):
                        self.assertEqual(state.safe_path(self.root, self.destination), self.destination)
        self.assertFalse(self.destination.parent.exists())

    def test_windows_extended_anchor_does_not_mask_resolved_escape(self):
        for resolved_root, resolved_destination in (
                (r'C:\library', r'\\?\C:\outside\game.png'),
                (r'\\?\C:\library', r'D:\library\game.png'),
                (r'\\server\share\library', r'\\?\UNC\server\other\library\game.png'),
                (r'\\?\UNC\server\share\library', r'\\server\share\outside\game.png')):
            with self.subTest(root=resolved_root, destination=resolved_destination):
                resolved = {self.root: PureWindowsPath(resolved_root),
                            self.destination: PureWindowsPath(resolved_destination)}
                with patch.object(Path, 'resolve', autospec=True,
                                  side_effect=lambda path: resolved[path]):
                    with self.assertRaises(ValueError):
                        state.safe_path(self.root, self.destination)

    def test_lexical_escape_and_parent_traversal_are_rejected(self):
        for destination in (self.root.parent/'outside.png',
                            self.root/'images'/'..'/'game.png'):
            with self.subTest(destination=destination):
                with self.assertRaises(ValueError):
                    state.safe_path(self.root, destination)

    def test_symlink_root_and_descendant_are_rejected(self):
        target = self.root/'other'
        target.mkdir()
        link = self.root/'images'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation unavailable')
        for root, destination in ((self.root, self.destination),
                                  (link, link/'game.png')):
            with self.subTest(root=root):
                with self.assertRaises(ValueError):
                    state.safe_path(root, destination)

    def test_reparse_point_component_is_rejected(self):
        original_lstat = Path.lstat
        def lstat(path):
            if path == self.root/'images':
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path)
        with patch.object(Path, 'lstat', autospec=True, side_effect=lstat):
            with self.assertRaises(ValueError):
                state.safe_path(self.root, self.destination)
