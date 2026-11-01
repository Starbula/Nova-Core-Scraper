"""TOSEC metadata variants retain publisher identity and altered-game flags."""
import importlib
from pathlib import Path

import pytest

from novacore_scraper.models import Game, System


def helper():
    spec = importlib.util.find_spec('novacore_scraper.computer_titles')
    assert spec is not None, 'the conservative computer-title parser must be available'
    return importlib.import_module('novacore_scraper.computer_titles')


def game(title, filename=None, system='zxspectrum'):
    path = Path('/roms') / system / ((filename or title) + '.zip')
    return Game(System(system, system, path.parent, frozenset({'.zip'})), path, path.name, title)


@pytest.mark.parametrize('raw,title,base,publisher', [
    ('180 (1986)(Mastertronic Added Dimension)(128k)', '180 (Mastertronic Added Dimension)', '180', 'Mastertronic Added Dimension'),
    ('Back to Skool (1985)(Microsphere)', 'Back to Skool (Microsphere)', 'Back to Skool', 'Microsphere'),
    ('Blasteroids (1987)(Image Works)(128k)', 'Blasteroids (Image Works)', 'Blasteroids', 'Image Works'),
    ('Alien 8 (1985)(Ultimate Play The Game)', 'Alien 8 (Ultimate Play The Game)', 'Alien 8', 'Ultimate Play The Game'),
])
def test_real_report_tosec_titles_retain_publisher_but_expose_structured_metadata(raw, title, base, publisher):
    result = helper().parse_computer_title(raw, 'zxspectrum')
    assert result is not None
    assert result.title == title and result.base_title == base
    assert result.publisher == publisher and result.source_title == raw
    assert result.date in {'1985', '1986', '1987'}
    assert 'TOSEC' in result.note


@pytest.mark.parametrize('system', ['zx81', 'zxspectrum', 'amstradcpc', 'c64', 'amiga', 'amiga500',
                                   'amiga1200', 'amiga4000', 'amigacd32', 'amigacdtv'])
def test_tosec_parsing_is_available_only_for_verified_computer_system_names(system):
    result = helper().parse_computer_title('Game (1985)(Publisher)', system)
    assert result is not None and result.title == 'Game (Publisher)'


@pytest.mark.parametrize('system', ['gb', 'gba', 'gbc', 'nes', 'n64', 'mame', 'windows', 'atari800'])
def test_other_platforms_do_not_reinterpret_parenthetical_title_parts(system):
    assert helper().parse_computer_title('Game (1985)(Publisher)(128k)', system) is None


@pytest.mark.parametrize('date', ['19xx', '199x', '1986', '2001-01', '1986-06-21', '19xx-12',
                                 '19xx-12-25', '19xx-12-2x', '1984-02-29'])
def test_documented_tosec_dates_are_structured_metadata(date):
    result = helper().parse_computer_title('Game (' + date + ')(Publisher)', 'zxspectrum')
    assert result is not None and result.date == date


@pytest.mark.parametrize('raw', ['Game (1985)', 'Game (Publisher)', 'Game(1985)(Publisher)',
    'Game (1985) (Publisher)', 'Game (1985)()', 'Game (1985)( )', 'Game (1985)(-)',
    'Game (1985)(!!!)', 'Game (19x5)(Publisher)', 'Game (1985-13)(Publisher)',
    'Game (1985-00)(Publisher)', 'Game (1985-02-29)(Publisher)', 'Game (1985-04-31)(Publisher)',
    'Game (1985)(Publisher) Another Game', 'Game (1985)(Publisher)[Hack',
    'Game (1985)(Publisher (Hack))', '(1985)(Publisher)'])
def test_missing_ambiguous_or_malformed_metadata_does_not_create_a_title_variant(raw):
    assert helper().parse_computer_title(raw, 'zxspectrum') is None


@pytest.mark.parametrize('qualifier', ['[h]', '[cr Group]', '[t +2]', '[tr es]', '[type-in]',
                                     '[CSSCGC]', '[mod Jet Set Willy]', '(Prototype)',
                                     '(Beta)', '(Unknown Edition)', '(Part 1 of 2)'])
def test_all_unknown_and_modified_game_suffixes_remain_part_of_the_identity(qualifier):
    raw = 'Game (1985)(Publisher)(48k)' + qualifier
    result = helper().parse_computer_title(raw, 'zxspectrum')
    assert result.title == 'Game (Publisher)' + qualifier
    assert result.base_title == 'Game' + qualifier


def test_demo_and_version_prefixes_and_publisher_text_are_preserved():
    result = helper().parse_computer_title('Game v1.03b (demo) (1985)(128k Software)(16k)', 'zxspectrum')
    assert result.title == 'Game v1.03b (demo) (128k Software)'
    assert result.base_title == 'Game v1.03b (demo)'
    assert result.publisher == '128k Software'


