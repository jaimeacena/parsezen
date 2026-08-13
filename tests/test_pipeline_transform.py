from pathlib import Path

from parsezen.pipeline.contracts import PreparedDocument, ProcessRequest
from parsezen.pipeline.transform import transform_prepared_document
from parsezen.semantic_blocks import analyze_markdown


def test_transformation_keeps_identity_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Original\n\nText.", encoding="utf-8")
    markdown = source.read_text(encoding="utf-8")
    prepared = PreparedDocument(
        source_path=source,
        resolved_page_range=None,
        output_stem=None,
        pdf_quality_report=None,
        source_cover_path=None,
        converted_resources=(),
        markdown=markdown,
        problematic_pdf_pages=(),
        semantic_document=analyze_markdown(markdown),
        translation_glossary=(),
    )

    transformed = transform_prepared_document(
        prepared,
        ProcessRequest(source, convert_to_markdown=False),
        None,
        None,
        None,
        None,
        None,
        generated_epub=False,
    )

    assert transformed.transformed_markdown == markdown
    assert transformed.public_markdown == markdown
    assert transformed.revision_draft is None
    assert transformed.review_required is False
    assert tuple(tmp_path.iterdir()) == (source,)
