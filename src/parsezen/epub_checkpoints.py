"""Private, local checkpoints for resumable EPUB translations."""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
from base64 import b64decode, b64encode
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
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkstemp
from typing import Any

from platformdirs import user_cache_path

from parsezen import APP_STORAGE_NAME
from parsezen.checkpoint_cache import (
    CheckpointCacheStats,
    CheckpointPruneResult,
    checkpoint_cache_stats,
    prune_checkpoint_cache,
)

LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 2
_IMPLEMENTATION_REVISION = "epub-semantic-parts-v2-encrypted"
_PART_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_TRANSLATED_PAYLOAD_BYTES = 2 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 3 * 1024 * 1024
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(Structure):
    _fields_ = [("cbData", DWORD), ("pbData", POINTER(c_byte))]


@dataclass(frozen=True, slots=True)
class EpubTranslationCheckpoints:
    """Read and write completed translation parts for one exact job."""

    directory: Path

    def load(self, part_key: str) -> str | None:
        """Return a verified part, ignoring absent or damaged checkpoint data."""

        if not _PART_KEY_PATTERN.fullmatch(part_key):
            return None
        path = self.directory / f"{part_key}.json"
        try:
            if path.is_symlink():
                return None
            if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "part_key",
            "protected_payload",
        }:
            return None
        protected_payload = raw.get("protected_payload")
        if (
            raw.get("schema_version") != _SCHEMA_VERSION
            or raw.get("part_key") != part_key
            or not isinstance(protected_payload, str)
            or not protected_payload
        ):
            return None
        try:
            payload = _unprotect_for_current_user(
                b64decode(protected_payload, validate=True)
            ).decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            return None
        if not payload.strip() or len(payload.encode("utf-8")) > _MAX_TRANSLATED_PAYLOAD_BYTES:
            return None
        return payload

    def save(self, part_key: str, translated_payload: str) -> bool:
        """Atomically persist one verified part without exposing document names."""

        encoded_payload = translated_payload.encode("utf-8")
        if (
            not _PART_KEY_PATTERN.fullmatch(part_key)
            or not translated_payload.strip()
            or len(encoded_payload) > _MAX_TRANSLATED_PAYLOAD_BYTES
        ):
            return False
        temporary_path: Path | None = None
        new_cache_directory = not self.directory.exists()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = mkstemp(
                dir=self.directory,
                prefix=".part-",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(temporary_name)
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "part_key": part_key,
                "protected_payload": b64encode(_protect_for_current_user(encoded_payload)).decode(
                    "ascii"
                ),
            }
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.directory / f"{part_key}.json")
            temporary_path = None
            if new_cache_directory:
                prune_epub_translation_cache(root=self.directory.parent)
            return True
        except (OSError, TypeError, UnicodeError):
            LOGGER.warning("epub_checkpoint_save_failed")
            return False
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        """Remove this completed job's owned files, leaving unexpected data alone."""

        if self.directory.is_symlink() or not _PART_KEY_PATTERN.fullmatch(self.directory.name):
            return
        try:
            for path in self.directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if _PART_KEY_PATTERN.fullmatch(path.stem) and path.suffix == ".json":
                    path.unlink(missing_ok=True)
                elif path.name.startswith(".part-") and path.suffix == ".tmp":
                    path.unlink(missing_ok=True)
            self.directory.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning("epub_checkpoint_cleanup_failed")


def open_epub_translation_checkpoints(
    source_path: Path,
    resume_key: str,
    *,
    root: Path | None = None,
) -> EpubTranslationCheckpoints:
    """Create an isolated checkpoint session for one source and translation setup."""

    source_digest = _sha256_file(source_path)
    identity = "\n".join(
        (
            str(_SCHEMA_VERSION),
            _IMPLEMENTATION_REVISION,
            source_digest,
            resume_key,
        )
    )
    job_key = _sha256_text(identity)
    cache_root = (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "epub-translations"
    )
    return EpubTranslationCheckpoints(cache_root / job_key)


def clear_epub_translation_cache(*, root: Path | None = None) -> int:
    """Remove every owned EPUB translation checkpoint directory."""

    cache_root = (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "epub-translations"
    )
    try:
        directories = list(cache_root.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:
        LOGGER.warning("epub_checkpoint_cache_scan_failed")
        return 0
    removed = 0
    for directory in directories:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not _PART_KEY_PATTERN.fullmatch(directory.name)
        ):
            continue
        EpubTranslationCheckpoints(directory).clear()
        if not directory.exists():
            removed += 1
    try:
        cache_root.rmdir()
    except (FileNotFoundError, OSError):
        pass
    return removed


def epub_translation_cache_stats(*, root: Path | None = None) -> CheckpointCacheStats:
    return checkpoint_cache_stats(_cache_root(root), _PART_KEY_PATTERN)


def prune_epub_translation_cache(
    *,
    root: Path | None = None,
    max_bytes: int = 1024 * 1024 * 1024,
    max_age_days: int = 30,
) -> CheckpointPruneResult:
    cache_root = _cache_root(root)
    return prune_checkpoint_cache(
        cache_root,
        _PART_KEY_PATTERN,
        lambda directory: EpubTranslationCheckpoints(directory).clear(),
        max_bytes=max_bytes,
        max_age_days=max_age_days,
    )


def _cache_root(root: Path | None) -> Path:
    return (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "epub-translations"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _protect_for_current_user(payload: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("El sistema no ofrece protección de caché compatible.")
    input_buffer = create_string_buffer(payload)
    input_blob = _DataBlob(
        len(payload),
        cast(input_buffer, POINTER(c_byte)),
    )
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


def _unprotect_for_current_user(payload: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("El sistema no ofrece protección de caché compatible.")
    input_buffer = create_string_buffer(payload)
    input_blob = _DataBlob(
        len(payload),
        cast(input_buffer, POINTER(c_byte)),
    )
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
