from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from parsezen.local_ai_policy import (
    POLICY_VERSION,
    ComponentCapability,
    ComponentManifest,
    LocalComponentManifestError,
    read_ollama_model_identities,
    read_ollama_show_metadata,
    verify_component_manifest,
)

_OLLAMA_DIGEST = "a" * 64
_TEMPLATE = "{{ .Prompt }}"
_PARAMETERS = "temperature 0.2\nnum_ctx 8192"
_LICENSE_TEXT = "Apache License 2.0\nCopyright upstream"


def _manifest(**overrides: object) -> ComponentManifest:
    values: dict[str, object] = {
        "capability": ComponentCapability.TRANSLATION,
        "policy_version": POLICY_VERSION,
        "model_name": "example/translator:7b-q4_k_m",
        "ollama_digest": _OLLAMA_DIGEST,
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
        "license_sha256": hashlib.sha256(_LICENSE_TEXT.encode()).hexdigest(),
        "required_notices": ("upstream-notice.txt",),
        "distribution_decision": "redistributable-with-notice",
        "upstream_repository": "https://huggingface.co/example/translator",
        "upstream_revision": "0123456789abcdef",
        "upstream_file": "translator-q4_k_m.gguf",
        "upstream_sha256": "b" * 64,
        "prompt_template_sha256": hashlib.sha256(_TEMPLATE.encode()).hexdigest(),
        "model_parameters_sha256": hashlib.sha256(_PARAMETERS.encode()).hexdigest(),
        "minimum_ollama_version": "0.9.0",
        "tested_ollama_versions": ("0.9.0", "0.10.1"),
        "test_vectors": ("translation-smoke-01", "translation-smoke-02"),
        "ollama_capabilities": ("completion",),
    }
    values.update(overrides)
    return ComponentManifest(**values)


def _metadata_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/version":
        assert request.method == "GET"
        return httpx.Response(200, json={"version": "0.10.1"})
    if request.url.path == "/api/tags":
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "example/translator:7b-q4_k_m",
                        "digest": _OLLAMA_DIGEST,
                        "details": {
                            "format": "gguf",
                            "family": "llama",
                            "families": ["llama"],
                            "quantization_level": "q4_k_m",
                        },
                    }
                ]
            },
        )
    assert request.url.path == "/api/show"
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "model": "example/translator:7b-q4_k_m",
        "verbose": False,
    }
    return httpx.Response(
        200,
        json={
            "template": _TEMPLATE,
            "parameters": _PARAMETERS,
            "license": _LICENSE_TEXT,
            "details": {
                "format": "gguf",
                "family": "llama",
                "families": ["llama"],
                "quantization_level": "q4_k_m",
                "context_length": 8192,
            },
            "capabilities": ["completion"],
        },
    )


def test_manifest_covers_concrete_capabilities_and_freezes_parameters() -> None:
    assert {capability.value for capability in ComponentCapability} == {
        "translation",
        "review",
        "visual",
    }
    manifest = _manifest()
    assert manifest.parameters["num_ctx"] == 8192
    assert manifest.license_sha256 == hashlib.sha256(_LICENSE_TEXT.encode()).hexdigest()
    with pytest.raises(TypeError):
        manifest.parameters["temperature"] = 0.8  # type: ignore[index]


def test_manifest_rejects_unknown_or_unbounded_values() -> None:
    with pytest.raises(ValueError, match="capability"):
        _manifest(capability="chat")
    with pytest.raises(ValueError, match="generation_parameters"):
        _manifest(generation_parameters={"prompt": ["private text"]})
    with pytest.raises(ValueError, match="ollama_digest"):
        _manifest(ollama_digest="not-a-digest")
    with pytest.raises(ValueError, match="cloud"):
        _manifest(model_name="example/model:cloud")
    canonical = _manifest(ollama_digest=f"sha256:{'A' * 64}")
    assert canonical.ollama_digest == "a" * 64


def test_reads_only_safe_identity_fields_from_local_tags() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "models": [
                    {
                        "model": "safe:7b",
                        "digest": _OLLAMA_DIGEST,
                        "details": {
                            "format": "gguf",
                            "family": "llama",
                            "families": ["llama"],
                            "quantization_level": "q4_k_m",
                        },
                        "unexpected": "ignored",
                    },
                    {"model": "unsafe\nmodel", "digest": _OLLAMA_DIGEST},
                    {"model": "remote:cloud", "digest": _OLLAMA_DIGEST},
                ]
            },
        )
    )

    identities = read_ollama_model_identities(transport=transport)

    assert identities[0].model_name == "safe:7b"
    assert identities[0].digest == _OLLAMA_DIGEST
    assert identities[0].families == ("llama",)
    assert identities[0].format == "gguf"
    assert len(identities) == 1


