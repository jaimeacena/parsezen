from pathlib import Path

import pytest

from parsezen.domain.execution_plan import ExecutionStep, compile_execution_plan
from parsezen.domain.jobs import (
    CoverStrategy,
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)


def _source(source_format: DocumentFormat) -> DocumentSource:
    suffix = {
        DocumentFormat.TEXT: ".txt",
        DocumentFormat.MARKDOWN: ".md",
        DocumentFormat.DOCX: ".docx",
        DocumentFormat.PDF: ".pdf",
        DocumentFormat.EPUB: ".epub",
    }[source_format]
    return DocumentSource(Path(f"book{suffix}"), source_format, 100, 1)


@pytest.mark.parametrize("source_format", tuple(DocumentFormat))
@pytest.mark.parametrize("output_format", (DocumentFormat.MARKDOWN, DocumentFormat.EPUB))
def test_plain_workflow_matrix_is_deterministic(
    source_format: DocumentFormat,
    output_format: DocumentFormat,
) -> None:
    configuration = JobConfiguration(output=OutputConfiguration(format=output_format))

    first = compile_execution_plan(_source(source_format), configuration)
    second = compile_execution_plan(_source(source_format), configuration)

    assert first == second
    if source_format is DocumentFormat.EPUB and output_format is DocumentFormat.EPUB:
        assert first.steps == (ExecutionStep.PRESERVE_EPUB,)
    else:
        publication = (
            ExecutionStep.BUILD_EPUB
            if output_format is DocumentFormat.EPUB
            else ExecutionStep.WRITE_MARKDOWN
        )
        assert first.steps == (ExecutionStep.CONVERT, publication)


@pytest.mark.parametrize(
    ("method", "translation_step"),
    (
        (TranslationMethod.LOCAL_AI, ExecutionStep.TRANSLATE_AI),
        (TranslationMethod.OFFLINE, ExecutionStep.TRANSLATE_OFFLINE),
    ),
)
def test_translation_and_repair_are_one_ordered_route(
    method: TranslationMethod,
    translation_step: ExecutionStep,
) -> None:
    configuration = JobConfiguration(
        translation=TranslationConfiguration(True, method, "es"),
    )

    plan = compile_execution_plan(_source(DocumentFormat.DOCX), configuration)

    assert plan.steps == (
        ExecutionStep.CONVERT,
        translation_step,
        ExecutionStep.REPAIR_TRANSLATION,
        ExecutionStep.WRITE_MARKDOWN,
    )


def test_reviewed_epub_contains_content_structure_and_build_in_order() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
        translation=TranslationConfiguration(True, TranslationMethod.OFFLINE, "es"),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )

    plan = compile_execution_plan(_source(DocumentFormat.PDF), configuration)

    assert plan.steps == (
        ExecutionStep.CONVERT,
        ExecutionStep.TRANSLATE_OFFLINE,
        ExecutionStep.REPAIR_TRANSLATION,
        ExecutionStep.REVIEW_CONTENT,
        ExecutionStep.REVIEW_STRUCTURE,
        ExecutionStep.BUILD_EPUB,
    )


def test_epub_translation_preserves_or_rebuilds_from_output_options() -> None:
    translated = TranslationConfiguration(True, TranslationMethod.LOCAL_AI, "es")
    preserved = compile_execution_plan(
        _source(DocumentFormat.EPUB),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=translated,
        ),
    )
    rebuilt = compile_execution_plan(
        _source(DocumentFormat.EPUB),
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                cover_strategy=CoverStrategy.REMOVE,
            ),
            translation=translated,
        ),
    )

    assert preserved.steps[-1] is ExecutionStep.PRESERVE_EPUB
    assert rebuilt.steps == (
        ExecutionStep.TRANSLATE_AI,
        ExecutionStep.REPAIR_TRANSLATION,
        ExecutionStep.BUILD_EPUB,
    )
    assert preserved.uses_direct_epub_executor
    assert rebuilt.uses_direct_epub_executor


def test_translated_reviewed_epub_keeps_its_explicit_direct_executor() -> None:
    plan = compile_execution_plan(
        _source(DocumentFormat.EPUB),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            translation=TranslationConfiguration(True, TranslationMethod.LOCAL_AI, "es"),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
    )

    assert plan.steps == (
        ExecutionStep.TRANSLATE_AI,
        ExecutionStep.REPAIR_TRANSLATION,
        ExecutionStep.REVIEW_CONTENT,
        ExecutionStep.REVIEW_STRUCTURE,
        ExecutionStep.PRESERVE_EPUB,
    )
    assert plan.uses_direct_epub_executor


def test_forced_pdf_ocr_remains_part_of_convert() -> None:
    plan = compile_execution_plan(
        _source(DocumentFormat.PDF),
        JobConfiguration(force_pdf_ocr=True),
    )

    assert plan.steps == (ExecutionStep.CONVERT, ExecutionStep.WRITE_MARKDOWN)
