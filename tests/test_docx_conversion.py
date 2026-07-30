from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from zipfile import BadZipFile

import pytest

import parsezen.docx_conversion as docx_module
from parsezen.errors import ConversionError


class _Image:
    alt_text = None

    def __init__(
        self,
        content_type: str,
        content: bytes = b"image",
        *,
        read_error: bool = False,
    ) -> None:
        self.content_type = content_type
        self._content = content
        self._read_error = read_error

    def open(self) -> AbstractContextManager[BinaryIO]:
        if self._read_error:
            raise OSError("unreadable")
        return BytesIO(self._content)


def _stub_conversion_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    images: list[_Image],
    *,
    markdown: object = "# Document",
) -> None:
    monkeypatch.setattr(docx_module, "pre_process_docx", lambda stream: stream)

    def convert_to_html(
        _stream: BinaryIO,
        *,
        convert_image: Callable[[object], list[object]],
    ) -> SimpleNamespace:
        for image in images:
            convert_image(image)
        return SimpleNamespace(value="<p>Document</p>")

    class Converter:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def convert_stream(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(markdown=markdown)

    monkeypatch.setattr(docx_module.mammoth, "convert_to_html", convert_to_html)
    monkeypatch.setattr(docx_module, "MarkItDown", Converter)


def test_docx_resources_deduplicate_supported_images_and_ignore_legacy_previews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    _stub_conversion_pipeline(
        monkeypatch,
        [
            _Image("image/png", b"same"),
            _Image("image/png", b"same"),
            _Image("image/x-emf", b"legacy"),
        ],
    )

    converted = docx_module.convert_docx(source)

    assert converted.markdown == "# Document"
    assert len(converted.resources) == 1
    assert converted.resources[0].content == b"same"


@pytest.mark.parametrize(
    ("images", "constant", "value", "message"),
    [
        ([_Image("image/png", read_error=True)], None, None, "leer una imagen"),
        ([_Image("image/png", b"abc")], "_MAX_IMAGE_BYTES", 2, "50 MiB"),
        ([_Image("image/png")], "_MAX_DOCX_IMAGES", 0, "500 imágenes"),
        ([_Image("image/png", b"abc")], "_MAX_TOTAL_IMAGE_BYTES", 2, "250 MiB"),
    ],
)
def test_docx_image_safety_limits_fail_with_an_actionable_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    images: list[_Image],
    constant: str | None,
    value: int | None,
    message: str,
) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    if constant is not None and value is not None:
        monkeypatch.setattr(docx_module, constant, value)
    _stub_conversion_pipeline(monkeypatch, images)

    with pytest.raises(ConversionError, match=message):
        docx_module.convert_docx(source)


def test_docx_rejects_an_invalid_converter_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    _stub_conversion_pipeline(monkeypatch, [], markdown=None)

    with pytest.raises(ConversionError, match="resultado no válido"):
        docx_module.convert_docx(source)


def test_docx_wraps_container_failures_as_conversion_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "book.docx"
    source.write_bytes(b"docx")
    monkeypatch.setattr(
        docx_module,
        "pre_process_docx",
        lambda _stream: (_ for _ in ()).throw(BadZipFile("broken")),
    )

    with pytest.raises(ConversionError, match="No se pudo convertir book.docx"):
        docx_module.convert_docx(source)
