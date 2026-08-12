from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import parsezen.model_recommendations as recommendations_module
from parsezen.errors import ModelRecommendationError
from parsezen.model_recommendations import (
    RecommendationOrigin,
    RecommendationRole,
    ensure_llmfit,
    parse_llmfit_recommendations,
    recommend_ollama_models,
)


def _llmfit_output() -> str:
    def model(
        ollama_name: str | None,
        *,
        name: str,
        fit: str = "Perfect",
        category: str = "General",
        score: float = 85.0,
        parameter_count: str = "4B",
        estimated_tps: float = 42.6,
        memory_required_gb: float = 4.75,
    ) -> dict[str, object]:
        return {
            "name": name,
            "ollama_name": ollama_name,
            "fit_level": fit,
            "category": category,
            "parameter_count": parameter_count,
            "disk_size_gb": 2.5,
            "effective_context_length": 8192,
            "estimated_tps": estimated_tps,
            "memory_required_gb": memory_required_gb,
            "score": score,
        }

    return json.dumps(
        {
            "models": [
                model(None, name="No Ollama mapping", score=99.0),
                model("remote:cloud", name="Cloud model", score=98.0),
                model(
                    "llama3.2:3b",
                    name="Llama",
                    score=90.0,
                    estimated_tps=40.0,
                ),
                model("llama3.2:3b", name="Duplicate", score=89.0),
                model(
                    "gemma3:3b",
                    name="Gemma",
                    fit="Good",
                    score=88.0,
                    parameter_count="3B",
                    estimated_tps=72.0,
                    memory_required_gb=3.8,
                ),
                model("nomic-embed-text", name="Embedding", category="Embedding"),
                model(
                    "qwen3:8b-instruct",
                    name="Qwen",
                    fit="Good",
                    score=84.0,
                    parameter_count="8B",
                    estimated_tps=18.0,
                    memory_required_gb=9.2,
                ),
                model("phi4-mini:3.8b", name="Phi", fit="Good", score=83.0),
                model("oversized:70b", name="Too large", fit="Too_Tight", score=100.0),
            ],
            "system": {
                "total_ram_gb": 31.3,
                "gpu_name": "AMD Radeon(TM) 610M",
                "gpu_vram_gb": 8.0,
                "gpus": [
                    {
                        "name": "AMD Radeon(TM) 610M",
                        "backend": "Vulkan",
                        "vram_gb": 8.0,
                    },
                    {
                        "name": "NVIDIA GeForce RTX 5060 Laptop GPU",
                        "backend": "CUDA",
                        "vram_gb": 7.96,
                    },
                ],
            },
        }
    )


def _release_transport(
    archive: bytes,
    *,
    digest: str | None = None,
    include_incomplete_latest: bool = False,
) -> httpx.MockTransport:
    version = "1.2.3"
    filename = f"llmfit-v{version}-x86_64-pc-windows-msvc.zip"
    asset_url = f"https://github.com/AlexsJones/llmfit/releases/download/v{version}/{filename}"
    expected_digest = digest or hashlib.sha256(archive).hexdigest()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            releases: list[dict[str, object]] = [
                {
                    "tag_name": f"v{version}",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": filename,
                            "size": len(archive),
                            "digest": f"sha256:{expected_digest}",
                            "browser_download_url": asset_url,
                        }
                    ],
                }
            ]
            if include_incomplete_latest:
                releases.insert(
                    0,
                    {
                        "tag_name": "v1.2.4",
                        "draft": False,
                        "prerelease": False,
                        "assets": [],
                    },
                )
            return httpx.Response(200, json=releases)
        assert str(request.url) == asset_url
        return httpx.Response(200, content=archive)

    return httpx.MockTransport(respond)


def _windows_archive() -> bytes:
    destination = io.BytesIO()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("llmfit-v1.2.3-x86_64-pc-windows-msvc/llmfit.exe", b"MZ" + b"x" * 1024)
        package.writestr(
            "llmfit-v1.2.3-x86_64-pc-windows-msvc/LICENSE",
            "MIT License\n\nCopyright (c) llmfit contributors\n",
        )
    return destination.getvalue()


