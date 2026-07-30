# Política de seguridad de dependencias

Estado verificado: 26 de julio de 2026.

Los dos lockfiles se generan con Python 3.12, fijan el grafo completo y contienen hashes. CI instala
con `--require-hashes`, ejecuta `pip check` y bloquea cualquier vulnerabilidad conocida mediante
`pip-audit` salvo la excepción explícita que sigue.

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

## Componente administrado llmfit

`llmfit` no es una dependencia importada ni forma parte de los lockfiles: Parsezen descarga el
binario MIT de Windows desde las releases de `AlexsJones/llmfit` cuando el usuario solicita una
recomendación. Consulta como máximo una vez cada siete días y puede actualizar ese componente sin
reinstalar la aplicación.

La descarga fija repositorio, arquitectura, nombre, esquema HTTPS y hosts de redirección; limita
metadatos, archivo y ejecutable; exige el digest SHA-256 comunicado por GitHub; extrae una única
entrada `llmfit.exe`; ejecuta `--version` antes de activarla atómicamente y conserva el hash para
verificar cada uso posterior. También conserva y verifica el texto MIT incluido en la release junto
al ejecutable. Ante cualquier fallo se usa la copia anterior ya verificada. El
artefacto 1.1.4 comprobado el 21 de julio de 2026 no presentaba firma Authenticode aunque el README
del proyecto afirme que los binarios de Windows se firman, por lo que el código no considera esa
firma una garantía disponible. El SHA-256 evita corrupción o sustituciones fuera del canal fijado,
pero una cuenta o release upstream comprometida sigue siendo un riesgo residual propio de cualquier
autoactualización. Por ello no se aceptan forks, URLs configurables ni versiones preliminares.

El comando de recomendación se ejecuta localmente con el dashboard desactivado y sin heredar una
clave de LocalMaxxing. No recibe documentos. La comprobación de releases contacta GitHub. Para una
variante inferida, Parsezen consulta como máximo tres manifiestos pequeños en
`registry.ollama.ai`: exige HTTPS, bloquea redirecciones, limita la respuesta, valida esquema,
digests y capas, y usa su suma como tamaño real. Solo transmite el identificador público del modelo;
no transmite hardware, documentos, rutas ni preferencias.

## Revisión

No se actualiza todo automáticamente. Una corrección compatible se valida con lint, tipos, tests,
conversión real, traducción real y arranque del ejecutable. Las actualizaciones mayores requieren
una mejora concreta y medible. Tras cambiar dependencias se regeneran ambos lockfiles y los avisos de
terceros del paquete.
