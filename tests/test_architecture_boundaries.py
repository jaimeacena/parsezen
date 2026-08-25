from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "parsezen"
_QT_COMPOSITION_ROOTS = frozenset({"__main__.py"})


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    return tuple(imported)


def _class_methods(path: Path, class_name: str) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return frozenset(
        node.name
        for node in class_node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def test_domain_depends_only_on_domain_modules() -> None:
    violations: list[str] = []
    for path in (_PACKAGE_ROOT / "domain").glob("*.py"):
        for imported in _imports(path):
            if imported.startswith("parsezen.") and not imported.startswith("parsezen.domain"):
                violations.append(f"{path.name}: {imported}")

    assert violations == []


def test_application_does_not_import_presentation_or_infrastructure() -> None:
    forbidden = ("parsezen.presentation", "parsezen.infrastructure")
    violations = [
        f"{path.name}: {imported}"
        for path in (_PACKAGE_ROOT / "application").glob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden)
    ]

    assert violations == []


def test_core_root_modules_do_not_depend_back_on_application() -> None:
    violations = [
        f"{path.name}: {imported}"
        for path in _PACKAGE_ROOT.glob("*.py")
        if path.name != "__main__.py"
        for imported in _imports(path)
        if imported.startswith("parsezen.application")
    ]

    assert violations == []


def test_infrastructure_does_not_depend_on_application_or_presentation() -> None:
    forbidden = ("parsezen.application", "parsezen.presentation")
    violations = [
        f"{path.name}: {imported}"
        for path in (_PACKAGE_ROOT / "infrastructure").glob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden)
    ]

    assert violations == []


def test_only_the_presentation_composition_root_imports_infrastructure() -> None:
    violations = [
        f"{path.name}: {imported}"
        for path in (_PACKAGE_ROOT / "presentation").glob("*.py")
        if path.name != "main_window.py"
        for imported in _imports(path)
        if imported.startswith("parsezen.infrastructure")
    ]

    assert violations == []


def test_qt_is_confined_to_presentation_and_the_entrypoint() -> None:
    violations = [
        str(path.relative_to(_PACKAGE_ROOT))
        for path in _PACKAGE_ROOT.rglob("*.py")
        if any(
            imported == "PySide6" or imported.startswith("PySide6.") for imported in _imports(path)
        )
        and path.parent != _PACKAGE_ROOT / "presentation"
        and path.name not in _QT_COMPOSITION_ROOTS
    ]

    assert violations == []


def test_pdf_checkpoint_codec_does_not_depend_on_extraction_or_ocr() -> None:
    imported = _imports(_PACKAGE_ROOT / "pdf_checkpoints.py")

    assert "pdfplumber" not in imported
    assert "parsezen.pdf_conversion" not in imported
    assert "parsezen.ocr_conversion" not in imported
    assert "parsezen.pdf_layout" in imported


def test_markdown_safety_does_not_own_network_or_model_selection() -> None:
    imported = _imports(_PACKAGE_ROOT / "ai_markdown_safety.py")

    assert "httpx" not in imported
    assert "parsezen.local_ai_transport" not in imported
    assert "parsezen.local_models" not in imported


def test_pipeline_contracts_do_not_depend_on_the_orchestrator_or_output() -> None:
    imported = _imports(_PACKAGE_ROOT / "pipeline" / "contracts.py")

    assert "parsezen.processing" not in imported
    assert "parsezen.output" not in imported


def test_pipeline_preparation_does_not_depend_on_transform_or_publish() -> None:
    imported = _imports(_PACKAGE_ROOT / "pipeline" / "prepare.py")

    assert "parsezen.processing" not in imported
    assert "parsezen.improvement" not in imported
    assert "parsezen.output" not in imported


def test_pipeline_publication_does_not_depend_on_transform_implementations() -> None:
    imported = _imports(_PACKAGE_ROOT / "pipeline" / "publish.py")

    assert "parsezen.processing" not in imported
    assert "parsezen.local_ai_transport" not in imported
    assert "parsezen.offline_translation" not in imported


def test_private_worker_channel_remains_protocol_agnostic() -> None:
    imported = _imports(_PACKAGE_ROOT / "workers" / "private_channel.py")

    assert not any(
        name.startswith(("parsezen.ocr", "parsezen.offline_translation")) for name in imported
    )
    assert "parsezen.workers.private_channel" in _imports(_PACKAGE_ROOT / "ocr_executor.py")
    assert "parsezen.workers.private_channel" in _imports(
        _PACKAGE_ROOT / "offline_translation_executor.py"
    )


def test_main_window_delegates_the_local_ai_workflow() -> None:
    delegated = _class_methods(
        _PACKAGE_ROOT / "presentation" / "local_ai_workflow.py",
        "LocalAIWorkflow",
    )
    window_methods = _class_methods(
        _PACKAGE_ROOT / "presentation" / "main_window.py",
        "ParsezenMainWindow",
    )

    assert {"show_component_setup", "start_model_discovery"} <= delegated
    workflow_actions = {name for name in delegated if not name.startswith("_")}
    assert workflow_actions.isdisjoint(window_methods)
    assert "__getattr__" not in window_methods


def test_main_window_does_not_own_queue_session_flags() -> None:
    source = (_PACKAGE_ROOT / "presentation" / "main_window.py").read_text(encoding="utf-8")

    assert "self._is_processing" not in source
    assert "self._batch_running" not in source
    assert "self._pause_requested" not in source
    assert "self._current_job_id" not in source
    assert "self._runtime_by_job" not in source


def test_main_window_timer_only_targets_temporal_projection() -> None:
    source = (_PACKAGE_ROOT / "presentation" / "main_window.py").read_text(encoding="utf-8")

    assert "setInterval(1_000)" in source
    assert "timeout.connect(self._refresh_temporal_projection)" in source
    assert "timeout.connect(self._sync_workspace)" not in source
