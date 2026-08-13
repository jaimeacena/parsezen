"""Small contracts and stages for the local document pipeline."""

from parsezen.pipeline.contracts import (
    PreparedDocument,
    ProcessRequest,
    ProcessResult,
    ProcessTelemetry,
    ProgressCallback,
    StageCallback,
    StageTelemetry,
    TransformedDocument,
)

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