def _trusted_test_archives(archive: bytes) -> dict[tuple[str, str], str]:
    return {
        (
            "1.2.3",
            "x86_64-pc-windows-msvc",
        ): hashlib.sha256(archive).hexdigest()
    }


def test_selects_balanced_fast_and_capacity_profiles_for_the_discrete_gpu() -> None:
    generated_at = datetime(2026, 7, 21, 10, tzinfo=UTC)

    result = parse_llmfit_recommendations(
        _llmfit_output(),
        version="1.2.3",
        generated_at=generated_at,
    )

    assert [model.model_id for model in result.models] == [
        "llama3.2:3b",
        "gemma3:3b",
        "qwen3:8b-instruct",
    ]
    assert [model.role for model in result.models] == [
        RecommendationRole.BALANCED,
        RecommendationRole.FASTER,
        RecommendationRole.CAPACITY,
    ]
    assert result.models[2].parameter_count_b == 8.0
    assert result.models[0].download_size_bytes == 2_500_000_000
    assert result.models[0].recommended_context == 8_192
    assert "mejor opción" in result.models[0].menu_label(0)
    assert "RTX 5060 Laptop GPU" in result.hardware_summary
    assert "32 GB de RAM" in result.hardware_summary


def test_capacity_profile_requires_headroom_and_usable_speed() -> None:
    def candidate(
        model_id: str,
        name: str,
        *,
        fit: str,
        parameters: str,
        speed: float,
        memory: float,
        score: float,
    ) -> dict[str, object]:
        return {
            "name": name,
            "ollama_name": model_id,
            "fit_level": fit,
            "category": "Chat",
            "parameter_count": parameters,
            "disk_size_gb": memory,
            "effective_context_length": 8192,
            "estimated_tps": speed,
            "memory_required_gb": memory,
            "score": score,
        }

    output = json.dumps(
        {
            "models": [
                candidate(
                    "balanced:4b",
                    "vendor/Balanced-4B-Instruct",
                    fit="Perfect",
                    parameters="4B",
                    speed=40,
                    memory=5,
                    score=90,
                ),
                candidate(
                    "fast:1b",
                    "vendor/Fast-1B-Instruct",
                    fit="Perfect",
                    parameters="1B",
                    speed=100,
                    memory=2,
                    score=82,
                ),
                candidate(
                    "unsafe:12b",
                    "vendor/Unsafe-12B-Instruct",
                    fit="Marginal",
                    parameters="12B",
                    speed=4,
                    memory=14,
                    score=86,
                ),
                candidate(
                    "capacity:8b",
                    "vendor/Capacity-8B-Instruct",
                    fit="Good",
                    parameters="8B",
                    speed=16,
                    memory=9,
                    score=78,
                ),
            ],
            "system": {"total_ram_gb": 32},
        }
    )

    result = parse_llmfit_recommendations(output, version="1.2.3")

    assert [item.model_id for item in result.models] == [
        "balanced:4b",
        "fast:1b",
        "capacity:8b",
    ]
    assert result.models[2].role is RecommendationRole.CAPACITY


def test_repackaged_winner_resolves_to_a_verified_specific_ollama_tag() -> None:
    output = json.dumps(
        {
            "models": [
                {
                    "name": "unsloth/Qwen3-4B-Instruct-2507-unsloth-bnb-4bit",
                    "ollama_name": None,
                    "fit_level": "Perfect",
                    "category": "Chat",
                    "parameter_count": "3.1B",
                    "disk_size_gb": 3.28,
                    "effective_context_length": 4096,
                    "estimated_tps": 42.2,
                    "memory_required_gb": 3.88,
                    "score": 88.2,
                    "best_quant": "Q8_0",
                },
                {
                    "name": "unsloth/Qwen3-4B-Instruct-2507",
                    "ollama_name": "qwen3:4b",
                    "fit_level": "Perfect",
                    "category": "Chat",
                    "parameter_count": "4.0B",
                    "disk_size_gb": 4.22,
                    "effective_context_length": 8192,
                    "estimated_tps": 32.8,
                    "memory_required_gb": 5.85,
                    "score": 81.9,
                    "best_quant": "Q8_0",
                },
            ],
            "system": {"total_ram_gb": 32},
        }
    )
    looked_up: list[str] = []

    def manifest_size(model_id: str) -> int | None:
        looked_up.append(model_id)
        return 4_280_417_796

    result = parse_llmfit_recommendations(
        output,
        version="1.2.3",
        manifest_loader=manifest_size,
    )

    assert looked_up == ["qwen3:4b-instruct-2507-q8_0"]
    assert result.models[0].model_id == "qwen3:4b-instruct-2507-q8_0"
    assert result.models[0].display_name == "Qwen3 4B Instruct Q8"
    assert result.models[0].download_size_bytes == 4_280_417_796
    assert result.models[0].estimated_tokens_per_second == 32.8
    assert result.models[0].memory_required_gb == 5.85
    assert result.models[0].recommended_context == 8192
    assert result.models[0].score == 88.2


