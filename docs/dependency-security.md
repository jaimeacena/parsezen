# Política de seguridad de dependencias

Estado verificado: 12 de agosto de 2026.

Los dos lockfiles se generan con Python 3.12, fijan el grafo completo y contienen hashes. CI instala
con `--require-hashes`, ejecuta `pip check` y bloquea cualquier vulnerabilidad conocida mediante
`pip-audit` salvo la excepción explícita que sigue. La auditoría también falla si `pip-audit` omite
una dependencia inesperada. Las variantes Windows `torch` y `torchvision` con sufijo `+cpu` se
vinculan obligatoriamente con la misma versión canónica, auditada desde `requirements.lock`.

## Excepción temporal: CVE-2026-54499 en Stanza

Argos Translate 1.11.0 exige actualmente `stanza==1.10.1`. Stanza anterior a 1.12.2 tiene una
vulnerabilidad de deserialización insegura al cargar un modelo `.pt` malicioso. Parsezen no usa
esa ruta: antes de importar o preparar un traductor fuerza `ARGOS_CHUNK_TYPE=MINISBD` y también fija
el enum de Argos en memoria. De este modo la segmentación se realiza con MiniSBD y nunca se construye
ni carga un pipeline o modelo Stanza. Hay una prueba de regresión que impide retirar esta guarda por
accidente.

Por ello CI ignora **solo** `CVE-2026-54499`; cualquier otra vulnerabilidad continúa fallando el
build. La excepción debe eliminarse cuando Argos publique una versión compatible con Stanza 1.12.2 o
superior y esa versión supere las pruebas reales de traducción. No se instalará Stanza 1.12.2 a la
fuerza mientras contradiga el requisito exacto de Argos, porque produciría un entorno inconsistente.

Referencias primarias:

- [Aviso de seguridad de Stanza](https://github.com/stanfordnlp/stanza/security/advisories/GHSA-v5jw-96jm-7h2c)
- [Corrección publicada en Stanza 1.12.2](https://github.com/stanfordnlp/stanza/releases/tag/v1.12.2)
- [Argos Translate 1.11.0 en PyPI](https://pypi.org/project/argostranslate/)

## Componentes de IA local

Parsezen ya no descarga, actualiza ni ejecuta herramientas de recomendación de modelos. La interfaz
activa muestra únicamente las capacidades fijadas por el catálogo de Parsezen. Sus comprobaciones
usan el transporte de loopback y solo leen `/api/version`, `/api/tags` y `/api/show`; no reciben
documentos ni aceptan endpoints configurables.

Un componente se considera preparado únicamente cuando el manifest local y el digest anunciado por
Ollama coinciden. Los estados `Descargable` e `Insuficiente` son decisiones fail-closed de la
evaluación y no autorizan descargar un tag arbitrario. Cualquier futuro instalador deberá recibir
solo una capacidad del catálogo y volver a comprobar `/api/tags` antes de publicar el estado.

## Revisión

No se actualiza todo automáticamente. Una corrección compatible se valida con lint, tipos, tests,
conversión real, traducción real y arranque del ejecutable. Las actualizaciones mayores requieren
una mejora concreta y medible. Tras cambiar dependencias se regeneran ambos lockfiles y los avisos de
terceros del paquete.
