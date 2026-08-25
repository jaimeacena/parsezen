"""Fail-closed readiness decisions for explicitly catalogued local components.

This module is deliberately independent from the presentation layer.  The
decision function consumes bounded snapshots and the content-free result of
the local Ollama verification.  The catalog inspection helper may perform the
read-only Ollama metadata check, but it never downloads or selects a model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx

from parsezen.local_ai_policy import (
    POLICY_VERSION,
    ComponentCapability,
    ComponentManifest,
    ComponentVerification,
    verify_component_manifest,
)
from parsezen.local_models import (
    ComponentReadiness as HardwareReadiness,
)
from parsezen.local_models import (
    ComponentRequirements,
    ComponentStatus,
    HardwareComponent,
    LocalHardware,
    calculate_component_readiness,
    is_cloud_model_id,
    is_ollama_local_only_configured,
    validate_ollama_model_id,
)


class ReadinessStatus(StrEnum):
    """The only states exposed by the component setup contract."""

    PREPARED = "prepared"
    DOWNLOADABLE = "downloadable"
    INSUFFICIENT = "insufficient"

    # Compatibility names for callers that used the lower-level hardware
    # vocabulary before the visible setup states were introduced.
    READY = PREPARED
    INSTALLABLE = DOWNLOADABLE


# Names that are convenient for callers that use the product terminology.
ComponentReadinessStatus = ReadinessStatus
ComponentAvailability = ReadinessStatus


@dataclass(frozen=True, slots=True)
class ComponentCatalogEntry:
    """One manifest and its explicit local resource requirements."""

    manifest: ComponentManifest
    requirements: ComponentRequirements


CatalogEntry = ComponentCatalogEntry


@dataclass(frozen=True, slots=True)
class ComponentReadiness:
    """A content-free, UI-ready state for one catalogued capability."""

    component: ComponentCapability
    status: ReadinessStatus
    reasons: tuple[str, ...] = ()

    @property
    def capability(self) -> ComponentCapability:
        """Compatibility spelling for callers that name the manifest field."""

        return self.component

    @property
    def prepared(self) -> bool:
        """Whether all hard gates passed."""

        return self.status is ReadinessStatus.PREPARED


ComponentReadinessResult = ComponentReadiness


_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_SAFE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "local_only_required",
        "manifest_invalid",
        "requirements_invalid",
        "hardware_unknown",
        "ram_insufficient",
        "disk_insufficient",
        "vram_insufficient",
        "model_not_installed",
        "ollama_unavailable",
        "ollama_http_error",
        "ollama_redirect_blocked",
        "ollama_metadata_invalid",
        "ollama_version_invalid",
        "ollama_version_too_old",
        "ollama_version_not_tested",
        "ollama_verification_failed",
        "ollama_digest_mismatch",
        "format_mismatch",
        "family_mismatch",
        "families_mismatch",
        "quantization_mismatch",
        "show_format_mismatch",
        "show_family_mismatch",
        "show_families_mismatch",
        "show_quantization_mismatch",
        "context_window_insufficient",
        "prompt_template_mismatch",
        "model_parameters_mismatch",
        "license_sha256_mismatch",
        "capabilities_mismatch",
    }
)


def evaluate_component_readiness(
    entry: ComponentCatalogEntry,
    hardware: LocalHardware,
    verification: ComponentVerification | None,
    *,
    local_only_configured: bool,
) -> ComponentReadiness:
    """Evaluate one component without I/O and fail closed on every unknown.

    ``verification`` is the content-free result returned by
    :func:`parsezen.local_ai_policy.verify_component_manifest`.  A missing
    model is the sole verification failure that can become ``DOWNLOADABLE``;
    every other failure remains ``INSUFFICIENT``.
    """

    component = _entry_component(entry)
    if component is None:
        # A malformed catalog entry cannot be safely associated with a visible
        # capability.  Keep the return type total for callers that inspect a
        # catalog assembled from untrusted configuration.
        component = ComponentCapability.TRANSLATION
        return ComponentReadiness(component, ReadinessStatus.INSUFFICIENT, ("manifest_invalid",))

    if not _valid_manifest_for_readiness(entry.manifest, component):
        return ComponentReadiness(component, ReadinessStatus.INSUFFICIENT, ("manifest_invalid",))

    hardware_reasons = _hardware_failure_reasons(entry.requirements, hardware)
    if hardware_reasons:
        return ComponentReadiness(
            component,
            ReadinessStatus.INSUFFICIENT,
            hardware_reasons,
        )

    if not isinstance(local_only_configured, bool) or not local_only_configured:
        return ComponentReadiness(
            component,
            ReadinessStatus.INSUFFICIENT,
            ("local_only_required",),
        )

    if verification is None or not isinstance(verification, ComponentVerification):
        return ComponentReadiness(
            component,
            ReadinessStatus.INSUFFICIENT,
            ("ollama_verification_failed",),
        )

    if verification.valid:
        if (
            verification.issues
            or verification.tags is None
            or not _matching_digest(verification.tags.digest, entry.manifest.ollama_digest)
        ):
            return ComponentReadiness(
                component,
                ReadinessStatus.INSUFFICIENT,
                _verification_reasons(verification, digest_mismatch=True),
            )
        return ComponentReadiness(component, ReadinessStatus.PREPARED)

    reasons = _verification_reasons(verification)
    if reasons == ("model_not_installed",):
        return ComponentReadiness(component, ReadinessStatus.DOWNLOADABLE, reasons)
    return ComponentReadiness(component, ReadinessStatus.INSUFFICIENT, reasons)


def evaluate_component_catalog(
    catalog: Mapping[ComponentCapability | HardwareComponent | str, ComponentCatalogEntry],
    hardware: LocalHardware,
    verifications: Mapping[
        ComponentCapability | HardwareComponent | str,
        ComponentVerification | None,
    ],
    *,
    local_only_configured: bool,
) -> dict[ComponentCapability, ComponentReadiness]:
    """Evaluate exactly the explicitly supplied catalog, preserving its order."""

    if not isinstance(catalog, Mapping) or not isinstance(verifications, Mapping):
        raise TypeError("El catálogo y las verificaciones deben ser mapas explícitos.")

    result: dict[ComponentCapability, ComponentReadiness] = {}
    for raw_component, entry in catalog.items():
        component = _coerce_component(raw_component)
        if component is None:
            continue
        verification = _lookup_verification(verifications, component)
        if _entry_component(entry) is not component:
            result[component] = ComponentReadiness(
                component,
                ReadinessStatus.INSUFFICIENT,
                ("manifest_invalid",),
            )
            continue
        result[component] = evaluate_component_readiness(
            entry,
            hardware,
            verification,
            local_only_configured=local_only_configured,
        )
    return result


def inspect_component_catalog(
    catalog: Mapping[ComponentCapability | HardwareComponent | str, ComponentCatalogEntry],
    hardware: LocalHardware,
    *,
    local_only_configured: bool | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[ComponentCapability, ComponentReadiness]:
    """Read local Ollama metadata and evaluate an explicit catalog.

    Only ``/api/version``, ``/api/tags`` and ``/api/show`` are queried by the
    existing verifier.  This function has no installation or download path.
    Pass ``local_only_configured`` explicitly in pure callers; omitting it
    reads the local Ollama privacy setting through the existing helper.
    """

    local_only = (
        is_ollama_local_only_configured()
        if local_only_configured is None
        else local_only_configured
    )
    verifications: dict[
        ComponentCapability | HardwareComponent | str,
        ComponentVerification | None,
    ] = {}
    for raw_component, entry in catalog.items():
        component = _coerce_component(raw_component)
        if component is None:
            continue
        if not isinstance(entry, ComponentCatalogEntry) or not _valid_manifest_for_readiness(
            entry.manifest,
            component,
        ):
            verifications[component] = None
            continue
        if not local_only:
            verifications[component] = None
            continue
        try:
            verifications[component] = verify_component_manifest(
                entry.manifest,
                transport=transport,
            )
        except Exception:
            # Do not expose transport details or a response body.  Unknown
            # verifier failures are deliberately not treated as installable.
            verifications[component] = None
    return evaluate_component_catalog(
        catalog,
        hardware,
        verifications,
        local_only_configured=local_only,
    )


# Short names make the boundary discoverable without forcing UI callers to
# know whether they need the pure evaluator or the local inspection helper.
assess_component_readiness = evaluate_component_readiness
assess_component_catalog = evaluate_component_catalog
check_component_catalog = inspect_component_catalog


def _coerce_component(value: object) -> ComponentCapability | None:
    try:
        if isinstance(value, ComponentCapability):
            return value
        if isinstance(value, (HardwareComponent, str)):
            return ComponentCapability(
                value.value if isinstance(value, HardwareComponent) else value
            )
    except (TypeError, ValueError):
        return None
    return None


def _entry_component(entry: object) -> ComponentCapability | None:
    if not isinstance(entry, ComponentCatalogEntry):
        return None
    return _coerce_component(getattr(entry.manifest, "capability", None))


def _valid_manifest_for_readiness(
    manifest: object,
    expected_component: ComponentCapability,
) -> bool:
    if not isinstance(manifest, ComponentManifest):
        return False
    try:
        if manifest.capability is not expected_component:
            return False
        if manifest.policy_version != POLICY_VERSION:
            return False
        digest = manifest.ollama_digest
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            return False
        model_name = manifest.model_name
        if not isinstance(model_name, str) or not model_name.strip() or "\n" in model_name:
            return False
        if validate_ollama_model_id(model_name) != model_name or is_cloud_model_id(model_name):
            return False
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def _hardware_failure_reasons(
    requirements: object,
    hardware: object,
) -> tuple[str, ...]:
    if not isinstance(requirements, ComponentRequirements) or not isinstance(
        hardware,
        LocalHardware,
    ):
        return ("requirements_invalid",)
    if not _valid_hardware_snapshot(hardware):
        return ("hardware_unknown",)

    try:
        readiness = _hardware_readiness_without_install_state(requirements, hardware)
    except (TypeError, ValueError):
        return ("requirements_invalid",)
    if readiness.status is ComponentStatus.INSUFFICIENT:
        return _hardware_reason_codes(requirements, hardware)
    if readiness.status is ComponentStatus.UNKNOWN:
        return ("hardware_unknown",)
    return ()


def _hardware_readiness_without_install_state(
    requirements: ComponentRequirements,
    hardware: LocalHardware,
) -> HardwareReadiness:
    return calculate_component_readiness(
        HardwareComponent.TRANSLATION,
        hardware,
        ComponentRequirements(
            min_ram_mebibytes=requirements.min_ram_mebibytes,
            min_disk_free_bytes=requirements.min_disk_free_bytes,
            min_vram_mebibytes=requirements.min_vram_mebibytes,
            installed=False,
        ),
    )


def _hardware_reason_codes(
    requirements: ComponentRequirements,
    hardware: LocalHardware,
) -> tuple[str, ...]:
    reasons: list[str] = []
    for required, actual, reason in (
        (requirements.min_ram_mebibytes, hardware.ram_available_mebibytes, "ram_insufficient"),
        (requirements.min_disk_free_bytes, hardware.disk_free_bytes, "disk_insufficient"),
        (
            requirements.min_vram_mebibytes,
            hardware.nvidia_vram_available_mebibytes,
            "vram_insufficient",
        ),
    ):
        if required:
            if actual is None:
                reasons.append("hardware_unknown")
            elif actual < required:
                reasons.append(reason)
    return tuple(reasons) or ("hardware_unknown",)


def _valid_hardware_snapshot(hardware: LocalHardware) -> bool:
    for value in (
        hardware.ram_total_mebibytes,
        hardware.ram_available_mebibytes,
        hardware.disk_free_bytes,
        hardware.nvidia_vram_total_mebibytes,
        hardware.nvidia_vram_available_mebibytes,
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            return False
    return True


def _lookup_verification(
    verifications: Mapping[
        ComponentCapability | HardwareComponent | str, ComponentVerification | None
    ],
    component: ComponentCapability,
) -> ComponentVerification | None:
    for raw_component, verification in verifications.items():
        if _coerce_component(raw_component) is component:
            return verification
    return None


def _verification_reasons(
    verification: ComponentVerification,
    *,
    digest_mismatch: bool = False,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if digest_mismatch:
        reasons.append("ollama_digest_mismatch")
    for issue in verification.issues:
        if issue == "ollama_digest_mismatch" and "ollama_digest_mismatch" not in reasons:
            reasons.append(issue)
        elif issue in _SAFE_REASONS and issue not in reasons:
            reasons.append(issue)
    if not reasons:
        reasons.append("ollama_verification_failed")
    return tuple(reasons)


def _matching_digest(observed: object, expected: object) -> bool:
    return (
        isinstance(observed, str)
        and isinstance(expected, str)
        and _SHA256_PATTERN.fullmatch(observed) is not None
        and _SHA256_PATTERN.fullmatch(expected) is not None
        and observed.casefold() == expected.casefold()
    )
