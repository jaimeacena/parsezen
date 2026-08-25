"""Transactional SQLite metadata store for jobs and phase reviews."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from parsezen.domain.books import BookDocument, BookMetadata, BookResource, BookSection
from parsezen.domain.estimates import ProcessingMetric, WorkloadProfile
from parsezen.domain.jobs import (
    AIProfileConfiguration,
    CoverStrategy,
    DocumentFormat,
    DocumentJob,
    DocumentSource,
    JobConfiguration,
    LocalAIComponentSnapshot,
    LocalAIPolicySnapshot,
    MarkdownOrganization,
    OutputConfiguration,
    PageRangeConfiguration,
    ProcessingPlan,
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
    ReviewStatus,
    ReviewUnit,
)
from parsezen.domain.stages import (
    StageAvailability,
    StageKind,
    StageState,
    StageStatus,
)

SCHEMA_VERSION = 6


class StateStoreError(RuntimeError):
    """Raised when the durable queue cannot be read or updated safely."""


class StateStore:
    """Small thread-safe store containing metadata but no document content."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        except (OSError, sqlite3.Error):
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                if connection is not None:
                    connection.rollback()
                raise StateStoreError("The Parsezen state could not be updated.") from exc
            finally:
                if connection is not None:
                    connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        """Open a read connection whose file handle is always released."""

        with self._lock:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect()
                yield connection
            finally:
                if connection is not None:
                    connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    order_index INTEGER NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS reviews_job_id ON reviews(job_id);
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS books (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS result_snapshots (
                    job_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS processing_metrics (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            current = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if current is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            else:
                current_version = int(current["value"])
                if current_version < SCHEMA_VERSION:
                    # Product configuration v5 is an intentional clean break:
                    # discard only Parsezen's rebuildable metadata, never source files.
                    connection.execute("DELETE FROM jobs")
                    connection.execute("DELETE FROM job_events")
                    connection.execute("DELETE FROM processing_metrics")
                    connection.execute(
                        "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
                elif current_version != SCHEMA_VERSION:
                    raise StateStoreError("The Parsezen state uses an unsupported schema.")

    def replace_jobs(self, jobs: tuple[DocumentJob, ...]) -> None:
        """Atomically replace the queue projection while preserving its reviews."""

        orders = tuple(job.order for job in jobs)
        if len(orders) != len(set(orders)):
            raise ValueError("Job order values must be unique.")
        identifiers = tuple(job.id for job in jobs)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Job ids must be unique.")

        with self._transaction() as connection:
            retained = set(identifiers)
            existing = connection.execute(
                "SELECT id, order_index FROM jobs ORDER BY order_index, id"
            ).fetchall()
            if existing:
                lowest_order = min(
                    *(int(row["order_index"]) for row in existing),
                    *orders,
                    0,
                )
                temporary_start = lowest_order - len(existing) - 1
                for offset, row in enumerate(existing):
                    connection.execute(
                        "UPDATE jobs SET order_index = ? WHERE id = ?",
                        (temporary_start - offset, row["id"]),
                    )
            for job in jobs:
                connection.execute(
                    """
                    INSERT INTO jobs(id, order_index, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        order_index = excluded.order_index,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        job.id,
                        job.order,
                        _json_dump(_job_to_json(job)),
                        datetime.now().astimezone().isoformat(),
                    ),
                )
            for row in existing:
                if row["id"] not in retained:
                    connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))

    def upsert_job(self, job: DocumentJob, *, event_type: str | None = None) -> None:
        with self._transaction() as connection:
            now = datetime.now().astimezone().isoformat()
            connection.execute(
                """
                INSERT INTO jobs(id, order_index, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    order_index = excluded.order_index,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (job.id, job.order, _json_dump(_job_to_json(job)), now),
            )
            if event_type is not None:
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, event_type, payload, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job.id, event_type, "{}", now),
                )

    def load_jobs(self) -> tuple[DocumentJob, ...]:
        try:
            with self._read_connection() as connection:
                rows = connection.execute(
                    "SELECT payload FROM jobs ORDER BY order_index, id"
                ).fetchall()
            return tuple(_job_from_json(json.loads(row["payload"])) for row in rows)
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise StateStoreError("The saved Parsezen queue is invalid.") from exc

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent SQLite backup without modifying the active state."""

        target = destination.resolve(strict=False)
        if target == self.path.resolve(strict=False):
            raise ValueError("The state backup must use a different path.")
        if target.exists():
            raise FileExistsError("The state backup already exists.")
        target.parent.mkdir(parents=True, exist_ok=True)
        failure: OSError | sqlite3.Error | None = None
        with self._lock:
            source: sqlite3.Connection | None = None
            backup: sqlite3.Connection | None = None
            try:
                source = self._connect()
                backup = sqlite3.connect(target)
                source.backup(backup)
                backup.commit()
            except (OSError, sqlite3.Error) as exc:
                failure = exc
            finally:
                if backup is not None:
                    backup.close()
                if source is not None:
                    source.close()
        if failure is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise StateStoreError("The Parsezen state backup could not be created.") from failure
        return target

    def reset_queue(self) -> None:
        """Start an empty queue after the previous state was backed up safely."""

        with self._transaction() as connection:
            connection.execute("DELETE FROM job_events")
            connection.execute("DELETE FROM jobs")

    def save_review(self, review: ReviewSession) -> None:
        with self._transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE id = ?",
                    (review.job_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("A review cannot outlive its document job.")
            connection.execute(
                """
                INSERT INTO reviews(id, job_id, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    review.id,
                    review.job_id,
                    _json_dump(_review_to_json(review)),
                    review.updated_at.isoformat(),
                ),
            )

    def load_reviews(self, *, job_id: str | None = None) -> tuple[ReviewSession, ...]:
        query = "SELECT payload FROM reviews"
        parameters: tuple[str, ...] = ()
        if job_id is not None:
            query += " WHERE job_id = ?"
            parameters = (job_id,)
        query += " ORDER BY updated_at, id"
        try:
            with self._read_connection() as connection:
                rows = connection.execute(query, parameters).fetchall()
            return tuple(_review_from_json(json.loads(row["payload"])) for row in rows)
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise StateStoreError("The saved Parsezen reviews are invalid.") from exc

    def event_types(self, job_id: str) -> tuple[str, ...]:
        try:
            with self._read_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT event_type FROM job_events
                    WHERE job_id = ? ORDER BY sequence
                    """,
                    (job_id,),
                ).fetchall()
            return tuple(str(row["event_type"]) for row in rows)
        except sqlite3.Error as exc:
            raise StateStoreError("The Parsezen event history could not be read.") from exc

    def save_book(self, job_id: str, book: BookDocument) -> None:
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                raise ValueError("A book cannot outlive its document job.")
            connection.execute(
                """
                INSERT INTO books(job_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    _json_dump(_book_to_json(book)),
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def load_book(self, job_id: str) -> BookDocument | None:
        try:
            with self._read_connection() as connection:
                row = connection.execute(
                    "SELECT payload FROM books WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            return None if row is None else _book_from_json(json.loads(row["payload"]))
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise StateStoreError("The saved Parsezen book is invalid.") from exc

    def delete_book(self, job_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM books WHERE job_id = ?", (job_id,))

    def save_result_snapshot(self, job_id: str, artifact_id: str) -> None:
        """Point a job at one encrypted processing-result manifest."""

        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                raise ValueError("A result snapshot cannot outlive its document job.")
            connection.execute(
                """
                INSERT INTO result_snapshots(job_id, artifact_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    artifact_id = excluded.artifact_id,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    artifact_id,
                    datetime.now().astimezone().isoformat(),
                ),
            )

    def load_result_snapshot(self, job_id: str) -> str | None:
        try:
            with self._read_connection() as connection:
                row = connection.execute(
                    "SELECT artifact_id FROM result_snapshots WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            return None if row is None else str(row["artifact_id"])
        except sqlite3.Error as exc:
            raise StateStoreError("The saved Parsezen result could not be read.") from exc

    def delete_result_snapshot(self, job_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM result_snapshots WHERE job_id = ?",
                (job_id,),
            )

    def delete_review_material(self, job_id: str) -> None:
        """Remove persisted review metadata after a final result is published."""

        with self._transaction() as connection:
            connection.execute("DELETE FROM reviews WHERE job_id = ?", (job_id,))
            connection.execute("DELETE FROM books WHERE job_id = ?", (job_id,))
            connection.execute(
                "DELETE FROM result_snapshots WHERE job_id = ?",
                (job_id,),
            )

    def save_processing_metric(
        self,
        metric: ProcessingMetric,
        *,
        retention_limit: int = 200,
    ) -> None:
        """Retain a bounded, content-free timing history for local estimates."""

        if retention_limit < 1:
            raise ValueError("The processing metric retention limit must be positive.")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO processing_metrics(payload, created_at)
                VALUES (?, ?)
                """,
                (
                    _json_dump(_processing_metric_to_json(metric)),
                    metric.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                DELETE FROM processing_metrics
                WHERE sequence NOT IN (
                    SELECT sequence FROM processing_metrics
                    ORDER BY sequence DESC LIMIT ?
                )
                """,
                (retention_limit,),
            )

    def load_processing_metrics(self, *, limit: int = 200) -> tuple[ProcessingMetric, ...]:
        if limit < 1:
            raise ValueError("The processing metric limit must be positive.")
        try:
            with self._read_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT payload FROM processing_metrics
                    ORDER BY sequence DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return tuple(_processing_metric_from_json(json.loads(row["payload"])) for row in rows)
        except (json.JSONDecodeError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise StateStoreError("The saved processing estimates are invalid.") from exc


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _optional_path(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def _processing_metric_to_json(metric: ProcessingMetric) -> dict[str, Any]:
    profile = metric.profile
    return {
        "source_format": profile.source_format.value,
        "output_format": profile.output_format.value,
        "work_units": profile.work_units,
        "force_pdf_ocr": profile.force_pdf_ocr,
        "translation_method": (
            profile.translation_method.value if profile.translation_method is not None else None
        ),
        "refinement_enabled": profile.refinement_enabled,
        "structure_enabled": profile.structure_enabled,
        "include_images": profile.include_images,
        "preserve_styles": profile.preserve_styles,
        "model_key": profile.model_key,
        "duration_ms": metric.duration_ms,
        "created_at": metric.created_at.isoformat(),
    }


def _processing_metric_from_json(raw: object) -> ProcessingMetric:
    if not isinstance(raw, dict):
        raise TypeError
    translation_method = raw["translation_method"]
    created_at = datetime.fromisoformat(str(raw["created_at"]))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ProcessingMetric(
        profile=WorkloadProfile(
            source_format=DocumentFormat(raw["source_format"]),
            output_format=DocumentFormat(raw["output_format"]),
            work_units=int(raw["work_units"]),
            force_pdf_ocr=bool(raw["force_pdf_ocr"]),
            translation_method=(
                TranslationMethod(translation_method) if translation_method is not None else None
            ),
            refinement_enabled=bool(raw["refinement_enabled"]),
            structure_enabled=bool(raw["structure_enabled"]),
            include_images=bool(raw["include_images"]),
            preserve_styles=bool(raw["preserve_styles"]),
            model_key=raw["model_key"],
        ),
        duration_ms=int(raw["duration_ms"]),
        created_at=created_at,
    )


def _book_to_json(book: BookDocument) -> dict[str, Any]:
    def section_json(section: BookSection) -> dict[str, Any]:
        return {
            "id": section.id,
            "title": section.title,
            "xhtml_artifact_id": section.xhtml_artifact_id,
            "children": [section_json(child) for child in section.children],
            "source_filename": section.source_filename,
            "source_archive_path": section.source_archive_path,
            "source_xhtml_artifact_id": section.source_xhtml_artifact_id,
        }

    return {
        "metadata": {
            "title": book.metadata.title,
            "language": book.metadata.language,
            "author": book.metadata.author,
            "identifier": book.metadata.identifier,
            "identifiers": list(book.metadata.identifiers),
            "publisher": book.metadata.publisher,
            "publication_date": book.metadata.publication_date,
        },
        "sections": [section_json(section) for section in book.sections],
        "spine": list(book.spine),
        "resources": [
            {
                "id": resource.id,
                "href": resource.href,
                "media_type": resource.media_type,
                "payload_artifact_id": resource.payload_artifact_id,
            }
            for resource in book.resources
        ],
        "stylesheet_artifact_ids": list(book.stylesheet_artifact_ids),
        "cover_resource_id": book.cover_resource_id,
        "source_fingerprint": book.source_fingerprint,
        "baseline_fingerprint": book.baseline_fingerprint,
        "source_package_artifact_id": book.source_package_artifact_id,
        "package_structure_fingerprint": book.package_structure_fingerprint,
    }


def _book_from_json(value: dict[str, Any]) -> BookDocument:
    def section_from_json(raw: dict[str, Any]) -> BookSection:
        return BookSection(
            id=str(raw["id"]),
            title=str(raw["title"]),
            xhtml_artifact_id=str(raw["xhtml_artifact_id"]),
            children=tuple(section_from_json(child) for child in raw.get("children", ())),
            source_filename=(
                str(raw["source_filename"]) if raw.get("source_filename") is not None else None
            ),
            source_archive_path=(
                str(raw["source_archive_path"])
                if raw.get("source_archive_path") is not None
                else None
            ),
            source_xhtml_artifact_id=(
                str(raw["source_xhtml_artifact_id"])
                if raw.get("source_xhtml_artifact_id") is not None
                else None
            ),
        )

    metadata = value["metadata"]
    return BookDocument(
        metadata=BookMetadata(
            title=str(metadata["title"]),
            language=str(metadata.get("language", "und")),
            author=metadata.get("author"),
            identifier=metadata.get("identifier"),
            identifiers=tuple(str(value) for value in metadata.get("identifiers", ())),
            publisher=metadata.get("publisher"),
            publication_date=metadata.get("publication_date"),
        ),
        sections=tuple(section_from_json(section) for section in value["sections"]),
        spine=tuple(str(identifier) for identifier in value["spine"]),
        resources=tuple(
            BookResource(
                id=str(resource["id"]),
                href=str(resource["href"]),
                media_type=str(resource["media_type"]),
                payload_artifact_id=str(resource["payload_artifact_id"]),
            )
            for resource in value.get("resources", ())
        ),
        stylesheet_artifact_ids=tuple(
            str(identifier) for identifier in value.get("stylesheet_artifact_ids", ())
        ),
        cover_resource_id=value.get("cover_resource_id"),
        source_fingerprint=value.get("source_fingerprint"),
        baseline_fingerprint=value.get("baseline_fingerprint"),
        source_package_artifact_id=value.get("source_package_artifact_id"),
        package_structure_fingerprint=value.get("package_structure_fingerprint"),
    )


def _local_ai_component_to_json(
    component: LocalAIComponentSnapshot | None,
) -> dict[str, str | int] | None:
    if component is None:
        return None
    return {
        "policy_version": component.policy_version,
        "model": component.model,
        "digest": component.digest,
        **(
            {"context_window": component.context_window}
            if component.context_window is not None
            else {}
        ),
    }


def _local_ai_policy_to_json(policy: LocalAIPolicySnapshot) -> dict[str, object]:
    return {
        "translation": _local_ai_component_to_json(policy.translation),
        "review": _local_ai_component_to_json(policy.review),
        "visual_ocr": _local_ai_component_to_json(policy.visual_ocr),
    }


def _local_ai_component_from_json(raw: object) -> LocalAIComponentSnapshot | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError
    context_window = raw.get("context_window")
    if context_window is not None and (
        isinstance(context_window, bool) or not isinstance(context_window, int)
    ):
        raise TypeError
    return LocalAIComponentSnapshot(
        policy_version=_required_text(raw, "policy_version"),
        model=_required_text(raw, "model"),
        digest=_required_text(raw, "digest"),
        context_window=context_window,
    )


def _local_ai_policy_from_json(raw: object) -> LocalAIPolicySnapshot:
    if raw is None:
        return LocalAIPolicySnapshot()
    if not isinstance(raw, dict):
        raise TypeError
    if not all(key in raw for key in ("translation", "review")):
        raise TypeError
    if "visual_ocr" not in raw and "visual" not in raw:
        raise TypeError
    return LocalAIPolicySnapshot(
        translation=_local_ai_component_from_json(raw.get("translation")),
        review=_local_ai_component_from_json(raw.get("review")),
        visual_ocr=_local_ai_component_from_json(raw.get("visual_ocr", raw.get("visual"))),
    )


def _required_text(raw: dict[object, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError
    return value


def _job_to_json(job: DocumentJob) -> dict[str, Any]:
    configuration = job.configuration
    return {
        "id": job.id,
        "order": job.order,
        "source": {
            "path": str(job.source.path),
            "format": job.source.format.value,
            "size_bytes": job.source.size_bytes,
            "modified_ns": job.source.modified_ns,
            "content_sha256": job.source.content_sha256,
        },
        "configuration_revision": job.configuration_revision,
        "configuration": {
            "output": {
                "configured": configuration.output.configured,
                "format": configuration.output.format.value,
                "directory": _optional_path(configuration.output.directory),
                "include_images": configuration.output.include_images,
                "image_directory": _optional_path(configuration.output.image_directory),
                "preserve_styles": configuration.output.preserve_styles,
                "markdown_organization": configuration.output.markdown_organization.value,
                "markdown_include_metadata": configuration.output.markdown_include_metadata,
                "markdown_include_page_references": (
                    configuration.output.markdown_include_page_references
                ),
                "title": configuration.output.title,
                "author": configuration.output.author,
                "cover_strategy": configuration.output.cover_strategy.value,
                "cover_path": _optional_path(configuration.output.cover_path),
            },
            "ai": {
                "model": configuration.ai.model,
                "context_window": configuration.ai.context_window,
                "translation_model": configuration.ai.translation_model,
                "translation_context_window": configuration.ai.translation_context_window,
                "review_model": configuration.ai.review_model,
                "review_context_window": configuration.ai.review_context_window,
                "components": _local_ai_policy_to_json(configuration.ai.components),
            },
            "translation": {
                "enabled": configuration.translation.enabled,
                "method": configuration.translation.method.value,
                "target_language": configuration.translation.target_language,
                "glossary": [list(item) for item in configuration.translation.glossary],
            },
            "plan": configuration.plan.value,
            "page_range": (
                {
                    "first_page": configuration.page_range.first_page,
                    "last_page": configuration.page_range.last_page,
                }
                if configuration.page_range is not None
                else None
            ),
            "force_pdf_ocr": configuration.force_pdf_ocr,
        },
        "stages": [_stage_to_json(stage) for stage in job.stages],
        "result_path": _optional_path(job.result_path),
        "warnings": list(job.warnings),
        "review_recommendation": (
            {
                "signal_counts": [
                    [signal.value, count]
                    for signal, count in job.review_recommendation.signal_counts
                ],
                "block_positions": list(job.review_recommendation.block_positions),
                "scope_fingerprint": job.review_recommendation.scope_fingerprint,
                "block_fingerprints": list(job.review_recommendation.block_fingerprints),
            }
            if job.review_recommendation is not None
            else None
        ),
    }


def _job_from_json(raw: object) -> DocumentJob:
    if not isinstance(raw, dict):
        raise TypeError
    source_raw = raw["source"]
    configuration_raw = raw["configuration"]
    if not isinstance(source_raw, dict) or not isinstance(configuration_raw, dict):
        raise TypeError
    output_raw = configuration_raw["output"]
    ai_raw = configuration_raw["ai"]
    translation_raw = configuration_raw["translation"]
    if not isinstance(ai_raw, dict) or not all(
        isinstance(value, dict) for value in (output_raw, translation_raw)
    ):
        raise TypeError
    page_range_raw = configuration_raw["page_range"]
    page_range = (
        None
        if page_range_raw is None
        else PageRangeConfiguration(
            first_page=int(page_range_raw["first_page"]),
            last_page=int(page_range_raw["last_page"]),
        )
    )
    configuration = JobConfiguration(
        output=OutputConfiguration(
            configured=bool(output_raw["configured"]),
            format=DocumentFormat(output_raw["format"]),
            directory=Path(output_raw["directory"]) if output_raw["directory"] else None,
            include_images=bool(output_raw["include_images"]),
            image_directory=(
                Path(output_raw["image_directory"]) if output_raw["image_directory"] else None
            ),
            preserve_styles=bool(output_raw["preserve_styles"]),
            markdown_organization=MarkdownOrganization(output_raw["markdown_organization"]),
            markdown_include_metadata=bool(output_raw["markdown_include_metadata"]),
            markdown_include_page_references=bool(output_raw["markdown_include_page_references"]),
            title=output_raw["title"],
            author=output_raw["author"],
            cover_strategy=CoverStrategy(output_raw["cover_strategy"]),
            cover_path=Path(output_raw["cover_path"]) if output_raw["cover_path"] else None,
        ),
        ai=AIProfileConfiguration(
            model=ai_raw.get("model"),
            context_window=ai_raw.get("context_window"),
            translation_model=ai_raw.get("translation_model"),
            translation_context_window=ai_raw.get("translation_context_window"),
            review_model=ai_raw.get("review_model"),
            review_context_window=ai_raw.get("review_context_window"),
            components=_local_ai_policy_from_json(ai_raw.get("components")),
        ),
        translation=TranslationConfiguration(
            enabled=bool(translation_raw["enabled"]),
            method=TranslationMethod(translation_raw["method"]),
            target_language=translation_raw["target_language"],
            glossary=tuple(tuple(item) for item in translation_raw["glossary"]),
        ),
        plan=ProcessingPlan(configuration_raw["plan"]),
        page_range=page_range,
        force_pdf_ocr=bool(configuration_raw["force_pdf_ocr"]),
    )
    source = DocumentSource(
        path=Path(source_raw["path"]),
        format=DocumentFormat(source_raw["format"]),
        size_bytes=int(source_raw["size_bytes"]),
        modified_ns=int(source_raw["modified_ns"]),
        content_sha256=(
            str(source_raw["content_sha256"])
            if source_raw.get("content_sha256") is not None
            else None
        ),
    )
    recommendation_raw = raw.get("review_recommendation")
    recommendation = (
        ReviewRecommendation(
            tuple(
                (ReviewSignal(signal), int(count))
                for signal, count in recommendation_raw["signal_counts"]
            ),
            tuple(int(position) for position in recommendation_raw["block_positions"]),
            (
                str(recommendation_raw["scope_fingerprint"])
                if recommendation_raw.get("scope_fingerprint") is not None
                else None
            ),
            tuple(str(value) for value in recommendation_raw.get("block_fingerprints", ())),
        )
        if isinstance(recommendation_raw, dict)
        else None
    )
    return DocumentJob(
        id=str(raw["id"]),
        order=int(raw["order"]),
        source=source,
        configuration=configuration,
        configuration_revision=int(raw["configuration_revision"]),
        stages=tuple(_stage_from_json(stage) for stage in raw["stages"]),
        result_path=Path(raw["result_path"]) if raw["result_path"] else None,
        warnings=tuple(str(item) for item in raw["warnings"]),
        review_recommendation=recommendation,
    )


def _stage_to_json(stage: StageState) -> dict[str, Any]:
    return {
        "kind": stage.kind.value,
        "availability": stage.availability.value,
        "status": stage.status.value,
        "progress_current": stage.progress_current,
        "progress_total": stage.progress_total,
        "progress_message": stage.progress_message,
        "attempt": stage.attempt,
        "review_id": stage.review_id,
        "artifact_ids": list(stage.artifact_ids),
        "error_code": stage.error_code,
        "error_message": stage.error_message,
        "started_at": stage.started_at.isoformat() if stage.started_at is not None else None,
        "finished_at": stage.finished_at.isoformat() if stage.finished_at is not None else None,
    }


def _stage_from_json(raw: object) -> StageState:
    if not isinstance(raw, dict):
        raise TypeError
    return StageState(
        kind=StageKind(raw["kind"]),
        availability=StageAvailability(raw["availability"]),
        status=StageStatus(raw["status"]),
        progress_current=int(raw["progress_current"]),
        progress_total=int(raw["progress_total"]),
        progress_message=raw["progress_message"],
        attempt=int(raw["attempt"]),
        review_id=raw["review_id"],
        artifact_ids=tuple(str(item) for item in raw["artifact_ids"]),
        error_code=raw["error_code"],
        error_message=raw["error_message"],
        started_at=(
            datetime.fromisoformat(raw["started_at"]) if raw["started_at"] is not None else None
        ),
        finished_at=(
            datetime.fromisoformat(raw["finished_at"]) if raw["finished_at"] is not None else None
        ),
    )


def _review_to_json(review: ReviewSession) -> dict[str, Any]:
    return {
        "id": review.id,
        "job_id": review.job_id,
        "stage": review.stage.value,
        "kind": review.kind.value,
        "input_artifact_id": review.input_artifact_id,
        "input_version": review.input_version,
        "status": review.status.value,
        "created_at": review.created_at.isoformat(),
        "updated_at": review.updated_at.isoformat(),
        "units": [
            {
                "id": unit.id,
                "original_artifact_id": unit.original_artifact_id,
                "proposed_artifact_id": unit.proposed_artifact_id,
                "edited_artifact_id": unit.edited_artifact_id,
                "choice": unit.choice.value if unit.choice is not None else None,
                "required": unit.required,
                "label": unit.label,
                "original_selectable": unit.original_selectable,
                "target": unit.target,
                "recommended_choice": (
                    unit.recommended_choice.value if unit.recommended_choice is not None else None
                ),
                "warning": unit.warning,
                "severity": unit.severity.value,
                "proposed_selectable": unit.proposed_selectable,
            }
            for unit in review.units
        ],
    }


def _review_from_json(raw: object) -> ReviewSession:
    if not isinstance(raw, dict):
        raise TypeError
    units = tuple(
        ReviewUnit(
            id=str(unit["id"]),
            original_artifact_id=str(unit["original_artifact_id"]),
            proposed_artifact_id=unit["proposed_artifact_id"],
            edited_artifact_id=unit["edited_artifact_id"],
            choice=ReviewChoice(unit["choice"]) if unit["choice"] is not None else None,
            required=bool(unit["required"]),
            label=unit["label"],
            original_selectable=bool(unit.get("original_selectable", True)),
            target=unit.get("target"),
            recommended_choice=(
                ReviewChoice(unit["recommended_choice"])
                if unit.get("recommended_choice") is not None
                else None
            ),
            warning=unit.get("warning"),
            severity=ReviewSeverity(unit.get("severity", ReviewSeverity.MEDIUM.value)),
            proposed_selectable=bool(unit.get("proposed_selectable", True)),
        )
        for unit in raw["units"]
    )
    return ReviewSession(
        id=str(raw["id"]),
        job_id=str(raw["job_id"]),
        stage=StageKind(raw["stage"]),
        kind=ReviewKind(raw["kind"]),
        input_artifact_id=str(raw["input_artifact_id"]),
        input_version=int(raw["input_version"]),
        units=units,
        status=ReviewStatus(raw["status"]),
        created_at=datetime.fromisoformat(raw["created_at"]),
        updated_at=datetime.fromisoformat(raw["updated_at"]),
    )
