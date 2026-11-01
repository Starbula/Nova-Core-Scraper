"""Exact game identity with tolerant typography and explicit cover fallbacks.

Only recognized region, language, revision and disc packaging tags are metadata.
Unknown parenthetical text stays part of the title; digits and edition labels
are never discarded. Providers must establish a unique game before ranking art.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from .models import Game


@dataclass(frozen=True)
class MatchPolicy:
    allow_other_regions: bool = True
    allow_base_cover: bool = True


@dataclass(frozen=True)
class TitleInfo:
    key: str
    regions: frozenset[str]
    qualifiers: tuple[str, ...]
    packaging: tuple[str, ...]
    title: str


@dataclass(frozen=True)
class TitleMatch:
    name: str
    region: str
    note: str = ''


REGION_ALIASES = {
    'eu': 'eu', 'europe': 'eu', 'european': 'eu', 'pal': 'eu',
    'us': 'us', 'usa': 'us', 'united states': 'us', 'north america': 'us', 'ntsc-u': 'us',
    'jp': 'jp', 'japan': 'jp', 'japanese': 'jp', 'ntsc-j': 'jp',
    'wor': 'wor', 'world': 'wor', 'fr': 'fr', 'france': 'fr', 'de': 'de', 'germany': 'de',
    'es': 'es', 'spain': 'es', 'it': 'it', 'italy': 'it', 'uk': 'uk', 'united kingdom': 'uk',
    'au': 'au', 'australia': 'au', 'oceania': 'au', 'ca': 'ca', 'canada': 'ca',
    'br': 'br', 'brazil': 'br', 'kr': 'kr', 'korea': 'kr', 'cn': 'cn', 'china': 'cn',
    'tw': 'tw', 'taiwan': 'tw', 'asia': 'asi', 'asi': 'asi',
}
_TAGS = re.compile(r'\(([^()]*)\)|\[([^\[\]]*)\]')
_LANGUAGES = {'en', 'fr', 'de', 'es', 'it', 'ja', 'pt', 'nl', 'sv', 'zh', 'ko', 'ru', 'da', 'fi', 'no', 'pl', 'cs', 'hu'}
_PACKAGING = re.compile(
    r'(?:rev(?:ision)?\.?\s*(?:\d+(?:[.\-_]\d+)*[a-z]?|[a-z])'
    r'|(?:v|ver\.?|version)\s*\d+(?:[.\-_]\d+)*[a-z]?'
    r'|(?:disc|disk|cd)\s*\d+(?:\s+of\s+\d+)?)', re.IGNORECASE)


def _normal(text: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def region_key(text: str) -> str:
    value = _normal(text)
    return REGION_ALIASES.get(value, value)


def title_key(text: str) -> str:
    """Normalize typography, preserving letters, numbers and leading articles."""
    text = unicodedata.normalize('NFKD', text).casefold()
    characters = []
    non_latin_letter = False
    for char in text:
        if unicodedata.category(char).startswith('M'):
            # Removing Latin accents accommodates catalogue spelling. Other
            # scripts use marks as meaningful letters (e.g. Japanese dakuten).
            if non_latin_letter:
                characters.append(char)
        else:
            non_latin_letter = char.isalpha() and not unicodedata.name(char, '').startswith('LATIN ')
            characters.append(char)
    text = ''.join(characters)
    text = re.sub(r'^(.*),\s*(the|an|a)\s*$', r'\2 \1', text.strip())
    return ''.join(char for char in text if char.isalnum() or unicodedata.category(char).startswith('M'))


@lru_cache(maxsize=32768)
def title_info(text: str) -> TitleInfo:
    regions: set[str] = set()
    qualifiers, packaging = [], []

    def tag(match):
        value = _normal(match.group(1) or match.group(2) or '')
        if not value or (match.group(2) is not None and value == '!'):
            return ''
        parts = [part.strip() for part in re.split(r'[,/]', value)]
        if all(part in REGION_ALIASES for part in parts):
            regions.update(REGION_ALIASES[part] for part in parts)
        elif re.fullmatch(r'[uej]{1,3}', value) and len(set(value)) == len(value):
            regions.update({'u': 'us', 'e': 'eu', 'j': 'jp'}[letter] for letter in value)
        elif all(part in _LANGUAGES for part in parts):
            pass
        elif _PACKAGING.fullmatch(value):
            packaging.append(value)
        else:
            qualifiers.append(title_key(value))
            return match.group(0)
        return ''

    title = ' '.join(_TAGS.sub(tag, text).split())
    return TitleInfo(title_key(title), frozenset(regions), tuple(sorted(qualifiers)), tuple(sorted(packaging)), title)


def requested_info(game: Game) -> TitleInfo:
    """File metadata may add constraints that a shorter display title omitted."""
    display, filename = title_info(game.title), title_info(game.path.stem)
    identity = filename if filename.qualifiers and filename.qualifiers != display.qualifiers else display
    return TitleInfo(identity.key, filename.regions or display.regions,
                     identity.qualifiers, filename.packaging or display.packaging, identity.title)


def region_rank(region: str, explicit_regions: Iterable[str], preference: str,
                policy: MatchPolicy | None = None) -> tuple[int, int, int, str] | None:
    """Rank covers only after game identity is proven; None means disallowed."""
    policy = policy or MatchPolicy()
    explicit = frozenset(region_key(value) for value in explicit_regions)
    region, preference = region_key(region), region_key(preference)
    if explicit:
        tier = 0 if region in explicit else 1 if region == 'wor' else 2 if not region else 3
        if tier > 1 and not policy.allow_other_regions:
            return None
    else:
        tier = 0
    order = dict.fromkeys([preference, 'wor', 'us', 'eu', 'jp', ''])
    priorities = {value: index for index, value in enumerate(order)}
    return tier, priorities.get(region, len(priorities)), 0 if region else 1, region


def match_note(requested: TitleInfo, candidate: TitleInfo, region: str) -> str:
    notes = []
    if requested.packaging != candidate.packaging:
        labels = ', '.join(requested.packaging or candidate.packaging)
        notes.append('Base-game cover shared across revision/disc packaging (' + labels + ').')
    if requested.regions and region not in requested.regions and region != 'wor':
        actual = region.upper() if region else 'unspecified'
        notes.append('Other-region cover: ' + actual + '; requested ' + ', '.join(sorted(value.upper() for value in requested.regions)) + '.')
    return ' '.join(notes)


def match_titles(candidate_names: Iterable[str], game: Game, policy: MatchPolicy | None = None,
                 preference: str = 'eu') -> Iterable[TitleMatch]:
    """Yield ranked variants of one exact title, never a similar title."""
    policy = policy or MatchPolicy()
    requested = requested_info(game)
    choices = []
    for name in dict.fromkeys(candidate_names):
        candidate = title_info(name)
        if candidate.key != requested.key:
            continue
        if not policy.allow_base_cover and candidate.packaging != requested.packaging:
            continue
        ranked_regions = [(region_rank(region, requested.regions, preference, policy), region)
                          for region in candidate.regions or {''}]
        ranked_regions = [(rank, region) for rank, region in ranked_regions if rank is not None]
        if not ranked_regions:
            continue
        rank, region = min(ranked_regions)
        # Regional fidelity precedes packaging fidelity: art for a revision is
        # still a cover for this proven title, not another game.
        choices.append((rank, candidate.packaging != requested.packaging, bool(candidate.packaging),
                        _normal(name), name, region, candidate))
    for _, _, _, _, name, region, candidate in sorted(choices):
        yield TitleMatch(name, region, match_note(requested, candidate, region))


def match_title(candidate_names: Iterable[str], game: Game, policy: MatchPolicy | None = None,
                preference: str = 'eu') -> TitleMatch | None:
    """Return the first ranked cover variant for backwards compatibility."""
    return next(iter(match_titles(candidate_names, game, policy, preference)), None)
