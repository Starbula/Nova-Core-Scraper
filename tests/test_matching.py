"""Title normalization widens formatting tolerance without erasing game identity."""
from pathlib import Path

import pytest

from novacore_scraper.models import Game, System
from novacore_scraper.matching import MatchPolicy, match_title, title_info, title_key


def game(title):
    path = Path('/roms/gb') / (title + '.zip')
    return Game(System('gb', 'Game Boy', path.parent, frozenset({'.zip'})), path, path.name, title)


@pytest.mark.parametrize('left,right', [
    ('Pokémon - Red Version', 'PokemonRedVersion'),
    ('Legend of Zelda, The', 'The Legend of Zelda'),
    ('Élite: Dangerous!', 'elite dangerous'),
    ('Quest, An', 'An Quest'),
])
def test_formatting_changes_preserve_title_identity(left, right):
    assert title_key(left) == title_key(right)


@pytest.mark.parametrize('left,right', [
    ('The Adventure', 'Adventure'), ('Sonic', 'Sonic 2'),
    ('Quest I', 'Quest II'), ('Adventure (Episode I)', 'Adventure (Episode II)'),
    ('Adventure', 'Adventure (Adventure)'), ('Game', 'Game (Hack)'),
    ('Game', 'Game (Prototype)'), ('Game', 'Game (Beta)'),
    ('Game', 'Game (Demo)'), ('Game', 'Game (Special Edition)'),
])
def test_meaningful_title_parts_remain_distinct(left, right):
    assert title_info(left).key != title_info(right).key
    assert match_title([right + ' (Europe)'], game(left + ' (Europe)')) is None


@pytest.mark.parametrize('tag', ['Rev 1', 'Revision A', 'v1.2', 'Version 1.0', 'Disc 2', 'Disk 1 of 2'])
def test_verified_revision_and_disc_tags_allow_annotated_base_cover(tag):
    result = match_title(['Game (USA)'], game('Game (Europe) (' + tag + ')'))
    assert result and result.region == 'us'
    assert 'base' in result.note.lower() and 'region' in result.note.lower()
    assert tag.lower() in result.note.lower()
    assert match_title(['Game (Europe)'], game('Game (Europe) (' + tag + ')'), MatchPolicy(False, False)) is None


def test_requested_region_outranks_preference_and_fallback_is_last():
    result = match_title(['Game (USA)', 'Game (Europe)'], game('Game (Europe)'), preference='us')
    assert result and result.region == 'eu' and not result.note
    result = match_title(['Game (USA)'], game('Game (Europe)'))
    assert result and result.region == 'us' and 'region' in result.note.lower()
    assert match_title(['Game (USA)'], game('Game (Europe)'), MatchPolicy(False, True)) is None


def test_matching_same_game_variants_is_deterministic():
    result = match_title(['Game (USA) (Rev 1)', 'Game (USA)', 'Game (Europe)'], game('Game (Japan)'))
    assert result and result.name == 'Game (Europe)'


def test_display_name_cannot_erase_file_hack():
    item = game('Game (Europe) (Hack)')
    item = Game(item.system, item.path, item.relative_path, 'Game')
    assert match_title(['Game (Europe)'], item) is None


def test_japanese_voicing_is_meaningful_title_text():
    # NFKD emits combining dakuten; these are letters' voicing, not Latin accents.
    assert title_key('バトル') != title_key('ハトル')


def test_ranked_title_matches_expose_only_verified_same_game_variants():
    from novacore_scraper.matching import match_titles
    matches = list(match_titles(['Game (USA)', 'Game (Europe) (Rev 1)', 'Game (Europe)',
                                'Game 2 (Europe)', 'Game (Europe) (Hack)'], game('Game (Europe)')))
    assert [item.name for item in matches] == ['Game (Europe)', 'Game (Europe) (Rev 1)', 'Game (USA)']
    assert 'base' in matches[1].note.lower() and 'region' in matches[2].note.lower()
    strict = list(match_titles([item.name for item in matches], game('Game (Europe)'), MatchPolicy(False, False)))
    assert [item.name for item in strict] == ['Game (Europe)']


@pytest.mark.parametrize('system', ['gba', 'gb', 'gbc', 'nds'])
def test_numbered_handheld_archive_prefix_is_request_metadata(system):
    from novacore_scraper.matching import requested_info
    title = '0002 - Super Mario Advance (J) (Rev 1)'
    path = Path('/roms') / system / (title + '.zip')
    item = Game(System(system, system, path.parent, frozenset({'.zip'})), path, path.name, title)
    info = requested_info(item)
    assert info.key == title_info('Super Mario Advance').key
    assert info.title == 'Super Mario Advance' and info.regions == frozenset({'jp'})
    assert info.packaging == ('rev 1',)
    result = match_title(['Super Mario Advance (Japan)'], item)
    assert result and result.name == 'Super Mario Advance (Japan)'
    # Provider catalog names and IDs retain every digit.
    assert title_info(title).key.startswith('0002')
    assert title_key('0002 - Super Mario Advance').startswith('0002')


