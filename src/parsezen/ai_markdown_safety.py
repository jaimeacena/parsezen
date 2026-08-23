"""Markdown protection, reconciliation and validation for local-AI output."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from difflib import SequenceMatcher

from markdown_it import MarkdownIt
from markdown_it.token import Token

from parsezen.errors import ImprovementError
from parsezen.improvement_contracts import (
    MAX_LOCAL_AI_OUTPUT_CHARACTERS as MAX_OUTPUT_CHARACTERS,
)
from parsezen.improvement_contracts import (
    ImprovementMode,
    _MarkdownPart,
    _ProtectedMarkdown,
    _ProtectedValue,
    _TranslationContext,
)
from parsezen.revision import validate_review_content_candidate
from parsezen.translation_quality import (
    ATX_HEADING_PATTERN,
    FENCE_PATTERN,
    HTML_COMMENT_PATTERN,
    HTML_TAG_PATTERN,
    INLINE_CODE_PATTERN,
    MARKDOWN_LINK_PATTERN,
    NUMBER_PATTERN,
    TABLE_DIVIDER_PATTERN,
    TITLE_LANGUAGE_HINTS,
    TranslationQualityError,
    detect_language_code,
    html_tag_structure,
    is_probable_organization_name_line,
    is_unmarked_title_line,
    link_destination_spans,
    markdown_heading_levels,
    markdown_link_destinations,
    markdown_table_shapes,
    natural_language_text,
    numeric_token_counts,
    probable_uppercase_person_name_bases,
    replace_established_term_residues,
    uppercase_person_name_base,
    validate_translation_quality,
)

MAX_INPUT_CHARACTERS = 4_000
MAX_DOCUMENT_CHARACTERS = 1_000_000
MAX_CONSERVED_VALUES_PER_CHUNK = 16
MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK = 8

LOGGER = logging.getLogger(__name__)

RAW_URL_PATTERN = re.compile(r"(?:https?://|mailto:)[^\s<>)\]]+")
EMPHASIZED_TEXT_PATTERN = re.compile(
    r"(?<![*_])(?P<marker>[*_])(?P<body>[^*_\r\n]{2,100})(?P=marker)(?![*_])"
)
FOREIGN_MACRON_WORD_PATTERN = re.compile(
    r"(?<!\w)[A-Za-zĀāĒēĪīŌōŪūȲȳ]*[ĀāĒēĪīŌōŪūȲȳ]"
    r"[A-Za-zĀāĒēĪīŌōŪūȲȳ]{1,47}(?!\w)"
)
FORMULA_PATTERN = re.compile(
    r"(?<!\\)\$(?=[^\r\n$]{1,200}\$)[^\r\n$]+\$"
    r"|\\\([^\r\n]{1,200}\\\)"
    r"|\\\[[^\r\n]{1,500}\\\]"
    r"|(?<!\w)(?:[A-Za-zΑ-ω]\w*|\d+(?:[.,]\d+)?)"
    r"(?:[ \t]*(?:[=±×÷∑√^]|<=|>=|!=|≈|≠|≤|≥)[ \t]*"
    r"(?:[A-Za-zΑ-ω]\w*|\d+(?:[.,]\d+)?)){1,6}(?!\w)",
)
REFERENCE_IDENTIFIER_PATTERN = re.compile(
    r"(?<!\w)\[(?:\d{1,5}(?:[ \t]*[-–,;][ \t]*\d{1,5})*)\]"
    r"|\bdoi:[ \t]*10\.\d{4,9}/[-._;()/:A-Z0-9]+"
    r"|\bISBN(?:-1[03])?:?[ \t]*(?:97[89][ -]?)?[0-9X](?:[ -]?[0-9X]){8,12}\b",
    re.IGNORECASE,
)
_SAFE_TRANSLATION_TABLE_OUTER_PATTERN = re.compile(
    r'\A\s*<table(?: class="document-toc")?>[\s\S]*</table>\s*\Z',
    re.IGNORECASE,
)
_SAFE_TRANSLATION_TABLE_TAG_PATTERN = re.compile(
    r"</?(?:table|thead|tbody|tr|strong|em)>"
    r'|<table class="document-toc">'
    r'|<th class="(?:toc-label|toc-folio)">|</th>|<th>'
    r'|<td class="(?:toc-folio|toc-label toc-level-[0-2])">|</td>|<td>'
    r'|<a href="#page-\d{1,6}">|</a>|<br\s*/?>',
    re.IGNORECASE,
)
TITLE_ROMAN_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z])([IVXLCDM]+)(?=[ \t]+[+-]?\d)",
)
_PRIVATE_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*(?:<)?(?P<resource>__parsezen_resources__/[^\s)>\"']+)(?:>)?[^)]*\)",
)
_EMPTY_PRIVATE_IMAGE_PATTERN = re.compile(
    r"\s*!\[[ \t]*]\(\s*(?:<)?__parsezen_resources__/[^\s)>\"']+(?:>)?[^)]*\)\s*"
)
_PRIVATE_MARKER_TOKEN_PATTERN = re.compile(r"\bPZDOC[^\s<>()\]`]*", re.IGNORECASE)
_PRIVATE_COMMENT_TEXT_PATTERN = re.compile(r"comentario\s+interno", re.IGNORECASE)

INSTRUCTION_PLACEHOLDER_COMMENT = "<!-- PZDOC... -->"
_INSTRUCTION_PLACEHOLDER_TAG_PATTERN = re.compile(
    r"(?:<\s*/?\s*PZDOC\s*/?\s*>|&lt;\s*/?\s*PZDOC\s*/?\s*&gt;)",
    re.IGNORECASE,
)


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
        response = _localize_copied_english_conventions(source, response, translation_context)
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


def _localize_copied_english_conventions(
    source: str,
    translated: str,
    context: _TranslationContext,
) -> str:
    """Localize copied English conventions without touching literal Markdown values."""

    if (context.source_language, context.target_language) != ("en", "es"):
        return translated
    source_spans = _literal_markdown_spans(source)
    translated_spans = _literal_markdown_spans(translated)

    def outside_literal(spans: tuple[tuple[int, int], ...], position: int) -> bool:
        return not any(start <= position < end for start, end in spans)

    patterns = (
        (
            re.compile(r"(?<![\w/])(?P<number>\d{1,4})(?:st|nd|rd|th)\b", re.IGNORECASE),
            lambda match: f"{match.group('number')}.º",
        ),
        (
            re.compile(r"(?<![\w/])BCE\b\.?", re.IGNORECASE),
            lambda _match: "a. e. c.",
        ),
        (
            re.compile(r"(?<![\w/])BC\b\.?", re.IGNORECASE),
            lambda _match: "a. C.",
        ),
        (
            re.compile(
                r"(?<!\w)PART[ \t]+(?P<roman>[IVXLCDM]{1,8})(?!\w)",
                re.IGNORECASE,
            ),
            lambda match: f"PARTE {match.group('roman').upper()}",
        ),
    )
    result = translated
    for pattern, replacement in patterns:
        source_count = sum(
            outside_literal(source_spans, match.start()) for match in pattern.finditer(source)
        )
        if source_count == 0:
            continue
        replaced = 0

        def replace_copied(
            match: re.Match[str],
            maximum: int = source_count,
            literal_spans: tuple[tuple[int, int], ...] = translated_spans,
            replace_value: Callable[[re.Match[str]], str] = replacement,
        ) -> str:
            nonlocal replaced
            if replaced >= maximum or not outside_literal(literal_spans, match.start()):
                return match.group(0)
            replaced += 1
            return replace_value(match)

        result = pattern.sub(replace_copied, result)
        translated_spans = _literal_markdown_spans(result)
    source_visible = _text_outside_literal_spans(source, source_spans)
    return _replace_outside_literal_spans(
        result,
        translated_spans,
        lambda value: replace_established_term_residues(
            source_visible,
            value,
            context.source_language or "",
            context.target_language,
        ),
    )


def _literal_markdown_spans(markdown: str) -> tuple[tuple[int, int], ...]:
    """Return code, URL, destination and private-comment spans excluded from post-editing."""

    spans = [
        *((match.start(), match.end()) for match in INLINE_CODE_PATTERN.finditer(markdown)),
        *((match.start(), match.end()) for match in RAW_URL_PATTERN.finditer(markdown)),
        *((start, end) for start, end, _value in link_destination_spans(markdown)),
        *((match.start(), match.end()) for match in HTML_COMMENT_PATTERN.finditer(markdown)),
    ]
    offset = 0
    fence_start: int | None = None
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        fence = FENCE_PATTERN.match(line)
        if fence is not None:
            marker = fence.group(1)
            if fence_start is None:
                fence_start = offset
                fence_character = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                spans.append((fence_start, offset + len(line)))
                fence_start = None
                fence_character = None
                fence_length = 0
        offset += len(line)
    if fence_start is not None:
        spans.append((fence_start, len(markdown)))
    return tuple(sorted(spans))


def _text_outside_literal_spans(
    markdown: str,
    spans: tuple[tuple[int, int], ...],
) -> str:
    visible: list[str] = []
    cursor = 0
    for start, end in spans:
        if end <= cursor:
            continue
        if start > cursor:
            visible.append(markdown[cursor:start])
        cursor = max(cursor, end)
    visible.append(markdown[cursor:])
    return " ".join(visible)


def _replace_outside_literal_spans(
    markdown: str,
    spans: tuple[tuple[int, int], ...],
    transform: Callable[[str], str],
) -> str:
    rebuilt: list[str] = []
    cursor = 0
    for start, end in spans:
        if end <= cursor:
            continue
        if start > cursor:
            rebuilt.append(transform(markdown[cursor:start]))
        literal_start = max(start, cursor)
        rebuilt.append(markdown[literal_start:end])
        cursor = end
    rebuilt.append(transform(markdown[cursor:]))
    return "".join(rebuilt)


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


def _protect_translation_values(
    markdown: str,
    *,
    protect_numbers: bool = True,
    protect_headings: bool = True,
    protect_paragraphs: bool = True,
    foreign_emphasis_languages: tuple[str, str] | None = None,
) -> _ProtectedMarkdown:
    spans = (
        [(match.start(), match.end(), False) for match in NUMBER_PATTERN.finditer(markdown)]
        if protect_numbers
        else []
    )
    spans.extend(
        (match.start(), match.end(), False) for match in INLINE_CODE_PATTERN.finditer(markdown)
    )
    spans.extend(
        (match.start(), match.end(), False) for match in FORMULA_PATTERN.finditer(markdown)
    )
    spans.extend(
        (match.start(), match.end(), False)
        for match in REFERENCE_IDENTIFIER_PATTERN.finditer(markdown)
    )
    spans.extend((start, end, False) for start, end in _title_roman_reference_spans(markdown))
    spans.extend(
        (match.start(), match.end(), False)
        for match in MARKDOWN_LINK_PATTERN.finditer(markdown)
        if not any(character.isalpha() for character in natural_language_text(match.group(1)))
    )
    spans.extend((start, end, False) for start, end, _value in link_destination_spans(markdown))
    spans.extend(
        (match.start(), match.end(), False) for match in RAW_URL_PATTERN.finditer(markdown)
    )
    spans.extend(
        (match.start(), match.end(), False) for match in HTML_COMMENT_PATTERN.finditer(markdown)
    )
    if foreign_emphasis_languages is not None:
        source_language, target_language = foreign_emphasis_languages
        spans.extend(
            (start, end, False)
            for start, end in _foreign_emphasis_value_spans(
                markdown,
                source_language=source_language,
                target_language=target_language,
            )
        )
        spans.extend(
            (match.start(), match.end(), False)
            for match in FOREIGN_MACRON_WORD_PATTERN.finditer(markdown)
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


def _foreign_emphasis_value_spans(
    markdown: str,
    *,
    source_language: str,
    target_language: str,
) -> tuple[tuple[int, int], ...]:
    """Keep short emphasized foreign terms exact while surrounding prose is translated."""

    source_hints = TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
    spans: list[tuple[int, int]] = []
    for match in EMPHASIZED_TEXT_PATTERN.finditer(markdown):
        body = match.group("body").strip()
        words = re.findall(r"[^\W\d_]+", natural_language_text(body))
        normalized_words = {word.casefold() for word in words}
        if (
            not 1 <= len(words) <= 5
            or sum(character.isalpha() for character in body) < 5
            or normalized_words & source_hints
            or re.search(r"[.!?;](?:\s|$)", body)
        ):
            continue
        detected = detect_language_code(
            body,
            minimum_letters=4,
            minimum_confidence=0.50,
        )
        has_non_ascii_letter = any(
            character.isalpha() and ord(character) > 127 for character in body
        )
        single_title_word = len(words) == 1 and (words[0].istitle() or words[0].isupper())
        if not (
            has_non_ascii_letter
            or detected not in {None, source_language, target_language}
            or single_title_word
        ):
            continue
        spans.append((match.start("body"), match.end("body")))
    return tuple(spans)


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
    required_line_predicate: Callable[[str], bool] | None = None,
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
        if protect_paragraphs and _EMPTY_PRIVATE_IMAGE_PATTERN.fullmatch(block):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if protect_paragraphs and is_probable_organization_name_line(block):
            flush_pending()
            parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
            continue
        if protect_paragraphs and (
            _is_safe_translation_html_table(block) or _is_safe_translation_markdown_table(block)
        ):
            # A complete table is one resumable unit. Splitting it on newlines
            # creates invalid fragments and multiplies retries; the translation
            # layer handles only its text cells in bounded batches.
            flush_pending()
            parts.append(_MarkdownPart(block, True, "\n\n" if parts else ""))
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
            if required_line_predicate is not None and not any(
                required_line_predicate(line) for line in block.splitlines()
            ):
                # Structure review only changes heading candidates. Dense prose,
                # indexes and tables are therefore atomic pass-through blocks:
                # splitting or sending them to the model adds risk without any
                # possible structural improvement.
                parts.append(_MarkdownPart(block, False, "\n\n" if parts else ""))
                continue
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
    if protect_paragraphs:
        return _coalesce_translation_title_runs(
            parts,
            max_characters=max_characters,
        )
    return parts


def _coalesce_translation_title_runs(
    parts: list[_MarkdownPart],
    *,
    max_characters: int,
) -> list[_MarkdownPart]:
    """Batch long title/index runs while keeping isolated headings focused."""

    planned: list[_MarkdownPart] = []
    run: list[_MarkdownPart] = []

    def is_title(part: _MarkdownPart) -> bool:
        text = part.text.strip()
        return part.should_improve and bool(
            is_unmarked_title_line(text) or ATX_HEADING_PATTERN.match(text)
        )

    def flush_run() -> None:
        if len(run) < 3:
            planned.extend(run)
            run.clear()
            return
        group: list[_MarkdownPart] = []
        group_length = 0
        group_values = 0

        def flush_group() -> None:
            nonlocal group_length, group_values
            if not group:
                return
            first, *remaining = group
            text = first.text + "".join(f"{part.separator_before}{part.text}" for part in remaining)
            planned.append(_MarkdownPart(text, True, first.separator_before))
            group.clear()
            group_length = 0
            group_values = 0

        for part in run:
            separator_length = len(part.separator_before) if group else 0
            value_count = _conserved_value_count(part.text) + (1 if group else 0)
            if group and (
                group_length + separator_length + len(part.text) > max_characters
                or group_values + value_count > MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
            ):
                flush_group()
                separator_length = 0
                value_count = _conserved_value_count(part.text)
            group.append(part)
            group_length += separator_length + len(part.text)
            group_values += value_count
        flush_group()
        run.clear()

    for part in parts:
        if is_title(part):
            run.append(part)
        else:
            flush_run()
            planned.append(part)
    flush_run()
    return planned


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
    protected_spans = (
        (match.start(), match.end())
        for pattern in (
            INLINE_CODE_PATTERN,
            MARKDOWN_LINK_PATTERN,
            HTML_COMMENT_PATTERN,
            HTML_TAG_PATTERN,
            FORMULA_PATTERN,
        )
        for match in pattern.finditer(text)
    )
    return not any(start < position < end for start, end in protected_spans)


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


def _safe_translation_markdown_table_cell_spans(
    block: str,
) -> tuple[tuple[int, int], ...] | None:
    """Return exact cell spans for one simple complete Markdown table."""

    lines = block.splitlines(keepends=True)
    if len(lines) < 2:
        return None
    divider_rows = [
        index
        for index, line in enumerate(lines)
        if TABLE_DIVIDER_PATTERN.fullmatch(line.rstrip("\r\n"))
    ]
    if divider_rows != [1]:
        return None

    expected_pipes: int | None = None
    spans: list[tuple[int, int]] = []
    offset = 0
    for row_index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        pipe_positions: list[int] = []
        for position, character in enumerate(body):
            if character != "|":
                continue
            preceding_backslashes = 0
            cursor = position - 1
            while cursor >= 0 and body[cursor] == "\\":
                preceding_backslashes += 1
                cursor -= 1
            if preceding_backslashes % 2 == 0:
                pipe_positions.append(position)
        if (
            len(pipe_positions) < 2
            or body[: pipe_positions[0]].strip()
            or body[pipe_positions[-1] + 1 :].strip()
        ):
            return None
        if expected_pipes is None:
            expected_pipes = len(pipe_positions)
        elif len(pipe_positions) != expected_pipes:
            return None
        if row_index != 1:
            spans.extend(
                (offset + start + 1, offset + end)
                for start, end in zip(pipe_positions, pipe_positions[1:], strict=False)
            )
        offset += len(line)

    expected_columns = (expected_pipes or 1) - 1
    if markdown_table_shapes(block) != ((len(lines), expected_columns),):
        return None
    return tuple(spans)


def _is_safe_translation_markdown_table(block: str) -> bool:
    return _safe_translation_markdown_table_cell_spans(block) is not None


def _is_safe_translation_html_table(block: str) -> bool:
    """Recognize only generated table markup eligible for text-node translation."""

    if _SAFE_TRANSLATION_TABLE_OUTER_PATTERN.fullmatch(block) is None:
        return False
    cursor = 0
    for match in re.finditer(r"<[^<>]*>", block):
        between = block[cursor : match.start()]
        if "<" in between or ">" in between:
            return False
        if _SAFE_TRANSLATION_TABLE_TAG_PATTERN.fullmatch(match.group(0)) is None:
            return False
        cursor = match.end()
    return "<" not in block[cursor:] and ">" not in block[cursor:]


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
        validate_review_content_candidate(source, improved)
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
    if len(MARKDOWN_LINK_PATTERN.findall(source)) != len(MARKDOWN_LINK_PATTERN.findall(improved)):
        raise ImprovementError("El modelo dejó un enlace Markdown incompleto.")
    if Counter(INLINE_CODE_PATTERN.findall(source)) != Counter(
        INLINE_CODE_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo cambió u omitió código en línea del documento.")
    _validate_html_tags(source, improved)


def _validate_html_tags(source: str, improved: str) -> None:
    """Keep raw markup byte-stable so model wrappers can never become reader text."""

    if html_tag_structure(source) != html_tag_structure(improved):
        raise ImprovementError("El modelo añadió o cambió etiquetas HTML del documento.")


def _validate_internal_markers(source: str, improved: str) -> None:
    """Reject any private-marker mutation, addition or leakage."""
    source_markers = Counter(
        match.group(0)
        for match in HTML_COMMENT_PATTERN.finditer(source)
        if re.search(r"PZDOC|comentario\s+interno", match.group(0), re.IGNORECASE)
    )
    improved_markers = Counter(
        match.group(0)
        for match in HTML_COMMENT_PATTERN.finditer(improved)
        if re.search(r"PZDOC|comentario\s+interno", match.group(0), re.IGNORECASE)
    )
    if source_markers != improved_markers:
        raise ImprovementError("El modelo cambió u omitió marcadores internos del documento.")
    source_resources = Counter(
        re.findall(
            re.escape("__parsezen_resources__/") + r"[^\s)>'\"]+",
            source,
        )
    )
    improved_resources = Counter(
        re.findall(
            re.escape("__parsezen_resources__/") + r"[^\s)>'\"]+",
            improved,
        )
    )
    if source_resources != improved_resources:
        raise ImprovementError("El modelo cambió u omitió recursos privados del documento.")
    # The resource and its image role are private structure; the alternative
    # text is user-visible prose and should be translated. Comparing the whole
    # Markdown image used to reject a faithful ``Cover`` -> ``Portada`` change
    # even though the protected destination remained byte-for-byte identical.
    source_images = Counter(
        match.group("resource") for match in _PRIVATE_IMAGE_PATTERN.finditer(source)
    )
    improved_images = Counter(
        match.group("resource") for match in _PRIVATE_IMAGE_PATTERN.finditer(improved)
    )
    if source_images != improved_images:
        raise ImprovementError("El modelo cambió la sintaxis de una imagen privada.")
    if Counter(_INSTRUCTION_PLACEHOLDER_TAG_PATTERN.findall(source)) != Counter(
        _INSTRUCTION_PLACEHOLDER_TAG_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo añadió un marcador técnico al texto del documento.")
    source_inline_private = Counter(
        match.group(0)
        for match in INLINE_CODE_PATTERN.finditer(source)
        if _PRIVATE_MARKER_TOKEN_PATTERN.search(match.group(0))
        or _PRIVATE_COMMENT_TEXT_PATTERN.search(match.group(0))
    )
    improved_inline_private = Counter(
        match.group(0)
        for match in INLINE_CODE_PATTERN.finditer(improved)
        if _PRIVATE_MARKER_TOKEN_PATTERN.search(match.group(0))
        or _PRIVATE_COMMENT_TEXT_PATTERN.search(match.group(0))
    )
    if source_inline_private != improved_inline_private:
        LOGGER.warning(
            "private_marker_validation_failed category=inline source=%d improved=%d",
            sum(source_inline_private.values()),
            sum(improved_inline_private.values()),
        )
        raise ImprovementError("El modelo añadió o modificó un comentario interno.")
    source_visible = INLINE_CODE_PATTERN.sub("", HTML_COMMENT_PATTERN.sub("", source))
    improved_visible = INLINE_CODE_PATTERN.sub("", HTML_COMMENT_PATTERN.sub("", improved))
    source_private_tokens = Counter(
        match.group(0) for match in _PRIVATE_MARKER_TOKEN_PATTERN.finditer(source_visible)
    )
    improved_private_tokens = Counter(
        match.group(0) for match in _PRIVATE_MARKER_TOKEN_PATTERN.finditer(improved_visible)
    )
    source_private_phrases = Counter(
        match.group(0).casefold()
        for match in _PRIVATE_COMMENT_TEXT_PATTERN.finditer(source_visible)
    )
    improved_private_phrases = Counter(
        match.group(0).casefold()
        for match in _PRIVATE_COMMENT_TEXT_PATTERN.finditer(improved_visible)
    )
    if (
        source_private_tokens != improved_private_tokens
        or source_private_phrases != improved_private_phrases
    ):
        LOGGER.warning(
            "private_marker_validation_failed category=unquoted "
            "source_tokens=%d improved_tokens=%d source_phrases=%d improved_phrases=%d",
            sum(source_private_tokens.values()),
            sum(improved_private_tokens.values()),
            sum(source_private_phrases.values()),
            sum(improved_private_phrases.values()),
        )
        raise ImprovementError("El modelo añadió o modificó un comentario interno.")


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
    if numeric_token_counts(source) != numeric_token_counts(improved):
        raise ImprovementError("El modelo cambió u omitió números o fechas del documento.")
    if _title_roman_reference_values(source) != _title_roman_reference_values(improved):
        raise ImprovementError("El modelo cambió números romanos de un título o índice.")
    if markdown_link_destinations(source) != markdown_link_destinations(improved):
        raise ImprovementError("El modelo cambió u omitió destinos de enlaces del documento.")
    if len(MARKDOWN_LINK_PATTERN.findall(source)) != len(MARKDOWN_LINK_PATTERN.findall(improved)):
        raise ImprovementError("El modelo dejó un enlace Markdown incompleto.")
    if Counter(INLINE_CODE_PATTERN.findall(source)) != Counter(
        INLINE_CODE_PATTERN.findall(improved)
    ):
        raise ImprovementError("El modelo cambió u omitió código en línea del documento.")
    _validate_html_tags(source, improved)
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
