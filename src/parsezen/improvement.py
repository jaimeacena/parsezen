"""Conservative Markdown improvement through Ollama's native local API."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

import httpx
from markdown_it import MarkdownIt
from markdown_it.token import Token

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import ImprovementError, LocalModelUnavailableError, SettingsError
from parsezen.local_models import (
    DEFAULT_CONTEXT_WINDOW,
    OLLAMA_BASE_URL,
    is_cloud_model_id,
    is_ollama_local_only_configured,
)
from parsezen.settings import AppSettings, validate_settings
from parsezen.translation_quality import (
    ATX_HEADING_PATTERN,
    FENCE_PATTERN,
    HTML_COMMENT_PATTERN,
    INLINE_CODE_PATTERN,
    NUMBER_PATTERN,
    TABLE_DIVIDER_PATTERN,
    TranslationQualityError,
    detect_language_code,
    find_titles_with_source_language_residue,
    find_untranslated_title_lines,
    is_probable_organization_name_line,
    is_unmarked_title_line,
    link_destination_spans,
    markdown_heading_levels,
    markdown_link_destinations,
    markdown_table_shapes,
    natural_language_text,
    probable_uppercase_person_name_bases,
    resolve_language_code,
    uppercase_person_name_base,
    validate_translation_quality,
)

MAX_CHUNK_CHARACTERS = 4_000
MAX_INPUT_CHARACTERS = MAX_CHUNK_CHARACTERS
MAX_TRANSLATION_CHUNK_CHARACTERS = 1_500
MAX_CONSERVED_VALUES_PER_CHUNK = 16
MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK = 8
MAX_FOCUSED_TITLE_REPAIRS_PER_CHUNK = MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
MAX_STRUCTURE_DIRECTIVES_PER_CHUNK = 16
MAX_OUTPUT_CHARACTERS = 100_000
MAX_DOCUMENT_CHARACTERS = 1_000_000
MAX_DOCUMENT_OUTPUT_CHARACTERS = 2_000_000

LOGGER = logging.getLogger(__name__)

RAW_URL_PATTERN = re.compile(r"(?:https?://|mailto:)[^\s<>)\]]+")
TITLE_ROMAN_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z])([IVXLCDM]+)(?=[ \t]+[+-]?\d)",
)

BASE_INSTRUCTIONS = """Eres un editor conservador de documentos Markdown.
Devuelve únicamente el Markdown transformado, sin introducciones, comentarios ni cercas externas.

Reglas obligatorias:
- No resumas, inventes, omitas ni reordenes contenido.
- Conserva exactamente números, fechas, destinos de enlaces y referencias.
- Traduce las cantidades escritas con palabras como palabras; no las conviertas en cifras.
- No alteres bloques de código, código en línea ni el significado de las tablas.
- Conserva exactamente la cantidad y el nivel de cada encabezado (`#`, `##`, etc.).
- Conserva listas, énfasis y estructura Markdown.
- Conserva la separación de párrafos y traduce cada frase, incluidos títulos, listas y tablas.
- Traduce también los títulos escritos en mayúsculas; conserva únicamente sus nombres propios.
- Mantén nombres propios, siglas, identificadores y terminología de forma coherente.
- No modifiques ni elimines marcadores `PZDOC` ni comentarios internos que los contengan.
- No añadas explicaciones sobre los cambios.
"""

REVIEW_BASE_INSTRUCTIONS = """Eres un editor profesional de documentos Markdown.
Devuelve únicamente el Markdown revisado, sin introducciones, comentarios ni cercas externas.

Reglas obligatorias:
- No resumas, inventes ni cambies el significado o la voz del autor.
- Conserva nombres propios, citas, cifras con significado, enlaces, imágenes, código y tablas.
- No modifiques ni elimines marcadores `PZDOC` ni comentarios internos que los contengan.
- Mantén el orden de lectura y no añadas contenido que no exista en el documento.
- No añadas explicaciones sobre los cambios.
"""

PLAIN_REVIEW_BASE_INSTRUCTIONS = """Eres un editor profesional de texto sin formato.
Devuelve únicamente el texto revisado, sin introducciones, comentarios ni cercas externas.

Reglas obligatorias:
- No resumas, inventes ni cambies el significado o la voz del autor.
- Conserva nombres propios, citas, cifras con significado y el orden de lectura.
- No modifiques ni elimines marcadores `PZDOC` ni comentarios internos que los contengan.
- No añadas sintaxis Markdown ni explicaciones sobre los cambios.
"""

PLAIN_TEXT_INSTRUCTIONS = """Eres un editor conservador de texto sin formato.
Devuelve únicamente el texto transformado, sin introducciones, comentarios ni cercas externas.

