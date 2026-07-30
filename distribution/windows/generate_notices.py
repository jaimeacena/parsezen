"""Generate the third-party license bundle used by the Windows package."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path, PurePosixPath

_MAX_LICENSE_BYTES = 2 * 1024 * 1024


def _decode_license_text(payload: bytes) -> str:
    """Decode common license encodings without silently inserting replacement glyphs."""

    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AssertionError("latin-1 can decode every byte sequence")


def _repair_upstream_replacement_glyphs(text: str) -> str:
    """Repair known upstream metadata that already ships with a lost character."""

    return text.replace("J\ufffdrgen Stenarson", "Jürgen Stenarson")


def _declared_license(distribution: metadata.Distribution) -> str:
    fields = distribution.metadata
    expression = fields.get("License-Expression")
    license_name = fields.get("License")
    classifiers = [
        value.removeprefix("License :: ")
        for value in fields.get_all("Classifier", ())
        if value.startswith("License :: ")
    ]
    return expression or license_name or "; ".join(classifiers) or "No declarada en los metadatos"


def _license_texts(distribution: metadata.Distribution) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    seen_texts: set[str] = set()
    for package_file in distribution.files or ():
        normalized = str(package_file).replace("\\", "/")
        path = PurePosixPath(normalized)
        name = path.name.casefold()
        in_metadata = any(part.casefold().endswith(".dist-info") for part in path.parts)
        in_license_dir = "licenses" in (part.casefold() for part in path.parts)
        if not in_metadata or not (
            in_license_dir or name.startswith(("license", "copying", "notice"))
        ):
            continue
        metadata_index = next(
            index for index, part in enumerate(path.parts) if part.casefold().endswith(".dist-info")
        )
        display_name = "/".join(path.parts[metadata_index + 1 :])
        resolved = Path(distribution.locate_file(package_file))
        try:
            if not resolved.is_file() or resolved.stat().st_size > _MAX_LICENSE_BYTES:
                continue
            text = _repair_upstream_replacement_glyphs(
                _decode_license_text(resolved.read_bytes())
            ).strip()
        except OSError:
            continue
        if text and text not in seen_texts:
            seen_texts.add(text)
            results.append((display_name, text))
    return sorted(results, key=lambda item: item[0].casefold())


def render_notices() -> str:
    sections = [
        "Parsezen — avisos de software de terceros",
        "=" * 48,
        "",
        "Generado automáticamente desde el entorno exacto de empaquetado.",
        "Este documento no concede una licencia sobre el código propio de Parsezen.",
    ]
    installed = sorted(
        metadata.distributions(),
        key=lambda distribution: (distribution.metadata.get("Name") or "").casefold(),
    )
    for distribution in installed:
        name = distribution.metadata.get("Name") or "Paquete sin nombre"
        sections.extend(
            [
                "",
                "-" * 80,
                f"{name} {distribution.version}",
                f"Licencia declarada: {_declared_license(distribution)}",
            ]
        )
        license_texts = _license_texts(distribution)
        if not license_texts:
            sections.append("No se encontró un texto de licencia incluido en la distribución.")
            continue
        for filename, text in license_texts:
            sections.extend(["", f"[{filename}]", text])
    return "\n".join(sections).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_notices(), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
