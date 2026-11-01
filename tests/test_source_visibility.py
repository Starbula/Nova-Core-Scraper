"""Evidence of actual source participation must survive fallback and concurrency."""
from concurrent.futures import ThreadPoolExecutor
import csv
import threading

import pytest

from novacore_scraper.models import Artwork, Game, System
from novacore_scraper.providers import AuthenticationError, ProviderError, QuotaError
from novacore_scraper.sources import FallbackProvider, error_diagnostic


class Source:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error = name, result, error
        self.checks = 0

    def prepare(self, checkpoint):
        checkpoint()

    def check_connection(self, checkpoint):
        checkpoint()
        self.checks += 1
        if self.error:
            raise self.error
        return 'Authenticated API request succeeded.'

    def find(self, game, checkpoint):
        checkpoint()
        if self.error:
            raise self.error
        return self.result

    def missing_reason(self, game=None):
        return 'no_title_match', 'No matching title was found.'

    def download(self, artwork, destination, checkpoint):
        checkpoint()


def test_quota_failure_retains_healthy_no_match_results_and_counts(tmp_path):
    chain = FallbackProvider([Source('Libretro'), Source('LaunchBox'),
                              Source('IGDB'), Source('TheGamesDB', error=QuotaError('quota'))])
    for _ in range(2):
        with pytest.raises(ProviderError) as error:
            list(chain.candidates(object(), lambda: None))
        trace = chain.attempt_summary()
        assert all(name in trace for name in ('Libretro', 'LaunchBox', 'IGDB', 'TheGamesDB'))
        assert 'no title match' in trace.lower() and 'quota exhausted' in trace.lower()
        assert 'IGDB' in str(error.value) and 'Libretro' in str(error.value)
    rows = {row.name: row for row in chain.source_statuses()}
    assert rows['IGDB'].lookups == 2
    assert rows['TheGamesDB'].lookups == 1
    assert rows['TheGamesDB'].state == 'quota_exhausted'
    assert rows['IGDB'].downloads == 0 and rows['IGDB'].requests == 0


def test_healthy_source_can_download_after_quota_failure(tmp_path):
    art = Artwork('https://example.test/cover.png', 'IGDB')
    chain = FallbackProvider([Source('TheGamesDB', error=QuotaError('quota')), Source('IGDB', art)])
    matches = chain.candidates(object(), lambda: None)
    found = next(matches)
    chain.download(found, tmp_path/'cover.png', lambda: None)
    matches.close()
    assert 'IGDB: downloaded' in chain.attempt_summary()
    rows = {row.name: row for row in chain.source_statuses()}
    assert rows['IGDB'].lookups == rows['IGDB'].matches == rows['IGDB'].downloads == 1
    assert rows['TheGamesDB'].downloads == 0


def test_concurrent_game_traces_do_not_mix():
    barrier = threading.Barrier(2)
    class Concurrent(Source):
        def find(self, game, checkpoint):
            barrier.wait(timeout=2)
            if game == 'first':
                raise QuotaError('quota')
            return self.result
    chain = FallbackProvider([Concurrent('IGDB', Artwork('https://example.test/c.png', 'IGDB'))])
    def work(game):
        matches = chain.candidates(game, lambda: None)
        try:
            next(matches, None)
        except ProviderError:
            pass
        finally:
            matches.close()
        return chain.attempt_summary()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(work, ('first', 'second')))
    assert 'quota exhausted' in first and 'matched' not in first
    assert 'matched' in second and 'quota exhausted' not in second


def test_check_sources_bypasses_preparation_cache_and_isolates_failure():
    bad, good = Source('TheGamesDB', error=QuotaError('quota')), Source('IGDB')
    chain = FallbackProvider([bad, good])
    chain.check_sources(lambda: None)
    rows = {row.name: row for row in chain.source_statuses()}
    assert bad.checks == good.checks == 1
    assert rows['TheGamesDB'].state == 'quota_exhausted'
    assert rows['IGDB'].state == 'checked'
    assert rows['IGDB'].lookups == rows['IGDB'].downloads == 0
    assert rows['IGDB'].detail == 'Authenticated API request succeeded.'


