"""Free offline Markdown translation using Argos Translate."""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from html import unescape

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import TranslationError
from parsezen.translation_quality import (
    MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS,
    NUMBER_PATTERN,
    TranslationQualityError,
    detect_language_code,
    find_titles_with_source_language_residue,
    find_untranslated_source_sentences,
    find_untranslated_title_lines,
    resolve_language_code,
    validate_translation_quality,
)

MAX_DOCUMENT_CHARACTERS = 1_000_000
MAX_TRANSLATABLE_PART_CHARACTERS = 4_500
MAX_OUTPUT_CHARACTERS = 2_000_000
TRANSLATION_SEGMENT_MARKER = "PZTRANSLATIONSEGMENTV1"

LOGGER = logging.getLogger(__name__)

FENCE_PATTERN = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
TABLE_DIVIDER_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*(?:\r?\n)?$")
THEMATIC_BREAK_PATTERN = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$")
REFERENCE_DEFINITION_PATTERN = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+")
PROTECTED_INLINE_PATTERN = re.compile(
    r"(`+)[^`\n]*?\1"  # inline code
    r"|\]\((?:\\.|[^)\n])*\)"  # link destination
    r"|<(?:(?:https?|mailto):)[^>\n]+>"  # autolink
    r"|</?[A-Za-z][^>\n]*>"  # HTML tag
    r"|(?:https?://|mailto:)[^\s<>)\]]+"  # raw URL
    r"|<!--[\s\S]*?-->"  # protected HTML comment
    r"|\\."  # escaped Markdown character
    r"|[\\`*_~#>\[\]{}|]+"  # Markdown punctuation
    r"|(?<!\w)[+-](?!\w)"  # list marker or isolated sign
)
HTML_ENTITY_PATTERN = re.compile(r"&(?:amp|quot|apos|lt|gt|#\d+|#x[0-9A-Fa-f]+);")

TranslationProgressCallback = Callable[[int, int], None]
TranslationReadyCallback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class _MarkdownPart:
    text: str
    should_translate: bool


@dataclass(frozen=True, slots=True)
class _TranslationGroup:
    part_indexes: tuple[int, ...]


def translate_markdown_offline(
    markdown: str,
    target_language: str,
    *,
    source_language_code: str | None = None,
    on_progress: TranslationProgressCallback | None = None,
    on_engine_ready: TranslationReadyCallback | None = None,
    cancellation: CancellationToken | None = None,
) -> str:
    """Translate in a short-lived local process so model memory is reclaimed."""

    from parsezen.offline_translation_executor import translate_markdown_in_worker

    return translate_markdown_in_worker(
        markdown,
        target_language,
        source_language_code=source_language_code,
        on_progress=on_progress,
        on_engine_ready=on_engine_ready,
        cancellation=cancellation,
    )


def _translate_markdown_offline_in_process(
    markdown: str,
    target_language: str,
    *,
    source_language_code: str | None = None,
    on_progress: TranslationProgressCallback | None = None,
    on_engine_ready: TranslationReadyCallback | None = None,
    cancellation: CancellationToken | None = None,
) -> str:
    """Translate natural-language spans locally while preserving Markdown syntax."""
    check_cancelled(cancellation)
    _validate_document(markdown)
    target_code = resolve_target_language(target_language)
    parts = _plan_markdown_parts(markdown)
    groups = _translation_groups(parts)
    if not groups:
        LOGGER.info("offline_translation_completed engine=argos segments=0")
        return markdown
    source_code = _resolve_source_language(
        source_language_code,
        " ".join(part.text for part in parts if part.should_translate),
    )
    LOGGER.info(
        "offline_translation_started engine=argos source=%s target=%s segments=%d",
        source_code,
        target_code,
        len(groups),
    )

    if source_code == target_code:
        LOGGER.info("offline_translation_completed engine=argos segments=0")
        return markdown

    check_cancelled(cancellation)
    translate_text = _get_or_install_translator(source_code, target_code)
    if on_engine_ready is not None:
        on_engine_ready()
    translated_parts: list[str | None] = [None] * len(parts)
    for current, group in enumerate(groups, start=1):
        check_cancelled(cancellation)
        if on_progress is not None:
            on_progress(current, len(groups))
        try:
            translated_values = _translate_group(parts, group, translate_text)
        except Exception as exc:
            raise TranslationError(
                "El motor gratuito no pudo traducir uno de los fragmentos."
            ) from exc
        for part_index, translated in zip(
            group.part_indexes,
            translated_values,
            strict=True,
        ):
            translated_parts[part_index] = translated

    check_cancelled(cancellation)
    result_parts: list[str] = []
    for index, part in enumerate(parts):
        if not part.should_translate:
            result_parts.append(part.text)
            continue
        translated_part = translated_parts[index]
        if translated_part is None:
            raise TranslationError("Faltan fragmentos en la traducción offline.")
        result_parts.append(translated_part)
    result = "".join(result_parts)

    result = _decode_new_html_entities(markdown, result)
    result = _validate_preserved_numbers(markdown, result)
    result = _retry_unchanged_titles(
        markdown,
        result,
        source_code,
        translate_text,
    )
    result = _retry_unchanged_sentences(
        markdown,
        result,
        source_code,
        target_code,
        translate_text,
    )
    if not result.strip() or "\0" in result or len(result) > MAX_OUTPUT_CHARACTERS:
        raise TranslationError("La traducción recibida no es segura para guardarla.")
    try:
        validate_translation_quality(
            markdown,
            result,
            source_language=source_code,
            target_language=target_code,
            preserve_paragraphs=True,
        )
    except TranslationQualityError as exc:
        raise TranslationError(str(exc)) from exc
    LOGGER.info("offline_translation_completed engine=argos segments=%d", len(groups))
    return result


