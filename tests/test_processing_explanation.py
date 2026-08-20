from __future__ import annotations

import pytest

from parsezen.application.processing_explanation import (
    HumanReviewPolicy,
    linguistic_review_summary,
    processing_flow,
    processing_flow_steps,
    processing_pass_summary,
    translation_route_summary,
)
from parsezen.domain.jobs import (
    DocumentFormat,
    JobConfiguration,
    OutputConfiguration,
    ProcessingPlan,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.translation_quality import LinguisticReviewCoverage, LinguisticReviewMode


@pytest.mark.parametrize(
    ("method", "reviewed", "epub", "expected"),
    (
        (TranslationMethod.OFFLINE, False, False, "Argos traduce sin Ollama"),
        (TranslationMethod.OFFLINE, True, False, "independiente (2 pasadas)"),
        (TranslationMethod.OFFLINE, True, True, "estructura (3 pasadas)"),
        (TranslationMethod.LOCAL_AI, False, False, "Coste aproximado medio"),
        (TranslationMethod.LOCAL_AI, True, False, "no es una verificación"),
        (TranslationMethod.LOCAL_AI, True, True, "segunda verificación bilingüe"),
    ),
)
def test_translation_route_explains_every_effective_path(
    method: TranslationMethod,
    reviewed: bool,
    epub: bool,
    expected: str,
) -> None:
    assert expected in translation_route_summary(method, reviewed=reviewed, epub=epub)


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (LinguisticReviewMode.NOT_REVIEWED, "Sin revisión semántica posterior"),
        (LinguisticReviewMode.CORRECTED_DURING_TRANSLATION, "durante la traducción"),
        (LinguisticReviewMode.INDEPENDENT_BILINGUAL, "bilingüe independiente"),
        (LinguisticReviewMode.TARGETED_BILINGUAL, "se limitó a bloques"),
    ),
)
def test_linguistic_summary_names_mode_and_complete_coverage(
    mode: LinguisticReviewMode,
    expected: str,
) -> None:
    coverage = LinguisticReviewCoverage(mode, 7, 6, 5, 4, 2)

    summary = linguistic_review_summary(coverage)

    assert summary is not None
    assert expected in summary
    assert "6 bloques comprobados" in summary
    assert "2 sin revisión semántica" in summary
    assert "2 incidencias pendientes" in summary


def test_linguistic_summary_omits_missing_translation_coverage() -> None:
    assert linguistic_review_summary(None) is None


