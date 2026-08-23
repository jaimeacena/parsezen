"""Free offline Markdown translation using Argos Translate."""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from hashlib import sha256
from html import unescape

from parsezen.cancellation import CancellationToken, check_cancelled
from parsezen.errors import TranslationError
from parsezen.processing_metrics import (
    record_checkpoint_lookup,
    record_retry,
    record_validation_rejection,
)
from parsezen.translation_quality import (
    ATX_HEADING_PATTERN,
    MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS,
    NUMBER_PATTERN,
    TITLE_ROMAN_REFERENCE_PATTERN,
    TranslationQualityError,
    detect_language_code,
    find_titles_with_source_language_residue,
    find_untranslated_source_sentences,
    find_untranslated_title_lines,
    is_probable_organization_name_line,
    is_probable_proper_name_line,
    link_destination_spans,
    natural_language_text,
    numeric_tokens_are_conserved,
    resolve_language_code,
    validate_translation_quality,
)

MAX_DOCUMENT_CHARACTERS = 1_000_000
MAX_TRANSLATABLE_PART_CHARACTERS = 4_500
MAX_TRANSLATION_RETRY_PART_CHARACTERS = 700
TRANSLATION_RETRY_PART_CHARACTER_LIMITS = (700, 350, 175, 90, 45, 24)
MAX_OFFLINE_TRANSLATION_WORK_ITEM_CHARACTERS = 60_000
MAX_OUTPUT_CHARACTERS = 2_000_000
TRANSLATION_SEGMENT_MARKER = "PZTRANSLATIONSEGMENTV1"
_OFFLINE_TRANSLATION_CHECKPOINT_REVISION = "offline-translation-work-v6"

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
TRAILING_LIST_FOLIO_PATTERN = re.compile(
    r"^[ \t]{0,3}(?:[-+*]|\d+[.)])[ \t]+.*?"
    r"(?P<folio>(?<!\d)\d{1,3}|(?<![A-Za-z])[ivxlcdm]+)"
    r"(?=[ \t]*(?:\]\((?:\\.|[^)\n])*\))?[ \t]*(?:\r?\n)?$)",
    re.IGNORECASE,
)
HEADING_ROMAN_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z])[IVXLCDM]+(?![A-Za-z])")

TranslationProgressCallback = Callable[[int, int], None]
TranslationReadyCallback = Callable[[], None]
TranslationCheckpointLoader = Callable[[str], str | None]
TranslationCheckpointSaver = Callable[[str, str], bool]


