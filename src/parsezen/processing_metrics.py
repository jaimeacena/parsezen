"""Privacy-safe batch metrics collected during one local processing attempt."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AiOperationTelemetry:
    """Aggregate one privacy-safe class of local model requests."""

    operation: str
    requests: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    wall_duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class BatchTelemetry:
    """Aggregate operational evidence without document text, prompts or paths."""

    local_ai_requests: int = 0
    checkpoint_hits: int = 0
    checkpoint_misses: int = 0
    retries: int = 0
    validation_rejections: int = 0
    input_characters: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    wall_duration_ms: int = 0
    ollama_total_duration_ms: int = 0
    ollama_load_duration_ms: int = 0
    operations: tuple[AiOperationTelemetry, ...] = ()

    @property
    def cache_hit_rate(self) -> float:
        """Return the fraction of attempted checkpoint reads that were reusable."""

        lookups = self.checkpoint_hits + self.checkpoint_misses
        return self.checkpoint_hits / lookups if lookups else 0.0

    @property
    def output_tokens_per_second(self) -> float:
        """Return aggregate local generation throughput from Ollama wall time."""

        if not self.output_tokens or not self.wall_duration_ms:
            return 0.0
        return self.output_tokens / (self.wall_duration_ms / 1_000)


@dataclass(slots=True)
class _MutableBatchTelemetry:
    local_ai_requests: int = 0
    checkpoint_hits: int = 0
    checkpoint_misses: int = 0
    retries: int = 0
    validation_rejections: int = 0
    input_characters: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    wall_duration_ms: int = 0
    ollama_total_duration_ms: int = 0
    ollama_load_duration_ms: int = 0
    operations: dict[str, AiOperationTelemetry] | None = None

    def snapshot(self) -> BatchTelemetry:
        return BatchTelemetry(
            local_ai_requests=self.local_ai_requests,
            checkpoint_hits=self.checkpoint_hits,
            checkpoint_misses=self.checkpoint_misses,
            retries=self.retries,
            validation_rejections=self.validation_rejections,
            input_characters=self.input_characters,
            prompt_tokens=self.prompt_tokens,
            output_tokens=self.output_tokens,
            wall_duration_ms=self.wall_duration_ms,
            ollama_total_duration_ms=self.ollama_total_duration_ms,
            ollama_load_duration_ms=self.ollama_load_duration_ms,
            operations=tuple(
                sorted((self.operations or {}).values(), key=lambda item: item.operation)
            ),
        )


_ACTIVE_BATCH_TELEMETRY: ContextVar[_MutableBatchTelemetry | None] = ContextVar(
    "parsezen_active_batch_telemetry",
    default=None,
)


@contextmanager
def capture_batch_telemetry() -> Iterator[_MutableBatchTelemetry]:
    """Capture counters for the current execution context and restore nesting safely."""

    collector = _MutableBatchTelemetry()
    token = _ACTIVE_BATCH_TELEMETRY.set(collector)
    try:
        yield collector
    finally:
        _ACTIVE_BATCH_TELEMETRY.reset(token)


def record_local_ai_request(
    *,
    input_characters: int,
    prompt_tokens: int,
    output_tokens: int,
    wall_duration_ms: int,
    ollama_total_duration_ms: int,
    ollama_load_duration_ms: int,
    operation: str = "other",
) -> None:
    collector = _ACTIVE_BATCH_TELEMETRY.get()
    if collector is None:
        return
    collector.local_ai_requests += 1
    collector.input_characters += max(0, input_characters)
    collector.prompt_tokens += max(0, prompt_tokens)
    collector.output_tokens += max(0, output_tokens)
    collector.wall_duration_ms += max(0, wall_duration_ms)
    collector.ollama_total_duration_ms += max(0, ollama_total_duration_ms)
    collector.ollama_load_duration_ms += max(0, ollama_load_duration_ms)
    operation = operation.strip() or "other"
    if collector.operations is None:
        collector.operations = {}
    current = collector.operations.get(operation, AiOperationTelemetry(operation))
    collector.operations[operation] = AiOperationTelemetry(
        operation=operation,
        requests=current.requests + 1,
        prompt_tokens=current.prompt_tokens + max(0, prompt_tokens),
        output_tokens=current.output_tokens + max(0, output_tokens),
        wall_duration_ms=current.wall_duration_ms + max(0, wall_duration_ms),
    )


def record_checkpoint_lookup(*, hit: bool) -> None:
    collector = _ACTIVE_BATCH_TELEMETRY.get()
    if collector is None:
        return
    if hit:
        collector.checkpoint_hits += 1
    else:
        collector.checkpoint_misses += 1


def record_retry() -> None:
    collector = _ACTIVE_BATCH_TELEMETRY.get()
    if collector is not None:
        collector.retries += 1


def record_validation_rejection() -> None:
    collector = _ACTIVE_BATCH_TELEMETRY.get()
    if collector is not None:
        collector.validation_rejections += 1


__all__ = [
    "AiOperationTelemetry",
    "BatchTelemetry",
    "capture_batch_telemetry",
    "record_checkpoint_lookup",
    "record_local_ai_request",
    "record_retry",
    "record_validation_rejection",
]
