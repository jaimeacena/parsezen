import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import parsezen.infrastructure.state_store as state_store_module
from parsezen.application.planner import activate_next_stage
from parsezen.domain.books import BookDocument, BookMetadata, BookSection
from parsezen.domain.estimates import ProcessingMetric, WorkloadProfile
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    OutputConfiguration,
    ReviewRecommendation,
    ReviewSignal,
    TranslationConfiguration,
    TranslationMethod,
)
from parsezen.domain.reviews import (
    ReviewChoice,
    ReviewKind,
    ReviewSession,
    ReviewSeverity,
    ReviewUnit,
)
from parsezen.domain.stages import StageKind, StageStatus
from parsezen.infrastructure.state_store import StateStore, StateStoreError


def make_job(identifier: str, order: int) -> DocumentJob:
    job = DocumentJob.create(
        DocumentSource(
            Path(f"{identifier}.pdf"),
            DocumentFormat.PDF,
            size_bytes=100 + order,
            modified_ns=200 + order,
        ),
        JobConfiguration(
            output=OutputConfiguration(
                format=DocumentFormat.EPUB,
                directory=Path("output"),
            ),
            ai=AIProfileConfiguration(
                model="qwen3:4b",
                context_window=8192,
            ),
            translation=TranslationConfiguration(
                enabled=True,
                method=TranslationMethod.LOCAL_AI,
                target_language="es",
                glossary=(("source", "destino"),),
            ),
        ),
        order=order,
        job_id=identifier,
    )
    return activate_next_stage(job)


