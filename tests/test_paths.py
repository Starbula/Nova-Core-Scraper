import unittest
from novacore_scraper import paths


class XMLPathTextTests(unittest.TestCase):
    def test_literal_spaces_remain_distinct_even_for_missing_entries(self):
        for value in (' Game.nes', 'Game.nes ', './ Game.nes ', ' covers/box.png', '~Game.nes', './~Game.nes'):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value)

    def test_explicit_xml_line_formatting_is_removed(self):
        for value in ('\n ./Game.nes\n', '\r\n  ./folder/Game.nes\r\n ', '\t./images/box.png\t', '  ./images/box.png  ', '  ../shared/box.png  '):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value.strip())

    def test_home_absolute_and_parent_syntax_remains_for_boundary_validation(self):
        for value in ('~/file', '/outside/file', '../file', 'C:/file', '//server/file'):
            with self.subTest(value=value):
                self.assertEqual(paths.xml_path_text(value), value)
