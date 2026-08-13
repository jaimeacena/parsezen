"""Configuration and aggregate state for one independent document job."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from parsezen.domain.source_identity import SourceIdentity, is_sha256_digest
from parsezen.domain.stages import (
    STAGE_ORDER,
    StageAvailability,
    StageKind,
    StageState,
    StageStatus,
)


class DocumentFormat(StrEnum):
    TEXT = "txt"
    MARKDOWN = "md"
    DOCX = "docx"
    PDF = "pdf"
    EPUB = "epub"

    @classmethod
    def from_path(cls, path: Path) -> DocumentFormat:
        suffix = path.suffix.casefold().lstrip(".")
        try:
            return cls(suffix)
        except ValueError as exc:
            raise ValueError(f"Unsupported document format: {path.suffix}") from exc


class TranslationMethod(StrEnum):
    """The two local translation engines intentionally exposed by the product."""

    OFFLINE = "offline"
    LOCAL_AI = "local_ai"


class ProcessingPlan(StrEnum):
    """The two deliberate product-level ways to process a document."""

    STANDARD = "standard"
    LOCAL_AI_REVIEWED = "local_ai_reviewed"


class ReviewSignal(StrEnum):
    """Content-free evidence that can justify an optional targeted AI review."""

    CONVERSION_DAMAGE = "conversion_damage"
    SOURCE_TEXT_RESIDUE = "source_text_residue"
    TRANSLATION_INCONSISTENCY = "translation_inconsistency"


@dataclass(frozen=True, slots=True)
class ReviewRecommendation:
    """Bounded late-review scope derived without retaining document excerpts."""

    signal_counts: tuple[tuple[ReviewSignal, int], ...]
    block_positions: tuple[int, ...]
    scope_fingerprint: str | None = None
    block_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.signal_counts or not self.block_positions:
            raise ValueError("A review recommendation requires evidence and target blocks.")
        if any(count < 1 for _signal, count in self.signal_counts):
            raise ValueError("Review signal counts must be positive.")
        if len({signal for signal, _count in self.signal_counts}) != len(self.signal_counts):
            raise ValueError("Review signals must be unique.")
        if tuple(sorted(set(self.block_positions))) != self.block_positions:
            raise ValueError("Review target blocks must be sorted and unique.")
        if any(position < 0 for position in self.block_positions):
            raise ValueError("Review target block positions cannot be negative.")
        if len(self.block_positions) > 64:
            raise ValueError("A targeted review cannot contain more than 64 blocks.")
        if self.scope_fingerprint is not None and (
            len(self.scope_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.scope_fingerprint)
        ):
            raise ValueError("The review scope fingerprint must be a SHA-256 digest.")
        if self.block_fingerprints and len(self.block_fingerprints) != len(self.block_positions):
            raise ValueError("Review block fingerprints must match the target block count.")
        if any(
            len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            for fingerprint in self.block_fingerprints
        ):
            raise ValueError("Review block fingerprints must be SHA-256 digests.")

    @property
    def signal_total(self) -> int:
        return sum(count for _signal, count in self.signal_counts)


class CoverStrategy(StrEnum):
    NONE = "none"
    FIRST_PAGE = "first_page"
    CUSTOM = "custom"
    REMOVE = "remove"


class MarkdownOrganization(StrEnum):
    """How the final Markdown is arranged without changing its canonical content."""

    SINGLE_FILE = "single_file"
    BY_CHAPTER = "by_chapter"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class DocumentSource:
    path: Path
    format: DocumentFormat
    size_bytes: int
    modified_ns: int
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.content_sha256 is not None and not is_sha256_digest(self.content_sha256):
            raise ValueError("The document content identity must be a SHA-256 digest.")

    @classmethod
    def inspect(cls, path: Path, *, include_content_hash: bool = True) -> DocumentSource:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("The document source must be a file.")
        if include_content_hash:
            identity = SourceIdentity.inspect(resolved)
            size_bytes = identity.size_bytes
            modified_ns = identity.modified_ns
            content_sha256 = identity.sha256
        else:
            statistics = resolved.stat()
            size_bytes = statistics.st_size
            modified_ns = statistics.st_mtime_ns
            content_sha256 = None
        return cls(
            path=resolved,
            format=DocumentFormat.from_path(resolved),
            size_bytes=size_bytes,
            modified_ns=modified_ns,
            content_sha256=content_sha256,
        )


@dataclass(frozen=True, slots=True)
class PageRangeConfiguration:
    first_page: int
    last_page: int

    def __post_init__(self) -> None:
        if self.first_page < 1:
            raise ValueError("The first page must be positive.")
        if self.last_page < self.first_page:
            raise ValueError("The last page cannot precede the first page.")


@dataclass(frozen=True, slots=True)
class TranslationConfiguration:
    enabled: bool = False
    method: TranslationMethod = TranslationMethod.OFFLINE
    target_language: str | None = None
    glossary: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AIProfileConfiguration:
    """Snapshot of the global local-AI profile used by a queued document."""

    model: str | None = None
    context_window: int | None = None


@dataclass(frozen=True, slots=True)
class OutputConfiguration:
    configured: bool = True
    format: DocumentFormat = DocumentFormat.MARKDOWN
    directory: Path | None = None
    include_images: bool = True
    image_directory: Path | None = None
    preserve_styles: bool = True
    markdown_organization: MarkdownOrganization = MarkdownOrganization.SINGLE_FILE
    markdown_include_metadata: bool = False
    markdown_include_page_references: bool = False
    title: str | None = None
    author: str | None = None
    cover_strategy: CoverStrategy = CoverStrategy.NONE
    cover_path: Path | None = None

    def __post_init__(self) -> None:
        if self.format not in {DocumentFormat.MARKDOWN, DocumentFormat.EPUB}:
            raise ValueError("Parsezen only publishes Markdown or EPUB outputs.")


@dataclass(frozen=True, slots=True)
class JobConfiguration:
    output: OutputConfiguration = OutputConfiguration()
    ai: AIProfileConfiguration = AIProfileConfiguration()
    translation: TranslationConfiguration = TranslationConfiguration()
    plan: ProcessingPlan = ProcessingPlan.STANDARD
    page_range: PageRangeConfiguration | None = None
    force_pdf_ocr: bool = False


def initial_stage_states(
    source: DocumentSource,
    configuration: JobConfiguration,
) -> tuple[StageState, ...]:
    structure_availability = StageAvailability.DISABLED
    if not configuration.output.configured:
        structure_availability = StageAvailability.DISABLED
    elif configuration.output.format is not DocumentFormat.EPUB:
        structure_availability = StageAvailability.UNAVAILABLE
    elif configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED:
        structure_availability = StageAvailability.ENABLED

    availability = {
        StageKind.PREPARE: StageAvailability.ENABLED,
        StageKind.TRANSLATE: (
            StageAvailability.ENABLED
            if configuration.output.configured and configuration.translation.enabled
            else StageAvailability.DISABLED
        ),
        StageKind.REFINE: (
            StageAvailability.ENABLED
            if configuration.output.configured
            and configuration.plan is ProcessingPlan.LOCAL_AI_REVIEWED
            else StageAvailability.DISABLED
        ),
        StageKind.STRUCTURE: structure_availability,
        StageKind.PUBLISH: (
            StageAvailability.ENABLED
            if configuration.output.configured
            else StageAvailability.DISABLED
        ),
    }
    if configuration.page_range is not None and source.format is not DocumentFormat.PDF:
        raise ValueError("Page ranges are available only for PDF documents.")
    return tuple(StageState(kind=kind, availability=availability[kind]) for kind in STAGE_ORDER)


@dataclass(frozen=True, slots=True)
class DocumentJob:
    id: str
    order: int
    source: DocumentSource
    configuration: JobConfiguration
    configuration_revision: int
    stages: tuple[StageState, ...]
    result_path: Path | None = None
    warnings: tuple[str, ...] = ()
    review_recommendation: ReviewRecommendation | None = None

    @classmethod
    def create(
        cls,
        source: DocumentSource,
        configuration: JobConfiguration,
        *,
        order: int,
        job_id: str | None = None,
    ) -> DocumentJob:
        if order < 0:
            raise ValueError("Job order cannot be negative.")
        return cls(
            id=job_id or uuid4().hex,
            order=order,
            source=source,
            configuration=configuration,
            configuration_revision=1,
            stages=initial_stage_states(source, configuration),
        )

    def __post_init__(self) -> None:
        if not self.id or any(character.isspace() for character in self.id):
            raise ValueError("Job id must be a non-empty compact identifier.")
        if self.order < 0:
            raise ValueError("Job order cannot be negative.")
        if tuple(stage.kind for stage in self.stages) != STAGE_ORDER:
            raise ValueError("Every job must contain each stage in chronological order.")
        if self.configuration_revision < 1:
            raise ValueError("Configuration revision must be positive.")

    def stage(self, kind: StageKind) -> StageState:
        return self.stages[STAGE_ORDER.index(kind)]

    @property
    def is_configured(self) -> bool:
        """Whether the job has a deliberate, processable destination."""

        return self.configuration.output.configured

    def replace_stage(self, state: StageState) -> DocumentJob:
        index = STAGE_ORDER.index(state.kind)
        stages = list(self.stages)
        stages[index] = state
        return replace(self, stages=tuple(stages))

    def with_configuration(self, configuration: JobConfiguration) -> DocumentJob:
        if self.status not in {
            JobStatus.QUEUED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            raise ValueError("A job cannot be reconfigured after processing has started.")
        return replace(
            self,
            configuration=configuration,
            configuration_revision=self.configuration_revision + 1,
            stages=initial_stage_states(self.source, configuration),
            result_path=None,
            warnings=(),
            review_recommendation=None,
        )

    @property
    def status(self) -> JobStatus:
        participating = tuple(stage for stage in self.stages if stage.participates)
        statuses = {stage.status for stage in participating}
        if StageStatus.RUNNING in statuses:
            return JobStatus.RUNNING
        if StageStatus.BLOCKED_FOR_REVIEW in statuses:
            return JobStatus.WAITING_REVIEW
        if StageStatus.FAILED in statuses:
            return JobStatus.FAILED
        if StageStatus.PAUSED in statuses:
            return JobStatus.PAUSED
        if StageStatus.CANCELLED in statuses:
            return JobStatus.CANCELLED
        if participating and all(stage.status is StageStatus.COMPLETED for stage in participating):
            return JobStatus.COMPLETED
        return JobStatus.QUEUED
