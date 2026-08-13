"""Measure local runtime costs without reading or retaining user documents."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from time import monotonic

from parsezen.application.job_queue import JobQueue
from parsezen.domain.jobs import DocumentSource, JobConfiguration
from parsezen.infrastructure.artifact_store import ArtifactStore
from parsezen.infrastructure.result_snapshots import ResultSnapshotStore
from parsezen.infrastructure.state_store import StateStore
from parsezen.infrastructure.user_data_protection import (
    protect_for_current_user,
    unprotect_for_current_user,
)
from parsezen.pipeline.contracts import ProcessResult

SCHEMA_VERSION = 1
MAX_PAYLOAD_MIB = 64
MAX_STARTUP_RUNS = 10
_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeMetrics:
    """Content-free measurements for recovery, storage and cold startup."""

    payload_bytes: int
    protect_seconds: float
    unprotect_seconds: float
    snapshot_write_seconds: float
    snapshot_recovery_seconds: float
    snapshot_disk_bytes: int
    temporary_peak_bytes: int
    cold_start_seconds: float
    installation_bytes: int
    installer_bytes: int | None


def measure_runtime(
    installation_root: Path,
    *,
    installer_path: Path | None = None,
    payload_mib: int = 1,
    startup_runs: int = 1,
) -> RuntimeMetrics:
    """Measure synthetic local operations and filesystem sizes."""

    if isinstance(payload_mib, bool) or not 1 <= payload_mib <= MAX_PAYLOAD_MIB:
        raise ValueError(f"El payload debe estar entre 1 y {MAX_PAYLOAD_MIB} MiB.")
    if isinstance(startup_runs, bool) or not 1 <= startup_runs <= MAX_STARTUP_RUNS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_STARTUP_RUNS}.")
    installation = installation_root.resolve(strict=True)
    if not installation.is_dir():
        raise ValueError("La instalación medida debe ser una carpeta.")
    installer = installer_path.resolve(strict=True) if installer_path is not None else None
    if installer is not None and not installer.is_file():
        raise ValueError("El instalador medido debe ser un archivo.")

    payload = _synthetic_payload(payload_mib * _MIB)
    protect_started = monotonic()
    protected = protect_for_current_user(payload)
    protect_seconds = monotonic() - protect_started
    unprotect_started = monotonic()
    restored = unprotect_for_current_user(protected)
    unprotect_seconds = monotonic() - unprotect_started
    if restored != payload:
        raise RuntimeError("La protección local no recuperó el payload sintético.")

    with TemporaryDirectory(prefix="parsezen-runtime-benchmark-") as temporary_name:
        temporary_root = Path(temporary_name)
        snapshot_write, snapshot_recovery, snapshot_disk, temporary_peak = _measure_snapshot(
            temporary_root,
            payload.decode("ascii"),
        )

    return RuntimeMetrics(
        payload_bytes=len(payload),
        protect_seconds=protect_seconds,
        unprotect_seconds=unprotect_seconds,
        snapshot_write_seconds=snapshot_write,
        snapshot_recovery_seconds=snapshot_recovery,
        snapshot_disk_bytes=snapshot_disk,
        temporary_peak_bytes=temporary_peak,
        cold_start_seconds=measure_cold_start(startup_runs),
        installation_bytes=_tree_size(installation),
        installer_bytes=installer.stat().st_size if installer is not None else None,
    )


def measure_cold_start(runs: int = 1) -> float:
    """Return the worst clean Python-to-window initialization time."""

    if isinstance(runs, bool) or not 1 <= runs <= MAX_STARTUP_RUNS:
        raise ValueError(f"Las repeticiones deben estar entre 1 y {MAX_STARTUP_RUNS}.")
    source_root = Path(__file__).resolve().parents[1] / "src"
    command = [sys.executable, "-c", _STARTUP_PROBE]
    samples: list[float] = []
    for _index in range(runs):
        with TemporaryDirectory(prefix="parsezen-startup-benchmark-") as temporary_name:
            environment = os.environ.copy()
            inherited_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                os.pathsep.join((str(source_root), inherited_path))
                if inherited_path
                else str(source_root)
            )
            environment["QT_QPA_PLATFORM"] = "offscreen"
            environment["PARSEZEN_BENCHMARK_ROOT"] = temporary_name
            started = monotonic()
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=60,
                check=False,
            )
            elapsed = monotonic() - started
            if completed.returncode != 0:
                raise RuntimeError("La medición de arranque local no pudo completar la ventana.")
            samples.append(elapsed)
    return max(samples)


def write_profile(path: Path, metrics: RuntimeMetrics) -> None:
    """Atomically write only numeric runtime measurements."""

    payload = {
        "schema_version": SCHEMA_VERSION,
        "metrics": asdict(metrics),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = mkstemp(
        dir=path.parent,
        prefix=".runtime-profile-",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _measure_snapshot(root: Path, review_text: str) -> tuple[float, float, int, int]:
    state_path = root / "state.db"
    source_path = root / "synthetic.pdf"
    source_path.write_bytes(b"%PDF-1.7\n% Parsezen synthetic benchmark\n")
    state = StateStore(state_path)
    queue = JobQueue()
    source = DocumentSource.inspect(source_path)
    job = queue.add(source, JobConfiguration(), job_id="synthetic-benchmark")
    state.replace_jobs(queue.jobs)
    artifacts = ArtifactStore(root / "artifacts")
    snapshots = ResultSnapshotStore(state, artifacts)
    result = ProcessResult(
        final_path=root / "synthetic-result.md",
        review_markdown=review_text,
        review_required=True,
    )

    sampler = _DirectorySizeSampler(root)
    sampler.start()
    try:
        started = monotonic()
        snapshots.save(job.id, result)
        write_seconds = monotonic() - started
        sampler.observe()

        reopened = ResultSnapshotStore(StateStore(state_path), ArtifactStore(root / "artifacts"))
        started = monotonic()
        recovered = reopened.load(job.id)
        recovery_seconds = monotonic() - started
        sampler.observe()
        if recovered is None or recovered.review_markdown != review_text:
            raise RuntimeError("La instantánea sintética no se recuperó íntegramente.")
    finally:
        temporary_peak = sampler.stop()
    snapshot_disk = _tree_size(root / "artifacts")
    return write_seconds, recovery_seconds, snapshot_disk, temporary_peak


class _DirectorySizeSampler:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._stop = threading.Event()
        self._peak = 0
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def start(self) -> None:
        self.observe()
        self._thread.start()

    def observe(self) -> None:
        self._peak = max(self._peak, _tree_size(self._root))

    def stop(self) -> int:
        self._stop.set()
        self._thread.join()
        self.observe()
        return self._peak

    def _sample(self) -> None:
        while not self._stop.wait(0.005):
            self.observe()


def _tree_size(root: Path) -> int:
    if root.is_file():
        return root.stat().st_size
    total = 0
    for candidate in root.rglob("*"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _synthetic_payload(size: int) -> bytes:
    pattern = b"Parsezen synthetic runtime payload.\n"
    return (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]


_STARTUP_PROBE = """
import os
from pathlib import Path
from PySide6.QtWidgets import QApplication
from parsezen.presentation.main_window import ParsezenMainWindow

root = Path(os.environ["PARSEZEN_BENCHMARK_ROOT"])
application = QApplication.instance() or QApplication([])
window = ParsezenMainWindow(
    auto_discover_ai=False,
    history_path=root / "recent.json",
    work_checkpoint_root=root / "checkpoints",
    state_path=root / "state.db",
)
window.close()
application.processEvents()
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mide costes locales con datos sintéticos, sin abrir documentos del usuario."
    )
    parser.add_argument("profile", type=Path)
    parser.add_argument("installation_root", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--payload-mib", type=int, default=1)
    parser.add_argument("--startup-runs", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        metrics = measure_runtime(
            arguments.installation_root,
            installer_path=arguments.installer,
            payload_mib=arguments.payload_mib,
            startup_runs=arguments.startup_runs,
        )
        write_profile(arguments.profile, metrics)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"Perfil guardado: arranque {metrics.cold_start_seconds:.2f} s, "
        f"snapshot {metrics.snapshot_write_seconds:.3f} s, "
        f"recuperación {metrics.snapshot_recovery_seconds:.3f} s."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
