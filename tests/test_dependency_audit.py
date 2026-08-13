from pathlib import Path

import pytest
from scripts.audit_dependencies import locked_versions, validate_audit_coverage


def test_locked_versions_reads_compiled_requirements(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "Torch==2.13.0+cpu \\\n    --hash=sha256:abc\nhttpx==0.28.1\n",
        encoding="utf-8",
    )

    assert locked_versions(lock) == {"torch": "2.13.0+cpu", "httpx": "0.28.1"}


def test_variant_skip_requires_an_audited_aligned_canonical_package() -> None:
    primary = {"dependencies": [{"name": "torch", "skip_reason": "not on PyPI"}]}
    canonical = {"dependencies": [{"name": "torch", "version": "2.13.0", "vulns": []}]}

    validate_audit_coverage(
        primary,
        primary_versions={"torch": "2.13.0+cpu"},
        allowed_variant_skips=frozenset({"torch"}),
        canonical=canonical,
        canonical_versions={"torch": "2.13.0"},
    )


def test_unexpected_or_misaligned_skips_fail_closed() -> None:
    primary = {"dependencies": [{"name": "torch", "skip_reason": "not on PyPI"}]}

    with pytest.raises(ValueError, match="omisiones inesperadas"):
        validate_audit_coverage(primary, primary_versions={"torch": "2.13.0+cpu"})

    with pytest.raises(ValueError, match="no coincide"):
        validate_audit_coverage(
            primary,
            primary_versions={"torch": "2.13.0+cpu"},
            allowed_variant_skips=frozenset({"torch"}),
            canonical={"dependencies": [{"name": "torch", "version": "2.12.0"}]},
            canonical_versions={"torch": "2.12.0"},
        )


def test_canonical_audit_cannot_hide_an_unrelated_skip() -> None:
    with pytest.raises(ValueError, match="auditoría canónica: torchvision"):
        validate_audit_coverage(
            {"dependencies": [{"name": "torch", "skip_reason": "variant"}]},
            primary_versions={"torch": "2.13.0+cpu"},
            allowed_variant_skips=frozenset({"torch"}),
            canonical={
                "dependencies": [
                    {"name": "torch", "version": "2.13.0", "vulns": []},
                    {"name": "torchvision", "skip_reason": "missing"},
                ]
            },
            canonical_versions={"torch": "2.13.0", "torchvision": "0.28.0"},
        )
