from __future__ import annotations

import json
import os
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import parsezen.epub_checkpoints as checkpoints_module
from parsezen.epub_checkpoints import (
    EpubTranslationCheckpoints,
    clear_epub_translation_cache,
    epub_translation_cache_stats,
    open_epub_translation_checkpoints,
    prune_epub_translation_cache,
)


def _disable_platform_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoints_module, "_protect_for_current_user", lambda value: value)
    monkeypatch.setattr(checkpoints_module, "_unprotect_for_current_user", lambda value: value)


def test_epub_checkpoint_round_trip_and_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"test-epub")
    cache_root = tmp_path / "cache"
    checkpoints = open_epub_translation_checkpoints(source, "Spanish/offline", root=cache_root)
    part_key = "a" * 64

    assert checkpoints.save(part_key, "translated text") is True

    assert checkpoints.load(part_key) == "translated text"
    checkpoint_text = (checkpoints.directory / f"{part_key}.json").read_text(encoding="utf-8")
    assert "translated text" not in checkpoint_text
    assert "translated_payload" not in checkpoint_text
    assert source.name not in str(checkpoints.directory)
    checkpoints.clear()
    assert not checkpoints.directory.exists()


def test_epub_checkpoint_rejects_corrupted_content(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"test-epub")
    checkpoints = open_epub_translation_checkpoints(source, "Spanish/offline", root=tmp_path)
    part_key = "b" * 64
    assert checkpoints.save(part_key, "translated text") is True
    checkpoint_path = checkpoints.directory / f"{part_key}.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    protected = payload["protected_payload"]
    payload["protected_payload"] = f"{protected[:-4]}AAAA"
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    assert checkpoints.load(part_key) is None


def test_epub_checkpoint_reports_invalid_or_failed_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"test-epub")
    checkpoints = open_epub_translation_checkpoints(source, "Spanish/offline", root=tmp_path)

    assert checkpoints.save("invalid", "translated text") is False
    monkeypatch.setattr(
        "parsezen.epub_checkpoints.os.replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert checkpoints.save("d" * 64, "translated text") is False


def test_epub_checkpoint_identity_changes_with_source_or_translation_setup(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first.epub"
    second_source = tmp_path / "second.epub"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")

    first = open_epub_translation_checkpoints(first_source, "Spanish/offline", root=tmp_path)
    other_source = open_epub_translation_checkpoints(
        second_source,
        "Spanish/offline",
        root=tmp_path,
    )
    other_setup = open_epub_translation_checkpoints(
        first_source,
        "French/offline",
        root=tmp_path,
    )

    assert len({first.directory, other_source.directory, other_setup.directory}) == 3


def test_epub_checkpoint_cache_can_be_cleared_without_touching_unowned_data(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"test-epub")
    cache_root = tmp_path / "cache"
    checkpoints = open_epub_translation_checkpoints(source, "Spanish/offline", root=cache_root)
    checkpoints.save("c" * 64, "translated text")
    unexpected = cache_root / "keep-me"
    unexpected.mkdir(parents=True)
    (unexpected / "data.txt").write_text("keep", encoding="utf-8")

    removed = clear_epub_translation_cache(root=cache_root)

    assert removed == 1
    assert not checkpoints.directory.exists()
    assert (unexpected / "data.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"schema_version": 2},
        {"schema_version": 1, "part_key": "{key}", "protected_payload": "eA=="},
        {"schema_version": 2, "part_key": "different", "protected_payload": "eA=="},
        {"schema_version": 2, "part_key": "{key}", "protected_payload": "not-base64"},
    ],
)
def test_epub_checkpoint_load_rejects_untrusted_record_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: object,
) -> None:
    _disable_platform_encryption(monkeypatch)
    key = "a" * 64
    directory = tmp_path / ("b" * 64)
    directory.mkdir()
    serialized = json.dumps(record).replace("{key}", key)
    (directory / f"{key}.json").write_text(serialized, encoding="utf-8")

    assert EpubTranslationCheckpoints(directory).load(key) is None


def test_epub_checkpoint_rejects_empty_and_oversized_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    monkeypatch.setattr(checkpoints_module, "_MAX_TRANSLATED_PAYLOAD_BYTES", 2)
    key = "c" * 64
    checkpoints = EpubTranslationCheckpoints(tmp_path / ("d" * 64))

    assert checkpoints.load("invalid") is None
    assert not checkpoints.save(key, "  ")
    assert not checkpoints.save(key, "abc")
    checkpoints.directory.mkdir()
    record = {
        "schema_version": 2,
        "part_key": key,
        "protected_payload": b64encode(b"abc").decode("ascii"),
    }
    (checkpoints.directory / f"{key}.json").write_text(json.dumps(record), encoding="utf-8")
    assert checkpoints.load(key) is None


def test_epub_checkpoint_clear_ignores_unowned_and_unexpected_files(tmp_path: Path) -> None:
    foreign = tmp_path / "human-name"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    EpubTranslationCheckpoints(foreign).clear()
    assert (foreign / "keep.txt").exists()

    owned = tmp_path / ("e" * 64)
    owned.mkdir()
    (owned / "keep.txt").write_text("keep", encoding="utf-8")
    EpubTranslationCheckpoints(owned).clear()
    assert (owned / "keep.txt").exists()

    EpubTranslationCheckpoints(tmp_path / ("f" * 64)).clear()


def test_epub_cache_stats_and_pruning_share_the_safe_cache_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_platform_encryption(monkeypatch)
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    root = tmp_path / "cache"
    checkpoints = open_epub_translation_checkpoints(source, "Spanish/offline", root=root)
    assert checkpoints.save("a" * 64, "translated")
    assert epub_translation_cache_stats(root=root).jobs == 1
    old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    for path in checkpoints.directory.iterdir():
        os.utime(path, (old, old))
    os.utime(checkpoints.directory, (old, old))

    result = prune_epub_translation_cache(root=root, max_age_days=30)

    assert result.jobs_removed == 1
    assert not checkpoints.directory.exists()


def test_epub_cache_clear_tolerates_an_unavailable_root() -> None:
    class UnavailableRoot:
        def iterdir(self) -> tuple[()]:
            raise OSError("unavailable")

    assert clear_epub_translation_cache(root=UnavailableRoot()) == 0  # type: ignore[arg-type]
