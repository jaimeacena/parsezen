from pathlib import Path
from unittest.mock import Mock

from parsezen.application.review_flow import ReviewFlowCoordinator
from parsezen.domain.jobs import DocumentFormat, DocumentJob, DocumentSource, JobConfiguration
from parsezen.pipeline.contracts import ProcessResult
from parsezen.revision import RevisionKind, build_revision_draft


def test_review_flow_delegates_materialization_and_rebuilds_draft() -> None:
    materialization = Mock()
    phases = Mock()
    publication = Mock()
    artifacts = Mock()
    flow = ReviewFlowCoordinator(materialization, phases, publication, artifacts)
    job = DocumentJob.create(
        DocumentSource(Path("book.md"), DocumentFormat.MARKDOWN, 10, 1),
        JobConfiguration(),
        order=0,
    )
    draft = build_revision_draft(
        "# Book\n\nOriginal.",
        "# Book\n\nProposal.",
        kinds=frozenset({RevisionKind.CONTENT}),
    )
    assert draft is not None
    result = ProcessResult(Path("book.md"), revision_draft=draft)
    expected = Mock()
    materialization.materialize_quality.return_value = expected

    assert flow.materialize_quality(job, result, "Candidate") is expected
    materialization.materialize_quality.assert_called_once_with(
        job,
        result,
        job.source.path,
        "Candidate",
    )
    rebuilt = flow.rebuild_revision_draft(result, "# Book\n\nAccepted.")
    assert rebuilt is not None
    assert rebuilt.proposed_markdown == "# Book\n\nAccepted."
