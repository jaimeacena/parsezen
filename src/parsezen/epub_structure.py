"""Editable, Qt-free EPUB chapter and heading plan used by the final review step."""

from __future__ import annotations

import re
from dataclasses import dataclass

from parsezen.epub_builder import compose_explicit_epub_chapters, plan_epub

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class EditableHeading:
    """One heading address inside a chapter."""

    line_number: int
    level: int
    title: str


class EditableEpubStructure:
    """Small safe model for explicit EPUB chapter and heading operations."""

    def __init__(self, markdown: str, fallback_title: str) -> None:
        plan = plan_epub(markdown, fallback_title)
        self._fallback_title = fallback_title
        self._chapters = [chapter.markdown.strip() for chapter in plan.chapters]
        if not self._chapters and markdown.strip():
            self._chapters = [markdown.strip()]

    @property
    def chapter_count(self) -> int:
        return len(self._chapters)

    def chapter_markdown(self, index: int) -> str:
        return self._chapters[index]

    def chapter_title(self, index: int) -> str:
        headings = self.headings(index)
        if headings:
            return headings[0].title
        if len(self._chapters) == 1:
            return self._fallback_title
        return f"Parte {index + 1}"

    def headings(self, chapter_index: int) -> tuple[EditableHeading, ...]:
        headings: list[EditableHeading] = []
        for line_number, line in enumerate(self._chapters[chapter_index].splitlines(), start=1):
            match = _HEADING_LINE.fullmatch(line.strip())
            if match is None:
                continue
            headings.append(
                EditableHeading(line_number, len(match.group(1)), match.group(2).strip())
            )
        return tuple(headings)

    def markdown(self) -> str:
        return compose_explicit_epub_chapters(self._chapters)

    def rename(self, chapter_index: int, line_number: int | None, title: str) -> None:
        cleaned = " ".join(title.split()).strip()
        if not cleaned or "\0" in cleaned:
            raise ValueError("El título no puede estar vacío.")
        lines = self._chapters[chapter_index].splitlines(keepends=True)
        if line_number is None:
            headings = self.headings(chapter_index)
            if headings:
                line_number = headings[0].line_number
            else:
                self._chapters[chapter_index] = (
                    f"# {cleaned}\n\n{self._chapters[chapter_index].lstrip()}"
                ).rstrip()
                return
        if not 1 <= line_number <= len(lines):
            raise ValueError("El título seleccionado ya no existe.")
        original = lines[line_number - 1]
        match = _HEADING_LINE.fullmatch(original.rstrip("\r\n").strip())
        if match is None:
            raise ValueError("La línea seleccionada no es un título.")
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        lines[line_number - 1] = f"{'#' * len(match.group(1))} {cleaned}{ending}"
        self._chapters[chapter_index] = "".join(lines).strip()

    def add_chapter(self, after_index: int, title: str) -> int:
        cleaned = " ".join(title.split()).strip()
        if not cleaned or "\0" in cleaned:
            raise ValueError("El título no puede estar vacío.")
        position = max(0, min(after_index + 1, len(self._chapters)))
        self._chapters.insert(position, f"# {cleaned}")
        return position

    def split_at_heading(self, chapter_index: int, line_number: int) -> int:
        lines = self._chapters[chapter_index].splitlines(keepends=True)
        if not 1 < line_number <= len(lines):
            raise ValueError("Elige un título situado dentro del capítulo.")
        before = "".join(lines[: line_number - 1]).strip()
        after = "".join(lines[line_number - 1 :]).strip()
        if not before or not after:
            raise ValueError("No hay contenido suficiente para dividir el capítulo aquí.")
        self._chapters[chapter_index : chapter_index + 1] = [before, after]
        return chapter_index + 1

    def merge_with_previous(self, chapter_index: int) -> int:
        if chapter_index <= 0 or chapter_index >= len(self._chapters):
            raise ValueError("Este capítulo no se puede unir con el anterior.")
        merged = (
            self._chapters[chapter_index - 1].rstrip()
            + "\n\n"
            + self._chapters[chapter_index].lstrip()
        )
        self._chapters[chapter_index - 1 : chapter_index + 1] = [merged]
        return chapter_index - 1

    def move_chapter(self, chapter_index: int, offset: int) -> int:
        target = chapter_index + offset
        if not 0 <= target < len(self._chapters):
            return chapter_index
        chapter = self._chapters.pop(chapter_index)
        self._chapters.insert(target, chapter)
        return target

    def adjust_heading_level(
        self,
        chapter_index: int,
        line_number: int,
        delta: int,
    ) -> int:
        lines = self._chapters[chapter_index].splitlines(keepends=True)
        if not 1 <= line_number <= len(lines):
            raise ValueError("El título seleccionado ya no existe.")
        original = lines[line_number - 1]
        match = _HEADING_LINE.fullmatch(original.rstrip("\r\n").strip())
        if match is None:
            raise ValueError("La línea seleccionada no es un título.")
        level = max(1, min(6, len(match.group(1)) + delta))
        ending = "\n" if original.endswith(("\n", "\r")) else ""
        lines[line_number - 1] = f"{'#' * level} {match.group(2).strip()}{ending}"
        self._chapters[chapter_index] = "".join(lines).strip()
        return level