def _retry_unchanged_titles(
    source: str,
    translated: str,
    source_language: str,
    translate_text: Callable[[str], str],
) -> str:
    repairs = [
        (title, title)
        for title in find_untranslated_title_lines(
            source,
            translated,
            source_language,
        )
    ]
    repairs.extend(
        find_titles_with_source_language_residue(
            source,
            translated,
            source_language,
        )
    )
    unique_repairs = list(dict.fromkeys(repairs))
    if not unique_repairs:
        return translated
    LOGGER.warning("offline_translation_title_fallback count=%d", len(unique_repairs))
    repaired = translated
    for source_title, current_title in unique_repairs:
        replacement = _translate_title_case_normalized(source_title, translate_text)
        repaired = _replace_exact_title_lines(repaired, current_title, replacement)
    return repaired


def _retry_unchanged_sentences(
    source: str,
    translated: str,
    source_language: str,
    target_language: str,
    translate_text: Callable[[str], str],
) -> str:
    sentences = find_untranslated_source_sentences(
        source,
        translated,
        source_language,
    )[:MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS]
    if not sentences:
        return translated

    repaired = translated
    accepted = 0
    for sentence in sentences:
        try:
            replacement = _translate_value_safely(translate_text, sentence)
            if replacement.casefold() == sentence.casefold() and NUMBER_PATTERN.search(sentence):
                replacement = _translate_around_numbers(sentence, translate_text)
            replacement = _validate_preserved_numbers(sentence, replacement)
            validate_translation_quality(
                sentence,
                replacement,
                source_language=source_language,
                target_language=target_language,
                preserve_paragraphs=True,
            )
        except (TranslationError, TranslationQualityError):
            continue
        if replacement.casefold() == sentence.casefold() or sentence not in repaired:
            continue
        repaired = repaired.replace(sentence, replacement, 1)
        accepted += 1
    LOGGER.info(
        "offline_translation_sentence_repair attempted=%d accepted=%d",
        len(sentences),
        accepted,
    )
    return repaired


def _translate_around_numbers(
    sentence: str,
    translate_text: Callable[[str], str],
) -> str:
    pieces: list[str] = []
    position = 0
    for match in NUMBER_PATTERN.finditer(sentence):
        pieces.append(_translate_text_piece(sentence[position : match.start()], translate_text))
        pieces.append(match.group())
        position = match.end()
    pieces.append(_translate_text_piece(sentence[position:], translate_text))
    return "".join(pieces)


def _translate_text_piece(text: str, translate_text: Callable[[str], str]) -> str:
    if not any(character.isalpha() for character in text):
        return text
    leading_length = len(text) - len(text.lstrip())
    trailing_length = len(text) - len(text.rstrip())
    end = len(text) - trailing_length if trailing_length else len(text)
    core = text[leading_length:end]
    translated = _translate_nonempty(translate_text, core).strip()
    return f"{text[:leading_length]}{translated}{text[end:]}"


