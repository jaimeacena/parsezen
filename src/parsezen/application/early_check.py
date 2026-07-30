"""Bounded representative checks for long or uncertain PDF workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.domain.outcomes import EarlyCheckReport
from parsezen.pdf_conversion import PdfPageRange, resolve_pdf_page_range
from parsezen.processing import (
    OutputFormat,
    ProcessRequest,
    ProcessResult,
    clear_general_work_checkpoints,
    process_document,
)
from parsezen.settings import AppSettings

EarlyCheckProgress = Callable[[int, int], None]
DocumentProcessor = Callable[..., ProcessResult]


def representative_pdf_pages(request: ProcessRequest) -> tuple[int, ...]:
    """Choose stable first, middle and final pages inside the requested interval."""

    if request.source_path.suffix.casefold() != ".pdf":
        return ()
    requested = request.pdf_page_range or PdfPageRange(1, 2_147_483_647)
    resolved = resolve_pdf_page_range(request.source_path, requested)
    midpoint = resolved.first_page + (resolved.last_page - resolved.first_page) // 2
    return tuple(dict.fromkeys((resolved.first_page, midpoint, resolved.last_page)))


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
            if blocking_pdf_issue or low_confidence:
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
