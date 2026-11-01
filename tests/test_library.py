"""Read-only library fixtures: portable keys, metadata, launchers and boundaries."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from novacore_scraper import library
from novacore_scraper.models import Game, System


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'RetroBat').resolve()
        (self.root / 'roms').mkdir(parents=True)
        (self.root / 'RetroBat.exe').write_bytes(b'fixture')

    def write(self, relative, data=''):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding='utf-8')
        return path

    def system(self, name='psx', extensions=('.cue', '.bin', '.chd', '.m3u', '.gdi')):
        path = self.root / 'roms' / name
        path.mkdir(parents=True, exist_ok=True)
        return System(name, name.upper(), path, frozenset(extensions))

    def config(self, body, custom=False):
        return self.write('emulationstation/.emulationstation/es_systems' + ('_custom' if custom else '') + '.cfg', '<systemList>' + body + '</systemList>')

    def test_validate_requires_executable_and_local_roms(self):
        self.assertEqual(library.validate_retrobat(self.root), self.root)
        (self.root / 'RetroBat.exe').unlink()
        with self.assertRaises(ValueError):
            library.validate_retrobat(self.root)

    def test_detect_installations_validates_and_deduplicates_candidates(self):
        bad = self.root.parent / 'not-retrobat'
        bad.mkdir()
        self.assertEqual(library.detect_installations([bad, self.root, self.root]), [self.root])

    def test_config_custom_override_and_disabled_unsafe_paths(self):
        self.system('snes', ('.sfc',))
        self.system('psx')
        self.system('nes', ('.nes',))
        self.config('<system><name>snes</name><fullname>Super Nintendo</fullname><path>%ROMPATH%/snes</path><extension>.SFC .smc .fake</extension><screenscraper>4</screenscraper></system><system><name>psx</name><path>roms/psx</path><extension>.cue</extension></system>')
        self.config('<system><name>snes</name><fullname>Custom SNES</fullname><path>roms/snes</path><extension>.sfc</extension><screenscraper>4</screenscraper></system><system><name>psx</name><path>../outside</path><extension>.cue</extension></system><system><name>nes</name><path>roms/nes</path><extension>.nes</extension></system>', custom=True)
        systems = library.discover_systems(self.root)
        self.assertEqual([s.name for s in systems], ['nes', 'snes'])
        self.assertEqual(systems[1].fullname, 'Custom SNES')
        self.assertEqual(systems[1].extensions, frozenset({'.sfc'}))
        self.assertEqual(systems[1].screenscraper_id, '4')

    def test_standard_retrobat_tilde_parent_paths_discover_and_scan(self):
        self.system('nes', ('.nes',))
        self.system('ps2', ('.iso',))
        self.system('snes', ('.sfc',))
        self.write('roms/nes/Mario.nes', 'rom')
        self.config(r'<system><name>nes</name><fullname>Nintendo Entertainment System - Famicom</fullname><path>~\..\roms\nes</path><extension>.fds .nes .wad .zip .7z</extension></system><system><name>ps2</name><path>~/../roms/ps2</path><extension>.iso</extension></system><system><name>snes</name><path>emulationstation/../roms/snes</path><extension>.sfc</extension></system>')
        systems = library.discover_systems(self.root)
        self.assertEqual([system.name for system in systems], ['nes', 'ps2', 'snes'])
        self.assertEqual([system.path for system in systems], [self.root/'roms'/'nes', self.root/'roms'/'ps2', self.root/'roms'/'snes'])
        self.assertEqual([game.relative_path for game in library.scan_system(systems[0])], ['Mario.nes'])

    def test_normalized_config_paths_still_reject_final_outside_roms(self):
        self.system('nes', ('.nes',))
        (self.root/'outside').mkdir()
        for raw in ('~/../outside', 'emulationstation/../../outside', 'roms/nes/../../outside'):
            with self.subTest(path=raw):
                self.config('<system><name>nes</name><path>'+raw+'</path><extension>.nes</extension></system>')
                self.assertEqual(library.discover_systems(self.root), [])

    def test_normalization_does_not_erase_linked_traversal_ancestor(self):
        self.system('nes', ('.nes',))
        outside = self.root.parent/'outside'
        outside.mkdir()
        try:
            (self.root/'roms'/'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.config('<system><name>nes</name><path>roms/linked/../nes</path><extension>.nes</extension></system>')
        self.assertEqual(library.discover_systems(self.root), [])

    def test_additional_system_config_filenames_merge_partial_overrides(self):
        self.system('nes', ('.nes',))
        self.system('snes', ('.sfc',))
        self.config('<system><name>nes</name><fullname>Nintendo</fullname><path>roms/nes</path><extension>.nes .zip</extension></system>')
        self.write('emulationstation/.emulationstation/es_systems_nes.cfg', '<systemList><system><name>nes</name><fullname>Custom Nintendo</fullname><emulators><emulator name="mesen"/></emulators></system></systemList>')
        self.write('emulationstation/.emulationstation/es_systems_snes.cfg', '<systemList><system><name>snes</name><path>roms/snes</path><extension>.sfc</extension></system></systemList>')
        systems = library.discover_systems(self.root)
        self.assertEqual([system.name for system in systems], ['nes', 'snes'])
        self.assertEqual(systems[0].fullname, 'Custom Nintendo')
        self.assertEqual(systems[0].extensions, frozenset({'.nes', '.zip'}))
        self.assertEqual(systems[0].path, self.root/'roms'/'nes')

    def test_autodetect_first_only_stops_after_first_valid_candidate(self):
        later = self.root.parent/'other-retrobat'
        attempted = []
        original = library.validate_retrobat
        def validate(path):
            attempted.append(path)
            if path == later:
                self.fail('later drives must not be probed after finding the preferred installation')
            return original(path)
        with patch.object(library, 'validate_retrobat', validate):
            self.assertEqual(library.detect_installations([self.root, later], first_only=True), [self.root])
        self.assertEqual(attempted, [self.root])

    def test_autodetect_cancellation_stops_before_candidate_validation(self):
        with patch.object(library, 'validate_retrobat', side_effect=AssertionError('cancelled detection must not probe a drive')):
            self.assertEqual(library.detect_installations([self.root], cancelled=lambda: True), [])

    def test_scan_skips_unrelated_media_subtrees_but_honors_explicit_custom_thumbnail(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        self.write('roms/snes/videos/nested/ignored.mp4', 'video')
        self.write('roms/snes/manuals/nested/ignored.pdf', 'manual')
        self.write('roms/snes/media/screenshots/nested/A-image.png', 'screenshot')
        box = self.write('roms/snes/manuals/custom/A.png', 'explicit custom box')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><thumbnail>manuals/custom/A.png</thumbnail></game></gameList>')
        original = os.scandir
        forbidden = [system.path/'videos', system.path/'manuals', system.path/'media'/'screenshots']
        def guarded(path):
            if any(Path(path).is_relative_to(folder) for folder in forbidden):
                self.fail('unrelated media subtree should not be enumerated: '+str(path))
            return original(path)
        with patch.object(os, 'scandir', guarded):
            games = library.scan_system(system)
        self.assertEqual([game.relative_path for game in games], ['A.sfc'])
        self.assertEqual(games[0].existing_artwork, box)

    def test_scan_uses_directory_file_information_and_checks_indexed_covers_once(self):
        system = self.system('snes', ('.sfc',))
        entries = []
        for index in range(100):
            self.write(f'roms/snes/Game {index}.sfc', 'rom')
            self.write(f'roms/snes/images/Game {index}-thumb.png', 'box')
            self.write(f'roms/snes/notes {index}.txt', 'irrelevant')
            entries.append(f'<game><path>Game {index}.sfc</path><thumbnail>images/Game {index}-thumb.png</thumbnail></game>')
        self.write('roms/snes/gamelist.xml', '<gameList>'+''.join(entries)+'</gameList>')
        original = Path.stat
        calls = []
        def counted(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', counted):
            games = library.scan_system(system)
        self.assertEqual(len(games), 100)
        self.assertTrue(all(game.existing_artwork is not None for game in games))
        self.assertLess(len(calls), 50, 'freshly indexed ROMs and thumbnails should not each be restatted by Path')

    def test_live_artwork_recheck_rejects_image_directory_replaced_by_link(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        box = self.write('roms/snes/images/A-thumb.png', 'box')
        game = library.scan_system(system)[0]
        self.assertEqual(game.existing_artwork, box)
        outside = self.root.parent/'outside-covers'
        outside.mkdir()
        (outside/'A-thumb.png').write_text('external')
        (system.path/'images').rename(system.path/'old-images')
        try:
            (system.path/'images').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertIsNone(library.existing_artwork(game))
        self.assertEqual((outside/'A-thumb.png').read_text(), 'external')

    def test_scan_cancels_during_directory_enumeration_without_partial_results(self):
        system = self.system('snes', ('.sfc',))
        for index in range(100):
            self.write(f'roms/snes/Nested/Game {index}.sfc', 'rom')
        checks = 0
        def cancelled():
            nonlocal checks
            checks += 1
            return checks >= 20
        self.assertEqual(library.scan_system(system, cancelled=cancelled), [])
        self.assertFalse((system.path/'images').exists())

    def test_queued_directory_replaced_with_link_is_not_traversed(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/Nested/Local.sfc', 'rom')
        outside = self.root.parent/'outside-roms'
        outside.mkdir()
        (outside/'External.sfc').write_text('rom')
        probe = self.root.parent/'link-probe'
        try:
            probe.symlink_to(outside, target_is_directory=True)
            probe.unlink()
        except OSError:
            self.skipTest('symlinks unavailable')
        original = os.scandir
        class SwapAfterListing:
            def __init__(self, path):
                self.path = Path(path)
                self.iterator = original(path)
            def __enter__(self):
                return self.iterator
            def __exit__(self, *args):
                self.iterator.close()
                if self.path == system.path:
                    (system.path/'Nested').rename(system.path/'Old Nested')
                    (system.path/'Nested').symlink_to(outside, target_is_directory=True)
        with patch.object(os, 'scandir', SwapAfterListing):
            self.assertEqual(library.scan_system(system), [])

    def test_fallback_only_includes_existing_common_system_directories(self):
        self.system('snes', ('.sfc',))
        self.system('psx')
        self.system('unknown', ('.rom',))
        self.assertEqual([s.name for s in library.discover_systems(self.root)], ['psx', 'snes'])

    def test_invalid_configuration_is_not_used_as_fallback(self):
        self.system('snes', ('.sfc',))
        self.write('emulationstation/.emulationstation/es_systems.cfg', '<!DOCTYPE systemList [<!ENTITY x "boom">]><systemList/>')
        self.assertEqual(library.discover_systems(self.root), [])

    def test_scans_nested_files_using_gamelist_title_and_artwork(self):
        system = self.system('snes', ('.sfc', '.smc'))
        rom = self.write('roms/snes/Europe/Chrono.sfc', 'rom bytes')
        art = self.write('roms/snes/images/Chrono.png', 'existing')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>./Europe/Chrono.sfc</path><name>Chrono Trigger</name><thumbnail>./images/Chrono.png</thumbnail></game></gameList>')
        self.write('roms/snes/images/not-a-game.sfc', 'metadata directory')
        games = library.scan_system(system)
        self.assertEqual([(g.relative_path, g.title, g.key) for g in games], [('Europe/Chrono.sfc', 'Chrono Trigger', 'snes/Europe/Chrono.sfc')])
        self.assertEqual(games[0].existing_artwork, art)
        self.assertEqual(rom.read_text(), 'rom bytes')

    def test_manifest_chains_suppress_discs_and_tracks(self):
        system = self.system()
        self.write('roms/psx/Final.m3u', '#EXTM3U\nDiscs/Disc 1.cue\nDiscs/Disc 2.chd\n')
        self.write('roms/psx/Discs/Disc 1.cue', 'FILE "Track 1.bin" BINARY\n  TRACK 01 MODE2/2352\n')
        self.write('roms/psx/Discs/Track 1.bin', 'track')
        self.write('roms/psx/Discs/Disc 2.chd', 'disc')
        self.write('roms/psx/Other.gdi', '1\n1 0 4 2352 "Other Track.bin" 0\n')
        self.write('roms/psx/Other Track.bin', 'track')
        self.assertEqual([g.relative_path for g in library.scan_system(system)], ['Final.m3u', 'Other.gdi'])

    def test_directory_gamelist_entries_prevent_contents_becoming_games(self):
        system = self.system('ps3', ('.iso', '.ps3'))
        self.write('roms/ps3/Folder Game/PS3_GAME/USRDIR/data.iso', 'data')
        self.write('roms/ps3/gamelist.xml', '<gameList><game><path>./Folder Game</path><name>Playable Folder</name></game></gameList>')
        self.assertEqual([(g.relative_path, g.title) for g in library.scan_system(system)], [('Folder Game', 'Playable Folder')])

    def test_artwork_honors_thumbnail_and_nested_common_locations(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'a')
        thumb = self.write('roms/snes/thumbs/A.png', 'art')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>missing.png</image><thumbnail>thumbs/A.png</thumbnail></game></gameList>')
        self.write('roms/snes/Nested/B.sfc', 'b')
        built_in = self.write('roms/snes/images/Nested/B-thumb.jpg', 'art')
        self.write('roms/snes/C.sfc', 'c')
        downloaded = self.write('roms/snes/downloaded_images/C-thumb.png', 'art')
        self.write('roms/snes/D.sfc', 'd')
        box = self.write('roms/snes/media/box2d/D.png', 'art')
        self.assertEqual({g.relative_path: g.existing_artwork for g in library.scan_system(system)}, {'A.sfc': thumb, 'Nested/B.sfc': built_in, 'C.sfc': downloaded, 'D.sfc': box})

    def test_destinations_are_portable_unique_and_do_not_create_directories(self):
        system = self.system('snes', ('.sfc',))
        one = Game(system, system.path/'A.sfc', 'A.sfc', 'A')
        two = Game(system, system.path/'Nested/A.sfc', 'Nested/A.sfc', 'A')
        path = library.artwork_destination(one)
        self.assertEqual(path.parent, system.path/'images'/'novacore')
        self.assertRegex(path.name, r'^A-[a-f0-9]{16}\.png$')
        self.assertNotEqual(path, library.artwork_destination(two))
        moved = System('snes', 'SNES', self.root.parent/'Moved'/'roms'/'snes', system.extensions)
        self.assertEqual(library.artwork_destination(Game(moved, moved.path/'A.sfc', 'A.sfc', 'A')).name, path.name)
        self.assertFalse(path.parent.exists())

    def test_scanner_does_not_follow_links_or_accept_external_metadata(self):
        system = self.system('snes', ('.sfc',))
        outside = self.root.parent/'outside'
        outside.mkdir()
        (outside/'Hidden.sfc').write_text('rom')
        (outside/'art.png').write_text('art')
        try:
            (system.path/'linked').symlink_to(outside, target_is_directory=True)
            (system.path/'Link.sfc').symlink_to(outside/'Hidden.sfc')
        except OSError:
            self.skipTest('symlinks unavailable')
        self.write('roms/snes/Local.sfc', 'rom')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>Local.sfc</path><thumbnail>../../outside/art.png</thumbnail></game><game><path>../../outside/Hidden.sfc</path></game></gameList>')
        games = library.scan_system(system)
        self.assertEqual([g.relative_path for g in games], ['Local.sfc'])
        self.assertIsNone(games[0].existing_artwork)
        (self.root/'roms').rename(self.root/'local-roms')
        (self.root/'roms').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            library.validate_retrobat(self.root)

    def test_public_artwork_lookup_reads_metadata_and_rechecks_missing_images(self):
        system = self.system('snes', ('.sfc',))
        rom = self.write('roms/snes/A.sfc', 'rom')
        art = self.write('roms/snes/Custom/Cover.png', 'art')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>Custom/Cover.png</image></game></gameList>')
        game = Game(system, rom, 'A.sfc', 'A')
        self.assertEqual(library.existing_artwork(game), art)
        art.unlink()
        self.assertIsNone(library.existing_artwork(game))

    def test_artwork_destination_rejects_unsafe_game_paths(self):
        system = self.system('snes', ('.sfc',))
        for relative in ('../A.sfc', '/A.sfc', 'C:/A.sfc', 'A.sfc:stream'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                library.artwork_destination(Game(system, system.path/'A.sfc', relative, 'A'))

    def test_configured_directory_extension_launches_without_gamelist(self):
        system = self.system('ps3', ('.ps3', '.bin'))
        self.write('roms/ps3/Folder Game.ps3/PS3_GAME/USRDIR/EBOOT.BIN', 'program')
        games = library.scan_system(system)
        self.assertEqual([(game.relative_path, game.title) for game in games], [('Folder Game.ps3', 'Folder Game')])

    def test_directory_metadata_uses_windows_case_matching(self):
        system = self.system('ps3', ('.iso',))
        self.write('roms/ps3/Big Game/data.iso', 'data')
        self.write('roms/ps3/gamelist.xml', '<gameList><game><path>big game</path><name>Folder Title</name></game></gameList>')
        self.assertEqual([(g.relative_path, g.title) for g in library.scan_system(system)], [('Big Game', 'Folder Title')])

    def test_invalid_gamelist_preserves_rom_scan(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        self.write('roms/snes/gamelist.xml', '<gameList><broken')
        self.assertEqual([g.relative_path for g in library.scan_system(system)], ['A.sfc'])

    def test_manifest_parent_references_stay_inside_system(self):
        system = self.system()
        self.write('roms/psx/Launch/Game.m3u', '../Discs/Disc.chd\n../../../../outside.chd\n')
        self.write('roms/psx/Discs/Disc.chd', 'disc')
        self.assertEqual([g.relative_path for g in library.scan_system(system)], ['Launch/Game.m3u'])

    def test_symlink_configuration_cannot_escape_installation(self):
        self.system('snes', ('.sfc',))
        outside = self.root.parent/'outside.cfg'
        outside.write_text('<systemList><system><name>snes</name><extension>.sfc</extension></system></systemList>')
        config = self.root/'emulationstation'/'.emulationstation'/'es_systems.cfg'
        config.parent.mkdir(parents=True)
        try:
            config.symlink_to(outside)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertEqual(library.discover_systems(self.root), [])

    def test_local_absolute_artwork_is_read_only_without_enumerating_external_roms(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        outside = self.root.parent/'custom-covers'
        outside.mkdir()
        art = outside/'A.png'
        art.write_bytes(b'custom art')
        external_rom = outside/'Hidden.sfc'
        external_rom.write_bytes(b'external rom')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>'+escape(str(art))+'</image></game><game><path>'+escape(str(external_rom))+'</path></game></gameList>')
        games = library.scan_system(system)
        self.assertEqual([game.relative_path for game in games], ['A.sfc'])
        self.assertEqual(games[0].existing_artwork, art)
        self.assertEqual(library.existing_artwork(games[0]), art)
        self.assertEqual(art.read_bytes(), b'custom art')
        self.assertEqual(external_rom.read_bytes(), b'external rom')

    def test_absolute_custom_artwork_is_normalized_before_external_classification(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        art = self.write('shared-covers/A.png', 'art')
        raw = str(system.path/'../../shared-covers/A.png')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><thumbnail>'+escape(raw)+'</thumbnail></game></gameList>')
        game = library.scan_system(system)[0]
        self.assertEqual(game.existing_artwork, art)
        self.assertFalse(game.existing_artwork.is_relative_to(system.path))

    def test_shared_relative_artwork_stays_inside_installation(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        art = self.write('roms/shared-covers/A.png', 'shared art')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><thumbnail>../shared-covers/A.png</thumbnail></game></gameList>')
        game = library.scan_system(system)[0]
        self.assertEqual(game.existing_artwork, art)
        self.assertEqual(library.existing_artwork(game), art)

    def test_network_and_foreign_drive_artwork_references_are_ignored(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        for raw in ('//server/share/A.png', r'\\server\share\A.png', 'Z:/unavailable/A.png', 'https://example.test/A.png'):
            with self.subTest(path=raw):
                self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><thumbnail>'+escape(raw)+'</thumbnail></game></gameList>')
                self.assertIsNone(library.scan_system(system)[0].existing_artwork)

    def test_missing_artwork_rechecks_use_constant_disk_work_after_scan(self):
        system = self.system('snes', ('.sfc',))
        for index in range(100):
            self.write(f'roms/snes/Game {index}.sfc', 'rom')
        games = library.scan_system(system)
        original = Path.lstat
        calls = []
        def counted(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', counted):
            results = [library.existing_artwork(game) for game in games]
        self.assertEqual(results, [None] * 100)
        self.assertLess(len(calls), 2500, 'missing games should not stat every conventional candidate again')

    def test_scan_snapshot_refreshes_on_rescan_and_live_metadata_remains_fresh(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        game = library.scan_system(system)[0]
        conventional = self.write('roms/snes/images/A-thumb.png', 'new conventional')
        self.assertIsNone(library.existing_artwork(game))
        game = library.scan_system(system)[0]
        self.assertEqual(library.existing_artwork(game), conventional)
        custom = self.write('roms/snes/custom/A.png', 'new custom')
        self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><thumbnail>custom/A.png</thumbnail></game></gameList>')
        self.assertEqual(library.existing_artwork(game), custom)
        custom.unlink()
        conventional.unlink()
        self.assertIsNone(library.existing_artwork(game))
        destination = library.artwork_destination(game)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b'downloaded')
        self.assertEqual(library.existing_artwork(game), destination)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO fixture requires POSIX')
    def test_nonregular_gamelist_is_not_opened(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        fifo = system.path/'gamelist.xml'
        os.mkfifo(fifo)
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == fifo:
                self.fail('scanner must reject FIFO metadata before opening')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            games = library.scan_system(system)
        self.assertEqual([game.relative_path for game in games], ['A.sfc'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO fixture requires POSIX')
    def test_nonregular_rom_and_manifest_entries_are_ignored(self):
        system = self.system('psx', ('.cue', '.chd'))
        self.write('roms/psx/Actual.chd', 'rom')
        fifo = system.path/'Pipe.cue'
        os.mkfifo(fifo)
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path == fifo:
                self.fail('scanner must reject FIFO manifests before opening')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            games = library.scan_system(system)
        self.assertEqual([game.relative_path for game in games], ['Actual.chd'])

    def test_screenshot_image_and_conventional_image_do_not_fill_box(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        screenshot = self.write('roms/snes/images/A-image.png', 'screenshot')
        xml = self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>images/A-image.png</image></game></gameList>')
        original = xml.read_bytes()
        game = library.scan_system(system)[0]
        self.assertIsNone(game.existing_artwork)
        self.assertIsNone(library.existing_artwork(game))
        self.assertEqual(xml.read_bytes(), original)
        self.assertEqual(screenshot.read_text(), 'screenshot')

    def test_thumbnail_takes_priority_and_accepts_custom_unhinted_filename(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        box = self.write('roms/snes/assets/A.png', 'current thumbnail box')
        for image in ('A-image.png', 'A-cover.png'):
            with self.subTest(image=image):
                self.write('roms/snes/images/'+image, 'screenshot or legacy cover')
                self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>images/'+image+'</image><thumbnail>assets/A.png</thumbnail></game></gameList>')
                game = library.scan_system(system)[0]
                self.assertEqual(game.existing_artwork, box)
                self.assertEqual(library.existing_artwork(game), box)

    def test_legacy_box_image_hints_are_reused(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/A.sfc', 'rom')
        for relative in ('covers/A.png', 'assets/A-thumb.png', 'Named_Boxarts/A.png', 'media/box2d/A.png', 'images/novacore/A.png'):
            with self.subTest(path=relative):
                art = self.write('roms/snes/'+relative, 'box')
                xml = self.write('roms/snes/gamelist.xml', '<gameList><game><path>A.sfc</path><image>'+relative+'</image></game></gameList>')
                original = xml.read_bytes()
                game = library.scan_system(system)[0]
                self.assertEqual(game.existing_artwork, art)
                self.assertEqual(library.existing_artwork(game), art)
                self.assertEqual(xml.read_bytes(), original)
                art.unlink()

    def test_scan_cancelled_before_work_returns_without_mutation(self):
        system = self.system('snes', ('.sfc',))
        self.write('roms/snes/Game.sfc', 'rom')
        self.assertEqual(library.scan_system(system, cancelled=lambda: True), [])
        self.assertFalse((system.path/'images').exists())


if __name__ == '__main__':
    unittest.main()
