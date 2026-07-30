from pathlib import Path


def test_development_launcher_always_prefers_current_checkout_sources() -> None:
    launcher = Path("Abrir Parsezen.cmd").read_text(encoding="utf-8")

    assert 'set "PYTHONPATH=%~dp0src;%PYTHONPATH%"' in launcher
    assert '"%PARSEZEN_PYTHON%" -m parsezen' in launcher
