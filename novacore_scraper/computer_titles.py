"""Conservative title variants for documented TOSEC computer filenames.

TOSEC's 2015-03-23 naming convention defines mandatory ``Title (date)(publisher)``
fields (https://www.tosecdev.org/tosec-naming-convention). Publishers remain in
matching titles: two unrelated games can share a title. ``base_title`` is only
for providers that independently corroborate the publisher and release year.
Unknown flags, altered editions and actual title text are never discarded.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
import re

from .models import Game


_SYSTEMS = frozenset({'zx81', 'zxspectrum', 'amstradcpc', 'c64', 'amiga',
                      'amiga500', 'amiga1200', 'amiga4000', 'amigacd32', 'amigacdtv'})
# Only these memory labels were verified in the reported Spectrum collection.
# Other machines' compatibility labels (e.g. Amiga AGA) remain constraints.
_MEMORY = {'zxspectrum': frozenset({'16k', '48k', '128k'})}
_LANGUAGES = frozenset({'ar', 'bg', 'bs', 'ca', 'cs', 'da', 'de', 'el', 'en', 'eo', 'es',
                        'et', 'fa', 'fi', 'fr', 'he', 'hi', 'hr', 'hu', 'is', 'it', 'ja',
                        'ko', 'lt', 'lv', 'nl', 'no', 'pl', 'pt', 'ro', 'ru', 'se', 'sh',
                        'sk', 'sl', 'sr', 'sv', 'th', 'tr', 'uk', 'vi', 'zh'})
_FLAG = re.compile(r'\([^()\[\]\r\n]*\)|\[[^\[\]\r\n]*\]')
_TAIL = re.compile(r'(?:[ \t]*(?:\([^()\[\]\r\n]*\)|\[[^\[\]\r\n]*\]))*[ \t]*\Z')
_TOSEC = re.compile(r'(?P<prefix>.+?)[ \t]+\((?P<date>(?:19|20)[0-9xX]{2}'
                    r'(?:-[0-9xX]{2}){0,2})\)\((?P<publisher>[^()\[\]\r\n]+)\)(?P<tail>.*)\Z')
_C64_SUFFIX = re.compile(r'[ \t]+\(C64\)[ \t]*\Z', re.IGNORECASE)


@dataclass(frozen=True)
class ComputerTitleVariant:
    title: str
    date: str = ''
    publisher: str = ''
    note: str = ''
    source_title: str = ''
    base_title: str = ''


def _possible(component: str, minimum: int, maximum: int) -> list[int]:
    if not re.fullmatch(r'(?:[0-9]{2}|[0-9]x|xx)', component):
        return []
    return [value for value in range(minimum, maximum + 1)
            if all(actual == expected or expected == 'x'
                   for actual, expected in zip(f'{value:02d}', component))]


def _valid_date(value: str) -> bool:
    parts = value.casefold().split('-')
    if not re.fullmatch(r'(?:19|20)(?:[0-9]{2}|[0-9]x|xx)', parts[0]):
        return False
    if len(parts) == 1:
        return True
    months = _possible(parts[1], 1, 12)
    if not months:
        return False
    if len(parts) == 2:
        return True
    days = _possible(parts[2], 1, 31)
    year = int(parts[0]) if parts[0].isdigit() else 2000
    # An unknown year/decade can include a leap year; a known date cannot.
    return any(day <= calendar.monthrange(year, month)[1] for month in months for day in days)


def _flags(value: str) -> tuple[str, ...] | None:
    """Return complete flags, refusing malformed or nested bracket syntax."""
    flags = tuple(match.group(0) for match in _FLAG.finditer(value))
    remainder = _FLAG.sub('', value)
    return None if any(char in remainder for char in '()[]\r\n') else flags


def _language_flag(value: str) -> bool:
    return (value.startswith('(') and value.endswith(')')
            and bool(re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})*', value[1:-1]))
            and all(part in _LANGUAGES for part in value[1:-1].split('-')))


def parse_computer_title(text: str, system: str) -> ComputerTitleVariant | None:
    """Parse one source name without inferring an identity or changing a file.

    Only the mandatory TOSEC date is removed from ``title``. Exact known memory
    tags after its publisher are packaging metadata. Other flags and version or
    demo text remain. A terminal ``(C64)`` is redundant only in a C64 selection.
    Unknown publishers cannot yield a safe publisher-preserving match.
    """
    system = system.casefold()
    if system not in _SYSTEMS or not isinstance(text, str) or not 0 < len(text) <= 4096:
        return None
    match = _TOSEC.fullmatch(text)
    if match:
        prefix = match['prefix'].rstrip()
        date, publisher, tail = match['date'], match['publisher'].strip(), match['tail']
        if (not prefix or _flags(prefix) is None or not _valid_date(date)
                or not any(char.isalnum() for char in publisher) or not _TAIL.fullmatch(tail)):
            return None
        memory = _MEMORY.get(system, frozenset())
        removed = []
        languages = []

        def flag(value):
            raw = value.group(0)
            if _language_flag(raw):
                languages.append(raw)
                return ''
            if raw.startswith('(') and raw[1:-1].casefold() in memory:
                removed.append(raw)
                return ''
            return raw

        remaining = _FLAG.sub(flag, tail).rstrip()
        title = prefix + ' (' + publisher + ')' + remaining
        base = prefix + remaining
        note = 'TOSEC release date (' + date + ') treated as metadata; publisher retained.'
        if removed:
            note += ' Shared cover across memory configuration ' + ', '.join(dict.fromkeys(removed)) + '.'
        if languages:
            note += ' TOSEC language flags treated as language metadata; no country inferred.'
        return ComputerTitleVariant(title, date, publisher, note, text, base)
    if system == 'c64' and (suffix := _C64_SUFFIX.search(text)):
        title = text[:suffix.start()].rstrip()
        if title and _flags(title) is not None:
            return ComputerTitleVariant(title, note='Selected-system label (C64) treated as metadata.',
                                        source_title=text, base_title=title)
    return None


def computer_region_title(text: str, system: str) -> str:
    """Remove known TOSEC language flags only for geographic interpretation."""
    parsed = parse_computer_title(text, system)
    match = _TOSEC.fullmatch(text) if parsed is not None and parsed.date else None
    if match is None:
        return text
    tail = _FLAG.sub(lambda value: '' if _language_flag(value.group(0)) else value.group(0), match['tail'])
    return text[:match.start('tail')] + tail


def computer_title_variants(game: Game) -> tuple[ComputerTitleVariant, ...]:
    """Parse only the source selected by the existing filename qualifier guard.

    Never move a filename qualifier onto a different display title. Providers
    still resolve uniqueness and enforce their normal region/packaging policy.
    """
    # Runtime import keeps the low-level parser available to matching.py without
    # creating a module-import cycle.
    from .matching import title_info

    filename = game.path.stem
    if _flags(filename) is None:
        return ()
    display_info, file_info = title_info(game.title), title_info(filename)
    source = filename if file_info.qualifiers and file_info.qualifiers != display_info.qualifiers else game.title
    parsed = parse_computer_title(source, game.system.name)
    return (parsed,) if parsed is not None else ()
