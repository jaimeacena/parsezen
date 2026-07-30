# Parsezen

![Parsezen](assets/branding/generated/parsezen-readme.png)

Parsezen es una aplicación de escritorio para convertir, traducir, corregir y personalizar
documentos de forma local. Está diseñada para una persona que quiere preparar Markdown, Word,
texto o libros EPUB sin depender de servicios documentales remotos.

**Versión 1.1.0 · Windows x64 · Python 3.12 · licencia MIT**

## Cómo se usa

1. Arrastra uno o varios documentos a la zona discontinua situada bajo la cola, o púlsala para
   seleccionarlos.
2. Abre **Configurar**. El resumen del plan y la navegación lateral reúnen Resultado, Traducir,
   Corregir, Personalizar e IA local sin repartir la configuración entre ventanas.
3. Elige un destino general en la cabecera. Cada documento muestra ese destino como un botón y
   permite elegir directamente otra carpeta cuando sea una excepción.
4. Comprueba en la cola el tiempo automático aproximado y pulsa la acción contextual de la
   cabecera: `Procesar` o `Revisar`, según corresponda.
5. Parsezen ejecuta una tarea pesada cada vez. Si un documento necesita revisión, lo deja en espera
   y continúa con el siguiente.
6. Revisa únicamente las fases que requieren una decisión y abre el resultado final.

La pantalla principal representa siempre el mismo recorrido:

| Documento | Flujo | Salida | Siguiente paso |
| --- | --- | --- | --- |
| origen, formato, tamaño y páginas | operaciones activadas | formato y destino | progreso y acción necesaria |

La columna **Flujo** resume traducción, corrección y personalización sin convertir la tabla en un
formulario y muestra su orden mediante conectores. **Salida** reúne el formato y el destino, y
**Siguiente paso** muestra la fase exacta pendiente, activa, bloqueada o fallida. Las acciones de cada
trabajo viven en su propia celda; no existe un segundo inspector que duplique la tabla. Durante el
trabajo, la fila activa y el resumen de la cabecera quedan destacados sin repetir información.

## Funciones principales

- Cola de trabajos independientes, reordenable y recuperable.
- Apariencia del sistema por defecto, con temas claro y oscuro persistentes y cambio sin recarga.
- Procesamiento secuencial para limitar CPU, memoria y GPU.
- Conversión entre TXT, Markdown, DOCX, PDF y EPUB según las capacidades de cada formato.
- Salidas TXT, Markdown, DOCX y EPUB.
- Rango de páginas independiente para cada PDF.
- OCR local adaptativo: puntúa la extracción nativa, analiza solo las páginas dudosas y sustituye
  su texto únicamente cuando el resultado OCR es objetivamente mejor.
- Traducción offline con Argos Translate o mediante un modelo local de Ollama.
- Respaldo por párrafos y frases: una unidad dudosa no descarta traducciones vecinas ya validadas.
- Omisión automática de traducciones redundantes cuando el documento ya está en el idioma elegido.
- Traducción y corrección con IA fusionadas en una sola pasada cuando se activan juntas; una
  segunda corrección se limita a páginas con señales reales de conversión u OCR.
- Memoria terminológica local para mantener firmas, organizaciones y nombres propios repetidos sin
  congelar encabezados, números romanos ni traducciones ordinarias.
- Corrección conservadora con IA, reintentos específicos según la incidencia, detección de
  propuestas arriesgadas y revisión manual ordenada por gravedad.
- Configuración, revisiones, glosario, modelos y editor dentro de la ventana principal.
- Sistema visual semántico único, contraste AA, foco visible y reflow desde 320 px.
- Revisiones separadas de OCR, traducción, corrección y estructura.
- Editor EPUB normalizado para capítulos, jerarquía, texto y formato básico.
- Pestaña Personalizar previa reducida a la pre-organización opcional de capítulos; todo resultado
  EPUB abre después un editor final con metadatos, portada, estructura y contenido.
- Pausa, reanudación, checkpoints e instantáneas cifradas de revisiones pendientes. Desde
  **Ajustes → Conservar trabajo temporal** se elige una retención de 0, 7, 30 o 90 días.
- Telemetría local por etapa, sin rutas ni contenido, disponible también en la validación real.
- Preanálisis del lote con carga prevista, riesgos comprensibles y estimación temporal que se
  calibra con ejecuciones similares realizadas en el propio equipo.
