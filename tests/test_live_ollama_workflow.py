"""Small opt-in acceptance check against the actual local Ollama installation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.validate_real_workflows import (
    run_live_workflows,
    select_live_settings,
    write_synthetic_pdf,
)

pytestmark = pytest.mark.live_ollama


@pytest.mark.skipif(
    os.environ.get("PARSEZEN_RUN_LIVE_OLLAMA") != "1",
    reason="La prueba real de Ollama solo se ejecuta de forma explícita.",
)
def test_short_pdf_completes_translation_reviews_and_epub_build(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-workflow.pdf"
    write_synthetic_pdf(source)

    results = run_live_workflows(
        (source,),
        tmp_path / "outputs",
        select_live_settings(),
    )

    assert len(results) == 1
    assert results[0].passed, (
        f"{results[0].error_type or 'error'} en {results[0].failed_stage or 'inicio'}"
    )