def test_reasoning_variant_is_not_recommended_without_a_verified_instruct_tag() -> None:
    output = json.dumps(
        {
            "models": [
                {
                    "name": "Qwen/Qwen3-4B",
                    "ollama_name": "qwen3:4b",
                    "fit_level": "Perfect",
                    "category": "Chat",
                    "score": 90,
                }
            ],
            "system": {"total_ram_gb": 32},
        }
    )

    with pytest.raises(ModelRecommendationError, match="adecuado"):
        parse_llmfit_recommendations(output, version="1.2.3")


def test_unrelated_unmapped_model_is_not_guessed_from_another_family() -> None:
    output = json.dumps(
        {
            "models": [
                {
                    "name": "vendor/FutureOne-4B-Instruct",
                    "ollama_name": None,
                    "fit_level": "Perfect",
                    "category": "Chat",
                    "score": 99,
                    "best_quant": "Q8_0",
                },
                {
                    "name": "vendor/FutureTwo-4B-Instruct",
                    "ollama_name": "futuretwo:4b",
                    "fit_level": "Perfect",
                    "category": "Chat",
                    "parameter_count": "4B",
                    "disk_size_gb": 4,
                    "score": 80,
                },
            ],
            "system": {},
        }
    )
    looked_up: list[str] = []

    result = parse_llmfit_recommendations(
        output,
        version="1.2.3",
        manifest_loader=lambda model_id: looked_up.append(model_id),
    )

    assert result.models[0].model_id == "futuretwo:4b"
    assert looked_up == []


