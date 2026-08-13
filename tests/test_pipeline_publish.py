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
        markdown,
        None,
        None,
        (),
        None,
        markdown,
        False,
        markdown,
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
