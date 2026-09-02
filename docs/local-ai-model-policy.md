# Política de modelos de IA local

## Estado

Esta política fija la pila de componentes propiedad de Parsezen. El runtime consume snapshots
locales independientes para traducción y revisión, y la interfaz activa muestra únicamente esas
dos capacidades fijas. No existe un asistente de recomendaciones, un selector genérico ni un campo
para tags o endpoints arbitrarios.

El componente de traducción fijado es `parsezen/hymt-translation:Q4_K_M`, basado en Hy-MT2 Q4_K_M.
El componente de revisión fijado es `parsezen/lfm-review:Q6_K`, basado en LFM Q6_K. Sus manifests
fijan digest, contexto, adaptador, parámetros, licencias y procedencia; no se ofrecen variantes
alternativas en la UI. «Fijado» acredita identidad y uso dentro de su contrato, no corrección
semántica universal ni autorización para aprobar cambios sin una persona.

## Decisión de producto

La dirección implementada separa capacidades concretas en lugar de exponer un modelo conversacional
arbitrario:

| Capacidad | Contrato objetivo | Estado |
|---|---|---|
| Traducción con IA | `parsezen/hymt-translation:Q4_K_M` | baseline EN→ES verificado en corpus |
| Revisión bilingüe, revisión de contenido y estructura | `parsezen/lfm-review:Q6_K` | propuestas protegidas y supervisadas; autoaceptación no aprobada |
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
proponer una revisión dirigida, pero nunca arrancan Ollama por sí solas. La revisión adicional
proactiva sigue siendo una decisión explícita para trabajos que justifiquen su
coste y declara su cobertura real; solicitarla no significa que cada bloque haya recibido una
respuesta válida.

### Alcances que no deben confundirse

La política separa cuatro autorizaciones:

1. **instalable**: procedencia, licencia y requisitos permiten preparar el artefacto;
2. **preparado**: manifest, digest y metadatos locales coinciden;
3. **utilizable bajo guardas**: puede generar una traducción o propuesta que el código volverá a
   validar y, cuando corresponda, mostrará a una persona;
4. **promocionable automáticamente**: calidad demostrada sobre el corpus completo para aplicar una
   modificación sin decisión humana.

Hy-MT2 está verificado como baseline de traducción EN→ES bajo las guardas y límites documentados. LFM
está fijado como generador local de propuestas de revisión y estructura dentro de una puerta humana.
Ningún revisor actual dispone de autorización de promoción semántica automática. La interfaz puede
mostrar un componente `Preparado` sin afirmar el cuarto alcance.

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
bilingüe repetida terminó 9/9 sin falsos positivos ni falsos negativos. Esta fue una criba de
selección, menor que el corpus exigido para promoción automática; la evidencia ampliada posterior es
la que determina el alcance operativo vigente.

### Revalidación ampliada

La selección histórica no sustituye la evaluación sobre documentos representativos del uso real. La
revalidación privada EN→ES quedó congelada con 41 referencias humanas: 40 lingüísticas y una
estructural. Mezcla documentos, autores, prosa, títulos, índices, tablas, instrucciones y registros;
cada referencia recibió adecuación y fluidez 5/5, sin error crítico, tras las rondas humanas
necesarias. El criterio editorial es español internacional natural, tratamiento de «tú» cuando el
original se dirige al lector, conservación del registro del autor y equivalentes técnicos asentados.

Sobre esas 41 referencias, Hy-MT2 superó 41/41 puertas duras y obtuvo chrF medio 76,935; Qwen3.5-9B
superó 39/41 y obtuvo 73,911. Hy-MT2 ganó 27 casos, Qwen 13 y hubo un empate. chrF fue solo una señal
de cribado: la revisión humana reveló errores de naturalidad, terminología, gramática, elección
léxica, sentido, tratamiento y registro que las guardas estructurales no pretenden decidir.

La memoria terminológica de producto se limita a sintagmas astrológicos completos y se activa solo
con varias señales inequívocas del dominio. En los 13 casos afectados conservó las puertas duras y
mejoró nueve resultados, dejó tres iguales y corrigió aparte el único retroceso aparente de
capitalización. No se incorporaron sustituciones aisladas ambiguas como `chart` o `agenda`.

También se ensayaron dos cambios globales pequeños sobre Hy-MT2 y se rechazaron. Añadir contexto e
instrucciones generales al prompt oficial degradó 13 de 14 casos, con una variación media de chrF de
−3,475 y una puerta dura perdida. Añadir `repeat_penalty=1.05` dejó un empate inestable —cuatro casos
mejoraron, cuatro empeoraron—, redujo ligeramente la media (−0,065) y perdió una puerta dura. El
adaptador mantiene por ello el prompt y los parámetros de producción ya aprobados; estas ablaciones
no justifican una variante global.

