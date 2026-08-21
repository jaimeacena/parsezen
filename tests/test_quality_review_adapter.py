import logging
from dataclasses import replace
from pathlib import Path

import pytest

from parsezen.application.quality_review_adapter import (
    apply_pdf_review,
    apply_translation_review,
    create_pdf_review,
    create_translation_review,
    ensure_quality_reviews_applied,
)
from parsezen.domain.reviews import ReviewChoice, ReviewKind, ReviewSession, ReviewUnit
from parsezen.domain.stages import StageKind
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.pdf_conversion import PdfQualityReport, PdfReviewIssue
from parsezen.review_projection import project_review_text
from parsezen.translation_quality import (
    TranslationIssueKind,
    TranslationQualityIssue,
    TranslationQualityReport,
    build_translation_quality_report,
)


def reversible(payload: bytes) -> bytes:
    return bytes(value ^ 0x17 for value in payload)


def test_translation_review_applies_manual_excerpt_without_touching_other_text(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    input_record = store.put_text(job_id="job", text="Hello. Hola. End.")
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        6,
        5,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar este segmento",
                "Hello.",
                "Hola.",
                "segment",
            ),
        ),
    )
    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )
    assert review is not None
    edited = store.put_text(job_id="job", text="Buenas.")
    review = review.decide(
        review.units[0].id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_translation_review("Start. Hola. End.", review, store) == ("Start. Buenas. End.")


def test_translation_review_anchors_collapsed_whitespace_to_the_real_document(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    document = "Start.\n\nHola\nmundo.\n\nEnd."
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        6,
        5,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Revisar este segmento",
                "Hello world.",
                "Hola mundo.",
                "segment",
            ),
        ),
    )
    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    assert store.read_text("job", review.units[0].proposed_artifact_id or "") == "Hola\nmundo."
    edited = store.put_text(job_id="job", text="Buenas, mundo.")
    review = review.decide(
        review.units[0].id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )
    assert apply_translation_review(document, review, store) == ("Start.\n\nBuenas, mundo.\n\nEnd.")


@pytest.mark.parametrize("marker", ["…", "..."])
def test_translation_review_completes_only_the_truncated_sentence(
    tmp_path: Path,
    marker: str,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    paragraph = (
        "Este párrafo traducido es deliberadamente largo y continúa en otra línea "
        "para que el informe solo conserve un prefijo.\n"
        "La segunda línea también forma parte del mismo bloque."
    )
    document = f"Inicio.\n\n{paragraph}\n\nFin."
    input_record = store.put_text(job_id="job", text=document)
    prefix = "Este párrafo traducido es deliberadamente largo"
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        20,
        19,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.FIDELITY,
                "Revisar párrafo",
                "Long source excerpt",
                f"{prefix}{marker}",
                "long-block",
            ),
        ),
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    unit = review.units[0]
    assert not unit.original_selectable
    first_sentence = paragraph.splitlines()[0]
    assert store.read_text("job", unit.proposed_artifact_id or "") == first_sentence
    assert len(first_sentence) < len(paragraph)
    edited = store.put_text(job_id="job", text="Edición completa y segura.")
    decided = review.decide(
        unit.id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )
    assert apply_translation_review(document, decided, store) == (
        "Inicio.\n\nEdición completa y segura.\n"
        "La segunda línea también forma parte del mismo bloque.\n\nFin."
    )


def test_translation_review_materializes_multiple_non_overlapping_long_blocks(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    first = "Primer párrafo traducido con bastante contenido y una continuación segura."
    second = "Segundo párrafo traducido separado para mantener un anclaje independiente."
    document = f"{first}\n\n{second}\n\nFinal."
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        2,
        30,
        28,
        2,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Primero",
                "First source",
                "Primer párrafo traducido con bastante…",
                "first",
            ),
            TranslationQualityIssue(
                2,
                TranslationIssueKind.FIDELITY,
                "Segundo",
                "Second source",
                "Segundo párrafo traducido separado…",
                "second",
            ),
        ),
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    assert {unit.id.split("-", 1)[0] for unit in review.units} == {"first", "second"}
    assert {
        unit.id.split("-", 1)[0]: store.read_text("job", unit.proposed_artifact_id or "")
        for unit in review.units
    } == {"first": first, "second": second}


