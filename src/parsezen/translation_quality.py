"""Shared fidelity checks for local Markdown translations."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from hashlib import sha256

LOGGER = logging.getLogger(__name__)

MAX_DETECTION_CHARACTERS = 20_000
MIN_LANGUAGE_CONFIDENCE = 0.65
MIN_LANGUAGE_VALIDATION_LETTERS = 120
MIN_BLOCK_LANGUAGE_LETTERS = 180
MIN_SOURCE_TEXT_REPORT_LETTERS = 24
MIN_CONTENT_RATIO = 0.55
MAX_CONTENT_RATIO = 1.80
MIN_SHORT_CONTENT_COVERAGE_LETTERS = 20
MAX_SHORT_CONTENT_RATIO = 2.50
MIN_SHORT_CONTENT_ADDED_LETTERS = 80
MAX_REPORT_ISSUES = 20
MAX_REPORT_EXCERPT_CHARACTERS = 320
MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS = 20

TARGET_LANGUAGE_CODES = {
    "Español": "es",
    "Inglés": "en",
    "Francés": "fr",
    "Alemán": "de",
    "Italiano": "it",
    "Portugués": "pt",
}

DETECTED_LANGUAGE_ALIASES = {
    "zh-cn": "zh",
    "zh-tw": "zt",
}

TITLE_LANGUAGE_HINTS = {
    "en": frozenset(
        {
            "the",
            "and",
            "of",
            "to",
            "day",
            "chapter",
            "introduction",
            "challenge",
            "first",
            "second",
            "third",
            "fourth",
            "fifth",
            "sixth",
            "seventh",
            "eighth",
            "ninth",
            "tenth",
            "house",
            "houses",
            "face",
            "faces",
            "table",
            "contents",
            "part",
            "volume",
            "rulership",
            "book",
            "section",
            "preface",
            "foreword",
            "appendix",
            "ancient",
            "afterword",
            "astrology",
            "aquarius",
            "appendices",
            "cancer",
            "capricorn",
            "condition",
            "benefic",
            "bodyguard",
            "bodyguards",
            "exercise",
            "fortune",
            "gemini",
            "judgment",
            "lot",
            "moon",
            "money",
            "nodes",
            "planets",
            "planetary",
            "phases",
            "pisces",
            "quadrant",
            "readings",
            "reception",
            "rejoicing",
            "releasing",
            "sagittarius",
            "scorpio",
            "scorpion",
            "source",
            "sect",
            "striking",
            "taurus",
            "your",
            "level",
            "overview",
            "rulerships",
            "triplicity",
            "triplicities",
        }
    ),
    "es": frozenset(
        {
            "el",
            "la",
            "los",
            "las",
            "de",
            "del",
            "y",
            "día",
            "capítulo",
            "contenido",
            "índice",
            "introducción",
            "parte",
            "libro",
            "sección",
            "prefacio",
            "prólogo",
            "epílogo",
            "apéndice",
            "apéndices",
            "exposición",
            "edición",
            "ediciones",
            "lujo",
            "centro",
            "nutrición",
            "realidad",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "de",
            "du",
            "et",
            "jour",
            "chapitre",
            "contenu",
            "sommaire",
            "introduction",
            "partie",
            "livre",
            "section",
            "préface",
            "annexe",
        }
    ),
    "de": frozenset(
        {
            "der",
            "die",
            "das",
            "und",
            "von",
            "tag",
            "kapitel",
            "inhalt",
            "einleitung",
            "teil",
            "buch",
            "abschnitt",
            "vorwort",
            "anhang",
        }
    ),
    "it": frozenset(
        {
            "il",
            "la",
            "i",
            "le",
            "di",
            "e",
            "giorno",
            "capitolo",
            "indice",
            "introduzione",
            "parte",
            "libro",
            "sezione",
            "prefazione",
            "appendice",
        }
    ),
    "pt": frozenset(
        {
            "o",
            "a",
            "os",
            "as",
            "de",
            "do",
            "e",
            "dia",
            "capítulo",
            "conteúdo",
            "índice",
            "introdução",
            "parte",
            "livro",
            "seção",
            "prefácio",
            "apêndice",
        }
    ),
}

_ESTABLISHED_TITLE_TRANSLATIONS = {
    ("en", "es"): {
        "afterword": "epílogo",
        "aries": "Aries",
        "art of judgment": "arte del juicio",
        "benefic": "benéfico",
        "benefic and malefic planets, conditions, and houses": (
            "planetas benéficos y maléficos, condiciones y casas"
        ),
        "bodyguard": "guardaespaldas",
        "bodyguards": "guardaespaldas",
        "bonding": "vinculación",
        "bound lord": "señor de los términos",
        "bound lords": "señores de los términos",
        "bound rulerships": "regencias por término",
        "taurus": "Tauro",
        "gemini": "Géminis",
        "cancer": "Cáncer",
        "leo": "Leo",
        "virgo": "Virgo",
        "libra": "Libra",
        "scorpio": "Escorpio",
        "sagittarius": "Sagitario",
        "capricorn": "Capricornio",
        "aquarius": "Acuario",
        "pisces": "Piscis",
        "domicile rulerships": "regencias por domicilio",
        "delineating a planet in a house": "delineación de un planeta en una casa",
        "delineating a planet with its condition in a house": (
            "delineación de un planeta con su condición en una casa"
        ),
        "according to zodiacal sign": "según el signo zodiacal",
        "historical overview": "panorama histórico",
        "historical overview of aspect doctrine": (
            "panorama histórico de la doctrina de los aspectos"
        ),
        "malefic": "maléfico",
        "maltreatment": "maltrato",
        "maltreatment by striking with a ray": "maltrato al golpear con un rayo",
        "planetary phases": "fases planetarias",
        "planetary reception": "recepción planetaria",
        "planetary domiciles": "domicilios planetarios",
        "primary source readings": "lecturas de fuentes primarias",
        "ray": "rayo",
        "rays": "rayos",
        "rejoicing": "gozo",
        "rulership": "regencia",
        "rulerships": "regencias",
        "sect": "secta",
        "sect rejoicing": "gozo por secta",
        "sect rejoicing by hemisphere": "gozo por secta según el hemisferio",
        "science of judgment": "ciencia del juicio",
        "source readings": "lecturas de fuentes",
        "spear-bearing bodyguards": "guardaespaldas portadores de lanza",
        "striking with a ray": "golpear con un rayo",
        "summary and source readings": "resumen y lecturas de fuentes",
        "part six: the art of judgment": "parte seis: el arte del juicio",
        "the art of judgment": "el arte del juicio",
        "the bound lord": "el señor de los términos",
        "the bound lords": "los señores de los términos",
        "the first house": "la primera casa",
        "the second house": "la segunda casa",
        "the third house": "la tercera casa",
        "the fourth house": "la cuarta casa",
        "the fifth house": "la quinta casa",
        "the sixth house": "la sexta casa",
        "the seventh house": "la séptima casa",
        "the eighth house": "la octava casa",
        "the ninth house": "la novena casa",
        "the tenth house": "la décima casa",
        "the eleventh house": "la undécima casa",
        "the twelfth house": "la duodécima casa",
        "the science of judgment": "la ciencia del juicio",
        "the triplicity lord": "el señor de la triplicidad",
        "the triplicity lords": "los señores de la triplicidad",
        "triplicity": "triplicidad",
        "triplicities": "triplicidades",
        "triplicity lord": "señor de la triplicidad",
        "triplicity lords": "señores de la triplicidad",
        "triplicity rulerships": "regencias por triplicidad",
        "three types of bodyguards": "tres tipos de guardaespaldas",
        "zodiacal sign rulerships": "regencias de los signos zodiacales",
    },
    ("es", "en"): {
        "aries": "Aries",
        "tauro": "Taurus",
        "géminis": "Gemini",
        "cáncer": "Cancer",
        "leo": "Leo",
        "virgo": "Virgo",
        "libra": "Libra",
        "escorpio": "Scorpio",
        "sagitario": "Sagittarius",
        "capricornio": "Capricorn",
        "acuario": "Aquarius",
        "piscis": "Pisces",
        "regencia": "rulership",
    },
}

_ESTABLISHED_UNCHANGED_TITLE_TERMS = {
    languages: frozenset(
        source for source, target in translations.items() if source == target.casefold()
    )
    for languages, translations in _ESTABLISHED_TITLE_TRANSLATIONS.items()
}


def established_term_translation(
    source_term: str,
    source_language: str,
    target_language: str,
) -> str | None:
    """Return a curated conventional equivalent independently of document layout."""

    return _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    ).get(source_term.strip().casefold())


def established_terms_requiring_translation(
    source: str,
    source_language: str,
    target_language: str,
) -> tuple[str, ...]:
    """Return conventional terms present in source whose target spelling changes."""

    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    candidates = {term for term, target in translations.items() if term != target.casefold()}
    found: list[str] = []
    seen: set[str] = set()
    for term in sorted(candidates, key=len, reverse=True):
        if " " not in term:
            continue
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", source, re.IGNORECASE):
            seen.add(term)
            found.append(term)
    for match in re.finditer(r"[^\W\d_]+", source, re.UNICODE):
        term = match.group(0).casefold()
        if term in candidates and term not in seen:
            seen.add(term)
            found.append(term)
    return tuple(found)


def contains_established_translation_candidate(source: str) -> bool:
    """Return whether source contains any conventional term that changes by language."""

    candidates = {
        term
        for translations in _ESTABLISHED_TITLE_TRANSLATIONS.values()
        for term, target in translations.items()
        if term != target.casefold()
    }
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", source, re.IGNORECASE) for term in candidates
    )


_SOURCE_LANGUAGE_WORD_RESIDUE_HINTS = {
    "en": frozenset(
        {
            "although",
            "because",
            "between",
            "during",
            "however",
            "through",
            "toward",
            "towards",
            "whereas",
            "whether",
            "while",
            "within",
            "without",
        }
    ),
}

_SOURCE_LANGUAGE_MORPHOLOGICAL_RESIDUE_SUFFIXES = {
    "en": ("fulness", "lessness", "manship", "ness", "ship", "wards"),
}
MAX_SOURCE_WORD_TRANSLATION_ATTENTION_TERMS = 12

ORGANIZATION_NAME_SUFFIXES = frozenset(
    {
        "books",
        "inc",
        "llc",
        "ltd",
        "press",
        "publisher",
        "publishers",
        "publishing",
    }
)

NUMBER_PATTERN = re.compile(r"(?<!\d)[+-]?\d+(?:[.,:/-]\d+)*(?!\d)")
WRITTEN_NUMBER_VALUES = {
    "two": 2,
    "dos": 2,
    "three": 3,
    "tres": 3,
    "four": 4,
    "cuatro": 4,
    "five": 5,
    "cinco": 5,
    "six": 6,
    "seis": 6,
    "seven": 7,
    "siete": 7,
    "eight": 8,
    "ocho": 8,
    "nine": 9,
    "nueve": 9,
    "ten": 10,
    "diez": 10,
    "eleven": 11,
    "once": 11,
    "twelve": 12,
    "doce": 12,
    "thirteen": 13,
    "trece": 13,
    "fourteen": 14,
    "catorce": 14,
    "fifteen": 15,
    "quince": 15,
    "sixteen": 16,
    "dieciséis": 16,
    "seventeen": 17,
    "diecisiete": 17,
    "eighteen": 18,
    "dieciocho": 18,
    "nineteen": 19,
    "diecinueve": 19,
    "twenty": 20,
    "veinte": 20,
    "thirty": 30,
    "treinta": 30,
    "forty": 40,
    "cuarenta": 40,
    "fifty": 50,
    "cincuenta": 50,
    "sixty": 60,
    "sesenta": 60,
    "seventy": 70,
    "setenta": 70,
    "eighty": 80,
    "ochenta": 80,
    "ninety": 90,
    "noventa": 90,
    "hundred": 100,
    "cien": 100,
    "ciento": 100,
}
WRITTEN_ORDINAL_VALUES = {
    "first": 1,
    "primero": 1,
    "primera": 1,
    "second": 2,
    "segundo": 2,
    "segunda": 2,
    "third": 3,
    "tercero": 3,
    "tercera": 3,
    "fourth": 4,
    "cuarto": 4,
    "cuarta": 4,
    "fifth": 5,
    "quinto": 5,
    "quinta": 5,
    "sixth": 6,
    "sexto": 6,
    "sexta": 6,
    "seventh": 7,
    "séptimo": 7,
    "séptima": 7,
    "eighth": 8,
    "octavo": 8,
    "octava": 8,
    "ninth": 9,
    "noveno": 9,
    "novena": 9,
    "tenth": 10,
    "décimo": 10,
    "décima": 10,
    "eleventh": 11,
    "undécimo": 11,
    "undécima": 11,
    "twelfth": 12,
    "duodécimo": 12,
    "duodécima": 12,
    "thirteenth": 13,
    "decimotercero": 13,
    "decimotercera": 13,
    "fourteenth": 14,
    "decimocuarto": 14,
    "decimocuarta": 14,
    "fifteenth": 15,
    "decimoquinto": 15,
    "decimoquinta": 15,
    "sixteenth": 16,
    "decimosexto": 16,
    "decimosexta": 16,
    "seventeenth": 17,
    "decimoséptimo": 17,
    "decimoséptima": 17,
    "eighteenth": 18,
    "decimoctavo": 18,
    "decimoctava": 18,
    "nineteenth": 19,
    "decimonoveno": 19,
    "decimonovena": 19,
    "twentieth": 20,
    "vigésimo": 20,
    "vigésima": 20,
}
TITLE_ROMAN_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z])([IVXLCDM]+)(?=[ \t]+[+-]?\d)",
)
INLINE_LINK_PATTERN = re.compile(r"\]\(\s*(?:<([^>]+)>|([^\s)]+))")
REFERENCE_LINK_PATTERN = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
INLINE_CODE_PATTERN = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
FENCE_PATTERN = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
ATX_HEADING_PATTERN = re.compile(r"(?m)^\s{0,3}(#{1,6})(?:\s+|$)")
TABLE_DIVIDER_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
UNESCAPED_PIPE_PATTERN = re.compile(r"(?<!\\)\|")
LIST_ITEM_PATTERN = re.compile(r"(?m)^([ \t]*)([-+*]|\d+[.)])[ \t]+")
LIST_ITEM_LINE_PATTERN = re.compile(r"^[ \t]*([-+*]|\d+[.)])[ \t]+(?P<content>.*)$")
TRAILING_LIST_REFERENCE_PATTERN = re.compile(
    r"(?P<reference>(?<!\d)\d{1,3}|(?<![A-Za-z])[ivxlcdm]+)"
    r"(?=[ \t]*(?:\]\(\s*(?:<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\))?[ \t]*$)",
    re.IGNORECASE,
)
BLOCKQUOTE_PATTERN = re.compile(r"(?m)^([ \t]{0,3}>+)[ \t]?")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(")
HTML_COMMENT_PATTERN = re.compile(r"<!--[\s\S]*?-->")
EPUB_XML_PROTECTION_COMMENT_PATTERN = re.compile(
    r"^<!--\s*Z*PZDOC_EPUB_XML(?:_RETRY)?_[A-Z]+_[A-Z]+_XZQ\s*-->$",
    re.IGNORECASE,
)
PDF_PAGE_MARKER_PATTERN = re.compile(
    r"(?m)^\s*<!--\s*PZDOC PDF PAGE \d+\s*-->\s*$",
    re.IGNORECASE,
)
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[([^\]]*)\]\(\s*(?:<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
RAW_URL_PATTERN = re.compile(r"(?:https?://|mailto:)\S+")
WEB_IDENTIFIER_PATTERN = re.compile(
    r"(?i)^(?:www\.)?[a-z0-9](?:[a-z0-9-]*\.)+[a-z]{2,}(?:/[^\s]*)?$"
)
HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]*>")
RAW_TABLE_STRUCTURE_TAG_PATTERN = re.compile(
    r"<\s*(/?)\s*(table|thead|tbody|tr|th|td|br)\b([^>]*)>",
    re.IGNORECASE,
)
REFERENCE_DEFINITION_LINE_PATTERN = re.compile(r"(?m)^\s{0,3}\[[^\]]+\]:\s*\S+.*$")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+|\n+")


class TranslationQualityError(ValueError):
    """A translation is not safe enough to publish."""


class TranslationIssueKind(StrEnum):
    """Small set of non-blocking reasons exposed by the review report."""

    SOURCE_TEXT = "source_text"
    LENGTH = "length"
    LANGUAGE = "language"
    ALIGNMENT = "alignment"
    FIDELITY = "fidelity"


class LinguisticReviewMode(StrEnum):
    """How semantic language review related to the translation pass."""

    NOT_REVIEWED = "not_reviewed"
    CORRECTED_DURING_TRANSLATION = "corrected_during_translation"
    INDEPENDENT_BILINGUAL = "independent_bilingual"
    TARGETED_BILINGUAL = "targeted_bilingual"


@dataclass(frozen=True, slots=True)
class TranslationQualityIssue:
    """One local signal that deserves human review, with bounded excerpts."""

    segment_number: int
    kind: TranslationIssueKind
    message: str
    original_excerpt: str
    translated_excerpt: str
    identifier: str = ""


@dataclass(frozen=True, slots=True)
class TranslationQualityReport:
    """Non-blocking diagnostics produced after a structurally safe translation."""

    source_language: str | None
    target_language: str
    detected_language: str | None
    checked_segments: int
    source_characters: int
    translated_characters: int
    total_issues: int
    issues: tuple[TranslationQualityIssue, ...]
    issue_totals: tuple[tuple[TranslationIssueKind, int], ...] = ()
    source_blocks: int = 0
    translated_blocks: int = 0

    @property
    def issues_by_kind(self) -> dict[TranslationIssueKind, int]:
        """Return exhaustive category totals, including details hidden by the report cap."""

        if self.issue_totals:
            return dict(self.issue_totals)
        return dict(Counter(issue.kind for issue in self.issues))


@dataclass(frozen=True, slots=True)
class LinguisticReviewCoverage:
    """Content-free account of automatic checks and semantic review coverage."""

    mode: LinguisticReviewMode
    translated_blocks: int
    automatically_checked_blocks: int
    semantically_reviewed_blocks: int
    independently_verified_blocks: int
    remaining_issues: int

    def __post_init__(self) -> None:
        counts = (
            self.translated_blocks,
            self.automatically_checked_blocks,
            self.semantically_reviewed_blocks,
            self.independently_verified_blocks,
            self.remaining_issues,
        )
        if any(count < 0 for count in counts):
            raise ValueError("Linguistic review counts cannot be negative.")
        if (
            self.automatically_checked_blocks > self.translated_blocks
            or self.semantically_reviewed_blocks > self.translated_blocks
            or self.independently_verified_blocks > self.semantically_reviewed_blocks
        ):
            raise ValueError("Linguistic review counts are inconsistent.")

    @property
    def semantically_unreviewed_blocks(self) -> int:
        return max(0, self.translated_blocks - self.semantically_reviewed_blocks)


@dataclass(frozen=True, slots=True)
class TranslationRepairResult:
    """A translation after bounded, conservative source-text repair attempts."""

    translated: str
    attempted_segments: int
    repaired_segments: int


TranslationRepairCallback = Callable[[str, str], str]


def resolve_language_code(language: str | None) -> str | None:
    """Resolve one supported display name or ISO code without raising."""
    if not isinstance(language, str):
        return None
    normalized = language.strip().casefold()
    for display_name, language_code in TARGET_LANGUAGE_CODES.items():
        if normalized in {display_name.casefold(), language_code.casefold()}:
            return language_code
    return None


def detect_language_code(
    markdown: str,
    *,
    minimum_letters: int = 20,
    minimum_confidence: float = MIN_LANGUAGE_CONFIDENCE,
) -> str | None:
    """Detect the dominant natural-language code locally and deterministically."""
    sample = natural_language_text(markdown)[:MAX_DETECTION_CHARACTERS]
    if _letter_count(sample) < minimum_letters:
        return None
    try:
        from langdetect import DetectorFactory, LangDetectException, detect_langs

        DetectorFactory.seed = 0
        candidates = detect_langs(sample)
    except (ImportError, LangDetectException):
        return None
    if not candidates or candidates[0].prob < minimum_confidence:
        return None
    detected_language = str(candidates[0].lang)
    return DETECTED_LANGUAGE_ALIASES.get(detected_language, detected_language)


def validate_translation_quality(
    source: str,
    translated: str,
    *,
    source_language: str | None,
    target_language: str | None,
    preserve_paragraphs: bool,
) -> None:
    """Reject incomplete, wrong-language or structurally unsafe translations."""
    _validate_structure(source, translated, preserve_paragraphs=preserve_paragraphs)
    _validate_content_coverage(source, translated)
    if _has_critical_polarity_reversal(
        source,
        translated,
        source_language=source_language,
        target_language=target_language,
    ):
        raise TranslationQualityError(
            "La traducción invirtió una relación de certeza o ambigüedad."
        )
    if target_language is not None:
        _validate_target_language(source, translated, source_language, target_language)


def validate_translation_content_coverage(source: str, translated: str) -> None:
    """Reject only substantive omission or duplication when structure is checked elsewhere."""

    _validate_content_coverage(source, translated)


def build_translation_quality_report(
    source: str,
    translated: str,
    *,
    target_language: str,
    source_language: str | None = None,
) -> TranslationQualityReport:
    """Return bounded, non-blocking review signals without logging document text."""

    return _build_translation_quality_report(
        source,
        translated,
        source_blocks=_report_blocks(source),
        translated_blocks=_report_blocks(translated),
        target_language=target_language,
        source_language=source_language,
        check_heading_fidelity=True,
        literal_work_title_segments=frozenset(),
    )


def build_aligned_translation_quality_report(
    source_segments: Iterable[str],
    translated_segments: Iterable[str],
    *,
    target_language: str,
    source_language: str | None = None,
    literal_work_title_segments: Iterable[int] = (),
) -> TranslationQualityReport:
    """Review known one-to-one segments without guessing alignment from blank lines."""

    sources = tuple(source_segments)
    translations = tuple(translated_segments)
    source = "\n\n".join(sources)
    translated = "\n\n".join(translations)
    return _build_translation_quality_report(
        source,
        translated,
        source_blocks=sources,
        translated_blocks=translations,
        target_language=target_language,
        source_language=source_language,
        check_heading_fidelity=False,
        literal_work_title_segments=frozenset(literal_work_title_segments),
    )


def _build_translation_quality_report(
    source: str,
    translated: str,
    *,
    source_blocks: tuple[str, ...],
    translated_blocks: tuple[str, ...],
    target_language: str,
    source_language: str | None,
    check_heading_fidelity: bool,
    literal_work_title_segments: frozenset[int],
) -> TranslationQualityReport:
    resolved_source_language = source_language or detect_language_code(source)
    detected_language = detect_language_code(translated)
    checked_segments = min(len(source_blocks), len(translated_blocks))
    issues: list[TranslationQualityIssue] = []
    total_issues = 0
    issue_totals: Counter[TranslationIssueKind] = Counter()

    def add_issue(issue: TranslationQualityIssue) -> None:
        nonlocal total_issues
        total_issues += 1
        issue_totals[issue.kind] += 1
        if len(issues) < MAX_REPORT_ISSUES:
            issues.append(issue)

    translated_natural_letters = _letter_count(natural_language_text(translated))
    if (
        translated_natural_letters >= MIN_LANGUAGE_VALIDATION_LETTERS
        and detected_language is not None
        and detected_language != target_language
    ):
        add_issue(
            _report_issue(
                0,
                TranslationIssueKind.LANGUAGE,
                "El idioma predominante no parece ser el idioma solicitado.",
                source_blocks[0] if source_blocks else source,
                translated_blocks[0] if translated_blocks else translated,
            )
        )

    if len(source_blocks) != len(translated_blocks):
        add_issue(
            _report_issue(
                0,
                TranslationIssueKind.ALIGNMENT,
                "No se pudieron alinear todos los fragmentos para el informe.",
                source_blocks[-1] if source_blocks else source,
                translated_blocks[-1] if translated_blocks else translated,
            )
        )

    for index, (source_block, translated_block) in enumerate(
        zip(source_blocks, translated_blocks, strict=False),
        start=1,
    ):
        issue = _translation_segment_issue(
            index,
            source_block,
            translated_block,
            resolved_source_language,
            target_language,
        )
        if issue is None and check_heading_fidelity:
            issue = _source_language_word_residue_issue(
                index,
                source_block,
                translated_block,
                resolved_source_language,
                target_language,
            )
        if (
            issue is not None
            and issue.kind is TranslationIssueKind.SOURCE_TEXT
            and index in literal_work_title_segments
        ):
            issue = None
        if issue is not None:
            add_issue(issue)

    if (
        check_heading_fidelity
        and resolved_source_language is not None
        and resolved_source_language != target_language
    ):
        for issue in _heading_fidelity_issues(
            source,
            translated,
            source_language=resolved_source_language,
            target_language=target_language,
        ):
            add_issue(issue)
        for source_title, current_title in _title_source_language_residues(
            source,
            translated,
            resolved_source_language,
            target_language,
        ):
            add_issue(
                _report_issue(
                    0,
                    TranslationIssueKind.SOURCE_TEXT,
                    "Un título o entrada de índice conserva texto del idioma original.",
                    source_title,
                    current_title,
                )
            )

    return TranslationQualityReport(
        source_language=resolved_source_language,
        target_language=target_language,
        detected_language=detected_language,
        checked_segments=checked_segments,
        source_characters=len(source),
        translated_characters=len(translated),
        total_issues=total_issues,
        issues=tuple(issues),
        issue_totals=tuple(
            (kind, issue_totals[kind]) for kind in TranslationIssueKind if issue_totals[kind]
        ),
        source_blocks=len(source_blocks),
        translated_blocks=len(translated_blocks),
    )


def _heading_fidelity_issues(
    source: str,
    translated: str,
    *,
    source_language: str,
    target_language: str,
) -> tuple[TranslationQualityIssue, ...]:
    source_headings = _markdown_heading_texts(source)
    translated_headings = _markdown_heading_texts(translated)
    if not source_headings or len(source_headings) != len(translated_headings):
        return ()
    issues = list(
        _inconsistent_heading_term_issues(
            source_headings,
            translated_headings,
        )
    )
    for index, (source_heading, translated_heading) in enumerate(
        zip(source_headings, translated_headings, strict=True),
        start=1,
    ):
        if not _is_third_language_heading(
            source_heading,
            source_language=source_language,
            target_language=target_language,
        ):
            continue
        if _natural_text_similarity(source_heading, translated_heading) >= 0.92:
            continue
        issues.append(
            _report_issue(
                index,
                TranslationIssueKind.FIDELITY,
                (
                    "Se transformó un encabezado que parece estar en un tercer idioma; "
                    "conviene revisar su sentido."
                ),
                source_heading,
                translated_heading,
            )
        )
    return tuple(issues)


def restore_changed_third_language_headings(
    source: str,
    translated: str,
    *,
    source_language: str,
    target_language: str,
) -> str:
    """Restore a clearly third-language heading if translation changed its wording."""

    source_matches = _markdown_heading_line_matches(source)
    translated_matches = _markdown_heading_line_matches(translated)
    if not source_matches or len(source_matches) != len(translated_matches):
        return translated

    repairs: list[tuple[int, int, str]] = []
    for source_match, translated_match in zip(
        source_matches,
        translated_matches,
        strict=True,
    ):
        source_heading = source_match.group("text").strip()
        translated_heading = translated_match.group("text").strip()
        if not _is_third_language_heading(
            source_heading,
            source_language=source_language,
            target_language=target_language,
        ):
            continue
        if _natural_text_similarity(source_heading, translated_heading) >= 0.92:
            continue
        replacement = (
            f"{translated_match.group('prefix')}{source_heading}"
            f"{translated_match.group('closing') or ''}"
        )
        repairs.append((translated_match.start(), translated_match.end(), replacement))

    repaired = translated
    for start, end, replacement in reversed(repairs):
        repaired = f"{repaired[:start]}{replacement}{repaired[end:]}"
    return repaired


def _is_third_language_heading(
    heading: str,
    *,
    source_language: str,
    target_language: str,
) -> bool:
    source_text = natural_language_text(heading)
    words = re.findall(r"[^\W\d_]+", source_text)
    normalized_words = {word.casefold() for word in words}
    if (
        not 4 <= len(words) <= 16
        or _letter_count(source_text) < 24
        or normalized_words & TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
        or is_probable_organization_name_line(source_text)
        or _looks_like_probable_proper_name(source_text, source_language)
    ):
        return False
    detected = detect_language_code(
        heading,
        minimum_letters=20,
        minimum_confidence=0.80,
    )
    return detected not in {None, source_language, target_language}


def _inconsistent_heading_term_issues(
    source_headings: tuple[str, ...],
    translated_headings: tuple[str, ...],
) -> tuple[TranslationQualityIssue, ...]:
    observations: dict[str, list[tuple[str, int, str, str]]] = {}
    for index, (source_heading, translated_heading) in enumerate(
        zip(source_headings, translated_headings, strict=True),
        start=1,
    ):
        source_words = {
            _normalized_word(word)
            for word in re.findall(r"[^\W\d_]+", natural_language_text(source_heading))
            if len(_normalized_word(word)) >= 6
        }
        translated_words = {
            _normalized_word(word)
            for word in re.findall(r"[^\W\d_]+", natural_language_text(translated_heading))
            if len(_normalized_word(word)) >= 6
        }
        for source_word in source_words:
            ranked = sorted(
                (
                    (
                        SequenceMatcher(
                            None,
                            source_word,
                            translated_word,
                            autojunk=False,
                        ).ratio(),
                        translated_word,
                    )
                    for translated_word in translated_words
                ),
                reverse=True,
            )
            if not ranked or ranked[0][0] < 0.60:
                continue
            observations.setdefault(source_word, []).append(
                (ranked[0][1], index, source_heading, translated_heading)
            )

    issues: list[TranslationQualityIssue] = []
    for records in observations.values():
        counts = Counter(record[0] for record in records)
        common = counts.most_common(2)
        if len(common) < 2 or common[0][1] < 2 or common[0][1] <= common[1][1]:
            continue
        canonical = common[0][0]
        for variant, index, source_heading, translated_heading in records:
            if variant == canonical or not _looks_like_incompatible_cognate(
                canonical,
                variant,
            ):
                continue
            issues.append(
                _report_issue(
                    index,
                    TranslationIssueKind.FIDELITY,
                    "Un término repetido parece haberse traducido de formas incompatibles.",
                    source_heading,
                    translated_heading,
                )
            )
            break
    return tuple(issues)


def _looks_like_incompatible_cognate(canonical: str, variant: str) -> bool:
    if canonical.startswith(variant) or variant.startswith(canonical):
        return False
    if abs(len(canonical) - len(variant)) > 2:
        return False
    similarity = SequenceMatcher(None, canonical, variant, autojunk=False).ratio()
    if similarity < 0.75:
        return False
    common_prefix = 0
    while (
        common_prefix < min(len(canonical), len(variant))
        and canonical[common_prefix] == variant[common_prefix]
    ):
        common_prefix += 1
    return common_prefix < min(len(canonical), len(variant)) - 1


def _normalized_word(word: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", word.casefold())
        if not unicodedata.combining(character)
    )


def _markdown_heading_texts(markdown: str) -> tuple[str, ...]:
    return tuple(match.group("text").strip() for match in _markdown_heading_line_matches(markdown))


def _markdown_heading_line_matches(markdown: str) -> tuple[re.Match[str], ...]:
    return tuple(
        re.finditer(
            (
                r"(?m)^(?P<prefix>[ \t]{0,3}#{1,6}[ \t]+)"
                r"(?P<text>.*?)(?P<closing>[ \t]+#+[ \t]*)?$"
            ),
            markdown,
        )
    )


def repair_untranslated_source_text(
    source: str,
    translated: str,
    *,
    target_language: str,
    translate_segment: TranslationRepairCallback,
    source_language: str | None = None,
    preserve_paragraphs: bool = True,
) -> TranslationRepairResult:
    """Retry aligned blocks with source-language residue or a critical polarity reversal.

    Ambiguous language, length and alignment warnings remain review-only. A proposed replacement
    is accepted only when the shared structural and translation checks pass for both the block and
    the complete document.
    """

    resolved_source_language = source_language or detect_language_code(source)
    if (
        resolved_source_language is None
        or resolved_source_language == target_language
        or not source.strip()
        or not translated.strip()
    ):
        return TranslationRepairResult(translated, 0, 0)

    source_parts, source_blocks = _repairable_report_blocks(source)
    translated_parts, translated_blocks = _repairable_report_blocks(translated)
    if len(source_blocks) != len(translated_blocks):
        return TranslationRepairResult(translated, 0, 0)

    page_aligned = bool(PDF_PAGE_MARKER_PATTERN.search(source)) and bool(
        PDF_PAGE_MARKER_PATTERN.search(translated)
    )
    attempted = 0
    repaired = 0
    attempted_fragments: set[tuple[int, str]] = set()
    rejected_proposals: Counter[str] = Counter()

    def propose_repair(
        source_fragment: str,
        current_fragment: str,
        segment_number: int,
    ) -> str | None:
        nonlocal attempted
        if attempted >= MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS:
            return None
        natural_fragment = natural_language_text(source_fragment).casefold()
        fragment_key = (segment_number, natural_fragment)
        if not natural_fragment or fragment_key in attempted_fragments:
            return None
        attempted_fragments.add(fragment_key)
        attempted += 1
        proposal = translate_segment(source_fragment, current_fragment)
        if not isinstance(proposal, str) or not proposal.strip() or proposal == current_fragment:
            rejected_proposals["unchanged"] += 1
            return None
        try:
            validate_translation_quality(
                source_fragment,
                proposal,
                source_language=resolved_source_language,
                target_language=target_language,
                preserve_paragraphs=True,
            )
        except TranslationQualityError:
            rejected_proposals["quality"] += 1
            return None
        if _title_source_language_residues(
            source_fragment,
            proposal,
            resolved_source_language,
            target_language,
        ):
            rejected_proposals["source_title"] += 1
            return None
        proposal_issue = _translation_segment_issue(
            segment_number,
            natural_language_text(source_fragment),
            natural_language_text(proposal),
            resolved_source_language,
            target_language,
        )
        if proposal_issue is None:
            proposal_issue = _source_language_word_residue_issue(
                segment_number,
                source_fragment,
                proposal,
                resolved_source_language,
                target_language,
            )
        if proposal_issue is not None:
            rejected_proposals["segment"] += 1
            return None
        return proposal

    def localized_page_is_safe(
        source_part: str,
        current_part: str,
        candidate: str,
    ) -> bool:
        try:
            _validate_structure(
                source_part,
                candidate,
                preserve_paragraphs=False,
            )
            _validate_structure(
                current_part,
                candidate,
                preserve_paragraphs=True,
            )
            _validate_content_coverage(current_part, candidate)
        except TranslationQualityError:
            return False
        return True

    title_repairs = 0
    for source_title, current_title in _title_source_language_residues(
        source,
        translated,
        resolved_source_language,
        target_language,
    ):
        matching_parts = [
            part_index
            for part_index, (source_part, translated_part) in enumerate(
                zip(source_parts, translated_parts, strict=False)
            )
            if _stripped_line_occurrences(source_part, source_title) == 1
            and _stripped_line_occurrences(translated_part, current_title) == 1
        ]
        if len(matching_parts) != 1:
            continue
        part_index = matching_parts[0]
        proposal = propose_repair(
            source_title,
            current_title,
            part_index + 1,
        )
        if proposal is None:
            continue
        replaced = _replace_unique_stripped_line(
            translated_parts[part_index],
            current_title,
            proposal,
        )
        if replaced is None:
            continue
        translated_parts[part_index] = replaced
        title_repairs += 1
        repaired += 1

    if title_repairs:
        candidate_parts, candidate_blocks = _repairable_report_blocks("".join(translated_parts))
        if len(candidate_blocks) == len(source_blocks):
            translated_parts = candidate_parts
            translated_blocks = candidate_blocks

    for source_block, translated_block in zip(source_blocks, translated_blocks, strict=True):
        issue = _translation_segment_issue(
            source_block.segment_number,
            source_block.natural_text,
            translated_block.natural_text,
            resolved_source_language,
            target_language,
        )
        if issue is None:
            issue = _source_language_word_residue_issue(
                source_block.segment_number,
                source_block.natural_text,
                translated_block.natural_text,
                resolved_source_language,
                target_language,
            )
        if issue is None or issue.kind not in {
            TranslationIssueKind.SOURCE_TEXT,
            TranslationIssueKind.FIDELITY,
        }:
            continue
        if attempted >= MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS:
            break

        source_part = source_parts[source_block.part_index]
        current = translated_parts[translated_block.part_index]
        if page_aligned:
            source_subparts, source_subblocks = _paragraph_repairable_report_blocks(source_part)
            translated_subparts, translated_subblocks = _paragraph_repairable_report_blocks(current)
            aligned_subblocks = (
                len(source_subblocks) == len(translated_subblocks) and len(source_subblocks) > 1
            )
            if aligned_subblocks:
                localized_repairs = 0
                for source_subblock, translated_subblock in zip(
                    source_subblocks,
                    translated_subblocks,
                    strict=True,
                ):
                    subissue = _translation_segment_issue(
                        source_subblock.segment_number,
                        source_subblock.natural_text,
                        translated_subblock.natural_text,
                        resolved_source_language,
                        target_language,
                    )
                    if subissue is None:
                        subissue = _source_language_word_residue_issue(
                            source_subblock.segment_number,
                            source_subblock.natural_text,
                            translated_subblock.natural_text,
                            resolved_source_language,
                            target_language,
                        )
                    if subissue is None or subissue.kind not in {
                        TranslationIssueKind.SOURCE_TEXT,
                        TranslationIssueKind.FIDELITY,
                    }:
                        continue
                    proposal = propose_repair(
                        source_subparts[source_subblock.part_index],
                        translated_subparts[translated_subblock.part_index],
                        source_block.segment_number,
                    )
                    if proposal is None:
                        continue
                    current_subpart = translated_subparts[translated_subblock.part_index]
                    translated_subparts[translated_subblock.part_index] = (
                        _preserve_boundary_whitespace(current_subpart, proposal)
                    )
                    localized_repairs += 1
                if localized_repairs:
                    localized = "".join(translated_subparts)
                    if localized_page_is_safe(
                        source_part,
                        current,
                        localized,
                    ):
                        translated_parts[translated_block.part_index] = localized
                        repaired += localized_repairs
                        continue
            elif source_subblocks:
                localized = current
                localized_repairs = 0
                for source_subblock in source_subblocks:
                    source_fragment = source_subparts[source_subblock.part_index]
                    occurrence = _unique_prose_occurrence(localized, source_fragment)
                    if occurrence is None:
                        continue
                    subissue = _translation_segment_issue(
                        source_subblock.segment_number,
                        source_subblock.natural_text,
                        source_subblock.natural_text,
                        resolved_source_language,
                        target_language,
                    )
                    if subissue is None or subissue.kind not in {
                        TranslationIssueKind.SOURCE_TEXT,
                        TranslationIssueKind.FIDELITY,
                    }:
                        continue
                    current_fragment = localized[occurrence[0] : occurrence[1]]
                    proposal = propose_repair(
                        source_fragment,
                        current_fragment,
                        source_block.segment_number,
                    )
                    if proposal is None:
                        continue
                    proposal = _preserve_boundary_whitespace(current_fragment, proposal)
                    localized = (
                        f"{localized[: occurrence[0]]}{proposal}{localized[occurrence[1] :]}"
                    )
                    localized_repairs += 1
                if localized_repairs and localized_page_is_safe(
                    source_part,
                    current,
                    localized,
                ):
                    translated_parts[translated_block.part_index] = localized
                    repaired += localized_repairs
                    continue

            sentence_repairs = 0
            sentence_candidate = current
            for source_sentence in find_untranslated_source_sentences(
                source_part,
                sentence_candidate,
                resolved_source_language,
            ):
                if attempted >= MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS:
                    break
                occurrence = _unique_prose_occurrence(sentence_candidate, source_sentence)
                if occurrence is None:
                    continue
                proposal = propose_repair(
                    source_sentence,
                    sentence_candidate[occurrence[0] : occurrence[1]],
                    source_block.segment_number,
                )
                if proposal is None:
                    continue
                sentence_candidate = (
                    f"{sentence_candidate[: occurrence[0]]}{proposal}"
                    f"{sentence_candidate[occurrence[1] :]}"
                )
                sentence_repairs += 1
            if sentence_repairs and localized_page_is_safe(
                source_part,
                current,
                sentence_candidate,
            ):
                translated_parts[translated_block.part_index] = sentence_candidate
                repaired += sentence_repairs
                continue

        if not page_aligned:
            sentence_repairs = 0
            sentence_candidate = current
            for source_sentence in find_untranslated_source_sentences(
                source_part,
                sentence_candidate,
                resolved_source_language,
            ):
                if attempted >= MAX_AUTOMATIC_SOURCE_TEXT_REPAIRS:
                    break
                occurrence = _unique_prose_occurrence(sentence_candidate, source_sentence)
                if occurrence is None:
                    continue
                proposal = propose_repair(
                    source_sentence,
                    sentence_candidate[occurrence[0] : occurrence[1]],
                    source_block.segment_number,
                )
                if proposal is None:
                    continue
                sentence_candidate = (
                    f"{sentence_candidate[: occurrence[0]]}{proposal}"
                    f"{sentence_candidate[occurrence[1] :]}"
                )
                sentence_repairs += 1
            if sentence_repairs and localized_page_is_safe(
                source_part,
                current,
                sentence_candidate,
            ):
                translated_parts[translated_block.part_index] = sentence_candidate
                repaired += sentence_repairs
                continue

        proposal = propose_repair(
            source_part,
            current,
            source_block.segment_number,
        )
        if proposal is not None:
            translated_parts[translated_block.part_index] = proposal
            repaired += 1

    if repaired == 0:
        if rejected_proposals:
            LOGGER.info(
                "translation_repair_proposals_rejected counts=%s",
                ",".join(
                    f"{reason}:{count}" for reason, count in sorted(rejected_proposals.items())
                ),
            )
        return TranslationRepairResult(translated, attempted, 0)

    candidate = "".join(translated_parts)
    try:
        _validate_structure(
            source,
            candidate,
            preserve_paragraphs=preserve_paragraphs and not page_aligned,
        )
        _validate_structure(translated, candidate, preserve_paragraphs=True)
        _validate_content_coverage(translated, candidate)
    except TranslationQualityError:
        rejected_proposals["document"] += repaired
        LOGGER.info(
            "translation_repair_proposals_rejected counts=%s",
            ",".join(f"{reason}:{count}" for reason, count in sorted(rejected_proposals.items())),
        )
        return TranslationRepairResult(translated, attempted, 0)
    if rejected_proposals:
        LOGGER.info(
            "translation_repair_proposals_rejected counts=%s",
            ",".join(f"{reason}:{count}" for reason, count in sorted(rejected_proposals.items())),
        )
    return TranslationRepairResult(candidate, attempted, repaired)


def find_untranslated_title_lines(
    source: str,
    translated: str,
    source_language: str | None,
) -> tuple[str, ...]:
    """Return exact source-language title lines still present in the translation."""
    if source_language is None:
        return ()
    translated_titles = {
        natural_language_text(line).casefold()
        for line in translated.splitlines()
        if _looks_like_title(line)
    }
    repeated_uppercase_names = probable_uppercase_person_name_bases(
        source,
        source_language,
    )
    untranslated: list[str] = []
    seen: set[str] = set()
    for line in source.splitlines():
        stripped = line.strip()
        if not _looks_like_title(stripped):
            continue
        source_text = natural_language_text(stripped)
        detected_source = detect_language_code(
            stripped,
            minimum_letters=12,
            minimum_confidence=0.80,
        )
        has_language_hint = _has_title_language_hint(source_text, source_language)
        if (
            (_letter_count(source_text) >= 12 or has_language_hint)
            and source_text.casefold() in translated_titles
            and not _looks_like_probable_proper_name(source_text, source_language)
            and uppercase_person_name_base(stripped, source_language)
            not in repeated_uppercase_names
            and (detected_source == source_language or has_language_hint)
            and stripped not in seen
        ):
            untranslated.append(stripped)
            seen.add(stripped)
    return tuple(untranslated)


def _preserve_boundary_whitespace(reference: str, replacement: str) -> str:
    """Keep page and paragraph separators outside a locally repaired prose block."""

    leading = re.match(r"\s*", reference)
    trailing = re.search(r"\s*$", reference)
    prefix = leading.group(0) if leading is not None else ""
    suffix = trailing.group(0) if trailing is not None else ""
    return f"{prefix}{replacement.strip()}{suffix}"


def find_titles_with_source_language_residue(
    source: str,
    translated: str,
    source_language: str | None,
) -> tuple[tuple[str, str], ...]:
    """Return titles that retain characteristic source-language words."""
    if source_language is None:
        return ()
    source_lines = source.splitlines()
    translated_lines = translated.splitlines()
    residues: list[tuple[str, str]] = []
    if len(source_lines) == len(translated_lines):
        for source_line, translated_line in zip(source_lines, translated_lines, strict=True):
            source_title = source_line.strip()
            translated_title = translated_line.strip()
            if not _looks_like_title(source_title) or not translated_title:
                continue
            if _has_title_language_hint(
                source_title,
                source_language,
            ) and _has_title_language_hint(translated_title, source_language):
                residues.append((source_title, translated_title))
        return tuple(residues)

    source_titles: list[tuple[str, Counter[str]]] = []
    for source_line in source_lines:
        source_title = source_line.strip()
        if not _looks_like_title(source_title):
            continue
        if _has_title_language_hint(source_title, source_language):
            source_titles.append((source_title, Counter(NUMBER_PATTERN.findall(source_title))))
    if not source_titles:
        return ()

    for translated_line in translated_lines:
        translated_title = translated_line.strip()
        if not _looks_like_title(translated_title):
            continue
        if (
            not _has_title_language_hint(translated_title, source_language)
            or is_probable_organization_name_line(translated_title)
            or _looks_like_probable_proper_name(translated_title, source_language)
        ):
            continue
        translated_numbers = Counter(NUMBER_PATTERN.findall(translated_title))
        numbered_matches = [
            candidate
            for candidate, numbers in source_titles
            if numbers and numbers == translated_numbers
        ]
        source_title = numbered_matches[0] if len(numbered_matches) == 1 else translated_title
        residues.append((source_title, translated_title))
    return tuple(residues)


def _has_title_language_hint(text: str, language: str) -> bool:
    """Recognize compact source-language terms despite accents or OCR word joining."""

    hints = {_normalized_word(hint) for hint in TITLE_LANGUAGE_HINTS.get(language, frozenset())}
    words = {
        _normalized_word(word) for word in re.findall(r"[^\W\d_]+", natural_language_text(text))
    }
    if words & hints:
        return True
    long_hints = {hint for hint in hints if len(hint) >= 4}
    return any(hint in word and len(word) - len(hint) >= 3 for word in words for hint in long_hints)


def _title_source_language_residues(
    source: str,
    translated: str,
    source_language: str | None,
    target_language: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return unique untranslated or partially translated compact title lines."""

    if source_language is None:
        return ()
    residues: list[tuple[str, str]] = [
        (title, title)
        for title in find_untranslated_title_lines(
            source,
            translated,
            source_language,
        )
    ]
    residues.extend(
        find_titles_with_source_language_residue(
            source,
            translated,
            source_language,
        )
    )
    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source_title, current_title in residues:
        if _is_established_unchanged_title_term(
            source_title,
            current_title,
            source_language,
            target_language,
        ):
            continue
        key = (source_title.casefold(), current_title.casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append((source_title, current_title))
    return tuple(unique)


def _is_established_unchanged_title_term(
    source_title: str,
    current_title: str,
    source_language: str,
    target_language: str | None,
) -> bool:
    terms = _ESTABLISHED_UNCHANGED_TITLE_TERMS.get(
        (source_language, target_language or ""),
        frozenset(),
    )
    if not terms:
        return False

    def lexical_base(value: str) -> tuple[str, ...]:
        return tuple(
            word
            for word in (
                _normalized_word(token)
                for token in re.findall(r"[^\W\d_]+", natural_language_text(value))
            )
            if word and not re.fullmatch(r"[ivxlcdm]+", word)
        )

    source_base = lexical_base(source_title)
    return (
        len(source_base) == 1
        and source_base == lexical_base(current_title)
        and source_base[0] in terms
    )


def _stripped_line_occurrences(markdown: str, candidate: str) -> int:
    return sum(line.strip() == candidate for line in markdown.splitlines())


def _replace_unique_stripped_line(
    markdown: str,
    current: str,
    replacement: str,
) -> str | None:
    if "\n" in replacement or "\r" in replacement:
        return None
    lines = markdown.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.strip() == current]
    if len(matches) != 1:
        return None
    index = matches[0]
    line = lines[index]
    content = line.rstrip("\r\n")
    line_ending = line[len(content) :]
    leading = content[: len(content) - len(content.lstrip())]
    trailing = content[len(content.rstrip()) :]
    lines[index] = f"{leading}{replacement.strip()}{trailing}{line_ending}"
    return "".join(lines)