@pytest.mark.parametrize(
    ("source", "configuration", "flow_fragment", "pass_fragment"),
    (
        (
            DocumentFormat.PDF,
            JobConfiguration(),
            "PDF",
            "Sin pasadas de IA",
        ),
        (
            DocumentFormat.PDF,
            JobConfiguration(
                output=OutputConfiguration(format=DocumentFormat.EPUB),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            "Organizar EPUB con IA local",
            "2 pasadas de IA",
        ),
        (
            DocumentFormat.PDF,
            JobConfiguration(plan=ProcessingPlan.LOCAL_AI_REVIEWED),
            "Corregir contenido con IA local",
            "1 pasada de IA para la revisión semántica",
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                translation=TranslationConfiguration(True, TranslationMethod.OFFLINE, "es"),
            ),
            "Traducir con Argos a español",
            "1 pasada de traducción con Argos",
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                translation=TranslationConfiguration(True, TranslationMethod.OFFLINE, "es"),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            "Verificar traducción con IA local",
            "2 pasadas: Argos",
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                output=OutputConfiguration(format=DocumentFormat.EPUB),
                translation=TranslationConfiguration(True, TranslationMethod.OFFLINE, "es"),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            "Verificar traducción con IA local",
            "3 pasadas",
        ),
        (
            DocumentFormat.TEXT,
            JobConfiguration(
                translation=TranslationConfiguration(True, TranslationMethod.LOCAL_AI, "Klingon"),
            ),
            "Traducir con IA local a Klingon",
            "1 pasada de IA para traducir",
        ),
        (
            DocumentFormat.MARKDOWN,
            JobConfiguration(
                translation=TranslationConfiguration(True, TranslationMethod.LOCAL_AI, None),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            "Traducir y corregir con IA local",
            "combina traducción y corrección",
        ),
        (
            DocumentFormat.MARKDOWN,
            JobConfiguration(
                output=OutputConfiguration(format=DocumentFormat.EPUB),
                translation=TranslationConfiguration(True, TranslationMethod.LOCAL_AI, "es"),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            "Organizar EPUB con IA local",
            "2 pasadas de IA: traducción y corrección",
        ),
        (
            DocumentFormat.EPUB,
            JobConfiguration(output=OutputConfiguration(format=DocumentFormat.EPUB)),
            "Personalizar EPUB",
            "Sin pasadas de IA",
        ),
    ),
)
def test_flow_and_pass_summary_cover_product_routes(
    source: DocumentFormat,
    configuration: JobConfiguration,
    flow_fragment: str,
    pass_fragment: str,
) -> None:
    assert flow_fragment in processing_flow_steps(source, configuration)
    assert pass_fragment in processing_pass_summary(configuration)


@pytest.mark.parametrize(
    ("source", "configuration", "steps", "review_policy", "review_note"),
    (
        (
            DocumentFormat.PDF,
            JobConfiguration(),
            ("Convertir",),
            HumanReviewPolicy.NONE,
            None,
        ),
        (
            DocumentFormat.PDF,
            JobConfiguration(force_pdf_ocr=True),
            ("OCR", "Convertir"),
            HumanReviewPolicy.NONE,
            None,
        ),
        (
            DocumentFormat.MARKDOWN,
            JobConfiguration(plan=ProcessingPlan.LOCAL_AI_REVIEWED),
            ("Corregir contenido",),
            HumanReviewPolicy.IF_CHANGES,
            "Tu revisión si hay cambios",
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                translation=TranslationConfiguration(
                    True,
                    TranslationMethod.LOCAL_AI,
                    "es",
                )
            ),
            ("Traducir a español",),
            HumanReviewPolicy.NONE,
            None,
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                translation=TranslationConfiguration(
                    True,
                    TranslationMethod.LOCAL_AI,
                    "es",
                ),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            ("Traducir y corregir a español",),
            HumanReviewPolicy.IF_CHANGES,
            "Tu revisión si hay cambios",
        ),
        (
            DocumentFormat.DOCX,
            JobConfiguration(
                translation=TranslationConfiguration(
                    True,
                    TranslationMethod.OFFLINE,
                    "es",
                ),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            ("Traducir a español", "Verificar traducción"),
            HumanReviewPolicy.IF_CHANGES,
            "Tu revisión si hay cambios",
        ),
        (
            DocumentFormat.PDF,
            JobConfiguration(
                output=OutputConfiguration(format=DocumentFormat.EPUB),
                translation=TranslationConfiguration(
                    True,
                    TranslationMethod.LOCAL_AI,
                    "es",
                ),
                plan=ProcessingPlan.LOCAL_AI_REVIEWED,
            ),
            ("Traducir y corregir a español", "Organizar EPUB"),
            HumanReviewPolicy.BEFORE_PUBLISHING,
            "Tu revisión antes de publicar",
        ),
        (
            DocumentFormat.EPUB,
            JobConfiguration(output=OutputConfiguration(format=DocumentFormat.EPUB)),
            ("Personalizar EPUB",),
            HumanReviewPolicy.BEFORE_PUBLISHING,
            "Tu revisión antes de publicar",
        ),
    ),
)
def test_compact_flow_uses_one_vocabulary_and_explicit_human_policy(
    source: DocumentFormat,
    configuration: JobConfiguration,
    steps: tuple[str, ...],
    review_policy: HumanReviewPolicy,
    review_note: str | None,
) -> None:
    flow = processing_flow(source, configuration)

    assert flow.compact_steps == steps
    assert flow.human_review is review_policy
    assert flow.human_review_note == review_note
