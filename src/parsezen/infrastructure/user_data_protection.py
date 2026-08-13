"""Windows user-scoped protection for private local payloads."""

from __future__ import annotations

import ctypes
import os
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_byte,
    c_void_p,
    cast,
    create_string_buffer,
    string_at,
)
from ctypes.wintypes import BOOL, DWORD, LPCWSTR
from typing import Any

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(Structure):
    _fields_ = [("cbData", DWORD), ("pbData", POINTER(c_byte))]


def protect_for_current_user(payload: bytes) -> bytes:
    """Protect bytes with Windows DPAPI for the current user account."""

    if os.name != "nt":
        raise OSError("El sistema no ofrece protección de caché compatible.")
    input_buffer = create_string_buffer(payload)
    input_blob = _DataBlob(len(payload), cast(input_buffer, POINTER(c_byte)))
    output_blob = _DataBlob()
    protect = ctypes.windll.crypt32.CryptProtectData
    protect.argtypes = [
        POINTER(_DataBlob),
        LPCWSTR,
        POINTER(_DataBlob),
        c_void_p,
        c_void_p,
        DWORD,
        POINTER(_DataBlob),
    ]
    protect.restype = BOOL
    success = protect(
        byref(input_blob),
        "Parsezen EPUB checkpoint",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        byref(output_blob),
    )
    if not success:
        raise OSError("No se pudo proteger la caché de traducción.")
    try:
        return string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _local_free(output_blob.pbData)


def unprotect_for_current_user(payload: bytes) -> bytes:
    """Open bytes protected by the current Windows user account."""

    if os.name != "nt":
        raise OSError("El sistema no ofrece protección de caché compatible.")
    input_buffer = create_string_buffer(payload)
    input_blob = _DataBlob(len(payload), cast(input_buffer, POINTER(c_byte)))
    output_blob = _DataBlob()
    unprotect = ctypes.windll.crypt32.CryptUnprotectData
    unprotect.argtypes = [
        POINTER(_DataBlob),
        POINTER(LPCWSTR),
        POINTER(_DataBlob),
        c_void_p,
        c_void_p,
        DWORD,
        POINTER(_DataBlob),
    ]
    unprotect.restype = BOOL
    success = unprotect(
        byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        byref(output_blob),
    )
    if not success:
        raise OSError("No se pudo abrir la caché de traducción.")
    try:
        return string_at(output_blob.pbData, output_blob.cbData)
    finally:
        _local_free(output_blob.pbData)


def _local_free(pointer: Any) -> None:
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [c_void_p]
    local_free.restype = c_void_p
    local_free(cast(pointer, c_void_p))