def test_length_review_exposes_the_suspicious_pdf_tail_without_the_next_page(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    suspicious = (
        "Inicio traducido fielmente.\n\n"
        "Contenido ajeno añadido por el modelo.\n\n"
        "Otra continuación ajena que también debe quedar visible."
    )
    document = (
        "<!-- PZDOC PDF PAGE 16 -->\n\n"
        f"{suspicious}\n\n"
        "<!-- PZDOC PDF PAGE 17 -->\n\n"
        "Contenido correcto de la página siguiente."
    )
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        2,
        20,
        45,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.LENGTH,
                "La traducción añadió demasiado contenido",
                "Faithful source beginning…",
                "Inicio traducido fielmente.…",
                "length-tail",
            ),
        ),
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    proposal = store.read_text("job", review.units[0].proposed_artifact_id or "")
    assert proposal == suspicious
    assert "página siguiente" not in proposal


def test_review_replacement_preserves_structural_boundary_whitespace(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="\n\nOld text\n\n")
    proposal = store.put_text(job_id="job", text="\n\nOld text\n\n")
    edited = store.put_text(job_id="job", text="New text")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("unit", original.id, proposal.id),),
    ).decide("unit", ReviewChoice.EDITED, edited_artifact_id=edited.id)

    assert apply_translation_review("Before\n\nOld text\n\nAfter", review, store) == (
        "Before\n\nNew text\n\nAfter"
    )


def test_translation_review_shows_a_readable_context_and_changes_identity_with_scope(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    document = (
        "The current result starts here and continues until this complete sentence. "
        "Unrelated translated content must stay outside the review."
    )
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        100,
        100,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.SOURCE_TEXT,
                "Parece conservar texto original",
                "The source context is complete. Another unfinished senten…",
                "The current result starts here and contin…",
                "legacy-id",
            ),
        ),
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    unit = review.units[0]
    assert unit.id.startswith("legacy-id-")
    assert unit.recommended_choice is None
    assert unit.warning is not None
    assert store.read_text("job", unit.original_artifact_id) == "The source context is complete. …"
    assert store.read_text("job", unit.proposed_artifact_id or "") == (
        "The current result starts here and continues until this complete sentence."
    )


def test_future_quality_excerpts_stop_at_the_last_complete_sentence(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    document = "A complete opening sentence. " + "unfinished context " * 30
    input_record = store.put_text(job_id="job", text=document)
    report = build_translation_quality_report(
        document,
        document,
        source_language="en",
        target_language="es",
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert report.issues[0].translated_excerpt == "A complete opening sentence.…"
    assert review is not None
    assert store.read_text("job", review.units[0].proposed_artifact_id or "") == (
        "A complete opening sentence."
    )


def test_legacy_truncated_translation_uses_the_same_complete_boundary_as_context(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    first = "The first complete sentence ends here."
    document = f"{first} A second sentence continues beyond the old report limit."
    input_record = store.put_text(job_id="job", text=document)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        len(document),
        len(document),
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.SOURCE_TEXT,
                "Parece conservar texto original",
                f"{first} A second senten…",
                f"{first} A second senten…",
                "legacy-boundary",
            ),
        ),
    )

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    unit = review.units[0]
    assert store.read_text("job", unit.original_artifact_id) == f"{first} …"
    assert store.read_text("job", unit.proposed_artifact_id or "") == first
    assert "únicamente todo el texto visible" in (unit.warning or "")


