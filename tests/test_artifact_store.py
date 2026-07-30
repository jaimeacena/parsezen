from pathlib import Path

import pytest

from parsezen.infrastructure.artifact_store import ArtifactStore


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0xA5 for value in payload)


def test_artifact_store_keeps_review_text_out_of_plain_files(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)

    record = store.put_text(
        job_id="job-one",
        artifact_id="translation-one",
        text="Contenido privado",
    )

    stored = (tmp_path / "artifacts" / "job-one" / "translation-one.pza").read_bytes()
    assert b"Contenido privado" not in stored
    assert store.read_text(record.job_id, record.id) == "Contenido privado"
    assert record.size_bytes == len(b"Contenido privado")


def test_artifact_store_is_immutable_and_path_safe(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    store.put(job_id="job", artifact_id="one", payload=b"first", media_type="text/plain")

    with pytest.raises(FileExistsError):
        store.put(job_id="job", artifact_id="one", payload=b"second", media_type="text/plain")
    with pytest.raises(ValueError):
        store.put(job_id="../escape", payload=b"x", media_type="text/plain")


def test_artifact_store_removes_only_one_job(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    store.put(job_id="one", artifact_id="a", payload=b"a", media_type="text/plain")
    store.put(job_id="two", artifact_id="b", payload=b"b", media_type="text/plain")

    store.remove_job("one")

    assert not (tmp_path / "artifacts" / "one").exists()
    assert store.read("two", "b") == b"b"


def test_artifact_store_prunes_only_orphaned_review_data(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root, protect=reversible, unprotect=reversible)
    store.put(job_id="active", artifact_id="a", payload=b"a", media_type="text/plain")
    store.put(job_id="orphan", artifact_id="b", payload=b"b", media_type="text/plain")
    (root / "active" / ".artifact-crashed.tmp").write_bytes(b"protected temporary data")
    foreign = root / "foreign.folder"
    foreign.mkdir()
    (foreign / "do-not-touch.txt").write_text("foreign", encoding="utf-8")

    removed = store.prune_orphaned_jobs(("active",))

    assert removed == ("orphan",)
    assert store.read("active", "a") == b"a"
    assert not (root / "active" / ".artifact-crashed.tmp").exists()
    assert not (root / "orphan").exists()
    assert (foreign / "do-not-touch.txt").read_text(encoding="utf-8") == "foreign"


def test_artifact_store_prune_validates_every_retained_identifier(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)

    with pytest.raises(ValueError, match="path-safe"):
        store.prune_orphaned_jobs(("valid", "../escape"))


def test_artifact_store_rejects_corrupted_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    store.put(job_id="job", artifact_id="one", payload=b"private", media_type="text/plain")
    path = tmp_path / "artifacts" / "job" / "one.pza"
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 1
    path.write_bytes(corrupted)

    with pytest.raises(ValueError, match="integrity"):
        store.read("job", "one")
