"""Optional Windows-account DPAPI storage; application keys are never borrowed."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import tempfile

from .settings import data_directory
from .state import is_link

LOG = logging.getLogger(__name__)
ENVIRONMENT = {
    'username': 'NOVACORE_SS_USERNAME', 'password': 'NOVACORE_SS_PASSWORD',
    'tgdb_api_key': 'NOVACORE_TGDB_API_KEY',
    'igdb_client_id': 'NOVACORE_IGDB_CLIENT_ID', 'igdb_client_secret': 'NOVACORE_IGDB_CLIENT_SECRET',
    'mobygames_api_key': 'NOVACORE_MOBYGAMES_API_KEY',
}
_MAGIC = b'NovaCoreCredentials\x00\x01'
_MAX_BYTES = 128 * 1024


class CredentialError(RuntimeError):
    pass


def can_remember_credentials() -> bool:
    return os.name == 'nt'


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw: bytes, decrypt: bool) -> bytes:
    if not can_remember_credentials():
        raise CredentialError('Remembering credentials requires this Windows account. Values still work for this session.')
    if not 0 < len(raw) <= _MAX_BYTES:
        raise CredentialError('The credential file has an unsupported size.')
    buffer = ctypes.create_string_buffer(raw)
    source = _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    second = ctypes.POINTER(wintypes.LPWSTR) if decrypt else wintypes.LPCWSTR
    function.argtypes = [ctypes.POINTER(_Blob), second, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None if decrypt else 'NovaCore artwork provider credentials',
                    None, None, None, 1, ctypes.byref(destination)):
        raise CredentialError('Windows could not read or protect credentials for this account. Enter them again to use this session.')
    try:
        if destination.size > _MAX_BYTES:
            raise CredentialError('Windows returned an unsupported credential size.')
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(ctypes.cast(destination.data, ctypes.c_void_p))


def _protect(raw: bytes) -> bytes:
    return _crypt(raw, False)


def _unprotect(raw: bytes) -> bytes:
    return _crypt(raw, True)


def _path() -> Path:
    path = data_directory()/'credentials.dat'
    if is_link(path) or is_link(path.parent):
        raise CredentialError('Credential storage cannot use a linked file or directory.')
    return path


def _values(values) -> dict[str, str]:
    if not isinstance(values, dict):
        raise CredentialError('The credential file is invalid.')
    return {key: value for key in ENVIRONMENT if isinstance((value := values.get(key, '')), str)
            and len(value) <= 8192}


def load_credentials() -> dict[str, str]:
    if not can_remember_credentials():
        return {}
    try:
        path = _path()
        if not path.exists():
            return {}
        if path.stat().st_size > _MAX_BYTES:
            raise CredentialError('The credential file has an unsupported size.')
        raw = path.read_bytes()
        if not raw.startswith(_MAGIC):
            raise CredentialError('The credential file is invalid.')
        return _values(json.loads(_unprotect(raw[len(_MAGIC):]).decode('utf-8')))
    except (OSError, ValueError, CredentialError) as exc:
        LOG.warning('Saved provider credentials unavailable (%s); current-session entry is still available.', type(exc).__name__)
        return {}


def configured_credentials() -> dict[str, str]:
    values = {key: '' for key in ENVIRONMENT}
    values.update(load_credentials())
    for key, variable in ENVIRONMENT.items():
        value = os.environ.get(variable, '')
        if value.strip():
            values[key] = value
    return values


def save_credentials(values: dict[str, str]) -> None:
    if not can_remember_credentials():
        raise CredentialError('Remembering credentials requires Windows. Values still work for this session.')
    protected = _MAGIC + _protect(json.dumps(_values(values), ensure_ascii=False).encode('utf-8'))
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(protected)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_credentials() -> None:
    _path().unlink(missing_ok=True)