def _translate_title_case_normalized(
    title: str,
    translate_text: Callable[[str], str],
) -> str:
    heading = re.match(r"^(\s{0,3}#{1,6}[ \t]+)(.*)$", title)
    prefix = heading.group(1) if heading is not None else ""
    content = heading.group(2) if heading is not None else title
    letters = [character for character in content if character.isalpha()]
    was_uppercase = bool(letters) and all(character.isupper() for character in letters)

    normalized = content.casefold()
    translated = _translate_value_safely(translate_text, normalized)
    translated = _validate_preserved_numbers(content, translated).strip()
    if translated.casefold() == normalized:
        raise TranslationError("El traductor offline dejó un título en el idioma original.")
    if was_uppercase:
        translated = translated.upper()
    else:
        translated = _capitalize_first_letter(translated)
    return f"{prefix}{translated}"


def _capitalize_first_letter(text: str) -> str:
    for index, character in enumerate(text):
        if character.isalpha():
            return f"{text[:index]}{character.upper()}{text[index + 1 :]}"
    return text


def _replace_exact_title_lines(markdown: str, source_title: str, translated_title: str) -> str:
    lines = markdown.splitlines(keepends=True)
    replacements = 0
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        line_ending = line[len(content) :]
        if content.strip() != source_title:
            continue
        leading = content[: len(content) - len(content.lstrip())]
        trailing = content[len(content.rstrip()) :]
        lines[index] = f"{leading}{translated_title}{trailing}{line_ending}"
        replacements += 1
    if replacements == 0:
        raise TranslationError("No se pudo localizar un título pendiente de traducir.")
    return "".join(lines)


def _decode_new_html_entities(source: str, translated: str) -> str:
    allowed = Counter(HTML_ENTITY_PATTERN.findall(source))
    seen: Counter[str] = Counter()

    def replace(match: re.Match[str]) -> str:
        entity = match.group(0)
        seen[entity] += 1
        if seen[entity] <= allowed[entity]:
            return entity
        return unescape(entity)

    return HTML_ENTITY_PATTERN.sub(replace, translated)


def _validate_preserved_numbers(source: str, translated: str) -> str:
    if Counter(NUMBER_PATTERN.findall(source)) != Counter(NUMBER_PATTERN.findall(translated)):
        raise TranslationError("La traducción cambió u omitió números o fechas.")
    return translated


def resolve_target_language(target_language: str) -> str:
    """Resolve one supported user-facing language to its ISO code."""
    if not isinstance(target_language, str):
        raise TranslationError("El idioma de destino no es válido.")
    language_code = resolve_language_code(target_language)
    if language_code is not None:
        return language_code
    raise TranslationError("El idioma de destino no está soportado para traducir offline.")


def detect_source_language(text: str) -> str:
    """Detect the dominant source language locally and deterministically."""
    language_code = detect_language_code(text)
    if language_code is None:
        raise TranslationError(
            "No hay suficiente texto para detectar automáticamente el idioma original."
        )
    return language_code


def _resolve_source_language(requested: str | None, text: str) -> str:
    if requested is None:
        return detect_source_language(text)
    normalized = requested.strip().casefold() if isinstance(requested, str) else ""
    if not re.fullmatch(r"[a-z]{2,3}", normalized):
        raise TranslationError("El idioma original no es válido para traducir offline.")
    return normalized


def _get_or_install_translator(
    source_code: str,
    target_code: str,
) -> Callable[[str], str]:
    _configure_safe_argos()
    try:
        return _get_installed_translation(source_code, target_code)
    except TranslationError:
        pass

    LOGGER.info(
        "offline_translation_package_install_started source=%s target=%s",
        source_code,
        target_code,
    )
    try:
        import argostranslate.package as argos_package

        argos_package.update_package_index()
        available_packages = argos_package.get_available_packages()
        pairs = _required_package_pairs(source_code, target_code, available_packages)
        installed_pairs = {
            (package.from_code, package.to_code)
            for package in argos_package.get_installed_packages()
        }
        for from_code, to_code in pairs:
            if (from_code, to_code) in installed_pairs:
                continue
            package = next(
                (
                    candidate
                    for candidate in available_packages
                    if candidate.from_code == from_code and candidate.to_code == to_code
                ),
                None,
            )
            if package is None:
                raise TranslationError(
                    "No existe un paquete gratuito para esta combinación de idiomas."
                )
            argos_package.install_from_path(package.download())
    except TranslationError:
        raise
    except Exception as exc:
        raise TranslationError(
            "No se pudo descargar el paquete gratuito de idioma. "
            "Comprueba la conexión a Internet y vuelve a intentarlo."
        ) from exc

    LOGGER.info(
        "offline_translation_package_install_completed source=%s target=%s",
        source_code,
        target_code,
    )
    return _get_installed_translation(source_code, target_code)


