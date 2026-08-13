"""Stable local identities for immutable document sources."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Stable metadata and content digest captured from one unchanged file."""

    size_bytes: int
    modified_ns: int
    sha256: str

    def __post_init__(self) -> None:
        if self.size_bytes < 0 or self.modified_ns < 0 or not is_sha256_digest(self.sha256):
            raise ValueError("The document source identity is invalid.")

    @classmethod
    def inspect(cls, path: Path) -> SourceIdentity:
        """Capture an identity and reject a source changed during hashing."""

        statistics = path.stat()
        digest = sha256_file(path)
        final_statistics = path.stat()
        if (
            final_statistics.st_size != statistics.st_size
            or final_statistics.st_mtime_ns != statistics.st_mtime_ns
        ):
            raise OSError("The document changed while its identity was being captured.")
        return cls(statistics.st_size, statistics.st_mtime_ns, digest)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one local file without retaining its content."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def is_sha256_digest(value: object) -> bool:
    """Return whether *value* is one canonical lowercase SHA-256 digest."""

    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None