def test_unknown_quantization_remains_a_bounded_reported_value() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "models": [
                    {
                        "model": "lfm:3b",
                        "digest": _OLLAMA_DIGEST,
                        "details": {
                            "format": "gguf",
                            "family": "lfm",
                            "families": ["lfm"],
                            "quantization_level": "unknown",
                        },
                    }
                ]
            },
        )
    )

    identity = read_ollama_model_identities(transport=transport)[0]
    manifest = _manifest(family="lfm", families=("lfm",), quantization="unknown")

    assert identity.quantization == "unknown"
    assert manifest.quantization == "unknown"


def test_tags_unknown_quantization_defers_to_exact_show_metadata() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = _metadata_response(request)
        if request.url.path == "/api/tags":
            payload = response.json()
            payload["models"][0]["details"]["quantization_level"] = "unknown"
            return httpx.Response(200, json=payload)
        return response

    result = verify_component_manifest(
        _manifest(),
        transport=httpx.MockTransport(respond),
    )

    assert result.valid


def test_model_may_expose_more_context_than_the_frozen_runtime_policy() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = _metadata_response(request)
        if request.url.path == "/api/show":
            payload = response.json()
            payload["details"]["context_length"] = 131072
            return httpx.Response(200, json=payload)
        return response

    result = verify_component_manifest(
        _manifest(context_window=8192),
        transport=httpx.MockTransport(respond),
    )

    assert result.valid


def test_verifies_digest_and_show_metadata_without_document_content() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _metadata_response(request)

    result = verify_component_manifest(
        _manifest(),
        transport=httpx.MockTransport(respond),
    )

    assert result.valid
    assert result.issues == ()
    assert result.ollama_version == "0.10.1"
    assert [request.url.path for request in requests] == [
        "/api/version",
        "/api/tags",
        "/api/show",
    ]
    assert b"Prompt" not in requests[2].content
    assert b"document" not in requests[2].content
    assert b"Apache License" not in requests[2].content


def test_verification_rejects_changed_digest_or_show_template() -> None:
    def changed(request: httpx.Request) -> httpx.Response:
        response = _metadata_response(request)
        if request.url.path == "/api/tags":
            payload = response.json()
            payload["models"][0]["digest"] = f"sha256:{'c' * 64}"
            return httpx.Response(200, json=payload)
        return response

    digest_result = verify_component_manifest(_manifest(), transport=httpx.MockTransport(changed))
    assert not digest_result.valid
    assert digest_result.issues == ("ollama_digest_mismatch",)

    def changed_template(request: httpx.Request) -> httpx.Response:
        response = _metadata_response(request)
        if request.url.path == "/api/show":
            payload = response.json()
            payload["template"] = "changed"
            return httpx.Response(200, json=payload)
        return response

    template_result = verify_component_manifest(
        _manifest(), transport=httpx.MockTransport(changed_template)
    )
    assert not template_result.valid
    assert template_result.issues == ("prompt_template_mismatch",)


def test_verification_rejects_old_or_untrusted_ollama_versions() -> None:
    def old_version(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.8.9"})
        return _metadata_response(request)

    old_result = verify_component_manifest(_manifest(), transport=httpx.MockTransport(old_version))
    assert not old_result.valid
    assert old_result.issues == ("ollama_version_too_old",)
    assert old_result.tags is None

    def untested_version(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.11.0"})
        return _metadata_response(request)

    untested_result = verify_component_manifest(
        _manifest(), transport=httpx.MockTransport(untested_version)
    )
    assert not untested_result.valid
    assert untested_result.issues == ("ollama_version_not_tested",)


def test_license_hash_mismatch_does_not_retain_license_text() -> None:
    def changed_license(request: httpx.Request) -> httpx.Response:
        response = _metadata_response(request)
        if request.url.path == "/api/show":
            payload = response.json()
            payload["license"] = "changed license text"
            return httpx.Response(200, json=payload)
        return response

    result = verify_component_manifest(_manifest(), transport=httpx.MockTransport(changed_license))

    assert not result.valid
    assert result.issues == ("license_sha256_mismatch",)
    assert result.show is not None
    assert result.show.license_sha256 == hashlib.sha256(b"changed license text").hexdigest()
    assert not hasattr(result.show, "license")


def test_show_request_is_content_free_and_redirects_are_blocked() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://example.test"})

    with pytest.raises(LocalComponentManifestError, match="redirigir"):
        read_ollama_show_metadata(
            "example/translator:7b-q4_k_m",
            transport=httpx.MockTransport(respond),
        )
    assert requests[0].url.host == "127.0.0.1"