def _get_installed_translation(
    source_code: str,
    target_code: str,
) -> Callable[[str], str]:
    _configure_safe_argos()
    try:
        import argostranslate.translate as argos_translate

        translation = argos_translate.get_translation_from_codes(source_code, target_code)
    except Exception as exc:
        raise TranslationError("Falta el paquete de idioma necesario.") from exc
    if translation is None:
        raise TranslationError("Falta el paquete de idioma necesario.")

    def translate(text: str) -> str:
        result = translation.translate(text)
        if not isinstance(result, str):
            raise TranslationError("El motor gratuito devolvió un resultado no válido.")
        return result

    return translate


def _configure_safe_argos() -> None:
    """Force the non-Stanza segmenter before any translation model is loaded."""

    os.environ["ARGOS_CHUNK_TYPE"] = "MINISBD"
    try:
        import argostranslate.settings as argos_settings

        argos_settings.chunk_type = argos_settings.ChunkType.MINISBD
    except (AttributeError, ImportError) as exc:
        raise TranslationError("El traductor offline seguro no está disponible.") from exc
    if argos_settings.chunk_type is not argos_settings.ChunkType.MINISBD:
        raise TranslationError("No se pudo activar el segmentador seguro del traductor offline.")


def _required_package_pairs(
    source_code: str,
    target_code: str,
    available_packages: list[object],
) -> tuple[tuple[str, str], ...]:
    direct_pair = (source_code, target_code)
    if any(
        getattr(package, "from_code", None) == source_code
        and getattr(package, "to_code", None) == target_code
        for package in available_packages
    ):
        return (direct_pair,)
    if source_code != "en" and target_code != "en":
        return ((source_code, "en"), ("en", target_code))
    return (direct_pair,)


def _translation_groups(parts: list[_MarkdownPart]) -> list[_TranslationGroup]:
    groups: list[_TranslationGroup] = []
    pending: list[int] = []
    pending_length = 0

    def flush() -> None:
        nonlocal pending_length
        if pending:
            groups.append(_TranslationGroup(tuple(pending)))
            pending.clear()
            pending_length = 0

    for index, part in enumerate(parts):
        if part.should_translate:
            separator_length = len(TRANSLATION_SEGMENT_MARKER) + 2 if pending else 0
            candidate_length = pending_length + separator_length + len(part.text)
            if pending and candidate_length > MAX_TRANSLATABLE_PART_CHARACTERS:
                flush()
                separator_length = 0
            pending.append(index)
            pending_length += separator_length + len(part.text)
        if "\n" in part.text:
            flush()
    flush()
    return groups


def _translate_group(
    parts: list[_MarkdownPart],
    group: _TranslationGroup,
    translate_text: Callable[[str], str],
) -> list[str]:
    source_values = [parts[index].text for index in group.part_indexes]
    if len(source_values) == 1 or any(
        TRANSLATION_SEGMENT_MARKER in value for value in source_values
    ):
        return [_translate_value_safely(translate_text, value) for value in source_values]

    joined = f" {TRANSLATION_SEGMENT_MARKER} ".join(source_values)
    translated = _translate_nonempty(translate_text, joined)
    if translated.count(TRANSLATION_SEGMENT_MARKER) != len(source_values) - 1:
        LOGGER.warning("offline_translation_group_fallback marker_changed=true")
        return [_translate_value_safely(translate_text, value) for value in source_values]

    translated_values = [value.strip() for value in translated.split(TRANSLATION_SEGMENT_MARKER)]
    if len(translated_values) != len(source_values) or any(
        not value for value in translated_values
    ):
        LOGGER.warning("offline_translation_group_fallback marker_moved=true")
        return [_translate_value_safely(translate_text, value) for value in source_values]
    if not _numbers_match(source_values, translated_values):
        LOGGER.warning("offline_translation_group_fallback numbers_changed=true")
        translated_values = [
            _translate_value_safely(translate_text, value) for value in source_values
        ]
    return translated_values


def _translate_nonempty(translate_text: Callable[[str], str], text: str) -> str:
    translated = translate_text(text)
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationError("El motor gratuito devolvió una traducción vacía.")
    return translated


