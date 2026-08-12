from uuid import uuid4

import pytest

from parsezen.domain.books import (
    BookDocument,
    BookMetadata,
    BookResource,
    BookSection,
)


def test_book_document_validates_spine_resources_and_hierarchy() -> None:
    child = BookSection("child", "Section", "child-xhtml")
    root = BookSection("root", "Chapter", "root-xhtml", (child,))
    book = BookDocument(
        metadata=BookMetadata("Book", language="es"),
        sections=(root,),
        spine=("root", "child"),
        resources=(BookResource("cover", "cover.png", "image/png", "cover-bytes"),),
        cover_resource_id="cover",
    )

    assert book.section("child") is child


def test_book_document_rejects_an_incomplete_spine() -> None:
    with pytest.raises(ValueError):
        BookDocument(
            metadata=BookMetadata("Book"),
            sections=(BookSection("root", "Chapter", "root-xhtml"),),
            spine=(),
        )


def test_book_document_rejects_duplicate_source_filenames() -> None:
    with pytest.raises(ValueError, match="source filenames"):
        BookDocument(
            BookMetadata("Book"),
            (
                BookSection(
                    "first",
                    "First",
                    "first-xhtml",
                    source_filename="chapter-0001.xhtml",
                ),
                BookSection(
                    "second",
                    "Second",
                    "second-xhtml",
                    source_filename="CHAPTER-0001.XHTML",
                ),
            ),
            ("first", "second"),
        )


def test_book_metadata_accepts_only_normalized_bounded_values() -> None:
    metadata = BookMetadata(
        "Book",
        identifier=str(uuid4()),
        identifiers=("primary-id", "secondary-id"),
        publisher="Local Publisher",
        publication_date="2024-03-14",
    )
    assert metadata.title == "Book"
    assert metadata.identifiers == ("primary-id", "secondary-id")
    with pytest.raises(ValueError, match="language"):
        BookMetadata("Book", language="EN")
    with pytest.raises(ValueError, match="identifiers"):
        BookMetadata("Book", identifiers=("duplicate", "duplicate"))


def test_book_model_rejects_unsafe_resources_and_wrong_reading_order() -> None:
    root = BookSection("root", "Chapter", "root-xhtml")
    with pytest.raises(ValueError, match="safe relative"):
        BookResource("image", "../outside.png", "image/png", "payload")
    with pytest.raises(ValueError, match="spine"):
        BookDocument(
            metadata=BookMetadata("Book"),
            sections=(root,),
            spine=("missing",),
        )


def test_book_model_bounds_section_count_and_hierarchy_depth() -> None:
    sections = tuple(
        BookSection(f"section-{index}", f"Section {index}", f"xhtml-{index}")
        for index in range(251)
    )
    with pytest.raises(ValueError, match="too many"):
        BookDocument(
            metadata=BookMetadata("Book"),
            sections=sections,
            spine=tuple(section.id for section in sections),
        )

    nested = BookSection("leaf", "Leaf", "leaf-xhtml")
    for index in range(12):
        nested = BookSection(
            f"parent-{index}",
            f"Parent {index}",
            f"parent-xhtml-{index}",
            (nested,),
        )
    with pytest.raises(ValueError, match="deeply nested"):
        BookDocument(
            metadata=BookMetadata("Book"),
            sections=(nested,),
            spine=tuple(section.id for section in nested.walk()),
        )