El 2 de septiembre de 2026 se comprobó también el par Argos EN→ES realmente instalado como posible
segundo motor para residuos de IA. Funcionó en alguna frase de prosa, pero dejó rótulos breves sin
traducir y produjo al menos una salida corrupta; no alcanza la precisión necesaria para decidir por
tipo de bloque. Se rechaza por ello cualquier degradación automática de párrafos o tablas a Argos. Su
alcance continúa siendo el motor completo elegido explícitamente y el respaldo, sin descargas, de un
único título residual que supere las guardas compartidas.

LFM y Qwen se evaluaron aparte como revisores bilingües. La pasada general no corrigió los cuatro
casos difíciles seleccionados. El piloto focalizado posterior usó un vocabulario cerrado de cinco
categorías —sentido, terminología, gramática, registro y naturalidad— sobre diez errores humanos
sembrados y diez referencias limpias. LFM no alteró ningún bloque: mantuvo 20/20 puertas duras y cero
falsos positivos, pero obtuvo recall cero; 32 respuestas fueron rechazadas en 37 peticiones. Qwen
tampoco modificó ninguno de los diez casos sembrados, aunque respetó el contrato sin rechazos. Por
tanto el foco cerrado permanece solo como instrumento experimental, aislado por su propia clave de
checkpoint, y no se activa en el flujo normal. Un modelo futuro deberá superar las puertas de
revisión sembrada y controles limpios antes de cambiar esta decisión.

Se descartó también abreviar artificialmente el razonamiento de LFM para obtener antes el array JSON.
En diez referencias humanas produjo más respuestas completas, pero modificó siete casos, degradó
cinco frente a la referencia y redujo el chrF medio en 2,636 puntos; las guardas rechazaron además 40
propuestas en 47 peticiones. El adaptador conserva por ello la plantilla de razonamiento aprobada y
no interpreta una respuesta JSON válida como evidencia de corrección semántica.

#### Piloto residual sobre holdouts

El 31 de agosto de 2026 se cerró un piloto posterior sobre tres holdouts completos. Las comprobaciones
detectaron 67 señales residuales. Un clasificador local doble dejó una como falso positivo seguro por
consenso; los correctores locales solo lograron producir 12 propuestas que superaban las guardas y
merecían revisión humana. Tras cuatro paquetes de decisiones y varias rondas de corrección:

- una propuesta quedó aprobada y se incorporó al corpus privado;
- once propuestas no alcanzaron aprobación humana;
- 54 señales nunca obtuvieron una propuesta suficientemente segura;
- 65 señales quedaron finalmente como revisión manual requerida.

Estas 65 señales no equivalen a 65 errores confirmados: mezclan posibles fallos, falsos positivos y
casos cuyo cambio no puede demostrarse automáticamente. La tasa observada de aprobación fue 1/12,
muy inferior al gate de precisión del 98 %. Por tanto, el mecanismo de reparación residual queda
**EXPERIMENTAL y NO PROMOCIONABLE**. Ninguna propuesta dudosa se aplica a libros ni al flujo normal.

La siguiente reevaluación debe distinguir primero fallo de detección, contexto, propuesta o guarda;
no generará otra paráfrasis de los mismos casos sin un mecanismo nuevo. Solo volverá a solicitar
revisión humana tras superar controles limpios y un descarte interno. El conjunto consolidado conserva
conteos, hashes y procedencia de ronda; el único par aprobado permanece privado y los rechazos sirven
como controles negativos.

La auditoría causal privada posterior validó 19 eventos de revisión sobre esos 12 casos. Todas las
propuestas habían superado las guardas mecánicas, pero la aprobación fue 1/12 en la primera ronda y
0/7 en las posteriores. Esto identifica la generación de propuestas como el cuello de botella
observado y demuestra, a la vez, que pasar guardas no acredita sentido ni naturalidad. Las decisiones
no bastan para etiquetar individualmente detector y contexto: ambos permanecen no demostrados. Una
taxonomía local doble de las 13 notas solo alcanzó acuerdo completo en una, por lo que se conserva
como diagnóstico orientativo y nunca como gate.

El descarte privado posterior cambió una sola variable: reemplazó la reescritura completa por parches
mínimos propuestos independientemente. Terminó con 0/13 candidatos por consenso y ninguna
modificación del control limpio. Qwen 3.5 produjo nueve respuestas estructuradas de catorce, pero
Qwen 4B solo una y LFM ninguna; por tanto no existe actualmente una pareja local capaz de sostener el
contrato dual. No se interpreta la ausencia de cambios como aprobación ni se relaja el consenso. La
vía queda **RECHAZADA con los modelos instalados**, no habilita corrección automática y solo puede
reabrirse con una capacidad nueva, no con otra redacción del mismo prompt.

La interfaz privada de revisión permanece ligada al propio equipo por defecto. Cuando la revisión se
hace desde un móvil, solo puede habilitarse de forma explícita en una red privada de confianza —la red
local o una VPN privada del usuario— mediante un enlace temporal largo; sin esa clave, la interfaz no
entrega fragmentos ni acepta decisiones. Este modo no publica contenido en Internet: exige que el
equipo siga encendido y que ambos dispositivos compartan esa red privada. El enlace caduca al cerrar
el servidor de revisión.

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
  protegidos, símbolos de moneda o porcentaje, o negación;
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
