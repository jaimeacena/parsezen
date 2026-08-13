"""Run pip-audit while rejecting dependencies that silently escape coverage."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

_LOCKED_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def locked_versions(path: Path) -> dict[str, str]:
    """Return canonical package names and exact versions from a compiled lockfile."""

    versions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LOCKED_REQUIREMENT.match(line.strip())
        if match is not None:
            versions[canonicalize_name(match.group(1))] = match.group(2)
    return versions


def validate_audit_coverage(
    primary: dict[str, Any],
    *,
    primary_versions: dict[str, str],
    allowed_variant_skips: frozenset[str] = frozenset(),
    canonical: dict[str, Any] | None = None,
    canonical_versions: dict[str, str] | None = None,
) -> None:
    """Reject unexpected skips and prove allowed variants through canonical packages."""

    dependencies = primary.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("pip-audit no devolvió una lista de dependencias válida.")
    skipped = {
        canonicalize_name(item.get("name", ""))
        for item in dependencies
        if isinstance(item, dict) and item.get("skip_reason")
    }
    allowed = {canonicalize_name(name) for name in allowed_variant_skips}
    unexpected = sorted(skipped - allowed)
    if unexpected:
        raise ValueError(f"omisiones inesperadas: {', '.join(unexpected)}")
    if not skipped:
        return
    if canonical is None or canonical_versions is None:
        raise ValueError("Falta la auditoría canónica para justificar variantes omitidas.")
    canonical_dependencies = canonical.get("dependencies")
    if not isinstance(canonical_dependencies, list):
        raise ValueError("La auditoría canónica no devolvió dependencias válidas.")
    canonical_results = {
        canonicalize_name(item.get("name", "")): item
        for item in canonical_dependencies
        if isinstance(item, dict)
    }
    canonical_skips = sorted(
        name for name, item in canonical_results.items() if item.get("skip_reason")
    )
    if canonical_skips:
        raise ValueError(
            "omisiones inesperadas en la auditoría canónica: " + ", ".join(canonical_skips)
        )
    for name in sorted(skipped):
        primary_version = primary_versions.get(name)
        canonical_version = canonical_versions.get(name)
        result = canonical_results.get(name)
        if primary_version is None or canonical_version is None or result is None:
            raise ValueError(f"No se pudo vincular {name} con su paquete canónico auditado.")
        if result.get("skip_reason"):
            raise ValueError(f"La variante canónica de {name} también fue omitida.")
        if result.get("version") != canonical_version:
            raise ValueError(f"pip-audit no examinó la versión canónica esperada de {name}.")
        if primary_version.split("+", 1)[0] != canonical_version:
            raise ValueError(
                f"La variante {name} {primary_version} no coincide con "
                f"la versión canónica {canonical_version}."
            )


def run_audit(path: Path, ignored_vulnerabilities: tuple[str, ...]) -> dict[str, Any]:
    """Run pip-audit with machine-readable output and return its report."""

    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(path),
        "--no-deps",
        "--disable-pip",
        "--format=json",
    ]
    for vulnerability in ignored_vulnerabilities:
        command.extend(("--ignore-vuln", vulnerability))
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pip-audit no devolvió un informe JSON válido.") from exc
    if completed.returncode != 0:
        raise RuntimeError("pip-audit detectó una vulnerabilidad o no pudo completar la auditoría.")
    if not isinstance(payload, dict):
        raise RuntimeError("pip-audit devolvió un informe inesperado.")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lockfile", type=Path)
    parser.add_argument("--canonical-lock", type=Path)
    parser.add_argument("--allow-variant-skip", action="append", default=[])
    parser.add_argument("--ignore-vuln", action="append", default=[])
    args = parser.parse_args()
    try:
        primary = run_audit(args.lockfile, tuple(args.ignore_vuln))
        canonical = (
            run_audit(args.canonical_lock, tuple(args.ignore_vuln))
            if args.canonical_lock is not None
            else None
        )
        validate_audit_coverage(
            primary,
            primary_versions=locked_versions(args.lockfile),
            allowed_variant_skips=frozenset(args.allow_variant_skip),
            canonical=canonical,
            canonical_versions=(
                locked_versions(args.canonical_lock) if args.canonical_lock is not None else None
            ),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Auditoría incompleta: {exc}")
        return 1
    print("Auditoría completa: no hay dependencias sin cobertura inesperada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
