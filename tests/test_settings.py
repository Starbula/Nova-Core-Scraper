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
                self.assertEqual(load_settings().provider,'libretro')
                save_settings(Settings(workers=2,selected_systems=['nes']))
                self.assertEqual(load_settings().selected_systems,['nes'])
                text=(directory/'settings.json').read_text()
                self.assertNotIn('password',text)
                self.assertNotIn('devid',text)
    def test_bad_settings_fall_back_and_limit_concurrency(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            with patch('novacore_scraper.settings.data_directory',return_value=directory):
                (directory/'settings.json').write_text('{')
                self.assertEqual(load_settings().workers,2)
                (directory/'settings.json').write_text(json.dumps({'workers':100,'interval':0,'provider':'unknown','password':'never save'}))
                settings=load_settings()
                self.assertEqual(settings.workers,4)
                self.assertGreaterEqual(settings.interval,.5)
                self.assertEqual(settings.provider,'libretro')
