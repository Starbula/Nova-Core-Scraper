"""Local application-auth readiness never probes accounts or reveals credentials."""
from __future__ import annotations

import importlib
import os
import socket
from pathlib import Path

import pytest


def auth_module():
    assert importlib.util.find_spec('novacore_scraper.auth') is not None, 'application auth helper is missing'
    return importlib.import_module('novacore_scraper.auth')


@pytest.mark.parametrize('devid, password', [
    (None, None), ('app-id', None), (None, 'app-secret'),
    ('', 'app-secret'), ('   ', 'app-secret'), ('app-id', '\t  '),
])
def test_incomplete_or_blank_application_pair_is_unavailable(monkeypatch, devid, password):
    for name, value in [('NOVACORE_SS_DEVID', devid), ('NOVACORE_SS_DEVPASSWORD', password)]:
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert auth_module().screen_scraper_app_credentials() is None


def test_complete_pair_is_read_fresh_and_preserves_credential_values(monkeypatch):
    monkeypatch.setenv('NOVACORE_SS_DEVID', 'my-authorized-application')
    monkeypatch.setenv('NOVACORE_SS_DEVPASSWORD', ' exact secret with spaces ')
    auth = auth_module()
    assert auth.screen_scraper_app_credentials() == ('my-authorized-application', ' exact secret with spaces ')
    monkeypatch.delenv('NOVACORE_SS_DEVPASSWORD')
    assert auth.screen_scraper_app_credentials() is None


def test_unavailable_status_explains_novacore_registration_and_working_alternative(monkeypatch):
    monkeypatch.delenv('NOVACORE_SS_DEVID', raising=False)
    monkeypatch.delenv('NOVACORE_SS_DEVPASSWORD', raising=False)
    status = auth_module().screen_scraper_status().casefold()
    assert 'unavailable' in status
    assert 'novacore' in status and 'application' in status and 'registration' in status
    assert 'libretro' in status and 'without an account' in status
    assert 'enter developer' not in status


def test_configured_status_is_not_a_claim_of_verified_account_access(monkeypatch):
    monkeypatch.setenv('NOVACORE_SS_DEVID', 'sensitive-application-identity')
    monkeypatch.setenv('NOVACORE_SS_DEVPASSWORD', 'sensitive-application-secret')
    status = auth_module().screen_scraper_status()
    assert 'configured' in status.casefold()
    assert 'username and password' in status.casefold()
    assert 'verif' in status.casefold()
    assert 'sensitive-application-identity' not in status
    assert 'sensitive-application-secret' not in status


def test_partial_pair_never_appears_in_status_or_output(monkeypatch, capsys):
    monkeypatch.setenv('NOVACORE_SS_DEVID', 'private-partial-value')
    monkeypatch.delenv('NOVACORE_SS_DEVPASSWORD', raising=False)
    auth = auth_module()
    status = auth.screen_scraper_status()
    assert 'private-partial-value' not in status
    assert auth.screen_scraper_app_credentials() is None
    output = capsys.readouterr()
    assert output.out == '' and output.err == ''


def test_readiness_does_not_use_files_network_or_change_environment(monkeypatch):
    monkeypatch.setenv('NOVACORE_SS_DEVID', 'my-app-id')
    monkeypatch.setenv('NOVACORE_SS_DEVPASSWORD', 'my-app-secret')
    auth = auth_module()
    before = dict(os.environ)
    def forbidden(*args, **kwargs):
        pytest.fail('local readiness attempted file or network I/O')
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(Path, 'read_text', forbidden)
    monkeypatch.setattr(Path, 'write_text', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    assert auth.screen_scraper_app_credentials() == ('my-app-id', 'my-app-secret')
    assert auth.screen_scraper_status()
    unchanged = dict(os.environ) == before
    assert unchanged, 'readiness changed the process environment'
