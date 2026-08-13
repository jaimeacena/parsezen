"""Stable local identities for immutable document sources."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


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
