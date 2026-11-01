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
