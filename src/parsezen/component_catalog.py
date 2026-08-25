"""The small, frozen product catalog for specialized local components.

The translation and review components are released by this increment.  The
catalog is data-only: it does not install, select, or contact Ollama.  Callers can pass
``PRODUCT_COMPONENT_CATALOG`` directly to
``parsezen.component_readiness.inspect_component_catalog`` or to its pure
evaluator.

The LFM artifact won the product comparison against Q8 at equivalent quality
with lower cost and disk footprint.  It also won against Qwen 3.5 9B on
monolingual precision and size, although Qwen was faster.  These notes record
the selection rationale only; they are not a claim that the model is suitable
for every language pair or document.

Hy-MT2 won the translation comparison with all evaluation gates passing.  The
selection notes below contain only aggregate, content-free benchmark facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from parsezen.component_readiness import ComponentCatalogEntry
from parsezen.local_ai_policy import POLICY_VERSION, ComponentCapability, ComponentManifest
from parsezen.local_models import ComponentRequirements

REVIEW_MODEL_NAME: Final = "parsezen/lfm-review:Q6_K"
REVIEW_OLLAMA_DIGEST: Final = "9cb653ed8242477adeefaf25923540efdd29a45942a8c001b76bbfedb1422e80"
REVIEW_UPSTREAM_REPOSITORY: Final = "https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF"
REVIEW_UPSTREAM_REVISION: Final = "84022ce711b28455e8c4fc364ce68c00cf995875"
REVIEW_UPSTREAM_FILE: Final = "LFM2.5-2.6B-Q6_K.gguf"
REVIEW_OLLAMA_SOURCE_MODEL: Final = "hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q6_K"
REVIEW_UPSTREAM_SHA256: Final = "2e74b1a0979a4a1936a408445147d103b8f15b2e2ec31c65fa0166f9069c250d"
REVIEW_LICENSE: Final = "LFM Open License v1.0"
REVIEW_LICENSE_SHA256: Final = "30adf9d6478191fb87f2424f63ba0728598335aaf99cd2848ef17e8e545fe94b"
REVIEW_PROMPT_TEMPLATE_SHA256: Final = (
    "ea663864491de7ade391839479860ca95541f892f72665c73251fbd4643b1bef"
)
REVIEW_MODEL_PARAMETERS_SHA256: Final = (
    "c1e038a3cf0a3e4d1d21a2b4382cf1f74cd3ef1d4e54ff841ac46f11f53368df"
)
REVIEW_CONTEXT_WINDOW: Final = 8_192
REVIEW_MODEL_MAX_CONTEXT_WINDOW: Final = 131_072
REVIEW_ARTIFACT_SIZE_BYTES: Final = 2_221_626_645

# The disk requirement includes a small amount above the 1.35x download
# margin used by the existing Ollama installer.  RAM is measured as available
# RAM, because that is what the existing readiness boundary can observe.
REVIEW_MIN_RAM_MEBIBYTES: Final = 8_192
REVIEW_MIN_DISK_FREE_BYTES: Final = 3_000_000_000

REVIEW_HARDWARE_REQUIREMENTS: Final = ComponentRequirements(
    min_ram_mebibytes=REVIEW_MIN_RAM_MEBIBYTES,
    min_disk_free_bytes=REVIEW_MIN_DISK_FREE_BYTES,
    # CPU execution is supported; an unknown NVIDIA adapter must not be a
    # hidden requirement for this component.
    min_vram_mebibytes=None,
)
REVIEW_COMPONENT_REQUIREMENTS: Final = REVIEW_HARDWARE_REQUIREMENTS

TRANSLATION_MODEL_NAME: Final = "parsezen/hymt-translation:Q4_K_M"
TRANSLATION_OLLAMA_DIGEST: Final = (
    "bb608502d6eb617216e27f34d16662f121be32af3ea5c0854333d2d5908ca12c"
)
TRANSLATION_UPSTREAM_REPOSITORY: Final = "https://huggingface.co/tencent/Hy-MT2-7B-GGUF"
TRANSLATION_UPSTREAM_REVISION: Final = "ab8472660ac61fac25f1af43fac2599d52a8a775"
TRANSLATION_UPSTREAM_FILE: Final = "Hy-MT2-7B-Q4_K_M.gguf"
TRANSLATION_OLLAMA_SOURCE_MODEL: Final = "hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M"
TRANSLATION_UPSTREAM_SHA256: Final = (
    "9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b"
)
TRANSLATION_LICENSE: Final = "Apache-2.0"
TRANSLATION_LICENSE_SHA256: Final = (
    "746750afa6af28fe4f8b326751ad2a40c700d2e5c459c0a1f6a2e76d99ace224"
)
TRANSLATION_PROMPT_TEMPLATE_SHA256: Final = (
    "1a1fe0dc6d69bb8b0f44f65b6bbe80fa342155bf812e7135002a1c429eccb89c"
)
TRANSLATION_MODEL_PARAMETERS_SHA256: Final = (
    "27b7c6960880d31e5f9b36f1aef7c35488a388d7175b864dfdc13e7df77ed057"
)
TRANSLATION_CONTEXT_WINDOW: Final = 8_192
TRANSLATION_MODEL_MAX_CONTEXT_WINDOW: Final = 262_144
TRANSLATION_UPSTREAM_SIZE_BYTES: Final = 4_624_648_896
TRANSLATION_OLLAMA_SIZE_BYTES: Final = 4_624_661_291
TRANSLATION_ARTIFACT_SIZE_BYTES: Final = TRANSLATION_OLLAMA_SIZE_BYTES

# The alias size is approximately 4.62 GB.  6.25 GB leaves a conservative
# rounded-up margin above 1.35x for the download and Ollama's local metadata.
# RAM is measured as available RAM: on a 16 GB machine this leaves room for
# the operating system and Parsezen while keeping CPU execution viable.
TRANSLATION_MIN_RAM_MEBIBYTES: Final = 8_192
TRANSLATION_MIN_DISK_FREE_BYTES: Final = 6_250_000_000

TRANSLATION_HARDWARE_REQUIREMENTS: Final = ComponentRequirements(
    min_ram_mebibytes=TRANSLATION_MIN_RAM_MEBIBYTES,
    min_disk_free_bytes=TRANSLATION_MIN_DISK_FREE_BYTES,
    min_vram_mebibytes=None,
)
TRANSLATION_COMPONENT_REQUIREMENTS: Final = TRANSLATION_HARDWARE_REQUIREMENTS

REVIEW_LICENSE_GATE: Final = (
    "redistribution_requires_license_and_notices;"
    "commercial_use_not_covered_for_entities_at_or_above_usd_10m;"
    "legal_review_required"
)
REVIEW_REQUIRED_NOTICES: Final = (
    "Redistribution requires the LFM Open License v1.0 and its notices.",
    "Commercial use is not covered for entities with annual revenue >= USD 10M.",
    "This catalog does not constitute legal approval.",
)

REVIEW_SELECTION_RATIONALE: Final = (
    "Q6_K matched Q8 quality at lower cost and size.",
    "Q6_K beat Qwen 3.5 9B on monolingual precision and size; Qwen was faster.",
)

TRANSLATION_LICENSE_GATE: Final = (
    "redistribution_allowed_with_apache_2_0_license_and_notices;not_legal_advice"
)
TRANSLATION_REQUIRED_NOTICES: Final = (
    "Redistribution must retain the Apache-2.0 license and notices.",
    "This catalog is product metadata, not legal advice.",
)
TRANSLATION_SELECTION_RATIONALE: Final = (
    "Hy-MT2 passed 27/27 evaluation gates with zero residuals, retries, or rejections.",
    "MiLMMT and TranslateGemma passed 24/27 gates with three residuals each.",
    "Hy-MT2 doubled the exact-match rate (6/27 versus 3/27); generation time excluding "
    "comparable loading was similar to MiLMMT.",
)

# Keep this list to the languages currently represented by Parsezen's product
# language contract.  It describes the intended review scope, not benchmark
# corpus contents.
_REVIEW_LANGUAGES: Final = ("de", "en", "es", "fr", "it", "pt")
_REVIEW_TASKS: Final = ("review_content", "review_structure", "review_translation")
_TRANSLATION_LANGUAGES: Final = _REVIEW_LANGUAGES
_TRANSLATION_TASKS: Final = ("translate",)

TRANSLATION_COMPONENT_MANIFEST: Final = ComponentManifest(
    capability=ComponentCapability.TRANSLATION,
    policy_version=POLICY_VERSION,
    model_name=TRANSLATION_MODEL_NAME,
    ollama_digest=TRANSLATION_OLLAMA_DIGEST,
    family="hunyuan-dense",
    families=("hunyuan-dense",),
    format="gguf",
    quantization="Q4_K_M",
    context_window=TRANSLATION_CONTEXT_WINDOW,
    adapter="hymt-translation-v1",
    prompt_version="hymt-translation-official-raw-v1",
    generation_parameters={
        "num_ctx": TRANSLATION_CONTEXT_WINDOW,
        "temperature": 0.7,
        "top_p": 0.6,
        "top_k": 20,
        "seed": 0,
    },
    languages=_TRANSLATION_LANGUAGES,
    tasks=_TRANSLATION_TASKS,
    license=TRANSLATION_LICENSE,
    license_sha256=TRANSLATION_LICENSE_SHA256,
    required_notices=TRANSLATION_REQUIRED_NOTICES,
    distribution_decision=TRANSLATION_LICENSE_GATE,
    upstream_repository=TRANSLATION_UPSTREAM_REPOSITORY,
    upstream_revision=TRANSLATION_UPSTREAM_REVISION,
    upstream_file=TRANSLATION_UPSTREAM_FILE,
    upstream_sha256=TRANSLATION_UPSTREAM_SHA256,
    prompt_template_sha256=TRANSLATION_PROMPT_TEMPLATE_SHA256,
    model_parameters_sha256=TRANSLATION_MODEL_PARAMETERS_SHA256,
    minimum_ollama_version="0.32.5",
    tested_ollama_versions=("0.32.5",),
    test_vectors=(
        "translation-installation-v1",
        "translation-metadata-v1",
        "translation-output-v1",
    ),
    ollama_capabilities=("completion",),
)
TRANSLATION_MANIFEST: Final = TRANSLATION_COMPONENT_MANIFEST

TRANSLATION_COMPONENT_ENTRY: Final = ComponentCatalogEntry(
    manifest=TRANSLATION_COMPONENT_MANIFEST,
    requirements=TRANSLATION_COMPONENT_REQUIREMENTS,
)
TRANSLATION_CATALOG_ENTRY: Final = TRANSLATION_COMPONENT_ENTRY

REVIEW_COMPONENT_MANIFEST: Final = ComponentManifest(
    capability=ComponentCapability.REVIEW,
    policy_version=POLICY_VERSION,
    model_name=REVIEW_MODEL_NAME,
    ollama_digest=REVIEW_OLLAMA_DIGEST,
    family="lfm2",
    families=("lfm2",),
    format="gguf",
    quantization="Q6_K",
    context_window=REVIEW_CONTEXT_WINDOW,
    adapter="lfm-review-v1",
    prompt_version="lfm-review-chatml-v1",
    generation_parameters={
        "num_ctx": REVIEW_CONTEXT_WINDOW,
        "temperature": 0.0,
        "seed": 0,
    },
    languages=_REVIEW_LANGUAGES,
    tasks=_REVIEW_TASKS,
    license=REVIEW_LICENSE,
    license_sha256=REVIEW_LICENSE_SHA256,
    required_notices=REVIEW_REQUIRED_NOTICES,
    distribution_decision=REVIEW_LICENSE_GATE,
    upstream_repository=REVIEW_UPSTREAM_REPOSITORY,
    upstream_revision=REVIEW_UPSTREAM_REVISION,
    upstream_file=REVIEW_UPSTREAM_FILE,
    upstream_sha256=REVIEW_UPSTREAM_SHA256,
    prompt_template_sha256=REVIEW_PROMPT_TEMPLATE_SHA256,
    model_parameters_sha256=REVIEW_MODEL_PARAMETERS_SHA256,
    minimum_ollama_version="0.32.5",
    tested_ollama_versions=("0.32.5",),
    # Opaque smoke identifiers only; no private corpus or document text is
    # embedded in the product catalog.
    test_vectors=("review-installation-v1", "review-metadata-v1", "review-guards-v1"),
    ollama_capabilities=("tools", "thinking", "completion"),
)
REVIEW_MANIFEST: Final = REVIEW_COMPONENT_MANIFEST

REVIEW_COMPONENT_ENTRY: Final = ComponentCatalogEntry(
    manifest=REVIEW_COMPONENT_MANIFEST,
    requirements=REVIEW_COMPONENT_REQUIREMENTS,
)
REVIEW_CATALOG_ENTRY: Final = REVIEW_COMPONENT_ENTRY

# MappingProxyType prevents a caller from silently adding a visual component
# or replacing either frozen product component.
PRODUCT_COMPONENT_CATALOG: Final[Mapping[ComponentCapability, ComponentCatalogEntry]] = (
    MappingProxyType(
        {
            ComponentCapability.TRANSLATION: TRANSLATION_COMPONENT_ENTRY,
            ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY,
        }
    )
)
REVIEW_COMPONENT_CATALOG: Final[Mapping[ComponentCapability, ComponentCatalogEntry]] = (
    MappingProxyType({ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY})
)
TRANSLATION_COMPONENT_CATALOG: Final[Mapping[ComponentCapability, ComponentCatalogEntry]] = (
    MappingProxyType({ComponentCapability.TRANSLATION: TRANSLATION_COMPONENT_ENTRY})
)


def product_component_catalog() -> Mapping[ComponentCapability, ComponentCatalogEntry]:
    """Return the immutable product catalog accepted by readiness checks."""

    return PRODUCT_COMPONENT_CATALOG
