from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

import parsezen.output as output
from parsezen.document_model import ConvertedResource
from parsezen.errors import FinalIntegrityError, OutputWriteError


def _resource(path: str, content: bytes = b"image") -> ConvertedResource:
    return ConvertedResource(PurePosixPath(path), content, "image/png")


def test_same_format_outputs_are_atomic_and_collision_free(tmp_path: Path) -> None:
    text_source = tmp_path / "notes.txt"
    text_source.write_text("original", encoding="utf-8")
    (tmp_path / "notes.mended.txt").write_text("existing", encoding="utf-8")
    docx_source = tmp_path / "book.docx"
    docx_source.write_bytes(b"original-docx")
    (tmp_path / "book.mended.docx").write_bytes(b"existing-docx")

    text_result = output.write_text_output(text_source, "improved")
    docx_result = output.write_docx_output(docx_source, b"improved-docx")

    assert text_result.name == "notes-2.mended.txt"
    assert text_result.read_text(encoding="utf-8") == "improved"
    assert docx_result.name == "book-2.mended.docx"
    assert docx_result.read_bytes() == b"improved-docx"
    assert text_source.read_text(encoding="utf-8") == "original"
    assert docx_source.read_bytes() == b"original-docx"


def test_conversion_rejects_duplicate_or_unsafe_resource_paths_without_partial_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    duplicate = (_resource("images/a.png", b"first"), _resource("images/a.png", b"second"))

    with pytest.raises(OutputWriteError, match="repetidas"):
        output.write_conversion_output(source, "Text", resources=duplicate)
    assert not (tmp_path / "book.md").exists()
    assert not (tmp_path / "book.assets").exists()

    with pytest.raises(OutputWriteError, match="no válida"):
        output.write_conversion_output(
            source,
            "Text",
            resources=(_resource("../private.png"),),
        )
    assert not (tmp_path / "book.assets").exists()


def test_improvement_collision_rolls_back_the_first_pair_and_retries_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    original_publish = output._publish_staged_file
    calls = 0

    def collide_once(staged: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FileExistsError
        original_publish(staged, destination)

    monkeypatch.setattr(output, "_publish_staged_file", collide_once)

    final_path, raw_path = output.write_improvement_outputs(
        source,
        "Raw",
        "Improved",
        keep_raw=True,
        resources=(_resource("figure.png"),),
    )

    assert final_path.name == "book-2.mended.md"
    assert raw_path is not None and raw_path.name == "book-2.raw.md"
    assert not (tmp_path / "book.raw.md").exists()
    assert not (tmp_path / "book.assets").exists()
    assert (tmp_path / "book-2.assets" / "figure.png").read_bytes() == b"image"


def test_improvement_failure_removes_already_published_raw_and_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"pdf")
    original_publish = output._publish_staged_file
    calls = 0

    def fail_final(staged: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk disappeared")
        original_publish(staged, destination)

    monkeypatch.setattr(output, "_publish_staged_file", fail_final)

    with pytest.raises(RuntimeError, match="disk disappeared"):
        output.write_improvement_outputs(
            source,
            "Raw",
            "Improved",
            keep_raw=True,
            resources=(_resource("figure.png"),),
        )

    assert not (tmp_path / "book.raw.md").exists()
    assert not (tmp_path / "book.mended.md").exists()
    assert not (tmp_path / "book.assets").exists()


def test_output_helpers_translate_filesystem_failures_to_user_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OutputWriteError, match="preparar"):
        output._stage_markdown(tmp_path / "missing", "Text")

    with pytest.raises(OutputWriteError, match="escribir"):
        output._write_bytes_exclusively(tmp_path / "missing" / "book.epub", b"epub")

    monkeypatch.setattr(
        output.os.path,
        "relpath",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("different drive")),
    )
    with pytest.raises(OutputWriteError, match="misma unidad"):
        output._resource_reference(tmp_path, tmp_path / "assets")


def test_review_replacements_are_atomic_and_report_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_path = tmp_path / "result.md"
    binary_path = tmp_path / "result.epub"
    text_path.write_text("before", encoding="utf-8")
    binary_path.write_bytes(b"before")

    output.replace_text_output(text_path, "after")
    output.replace_binary_output(binary_path, b"after")

    assert text_path.read_text(encoding="utf-8") == "after"
    assert binary_path.read_bytes() == b"after"

    monkeypatch.setattr(
        output.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("locked")),
    )
    with pytest.raises(OutputWriteError, match="actualizar"):
        output.replace_text_output(text_path, "blocked")
    with pytest.raises(OutputWriteError, match="actualizar"):
        output.replace_binary_output(binary_path, b"blocked")


def test_staging_validation_runs_before_publication_and_leaves_no_partial_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "notes.mended.txt"

    def reject(staged: Path) -> None:
        assert staged.read_text(encoding="utf-8") == "candidate"
        assert not destination.exists()
        raise FinalIntegrityError("Control final fallido")

    with pytest.raises(FinalIntegrityError, match="Control final"):
        output.write_text_output(source, "candidate", validate_staged=reject)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".parsezen-*.tmp"))


def test_failed_review_validation_preserves_the_previous_result(tmp_path: Path) -> None:
    destination = tmp_path / "result.epub"
    destination.write_bytes(b"previous")

    with pytest.raises(FinalIntegrityError):
        output.replace_binary_output(
            destination,
            b"candidate",
            validate_staged=lambda _path: (_ for _ in ()).throw(
                FinalIntegrityError("Control final fallido")
            ),
        )

    assert destination.read_bytes() == b"previous"
    assert not tuple(tmp_path.glob(".parsezen-review-*.tmp"))


def test_non_windows_publish_does_not_fall_back_to_an_overwriting_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "staged"
    staged.write_text("complete", encoding="utf-8")
    destination = tmp_path / "result"
    monkeypatch.setattr(output.os, "name", "posix")
    monkeypatch.setattr(
        output.os,
        "link",
        lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
    )

    with pytest.raises(OSError, match="unsupported"):
        output._publish_without_overwrite(staged, destination)

    assert staged.exists()
    assert not destination.exists()


def test_cleanup_helpers_tolerate_races_and_locked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert output._validate_resource_path(PurePosixPath("nested/image.png")) == PurePosixPath(
        "nested/image.png"
    )
    output._remove_resource_directory(None)
    output._remove_if_present(None)

    locked = tmp_path / "locked.tmp"
    locked.write_bytes(b"x")
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
    output._remove_if_present(locked)
