"""Configuration and aggregate state for one independent document job."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

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
    OFFLINE = "offline"
    LOCAL_AI = "local_ai"


class CoverStrategy(StrEnum):
    NONE = "none"
    FIRST_PAGE = "first_page"
    CUSTOM = "custom"
    REMOVE = "remove"


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

    @classmethod
    def inspect(cls, path: Path) -> DocumentSource:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("The document source must be a file.")
        statistics = resolved.stat()
        return cls(
            path=resolved,
            format=DocumentFormat.from_path(resolved),
            size_bytes=statistics.st_size,
            modified_ns=statistics.st_mtime_ns,
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
    model: str | None = None
    context_window: int | None = None
    glossary: tuple[tuple[str, str], ...] = ()
    manual_review: bool = True


@dataclass(frozen=True, slots=True)
class AIProfileConfiguration:
    """Shared local-AI choices for every AI-backed phase of one document."""

    model: str | None = None
    context_window: int | None = None
    is_custom: bool = False


@dataclass(frozen=True, slots=True)
class RefinementConfiguration:
    enabled: bool = False
    model: str | None = None
    context_window: int | None = None
    manual_review: bool = True


@dataclass(frozen=True, slots=True)
class StructureConfiguration:
    enabled: bool = False
    manual_review: bool = True


@dataclass(frozen=True, slots=True)
class OutputConfiguration:
    configured: bool = True
    format: DocumentFormat = DocumentFormat.MARKDOWN
    directory: Path | None = None
    directory_is_custom: bool = False
    include_images: bool = True
    image_directory: Path | None = None
    preserve_styles: bool = True
    title: str | None = None
    author: str | None = None
    cover_strategy: CoverStrategy = CoverStrategy.NONE
    cover_path: Path | None = None


@dataclass(frozen=True, slots=True)
class JobConfiguration:
    output: OutputConfiguration = OutputConfiguration()
    ai: AIProfileConfiguration = AIProfileConfiguration()
    translation: TranslationConfiguration = TranslationConfiguration()
    refinement: RefinementConfiguration = RefinementConfiguration()
    structure: StructureConfiguration = StructureConfiguration()
    page_range: PageRangeConfiguration | None = None
    force_pdf_ocr: bool = False


def effective_ai_profile(configuration: JobConfiguration) -> AIProfileConfiguration:
    """Read the canonical profile, falling back to pre-profile saved jobs."""

    if configuration.ai.model is not None or configuration.ai.context_window is not None:
        return configuration.ai
    legacy_model = configuration.refinement.model or configuration.translation.model
    legacy_context = (
        configuration.refinement.context_window or configuration.translation.context_window
    )
    return AIProfileConfiguration(
        model=legacy_model,
        context_window=legacy_context,
        # Historical per-operation values were explicit document choices;
        # a completely empty profile still inherits the application default.
        is_custom=legacy_model is not None or legacy_context is not None,
    )


def initial_stage_states(
    source: DocumentSource,
    configuration: JobConfiguration,
) -> tuple[StageState, ...]:
    structure_availability = StageAvailability.DISABLED
    if not configuration.output.configured:
        structure_availability = StageAvailability.DISABLED
    elif configuration.output.format is not DocumentFormat.EPUB:
        structure_availability = StageAvailability.UNAVAILABLE
    elif configuration.structure.enabled:
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
            if configuration.output.configured and configuration.refinement.enabled
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
