# Publicar una versión de Parsezen

Este documento describe la entrega gratuita mediante GitHub Releases. El instalador no utiliza
firma Authenticode y Windows puede mostrar `Editor desconocido`; el checksum y la atestación de
GitHub permiten comprobar integridad y procedencia, pero no eliminan ese aviso.

## Preparar

1. Trabajar desde una rama revisada y sin cambios locales pendientes.
2. Actualizar `version` en `pyproject.toml` con formato `X.Y.Z`.
3. Ejecutar `python scripts/sync_version.py` y completar `CHANGELOG.md`.
4. Confirmar que no se han añadido documentos, resultados, cachés, credenciales ni rutas privadas.
5. Revisar [`work-plan.md`](work-plan.md): todo mecanismo `EXPERIMENTAL` debe seguir aislado y las
   limitaciones relevantes para la versión deben estar reflejadas en la guía o las notas.
6. Completar [`acceptance-checklist.md`](acceptance-checklist.md) sobre el commit candidato y conservar
   el registro de evidencia sin contenido descrito allí.

## Validar

```powershell
python scripts/sync_version.py --check
python -m ruff check .
python -m ruff format --check .
python -m mypy src/parsezen
python -m pytest --cov=parsezen --cov-report=term-missing --cov-fail-under=88
python -m pip check
python -m pip_audit -r requirements.lock --no-deps --disable-pip --ignore-vuln CVE-2026-54499
```

La excepción `CVE-2026-54499` corresponde a la versión de Stanza fijada por Argos 1.11.0. Parsezen
fuerza MiniSBD antes de cargar cualquier paquete y no usa ese segmentador, pero la excepción debe
revisarse en cada release y retirarse en cuanto Argos permita una versión corregida compatible.

La validación editorial con EPUBCheck y el candidato CPU de Windows deben terminar correctamente en
GitHub Actions. Antes de publicar se prueba el instalador exacto en un perfil limpio, sin Python,
incluidos instalación, arranque, actualización, desinstalación y conservación de datos.

## Empaquetar

El workflow `Paquete de Windows` instala únicamente `requirements-windows-cpu.lock`, genera
`THIRD-PARTY-NOTICES.txt`, construye el paquete PyInstaller, ejecuta `--package-smoke` y crea:

- `Parsezen-Setup-X.Y.Z.exe`;
- `Parsezen-Setup-X.Y.Z.exe.sha256`;
- `python-environment.json`.

Una ejecución manual crea un candidato. Una etiqueta `vX.Y.Z` solo se acepta si su commit pertenece
a `main`; vuelve a auditar el lock CPU, añade la atestación de procedencia y crea o actualiza una
GitHub Release en borrador con los tres archivos generados por esa misma ejecución.

## Publicar

1. Integrar la rama validada en `main`.
2. Crear la etiqueta anotada `vX.Y.Z` sobre el commit integrado.
3. Esperar a que finalicen Calidad, EPUBCheck y Paquete de Windows.
4. Abrir la Release en borrador creada por el workflow y descargar sus tres archivos.
5. Verificar checksum, versión y arranque del instalador exacto adjunto.
6. Revisar las notas, publicar y volver a descargar el instalador público para verificarlo.

No se reutilizan binarios locales ni artefactos de otra ejecución. No se reescriben etiquetas
publicadas.
