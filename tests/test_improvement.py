from __future__ import annotations

import json
import re

import httpx
import pytest

import parsezen.improvement as improvement_module
from parsezen.cancellation import CancellationToken
from parsezen.errors import (
    ImprovementError,
    LocalModelUnavailableError,
    ProcessingCancelledError,
    SettingsError,
)
from parsezen.improvement import (
    MAX_CONSERVED_VALUES_PER_CHUNK,
    MAX_DOCUMENT_CHARACTERS,
    MAX_INPUT_CHARACTERS,
    MAX_TRANSLATION_CHUNK_CHARACTERS,
    MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK,
    NUMBER_PATTERN,
    ImprovementMode,
    build_instructions,
    improve_markdown,
)
from parsezen.settings import AppSettings

LOCAL_SETTINGS = AppSettings(
    model="parsezen-local",
    context_window=8_192,
    timeout_seconds=30,
)


def test_validated_chunks_resume_without_calling_ollama_again() -> None:
    source = "# Original\n\nThis paragraph keeps the same Markdown structure and number 42."
    cache: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"].replace("Original", "Improved")
        return httpx.Response(200, json={"message": {"content": content}})

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    first = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A validated checkpoint must avoid a repeated Ollama request.")

    resumed = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
    )

    assert first == resumed
    assert cache


def test_legacy_position_based_checkpoint_is_validated_and_migrated() -> None:
    source = "This translated paragraph contains enough meaningful natural language."
    translated = "Este párrafo traducido contiene suficiente lenguaje natural significativo."
    legacy_key = improvement_module._legacy_chunk_checkpoint_key(
        ImprovementMode.TRANSLATE,
        1,
        source,
    )
    cache = {legacy_key: translated}

    def reject_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Un checkpoint antiguo válido debe evitar una petición repetida.")

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(reject_network),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    assert result == translated
    assert cache[improvement_module._chunk_checkpoint_key(ImprovementMode.TRANSLATE, source)] == (
        translated
    )


def test_unchanged_optional_review_is_not_cached_as_a_validated_change() -> None:
    source = "This paragraph is already correct and should remain unchanged."
    cache: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={"message": {"content": payload["messages"][1]["content"]}},
        )

    def save_checkpoint(key: str, value: str) -> bool:
        cache[key] = value
        return True

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        load_checkpoint=cache.get,
        save_checkpoint=save_checkpoint,
    )

    assert result == source
    assert cache == {}


def test_plain_text_instructions_do_not_request_markdown_structure() -> None:
    instructions = build_instructions(
        ImprovementMode.CLEAN,
        plain_text=True,
    )

    assert "texto sin formato" in instructions
    assert "No añadas sintaxis Markdown" in instructions


def test_validation_requires_internal_structure_markers() -> None:
    marker = "<!-- PZDOCDOCX000001XZQ -->"

    with pytest.raises(ImprovementError, match="marcadores internos"):
        improvement_module._validate_output(f"Antes\n{marker}\nDespués", "Antes\nDespués")


@pytest.mark.parametrize(
    ("mode", "language", "expected"),
    [
        (ImprovementMode.CLEAN, None, "No traduzcas"),
        (ImprovementMode.TRANSLATE, "Español", "idioma Español"),
        (ImprovementMode.CLEAN_AND_TRANSLATE, "Francés", "única transformación"),
    ],
)
def test_prompts_are_conservative_for_every_mode(
    mode: ImprovementMode,
    language: str | None,
    expected: str,
) -> None:
    instructions = build_instructions(mode, language)

    assert expected in instructions
    assert "No resumas" in instructions
    assert "No añadas explicaciones" in instructions
    assert "números, fechas, destinos de enlaces" in instructions
    assert "Devuelve únicamente el Markdown" in instructions


def test_review_prompts_are_scoped_and_structure_requires_markdown() -> None:
    content = build_instructions(ImprovementMode.REVIEW_CONTENT)
    structure = build_instructions(ImprovementMode.REVIEW_STRUCTURE)

    assert "ruido inequívoco" in content
    assert "no cambies la jerarquía" in content
    assert "No resumas" in content
    assert "No añadas explicaciones" in content
    assert "Conserva exactamente" in structure
    assert "no corrijas, traduzcas, elimines ni añadas" in structure
    assert "No resumas" in structure
    with pytest.raises(ImprovementError, match="formato Markdown"):
        build_instructions(ImprovementMode.REVIEW_STRUCTURE, plain_text=True)


def test_optional_review_preserves_the_document_if_final_reassembly_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "# Chapter\n\nThe complete document must remain recoverable."
    original_validate = improvement_module._validate_mode_output

    def validate_except_final(
        original: str,
        improved: str,
        mode: ImprovementMode,
        *,
        max_characters: int = improvement_module.MAX_OUTPUT_CHARACTERS,
    ) -> None:
        if max_characters == improvement_module.MAX_DOCUMENT_OUTPUT_CHARACTERS:
            raise ImprovementError("final reassembly mismatch")
        original_validate(original, improved, mode, max_characters=max_characters)

    monkeypatch.setattr(improvement_module, "_validate_mode_output", validate_except_final)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": payload["messages"][1]["content"]}})

    assert (
        improve_markdown(
            source,
            ImprovementMode.REVIEW_STRUCTURE,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )
        == source
    )


