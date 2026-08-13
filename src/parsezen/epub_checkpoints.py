"""Private, local checkpoints for resumable EPUB translations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from base64 import b64decode, b64encode
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path

from parsezen import APP_STORAGE_NAME
from parsezen.checkpoint_cache import (
    CheckpointCacheStats,
    CheckpointPruneResult,
    checkpoint_cache_stats,
    prune_checkpoint_cache,
)
from parsezen.domain.source_identity import sha256_file
from parsezen.infrastructure.protected_file import atomic_write_bytes
from parsezen.infrastructure.user_data_protection import (
    protect_for_current_user as _protect_for_current_user,
)
from parsezen.infrastructure.user_data_protection import (
    unprotect_for_current_user as _unprotect_for_current_user,
)

LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 2
_IMPLEMENTATION_REVISION = "epub-semantic-parts-v2-encrypted"
_PART_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_TRANSLATED_PAYLOAD_BYTES = 2 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 3 * 1024 * 1024


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
        new_cache_directory = not self.directory.exists()
        try:
            payload = {
                "schema_version": _SCHEMA_VERSION,
                "part_key": part_key,
                "protected_payload": b64encode(_protect_for_current_user(encoded_payload)).decode(
                    "ascii"
                ),
            }
            atomic_write_bytes(
                self.directory / f"{part_key}.json",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                temporary_prefix=".part-",
            )
            if new_cache_directory:
                prune_epub_translation_cache(root=self.directory.parent)
            return True
        except (OSError, TypeError, UnicodeError):
            LOGGER.warning("epub_checkpoint_save_failed")
            return False

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
    source_digest: str | None = None,
) -> EpubTranslationCheckpoints:
    """Create an isolated checkpoint session for one source and translation setup."""

    effective_source_digest = source_digest or sha256_file(source_path)
    identity = "\n".join(
        (
            str(_SCHEMA_VERSION),
            _IMPLEMENTATION_REVISION,
            effective_source_digest,
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


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