- Comprobación temprana automática de tres páginas representativas en PDFs largos o inciertos:
  continúa sin intervención cuando la muestra es segura y detiene el trabajo completo con una
  explicación accionable cuando el riesgo se repite.
- Tiempo restante actualizado con el avance real y explicación del cambio cuando el ritmo observado
  obliga a ampliar la estimación inicial.
- Cierre por documento y por lote con integridad técnica, incidencias detectadas y revisión humana
  claramente separadas; actividad reciente limitada a 20 intentos y avisos de Windows solo cuando
  Parsezen no está en primer plano.
- Control final determinista y publicación atómica: Parsezen vuelve a leer el temporal, comprueba
  contenido, estructura y contenedor, y solo entonces lo presenta como resultado.
- Recuperación contextual: cada fallo ofrece reintentar únicamente la fase y el documento
  afectados, revisar su configuración o abrir la IA local según la causa.
- Originales inmutables, logs sin contenido documental y procesamiento local.

## Formatos

| Entrada | Salidas principales | Observaciones |
| --- | --- | --- |
| TXT | TXT, Markdown, EPUB | TXT→TXT requiere traducción o corrección |
| Markdown | Markdown, EPUB | las imágenes locales pueden incorporarse |
| DOCX | DOCX, Markdown, EPUB | DOCX→DOCX conserva el paquete y sus estilos compatibles |
| PDF | Markdown, EPUB | admite páginas, OCR e imágenes |
| EPUB | EPUB, Markdown | la traducción directa puede conservar el paquete o normalizarlo |

Las opciones incompatibles aparecen desactivadas. Parsezen no crea copias idénticas sin una
operación útil.

## Revisiones y editor EPUB

Cada revisión pertenece a un documento y a una fase. El original aparece a la izquierda y la
propuesta editable a la derecha. Para una página PDF, el lado izquierdo muestra la página real.
Puedes conservar el original, usar la propuesta o editarla; las decisiones se guardan para continuar
después. Al volver, la revisión abre el siguiente caso todavía pendiente y señala cuántas decisiones
de prioridad alta quedan. La propuesta aparece preseleccionada para agilizar el recorrido y también
puedes aprobar
de una sola vez todas las traducciones o correcciones de la fase actual. La estructura permanece
siempre como una revisión independiente.

Las fases posteriores no se presentan como completadas mientras una decisión anterior las bloquee.
Al terminar una revisión, Parsezen aplica los cambios y publica ese documento sin esperar a que
revises el resto.

El editor EPUB trabaja sobre un modelo de libro independiente del formato de origen. Permite:

- crear, dividir, unir y renombrar divisiones;
- reordenar, anidar y elevar secciones;
- editar texto, encabezados, negrita, cursiva, subrayado, listas, alineación y enlaces;
- limpiar formato local sin borrar el contenido;
- guardar para continuar más tarde;
- generar un EPUB definitivo mediante escritura atómica.

Quitar una división une su contenido con la sección anterior; no elimina texto.

## IA local guiada

Ollama es opcional. Cuando una función lo necesita, Parsezen guía la instalación, el inicio del
servidor, el modo solo local y la descarga de un modelo sin exigir comandos.

El gestor de modelos:

- detecta los modelos instalados mediante la API local de Ollama;
- analiza el equipo con `llmfit`;
- muestra una opción equilibrada, otra más rápida y otra de mayor capacidad;
- acepta identificadores canónicos `modelo:tag` compatibles con transformación documental;
- permite instalar, elegir o eliminar modelos;
- excluye modelos cloud y bloquea variantes conocidas que consumen la respuesta en razonamiento
  en lugar de devolver el texto transformado.

`IA local`, en la cabecera, muestra su disponibilidad y abre una sola página. `Instalados` aparece
primero para elegir el modelo predeterminado; `Añadir modelo` reúne recomendaciones, búsqueda,
instalación por nombre y el catálogo. La misma página explica cuántos trabajos pendientes heredan
el predeterminado y permite ajustar su contexto.

Cada documento que necesita IA muestra una sección contextual `IA local` dentro de Configurar.
Hereda el predeterminado salvo que el usuario active una elección específica. Cambiar el
predeterminado actualiza únicamente los trabajos pendientes que siguen heredándolo; no altera
excepciones, fallos, revisiones ni ejecuciones iniciadas. Un modelo utilizado por trabajo sin
terminar no puede eliminarse hasta resolver esas dependencias.

Los identificadores se conservan tal como los publica Ollama; Parsezen no crea alias propios. La
guía detallada está en [IA local](docs/local-ai-setup.md).

