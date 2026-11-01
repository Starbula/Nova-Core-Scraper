"""Windows desktop entry point and network-free frozen executable smoke test."""
from __future__ import annotations
import argparse
from pathlib import Path
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import tempfile


def self_test(output: Path) -> bool:
    result = {'success': False}
    try:
        from PIL import Image
        from novacore_scraper.models import Artwork
        from novacore_scraper.library import validate_retrobat, discover_systems, scan_system
        from novacore_scraper.engine import ScrapeEngine
        import xml.etree.ElementTree as ET
        class FixtureProvider:
            workers = 1
            def prepare(self, checkpoint):
                checkpoint()
            def find(self, game, checkpoint):
                checkpoint()
                return Artwork('https://example.invalid/fixture.png', 'self-test fixture')
            def download(self, artwork, destination, checkpoint):
                checkpoint()
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (4, 4), 'blue').save(destination)
        with tempfile.TemporaryDirectory(prefix='novacore-self-test-') as name:
            root = Path(name)
            (root/'retrobat.exe').write_bytes(b'Fixture only, never executed')
            system_path = root/'roms'/'nes'
            system_path.mkdir(parents=True)
            rom = system_path/'Example (Europe).nes'
            rom.write_bytes(b'ROM fixture must stay unchanged')
            original = rom.read_bytes()
            validate_retrobat(root)
            systems = discover_systems(root)
            nes = next(s for s in systems if s.name == 'nes')
            games = scan_system(nes)
            if len(games) != 1:
                raise AssertionError('Fixture scan did not return one game')
            engine = ScrapeEngine(root, FixtureProvider(), process_running=lambda: True)
            engine.start(games)
            if not engine.wait(15) or engine.snapshot().completed != 1:
                raise AssertionError('Fixture download failed: '+engine.snapshot().message)
            if (system_path/'gamelist.xml').exists() or engine.snapshot().pending != 1:
                raise AssertionError('Running-process metadata deferral failed')
            engine.process_running = lambda: False
            engine.apply_pending()
            xml = ET.parse(system_path/'gamelist.xml')
            if not xml.findtext('game/thumbnail') or engine.snapshot().pending:
                raise AssertionError('Pending metadata was not applied')
            engine.start(scan_system(nes))
            if not engine.wait(15) or engine.snapshot().skipped != 1:
                raise AssertionError('Repeated scrape did not skip existing artwork')
            if rom.read_bytes() != original:
                raise AssertionError('Fixture ROM changed')
            result.update(success=True, tests=['discovery', 'scan', 'fixture image', 'process deferral', 'metadata', 'repeat skip', 'ROM integrity'])
    except Exception as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result['success']


def configure_logging():
    from novacore_scraper.settings import data_directory
    directory = data_directory()/'logs'
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory/'scraper.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    # HTTP debug logs include authenticated query strings; never enable them.
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    logging.getLogger('requests').setLevel(logging.CRITICAL)


def main():
    parser = argparse.ArgumentParser(description='NovaCore Artwork Scraper')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--self-test', metavar='OUTPUT_JSON', type=Path, help='Network-free temporary-fixture smoke test')
    group.add_argument('--smoke-test-ui', action='store_true', help='Open and close the desktop interface without scanning')
    args = parser.parse_args()
    if args.self_test:
        return 0 if self_test(args.self_test) else 1
    try:
        configure_logging()
        from novacore_scraper.ui import launch
        launch(smoke_test=args.smoke_test_ui)
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error('Application could not start (%s)', type(exc).__name__)
        try:
            import tkinter.messagebox as messages
            messages.showerror('NovaCore Artwork Scraper', 'Could not start: '+str(exc)+'\nCheck data/logs/scraper.log beside the application.')
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