@dataclass(frozen=True, slots=True)
class _MarkdownPart:
    text: str
    should_translate: bool
    normalize_sentence_start: bool = False


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
    load_checkpoint: TranslationCheckpointLoader | None = None,
    save_checkpoint: TranslationCheckpointSaver | None = None,
) -> str:
    """Translate bounded, resumable work items in one private local worker."""

    from parsezen.offline_translation_executor import translate_markdown_in_worker

    check_cancelled(cancellation)
    _validate_document(markdown)
    target_code = resolve_target_language(target_language)
    planned_parts = _plan_markdown_parts(markdown)
    if not any(part.should_translate for part in planned_parts):
        LOGGER.info("offline_translation_completed engine=argos segments=0")
        return markdown
    source_code = _resolve_source_language(
        source_language_code,
        " ".join(part.text for part in planned_parts if part.should_translate),
    )
    if source_code == target_code:
        return markdown

    work_items = tuple(_offline_translation_work_items(markdown))
    work_totals = tuple(
        len(_translation_groups(_plan_markdown_parts(work_item))) for work_item in work_items
    )
    total_groups = sum(work_totals)
    completed_groups = 0
    engine_announced = False
    translated_items: list[str] = []

    def announce_engine_once() -> None:
        nonlocal engine_announced
        if engine_announced:
            return
        engine_announced = True
        if on_engine_ready is not None:
            on_engine_ready()

    for work_item, work_total in zip(work_items, work_totals, strict=True):
        check_cancelled(cancellation)
        checkpoint = _offline_translation_checkpoint_key(
            work_item,
            source_code=source_code,
            target_code=target_code,
        )
        cached = load_checkpoint(checkpoint) if load_checkpoint is not None else None
        if load_checkpoint is not None:
            record_checkpoint_lookup(hit=cached is not None)
        if cached is not None and _valid_offline_translation_work_item(
            work_item,
            cached,
            source_code=source_code,
            target_code=target_code,
        ):
            translated_items.append(cached)
            completed_groups += work_total
            if on_progress is not None and total_groups:
                on_progress(completed_groups, total_groups)
            continue
        if cached is not None:
            record_validation_rejection()

        base_completed = completed_groups

        def report_progress(
            current: int,
            _total: int,
            completed_before_item: int = base_completed,
        ) -> None:
            if on_progress is not None and total_groups:
                on_progress(
                    min(completed_before_item + current, total_groups),
                    total_groups,
                )

        translated = translate_markdown_in_worker(
            work_item,
            target_language,
            source_language_code=source_code,
            on_progress=report_progress,
            on_engine_ready=announce_engine_once,
            cancellation=cancellation,
        )
        if not _valid_offline_translation_work_item(
            work_item,
            translated,
            source_code=source_code,
            target_code=target_code,
        ):
            record_validation_rejection()
            raise TranslationError("La traducciÃ³n local de un bloque no superÃ³ las guardas.")
        translated_items.append(translated)
        completed_groups += work_total
        if save_checkpoint is not None and not save_checkpoint(checkpoint, translated):
            LOGGER.warning("offline_translation_checkpoint_save_failed")

    result = "".join(translated_items)
    if not result.strip() or "\0" in result or len(result) > MAX_OUTPUT_CHARACTERS:
        raise TranslationError("La traducciÃ³n recibida no es segura para guardarla.")
    try:
        validate_translation_quality(
            markdown,
            result,
            source_language=source_code,
            target_language=target_code,
            preserve_paragraphs=True,
        )
    except TranslationQualityError as exc:
        record_validation_rejection()
        raise TranslationError(str(exc)) from exc
    return result


def _offline_translation_work_items(markdown: str) -> Iterator[str]:
    pending: list[str] = []
    pending_characters = 0
    fence_character: str | None = None
    fence_length = 0

    for line in markdown.splitlines(keepends=True):
        if (
            pending
            and fence_character is None
            and pending_characters + len(line) > MAX_OFFLINE_TRANSLATION_WORK_ITEM_CHARACTERS
        ):
            yield "".join(pending)
            pending.clear()
            pending_characters = 0

        pending.append(line)
        pending_characters += len(line)
        fence = FENCE_PATTERN.match(line)
        if fence is None:
            continue
        marker = fence.group(1)
        if fence_character is None:
            fence_character = marker[0]
            fence_length = len(marker)
        elif marker[0] == fence_character and len(marker) >= fence_length:
            fence_character = None
            fence_length = 0

    if pending:
        yield "".join(pending)


