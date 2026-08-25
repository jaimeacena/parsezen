# Política de modelos de IA local

## Estado

Esta política fija la pila de componentes propiedad de Parsezen. El runtime consume snapshots
locales independientes para traducción y revisión, y la interfaz activa muestra únicamente esas
dos capacidades fijas. No existe un asistente de recomendaciones, un selector genérico ni un campo
para tags o endpoints arbitrarios.

La traducción aprobada es `parsezen/hymt-translation:Q4_K_M`, basada en Hy-MT2 Q4_K_M. La revisión
aprobada es `parsezen/lfm-review:Q6_K`, basada en LFM Q6_K. Sus manifests fijan digest, contexto,
adaptador, parámetros, licencias y procedencia; no se ofrecen variantes alternativas en la UI.

## Decisión de producto

La dirección implementada separa capacidades concretas en lugar de exponer un modelo conversacional
arbitrario:

| Capacidad | Contrato objetivo | Estado |
|---|---|---|
| Traducción con IA | `parsezen/hymt-translation:Q4_K_M` | Hy-MT2 Q4_K_M aprobado |
| Revisión bilingüe, revisión de contenido y estructura | `parsezen/lfm-review:Q6_K` | LFM Q6_K aprobado |
| Traducción sin LLM | Argos offline | se conserva sin cambios |
| Arbitraje OCR visual | componente visual independiente | fuera de esta selección |

Los perfiles efectivos están separados por fase y Parsezen conserva una instantánea de la política
verificada en cada trabajo. Las cargas antiguas que solo contienen la pareja global conservan su
identidad de checkpoint legacy; al volver a preparar los componentes, los trabajos editables reciben
los perfiles especializados. La interfaz muestra capacidades y estados, no selectores de modelo.
El contrato objetivo no tendrá excepciones de modelo por documento ni degradación silenciosa:

- si falta el traductor, la traducción con IA no está disponible;
- si falta el revisor o no cubre el par lingüístico, no se ofrece revisión semántica para ese caso;
- Argos solo se usa cuando la persona lo elige;
- el traductor no sustituye al revisor ni el revisor al traductor;
- el árbitro visual no se considera parte de la pila textual garantizada.

El procesamiento directo será el recorrido recomendado. Las comprobaciones deterministas pueden
proponer una revisión dirigida, pero nunca arrancan Ollama por sí solas. La revisión completa sigue
siendo una decisión explícita para trabajos que justifiquen su coste.

## Identidad reproducible

Cada componente especializado aprobado tiene un manifest versionado por Parsezen con, como mínimo:

- capacidad y versión de la política;
- repositorio, revisión upstream y archivo exacto;
- SHA-256 del artefacto upstream cuando exista un archivo distribuible identificable;
- nombre local y digest anunciado por Ollama en `GET /api/tags`;
- formato, familia, cuantización y tamaño;
- licencia, avisos exigidos y decisión de distribución del producto;
- plantilla, parámetros y capacidades comprobados mediante `/api/show`;
- adaptador, versión del prompt y parámetros de generación;
- ventana de contexto aprobada;
- idiomas y tareas validados;
- versión mínima de Ollama y versiones realmente probadas;
- vectores de autoprueba que no contengan texto documental privado.

La instalación especializada solo queda lista cuando el modelo aparezca en `/api/tags` y
sus metadatos coincidan con el manifest. La tarjeta de cada capacidad solo puede emitir su identidad
catalogada; no convierte un nombre de usuario en una orden de instalación. El SHA-256 identifica el artefacto upstream y el digest de Ollama identifica el contenido
registrado localmente; no se presuponen equivalentes. Un tag mutable sin digest no constituye
identidad suficiente. La fijación permite repetir la política y detectar cambios, pero no promete
resultados idénticos bit a bit entre todos los procesadores, controladores y versiones de Ollama.

El soporte directo de nombres `hf.co/...`, URLs o endpoints adicionales queda fuera del primer
incremento. Un GGUF solo puede aprobarse si su procedencia y proceso de cuantización son oficiales o
reproducibles y controlados por Parsezen. Los únicos artefactos de la política actual son los
manifests aprobados de Hy-MT2 Q4_K_M y LFM Q6_K.

## Contratos de petición

El transporte mecánico de loopback, streaming, cancelación, límites y telemetría sigue siendo común.
La evaluación convirtió estas hipótesis en los contratos actuales:

- Hy-MT2 usa su prompt oficial plano y `POST /api/generate` en modo raw, con los parámetros fijados
  en el manifest;
- LFM usa el contrato ChatML raw de revisión, descarta razonamiento acotado y extrae la salida JSON
  completa cuando la tarea lo exige;
- cada adaptador fija contexto y parámetros en su manifest; no hereda automáticamente los valores
  del antiguo modelo general;
