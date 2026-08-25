"""The small, frozen product catalog for specialized local components.

Only the review component is released by this increment.  The catalog is
data-only: it does not install, select, or contact Ollama.  Callers can pass
``PRODUCT_COMPONENT_CATALOG`` directly to
``parsezen.component_readiness.inspect_component_catalog`` or to its pure
evaluator.

The LFM artifact won the product comparison against Q8 at equivalent quality
with lower cost and disk footprint.  It also won against Qwen 3.5 9B on
monolingual precision and size, although Qwen was faster.  These notes record
the selection rationale only; they are not a claim that the model is suitable
for every language pair or document.
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
REVIEW_ARTIFACT_SIZE_BYTES: Final = 2_220_000_000

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

# Keep this list to the languages currently represented by Parsezen's product
# language contract.  It describes the intended review scope, not benchmark
# corpus contents.
_REVIEW_LANGUAGES: Final = ("de", "en", "es", "fr", "it", "pt")
_REVIEW_TASKS: Final = ("review_content", "review_structure", "review_translation")

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
        "top_p": 0.9,
        "repeat_penalty": 1.05,
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

# MappingProxyType prevents a caller from silently adding a translation or
# visual component and accidentally presenting an unreviewed setup as ready.
PRODUCT_COMPONENT_CATALOG: Final[Mapping[ComponentCapability, ComponentCatalogEntry]] = (
    MappingProxyType({ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY})
)
REVIEW_COMPONENT_CATALOG: Final[Mapping[ComponentCapability, ComponentCatalogEntry]] = (
    PRODUCT_COMPONENT_CATALOG
)


def product_component_catalog() -> Mapping[ComponentCapability, ComponentCatalogEntry]:
    """Return the immutable product catalog accepted by readiness checks."""

    return PRODUCT_COMPONENT_CATALOG
