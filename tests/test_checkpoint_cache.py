from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parsezen.checkpoint_cache import (
    CheckpointCacheStats,
    checkpoint_cache_stats,
    prune_checkpoint_cache,
)

KEY_PATTERN = re.compile(r"[0-9a-f]{64}")


def _cache_job(root: Path, character: str, size: int, modified: datetime) -> Path:
    directory = root / (character * 64)
    directory.mkdir(parents=True)
    payload = directory / ("f" * 64 + ".json")
    payload.write_bytes(b"x" * size)
    timestamp = modified.timestamp()
    os.utime(payload, (timestamp, timestamp))
    os.utime(directory, (timestamp, timestamp))
    return directory


def _clear_directory(directory: Path) -> None:
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()


def test_cache_stats_only_count_owned_opaque_directories(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _cache_job(tmp_path, "a", 12, now)
    unexpected = tmp_path / "do-not-touch"
    unexpected.mkdir()
    (unexpected / "payload").write_bytes(b"private")

    stats = checkpoint_cache_stats(tmp_path, KEY_PATTERN)

    assert stats == CheckpointCacheStats(bytes_used=12, jobs=1)
    assert (unexpected / "payload").exists()
    assert stats + CheckpointCacheStats(8, 2) == CheckpointCacheStats(20, 3)
    assert stats.__add__(12) is NotImplemented


def test_cache_stats_tolerate_an_unavailable_root() -> None:
    class UnavailableRoot:
        def iterdir(self) -> tuple[()]:
            raise OSError("unavailable")

    assert checkpoint_cache_stats(UnavailableRoot(), KEY_PATTERN) == CheckpointCacheStats()  # type: ignore[arg-type]


def test_prune_removes_stale_jobs_but_keeps_recent_and_unknown_data(tmp_path: Path) -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    stale = _cache_job(tmp_path, "a", 30, now - timedelta(days=31))
    recent = _cache_job(tmp_path, "b", 40, now - timedelta(hours=2))
    unknown = tmp_path / "manual"
    unknown.mkdir()

    result = prune_checkpoint_cache(
        tmp_path,
        KEY_PATTERN,
        _clear_directory,
        max_bytes=1_000,
        max_age_days=30,
        now=now,
    )

    assert result.bytes_freed == 30
    assert result.jobs_removed == 1
    assert not stale.exists()
    assert recent.exists()
    assert unknown.exists()


def test_size_pruning_never_removes_a_job_modified_during_the_last_day(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    old = _cache_job(tmp_path, "a", 80, now - timedelta(days=2))
    recent = _cache_job(tmp_path, "b", 80, now - timedelta(hours=1))

    result = prune_checkpoint_cache(
        tmp_path,
        KEY_PATTERN,
        _clear_directory,
        max_bytes=100,
        max_age_days=30,
        now=now,
    )

    assert result.jobs_removed == 1
    assert not old.exists()
    assert recent.exists()


@pytest.mark.parametrize(
    ("max_bytes", "max_age_days"),
    [(-1, 30), (1, 0)],
)
def test_prune_rejects_invalid_limits(
    tmp_path: Path,
    max_bytes: int,
    max_age_days: int,
) -> None:
    with pytest.raises(ValueError, match="límites de caché"):
        prune_checkpoint_cache(
            tmp_path,
            KEY_PATTERN,
            _clear_directory,
            max_bytes=max_bytes,
            max_age_days=max_age_days,
        )


def test_prune_only_counts_directories_that_were_actually_removed(tmp_path: Path) -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    stale = _cache_job(tmp_path, "a", 30, now - timedelta(days=31))

    result = prune_checkpoint_cache(
        tmp_path,
        KEY_PATTERN,
        lambda _directory: None,
        max_bytes=0,
        max_age_days=30,
        now=now,
    )

    assert result.bytes_freed == 0
    assert result.jobs_removed == 0
    assert stale.exists()