def _translate_value_safely(
    translate_text: Callable[[str], str],
    text: str,
) -> str:
    translated = _translate_nonempty(translate_text, text)
    if NUMBER_PATTERN.findall(text) == NUMBER_PATTERN.findall(translated):
        return translated
    return _translate_with_protected_numbers(translate_text, text)


def _numbers_match(source_values: list[str], translated_values: list[str]) -> bool:
    source_numbers = NUMBER_PATTERN.findall(" ".join(source_values))
    translated_numbers = NUMBER_PATTERN.findall(" ".join(translated_values))
    return source_numbers == translated_numbers


def _translate_with_protected_numbers(
    translate_text: Callable[[str], str],
    text: str,
) -> str:
    numbers: list[str] = []

    def protect(match: re.Match[str]) -> str:
        index = len(numbers)
        numbers.append(match.group())
        return _number_placeholder(index)

    protected = NUMBER_PATTERN.sub(protect, text)
    translated = _translate_nonempty(translate_text, protected)
    for index, number in enumerate(numbers):
        placeholder = _number_placeholder(index)
        if translated.count(placeholder) != 1:
            raise TranslationError("El motor gratuito cambió un valor numérico protegido.")
        translated = translated.replace(placeholder, number)
    return translated


def _number_placeholder(index: int) -> str:
    letters: list[str] = []
    value = index
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
        value -= 1
    suffix = "".join(reversed(letters))
    return f"PZNUMBERTOKEN{suffix}ENDPZ"


def _plan_markdown_parts(markdown: str) -> list[_MarkdownPart]:
    parts: list[_MarkdownPart] = []
    fence_character: str | None = None
    fence_length = 0

    for line in markdown.splitlines(keepends=True):
        fence = FENCE_PATTERN.match(line)
        if fence_character is not None:
            parts.append(_MarkdownPart(line, False))
            if fence is not None:
                marker = fence.group(1)
                if marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
            continue
        if fence is not None:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            parts.append(_MarkdownPart(line, False))
            continue
        if (
            TABLE_DIVIDER_PATTERN.fullmatch(line)
            or THEMATIC_BREAK_PATTERN.fullmatch(line.rstrip("\r\n"))
            or REFERENCE_DEFINITION_PATTERN.match(line)
        ):
            parts.append(_MarkdownPart(line, False))
            continue
        _append_tokenized_line(parts, line)
    return parts


def _append_tokenized_line(parts: list[_MarkdownPart], line: str) -> None:
    position = 0
    for match in PROTECTED_INLINE_PATTERN.finditer(line):
        _append_translatable(parts, line[position : match.start()])
        parts.append(_MarkdownPart(match.group(0), False))
        position = match.end()
    _append_translatable(parts, line[position:])


def _append_translatable(parts: list[_MarkdownPart], candidate: str) -> None:
    if not candidate:
        return
    if not candidate.strip():
        parts.append(_MarkdownPart(candidate, False))
        return
    leading_length = len(candidate) - len(candidate.lstrip())
    trailing_length = len(candidate) - len(candidate.rstrip())
    if leading_length:
        parts.append(_MarkdownPart(candidate[:leading_length], False))
    end = len(candidate) - trailing_length if trailing_length else len(candidate)
    core = candidate[leading_length:end]
    if core:
        if any(character.isalpha() for character in core):
            for chunk in _split_long_text(core):
                parts.append(_MarkdownPart(chunk, True))
        else:
            parts.append(_MarkdownPart(core, False))
    if trailing_length:
        parts.append(_MarkdownPart(candidate[end:], False))


def _split_long_text(text: str) -> Iterator[str]:
    remaining = text
    while len(remaining) > MAX_TRANSLATABLE_PART_CHARACTERS:
        split_at = remaining.rfind(" ", 0, MAX_TRANSLATABLE_PART_CHARACTERS + 1)
        if split_at <= 0:
            split_at = MAX_TRANSLATABLE_PART_CHARACTERS
        yield remaining[:split_at]
        remaining = remaining[split_at:]
    if remaining:
        yield remaining


def _validate_document(markdown: str) -> None:
    if not isinstance(markdown, str) or not markdown.strip():
        raise TranslationError("El documento no contiene texto que traducir.")
    if "\0" in markdown:
        raise TranslationError("El documento contiene caracteres no válidos.")
    if len(markdown) > MAX_DOCUMENT_CHARACTERS:
        raise TranslationError(
            f"El documento supera el límite de {MAX_DOCUMENT_CHARACTERS:,} caracteres."
        )