def test_unconfirmed_local_budget_is_not_claimed_as_confirmed_server_quota():
    error = QuotaError('Local request budget is exhausted; server allowance is unconfirmed.')
    error.reason, error.retryable = 'allowance_unconfirmed', False
    assert error_diagnostic(error) == ('allowance_unconfirmed', str(error), False)


@pytest.mark.parametrize('reason', ['quota_exhausted', 'allowance_unconfirmed'])
def test_late_read_error_retains_quota_suspension_and_allows_already_routed_cover(tmp_path, reason):
    entered, release = threading.Event(), threading.Event()
    quota = QuotaError('Local request budget is exhausted; server allowance is unconfirmed.')
    if reason == 'allowance_unconfirmed':
        quota.reason, quota.retryable = reason, False

    class RacingSource(Source):
        def find(self, game, checkpoint):
            if game == 'late':
                entered.set()
                assert release.wait(2)
                raise ProviderError('The provider response was interrupted or invalid.')
            if game == 'quota':
                raise quota
            return self.result

        def download(self, artwork, destination, checkpoint):
            checkpoint()
            destination.write_bytes(b'public cover already identified')

    chain = FallbackProvider([RacingSource('TheGamesDB', Artwork('https://example.test/a.png', 'TheGamesDB'))])
    routed = chain.candidates('cover', lambda: None)
    artwork = next(routed)

    def consume(game):
        try:
            list(chain.candidates(game, lambda: None))
        except ProviderError as error:
            return error, chain.attempt_summary()
        raise AssertionError('the fixture lookup must fail')

    with ThreadPoolExecutor(max_workers=2) as pool:
        late = pool.submit(consume, 'late')
        assert entered.wait(2)
        exhausted = pool.submit(consume, 'quota')
        try:
            quota_failure, quota_trace = exhausted.result(timeout=2)
        finally:
            release.set()
        late_failure, late_trace = late.result(timeout=2)
    assert error_diagnostic(quota_failure)[0] == reason
    assert reason.replace('_', ' ') in quota_trace
    assert 'temporary failure' in late_trace
    row = chain.source_statuses()[0]
    assert row.state == reason
    destination = tmp_path / 'public.png'
    chain.download(artwork, destination, lambda: None)
    routed.close()
    assert destination.read_bytes() == b'public cover already identified'
    assert chain.source_statuses()[0].downloads == 1


@pytest.mark.parametrize('name', ['MobyGames', 'ScreenScraper'])
def test_preparation_only_source_check_never_claims_authentication(name):
    class CachedPreparation(Source):
        check_connection = None

    chain = FallbackProvider([CachedPreparation(name)])
    chain.check_sources(lambda: None)
    row = chain.source_statuses()[0]
    assert row.state == 'prepared'
    assert 'cached' in row.detail.lower()
    assert 'no account authentication is implied' in row.detail.lower()
    assert row.requests == row.api_requests == row.lookups == 0


