from base64 import b64decode
from pathlib import Path, PurePosixPath

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QFont, QImage, QTextCursor, QTextDocument
from PySide6.QtWidgets import QInputDialog, QLabel, QMessageBox, QToolButton

import parsezen.presentation.book_editor_dialog as editor_dialog_module
from parsezen.application.book_editor import create_book_from_markdown
from parsezen.document_model import ConvertedResource
from parsezen.epub_builder import EPUB_CHAPTER_MARKER, EpubBookMetadata
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.presentation.book_editor_dialog import BookEditorDialog


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x44 for value in payload)


def test_qt_editor_round_trip_preserves_internal_anchors_and_links(qtbot) -> None:
    del qtbot
    source = (
        '<h2 id="destination">Target</h2>'
        '<p><a href="chapter-0002.xhtml#destination">Jump</a></p>'
        '<span id="empty-anchor"></span>'
    )
    document = QTextDocument()

    document.setHtml(editor_dialog_module._html_for_qt_editor(source))
    restored = editor_dialog_module._body_from_qt_html(document.toHtml())

    parsed = editor_dialog_module.html.fragment_fromstring(
        restored,
        create_parent="div",
    )
    assert parsed.xpath(".//*[@id='destination']")
    assert parsed.xpath(".//*[@id='empty-anchor']")
    assert parsed.xpath(".//a[@href='chapter-0002.xhtml#destination']")
    assert "\u2060" not in restored
    assert "parsezen-anchor:" not in restored


