"""Bounded, content-free records for one local processing attempt."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

MAX_ATTEMPT_EVENTS = 32
MAX_ATTEMPT_EVENT_MESSAGE = 240
MAX_ATTEMPT_EVENT_TIMESTAMP = 64
MAX_FAILURE_MESSAGE = 512
MAX_ERROR_CODE = 64
MAX_DIAGNOSTIC_REFERENCE = 64
MAX_ATTEMPT_ID = 64

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SAFE_REFERENCE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DURABLE_FAILURE_MESSAGES = {
    "local_ai": "La IA local no pudo completar esta fase.",
    "output": "No se pudo guardar el resultado.",
    "integrity": "El control final detuvo la publicaci\u00f3n.",
    "ocr": "El OCR no pudo completar la preparaci\u00f3n.",
    "configuration": "La configuraci\u00f3n necesita un ajuste.",
    "transformation": "La transformaci\u00f3n no pudo completarse.",
    "source": "No se pudo procesar el documento de origen.",
    "early_check": "La comprobaci\u00f3n temprana necesita revisi\u00f3n.",
    "unexpected": "Se produjo un error inesperado durante el procesamiento.",
}
_DEFAULT_DURABLE_FAILURE_MESSAGE = "Se produjo un error durante el procesamiento."
_DIAGNOSTIC_REFERENCE = re.compile(
    r"(?:referencia\s+local|local\s+reference)\s*:\s*([A-Za-z0-9_-]{1,64})",
    re.IGNORECASE,
)


class AttemptPhase(StrEnum):
    """Stable user-visible phases in one processing attempt."""

    PREPARATION = "prepare"
    EARLY_CHECK = "early_check"
    TRANSLATION = "translate"
    CORRECTION = "refine"
    PERSONALIZATION = "structure"
    PUBLICATION = "publish"
    COMPLETION = "completion"
    FAILURE = "failure"
    CANCELLATION = "cancellation"
    PAUSE = "pause"


class AttemptEventStatus(StrEnum):
    """Lifecycle state attached to one user-visible attempt event."""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class ReusableWork(StrEnum):
    """Stable tokens describing work that remains safe to reuse."""

    NONE = "none"
    PREPARATION_CHECKPOINTS = "preparation_checkpoints"
    PREVIOUS_PHASES = "previous_phases"


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    """One bounded, content-free transition shown in recent activity."""

    phase: AttemptPhase
    status: AttemptEventStatus
    message: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.phase, AttemptPhase):
            raise ValueError("An attempt event requires a known phase.")
        if not isinstance(self.status, AttemptEventStatus):
            raise ValueError("An attempt event requires a known status.")
        _validate_optional_text(self.message, MAX_ATTEMPT_EVENT_MESSAGE, "event message")
        if not isinstance(self.timestamp, datetime):
            raise ValueError("An attempt event requires a timestamp.")
        try:
            offset = self.timestamp.utcoffset()
            normalized = self.timestamp.astimezone(UTC)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("An attempt event requires a valid timestamp.") from exc
        if offset is None:
            raise ValueError("An attempt event requires a timezone-aware timestamp.")
        object.__setattr__(self, "timestamp", normalized)

    @property
    def state(self) -> AttemptEventStatus:
        return self.status

    @property
    def kind(self) -> AttemptEventStatus:
        return self.status

    @property
    def occurred_at(self) -> datetime:
        """Compatibility name for callers that describe the event time."""

        return self.timestamp


@dataclass(frozen=True, slots=True)
class AttemptTimeline:
    """Immutable bounded timeline for one processing attempt."""

    events: tuple[AttemptEvent, ...] = ()

    def __post_init__(self) -> None:
        if len(self.events) > MAX_ATTEMPT_EVENTS:
            raise ValueError("An attempt timeline is too long.")
        if any(not isinstance(event, AttemptEvent) for event in self.events):
            raise ValueError("An attempt timeline contains an invalid event.")

    def append(self, event: AttemptEvent) -> AttemptTimeline:
        """Return a timeline retaining the newest events within the hard bound."""

        if not isinstance(event, AttemptEvent):
            raise ValueError("An attempt timeline can only contain attempt events.")
        events = (*self.events, event)
        if len(events) > MAX_ATTEMPT_EVENTS:
            events = events[-MAX_ATTEMPT_EVENTS:]
        return AttemptTimeline(events)

    @property
    def phase_events(self) -> tuple[AttemptEvent, ...]:
        return self.events

    @classmethod
    def from_events(cls, events: Iterable[AttemptEvent]) -> AttemptTimeline:
        """Build a bounded timeline while retaining its newest events."""

        materialized = tuple(events)
        return cls(materialized[-MAX_ATTEMPT_EVENTS:])


@dataclass(frozen=True, slots=True)
class FailureSnapshot:
    """Safe failure information retained with a failed recent attempt."""

    phase: AttemptPhase
    error_code: str
    message: str
    diagnostic_reference: str | None = None
    reusable_work: ReusableWork = ReusableWork.PREVIOUS_PHASES

    def __post_init__(self) -> None:
        if not isinstance(self.phase, AttemptPhase) or self.phase in {
            AttemptPhase.FAILURE,
            AttemptPhase.CANCELLATION,
            AttemptPhase.PAUSE,
            AttemptPhase.COMPLETION,
        }:
            raise ValueError("A failure snapshot requires the failed work phase.")
        if not isinstance(self.error_code, str) or not _SAFE_CODE.fullmatch(self.error_code):
            raise ValueError("A failure snapshot requires a bounded error code.")
        _validate_required_text(self.message, MAX_FAILURE_MESSAGE, "failure message")
        if self.diagnostic_reference is not None and not _SAFE_REFERENCE.fullmatch(
            self.diagnostic_reference
        ):
            raise ValueError("A diagnostic reference is not safe to retain.")
        if not isinstance(self.reusable_work, ReusableWork):
            raise ValueError("A failure snapshot requires a reusable-work token.")

    @property
    def error_kind(self) -> str:
        """Compatibility name for callers that describe codes as kinds."""

        return self.error_code

    @property
    def safe_message(self) -> str:
        return self.message

    @property
    def reusable_work_token(self) -> str:
        return self.reusable_work.value

    @property
    def diagnostic_id(self) -> str | None:
        return self.diagnostic_reference


# ``AttemptTrace`` is a useful semantic alias for UI/application callers.
AttemptTrace = AttemptTimeline


def reusable_work_for_phase(phase: AttemptPhase) -> ReusableWork:
    """Return a stable explanation token without persisting document details."""

    if phase in {AttemptPhase.PREPARATION, AttemptPhase.EARLY_CHECK}:
        return ReusableWork.PREPARATION_CHECKPOINTS
    return ReusableWork.PREVIOUS_PHASES


def make_failure_snapshot(
    phase: AttemptPhase,
    error_code: str,
    *,
    diagnostic_reference: str | None = None,
    reusable_work: ReusableWork | None = None,
) -> FailureSnapshot:
    """Create durable failure state without exception or document text."""

    return FailureSnapshot(
        phase=phase,
        error_code=error_code,
        message=durable_failure_message(error_code),
        diagnostic_reference=diagnostic_reference,
        reusable_work=reusable_work or reusable_work_for_phase(phase),
    )


def durable_failure_message(error_code: str) -> str:
    """Return the stable content-free message persisted for one failure code."""

    return _DURABLE_FAILURE_MESSAGES.get(error_code, _DEFAULT_DURABLE_FAILURE_MESSAGE)


def extract_diagnostic_reference(message: str) -> str | None:
    """Extract only the app-generated opaque reference from a safe message."""

    match = _DIAGNOSTIC_REFERENCE.search(message)
    return match.group(1) if match is not None else None


def is_safe_token(value: str, *, maximum: int = MAX_ATTEMPT_ID) -> bool:
    """Check a bounded opaque token before it is written to a technical log."""

    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and "\r" not in value
        and "\n" not in value
        and "\0" not in value
        and _SAFE_REFERENCE.fullmatch(value) is not None
    )


def _validate_required_text(value: object, maximum: int, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError(f"A {label} is not valid.")


def _validate_optional_text(value: object, maximum: int, label: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError(f"An {label} is not valid.")
