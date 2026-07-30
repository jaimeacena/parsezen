"""Synchronize generated release metadata with the version in pyproject.toml."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONED_FILES = (
    Path("src/parsezen/__init__.py"),
    Path("distribution/windows/Parsezen.iss"),
    Path("distribution/windows/version_info.txt"),
    Path("README.md"),
    Path("docs/user-guide.md"),
)


def project_version(root: Path = ROOT) -> str:
    """Return the sole authored version value."""
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = payload["project"]["version"]
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("La versión de pyproject.toml debe usar el formato X.Y.Z.")
    return version


def synchronized_text(path: Path, text: str, version: str) -> str:
    """Render one known generated file without touching the filesystem."""
    relative = path.as_posix()
    if relative == "src/parsezen/__init__.py":
        return re.sub(r'^__version__ = "[^"]+"$', f'__version__ = "{version}"', text, flags=re.M)
    if relative == "distribution/windows/Parsezen.iss":
        return re.sub(
            r'^#define AppVersion "[^"]+"$',
            f'#define AppVersion "{version}"',
            text,
            flags=re.M,
        )
    if relative == "distribution/windows/version_info.txt":
        parts = tuple(int(part) for part in version.split(".")) + (0,)
        rendered = re.sub(
            r"(?m)^(\s*(?:filevers|prodvers)=)\([^)]*\)",
            rf"\g<1>{parts}",
            text,
        )
        return re.sub(
            r"(StringStruct\('(FileVersion|ProductVersion)', ')[^']+('\))",
            rf"\g<1>{version}\g<3>",
            rendered,
        )
    if relative == "README.md":
        return re.sub(r"\*\*Versión [^ ·*]+", f"**Versión {version}", text, count=1)
    if relative == "docs/user-guide.md":
        return re.sub(
            r"(Esta guía describe Parsezen )\d+\.\d+\.\d+",
            rf"\g<1>{version}",
            text,
            count=1,
        )
    raise ValueError(f"Archivo de versión no reconocido: {relative}")


def synchronize(root: Path = ROOT, *, check: bool = False) -> tuple[Path, ...]:
    """Write stale metadata or return it unchanged in check mode."""
    version = project_version(root)
    stale: list[Path] = []
    for relative_path in VERSIONED_FILES:
        target = root / relative_path
        current = target.read_text(encoding="utf-8")
        expected = synchronized_text(relative_path, current, version)
        if current == expected:
            continue
        stale.append(relative_path)
        if not check:
            target.write_text(expected, encoding="utf-8")
    return tuple(stale)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="No escribe; falla si algún metadato no coincide con pyproject.toml.",
    )
    args = parser.parse_args()
    stale = synchronize(check=args.check)
    if args.check and stale:
        print("Metadatos de versión desactualizados:")
        for path in stale:
            print(f"- {path.as_posix()}")
        print("Ejecuta: python scripts/sync_version.py")
        return 1
    if stale:
        print(f"Versión sincronizada en {len(stale)} archivos.")
    else:
        print("La versión ya está sincronizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
