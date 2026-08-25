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
    TRANSLATION_ARTIFACT_SIZE_BYTES,
    TRANSLATION_COMPONENT_ENTRY,
    TRANSLATION_COMPONENT_MANIFEST,
    TRANSLATION_COMPONENT_REQUIREMENTS,
    TRANSLATION_CONTEXT_WINDOW,
    TRANSLATION_LICENSE,
    TRANSLATION_LICENSE_GATE,
    TRANSLATION_MIN_DISK_FREE_BYTES,
    TRANSLATION_MODEL_MAX_CONTEXT_WINDOW,
    TRANSLATION_MODEL_NAME,
    TRANSLATION_OLLAMA_SIZE_BYTES,
    TRANSLATION_SELECTION_RATIONALE,
    TRANSLATION_UPSTREAM_SIZE_BYTES,
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


def test_product_catalog_contains_the_frozen_translation_and_review_components() -> None:
    assert dict(PRODUCT_COMPONENT_CATALOG) == {
        ComponentCapability.TRANSLATION: TRANSLATION_COMPONENT_ENTRY,
        ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY,
    }
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
    assert REVIEW_ARTIFACT_SIZE_BYTES == 2_221_626_645
    assert REVIEW_COMPONENT_REQUIREMENTS.min_ram_mebibytes == 8_192
    assert REVIEW_COMPONENT_REQUIREMENTS.min_disk_free_bytes == 3_000_000_000
    assert REVIEW_COMPONENT_REQUIREMENTS.min_vram_mebibytes is None
    enough_hardware = LocalHardware(
        ram_available_mebibytes=8_192,
        disk_free_bytes=6_250_000_000,
    )
    result = evaluate_component_catalog(
        _READINESS_CATALOG,
        enough_hardware,
        {
            ComponentCapability.REVIEW: ComponentVerification(False, ("model_not_installed",)),
            ComponentCapability.TRANSLATION: ComponentVerification(
                False,
                ("model_not_installed",),
            ),
        },
        local_only_configured=True,
    )
    assert result[ComponentCapability.REVIEW].status is ReadinessStatus.DOWNLOADABLE
    assert result[ComponentCapability.TRANSLATION].status is ReadinessStatus.DOWNLOADABLE

    unknown_hardware = evaluate_component_catalog(
        _READINESS_CATALOG,
        LocalHardware(disk_free_bytes=6_250_000_000),
        {
            ComponentCapability.REVIEW: ComponentVerification(False, ("model_not_installed",)),
            ComponentCapability.TRANSLATION: ComponentVerification(
                False,
                ("model_not_installed",),
            ),
        },
        local_only_configured=True,
    )
    assert unknown_hardware[ComponentCapability.REVIEW].reasons == ("hardware_unknown",)
    assert unknown_hardware[ComponentCapability.TRANSLATION].reasons == ("hardware_unknown",)


def test_translation_manifest_identity_and_generation_contract_are_frozen() -> None:
    manifest = TRANSLATION_COMPONENT_MANIFEST
    assert manifest.capability is ComponentCapability.TRANSLATION
    assert manifest.model_name == TRANSLATION_MODEL_NAME == "parsezen/hymt-translation:Q4_K_M"
    assert manifest.ollama_digest == (
        "bb608502d6eb617216e27f34d16662f121be32af3ea5c0854333d2d5908ca12c"
    )
    assert manifest.family == "hunyuan-dense"
    assert manifest.families == ("hunyuan-dense",)
    assert manifest.format == "gguf"
    assert manifest.quantization == "Q4_K_M"
    assert manifest.context_window == TRANSLATION_CONTEXT_WINDOW == 8_192
    assert TRANSLATION_MODEL_MAX_CONTEXT_WINDOW == 262_144
    assert manifest.upstream_repository == "https://huggingface.co/tencent/Hy-MT2-7B-GGUF"
    assert manifest.upstream_revision == "ab8472660ac61fac25f1af43fac2599d52a8a775"
    assert manifest.upstream_file == "Hy-MT2-7B-Q4_K_M.gguf"
    assert manifest.upstream_sha256 == (
        "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b"
    )
    assert manifest.prompt_template_sha256 == (
        "1a1fe0dc6d69bb8b0f44f65b6bbe80fa342155bf812e7135002a1c429eccb89c"
    )
    assert manifest.model_parameters_sha256 == (
        "27b7c6960880d31e5f9b36f1aef7c35488a388d7175b864dfdc13e7df77ed057"
    )
    assert manifest.license == TRANSLATION_LICENSE == "Apache-2.0"
    assert manifest.license_sha256 == (
        "746750afa6af28fe4f8b326751ad2a40c700d2e5c459c0a1f6a2e76d99ace224"
    )
    assert manifest.generation_parameters == {
        "num_ctx": 8_192,
        "temperature": 0.7,
        "top_p": 0.6,
        "top_k": 20,
        "seed": 0,
    }
    assert manifest.ollama_capabilities == ("completion",)
    assert manifest.minimum_ollama_version == "0.32.5"
    assert manifest.tested_ollama_versions == ("0.32.5",)
    assert all("document" not in vector.casefold() for vector in manifest.test_vectors)


def test_translation_hardware_gate_has_16gb_cpu_target_and_disk_margin() -> None:
    assert TRANSLATION_ARTIFACT_SIZE_BYTES == 4_624_661_291
    assert TRANSLATION_UPSTREAM_SIZE_BYTES == 4_624_648_896
    assert TRANSLATION_OLLAMA_SIZE_BYTES == TRANSLATION_ARTIFACT_SIZE_BYTES
    assert TRANSLATION_MIN_DISK_FREE_BYTES >= int(TRANSLATION_ARTIFACT_SIZE_BYTES * 1.35)
    assert TRANSLATION_COMPONENT_REQUIREMENTS == TRANSLATION_COMPONENT_ENTRY.requirements
    assert TRANSLATION_COMPONENT_REQUIREMENTS.min_ram_mebibytes == 8_192
    assert TRANSLATION_COMPONENT_REQUIREMENTS.min_disk_free_bytes == 6_250_000_000
    assert TRANSLATION_COMPONENT_REQUIREMENTS.min_vram_mebibytes is None


def test_translation_license_gate_and_selection_notes_are_content_free() -> None:
    gate = TRANSLATION_LICENSE_GATE.casefold()
    assert "redistribution" in gate
    assert "apache_2_0" in gate
    assert "notices" in gate
    assert "not_legal_advice" in gate
    assert all("approval" not in note.casefold() for note in TRANSLATION_SELECTION_RATIONALE)


def test_license_gate_and_selection_notes_do_not_claim_legal_approval() -> None:
    gate = REVIEW_LICENSE_GATE.casefold()
    assert "license" in gate
    assert "notices" in gate
    assert "usd_10m" in gate
    assert "legal_review_required" in gate
    assert all("approval" not in note.casefold() for note in REVIEW_SELECTION_RATIONALE)