def test_ollama_manifest_lookup_verifies_tag_and_uses_real_layer_size() -> None:
    layers = [
        {
            "mediaType": "application/vnd.ollama.image.model",
            "digest": f"sha256:{'a' * 64}",
            "size": 4_280_404_960,
        },
        {
            "mediaType": "application/vnd.ollama.image.template",
            "digest": f"sha256:{'b' * 64}",
            "size": 1_379,
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == (
            "https://registry.ollama.ai/v2/library/qwen3/manifests/4b-instruct-2507-q8_0"
        )
        return httpx.Response(
            200,
            json={"schemaVersion": 2, "layers": layers},
        )

    size = recommendations_module._fetch_ollama_manifest_size(
        "qwen3:4b-instruct-2507-q8_0",
        transport=httpx.MockTransport(respond),
    )

    assert size == 4_280_406_339


@pytest.mark.parametrize("status", [302, 404, 503])
def test_ollama_manifest_lookup_fails_closed(status: int) -> None:
    response = (
        httpx.Response(status, headers={"Location": "https://example.com"})
        if status == 302
        else httpx.Response(status)
    )

    assert (
        recommendations_module._fetch_ollama_manifest_size(
            "qwen3:4b-instruct-2507-q8_0",
            transport=httpx.MockTransport(lambda _request: response),
        )
        is None
    )


def test_managed_llmfit_install_is_hash_checked_atomic_and_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _windows_archive()
    transport = _release_transport(archive)
    checked_versions: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        recommendations_module,
        "_validate_downloaded_executable",
        lambda path, version: checked_versions.append((path, version)),
    )
    now = datetime(2026, 7, 21, 10, tzinfo=UTC)

    component = ensure_llmfit(
        component_directory=tmp_path,
        transport=transport,
        now=now,
        platform_name="win32",
        machine_name="AMD64",
        trusted_archives=_trusted_test_archives(archive),
    )

    assert component.version == "1.2.3"
    assert component.executable.read_bytes().startswith(b"MZ")
    assert "MIT License" in component.license_path.read_text(encoding="utf-8")
    assert checked_versions and checked_versions[0][1] == "1.2.3"
    metadata = json.loads((tmp_path / "component.json").read_text(encoding="utf-8"))
    assert metadata["repository"] == "AlexsJones/llmfit"
    assert metadata["target"] == "x86_64-pc-windows-msvc"
    assert metadata["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    assert metadata["executable_sha256"] == component.executable_sha256
    assert metadata["license_sha256"] == component.license_sha256

    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A fresh component must not use the network")

    reused = ensure_llmfit(
        component_directory=tmp_path,
        transport=httpx.MockTransport(unexpected_request),
        now=now + timedelta(days=1),
        platform_name="win32",
        machine_name="AMD64",
        trusted_archives=_trusted_test_archives(archive),
    )
    assert reused == component


def test_incomplete_latest_release_falls_back_to_latest_usable_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recommendations_module,
        "_validate_downloaded_executable",
        lambda _path, _version: None,
    )

    archive = _windows_archive()
    component = ensure_llmfit(
        component_directory=tmp_path,
        transport=_release_transport(archive, include_incomplete_latest=True),
        now=datetime(2026, 7, 21, 10, tzinfo=UTC),
        platform_name="win32",
        machine_name="AMD64",
        trusted_archives=_trusted_test_archives(archive),
    )

    assert component.version == "1.2.3"
    assert component.executable.exists()


def test_existing_verified_llmfit_remains_available_while_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recommendations_module,
        "_validate_downloaded_executable",
        lambda _path, _version: None,
    )
    now = datetime(2026, 7, 21, 10, tzinfo=UTC)
    archive = _windows_archive()
    installed = ensure_llmfit(
        component_directory=tmp_path,
        transport=_release_transport(archive),
        now=now,
        platform_name="win32",
        machine_name="AMD64",
        trusted_archives=_trusted_test_archives(archive),
    )

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    reused = ensure_llmfit(
        component_directory=tmp_path,
        transport=httpx.MockTransport(offline),
        now=now + timedelta(days=8),
        platform_name="win32",
        machine_name="AMD64",
        trusted_archives=_trusted_test_archives(archive),
    )

    assert reused.executable == installed.executable
    assert reused.executable_sha256 == installed.executable_sha256


def test_unverified_llmfit_download_is_never_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        recommendations_module,
        "_validate_downloaded_executable",
        lambda _path, _version: None,
    )

    archive = _windows_archive()
    with pytest.raises(ModelRecommendationError, match="recomendador automático"):
        ensure_llmfit(
            component_directory=tmp_path,
            transport=_release_transport(archive, digest="0" * 64),
            now=datetime(2026, 7, 21, 10, tzinfo=UTC),
            platform_name="win32",
            machine_name="AMD64",
            trusted_archives=_trusted_test_archives(archive),
        )

    assert not (tmp_path / "llmfit.exe").exists()


def test_a_github_digest_without_parsezen_pinned_trust_is_never_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions: list[Path] = []
    monkeypatch.setattr(
        recommendations_module,
        "_validate_downloaded_executable",
        lambda path, _version: executions.append(path),
    )

    with pytest.raises(ModelRecommendationError, match="recomendador automático"):
        ensure_llmfit(
            component_directory=tmp_path,
            transport=_release_transport(_windows_archive()),
            now=datetime(2026, 7, 21, 10, tzinfo=UTC),
            platform_name="win32",
            machine_name="AMD64",
        )

    assert executions == []
    assert not (tmp_path / "llmfit.exe").exists()


