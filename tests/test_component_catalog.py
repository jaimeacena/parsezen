from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from parsezen.component_catalog import (
    PRODUCT_COMPONENT_CATALOG,
    REVIEW_ARTIFACT_SIZE_BYTES,
    REVIEW_COMPONENT_ENTRY,
    REVIEW_COMPONENT_MANIFEST,
    REVIEW_COMPONENT_REQUIREMENTS,
    REVIEW_CONTEXT_WINDOW,
    REVIEW_LICENSE_GATE,
    REVIEW_MODEL_MAX_CONTEXT_WINDOW,
    REVIEW_SELECTION_RATIONALE,
    product_component_catalog,
)
from parsezen.component_readiness import (
    ComponentCatalogEntry,
    ReadinessStatus,
    evaluate_component_catalog,
)
from parsezen.local_ai_policy import ComponentCapability, ComponentVerification
from parsezen.local_models import HardwareComponent, LocalHardware

_READINESS_CATALOG = cast(
    "Mapping[ComponentCapability | HardwareComponent | str, ComponentCatalogEntry]",
    PRODUCT_COMPONENT_CATALOG,
)


def test_product_catalog_contains_only_the_frozen_review_component() -> None:
    assert dict(PRODUCT_COMPONENT_CATALOG) == {ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY}
    assert product_component_catalog() is PRODUCT_COMPONENT_CATALOG
    with pytest.raises(TypeError):
        PRODUCT_COMPONENT_CATALOG[ComponentCapability.TRANSLATION] = REVIEW_COMPONENT_ENTRY  # type: ignore[index]


def test_review_manifest_identity_is_frozen_without_private_corpus_content() -> None:
    manifest = REVIEW_COMPONENT_MANIFEST
    assert manifest.capability is ComponentCapability.REVIEW
    assert manifest.model_name == "parsezen/lfm-review:Q6_K"
    assert manifest.ollama_digest == (
        "9cb653ed8242477adeefaf25923540efdd29a45942a8c001b76bbfedb1422e80"
    )
    assert manifest.family == "lfm2"
    assert manifest.families == ("lfm2",)
    assert manifest.format == "gguf"
    assert manifest.quantization == "Q6_K"
    assert manifest.context_window == REVIEW_CONTEXT_WINDOW == 8_192
    assert REVIEW_MODEL_MAX_CONTEXT_WINDOW == 131_072
    assert manifest.upstream_revision == "84022ce711b28455e8c4fc364ce68c00cf995875"
    assert manifest.upstream_file == "LFM2.5-2.6B-Q6_K.gguf"
    assert manifest.upstream_sha256 == (
        "2e74b1a0979a4a1936a408445147d103b8f15b2e2ec31c65fa0166f9069c250d"
    )
    assert manifest.prompt_template_sha256 == (
        "ea663864491de7ade391839479860ca95541f892f72665c73251fbd4643b1bef"
    )
    assert manifest.model_parameters_sha256 == (
        "c1e038a3cf0a3e4d1d21a2b4382cf1f74cd3ef1d4e54ff841ac46f11f53368df"
    )
    assert manifest.license_sha256 == (
        "30adf9d6478191fb87f2424f63ba0728598335aaf99cd2848ef17e8e545fe94b"
    )
    assert manifest.ollama_capabilities == ("tools", "thinking", "completion")
    assert manifest.minimum_ollama_version == "0.32.5"
    assert manifest.tested_ollama_versions == ("0.32.5",)
    assert all("document" not in vector.casefold() for vector in manifest.test_vectors)


def test_review_hardware_gate_is_conservative_and_cpu_compatible() -> None:
    assert REVIEW_ARTIFACT_SIZE_BYTES == 2_220_000_000
    assert REVIEW_COMPONENT_REQUIREMENTS.min_ram_mebibytes == 8_192
    assert REVIEW_COMPONENT_REQUIREMENTS.min_disk_free_bytes == 3_000_000_000
    assert REVIEW_COMPONENT_REQUIREMENTS.min_vram_mebibytes is None
    enough_hardware = LocalHardware(
        ram_available_mebibytes=8_192,
        disk_free_bytes=3_000_000_000,
    )
    result = evaluate_component_catalog(
        _READINESS_CATALOG,
        enough_hardware,
        {ComponentCapability.REVIEW: ComponentVerification(False, ("model_not_installed",))},
        local_only_configured=True,
    )
    assert result[ComponentCapability.REVIEW].status is ReadinessStatus.DOWNLOADABLE

    unknown_hardware = evaluate_component_catalog(
        _READINESS_CATALOG,
        LocalHardware(disk_free_bytes=3_000_000_000),
        {ComponentCapability.REVIEW: ComponentVerification(False, ("model_not_installed",))},
        local_only_configured=True,
    )
    assert unknown_hardware[ComponentCapability.REVIEW].reasons == ("hardware_unknown",)


def test_license_gate_and_selection_notes_do_not_claim_legal_approval() -> None:
    gate = REVIEW_LICENSE_GATE.casefold()
    assert "license" in gate
    assert "notices" in gate
    assert "usd_10m" in gate
    assert "legal_review_required" in gate
    assert all("approval" not in note.casefold() for note in REVIEW_SELECTION_RATIONALE)
