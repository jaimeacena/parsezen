from pathlib import Path

from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    TransformedDocument,
)
from parsezen.pipeline.publish import publish_transformed_document
from parsezen.semantic_blocks import analyze_markdown


def test_publication_writes_a_prepared_transformation_without_transforming_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    markdown = "# Prepared\n\nText."
    prepared = PreparedDocument(
        source,
        None,
        None,
        None,
        None,
        (),
        markdown,
        (),
        analyze_markdown(markdown),
        (),
    )
    transformed = TransformedDocument(
        transformed_markdown=markdown,
        translation_quality_report=None,
        review_translation_quality_report=None,
        linguistic_review_coverage=None,
        preserved_translation_chunks=(),
        revision_draft=None,
        published_markdown=markdown,
        review_required=False,
        public_markdown=markdown,
    )
    stages: list[ProcessStage] = []

    result = publish_transformed_document(
        prepared,
        transformed,
        ProcessRequest(source, convert_to_markdown=True),
        stages.append,
        None,
        None,
        None,
        None,
        None,
    )

    assert stages == [ProcessStage.WRITING, ProcessStage.COMPLETED]
    assert result.final_path.read_text(encoding="utf-8") == markdown
    assert source.read_text(encoding="utf-8") == "Original"
