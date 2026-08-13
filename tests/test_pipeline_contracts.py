from parsezen.pipeline.contracts import (
    ProcessRequest as PipelineRequest,
)
from parsezen.pipeline.contracts import (
    ProcessResult as PipelineResult,
)
from parsezen.pipeline.contracts import (
    ProcessTelemetry as PipelineTelemetry,
)
from parsezen.processing import ProcessRequest, ProcessResult, ProcessTelemetry


def test_processing_facade_reexports_the_pipeline_contracts() -> None:
    assert ProcessRequest is PipelineRequest
    assert ProcessResult is PipelineResult
    assert ProcessTelemetry is PipelineTelemetry
