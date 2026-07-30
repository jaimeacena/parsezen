from pathlib import Path

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
    RefinementConfiguration,
    StructureConfiguration,
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
            refinement=RefinementConfiguration(enabled=True),
        ),
    )

    assert len(issues) == 1
    assert issues[0].section is ConfigurationSection.RESULT


def test_shared_ai_profile_satisfies_every_ai_backed_phase() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
        ai=AIProfileConfiguration(model="qwen3:4b", context_window=8192),
        translation=TranslationConfiguration(
            enabled=True,
            method=TranslationMethod.LOCAL_AI,
            target_language="Español",
        ),
        refinement=RefinementConfiguration(enabled=True),
        structure=StructureConfiguration(enabled=True),
    )

    assert requires_ai(configuration)
    assert configuration_issues(source(), configuration) == ()


def test_validation_groups_actionable_errors_by_their_configuration_section() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(
            format=DocumentFormat.MARKDOWN,
            directory_is_custom=True,
        ),
        translation=TranslationConfiguration(
            enabled=True,
            method=TranslationMethod.LOCAL_AI,
        ),
        structure=StructureConfiguration(enabled=True),
    )

    issues = configuration_issues(
        source(DocumentFormat.TEXT),
        configuration,
    )

    assert {issue.section for issue in issues} == {
        ConfigurationSection.RESULT,
        ConfigurationSection.TRANSLATION,
        ConfigurationSection.STRUCTURE,
        ConfigurationSection.AI,
    }


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


def test_legacy_same_format_noop_can_be_loaded_but_not_newly_saved() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.TEXT),
    )
    document = source(DocumentFormat.TEXT)

    assert any(
        "Activa al menos" in issue.message
        for issue in configuration_issues(document, configuration)
    )
    assert (
        configuration_issues(
            document,
            configuration,
            allow_noop_same_format=True,
        )
        == ()
    )


def test_epub_same_format_is_valid_because_final_personalization_is_an_operation() -> None:
    configuration = JobConfiguration(
        output=OutputConfiguration(format=DocumentFormat.EPUB),
    )

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
