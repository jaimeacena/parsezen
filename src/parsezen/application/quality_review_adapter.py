"""Create and materialize focused OCR and translation quality reviews."""

from __future__ import annotations

from pathlib import Path

from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.pdf_conversion import PdfQualityReport, render_pdf_page_cover
from parsezen.translation_quality import TranslationQualityReport


def create_translation_review(
    report: TranslationQualityReport,
    *,
    job_id: str,
    configuration_revision: int,
    input_artifact_id: str,
    artifacts: ArtifactStore,
) -> ReviewSession | None:
    if not report.issues:
        return None
    document_text = artifacts.read_text(job_id, input_artifact_id)
    units: list[ReviewUnit] = []
    occupied: list[tuple[int, int]] = []
    for index, issue in enumerate(report.issues, start=1):
        span = _find_review_span(
            document_text,
            issue.translated_excerpt,
            occupied=tuple(occupied),
        )
        if span is None:
            continue
        occupied.append(span)
        anchored_translation = document_text[slice(*span)]
        original = artifacts.put_text(
            job_id=job_id,
            text=issue.original_excerpt,
            media_type="text/plain; charset=utf-8",
        )
        translated = artifacts.put_text(
            job_id=job_id,
            text=anchored_translation,
            media_type="text/plain; charset=utf-8",
        )
        units.append(
            ReviewUnit(
                issue.identifier or f"translation-{index:04d}",
                original.id,
                translated.id,
                label=issue.message,
                severity=_translation_issue_severity(issue.kind.value),
            )
        )
    if not units:
        return None
    return ReviewSession.create(
        job_id=job_id,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id=input_artifact_id,
        input_version=configuration_revision,
        units=tuple(units),
    )


def create_pdf_review(
    report: PdfQualityReport,
    source_path: Path,
    *,
    job_id: str,
    configuration_revision: int,
    input_artifact_id: str,
    artifacts: ArtifactStore,
) -> ReviewSession | None:
    if not report.issues:
        return None
    units: list[ReviewUnit] = []
    for index, issue in enumerate(report.issues, start=1):
        page = artifacts.put(
            job_id=job_id,
            payload=render_pdf_page_cover(source_path, issue.page_number),
            media_type="image/jpeg",
        )
        converted = artifacts.put_text(
            job_id=job_id,
            text=issue.markdown,
            media_type="text/markdown; charset=utf-8",
        )
        units.append(
            ReviewUnit(
                issue.identifier or f"pdf-page-{issue.page_number}-{index}",
                page.id,
                converted.id,
                label=f"Página {issue.page_number} · {issue.message}",
                original_selectable=False,
                target=issue.target_marker or None,
                severity=(ReviewSeverity.HIGH if issue.blocking else ReviewSeverity.MEDIUM),
            )
        )
    return ReviewSession.create(
        job_id=job_id,
        stage=StageKind.PREPARE,
        kind=ReviewKind.OCR,
        input_artifact_id=input_artifact_id,
        input_version=configuration_revision,
        units=tuple(units),
    )


def _translation_issue_severity(kind: str) -> ReviewSeverity:
    if kind in {"source_text", "language", "fidelity"}:
        return ReviewSeverity.HIGH
    return ReviewSeverity.MEDIUM


def apply_translation_review(
    text: str,
    review: ReviewSession,
    artifacts: ArtifactStore,
) -> str:
    replacements: list[tuple[str, str]] = []
    for unit in review.units:
        proposed = (
            artifacts.read_text(review.job_id, unit.proposed_artifact_id)
            if unit.proposed_artifact_id is not None
            else ""
        )
        replacements.append(
            (
                proposed,
                _selected_text(review, unit, artifacts),
            )
        )
    return _apply_review_replacements(text, tuple(replacements))


def apply_pdf_review(
    text: str,
    review: ReviewSession,
    artifacts: ArtifactStore,
) -> str:
    replacements: list[tuple[str, str]] = []
    insertions: list[tuple[str, str]] = []
    for unit in review.units:
        if unit.proposed_artifact_id is None:
            continue
        proposed = artifacts.read_text(review.job_id, unit.proposed_artifact_id)
        replacement = _selected_text(review, unit, artifacts)
        if not proposed and unit.target:
            insertions.append((unit.target, replacement))
        elif proposed:
            replacements.append((proposed, replacement))
    updated = _apply_review_replacements(text, tuple(replacements))
    for target, replacement in insertions:
        if target not in updated:
            raise ValueError("La página revisada ya no coincide con el documento.")
        if replacement:
            updated = updated.replace(
                target,
                f"{target}\n\n{replacement}",
                1,
            )
    return updated