def test_unanchored_translation_issue_logs_only_a_sanitized_count(
    tmp_path: Path,
    caplog,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    private_text = "Contenido privado que nunca debe aparecer en el registro."
    input_record = store.put_text(job_id="job", text=private_text)
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        10,
        9,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.ALIGNMENT,
                "Incidencia sin anclaje",
                "Fuente privada",
                "Fragmento que no existe…",
                "missing",
            ),
        ),
    )
    caplog.set_level(logging.WARNING, logger="parsezen.application.quality_review_adapter")

    review = create_translation_review(
        report,
        job_id="job",
        configuration_revision=1,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is None
    assert "translation_review_issue_not_materialized count=1 high=0" in caplog.text
    assert private_text not in caplog.text
    assert "Fuente privada" not in caplog.text


def test_unanchored_high_severity_translation_issue_blocks_review_materialization(
    tmp_path: Path,
    caplog,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    input_record = store.put_text(job_id="job", text="Texto candidato distinto.")
    report = TranslationQualityReport(
        "en",
        "Español",
        "es",
        1,
        10,
        9,
        1,
        (
            TranslationQualityIssue(
                1,
                TranslationIssueKind.FIDELITY,
                "Incidencia importante sin anclaje",
                "Private source",
                "Missing candidate…",
                "missing-high",
            ),
        ),
    )
    caplog.set_level(logging.WARNING, logger="parsezen.application.quality_review_adapter")

    with pytest.raises(ValueError, match="incidencia importante"):
        create_translation_review(
            report,
            job_id="job",
            configuration_revision=1,
            input_artifact_id=input_record.id,
            artifacts=store,
        )

    assert "translation_review_issue_not_materialized count=1 high=1" in caplog.text
    assert "Private source" not in caplog.text
    assert "Missing candidate" not in caplog.text


def test_saved_review_tolerates_line_ending_normalization_when_applied(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Hello world.")
    proposal = store.put_text(job_id="job", text="Hola\r\nmundo.")
    edited = store.put_text(job_id="job", text="Buenas, mundo.")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("segment", original.id, proposal.id),),
    ).decide(
        "segment",
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_translation_review("Inicio.\nHola\nmundo.\nFin.", review, store) == (
        "Inicio.\nBuenas, mundo.\nFin."
    )


def test_pdf_review_uses_page_image_and_applies_edited_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    input_record = store.put_text(job_id="job", text="<!-- page -->\nOld OCR")
    report = PdfQualityReport(
        (1,),
        (1,),
        (
            PdfReviewIssue(
                1,
                "OCR dudoso",
                "Old OCR",
                "page-one",
                True,
                "<!-- page -->",
            ),
        ),
    )
    monkeypatch.setattr(
        "parsezen.application.quality_review_adapter.render_pdf_page_cover",
        lambda *_args: b"jpeg-page",
    )

    review = create_pdf_review(
        report,
        source,
        job_id="job",
        configuration_revision=2,
        input_artifact_id=input_record.id,
        artifacts=store,
    )
    assert review is not None
    assert review.kind is ReviewKind.OCR
    assert not review.units[0].original_selectable
    edited = store.put_text(job_id="job", text="Corrected OCR")
    decided = review.decide(
        review.units[0].id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    )

    assert apply_pdf_review("<!-- page -->\nOld OCR", decided, store) == (
        "<!-- page -->\nCorrected OCR"
    )


def test_applied_quality_decision_is_restored_after_downstream_rendering(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Source")
    proposal = store.put_text(job_id="job", text="Crea tu visión")
    edited = store.put_text(job_id="job", text="CREA TU VISIÓN")
    review = (
        ReviewSession.create(
            job_id="job",
            stage=StageKind.TRANSLATE,
            kind=ReviewKind.TRANSLATION,
            input_artifact_id="input",
            input_version=1,
            units=(ReviewUnit("heading", original.id, proposal.id),),
        )
        .decide(
            "heading",
            ReviewChoice.EDITED,
            edited_artifact_id=edited.id,
        )
        .apply()
    )

    restored = ensure_quality_reviews_applied("# Crea tu visión\n", (review,), store)

    assert restored == "# CREA TU VISIÓN\n"
    assert ensure_quality_reviews_applied(restored, (review,), store) == restored


def test_missing_applied_quality_decision_blocks_publication(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Source")
    proposal = store.put_text(job_id="job", text="Old proposal")
    edited = store.put_text(job_id="job", text="Human decision")
    review = (
        ReviewSession.create(
            job_id="job",
            stage=StageKind.TRANSLATE,
            kind=ReviewKind.TRANSLATION,
            input_artifact_id="input",
            input_version=1,
            units=(ReviewUnit("segment", original.id, proposal.id),),
        )
        .decide(
            "segment",
            ReviewChoice.EDITED,
            edited_artifact_id=edited.id,
        )
        .apply()
    )

    with pytest.raises(ValueError, match="no coincide"):
        ensure_quality_reviews_applied("Unrelated final text", (review,), store)


def test_edited_ocr_page_overrides_a_later_revision_of_the_same_page(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    target = "<!-- PZDOC PDF PAGE 1 -->"
    proposal_text = f"{target}\n\n# Crea tu visión\n\n"
    edited_text = f"{target}\n\n# CREA TU VISIÓN\n\n"
    original = store.put_text(job_id="job", text="page image")
    proposal = store.put_text(job_id="job", text=proposal_text)
    edited = store.put_text(job_id="job", text=edited_text)
    review = (
        ReviewSession.create(
            job_id="job",
            stage=StageKind.PREPARE,
            kind=ReviewKind.OCR,
            input_artifact_id="input",
            input_version=1,
            units=(
                ReviewUnit(
                    "page-one",
                    original.id,
                    proposal.id,
                    original_selectable=False,
                    target=target,
                ),
            ),
        )
        .decide(
            "page-one",
            ReviewChoice.EDITED,
            edited_artifact_id=edited.id,
        )
        .apply()
    )
    rendered = f"{target}\n\n# Crear una visión\n\n<!-- PZDOC PDF PAGE 2 -->\n\nTexto estable.\n"

    reconciled = ensure_quality_reviews_applied(rendered, (review,), store)

    assert reconciled.startswith(edited_text)
    assert "<!-- PZDOC PDF PAGE 2 -->\n\nTexto estable." in reconciled


def test_pdf_review_anchors_multiple_issues_to_the_transformed_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF")
    document = (
        "<!-- PZDOC PDF PAGE 1 -->\n\n"
        "Texto ya traducido.\n\n"
        "<!-- PZDOC PDF PAGE 2 -->\n\n"
        "![](<__parsezen_resources__/pdf/page-0002-image-01.jpg>)\n"
    )
    input_record = store.put_text(job_id="job", text=document)
    report = PdfQualityReport(
        (1, 2),
        (1, 2),
        (
            PdfReviewIssue(
                1,
                "OCR dudoso",
                "Stale source text.",
                "page-one",
                False,
                "<!-- PZDOC PDF PAGE 1 -->",
            ),
            PdfReviewIssue(
                2,
                "Página visual",
                "",
                "page-two",
                False,
                "<!-- PZDOC PDF PAGE 2 -->",
            ),
        ),
    )
    monkeypatch.setattr(
        "parsezen.application.quality_review_adapter.render_pdf_page_cover",
        lambda *_args: b"jpeg-page",
    )

    review = create_pdf_review(
        report,
        source,
        job_id="job",
        configuration_revision=2,
        input_artifact_id=input_record.id,
        artifacts=store,
    )

    assert review is not None
    first, second = review.units
    assert first.proposed_artifact_id is not None
    assert second.proposed_artifact_id is not None
    first_proposal = store.read_text("job", first.proposed_artifact_id)
    second_proposal = store.read_text("job", second.proposed_artifact_id)
    assert "Texto ya traducido." in first_proposal
    assert "Stale source text." not in first_proposal
    assert project_review_text(second_proposal).visible_text.strip() == ""

    edited_text = project_review_text(first_proposal).restore("Texto ya corregido.\n\n")
    edited = store.put_text(job_id="job", text=edited_text)
    decided = review.decide(
        first.id,
        ReviewChoice.EDITED,
        edited_artifact_id=edited.id,
    ).decide(second.id, ReviewChoice.NO_TEXT)

    applied = apply_pdf_review(document, decided, store)
    assert "Texto ya corregido." in applied
    assert "Texto ya traducido." not in applied
    assert "<!-- PZDOC PDF PAGE 2 -->" in applied
    assert "page-0002-image-01.jpg" in applied


def test_quality_review_handles_empty_reports_targets_and_invalid_decisions(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    empty_translation = TranslationQualityReport("en", "Español", "es", 1, 1, 1, 0, ())
    assert (
        create_translation_review(
            empty_translation,
            job_id="job",
            configuration_revision=1,
            input_artifact_id="input",
            artifacts=store,
        )
        is None
    )
    assert (
        create_pdf_review(
            PdfQualityReport((), (), ()),
            tmp_path / "missing.pdf",
            job_id="job",
            configuration_revision=1,
            input_artifact_id="input",
            artifacts=store,
        )
        is None
    )

    original = store.put_text(job_id="job", text="image")
    proposal = store.put_text(job_id="job", text="")
    unit = ReviewUnit(
        "unit",
        original.id,
        proposal.id,
        original_selectable=False,
        target="<!-- marker -->",
    )
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id="input",
        input_version=1,
        units=(unit,),
    )
    edited = store.put_text(job_id="job", text="Inserted")
    decided = review.decide("unit", ReviewChoice.EDITED, edited_artifact_id=edited.id)
    assert apply_pdf_review("<!-- marker -->", decided, store) == ("<!-- marker -->\n\nInserted")

    with pytest.raises(ValueError, match="Decide todos"):
        apply_pdf_review("<!-- marker -->", review, store)
    with pytest.raises(ValueError, match="ya no coincide"):
        apply_pdf_review("different", decided, store)


def test_quality_review_supports_original_proposed_and_defensive_failures(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="Original")
    proposal = store.put_text(job_id="job", text="Proposal")
    base_unit = ReviewUnit("unit", original.id, proposal.id)
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(base_unit,),
    )
    original_review = review.decide("unit", ReviewChoice.ORIGINAL)
    proposed_review = review.decide("unit", ReviewChoice.PROPOSED)

    assert apply_translation_review("Proposal", original_review, store) == "Original"
    assert apply_translation_review("Proposal", proposed_review, store) == "Proposal"

    image_only = replace(base_unit, original_selectable=False)
    invalid_original = replace(review, units=(replace(image_only, choice=ReviewChoice.ORIGINAL),))
    with pytest.raises(ValueError, match="imagen original"):
        apply_translation_review("Proposal", invalid_original, store)

    missing_edit = replace(review, units=(replace(base_unit, choice=ReviewChoice.EDITED),))
    with pytest.raises(ValueError, match="edición guardada"):
        apply_translation_review("Proposal", missing_edit, store)

    empty_proposal = store.put_text(job_id="job", text="")
    empty_review = replace(
        review,
        units=(
            replace(
                base_unit,
                proposed_artifact_id=empty_proposal.id,
                choice=ReviewChoice.PROPOSED,
            ),
        ),
    )
    with pytest.raises(ValueError, match="está vacío"):
        apply_translation_review("Proposal", empty_review, store)


def test_translation_review_accepts_a_choice_already_applied_by_ocr(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts", protect=reversible, unprotect=reversible)
    original = store.put_text(job_id="job", text="YO' MONEY")
    proposal = store.put_text(job_id="job", text="YO' MONEY")
    edited = store.put_text(job_id="job", text="TU DINERO")
    review = ReviewSession.create(
        job_id="job",
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(ReviewUnit("unit", original.id, proposal.id),),
    ).decide("unit", ReviewChoice.EDITED, edited_artifact_id=edited.id)

    assert apply_translation_review("## TU DINERO\n", review, store) == "## TU DINERO\n"
