"""Encrypted, immutable artifacts used between stages and during reviews."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from parsezen.infrastructure.protected_file import atomic_write_bytes
from parsezen.infrastructure.user_data_protection import (
    protect_for_current_user,
    unprotect_for_current_user,
)

_SAFE_IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_ARTIFACT_MAGIC = b"PARSEZEN-ARTIFACT-V1\0"
_DIGEST_BYTES = 32
Protect = Callable[[bytes], bytes]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    job_id: str
    media_type: str
    size_bytes: int
    sha256: str


class ArtifactStore:
    """Write-once DPAPI-protected content addressed by opaque identifiers."""

    def __init__(
        self,
        root: Path,
        *,
        protect: Protect = protect_for_current_user,
        unprotect: Protect = unprotect_for_current_user,
    ) -> None:
        self.root = root
        self._protect = protect
        self._unprotect = unprotect

    def put(
        self,
        *,
        job_id: str,
        payload: bytes,
        media_type: str,
        artifact_id: str | None = None,
        generation: str | None = None,
    ) -> ArtifactRecord:
        _validate_identifier(job_id)
        identifier = artifact_id or secrets.token_hex(16)
        _validate_identifier(identifier)
        if not media_type or any(character in media_type for character in "\r\n\0"):
            raise ValueError("Artifact media type is invalid.")

        destination = self._path(job_id, identifier, generation=generation)
        if destination.exists():
            raise FileExistsError("Artifact ids are immutable.")
        digest = hashlib.sha256(payload).digest()
        protected = self._protect(_ARTIFACT_MAGIC + digest + payload)
        atomic_write_bytes(
            destination,
            protected,
            temporary_prefix=".artifact-",
        )
        return ArtifactRecord(
            id=identifier,
            job_id=job_id,
            media_type=media_type,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def put_text(
        self,
        *,
        job_id: str,
        text: str,
        media_type: str = "text/plain; charset=utf-8",
        artifact_id: str | None = None,
        generation: str | None = None,
    ) -> ArtifactRecord:
        return self.put(
            job_id=job_id,
            payload=text.encode("utf-8"),
            media_type=media_type,
            artifact_id=artifact_id,
            generation=generation,
        )

    def read(self, job_id: str, artifact_id: str, *, generation: str | None = None) -> bytes:
        source = self._path(job_id, artifact_id, generation=generation)
        envelope = self._unprotect(source.read_bytes())
        minimum_size = len(_ARTIFACT_MAGIC) + _DIGEST_BYTES
        if len(envelope) < minimum_size or not envelope.startswith(_ARTIFACT_MAGIC):
            raise ValueError("The artifact envelope is invalid.")
        digest_start = len(_ARTIFACT_MAGIC)
        digest = envelope[digest_start : digest_start + _DIGEST_BYTES]
        payload = envelope[digest_start + _DIGEST_BYTES :]
        if not secrets.compare_digest(digest, hashlib.sha256(payload).digest()):
            raise ValueError("The artifact failed its integrity check.")
        return payload

    def read_text(
        self,
        job_id: str,
        artifact_id: str,
        *,
        generation: str | None = None,
    ) -> str:
        return self.read(job_id, artifact_id, generation=generation).decode("utf-8")

    def remove_artifact(
        self,
        job_id: str,
        artifact_id: str,
        *,
        generation: str | None = None,
    ) -> None:
        """Remove one exact artifact without touching sibling review material."""

        path = self._path(job_id, artifact_id, generation=generation)
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            # Other artifacts, an active temporary write or a transient lock
            # legitimately keeps the job directory in place.
            pass

    def generation_exists(self, job_id: str, generation: str) -> bool:
        """Return whether one validated snapshot generation is present."""

        return self._generation_directory(job_id, generation).is_dir()

    def remove_generation(self, job_id: str, generation: str) -> None:
        """Remove one generation only when it contains known artifact files."""

        directory = self._generation_directory(job_id, generation)
        self._remove_generation_directory(directory)
        try:
            directory.parent.rmdir()
        except OSError:
            pass

    def prune_orphaned_generations(
        self,
        job_id: str,
        retained_generation_ids: Iterable[str],
    ) -> tuple[str, ...]:
        """Remove only complete, unreferenced generations for one retained job."""

        _validate_identifier(job_id)
        retained = frozenset(retained_generation_ids)
        for generation in retained:
            _validate_identifier(generation)
        job_directory = self.root / job_id
        if not job_directory.is_dir():
            return ()

        removed: list[str] = []
        for candidate in job_directory.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                _validate_identifier(candidate.name)
            except ValueError:
                continue
            if candidate.name in retained:
                self._remove_temporary_files(candidate)
                continue
            try:
                self._remove_generation_directory(candidate)
            except OSError:
                continue
            removed.append(candidate.name)
        return tuple(sorted(removed))

    def remove_job(self, job_id: str) -> None:
        """Remove only one validated job directory and its encrypted contents."""

        _validate_identifier(job_id)
        directory = (self.root / job_id).resolve(strict=False)
        root = self.root.resolve(strict=False)
        if directory.parent != root or not directory.is_dir():
            return
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                try:
                    _validate_identifier(child.name)
                    self._remove_generation_directory(child)
                except (OSError, ValueError):
                    continue
            elif child.is_file() and (
                child.suffix == ".pza"
                or child.name.startswith(".artifact-")
                and child.suffix == ".tmp"
            ):
                child.unlink()
        directory.rmdir()

    def prune_orphaned_jobs(self, retained_job_ids: Iterable[str]) -> tuple[str, ...]:
        """Remove encrypted review data that cannot belong to a recoverable job.

        Unknown files and directories are never removed. Temporary files inside
        retained job directories are safe to discard at application startup,
        before any writer can be active.
        """

        retained = frozenset(retained_job_ids)
        for job_id in retained:
            _validate_identifier(job_id)
        root = self.root.resolve(strict=False)
        if not root.is_dir():
            return ()

        removed: list[str] = []
        for candidate in root.iterdir():
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            try:
                _validate_identifier(candidate.name)
            except ValueError:
                continue
            if candidate.name in retained:
                self._remove_temporary_files(candidate)
                continue
            try:
                self.remove_job(candidate.name)
            except OSError:
                # A foreign file or a transient Windows lock keeps the
                # directory in place; a later startup can retry safely.
                continue
            removed.append(candidate.name)
        return tuple(sorted(removed))

    @staticmethod
    def _remove_temporary_files(directory: Path) -> None:
        for child in directory.iterdir():
            if child.is_file() and child.name.startswith(".artifact-") and child.suffix == ".tmp":
                child.unlink()

    @staticmethod
    def _remove_generation_directory(directory: Path) -> None:
        if not directory.is_dir() or directory.is_symlink():
            return
        children = tuple(directory.iterdir())
        if any(
            not child.is_file()
            or not (
                child.suffix == ".pza"
                or child.name.startswith(".artifact-")
                and child.suffix == ".tmp"
            )
            for child in children
        ):
            raise OSError("Generation contains unknown data.")
        for child in children:
            child.unlink()
        directory.rmdir()

    def _generation_directory(self, job_id: str, generation: str) -> Path:
        _validate_identifier(job_id)
        _validate_identifier(generation)
        return self.root / job_id / generation

    def _path(
        self,
        job_id: str,
        artifact_id: str,
        *,
        generation: str | None = None,
    ) -> Path:
        _validate_identifier(job_id)
        _validate_identifier(artifact_id)
        directory = (
            self.root / job_id
            if generation is None
            else self._generation_directory(job_id, generation)
        )
        return directory / f"{artifact_id}.pza"


def _validate_identifier(value: str) -> None:
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Artifact identifiers must be compact and path-safe.")