def find_untranslated_source_sentences(
    source: str,
    translated: str,
    source_language: str | None,
) -> tuple[str, ...]:
    """Return exact prose sentences that still occur unchanged in a translation."""
    if source_language is None:
        return ()
    normalized_translation = translated.casefold()
    compact_reference_prefixes = tuple(
        prefix
        for block in re.split(r"\n\s*\n", source)
        if (prefix := _compact_reference_prefix(block)) is not None
    )
    has_index_context = _is_collapsed_index(source)
    catalogue_cells = {
        natural_language_text(cell).casefold()
        for line in source.splitlines()
        if line.count("|") >= 2 and is_reference_or_catalogue_text(line)
        for cell in line.split("|")
        if natural_language_text(cell)
    }
    untranslated: list[str] = []
    raw_candidates: list[str] = []
    for sentence_or_line in SENTENCE_SPLIT_PATTERN.split(source):
        if sentence_or_line.count("|") >= 2:
            raw_candidates.extend(sentence_or_line.split("|"))
        else:
            raw_candidates.append(sentence_or_line)
    for raw_candidate in raw_candidates:
        candidate = re.sub(
            r"\s+",
            " ",
            _strip_boundary_markup(raw_candidate.strip()),
        ).strip()
        natural_candidate = natural_language_text(candidate)
        if _letter_count(natural_candidate) < 12:
            continue
        normalized_natural_candidate = natural_candidate.casefold()
        if (
            is_reference_or_catalogue_text(candidate)
            or (has_index_context and is_index_entry(candidate))
            or normalized_natural_candidate in catalogue_cells
            or any(normalized_natural_candidate in prefix for prefix in compact_reference_prefixes)
        ):
            continue
        normalized_candidate = candidate.casefold()
        if (
            not _contains_standalone_text(normalized_translation, normalized_candidate)
            and _unique_prose_occurrence(normalized_translation, normalized_candidate) is None
        ):
            continue
        if _likely_language(natural_candidate, source_language):
            untranslated.append(candidate)
    return tuple(untranslated)