Reglas obligatorias:
- No resumas, inventes, omitas ni reordenes contenido.
- Conserva exactamente números, fechas, destinos de enlaces y referencias.
- Traduce las cantidades escritas con palabras como palabras; no las conviertas en cifras.
- Conserva la separación de párrafos y traduce cada frase y título.
- Mantén nombres propios, siglas, identificadores y terminología de forma coherente.
- No añadas sintaxis Markdown, listas, encabezados ni tablas que no existan en el original.
- No modifiques ni elimines marcadores `PZDOC` ni comentarios internos que los contengan.
- No añadas explicaciones sobre los cambios.
"""

VALIDATION_RETRY_INSTRUCTION = """
La respuesta anterior no superó la validación conservadora. Repite la tarea completa sin omitir,
duplicar ni dejar párrafos en el idioma original. Conserva exactamente todos los números, fechas,
destinos de enlace, bloques de código y la estructura Markdown. La salida debe quedar enteramente
en el idioma solicitado, excepto nombres propios, código e identificadores que no deban traducirse.
No conviertas cantidades escritas con palabras en cifras ni cifras en palabras.
Traduce todos los títulos y encabezados, incluso cuando estén completamente en mayúsculas.
"""

FIDELITY_RETRY_INSTRUCTION = """
La respuesta anterior alteró elementos protegidos. Repite la tarea copiando exactamente, en el
mismo orden, cifras, fechas, enlaces, comentarios internos, marcadores y cualquier valor opaco.
Transforma únicamente el texto natural permitido.
"""

LANGUAGE_RETRY_INSTRUCTION = """
La respuesta anterior dejó texto natural o títulos en el idioma de origen. Repite la traducción
solo para completar esos pasajes; conserva nombres propios, siglas, cifras y elementos protegidos.
"""

MARKDOWN_RETRY_INSTRUCTION = """
La respuesta anterior cambió la estructura Markdown. Repite la tarea conservando exactamente la
cantidad y niveles de encabezados, tablas, listas, citas, imágenes y separadores de párrafo.
"""

COVERAGE_RETRY_INSTRUCTION = """
La respuesta anterior omitió, repitió o añadió contenido. Devuelve una sola traducción completa:
cada idea y oración de origen debe aparecer exactamente una vez, sin incluir también el original,
sin resúmenes, notas, alternativas ni explicaciones.
"""

SINGLE_LINE_RETRY_INSTRUCTION = """
El fragmento es un único título o elemento de una línea. Devuelve exactamente una sola línea de
texto traducido, sin repetir el original, sin etiquetas, alternativas, comentarios ni explicaciones.
"""

SEMANTIC_RETRY_INSTRUCTION = """
La respuesta anterior invirtió una relación crítica de certeza, negación o ambigüedad. Repite la
traducción conservando exactamente la polaridad y las condiciones lógicas de cada oración.
"""

PROTECTED_VALUES_INSTRUCTION = """
Este fragmento contiene {marker_count} marcadores internos obligatorios. Copia exactamente todos,
una sola vez y en el mismo orden. Los marcadores con formato de comentario HTML representan límites
de párrafo: deben permanecer como líneas independientes entre los mismos dos pasajes.
"""

INSTRUCTION_PLACEHOLDER_COMMENT = "<!-- PZDOC... -->"
_INSTRUCTION_PLACEHOLDER_TAG_PATTERN = re.compile(
    r"(?:<\s*/?\s*PZDOC\s*/?\s*>|&lt;\s*/?\s*PZDOC\s*/?\s*&gt;)",
    re.IGNORECASE,
)

FOCUSED_TITLE_INSTRUCTION = """
El fragmento siguiente es exclusivamente un título o encabezado. Tradúcelo por completo al idioma
solicitado, con una redacción natural y el orden propio de ese idioma. Conserva su nivel Markdown,
números, nombres propios y marcadores internos. Traduce también el significado visible de títulos
de canciones, libros u otras obras: no los conserves en el idioma original solo por ser nombres de
obras, aunque sí debes conservar artistas y nombres propios. Devuelve únicamente el título
traducido.
"""

STRUCTURE_DIRECTIVE_INSTRUCTIONS = """
Analiza la estructura del fragmento numerado. No devuelvas ni reescribas el texto.
Responde únicamente con cero o más directivas, una por línea, en el formato exacto `PZL12=2`,
donde el primer número es un identificador de línea marcado como CANDIDATA y el segundo es un nivel
de encabezado entre 1 y 6. Elige solo títulos reales de capítulo o sección; ignora cuerpo de texto,
cabeceras o pies repetidos, números de página, enlaces, imágenes, tablas y comentarios internos.
Si la candidata ya es un encabezado, conserva su nivel o muévelo como máximo un nivel. Para una
candidata nueva usa únicamente los niveles 1, 2 o 3; los niveles más profundos necesitan una
jerarquía previa explícita que este fragmento no puede demostrar.
Las líneas no incluidas permanecerán sin cambios. No uses cercas, listas ni explicaciones.
"""


class ImprovementMode(StrEnum):
    """Supported conservative transformation modes."""

    CLEAN = "clean"
    TRANSLATE = "translate"
    CLEAN_AND_TRANSLATE = "clean_and_translate"
    REVIEW_CONTENT = "review_content"
    REVIEW_STRUCTURE = "review_structure"


ChunkProgressCallback = Callable[[int, int], None]
TranslationPreservedCallback = Callable[[int, int], None]
CheckpointLoader = Callable[[str], str | None]
CheckpointSaver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class _MarkdownPart:
    text: str
    should_improve: bool
    separator_before: str = ""


@dataclass(frozen=True, slots=True)
class _TranslationContext:
    source_language: str | None
    target_language: str | None
    preserve_paragraphs: bool
    preserved_segments: list[str] = field(default_factory=list, compare=False)


@dataclass(frozen=True, slots=True)
class _ProtectedMarkdown:
    text: str
    values: tuple[_ProtectedValue, ...]


@dataclass(frozen=True, slots=True)
class _ProtectedValue:
    token: str
    value: str
    expected_count: int = 1
    paragraph: bool = False


def build_instructions(
    mode: ImprovementMode,
    target_language: str | None = None,
    *,
    plain_text: bool = False,
) -> str:
    """Build conservative instructions without including document content."""
    language = _validated_language(target_language)

    if mode is ImprovementMode.CLEAN:
        operation = (
            "Corrige únicamente errores claros de formato, espaciado, puntuación y estructura "
            "Markdown. No traduzcas el contenido."
        )
    elif mode is ImprovementMode.TRANSLATE:
        if language is None:
            raise ImprovementError("Indica el idioma de destino para traducir.")
        operation = (
            f"Traduce fielmente TODO el texto natural al idioma {language}. "
            "Usa una redacción natural e idiomática y el orden propio del idioma de destino, "
            "sin calcos innecesarios ni cambios de significado. "
            "Traduce cada título, párrafo, elemento de lista y celda de tabla; no dejes pasajes "
            "en el idioma original salvo nombres propios, siglas, código o identificadores. "
            "No hagas una limpieza independiente ni cambies el contenido."
        )
    elif mode is ImprovementMode.CLEAN_AND_TRANSLATE:
        if language is None:
            raise ImprovementError("Indica el idioma de destino para limpiar y traducir.")
        operation = (
            "En una única transformación, corrige únicamente problemas claros de Markdown y "
            f"traduce fielmente TODO el texto natural al idioma {language}, con una redacción "
            "natural, idiomática y en el orden propio del idioma de destino. No omitas ningún "
            "título, párrafo, elemento de lista ni celda de tabla."
        )
    elif mode is ImprovementMode.REVIEW_CONTENT:
        operation = (
            "Corrige errores claros de OCR, conversión, gramática, espaciado y puntuación. "
            "Elimina únicamente ruido inequívoco producido por la conversión, como números de "
            "página aislados y cabeceras o pies repetidos. No reescribas por estilo, no cambies "
            "la jerarquía de títulos y conserva cualquier fragmento cuya eliminación sea dudosa."
        )
    elif mode is ImprovementMode.REVIEW_STRUCTURE:
        if plain_text:
            raise ImprovementError(
                "La revisión de estructura necesita un documento con formato Markdown."
            )
        operation = (
            "Organiza únicamente la estructura Markdown: reconoce títulos reales, asigna niveles "
            "coherentes y marca límites de capítulos mediante encabezados. Conserva exactamente "
            "las palabras, el orden y todo el contenido; no corrijas, traduzcas, elimines ni "
            "añadas texto. No conviertas cabeceras o pies repetidos en títulos."
        )
    else:
        raise ImprovementError("El modo de mejora seleccionado no es válido.")

    if mode in {ImprovementMode.REVIEW_CONTENT, ImprovementMode.REVIEW_STRUCTURE}:
        base = PLAIN_REVIEW_BASE_INSTRUCTIONS if plain_text else REVIEW_BASE_INSTRUCTIONS
    else:
        base = PLAIN_TEXT_INSTRUCTIONS if plain_text else BASE_INSTRUCTIONS
    return f"{base}\nTarea:\n{operation}"


def improve_markdown(
    markdown: str,
    mode: ImprovementMode,
    settings: AppSettings,
    target_language: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    on_progress: ChunkProgressCallback | None = None,
    on_translation_preserved: TranslationPreservedCallback | None = None,
    cancellation: CancellationToken | None = None,
    load_checkpoint: CheckpointLoader | None = None,
    save_checkpoint: CheckpointSaver | None = None,
    plain_text: bool = False,
    source_language_code: str | None = None,
) -> str:
    """Improve Markdown through ordered, structurally bounded local requests."""
    check_cancelled(cancellation)
    _validate_input(markdown)
    normalized_settings = validate_settings(settings)
    model = normalized_settings.model
    if model is None:
        raise SettingsError("Elige un modelo de IA instalado antes de usar la IA.")
    if is_cloud_model_id(model):
        raise SettingsError("Parsezen solo permite modelos almacenados localmente.")
    if transport is None and not is_ollama_local_only_configured():
        raise SettingsError("Activa el modo solo local de Ollama antes de procesar documentos.")
    context_window = normalized_settings.context_window or DEFAULT_CONTEXT_WINDOW

    instructions = build_instructions(mode, target_language, plain_text=plain_text)
    translation_context: _TranslationContext | None = None
    if mode in {ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE}:
        source_language = source_language_code or detect_language_code(markdown)
        target_code = resolve_language_code(target_language)
        if (
            mode is ImprovementMode.TRANSLATE
            and target_code is not None
            and source_language == target_code
        ):
            LOGGER.info("improvement_completed chunks=0 already_in_target_language=true")
            return markdown
        translation_context = _TranslationContext(
            source_language=source_language,
            target_language=target_code,
            preserve_paragraphs=mode is ImprovementMode.TRANSLATE,
        )
    max_chunk_characters = (
        MAX_TRANSLATION_CHUNK_CHARACTERS
        if translation_context is not None
        else MAX_INPUT_CHARACTERS
    )
    parts = _plan_markdown_parts(
        markdown,
        max_characters=max_chunk_characters,
        protect_paragraphs=translation_context is not None,
    )
    if mode is ImprovementMode.REVIEW_STRUCTURE:
        parts = [
            _MarkdownPart(
                part.text,
                part.should_improve
                and any(_is_structure_heading_candidate(line) for line in part.text.splitlines()),
                part.separator_before,
            )
            for part in parts
        ]
    request_count = sum(part.should_improve for part in parts)
    LOGGER.info("improvement_started chunks=%d", request_count)

    if request_count == 0:
        LOGGER.info("improvement_completed chunks=0")
        return markdown

    try:
        with httpx.Client(
            timeout=normalized_settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            improved_parts: list[str] = []
            preserved_translation_chunks = 0
            current = 0
            for part in parts:
                check_cancelled(cancellation)
                if not part.should_improve:
                    improved_parts.append(part.text)
                    continue

                current += 1
                if on_progress is not None:
                    on_progress(current, request_count)
                part_key = _chunk_checkpoint_key(mode, part.text)
                cached_part = load_checkpoint(part_key) if load_checkpoint is not None else None
                loaded_legacy_checkpoint = False
                if cached_part is None and load_checkpoint is not None:
                    cached_part = load_checkpoint(
                        _legacy_chunk_checkpoint_key(mode, current, part.text)
                    )
                    loaded_legacy_checkpoint = cached_part is not None
                if cached_part is not None:
                    try:
                        _validate_mode_output(part.text, cached_part, mode)
                        if translation_context is not None:
                            _validate_translation(part.text, cached_part, translation_context)
                    except ImprovementError:
                        cached_part = None
                if cached_part is not None:
                    improved_parts.append(cached_part)
                    if loaded_legacy_checkpoint and save_checkpoint is not None:
                        save_checkpoint(part_key, cached_part)
                    continue
                preserved_segments_before = (
                    len(translation_context.preserved_segments)
                    if translation_context is not None
                    else 0
                )
                try:
                    improved_part = _improve_part(
                        client,
                        model,
                        context_window,
                        instructions,
                        part.text,
                        preserve_original_on_failure=mode
                        in {
                            ImprovementMode.CLEAN,
                            ImprovementMode.REVIEW_CONTENT,
                            ImprovementMode.REVIEW_STRUCTURE,
                        },
                        mode=mode,
                        translation_context=translation_context,
                        cancellation=cancellation,
                    )
                    check_cancelled(cancellation)
                    improved_parts.append(improved_part)
                    partially_preserved_translation = (
                        translation_context is not None
                        and len(translation_context.preserved_segments) > preserved_segments_before
                    )
                    if partially_preserved_translation:
                        preserved_translation_chunks += 1
                        if on_translation_preserved is not None:
                            on_translation_preserved(current, request_count)
                    unchanged_optional_review = improved_part == part.text and mode in {
                        ImprovementMode.CLEAN,
                        ImprovementMode.REVIEW_CONTENT,
                        ImprovementMode.REVIEW_STRUCTURE,
                    }
                    if (
                        save_checkpoint is not None
                        and not unchanged_optional_review
                        and not partially_preserved_translation
                    ):
                        save_checkpoint(part_key, improved_part)
                except ImprovementError as exc:
                    if translation_context is not None:
                        preserved_translation_chunks += 1
                        improved_parts.append(part.text)
                        if on_translation_preserved is not None:
                            on_translation_preserved(current, request_count)
                        LOGGER.warning(
                            "translation_chunk_preserved validation_failed_after_retry=true "
                            "chunk=%d total=%d reason=%s",
                            current,
                            request_count,
                            str(exc),
                        )
                        continue
                    raise ImprovementError(
                        f"El fragmento {current} de {request_count} no superó la validación: {exc}"
                    ) from exc
    except httpx.RequestError as exc:
        raise LocalModelUnavailableError(
            "No se pudo contactar con el modelo local. Comprueba que el servidor esté iniciado."
        ) from exc

    check_cancelled(cancellation)
    improved = _assemble_markdown_parts(parts, improved_parts)
    try:
        _validate_mode_output(
            markdown,
            improved,
            mode,
            max_characters=MAX_DOCUMENT_OUTPUT_CHARACTERS,
        )
    except ImprovementError as exc:
        LOGGER.warning(
            "improvement_document_validation_failed mode=%s reason=%s",
            mode.value,
            str(exc),
        )
        if mode not in {ImprovementMode.REVIEW_CONTENT, ImprovementMode.REVIEW_STRUCTURE}:
            raise
        recovered, accepted = _recover_review_reassembly(
            markdown,
            parts,
            improved_parts,
            mode,
            cancellation=cancellation,
        )
        LOGGER.warning(
            "improvement_reassembly_recovered mode=%s accepted_chunks=%d preserved_chunks=%d",
            mode.value,
            accepted,
            sum(
                part.should_improve and proposal != part.text
                for part, proposal in zip(parts, improved_parts, strict=True)
            )
            - accepted,
        )
        return recovered
    if translation_context is not None and preserved_translation_chunks == 0:
        _validate_translation(
            markdown,
            improved,
            translation_context,
        )
    elif translation_context is not None:
        LOGGER.warning(
            "translation_completed_with_preserved_chunks count=%d",
            preserved_translation_chunks,
        )
    check_cancelled(cancellation)
    LOGGER.info("improvement_completed chunks=%d", request_count)
    return improved


def _assemble_markdown_parts(
    parts: list[_MarkdownPart],
    contents: list[str],
) -> str:
    return "".join(
        f"{part.separator_before}{content}" for part, content in zip(parts, contents, strict=True)
    )


def _chunk_checkpoint_key(mode: ImprovementMode, text: str) -> str:
    return hashlib.sha256(f"ollama-chunk-v3\n{mode.value}\n{text}".encode()).hexdigest()


def _legacy_chunk_checkpoint_key(
    mode: ImprovementMode,
    position: int,
    text: str,
) -> str:
    return hashlib.sha256(f"ollama-chunk-v2\n{mode.value}\n{position}\n{text}".encode()).hexdigest()


def _recover_review_reassembly(
    source: str,
    parts: list[_MarkdownPart],
    proposed_parts: list[str],
    mode: ImprovementMode,
    *,
    cancellation: CancellationToken | None,
) -> tuple[str, int]:
    accepted_parts = [part.text for part in parts]
    changed = tuple(
        index
        for index, (part, proposal) in enumerate(zip(parts, proposed_parts, strict=True))
        if part.should_improve and proposal != part.text
    )
    accepted_count = 0

    def consider(indices: tuple[int, ...]) -> None:
        nonlocal accepted_count, accepted_parts
        if not indices:
            return
        check_cancelled(cancellation)
        candidate_parts = list(accepted_parts)
        for index in indices:
            candidate_parts[index] = proposed_parts[index]
        candidate = _assemble_markdown_parts(parts, candidate_parts)
        try:
            _validate_mode_output(
                source,
                candidate,
                mode,
                max_characters=MAX_DOCUMENT_OUTPUT_CHARACTERS,
            )
        except ImprovementError:
            if len(indices) == 1:
                return
            midpoint = len(indices) // 2
            consider(indices[:midpoint])
            consider(indices[midpoint:])
            return
        accepted_parts = candidate_parts
        accepted_count += len(indices)

    consider(changed)
    recovered = _assemble_markdown_parts(parts, accepted_parts)
    try:
        _validate_mode_output(
            source,
            recovered,
            mode,
            max_characters=MAX_DOCUMENT_OUTPUT_CHARACTERS,
        )
    except ImprovementError:
        return source, 0
    return recovered, accepted_count


def _improve_part(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    markdown: str,
    *,
    preserve_original_on_failure: bool,
    mode: ImprovementMode,
    translation_context: _TranslationContext | None,
    allow_paragraph_fallback: bool = True,
    cancellation: CancellationToken | None = None,
) -> str:
    if mode is ImprovementMode.REVIEW_STRUCTURE:
        try:
            return _improve_structure_with_directives(
                client,
                model,
                context_window,
                markdown,
                cancellation,
            )
        except ImprovementError as exc:
            if not preserve_original_on_failure:
                raise
            LOGGER.warning(
                "improvement_chunk_preserved structure_directives_unavailable=true reason=%s",
                str(exc),
            )
            return markdown

    active_translation_context = translation_context
    if translation_context is not None:
        part_source_language = detect_language_code(markdown)
        title_dense_fragment = _is_title_dense_translation_fragment(markdown)
        document_source_evidence = (
            translation_context.source_language is not None
            and part_source_language != translation_context.source_language
            and _fragment_has_language_evidence(
                markdown,
                translation_context.source_language,
            )
        )
        if (
            translation_context.preserve_paragraphs
            and part_source_language is not None
            and part_source_language == translation_context.target_language
            and (
                translation_context.source_language
                in {
                    None,
                    translation_context.target_language,
                }
                or (not title_dense_fragment and not document_source_evidence)
            )
        ):
            return markdown
        if part_source_language is not None and (
            translation_context.source_language is None
            or part_source_language == translation_context.source_language
            or (not title_dense_fragment and not document_source_evidence)
        ):
            active_translation_context = _TranslationContext(
                source_language=part_source_language,
                target_language=translation_context.target_language,
                preserve_paragraphs=translation_context.preserve_paragraphs,
                preserved_segments=translation_context.preserved_segments,
            )

    stripped_markdown = markdown.strip()
    is_unmarked_title = is_unmarked_title_line(stripped_markdown)
    is_focused_title = active_translation_context is not None and (
        is_unmarked_title
        or ("\n" not in stripped_markdown and ATX_HEADING_PATTERN.match(stripped_markdown))
    )
    request_markdown = markdown
    heading_prefix = ""
    heading_suffix = ""
    structural_prefix = ""
    structural_suffix = ""
    if is_focused_title and ATX_HEADING_PATTERN.match(stripped_markdown):
        heading_match = re.fullmatch(
            r"([ \t]{0,3}#{1,6}[ \t]+)([^\r\n]+?)([ \t]*)(\r?\n?)",
            markdown,
        )
        if heading_match is not None:
            heading_prefix = heading_match.group(1)
            request_markdown = heading_match.group(2)
            heading_suffix = f"{heading_match.group(3)}{heading_match.group(4)}"
    elif active_translation_context is not None:
        structural_match = re.fullmatch(
            r"(?P<prefix>[ \t]*(?:(?:>[ \t]*)+)?"
            r"(?:(?:[-+*]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?|(?:>[ \t]*)+))"
            r"(?P<body>\S[^\r\n]*?)(?P<suffix>[ \t]*\r?\n?)",
            markdown,
        )
        if structural_match is not None:
            structural_prefix = structural_match.group("prefix")
            request_markdown = structural_match.group("body")
            structural_suffix = structural_match.group("suffix")

    def restore_markdown_envelope(value: str) -> str:
        restored_value = _restore_protected_values(value, protected.values)
        prefix = heading_prefix or structural_prefix
        suffix = heading_suffix or structural_suffix
        if not prefix:
            return restored_value
        visible = restored_value.strip()
        if heading_prefix:
            visible = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", visible)
        if "\n" in visible:
            raise ImprovementError("El modelo no devolvió el fragmento en una sola línea.")
        return f"{prefix}{visible}{suffix}"

    if active_translation_context is not None:
        protected = _protect_translation_values(
            request_markdown,
            protect_numbers=not (is_unmarked_title or heading_prefix),
            protect_headings=False,
        )
    elif mode is ImprovementMode.REVIEW_CONTENT:
        protected = _protect_translation_values(
            markdown,
            protect_numbers=False,
            protect_paragraphs=False,
        )
    else:
        protected = _ProtectedMarkdown(markdown, ())
    request_instructions = instructions
    if is_focused_title:
        request_instructions = f"{request_instructions}\n{FOCUSED_TITLE_INSTRUCTION}"
    if protected.values:
        marker_count = sum(value.expected_count for value in protected.values)
        request_instructions = (
            f"{request_instructions}\n"
            f"{PROTECTED_VALUES_INSTRUCTION.format(marker_count=marker_count)}"
        )
    content = _request_improvement(
        client,
        model,
        context_window,
        request_instructions,
        protected.text,
        cancellation,
        prediction_characters=len(markdown),
    )
    try:
        content = restore_markdown_envelope(content)
        if active_translation_context is not None:
            content = _repair_untranslated_titles(
                client,
                model,
                context_window,
                instructions,
                markdown,
                content,
                active_translation_context,
                cancellation,
            )
        content = _prepare_and_validate_response(
            markdown,
            content,
            active_translation_context,
            mode,
        )
    except ImprovementError as exc:
        retryable_errors = (
            "encabezados",
            "números o fechas",
            "números romanos",
            "destinos de enlaces",
            "marcadores internos",
        )
        review_mode = mode in {
            ImprovementMode.REVIEW_CONTENT,
            ImprovementMode.REVIEW_STRUCTURE,
        }
        if (
            active_translation_context is None
            and not review_mode
            and not any(error in str(exc) for error in retryable_errors)
        ):
            raise
        specialized_retry = _specialized_retry_instruction(exc)
        retry_instruction = (
            VALIDATION_RETRY_INSTRUCTION
            if specialized_retry == VALIDATION_RETRY_INSTRUCTION
            else f"{VALIDATION_RETRY_INSTRUCTION}\n{specialized_retry}"
        )
        retry_content = _request_improvement(
            client,
            model,
            context_window,
            f"{request_instructions}\n{retry_instruction}",
            protected.text,
            cancellation,
            prediction_characters=len(markdown),
        )
        try:
            retry_content = restore_markdown_envelope(retry_content)
            if active_translation_context is not None:
                retry_content = _repair_untranslated_titles(
                    client,
                    model,
                    context_window,
                    instructions,
                    markdown,
                    retry_content,
                    active_translation_context,
                    cancellation,
                )
            content = _prepare_and_validate_response(
                markdown,
                retry_content,
                active_translation_context,
                mode,
            )
        except ImprovementError as retry_error:
            fallback_reasons = (
                "valor protegido",
                "separador de párrafo",
                "idioma solicitado",
                "título",
                "encabezado",
                "números o fechas",
                "números romanos",
                "destinos de enlaces",
                "comentarios HTML",
                "marcadores internos",
                "estructura de una tabla",
                "estructura de las listas",
                "estructura de las citas",
                "imágenes Markdown",
            )
            if (
                mode is ImprovementMode.REVIEW_CONTENT
                and allow_paragraph_fallback
                and any(reason in str(retry_error) for reason in fallback_reasons)
            ):
                LOGGER.warning("review_chunk_segment_fallback validation_failed_after_retry=true")
                try:
                    return _improve_review_content_segments(
                        client,
                        model,
                        context_window,
                        instructions,
                        markdown,
                        cancellation,
                    )
                except ImprovementError:
                    pass
            if (
                not preserve_original_on_failure
                and active_translation_context is not None
                and allow_paragraph_fallback
                and any(reason in str(retry_error) for reason in fallback_reasons)
            ):
                LOGGER.warning(
                    "improvement_chunk_segment_fallback validation_failed_after_retry=true"
                )
                try:
                    return _improve_translation_segments(
                        client,
                        model,
                        context_window,
                        instructions,
                        markdown,
                        active_translation_context,
                        cancellation,
                    )
                except ImprovementError:
                    pass
            if not preserve_original_on_failure:
                raise retry_error
            LOGGER.warning(
                "improvement_chunk_preserved validation_failed_after_retry=true reason=%s",
                str(retry_error),
            )
            return markdown
    return content


def _is_title_dense_translation_fragment(markdown: str) -> bool:
    visible_lines = [
        line.strip()
        for line in markdown.splitlines()
        if natural_language_text(line) and not HTML_COMMENT_PATTERN.fullmatch(line.strip())
    ]
    if len(visible_lines) < 2:
        return False
    title_lines = sum(
        is_unmarked_title_line(line) or ATX_HEADING_PATTERN.match(line) is not None
        for line in visible_lines
    )
    return title_lines * 2 >= len(visible_lines)


def _fragment_has_language_evidence(markdown: str, language: str) -> bool:
    """Find a substantial clause in the document's known source language."""

    natural_text = natural_language_text(markdown)
    clauses = re.split(r"(?<=[.!?;:)])\s+|,\s+", natural_text)
    for clause in clauses:
        if sum(character.isalpha() for character in clause) < 40:
            continue
        detected = detect_language_code(
            clause,
            minimum_letters=40,
            minimum_confidence=0.80,
        )
        if detected == language:
            return True
    return False


