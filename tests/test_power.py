from unittest.mock import Mock, patch
import pytest


def test_windows_awake_request_clears_on_exception_and_never_keeps_display_on():
    from novacore_scraper.power import keep_system_awake
    setter = Mock(return_value=0x80000000)
    with patch('novacore_scraper.power.sys.platform', 'win32'), \
            patch('novacore_scraper.power._set_thread_execution_state', setter):
        with pytest.raises(ValueError):
            with keep_system_awake():
                raise ValueError('test failure')
    assert [call.args[0] for call in setter.call_args_list] == [0x80000001, 0x80000000]


def test_disabled_and_non_windows_do_not_change_power_settings():
    from novacore_scraper.power import keep_system_awake
    with patch('novacore_scraper.power._set_thread_execution_state') as setter:
        with keep_system_awake(False):
            pass
        with patch('novacore_scraper.power.sys.platform', 'linux'):
            with keep_system_awake(True):
                pass
    setter.assert_not_called()