def test_book_editor_dialog_edits_and_publishes_the_same_book(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nOriginal.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    destination = tmp_path / "book.epub"
    destination.write_bytes(b"draft")
    dialog = BookEditorDialog(
        book,
        store,
        job_id="job",
        destination=destination,
    )
    qtbot.addWidget(dialog)

    dialog.editor.setHtml("<h1>Chapter</h1><p>Edited content.</p>")
    dialog._publish()

    assert destination.read_bytes().startswith(b"PK")
    assert "Edited content." in dialog._service().editable_html(book.spine[0])


def test_book_editor_dialog_supports_the_complete_structural_toolbar(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        f"# First\n\nOne.\n\n{EPUB_CHAPTER_MARKER}\n\n# Second\n\nTwo.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    destination = tmp_path / "book.epub"
    dialog = BookEditorDialog(book, store, job_id="job", destination=destination)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    answers = iter((("Renamed", True), ("Added", True), ("Split", True)))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    second = dialog._find_item(dialog.book.spine[1])
    assert second is not None
    dialog.tree.setCurrentItem(second)
    dialog._rename()
    dialog._add()
    dialog._move_up()
    dialog._move_down()
    dialog._indent()
    dialog._outdent()
    dialog._navigate(-1)
    dialog._navigate(1)
    dialog._bold()
    dialog._italic()
    dialog._underline()
    dialog._bullet_list()
    dialog._numbered_list()
    dialog.alignment.setCurrentIndex(1)
    dialog._clear_formatting()
    dialog.heading.setCurrentIndex(dialog.heading.findData(2))
    dialog._heading_changed()
    dialog._cursor_changed()

    visible_structure_actions = {
        button.toolTip()
        for button in dialog.structure_toolbar.findChildren(QToolButton)
        if button.isVisible()
    }
    assert {
        "Separar desde aquí",
        "Unir con la división anterior",
        "Mover arriba",
        "Mover abajo",
        "Elevar nivel",
        "Anidar",
        "Renombrar",
    } <= visible_structure_actions
    assert dialog.book.section(dialog._selected_id()).title in {"Renamed", "Added"}
    assert dialog.editor.fontWeight() in {QFont.Weight.Normal, QFont.Weight.Bold}
    assert not dialog.grab().isNull()

    dialog.editor.setHtml("<p>First part.</p><p>Second part.</p>")
    cursor = dialog.editor.document().find("Second")
    cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
    dialog.editor.setTextCursor(cursor)
    dialog._split()
    split_count = len(dialog.book.spine)
    dialog._merge()

    assert len(dialog.book.spine) == split_count - 1


def test_book_editor_prioritizes_editing_space_and_offers_bounded_zoom(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nReadable content.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.splitter.sizes()[1] > dialog.splitter.sizes()[0] * 2
    assert dialog.zoom_reset_button.text() == "100 %"

    dialog.zoom_in_button.click()
    assert dialog.zoom_reset_button.text() == "110 %"
    dialog.zoom_out_button.click()
    assert dialog.zoom_reset_button.text() == "100 %"

    for _ in range(20):
        dialog.zoom_out_button.click()
    assert dialog.zoom_reset_button.text() == "70 %"
    assert not dialog.zoom_out_button.isEnabled()

    dialog.zoom_reset_button.click()
    assert dialog.zoom_reset_button.text() == "100 %"
    assert dialog.zoom_out_button.isEnabled()
    assert dialog.zoom_in_button.isEnabled()


def test_book_editor_uses_one_compact_content_toolbar_without_redundant_headings(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nReadable content.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    labels = {label.text() for label in dialog.findChildren(QLabel)}
    assert "Capítulos y secciones" not in labels
    assert "Contenido" not in labels

    tools = dialog.content_toolbar.findChildren(QToolButton)
    assert tools
    assert all(tool.height() == tools[0].height() for tool in tools)
    assert tools[0].height() <= 34
    assert all(tool.parentWidget() is dialog.content_toolbar for tool in tools)
    assert all(tool.geometry().center().y() == tools[0].geometry().center().y() for tool in tools)
    assert all(
        not tool.icon().isNull()
        for tool in tools
        if tool.property("editorAction")
        in {
            "undo",
            "redo",
            "italic",
            "bullet_list",
            "numbered_list",
            "link",
            "clear_formatting",
            "zoom_out",
            "zoom_in",
            "previous",
            "next",
        }
    )
    assert dialog.heading.height() == dialog.alignment.height() == tools[0].height()

    structure_tools = dialog.structure_toolbar.findChildren(QToolButton)
    assert structure_tools
    assert all(tool.height() == tools[0].height() for tool in structure_tools)
    assert all(
        tool.geometry().center().y() == structure_tools[0].geometry().center().y()
        for tool in structure_tools
    )
    assert dialog.structure_toolbar.geometry().top() < dialog.tree.geometry().top()
    assert "QScrollBar:horizontal" in dialog.styleSheet()


def test_book_editor_groups_metadata_and_cover_in_one_dropdown(
    qtbot,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nReadable content.",
        (),
        EpubBookMetadata("Book", "en", "Author"),
        store,
        job_id="job",
    )
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"cover")
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)

    assert dialog.metadata_panel.isHidden()
    assert dialog.metadata_button.accessibleName() == "Mostrar metadatos y portada"
    assert dialog.tree.accessibleName() == "Capítulos y secciones"
    assert dialog.position_label.accessibleName() == "Posición del capítulo"
    dialog.metadata_button.click()
    assert not dialog.metadata_panel.isHidden()
    assert dialog.metadata_button.accessibleName() == "Ocultar metadatos y portada"
    assert 34 <= dialog.title_input.height() <= 38
    assert 34 <= dialog.author_input.height() <= 38
    assert 34 <= dialog.language_input.height() <= 38
    assert 34 <= dialog.cover_selector.height() <= 38

    monkeypatch.setattr(
        editor_dialog_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(cover), "Imágenes"),
    )
    dialog.cover_selector.setCurrentIndex(
        dialog.cover_selector.findData("choose"),
    )
    dialog.cover_selector.activated.emit(dialog.cover_selector.currentIndex())

    assert dialog.book.cover_resource_id is not None
    assert dialog.cover_selector.currentData() == "keep"


def test_book_editor_dialog_saves_for_later_without_publishing(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nOriginal.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    destination = tmp_path / "book.epub"
    dialog = BookEditorDialog(book, store, job_id="job", destination=destination)
    qtbot.addWidget(dialog)
    dialog.editor.setHtml("<h1>Chapter</h1><p>Saved draft.</p>")

    dialog._save_and_close()

    assert "Saved draft." in dialog._service().editable_html(dialog.book.spine[0])
    assert not destination.exists()


def test_book_editor_dialog_resolves_encrypted_book_images(qtbot, tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    image = b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    book = create_book_from_markdown(
        "# Chapter\n\n![Figure](__parsezen_resources__/pdf/figure.png)",
        (ConvertedResource(PurePosixPath("pdf/figure.png"), image, "image/png"),),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)

    loaded = dialog._editor_document.loadResource(  # noqa: SLF001
        int(QTextDocument.ResourceType.ImageResource),
        QUrl("../images/pdf/figure.png"),
    )

    assert isinstance(loaded, QImage)
    assert not loaded.isNull()


def test_book_editor_reflows_panes_toolbars_and_footer_at_320(
    qtbot,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    book = create_book_from_markdown(
        "# Chapter\n\nOriginal.",
        (),
        EpubBookMetadata("Book", "en"),
        store,
        job_id="job",
    )
    dialog = BookEditorDialog(book, store, job_id="job", destination=None)
    qtbot.addWidget(dialog)
    dialog.resize(320, 760)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.width() == 320
    assert dialog.splitter.orientation() is Qt.Orientation.Vertical
    assert dialog.structure_tool_strip.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    assert dialog.content_tool_strip.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    save_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.save_later_button)
    )
    cancel_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.cancel_button)
    )
    publish_position = dialog.footer_layout.getItemPosition(
        dialog.footer_layout.indexOf(dialog.publish_button)
    )
    assert publish_position[0] == cancel_position[0]
    assert save_position[0] < publish_position[0]