def is_unmarked_title_line(line: str) -> bool:
    """Return whether one line looks like an all-caps title without Markdown heading marks."""
    stripped = line.strip()
    return ATX_HEADING_PATTERN.match(stripped) is None and _looks_like_title(stripped)


def is_probable_organization_name_line(line: str) -> bool:
    """Recognize compact publisher and company names that should remain literal."""
    words = re.findall(r"[^\W\d_]+", natural_language_text(line))
    if not 2 <= len(words) <= 10:
        return False
    normalized = tuple(word.casefold() for word in words)
    has_organization_suffix = normalized[-1] in ORGANIZATION_NAME_SUFFIXES or normalized[-2:] == (
        "university",
        "press",
    )
    name_like_casing = all(word.isupper() or word[:1].isupper() for word in words)
    return has_organization_suffix and name_like_casing


def is_probable_proper_name_line(text: str, source_language: str) -> bool:
    """Expose the conservative proper-name check to local translation fallbacks."""

    return _looks_like_probable_proper_name(text, source_language)


def is_literal_work_title_translation(
    source: str,
    translated: str,
    *,
    source_language: str,
    target_language: str,
) -> bool:
    """Recognize a cited work title whose literal wording should remain stable."""

    source_words = re.findall(r"[^\W\d_]+", natural_language_text(source))
    translated_words = re.findall(r"[^\W\d_]+", natural_language_text(translated))
    if not 2 <= len(source_words) <= 12 or len(source_words) != len(translated_words):
        return False
    if all(word.isupper() for word in source_words):
        return all(
            source_word.casefold() == translated_word.casefold()
            for source_word, translated_word in zip(
                source_words,
                translated_words,
                strict=True,
            )
        )

    changed_indexes = {
        index
        for index, (source_word, translated_word) in enumerate(
            zip(source_words, translated_words, strict=True)
        )
        if source_word.casefold() != translated_word.casefold()
    }
    if changed_indexes != {0}:
        return False
    source_hints = TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
    target_hints = TITLE_LANGUAGE_HINTS.get(target_language, frozenset())
    return (
        source_words[0].casefold() in source_hints
        and translated_words[0].casefold() in target_hints
        and sum(word.isupper() or word[:1].isupper() for word in source_words[1:]) >= 2
    )