def _specialized_retry_instruction(error: ImprovementError) -> str:
    reason = str(error).casefold()
    if "una sola línea" in reason:
        return SINGLE_LINE_RETRY_INSTRUCTION
    if any(
        marker in reason
        for marker in (
            "omitido parte del contenido",
            "duplicado o añadido contenido",
        )
    ):
        return COVERAGE_RETRY_INSTRUCTION
    if any(marker in reason for marker in ("certeza", "ambigüedad", "polaridad")):
        return SEMANTIC_RETRY_INSTRUCTION
    if any(
        marker in reason
        for marker in (
            "idioma solicitado",
            "texto de origen",
            "título",
        )
    ):
        return LANGUAGE_RETRY_INSTRUCTION
    if any(
        marker in reason
        for marker in (
            "estructura de una tabla",
            "estructura de las listas",
            "estructura de las citas",
            "encabezados",
            "imágenes markdown",
            "separador de párrafo",
        )
    ):
        return MARKDOWN_RETRY_INSTRUCTION
    if any(
        marker in reason
        for marker in (
            "valor protegido",
            "números o fechas",
            "números romanos",
            "destinos de enlaces",
            "comentarios html",
            "marcadores internos",
        )
    ):
        return FIDELITY_RETRY_INSTRUCTION
    return VALIDATION_RETRY_INSTRUCTION


