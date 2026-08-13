from __future__ import annotations

from pathlib import Path

import pytest
from scripts.sync_version import (
    VERSIONED_FILES,
    project_version,
    synchronize,
    validate_release_tag,
)


def _write_version_fixture(root: Path, version: str = "1.2.3") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "example"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    contents = {
        "src/parsezen/__init__.py": '__version__ = "0.0.0"\n',
        "distribution/windows/Parsezen.iss": '#define AppVersion "0.0.0"\n',
        "distribution/windows/version_info.txt": (
            "  filevers=(0, 0, 0, 0)\n"
            "  prodvers=(0, 0, 0, 0)\n"
            "  StringStruct('FileVersion', '0.0.0'),\n"
            "  StringStruct('ProductVersion', '0.0.0')\n"
        ),
        "README.md": "**Versión 0.0.0 · Windows**\n",
        "docs/user-guide.md": "Esta guía describe Parsezen 0.0.0. Más texto.\n",
    }
    for relative_path, content in contents.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_sync_uses_pyproject_as_the_single_version_source(tmp_path: Path) -> None:
    _write_version_fixture(tmp_path)

    assert project_version(tmp_path) == "1.2.3"
    assert synchronize(tmp_path, check=True) == VERSIONED_FILES
    assert synchronize(tmp_path) == VERSIONED_FILES
    assert synchronize(tmp_path, check=True) == ()

    assert '__version__ = "1.2.3"' in (tmp_path / "src/parsezen/__init__.py").read_text(
        encoding="utf-8"
    )
    version_info = (tmp_path / "distribution/windows/version_info.txt").read_text(encoding="utf-8")
    assert "filevers=(1, 2, 3, 0)" in version_info
    assert "ProductVersion', '1.2.3" in version_info


def test_repository_version_metadata_is_synchronized() -> None:
    root = Path(__file__).parents[1]

    assert synchronize(root, check=True) == ()


def test_release_tag_must_match_the_exact_project_version() -> None:
    validate_release_tag("v1.2.3", "1.2.3")

    with pytest.raises(ValueError, match="debe ser v1.2.3"):
        validate_release_tag("v9.9.9", "1.2.3")
