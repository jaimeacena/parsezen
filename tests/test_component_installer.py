from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

import parsezen.component_installer as installer
from parsezen.component_catalog import (
    REVIEW_MODEL_NAME,
    REVIEW_OLLAMA_SOURCE_MODEL,
    TRANSLATION_LICENSE_SHA256,
    TRANSLATION_MODEL_NAME,
    TRANSLATION_OLLAMA_SOURCE_MODEL,
)
from parsezen.errors import LocalModelUnavailableError
from parsezen.local_ai_policy import ComponentCapability, ComponentVerification
from parsezen.local_models import LocalHardware


def _hardware() -> LocalHardware:
    return LocalHardware(
        ram_total_mebibytes=32_768,
        ram_available_mebibytes=16_384,
        disk_free_bytes=50_000_000_000,
    )


@pytest.mark.parametrize(
    ("capability", "source", "alias", "final_path"),
    [
        (
            ComponentCapability.TRANSLATION,
            TRANSLATION_OLLAMA_SOURCE_MODEL,
            TRANSLATION_MODEL_NAME,
            "/api/create",
        ),
        (
            ComponentCapability.REVIEW,
            REVIEW_OLLAMA_SOURCE_MODEL,
            REVIEW_MODEL_NAME,
            "/api/copy",
        ),
    ],
)
def test_installer_uses_only_fixed_source_and_verifies_final_alias(
    monkeypatch: Any,
    capability: ComponentCapability,
    source: str,
    alias: str,
    final_path: str,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    verifications = iter(
        (
            ComponentVerification(False, ("model_not_installed",)),
            ComponentVerification(True),
        )
    )
    monkeypatch.setattr(
        installer, "verify_component_manifest", lambda *_a, **_k: next(verifications)
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, payload))
        if request.url.path == "/api/pull":
            return httpx.Response(
                200,
                text='{"status":"pulling manifest"}\n{"status":"success"}\n',
            )
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/api/copy":
            return httpx.Response(200)
        raise AssertionError(request.url.path)

    result = installer.install_product_component(
        capability,
        transport=httpx.MockTransport(respond),
        hardware=_hardware(),
        local_only_configured=True,
    )

    assert result.valid
    assert [path for path, _payload in requests] == ["/api/pull", final_path]
    assert requests[0][1] == {"model": source, "stream": True}
    final_payload = requests[1][1]
    if capability is ComponentCapability.TRANSLATION:
        assert final_payload["model"] == alias
        assert final_payload["from"] == source
        assert final_payload["stream"] is False
        license_text = final_payload["license"]
        assert isinstance(license_text, str)
        assert hashlib.sha256(license_text.encode()).hexdigest() == TRANSLATION_LICENSE_SHA256
    else:
        assert final_payload == {"source": source, "destination": alias}


def test_installer_fails_closed_before_download_on_identity_mismatch(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        installer,
        "verify_component_manifest",
        lambda *_a, **_k: ComponentVerification(False, ("ollama_digest_mismatch",)),
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    with pytest.raises(LocalModelUnavailableError, match="requisitos locales"):
        installer.install_product_component(
            ComponentCapability.TRANSLATION,
            transport=httpx.MockTransport(respond),
            hardware=_hardware(),
            local_only_configured=True,
        )

    assert requests == []


def test_installer_rejects_non_product_capability() -> None:
    with pytest.raises(LocalModelUnavailableError, match="no forma parte"):
        installer.install_product_component(
            ComponentCapability.VISUAL,
            hardware=_hardware(),
            local_only_configured=True,
        )