def _improve_structure_with_directives(
    client: httpx.Client,
    model: str,
    context_window: int,
    markdown: str,
    cancellation: CancellationToken | None,
) -> str:
    lines = markdown.splitlines(keepends=True)
    candidate_lines = {
        index for index, line in enumerate(lines, start=1) if _is_structure_heading_candidate(line)
    }
    if not candidate_lines:
        raise ImprovementError("El fragmento no contiene candidatos de encabezado seguros.")

    numbered_lines: list[str] = []
    for index, line in enumerate(lines, start=1):
        content = line.rstrip("\r\n")
        candidate = " CANDIDATA" if index in candidate_lines else ""
        numbered_lines.append(f"PZL{index}{candidate}: {content}")
    response = _request_improvement(
        client,
        model,
        context_window,
        STRUCTURE_DIRECTIVE_INSTRUCTIONS,
        "\n".join(numbered_lines),
        cancellation,
        prediction_characters=256,
    )
    directives: dict[int, int] = {}
    for response_line in response.splitlines():
        stripped = response_line.strip()
        if not stripped:
            continue
        match = re.fullmatch(r"PZL(\d+)=(\d)", stripped)
        if match is None:
            raise ImprovementError("El modelo devolvió directivas estructurales incompatibles.")
        line_number = int(match.group(1))
        level = int(match.group(2))
        if line_number not in candidate_lines or not 1 <= level <= 6 or line_number in directives:
            raise ImprovementError("El modelo devolvió una directiva estructural no válida.")
        if not _is_safe_structure_level(lines[line_number - 1], level):
            raise ImprovementError("El modelo propuso un nivel de encabezado incoherente.")
        directives[line_number] = level
    if not directives or len(directives) > MAX_STRUCTURE_DIRECTIVES_PER_CHUNK:
        raise ImprovementError("El modelo no devolvió directivas estructurales utilizables.")

    structured = list(lines)
    for line_number, level in directives.items():
        source_line = lines[line_number - 1]
        ending_start = len(source_line.rstrip("\r\n"))
        visible = source_line[:ending_start]
        ending = source_line[ending_start:]
        indent = visible[: len(visible) - len(visible.lstrip())]
        exact_words = re.sub(r"^#{1,6}[ \t]+", "", visible.lstrip()).strip()
        structured[line_number - 1] = f"{indent}{'#' * level} {exact_words}{ending}"
    candidate = "".join(structured)
    return _prepare_and_validate_response(
        markdown,
        candidate,
        None,
        ImprovementMode.REVIEW_STRUCTURE,
    )


def _is_safe_structure_level(source_line: str, proposed_level: int) -> bool:
    existing = ATX_HEADING_PATTERN.match(source_line.strip())
    if existing is None:
        return proposed_level <= 3
    return abs(proposed_level - len(existing.group(1))) <= 1


def _is_structure_heading_candidate(line: str) -> bool:
    stripped = line.strip()
    if (
        not stripped
        or HTML_COMMENT_PATTERN.fullmatch(stripped)
        or FENCE_PATTERN.match(stripped)
        or TABLE_DIVIDER_PATTERN.fullmatch(stripped)
        or stripped.startswith((">", "![", "["))
        or re.match(r"(?:[-+*]|\d+[.)])[ \t]+", stripped)
        or "|" in stripped
        or RAW_URL_PATTERN.search(stripped)
    ):
        return False
    visible = re.sub(r"^#{1,6}[ \t]+", "", stripped).strip()
    natural = natural_language_text(visible)
    words = natural.split()
    return bool(
        2 <= sum(character.isalpha() for character in natural)
        and len(visible) <= 160
        and len(words) <= 18
        and (ATX_HEADING_PATTERN.match(stripped) or not visible.endswith((".", "?", "!", ";")))
    )