def uppercase_person_name_base(line: str, source_language: str | None) -> str | None:
    """Return a conservative key for a short, uppercase person-name candidate."""
    if source_language is None:
        return None
    words = re.findall(r"[^\W\d_]+", natural_language_text(line))
    if not 2 <= len(words) <= 4 or not all(word.isupper() for word in words):
        return None
    normalized = {word.casefold() for word in words}
    if normalized & TITLE_LANGUAGE_HINTS.get(source_language, frozenset()):
        return None
    return " ".join(word.casefold() for word in words)


def probable_uppercase_person_name_bases(
    markdown: str,
    source_language: str | None,
) -> frozenset[str]:
    """Recognize repeated uppercase bylines without treating every uppercase title as a name."""
    counts: Counter[str] = Counter()
    has_lifespan: set[str] = set()
    for line in markdown.splitlines():
        base = uppercase_person_name_base(line, source_language)
        if base is None:
            continue
        counts[base] += 1
        if re.search(r"\(\s*\d{4}\s*[-–—]\s*\d{4}\s*\)", line):
            has_lifespan.add(base)
    return frozenset(base for base, count in counts.items() if count >= 2 or base in has_lifespan)


def natural_language_text(markdown: str) -> str:
    """Return prose suitable for language and coverage checks, excluding code and URLs."""
    visible_lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines():
        fence = FENCE_PATTERN.match(line)
        if fence_character is not None:
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
            continue
        visible_lines.append(line)

    text = "\n".join(visible_lines)
    text = HTML_COMMENT_PATTERN.sub(" ", text)
    text = REFERENCE_DEFINITION_LINE_PATTERN.sub(" ", text)
    text = INLINE_CODE_PATTERN.sub(" ", text)
    text = MARKDOWN_LINK_PATTERN.sub(lambda match: match.group(1), text)
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = RAW_URL_PATTERN.sub(" ", text)
    text = re.sub(r"[\\`*_~#>\[\]{}|]", " ", text)
    text = NUMBER_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _unique_prose_occurrence(container: str, fragment: str) -> tuple[int, int] | None:
    comments = HTML_COMMENT_PATTERN.findall(fragment)
    if (
        not fragment.strip()
        or "\n\n" in fragment
        or ATX_HEADING_PATTERN.search(fragment)
        or LIST_ITEM_PATTERN.search(fragment)
        or TABLE_DIVIDER_PATTERN.search(fragment)
        or IMAGE_PATTERN.search(fragment)
        or any(
            EPUB_XML_PROTECTION_COMMENT_PATTERN.fullmatch(comment) is None for comment in comments
        )
    ):
        return None
    if container.count(fragment) == 1:
        start = container.index(fragment)
        end = start + len(fragment)
        if _has_standalone_text_boundaries(container, start, end):
            return start, end
    tokens = fragment.split()
    if not tokens:
        return None
    flexible = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    matches = tuple(
        match
        for match in flexible.finditer(container)
        if _has_standalone_text_boundaries(container, match.start(), match.end())
    )
    if len(matches) != 1:
        return None
    return matches[0].start(), matches[0].end()


