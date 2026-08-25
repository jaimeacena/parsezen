"""Configuration and aggregate state for one independent document job."""

from __future__ import annotations

import re
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
    method: TranslationMethod = TranslationMethod.LOCAL_AI
    target_language: str | None = None
    glossary: tuple[tuple[str, str], ...] = ()


_LOCAL_AI_POLICY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOCAL_AI_MODEL = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?(?::[a-z0-9][a-z0-9._-]*)?$",
    flags=re.IGNORECASE,
)


def _is_cloud_model(model: str) -> bool:
    normalized = model.casefold()
    _name, separator, tag = normalized.rpartition(":")
    return (
        normalized.endswith("-cloud")
        or bool(separator)
        and (tag == "cloud" or tag.endswith("-cloud"))
    )


@dataclass(frozen=True, slots=True)
class LocalAIComponentSnapshot:
    """Content-free identity of one approved local AI component.

    A queued job stores only identifiers that can be checked against local
    Ollama metadata later.  It deliberately does not carry prompts, output,
    document text, or filesystem locations.
    """

    policy_version: str
    model: str
    digest: str
    context_window: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy_version, str)
            or _LOCAL_AI_POLICY_TOKEN.fullmatch(self.policy_version) is None
        ):
            raise ValueError("The local AI policy version is invalid.")
        if (
            not isinstance(self.model, str)
            or len(self.model) > 200
            or self.model != self.model.strip()
            or _LOCAL_AI_MODEL.fullmatch(self.model) is None
            or _is_cloud_model(self.model)
        ):
            raise ValueError("The local AI component model is invalid.")
        if not isinstance(self.digest, str):
            raise ValueError("The local AI component digest is invalid.")
        candidate = self.digest.strip()
        if candidate.casefold().startswith("sha256:"):
            candidate = candidate[7:]
        if not is_sha256_digest(candidate.casefold()):
            raise ValueError("The local AI component digest must be SHA-256.")
        if self.context_window is not None and (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or not 512 <= self.context_window <= 262_144
        ):
            raise ValueError("The local AI component context window is invalid.")
        object.__setattr__(self, "digest", candidate.casefold())

    @property
    def model_name(self) -> str:
        """Compatibility spelling used by Ollama's model metadata."""

        return self.model

    @property
    def ollama_digest(self) -> str:
        """Compatibility spelling used by the local policy manifest."""

        return self.digest


@dataclass(frozen=True, slots=True)
class LocalAIPolicySnapshot:
    """Effective content-free component identities captured for one job."""

    translation: LocalAIComponentSnapshot | None = None
    review: LocalAIComponentSnapshot | None = None
    visual_ocr: LocalAIComponentSnapshot | None = None

    def __post_init__(self) -> None:
        if any(
            component is not None and not isinstance(component, LocalAIComponentSnapshot)
            for component in (self.translation, self.review, self.visual_ocr)
        ):
            raise ValueError("The local AI policy components are invalid.")

    @property
    def visual(self) -> LocalAIComponentSnapshot | None:
        """Short compatibility spelling for the visual/OCR component."""

        return self.visual_ocr


@dataclass(frozen=True, slots=True)
class AIProfileConfiguration:
    """Snapshot of the global local-AI profile used by a queued document."""

    model: str | None = None
    context_window: int | None = None
    components: LocalAIPolicySnapshot = LocalAIPolicySnapshot()
    translation_model: str | None = None
    translation_context_window: int | None = None
    review_model: str | None = None
    review_context_window: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.components, LocalAIPolicySnapshot):
            raise ValueError("The local AI policy snapshot is invalid.")

    @property
    def policy_snapshot(self) -> LocalAIPolicySnapshot:
        """Return the immutable specialized-component policy snapshot."""

        return self.components

    @property
    def local_ai_policy(self) -> LocalAIPolicySnapshot:
        """Compatibility spelling for callers that name the policy explicitly."""

        return self.components

    @property
    def effective_translation_model(self) -> str | None:
        component = self.components.translation
        return (
            (component.model if component is not None else None)
            or self.translation_model
            or self.model
        )

    @property
    def effective_review_model(self) -> str | None:
        component = self.components.review
        return (
            (component.model if component is not None else None) or self.review_model or self.model
        )

    @property
    def effective_translation_context_window(self) -> int | None:
        component = self.components.translation
        return (
            component.context_window
            if component is not None and component.context_window is not None
            else self.translation_context_window
            if self.translation_context_window is not None
            else self.context_window
        )

    @property
    def effective_review_context_window(self) -> int | None:
        component = self.components.review
        return (
            component.context_window
            if component is not None and component.context_window is not None
            else self.review_context_window
            if self.review_context_window is not None
            else self.context_window
        )


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