def test_optional_review_keeps_safe_chunks_when_only_their_combination_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "First paragraph contains teh first bounded correction and enough words to split.\n\n"
        "Second paragraph must recieve another bounded correction without losing content."
    )
    monkeypatch.setattr(improvement_module, "MAX_INPUT_CHARACTERS", 90)
    original_validate = improvement_module._validate_mode_output

    def reject_only_combination(
        original: str,
        improved: str,
        mode: ImprovementMode,
        *,
        max_characters: int = improvement_module.MAX_OUTPUT_CHARACTERS,
    ) -> None:
        original_validate(original, improved, mode, max_characters=max_characters)
        if (
            max_characters == improvement_module.MAX_DOCUMENT_OUTPUT_CHARACTERS
            and "the first bounded" in improved
            and "receive another" in improved
        ):
            raise ImprovementError("combined final mismatch")

    monkeypatch.setattr(
        improvement_module,
        "_validate_mode_output",
        reject_only_combination,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        proposal = (
            payload["messages"][1]["content"]
            .replace("teh", "the")
            .replace(
                "recieve",
                "receive",
            )
        )
        return httpx.Response(200, json={"message": {"content": proposal}})

    reviewed = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert "the first bounded correction" in reviewed
    assert "must recieve another bounded correction" in reviewed
    assert reviewed != source


def test_unsafe_translation_chunk_is_preserved_and_reported_for_manual_review() -> None:
    source = "This complete paragraph must remain available when a translation is unsafe."
    preserved: list[tuple[int, int]] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "Texto traducido.\n\nPárrafo añadido."}},
        )

    translated = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert translated == source
    assert preserved == [(1, 1)]


@pytest.mark.parametrize(
    "mode",
    [ImprovementMode.TRANSLATE, ImprovementMode.CLEAN_AND_TRANSLATE],
)
def test_translation_modes_require_a_language(mode: ImprovementMode) -> None:
    with pytest.raises(ImprovementError, match="idioma"):
        build_instructions(mode, " ")


def test_translation_prompt_requires_every_markdown_text_block() -> None:
    instructions = build_instructions(ImprovementMode.TRANSLATE, "Español")

    assert "TODO" in instructions
    assert "título, párrafo, elemento de lista y celda de tabla" in instructions
    assert "no dejes pasajes" in instructions
    assert "redacción natural e idiomática" in instructions
    assert "orden propio del idioma de destino" in instructions


def test_focused_title_prompt_translates_names_of_works_but_preserves_people() -> None:
    assert "títulos" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "canciones, libros u otras obras" in improvement_module.FOCUSED_TITLE_INSTRUCTION
    assert "artistas y nombres propios" in improvement_module.FOCUSED_TITLE_INSTRUCTION


