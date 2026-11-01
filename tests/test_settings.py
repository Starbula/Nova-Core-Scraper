import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from novacore_scraper.settings import Settings, load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def test_round_trip_defaults_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'data'
            with patch('novacore_scraper.settings.data_directory', return_value=directory):
                self.assertEqual(load_settings().provider,'automatic')
                save_settings(Settings(workers=2,selected_systems=['nes']))
                self.assertEqual(load_settings().selected_systems,['nes'])
                text=(directory/'settings.json').read_text()
                self.assertEqual(json.loads(text)['version'],2)
                self.assertNotIn('password',text)
                self.assertNotIn('devid',text)
    def test_bad_settings_fall_back_and_limit_concurrency(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            with patch('novacore_scraper.settings.data_directory',return_value=directory):
                (directory/'settings.json').write_text('{')
                self.assertEqual(load_settings().workers,8)
                (directory/'settings.json').write_text(json.dumps({'workers':100,'interval':0,'provider':'unknown','password':'never save'}))
                settings=load_settings()
                self.assertEqual(settings.workers,16)
                self.assertEqual(settings.interval,.25)
                self.assertEqual(settings.provider,'automatic')

    def test_legacy_libretro_migrates_defaults_and_preserves_custom_throughput(self):
        cases = (
            ({'provider':'libretro','workers':2,'interval':1}, ('automatic',8,.25)),
            ({'provider':'libretro','workers':3,'interval':2}, ('automatic',3,2)),
            ({'provider':'libretro','workers':3,'interval':1}, ('automatic',3,.25)),
            ({'provider':'screenscraper','workers':2,'interval':1}, ('screenscraper',2,1)),
        )
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            with patch('novacore_scraper.settings.data_directory',return_value=directory):
                for raw, expected in cases:
                    with self.subTest(raw=raw):
                        (directory/'settings.json').write_text(json.dumps(raw))
                        settings=load_settings()
                        self.assertEqual((settings.provider,settings.workers,settings.interval),expected)

    def test_version_two_preserves_explicit_modes_and_new_throughput_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            with patch('novacore_scraper.settings.data_directory',return_value=directory):
                for provider in ('automatic','libretro','launchbox','screenscraper'):
                    with self.subTest(provider=provider):
                        save_settings(Settings(provider=provider,workers=16,interval=.25))
                        settings=load_settings()
                        self.assertEqual((settings.provider,settings.workers,settings.interval),(provider,16,.25))
