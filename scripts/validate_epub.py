"""Compare EPUBCheck errors in an original EPUB and its generated copies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

EPUBCHECK_VERSION = "5.3.0"
ERROR_SEVERITIES = frozenset({"ERROR", "FATAL"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One EPUBCheck message at one internal publication path."""

    severity: str
    identifier: str
    path: str
    message: str

    @property
    def signature(self) -> tuple[str, str, str, str]:
        """Stable identity that deliberately ignores line and column changes."""
        return (self.severity, self.identifier, self.path, self.message)


@dataclass(frozen=True, slots=True)
class EpubCheckReport:
    """Small parsed subset of one EPUBCheck JSON report."""

    findings: tuple[Finding, ...]

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity in ERROR_SEVERITIES)


def load_report(path: Path) -> EpubCheckReport:
    """Read EPUBCheck JSON and expand messages with multiple locations."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el informe EPUBCheck {path.name}.") from exc
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("El informe EPUBCheck no contiene una lista de mensajes válida.")

    findings: list[Finding] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("El informe EPUBCheck contiene un mensaje no válido.")
        severity = str(message.get("severity", "")).upper()
        identifier = str(message.get("ID", "UNKNOWN"))
        text = _normalize_message(str(message.get("message", "")))
        locations = message.get("locations") or [{}]
        if not isinstance(locations, list):
            locations = [{}]
        for location in locations:
            internal_path = ""
            if isinstance(location, dict):
                internal_path = str(location.get("path") or "")
            findings.append(Finding(severity, identifier, internal_path, text))
    return EpubCheckReport(tuple(findings))


def new_errors(
    original: EpubCheckReport,
    generated: EpubCheckReport,
) -> tuple[Finding, ...]:
    """Return generated errors that are not already present in the original."""
    remaining = Counter(finding.signature for finding in original.errors)
    introduced: list[Finding] = []
    for finding in generated.errors:
        signature = finding.signature
        if remaining[signature]:
            remaining[signature] -= 1
        else:
            introduced.append(finding)
    return tuple(introduced)


def run_epubcheck(
    epub_path: Path,
    *,
    java_path: Path,
    jar_path: Path,
    timeout_seconds: int = 120,
) -> EpubCheckReport:
    """Run the pinned EPUBCheck tool and parse its structured report."""
    if not epub_path.is_file() or epub_path.suffix.lower() != ".epub":
        raise ValueError(f"No existe un EPUB válido en {epub_path}.")
    if not java_path.is_file():
        raise ValueError(f"No se encontró Java en {java_path}.")
    if not jar_path.is_file():
        raise ValueError(f"No se encontró EPUBCheck en {jar_path}.")

    _require_pinned_version(java_path, jar_path, timeout_seconds)
    with tempfile.TemporaryDirectory(prefix="parsezen-epubcheck-") as temporary:
        report_path = Path(temporary) / "report.json"
        command = [
            str(java_path),
            "-Duser.language=en",
            "-Dfile.encoding=UTF-8",
            "-jar",
            str(jar_path),
            "--json",
            str(report_path),
            str(epub_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"EPUBCheck no pudo analizar {epub_path.name}.") from exc
        if not report_path.is_file():
            details = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"EPUBCheck no generó el informe para {epub_path.name}: {details or 'sin detalles'}"
            )
        return load_report(report_path)


def _require_pinned_version(java_path: Path, jar_path: Path, timeout_seconds: int) -> None:
    try:
        completed = subprocess.run(
            [str(java_path), "-jar", str(jar_path), "--version"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("No se pudo comprobar la versión de EPUBCheck.") from exc
    version_output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or EPUBCHECK_VERSION not in version_output:
        raise RuntimeError(f"Las pruebas requieren EPUBCheck {EPUBCHECK_VERSION}.")


def _normalize_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip()


def _configured_java_path(argument: str | None) -> Path | None:
    candidate = argument or os.environ.get("EPUBCHECK_JAVA") or shutil.which("java")
    return Path(candidate).resolve() if candidate else None


def _configured_jar_path(argument: str | None) -> Path | None:
    candidate = argument or os.environ.get("EPUBCHECK_JAR")
    return Path(candidate).resolve() if candidate else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Comprueba que los EPUB generados por Parsezen no introduzcan errores nuevos.")
    )
    parser.add_argument("original", type=Path, help="EPUB original usado como referencia")
    parser.add_argument("generated", type=Path, nargs="+", help="EPUB generado que se comprobará")
    parser.add_argument("--java", help="Ruta al ejecutable java de desarrollo")
    parser.add_argument("--jar", help=f"Ruta a epubcheck.jar {EPUBCHECK_VERSION}")
    parser.add_argument("--timeout", type=int, default=120, help="Límite por archivo en segundos")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the internal comparison command."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    java_path = _configured_java_path(arguments.java)
    jar_path = _configured_jar_path(arguments.jar)
    if java_path is None or jar_path is None:
        parser.error("Configura --java y --jar, o EPUBCHECK_JAVA y EPUBCHECK_JAR.")

    try:
        original_report = run_epubcheck(
            arguments.original.resolve(),
            java_path=java_path,
            jar_path=jar_path,
            timeout_seconds=arguments.timeout,
        )
        failed = False
        print(f"Original: {len(original_report.errors)} errores técnicos.")
        for generated_path in arguments.generated:
            generated_report = run_epubcheck(
                generated_path.resolve(),
                java_path=java_path,
                jar_path=jar_path,
                timeout_seconds=arguments.timeout,
            )
            introduced = new_errors(original_report, generated_report)
            print(
                f"{generated_path.name}: {len(generated_report.errors)} errores, "
                f"{len(introduced)} nuevos."
            )
            for finding in introduced:
                print(
                    f"  {finding.severity} {finding.identifier} "
                    f"{finding.path or '(paquete)'}: {finding.message}"
                )
            failed = failed or bool(introduced)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
