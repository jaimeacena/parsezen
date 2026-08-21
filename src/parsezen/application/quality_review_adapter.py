"""Create and materialize focused OCR and translation quality reviews."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from parsezen.application.artifact_repository import ArtifactRepository
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind
from parsezen.pdf_conversion import PdfQualityReport, render_pdf_page_cover
from parsezen.review_projection import private_review_content
from parsezen.translation_quality import TranslationQualityReport

LOGGER = logging.getLogger(__name__)
_PDF_PAGE_MARKER_PATTERN = re.compile(
    r"(?m)^<!--\s*PZDOC PDF PAGE \d+\s*-->[ \t]*(?:\r?\n|$)",
    re.IGNORECASE,
)
_TRANSLATION_REVIEW_MAX_CHARACTERS = 640
_TRANSLATION_LENGTH_REVIEW_MAX_CHARACTERS = 4_000


def create_translation_review(
    report: TranslationQualityReport,
    *,
    job_id: str,
    configuration_revision: int,
    input_artifact_id: str,
    artifacts: ArtifactRepository,
) -> ReviewSession | None:
    if not report.issues:
        return None
    document_text = artifacts.read_text(job_id, input_artifact_id)
    units: list[ReviewUnit] = []
    occupied: list[tuple[int, int]] = []
    skipped_count = 0
    skipped_high_severity_count = 0
    for index, issue in enumerate(report.issues, start=1):
        original_context = _readable_translation_context(issue.original_excerpt)
        translated_context = _readable_translation_context(issue.translated_excerpt)
        span = _find_translation_span(
            document_text,
            translated_context,
            occupied=tuple(occupied),
            expand_length_issue=issue.kind.value == "length",
        )
        if span is None:
            skipped_count += 1
            unoccupied_span = _find_translation_span(
                document_text,
                translated_context,
                occupied=(),
                expand_length_issue=issue.kind.value == "length",
            )
            if (
                unoccupied_span is None
                and _translation_issue_severity(issue.kind.value) is ReviewSeverity.HIGH
            ):
                skipped_high_severity_count += 1
            continue
        occupied.append(span)
        anchored_translation = document_text[slice(*span)]
        original = artifacts.put_text(
            job_id=job_id,
            text=original_context,
            media_type="text/plain; charset=utf-8",
        )
        translated = artifacts.put_text(
            job_id=job_id,
            text=anchored_translation,
            media_type="text/plain; charset=utf-8",
        )
        base_identifier = issue.identifier or f"translation-{index:04d}"
        content_identifier = hashlib.sha256(anchored_translation.encode("utf-8")).hexdigest()[:12]
        units.append(
            ReviewUnit(
                f"{base_identifier}-{content_identifier}",
                original.id,
                translated.id,
                label=issue.message,
                original_selectable=False,
                warning=_translation_review_warning(issue.kind.value),
                severity=_translation_issue_severity(issue.kind.value),
            )
        )
    if skipped_count:
        LOGGER.warning(
            "translation_review_issue_not_materialized count=%d high=%d",
            skipped_count,
            skipped_high_severity_count,
        )
    if skipped_high_severity_count:
        raise ValueError(
            "El informe detectó una incidencia importante, pero su fragmento ya no coincide "
            "con el texto revisable. Se ha conservado el resultado sin aplicar decisiones."
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
    artifacts: ArtifactRepository,
) -> ReviewSession | None:
    if not report.issues:
        return None
    document_text = artifacts.read_text(job_id, input_artifact_id)
    units: list[ReviewUnit] = []
    for index, issue in enumerate(report.issues, start=1):
        page = artifacts.put(
            job_id=job_id,
            payload=render_pdf_page_cover(source_path, issue.page_number),
            media_type="image/jpeg",
        )
        review_text = (
            _reviewable_pdf_page(
                document_text,
                issue.target_marker,
            )
            or issue.markdown
        )
        converted = artifacts.put_text(
            job_id=job_id,
            text=review_text,
            media_type="text/markdown; charset=utf-8",
        )
        base_identifier = issue.identifier or f"pdf-page-{issue.page_number}-{index}"
        content_identifier = hashlib.sha256(review_text.encode("utf-8")).hexdigest()[:12]
        units.append(
            ReviewUnit(
                f"{base_identifier}-{content_identifier}",
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


def _reviewable_pdf_page(document_text: str, target_marker: str) -> str | None:
    """Return one exact transformed page so review edits match the final document."""

    span = _pdf_page_span(document_text, target_marker)
    return document_text[slice(*span)] if span is not None else None


def _pdf_page_span(document_text: str, target_marker: str) -> tuple[int, int] | None:
    if not target_marker:
        return None
    marker_start = document_text.find(target_marker)
    if marker_start < 0:
        return None
    marker_match = _PDF_PAGE_MARKER_PATTERN.match(document_text, marker_start)
    if marker_match is None:
        return None
    next_marker = _PDF_PAGE_MARKER_PATTERN.search(document_text, marker_match.end())
    page_end = next_marker.start() if next_marker is not None else len(document_text)
    return marker_start, page_end


def _translation_issue_severity(kind: str) -> ReviewSeverity:
    if kind in {"source_text", "language", "fidelity"}:
        return ReviewSeverity.HIGH
    return ReviewSeverity.MEDIUM


def _translation_review_warning(kind: str) -> str | None:
    if kind == "source_text":
        return (
            "Este fragmento parece conservar texto en el idioma original. "
            "Traduce únicamente todo el texto visible del panel derecho; "
            "no necesitas continuar fuera del fragmento."
        )
    if kind == "language":
        return "Comprueba que todo el fragmento esté escrito en el idioma del resultado."
    return None


def apply_translation_review(
    text: str,
    review: ReviewSession,
    artifacts: ArtifactRepository,
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
    artifacts: ArtifactRepository,
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


def ensure_quality_reviews_applied(
    text: str,
    reviews: tuple[ReviewSession, ...],
    artifacts: ArtifactRepository,
) -> str:
    """Reconcile durable human decisions after downstream draft rendering.

    Quality decisions are first applied before refinement and structure review.
    Those later phases render a new immutable revision snapshot, so a stale or
    incomplete snapshot must never be allowed to silently discard a decision
    that the user already confirmed. This pass is deliberately idempotent: it
    applies a missing decision, accepts one that is already present, and blocks
    publication when neither the reviewed nor the previous fragment can be
    anchored safely.
    """

    reviewed_text = text
    for review in reviews:
        if review.status is not ReviewStatus.APPLIED or review.kind not in {
            ReviewKind.OCR,
            ReviewKind.TRANSLATION,
        }:
            continue
        reviewed_text = _ensure_review_decisions_applied(
            reviewed_text,
            review,
            artifacts,
        )
    return reviewed_text


def _ensure_review_decisions_applied(
    text: str,
    review: ReviewSession,
    artifacts: ArtifactRepository,
) -> str:
    occupied: list[tuple[int, int]] = []
    planned: list[tuple[int, int, str]] = []
    insertions: list[tuple[int, str]] = []
    for unit in review.units:
        # Accepting the generated candidate introduces no human-authored delta.
        # Later refinement or structure choices may legitimately transform it.
        if unit.choice is ReviewChoice.PROPOSED:
            continue
        proposed = (
            artifacts.read_text(review.job_id, unit.proposed_artifact_id)
            if unit.proposed_artifact_id is not None
            else ""
        )
        selected = _selected_text(review, unit, artifacts)
        if proposed == selected:
            continue

        if selected:
            selected_span = _find_review_span(text, selected, occupied=tuple(occupied))
            if selected_span is not None:
                occupied.append(selected_span)
                continue

        if review.kind is ReviewKind.OCR and unit.target:
            page_span = _pdf_page_span(text, unit.target)
            if page_span is not None and not _overlaps(page_span, tuple(occupied)):
                occupied.append(page_span)
                planned.append((*page_span, selected or unit.target))
                continue

        if proposed:
            proposed_span = _find_review_span(text, proposed, occupied=tuple(occupied))
            if proposed_span is not None:
                occupied.append(proposed_span)
                planned.append((*proposed_span, selected))
                continue
        elif not selected:
            continue

        if not proposed and unit.target:
            if not selected:
                continue
            target_span = _find_review_span(text, unit.target, occupied=tuple(occupied))
            if target_span is not None:
                occupied.append(target_span)
                insertions.append((target_span[1], f"\n\n{selected}"))
                continue
        raise ValueError(
            "Una decisión de revisión ya no coincide con el documento. "
            "El resultado no se ha publicado."
        )

    updated = text
    operations = [*planned, *((position, position, value) for position, value in insertions)]
    for start, end, replacement in sorted(operations, reverse=True):
        updated = f"{updated[:start]}{replacement}{updated[end:]}"
    return updated


def _selected_text(
    review: ReviewSession,
    unit: ReviewUnit,
    artifacts: ArtifactRepository,
) -> str:
    if unit.choice is ReviewChoice.EDITED:
        if unit.edited_artifact_id is None:
            raise ValueError("No se encontró la edición guardada de esta revisión.")
        return artifacts.read_text(review.job_id, unit.edited_artifact_id)
    if unit.choice is ReviewChoice.ORIGINAL:
        if not unit.original_selectable:
            raise ValueError("La imagen original no puede sustituir al texto convertido.")
        return artifacts.read_text(review.job_id, unit.original_artifact_id)
    if unit.choice is ReviewChoice.NO_TEXT:
        if review.kind is not ReviewKind.OCR or unit.proposed_artifact_id is None:
            raise ValueError("La opción de texto vacío solo está disponible para OCR.")
        return private_review_content(
            artifacts.read_text(review.job_id, unit.proposed_artifact_id),
        )
    if unit.choice is ReviewChoice.PROPOSED:
        if not unit.proposed_selectable or unit.proposed_artifact_id is None:
            raise ValueError("La propuesta de esta revisión no se puede seleccionar.")
        return artifacts.read_text(review.job_id, unit.proposed_artifact_id)
    raise ValueError("Decide todos los elementos de la revisión antes de continuar.")


def _replace_review_excerpt(text: str, original: str, replacement: str) -> str:
    return _apply_review_replacements(text, ((original, replacement),))


def _find_translation_span(
    text: str,
    excerpt: str,
    *,
    occupied: tuple[tuple[int, int], ...] = (),
    expand_length_issue: bool = False,
) -> tuple[int, int] | None:
    """Anchor one readable fragment and expose the full suspicious expansion when needed."""

    prefix, truncated = _truncated_excerpt_prefix(excerpt)
    match = _find_review_span(text, prefix if truncated else excerpt, occupied=occupied)
    if match is None:
        return None
    if expand_length_issue:
        return _complete_translation_length_span(text, match, occupied=occupied)
    if not truncated or prefix.rstrip().endswith((".", "!", "?")):
        return match
    return _complete_translation_span(text, match, occupied=occupied)


def _complete_translation_length_span(
    text: str,
    span: tuple[int, int],
    *,
    occupied: tuple[tuple[int, int], ...],
) -> tuple[int, int]:
    """Keep an excessive translation editable through its local block or PDF page."""

    limit = min(len(text), span[0] + _TRANSLATION_LENGTH_REVIEW_MAX_CHARACTERS)
    marker = _PDF_PAGE_MARKER_PATTERN.search(text, span[1], limit)
    if marker is not None:
        limit = marker.start()
    elif "<!-- PZDOC PDF PAGE " not in text:
        paragraph_end = text.find("\n\n", span[1], limit)
        if paragraph_end >= 0:
            limit = paragraph_end
    while limit > span[1] and text[limit - 1].isspace():
        limit -= 1
    completed = (span[0], limit)
    return span if _overlaps(completed, occupied) else completed


def _truncated_excerpt_prefix(excerpt: str) -> tuple[str, bool]:
    compact = excerpt.rstrip()
    if compact.endswith("…"):
        prefix = _drop_possible_partial_word(compact[:-1].rstrip())
        return prefix, bool(prefix)
    if compact.endswith("..."):
        prefix = _drop_possible_partial_word(compact[:-3].rstrip())
        return prefix, bool(prefix)
    return excerpt, False


def _drop_possible_partial_word(prefix: str) -> str:
    """Remove only the final token of an explicitly truncated preview."""

    if not prefix or prefix[-1] in ".!?;:":
        return prefix
    head, separator, _tail = prefix.rpartition(" ")
    return head.rstrip() if separator and head.strip() else prefix


def _complete_translation_span(
    text: str,
    span: tuple[int, int],
    *,
    occupied: tuple[tuple[int, int], ...],
) -> tuple[int, int]:
    """Finish the current sentence while keeping the editable fragment bounded."""

    limit = min(len(text), span[0] + _TRANSLATION_REVIEW_MAX_CHARACTERS)
    paragraph_end = text.find("\n\n", span[1], limit)
    if paragraph_end >= 0:
        limit = paragraph_end
    marker_end = text.find("<!-- PZDOC PDF PAGE ", span[1], limit)
    if marker_end >= 0:
        limit = marker_end
    ending = re.search(r"[.!?](?=\s|$)", text[span[1] : limit])
    completed = (span[0], span[1] + ending.end()) if ending is not None else span
    return span if _overlaps(completed, occupied) else completed


def _readable_translation_context(excerpt: str) -> str:
    """Present a bounded report preview at a sentence or word boundary."""

    compact = excerpt.strip()
    marker = "…" if compact.endswith("…") else "..." if compact.endswith("...") else ""
    if not marker:
        return compact
    body = compact[: -len(marker)].rstrip()
    sentence_ends = tuple(re.finditer(r"[.!?](?=\s|$)", body))
    if sentence_ends:
        body = body[: sentence_ends[-1].end()].rstrip()
    else:
        body = _drop_possible_partial_word(body)
    return f"{body} …" if body else compact


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
            # An earlier review phase can legitimately replace the same source
            # fragment (for example, OCR before translation). Treat the later
            # decision as already applied when its exact selected text is now
            # present, while reserving that occurrence for this unit.
            existing = _find_review_span(
                text,
                replacement,
                occupied=tuple(occupied),
            )
            if replacement and existing is not None:
                occupied.append(existing)
                continue
            raise ValueError("El fragmento revisado ya no coincide con el documento.")
        occupied.append(span)
        planned.append(
            (
                span[0],
                span[1],
                _preserve_review_boundary_whitespace(text[slice(*span)], replacement),
            )
        )

    updated = text
    for start, end, replacement in sorted(planned, reverse=True):
        updated = f"{updated[:start]}{replacement}{updated[end:]}"
    return updated


def _preserve_review_boundary_whitespace(original: str, replacement: str) -> str:
    """Retain structural separators that are outside the editable natural text."""

    if not replacement:
        return replacement
    leading = original[: len(original) - len(original.lstrip())]
    trailing = original[len(original.rstrip()) :]
    if leading and not replacement[0].isspace():
        replacement = f"{leading}{replacement}"
    if trailing and not replacement[-1].isspace():
        replacement = f"{replacement}{trailing}"
    return replacement


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
