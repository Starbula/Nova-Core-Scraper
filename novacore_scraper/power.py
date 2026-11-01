"""Keep the coordinator active while scraping; never force the display on.

https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
"""
from contextlib import contextmanager
import ctypes
import logging
import sys

LOG = logging.getLogger(__name__)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_thread_execution_state(flags):
    setter = ctypes.windll.kernel32.SetThreadExecutionState
    setter.argtypes = [ctypes.c_uint]
    setter.restype = ctypes.c_uint
    return setter(flags)


@contextmanager
def keep_system_awake(enabled=True):
    """Request wakefulness on this thread and always restore normal sleep."""
    requested = enabled and sys.platform == 'win32'
    if requested:
        try:
            if not _set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                LOG.warning('Windows could not prevent automatic sleep during scraping.')
        except (AttributeError, OSError):
            requested = False
            LOG.warning('Windows power management was unavailable during scraping.')
    try:
        yield
    finally:
        if requested:
            try:
                _set_thread_execution_state(ES_CONTINUOUS)
            except (AttributeError, OSError):
                LOG.warning('Windows could not restore the normal sleep request.')
