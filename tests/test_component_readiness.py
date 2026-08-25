from __future__ import annotations

import hashlib

import httpx

from parsezen.component_readiness import (
    ComponentCatalogEntry,
    ReadinessStatus,
    evaluate_component_catalog,
    evaluate_component_readiness,
    inspect_component_catalog,
)
from parsezen.local_ai_policy import (
    POLICY_VERSION,
    ComponentCapability,
    ComponentManifest,
    ComponentVerification,
    OllamaModelIdentity,
)
from parsezen.local_models import ComponentRequirements, LocalHardware

_DIGEST = "a" * 64


def _manifest(**overrides: object) -> ComponentManifest:
    values: dict[str, object] = {
        "capability": ComponentCapability.TRANSLATION,
        "policy_version": POLICY_VERSION,
        "model_name": "example/translator:7b-q4_k_m",
        "ollama_digest": _DIGEST,
        "family": "llama",
        "families": ("llama",),
        "format": "gguf",
        "quantization": "q4_k_m",
        "context_window": 8192,
        "adapter": "translation-v1",
        "prompt_version": "translation-prompt-v1",
        "generation_parameters": {"temperature": 0.2, "num_ctx": 8192},
        "languages": ("en", "es"),
        "tasks": ("translate",),
        "license": "Apache-2.0",
        "license_sha256": hashlib.sha256(b"Apache-2.0").hexdigest(),
        "required_notices": (),
        "distribution_decision": "redistributable-with-notice",
        "upstream_repository": "https://huggingface.co/example/translator",
        "upstream_revision": "0123456789abcdef",
        "upstream_file": "translator.gguf",
        "upstream_sha256": "b" * 64,
        "prompt_template_sha256": hashlib.sha256(b"{{ .Prompt }}").hexdigest(),
        "model_parameters_sha256": hashlib.sha256(b"temperature 0.2\nnum_ctx 8192").hexdigest(),
        "minimum_ollama_version": "0.9.0",
        "tested_ollama_versions": ("0.9.0", "0.10.1"),
        "test_vectors": ("translation-smoke-01",),
        "ollama_capabilities": ("completion",),
    }
    values.update(overrides)
    return ComponentManifest(**values)


def _hardware() -> LocalHardware:
    return LocalHardware(
        ram_total_mebibytes=32_768,
        ram_available_mebibytes=8_192,
        disk_free_bytes=20_000_000_000,
        nvidia_vram_total_mebibytes=16_384,
        nvidia_vram_available_mebibytes=12_288,
    )


def _entry(**manifest_overrides: object) -> ComponentCatalogEntry:
    return ComponentCatalogEntry(
        _manifest(**manifest_overrides),
        ComponentRequirements(
            min_ram_mebibytes=4_096,
            min_disk_free_bytes=10_000_000_000,
        ),
    )


def _installed_verification(digest: str = _DIGEST) -> ComponentVerification:
    return ComponentVerification(
        True,
        tags=OllamaModelIdentity(
            model_name="example/translator:7b-q4_k_m",
            digest=digest,
            format="gguf",
            family="llama",
            families=("llama",),
            quantization="q4_k_m",
        ),
    )


def test_valid_local_component_is_prepared() -> None:
    result = evaluate_component_readiness(
        _entry(),
        _hardware(),
        _installed_verification(),
        local_only_configured=True,
    )

    assert result.status is ReadinessStatus.PREPARED
    assert result.reasons == ()
    assert result.prepared


def test_missing_model_is_downloadable_but_never_downloaded() -> None:
    result = evaluate_component_readiness(
        _entry(),
        _hardware(),
        ComponentVerification(False, ("model_not_installed",)),
        local_only_configured=True,
    )

    assert result.status is ReadinessStatus.DOWNLOADABLE
    assert result.reasons == ("model_not_installed",)


def test_local_only_requirement_and_unknown_hardware_fail_closed() -> None:
    no_local_mode = evaluate_component_readiness(
        _entry(),
        _hardware(),
        _installed_verification(),
        local_only_configured=False,
    )
    unknown_hardware = evaluate_component_readiness(
        _entry(),
        LocalHardware(ram_available_mebibytes=4_096),
        _installed_verification(),
        local_only_configured=True,
    )

    assert no_local_mode.status is ReadinessStatus.INSUFFICIENT
    assert no_local_mode.reasons == ("local_only_required",)
    assert unknown_hardware.status is ReadinessStatus.INSUFFICIENT
    assert unknown_hardware.reasons == ("hardware_unknown",)


def test_digest_mismatch_cannot_be_prepared() -> None:
    result = evaluate_component_readiness(
        _entry(),
        _hardware(),
        _installed_verification("c" * 64),
        local_only_configured=True,
    )

    assert result.status is ReadinessStatus.INSUFFICIENT
    assert result.reasons == ("ollama_digest_mismatch",)


def test_invalid_manifest_cannot_be_prepared() -> None:
    manifest = _manifest()
    object.__setattr__(manifest, "ollama_digest", "not-a-digest")

    result = evaluate_component_readiness(
        ComponentCatalogEntry(manifest, ComponentRequirements()),
        _hardware(),
        _installed_verification(),
        local_only_configured=True,
    )

    assert result.status is ReadinessStatus.INSUFFICIENT
    assert result.reasons == ("manifest_invalid",)


def test_catalog_is_explicit_and_keeps_components_independent() -> None:
    catalog = {
        ComponentCapability.TRANSLATION: _entry(),
        ComponentCapability.REVIEW: _entry(capability=ComponentCapability.REVIEW),
    }
    verifications = {
        ComponentCapability.TRANSLATION: _installed_verification(),
        ComponentCapability.REVIEW: ComponentVerification(False, ("model_not_installed",)),
    }

    result = evaluate_component_catalog(
        catalog,
        _hardware(),
        verifications,
        local_only_configured=True,
    )

    assert list(result) == [ComponentCapability.TRANSLATION, ComponentCapability.REVIEW]
    assert result[ComponentCapability.TRANSLATION].status is ReadinessStatus.PREPARED
    assert result[ComponentCapability.REVIEW].status is ReadinessStatus.DOWNLOADABLE


def test_inspection_uses_only_read_only_ollama_metadata() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.10.1"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        raise AssertionError("No debe solicitarse /api/show para un modelo ausente.")

    result = inspect_component_catalog(
        {ComponentCapability.TRANSLATION: _entry()},
        _hardware(),
        local_only_configured=True,
        transport=httpx.MockTransport(respond),
    )

    assert result[ComponentCapability.TRANSLATION].status is ReadinessStatus.DOWNLOADABLE
    assert [request.url.path for request in requests] == ["/api/version", "/api/tags"]