def test_translation_restores_exact_paragraph_spacing_around_internal_markers() -> None:
    source = "First paragraph.\n\nSecond paragraph."

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        translated = protected.replace("First paragraph", "Primer párrafo").replace(
            "Second paragraph",
            "Segundo párrafo",
        )
        translated = translated.replace("<!-- PZDOC", "  <!-- PZDOC").replace(
            "XZQ -->",
            "XZQ -->  ",
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "Primer párrafo.\n\nSegundo párrafo."


def test_translation_falls_back_to_individual_paragraphs_after_marker_loss() -> None:
    source = (
        "This first English paragraph contains enough meaningful text for translation.\n\n"
        "This second English paragraph also contains meaningful text for translation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "<!--" in protected:
            translated = re.sub(r"\n<!-- PZDOCP[A-Z]+XZQ -->\n", "\n\n", protected)
        else:
            translated = protected.replace(
                "This first English paragraph contains enough meaningful text for translation.",
                "Este primer párrafo contiene suficiente texto con significado para traducir.",
            ).replace(
                "This second English paragraph also contains meaningful text for translation.",
                "Este segundo párrafo también contiene texto con significado para traducir.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Este primer párrafo contiene suficiente texto con significado para traducir.\n\n"
        "Este segundo párrafo también contiene texto con significado para traducir."
    )
    assert calls == 4


def test_combined_mode_falls_back_to_safe_segments_after_marker_loss() -> None:
    source = (
        "This first English paragraph contains enough meaningful text for translation.\n\n"
        "This second English paragraph also contains meaningful text for translation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "<!--" in protected:
            translated = re.sub(r"\n<!-- PZDOCP[A-Z]+XZQ -->\n", "\n\n", protected)
        else:
            translated = protected.replace(
                "This first English paragraph contains enough meaningful text for translation.",
                "Este primer párrafo contiene suficiente texto con significado para traducir.",
            ).replace(
                "This second English paragraph also contains meaningful text for translation.",
                "Este segundo párrafo también contiene texto con significado para traducir.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN_AND_TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Este primer párrafo contiene suficiente texto con significado para traducir.\n\n"
        "Este segundo párrafo también contiene texto con significado para traducir."
    )
    assert calls == 4


def test_translation_locks_numeric_values_outside_the_final_fallback_requests() -> None:
    source = (
        "This single sentence contains 36 carefully counted houses and enough English words "
        "for reliable local validation."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "PZDOC" in protected:
            translated = re.sub(r"PZDOC[A-Z]+XZQ", "", protected)
        else:
            translated = protected.replace(
                "This single sentence contains",
                "Esta oración contiene",
            ).replace(
                "carefully counted houses and enough English words for reliable local validation.",
                "casas contadas cuidadosamente y suficientes palabras para una validación local "
                "fiable.",
            )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "Esta oración contiene 36 casas contadas cuidadosamente y suficientes palabras para "
        "una validación local fiable."
    )
    assert calls == 4


def test_translation_falls_back_to_sentences_inside_one_paragraph() -> None:
    first = (
        "This first English sentence contains enough natural language to validate a complete "
        "translation safely."
    )
    second = (
        "This second English sentence also contains enough meaningful prose to verify the "
        "sentence fallback."
    )
    source = f"{first} {second}"
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        translated = (
            content.replace(
                first,
                "Esta primera oración contiene suficiente lenguaje natural para validar con "
                "seguridad una traducción completa.",
            ).replace(
                second,
                "Esta segunda oración también contiene suficiente prosa significativa para "
                "comprobar el respaldo por oraciones.",
            )
            if content in {first, second}
            else content
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("Esta primera oración")
    assert "Esta segunda oración" in result
    assert calls == 4


def test_translation_segment_fallback_preserves_list_prefixes_locally() -> None:
    source = "- Read the complete guide.\n- Keep every useful detail."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        fragment = payload["messages"][1]["content"]
        if "\n" in fragment:
            translated = "Lee la guía completa.\nConserva cada detalle útil."
        elif "Read" in fragment:
            translated = "Lee la guía completa."
        else:
            translated = "Conserva cada detalle útil."
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "- Lee la guía completa.\n- Conserva cada detalle útil."
    assert calls == 4


def test_translation_segment_fallback_preserves_only_the_sentence_that_still_fails() -> None:
    source_sentences = (
        "This first English sentence contains enough natural language for a safe translation.",
        "This second English sentence also provides meaningful prose for the local validator.",
        "This third English sentence confirms that most of the paragraph can remain translated.",
        "This final English sentence is deliberately preserved for a focused repair.",
    )
    translations = {
        source_sentences[0]: (
            "Esta primera oración contiene suficiente lenguaje natural para una traducción segura."
        ),
        source_sentences[1]: (
            "Esta segunda oración también aporta prosa significativa para el validador local."
        ),
        source_sentences[2]: (
            "Esta tercera oración confirma que la mayor parte del párrafo puede quedar traducida."
        ),
    }
    source = " ".join(source_sentences)
    calls = 0
    preserved: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"message": {"content": translations.get(content, content)}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert result.startswith("Esta primera oración")
    assert "Esta tercera oración" in result
    assert result.endswith(source_sentences[3])
    assert calls == 3 + len(source_sentences)
    assert preserved == [(1, 1)]


def test_translation_segment_fallback_keeps_valid_paragraphs_when_residue_is_large() -> None:
    source_paragraphs = (
        "This first English paragraph contains enough natural language for safe translation.",
        "This second English paragraph also provides meaningful prose for the validator.",
        "This third English paragraph is deliberately preserved for a focused repair.",
        "This fourth English paragraph is also deliberately preserved for manual review.",
    )
    translations = {
        source_paragraphs[0]: (
            "Este primer párrafo contiene suficiente lenguaje natural para una traducción segura."
        ),
        source_paragraphs[1]: (
            "Este segundo párrafo también aporta prosa significativa para el validador."
        ),
    }
    source = "\n\n".join(source_paragraphs)
    preserved: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"message": {"content": translations.get(content, content)}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
        on_translation_preserved=lambda current, total: preserved.append((current, total)),
    )

    assert result.startswith("Este primer párrafo")
    assert "Este segundo párrafo" in result
    assert source_paragraphs[2] in result
    assert result.endswith(source_paragraphs[3])
    assert preserved == [(1, 1)]


def test_translation_repairs_individual_lines_in_a_dense_index() -> None:
    source_lines = (
        "FIRST ASTROLOGICAL HOUSE 1",
        "SECOND ASTROLOGICAL HOUSE 2",
        "THIRD ASTROLOGICAL HOUSE 3",
        "FOURTH ASTROLOGICAL HOUSE 4",
        "FIFTH ASTROLOGICAL HOUSE 5",
        "SIXTH ASTROLOGICAL HOUSE 6",
    )
    translated_lines = (
        "PRIMERA CASA ASTROLÓGICA 1",
        "SEGUNDA CASA ASTROLÓGICA 2",
        "TERCERA CASA ASTROLÓGICA 3",
        "CUARTA CASA ASTROLÓGICA 4",
        "QUINTA CASA ASTROLÓGICA 5",
        "SEXTA CASA ASTROLÓGICA 6",
    )
    source = "\n".join(source_lines)
    translations = dict(zip(source_lines, translated_lines, strict=True))
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        translated = translations.get(content, content)
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "\n".join(translated_lines)
    assert calls == 1 + len(source_lines)


def test_translation_repairs_a_partially_translated_line_in_a_dense_index() -> None:
    index = "\n".join(
        (
            "SCORPIO I 174",
            "SAGITTARIUS I 192",
            "CAPRICORN I 210",
        )
    )
    paragraph = "This English paragraph establishes the source language for the complete document."
    translated_paragraph = "Este parrafo establece el idioma de origen para el documento completo."
    source = f"{index}\n\n{paragraph}"
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        translated = (
            f"ESCORPIO{content[len('SCORPIO') :]}"
            if "\n" not in content and content.startswith("SCORPIO")
            else content.replace("SCORPIO", "SCORPION")
            .replace("SAGITTARIUS", "SAGITARIO")
            .replace("CAPRICORN", "CAPRICORNO")
            .replace(paragraph, translated_paragraph)
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "\n".join(
        (
            "ESCORPIO I 174",
            "SAGITARIO I 192",
            "CAPRICORNO I 210",
            "",
            translated_paragraph,
        )
    )
    focused_requests = [
        request for request in requests if "\n" not in request and request.startswith("SCORPIO")
    ]
    assert len(focused_requests) == 1
    assert " I " not in focused_requests[0]


def test_translation_repairs_an_unchanged_title_with_one_focused_request() -> None:
    source = (
        "30 DAY MILLIONAIRE CHALLENGE\n\n"
        "This deliberately long English paragraph contains enough natural language to verify "
        "that the main passage reaches Spanish while the unchanged title is repaired separately."
    )
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        if "DAY MILLIONAIRE CHALLENGE" not in protected:
            translated = protected.replace(
                "This deliberately long English paragraph contains enough natural language to "
                "verify that the main passage reaches Spanish while the unchanged title is "
                "repaired separately.",
                "Este párrafo deliberadamente largo contiene suficiente lenguaje natural para "
                "comprobar que el pasaje principal llega al español mientras el título sin "
                "traducir se repara por separado.",
            )
        elif calls == 1:
            translated = protected
        else:
            number_marker = protected.split()[0]
            translated = f"DESAFÍO DEL MILLONARIO EN {number_marker} DÍAS"
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("DESAFÍO DEL MILLONARIO EN 30 DÍAS\n\nEste párrafo")
    assert calls == 3


def test_translation_repairs_an_unchanged_markdown_heading_without_exposing_its_prefix() -> None:
    requests: list[str] = []
    paragraph = (
        "This English paragraph contains enough natural language to establish the source language."
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        requests.append(content)
        if content == "THE HOUSES OF ASTROLOGY" and requests.count(content) == 2:
            translated = "LAS CASAS DE LA ASTROLOGÍA"
        elif content == paragraph:
            translated = (
                "Este párrafo en inglés contiene suficiente lenguaje natural para establecer "
                "el idioma de origen."
            )
        else:
            translated = content
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        f"# THE HOUSES OF ASTROLOGY\n\n{paragraph}",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result.startswith("# LAS CASAS DE LA ASTROLOGÍA\n\nEste párrafo")
    assert requests[:2] == ["THE HOUSES OF ASTROLOGY", "THE HOUSES OF ASTROLOGY"]


def test_translation_planner_isolates_an_unmarked_all_caps_title() -> None:
    source = (
        "30 DAY MILLIONAIRE CHALLENGE\n\n# First chapter\n\nA normal paragraph follows the title."
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if part.should_improve] == [
        "30 DAY MILLIONAIRE CHALLENGE",
        "# First chapter",
        "A normal paragraph follows the title.",
    ]


def test_translation_planner_never_sends_standalone_html_comments_to_the_model() -> None:
    marker = "<!-- PZDOC PDF PAGE 1 -->"
    source = f"{marker}\n\n# First chapter\n\nA normal paragraph follows the title."

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == [marker]
    assert [part.text for part in parts if part.should_improve] == [
        "# First chapter",
        "A normal paragraph follows the title.",
    ]


def test_translation_planner_keeps_a_publisher_name_literal() -> None:
    source = "THREE HANDS PRESS\n\nA normal English paragraph follows the publisher name."

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == ["THREE HANDS PRESS"]
    assert [part.text for part in parts if part.should_improve] == [
        "A normal English paragraph follows the publisher name."
    ]


def test_translation_planner_keeps_repeated_uppercase_person_names_literal() -> None:
    source = (
        "# CHRIS BRENNAN\n\n"
        "A normal English paragraph follows the first byline.\n\n"
        "## CHRIS BRENNAN"
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )

    assert [part.text for part in parts if not part.should_improve] == [
        "# CHRIS BRENNAN",
        "## CHRIS BRENNAN",
    ]
    assert [part.text for part in parts if part.should_improve] == [
        "A normal English paragraph follows the first byline."
    ]


def test_translation_preserves_numbers_and_level_in_a_markdown_heading() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        return httpx.Response(
            200,
            json={"message": {"content": protected.replace("DAYS", "DÍAS")}},
        )

    result = improve_markdown(
        "# DAYS6+7",
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == "# DÍAS6+7"
    assert len(requests) == 1
    assert not requests[0].startswith("#")
    assert "6" in requests[0]
    assert "7" in requests[0]


def test_translation_restores_a_plain_marker_wrapped_in_an_html_comment() -> None:
    source = "# DAYS6+7"
    protected = improvement_module._protect_translation_values(
        source,
        protect_headings=False,
    )
    response = protected.text
    for value in protected.values:
        response = response.replace(value.token, f"<!-- {value.token} -->")

    restored = improvement_module._restore_protected_values(response, protected.values)

    assert restored == source


def test_translation_protects_roman_numerals_used_in_index_references() -> None:
    source = "**LIBRA III 168**"
    protected = improvement_module._protect_translation_values(source)

    assert " III " not in protected.text
    assert any(value.value == "III" for value in protected.values)

    restored = improvement_module._restore_protected_values(
        protected.text.replace("LIBRA", "BALANZA"),
        protected.values,
    )

    assert restored == "**BALANZA III 168**"


def test_translation_repairs_one_unambiguous_internal_marker_typo() -> None:
    source = "Published in 1809-1822 and illustrated on page 23."
    protected = improvement_module._protect_translation_values(source)
    values = {value.value: value for value in protected.values}
    damaged = protected.text.replace(
        values["1809-1822"].token,
        f"{values['1809-1822'].token[:-1]}-",
    )

    restored = improvement_module._restore_protected_values(damaged, protected.values)

    assert restored == source


def test_translation_does_not_guess_an_ambiguous_internal_marker() -> None:
    source = "Values 10 and 20 remain exact."
    protected = improvement_module._protect_translation_values(source)
    first, second = protected.values
    ambiguous = protected.text.replace(first.token, second.token)

    with pytest.raises(ImprovementError, match="valor protegido"):
        improvement_module._restore_protected_values(ambiguous, protected.values)


def test_translation_removes_only_instruction_placeholder_comments_added_by_model() -> None:
    source = "Existing <!-- PZDOC... --> comment."
    response = "Existing <!-- PZDOC... --> comment.<!-- PZDOC... -->"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


@pytest.mark.parametrize(
    "added_placeholder",
    ("<PZDOC>", "</PZDOC>", "<PZDOC />", "&lt;PZDOC&gt;"),
)
def test_translation_removes_instruction_placeholder_tags_added_by_model(
    added_placeholder: str,
) -> None:
    source = "Texto sin etiquetas técnicas."
    response = f"{source}{added_placeholder}"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


def test_translation_preserves_only_the_instruction_placeholder_tags_from_source() -> None:
    source = "Existing <PZDOC> label."
    response = f"{source}<PZDOC>"

    repaired = improvement_module._remove_added_instruction_placeholders(source, response)

    assert repaired == source


def test_translation_validation_rejects_a_cached_instruction_placeholder_tag() -> None:
    with pytest.raises(ImprovementError, match="marcador técnico"):
        improvement_module._validate_mode_output(
            "Source paragraph.",
            "Párrafo traducido.<PZDOC>",
            ImprovementMode.TRANSLATE,
        )


def test_combined_mode_uses_one_operation_per_safe_fragment() -> None:
    source = "# Price\n\nValue 10 on 2026-07-17. [Site](https://example.com)"
    improved = "# Precio\n\nValor 10 el 2026-07-17. [Sitio](https://example.com)"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        changed = (
            protected.replace("Price", "Precio")
            .replace("Value", "Valor")
            .replace(" on ", " el ")
            .replace("[Site]", "[Sitio]")
        )
        return httpx.Response(
            200,
            json={"message": {"content": changed}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN_AND_TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == improved
    assert len(requests) == 2
    assert all(request.url == "http://127.0.0.1:11434/api/chat" for request in requests)
    heading_payload = json.loads(requests[0].content)
    payload = json.loads(requests[1].content)
    assert payload["model"] == "parsezen-local"
    assert payload["stream"] is True
    assert payload["think"] is False
    assert payload["options"] == {
        "temperature": 0,
        "num_ctx": 8_192,
        "num_predict": improvement_module._prediction_token_limit(
            len(source.split("\n\n", 1)[1]),
            8_192,
        ),
        "seed": 0,
    }
    assert payload["messages"][1]["role"] == "user"
    protected_markdown = payload["messages"][1]["content"]
    assert "Value" in protected_markdown
    assert "10" not in protected_markdown
    assert "2026-07-17" not in protected_markdown
    assert "https://example.com" not in protected_markdown
    assert protected_markdown.count("PZDOC") == 3
    assert heading_payload["messages"][1]["content"] == "Price"
    assert "exclusivamente un título" in heading_payload["messages"][0]["content"]
    assert all(
        "única transformación" in json.loads(request.content)["messages"][0]["content"]
        for request in requests
    )


def test_prediction_limit_scales_with_the_fragment_and_context() -> None:
    assert improvement_module._prediction_token_limit(5, 8_192) == 259
    assert improvement_module._prediction_token_limit(3_000, 8_192) == 1_756
    assert improvement_module._prediction_token_limit(20_000, 8_192) == 4_096
    assert improvement_module._prediction_token_limit(20_000, 2_048) == 1_024


def test_missing_model_is_rejected_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(SettingsError, match="Elige un modelo"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            AppSettings(),
            transport=httpx.MockTransport(respond),
        )

    assert not called


@pytest.mark.parametrize("model", ["glm-4.7:cloud", "gpt-oss:120b-cloud"])
def test_cloud_models_are_rejected_before_any_document_request(model: str) -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(SettingsError, match="almacenados localmente"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            AppSettings(model=model),
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_redirect_to_a_remote_server_is_not_followed() -> None:
    requests = 0

    def redirect(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(307, headers={"Location": "https://example.com/steal"})

    with pytest.raises(ImprovementError, match="redirigir"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(redirect),
        )

    assert requests == 1


def test_unavailable_local_server_has_a_specific_error() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(LocalModelUnavailableError, match="servidor esté iniciado"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(unavailable),
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("empty", "vacía"),
        ("nul", "caracteres"),
    ],
)
def test_rejects_unsafe_model_output(mutation: str, expected_error: str) -> None:
    source = "Price 10 [Site](https://example.com)"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        response_content = payload["messages"][1]["content"]
        if mutation == "empty":
            response_content = "   "
        else:
            response_content += "\0"
        return httpx.Response(
            200,
            json={"message": {"content": response_content}},
        )

    with pytest.raises(ImprovementError, match=expected_error):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


def test_preserves_the_original_chunk_when_cleaning_changes_protected_values() -> None:
    source = "Price 10 on 2026-07-17. [Site](https://example.com)"
    changed = "Precio 11 el 2027-08-18. [Sitio](https://changed.example.com)"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": changed}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_translation_keeps_reordered_numbers_and_links_attached_to_their_markers() -> None:
    source = "[Alice](https://a.example) has 10 apples. [Bob](https://b.example) has 20 pears."

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        tokens = re.findall(r"PZDOC[A-Z]+XZQ", protected)
        translated = (
            f"[Bob]({tokens[2]}) tiene {tokens[3]} peras. "
            f"[Alice]({tokens[0]}) tiene {tokens[1]} manzanas."
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "[Bob](https://b.example) tiene 20 peras. [Alice](https://a.example) tiene 10 manzanas."
    )


def test_rejects_incompatible_ollama_json() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"result": "no"}))

    with pytest.raises(ImprovementError, match="incompatible"):
        improve_markdown(
            "Content",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=transport,
        )


def test_long_document_is_improved_in_ordered_structural_chunks() -> None:
    block = ("Texto conservador para comprobar la división estructural. " * 32).strip()
    source = "\n\n".join(f"## Sección\n\n{block}" for _ in range(7))
    requests: list[str] = []
    progress: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == source
    assert len(requests) >= 2
    assert all(len(chunk) <= MAX_INPUT_CHARACTERS for chunk in requests)
    assert progress == [(index, len(requests)) for index in range(1, len(requests) + 1)]


def test_tables_are_never_split_between_model_requests() -> None:
    long_paragraph = "palabra " * 995
    table = "| Nombre | Estado |\n| --- | --- |\n| Parsezen | Listo |"
    source = f"{long_paragraph}\n\n{table}\n\nTexto posterior."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert len(requests) >= 2
    assert sum(table in chunk for chunk in requests) == 1


def test_translation_recovers_table_rows_when_a_full_response_breaks_the_table() -> None:
    source = (
        "| Topic | Description |\n"
        "| --- | --- |\n"
        "| First house | This section explains the first astrological house clearly. |\n"
        "| Second house | This section explains the second astrological house clearly. |"
    )
    translations = {
        "| Topic | Description |": "| Tema | Descripción |",
        (
            "| First house | This section explains the first astrological house clearly. |"
        ): "| Primera casa | Esta sección explica claramente la primera casa astrológica. |",
        (
            "| Second house | This section explains the second astrological house clearly. |"
        ): "| Segunda casa | Esta sección explica claramente la segunda casa astrológica. |",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        translated = (
            content.replace("| --- | --- |\n", "")
            if "\n" in content
            else translations.get(content, content)
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == (
        "| Tema | Descripción |\n"
        "| --- | --- |\n"
        "| Primera casa | Esta sección explica claramente la primera casa astrológica. |\n"
        "| Segunda casa | Esta sección explica claramente la segunda casa astrológica. |"
    )


def test_fenced_code_is_preserved_without_sending_it_to_the_model() -> None:
    code = "```python\nvalue = 12\n\nprint(value)\n```"
    source = f"Texto anterior.\n\n{code}\n\nTexto posterior."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert code in result
    assert len(requests) == 2
    assert all("value = 12" not in chunk for chunk in requests)


def test_code_only_document_does_not_need_a_model_request() -> None:
    source = "```python\nprint('local')\n```"
    transport = httpx.MockTransport(lambda _request: pytest.fail("Unexpected request"))

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_rejects_an_oversized_individual_block_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ImprovementError, match="bloque Markdown"):
        improve_markdown(
            "x" * (MAX_INPUT_CHARACTERS + 1),
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_splits_an_oversized_prose_block_at_safe_boundaries() -> None:
    source = ("Sentence with useful text. " * 700).strip()
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert len(requests) >= 2
    assert all(len(chunk) <= MAX_INPUT_CHARACTERS for chunk in requests)


def test_splits_a_number_dense_index_without_changing_it() -> None:
    source = "\n".join(f"Chapter {index} {100 + index}" for index in range(60))
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        requests.append(chunk)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert len(requests) >= 5
    assert all(
        len(NUMBER_PATTERN.findall(chunk)) <= MAX_CONSERVED_VALUES_PER_CHUNK for chunk in requests
    )


def test_translation_planner_limits_all_protected_values_including_paragraphs() -> None:
    source = "\n\n".join(
        f"Paragraph {index} contains enough natural language for a complete translation."
        for index in range(60)
    )

    parts = improvement_module._plan_markdown_parts(
        source,
        max_characters=MAX_TRANSLATION_CHUNK_CHARACTERS,
        protect_paragraphs=True,
    )
    translated_parts = [part for part in parts if part.should_improve]

    assert len(translated_parts) >= 4
    assert all(
        len(improvement_module._protect_translation_values(part.text).values)
        <= MAX_TRANSLATION_PROTECTED_VALUES_PER_CHUNK
        for part in translated_parts
    )


def test_repairs_changed_heading_levels_when_heading_count_is_preserved() -> None:
    source = "# Main title\n\n## Section\n\nText remains."

    def respond(_request: httpx.Request) -> httpx.Response:
        changed = "## Main Title\n\n### Section\n\nText remains."
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == "# Main Title\n\n## Section\n\nText remains."


def test_removes_headings_invented_in_a_fragment_without_headings() -> None:
    source = "Estado de referencia 58\nCambiar el programa 63"
    changed = "### Estado de referencia 58\n### Cambiar el programa 63"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": changed}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_retries_once_when_the_model_removes_a_heading() -> None:
    source = "# Main title\n\n## Section\n\nText remains."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            changed = "# Main title\n\nSection\n\nText remains."
        else:
            payload = json.loads(request.content)
            changed = payload["messages"][1]["content"]
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert calls == 2


def test_rejects_a_document_above_the_global_limit_before_any_request() -> None:
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    with pytest.raises(ImprovementError, match="límite"):
        improve_markdown(
            "x" * (MAX_DOCUMENT_CHARACTERS + 1),
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )

    assert not called


def test_cleanup_preserves_a_failing_chunk_without_losing_content() -> None:
    source = f"{'texto ' * 1332}\n\nTotal 10."
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        chunk = payload["messages"][1]["content"]
        if requests >= 2:
            chunk = chunk.replace("10", "", 1)
        return httpx.Response(200, json={"message": {"content": chunk}})

    result = improve_markdown(
        source,
        ImprovementMode.CLEAN,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_content_review_locks_internal_markers_and_link_destinations() -> None:
    marker = "<!-- PZDOC-PAGE: 1 -->"
    source = f"{marker}\n\n[Sitio](https://example.com) contiene un eror claro."
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        return httpx.Response(
            200,
            json={"message": {"content": protected.replace("un eror", "un error")}},
        )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source.replace("un eror", "un error")
    assert len(requests) == 1
    assert marker not in requests[0]
    assert "https://example.com" not in requests[0]


def test_content_review_skips_marker_blocks_and_reviews_each_text_segment() -> None:
    source = (
        "<!-- PZDOC-PAGE: 1 -->\n\n"
        "Este primer párrafo contiene un eror claro.\n\n"
        "<!-- PZDOC-PAGE: 2 -->\n\n"
        "Este segundo párrafo también contiene un eror claro."
    )
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requests.append(protected)
        response = protected.replace("un eror", "un error")
        return httpx.Response(200, json={"message": {"content": response}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_CONTENT,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source.replace("un eror", "un error")
    assert len(requests) == 2
    assert all("PZDOC-PAGE" not in request for request in requests)


def test_structure_review_accepts_formatting_changes_that_preserve_visible_text() -> None:
    source = "**Chapter One**\n\nText with *exactly* the same words."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "# **Chapter One**\n\nText with *exactly* the same words."


def test_structure_review_keeps_wording_while_accepting_an_aligned_heading_change() -> None:
    source = "## Chapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "# Chapter One\n\nThe original paragraph remains complete."


def test_structure_review_rejects_an_extreme_existing_heading_demotion() -> None:
    source = "# Chapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL1=6"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == source


def test_structure_review_maps_a_reordered_heading_back_to_the_exact_source() -> None:
    source = "Opening text.\n\nChapter One\n\nThe original paragraph remains complete."
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "PZL3=1"}})
    )

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=transport,
    )

    assert result == "Opening text.\n\n# Chapter One\n\nThe original paragraph remains complete."


def test_structure_review_preserves_an_unsafe_chunk_instead_of_failing_the_document() -> None:
    source = "**Chapter One**\n\nThe original paragraph remains complete."
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        changed = "# Different chapter\n\nA rewritten paragraph."
        return httpx.Response(200, json={"message": {"content": changed}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == source
    assert requests == 1


def test_structure_review_does_not_promote_a_long_body_paragraph_to_heading() -> None:
    source = (
        "This is a complete body paragraph with enough words and sentences to remain ordinary "
        "prose. It must never become a chapter heading merely because the model returned it on "
        "one line, while every word and its order happened to remain unchanged."
    )
    proposed = f"# {source}"
    requests = 0
    progress: list[tuple[int, int]] = []

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"message": {"content": proposed}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
        on_progress=lambda current, total: progress.append((current, total)),
    )

    assert result == source
    assert requests == 0
    assert progress == []


def test_structure_review_recovers_exact_source_words_from_heading_directives() -> None:
    source = "Opening text.\n\nChapter One\n\nThe original paragraph remains complete."
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        numbered = payload["messages"][1]["content"]
        assert "PZL3 CANDIDATA: Chapter One" in numbered
        return httpx.Response(200, json={"message": {"content": "PZL3=1"}})

    result = improve_markdown(
        source,
        ImprovementMode.REVIEW_STRUCTURE,
        LOCAL_SETTINGS,
        transport=httpx.MockTransport(respond),
    )

    assert result == "Opening text.\n\n# Chapter One\n\nThe original paragraph remains complete."
    assert calls == 1


def test_translation_preserves_a_failing_chunk_instead_of_losing_the_document() -> None:
    source = "Total 10."

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "Total."}})

    assert (
        improve_markdown(
            source,
            ImprovementMode.TRANSLATE,
            LOCAL_SETTINGS,
            "Español",
            transport=httpx.MockTransport(respond),
        )
        == source
    )


def test_rejects_changes_to_inline_code() -> None:
    source = "Ejecuta `python -m parsezen` localmente."

    def respond(_request: httpx.Request) -> httpx.Response:
        changed = "Ejecuta `python app.py` localmente."
        return httpx.Response(200, json={"message": {"content": changed}})

    with pytest.raises(ImprovementError, match="código en línea"):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


@pytest.mark.parametrize(
    ("source", "changed", "expected_error"),
    [
        (
            "| Nombre | Estado |\n| --- | --- |\n| Parsezen | Listo |",
            "| Nombre | Estado |\n| --- | --- |",
            "tabla",
        ),
    ],
)
def test_rejects_changes_to_markdown_structure(
    source: str,
    changed: str,
    expected_error: str,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": changed}})

    with pytest.raises(ImprovementError, match=expected_error):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
        )


def test_cancellation_after_a_model_response_stops_before_the_next_chunk() -> None:
    cancellation = CancellationToken()
    source = f"{'texto ' * 1332}\n\nSegundo fragmento 10."
    requests = 0
    progress: list[tuple[int, int]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        payload = json.loads(request.content)
        cancellation.cancel()
        return httpx.Response(
            200,
            json={"message": {"content": payload["messages"][1]["content"]}},
        )

    with pytest.raises(ProcessingCancelledError):
        improve_markdown(
            source,
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=httpx.MockTransport(respond),
            on_progress=lambda current, total: progress.append((current, total)),
            cancellation=cancellation,
        )

    assert requests == 1
    assert progress == [(1, 3)]


def test_cancellation_interrupts_an_active_stream_before_it_is_published() -> None:
    cancellation = CancellationToken()

    class CancelledStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"message":{"content":"Texto parcial"},"done":false}\n'
            cancellation.cancel()
            yield b'{"message":{"content":" que no debe publicarse"},"done":true}\n'

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=CancelledStream()))

    with pytest.raises(ProcessingCancelledError):
        improve_markdown(
            "Texto original suficientemente claro.",
            ImprovementMode.CLEAN,
            LOCAL_SETTINGS,
            transport=transport,
            cancellation=cancellation,
        )


def test_translation_retries_when_the_first_response_stays_in_the_source_language() -> None:
    source = (
        "This deliberately long English paragraph contains enough natural language to verify "
        "that every passage reaches the requested language. It also confirms that the local "
        "model gets one careful retry before Parsezen rejects an unsafe translation."
    )
    spanish = (
        "Este párrafo deliberadamente largo contiene suficiente lenguaje natural para comprobar "
        "que cada pasaje llega al idioma solicitado. También confirma que el modelo local recibe "
        "un reintento cuidadoso antes de que Parsezen rechace una traducción insegura."
    )
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        content = source if len(requests) == 1 else spanish
        return httpx.Response(200, json={"message": {"content": content}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == spanish
    assert len(requests) == 2
    retry_messages = requests[1]["messages"]
    assert isinstance(retry_messages, list)
    assert "sin omitir" in retry_messages[0]["content"]
    assert "dejó texto natural" in retry_messages[0]["content"]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("cambió números o fechas", "elementos protegidos"),
        ("cambió la estructura de las listas", "estructura Markdown"),
        ("no quedó en el idioma solicitado", "dejó texto natural"),
        ("parece haber duplicado o añadido contenido", "exactamente una vez"),
        ("no devolvió el fragmento en una sola línea", "exactamente una sola línea"),
        ("invirtió una relación de certeza", "polaridad"),
    ],
)
def test_validation_retry_instruction_is_specialized_by_failure_type(
    reason: str,
    expected: str,
) -> None:
    instruction = improvement_module._specialized_retry_instruction(ImprovementError(reason))

    assert expected in instruction


def test_translation_preserves_content_after_an_omission_and_one_retry() -> None:
    source = (
        "This long source paragraph records every relevant condition, exception, promise and "
        "deadline so the translation guard can detect a serious omission. The complete document "
        "must remain available and no model may replace it with a short summary."
    )
    requests = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={"message": {"content": "El documento contiene varias condiciones."}},
        )

    assert (
        improve_markdown(
            source,
            ImprovementMode.TRANSLATE,
            LOCAL_SETTINGS,
            "Español",
            transport=httpx.MockTransport(respond),
        )
        == source
    )

    assert requests == 2


def test_translation_skips_the_model_when_text_is_already_in_the_target_language() -> None:
    source = (
        "Este documento ya está escrito completamente en español y contiene suficiente texto "
        "natural para detectar el idioma sin consultar al modelo local instalado."
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        pytest.fail("No debería realizarse ninguna petición")

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == source


def test_translation_skips_target_language_blocks_inside_a_mixed_document() -> None:
    source = (
        "# English section\n\n"
        "This deliberately long English paragraph explains the first part of the document and "
        "contains enough natural language to establish English as the predominant source.\n\n"
        "# Aviso local\n\n"
        "Este aviso ya está escrito en español y debe conservarse sin enviarlo al modelo.\n\n"
        "# Final section\n\n"
        "This final English paragraph contains additional natural language that must be "
        "translated while the Spanish warning remains untouched."
    )
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        protected = payload["messages"][1]["content"]
        requested.append(protected)
        translated = (
            protected.replace("English section", "Sección inglesa")
            .replace("Final section", "Sección final")
            .replace(
                "This deliberately long English paragraph explains the first part of the "
                "document and contains enough natural language to establish English as the "
                "predominant source.",
                "Este párrafo inglés deliberadamente largo explica la primera parte del documento "
                "y contiene suficiente lenguaje natural para establecer el inglés como fuente "
                "predominante.",
            )
            .replace(
                "This final English paragraph contains additional natural language that must be "
                "translated while the Spanish warning remains untouched.",
                "Este párrafo final contiene lenguaje natural adicional que debe traducirse "
                "mientras el aviso en español permanece intacto.",
            )
        )
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert "Este aviso ya está escrito en español" in result
    assert all("Este aviso ya está escrito" not in content for content in requested)


def test_translation_keeps_document_language_for_prose_around_a_foreign_citation() -> None:
    english_context = (
        "This long English paragraph establishes the predominant language of the complete "
        "document before a bibliographic note written with a foreign-language work title. "
        "It contains several ordinary sentences so a quoted title cannot override the known "
        "language of the surrounding publication details. The remaining text confirms that "
        "English is the source language for every prose fragment that still needs translation."
    )
    mixed_citation = (
        "The cover image represents an ancient ceiling from the Temple of Hathor at Dendera, "
        "as illustrated in "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française."
    )
    source = f"{english_context}\n\n{mixed_citation}"
    spanish_context = (
        "Este largo párrafo en inglés establece el idioma predominante del documento completo "
        "antes de una nota bibliográfica con el título de una obra en otro idioma. Contiene "
        "varias frases comunes para que un título citado no anule el idioma conocido de los "
        "datos editoriales que lo rodean. El texto restante confirma que el inglés es el idioma "
        "de origen de cada fragmento de prosa que aún necesita traducción."
    )
    translated_citation = (
        "La imagen de portada representa un techo antiguo del Templo de Hathor en Dendera, "
        "tal como aparece ilustrado en "
        "Description de l'Égypte, ou Recueil des observations et des recherches qui ont été "
        "faites en Égypte pendant l'expédition de l'armée française."
    )

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requested = payload["messages"][1]["content"]
        translated = spanish_context if english_context in requested else translated_citation
        return httpx.Response(200, json={"message": {"content": translated}})

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(respond),
    )

    assert result == f"{spanish_context}\n\n{translated_citation}"


def test_translation_accepts_explicit_source_language_for_a_mixed_citation() -> None:
    source = (
        "The cover image represents an ancient ceiling from the Temple of Hathor at Dendera, "
        "as illustrated in Description de l'Égypte, ou Recueil des observations et des "
        "recherches qui ont été faites en Égypte pendant l'expédition de l'armée française."
    )
    translated = (
        "La imagen de portada representa un techo antiguo del Templo de Hathor en Dendera, "
        "tal como aparece ilustrado en Description de l'Égypte, ou Recueil des observations "
        "et des recherches qui ont été faites en Égypte pendant l'expédition de l'armée française."
    )

    result = improve_markdown(
        source,
        ImprovementMode.TRANSLATE,
        LOCAL_SETTINGS,
        "Español",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"message": {"content": translated}})
        ),
        source_language_code="en",
    )

    assert result == translated
