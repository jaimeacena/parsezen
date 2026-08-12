"""Minimal artifact boundary consumed by Qt-free application services."""

from __future__ import annotations

from typing import Protocol


class ArtifactReference(Protocol):
    """Opaque identifier returned after storing private document material."""

    @property
    def id(self) -> str: ...


class ArtifactRepository(Protocol):
    """Store private bytes without exposing encryption or filesystem details."""

    def put(
        self,
        *,
        job_id: str,
        payload: bytes,
        media_type: str,
        artifact_id: str | None = None,
    ) -> ArtifactReference: ...

    def put_text(
        self,
        *,
        job_id: str,
        text: str,
        media_type: str = "text/plain; charset=utf-8",
        artifact_id: str | None = None,
    ) -> ArtifactReference: ...

    def read(self, job_id: str, artifact_id: str) -> bytes: ...

    def read_text(self, job_id: str, artifact_id: str) -> str: ...
