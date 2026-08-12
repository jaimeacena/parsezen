from pathlib import Path

import pytest

from parsezen.application.configuration_rules import (
    ConfigurationSection,
    configuration_issues,
    requires_ai,
)
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    PageRangeConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)


def source(document_format: DocumentFormat = DocumentFormat.PDF) -> DocumentSource:
    return DocumentSource(Path(f"book.{document_format.value}"), document_format, 100, 1)


def test_unconfigured_result_is_the_only_initial_blocker() -> None:
    issues = configuration_issues(
        source(),
        JobConfiguration(
            output=OutputConfiguration(configured=False),
            plan=ProcessingPlan.LOCAL_AI_REVIEWED,
        ),
    )

    assert len(issues) == 1
    assert issues[0].section is ConfigurationSection.RESULT


def test_reviewed_plan_uses_the_single_global_ai_profile() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
        ai=AIProfileConfiguration(model="qwen3:4b", context_window=8192),
        translation=TranslationConfiguration(enabled=True, target_language="es"),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )

    assert requires_ai(configuration)
    assert configuration_issues(source(), configuration) == ()


def test_standard_plan_never_requires_ai() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
        translation=TranslationConfiguration(enabled=True),
    )

    issues = configuration_issues(source(DocumentFormat.TEXT), configuration)

    assert not requires_ai(configuration)
    assert {issue.section for issue in issues} == {ConfigurationSection.TRANSLATION}


def test_reviewed_plan_requires_a_global_model() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.MARKDOWN),
        plan=ProcessingPlan.LOCAL_AI_REVIEWED,
    )

    issues = configuration_issues(source(DocumentFormat.TEXT), configuration)

    assert any(issue.section is ConfigurationSection.AI for issue in issues)


def test_ai_translation_requires_the_global_model_even_in_standard_plan() -> None:
    configuration = JobConfiguration(
        translation=TranslationConfiguration(
            enabled=True,
            method=TranslationMethod.LOCAL_AI,
            target_language="es",
        )
    )

    issues = configuration_issues(source(DocumentFormat.TEXT), configuration)

    assert requires_ai(configuration)
    assert any(
        issue.section is ConfigurationSection.AI
        and issue.message.endswith("antes de traducir con IA local.")
        for issue in issues
    )


def test_product_outputs_are_limited_to_markdown_and_epub() -> None:
    with pytest.raises(ValueError, match="Markdown or EPUB"):
        OutputConfiguration(format=DocumentFormat.TEXT)


def test_custom_epub_cover_requires_an_image() -> None:
    issues = configuration_issues(
        source(),
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                include_images=False,
                cover_strategy=CoverStrategy.CUSTOM,
            )
        ),
    )

    assert any("portada" in issue.message for issue in issues)


def test_markdown_same_format_requires_translation_or_reviewed_plan() -> None:
    configuration = JobConfiguration(output=OutputConfiguration(format=DocumentFormat.MARKDOWN))
    document = source(DocumentFormat.MARKDOWN)

    assert configuration_issues(document, configuration)
    assert configuration_issues(document, configuration, allow_noop_same_format=True) == ()


def test_epub_same_format_is_valid_because_final_confirmation_is_an_operation() -> None:
    configuration = JobConfiguration(output=OutputConfiguration(format=DocumentFormat.EPUB))

    assert configuration_issues(source(DocumentFormat.EPUB), configuration) == ()


def test_page_ranges_are_rejected_outside_pdf() -> None:
    issues = configuration_issues(
        source(DocumentFormat.MARKDOWN),
        JobConfiguration(
            output=OutputConfiguration(format=DocumentFormat.EPUB),
            page_range=PageRangeConfiguration(1, 2),
        ),
    )

    assert any("PDF" in issue.message for issue in issues)
