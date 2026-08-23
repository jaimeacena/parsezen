from __future__ import annotations

from parsezen.processing_metrics import (
    AiOperationTelemetry,
    BatchTelemetry,
    capture_batch_telemetry,
    record_checkpoint_lookup,
    record_local_ai_request,
    record_retry,
    record_validation_rejection,
)


def test_batch_telemetry_aggregates_only_privacy_safe_operational_values() -> None:
    with capture_batch_telemetry() as collector:
        record_checkpoint_lookup(hit=True)
        record_checkpoint_lookup(hit=False)
        record_retry()
        record_validation_rejection()
        record_local_ai_request(
            input_characters=900,
            prompt_tokens=120,
            output_tokens=40,
            wall_duration_ms=2_000,
            ollama_total_duration_ms=1_800,
            ollama_load_duration_ms=200,
            operation="translation",
        )
        record_local_ai_request(
            input_characters=600,
            prompt_tokens=80,
            output_tokens=20,
            wall_duration_ms=1_000,
            ollama_total_duration_ms=900,
            ollama_load_duration_ms=0,
            operation="translation_review",
        )

    metrics = collector.snapshot()

    assert metrics == BatchTelemetry(
        local_ai_requests=2,
        checkpoint_hits=1,
        checkpoint_misses=1,
        retries=1,
        validation_rejections=1,
        input_characters=1_500,
        prompt_tokens=200,
        output_tokens=60,
        wall_duration_ms=3_000,
        ollama_total_duration_ms=2_700,
        ollama_load_duration_ms=200,
        operations=(
            AiOperationTelemetry("translation", 1, 120, 40, 2_000),
            AiOperationTelemetry("translation_review", 1, 80, 20, 1_000),
        ),
    )
    assert metrics.cache_hit_rate == 0.5
    assert metrics.output_tokens_per_second == 20.0


def test_batch_telemetry_capture_is_context_local_and_restores_outer_capture() -> None:
    with capture_batch_telemetry() as outer:
        record_retry()
        with capture_batch_telemetry() as inner:
            record_validation_rejection()
        record_retry()

    assert outer.snapshot().retries == 2
    assert outer.snapshot().validation_rejections == 0
    assert inner.snapshot().retries == 0
    assert inner.snapshot().validation_rejections == 1