def _selected_text(
    review: ReviewSession,
    unit: ReviewUnit,
    artifacts: ArtifactStore,
) -> str:
    if unit.choice is ReviewChoice.EDITED:
        if unit.edited_artifact_id is None:
            raise ValueError("No se encontró la edición guardada de esta revisión.")
        return artifacts.read_text(review.job_id, unit.edited_artifact_id)
    if unit.choice is ReviewChoice.ORIGINAL:
        if not unit.original_selectable:
            raise ValueError("La imagen original no puede sustituir al texto convertido.")
        return artifacts.read_text(review.job_id, unit.original_artifact_id)
    if unit.choice is ReviewChoice.PROPOSED and unit.proposed_artifact_id is not None:
        return artifacts.read_text(review.job_id, unit.proposed_artifact_id)
    raise ValueError("Decide todos los elementos de la revisión antes de continuar.")


def _replace_review_excerpt(text: str, original: str, replacement: str) -> str:
    return _apply_review_replacements(text, ((original, replacement),))


def _apply_review_replacements(
    text: str,
    replacements: tuple[tuple[str, str], ...],
) -> str:
    """Resolve every decision against one immutable snapshot before editing.

    Qt normalizes line endings in editable review fields and quality excerpts
    can collapse whitespace. Exact sequential ``str.replace`` therefore made
    later decisions fail after an earlier replacement shifted or overlapped
    their text. Anchoring all spans first and applying them from right to left
    keeps the operation deterministic and prevents partial publication.
    """

    occupied: list[tuple[int, int]] = []
    planned: list[tuple[int, int, str]] = []
    for original, replacement in replacements:
        if not original:
            raise ValueError("El fragmento que se iba a revisar está vacío.")
        if original == replacement:
            continue
        span = _find_review_span(text, original, occupied=tuple(occupied))
        if span is None:
            raise ValueError("El fragmento revisado ya no coincide con el documento.")
        occupied.append(span)
        planned.append((span[0], span[1], replacement))

    updated = text
    for start, end, replacement in sorted(planned, reverse=True):
        updated = f"{updated[:start]}{replacement}{updated[end:]}"
    return updated


def _find_review_span(
    text: str,
    fragment: str,
    *,
    occupied: tuple[tuple[int, int], ...] = (),
) -> tuple[int, int] | None:
    if not fragment:
        return None

    offset = 0
    while True:
        start = text.find(fragment, offset)
        if start < 0:
            break
        span = (start, start + len(fragment))
        if not _overlaps(span, occupied):
            return span
        offset = start + max(1, len(fragment))

    normalized_text, source_spans = _normalized_text_with_spans(text)
    normalized_fragment, _fragment_spans = _normalized_text_with_spans(fragment)
    normalized_fragment = normalized_fragment.strip()
    if not normalized_fragment:
        return None
    offset = 0
    while True:
        start = normalized_text.find(normalized_fragment, offset)
        if start < 0:
            return None
        end_index = start + len(normalized_fragment) - 1
        if end_index < len(source_spans):
            span = (source_spans[start][0], source_spans[end_index][1])
            if not _overlaps(span, occupied):
                return span
        offset = start + max(1, len(normalized_fragment))


def _normalized_text_with_spans(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    normalized: list[str] = []
    spans: list[tuple[int, int]] = []
    whitespace = False
    for index, character in enumerate(text):
        if character.isspace():
            if whitespace:
                start, _end = spans[-1]
                spans[-1] = (start, index + 1)
                continue
            normalized.append(" ")
            spans.append((index, index + 1))
            whitespace = True
            continue
        normalized.append(character)
        spans.append((index, index + 1))
        whitespace = False
    return "".join(normalized), tuple(spans)


def _overlaps(
    candidate: tuple[int, int],
    occupied: tuple[tuple[int, int], ...],
) -> bool:
    return any(candidate[0] < end and start < candidate[1] for start, end in occupied)
