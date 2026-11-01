"""Reproducible scanner benchmark; fixture creation is excluded from timing.

Run: python tools/benchmark_library.py --games 10000 [--profile]
Only temporary fixture files are written. No installed library is scanned.
"""
from __future__ import annotations

import argparse
from collections import Counter
import cProfile
import io
import os
from pathlib import Path
import pstats
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from novacore_scraper import library
from novacore_scraper.models import System


def fixture(root: Path, count: int) -> System:
    for folder in ('roms', 'images', 'media/screenshots', 'media/videos', 'manuals', 'unrelated'):
        (root/folder).mkdir(parents=True, exist_ok=True)
    games = []
    for index in range(count):
        title = f'Game {index:05}'
        for folder, extension in (('roms', '.sfc'), ('images', '-thumb.png'), ('media/screenshots', '-image.png'), ('media/videos', '.mp4'), ('manuals', '.pdf'), ('unrelated', '.txt')):
            (root/folder/(title+extension)).touch()
        games.append(f'<game><path>roms/{title}.sfc</path><name>{title}</name><thumbnail>images/{title}-thumb.png</thumbnail><image>media/screenshots/{title}-image.png</image></game>')
    (root/'gamelist.xml').write_text('<gameList>'+''.join(games)+'</gameList>', encoding='utf-8')
    return System('snes', 'Super Nintendo', root, frozenset({'.sfc'}))


def measure(system: System, count: int, profile: bool) -> None:
    library._cached_gamelist.cache_clear()
    started = perf_counter()
    games = library.scan_system(system)
    elapsed = perf_counter()-started
    assert len(games) == count and all(game.existing_artwork is not None for game in games)
    print(f'{count:,} games, {count*6:,} fixture files: {elapsed:.3f} s')
    counters = Counter()
    original_stat, original_lstat, original_scandir = os.stat, os.lstat, os.scandir
    class Entry:
        def __init__(self, entry):
            self.entry = entry
        def __getattr__(self, name):
            return getattr(self.entry, name)
        def stat(self, *args, **kwargs):
            counters['DirEntry.stat'] += 1
            return self.entry.stat(*args, **kwargs)
    class Directory:
        def __init__(self, path):
            self.iterator = original_scandir(path)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
        def __iter__(self):
            return self
        def __next__(self):
            return Entry(next(self.iterator))
        def close(self):
            self.iterator.close()
    def counted_stat(*args, **kwargs):
        counters['os.stat'] += 1
        return original_stat(*args, **kwargs)
    def counted_lstat(*args, **kwargs):
        counters['os.lstat'] += 1
        return original_lstat(*args, **kwargs)
    def counted_scandir(path):
        counters['scandir'] += 1
        return Directory(path)
    library._cached_gamelist.cache_clear()
    with patch.object(os, 'stat', counted_stat), patch.object(os, 'lstat', counted_lstat), patch.object(os, 'scandir', counted_scandir):
        assert len(library.scan_system(system)) == count
    print('Metadata API calls:', dict(counters))
    if profile:
        library._cached_gamelist.cache_clear()
        profiler = cProfile.Profile()
        profiler.runcall(library.scan_system, system)
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).strip_dirs().sort_stats('cumtime').print_stats(15)
        print(output.getvalue())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games', type=int, default=10000)
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    with TemporaryDirectory() as temporary:
        measure(fixture(Path(temporary).resolve(), args.games), args.games, args.profile)
