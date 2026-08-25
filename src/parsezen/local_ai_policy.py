"""Versioned manifests for Parsezen's specialized local AI components.

This module deliberately has no Qt or application-state dependencies.  It is a
small policy boundary for components that may be integrated later: the current
generic model selector does not consume these manifests yet.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

import httpx

from parsezen.errors import LocalModelUnavailableError
from parsezen.local_models import (
    OLLAMA_BASE_URL,
    is_cloud_model_id,
    validate_ollama_model_id,
)

POLICY_VERSION: Final = "local-ai-policy-v1"
DISCOVERY_TIMEOUT_SECONDS: Final = 10.0
MAX_METADATA_RESPONSE_BYTES: Final = 1024 * 1024
MAX_DISCOVERED_MODELS: Final = 200
MAX_MANIFEST_TEXT: Final = 256
MAX_MANIFEST_ITEMS: Final = 32
MAX_PARAMETER_KEY: Final = 64
MAX_PARAMETER_STRING: Final = 256
MAX_CONTEXT_WINDOW: Final = 1_048_576
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", flags=re.IGNORECASE)
_TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", flags=re.IGNORECASE)
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
_UPSTREAM_FILE_PATTERN = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,256}$")

type ParameterValue = bool | int | float | str


class ComponentCapability(StrEnum):
    """A concrete capability with an independent local product contract."""

    TRANSLATION = "translation"
    REVIEW = "review"
    VISUAL = "visual"


class LocalComponentManifestError(LocalModelUnavailableError):
    """A local component manifest or Ollama metadata response is unusable."""


@dataclass(frozen=True, slots=True)
class OllamaModelIdentity:
    """Safe, bounded identity fields announced by ``GET /api/tags``."""

    model_name: str
    digest: str | None
    format: str | None
    family: str | None
    families: tuple[str, ...]
    quantization: str | None


@dataclass(frozen=True, slots=True)
class OllamaShowMetadata:
    """Content-free metadata extracted from ``POST /api/show``."""

    model_name: str
    format: str | None
    family: str | None
    families: tuple[str, ...]
    quantization: str | None
    context_window: int | None
    template_sha256: str | None
    parameters_sha256: str | None
    license_sha256: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    """Reproducible identity and contract for one approved local component."""

    capability: ComponentCapability | str
    policy_version: str
    model_name: str
    ollama_digest: str
    family: str
    families: tuple[str, ...]
    format: str
    quantization: str
    context_window: int
    adapter: str
    prompt_version: str
    generation_parameters: Mapping[str, ParameterValue]
    languages: tuple[str, ...]
    tasks: tuple[str, ...]
    license: str
    license_sha256: str
    required_notices: tuple[str, ...]
    distribution_decision: str
    upstream_repository: str
    upstream_revision: str
    upstream_file: str
    upstream_sha256: str | None
    prompt_template_sha256: str
    model_parameters_sha256: str
    minimum_ollama_version: str
    tested_ollama_versions: tuple[str, ...]
    test_vectors: tuple[str, ...]
    ollama_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability = _coerce_capability(self.capability)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(
            self,
            "generation_parameters",
            _freeze_parameters(self.generation_parameters),
        )
        object.__setattr__(self, "ollama_digest", _canonical_ollama_digest(self.ollama_digest))
        object.__setattr__(self, "families", _normalise_tokens(self.families, "families"))
        object.__setattr__(self, "languages", _normalise_languages(self.languages))
        object.__setattr__(self, "tasks", _normalise_tokens(self.tasks, "tasks"))
        object.__setattr__(
            self,
            "required_notices",
            _normalise_texts(self.required_notices, "notices"),
        )
        object.__setattr__(
            self,
            "tested_ollama_versions",
            _normalise_versions(self.tested_ollama_versions),
        )
        object.__setattr__(
            self,
            "test_vectors",
            _normalise_tokens(self.test_vectors, "test_vectors"),
        )
        object.__setattr__(
            self,
            "ollama_capabilities",
            _normalise_tokens(self.ollama_capabilities, "ollama_capabilities"),
        )
        _validate_manifest(self)

    @property
    def model_id(self) -> str:
        """Compatibility spelling for callers that use Ollama's model terminology."""
        return self.model_name

    @property
    def context_length(self) -> int:
        """Compatibility spelling used by Ollama's ``details`` payload."""
        return self.context_window

    @property
    def parameters(self) -> Mapping[str, ParameterValue]:
        """The immutable generation parameters fixed by this manifest."""
        return self.generation_parameters