- prompts, respuestas y contenido documental nunca se registran.

Parsezen libera explícitamente el componente anterior al cambiar de fase, siempre después de completar
todos sus fragmentos y nunca entre peticiones del mismo lote. El pico de memoria se mide con el
pipeline completo —incluidos conversión y OCR—, no solo con el tamaño de los pesos.

## Evaluación concluida (histórica; no es un selector)

La comparación que cerró la selección incluyó los siguientes comparadores. Sus nombres son
referencia histórica de evaluación y no aparecen como opciones instalables o seleccionables:

| Función | Ganador | Comparadores evaluados |
|---|---|---|
| Traducción | Hy-MT2 Q4_K_M | MiLMMT Q8 y TranslateGemma 4B Q8 |
| Revisión | LFM Q6_K | LFM Q8/QAD y Qwen3.5-9B |

El benchmark no descarga implícitamente comparadores ni recurre a artefactos comunitarios. La
selección consideró la calidad después de las guardas de Parsezen, memoria, tiempo, reintentos y
tamaño instalado. Hy-MT2 superó 27/27 gates, frente a 24/27 de MiLMMT y TranslateGemma. LFM Q6
igualó la calidad de Q8 con menor tamaño y superó a Qwen en precisión monolingüe; la comprobación
bilingüe repetida terminó 9/9 sin falsos positivos ni falsos negativos.

Traducción, revisión bilingüe, limpieza monolingüe y estructura se evalúan por separado. Un resultado
general de seguimiento de instrucciones no demuestra por sí solo capacidad de corrección semántica.
La disponibilidad final del revisor se calcula por tarea y par lingüístico; no se extrapola a idiomas
que el modelo o el corpus no cubren.

## Corpus privado

Los documentos privados permanecen fuera del repositorio y del contexto de servicios remotos. Los
procesa únicamente Parsezen con sus herramientas locales y Ollama en `127.0.0.1`. Los informes no
incluyen títulos, rutas, prompts, respuestas ni texto documental.

El piloto usa de tres a cuatro páginas por documento largo:

1. una página de prosa densa del primer tercio, evitando portada y créditos;
2. una página intermedia con estructura, tabla o imagen cuando exista;
3. una página de prosa del último tercio;
4. opcionalmente, una página difícil detectada localmente por OCR, layout o calidad de texto.

La selección de páginas se guarda como índices y huellas opacas en un manifest privado. El informe
compartible solo contiene identificadores de caso no reversibles, opciones, versiones, métricas y
decisiones agregadas. Los originales y las salidas completas no se copian junto al informe.

El corpus combina, como mínimo, prosa, títulos e índices, números y nombres propios, tablas u OCR, y
obliga a producir EPUB en al menos un caso. Una muestra pequeña sirve para descartar candidatos; no
basta por sí sola para aprobar un modelo. La aprobación exige al menos 40 segmentos de traducción por
par lingüístico, 20 errores sembrados por tarea de revisión y 100 bloques limpios. Un conjunto menor
se etiqueta `SMOKE_ONLY`.

Cada caso aprobado se ejecuta tres veces con las mismas opciones. Si la salida no tiene el mismo
SHA-256, las tres ejecuciones deben superar por separado todos los gates duros, conservar cero fallos
críticos y mantener los mismos contadores de incidencias críticas; la divergencia queda registrada.

El manifest privado puede contener rutas e identidad fuerte para volver a encontrar los originales,
pero permanece fuera del repositorio y nunca se comparte. Se almacena en un espacio local protegido
por la cuenta del sistema y su retención se decide explícitamente al cerrar la evaluación. El informe
compartible sigue sin incluir rutas, títulos, texto ni identificadores reversibles.

## Gates de aceptación

### Privacidad, integridad y compatibilidad

Los siguientes gates protegen el producto y se aplican a cada ejecución, no son avisos lingüísticos:

- un documento original cambia;
- una petición sale de loopback o un informe contiene contenido documental;
- se publica una salida inválida o aparece una regresión estructural EPUB;
- una modificación aceptada altera cifras, fechas, URLs, código, marcadores, tablas, nombres
  protegidos o negación;
- el sistema considera listo un modelo cloud, una capacidad incompatible o un digest distinto;
- el pipeline supera el límite de pico RSS fijado antes de ejecutar el corpus para el equipo objetivo
  de 16 GB, con entorno y carga secuencial descritos en el manifest privado.

Los rechazos de las guardas son fallos operativos cuantificados, no contenido que deba forzarse en la
salida. Los avisos no bloqueantes de longitud, idioma global o alineación dudosa siguen orientando la
revisión humana y no se convierten en una puntuación única.

### Traducción

El evaluador local versionado calcula los gates automáticos independientemente por par lingüístico.
Los umbrales que requieren valoración humana se mantienen como criterio para ampliar el corpus. Sobre
el corpus etiquetado, el candidato aprobado debe cumplir:

