"""Deterministic final-output checks executed before atomic publication."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree as SafeElementTree

from parsezen.document_model import RESOURCE_REFERENCE_PREFIX, ConvertedResource
from parsezen.errors import FinalIntegrityError
from parsezen.revision import split_markdown_blocks

_EPUB_ANCHOR_COMMENT_PATTERN = re.compile(r"<!--\s*PZDOC EPUB ANCHOR ([a-z0-9][a-z0-9-]*)\s*-->")
_IMAGE_PATTERN = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]"
    r"\(\s*<?(?P<destination>[^)>\s]+)>?"
    r"(?P<title>\s+(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\((?:\\.|[^)])*\)))?"
    r"\s*\)"
)
_HEADING_PATTERN = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+]\([^)]+\)")
_PARAGRAPH_TAGS = frozenset({"blockquote", "dd", "dt", "figcaption", "li", "p", "pre", "td", "th"})
_HEADING_TAGS = frozenset({f"h{level}" for level in range(1, 7)})
_IMAGE_TAGS = frozenset({"image", "img"})


class IntegritySeverity(StrEnum):
    """Publication impact of one deterministic final-output mismatch."""

    CRITICAL = "critical"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class IntegrityLedger:
    """Content-free structural inventory safe to expose and persist."""

    blocks: int = 0
    paragraphs: int = 0
    headings: int = 0
    images: int = 0
    links: int = 0
    resources: int = 0


@dataclass(frozen=True, slots=True)
class IntegrityFinding:
    """One actionable mismatch without document excerpts or paths."""

    code: str
    message: str
    severity: IntegritySeverity = IntegritySeverity.CRITICAL
    expected: int | None = None
    actual: int | None = None


@dataclass(frozen=True, slots=True)
class FinalIntegrityReport:
    """Successful checks and any content-free findings for one final artifact."""

    format_label: str
    checks: tuple[str, ...]
    ledger: IntegrityLedger = IntegrityLedger()
    findings: tuple[IntegrityFinding, ...] = ()

    @property
    def publishable(self) -> bool:
        return not any(finding.severity is IntegritySeverity.CRITICAL for finding in self.findings)

    @property
    def verified(self) -> bool:
        return self.publishable and bool(self.checks)


class IntegrityCapture:
    """Capture the report produced while an output writer validates its staging file."""

    def __init__(self, verifier: Callable[[Path], FinalIntegrityReport]) -> None:
        self._verifier = verifier
        self.report: FinalIntegrityReport | None = None

    def __call__(self, path: Path) -> None:
        self.report = None
        self.report = self._verifier(path)


def text_integrity_capture(
    expected: str,
    *,
    markdown: bool,
) -> IntegrityCapture:
    return IntegrityCapture(lambda path: verify_text_file(path, expected, markdown=markdown))


def binary_integrity_capture(
    expected: bytes,
    *,
    format_label: str,
    validate_container: Callable[[Path], None] | None = None,
    ledger: IntegrityLedger | None = None,
) -> IntegrityCapture:
    return IntegrityCapture(
        lambda path: verify_binary_file(
            path,
            expected,
            format_label=format_label,
            validate_container=validate_container,
            ledger=ledger,
        )
    )


def verify_text_file(
    path: Path,
    expected: str,
    *,
    markdown: bool,
) -> FinalIntegrityReport:
    """Require the staged UTF-8 file to match the approved canonical text."""

    try:
        actual = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FinalIntegrityError(
            "La comprobación final no pudo volver a leer el archivo temporal."
        ) from exc

    expected_canonical = _canonical_markdown(expected) if markdown else _canonical_text(expected)
    actual_canonical = _canonical_markdown(actual) if markdown else _canonical_text(actual)
    expected_ledger = markdown_ledger(expected) if markdown else text_ledger(expected)
    actual_ledger = markdown_ledger(actual) if markdown else text_ledger(actual)
    findings = _ledger_findings(expected_ledger, actual_ledger)
    if (
        sha256(expected_canonical.encode("utf-8")).digest()
        != sha256(actual_canonical.encode("utf-8")).digest()
    ):
        findings = (
            *findings,
            IntegrityFinding(
                "approved_text_changed",
                "El archivo temporal no coincide con el contenido aprobado.",
            ),
        )
    report = FinalIntegrityReport(
        "Markdown" if markdown else "Texto",
        (
            "Codificación UTF-8 comprobada",
            "Contenido aprobado conservado",
            "Estructura textual comprobada",
        ),
        actual_ledger,
        _deduplicate_findings(findings),
    )
    require_publishable(report)
    return report


def verify_binary_file(
    path: Path,
    expected: bytes,
    *,
    format_label: str,
    validate_container: Callable[[Path], None] | None = None,
    ledger: IntegrityLedger | None = None,
) -> FinalIntegrityReport:
    """Require staged bytes to equal the already validated in-memory package."""

    try:
        if validate_container is not None:
            validate_container(path)
        actual_size = path.stat().st_size
        actual_digest = _file_sha256(path)
    except FinalIntegrityError:
        raise
    except Exception as exc:
        raise FinalIntegrityError(
            f"La comprobación final no pudo validar el paquete {format_label} temporal."
        ) from exc
    findings: tuple[IntegrityFinding, ...] = ()
    if actual_size != len(expected) or actual_digest != sha256(expected).digest():
        findings = (
            IntegrityFinding(
                "published_bytes_changed",
                f"El paquete {format_label} temporal no coincide con el contenido validado.",
                expected=len(expected),
                actual=actual_size,
            ),
        )
    checks = ["Archivo temporal idéntico al paquete validado"]
    if validate_container is not None:
        checks.insert(0, f"Contenedor {format_label} válido")
    report = FinalIntegrityReport(
        format_label,
        tuple(checks),
        ledger or IntegrityLedger(),
        findings,
    )
    require_publishable(report)
    return report


def verify_epub_payload(
    content: bytes,
    chapters: Mapping[str, str],
    resources: Mapping[str, ConvertedResource],
) -> FinalIntegrityReport:
    """Check every generated chapter and resource against the final EPUB bytes."""

    findings: list[IntegrityFinding] = []
    ledger = IntegrityLedger()
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = frozenset(archive.namelist())
            expected_chapter_names = frozenset(f"EPUB/text/{filename}" for filename in chapters)
            actual_chapter_names = frozenset(
                name
                for name in names
                if name.startswith("EPUB/text/")
                and name.endswith(".xhtml")
                and name != "EPUB/text/cover.xhtml"
            )
            expected_resource_names = frozenset(
                f"EPUB/images/{relative_path}" for relative_path in resources
            )
            actual_resource_names = frozenset(
                name for name in names if name.startswith("EPUB/images/")
            )
            if actual_chapter_names - expected_chapter_names:
                findings.append(
                    IntegrityFinding(
                        "unexpected_chapter",
                        "El EPUB generado contiene un capítulo que no estaba en el plan validado.",
                        expected=len(expected_chapter_names),
                        actual=len(actual_chapter_names),
                    )
                )
            if actual_resource_names - expected_resource_names:
                findings.append(
                    IntegrityFinding(
                        "unexpected_resource",
                        "El EPUB generado contiene un recurso que no estaba en el plan validado.",
                        expected=len(expected_resource_names),
                        actual=len(actual_resource_names),
                    )
                )
            actual_documents: list[str] = []
            for filename, expected_xhtml in chapters.items():
                archive_name = f"EPUB/text/{filename}"
                if archive_name not in names:
                    findings.append(
                        IntegrityFinding(
                            "missing_chapter",
                            "Falta un capítulo aprobado en el EPUB generado.",
                            expected=len(chapters),
                            actual=sum(name.startswith("EPUB/text/chapter-") for name in names),
                        )
                    )
                    continue
                actual_xhtml = archive.read(archive_name).decode("utf-8")
                actual_documents.append(actual_xhtml)
                if actual_xhtml != expected_xhtml:
                    findings.append(
                        IntegrityFinding(
                            "chapter_changed",
                            "Un capítulo del EPUB no coincide con el XHTML validado.",
                        )
                    )
            for relative_path, resource in resources.items():
                archive_name = f"EPUB/images/{relative_path}"
                if archive_name not in names:
                    findings.append(
                        IntegrityFinding(
                            "missing_resource",
                            "Falta un recurso aprobado en el EPUB generado.",
                            expected=len(resources),
                            actual=sum(name.startswith("EPUB/images/") for name in names),
                        )
                    )
                    continue
                actual_payload = archive.read(archive_name)
                if sha256(actual_payload).digest() != sha256(resource.read_content()).digest():
                    findings.append(
                        IntegrityFinding(
                            "resource_changed",
                            "Un recurso binario del EPUB cambió durante la publicación.",
                        )
                    )
            ledger = _xhtml_ledger(tuple(actual_documents), resources=len(resources))
    except (BadZipFile, KeyError, OSError, UnicodeError, ValueError) as exc:
        raise FinalIntegrityError(
            "La comprobación final no pudo inspeccionar el EPUB generado."
        ) from exc

    report = FinalIntegrityReport(
        "EPUB",
        (
            "Capítulos comparados con el contenido validado",
            "Jerarquía XHTML comprobada",
            "Recursos binarios comparados byte a byte",
        ),
        ledger,
        _deduplicate_findings(tuple(findings)),
    )
    require_publishable(report)
    return report


def merge_integrity_reports(
    *reports: FinalIntegrityReport | None,
) -> FinalIntegrityReport | None:
    """Combine build-time and staged-file checks without duplicating labels."""

    available = tuple(report for report in reports if report is not None)
    if not available:
        return None
    checks = tuple(dict.fromkeys(check for report in available for check in report.checks))
    findings = _deduplicate_findings(
        tuple(finding for report in available for finding in report.findings)
    )
    ledger = max(
        (report.ledger for report in available),
        key=lambda item: (
            item.blocks
            + item.paragraphs
            + item.headings
            + item.images
            + item.links
            + item.resources
        ),
    )
    merged = FinalIntegrityReport(available[-1].format_label, checks, ledger, findings)
    require_publishable(merged)
    return merged


def markdown_ledger(markdown: str) -> IntegrityLedger:
    canonical = _canonical_markdown(markdown)
    blocks = split_markdown_blocks(canonical, enforce_review_limit=False)
    return IntegrityLedger(
        blocks=len(blocks),
        paragraphs=sum(int(bool(block.markdown.strip())) for block in blocks),
        headings=len(_HEADING_PATTERN.findall(canonical)),
        images=len(_IMAGE_PATTERN.findall(canonical)),
        links=len(_LINK_PATTERN.findall(canonical)),
    )


def text_ledger(text: str) -> IntegrityLedger:
    paragraphs = tuple(part for part in re.split(r"\n\s*\n", _canonical_text(text)) if part.strip())
    return IntegrityLedger(
        blocks=len(paragraphs),
        paragraphs=len(paragraphs),
    )


def require_publishable(report: FinalIntegrityReport) -> None:
    if report.publishable:
        return
    raise FinalIntegrityError(
        "El control final detectó una diferencia y detuvo la publicación. "
        "El resultado anterior y el trabajo recuperable permanecen intactos."
    )


def _canonical_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _canonical_markdown(markdown: str) -> str:
    normalized = _canonical_text(markdown)
    normalized = _EPUB_ANCHOR_COMMENT_PATTERN.sub(
        lambda match: f'<a id="{match.group(1)}"></a>',
        normalized,
    )
    normalized = _IMAGE_PATTERN.sub(_canonical_image, normalized)
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def _canonical_image(match: re.Match[str]) -> str:
    destination = match.group("destination")
    if destination.startswith(RESOURCE_REFERENCE_PREFIX):
        destination = f"PZ-RESOURCE/{destination.removeprefix(RESOURCE_REFERENCE_PREFIX)}"
    elif ".assets/" in destination:
        destination = f"PZ-RESOURCE/{destination.split('.assets/', 1)[1]}"
    title = match.group("title") or ""
    return f"![{match.group('alt')}]({destination}{title})"


def _ledger_findings(
    expected: IntegrityLedger,
    actual: IntegrityLedger,
) -> tuple[IntegrityFinding, ...]:
    findings: list[IntegrityFinding] = []
    for code, label, expected_count, actual_count in (
        ("missing_blocks", "bloques", expected.blocks, actual.blocks),
        ("missing_headings", "encabezados", expected.headings, actual.headings),
        ("missing_images", "imágenes", expected.images, actual.images),
        ("missing_links", "enlaces", expected.links, actual.links),
    ):
        if actual_count < expected_count:
            findings.append(
                IntegrityFinding(
                    code,
                    f"El archivo final contiene menos {label} de los aprobados.",
                    expected=expected_count,
                    actual=actual_count,
                )
            )
    return tuple(findings)


def _xhtml_ledger(
    documents: tuple[str, ...],
    *,
    resources: int,
) -> IntegrityLedger:
    paragraphs = 0
    headings = 0
    images = 0
    links = 0
    for document in documents:
        root = SafeElementTree.fromstring(document.encode("utf-8"))
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].casefold()
            paragraphs += int(local_name in _PARAGRAPH_TAGS)
            headings += int(local_name in _HEADING_TAGS)
            images += int(local_name in _IMAGE_TAGS)
            links += int(local_name == "a" and bool(element.attrib.get("href")))
    return IntegrityLedger(
        blocks=len(documents),
        paragraphs=paragraphs,
        headings=headings,
        images=images,
        links=links,
        resources=resources,
    )


def _deduplicate_findings(
    findings: tuple[IntegrityFinding, ...],
) -> tuple[IntegrityFinding, ...]:
    return tuple(
        {
            (
                finding.code,
                finding.message,
                finding.severity,
                finding.expected,
                finding.actual,
            ): finding
            for finding in findings
        }.values()
    )


def _file_sha256(path: Path) -> bytes:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()