@dataclass(frozen=True, slots=True)
class ComponentVerification:
    """Result of checking a manifest against local Ollama metadata."""

    valid: bool
    issues: tuple[str, ...] = ()
    tags: OllamaModelIdentity | None = None
    show: OllamaShowMetadata | None = None
    ollama_version: str | None = None


def read_ollama_version(
    *,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Read and validate Ollama's numeric version from the fixed local API."""
    payload = _request_json(
        "GET",
        "/api/version",
        transport=transport,
    )
    version = payload.get("version")
    if not isinstance(version, str) or _version_key(version) is None:
        raise LocalComponentManifestError("Ollama devolvió una versión incompatible.")
    return version


def read_ollama_model_identities(
    *,
    transport: httpx.BaseTransport | None = None,
) -> tuple[OllamaModelIdentity, ...]:
    """Read bounded identity metadata from Ollama's fixed local ``/api/tags``."""
    payload = _request_json(
        "GET",
        "/api/tags",
        transport=transport,
    )
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or len(raw_models) > MAX_DISCOVERED_MODELS:
        raise LocalComponentManifestError("Ollama devolvió una lista de modelos incompatible.")

    identities: list[OllamaModelIdentity] = []
    seen: set[str] = set()
    for item in raw_models:
        identity = _parse_tags_identity(item)
        if identity is None or identity.model_name.casefold() in seen:
            continue
        identities.append(identity)
        seen.add(identity.model_name.casefold())
    return tuple(identities)


def verify_component_manifest(
    manifest: ComponentManifest,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ComponentVerification:
    """Verify one manifest using only model metadata, never document content.

    Transport and malformed-response failures become an invalid result so a
    future caller can keep the current workflow untouched while deciding how to
    present the diagnostic.  No response body is included in the result.
    """
    try:
        ollama_version = read_ollama_version(transport=transport)
    except LocalComponentManifestError as exc:
        return ComponentVerification(False, (_safe_issue(str(exc)),))
    version_issues = _compare_ollama_version(manifest, ollama_version)
    if version_issues:
        return ComponentVerification(
            False,
            tuple(version_issues),
            ollama_version=ollama_version,
        )
    try:
        tags = read_ollama_model_identities(transport=transport)
    except LocalComponentManifestError as exc:
        return ComponentVerification(False, (_safe_issue(str(exc)),), ollama_version=ollama_version)

    observed = _find_identity(tags, manifest.model_name)
    if observed is None:
        return ComponentVerification(
            False,
            ("model_not_installed",),
            ollama_version=ollama_version,
        )

    issues = _compare_manifest_tags(manifest, observed)
    show: OllamaShowMetadata | None = None
    if not issues:
        try:
            show = read_ollama_show_metadata(manifest.model_name, transport=transport)
        except LocalComponentManifestError as exc:
            return ComponentVerification(
                False,
                (_safe_issue(str(exc)),),
                observed,
                ollama_version=ollama_version,
            )
        issues = _compare_manifest_show(manifest, show)
    return ComponentVerification(
        not issues,
        tuple(issues),
        observed,
        show,
        ollama_version,
    )


def read_ollama_show_metadata(
    model_name: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> OllamaShowMetadata:
    """Read model metadata from ``/api/show`` without sending a prompt or content."""
    if not isinstance(model_name, str):
        raise LocalComponentManifestError("El nombre local del modelo no es válido.")
    try:
        safe_model_name = validate_ollama_model_id(model_name)
    except LocalModelUnavailableError as exc:
        raise LocalComponentManifestError("El nombre local del modelo no es válido.") from exc
    if is_cloud_model_id(safe_model_name):
        raise LocalComponentManifestError("Los modelos cloud no son componentes locales válidos.")
    payload = _request_json(
        "POST",
        "/api/show",
        transport=transport,
        json_payload={"model": safe_model_name, "verbose": False},
    )
    return _parse_show_metadata(safe_model_name, payload)


def _request_json(
    method: str,
    path: str,
    *,
    transport: httpx.BaseTransport | None,
    json_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        with httpx.Client(
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            response = client.request(
                method,
                f"{OLLAMA_BASE_URL}{path}",
                json=json_payload,
            )
    except httpx.RequestError as exc:
        raise LocalComponentManifestError("No se pudo contactar con Ollama en local.") from exc
    if response.is_redirect:
        raise LocalComponentManifestError(
            "Ollama intentó redirigir la comprobación y fue bloqueado."
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LocalComponentManifestError(
            f"Ollama respondió con el estado HTTP {response.status_code}."
        ) from exc
    if len(response.content) > MAX_METADATA_RESPONSE_BYTES:
        raise LocalComponentManifestError("Ollama devolvió metadatos demasiado grandes.")
    try:
        payload = response.json()
    except (ValueError, UnicodeError) as exc:
        raise LocalComponentManifestError("Ollama devolvió metadatos incompatibles.") from exc
    if not isinstance(payload, dict):
        raise LocalComponentManifestError("Ollama devolvió metadatos incompatibles.")
    return payload


def _parse_tags_identity(item: object) -> OllamaModelIdentity | None:
    if not isinstance(item, dict):
        return None
    raw_name = item.get("model", item.get("name"))
    if not isinstance(raw_name, str):
        return None
    model_name = raw_name.strip()
    if not _safe_model_name(model_name):
        return None
    details = item.get("details")
    details = details if isinstance(details, dict) else {}
    digest = _optional_digest(item.get("digest"))
    return OllamaModelIdentity(
        model_name=model_name,
        digest=digest,
        format=_optional_token(details.get("format")),
        family=_optional_token(details.get("family")),
        families=_parse_families(details.get("families")),
        quantization=_optional_token(details.get("quantization_level")),
    )


def _parse_show_metadata(model_name: str, payload: dict[str, object]) -> OllamaShowMetadata:
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    model_info = payload.get("model_info")
    model_info = model_info if isinstance(model_info, dict) else {}
    context = _positive_int(details.get("context_length"))
    if context is None:
        for key, value in model_info.items():
            if isinstance(key, str) and key.casefold().endswith("context_length"):
                context = _positive_int(value)
                if context is not None:
                    break
    template = payload.get("template")
    parameters = payload.get("parameters")
    license_text = payload.get("license")
    capabilities = _parse_families(payload.get("capabilities"))
    return OllamaShowMetadata(
        model_name=model_name,
        format=_optional_token(details.get("format")),
        family=_optional_token(details.get("family")),
        families=_parse_families(details.get("families")),
        quantization=_optional_token(details.get("quantization_level")),
        context_window=context,
        template_sha256=_sha256_text(template),
        parameters_sha256=_sha256_text(parameters),
        license_sha256=_sha256_text(license_text),
        capabilities=capabilities,
    )


def _compare_manifest_tags(
    manifest: ComponentManifest,
    observed: OllamaModelIdentity,
) -> list[str]:
    issues: list[str] = []
    if observed.digest is None:
        issues.append("missing_ollama_digest")
    elif observed.digest.casefold() != manifest.ollama_digest.casefold():
        issues.append("ollama_digest_mismatch")
    if observed.format != manifest.format:
        issues.append("format_mismatch")
    if observed.family != manifest.family:
        issues.append("family_mismatch")
    if observed.families != manifest.families:
        issues.append("families_mismatch")
    # Some official adaptive GGUFs are reported as ``unknown`` by /api/tags
    # even though /api/show exposes the concrete underlying quantization.  The
    # digest remains mandatory and the show comparison below still has to match.
    if observed.quantization not in {manifest.quantization, "unknown"}:
        issues.append("quantization_mismatch")
    return issues


def _compare_ollama_version(
    manifest: ComponentManifest,
    observed_version: str,
) -> list[str]:
    observed = _version_key(observed_version)
    minimum = _version_key(manifest.minimum_ollama_version)
    tested = {_version_key(version) for version in manifest.tested_ollama_versions}
    if observed is None or minimum is None or None in tested:
        return ["ollama_version_invalid"]
    if observed < minimum:
        return ["ollama_version_too_old"]
    if observed not in tested:
        return ["ollama_version_not_tested"]
    return []


def _compare_manifest_show(
    manifest: ComponentManifest,
    observed: OllamaShowMetadata,
) -> list[str]:
    issues: list[str] = []
    if observed.format != manifest.format:
        issues.append("show_format_mismatch")
    if observed.family != manifest.family:
        issues.append("show_family_mismatch")
    if observed.families != manifest.families:
        issues.append("show_families_mismatch")
    if observed.quantization != manifest.quantization:
        issues.append("show_quantization_mismatch")
    if observed.context_window is None or observed.context_window < manifest.context_window:
        issues.append("context_window_insufficient")
    if observed.template_sha256 != manifest.prompt_template_sha256:
        issues.append("prompt_template_mismatch")
    if observed.parameters_sha256 != manifest.model_parameters_sha256:
        issues.append("model_parameters_mismatch")
    if observed.license_sha256 != manifest.license_sha256:
        issues.append("license_sha256_mismatch")
    if manifest.ollama_capabilities and observed.capabilities != manifest.ollama_capabilities:
        issues.append("capabilities_mismatch")
    return issues


def _validate_manifest(manifest: ComponentManifest) -> None:
    _validate_text(manifest.policy_version, "policy_version", MAX_MANIFEST_TEXT)
    if manifest.policy_version != POLICY_VERSION:
        raise ValueError(f"policy_version debe ser {POLICY_VERSION!r}.")
    if not isinstance(manifest.model_name, str):
        raise ValueError("model_name no es un nombre local de Ollama válido.")
    if is_cloud_model_id(manifest.model_name):
        raise ValueError("model_name no puede ser un modelo cloud.")
    if not _safe_model_name(manifest.model_name):
        raise ValueError("model_name no es un nombre local de Ollama válido.")
    if not _ollama_digest(manifest.ollama_digest):
        raise ValueError("ollama_digest debe contener 64 caracteres hexadecimales.")
    for value, label in (
        (manifest.family, "family"),
        (manifest.format, "format"),
        (manifest.quantization, "quantization"),
        (manifest.adapter, "adapter"),
        (manifest.prompt_version, "prompt_version"),
    ):
        if _optional_token(value) is None:
            raise ValueError(f"{label} contiene un valor no acotado o vacío.")
    if not manifest.families:
        raise ValueError("families no puede estar vacío.")
    if manifest.family not in manifest.families:
        raise ValueError("family debe aparecer también en families.")
    if not isinstance(manifest.context_window, int) or isinstance(manifest.context_window, bool):
        raise ValueError("context_window debe ser un entero positivo.")
    if not 1 <= manifest.context_window <= MAX_CONTEXT_WINDOW:
        raise ValueError("context_window está fuera de los límites permitidos.")
    if not manifest.languages or not manifest.tasks:
        raise ValueError("languages y tasks deben declarar al menos un valor.")
    _validate_text(manifest.license, "license", MAX_MANIFEST_TEXT)
    if (
        not isinstance(manifest.license_sha256, str)
        or _SHA256_PATTERN.fullmatch(manifest.license_sha256) is None
    ):
        raise ValueError("license_sha256 debe ser SHA-256 hexadecimal.")
    _validate_text(manifest.distribution_decision, "distribution_decision", MAX_MANIFEST_TEXT)
    _validate_text(manifest.upstream_repository, "upstream_repository", MAX_MANIFEST_TEXT)
    repository = urlsplit(manifest.upstream_repository)
    if (
        repository.scheme != "https"
        or not repository.netloc
        or repository.username is not None
        or repository.password is not None
    ):
        raise ValueError("upstream_repository solo puede usar https.")
    _validate_text(manifest.upstream_revision, "upstream_revision", MAX_MANIFEST_TEXT)
    if (
        not isinstance(manifest.upstream_file, str)
        or _UPSTREAM_FILE_PATTERN.fullmatch(manifest.upstream_file) is None
    ):
        raise ValueError("upstream_file debe ser un nombre de archivo acotado.")
    if manifest.upstream_sha256 is not None and (
        not isinstance(manifest.upstream_sha256, str)
        or _SHA256_PATTERN.fullmatch(manifest.upstream_sha256) is None
    ):
        raise ValueError("upstream_sha256 debe ser SHA-256 hexadecimal.")
    for digest, label in (
        (manifest.prompt_template_sha256, "prompt_template_sha256"),
        (manifest.model_parameters_sha256, "model_parameters_sha256"),
    ):
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError(f"{label} debe ser SHA-256 hexadecimal.")
    if (
        not isinstance(manifest.minimum_ollama_version, str)
        or _version_key(manifest.minimum_ollama_version) is None
    ):
        raise ValueError("minimum_ollama_version no tiene un formato de versión válido.")


def _coerce_capability(value: ComponentCapability | str) -> ComponentCapability:
    try:
        return value if isinstance(value, ComponentCapability) else ComponentCapability(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("capability debe ser translation, review o visual.") from exc


def _freeze_parameters(
    parameters: Mapping[str, ParameterValue],
) -> Mapping[str, ParameterValue]:
    if not isinstance(parameters, Mapping) or len(parameters) > MAX_MANIFEST_ITEMS:
        raise ValueError("generation_parameters debe ser un mapa pequeño.")
    validated: dict[str, ParameterValue] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not 1 <= len(key) <= MAX_PARAMETER_KEY:
            raise ValueError("Las claves de generation_parameters no son válidas.")
        if re.fullmatch(r"[a-z][a-z0-9_.-]*", key, flags=re.IGNORECASE) is None:
            raise ValueError("Las claves de generation_parameters no son seguras.")
        if isinstance(value, bool):
            validated[key] = value
        elif isinstance(value, int):
            if abs(value) > 10_000_000:
                raise ValueError("Un parámetro entero está fuera de límites.")
            validated[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value) or abs(value) > 1_000_000:
                raise ValueError("Un parámetro decimal está fuera de límites.")
            validated[key] = value
        elif isinstance(value, str) and 1 <= len(value) <= MAX_PARAMETER_STRING:
            if any(character in value for character in "\r\n\0"):
                raise ValueError("Un parámetro de texto contiene caracteres no válidos.")
            validated[key] = value
        else:
            raise ValueError("generation_parameters solo admite valores escalares acotados.")
    return MappingProxyType(validated)


def _normalise_tokens(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not 1 <= len(values) <= MAX_MANIFEST_ITEMS:
        raise ValueError(f"{label} debe ser una tupla pequeña.")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or _TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{label} contiene un identificador inválido.")
        if value not in result:
            result.append(value)
    return tuple(result)


def _normalise_languages(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not 1 <= len(values) <= MAX_MANIFEST_ITEMS:
        raise ValueError("languages debe ser una tupla pequeña.")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or _LANGUAGE_PATTERN.fullmatch(value) is None:
            raise ValueError("languages contiene un identificador inválido.")
        normalised = value.casefold()
        if normalised not in result:
            result.append(normalised)
    return tuple(result)


def _normalise_texts(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > MAX_MANIFEST_ITEMS:
        raise ValueError(f"{label} debe ser una tupla pequeña.")
    result: list[str] = []
    for value in values:
        _validate_text(value, label, MAX_MANIFEST_TEXT)
        if value not in result:
            result.append(value)
    return tuple(result)


def _normalise_versions(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not 1 <= len(values) <= MAX_MANIFEST_ITEMS:
        raise ValueError("tested_ollama_versions debe declarar versiones probadas.")
    for value in values:
        if not isinstance(value, str) or _version_key(value) is None:
            raise ValueError("tested_ollama_versions contiene una versión inválida.")
    return tuple(dict.fromkeys(values))


def _version_key(value: object) -> tuple[int, ...] | None:
    """Return a bounded numeric version key without lexical comparison pitfalls."""
    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        return None
    numeric = re.split(r"[-+]", value, maxsplit=1)[0]
    parts = numeric.split(".")
    if any(len(part) > 9 for part in parts):
        return None
    return tuple(int(part) for part in parts) + (0,) * (4 - len(parts))


def _validate_text(value: str, label: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError(f"{label} debe ser texto acotado sin saltos de línea.")


def _safe_model_name(value: str) -> bool:
    try:
        validated = validate_ollama_model_id(value)
    except LocalModelUnavailableError:
        return False
    return validated == value and not is_cloud_model_id(validated)


def _ollama_digest(value: str) -> bool:
    return _canonical_ollama_digest(value) is not None


def _optional_digest(value: object) -> str | None:
    return _canonical_ollama_digest(value)


def _canonical_ollama_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.casefold().startswith("sha256:"):
        candidate = candidate[7:]
    if _SHA256_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate.casefold()


def _optional_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if _TOKEN_PATTERN.fullmatch(stripped) else None


def _parse_families(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_MANIFEST_ITEMS:
        return ()
    values = tuple(item for item in value if isinstance(item, str))
    if len(values) != len(value):
        return ()
    try:
        return _normalise_tokens(values, "families")
    except ValueError:
        return ()


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_CONTEXT_WINDOW:
        return value
    return None


def _sha256_text(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_METADATA_RESPONSE_BYTES:
        return None
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        return None


def _find_identity(
    identities: tuple[OllamaModelIdentity, ...],
    model_name: str,
) -> OllamaModelIdentity | None:
    canonical = _canonical_model_name(model_name)
    return next(
        (
            identity
            for identity in identities
            if _canonical_model_name(identity.model_name) == canonical
        ),
        None,
    )


def _canonical_model_name(model_name: str) -> str:
    normalised = model_name.casefold()
    final_segment = normalised.rsplit("/", 1)[-1]
    return normalised if ":" in final_segment else f"{normalised}:latest"


def _safe_issue(message: str) -> str:
    """Keep transport diagnostics useful without returning response content."""
    if message.startswith("HTTP ") or "estado HTTP" in message:
        return "ollama_http_error"
    if "versión" in message:
        return "ollama_version_invalid"
    if "redirigir" in message:
        return "ollama_redirect_blocked"
    if "metadatos" in message or "lista de modelos" in message:
        return "ollama_metadata_invalid"
    return "ollama_unavailable"
