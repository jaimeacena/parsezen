"""Bounded representative checks for long or uncertain PDF workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import pdfplumber

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.pdf_conversion import PdfPageRange, resolve_pdf_page_range
from parsezen.pipeline.contracts import ProcessRequest, ProcessResult
from parsezen.processing import clear_general_work_checkpoints, process_document
from parsezen.settings import AppSettings
from parsezen.workflow import OutputFormat

EarlyCheckProgress = Callable[[int, int], None]
DocumentProcessor = Callable[..., ProcessResult]


@dataclass(frozen=True, slots=True)
class _PageFeatures:
    number: int
    text_characters: int
    image_area: float
    has_table: bool


def representative_pdf_pages(request: ProcessRequest) -> tuple[int, ...]:
    """Choose a small, diverse set of real pages inside the requested interval."""

    if request.source_path.suffix.casefold() != ".pdf":
        return ()
    requested = request.pdf_page_range or PdfPageRange(1, 2_147_483_647)
    resolved = resolve_pdf_page_range(request.source_path, requested)
    midpoint = resolved.first_page + (resolved.last_page - resolved.first_page) // 2
    fallback = tuple(dict.fromkeys((resolved.first_page, midpoint, resolved.last_page)))
    try:
        features = _inspect_candidate_pages(request.source_path, resolved)
    except Exception:
        # Sampling is a safeguard, never a new reason to reject a readable PDF.
        return fallback
    if not features:
        return fallback

    selected: list[int] = list(dict.fromkeys((resolved.first_page, resolved.last_page)))

    def add(feature: _PageFeatures | None) -> None:
        if feature is not None and feature.number not in selected:
            selected.append(feature.number)

    add(max(features, key=lambda item: item.text_characters, default=None))
    add(
        max(
            (item for item in features if item.has_table),
            key=lambda item: (item.text_characters, item.image_area),
            default=None,
        )
    )
    add(max(features, key=lambda item: item.image_area, default=None))
    if len(selected) < 4:
        add(min(features, key=lambda item: item.text_characters, default=None))

    target_count = min(5, len({feature.number for feature in features}))
    while len(selected) < target_count:
        remaining = tuple(feature for feature in features if feature.number not in selected)
        if not remaining:
            break
        selected_numbers = tuple(selected)
        add(
            max(
                remaining,
                key=lambda item: (
                    min(abs(item.number - number) for number in selected_numbers),
                    -item.number,
                ),
            )
        )
    return tuple(sorted(selected))


def _inspect_candidate_pages(
    source_path: Path,
    selected_range: PdfPageRange,
) -> tuple[_PageFeatures, ...]:
    span = selected_range.last_page - selected_range.first_page
    candidates = tuple(
        dict.fromkeys(
            selected_range.first_page + round(span * fraction / 8) for fraction in range(9)
        )
    )
    features: list[_PageFeatures] = []
    with pdfplumber.open(source_path, unicode_norm="NFC") as pdf:
        for page_number in candidates:
            page = pdf.pages[page_number - 1]
            try:
                text_characters = sum(
                    bool(str(character.get("text", "")).strip()) for character in page.chars
                )
                page_area = max(float(page.width) * float(page.height), 1.0)
                image_area = sum(
                    max(0.0, float(image.get("width", 0)))
                    * max(0.0, float(image.get("height", 0)))
                    / page_area
                    for image in page.images
                )
                has_table = False
                if len(page.lines) + len(page.rects) >= 4:
                    has_table = any(len(table.extract() or []) >= 2 for table in page.find_tables())
                features.append(_PageFeatures(page_number, text_characters, image_area, has_table))
            finally:
                page.close()
    return tuple(features)


def run_early_check(
    request: ProcessRequest,
    settings: AppSettings,
    *,
    cancellation: CancellationToken,
    work_checkpoint_root: Path | None,
    on_progress: EarlyCheckProgress | None = None,
    processor: DocumentProcessor = process_document,
) -> EarlyCheckReport:
    """Run representative pages through the real pipeline without publishing them."""

    sampled_pages = representative_pdf_pages(request)
    if not sampled_pages:
        return EarlyCheckReport(())

    started_at = monotonic()
    results: list[ProcessResult] = []
    warning_pages = 0
    severe_conversion_pages = 0
    unsafe_translation_pages = 0
    retained_settings = replace(
        settings,
        checkpoint_retention_days=max(1, settings.checkpoint_retention_days),
    )

    with TemporaryDirectory(prefix="parsezen-early-check-") as temporary_name:
        temporary_root = Path(temporary_name)
        for index, page_number in enumerate(sampled_pages, start=1):
            check_cancelled(cancellation)
            sample_request = replace(
                request,
                output_directory=temporary_root,
                image_output_directory=(
                    temporary_root / "assets"
                    if request.output_format is OutputFormat.MARKDOWN and request.include_images
                    else None
                ),
                pdf_page_range=PdfPageRange(page_number, page_number),
            )
            sample_settings = replace(
                retained_settings,
                output_directory=temporary_root,
                image_output_directory=(
                    temporary_root / "assets"
                    if request.output_format is OutputFormat.MARKDOWN and request.include_images
                    else None
                ),
            )
            try:
                result = processor(
                    sample_request,
                    settings=sample_settings,
                    cancellation=cancellation,
                    work_checkpoint_root=work_checkpoint_root,
                )
            finally:
                clear_general_work_checkpoints(
                    sample_request,
                    sample_settings,
                    root=work_checkpoint_root,
                )
            results.append(result)

            pdf_report = result.pdf_quality_report
            blocking_pdf_issue = bool(
                pdf_report is not None and any(issue.blocking for issue in pdf_report.issues)
            )
            low_confidence = bool(pdf_report is not None and pdf_report.low_confidence_pages)
            preserved_visual_endpoint = bool(
                request.include_images
                and page_number in {sampled_pages[0], sampled_pages[-1]}
                and result.preserved_images > 0
                and pdf_report is not None
                and (
                    page_number in pdf_report.ocr_replaced_pages
                    or any(
                        issue.page_number == page_number
                        and issue.blocking
                        and not issue.markdown.strip()
                        for issue in pdf_report.issues
                    )
                    or (
                        page_number in pdf_report.low_confidence_pages
                        and not any(
                            issue.page_number == page_number and issue.markdown.strip()
                            for issue in pdf_report.issues
                        )
                    )
                )
            )
            translation_report = result.translation_quality_report
            issue_heavy_translation = bool(
                translation_report is not None
                and translation_report.total_issues
                >= max(
                    2,
                    (translation_report.checked_segments + 1) // 2,
                )
            )
            preserved_translation = bool(result.preserved_translation_chunks)
            if (blocking_pdf_issue or low_confidence) and not preserved_visual_endpoint:
                severe_conversion_pages += 1
            if issue_heavy_translation or preserved_translation:
                unsafe_translation_pages += 1
            if (
                blocking_pdf_issue
                or low_confidence
                or issue_heavy_translation
                or preserved_translation
            ):
                warning_pages += 1
            if on_progress is not None:
                on_progress(index, len(sampled_pages))

    blocking_reasons: list[str] = []
    material_threshold = 1 if len(sampled_pages) == 1 else 2
    if severe_conversion_pages >= material_threshold:
        blocking_reasons.append(
            "La extracción fue dudosa en varias páginas representativas. "
            "Revisa el intervalo y la opción de OCR antes de procesar el documento completo."
        )
    if unsafe_translation_pages >= material_threshold:
        blocking_reasons.append(
            "La transformación conservó texto original o acumuló incidencias en varias páginas. "
            "Revisa el modelo, el idioma y la revisión de contenido antes de continuar."
        )

    pdf_reports = tuple(
        result.pdf_quality_report for result in results if result.pdf_quality_report is not None
    )
    translation_reports = tuple(
        result.translation_quality_report
        for result in results
        if result.translation_quality_report is not None
    )
    return EarlyCheckReport(
        sampled_pages=sampled_pages,
        ocr_pages=sum(len(report.ocr_pages) for report in pdf_reports),
        low_confidence_pages=sum(len(report.low_confidence_pages) for report in pdf_reports),
        conversion_issues=sum(len(report.issues) for report in pdf_reports),
        translation_issues=sum(report.total_issues for report in translation_reports),
        preserved_segments=sum(len(result.preserved_translation_chunks) for result in results),
        warning_pages=warning_pages,
        blocking_reasons=tuple(blocking_reasons),
        duration_seconds=max(1, round(monotonic() - started_at)),
    )
