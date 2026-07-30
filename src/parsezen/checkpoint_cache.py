"""Small safe primitives for measuring and bounding opaque checkpoint caches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from re import Pattern


@dataclass(frozen=True, slots=True)
class CheckpointCacheStats:
    bytes_used: int = 0
    jobs: int = 0

    def __add__(self, other: object) -> CheckpointCacheStats:
        if not isinstance(other, CheckpointCacheStats):
            return NotImplemented
        return CheckpointCacheStats(
            self.bytes_used + other.bytes_used,
            self.jobs + other.jobs,
        )


@dataclass(frozen=True, slots=True)
class CheckpointPruneResult:
    bytes_freed: int = 0
    jobs_removed: int = 0


@dataclass(frozen=True, slots=True)
class _CacheDirectory:
    path: Path
    bytes_used: int
    modified_at: datetime


def checkpoint_cache_stats(root: Path, key_pattern: Pattern[str]) -> CheckpointCacheStats:
    records = _cache_directories(root, key_pattern)
    return CheckpointCacheStats(
        bytes_used=sum(record.bytes_used for record in records),
        jobs=len(records),
    )


def prune_checkpoint_cache(
    root: Path,
    key_pattern: Pattern[str],
    clear_directory: Callable[[Path], None],
    *,
    max_bytes: int = 1024 * 1024 * 1024,
    max_age_days: int = 30,
    now: datetime | None = None,
) -> CheckpointPruneResult:
    """Remove stale jobs, then old inactive jobs until the soft size limit is met."""
    if max_bytes < 0 or max_age_days < 1:
        raise ValueError("Los límites de caché no son válidos.")
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    records = sorted(
        _cache_directories(root, key_pattern),
        key=lambda record: record.modified_at,
    )
    remaining_bytes = sum(record.bytes_used for record in records)
    stale_before = current_time - timedelta(days=max_age_days)
    inactive_before = current_time - timedelta(days=1)
    removed: set[Path] = set()
    bytes_freed = 0

    def remove(record: _CacheDirectory) -> None:
        nonlocal remaining_bytes, bytes_freed
        clear_directory(record.path)
        if record.path.exists():
            return
        removed.add(record.path)
        remaining_bytes -= record.bytes_used
        bytes_freed += record.bytes_used

    for record in records:
        if record.modified_at <= stale_before:
            remove(record)
    for record in records:
        if remaining_bytes <= max_bytes:
            break
        if record.path in removed or record.modified_at > inactive_before:
            continue
        remove(record)
    try:
        root.rmdir()
    except OSError:
        pass
    return CheckpointPruneResult(bytes_freed, len(removed))


def _cache_directories(root: Path, key_pattern: Pattern[str]) -> tuple[_CacheDirectory, ...]:
    try:
        candidates = tuple(root.iterdir())
    except (FileNotFoundError, OSError):
        return ()
    records: list[_CacheDirectory] = []
    for directory in candidates:
        try:
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or key_pattern.fullmatch(directory.name) is None
            ):
                continue
            modified_timestamp = directory.stat().st_mtime
            bytes_used = 0
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                bytes_used += stat.st_size
                modified_timestamp = max(modified_timestamp, stat.st_mtime)
        except OSError:
            continue
        records.append(
            _CacheDirectory(
                directory,
                bytes_used,
                datetime.fromtimestamp(modified_timestamp, tz=UTC),
            )
        )
    return tuple(records)
