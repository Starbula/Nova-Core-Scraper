"""Official RetroBat system names must reach the same verified source platform.

Names/fullnames come from RetroBat-Official/retrobat main's
system/templates/emulationstation/es_systems.cfg, checked 1 November 2026.
These fixtures keep neighboring platforms separate and do not infer aliases
from similar hardware or from a game's title.
"""
from __future__ import annotations

import zipfile
from urllib.parse import quote
from xml.sax.saxutils import escape

import pytest
import requests

from novacore_scraper.api_sources import _platform_id
from novacore_scraper.launchbox import LaunchBoxProvider
from novacore_scraper.models import Game, System
from novacore_scraper.providers import LibretroProvider


# alias, RetroBat fullname, LaunchBox platform, Libretro directory, fixture ID
ALIASES = [
    ('snes-msu1', 'Super NES MSU-1', 'Super Nintendo Entertainment System',
     'Nintendo - Super Nintendo Entertainment System', 101),
    ('megadrive-msu', 'MD-MSU', 'Sega Genesis', 'Sega - Mega Drive - Genesis', 102),
    ('amiga4000', 'Amiga 4000', 'Commodore Amiga', 'Commodore - Amiga', 103),
    ('wswan', 'WonderSwan', 'WonderSwan', 'Bandai - WonderSwan', 104),
    ('wswanc', 'WonderSwan Color', 'WonderSwan Color', 'Bandai - WonderSwan Color', 105),
    ('cps1', 'CPS-I', 'Arcade', 'MAME', 106),
    ('cps2', 'CPS-II', 'Arcade', 'MAME', 106),
    ('cps3', 'CPS-III', 'Arcade', 'MAME', 106),
    ('cave', 'CAVE', 'Arcade', 'MAME', 106),
    ('segastv', 'ST-V', 'Arcade', 'MAME', 106),
    ('model2', 'Model 2', 'Arcade', 'MAME', 106),
    ('model3', 'Model 3', 'Arcade', 'MAME', 106),
    ('bbcmicro', 'BBC Micro', 'BBC Microcomputer System', None, 107),
]


def game(root, name, fullname):
    path = root / name / 'Alias Coverage Fixture.zip'
    return Game(System(name, fullname, path.parent, frozenset({'.zip'})),
                path, path.name, 'Alias Coverage Fixture')


@pytest.fixture
def launchbox_catalogue(tmp_path):
    provider = LaunchBoxProvider(tmp_path / 'launchbox-cache')
    provider.cache_dir.mkdir()
    platforms = {
        101: 'Super Nintendo Entertainment System', 102: 'Sega Genesis',
        103: 'Commodore Amiga', 104: 'WonderSwan', 105: 'WonderSwan Color',
        106: 'Arcade', 107: 'BBC Microcomputer System', 999: 'Nintendo Game Boy Color',
    }
    records = []
    for identifier, platform in platforms.items():
        records.append('<Game><DatabaseID>' + str(identifier) + '</DatabaseID>'
                       '<Name>Alias Coverage Fixture</Name><Platform>' + escape(platform) + '</Platform></Game>')
        records.append('<GameImage><DatabaseID>' + str(identifier) + '</DatabaseID>'
                       '<FileName>cover-' + str(identifier) + '.png</FileName>'
                       '<Type>Box - Front</Type><Region>Europe</Region></GameImage>')
    archive = tmp_path / 'catalogue.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('Metadata.xml', '<LaunchBox>' + ''.join(records) + '</LaunchBox>')
    provider._index_archive(archive, provider._index, lambda: None)
    yield provider
    provider.close()


@pytest.mark.parametrize('name,fullname,platform,directory,identifier', ALIASES)
def test_launchbox_official_alias_reaches_only_its_exact_platform(
        tmp_path, launchbox_catalogue, name, fullname, platform, directory, identifier):
    result = launchbox_catalogue.find(game(tmp_path, name, fullname), lambda: None)
    assert result is not None
    assert result.provider_game_id == str(identifier)
    assert result.url == 'https://images.launchbox-app.com/cover-' + str(identifier) + '.png'


@pytest.mark.parametrize('name,fullname,platform,directory,identifier',
                         [row for row in ALIASES if row[3] is not None])
def test_libretro_official_alias_uses_the_verified_catalogue_directory(
        tmp_path, monkeypatch, name, fullname, platform, directory, identifier):
    expected = 'https://thumbnails.libretro.com/' + quote(directory, safe='') + '/Named_Boxarts/'

    def send(session, request, **kwargs):
        response = requests.Response()
        response.status_code = 200 if request.url == expected else 404
        response._content = b'<a href="Alias Coverage Fixture (Europe).png">cover</a>'
        response._content_consumed = True
        response.url = request.url
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    provider = LibretroProvider(tmp_path / 'libretro-cache')
    try:
        result = provider.find(game(tmp_path, name, fullname), lambda: None)
        assert result is not None
        assert result.url == expected + 'Alias%20Coverage%20Fixture%20%28Europe%29.png'
    finally:
        provider.close()


@pytest.mark.parametrize('name,fullname,platform,directory,identifier', ALIASES)
def test_official_api_alias_selects_one_platform_without_borrowing_neighbor_games(
        tmp_path, name, fullname, platform, directory, identifier):
    platforms = [
        {'id': identifier, 'names': [platform]},
        {'id': 999, 'names': ['Nintendo Game Boy Color']},
    ]
    assert _platform_id(platforms, game(tmp_path, name, fullname)) == identifier


def test_snes_msu_alias_does_not_accept_game_boy_color_only_catalogue(tmp_path):
    # A new spelling must resolve one existing machine, never broaden to a family.
    item = game(tmp_path, 'snes-msu1', 'Super NES MSU-1')
    assert _platform_id([{'id': 999, 'names': ['Nintendo Game Boy Color']}], item) is None