def _improve_review_content_segments(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    markdown: str,
    cancellation: CancellationToken | None,
) -> str:
    parts = _translation_fallback_parts(markdown)
    if len(parts) <= 1:
        raise ImprovementError("El fragmento de corrección no admite una división segura.")

    repaired: list[str] = []
    for part in parts:
        if not part.should_improve or not natural_language_text(part.text):
            repaired.append(part.text)
            continue
        repaired.append(
            _improve_part(
                client,
                model,
                context_window,
                instructions,
                part.text,
                preserve_original_on_failure=True,
                mode=ImprovementMode.REVIEW_CONTENT,
                translation_context=None,
                allow_paragraph_fallback=False,
                cancellation=cancellation,
            )
        )
    combined = "".join(repaired)
    return _prepare_and_validate_response(
        markdown,
        combined,
        None,
        ImprovementMode.REVIEW_CONTENT,
    )


def _improve_translation_segments(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    markdown: str,
    context: _TranslationContext,
    cancellation: CancellationToken | None,
) -> str:
    parts = _translation_fallback_parts(markdown)
    preserved_segments_before = len(context.preserved_segments)
    if sum(part.should_improve for part in parts) <= 1:
        return _improve_translation_with_locked_values(
            client,
            model,
            context_window,
            instructions,
            markdown,
            context,
            cancellation,
        )

    repaired: list[str] = []
    for part in parts:
        if not part.should_improve:
            repaired.append(part.text)
            continue
        try:
            improved_part = _improve_part(
                client,
                model,
                context_window,
                instructions,
                part.text,
                preserve_original_on_failure=False,
                mode=ImprovementMode.TRANSLATE,
                translation_context=context,
                allow_paragraph_fallback=False,
                cancellation=cancellation,
            )
        except ImprovementError as exc:
            try:
                locked_value_reasons = (
                    "valor protegido",
                    "marcadores internos",
                    "números o fechas",
                    "números romanos",
                    "destinos de enlaces",
                    "código en línea",
                )
                if not any(reason in str(exc) for reason in locked_value_reasons):
                    raise exc
                improved_part = _improve_translation_with_locked_values(
                    client,
                    model,
                    context_window,
                    instructions,
                    part.text,
                    context,
                    cancellation,
                )
            except ImprovementError as segment_error:
                LOGGER.warning(
                    "translation_segment_preserved validation_failed_after_retry=true reason=%s",
                    str(segment_error),
                )
                context.preserved_segments.append(part.text)
                improved_part = part.text
        repaired.append(improved_part)
    combined = "".join(repaired)
    return _prepare_and_validate_response(
        markdown,
        combined,
        None if len(context.preserved_segments) > preserved_segments_before else context,
        ImprovementMode.TRANSLATE,
    )


def _translation_fallback_parts(markdown: str) -> list[_MarkdownPart]:
    paragraph_pieces = re.split(r"(\n{2,})", markdown)
    paragraph_parts = [
        _MarkdownPart(piece, not bool(re.fullmatch(r"\n{2,}", piece)))
        for piece in paragraph_pieces
        if piece
    ]
    if sum(part.should_improve for part in paragraph_parts) > 1:
        return paragraph_parts

    line_pieces = re.split(r"(\r?\n)", markdown)
    line_parts = [
        _MarkdownPart(piece, "\n" not in piece and bool(natural_language_text(piece)))
        for piece in line_pieces
        if piece
    ]
    if sum(part.should_improve for part in line_parts) > 1:
        return line_parts

    boundaries = [
        match
        for match in re.finditer(r"(?<=[.!?…])(?:[ \t]+|\r?\n+)", markdown)
        if _is_safe_markdown_boundary(markdown, match.start())
    ]
    if not boundaries:
        return [_MarkdownPart(markdown, True)]

    parts: list[_MarkdownPart] = []
    previous = 0
    for boundary in boundaries:
        if boundary.start() > previous:
            parts.append(_MarkdownPart(markdown[previous : boundary.start()], True))
        parts.append(_MarkdownPart(markdown[boundary.start() : boundary.end()], False))
        previous = boundary.end()
    if previous < len(markdown):
        parts.append(_MarkdownPart(markdown[previous:], True))
    return parts


def _improve_translation_with_locked_values(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    markdown: str,
    context: _TranslationContext,
    cancellation: CancellationToken | None,
) -> str:
    protected = _protect_translation_values(markdown)
    source_letters = sum(character.isalpha() for character in natural_language_text(markdown))
    if not protected.values or source_letters < 12:
        raise ImprovementError("No hay valores protegidos que permitan segmentar el fragmento.")

    tokens = {value.token for value in protected.values}
    token_pattern = re.compile(
        f"({'|'.join(re.escape(token) for token in sorted(tokens, key=len, reverse=True))})"
    )
    repaired: list[str] = []
    translated_fragments = 0
    for fragment in token_pattern.split(protected.text):
        if not fragment:
            continue
        if fragment in tokens or not natural_language_text(fragment):
            repaired.append(fragment)
            continue
        leading_length = len(fragment) - len(fragment.lstrip())
        trailing_length = len(fragment) - len(fragment.rstrip())
        trailing_start = len(fragment) - trailing_length if trailing_length else len(fragment)
        core = fragment[leading_length:trailing_start]
        if not core:
            repaired.append(fragment)
            continue
        translated = _request_improvement(
            client,
            model,
            context_window,
            instructions,
            core,
            cancellation,
            prediction_characters=len(core),
        ).strip()
        repaired.append(f"{fragment[:leading_length]}{translated}{fragment[trailing_start:]}")
        translated_fragments += 1

    if translated_fragments == 0:
        raise ImprovementError("No hay texto natural que traducir en el fragmento protegido.")
    restored = _restore_protected_values("".join(repaired), protected.values)
    restored_letters = sum(character.isalpha() for character in natural_language_text(restored))
    if restored_letters / source_letters < 0.55 or restored_letters / source_letters > 1.80:
        raise ImprovementError("La traducción segmentada no conserva suficiente contenido.")
    return _prepare_and_validate_response(
        markdown,
        restored,
        context,
        ImprovementMode.TRANSLATE,
    )


def _repair_untranslated_titles(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    source: str,
    translated: str,
    context: _TranslationContext,
    cancellation: CancellationToken | None,
) -> str:
    untranslated = find_untranslated_title_lines(
        source,
        translated,
        context.source_language,
    )
    partial = find_titles_with_source_language_residue(
        source,
        translated,
        context.source_language,
    )
    repairs: list[tuple[str, str]] = []
    seen_current_titles: set[str] = set()
    for title in untranslated:
        repairs.append((title, title))
        seen_current_titles.add(title)
    for source_title, current_title in partial:
        if current_title in seen_current_titles:
            continue
        repairs.append((source_title, current_title))
        seen_current_titles.add(current_title)
    if not repairs:
        return translated
    if len(repairs) > MAX_FOCUSED_TITLE_REPAIRS_PER_CHUNK:
        raise ImprovementError("La traducción dejó demasiados títulos sin traducir.")

    repaired = translated
    for title, current_title in repairs:
        heading_match = re.fullmatch(
            r"(?P<prefix>[ \t]{0,3}#{1,6}[ \t]+)(?P<body>[^\r\n]+)",
            title,
        )
        heading_prefix = heading_match.group("prefix") if heading_match is not None else ""
        visible_title = heading_match.group("body") if heading_match is not None else title
        protected = _protect_translation_values(
            visible_title,
            protect_numbers=False,
            protect_headings=False,
        )
        marker_count = sum(value.expected_count for value in protected.values)
        focused_instructions = f"{instructions}\n{FOCUSED_TITLE_INSTRUCTION}"
        if marker_count:
            focused_instructions = (
                f"{focused_instructions}\n"
                f"{PROTECTED_VALUES_INSTRUCTION.format(marker_count=marker_count)}"
            )
        focused = _request_improvement(
            client,
            model,
            context_window,
            focused_instructions,
            protected.text,
            cancellation,
            prediction_characters=len(visible_title),
        )
        focused = _restore_protected_values(focused, protected.values).strip()
        if "\n" in focused:
            raise ImprovementError("El modelo no devolvió el título en una sola línea.")
        focused = f"{heading_prefix}{focused}"
        focused = _prepare_and_validate_response(
            title,
            focused,
            context,
            ImprovementMode.TRANSLATE,
        )
        repaired = _replace_exact_title_lines(repaired, current_title, focused)
    return repaired


def _replace_exact_title_lines(markdown: str, source_title: str, translated_title: str) -> str:
    lines = markdown.splitlines(keepends=True)
    replacements = 0
    for index, line in enumerate(lines):
        line_ending = "\n" if line.endswith("\n") else ""
        content = line[:-1] if line_ending else line
        if content.endswith("\r"):
            content = content[:-1]
            line_ending = f"\r{line_ending}"
        if content.strip() != source_title:
            continue
        leading = content[: len(content) - len(content.lstrip())]
        trailing = content[len(content.rstrip()) :]
        lines[index] = f"{leading}{translated_title}{trailing}{line_ending}"
        replacements += 1
    if replacements == 0:
        raise ImprovementError("No se pudo localizar un título pendiente de traducir.")
    return "".join(lines)