@dataclass(frozen=True, slots=True)
class _RepairableReportBlock:
    part_index: int
    segment_number: int
    natural_text: str


def _repairable_report_blocks(
    document: str,
) -> tuple[list[str], tuple[_RepairableReportBlock, ...]]:
    page_markers = tuple(PDF_PAGE_MARKER_PATTERN.finditer(document))
    if page_markers:
        parts: list[str] = []
        blocks: list[_RepairableReportBlock] = []
        if page_markers[0].start() > 0:
            prefix = document[: page_markers[0].start()]
            parts.append(prefix)
            prefix_text = natural_language_text(prefix)
            if prefix_text:
                blocks.append(_RepairableReportBlock(0, 1, prefix_text))
        for marker_index, marker in enumerate(page_markers):
            end = (
                page_markers[marker_index + 1].start()
                if marker_index + 1 < len(page_markers)
                else len(document)
            )
            part_index = len(parts)
            part = document[marker.start() : end]
            parts.append(part)
            natural_text = natural_language_text(part)
            if natural_text:
                blocks.append(
                    _RepairableReportBlock(
                        part_index=part_index,
                        segment_number=len(blocks) + 1,
                        natural_text=natural_text,
                    )
                )
        return parts, tuple(blocks)

    return _paragraph_repairable_report_blocks(document)


def _paragraph_repairable_report_blocks(
    document: str,
) -> tuple[list[str], tuple[_RepairableReportBlock, ...]]:
    parts = re.split(r"((?:\r?\n)[ \t]*(?:\r?\n)+)", document)
    blocks: list[_RepairableReportBlock] = []
    for part_index in range(0, len(parts), 2):
        natural_text = natural_language_text(parts[part_index])
        if not natural_text:
            continue
        blocks.append(
            _RepairableReportBlock(
                part_index=part_index,
                segment_number=len(blocks) + 1,
                natural_text=natural_text,
            )
        )
    return parts, tuple(blocks)


def _report_blocks(document: str) -> tuple[str, ...]:
    page_markers = tuple(PDF_PAGE_MARKER_PATTERN.finditer(document))
    if page_markers:
        # Page markers are protected throughout translation, so they are the
        # strongest alignment boundary available for converted PDFs.  Keep
        # every page here, including image-only or accidentally empty pages.
        # Filtering empty pages independently from source and result shifts all
        # subsequent pairs as soon as just one side contains visible text.
        blocks: list[str] = []
        if page_markers[0].start() > 0:
            blocks.append(document[: page_markers[0].start()])
        blocks.extend(
            document[
                marker.start() : (
                    page_markers[index + 1].start()
                    if index + 1 < len(page_markers)
                    else len(document)
                )
            ]
            for index, marker in enumerate(page_markers)
        )
        return tuple(blocks)
    parts, repairable_blocks = _repairable_report_blocks(document)
    return tuple(parts[block.part_index] for block in repairable_blocks)


