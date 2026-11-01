from __future__ import annotations

import os
from pathlib import Path
from pathlib import PureWindowsPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from xml.dom import minidom

from novacore_scraper.models import System
from novacore_scraper import metadata


def _element_text(element):
    return ''.join(node.data for node in element.childNodes if node.nodeType == node.TEXT_NODE)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.system = System('snes', 'Super Nintendo', self.root, frozenset({'.sfc'}))
        self.gamelist = self.root / 'gamelist.xml'
        self.art = self.root / 'images' / 'novacore' / 'box.png'
        self.art.parent.mkdir(parents=True)
        self.art.write_bytes(b'validated image fixture')
        self.rom = self.root / 'nested' / 'My Game.sfc'
        self.rom.parent.mkdir()
        self.rom.write_bytes(b'never alter the ROM')
        self.updates = {'nested/My Game.sfc': self.art}

    def merge(self, running=lambda: False):
        return metadata.merge_artwork(self.system, self.updates, running)

    def fields(self):
        document = minidom.parse(str(self.gamelist))
        game = document.getElementsByTagName('game')[0]
        return {child.tagName: ''.join(n.data for n in child.childNodes
                                      if n.nodeType == n.TEXT_NODE)
                for child in game.childNodes if child.nodeType == child.ELEMENT_NODE}

    def assert_no_temporary_files(self):
        self.assertEqual([], list(self.root.glob('.novacore-*')))

    def test_new_gamelist_adds_relative_paths_and_name_without_touching_rom(self):
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual({'path': './nested/My Game.sfc', 'name': 'My Game',
                          'thumbnail': './images/novacore/box.png',
                          'image': './images/novacore/box.png'}, self.fields())
        self.assertEqual(b'never alter the ROM', self.rom.read_bytes())
        self.assertFalse((self.root / 'gamelist.xml.novacore.bak').exists())
        self.assert_no_temporary_files()

    def test_hidden_rom_filename_is_valid(self):
        (self.root / '.Hidden.sfc').write_bytes(b'rom')
        self.updates = {'.Hidden.sfc': self.art}
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual('./.Hidden.sfc', self.fields()['path'])

    def test_directory_game_and_regular_game_can_be_merged_together(self):
        folder = self.root / 'Folder Game.v1'
        folder.mkdir()
        content = folder / 'EBOOT.BIN'
        content.write_bytes(b'untouched game content')
        self.updates['Folder Game.v1'] = self.art
        self.assertEqual(set(self.updates), self.merge())
        document = minidom.parse(str(self.gamelist))
        self.assertEqual(2, len(document.getElementsByTagName('game')))
        self.assertEqual('Folder Game.v1', _element_text(
            document.getElementsByTagName('game')[1].getElementsByTagName('name')[0]))
        self.assertEqual(b'untouched game content', content.read_bytes())

    def test_case_insensitive_xml_path_is_updated_without_duplicate_game(self):
        self.gamelist.write_text('<gameList><game><path>./NESTED/my game.SFC</path>'
                                 '<name>Personal Title</name></game></gameList>')
        self.assertEqual(set(self.updates), self.merge())
        document = minidom.parse(str(self.gamelist))
        self.assertEqual(1, len(document.getElementsByTagName('game')))
        self.assertEqual('Personal Title', self.fields()['name'])
        self.assertEqual('./NESTED/my game.SFC', self.fields()['path'])
        self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_plain_rom_update_does_not_touch_literal_spaced_rom_row(self):
        plain = self.root / 'Game.nes'
        spaced = self.root / ' Game.nes'
        plain.write_bytes(b'plain ROM')
        spaced.write_bytes(b'spaced ROM')
        self.gamelist.write_text('<gameList><game><path> Game.nes</path>'
                                 '<name>Spaced title</name></game><game><path>Game.nes</path>'
                                 '<name>Plain title</name></game></gameList>')
        self.updates = {'Game.nes': self.art}
        self.assertEqual({'Game.nes'}, self.merge())
        games = minidom.parse(str(self.gamelist)).getElementsByTagName('game')
        self.assertEqual(2, len(games))
        self.assertEqual(' Game.nes', _element_text(games[0].getElementsByTagName('path')[0]))
        self.assertEqual([], games[0].getElementsByTagName('thumbnail'))
        self.assertEqual([], games[0].getElementsByTagName('image'))
        self.assertEqual('Spaced title', _element_text(games[0].getElementsByTagName('name')[0]))
        self.assertEqual('./images/novacore/box.png',
                         _element_text(games[1].getElementsByTagName('thumbnail')[0]))
        self.assertEqual(b'spaced ROM', spaced.read_bytes())
        self.assertEqual(b'plain ROM', plain.read_bytes())

    def test_missing_spaced_rom_row_is_not_matched_to_plain_rom(self):
        (self.root / 'Game.nes').write_bytes(b'ROM')
        self.gamelist.write_text('<gameList><game><path> Game.nes</path>'
                                 '<name>Missing spaced game</name></game></gameList>')
        self.updates = {'Game.nes': self.art}
        self.assertEqual({'Game.nes'}, self.merge())
        games = minidom.parse(str(self.gamelist)).getElementsByTagName('game')
        self.assertEqual(2, len(games))
        self.assertEqual([], games[0].getElementsByTagName('thumbnail'))
        self.assertEqual('./Game.nes', _element_text(games[1].getElementsByTagName('path')[0]))

    def test_xml_path_formatting_is_normalized_without_duplicate_game(self):
        self.gamelist.write_text('<gameList><game><path>\n \t ./nested/My Game.sfc \n</path>'
                                 '<name>Formatted path</name></game></gameList>')
        self.assertEqual(set(self.updates), self.merge())
        document = minidom.parse(str(self.gamelist))
        self.assertEqual(1, len(document.getElementsByTagName('game')))
        self.assertEqual('Formatted path', self.fields()['name'])
        self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_merge_preserves_comments_processing_instructions_folder_and_custom_fields(self):
        original = (b'<?xml version="1.0"?><?custom keep?>\n'
                    b'<!-- outside --><gameList custom="yes">\n'
                    b'<!-- inside --><folder favourite="true"><path>./nested</path>'
                    b'<name>Folder</name></folder><game id="42"><path>nested/My Game.sfc</path>'
                    b'<name>Custom title</name><desc>A &amp; B</desc>'
                    b'<custom a="b">keep</custom><rating>0.7</rating></game></gameList>')
        self.gamelist.write_bytes(original)
        self.assertEqual(set(self.updates), self.merge())
        actual = self.gamelist.read_bytes()
        for preserved in (b'<?custom keep?>', b'<!-- outside -->', b'<!-- inside -->',
                          b'custom="yes"', b'<folder favourite="true">', b'<game id="42">',
                          b'<custom a="b">keep</custom>', b'<rating>0.7</rating>'):
            self.assertIn(preserved, actual)
        self.assertEqual('Custom title', self.fields()['name'])
        self.assertEqual('A & B', self.fields()['desc'])
        self.assertEqual(original, (self.root / 'gamelist.xml.novacore.bak').read_bytes())

    def test_original_backup_is_never_overwritten(self):
        original = b'<gameList/>'
        self.gamelist.write_bytes(original)
        self.merge()
        other_rom = self.root / 'Other.sfc'
        other_rom.write_bytes(b'rom')
        self.updates = {'Other.sfc': self.art}
        self.merge()
        self.assertEqual(original, (self.root / 'gamelist.xml.novacore.bak').read_bytes())

    def test_valid_existing_image_is_preserved_while_missing_box_is_added(self):
        custom = self.root / 'custom.png'
        custom.write_bytes(b'existing')
        original = (b'<gameList><game><path>./nested/My Game.sfc</path>'
                    b'<image>./custom.png</image></game></gameList>')
        self.gamelist.write_bytes(original)
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual('./custom.png', self.fields()['image'])
        self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])
        self.assertEqual(original, (self.root / 'gamelist.xml.novacore.bak').read_bytes())

    def test_valid_existing_thumbnail_is_accepted_without_any_write(self):
        original = (b'<gameList><game><path>./nested/My Game.sfc</path>'
                    b'<thumbnail>./images/novacore/box.png</thumbnail></game></gameList>')
        self.gamelist.write_bytes(original)
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual(original, self.gamelist.read_bytes())

    def test_valid_existing_thumbnail_and_screenshot_are_never_overwritten(self):
        screenshot = self.root / 'custom.png'
        screenshot.write_bytes(b'custom screenshot')
        original = (b'<gameList><game><path>./nested/My Game.sfc</path>'
                    b'<thumbnail>./images/novacore/box.png</thumbnail>'
                    b'<image>./custom.png</image></game></gameList>')
        self.gamelist.write_bytes(original)
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual(original, self.gamelist.read_bytes())

    def test_literal_leading_and_trailing_media_path_spaces_are_preserved(self):
        custom = self.root / ' custom-art' / 'picture.png '
        custom.parent.mkdir()
        custom.write_bytes(b'custom image')
        reference = ' custom-art/picture.png '
        for field in ('thumbnail', 'image'):
            with self.subTest(field=field):
                original = ('<gameList><game><path>nested/My Game.sfc</path>'
                            f'<{field}>{reference}</{field}></game></gameList>').encode()
                self.gamelist.write_bytes(original)
                self.assertEqual(set(self.updates), self.merge())
                self.assertEqual(reference, self.fields()[field])
                if field == 'thumbnail':
                    self.assertEqual(original, self.gamelist.read_bytes())
                else:
                    self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_external_absolute_existing_artwork_is_preserved(self):
        with tempfile.TemporaryDirectory() as other:
            custom = Path(other) / 'custom.png'
            custom.write_bytes(b'custom')
            original = ('<gameList><game><path>nested/My Game.sfc</path>'
                        f'<image>{custom}</image></game></gameList>').encode()
            self.gamelist.write_bytes(original)
            self.assertEqual(set(self.updates), self.merge())
            self.assertEqual(str(custom), self.fields()['image'])
            self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_foreign_or_unresolved_artwork_reference_is_conservatively_preserved(self):
        for reference in ('Z:/custom/artwork.png', 'https://example.test/custom.png',
                          '%UNKNOWN_NOVACORE_DIR%/custom.png', 'custom:artwork.png',
                          '~unknown_novacore_user/artwork.png'):
            with self.subTest(reference=reference):
                original = ('<gameList><game><path>nested/My Game.sfc</path>'
                            f'<image>{reference}</image></game></gameList>').encode()
                self.gamelist.write_bytes(original)
                self.assertEqual(set(self.updates), self.merge())
                self.assertEqual(reference, self.fields()['image'])
                self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_explicit_tilde_username_is_preserved_when_windows_invents_a_home_path(self):
        reference = '~unknown_novacore_user/artwork.png'
        self.gamelist.write_text('<gameList><game><path>nested/My Game.sfc</path>'
                                 f'<image>{reference}</image></game></gameList>')
        invented_home_path = self.root / 'absent-user-profile' / 'artwork.png'
        with mock.patch.object(metadata.os.path, 'expanduser', return_value=str(invented_home_path)):
            self.assertEqual(set(self.updates), self.merge())
        self.assertEqual(reference, self.fields()['image'])
        self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_broken_references_are_filled_and_existing_name_remains(self):
        self.gamelist.write_text('<gameList><game><path>./nested/My Game.sfc</path>'
                                 '<name>Special</name><thumbnail>./missing.png</thumbnail>'
                                 '<image>./missing-image.png</image></game></gameList>')
        self.merge()
        fields = self.fields()
        self.assertEqual('./images/novacore/box.png', fields['thumbnail'])
        self.assertEqual('./images/novacore/box.png', fields['image'])
        self.assertEqual('Special', fields['name'])

    def test_malformed_xml_remains_untouched(self):
        for original in (b'<gameList>', b'<not-a-game-list/>', b'not XML'):
            with self.subTest(original=original):
                self.gamelist.write_bytes(original)
                with self.assertRaises(metadata.MetadataError):
                    self.merge()
                self.assertEqual(original, self.gamelist.read_bytes())
                self.assertFalse((self.root / 'gamelist.xml.novacore.bak').exists())
                self.assert_no_temporary_files()

    def test_entities_and_doctype_are_rejected_including_utf16(self):
        xml = '<!DOCTYPE gameList [<!ENTITY value "expanded">]><gameList/>'
        for original in (xml.encode(), xml.encode('utf-16'), xml.encode('utf-16-be'),
                         b'<!DOCTYPE gameList SYSTEM "https://example.test/a.dtd"><gameList/>'):
            with self.subTest(original=original):
                self.gamelist.write_bytes(original)
                with self.assertRaises(metadata.MetadataError):
                    self.merge()
                self.assertEqual(original, self.gamelist.read_bytes())
                self.assert_no_temporary_files()

    def test_utf16_without_entities_is_supported(self):
        self.gamelist.write_bytes('<?xml version="1.0" encoding="utf-16"?><gameList/>'.encode('utf-16'))
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual('./nested/My Game.sfc', self.fields()['path'])

    def test_windows_fstat_change_time_can_differ_from_lstat_creation_time(self):
        self.gamelist.write_bytes(b'<gameList/>')
        original_fstat = metadata.os.fstat
        def windows_fstat(descriptor):
            original = original_fstat(descriptor)
            fields = {name: getattr(original, name) for name in
                      ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns', 'st_ctime_ns')}
            fields['st_ctime_ns'] += 100_000_000
            return SimpleNamespace(**fields)
        with mock.patch.object(metadata.os, 'fstat', side_effect=windows_fstat):
            self.assertEqual(set(self.updates), self.merge())
            self.assertEqual(set(self.updates), self.merge())
        self.assertEqual('./images/novacore/box.png', self.fields()['thumbnail'])

    def test_unavailable_windows_drive_reference_is_preserved(self):
        class UnavailableDrivePath(PureWindowsPath):
            def stat(self):
                raise FileNotFoundError('Drive is not mounted')
        element = minidom.parseString('<image>Z:/custom/artwork.png</image>').documentElement
        with mock.patch.object(metadata, 'Path', UnavailableDrivePath):
            self.assertTrue(metadata._reference_is_valid(element, self.root))

    def test_missing_file_on_available_windows_drive_is_broken(self):
        class AvailableDrivePath(PureWindowsPath):
            def stat(self):
                if str(self) == self.anchor:
                    return SimpleNamespace(st_mode=0o040755)
                raise FileNotFoundError('Image is missing')
        element = minidom.parseString('<image>Z:/custom/artwork.png</image>').documentElement
        with mock.patch.object(metadata, 'Path', AvailableDrivePath):
            self.assertFalse(metadata._reference_is_valid(element, self.root))

    def test_running_at_start_performs_zero_writes(self):
        with mock.patch.object(metadata.tempfile, 'mkstemp', side_effect=AssertionError('write')):
            self.assertEqual(set(), self.merge(lambda: True))
        self.assertFalse(self.gamelist.exists())

    def test_running_before_replace_leaves_metadata_pending(self):
        original = b'<gameList/>'
        self.gamelist.write_bytes(original)
        with mock.patch.object(metadata.os, 'replace', side_effect=AssertionError('replace')):
            self.assertEqual(set(), self.merge(mock.Mock(side_effect=[False, True])))
        self.assertEqual(original, self.gamelist.read_bytes())
        self.assertFalse((self.root / 'gamelist.xml.novacore.bak').exists())
        self.assert_no_temporary_files()

    def test_concurrent_writer_change_before_commit_is_not_overwritten(self):
        self.gamelist.write_bytes(b'<gameList/>')
        external = b'<gameList><custom>external</custom></gameList>'
        original_fsync = metadata.os.fsync
        changed = False
        def change_during_temporary_write(fd):
            nonlocal changed
            original_fsync(fd)
            if not changed:
                changed = True
                self.gamelist.write_bytes(external)
        with mock.patch.object(metadata.os, 'fsync', side_effect=change_during_temporary_write):
            with self.assertRaises(metadata.ConflictError):
                self.merge()
        self.assertEqual(external, self.gamelist.read_bytes())
        self.assert_no_temporary_files()

    def test_concurrent_writer_creation_is_not_overwritten(self):
        external = b'<gameList><custom>external</custom></gameList>'
        original_fsync = metadata.os.fsync
        changed = False
        def create_during_temporary_write(fd):
            nonlocal changed
            original_fsync(fd)
            if not changed:
                changed = True
                self.gamelist.write_bytes(external)
        with mock.patch.object(metadata.os, 'fsync', side_effect=create_during_temporary_write):
            with self.assertRaises(metadata.ConflictError):
                self.merge()
        self.assertEqual(external, self.gamelist.read_bytes())

    def test_digest_detects_same_length_edit_with_restored_mtime_and_creation_time(self):
        original = b'<gameList a="1"/>'
        external = b'<gameList a="2"/>'
        self.gamelist.write_bytes(original)
        original_stat = self.gamelist.stat()
        original_fsync = metadata.os.fsync
        original_safe_path = metadata._safe_path
        changed = False
        def rewrite_with_original_mtime(descriptor):
            nonlocal changed
            original_fsync(descriptor)
            if not changed:
                changed = True
                self.gamelist.write_bytes(external)
                os.utime(self.gamelist, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        def windows_creation_time(root, path, **kwargs):
            info = original_safe_path(root, path, **kwargs)
            if path != self.gamelist or info is None:
                return info
            fields = {name: getattr(info, name) for name in
                      ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_mtime_ns')}
            fields['st_ctime_ns'] = original_stat.st_ctime_ns
            return SimpleNamespace(**fields)
        with mock.patch.object(metadata.os, 'fsync', side_effect=rewrite_with_original_mtime), \
                mock.patch.object(metadata, '_safe_path', side_effect=windows_creation_time):
            with self.assertRaises(metadata.ConflictError):
                self.merge()
        self.assertEqual(external, self.gamelist.read_bytes())
        self.assert_no_temporary_files()

    def test_concurrent_writer_during_backup_is_not_overwritten(self):
        self.gamelist.write_bytes(b'<gameList/>')
        external = b'<gameList><custom>external</custom></gameList>'
        backup = metadata._backup
        def change_after_backup(*args):
            backup(*args)
            self.gamelist.write_bytes(external)
        with mock.patch.object(metadata, '_backup', side_effect=change_after_backup):
            with self.assertRaises(metadata.ConflictError):
                self.merge()
        self.assertEqual(external, self.gamelist.read_bytes())

    def test_running_during_backup_leaves_canonical_xml_untouched(self):
        original = b'<gameList/>'
        self.gamelist.write_bytes(original)
        self.assertEqual(set(), self.merge(mock.Mock(side_effect=[False, False, True])))
        self.assertEqual(original, self.gamelist.read_bytes())
        self.assert_no_temporary_files()

    def test_interrupted_replace_preserves_original_and_atomic_backup(self):
        original = b'<gameList/>'
        self.gamelist.write_bytes(original)
        with mock.patch.object(metadata.os, 'replace', side_effect=OSError('simulated interruption')):
            with self.assertRaises(metadata.MetadataError):
                self.merge()
        self.assertEqual(original, self.gamelist.read_bytes())
        self.assertEqual(original, (self.root / 'gamelist.xml.novacore.bak').read_bytes())
        self.assert_no_temporary_files()

    def test_keyboard_interrupt_cleans_temporary_files(self):
        self.gamelist.write_bytes(b'<gameList/>')
        with mock.patch.object(metadata.os, 'replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.merge()
        self.assertEqual(b'<gameList/>', self.gamelist.read_bytes())
        self.assert_no_temporary_files()

    def test_unsafe_update_paths_are_rejected_before_writing(self):
        for game in ('../escape.sfc', '/absolute.sfc', 'C:/absolute.sfc',
                     'nested/../escape.sfc', './nested/My Game.sfc',
                     'nested\\My Game.sfc', 'nested//My Game.sfc', '\ufffe.sfc', ''):
            with self.subTest(game=game):
                self.updates = {game: self.art}
                with self.assertRaises(metadata.MetadataError):
                    self.merge()
                self.assertFalse(self.gamelist.exists())

    def test_outside_or_missing_artwork_is_rejected(self):
        for artwork in (self.root.parent / 'outside.png', self.root / 'missing.png', Path('relative.png')):
            with self.subTest(artwork=artwork):
                self.updates = {'nested/My Game.sfc': artwork}
                with self.assertRaises(metadata.MetadataError):
                    self.merge()
                self.assertFalse(self.gamelist.exists())

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlink_gamelist_is_rejected_without_touching_target(self):
        target = self.root / 'custom.xml'
        target.write_bytes(b'<gameList/>')
        try:
            self.gamelist.symlink_to(target)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(metadata.MetadataError):
            self.merge()
        self.assertEqual(b'<gameList/>', target.read_bytes())

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'named pipes unavailable')
    def test_nonregular_gamelist_is_rejected_before_open(self):
        os.mkfifo(self.gamelist)
        with mock.patch.object(metadata.os, 'open', side_effect=AssertionError('unsafe open')):
            with self.assertRaises(metadata.MetadataError):
                self.merge()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlink_artwork_is_rejected(self):
        link = self.root / 'link.png'
        try:
            link.symlink_to(self.art)
        except OSError as exc:
            self.skipTest(str(exc))
        self.updates = {'nested/My Game.sfc': link}
        with self.assertRaises(metadata.MetadataError):
            self.merge()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlink_artwork_directory_is_rejected(self):
        link = self.root / 'link'
        try:
            link.symlink_to(self.art.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        self.updates = {'nested/My Game.sfc': link / self.art.name}
        with self.assertRaises(metadata.MetadataError):
            self.merge()

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks unavailable')
    def test_symlink_backup_is_rejected_without_touching_target(self):
        self.gamelist.write_bytes(b'<gameList/>')
        target = self.root / 'custom.xml'
        target.write_bytes(b'precious')
        try:
            (self.root / 'gamelist.xml.novacore.bak').symlink_to(target)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(metadata.MetadataError):
            self.merge()
        self.assertEqual(b'precious', target.read_bytes())
        self.assertEqual(b'<gameList/>', self.gamelist.read_bytes())

    def test_size_limit_rejects_large_xml_without_touching_it(self):
        original = b'<gameList>' + b' ' * 200 + b'</gameList>'
        self.gamelist.write_bytes(original)
        with mock.patch.object(metadata, 'MAX_XML_BYTES', 100):
            with self.assertRaises(metadata.MetadataError):
                self.merge()
        self.assertEqual(original, self.gamelist.read_bytes())

    def test_fresh_read_and_batch_index_avoid_duplicate_games(self):
        count = 1200
        self.updates = {}
        entries = []
        for number in range(count):
            key = f'Game {number}.sfc'
            (self.root / key).write_bytes(b'rom')
            self.updates[key] = self.art
            entries.append(f'<game><path>./{key}</path><custom>{number}</custom></game>')
        self.gamelist.write_text('<gameList>' + ''.join(entries) + '</gameList>')
        self.assertEqual(set(self.updates), self.merge())
        document = minidom.parse(str(self.gamelist))
        self.assertEqual(count, len(document.getElementsByTagName('game')))
        self.assertEqual(count, len(document.getElementsByTagName('custom')))
        updated = self.gamelist.read_bytes()
        self.assertEqual(set(self.updates), self.merge())
        self.assertEqual(updated, self.gamelist.read_bytes())


if __name__ == '__main__':
    unittest.main()
