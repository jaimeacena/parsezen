from pathlib import Path

from parsezen.domain.jobs import MarkdownOrganization
from parsezen.improvement import ImprovementMode
from parsezen.pipeline.contracts import (
    ProcessRequest as PipelineRequest,
)
from parsezen.pipeline.contracts import (
    ProcessResult as PipelineResult,
)
from parsezen.pipeline.contracts import (
    ProcessTelemetry as PipelineTelemetry,
)
from parsezen.processing import ProcessRequest, ProcessResult, ProcessTelemetry
from parsezen.workflow import OutputFormat


def test_processing_facade_reexports_the_pipeline_contracts() -> None:
    assert ProcessRequest is PipelineRequest
    assert ProcessResult is PipelineResult
    assert ProcessTelemetry is PipelineTelemetry


def test_flat_process_contracts_expose_grouped_migration_adapters() -> None:
    request = ProcessRequest(
        Path("source.pdf"),
        True,
        output_directory=Path("output"),
        improvement_mode=ImprovementMode.TRANSLATE,
        target_language="es",
        output_format=OutputFormat.EPUB,
        review_content=True,
        markdown_organization=MarkdownOrganization.BY_CHAPTER,
        source_content_sha256="a" * 64,
    )
    result = ProcessResult(
        Path("output/book.epub"),
        review_markdown="local review text",
        review_required=True,
        problematic_pdf_pages=(4,),
    )

    assert request.source.path == request.source_path
    assert request.source.content_sha256 == "a" * 64
    assert request.translation.target_language == "es"
    assert request.review.content
    assert request.publication.output_format is OutputFormat.EPUB
    assert result.review.markdown == "local review text"
    assert result.publication.final_path == result.final_path
    assert result.quality.problematic_pdf_pages == (4,)