def _translation_segment_issue(
    segment_number: int,
    source: str,
    translated: str,
    source_language: str | None,
    target_language: str,
) -> TranslationQualityIssue | None:
    source_natural = natural_language_text(source)
    translated_natural = natural_language_text(translated)
    source_letters = _letter_count(source_natural)
    is_web_identifier = _looks_like_web_identifier(source_natural)
    is_reference_or_catalogue = is_reference_or_catalogue_text(source)
    has_source_language_hint = _has_title_language_hint(
        source_natural,
        source_language or "",
    )
    unchanged = (
        _unchanged_source_sentence(source, translated, source_language)
        if source_language is not None and source_language != target_language
        else None
    )
    if (
        unchanged is not None
        and source_language is not None
        and (
            _looks_like_probable_proper_name(unchanged, source_language)
            or is_probable_organization_name_line(unchanged)
        )
    ):
        unchanged = None
    if (
        source_language is not None
        and source_language != target_language
        and not is_web_identifier
        and (
            unchanged is not None
            or source_letters >= MIN_SOURCE_TEXT_REPORT_LETTERS
            or has_source_language_hint
        )
    ):
        if unchanged is not None:
            return _report_issue(
                segment_number,
                TranslationIssueKind.SOURCE_TEXT,
                "Parece conservar una frase en el idioma original.",
                source,
                translated,
            )
        similarity = SequenceMatcher(
            None,
            source_natural.casefold(),
            translated_natural.casefold(),
            autojunk=False,
        ).ratio()
        compact_source = "".join(
            character.casefold() for character in source_natural if character.isalpha()
        )
        compact_translated = "".join(
            character.casefold() for character in translated_natural if character.isalpha()
        )
        compact_similarity = (
            SequenceMatcher(
                None,
                compact_source,
                compact_translated,
                autojunk=False,
            ).ratio()
            if compact_source and compact_translated
            else 0.0
        )
        if (
            not is_reference_or_catalogue
            and (similarity >= 0.92 or compact_similarity >= 0.97)
            and _likely_language(source, source_language)
            and not _is_translated_name_credit(source, translated, source_language)
        ):
            return _report_issue(
                segment_number,
                TranslationIssueKind.SOURCE_TEXT,
                "El fragmento cambió muy poco y podría no estar traducido.",
                source,
                translated,
            )

    if _has_critical_polarity_reversal(
        source,
        translated,
        source_language=source_language,
        target_language=target_language,
    ):
        return _report_issue(
            segment_number,
            TranslationIssueKind.FIDELITY,
            "La traducción podría haber invertido una relación de certeza o ambigüedad.",
            source,
            translated,
        )

    translated_letters = _letter_count(translated_natural)
    if source_letters >= MIN_SHORT_CONTENT_COVERAGE_LETTERS:
        ratio = translated_letters / source_letters
        if source_letters >= 80 and ratio < MIN_CONTENT_RATIO:
            return _report_issue(
                segment_number,
                TranslationIssueKind.LENGTH,
                "La traducción es mucho más corta que el fragmento original.",
                source,
                translated,
            )
        excessive_growth = (source_letters >= 80 and ratio > MAX_CONTENT_RATIO) or (
            source_letters < 80
            and ratio > MAX_SHORT_CONTENT_RATIO
            and translated_letters - source_letters >= MIN_SHORT_CONTENT_ADDED_LETTERS
        )
        if excessive_growth:
            return _report_issue(
                segment_number,
                TranslationIssueKind.LENGTH,
                "La traducción es mucho más larga que el fragmento original.",
                source,
                translated,
            )
    return None


def _source_language_word_residue(
    source: str,
    translated: str,
    source_language: str,
) -> bool:
    return bool(source_language_word_residues(source, translated, source_language))


def source_language_word_residues(
    source: str,
    translated: str,
    source_language: str,
) -> tuple[str, ...]:
    """Return bounded unambiguous source-language words copied into a translation."""

    residues: list[str] = []
    for word in source_words_requiring_translation(source, source_language):
        pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
        if re.search(
            pattern,
            translated,
            re.IGNORECASE,
        ):
            residues.append(word)
    return tuple(sorted(residues, key=lambda word: translated.casefold().find(word)))


def source_words_requiring_translation(
    source: str,
    source_language: str,
) -> tuple[str, ...]:
    """Identify high-confidence natural-language words before model generation.

    These candidates let the initial translation request disambiguate ordinary vocabulary from
    names and opaque identifiers. Detection remains deliberately narrow: generic function-word
    hints plus productive, unambiguous suffixes on lowercase source tokens.
    """

    hints = _SOURCE_LANGUAGE_WORD_RESIDUE_HINTS.get(source_language, frozenset())
    morphological_candidates = set(
        source_words_requiring_focused_translation(source, source_language)
    )
    candidates: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r"[^\W\d_]+", source, re.UNICODE):
        token = match.group(0)
        word = token.casefold()
        if word in seen:
            continue
        is_hint = word in hints
        is_morphological_candidate = word in morphological_candidates
        if not (is_hint or is_morphological_candidate):
            continue
        seen.add(word)
        candidates.append((match.start(), word))
        if len(candidates) >= MAX_SOURCE_WORD_TRANSLATION_ATTENTION_TERMS:
            break
    return tuple(word for _position, word in candidates)


def source_words_requiring_focused_translation(
    source: str,
    source_language: str,
) -> tuple[str, ...]:
    """Return morphology-backed terms that benefit from smaller initial model requests."""

    suffixes = _SOURCE_LANGUAGE_MORPHOLOGICAL_RESIDUE_SUFFIXES.get(source_language, ())
    if not suffixes:
        return ()
    words: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[^\W\d_]{7,}", source, re.UNICODE):
        word = token.casefold()
        if token.islower() and word.endswith(suffixes) and word not in seen:
            seen.add(word)
            words.append(word)
            if len(words) >= MAX_SOURCE_WORD_TRANSLATION_ATTENTION_TERMS:
                break
    return tuple(words)


def _source_language_word_residue_issue(
    segment_number: int,
    source: str,
    translated: str,
    source_language: str | None,
    target_language: str,
) -> TranslationQualityIssue | None:
    if (
        source_language is None
        or source_language == target_language
        or _looks_like_web_identifier(natural_language_text(source))
        or is_reference_or_catalogue_text(source)
        or not _source_language_word_residue(
            natural_language_text(source),
            natural_language_text(translated),
            source_language,
        )
    ):
        return None
    return _report_issue(
        segment_number,
        TranslationIssueKind.SOURCE_TEXT,
        "El fragmento conserva una palabra funcional inequívoca del idioma original.",
        source,
        translated,
    )


def is_reference_or_catalogue_text(text: str) -> bool:
    """Recognize one reference entry or an overwhelmingly catalogue-like text unit.

    This deliberately does not classify a whole mixed page merely because it starts with an
    ``INDEX`` or ``BIBLIOGRAPHY`` heading. Callers use it on each unchanged sentence/title and use
    the aggregate result only to suppress the weak whole-segment similarity fallback.
    """

    table_cells = [
        natural_language_text(cell)
        for cell in text.split("|")
        if natural_language_text(cell) and not re.fullmatch(r"\s*:?-{3,}:?\s*", cell)
    ]
    if len(table_cells) >= 3:
        cell_word_counts = [len(re.findall(r"[^\W\d_]+", cell)) for cell in table_cells]
        name_like_cells = sum(_is_name_like_catalogue_cell(cell) for cell in table_cells)
        short_cells = sum(word_count <= 3 for word_count in cell_word_counts)
        if (
            max(cell_word_counts, default=0) <= 6
            and sum(cell_word_counts) >= 3
            and name_like_cells / len(table_cells) >= 0.60
            and short_cells / len(table_cells) >= 0.67
        ):
            return True

    if _is_collapsed_index(text):
        return True

    natural_text = natural_language_text(text)
    words = re.findall(r"[^\W\d_]+", natural_text)
    if not words:
        return False
    normalized = tuple(word.casefold() for word in words)
    if is_reference_or_catalogue_heading(text):
        return True
    if len(words) < 8:
        return False

    citation_hints = {
        "author",
        "authors",
        "cambridge",
        "edition",
        "edited",
        "editor",
        "journal",
        "london",
        "press",
        "publisher",
        "publishers",
        "published",
        "reprinted",
        "translated",
        "translator",
        "university",
        "volume",
    }
    distinct_citation_hints = set(normalized) & citation_hints
    citation_punctuation = len(re.findall(r"[.,:;()]", natural_text))
    if len(distinct_citation_hints) >= 3 and citation_punctuation >= 2:
        return True
    if (
        {"press"} <= distinct_citation_hints
        and citation_punctuation >= 1
        and sum(word.isupper() or word[:1].isupper() for word in words) >= 3
    ):
        return True

    sentence_endings = len(re.findall(r"[.!?](?=\s|$)", natural_text))
    title_like_words = sum(word.isupper() or word[:1].isupper() for word in words)
    title_like_ratio = title_like_words / len(words)
    return (
        len(words) >= 24
        and sentence_endings <= 2
        and natural_text.count(",") >= 8
        and title_like_ratio >= 0.25
    )


def is_reference_or_catalogue_heading(text: str) -> bool:
    """Return whether a short unit is explicitly a references, bibliography or index heading."""

    words = tuple(word.casefold() for word in re.findall(r"[^\W\d_]+", natural_language_text(text)))
    return (
        bool(words)
        and len(words) <= 3
        and words[0]
        in {
            "bibliography",
            "bibliografia",
            "bibliographie",
            "literaturverzeichnis",
            "references",
            "referencias",
            "index",
            "indice",
        }
    )


def _is_name_like_catalogue_cell(value: str) -> bool:
    words = re.findall(r"[^\W\d_]+", value)
    if not words or re.search(r"[.!?](?=\s|$)", value):
        return False
    capitalized = sum(1 for word in words if word.isupper() or word[:1].isupper())
    return capitalized * 5 >= len(words) * 3


def _is_collapsed_index(text: str) -> bool:
    heading = re.match(
        r"\A\s*(?:[#>*-]+\s*)?(?:index|indice)\s*[.:]?\s*",
        text,
        re.IGNORECASE,
    )
    body = text[heading.end() :] if heading is not None else text
    separators = r"\s*;\s*" if ";" in body else r"\r?\n+"
    entries = [entry.strip() for entry in re.split(separators, body) if entry.strip()]
    minimum_entries = 1 if heading is not None else 2
    return len(entries) >= minimum_entries and all(is_index_entry(entry) for entry in entries)


def is_index_entry(text: str) -> bool:
    """Recognize a compact index or TOC entry ending in a plausible folio."""

    candidate = re.sub(r"\A\s*(?:[-*+]\s+|\d+[.)]\s+)", "", text)
    if re.search(r"[.!?](?=\s|$)", candidate):
        return False
    match = re.search(r"(?:^|\s)(\d{1,4}|[ivxlcdm]+)\s*$", candidate, re.IGNORECASE)
    if match is None or len(re.findall(r"[^\W\d_]+", candidate)) > 20:
        return False
    folio = match.group(1)
    return not folio.isdigit() or 1 <= int(folio) <= 999


