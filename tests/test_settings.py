from __future__ import annotations

import json
from pathlib import Path

import pytest

import parsezen.settings as settings_module
from parsezen.errors import SettingsError
from parsezen.settings import (
    AppSettings,
    load_settings,
    save_settings,
    settings_for_review,
    settings_for_translation,
    validate_settings,
)


def test_missing_settings_file_uses_safe_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.json")

    assert settings == AppSettings()
    assert settings.model is None
    assert settings.context_window is None


def test_settings_round_trip_through_atomic_json(tmp_path: Path) -> None:
    path = tmp_path / "config" / "settings.json"
    expected = AppSettings(
        model="local-model",
        context_window=8_192,
        output_directory=tmp_path / "output",
        image_output_directory=tmp_path / "attachments",
        timeout_seconds=45,
    )

    save_settings(expected, path)

    assert load_settings(path) == AppSettings(
        model="local-model",
        context_window=8_192,
        output_directory=tmp_path / "output",
        image_output_directory=tmp_path / "attachments",
        timeout_seconds=45.0,
    )
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "model",
        "context_window",
        "output_directory",
        "image_output_directory",
        "timeout_seconds",
        "checkpoint_retention_days",
    }
    assert list(path.parent.glob(".parsezen-settings-*.tmp")) == []


def test_phase_settings_fall_back_to_the_legacy_global_pair() -> None:
    settings = AppSettings(
        model="legacy",
        context_window=4_096,
        translation_model="translator",
        translation_context_window=8_192,
        review_model="reviewer",
        review_context_window=16_384,
    )

    translation = settings_for_translation(settings)
    review = settings_for_review(settings)

    assert (translation.model, translation.context_window) == ("translator", 8_192)
    assert (review.model, review.context_window) == ("reviewer", 16_384)
    assert (settings_for_translation(AppSettings(model="legacy")).model) == "legacy"


def test_legacy_endpoint_is_ignored_during_settings_migration(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "endpoint": "http://127.0.0.1:11434/v1",
                "model": "qwen3:4b",
                "output_directory": None,
                "timeout_seconds": 120,
            }
        ),
        encoding="utf-8",
    )

    assert load_settings(path) == AppSettings(model="qwen3:4b")


def test_corrupt_settings_are_preserved_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    corrupt_content = "{not-json"
    path.write_text(corrupt_content, encoding="utf-8")

    with pytest.raises(SettingsError, match="no se pudo leer"):
        load_settings(path)

    assert path.read_text(encoding="utf-8") == corrupt_content


def test_unknown_settings_fields_are_rejected_without_rewriting(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    content = '{"model": null, "future": true}'
    path.write_text(content, encoding="utf-8")

    with pytest.raises(SettingsError, match="no es válida"):
        load_settings(path)

    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"model": 4},
        {"output_directory": 4},
        {"image_output_directory": 4},
    ],
)
def test_settings_json_rejects_invalid_shapes(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SettingsError, match="no es válida"):
        load_settings(path)


def test_settings_reject_invalid_paths_and_normalize_optional_model() -> None:
    with pytest.raises(SettingsError, match="carpeta de salida"):
        validate_settings(AppSettings(output_directory="invalid"))  # type: ignore[arg-type]
    with pytest.raises(SettingsError, match="carpeta de imágenes"):
        validate_settings(AppSettings(image_output_directory="invalid"))  # type: ignore[arg-type]
    with pytest.raises(SettingsError, match="modelo"):
        validate_settings(AppSettings(model=4))  # type: ignore[arg-type]
    with pytest.raises(SettingsError, match="caracteres"):
        validate_settings(AppSettings(model="bad\nmodel"))

    assert validate_settings(AppSettings(model="   ")).model is None


@pytest.mark.parametrize(
    "field_name,model_id",
    [
        ("model", "example:cloud"),
        ("translation_model", "example:fast-cloud"),
        ("review_model", "example-cloud"),
    ],
)
def test_settings_reject_cloud_model_ids(field_name: str, model_id: str) -> None:
    with pytest.raises(SettingsError, match="cloud"):
        validate_settings(AppSettings(**{field_name: model_id}))


@pytest.mark.parametrize("context_window", [True, 0, 511, 262_145, "8192", 8_192.0])
def test_rejects_invalid_context_windows(context_window: object) -> None:
    with pytest.raises(SettingsError, match="ventana de contexto"):
        validate_settings(AppSettings(context_window=context_window))  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [True, 0, 0.5, 601, "120"])
def test_rejects_invalid_timeouts(timeout: object) -> None:
    with pytest.raises(SettingsError, match="timeout"):
        validate_settings(AppSettings(timeout_seconds=timeout))  # type: ignore[arg-type]


@pytest.mark.parametrize("days", [True, -1, 91, 7.5, "30"])
def test_rejects_invalid_checkpoint_retention(days: object) -> None:
    with pytest.raises(SettingsError, match="retención"):
        validate_settings(
            AppSettings(checkpoint_retention_days=days)  # type: ignore[arg-type]
        )


def test_failed_atomic_save_keeps_previous_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.json"
    original = AppSettings(model="first")
    save_settings(original, path)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)

    with pytest.raises(SettingsError, match="No se pudo guardar"):
        save_settings(AppSettings(model="second"), path)

    assert load_settings(path) == original
    assert list(tmp_path.glob(".parsezen-settings-*.tmp")) == []