def _offline_translation_checkpoint_key(
    source: str,
    *,
    source_code: str,
    target_code: str,
) -> str:
    identity = "\n".join(
        (
            _OFFLINE_TRANSLATION_CHECKPOINT_REVISION,
            source_code,
            target_code,
            source,
        )
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def _valid_offline_translation_work_item(
    source: str,
    translated: str,
    *,
    source_code: str,
    target_code: str,
) -> bool:
    if (
        not isinstance(translated, str)
        or not translated.strip()
        or "\0" in translated
        or len(translated) > MAX_OUTPUT_CHARACTERS
    ):
        return False
    try:
        validate_translation_quality(
            source,
            translated,
            source_language=source_code,
            target_language=target_code,
            preserve_paragraphs=True,
        )
    except TranslationQualityError:
        return False
    return True


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
            translated_parts[part_index] = _preserve_source_uppercase(
                parts[part_index].text,
                translated,
            )

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
    preserved = 0
    for source_title, current_title in unique_repairs:
        try:
            replacement = _translate_title_case_normalized(source_title, translate_text)
            repaired = _replace_exact_title_lines(repaired, current_title, replacement)
        except TranslationError:
            preserved += 1
    if preserved:
        LOGGER.warning("offline_translation_title_preserved count=%d", preserved)
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
            replacement = _translate_protected_fragment_safely(translate_text, sentence)
            natural_sentence = natural_language_text(sentence)
            if (
                replacement.casefold() == sentence.casefold()
                and not is_probable_organization_name_line(natural_sentence)
                and not is_probable_proper_name_line(natural_sentence, source_language)
            ):
                replacement = _translate_sentence_case_normalized(sentence, translate_text)
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
    translated = _translate_protected_fragment_safely(translate_text, core).strip()
    return f"{text[:leading_length]}{translated}{text[end:]}"


def _translate_protected_fragment_safely(
    translate_text: Callable[[str], str],
    fragment: str,
) -> str:
    """Retry prose without ever sending adjacent Markdown guards to Argos."""

    parts = _plan_markdown_parts(fragment)
    translated: list[str] = []
    for part in parts:
        translated.append(
            _translate_value_safely(translate_text, part.text)
            if part.should_translate
            else part.text
        )
    result = "".join(translated)
    if not result.strip():
        raise TranslationError("El reintento offline devolvió un fragmento vacío.")
    return result


def _translate_title_case_normalized(
    title: str,
    translate_text: Callable[[str], str],
) -> str:
    heading = re.match(r"^(\s{0,3}#{1,6}[ \t]+)(.*)$", title)
    prefix = heading.group(1) if heading is not None else ""
    content = heading.group(2) if heading is not None else title
    letters = [character for character in natural_language_text(content) if character.isalpha()]
    was_uppercase = bool(letters) and all(character.isupper() for character in letters)

    normalized = "".join(
        part.text.casefold() if part.should_translate else part.text
        for part in _plan_markdown_parts(content)
    )
    translated = _translate_protected_fragment_safely(translate_text, normalized)
    translated = _validate_preserved_numbers(content, translated).strip()
    if translated.casefold() == normalized:
        raise TranslationError("El traductor offline dejó un título en el idioma original.")
    if was_uppercase:
        translated = _transform_translatable_parts(translated, str.upper)
    else:
        translated = _capitalize_first_translatable_part(translated)
    return f"{prefix}{translated}"


def _preserve_source_uppercase(source: str, translated: str) -> str:
    """Keep an all-caps source span all-caps after a successful translation."""

    letters = [character for character in natural_language_text(source) if character.isalpha()]
    if len(letters) < 2 or not all(character.isupper() for character in letters):
        return translated
    return translated.upper()


def _translate_sentence_case_normalized(
    sentence: str,
    translate_text: Callable[[str], str],
) -> str:
    normalized = _transform_translatable_parts(sentence, str.casefold)
    translated = _translate_protected_fragment_safely(translate_text, normalized)
    translated = _validate_preserved_numbers(sentence, translated)
    return _capitalize_first_translatable_part(translated)


def _transform_translatable_parts(text: str, transform: Callable[[str], str]) -> str:
    return "".join(
        transform(part.text) if part.should_translate else part.text
        for part in _plan_markdown_parts(text)
    )


def _capitalize_first_translatable_part(text: str) -> str:
    parts = _plan_markdown_parts(text)
    result: list[str] = []
    capitalized = False
    for part in parts:
        value = part.text
        if part.should_translate and not capitalized:
            value = _capitalize_first_letter(value)
            capitalized = True
        result.append(value)
    return "".join(result)


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
    if not numeric_tokens_are_conserved(source, translated):
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
    source_values = [_translation_source_value(parts[index]) for index in group.part_indexes]
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


def _translation_source_value(part: _MarkdownPart) -> str:
    if part.normalize_sentence_start:
        return _capitalize_first_letter(part.text)
    return part.text


def _translate_nonempty(translate_text: Callable[[str], str], text: str) -> str:
    translated = translate_text(text)
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationError("El motor gratuito devolvió una traducción vacía.")
    return translated


def _translate_value_safely(
    translate_text: Callable[[str], str],
    text: str,
) -> str:
    return _translate_value_with_retries(translate_text, text, retry_level=0)


def _translate_value_with_retries(
    translate_text: Callable[[str], str],
    text: str,
    *,
    retry_level: int,
) -> str:
    try:
        return _translate_value_once(translate_text, text)
    except Exception:
        for next_level in range(
            retry_level,
            len(TRANSLATION_RETRY_PART_CHARACTER_LIMITS),
        ):
            max_characters = TRANSLATION_RETRY_PART_CHARACTER_LIMITS[next_level]
            retry_parts = tuple(
                _split_translation_retry_parts(
                    text,
                    max_characters=max_characters,
                )
            )
            if len(retry_parts) < 2:
                continue
            LOGGER.warning(
                "offline_translation_part_retry smaller_parts=%d max_characters=%d",
                len(retry_parts),
                max_characters,
            )
            record_retry()
            return "".join(
                f"{_translate_value_with_retries(translate_text, part, retry_level=next_level + 1)}"
                f"{separator}"
                for part, separator in retry_parts
            )
        raise


def _translate_value_once(
    translate_text: Callable[[str], str],
    text: str,
) -> str:
    translated = _translate_nonempty(translate_text, text)
    source_numbers = NUMBER_PATTERN.findall(text)
    translated_numbers = NUMBER_PATTERN.findall(translated)
    if source_numbers == translated_numbers or (
        numeric_tokens_are_conserved(text, translated)
        and _is_subsequence(source_numbers, translated_numbers)
    ):
        return translated
    return _translate_with_protected_numbers(translate_text, text)


def _is_subsequence(expected: list[str], values: list[str]) -> bool:
    pending = iter(values)
    return all(any(candidate == value for candidate in pending) for value in expected)


def _split_translation_retry_parts(
    text: str,
    *,
    max_characters: int,
) -> Iterator[tuple[str, str]]:
    """Split one failed prose span at original whitespace without losing separators."""

    remaining = text
    while len(remaining) > max_characters:
        window = remaining[: max_characters + 1]
        boundaries = tuple(re.finditer(r"\s+", window))
        if not boundaries:
            return
        boundary = boundaries[-1]
        part = remaining[: boundary.start()]
        if not part:
            return
        yield part, remaining[boundary.start() : boundary.end()]
        remaining = remaining[boundary.end() :]
    if remaining:
        yield remaining, ""


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
    return _validate_preserved_numbers(text, translated)


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
    # Keep the token alphabetic so Argos does not reinterpret an index as a
    # document number. The deliberately non-linguistic guards also prevent an
    # ordinal suffix next to the source number (for example ``3rd``) from being
    # merged into the placeholder by the translation model.
    return f"PZXQ{suffix}QXZP"


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
    first_line_part = len(parts)
    position = 0
    for start, end in _protected_inline_spans(line):
        _append_translatable(parts, line[position:start])
        parts.append(_MarkdownPart(line[start:end], False))
        position = end
    _append_translatable(parts, line[position:])
    if _starts_with_lowercase_prose(line):
        for index in range(first_line_part, len(parts)):
            part = parts[index]
            if part.should_translate:
                parts[index] = _MarkdownPart(
                    part.text,
                    True,
                    normalize_sentence_start=True,
                )
                break


def _starts_with_lowercase_prose(line: str) -> bool:
    match = re.match(r"^[ \t]*([^\W\d_]+)(?=[ \t])", line, flags=re.UNICODE)
    return match is not None and match.group(1).islower()


def _protected_inline_spans(line: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in PROTECTED_INLINE_PATTERN.finditer(line)]
    spans.extend(
        (match.start(), match.end()) for match in TITLE_ROMAN_REFERENCE_PATTERN.finditer(line)
    )
    if ATX_HEADING_PATTERN.match(line) is not None:
        spans.extend(
            (match.start(), match.end()) for match in HEADING_ROMAN_TOKEN_PATTERN.finditer(line)
        )
    # Use the exact parser shared by the quality gate. Generic Markdown
    # punctuation can otherwise consume ``[]`` before the wider ``](...)``
    # alternative sees an empty-alt image, exposing a relative destination.
    spans.extend((start, end) for start, end, _value in link_destination_spans(line))
    trailing_folio = TRAILING_LIST_FOLIO_PATTERN.match(line)
    if trailing_folio is not None:
        spans.append((trailing_folio.start("folio"), trailing_folio.end("folio")))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


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
