"""Tiny deterministic PDFs covering high-risk extraction families in seconds."""

from __future__ import annotations

from pathlib import Path


def write_canary_suite(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    canaries = {
        "contents": root / "contents.pdf",
        "columns": root / "columns.pdf",
        "formatting": root / "formatting.pdf",
    }
    _write_contents(canaries["contents"])
    _write_columns(canaries["columns"])
    _write_formatting(canaries["formatting"])
    return canaries


def _write_contents(destination: Path) -> None:
    content = b"""BT
/F2 18 Tf
72 740 Td
(CONTENTS) Tj
ET
BT
/F2 12 Tf
72 700 Td
(1. FIRST CHAPTER                                      5) Tj
ET
BT
/F3 12 Tf
90 676 Td
(Opening principles                                    7) Tj
ET
BT
/F2 12 Tf
72 640 Td
(2. SECOND CHAPTER                                    19) Tj
ET
BT
/F3 12 Tf
90 616 Td
(Worked examples                                      23) Tj
ET
BT
/F2 12 Tf
72 580 Td
(3. FINAL CHAPTER                                     41) Tj
ET"""
    _write_single_page(destination, content)


def _write_columns(destination: Path) -> None:
    content = b"""BT
/F1 12 Tf
54 720 Td
(Left column first sentence.) Tj
ET
BT
/F1 12 Tf
330 720 Td
(Right column first sentence.) Tj
ET
BT
/F1 12 Tf
54 690 Td
(Left column second sentence.) Tj
ET
BT
/F1 12 Tf
330 690 Td
(Right column second sentence.) Tj
ET
BT
/F1 12 Tf
54 660 Td
(Left column final sentence.) Tj
ET
BT
/F1 12 Tf
330 660 Td
(Right column final sentence.) Tj
ET"""
    _write_single_page(destination, content)


def _write_formatting(destination: Path) -> None:
    content = b"""BT
/F2 14 Tf
72 720 Td
(Bold source emphasis) Tj
ET
BT
/F3 13 Tf
72 680 Td
(Italic source emphasis) Tj
ET
BT
/F1 12 Tf
72 640 Td
(Formula E = mc^2 and reference [12-14] remain exact.) Tj
ET"""
    _write_single_page(destination, content)


def _write_single_page(destination: Path, content: bytes) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R /F2 5 0 R /F3 6 0 R >> >> "
            b"/Contents 7 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Italic >>",
        _stream(content),
    ]
    _write_pdf(destination, objects)


def _stream(content: bytes) -> bytes:
    return b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"


def _write_pdf(destination: Path, objects: list[bytes]) -> None:
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{object_number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    document.extend(f"startxref\n{xref_offset}\n%%EOF\n".encode())
    destination.write_bytes(document)


__all__ = ["write_canary_suite"]
