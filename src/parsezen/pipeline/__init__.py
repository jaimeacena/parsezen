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
from parsezen.pipeline.prepare import combined_translation_glossary, prepare_document_input
from parsezen.pipeline.publish import publish_transformed_document

__all__ = [
    "PreparedDocument",
    "ProcessRequest",
    "ProcessResult",
    "ProcessTelemetry",
    "ProgressCallback",
    "StageCallback",
    "StageTelemetry",
    "TransformedDocument",
    "combined_translation_glossary",
    "prepare_document_input",
    "publish_transformed_document",
]