- 100 % de integridad en las guardas deterministas de cifras, enlaces, Markdown, tablas y cobertura;
- ninguna respuesta o sustitución rechazada se publica; el bloque original puede conservarse como
  fallback seguro y el caso queda señalado para revisión;
- al menos el 95 % de segmentos con adecuación humana de 4/5 o superior;
- al menos el 90 % de segmentos con fluidez humana de 4/5 o superior;
- cero regresiones en fallos críticos frente al modelo general actual;
- tasas de texto original residual, cobertura y adhesión al glosario que no empeoren más de dos
  puntos porcentuales frente al baseline; cualquier margen distinto debe justificarse y registrarse
  antes de ejecutar el corpus.

Estas tasas comparan candidatos para seleccionar un modelo. No convierten los avisos no bloqueantes
del producto en rechazos automáticos de una salida documental.

COMET o XCOMET pueden complementar opcionalmente la evaluación con artefactos ya descargados y
ejecución offline. No son un gate obligatorio, no se añaden a las dependencias de producto ni
sustituyen la revisión ciega. Sus licencias y descargas también deben revisarse antes de usarlas.

### Revisión y estructura

El corpus de revisión incluye errores sembrados y bloques limpios. Cada propuesta se etiqueta como
verdadero positivo, falso positivo o error omitido. Los resultados se calculan por separado para
revisión bilingüe, limpieza monolingüe y estructura, además de por idioma o par lingüístico. El
candidato aprobado debe alcanzar:

- precisión igual o superior al 98 % sobre todas las propuestas emitidas;
- cero falsos positivos críticos que cambien significado, cifras, nombres o negación;
- tasa de modificación propuesta sobre bloques limpios igual o inferior al 1 %;
- recall igual o superior al 80 % sobre los errores explícitamente sembrados;
- toda directiva estructural aceptada conserva las palabras visibles y supera las guardas, mientras
  precisión y recall se calculan contra los errores estructurales etiquetados;
- cero respuestas inválidas que atraviesen las guardas o se publiquen parcialmente.

La precisión tiene prioridad sobre el recall. Un modelo que no propone nada obtiene recall cero, no
una aprobación vacía. Las propuestas rechazadas, las respuestas inválidas y los reintentos se
informan por separado; el número de propuestas nunca se interpreta como calidad.

### Rendimiento y decisión final

Solo se compara rendimiento entre candidatos que ya superaron privacidad, integridad y calidad. Se
registran mediana y p95 de tiempo, pico de RAM/VRAM, tokens, reintentos, rechazos, carga del modelo y
tamaño final en disco. Una diferencia inferior al 10 % no justifica elegir un modelo de peor calidad.

La aprobación requiere además:

- revisión explícita de las licencias Gemma y LFM y de los avisos de redistribución;
- artefactos oficiales o una cuantización propia reproducible;
- cobertura documentada por idioma y tarea;
- autopruebas de instalación, carga, traducción y salida estructurada;
- una decisión registrada con las versiones y resultados que la sustentan.

Las fuentes upstream iniciales de la evaluación son los repositorios oficiales de
[MiLMMT](https://huggingface.co/xiaomi-research/MiLMMT-46-4B-v1.0),
[LFM2.5](https://huggingface.co/LiquidAI/LFM2.5-2.6B), su
[GGUF oficial](https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF),
[HY-MT2](https://huggingface.co/tencent/Hy-MT2-7B) y
[TranslateGemma](https://huggingface.co/google/translategemma-4b-it). Las comprobaciones de Ollama
se basan en sus API oficiales de [`/api/tags`](https://docs.ollama.com/api/tags) y
[`/api/show`](https://docs.ollama.com/api-reference/show-model-details). Antes de fijar una versión se
vuelven a revisar esas fuentes; un enlace no sustituye el SHA-256 ni el digest local del manifest.

## Secuencia implementada

1. Se añadieron manifests y verificación de digest/metadatos fail-closed.
2. Se incorporaron los adaptadores concretos sin debilitar las guardas existentes.
3. Se ejecutó la matriz y se fijaron artefactos, contexto y parámetros ganadores.
4. Se persistió la instantánea por fase en trabajos y checkpoints, conservando las claves legacy
   cuando no existen perfiles especializados.
5. Se conectaron hardware, instalación fija y estados de componentes.
6. Se validó la vista de `Traducción IA` y `Revisión IA`, incluida la licencia LFM antes de descargar.
7. Procesamiento directo es el valor inicial y la revisión dirigida continúa siendo voluntaria.

Cada punto se desarrolló como incremento verificable. La compatibilidad de cargas y checkpoints
anteriores se conserva mediante fallbacks y claves legacy explícitas.