def test_recommendations_use_a_recent_or_stale_cache_when_needed(tmp_path: Path) -> None:
    cache_path = tmp_path / "recommendations.json"
    now = datetime(2026, 7, 21, 10, tzinfo=UTC)
    live = recommend_ollama_models(
        cache_path=cache_path,
        now=now,
        force_refresh=True,
        executable=tmp_path / "llmfit.exe",
        component_version="1.2.3",
        runner=lambda _executable: _llmfit_output(),
    )
    assert live.origin is RecommendationOrigin.LIVE
    assert cache_path.exists()

    recent = recommend_ollama_models(
        cache_path=cache_path,
        now=now + timedelta(hours=1),
        executable=tmp_path / "missing.exe",
        component_version="1.2.3",
        runner=lambda _executable: "invalid",
    )
    assert recent.origin is RecommendationOrigin.CACHE

    def fail(_executable: Path) -> str:
        raise ModelRecommendationError("unavailable")

    stale = recommend_ollama_models(
        cache_path=cache_path,
        now=now + timedelta(days=30),
        force_refresh=True,
        executable=tmp_path / "missing.exe",
        component_version="1.2.3",
        runner=fail,
    )
    assert stale.origin is RecommendationOrigin.CACHE

    with pytest.raises(ModelRecommendationError, match="calcular recomendaciones"):
        recommend_ollama_models(
            cache_path=cache_path,
            now=now + timedelta(days=91),
            force_refresh=True,
            executable=tmp_path / "missing.exe",
            component_version="1.2.3",
            runner=fail,
        )


def test_old_recommendation_cache_is_invalidated_after_layout_changes(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "recommendations.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generated_at": "2026-07-21T10:00:00+00:00",
                "llmfit_version": "1.2.3",
                "hardware_summary": "Equipo antiguo",
                "models": [],
            }
        ),
        encoding="utf-8",
    )

    assert recommendations_module._load_recommendation_cache(cache_path) is None


def test_reasoning_model_recommendation_cache_is_invalidated(tmp_path: Path) -> None:
    cache_path = tmp_path / "recommendations.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": recommendations_module.RECOMMENDATION_CACHE_SCHEMA_VERSION,
                "generated_at": "2026-07-21T10:00:00+00:00",
                "llmfit_version": "1.2.3",
                "hardware_summary": "Equipo local",
                "models": [
                    {
                        "model_id": "qwen3:4b",
                        "display_name": "Qwen3 4B",
                        "description": "Anterior",
                        "download_size_bytes": 2_500_000_000,
                        "recommended_context": 4096,
                        "estimated_tokens_per_second": 20,
                        "memory_required_gb": 4,
                        "score": 80,
                        "parameter_count_b": 4,
                        "role": "balanced",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert recommendations_module._load_recommendation_cache(cache_path) is None


def test_llmfit_runner_disables_the_dashboard_and_remote_benchmark_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "llmfit.exe"
    executable.write_bytes(b"MZ")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout=_llmfit_output(), stderr="")

    monkeypatch.setenv("LOCALMAXXING_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-forwarded-either")
    monkeypatch.setattr(recommendations_module.subprocess, "run", run)

    output = recommendations_module._run_llmfit(executable)

    assert json.loads(output)["models"]
    command, options = calls[0]
    assert command == [
        str(executable),
        "recommend",
        "--json",
        "--limit",
        "500",
        "--use-case",
        "chat",
        "--no-dashboard",
    ]
    environment = options["env"]
    assert isinstance(environment, dict)
    assert "LOCALMAXXING_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["LLMFIT_DASHBOARD_HOST"] == "127.0.0.1"


@pytest.mark.parametrize(
    "output",
    [
        "not-json",
        "[]",
        '{"models": {}}',
        '{"models": [{"ollama_name": null}]}',
    ],
)
def test_recommendation_parser_rejects_incompatible_or_empty_results(output: str) -> None:
    with pytest.raises(ModelRecommendationError):
        parse_llmfit_recommendations(output, version="1.2.3")


def test_llmfit_package_must_include_its_mit_license() -> None:
    destination = io.BytesIO()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("llmfit/llmfit.exe", b"MZ" + b"x" * 1024)

    with pytest.raises(ModelRecommendationError, match="licencia"):
        recommendations_module._extract_llmfit_package(destination.getvalue())


@pytest.mark.parametrize(
    ("platform_name", "machine_name"),
    [("linux", "x86_64"), ("win32", "sparc")],
)
def test_managed_llmfit_rejects_unsupported_systems(
    platform_name: str,
    machine_name: str,
) -> None:
    with pytest.raises(ModelRecommendationError):
        recommendations_module._release_target(platform_name, machine_name)
