import os
from pathlib import Path
from unittest.mock import patch

import pytest

from novacore_scraper import credentials


def test_environment_overrides_saved_secrets_without_including_application_pair():
    with patch.object(credentials, 'load_credentials', return_value={'tgdb_api_key': 'saved', 'password': 'saved password'}), \
         patch.dict(os.environ, {'NOVACORE_TGDB_API_KEY': 'environment', 'NOVACORE_SS_DEVID': 'app credential'}, clear=True):
        values = credentials.configured_credentials()
    assert values['tgdb_api_key'] == 'environment'
    assert values['password'] == 'saved password'
    assert 'devid' not in values
    assert set(values) == set(credentials.ENVIRONMENT)


def test_saved_credentials_are_protected_and_atomic_failure_keeps_old_values(tmp_path):
    with patch.object(credentials, 'data_directory', return_value=tmp_path), \
         patch.object(credentials, 'can_remember_credentials', return_value=True), \
         patch.object(credentials, '_protect', side_effect=lambda raw: raw[::-1]), \
         patch.object(credentials, '_unprotect', side_effect=lambda raw: raw[::-1]):
        credentials.save_credentials({'tgdb_api_key': 'old secret', 'devid': 'never stored'})
        before = (tmp_path/'credentials.dat').read_bytes()
        assert b'old secret' not in before and b'never stored' not in before
        assert credentials.load_credentials()['tgdb_api_key'] == 'old secret'
        with patch.object(credentials, '_protect', side_effect=credentials.CredentialError('Encryption failed.')):
            with pytest.raises(credentials.CredentialError):
                credentials.save_credentials({'tgdb_api_key': 'replacement'})
        assert (tmp_path/'credentials.dat').read_bytes() == before
        assert not list(tmp_path.glob('*.tmp'))
        credentials.clear_credentials()
        assert not (tmp_path/'credentials.dat').exists()


def test_unreadable_credentials_are_empty_and_do_not_reveal_payload(tmp_path):
    (tmp_path/'credentials.dat').write_bytes(b'bad file secret')
    with patch.object(credentials, 'data_directory', return_value=tmp_path), \
         patch.object(credentials, 'can_remember_credentials', return_value=True):
        assert credentials.load_credentials() == {}


@pytest.mark.skipif(os.name == 'nt', reason='Non-Windows behavior')
def test_non_windows_credentials_stay_in_session(tmp_path):
    with patch.object(credentials, 'data_directory', return_value=tmp_path):
        assert not credentials.can_remember_credentials()
        with pytest.raises(credentials.CredentialError):
            credentials.save_credentials({'password': 'fixture'})
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows DPAPI check')
def test_native_dpapi_roundtrip_is_not_plaintext():
    raw = b'NovaCore fixture secret, not an account'
    protected = credentials._protect(raw)
    assert raw not in protected
    assert credentials._unprotect(protected) == raw
