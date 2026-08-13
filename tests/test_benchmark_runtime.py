from __future__ import annotations

import json
from pathlib import Path

import pytest
import scripts.benchmark_runtime as benchmark_module


def test_runtime_benchmark_uses_only_synthetic_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = tmp_path / "installation"
    installation.mkdir()
    (installation / "application.bin").write_bytes(b"installed")
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    monkeypatch.setattr(benchmark_module, "protect_for_current_user", lambda payload: payload[::-1])
    monkeypatch.setattr(
        benchmark_module, "unprotect_for_current_user", lambda payload: payload[::-1]
    )
    monkeypatch.setattr(benchmark_module, "measure_cold_start", lambda _runs: 0.25)
    monkeypatch.setattr(
        benchmark_module,
        "ArtifactStore",
        lambda root: __import__(
            "parsezen.infrastructure.artifact_store",
            fromlist=["ArtifactStore"],
        ).ArtifactStore(root, protect=lambda payload: payload, unprotect=lambda payload: payload),
    )

    metrics = benchmark_module.measure_runtime(
        installation,
        installer_path=installer,
        payload_mib=1,
        startup_runs=2,
    )
    profile = tmp_path / "profile.json"
    benchmark_module.write_profile(profile, metrics)
    raw = json.loads(profile.read_text(encoding="utf-8"))

    assert raw["schema_version"] == benchmark_module.SCHEMA_VERSION
    assert raw["metrics"]["payload_bytes"] == 1024 * 1024
    assert raw["metrics"]["installation_bytes"] == len(b"installed")
    assert raw["metrics"]["installer_bytes"] == len(b"installer")
    assert raw["metrics"]["snapshot_disk_bytes"] > 0
    assert raw["metrics"]["temporary_peak_bytes"] >= raw["metrics"]["snapshot_disk_bytes"]
    assert "synthetic runtime payload" not in profile.read_text(encoding="utf-8")
    assert str(tmp_path) not in profile.read_text(encoding="utf-8")


@pytest.mark.parametrize("payload_mib,startup_runs", [(0, 1), (65, 1), (1, 0), (1, 11)])
def test_runtime_benchmark_rejects_unbounded_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_mib: int,
    startup_runs: int,
) -> None:
    monkeypatch.setattr(benchmark_module, "protect_for_current_user", lambda payload: payload)
    monkeypatch.setattr(benchmark_module, "unprotect_for_current_user", lambda payload: payload)

    with pytest.raises(ValueError):
        benchmark_module.measure_runtime(
            tmp_path,
            payload_mib=payload_mib,
            startup_runs=startup_runs,
        )


def test_tree_size_ignores_symlinks_when_supported(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"12345")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(payload)
    except OSError:
        pytest.skip("La cuenta no permite crear symlinks.")

    assert benchmark_module._tree_size(tmp_path) == 5