def test_memory_removal_is_system_specific_and_does_not_erase_other_hardware():
    parse = helper().parse_computer_title
    assert parse('Game (1985)(Publisher)(128k)', 'c64').title == 'Game (Publisher)(128k)'
    assert parse('Game (1985)(Publisher)(AGA)(512k)', 'amiga500').title == 'Game (Publisher)(AGA)(512k)'
    assert parse('Game (1985)(Publisher)(256k)', 'zxspectrum').title == 'Game (Publisher)(256k)'


def test_strict_tosec_filename_controls_identity_when_display_omits_hack():
    item = game('Game (1985)(Publisher)', 'Game (1985)(Publisher)(128k)[h]')
    variants = helper().computer_title_variants(item)
    assert len(variants) == 1 and variants[0].title == 'Game (Publisher)[h]'
    assert variants[0].base_title == 'Game[h]'
    assert variants[0].source_title == item.path.stem


@pytest.mark.parametrize('display,filename', [
    ('Game (1985)(Publisher)', 'Game (Hack)'),
    ('Other Game (1985)(Publisher)', 'Game 2 (Hack)'),
    ('Other Game (1985)(Publisher)', 'Game 2 (Unknown Edition)'),
])
def test_tosec_display_never_replaces_the_filename_identity_or_moves_its_qualifiers(display, filename):
    assert helper().computer_title_variants(game(display, filename)) == ()


@pytest.mark.parametrize('display,filename,source,title', [
    ('Other Game (1986)(Publisher)', 'Game 2 (1985)(Publisher)',
     'Game 2 (1985)(Publisher)', 'Game 2 (Publisher)'),
    ('Other Game (1985)(Publisher)', 'Game 2 (1985)(Publisher)',
     'Other Game (1985)(Publisher)', 'Other Game (Publisher)'),
    ('Other Game (Hack)', 'Game 2 (1985)(Publisher)',
     'Game 2 (1985)(Publisher)', 'Game 2 (Publisher)'),
])
def test_tosec_file_and_display_conflicts_use_the_existing_filename_qualifier_guard(display, filename, source, title):
    result, = helper().computer_title_variants(game(display, filename))
    assert result.source_title == source and result.title == title


def test_malformed_filename_flags_block_display_only_variant():
    assert helper().computer_title_variants(game('Game (1985)(Publisher)', 'Game (Hack')) == ()


def test_publisher_homonyms_stay_distinct():
    parse = helper().parse_computer_title
    first = parse('180 (1984)(CCS)', 'zxspectrum')
    second = parse('180 (1986)(Mastertronic Added Dimension)(128k)', 'zxspectrum')
    assert first.title == '180 (CCS)' and second.title == '180 (Mastertronic Added Dimension)'
    assert (first.publisher, first.date) != (second.publisher, second.date)


@pytest.mark.parametrize('title', ['Tapper (C64)', 'Turrican II - The Final Fight (C64)'])
def test_exact_selected_c64_platform_suffix_can_be_removed(title):
    result = helper().parse_computer_title(title, 'c64')
    assert result is not None and result.title == title[:-6].rstrip()
    assert result.base_title == result.title and result.date == result.publisher == ''
    assert '(C64)' in result.note
    assert helper().parse_computer_title(title, 'zxspectrum') is None


def test_c64_suffix_removal_preserves_earlier_meaningful_tags():
    result = helper().parse_computer_title('Tapper (Hack) (C64)', 'c64')
    assert result.title == result.base_title == 'Tapper (Hack)'
    assert helper().parse_computer_title('Tapper (C64) (Hack)', 'c64') is None


@pytest.mark.parametrize('title', ['0001a', '0499b', 'sb141167', 'sb179870'])
def test_numeric_collection_identifiers_are_never_reinterpreted_as_games(title):
    assert helper().computer_title_variants(game(title, system='c64')) == ()


@pytest.mark.parametrize('flag,remaining', [('es', ''), ('ca', ''), ('en-es', ''), ('ES', '(ES)'), ('CA', '(CA)'), ('zz', '(zz)')])
def test_tosec_language_flags_are_scoped_and_never_invent_a_country(flag, remaining):
    module = helper()
    raw = 'Game (1985)(Publisher)(' + flag + ')'
    result = module.parse_computer_title(raw, 'zxspectrum')
    assert result.title == 'Game (Publisher)' + remaining
    assert result.base_title == 'Game' + remaining
    assert module.computer_region_title(raw, 'zxspectrum') == 'Game (1985)(Publisher)' + remaining
    assert module.computer_region_title(raw, 'gba') == raw