def _prepare_and_validate_response(
    source: str,
    response: str,
    translation_context: _TranslationContext | None,
    mode: ImprovementMode,
) -> str:
    if not response.strip():
        raise ImprovementError("El modelo devolvió una respuesta vacía.")
    if "\0" in response:
        raise ImprovementError("El modelo devolvió caracteres no válidos.")
    if len(response) > MAX_OUTPUT_CHARACTERS:
        raise ImprovementError("La respuesta del modelo supera el tamaño permitido.")

    response = _remove_added_instruction_placeholders(source, response)
    if translation_context is not None:
        response = _restore_single_line_translation_layout(source, response)
    restored = response
    restored = _repair_common_markdown_spacing(restored)
    if mode is not ImprovementMode.REVIEW_STRUCTURE:
        restored = _restore_heading_levels(source, restored)
    try:
        _validate_mode_output(source, restored, mode)
    except ImprovementError:
        if mode is not ImprovementMode.REVIEW_STRUCTURE:
            raise
        reconciled = _reconcile_structure_response(source, restored)
        if reconciled is None:
            raise
        _validate_mode_output(source, reconciled, mode)
        restored = reconciled
    if translation_context is not None:
        _validate_translation(source, restored, translation_context)
    return restored


def _reconcile_structure_response(source: str, proposed: str) -> str | None:
    """Keep source wording while adopting safely aligned heading changes."""
    source_lines = source.splitlines(keepends=True)
    proposed_lines = proposed.splitlines()
    source_positions = [index for index, line in enumerate(source_lines) if line.strip()]
    proposed_nonempty = [line for line in proposed_lines if line.strip()]
    if len(source_positions) != len(proposed_nonempty):
        return _reconcile_fuzzy_structure_headings(source_lines, proposed_lines)

    heading_pattern = re.compile(r"^([ \t]{0,3})(#{1,6})[ \t]+")
    reconciled = list(source_lines)
    changed_headings = 0
    for source_index, proposed_line in zip(
        source_positions,
        proposed_nonempty,
        strict=True,
    ):
        source_line = source_lines[source_index].rstrip("\r\n")
        source_visible = natural_language_text(source_line)
        proposed_visible = natural_language_text(proposed_line)
        if source_visible and proposed_visible:
            similarity = SequenceMatcher(
                None,
                source_visible.casefold(),
                proposed_visible.casefold(),
                autojunk=False,
            ).ratio()
            if similarity < 0.60:
                return _reconcile_fuzzy_structure_headings(source_lines, proposed_lines)

        source_heading = heading_pattern.match(source_line)
        proposed_heading = heading_pattern.match(proposed_line)
        if proposed_heading is not None:
            source_body = (
                source_line[source_heading.end() :]
                if source_heading is not None
                else source_line.lstrip()
            )
            replacement = f"{proposed_heading.group(1)}{proposed_heading.group(2)} {source_body}"
        elif source_heading is not None:
            replacement = source_line[source_heading.end() :]
        else:
            continue
        if replacement == source_line:
            continue
        line_ending = source_lines[source_index][len(source_line) :]
        reconciled[source_index] = f"{replacement}{line_ending}"
        changed_headings += 1

    if changed_headings:
        return "".join(reconciled)
    return _reconcile_fuzzy_structure_headings(source_lines, proposed_lines)


