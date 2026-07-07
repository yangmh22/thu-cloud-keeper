from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


DEFAULT_CREDENTIAL_TARGET = "THUCloudKeeper:TsinghuaCloudToken"
DEFAULT_CREDENTIAL_USER = "cloud.tsinghua.edu.cn"


class CredentialError(RuntimeError):
    pass


def read_token(target: str = DEFAULT_CREDENTIAL_TARGET) -> str | None:
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is only available on Windows.")
    return _read_windows_credential(target)


def write_token(token: str, target: str = DEFAULT_CREDENTIAL_TARGET, username: str = DEFAULT_CREDENTIAL_USER) -> None:
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is only available on Windows.")
    token = token.strip()
    if not token:
        raise CredentialError("Token is empty.")
    _write_windows_credential(target, username, token)


def delete_token(target: str = DEFAULT_CREDENTIAL_TARGET) -> bool:
    if os.name != "nt":
        raise CredentialError("Windows Credential Manager is only available on Windows.")
    return _delete_windows_credential(target)


if os.name == "nt":
    LPBYTE = ctypes.POINTER(ctypes.c_ubyte)
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", LPBYTE),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)

    _advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
    _advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCREDENTIALW)]
    _advapi32.CredReadW.restype = wintypes.BOOL
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _advapi32.CredWriteW.restype = wintypes.BOOL
    _advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi32.CredDeleteW.restype = wintypes.BOOL
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


def _read_windows_credential(target: str) -> str | None:
    credential_ptr = PCREDENTIALW()
    ok = _advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr))
    if not ok:
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        credential = credential_ptr.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        _advapi32.CredFree(credential_ptr)


def _write_windows_credential(target: str, username: str, token: str) -> None:
    blob = token.encode("utf-16-le")
    blob_buffer = ctypes.create_string_buffer(blob)
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.UserName = username
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(blob_buffer, LPBYTE)
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    ok = _advapi32.CredWriteW(ctypes.byref(credential), 0)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _delete_windows_credential(target: str) -> bool:
    ok = _advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
    if ok:
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise ctypes.WinError(error)
