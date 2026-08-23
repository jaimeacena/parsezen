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
            "days",
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
            "acknowledgement",
            "acknowledgements",
            "acknowledgment",
            "acknowledgments",
            "astrology",
            "aquarius",
            "appendices",
            "cancer",
            "capricorn",
            "condition",
            "category",
            "character",
            "calculating",
            "choose",
            "benefic",
            "bodyguard",
            "bodyguards",
            "bad",
            "chart",
            "god",
            "goddess",
            "good",
            "exercise",
            "energized",
            "egyptian",
            "english",
            "edition",
            "editions",
            "expense",
            "factors",
            "favorability",
            "fortune",
            "greek",
            "history",
            "gemini",
            "judgment",
            "index",
            "illustration",
            "illustrations",
            "lot",
            "moon",
            "money",
            "meaning",
            "amount",
            "nodes",
            "planets",
            "planetary",
            "primary",
            "phases",
            "pisces",
            "quadrant",
            "readings",
            "bibliography",
            "carpet",
            "reception",
            "rejoicing",
            "releasing",
            "sagittarius",
            "scorpio",
            "scorpion",
            "source",
            "setting",
            "sign",
            "spirit",
            "spending",
            "subterranean",
            "summary",
            "sect",
            "striking",
            "taurus",
            "your",
            "dream",
            "down",
            "earth",
            "self",
            "success",
            "team",
            "treat",
            "level",
            "overview",
            "rulerships",
            "triplicity",
            "triplicities",
            "angularity",
            "angular",
            "aspect",
            "aspects",
            "cadent",
            "determining",
            "directions",
            "doctrine",
            "doctrines",
            "hermetic",
            "historical",
            "identifying",
            "interpretation",
            "interpreting",
            "introducing",
            "light",
            "idle",
            "lord",
            "lords",
            "location",
            "lots",
            "motion",
            "midheaven",
            "other",
            "phasis",
            "retrograde",
            "themes",
            "timing",
            "trine",
            "traditional",
            "triad",
            "triads",
            "analyzing",
            "comparing",
            "contrasting",
            "dignified",
            "formulas",
            "witnessing",
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
        "an introduction": "una introducción",
        "an overview of procedures": "panorama de los procedimientos",
        "an overview of timing methods": "panorama de los métodos de cronología",
        "angularity": "angularidad",
        "angularity, favorability, testimony": "angularidad, favorabilidad y testimonio",
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
        "cadent triplicity lords of the light sect": (
            "señores cadentes de la triplicidad de la luminaria de la secta"
        ),
        "cadent triplicity lords of the sect light": (
            "señores cadentes de la triplicidad de la luminaria de la secta"
        ),
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
        "down to earth": "con los pies en la tierra",
        "exercise": "ejercicio",
        "chart one": "carta uno",
        "chart two": "carta dos",
        "contents": "índice",
        "cusp": "cúspide",
        "cusps": "cúspides",
        "delineating a planet in a house": "delineación de un planeta en una casa",
        "delineating a planet with its condition in a house": (
            "delineación de un planeta con su condición en una casa"
        ),
        "delineating planetary meaning": "interpretación del significado planetario",
        "delineations for chart one": "interpretaciones de la carta uno",
        "delineations for chart two": "interpretaciones de la carta dos",
        "placing the planets in the houses": "colocación de los planetas en las casas",
        "the planet's domicile lord": "el regente domiciliario del planeta",
        "the planet’s domicile lord": "el regente domiciliario del planeta",
        "domicile lord": "regente domiciliario",
        "domicile lords": "regentes domiciliarios",
        "condition and location of the domicile lord": (
            "condición y ubicación del regente domiciliario"
        ),
        "a planet's assistance from its domicile lord": (
            "ayuda de un planeta procedente de su regente domiciliario"
        ),
        "a planet’s assistance from its domicile lord": (
            "ayuda de un planeta procedente de su regente domiciliario"
        ),
        "a planet's assistance": "ayuda de un planeta",
        "a planet’s assistance": "ayuda de un planeta",
        "from its domicile lord": "de su regente domiciliario",
        "its domicile lord": "su regente domiciliario",
        "the relative angularity of the houses": ("la angularidad relativa de las casas"),
        "relative angularity of the houses": "angularidad relativa de las casas",
        "the relative favorability of the houses": ("la favorabilidad relativa de las casas"),
        "relative favorability of the houses": "favorabilidad relativa de las casas",
        "angularity and favorability in quadrant and equal house divisions": (
            "angularidad y favorabilidad en las divisiones de casas por cuadrantes e iguales"
        ),
        "the houses and chronological ages": "las casas y las edades cronológicas",
        "according to zodiacal sign": "según el signo zodiacal",
        "favorability": "favorabilidad",
        "historical overview": "panorama histórico",
        "historical overview of aspect doctrine": (
            "panorama histórico de la doctrina de los aspectos"
        ),
        "historical overview of aspect doctrines": (
            "panorama histórico de las doctrinas de los aspectos"
        ),
        "hermetic lots": "lotes herméticos",
        "glossary": "glosario",
        "goddess": "diosa",
        "god": "dios",
        "setting": "ocaso",
        "midheaven": "medio cielo",
        "good spirit": "buen espíritu",
        "bad spirit": "mal espíritu",
        "subterranean place": "Lugar subterráneo",
        "idle": "Inactivo",
        "introduction": "introducción",
        "introducing lots": "presentación de los lotes",
        "interpreting phasis": "interpretación de la fasis",
        "interpreting retrograde motion": "interpretación del movimiento retrógrado",
        "determining phasis": "determinación de la fasis",
        "angular triads": "tríadas angulares",
        "lot of fortune": "lote de la fortuna",
        "length of life": "duración de la vida",
        "lord": "señor",
        "lords": "señores",
        "mainstream": "ámbito general",
        "malefic": "maléfico",
        "maltreatment": "maltrato",
        "maltreatment by striking with a ray": "maltrato al golpear con un rayo",
        "planetary phases": "fases planetarias",
        "planetary reception": "recepción planetaria",
        "planetary domiciles": "domicilios planetarios",
        "primary source readings": "lecturas de fuentes primarias",
        "readings": "lecturas",
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
        "step one": "paso uno",
        "step two": "paso dos",
        "step three": "paso tres",
        "step four": "paso cuatro",
        "step four: the condition and location of the domicile lord": (
            "paso cuatro: condición y ubicación del regente domiciliario"
        ),
        "step five": "paso cinco",
        "step six": "paso seis",
        "step six: location and topics of the lord": ("paso seis: ubicación y ámbitos del regente"),
        "spear-bearing bodyguards": "guardaespaldas portadores de lanza",
        "striking with a ray": "golpear con un rayo",
        "summary and source readings": "resumen y lecturas de fuentes",
        "steering the ship of life": "llevar el timón de la vida",
        "synodic cycle": "ciclo sinódico",
        "symbols and abbreviations used in this book": (
            "símbolos y abreviaturas utilizados en este libro"
        ),
        "part one": "parte uno",
        "part two": "parte dos",
        "part three": "parte tres",
        "part four": "parte cuatro",
        "part five": "parte cinco",
        "part seven": "parte siete",
        "part eight": "parte ocho",
        "part nine": "parte nueve",
        "part ten": "parte diez",
        "part eleven": "parte once",
        "part twelve": "parte doce",
        "part six: the art of judgment": "parte seis: el arte del juicio",
        "part six": "parte seis",
        "part": "parte",
        "ruler": "regente",
        "rulers": "regentes",
        "rulers of the nativity": "regentes de la natividad",
        "source reading": "lectura de fuentes",
        "the art of judgment": "el arte del juicio",
        "the bound lord": "el señor de los términos",
        "the bound lords": "los señores de los términos",
        "the first house": "la primera casa",
        "first house": "Primera casa",
        "the second house": "la segunda casa",
        "second house": "Segunda casa",
        "the third house": "la tercera casa",
        "third house": "Tercera casa",
        "the fourth house": "la cuarta casa",
        "fourth house": "Cuarta casa",
        "the fifth house": "la quinta casa",
        "fifth house": "Quinta casa",
        "the sixth house": "la sexta casa",
        "sixth house": "Sexta casa",
        "the seventh house": "la séptima casa",
        "seventh house": "Séptima casa",
        "the eighth house": "la octava casa",
        "eighth house": "Octava casa",
        "the ninth house": "la novena casa",
        "ninth house": "Novena casa",
        "the tenth house": "la décima casa",
        "tenth house": "Décima casa",
        "the eleventh house": "la undécima casa",
        "eleventh house": "Undécima casa",
        "the twelfth house": "la duodécima casa",
        "twelfth house": "Duodécima casa",
        "the science of judgment": "la ciencia del juicio",
        "the seven hermetic lots": "los siete lotes herméticos",
        "the domicile lord of the ascendant": "el regente domiciliario del Ascendente",
        "the domicile lord of the ascendant and the vital times": (
            "el regente domiciliario del Ascendente y los períodos vitales"
        ),
        "the domicile lord of fortune": "el regente domiciliario de la fortuna",
        "domicile lord of fortune": "regente domiciliario de la fortuna",
        "the lot of fortune and the lord of fortune": (
            "el lote de la fortuna y el regente de la fortuna"
        ),
        "the lot of fortune and the domicile lord of fortune": (
            "el lote de la fortuna y el regente domiciliario de la fortuna"
        ),
        "the lots": "los lotes",
        "the synodic cycle": "el ciclo sinódico",
        "and the moon under the bonds": "y la Luna bajo los lazos",
        "the triplicity lord": "el señor de la triplicidad",
        "the triplicity lords": "los señores de la triplicidad",
        "the ultimate rulers of the chart": "los regentes finales de la carta",
        "triplicity lords of the sect light": (
            "señores de la triplicidad de la luminaria de la secta"
        ),
        "triplicity": "triplicidad",
        "triplicities": "triplicidades",
        "triplicity lord": "señor de la triplicidad",
        "triplicity lords": "señores de la triplicidad",
        "triplicity rulerships": "regencias por triplicidad",
        "trine": "trígono",
        "three types of bodyguards": "tres tipos de guardaespaldas",
        "testimony": "testimonio",
        "ultimate rulers of the chart": "regentes finales de la carta",
        "sign rulerships": "regencias de los signos",
        "traditional sign rulerships": "regencias tradicionales de los signos",
        "rulerships of the signs": "regencias de los signos",
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

