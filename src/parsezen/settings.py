"""Small JSON settings file for Parsezen's user choices."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import mkstemp

from platformdirs import user_config_path

from parsezen import APP_DISPLAY_NAME, APP_STORAGE_NAME
from parsezen.errors import SettingsError

SETTINGS_FILENAME = "settings.json"
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 600.0
CHECKPOINT_RETENTION_PRESETS = (0, 7, 30, 90)
MIN_CHECKPOINT_RETENTION_DAYS = 0
MAX_CHECKPOINT_RETENTION_DAYS = 90
CONTEXT_WINDOW_PRESETS = (4_096, 8_192, 16_384)
MIN_CONTEXT_WINDOW = 512
MAX_CONTEXT_WINDOW = 262_144
KNOWN_FIELDS = frozenset(
    {
        "model",
        "context_window",
        "translation_model",
        "translation_context_window",
        "review_model",
        "review_context_window",
        "output_directory",
        "image_output_directory",
        "timeout_seconds",
        "checkpoint_retention_days",
        "endpoint",
    }
)


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Persistent settings shared by the UI and one processing request."""

    model: str | None = None
    context_window: int | None = None
    # These phase-specific values are optional while the old global pair is
    # still accepted for queued jobs and settings files created by older
    # versions.
    translation_model: str | None = None
    translation_context_window: int | None = None
    review_model: str | None = None
    review_context_window: int | None = None
    output_directory: Path | None = None
    image_output_directory: Path | None = None
    timeout_seconds: float = 120.0
    checkpoint_retention_days: int = 30


def get_settings_path() -> Path:
    """Return the platform-appropriate settings path."""
    return user_config_path(APP_STORAGE_NAME, appauthor=False) / SETTINGS_FILENAME


def load_settings(path: Path | None = None) -> AppSettings:
    """Load settings, preserving an invalid file and reporting a repairable error."""
    settings_path = path if path is not None else get_settings_path()
    try:
        raw_data = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppSettings()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(
            "La configuración guardada no se pudo leer. Se usarán valores seguros hasta repararla."
        ) from exc

    try:
        return _settings_from_json(raw_data)
    except (TypeError, ValueError, SettingsError) as exc:
        raise SettingsError(
            "La configuración guardada no es válida. Se usarán valores seguros hasta repararla."
        ) from exc


