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
    LocalAIComponentSnapshot,
    LocalAIPolicySnapshot,
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


def test_state_store_round_trips_content_free_local_ai_policy_snapshot(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    policy = LocalAIPolicySnapshot(
        translation=LocalAIComponentSnapshot("policy-v1", "translator:7b", "a" * 64),
        review=LocalAIComponentSnapshot("policy-v1", "reviewer:7b", "b" * 64),
        visual_ocr=LocalAIComponentSnapshot("policy-v1", "vision:7b", "c" * 64),
    )
    job = make_job("policy", 0)
    job = replace(
        job,
        configuration=replace(
            job.configuration,
            ai=replace(job.configuration.ai, components=policy),
        ),
    )

    store.replace_jobs((job,))

    loaded = store.load_jobs()[0]
    assert loaded.configuration.ai.components == policy
    payload = state_store_module._job_to_json(job)
    assert payload["configuration"]["ai"]["components"] == {
        "translation": {
            "policy_version": "policy-v1",
            "model": "translator:7b",
            "digest": "a" * 64,
        },
        "review": {
            "policy_version": "policy-v1",
            "model": "reviewer:7b",
            "digest": "b" * 64,
        },
        "visual_ocr": {
            "policy_version": "policy-v1",
            "model": "vision:7b",
            "digest": "c" * 64,
        },
    }


def test_state_store_round_trips_component_context_windows(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    policy = LocalAIPolicySnapshot(
        translation=LocalAIComponentSnapshot(
            "policy-v1", "translator:7b", "a" * 64, context_window=8_192
        ),
        review=LocalAIComponentSnapshot(
            "policy-v1", "reviewer:7b", "b" * 64, context_window=16_384
        ),
    )
    base_job = make_job("policy-context", 0)
    job = replace(
        base_job,
        configuration=replace(
            base_job.configuration,
            ai=AIProfileConfiguration(components=policy),
        ),
    )

    store.replace_jobs((job,))

    assert store.load_jobs()[0].configuration.ai.components == policy


def test_legacy_job_payload_without_local_ai_components_remains_readable() -> None:
    job = make_job("legacy-components", 0)
    payload = state_store_module._job_to_json(job)
    payload["configuration"]["ai"].pop("components")

    restored = state_store_module._job_from_json(payload)

    assert restored.configuration.ai.components == LocalAIPolicySnapshot()


def test_state_store_rejects_partial_local_ai_policy_snapshot() -> None:
    job = make_job("partial-components", 0)
    payload = state_store_module._job_to_json(job)
    payload["configuration"]["ai"]["components"].pop("review")

    with pytest.raises(TypeError):
        state_store_module._job_from_json(payload)


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


def test_source_digest_round_trips_and_legacy_payloads_remain_readable() -> None:
    job = make_job("digest", 0)
    job = replace(job, source=replace(job.source, content_sha256="a" * 64))
    payload = state_store_module._job_to_json(job)

    assert state_store_module._job_from_json(payload).source.content_sha256 == "a" * 64

    payload["source"].pop("content_sha256")
    assert state_store_module._job_from_json(payload).source.content_sha256 is None


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


def test_previous_schema_migration_preserves_queue_reviews_books_and_snapshots(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"original")
    database = tmp_path / "state.db"
    store = StateStore(database)
    queued = DocumentJob.create(
        DocumentSource.inspect(source),
        JobConfiguration(),
        order=0,
        job_id="queued",
    )
    paused = make_job("paused", 1)
    paused = paused.replace_stage(
        paused.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.PAUSED)
    )
    waiting = make_job("waiting", 2)
    waiting = waiting.replace_stage(
        waiting.stage(StageKind.PREPARE)
        .transition(StageStatus.RUNNING)
        .transition(StageStatus.COMPLETED)
        .require_cached_result_review("review-gate")
    )
    store.replace_jobs((queued, paused, waiting))
    review = ReviewSession.create(
        job_id=waiting.id,
        stage=StageKind.PREPARE,
        kind=ReviewKind.REFINEMENT,
        input_artifact_id="input",
        input_version=waiting.configuration_revision,
        units=(ReviewUnit("unit", "original", "proposal"),),
    )
    store.save_review(review)
    book = BookDocument(
        BookMetadata("Draft", "es"),
        (BookSection("chapter", "Chapter", "xhtml"),),
        ("chapter",),
    )
    store.save_book(waiting.id, book)
    store.save_result_snapshot(waiting.id, "snapshot-generation")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")

    reopened = StateStore(database)

    assert reopened.load_jobs() == (queued, paused, waiting)
    assert reopened.load_reviews(job_id=waiting.id) == (review,)
    assert reopened.load_book(waiting.id) == book
    assert reopened.load_result_snapshot(waiting.id) == "snapshot-generation"
    assert reopened.migration_backup_path is not None
    assert reopened.migration_backup_path.is_file()
    with sqlite3.connect(reopened.migration_backup_path) as backup:
        assert backup.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("5",)
        assert backup.execute("SELECT COUNT(*) FROM jobs").fetchone() == (3,)
    assert source.read_bytes() == b"original"


def test_failed_migration_rolls_back_and_keeps_the_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    job = make_job("preserved", 0)
    store.replace_jobs((job,))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
    original_migration = state_store_module.MIGRATIONS[5]

    def fail_midway(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO job_events(job_id, event_type, payload, created_at) "
            "VALUES ('preserved', 'partial', '{}', 'now')"
        )
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setitem(state_store_module.MIGRATIONS, 5, fail_midway)

    with pytest.raises(StateStoreError):
        StateStore(database)

    backups = tuple(tmp_path.glob("state.pre-migration-v5-to-v6-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("5",)
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM job_events").fetchone() == (0,)
    monkeypatch.setitem(state_store_module.MIGRATIONS, 5, original_migration)
    assert StateStore(database).load_jobs() == (job,)


def test_future_schema_is_rejected_without_modifying_state(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    store = StateStore(database)
    job = make_job("future", 0)
    store.replace_jobs((job,))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value = '7' WHERE key = 'schema_version'")

    with pytest.raises(StateStoreError, match="newer unsupported"):
        StateStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("7",)
    assert tuple(tmp_path.glob("*.pre-migration-*.db")) == ()


def test_new_durable_payloads_are_versioned_and_legacy_payloads_remain_readable() -> None:
    job = make_job("payload", 0)
    payload = state_store_module._job_to_json(job)

    assert payload["payload_version"] == 1
    payload.pop("payload_version")
    assert state_store_module._job_from_json(payload) == job


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
                source_archive_path="EPUB/text/chapter.xhtml",
                source_xhtml_artifact_id="source-xhtml",
            ),
        ),
        ("chapter",),
        source_package_artifact_id="source-package",
        package_structure_fingerprint="a" * 64,
    )

    store.save_book(job.id, book)

    assert store.load_book(job.id) == book
    store.delete_book(job.id)
    assert store.load_book(job.id) is None