La arquitectura visual, paletas, tokens, contraste y reglas responsive se documentan en
[Sistema de interfaz](docs/ui-design-system.md).

## Privacidad y recuperación

- Los documentos no se envían a Parsezen ni a servicios remotos.
- Ollama se usa únicamente mediante `127.0.0.1`.
- Argos, OCR y los modelos de IA se ejecutan localmente.
- Las descargas opcionales contienen modelos o herramientas públicas, nunca documentos.
- Los originales no se modifican.
- Los resultados se escriben primero en un temporal, se sincronizan y luego se publican.
- Los checkpoints y el material de revisión se cifran para la cuenta actual de Windows.
- SQLite guarda estado y metadatos de la cola; el contenido revisable permanece en artefactos
  cifrados.
- Al finalizar una revisión se elimina su material temporal. Los checkpoints cifrados se eliminan
  o conservan durante el plazo elegido en Ajustes y siempre pueden borrarse manualmente.
- Los logs no incluyen nombres, rutas, prompts ni contenido documental.

Datos locales:

- preferencias y cola: `%LOCALAPPDATA%\Parsezen\`
- logs: `%LOCALAPPDATA%\Parsezen\Logs\parsezen.log`
- checkpoints y revisiones cifradas: subcarpetas de `%LOCALAPPDATA%\Parsezen\`

Los checkpoints facilitan reanudar un trabajo, pero no sustituyen una copia de seguridad.

## Ejecutar desde el repositorio

Requisitos: Windows x64 y Python 3.12.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
python -m pip install -e . --no-deps --no-build-isolation
python -m parsezen
```

También puedes usar `Abrir Parsezen.cmd`. El lanzador antepone siempre el código de `src` de esta
carpeta, por lo que no abre por error una instalación anterior del entorno virtual.

## Validación

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src/parsezen
python -m pytest
python -m pytest --cov=parsezen --cov-report=term-missing --cov-fail-under=88
python scripts/sync_version.py --check
```

`Validar Parsezen.cmd` ejecuta la aceptación local rápida. `Validar con IA real.cmd` añade un
recorrido optativo de 20 páginas con el modelo de Ollama elegido. La construcción del instalador es
un proceso separado y no forma parte de la validación cotidiana.

Los corpus PDF privados pueden medirse sin guardar texto documental:

```powershell
python scripts/benchmark_documents.py record local-benchmarks/pdf.json libro.pdf --pages 1 100
python scripts/benchmark_documents.py check local-benchmarks/pdf.json
python scripts/validate_real_workflows.py libro.pdf --pages 1 100 --output-directory local-benchmarks/real
python scripts/validate_real_workflows.py libro.pdf --glossary "source term=término fijado"
```

El primer banco registra huellas, estructura, tiempo y memoria del árbol completo de procesos,
incluidos los trabajadores OCR. El recorrido real conserva resultados y checkpoints en el destino
indicado y escribe un informe operativo con tiempos por etapa, señales OCR y conteos semánticos,
pero sin nombres, rutas, prompts ni texto documental. Distingue
una ejecución técnica completada de un control automático de calidad superado; cualquier aviso PDF,
incidencia de idioma, fragmento conservado o encabezado EPUB con dimensiones de párrafo produce
`REVISAR`. Las entradas `--glossary` se aplican al trabajo, pero nunca se copian al informe.

Consulta también:

- [Guía de uso](docs/user-guide.md)
- [Arquitectura](docs/architecture.md)
- [Aceptación antes de publicar](docs/acceptance-checklist.md)
- [Dependencias y seguridad](docs/dependency-security.md)
- [Publicar una versión](docs/releasing.md)

## Límites conocidos

- Un PDF se convierte a contenido estructurado; no reproduce exactamente su diseño visual.
- OCR puede requerir corrección en tipografías decorativas, manuscritos, tablas o varias columnas.
- El texto dibujado dentro de imágenes no se traduce.
- Markdown solo mantiene imágenes de forma portable si sus rutas permanecen estables.
- Todo resultado EPUB pasa por el editor final. Al publicarlo se genera un EPUB refluible
  normalizado; puede simplificar estilos complejos del paquete de origen.
- EPUB con DRM o cifrado de contenido no es compatible.
- La revisión automática reduce trabajo, pero no demuestra equivalencia semántica perfecta.

## Licencia

El código propio se distribuye bajo [MIT](LICENSE). Las dependencias y modelos conservan sus
licencias independientes.