def save_settings(settings: AppSettings, path: Path | None = None) -> None:
    """Validate and atomically save settings without leaving a partial JSON file."""
    settings_path = path if path is not None else get_settings_path()
    normalized = validate_settings(settings)
    payload = {
        "model": normalized.model,
        "context_window": normalized.context_window,
        "output_directory": (
            str(normalized.output_directory) if normalized.output_directory is not None else None
        ),
        "image_output_directory": (
            str(normalized.image_output_directory)
            if normalized.image_output_directory is not None
            else None
        ),
        "timeout_seconds": normalized.timeout_seconds,
        "checkpoint_retention_days": normalized.checkpoint_retention_days,
    }
    for field_name in (
        "translation_model",
        "translation_context_window",
        "review_model",
        "review_context_window",
    ):
        value = getattr(normalized, field_name)
        if value is not None:
            payload[field_name] = value

    temporary_path: Path | None = None
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = mkstemp(
            dir=settings_path.parent,
            prefix=".parsezen-settings-",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as settings_file:
            json.dump(payload, settings_file, ensure_ascii=False, indent=2)
            settings_file.write("\n")
            settings_file.flush()
            os.fsync(settings_file.fileno())
        os.replace(temporary_path, settings_path)
        temporary_path = None
    except (OSError, UnicodeError, TypeError) as exc:
        raise SettingsError(f"No se pudo guardar la configuración de {APP_DISPLAY_NAME}.") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def validate_settings(settings: AppSettings) -> AppSettings:
    """Return normalized settings or raise a stable application error."""
    model = _optional_model_id(settings.model, "El modelo")
    context_window = _validate_context_window(settings.context_window)
    translation_model = _optional_model_id(
        settings.translation_model,
        "El modelo de traducción",
    )
    translation_context_window = _validate_context_window(settings.translation_context_window)
    review_model = _optional_model_id(settings.review_model, "El modelo de revisión")
    review_context_window = _validate_context_window(settings.review_context_window)

    output_directory = settings.output_directory
    if output_directory is not None and not isinstance(output_directory, Path):
        raise SettingsError("La carpeta de salida configurada no es válida.")
    image_output_directory = settings.image_output_directory
    if image_output_directory is not None and not isinstance(image_output_directory, Path):
        raise SettingsError("La carpeta de imágenes configurada no es válida.")

    timeout = settings.timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SettingsError("El timeout configurado no es válido.")
    timeout = float(timeout)
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise SettingsError(
            "El timeout debe estar entre "
            f"{MIN_TIMEOUT_SECONDS:g} y {MAX_TIMEOUT_SECONDS:g} segundos."
        )

    checkpoint_retention_days = settings.checkpoint_retention_days
    if (
        isinstance(checkpoint_retention_days, bool)
        or not isinstance(checkpoint_retention_days, int)
        or not MIN_CHECKPOINT_RETENTION_DAYS
        <= checkpoint_retention_days
        <= MAX_CHECKPOINT_RETENTION_DAYS
    ):
        raise SettingsError(
            "La retención de trabajo debe estar entre "
            f"{MIN_CHECKPOINT_RETENTION_DAYS} y {MAX_CHECKPOINT_RETENTION_DAYS} días."
        )

    return AppSettings(
        model=model,
        context_window=context_window,
        translation_model=translation_model,
        translation_context_window=translation_context_window,
        review_model=review_model,
        review_context_window=review_context_window,
        output_directory=output_directory,
        image_output_directory=image_output_directory,
        timeout_seconds=timeout,
        checkpoint_retention_days=checkpoint_retention_days,
    )


def _settings_from_json(raw_data: object) -> AppSettings:
    if not isinstance(raw_data, dict):
        raise TypeError
    unknown_fields = set(raw_data) - KNOWN_FIELDS
    if unknown_fields:
        raise ValueError

    model = raw_data.get("model")
    context_window = raw_data.get("context_window")
    translation_model = raw_data.get("translation_model")
    translation_context_window = raw_data.get("translation_context_window")
    review_model = raw_data.get("review_model")
    review_context_window = raw_data.get("review_context_window")
    output_value = raw_data.get("output_directory")
    image_output_value = raw_data.get("image_output_directory")
    timeout = raw_data.get("timeout_seconds", 120.0)
    checkpoint_retention_days = raw_data.get("checkpoint_retention_days", 30)

    for value in (model, translation_model, review_model):
        if value is not None and not isinstance(value, str):
            raise TypeError
    if output_value is not None and not isinstance(output_value, str):
        raise TypeError
    if image_output_value is not None and not isinstance(image_output_value, str):
        raise TypeError

    output_directory = Path(output_value) if output_value is not None else None
    image_output_directory = Path(image_output_value) if image_output_value is not None else None
    return validate_settings(
        AppSettings(
            model=model,
            context_window=context_window,
            translation_model=translation_model,
            translation_context_window=translation_context_window,
            review_model=review_model,
            review_context_window=review_context_window,
            output_directory=output_directory,
            image_output_directory=image_output_directory,
            timeout_seconds=timeout,
            checkpoint_retention_days=checkpoint_retention_days,
        )
    )


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{label} no es válido.")
    normalized = value.strip()
    if not normalized:
        return None
    if any(character in normalized for character in "\r\n\0"):
        raise SettingsError(f"{label} contiene caracteres no válidos.")
    return normalized


def _optional_model_id(value: str | None, label: str) -> str | None:
    candidate = _optional_text(value, label)
    if candidate is None:
        return None
    # Imported lazily because local_models consumes the context presets from
    # this module during its own initialization.
    from parsezen.errors import LocalModelUnavailableError
    from parsezen.local_models import is_cloud_model_id, validate_ollama_model_id

    if is_cloud_model_id(candidate) or candidate.casefold().endswith("-cloud"):
        raise SettingsError(
            f"{label} solo admite modelos almacenados localmente y no puede usar cloud."
        )
    try:
        validated = validate_ollama_model_id(candidate)
    except LocalModelUnavailableError as exc:
        raise SettingsError(f"{label} no es un identificador local válido.") from exc
    return validated


def settings_for_translation(settings: AppSettings) -> AppSettings:
    """Resolve the settings used by translation and translation repair."""

    return replace(
        settings,
        model=settings.translation_model or settings.model,
        context_window=(
            settings.translation_context_window
            if settings.translation_context_window is not None
            else settings.context_window
        ),
    )


def settings_for_review(settings: AppSettings) -> AppSettings:
    """Resolve the settings used by every review phase."""

    return replace(
        settings,
        model=settings.review_model or settings.model,
        context_window=(
            settings.review_context_window
            if settings.review_context_window is not None
            else settings.context_window
        ),
    )


def has_specialized_ai_profiles(settings: AppSettings | None) -> bool:
    """Return whether phase-specific AI settings alter the legacy profile."""

    return settings is not None and any(
        value is not None
        for value in (
            settings.translation_model,
            settings.translation_context_window,
            settings.review_model,
            settings.review_context_window,
        )
    )


def _validate_context_window(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError("La ventana de contexto configurada no es válida.")
    if not MIN_CONTEXT_WINDOW <= value <= MAX_CONTEXT_WINDOW:
        raise SettingsError("La ventana de contexto no está dentro de un tamaño seguro.")
    return value