def _reconcile_fuzzy_structure_headings(
    source_lines: list[str],
    proposed_lines: list[str],
) -> str | None:
    """Map only unambiguous proposed headings back onto exact source lines."""
    heading_pattern = re.compile(r"^([ \t]{0,3})(#{1,6})[ \t]+")
    source_candidates = [
        (index, natural_language_text(line))
        for index, line in enumerate(source_lines)
        if natural_language_text(line)
    ]
    reconciled = list(source_lines)
    used_source_indices: set[int] = set()
    changed_headings = 0
    for proposed_line in proposed_lines:
        proposed_heading = heading_pattern.match(proposed_line)
        proposed_visible = natural_language_text(proposed_line)
        if proposed_heading is None or not proposed_visible:
            continue
        ranked = sorted(
            (
                (
                    SequenceMatcher(
                        None,
                        source_visible.casefold(),
                        proposed_visible.casefold(),
                        autojunk=False,
                    ).ratio(),
                    source_index,
                )
                for source_index, source_visible in source_candidates
                if source_index not in used_source_indices
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.60:
            continue
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.10:
            continue
        source_index = ranked[0][1]
        source_line = source_lines[source_index].rstrip("\r\n")
        source_heading = heading_pattern.match(source_line)
        source_body = (
            source_line[source_heading.end() :]
            if source_heading is not None
            else source_line.lstrip()
        )
        replacement = f"{proposed_heading.group(1)}{proposed_heading.group(2)} {source_body}"
        if replacement == source_line:
            used_source_indices.add(source_index)
            continue
        line_ending = source_lines[source_index][len(source_line) :]
        reconciled[source_index] = f"{replacement}{line_ending}"
        used_source_indices.add(source_index)
        changed_headings += 1
    return "".join(reconciled) if changed_headings else None


def _restore_single_line_translation_layout(source: str, translated: str) -> str:
    source_letters = sum(character.isalpha() for character in natural_language_text(source))
    if source_letters < 80 or "\n" in source.strip() or "\n" not in translated:
        return translated
    return re.sub(r"[ \t]*\r?\n[ \t]*", " ", translated).strip()


def _remove_added_instruction_placeholders(source: str, response: str) -> str:
    def remove_excess(pattern: re.Pattern[str], value: str) -> str:
        allowed = len(pattern.findall(source))
        seen = 0

        def preserve_existing(match: re.Match[str]) -> str:
            nonlocal seen
            seen += 1
            return match.group(0) if seen <= allowed else ""

        return pattern.sub(preserve_existing, value)

    comment_pattern = re.compile(re.escape(INSTRUCTION_PLACEHOLDER_COMMENT))
    without_extra_comments = remove_excess(comment_pattern, response)
    return remove_excess(_INSTRUCTION_PLACEHOLDER_TAG_PATTERN, without_extra_comments)


def _request_improvement(
    client: httpx.Client,
    model: str,
    context_window: int,
    instructions: str,
    markdown: str,
    cancellation: CancellationToken | None,
    *,
    prediction_characters: int | None = None,
) -> str:
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": markdown},
        ],
        "stream": True,
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": context_window,
            "num_predict": _prediction_token_limit(
                prediction_characters if prediction_characters is not None else len(markdown),
                context_window,
            ),
            "seed": 0,
        },
    }
    check_cancelled(cancellation)
    with client.stream(
        "POST",
        f"{OLLAMA_BASE_URL}/api/chat",
        json=request_payload,
    ) as response:
        if response.is_redirect:
            raise ImprovementError("Ollama intentó redirigir la solicitud y fue bloqueado.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ImprovementError(
                f"El modelo local respondió con el estado HTTP {response.status_code}."
            ) from exc

        content_parts: list[str] = []
        content_length = 0
        saw_message = False
        for line in response.iter_lines():
            check_cancelled(cancellation)
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                message = payload["message"]
                content = message["content"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ImprovementError("Ollama devolvió una respuesta incompatible.") from exc
            if not isinstance(content, str):
                raise ImprovementError("Ollama devolvió una respuesta incompatible.")
            saw_message = True
            content_length += len(content)
            if content_length > MAX_OUTPUT_CHARACTERS:
                raise ImprovementError("La respuesta del modelo supera el tamaño permitido.")
            content_parts.append(content)
        check_cancelled(cancellation)
    if not saw_message:
        raise ImprovementError("Ollama devolvió una respuesta incompatible.")
    return "".join(content_parts)


def _prediction_token_limit(source_characters: int, context_window: int) -> int:
    """Bound runaway generations while leaving ample room for Latin-script output."""
    proportional_limit = (source_characters + 1) // 2 + 256
    context_limit = min(max(context_window // 2, 256), 4_096)
    return max(256, min(proportional_limit, context_limit))


def _protect_translation_values(
    markdown: str,
    *,
    protect_numbers: bool = True,
    protect_headings: bool = True,
    protect_paragraphs: bool = True,
) -> _ProtectedMarkdown:
    spans = (
        [(match.start(), match.end(), False) for match in NUMBER_PATTERN.finditer(markdown)]
        if protect_numbers
        else []
    )
    spans.extend(
        (match.start(), match.end(), False) for match in INLINE_CODE_PATTERN.finditer(markdown)
    )
    spans.extend((start, end, False) for start, end in _title_roman_reference_spans(markdown))
    spans.extend((start, end, False) for start, end, _value in link_destination_spans(markdown))
    spans.extend(
        (match.start(), match.end(), False) for match in RAW_URL_PATTERN.finditer(markdown)
    )
    spans.extend(
        (match.start(), match.end(), False) for match in HTML_COMMENT_PATTERN.finditer(markdown)
    )
    if protect_headings:
        spans.extend(
            (match.start(1), match.end(1), False)
            for match in ATX_HEADING_PATTERN.finditer(markdown)
        )
    if protect_paragraphs:
        spans.extend(
            (match.start(), match.end(), True) for match in re.finditer(r"\n{2,}", markdown)
        )

    non_overlapping: list[tuple[int, int, bool]] = []
    for start, end, is_paragraph in sorted(
        spans,
        key=lambda span: (span[0], -(span[1] - span[0])),
    ):
        if non_overlapping and start < non_overlapping[-1][1]:
            continue
        non_overlapping.append((start, end, is_paragraph))

    prefix = "PZDOC"
    while prefix in markdown:
        prefix = f"Z{prefix}"
    protected = markdown
    values: list[_ProtectedValue] = []
    for index, (start, end, is_paragraph) in reversed(list(enumerate(non_overlapping))):
        value = markdown[start:end]
        if is_paragraph:
            marker = f"{prefix}P{_alphabetic_index(index)}XZQ"
            token = f"<!-- {marker} -->"
            values.append(_ProtectedValue(token, value, paragraph=True))
            replacement = f"\n{token}\n"
        else:
            token = f"{prefix}{_alphabetic_index(index)}XZQ"
            values.append(_ProtectedValue(token, value))
            replacement = token
        protected = protected[:start] + replacement + protected[end:]
    values.reverse()
    return _ProtectedMarkdown(protected, tuple(values))


def _restore_protected_values(
    response: str,
    values: tuple[_ProtectedValue, ...],
) -> str:
    restored = _repair_unambiguous_protected_token_typos(response, values)
    for protected_value in values:
        if restored.count(protected_value.token) != protected_value.expected_count:
            raise ImprovementError("El modelo cambió u omitió un valor protegido del documento.")
        if protected_value.paragraph:
            paragraph_pattern = re.compile(
                rf"[ \t]*\r?\n[ \t]*{re.escape(protected_value.token)}[ \t]*\r?\n"
            )
            restored, replacements = paragraph_pattern.subn(
                protected_value.value,
                restored,
            )
            if replacements != protected_value.expected_count:
                raise ImprovementError(
                    "El modelo cambió la posición de un separador de párrafo protegido."
                )
        else:
            wrapped_pattern = re.compile(rf"<!--[ \t]*{re.escape(protected_value.token)}[ \t]*-->")
            restored, wrapped_replacements = wrapped_pattern.subn(
                protected_value.value,
                restored,
            )
            if wrapped_replacements == 0:
                restored = restored.replace(protected_value.token, protected_value.value)
    return restored


def _repair_unambiguous_protected_token_typos(
    response: str,
    values: tuple[_ProtectedValue, ...],
) -> str:
    """Repair one substituted character in an otherwise unique internal marker.

    Small local models occasionally copy ``PZDOC...XZQ`` as ``PZDOC...XZT`` while translating
    the surrounding prose correctly. The prefix is chosen so it cannot occur in the source, so a
    same-length, one-character candidate can be restored safely only when it maps one-to-one to a
    single missing marker. Missing, duplicated or ambiguous markers remain hard failures.
    """

    expected_tokens = {value.token for value in values}
    missing = [
        value.token for value in values if response.count(value.token) < value.expected_count
    ]
    if not missing:
        return response
    candidates = {
        match.group(0)
        for match in re.finditer(
            r"(?<![A-Za-z0-9_-])[A-Z][A-Z0-9_-]{7,}(?![A-Za-z0-9_-])",
            response,
        )
        if match.group(0) not in expected_tokens
    }
    mappings: dict[str, str] = {}
    for candidate in candidates:
        matches = [
            token
            for token in missing
            if len(candidate) == len(token)
            and sum(left != right for left, right in zip(candidate, token, strict=True)) == 1
        ]
        if len(matches) == 1:
            mappings[candidate] = matches[0]
    if not mappings:
        return response
    mapped_targets = Counter(mappings.values())
    repaired = response
    repaired_count = 0
    for candidate, target in mappings.items():
        if mapped_targets[target] != 1 or repaired.count(candidate) != 1:
            continue
        repaired = repaired.replace(candidate, target)
        repaired_count += 1
    if repaired_count:
        LOGGER.info("protected_marker_typos_repaired count=%d", repaired_count)
    return repaired


def _alphabetic_index(index: int) -> str:
    letters: list[str] = []
    value = index
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    return "".join(reversed(letters))


def _validate_input(markdown: str) -> None:
    if not markdown.strip():
        raise ImprovementError("El documento no contiene texto que mejorar.")
    if "\0" in markdown:
        raise ImprovementError("El documento contiene caracteres no válidos para el modelo.")
    if len(markdown) > MAX_DOCUMENT_CHARACTERS:
        raise ImprovementError(
            f"El documento supera el límite de {MAX_DOCUMENT_CHARACTERS:,} caracteres."
        )


def _plan_markdown_parts(
    markdown: str,
    *,
    max_characters: int = MAX_INPUT_CHARACTERS,
    protect_paragraphs: bool = False,
) -> list[_MarkdownPart]:
    parts: list[_MarkdownPart] = []
    pending_blocks: list[str] = []
    pending_length = 0
    pending_conserved_values = 0
    source_language = detect_language_code(markdown) if protect_paragraphs else None
    uppercase_person_names = (
        probable_uppercase_person_name_bases(markdown, source_language)
        if protect_paragraphs
        else frozenset()
    )

    def flush_pending() -> None:
        nonlocal pending_conserved_values, pending_length
        if pending_blocks:
            parts.append(
                _MarkdownPart(
                    "\n\n".join(pending_blocks),
                    True,
                    "\n\n" if parts else "",
                )
            )
            pending_blocks.clear()
            pending_length = 0
            pending_conserved_values = 0

    for block in _markdown_blocks(markdown):
        if _is_fenced_code_block(block):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if re.fullmatch(r"\s*<!--[\s\S]*?-->\s*", block):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if protect_paragraphs and is_probable_organization_name_line(block):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if (
            protect_paragraphs
            and uppercase_person_name_base(block, source_language) in uppercase_person_names
        ):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if protect_paragraphs and (
            is_unmarked_title_line(block) or ATX_HEADING_PATTERN.match(block)
        ):
            flush_pending()
            parts.append(_MarkdownPart(block, True, "\n\n" if parts else ""))
            continue
        conserved_values = _conserved_value_count(block)
        protected_value_limit = (
            MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
            if protect_paragraphs
            else MAX_CONSERVED_VALUES_PER_CHUNK
        )
        if len(block) > max_characters or conserved_values > protected_value_limit:
            flush_pending()
            for index, (chunk, separator) in enumerate(
                _split_dense_block(
                    block,
                    max_characters=max_characters,
                    max_conserved_values=protected_value_limit,
                )
            ):
                parts.append(
                    _MarkdownPart(
                        chunk,
                        True,
                        "\n\n" if index == 0 and parts else separator,
                    )
                )
            continue

        separator_length = 2 if pending_blocks else 0
        candidate_length = pending_length + separator_length + len(block)
        candidate_conserved_values = pending_conserved_values + conserved_values
        if pending_blocks and protect_paragraphs:
            candidate_conserved_values += 1
        if pending_blocks and (
            candidate_length > max_characters or candidate_conserved_values > protected_value_limit
        ):
            flush_pending()
            separator_length = 0
            candidate_conserved_values = conserved_values
        pending_blocks.append(block)
        pending_length += separator_length + len(block)
        pending_conserved_values = candidate_conserved_values

    flush_pending()
    return parts


def _split_dense_block(
    block: str,
    *,
    max_characters: int = MAX_INPUT_CHARACTERS,
    max_conserved_values: int = MAX_CONSERVED_VALUES_PER_CHUNK,
) -> list[tuple[str, str]]:
    if any(TABLE_DIVIDER_PATTERN.fullmatch(line) for line in block.splitlines()):
        raise ImprovementError(
            "Una tabla Markdown supera el tamaño seguro para el modelo y no puede dividirse."
        )

    chunks: list[tuple[str, str]] = []
    remaining = block
    separator_before = ""
    while (
        len(remaining) > max_characters or _conserved_value_count(remaining) > max_conserved_values
    ):
        cutoffs: list[int] = []
        if len(remaining) > max_characters:
            cutoffs.append(max_characters + 1)
        conserved_spans = _conserved_value_spans(remaining)
        if len(conserved_spans) > max_conserved_values:
            cutoffs.append(conserved_spans[max_conserved_values][0] + 1)
        window = remaining[: min(cutoffs)]
        boundary = _safe_split_boundary(
            window,
            minimum=max(min(len(window) // 3, max_characters // 3), 1),
        )
        if boundary is None:
            raise ImprovementError(
                "Un bloque Markdown individual supera el tamaño seguro para el modelo y no tiene "
                "un límite de frase o línea donde dividirlo."
            )
        start, end = boundary
        chunk = remaining[:start]
        if not chunk:
            raise ImprovementError("No se pudo dividir un bloque Markdown de forma segura.")
        chunks.append((chunk, separator_before))
        separator_before = remaining[start:end]
        remaining = remaining[end:]
    chunks.append((remaining, separator_before))
    return chunks


def _safe_split_boundary(
    window: str,
    *,
    minimum: int = MAX_INPUT_CHARACTERS // 3,
) -> tuple[int, int] | None:
    patterns = (r"\n+", r"(?<=[.!?…])\s+", r"\s+")
    for pattern in patterns:
        matches = [match for match in re.finditer(pattern, window) if match.start() >= minimum]
        for match in reversed(matches):
            if _is_safe_markdown_boundary(window, match.start()):
                return match.start(), match.end()
    return None


def _conserved_value_count(markdown: str) -> int:
    return len(_conserved_value_spans(markdown))


def _conserved_value_spans(markdown: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in NUMBER_PATTERN.finditer(markdown)]
    spans.extend(_title_roman_reference_spans(markdown))
    spans.extend((match.start(), match.end()) for match in INLINE_CODE_PATTERN.finditer(markdown))
    spans.extend((start, end) for start, end, _value in link_destination_spans(markdown))
    spans.extend((match.start(1), match.end(1)) for match in ATX_HEADING_PATTERN.finditer(markdown))
    return sorted(spans)


def _title_roman_reference_spans(markdown: str) -> list[tuple[int, int]]:
    return [
        (match.start(1), match.end(1)) for match in TITLE_ROMAN_REFERENCE_PATTERN.finditer(markdown)
    ]


def _title_roman_reference_values(markdown: str) -> Counter[str]:
    return Counter(markdown[start:end] for start, end in _title_roman_reference_spans(markdown))


def _is_safe_markdown_boundary(text: str, position: int) -> bool:
    prefix = text[:position]
    return (
        prefix.count("`") % 2 == 0
        and prefix.rfind("[") <= prefix.rfind("]")
        and prefix.rfind("(") <= prefix.rfind(")")
    )


def _markdown_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fence_character: str | None = None
    fence_length = 0

    for line in markdown.strip().splitlines():
        fence = FENCE_PATTERN.match(line)
        if fence is not None:
            marker = fence.group(1)
            if fence_character is None:
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0

        if not line.strip() and fence_character is None:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current))
    return blocks


def _is_fenced_code_block(block: str) -> bool:
    first_line = block.splitlines()[0] if block else ""
    return FENCE_PATTERN.match(first_line) is not None


def _repair_common_markdown_spacing(markdown: str) -> str:
    return re.sub(r"(?m)^(\s*(?:[-*>]\s*)?)\*\*[ \t]+(?=\S)", r"\1**", markdown)


def _restore_heading_levels(source: str, improved: str) -> str:
    source_matches = list(ATX_HEADING_PATTERN.finditer(source))
    improved_matches = list(ATX_HEADING_PATTERN.finditer(improved))
    if not source_matches:
        return ATX_HEADING_PATTERN.sub("", improved)
    if len(source_matches) != len(improved_matches):
        return improved

    restored: list[str] = []
    previous = 0
    for source_match, improved_match in zip(source_matches, improved_matches, strict=True):
        restored.append(improved[previous : improved_match.start(1)])
        restored.append(source_match.group(1))
        previous = improved_match.end(1)
    restored.append(improved[previous:])
    return "".join(restored)


def _validate_mode_output(
    source: str,
    improved: str,
    mode: ImprovementMode,
    *,
    max_characters: int = MAX_OUTPUT_CHARACTERS,
) -> None:
    if mode not in {ImprovementMode.REVIEW_CONTENT, ImprovementMode.REVIEW_STRUCTURE}:
        _validate_output(source, improved, max_characters=max_characters)
        return

    _validate_revision_common(source, improved, max_characters=max_characters)
    if mode is ImprovementMode.REVIEW_CONTENT:
        if markdown_heading_levels(source) != markdown_heading_levels(improved):
            raise ImprovementError("El modelo cambió la estructura de encabezados del documento.")
        if markdown_table_shapes(source) != markdown_table_shapes(improved):
            raise ImprovementError("El modelo cambió la estructura de una tabla del documento.")
        return

    if _structure_visible_text(source) != _structure_visible_text(improved):
        raise ImprovementError("La propuesta estructural cambió palabras o el orden del documento.")
    if markdown_table_shapes(source) != markdown_table_shapes(improved):
        raise ImprovementError("La propuesta estructural cambió una tabla del documento.")
    _validate_structure_heading_origins(source, improved)


def _validate_structure_heading_origins(source: str, improved: str) -> None:
    """Accept headings only when their full text comes from one safe source line."""
    source_headings = Counter(_markdown_heading_keys(source))
    source_candidates = Counter(
        key
        for line in source.splitlines()
        if _is_structure_heading_candidate(line)
        for key in [_structure_visible_text(line)]
        if key
    )
    improved_headings = Counter(_markdown_heading_keys(improved))
    for heading, count in improved_headings.items():
        if count > max(source_headings[heading], source_candidates[heading]):
            raise ImprovementError(
                "La propuesta estructural convirtió cuerpo de texto en un encabezado."
            )

    raw_heading_pattern = re.compile(r"<h[1-6](?:[ \t][^>]*)?>", re.IGNORECASE)
    source_raw_headings = Counter(raw_heading_pattern.findall(source))
    improved_raw_headings = Counter(raw_heading_pattern.findall(improved))
    if any(
        count > source_raw_headings[heading] for heading, count in improved_raw_headings.items()
    ):
        raise ImprovementError("La propuesta estructural añadió un encabezado HTML no verificable.")


def _markdown_heading_keys(markdown: str) -> list[str]:
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(markdown)
    headings: list[str] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or index + 1 >= len(tokens):
            continue
        inline = tokens[index + 1]
        if inline.type != "inline":
            continue
        visible = " ".join("".join(_inline_visible_fragments(inline.children or ())).split())
        if visible:
            headings.append(visible)
    return headings


def _validate_revision_common(
    source: str,
    improved: str,
    *,
    max_characters: int,
) -> None:
    if not improved.strip():
        raise ImprovementError("El modelo local devolvió una respuesta vacía.")
    if "\0" in improved:
        raise ImprovementError("La respuesta del modelo contiene caracteres no válidos.")
    if len(improved) > max_characters:
        raise ImprovementError("La respuesta del modelo es demasiado grande para revisarla.")
    _validate_internal_markers(source, improved)
    if _title_roman_reference_values(source) != _title_roman_reference_values(improved):
        raise ImprovementError("El modelo cambió números romanos de un título o índice.")
    if markdown_link_destinations(source) != markdown_link_destinations(improved):
        raise ImprovementError("El modelo cambió u omitió destinos de enlaces del documento.")
    if Counter(INLINE_CODE_PATTERN.findall(source)) != Counter(
        INLINE_CODE_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo cambió u omitió código en línea del documento.")


def _validate_internal_markers(source: str, improved: str) -> None:
    """Reject missing markers and instruction placeholders leaked into document text."""
    source_markers = Counter(
        match.group(0)
        for match in HTML_COMMENT_PATTERN.finditer(source)
        if "PZDOC" in match.group(0)
    )
    improved_markers = Counter(
        match.group(0)
        for match in HTML_COMMENT_PATTERN.finditer(improved)
        if "PZDOC" in match.group(0)
    )
    if source_markers != improved_markers:
        raise ImprovementError("El modelo cambió u omitió marcadores internos del documento.")
    if Counter(_INSTRUCTION_PLACEHOLDER_TAG_PATTERN.findall(source)) != Counter(
        _INSTRUCTION_PLACEHOLDER_TAG_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo añadió un marcador técnico al texto del documento.")


def _structure_visible_text(markdown: str) -> str:
    """Return reader-visible content while ignoring Markdown structure markers."""
    parser = MarkdownIt("commonmark").enable("table")
    fragments: list[str] = []
    for token in parser.parse(markdown):
        if token.type == "inline":
            fragments.extend(_inline_visible_fragments(token.children or ()))
        elif token.type in {"code_block", "fence"}:
            fragments.append(token.content)
        elif token.type == "html_block":
            fragments.append(_html_visible_text(token.content))
    return " ".join("".join(fragments).split())


def _inline_visible_fragments(tokens: tuple[Token, ...] | list[Token]) -> list[str]:
    fragments: list[str] = []
    for token in tokens:
        if token.type in {"text", "text_special", "code_inline", "image"}:
            fragments.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            fragments.append("\n")
        elif token.type == "html_inline":
            fragments.append(_html_visible_text(token.content))
    return fragments


def _html_visible_text(value: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", "", without_comments)


def _validate_output(
    source: str,
    improved: str,
    *,
    max_characters: int = MAX_OUTPUT_CHARACTERS,
) -> None:
    if not improved.strip():
        raise ImprovementError("El modelo local devolvió una respuesta vacía.")
    if "\0" in improved:
        raise ImprovementError("La respuesta del modelo contiene caracteres no válidos.")
    if len(improved) > max_characters:
        raise ImprovementError("La respuesta del modelo es demasiado grande para publicarla.")
    _validate_internal_markers(source, improved)
    if Counter(NUMBER_PATTERN.findall(source)) != Counter(NUMBER_PATTERN.findall(improved)):
        raise ImprovementError("El modelo cambió u omitió números o fechas del documento.")
    if _title_roman_reference_values(source) != _title_roman_reference_values(improved):
        raise ImprovementError("El modelo cambió números romanos de un título o índice.")
    if markdown_link_destinations(source) != markdown_link_destinations(improved):
        raise ImprovementError("El modelo cambió u omitió destinos de enlaces del documento.")
    if Counter(INLINE_CODE_PATTERN.findall(source)) != Counter(
        INLINE_CODE_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo cambió u omitió código en línea del documento.")
    if markdown_heading_levels(source) != markdown_heading_levels(improved):
        raise ImprovementError("El modelo cambió la estructura de encabezados del documento.")
    if markdown_table_shapes(source) != markdown_table_shapes(improved):
        raise ImprovementError("El modelo cambió la estructura de una tabla del documento.")


def _validated_language(target_language: str | None) -> str | None:
    if target_language is None:
        return None
    language = target_language.strip()
    if not language:
        return None
    if len(language) > 80 or any(character in language for character in "\r\n\0"):
        raise ImprovementError("El idioma de destino no es válido.")
    return language


def _validate_translation(
    source: str,
    translated: str,
    context: _TranslationContext,
) -> None:
    try:
        validate_translation_quality(
            source,
            translated,
            source_language=context.source_language,
            target_language=context.target_language,
            preserve_paragraphs=context.preserve_paragraphs,
        )
    except TranslationQualityError as exc:
        raise ImprovementError(str(exc)) from exc
