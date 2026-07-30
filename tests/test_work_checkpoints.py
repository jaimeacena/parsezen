import json
import os
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import parsezen.work_checkpoints as checkpoints_module
from parsezen.work_checkpoints import (
    WorkCheckpoints,
    checkpoint_key,
    clear_work_checkpoint_cache,
    open_work_checkpoints,
    prune_work_checkpoint_cache,
    work_checkpoint_cache_stats,
)


def _disable_platform_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoints_module, "_protect_for_current_user", lambda value: value)
    monkeypatch.setattr(checkpoints_module, "_unprotect_for_current_user", lambda value: value)


def test_work_checkpoints_are_atomic_private_and_bound_to_exact_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-first")
    root = tmp_path / "cache"
    key = checkpoint_key("ocr-page", "7")

    checkpoints = open_work_checkpoints(source, "same-options", root=root)
    assert checkpoints.save(key, "Texto reconocido")
    assert checkpoints.load(key) == "Texto reconocido"
    assert not any(path.suffix == ".tmp" for path in checkpoints.directory.iterdir())

    other_options = open_work_checkpoints(source, "different-options", root=root)
    assert other_options.directory != checkpoints.directory
    assert other_options.load(key) is None

    source.write_bytes(b"%PDF-second")
    changed_source = open_work_checkpoints(source, "same-options", root=root)
    assert changed_source.directory != checkpoints.directory
    assert changed_source.load(key) is None


def test_work_checkpoints_ignore_damage_and_clear_only_owned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    source = tmp_path / "book.md"
    source.write_text("Private content", encoding="utf-8")
    checkpoints = open_work_checkpoints(source, "clean", root=tmp_path / "cache")
    key = checkpoint_key("chunk", "1")
    assert checkpoints.save(key, "Safe result")
    (checkpoints.directory / f"{key}.json").write_text("not-json", encoding="utf-8")

    assert checkpoints.load(key) is None
    checkpoints.clear()
    assert not checkpoints.directory.exists()


def test_general_checkpoint_cache_clears_owned_jobs_but_not_foreign_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    root = tmp_path / "cache"
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-local")
    key = checkpoint_key("page", "1")
    for options in ("first", "second"):
        checkpoints = open_work_checkpoints(source, options, root=root)
        assert checkpoints.save(key, "Recovered")
    foreign = root / "foreign-data"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("Keep", encoding="utf-8")

    assert clear_work_checkpoint_cache(root=root) == 2
    assert foreign.is_dir()


def test_work_checkpoint_rejects_invalid_keys_and_failed_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.md"
    source.write_text("Private", encoding="utf-8")
    checkpoints = open_work_checkpoints(source, "options", root=tmp_path / "cache")

    assert checkpoints.load("not-a-key") is None
    assert not checkpoints.save("not-a-key", "Result")
    monkeypatch.setattr(
        checkpoints_module,
        "_protect_for_current_user",
        lambda _value: (_ for _ in ()).throw(OSError("blocked")),
    )
    assert not checkpoints.save(checkpoint_key("chunk", "1"), "Result")
    assert not any(path.suffix == ".tmp" for path in checkpoints.directory.glob("*"))


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"schema_version": 1},
        {"schema_version": 2, "key": "{key}", "protected_payload": "eA=="},
        {"schema_version": 1, "key": "different", "protected_payload": "eA=="},
        {"schema_version": 1, "key": "{key}", "protected_payload": "not-base64"},
    ],
)
def test_work_checkpoint_load_rejects_untrusted_record_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: object,
) -> None:
    _disable_platform_encryption(monkeypatch)
    key = checkpoint_key("chunk", "shape")
    directory = tmp_path / ("a" * 64)
    directory.mkdir()
    serialized = json.dumps(record).replace("{key}", key)
    (directory / f"{key}.json").write_text(serialized, encoding="utf-8")

    assert WorkCheckpoints(directory).load(key) is None


def test_work_checkpoint_rejects_empty_and_oversized_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    monkeypatch.setattr(checkpoints_module, "_MAX_PAYLOAD_BYTES", 2)
    key = checkpoint_key("chunk", "bounds")
    checkpoints = WorkCheckpoints(tmp_path / ("b" * 64))

    assert not checkpoints.save(key, "  ")
    assert not checkpoints.save(key, "abc")
    checkpoints.directory.mkdir()
    record = {
        "schema_version": 1,
        "key": key,
        "protected_payload": b64encode(b"abc").decode("ascii"),
    }
    (checkpoints.directory / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
    assert checkpoints.load(key) is None


def test_failed_atomic_replace_removes_its_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    checkpoints = WorkCheckpoints(tmp_path / ("c" * 64))
    monkeypatch.setattr(
        checkpoints_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert not checkpoints.save(checkpoint_key("chunk", "atomic"), "Result")
    assert not tuple(checkpoints.directory.glob(".work-*.tmp"))


def test_clear_ignores_unowned_and_unexpected_files(tmp_path: Path) -> None:
    foreign = tmp_path / "human-name"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    WorkCheckpoints(foreign).clear()
    assert (foreign / "keep.txt").exists()

    owned = tmp_path / ("d" * 64)
    owned.mkdir()
    (owned / "keep.txt").write_text("keep", encoding="utf-8")
    WorkCheckpoints(owned).clear()
    assert (owned / "keep.txt").exists()

    WorkCheckpoints(tmp_path / ("e" * 64)).clear()


def test_work_cache_stats_and_pruning_share_the_safe_cache_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    source = tmp_path / "document.md"
    source.write_text("Private", encoding="utf-8")
    root = tmp_path / "cache"
    checkpoints = open_work_checkpoints(source, "options", root=root)
    assert checkpoints.save(checkpoint_key("chunk", "1"), "Recovered")
    assert work_checkpoint_cache_stats(root=root).jobs == 1
    old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    for path in checkpoints.directory.iterdir():
        os.utime(path, (old, old))
    os.utime(checkpoints.directory, (old, old))

    result = prune_work_checkpoint_cache(root=root, max_age_days=30)

    assert result.jobs_removed == 1
    assert not checkpoints.directory.exists()