_ESTABLISHED_COMPACT_RESIDUAL_TOKEN_TRANSLATIONS = {
    ("en", "es"): {
        # These tokens are only localized when they belong to a longer established
        # expression found in the same compact label. They must not rewrite ordinary
        # prose such as "a lot" or an unrelated cardinal.
        "lots": "lotes",
        "six": "seis",
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

    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    normalized = re.sub(r"\s+", " ", source_term.strip()).casefold()
    translated = translations.get(normalized)
    if translated is not None:
        return translated
    collapsed = re.sub(r"\s+", "", normalized)
    matches = {
        target for source, target in translations.items() if re.sub(r"\s+", "", source) == collapsed
    }
    return next(iter(matches)) if len(matches) == 1 else None


def established_compact_label_translation(
    source_label: str,
    source_language: str,
    target_language: str,
) -> str | None:
    """Compose a complete compact label only from unambiguous curated terms.

    This intentionally refuses partial prose: every characteristic source-language
    word must disappear after composition. It is therefore safe for deterministic
    title and generated-TOC repair while longer sentences still go through the model.
    """

    exact = established_term_translation(source_label, source_language, target_language)
    if exact is not None:
        return exact
    composed = replace_established_compact_term_residues(
        source_label,
        source_label,
        source_language,
        target_language,
    )
    if composed == source_label or _has_title_language_hint(composed, source_language):
        return None
    return composed


def is_probable_third_language_compact_value(
    text: str,
    *,
    source_language: str,
    target_language: str,
) -> bool:
    """Recognize a short foreign term embedded in a table of another language.

    Requiring at least two words (or a diacritic) avoids preserving ordinary
    one-word English labels when short-text language detection is uncertain.
    """

    natural = natural_language_text(text).strip()
    words = re.findall(r"[^\W\d_]+", natural)
    normalized_words = {_normalized_word(word) for word in words}
    if (
        not 1 <= len(words) <= 5
        or _letter_count(natural) < 5
        or (
            all(ord(character) < 128 for character in natural)
            and (len(words) == 1 or any(word[:1].isupper() for word in words))
        )
        or normalized_words & TITLE_LANGUAGE_HINTS.get(source_language, frozenset())
        or contains_established_translation_candidate(natural)
    ):
        return False
    detected = detect_language_code(
        natural,
        minimum_letters=4,
        minimum_confidence=0.50,
    )
    return detected not in {None, source_language, target_language}


def _established_term_pattern(source_term: str) -> str:
    """Match a curated phrase despite harmless PDF whitespace variation."""

    words = re.split(r"\s+", source_term.strip())
    whitespace = r"\s*"
    word_patterns = [re.escape(word).replace("'", "['’]?").replace("’", "['’]?") for word in words]
    return rf"(?<!\w){whitespace.join(word_patterns)}(?!\w)"


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
        if re.search(_established_term_pattern(term), source, re.IGNORECASE):
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
        re.search(_established_term_pattern(term), source, re.IGNORECASE) for term in candidates
    )


_SOURCE_LANGUAGE_WORD_RESIDUE_HINTS = {
    "en": frozenset(
        {
            "although",
            "because",
            "between",
            "during",
            "however",
            "mainstream",
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
NUMERIC_HTML_ENTITY_PATTERN = re.compile(r"&#(?:x[0-9a-f]+|\d+);", re.IGNORECASE)
WRITTEN_NUMBER_VALUES = {
    "both": 2,
    "ambos": 2,
    "ambas": 2,
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
WRITTEN_NUMBER_VALUES_BY_LANGUAGE = {
    "en": {
        "both": 2,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    },
    "es": {
        "ambos": 2,
        "ambas": 2,
        "dos": 2,
        "tres": 3,
        "cuatro": 4,
        "cinco": 5,
        "seis": 6,
        "siete": 7,
        "ocho": 8,
        "nueve": 9,
        "diez": 10,
        "once": 11,
        "doce": 12,
        "trece": 13,
        "catorce": 14,
        "quince": 15,
        "dieciséis": 16,
        "diecisiete": 17,
        "dieciocho": 18,
        "diecinueve": 19,
    },
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
    review_segment_numbers: tuple[int, ...] = ()
    requires_full_review: bool = False

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
    except ImportError:
        return None

    try:
        DetectorFactory.seed = 0
        candidates = detect_langs(sample)
    except LangDetectException:
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
    if {source_language, target_language} == {"en", "es"} and not (
        _written_number_meaning_is_conserved(
            source,
            translated,
            source_language=source_language,
            target_language=target_language,
        )
    ):
        raise TranslationQualityError(
            "La traducción cambió el valor de una cantidad escrita con palabras."
        )
    if (source_language, target_language) == ("en", "es") and not (
        _english_spanish_duration_units_are_conserved(source, translated)
    ):
        raise TranslationQualityError(
            "La traducción no mantuvo la concordancia de una duración numérica."
        )
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
    review_segment_numbers: set[int] = set()
    requires_full_review = False

    def add_issue(
        issue: TranslationQualityIssue,
        *,
        review_segment_number: int | None = None,
    ) -> None:
        nonlocal requires_full_review, total_issues
        total_issues += 1
        issue_totals[issue.kind] += 1
        if review_segment_number is not None:
            review_segment_numbers.add(review_segment_number)
        elif len(issues) >= MAX_REPORT_ISSUES:
            # Detailed excerpts remain bounded, but an unlocatable hidden issue
            # must increase review coverage instead of disappearing silently.
            requires_full_review = True
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
            add_issue(issue, review_segment_number=index)

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
        review_segment_numbers=tuple(sorted(review_segment_numbers)),
        requires_full_review=requires_full_review,
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
    other_language_hints = {
        _normalized_word(hint)
        for other_language, language_hints in TITLE_LANGUAGE_HINTS.items()
        if other_language != language
        for hint in language_hints
    }
    return any(
        _is_joined_title_language_hint(word, hints)
        or _is_mixed_joined_title_language_hint(word, hints, other_language_hints)
        for word in words
    )


def _is_joined_title_language_hint(word: str, hints: set[str]) -> bool:
    """Accept only complete OCR-joined hint sequences, not target-language derivatives."""

    if len(word) < 5:
        return False
    candidates = tuple(sorted((hint for hint in hints if len(hint) >= 2), key=len, reverse=True))
    segment_counts: dict[int, int] = {0: 0}
    for position in range(len(word)):
        count = segment_counts.get(position)
        if count is None:
            continue
        for hint in candidates:
            if not word.startswith(hint, position):
                continue
            end = position + len(hint)
            segment_counts[end] = max(segment_counts.get(end, 0), count + 1)
    return segment_counts.get(len(word), 0) >= 2


def _is_mixed_joined_title_language_hint(
    word: str,
    source_hints: set[str],
    other_language_hints: set[str],
) -> bool:
    """Recognize a source residue attached to complete words from another language."""

    for hint in sorted((value for value in source_hints if len(value) >= 4), key=len, reverse=True):
        remainders: list[str] = []
        if word.startswith(hint) and len(word) > len(hint):
            remainders.append(word[len(hint) :])
        if word.endswith(hint) and len(word) > len(hint):
            remainders.append(word[: -len(hint)])
        for remainder in remainders:
            # Two-letter endings are commonly target-language inflections
            # (for example ``aspect`` -> ``aspectos``), not a second word
            # joined by PDF extraction.
            if len(remainder) >= 3 and (
                remainder in other_language_hints
                or _is_joined_title_language_hint(
                    remainder,
                    other_language_hints,
                )
            ):
                return True
    return False


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
        source_language == "en"
        and target_language != "en"
        and re.search(r"(?i)(?<!\w)yo['’](?:[ \t]+self)?(?!\w)", source_natural)
        and re.search(r"(?i)(?<!\w)yo['’](?:[ \t]+self)?(?!\w)", translated_natural)
    ):
        return _report_issue(
            segment_number,
            TranslationIssueKind.SOURCE_TEXT,
            "Una contracción posesiva inglesa conserva su forma original.",
            source,
            translated,
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
    return replace_established_term_residues(
        source,
        translated,
        source_language,
        target_language,
    )


def replace_established_term_residues(
    source: str,
    translated: str,
    source_language: str,
    target_language: str,
) -> str:
    """Localize copied conventional terms in an already aligned compact label."""

    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    result = translated
    for source_term, target_term in sorted(
        translations.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if source_term == target_term.casefold():
            continue
        copied_source = re.compile(_established_term_pattern(source_term), re.IGNORECASE)
        collapsed_source_term = re.sub(r"\s+", "", source_term)
        collapsed_source = re.compile(
            rf"(?<!\w){re.escape(collapsed_source_term)}(?!\w)",
            re.IGNORECASE,
        )
        if copied_source.search(source) is None and (
            collapsed_source_term == source_term or collapsed_source.search(source) is None
        ):
            continue

        def replacement(match: re.Match[str], term: str = target_term) -> str:
            if match.group().isupper():
                return term.upper()
            if match.group().islower():
                return term.lower()
            return term

        if copied_source.search(result) is not None:
            result = copied_source.sub(replacement, result)
        if collapsed_source_term != source_term and collapsed_source.search(result) is not None:
            result = collapsed_source.sub(replacement, result)
        collapsed_target = re.sub(r"\s+", "", target_term)
        if collapsed_target != target_term:

            def collapsed_replacement(
                match: re.Match[str],
                term: str = target_term,
            ) -> str:
                if match.group().isupper():
                    return term.upper()
                if match.group().islower():
                    return term.lower()
                if match.group()[:1].isupper():
                    return f"{term[:1].upper()}{term[1:]}"
                return term

            result = re.sub(
                rf"(?<!\w){re.escape(collapsed_target)}(?!\w)",
                collapsed_replacement,
                result,
                flags=re.IGNORECASE,
            )
    return result


def replace_established_compact_term_residues(
    source: str,
    translated: str,
    source_language: str,
    target_language: str,
) -> str:
    """Localize partial residues only when backed by a known multiword label term."""

    result = replace_established_term_residues(
        source,
        translated,
        source_language,
        target_language,
    )
    translations = _ESTABLISHED_TITLE_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    residual_tokens = _ESTABLISHED_COMPACT_RESIDUAL_TOKEN_TRANSLATIONS.get(
        (source_language, target_language),
        {},
    )
    for source_token, target_token in residual_tokens.items():
        supporting_terms = (
            term
            for term in translations
            if " " in term
            and re.search(rf"(?<!\w){re.escape(source_token)}(?!\w)", term, re.IGNORECASE)
        )
        if not any(
            re.search(_established_term_pattern(term), source, re.IGNORECASE)
            for term in supporting_terms
        ):
            continue
        pattern = re.compile(
            rf"(?<!\w){re.escape(source_token)}(?!\w)",
            re.IGNORECASE,
        )

        def replacement(match: re.Match[str], term: str = target_token) -> str:
            if match.group().isupper():
                return term.upper()
            return term.lower()

        result = pattern.sub(replacement, result)
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
    if len(MARKDOWN_LINK_PATTERN.findall(source)) != len(MARKDOWN_LINK_PATTERN.findall(translated)):
        raise TranslationQualityError("La traducción dejó un enlace Markdown incompleto.")
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
    if html_tag_structure(source) != html_tag_structure(translated):
        raise TranslationQualityError("La traducción añadió o cambió etiquetas HTML.")
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
    if _near_duplicate_long_block_count(translated) > _near_duplicate_long_block_count(source):
        raise TranslationQualityError(
            "La traducción parece haber repetido un párrafo que no estaba duplicado."
        )
    source_blocks = tuple(
        natural_language_text(block)
        for block in re.split(r"\n\s*\n", source.strip())
        if natural_language_text(block)
    )
    translated_blocks = tuple(
        natural_language_text(block)
        for block in re.split(r"\n\s*\n", translated.strip())
        if natural_language_text(block)
    )
    if len(source_blocks) != len(translated_blocks):
        return
    for source_block, translated_block in zip(
        source_blocks,
        translated_blocks,
        strict=True,
    ):
        source_block_letters = _letter_count(source_block)
        if source_block_letters < 80:
            continue
        if _letter_count(translated_block) / source_block_letters > MAX_CONTENT_RATIO:
            raise TranslationQualityError(
                "La traducción parece haber duplicado contenido dentro de un párrafo."
            )


def _near_duplicate_long_block_count(markdown: str) -> int:
    """Count nearby near-duplicate prose blocks with bounded comparison work."""

    blocks = [
        re.sub(r"\s+", " ", natural_language_text(block)).strip().casefold()
        for block in re.split(r"\n\s*\n", markdown.strip())
    ]
    long_blocks = [block for block in blocks if _letter_count(block) >= 180]
    duplicates = 0
    for index, current in enumerate(long_blocks):
        for previous in long_blocks[max(0, index - 12) : index]:
            length_ratio = min(len(previous), len(current)) / max(len(previous), len(current))
            if length_ratio < 0.70:
                continue
            if SequenceMatcher(None, previous, current, autojunk=False).ratio() >= 0.88:
                duplicates += 1
                break
    return duplicates


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

    source_tokens = numeric_token_counts(source)
    translated_tokens = numeric_token_counts(translated)
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


def numeric_token_counts(text: str) -> Counter[str]:
    """Count reader-visible numeric tokens, excluding numeric HTML character entities."""

    visible = NUMERIC_HTML_ENTITY_PATTERN.sub("", text)
    return Counter(NUMBER_PATTERN.findall(visible))


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


def _written_number_meaning_is_conserved(
    source: str,
    translated: str,
    *,
    source_language: str,
    target_language: str,
) -> bool:
    """Conserve unambiguous small cardinal values across English and Spanish.

    Values from two through nineteen avoid the article ambiguity of ``one`` and
    the compound spelling ambiguity of larger numbers. A model may still render
    one of these values as a digit exactly once.
    """

    if not _looks_like_title(source):
        return True
    if natural_language_text(source).casefold() == natural_language_text(translated).casefold():
        # Unchanged publisher names and other protected labels are handled by
        # the residual-language review; they are not evidence of a changed
        # quantity by themselves.
        return True

    def bounded_written(value: str, language: str) -> Counter[int]:
        mapping = WRITTEN_NUMBER_VALUES_BY_LANGUAGE.get(language, {})
        return Counter(
            mapping[word.casefold()]
            for word in re.findall(r"[^\W\d_]+", natural_language_text(value))
            if word.casefold() in mapping
        )

    source_values = bounded_written(source, source_language)
    translated_values = bounded_written(translated, target_language)
    if not source_values and not translated_values:
        return True
    source_digits = Counter(
        int(token)
        for token in numeric_token_counts(source).elements()
        if token.isdigit() and 2 <= int(token) <= 19
    )
    translated_digits = Counter(
        int(token)
        for token in numeric_token_counts(translated).elements()
        if token.isdigit() and 2 <= int(token) <= 19
    )
    converted_to_digits = translated_digits - source_digits
    return source_values == translated_values + converted_to_digits


def _english_spanish_duration_units_are_conserved(source: str, translated: str) -> bool:
    """Require a numeric English duration to retain its value and Spanish agreement."""

    if not _looks_like_title(source):
        return True

    def visible_text(value: str) -> str:
        text = INLINE_CODE_PATTERN.sub(" ", value)
        text = MARKDOWN_LINK_PATTERN.sub(lambda match: match.group(1), text)
        text = HTML_TAG_PATTERN.sub(" ", text)
        text = RAW_URL_PATTERN.sub(" ", text)
        text = re.sub(r"[\\`*_~#>\[\]{}|]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    source = visible_text(source)
    translated = visible_text(translated)

    units = {
        "day": ("día", "días"),
        "hour": ("hora", "horas"),
        "minute": ("minuto", "minutos"),
        "month": ("mes", "meses"),
        "week": ("semana", "semanas"),
        "year": ("año", "años"),
    }
    requirements: Counter[tuple[str, str, str]] = Counter()
    for match in re.finditer(
        r"(?<!\w)(?P<number>\d+)[ \t-]+(?P<unit>day|hour|minute|month|week|year)s?\b",
        source,
        re.IGNORECASE,
    ):
        number = match.group("number")
        english_unit = match.group("unit").casefold()
        singular, plural = units[english_unit]
        requirements[(number, singular if int(number) == 1 else plural, english_unit)] += 1
    for (number, unit, english_unit), required_count in requirements.items():
        translated_pattern = re.compile(
            rf"(?<!\d){re.escape(number)}[ \t-]+{re.escape(unit)}\b",
            re.IGNORECASE,
        )
        residual_pattern = re.compile(
            rf"(?<!\d){re.escape(number)}[ \t-]+{re.escape(english_unit)}s?\b",
            re.IGNORECASE,
        )
        if (
            len(translated_pattern.findall(translated)) + len(residual_pattern.findall(translated))
            < required_count
        ):
            return False
    return True


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


def html_tag_structure(markdown: str) -> tuple[tuple[str, str, str], ...]:
    """Return ordered raw HTML tags while normalizing harmless ``br`` syntax."""

    structure: list[tuple[str, str, str]] = []
    for raw_tag in HTML_TAG_PATTERN.findall(markdown):
        match = re.fullmatch(
            r"<(?P<closing>/)?(?P<name>[A-Za-z][^\s/>]*)(?P<suffix>[^>]*)>",
            raw_tag,
        )
        if match is None:
            continue
        name = match.group("name").casefold()
        suffix = re.sub(r"\s+", " ", match.group("suffix").strip())
        if name == "br" and suffix == "/":
            suffix = ""
        structure.append(("/" if match.group("closing") else "", name, suffix))
    return tuple(structure)


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
