"""Small atomic-write primitive shared by protected local stores."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import mkstemp


def atomic_write_bytes(
    destination: Path,
    payload: bytes,
    *,
    temporary_prefix: str,
) -> None:
    """Replace one file after a flushed, fsynced write in its directory."""

    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = mkstemp(
            dir=destination.parent,
            prefix=temporary_prefix,
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