def translate_established_index_classification(
    text: str,
    source_language: str,
    target_language: str,
) -> str | None:
    """Translate an unambiguous standard classification followed by index references."""

    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    if not translations:
        return None
    match = re.fullmatch(
        r"(?P<prefix>[ \t]*(?:\*\*|__)?)"
        r"(?P<label>[^\W\d_]+)"
        r"(?P<references>(?:[ \t]+[ivxlcdm]+)?[ \t]+\d{1,4})"
        r"(?P<suffix>(?:\*\*|__)?[ \t]*)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    source_label = match.group("label")
    target_label = translations.get(source_label.casefold())
    if target_label is None:
        return None
    if source_label.isupper():
        target_label = target_label.upper()
    elif source_label.islower():
        target_label = target_label.lower()
    return (
        f"{match.group('prefix')}{target_label}{match.group('references')}{match.group('suffix')}"
    )


def replace_established_index_term_residues(
    source: str,
    translated: str,
    source_language: str,
    target_language: str,
) -> str:
    """Localize known terms that a model copied inside an aligned TOC entry."""

    if not is_index_entry(source):
        return translated
    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    result = translated
    for source_term, target_term in translations.items():
        if re.search(rf"(?<!\w){re.escape(source_term)}(?!\w)", source, re.IGNORECASE) is None:
            continue

        def replacement(match: re.Match[str], term: str = target_term) -> str:
            if match.group().isupper():
                return term.upper()
            if match.group().islower():
                return term.lower()
            return term

        result = re.sub(
            rf"(?<!\w){re.escape(source_term)}(?!\w)",
            replacement,
            result,
            flags=re.IGNORECASE,
        )
    return result


def _compact_reference_prefix(text: str) -> str | None:
    """Return the citation prefix ending at its year, excluding any following prose."""

    if not re.match(
        r"\s*(?:[#>*-]+\s*)?(?:bibliography|bibliografia|bibliographie|"
        r"literaturverzeichnis|references|referencias)\b",
        text,
        re.IGNORECASE,
    ):
        return None
    year = re.search(r"\b(?:1[5-9]\d{2}|20\d{2}|21\d{2})\b", text)
    if year is None:
        return None
    prefix = text[: year.end()]
    words = re.findall(r"[^\W\d_]+", natural_language_text(prefix))
    if not 4 <= len(words) <= 30 or not re.search(
        r"\b(?:press|publisher|edition|volume)\b",
        prefix,
        re.IGNORECASE,
    ):
        return None
    return natural_language_text(prefix).casefold()


def _has_critical_polarity_reversal(
    source: str,
    translated: str,
    *,
    source_language: str | None,
    target_language: str | None,
) -> bool:
    """Detect only explicit, aligned reversals between ambiguous and unambiguous wording."""

    supported_codes = frozenset(TARGET_LANGUAGE_CODES.values())
    if (
        source_language not in supported_codes
        or target_language not in supported_codes
        or source_language == target_language
    ):
        return False
    source_sentences = tuple(
        sentence.strip()
        for sentence in SENTENCE_SPLIT_PATTERN.split(natural_language_text(source))
        if sentence.strip()
    )
    translated_sentences = tuple(
        sentence.strip()
        for sentence in SENTENCE_SPLIT_PATTERN.split(natural_language_text(translated))
        if sentence.strip()
    )
    if not source_sentences or len(source_sentences) != len(translated_sentences):
        return False
    for source_sentence, translated_sentence in zip(
        source_sentences,
        translated_sentences,
        strict=True,
    ):
        source_state = _ambiguity_state(source_sentence, source_language)
        translated_state = _ambiguity_state(translated_sentence, target_language)
        if (
            source_state is not None
            and translated_state is not None
            and source_state != translated_state
        ):
            return True
    return False


def _ambiguity_state(text: str, language: str) -> str | None:
    normalized = text.casefold()
    unambiguous_patterns = {
        "en": r"\b(?:unambiguous|unequivocal|unmistakable)\b|\bnot\s+ambiguous\b|"
        r"\bwithout\s+ambiguity\b",
        "es": r"\b(?:inequívoc\w*|indudable\w*)\b|\bsin\s+ambigüedad\b|"
        r"\bno\s+(?:es\s+)?ambigu\w*\b",
        "fr": r"\b(?:sans\s+ambiguïté|sans\s+équivoque|non\s+ambigu\w*|univoque\w*)\b",
        "de": r"\b(?:eindeutig\w*|unmissverständlich\w*|zweifelsfrei\w*|"
        r"nicht\s+mehrdeutig\w*)\b",
        "it": r"\b(?:inequivoc\w*|senza\s+ambiguità|non\s+ambigu\w*)\b",
        "pt": r"\b(?:inequívoc\w*|sem\s+ambiguidade|não\s+ambígu\w*)\b",
    }
    ambiguous_patterns = {
        "en": r"\b(?:ambiguous|equivocal)\b",
        "es": r"\bambigu\w*\b",
        "fr": r"\b(?:ambigu\w*|équivoque\w*)\b",
        "de": r"\b(?:mehrdeutig\w*|unklar\w*)\b",
        "it": r"\bambigu\w*\b",
        "pt": r"\bambígu\w*\b",
    }
    if pattern := unambiguous_patterns.get(language):
        if re.search(pattern, normalized):
            return "unambiguous"
    if pattern := ambiguous_patterns.get(language):
        if re.search(pattern, normalized):
            return "ambiguous"
    return None


def _unchanged_source_sentence(
    source: str,
    translated: str,
    source_language: str,
) -> str | None:
    for candidate in find_untranslated_source_sentences(source, translated, source_language):
        if _looks_like_probable_proper_name(candidate, source_language):
            continue
        if is_probable_organization_name_line(candidate):
            continue
        return candidate
    return None


def _likely_language(text: str, language: str) -> bool:
    detected = detect_language_code(
        text,
        minimum_letters=12,
        minimum_confidence=0.85,
    )
    if detected == language:
        return True
    if detected in TARGET_LANGUAGE_CODES.values():
        return False
    original_words = re.findall(r"[^\W\d_]+", text)
    words = {word.casefold() for word in original_words}
    if words & TITLE_LANGUAGE_HINTS.get(language, frozenset()):
        return True
    return len(original_words) >= 2 and any(word[:1].islower() for word in original_words)


def _is_translated_name_credit(
    source: str,
    translated: str,
    source_language: str,
) -> bool:
    """Accept a translated connector surrounded by names that must remain literal."""

    source_words = re.findall(r"[^\W\d_]+", source)
    translated_words = re.findall(r"[^\W\d_]+", translated)
    if not 3 <= len(source_words) <= 8 or len(source_words) != len(translated_words):
        return False
    changed_indexes = {
        index
        for index, (source_word, translated_word) in enumerate(
            zip(source_words, translated_words, strict=True)
        )
        if source_word.casefold() != translated_word.casefold()
    }
    if not changed_indexes:
        return False
    source_hints = TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
    for index, (source_word, translated_word) in enumerate(
        zip(source_words, translated_words, strict=True)
    ):
        if index in changed_indexes:
            if not source_word[:1].islower() or not translated_word[:1].islower():
                return False
            continue
        if source_word.casefold() in source_hints:
            return False
        if not (source_word.isupper() or source_word[:1].isupper()):
            return False
    return True


def _looks_like_web_identifier(text: str) -> bool:
    candidate = text.strip().rstrip(" \t\r\n.,;:!?")
    return WEB_IDENTIFIER_PATTERN.fullmatch(candidate) is not None


def _strip_boundary_markup(text: str) -> str:
    previous = None
    stripped = text
    while stripped != previous:
        previous = stripped
        stripped = re.sub(r"^(?:<!--[^>]*-->|</?[A-Za-z][^>]*>)\s*", "", stripped)
        stripped = re.sub(
            r"\s*(?:<!--[^>]*-->|</?[A-Za-z][^>]*>)$",
            "",
            stripped,
        )
    return stripped.strip()


def _contains_standalone_text(container: str, candidate: str) -> bool:
    position = container.find(candidate)
    while position >= 0:
        end = position + len(candidate)
        if _has_standalone_text_boundaries(container, position, end):
            return True
        position = container.find(candidate, position + 1)
    return False


def _has_standalone_text_boundaries(container: str, start: int, end: int) -> bool:
    before = container[start - 1] if start else ""
    after = container[end] if end < len(container) else ""
    before_is_boundary = not before or before.isspace() or before in ">\"'([{"
    after_is_boundary = not after or after.isspace() or after in "<\"')]}.,;:!?"
    return before_is_boundary and after_is_boundary


def _report_issue(
    segment_number: int,
    kind: TranslationIssueKind,
    message: str,
    source: str,
    translated: str,
) -> TranslationQualityIssue:
    identifier = sha256(
        f"translation\0{segment_number}\0{kind.value}\0{source}\0{translated}".encode()
    ).hexdigest()[:24]
    return TranslationQualityIssue(
        segment_number=segment_number,
        kind=kind,
        message=message,
        original_excerpt=_excerpt(source),
        translated_excerpt=_excerpt(translated),
        identifier=identifier,
    )


def _excerpt(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= MAX_REPORT_EXCERPT_CHARACTERS:
        return compact
    candidate = compact[: MAX_REPORT_EXCERPT_CHARACTERS - 1].rstrip()
    sentence_ends = tuple(re.finditer(r"[.!?](?=\s|$)", candidate))
    if sentence_ends:
        candidate = candidate[: sentence_ends[-1].end()].rstrip()
    elif " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0].rstrip()
    return f"{candidate}…"


def link_destination_spans(markdown: str) -> list[tuple[int, int, str]]:
    """Return ordered link destinations with their source positions."""
    spans: list[tuple[int, int, str]] = []
    for pattern in (INLINE_LINK_PATTERN, REFERENCE_LINK_PATTERN):
        for match in pattern.finditer(markdown):
            group = 1 if match.group(1) is not None else 2
            spans.append((match.start(group), match.end(group), match.group(group)))
    return sorted(spans)


def _validate_structure(source: str, translated: str, *, preserve_paragraphs: bool) -> None:
    if not numeric_tokens_are_conserved(source, translated):
        raise TranslationQualityError("La traducción cambió u omitió números o fechas.")
    if not _roman_reference_tokens_are_conserved(source, translated):
        raise TranslationQualityError("La traducción cambió números romanos de un título o índice.")
    if markdown_link_destinations(source) != markdown_link_destinations(translated):
        raise TranslationQualityError("La traducción cambió u omitió destinos de enlaces.")
    if Counter(INLINE_CODE_PATTERN.findall(source)) != Counter(
        INLINE_CODE_PATTERN.findall(translated)
    ):
        raise TranslationQualityError("La traducción cambió u omitió código en línea.")
    if _fenced_code_blocks(source) != _fenced_code_blocks(translated):
        raise TranslationQualityError("La traducción cambió bloques de código.")
    if markdown_heading_levels(source) != markdown_heading_levels(translated):
        raise TranslationQualityError("La traducción cambió la estructura de encabezados.")
    if markdown_table_shapes(source) != markdown_table_shapes(translated):
        raise TranslationQualityError("La traducción cambió la estructura de una tabla.")
    if _raw_table_structure(source) != _raw_table_structure(translated):
        raise TranslationQualityError("La traducción cambió la estructura de una tabla HTML.")
    if _list_structure(source) != _list_structure(translated):
        raise TranslationQualityError("La traducción cambió la estructura de las listas.")
    if _list_item_trailing_references(source) != _list_item_trailing_references(translated):
        raise TranslationQualityError(
            "La traducción desplazó referencias finales de una lista o índice."
        )
    if _blockquote_structure(source) != _blockquote_structure(translated):
        raise TranslationQualityError("La traducción cambió la estructura de las citas.")
    if len(IMAGE_PATTERN.findall(source)) != len(IMAGE_PATTERN.findall(translated)):
        raise TranslationQualityError("La traducción cambió imágenes Markdown.")
    if Counter(HTML_COMMENT_PATTERN.findall(source)) != Counter(
        HTML_COMMENT_PATTERN.findall(translated)
    ):
        raise TranslationQualityError("La traducción cambió comentarios HTML protegidos.")
    if preserve_paragraphs and _block_count(source) != _block_count(translated):
        raise TranslationQualityError("La traducción cambió la separación de párrafos.")


def _validate_content_coverage(source: str, translated: str) -> None:
    source_letters = _letter_count(natural_language_text(source))
    if source_letters < MIN_SHORT_CONTENT_COVERAGE_LETTERS:
        return
    translated_letters = _letter_count(natural_language_text(translated))
    ratio = translated_letters / source_letters
    if source_letters >= 80 and ratio < MIN_CONTENT_RATIO:
        raise TranslationQualityError("La traducción parece haber omitido parte del contenido.")
    excessive_growth = (source_letters >= 80 and ratio > MAX_CONTENT_RATIO) or (
        source_letters < 80
        and ratio > MAX_SHORT_CONTENT_RATIO
        and translated_letters - source_letters >= MIN_SHORT_CONTENT_ADDED_LETTERS
    )
    if excessive_growth:
        raise TranslationQualityError("La traducción parece haber duplicado o añadido contenido.")


def _validate_target_language(
    source: str,
    translated: str,
    source_language: str | None,
    target_language: str,
) -> None:
    natural_text = natural_language_text(translated)
    total_letters = _letter_count(natural_text)
    if total_letters < MIN_LANGUAGE_VALIDATION_LETTERS:
        if (
            source_language is not None
            and source_language != target_language
            and total_letters >= 40
            and _natural_text_similarity(source, translated) >= 0.95
            and not _looks_like_probable_proper_name(
                natural_language_text(source),
                source_language,
            )
            and not _is_translated_short_label_collection(
                source,
                translated,
                source_language=source_language,
            )
        ):
            raise TranslationQualityError("La traducción no quedó en el idioma solicitado.")
        return
    detected = detect_language_code(
        translated,
        minimum_letters=MIN_LANGUAGE_VALIDATION_LETTERS,
    )
    translated_short_labels = _is_translated_short_label_collection(
        source,
        translated,
        source_language=source_language,
    )
    translated_around_foreign_citation = (
        source_language is not None
        and source_language != target_language
        and _has_target_language_change_evidence(
            source,
            translated,
            source_language=source_language,
            target_language=target_language,
        )
    )
    if (
        detected != target_language
        and not translated_short_labels
        and not translated_around_foreign_citation
    ):
        raise TranslationQualityError("La traducción no quedó en el idioma solicitado.")
    if source_language is None or source_language == target_language:
        return

    source_residue_letters = 0
    for block in re.split(r"\n\s*\n", translated.strip()):
        block_text = natural_language_text(block)
        block_letters = _letter_count(block_text)
        if block_letters < MIN_BLOCK_LANGUAGE_LETTERS:
            continue
        block_language = detect_language_code(
            block,
            minimum_letters=MIN_BLOCK_LANGUAGE_LETTERS,
            minimum_confidence=0.90,
        )
        if block_language == source_language:
            source_residue_letters += block_letters

    if source_residue_letters >= max(240, int(total_letters * 0.20)):
        raise TranslationQualityError(
            "La traducción dejó un bloque importante en el idioma original."
        )


def _has_target_language_change_evidence(
    source: str,
    translated: str,
    *,
    source_language: str,
    target_language: str,
) -> bool:
    """Accept target prose around a conserved citation written in a third language.

    Whole-fragment language detection can be dominated by a long work title or bibliographic
    citation that legitimately remains in French, Latin or another language. Diffing words lets
    us validate the newly written prose while still rejecting sizeable unchanged runs in the
    actual source language.
    """

    source_words = re.findall(r"[^\W\d_]+", natural_language_text(source))
    translated_words = re.findall(r"[^\W\d_]+", natural_language_text(translated))
    if not source_words or not translated_words:
        return False

    changed_words: list[str] = []
    source_residue_letters = 0
    matcher = SequenceMatcher(
        None,
        [word.casefold() for word in source_words],
        [word.casefold() for word in translated_words],
        autojunk=False,
    )
    for tag, _source_start, _source_end, translated_start, translated_end in matcher.get_opcodes():
        words = translated_words[translated_start:translated_end]
        if tag != "equal":
            changed_words.extend(words)
            continue
        unchanged = " ".join(words)
        unchanged_letters = _letter_count(unchanged)
        if unchanged_letters < 40:
            continue
        unchanged_language = detect_language_code(
            unchanged,
            minimum_letters=40,
            minimum_confidence=0.80,
        )
        if unchanged_language == source_language:
            source_residue_letters += unchanged_letters

    changed_text = " ".join(changed_words)
    changed_letters = _letter_count(changed_text)
    if changed_letters < 40:
        return False
    changed_language = detect_language_code(
        changed_text,
        minimum_letters=40,
        minimum_confidence=0.80,
    )
    if changed_language != target_language:
        return False
    total_letters = _letter_count(natural_language_text(translated))
    return source_residue_letters < max(80, int(total_letters * 0.20))


def _is_translated_short_label_collection(
    source: str,
    translated: str,
    *,
    source_language: str | None = None,
) -> bool:
    blocks = [
        natural_language_text(block)
        for block in re.split(r"\n\s*\n", translated.strip())
        if block.strip()
    ]
    if len(blocks) < 4:
        blocks = [
            natural_language_text(line)
            for line in translated.splitlines()
            if natural_language_text(line)
        ]
    if len(blocks) < 4 or any(
        _letter_count(block) >= MIN_BLOCK_LANGUAGE_LETTERS for block in blocks
    ):
        return False
    if _natural_text_similarity(source, translated) < 0.90:
        return True
    source_words = {
        word.casefold() for word in re.findall(r"[^\W\d_]+", natural_language_text(source))
    }
    language_hints = (
        TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
        if source_language is not None
        else frozenset().union(*TITLE_LANGUAGE_HINTS.values())
    )
    detected_source = detect_language_code(source)
    lacks_source_language_evidence = detected_source is None or (
        source_language is not None and detected_source != source_language
    )
    return lacks_source_language_evidence and not source_words & language_hints


def _looks_like_probable_proper_name(text: str, source_language: str) -> bool:
    if is_probable_organization_name_line(text):
        return True
    words = re.findall(r"[^\W\d_]+", text)
    normalized = {word.casefold() for word in words}
    if normalized & TITLE_LANGUAGE_HINTS.get(source_language, frozenset()):
        return False
    if (
        len(words) == 5
        and detect_language_code(
            text,
            minimum_letters=12,
            minimum_confidence=0.80,
        )
        == source_language
    ):
        return False
    return 2 <= len(words) <= 5 and all(
        (word[:1].isupper() and not word.isupper()) or (len(word) == 1 and word.isupper())
        for word in words
    )


def _natural_text_similarity(source: str, translated: str) -> float:
    return SequenceMatcher(
        None,
        natural_language_text(source).casefold(),
        natural_language_text(translated).casefold(),
        autojunk=False,
    ).ratio()


def _looks_like_title(block: str) -> bool:
    stripped = block.strip()
    if not stripped or "\n" in stripped:
        return False
    if ATX_HEADING_PATTERN.match(stripped) is not None:
        return True
    letters = [character for character in stripped if character.isalpha()]
    if not letters or len(stripped.split()) > 12:
        return False
    uppercase_ratio = sum(character.isupper() for character in letters) / len(letters)
    return uppercase_ratio >= 0.80


def markdown_link_destinations(markdown: str) -> Counter[str]:
    """Return conserved link targets independently of visible labels."""

    return Counter(value for _start, _end, value in link_destination_spans(markdown))


def numeric_tokens_are_conserved(source: str, translated: str) -> bool:
    """Keep explicit values exact while allowing a written number to become digits once."""

    source_tokens = Counter(NUMBER_PATTERN.findall(source))
    translated_tokens = Counter(NUMBER_PATTERN.findall(translated))
    if source_tokens - translated_tokens:
        return False
    added_tokens = translated_tokens - source_tokens
    if not added_tokens:
        return True
    source_words = _written_number_value_counts(source)
    translated_words = _written_number_value_counts(translated)
    available_conversions = source_words - translated_words
    for token, count in added_tokens.items():
        if re.fullmatch(r"\d+", token) is None:
            return False
        value = int(token)
        if available_conversions[value] < count:
            return False
        available_conversions[value] -= count
    return True


def _written_number_value_counts(text: str) -> Counter[int]:
    values: list[int] = []
    for word in re.findall(r"[^\W\d_]+", natural_language_text(text)):
        normalized = word.casefold()
        value = WRITTEN_NUMBER_VALUES.get(normalized)
        if value is None and normalized.endswith("fold"):
            # English index and classification labels commonly use compounds
            # such as ``eightfold`` or ``elevenfold``. A faithful translation
            # may render those as an explicit digit (for example ``11
            # partes``), so account for the written value without weakening
            # conservation of any pre-existing numeric token.
            value = WRITTEN_NUMBER_VALUES.get(normalized.removesuffix("fold"))
        if value is not None:
            values.append(value)
    return Counter(values)


def _roman_reference_tokens_are_conserved(source: str, translated: str) -> bool:
    source_lines = source.splitlines()
    translated_lines = translated.splitlines()
    if len(source_lines) == len(translated_lines):
        return all(
            _roman_reference_line_is_conserved(source_line, translated_line)
            for source_line, translated_line in zip(
                source_lines,
                translated_lines,
                strict=True,
            )
        )
    return _roman_reference_line_is_conserved(source, translated)


def _roman_reference_line_is_conserved(source: str, translated: str) -> bool:
    source_tokens = Counter(TITLE_ROMAN_REFERENCE_PATTERN.findall(source))
    translated_tokens = Counter(TITLE_ROMAN_REFERENCE_PATTERN.findall(translated))
    if source_tokens - translated_tokens:
        return False
    added_tokens = translated_tokens - source_tokens
    if not added_tokens:
        return True
    source_ordinals = _written_ordinal_value_counts(source)
    translated_ordinals = _written_ordinal_value_counts(translated)
    available_conversions = source_ordinals - translated_ordinals
    for token, count in added_tokens.items():
        value = _roman_numeral_value(token)
        if value is None or available_conversions[value] < count:
            return False
        available_conversions[value] -= count
    return True


def _written_ordinal_value_counts(text: str) -> Counter[int]:
    return Counter(
        WRITTEN_ORDINAL_VALUES[word.casefold()]
        for word in re.findall(r"[^\W\d_]+", natural_language_text(text))
        if word.casefold() in WRITTEN_ORDINAL_VALUES
    )


def _roman_numeral_value(token: str) -> int | None:
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for character in reversed(token.upper()):
        value = values.get(character)
        if value is None:
            return None
        if value < previous:
            total -= value
        else:
            total += value
            previous = value
    return total or None


def markdown_heading_levels(markdown: str) -> tuple[int, ...]:
    """Return the ordered ATX heading hierarchy."""

    return tuple(len(match.group(1)) for match in ATX_HEADING_PATTERN.finditer(markdown))


def markdown_table_shapes(markdown: str) -> tuple[tuple[int, int], ...]:
    """Return row and column counts for every Markdown table."""

    lines = markdown.splitlines()
    shapes: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if TABLE_DIVIDER_PATTERN.fullmatch(line) is None:
            continue
        start = index - 1
        while start >= 0 and _is_table_row(lines[start]):
            start -= 1
        end = index + 1
        while end < len(lines) and _is_table_row(lines[end]):
            end += 1
        shapes.append((end - start - 1, _table_column_count(line)))
    return tuple(shapes)


def _raw_table_structure(markdown: str) -> tuple[tuple[str, str, str], ...]:
    """Return ordered raw-table tags, preserving attributes but normalizing harmless syntax."""

    structure: list[tuple[str, str, str]] = []
    for match in RAW_TABLE_STRUCTURE_TAG_PATTERN.finditer(markdown):
        closing = "/" if match.group(1) else ""
        name = match.group(2).casefold()
        suffix = re.sub(r"\s+", " ", match.group(3).strip())
        if name == "br" and suffix == "/":
            suffix = ""
        structure.append((closing, name, suffix.casefold()))
    return tuple(structure)


def _is_table_row(line: str) -> bool:
    return bool(line.strip()) and UNESCAPED_PIPE_PATTERN.search(line) is not None


def _table_column_count(line: str) -> int:
    content = line.strip()
    if content.startswith("|"):
        content = content[1:]
    if content.endswith("|") and not content.endswith(r"\|"):
        content = content[:-1]
    return len(UNESCAPED_PIPE_PATTERN.split(content))


def _fenced_code_blocks(markdown: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] | None = None
    fence_character: str | None = None
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        fence = FENCE_PATTERN.match(line)
        if current is None:
            if fence is None:
                continue
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            current = [line]
            continue
        current.append(line)
        if fence is not None:
            marker = fence.group(1)
            if marker[0] == fence_character and len(marker) >= fence_length:
                blocks.append("".join(current))
                current = None
                fence_character = None
                fence_length = 0
    if current is not None:
        blocks.append("".join(current))
    return tuple(blocks)


def _list_structure(markdown: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (
            len(match.group(1).expandtabs(4)),
            "ordered" if match.group(2)[0].isdigit() else "unordered",
        )
        for match in LIST_ITEM_PATTERN.finditer(markdown)
    )


def _list_item_trailing_references(markdown: str) -> tuple[str | None, ...]:
    """Keep page-like references attached to the end of their list entry."""

    references: list[str | None] = []
    for line in markdown.splitlines():
        item = LIST_ITEM_LINE_PATTERN.match(line)
        if item is None:
            continue
        reference = TRAILING_LIST_REFERENCE_PATTERN.search(item.group("content"))
        references.append(reference.group("reference").casefold() if reference else None)
    return tuple(references)


def _blockquote_structure(markdown: str) -> tuple[int, ...]:
    return tuple(
        len(match.group(1).replace(" ", "")) for match in BLOCKQUOTE_PATTERN.finditer(markdown)
    )


def _block_count(markdown: str) -> int:
    return len([block for block in re.split(r"\n\s*\n", markdown.strip()) if block.strip()])


def _letter_count(text: str) -> int:
    return sum(character.isalpha() for character in text)