@pytest.mark.parametrize('title', ['1942', '007', '2048', '1942: The Pacific Air War', '0002 Super Mario Advance',
                                   '002 - Super Mario Advance', '00002 - Super Mario Advance', '0002-', '0002 - '])
def test_actual_numbers_and_non_archive_prefixes_are_preserved(title):
    from novacore_scraper.matching import requested_info
    assert requested_info(game(title)).key == title_info(title).key


@pytest.mark.parametrize('system', ['nes', 'n64', 'psx', 'c64', 'zxspectrum', 'mame'])
def test_archive_prefix_removal_is_scoped_to_known_handheld_numbered_sets(system):
    from novacore_scraper.matching import requested_info
    title = '0002 - Super Mario Advance (Japan)'
    path = Path('/roms') / system / (title + '.zip')
    item = Game(System(system, system, path.parent, frozenset({'.zip'})), path, path.name, title)
    assert requested_info(item).key == title_info(title).key


@pytest.mark.parametrize('qualifier', ['Hack', 'Prototype', 'Beta', 'Demo', 'Special Edition', 'Adventure'])
def test_archive_prefix_cannot_hide_meaningful_file_qualifiers(qualifier):
    from novacore_scraper.matching import requested_info
    path = Path('/roms/gba') / ('0002 - Super Mario Advance (Japan) (' + qualifier + ').zip')
    item = Game(System('gba', 'Game Boy Advance', path.parent, frozenset({'.zip'})), path, path.name,
                'Super Mario Advance (Japan)')
    info = requested_info(item)
    assert info.qualifiers == title_info('Super Mario Advance (' + qualifier + ')').qualifiers
    assert info.key == title_info('Super Mario Advance (' + qualifier + ')').key
    assert match_title(['Super Mario Advance (Japan)'], item) is None


def test_numbered_metadata_display_title_can_differ_from_the_rom_filename():
    from novacore_scraper.matching import requested_info
    path = Path('/roms/gba/0003 - Battle Network Rockman EXE (J).zip')
    item = Game(System('gba', 'Game Boy Advance', path.parent, frozenset({'.zip'})), path, path.name,
                'Mega Man Battle Network')
    assert requested_info(item).key == title_info('Mega Man Battle Network').key
    assert requested_info(item).regions == frozenset({'jp'})
    assert match_title(['Mega Man Battle Network'], item)


def test_computer_requested_variants_keep_exact_title_and_publisher():
    from novacore_scraper.matching import requested_info, requested_title_variants
    title = 'Back to Skool (1985)(Microsphere)(128k)'
    path = Path('/roms/zxspectrum') / (title + '.tap')
    item = Game(System('zxspectrum', 'ZX Spectrum', path.parent, frozenset({'.tap'})), path, path.name, title)
    variants = requested_title_variants(item)
    assert variants[0].info == requested_info(item) and variants[0].note == ''
    assert len(variants) == 2
    assert variants[1].info.key == title_info('Back to Skool (Microsphere)').key
    assert variants[1].info.qualifiers == title_info('Back to Skool (Microsphere)').qualifiers
    assert variants[1].note


@pytest.mark.parametrize('filename,display,expected', [
    ('Back to Skool (Hack)', 'Back to Skool (1985)(Microsphere)', None),
    ('Different Game 2 (Hack)', 'Back to Skool (1985)(Microsphere)', None),
    ('Back to Skool (1985)(Microsphere)(Hack)', 'Back to Skool', 'Back to Skool (Microsphere)(Hack)'),
    ('180 (1986)(Mastertronic Added Dimension)(128k)', '180 (1985)(CCS)', '180 (Mastertronic Added Dimension)'),
])
def test_computer_requested_variants_preserve_effective_filename_identity(filename, display, expected):
    from novacore_scraper.matching import requested_title_variants
    path = Path('/roms/zxspectrum') / (filename + '.tap')
    item = Game(System('zxspectrum', 'ZX Spectrum', path.parent, frozenset({'.tap'})), path, path.name, display)
    variants = requested_title_variants(item)
    if expected is None:
        assert len(variants) == 1
    else:
        assert len(variants) == 2 and variants[1].info.key == title_info(expected).key
        assert variants[1].info.qualifiers == title_info(expected).qualifiers


def test_computer_variant_is_not_enabled_for_another_platform():
    from novacore_scraper.matching import requested_title_variants
    assert len(requested_title_variants(game('Game (1985)(Publisher)(128k)'))) == 1


@pytest.mark.parametrize('flag,regions', [('es', frozenset()), ('ca', frozenset()),
                                        ('ES', frozenset({'es'})), ('CA', frozenset({'ca'})),
                                        ('en-es', frozenset())])
def test_all_tosec_variants_distinguish_language_from_country(flag, regions):
    from novacore_scraper.matching import requested_title_variants
    title = 'Game (1985)(Publisher)(' + flag + ')'
    path = Path('/roms/zxspectrum') / (title + '.tap')
    item = Game(System('zxspectrum', 'ZX Spectrum', path.parent, frozenset({'.tap'})), path, path.name, title)
    variants = requested_title_variants(item)
    assert len(variants) == 2 and all(variant.info.regions == regions for variant in variants)
    assert variants[-1].info.key == title_info('Game (Publisher)').key