def test_authentication_suspension_still_blocks_an_already_routed_cover(tmp_path):
    class AuthSource(Source):
        def find(self, game, checkpoint):
            if game == 'unauthorized':
                raise AuthenticationError('Rejected account access.')
            return self.result

        def download(self, *_args):
            raise AssertionError('suspended authentication must block the routed request')

    chain = FallbackProvider([AuthSource('IGDB', Artwork('https://example.test/c.png', 'IGDB'))])
    routed = chain.candidates('cover', lambda: None)
    artwork = next(routed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def suspend():
            with pytest.raises(ProviderError):
                list(chain.candidates('unauthorized', lambda: None))
        pool.submit(suspend).result(timeout=2)
    with pytest.raises(ProviderError) as error:
        chain.download(artwork, tmp_path / 'cover.png', lambda: None)
    assert error_diagnostic(error.value)[0] == 'authentication'
    routed.close()


def test_engine_persists_worker_source_trace_and_exports_it_without_credentials(tmp_path):
    from novacore_scraper.engine import ScrapeEngine
    from novacore_scraper.state import State

    directory = tmp_path / 'roms' / 'nes'
    directory.mkdir(parents=True)
    path = directory / 'No Cover.nes'
    path.write_bytes(b'ROM preserved')
    game = Game(System('nes', 'NES', directory, frozenset({'.nes'})), path, path.name, 'No Cover')
    chain = FallbackProvider([Source('Libretro'), Source('LaunchBox'),
                              Source('TheGamesDB', error=QuotaError('quota https://example.test/?apikey=private-token'))])
    engine = ScrapeEngine(tmp_path, chain, process_running=lambda: True, retry_passes=0, keep_awake=False)
    engine.start([game])
    assert engine.wait(5)
    job = engine.jobs_page()[0][0]
    assert job.status == 'failed'
    assert job.source_trace == 'Libretro: no title match; LaunchBox: no title match; TheGamesDB: quota exhausted'
    with State(tmp_path) as state:
        assert state.previous(game)['source_trace'] == job.source_trace
    report = tmp_path / 'source-report.csv'
    assert State.export_report(tmp_path, report) == 1
    with report.open(encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
    assert row['source_trace'] == job.source_trace
    assert 'private-token' not in report.read_text(encoding='utf-8-sig')
    assert path.read_bytes() == b'ROM preserved'


def test_engine_persists_complete_attempts_in_history_and_export(tmp_path):
    import csv
    from PIL import Image
    from novacore_scraper.engine import ScrapeEngine
    from novacore_scraper.models import Game, System
    from novacore_scraper.state import State

    folder = tmp_path/'roms'/'gba'
    folder.mkdir(parents=True)
    system = System('gba', 'Game Boy Advance', folder, frozenset({'.gba'}))
    games = []
    for title in ('good', 'unresolved'):
        path = folder/(title+'.gba')
        path.write_bytes(b'ROM unchanged')
        games.append(Game(system, path, path.name, title))
    class Selective(Source):
        def find(self, game, checkpoint):
            return self.result if game.title == 'good' else None
        def download(self, artwork, destination, checkpoint):
            Image.new('RGB', (2, 2), 'blue').save(destination)
    chain = FallbackProvider([Source('Libretro'), Source('TheGamesDB', error=QuotaError('quota')),
                             Selective('IGDB', Artwork('https://example.test/c.png', 'IGDB'))])
    engine = ScrapeEngine(tmp_path, chain, process_running=lambda: True, retry_passes=0, keep_awake=False)
    engine.start(games)
    assert engine.wait(5)
    assert (engine.snapshot().completed, engine.snapshot().failed, engine.snapshot().pending) == (1, 1, 1)
    jobs = {job.title: job for job in engine.jobs_page()[0]}
    assert 'IGDB: downloaded' in jobs['good'].source_trace
    assert 'Libretro: no title match' in jobs['unresolved'].source_trace
    assert 'IGDB: no title match' in jobs['unresolved'].source_trace
    assert engine.job_snapshot(games[0].key) == jobs['good']
    assert engine.job_snapshot('not a submitted game') is None
    history, total = State.downloaded_page(tmp_path)
    assert total == 1 and history[0]['source_trace'] == jobs['good'].source_trace
    assert engine.downloaded_page()[0][0].source_trace == jobs['good'].source_trace
    destination = tmp_path/'report.csv'
    assert State.export_report(tmp_path, destination) == 2
    with destination.open(encoding='utf-8-sig', newline='') as stream:
        report = {row['title']: row for row in csv.DictReader(stream)}
    assert report['unresolved']['source_trace'] == jobs['unresolved'].source_trace
    assert report['good']['source_trace'] == jobs['good'].source_trace
    assert all(game.path.read_bytes() == b'ROM unchanged' for game in games)
    repeat = ScrapeEngine(tmp_path, chain, process_running=lambda: True, retry_passes=0, keep_awake=False)
    repeat.start([games[0]])
    assert repeat.wait(5)
    assert repeat.snapshot().skipped == 1
    assert repeat.jobs_page()[0][0].source_trace == jobs['good'].source_trace