def test_state_store_round_trips_independent_jobs(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    one = make_job("one", 0)
    two = make_job("two", 1)
    one = one.replace_stage(one.stage(StageKind.PREPARE).transition(StageStatus.RUNNING))

    store.replace_jobs((one, two))
    loaded = store.load_jobs()

    assert loaded == (one, two)
    assert loaded[0].configuration.ai == AIProfileConfiguration(
        model="qwen3:4b",
        context_window=8192,
    )
    assert loaded[0].configuration.translation.glossary == (("source", "destino"),)
    assert loaded[0].configuration.translation.method is TranslationMethod.LOCAL_AI


def test_state_store_persists_content_free_review_recommendation(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    job = replace(
        make_job("recommended", 0),
        review_recommendation=ReviewRecommendation(
            ((ReviewSignal.SOURCE_TEXT_RESIDUE, 2),),
            (1, 4),
            "a" * 64,
            ("b" * 64, "c" * 64),
        ),
    )

    store.replace_jobs((job,))

    assert store.load_jobs()[0].review_recommendation == job.review_recommendation
    payload = state_store_module._job_to_json(job)
    assert payload["review_recommendation"] == {
        "signal_counts": [["source_text_residue", 2]],
        "block_positions": [1, 4],
        "scope_fingerprint": "a" * 64,
        "block_fingerprints": ["b" * 64, "c" * 64],
    }


def test_saved_recommendation_without_scope_fingerprint_remains_readable() -> None:
    job = replace(
        make_job("older-recommendation", 0),
        review_recommendation=ReviewRecommendation(
            ((ReviewSignal.CONVERSION_DAMAGE, 1),),
            (0,),
        ),
    )
    payload = state_store_module._job_to_json(job)
    payload["review_recommendation"].pop("scope_fingerprint")

    restored = state_store_module._job_from_json(payload)

    assert restored.review_recommendation == job.review_recommendation


def test_legacy_job_payload_without_shared_ai_profile_is_rejected() -> None:
    original = make_job("legacy", 0)
    payload = state_store_module._job_to_json(original)
    payload["configuration"].pop("ai")

    with pytest.raises(KeyError):
        state_store_module._job_from_json(payload)


def test_legacy_job_payload_without_a_product_plan_is_rejected() -> None:
    original = make_job("legacy-ai", 0)
    payload = state_store_module._job_to_json(original)
    payload["configuration"].pop("plan")

    with pytest.raises(KeyError):
        state_store_module._job_from_json(payload)


def test_previous_schema_is_reset_without_touching_source_documents(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"original")
    database = tmp_path / "state.db"
    store = StateStore(database)
    stored = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="legacy",
    )
    store.replace_jobs((stored,))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")

    reopened = StateStore(database)

    assert reopened.load_jobs() == ()
    assert source.read_bytes() == b"original"


def test_state_store_preserves_previous_queue_after_invalid_replacement(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    one = make_job("one", 0)
    store.replace_jobs((one,))

    with pytest.raises(ValueError):
        store.replace_jobs((one, make_job("two", 0)))

    assert store.load_jobs() == (one,)


def test_state_store_reorders_jobs_without_losing_related_review_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    one = make_job("one", 0)
    two = make_job("two", 1)
    store.replace_jobs((one, two))
    review = ReviewSession.create(
        job_id=one.id,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=one.configuration_revision,
        units=(ReviewUnit("unit", "original", "proposal"),),
    )
    store.save_review(review)
    store.save_result_snapshot(one.id, "manifest")

    store.replace_jobs(
        (
            replace(two, order=0),
            replace(one, order=1),
        )
    )

    assert tuple(job.id for job in store.load_jobs()) == ("two", "one")
    assert store.load_reviews(job_id=one.id) == (review,)
    assert store.load_result_snapshot(one.id) == "manifest"


def test_state_store_rolls_back_every_temporary_order_when_replacement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.db")
    one = make_job("one", 0)
    two = make_job("two", 1)
    store.replace_jobs((one, two))
    serialize = state_store_module._job_to_json

    def fail_second_job(job: DocumentJob):
        if job.id == one.id:
            raise ValueError("synthetic serialization failure")
        return serialize(job)

    monkeypatch.setattr(state_store_module, "_job_to_json", fail_second_job)

    with pytest.raises(StateStoreError):
        store.replace_jobs(
            (
                replace(two, order=0),
                replace(one, order=1),
            )
        )

    assert store.load_jobs() == (one, two)


def test_state_store_backs_up_consistently_before_reset(tmp_path: Path) -> None:
    state_path = tmp_path / "workspace.sqlite3"
    backup_path = tmp_path / "workspace.backup.sqlite3"
    store = StateStore(state_path)
    job = make_job("one", 0)
    store.replace_jobs((job,))

    assert store.backup_to(backup_path) == backup_path
    store.reset_queue()

    assert store.load_jobs() == ()
    assert StateStore(backup_path).load_jobs() == (job,)


def test_state_store_never_overwrites_an_existing_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "workspace.sqlite3")
    backup = tmp_path / "existing.sqlite3"
    backup.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        store.backup_to(backup)

    assert backup.read_bytes() == b"keep"


def test_state_store_persists_manual_review_edits_and_events(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    job = make_job("one", 0)
    store.replace_jobs((job,))
    review = ReviewSession.create(
        job_id=job.id,
        stage=StageKind.TRANSLATE,
        kind=ReviewKind.TRANSLATION,
        input_artifact_id="input",
        input_version=1,
        units=(
            ReviewUnit(
                "unit",
                "original",
                "proposal",
                recommended_choice=ReviewChoice.ORIGINAL,
                warning="Conservar por seguridad.",
                severity=ReviewSeverity.HIGH,
            ),
        ),
    ).decide("unit", ReviewChoice.EDITED, edited_artifact_id="manual")

    store.save_review(review)
    store.upsert_job(job, event_type="review_saved")

    assert store.load_reviews(job_id=job.id) == (review,)
    assert store.event_types(job.id) == ("review_saved",)


def test_state_store_keeps_bounded_content_free_processing_metrics(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    profile = WorkloadProfile(
        source_format=DocumentFormat.PDF,
        output_format=DocumentFormat.EPUB,
        work_units=20,
        force_pdf_ocr=False,
        translation_method=TranslationMethod.LOCAL_AI,
        refinement_enabled=True,
        structure_enabled=True,
        include_images=True,
        preserve_styles=True,
        model_key="hashed-model",
    )
    first = ProcessingMetric(profile, 120_000, datetime(2026, 7, 29, tzinfo=UTC))
    second = ProcessingMetric(profile, 110_000, datetime(2026, 7, 30, tzinfo=UTC))

    store.save_processing_metric(first, retention_limit=1)
    store.save_processing_metric(second, retention_limit=1)

    assert store.load_processing_metrics() == (second,)


def test_state_store_round_trips_normalized_book(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    job = make_job("book", 0)
    store.replace_jobs((job,))
    book = BookDocument(
        BookMetadata(
            "Title",
            "es",
            identifiers=("primary-id", "secondary-id"),
            publisher="Editorial local",
            publication_date="2024-03-14",
        ),
        (
            BookSection(
                "chapter",
                "Chapter",
                "xhtml",
                source_filename="chapter-0001.xhtml",
            ),
        ),
        ("chapter",),
    )

    store.save_book(job.id, book)

    assert store.load_book(job.id) == book
    store.delete_book(job.id)
    assert store.load_book(job.id) is None
