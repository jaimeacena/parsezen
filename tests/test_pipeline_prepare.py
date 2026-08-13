from pathlib import Path

from parsezen.document_model import ConvertedDocument
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.pipeline.contracts import ProcessRequest
from parsezen.pipeline.prepare import prepare_document_input


def test_preparation_converts_and_analyzes_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Original", encoding="utf-8")
    stages: list[ProcessStage] = []

    prepared = prepare_document_input(
        ProcessRequest(source, convert_to_markdown=True),
        stages.append,
        None,
        None,
        None,
        converter=lambda *_args, **_kwargs: ConvertedDocument("# Prepared\n\nText."),
    )

    assert stages == [ProcessStage.READING]
    assert prepared.source_path == source
    assert prepared.markdown == "# Prepared\n\nText."
    assert prepared.semantic_document.blocks
    assert tuple(tmp_path.iterdir()) == (source,)
