"""Private encrypted checkpoints for expensive, resumable document work."""

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

_SCHEMA_VERSION = 1
_IMPLEMENTATION_REVISION = "expensive-work-v2"
_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkCheckpoints:
    """Load and save encrypted text for one exact source and configuration."""

    directory: Path

    def load(self, key: str) -> str | None:
        """Return one verified payload, ignoring absent or damaged cache data."""
        if not _KEY_PATTERN.fullmatch(key):
            return None
        path = self.directory / f"{key}.json"
        try:
            if path.is_symlink() or path.stat().st_size > _MAX_CHECKPOINT_BYTES:
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "key",
            "protected_payload",
        }:
            return None
        protected_payload = raw.get("protected_payload")
        if (
            raw.get("schema_version") != _SCHEMA_VERSION
            or raw.get("key") != key
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
        if not payload.strip() or len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            return None
        return payload

    def save(self, key: str, payload: str) -> bool:
        """Atomically save one bounded payload without exposing document content."""
        encoded = payload.encode("utf-8")
        if (
            not _KEY_PATTERN.fullmatch(key)
            or not payload.strip()
            or len(encoded) > _MAX_PAYLOAD_BYTES
        ):
            return False
        new_cache_directory = not self.directory.exists()
        try:
            record = {
                "schema_version": _SCHEMA_VERSION,
                "key": key,
                "protected_payload": b64encode(_protect_for_current_user(encoded)).decode("ascii"),
            }
            atomic_write_bytes(
                self.directory / f"{key}.json",
                json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                temporary_prefix=".work-",
            )
            if new_cache_directory:
                prune_work_checkpoint_cache(root=self.directory.parent)
            return True
        except (OSError, TypeError, UnicodeError):
            LOGGER.warning("work_checkpoint_save_failed")
            return False

    def clear(self) -> None:
        """Remove only files owned by this exact checkpoint directory."""
        if not _KEY_PATTERN.fullmatch(self.directory.name):
            return
        try:
            for path in self.directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if _KEY_PATTERN.fullmatch(path.stem) and path.suffix == ".json":
                    path.unlink(missing_ok=True)
                elif path.name.startswith(".work-") and path.suffix == ".tmp":
                    path.unlink(missing_ok=True)
            self.directory.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            LOGGER.warning("work_checkpoint_cleanup_failed")


def checkpoint_key(kind: str, identity: str) -> str:
    """Build an opaque key from a stable internal kind and exact content identity."""
    return hashlib.sha256(f"{kind}\n{identity}".encode()).hexdigest()


def open_work_checkpoints(
    source_path: Path,
    resume_key: str,
    *,
    root: Path | None = None,
    source_digest: str | None = None,
) -> WorkCheckpoints:
    """Open the cache bound to the source bytes and all processing choices."""
    effective_source_digest = source_digest or sha256_file(source_path)
    identity = "\n".join(
        (
            str(_SCHEMA_VERSION),
            _IMPLEMENTATION_REVISION,
            effective_source_digest,
            resume_key,
        )
    )
    job_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    cache_root = (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "work-checkpoints"
    )
    return WorkCheckpoints(cache_root / job_key)


def clear_work_checkpoint_cache(*, root: Path | None = None) -> int:
    """Remove all owned general-work checkpoint directories."""
    cache_root = (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "work-checkpoints"
    )
    removed = 0
    try:
        directories = tuple(cache_root.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:
        LOGGER.warning("work_checkpoint_cache_scan_failed")
        return 0
    for directory in directories:
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not _KEY_PATTERN.fullmatch(directory.name)
        ):
            continue
        before = directory.exists()
        WorkCheckpoints(directory).clear()
        if before and not directory.exists():
            removed += 1
    try:
        cache_root.rmdir()
    except OSError:
        pass
    return removed


def work_checkpoint_cache_stats(*, root: Path | None = None) -> CheckpointCacheStats:
    return checkpoint_cache_stats(_cache_root(root), _KEY_PATTERN)


def prune_work_checkpoint_cache(
    *,
    root: Path | None = None,
    max_bytes: int = 1024 * 1024 * 1024,
    max_age_days: int = 30,
) -> CheckpointPruneResult:
    cache_root = _cache_root(root)
    return prune_checkpoint_cache(
        cache_root,
        _KEY_PATTERN,
        lambda directory: WorkCheckpoints(directory).clear(),
        max_bytes=max_bytes,
        max_age_days=max_age_days,
    )


def _cache_root(root: Path | None) -> Path:
    return (
        root
        if root is not None
        else user_cache_path(APP_STORAGE_NAME, appauthor=False) / "work-checkpoints"
    )
