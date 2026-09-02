"""Validated, local terminology protection for translation workflows."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from parsezen.errors import RequestValidationError, TranslationError
from parsezen.translation_quality import link_destination_spans

MAX_GLOSSARY_ENTRIES = 100
MAX_GLOSSARY_TERM_CHARACTERS = 200
MAX_GLOSSARY_REPLACEMENTS = 10_000

_PROTECTED_MARKDOWN_PATTERN = re.compile(
    r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$"
    r"|`+[^`\n]*`+"
    r"|<!--[\s\S]*?-->"
    r"|<(?:(?:https?|mailto):)[^>\n]+>"
    r"|(?:(?:https?|mailto):)[^\s<>)\]]+"
)


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """One exact source term and the wording required in the result."""

    source: str
    target: str
    adapt_source_case: bool = False


@dataclass(frozen=True, slots=True)
class ProtectedGlossaryText:
    """Text with opaque markers and the private values needed to restore it."""

    text: str
    replacements: tuple[tuple[str, str], ...]

    def restore(self, translated: str) -> str:
        """Restore target terms only when every marker survived exactly once."""
        for marker, _target in self.replacements:
            if translated.count(marker) != 1:
                raise TranslationError(
                    "La traducción no pudo conservar todos los términos del glosario."
                )
        restored = translated
        for marker, target in self.replacements:
            restored = restored.replace(marker, target)
        return restored


def validate_glossary(entries: tuple[GlossaryEntry, ...]) -> tuple[GlossaryEntry, ...]:
    """Normalize a small glossary and reject ambiguous or unsafe entries."""
    if not isinstance(entries, tuple) or len(entries) > MAX_GLOSSARY_ENTRIES:
        raise RequestValidationError(
            f"El glosario admite un máximo de {MAX_GLOSSARY_ENTRIES} términos."
        )
    normalized: list[GlossaryEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, GlossaryEntry):
            raise RequestValidationError("El glosario contiene una entrada no válida.")
        source = entry.source.strip()
        target = entry.target.strip()
        if not source or not target:
            raise RequestValidationError("Completa ambos textos de cada término del glosario.")
        if len(source) > MAX_GLOSSARY_TERM_CHARACTERS or len(target) > MAX_GLOSSARY_TERM_CHARACTERS:
            raise RequestValidationError(
                "Los términos del glosario deben tener 200 caracteres o menos."
            )
        if any(character in source or character in target for character in "\r\n\0"):
            raise RequestValidationError("Cada término del glosario debe ocupar una sola línea.")
        key = source.casefold()
        if key in seen:
            raise RequestValidationError(f'El término "{source}" está repetido en el glosario.')
        seen.add(key)
        normalized.append(GlossaryEntry(source, target, entry.adapt_source_case))
    return tuple(normalized)


def protect_glossary(
    text: str,
    entries: tuple[GlossaryEntry, ...],
    *,
    marker_prefix: str = "PZDOCGLOSSARY",
) -> ProtectedGlossaryText:
    """Replace natural-language glossary matches, leaving code and destinations untouched."""
    if not re.fullmatch(r"PZDOC[A-Z]{3,24}", marker_prefix):
        raise RequestValidationError("El prefijo interno del glosario no es válido.")
    normalized = validate_glossary(entries)
    if not normalized or not text:
        return ProtectedGlossaryText(text, ())
    terms = sorted(normalized, key=lambda item: len(item.source), reverse=True)
    by_source = {entry.source.casefold(): entry for entry in terms}
    alternatives = "|".join(re.escape(entry.source) for entry in terms)
    term_pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)
    replacements: list[tuple[str, str]] = []

    def replace_unprotected(fragment: str) -> str:
        def replace_match(match: re.Match[str]) -> str:
            if len(replacements) >= MAX_GLOSSARY_REPLACEMENTS:
                raise RequestValidationError(
                    "El glosario coincide demasiadas veces en este documento."
                )
            entry = by_source[match.group(0).casefold()]
            target = entry.target
            if entry.adapt_source_case:
                if match.group(0).isupper():
                    target = target.upper()
                elif match.group(0)[:1].isupper():
                    target = f"{target[:1].upper()}{target[1:]}"
            marker = f"<!-- {marker_prefix}{len(replacements):05d}XZQ -->"
            replacements.append((marker, target))
            return marker

        return term_pattern.sub(replace_match, fragment)

    protected_spans = [
        (match.start(), match.end()) for match in _PROTECTED_MARKDOWN_PATTERN.finditer(text)
    ]
    protected_spans.extend((start, end) for start, end, _value in link_destination_spans(text))
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(protected_spans):
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged_spans:
        pieces.append(replace_unprotected(text[cursor:start]))
        pieces.append(text[start:end])
        cursor = end
    pieces.append(replace_unprotected(text[cursor:]))
    return ProtectedGlossaryText("".join(pieces), tuple(replacements))


def glossary_fingerprint(entries: tuple[GlossaryEntry, ...]) -> str:
    """Identify glossary options without exposing their text in cache keys or logs."""
    normalized = validate_glossary(entries)
    digest = hashlib.sha256()
    case_aware = any(entry.adapt_source_case for entry in normalized)
    if case_aware:
        digest.update(b"parsezen-glossary-case-v1\0")
    for entry in normalized:
        digest.update(entry.source.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.target.encode("utf-8"))
        digest.update(b"\0")
        if case_aware:
            digest.update(b"source-case\0" if entry.adapt_source_case else b"exact\0")
    return digest.hexdigest()
