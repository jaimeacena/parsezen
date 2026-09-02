"""Immutable contracts shared by the preparation, transformation and publication stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from parsezen.document_model import ConvertedResource
from parsezen.domain.execution_plan import ExecutionPlan
from parsezen.domain.jobs import MarkdownOrganization
from parsezen.domain.process_lifecycle import ProcessStage
from parsezen.epub_builder import EpubBookMetadata
from parsezen.final_integrity import FinalIntegrityReport
from parsezen.glossary import GlossaryEntry
from parsezen.improvement import ImprovementMode
from parsezen.pdf_conversion import PdfPageRange, PdfQualityReport
from parsezen.processing_metrics import BatchTelemetry
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
    batches: BatchTelemetry = BatchTelemetry()


@dataclass(frozen=True, slots=True)
class ProcessSourceRequest:
    """Source inputs projected from the legacy flat request facade."""

    path: Path
    convert_to_markdown: bool
    pdf_page_range: PdfPageRange | None
    force_pdf_ocr: bool
    size_bytes: int | None
    modified_ns: int | None
    content_sha256: str | None
    identity_verified: bool


@dataclass(frozen=True, slots=True)
class ProcessTranslationRequest:
    """Translation inputs projected from the legacy flat request facade."""

    improvement_mode: ImprovementMode | None
    target_language: str | None
    offline_language: str | None
    glossary: tuple[GlossaryEntry, ...]


@dataclass(frozen=True, slots=True)
class ProcessReviewRequest:
    """Review decisions projected from the legacy flat request facade."""

    content: bool
    structure: bool


@dataclass(frozen=True, slots=True)
class ProcessPublicationRequest:
    """Publication inputs projected from the legacy flat request facade."""

    output_directory: Path | None
    output_format: OutputFormat
    image_output_directory: Path | None
    epub_title: str | None
    epub_author: str | None
    epub_cover_path: Path | None
    include_images: bool
    preserve_styles: bool
    markdown_organization: MarkdownOrganization
    markdown_include_metadata: bool
    markdown_include_page_references: bool
    epub_first_page_cover: bool
    epub_remove_cover: bool


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
    execution_plan: ExecutionPlan | None = None

    @property
    def source(self) -> ProcessSourceRequest:
        """Temporary adapter while callers migrate away from flat source fields."""

        return ProcessSourceRequest(
            self.source_path,
            self.convert_to_markdown,
            self.pdf_page_range,
            self.force_pdf_ocr,
            self.source_size_bytes,
            self.source_modified_ns,
            self.source_content_sha256,
            self.source_identity_verified,
        )

    @property
    def translation(self) -> ProcessTranslationRequest:
        """Temporary adapter while callers migrate away from flat translation fields."""

        return ProcessTranslationRequest(
            self.improvement_mode,
            self.target_language,
            self.offline_translation_language,
            self.glossary,
        )

    @property
    def review(self) -> ProcessReviewRequest:
        """Temporary adapter while callers migrate away from flat review fields."""

        return ProcessReviewRequest(self.review_content, self.review_structure)

    @property
    def publication(self) -> ProcessPublicationRequest:
        """Temporary adapter while callers migrate away from flat publication fields."""

        return ProcessPublicationRequest(
            self.output_directory,
            self.output_format,
            self.image_output_directory,
            self.epub_title,
            self.epub_author,
            self.epub_cover_path,
            self.include_images,
            self.preserve_styles,
            self.markdown_organization,
            self.markdown_include_metadata,
            self.markdown_include_page_references,
            self.epub_first_page_cover,
            self.epub_remove_cover,
        )


@dataclass(frozen=True, slots=True)
class ProcessReviewResult:
    """Review output projected from the legacy flat result facade."""

    revision_draft: RevisionDraft | None
    resources: tuple[ConvertedResource, ...]
    epub_metadata: EpubBookMetadata | None
    markdown: str | None
    required: bool
    approved: bool
    preserve_epub_package_when_unchanged: bool


@dataclass(frozen=True, slots=True)
class ProcessPublicationResult:
    """Publication output projected from the legacy flat result facade."""

    final_path: Path
    raw_markdown_path: Path | None
    review_original_path: Path | None
    preserved_images: int
    epub_chapters: int
    markdown_organization: MarkdownOrganization
    markdown_include_metadata: bool
    markdown_include_page_references: bool
    markdown_source_name: str | None


@dataclass(frozen=True, slots=True)
class ProcessQualityResult:
    """Content-free quality evidence projected from the flat result facade."""

    problematic_pdf_pages: tuple[int, ...]
    pdf_report: PdfQualityReport | None
    exhaustive_pdf_ocr_used: bool
    translation_report: TranslationQualityReport | None
    review_translation_report: TranslationQualityReport | None
    linguistic_review_coverage: LinguisticReviewCoverage | None
    preserved_translation_chunks: tuple[int, ...]
    final_integrity_report: FinalIntegrityReport | None

    @property
    def translation_for_review(self) -> TranslationQualityReport | None:
        return self.review_translation_report or self.translation_report


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
    review_translation_quality_report: TranslationQualityReport | None = None
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
    preserve_epub_package_on_unchanged_review: bool = False
    final_integrity_report: FinalIntegrityReport | None = None
    telemetry: ProcessTelemetry | None = None
    front_matter_blocks: int = 0
    toc_blocks: int = 0
    terminology_terms: int = 0
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE
    markdown_include_metadata: bool = False
    markdown_include_page_references: bool = False
    markdown_source_name: str | None = None

    @property
    def review(self) -> ProcessReviewResult:
        """Temporary adapter while callers migrate away from flat review fields."""

        return ProcessReviewResult(
            self.revision_draft,
            self.revision_resources,
            self.revision_epub_metadata,
            self.review_markdown,
            self.review_required,
            self.revision_approved,
            self.preserve_epub_package_on_unchanged_review,
        )

    @property
    def publication(self) -> ProcessPublicationResult:
        """Temporary adapter while callers migrate away from flat publication fields."""

        return ProcessPublicationResult(
            self.final_path,
            self.raw_markdown_path,
            self.review_original_path,
            self.preserved_images,
            self.epub_chapters,
            self.markdown_organization,
            self.markdown_include_metadata,
            self.markdown_include_page_references,
            self.markdown_source_name,
        )

    @property
    def quality(self) -> ProcessQualityResult:
        """Temporary adapter while callers migrate away from flat quality fields."""

        return ProcessQualityResult(
            self.problematic_pdf_pages,
            self.pdf_quality_report,
            self.exhaustive_pdf_ocr_used,
            self.translation_quality_report,
            self.review_translation_quality_report,
            self.linguistic_review_coverage,
            self.preserved_translation_chunks,
            self.final_integrity_report,
        )

    @property
    def translation_quality_for_review(self) -> TranslationQualityReport | None:
        """Return quality evidence aligned with ``review_markdown`` when available."""

        return self.quality.translation_for_review


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
    review_translation_quality_report: TranslationQualityReport | None
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
    "ProcessPublicationRequest",
    "ProcessPublicationResult",
    "ProcessQualityResult",
    "ProcessRequest",
    "ProcessReviewRequest",
    "ProcessReviewResult",
    "ProcessResult",
    "ProcessSourceRequest",
    "ProcessTelemetry",
    "ProcessTranslationRequest",
    "ProgressCallback",
    "StageCallback",
    "StageTelemetry",
    "TransformedDocument",
]
