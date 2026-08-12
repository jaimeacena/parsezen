from __future__ import annotations

import pytest

from parsezen.workflow import (
    OutputFormat,
    WorkflowMode,
    WorkflowOptions,
    plan_workflow,
)


@pytest.mark.parametrize(
    ("extensions", "output_format", "available", "direct", "images", "inclusion"),
    [
        ([".txt"], OutputFormat.MARKDOWN, True, False, False, False),
        ([".pdf"], OutputFormat.MARKDOWN, True, False, True, True),
        (["docx"], OutputFormat.EPUB, True, False, False, True),
        ([".epub"], OutputFormat.EPUB, True, True, False, False),
        ([".epub", ".txt"], OutputFormat.EPUB, False, False, False, False),
        ([], OutputFormat.EPUB, False, False, False, False),
    ],
)
def test_plan_resolves_format_capabilities(
    extensions: list[str],
    output_format: OutputFormat,
    available: bool,
    direct: bool,
    images: bool,
    inclusion: bool,
) -> None:
    plan = plan_workflow(extensions, output_format)

    assert plan.output_available is available
    assert plan.direct_epub_translation is direct
    assert plan.image_options_visible is images
    assert plan.image_inclusion_available is inclusion


def test_direct_epub_can_be_personalized_without_translation_or_cleaning() -> None:
    missing_translation = plan_workflow([".epub"], OutputFormat.EPUB)
    cleaning = plan_workflow(
        [".epub"],
        OutputFormat.EPUB,
        options=WorkflowOptions(
            improvement_enabled=True,
            clean=True,
            translate=True,
        ),
    )
    valid = plan_workflow(
        [".epub"],
        OutputFormat.EPUB,
        options=WorkflowOptions(improvement_enabled=True, translate=True),
    )

    assert missing_translation.semantic_issue is None
    assert missing_translation.action_text(1) == "Personalizar EPUB"
    assert cleaning.semantic_issue == (
        "Esta configuración antigua no puede conservar el EPUB. "
        "Usa Corregir errores y ruido para revisarlo y reconstruirlo."
    )
    assert valid.semantic_issue is None
    assert "conservará capítulos" in valid.guidance


def test_enabled_improvement_requires_at_least_one_operation() -> None:
    plan = plan_workflow(
        [".pdf"],
        OutputFormat.MARKDOWN,
        options=WorkflowOptions(improvement_enabled=True),
    )

    assert plan.semantic_issue == (
        "Activa Traducir, Corregir errores y ruido u Organizar estructura."
    )


def test_mixed_epub_batch_explains_why_epub_is_unavailable() -> None:
    plan = plan_workflow([".EPUB", "txt"], OutputFormat.MARKDOWN)

    assert plan.mixed_with_epub
    assert "procésalo separado" in plan.guidance
    assert plan.epub_label == "Libro EPUB (.epub)"
    assert plan.epub_tooltip == "No está disponible para esta combinación de documentos."


def test_plan_centralizes_conversion_action_and_ready_copy() -> None:
    translated_epub = plan_workflow(
        ["epub"],
        OutputFormat.EPUB,
        options=WorkflowOptions(improvement_enabled=True, translate=True),
    )
    generated_epub = plan_workflow(
        ["pdf"],
        OutputFormat.EPUB,
        options=WorkflowOptions(improvement_enabled=True, translate=True),
    )
    markdown = plan_workflow(["txt"], OutputFormat.MARKDOWN)

    assert not translated_epub.convert_to_markdown_for("epub")
    assert translated_epub.action_text(2) == "Traducir 2 EPUB"
    assert translated_epub.ready_message(1) == (
        "Listo para traducir el EPUB conservando su formato."
    )
    assert generated_epub.convert_to_markdown_for("pdf")
    assert generated_epub.action_text(1) == "Traducir y crear EPUB"
    assert markdown.action_text(3) == "Crear 3 Markdown"
    assert markdown.ready_message(1) == "Listo para crear el Markdown."


def test_plan_rejects_an_untyped_output_format() -> None:
    with pytest.raises(TypeError, match="OutputFormat"):
        plan_workflow([".txt"], "markdown")  # type: ignore[arg-type]


def test_convert_mode_redirects_an_already_markdown_document_to_improve() -> None:
    plan = plan_workflow(
        [".md"],
        OutputFormat.MARKDOWN,
        options=WorkflowOptions(mode=WorkflowMode.CONVERT),
    )

    assert plan.semantic_issue == (
        "Este documento ya es Markdown. Usa Mejorar si quieres modificarlo."
    )


def test_epub_revision_is_rebuilt_after_approval() -> None:
    plan = plan_workflow(
        [".epub"],
        OutputFormat.EPUB,
        options=WorkflowOptions(
            mode=WorkflowMode.IMPROVE,
            review_content=True,
        ),
    )

    assert plan.epub_label == "EPUB revisado (.epub)"
    assert plan.epub_tooltip == (
        "Reconstruye el libro con los cambios que apruebes en la revisión."
    )


def test_translated_epub_uses_package_preserving_route_before_full_review() -> None:
    plan = plan_workflow(
        [".epub"],
        OutputFormat.EPUB,
        options=WorkflowOptions(
            mode=WorkflowMode.IMPROVE,
            improvement_enabled=True,
            translate=True,
            review_content=True,
            review_structure=True,
        ),
    )

    assert plan.direct_epub_translation
    assert not plan.convert_to_markdown_for(".epub")
    assert "conserva la estructura" in plan.epub_tooltip


def test_blank_extensions_are_ignored() -> None:
    plan = plan_workflow(["  "], OutputFormat.MARKDOWN)

    assert not plan.has_sources
