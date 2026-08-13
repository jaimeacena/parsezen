"""Immutable contracts shared by the preparation, transformation and publication stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from parsezen.document_model import ConvertedResource
from parsezen.domain.jobs import MarkdownOrganization
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.epub_builder import EpubBookMetadata
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport
from parsezen.revision import RevisionDraft
from parsezen.semantic_blocks import SemanticDocument
from parsezen.translation_quality import (
    LinguisticReviewCoverage,
    TranslationQualityReport,
)
from parsezen.workflow import OutputFormat


@dataclass(frozen=True, slots=True)
class StageTelemetry:
    """Aggregate time spent in one observable processing stage."""

    stage: ProcessStage
    duration_ms: int
    visits: int


@dataclass(frozen=True, slots=True)
class ProcessTelemetry:
    """Privacy-safe timings for one completed local transformation."""

    total_duration_ms: int
    stages: tuple[StageTelemetry, ...]


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """Stable facade request for one local document pipeline run."""

    source_path: Path
    convert_to_markdown: bool
    output_directory: Path | None = None
    improvement_mode: ImprovementMode | None = None
    target_language: str | None = None
    offline_translation_language: str | None = None
    pdf_page_range: PdfPageRange | None = None
    force_pdf_ocr: bool = False
    output_format: OutputFormat = OutputFormat.MARKDOWN
    image_output_directory: Path | None = None
    epub_title: str | None = None
    epub_author: str | None = None
    epub_cover_path: Path | None = None
    glossary: tuple[GlossaryEntry, ...] = ()
    include_images: bool = True
    preserve_styles: bool = True
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE
    markdown_include_metadata: bool = False
    markdown_include_page_references: bool = False
    epub_first_page_cover: bool = False
    epub_remove_cover: bool = False
    review_content: bool = False
    review_structure: bool = False
    source_size_bytes: int | None = None
    source_modified_ns: int | None = None
    source_content_sha256: str | None = None
    source_identity_verified: bool = False


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Stable facade result returned to application and presentation layers."""

    final_path: Path
    raw_markdown_path: Path | None = None
    review_original_path: Path | None = None
    problematic_pdf_pages: tuple[int, ...] = ()
    pdf_quality_report: PdfQualityReport | None = None
    exhaustive_pdf_ocr_used: bool = False
    epub_translation_parts: int = 0
    epub_resumed_parts: int = 0
    epub_checkpoint_degraded: bool = False
    translation_quality_report: TranslationQualityReport | None = None
    linguistic_review_coverage: LinguisticReviewCoverage | None = None
    preserved_translation_chunks: tuple[int, ...] = ()
    preserved_images: int = 0
    epub_chapters: int = 0
    revision_draft: RevisionDraft | None = None
    revision_resources: tuple[ConvertedResource, ...] = ()
    revision_epub_metadata: EpubBookMetadata | None = None
    review_markdown: str | None = None
    review_required: bool = False
    revision_approved: bool = False
    final_integrity_report: FinalIntegrityReport | None = None
    telemetry: ProcessTelemetry | None = None
    front_matter_blocks: int = 0
    toc_blocks: int = 0
    terminology_terms: int = 0
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE
    markdown_include_metadata: bool = False
    markdown_include_page_references: bool = False
    markdown_source_name: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    """In-memory output of source preparation; it has not been published."""

    source_path: Path
    resolved_page_range: PdfPageRange | None
    output_stem: str | None
    pdf_quality_report: PdfQualityReport | None
    source_cover_path: PurePosixPath | None
    converted_resources: tuple[ConvertedResource, ...]
    markdown: str
    problematic_pdf_pages: tuple[int, ...]
    semantic_document: SemanticDocument
    translation_glossary: tuple[GlossaryEntry, ...]


@dataclass(frozen=True, slots=True)
class TransformedDocument:
    """In-memory output of transformations; it has not been published."""

    transformed_markdown: str
    translation_quality_report: TranslationQualityReport | None
    linguistic_review_coverage: LinguisticReviewCoverage | None
    preserved_translation_chunks: tuple[int, ...]
    revision_draft: RevisionDraft | None
    published_markdown: str
    review_required: bool
    public_markdown: str


StageCallback = Callable[[ProcessStage], None]
ProgressCallback = Callable[[int, int], None]


__all__ = [
    "PreparedDocument",
    "ProcessRequest",
    "ProcessResult",
    "ProcessTelemetry",
    "ProgressCallback",
    "StageCallback",
    "StageTelemetry",
    "TransformedDocument",
]
