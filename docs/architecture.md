# Arquitectura de Parsezen

## Objetivos

Parsezen es una aplicación local de escritorio, orientada a una sola persona. La arquitectura
prioriza:

1. no perder ni corromper documentos;
2. representar un estado coherente por documento y fase;
3. limitar el consumo a una tarea pesada simultánea;
4. poder continuar tras una revisión o un cierre;
5. mantener la interfaz separada del procesamiento;
6. evitar infraestructura que no aporte valor a un uso personal.

## Capas

```text
presentation/
  tabla Documento/Flujo/Salida/Siguiente paso, navegación interna y editores
          ↓
application/
  planificación, planificador secuencial, materialización de revisiones y edición de libros
          ↓
domain/
  DocumentJob, StageState, ReviewSession y BookDocument
          ↓
infrastructure/
  SQLite, artefactos cifrados e instantáneas recuperables
          ↓
procesadores existentes
  PDF/OCR, Markdown, DOCX, EPUB, Argos y Ollama
```

`domain` y `application` no importan PySide6. La interfaz renderiza proyecciones inmutables y no
inventa estados al margen del dominio.

## Sistema de presentación

`presentation/design_system.py` es la única fuente de tokens base, colores semánticos, temas,
paleta Qt y vectores comunes. Los componentes consumen roles como `text_primary`,
`action_primary`, `border_focus` o `error`; no fijan pigmentos. No existe una segunda hoja de
estilos ni un shell visual oculto.

La preferencia de apariencia distingue sistema, claro y oscuro. Se aplica antes de construir la
ventana, se persiste con `QSettings`, escucha cambios de Windows cuando corresponde y se propaga a
las vistas ya abiertas. La reducción de movimiento y el alto contraste se delegan a las
preferencias nativas.

`presentation/components.py` contiene solo abstracciones repetidas con una necesidad real:
selector con chevron vectorial, interruptor de teclado, mensaje inline recuperable y tira de
herramientas desplazable. Layout, estado y contenido específico continúan en su pantalla.

El breakpoint compacto es 640 px y todos los flujos esenciales aceptan reflow hasta 320 px. En ese
modo se reorganizan cabecera, columnas, formularios, pies y comparadores; no se ocultan acciones ni
se habilita desplazamiento horizontal accidental. El contrato, paleta y matriz de contraste están
en `docs/ui-design-system.md`.

## Trabajo independiente

`DocumentJob` contiene:

- identidad y orden;
- ruta, formato, tamaño y modificación fijados al preparar el origen;
- configuración completa por documento;
- un perfil de IA compartido por documento, con lectura compatible de configuraciones antiguas;
- revisión de configuración;
- estado de cada fase;
- resultado, avisos y error.

`OutputConfiguration.configured` separa un documento recién añadido de un trabajo ejecutable.
Mientras sea falso, el planificador no lo incluye y `PUBLISH` no participa. La presentación abre
una única transacción de configuración. La interfaz usa un patrón maestro-detalle adaptable: el
resumen superior explica el plan físico, la navegación lateral muestra Resultado, Traducir,
Corregir y Personalizar y el panel contiguo contiene sus opciones. En el breakpoint compacto la
navegación se convierte en lista-detalle, no en un formulario horizontal comprimido. Personalizar,
visible solo para salidas EPUB, contiene únicamente la
activación opcional de la pre-organización de capítulos; metadatos y portada pertenecen al editor
final, pero no introducen una nueva fase:
el guardado automático al volver sigue siendo atómico.
`AIProfileConfiguration` guarda una sola pareja modelo/contexto para todas las fases de IA del
trabajo y `is_custom` distingue una excepción explícita de la herencia global. La sección contextual
`IA local` solo aparece cuando el plan la necesita. La página global administra Ollama, el modelo
predeterminado y su contexto; un cambio se propaga solo a trabajos `QUEUED` heredados. Perfiles
específicos y trabajos fallidos, cancelados, pausados, en revisión o en ejecución conservan su
instantánea. La eliminación se bloquea mientras algún trabajo sin terminar dependa del modelo. Al
leer estados anteriores, `effective_ai_profile` recupera los valores históricos de traducción o
corrección como elecciones específicas; la ausencia del nuevo campo se interpreta como herencia
para estados ya normalizados.

`OutputConfiguration.directory_is_custom` distingue una excepción por documento del destino global
heredado. Un único botón muestra la ruta efectiva y convierte la selección de otra carpeta en una
excepción. Cambiar la preferencia general actualiza solo trabajos editables que no estén marcados
como excepción. El campo se persiste junto al trabajo para que esa intención sobreviva al reinicio.

Las fases son:

1. `PREPARE`: lectura, conversión, OCR y preparación de recursos;
2. `TRANSLATE`;
3. `REFINE`;
4. `STRUCTURE`;
5. `PUBLISH`.

Este orden es la fuente de verdad de dominio y también de la configuración, el resumen del flujo,
las revisiones y la presentación de actividad. El adaptador `runtime_mapping` traduce cada evento
físico al tramo correspondiente; una traducción con Ollama emite `TRANSLATING` y no reutiliza el
estado visual de corrección.

Antes de crear el trabajador, `prepare_queue_run` fija las solicitudes inmutables y valida el lote.
La presentación proyecta ese preflight también como `Preparando` y mantiene la misma etiqueta cuando
el trabajador entra en `VALIDATING`, `READING`, `CONVERTING`, `OCR`, `PRESERVING_IMAGES` o
`STRUCTURING`. Así, los dos niveles de preparación forman un tramo visual continuo sin inventar una
fase intermedia.

El mismo preflight genera una proyección explicable y sin contenido: unidades de trabajo, revisiones
previsibles, advertencias por OCR completo, reconstrucción PDF→EPUB o cambios automáticos sin
revisión, y un intervalo de tiempo automático. La estimación parte de coeficientes conservadores y
se calibra mediante la mediana de hasta 200 ejecuciones locales comparables. SQLite conserva
únicamente formato, opciones, unidades, huella no reversible del modelo y duración; nunca rutas,
nombres, tamaño original ni texto. Un plan solo exige una confirmación adicional si supera media
hora en su
límite superior o contiene un riesgo alto.

`should_run_early_check` limita la comprobación representativa a PDFs cuyo coste o incertidumbre
pueden justificarla: al menos 120 unidades, o 60 con OCR forzado, IA local o salida EPUB.
`run_early_check` elige primera, central y última página dentro del intervalo pedido y ejecuta cada
una mediante el mismo `process_document`, con salidas en un directorio temporal. Una muestra bloquea
solo cuando una incidencia material de extracción/OCR o transformación aparece en dos de tres
páginas; de lo contrario el trabajador continúa sin interacción.

Las claves PDF son independientes del rango y siguen ligadas al contenido y a las opciones, de modo
que la ejecución completa reutiliza la extracción y el OCR de las páginas comprobadas. Los
checkpoints generales de cada muestra y sus archivos de salida se eliminan inmediatamente. El
marcador durable de una muestra superada incluye revisión de configuración, tamaño y fecha del
origen: cualquier cambio obliga a repetirla. Todo el tramo se proyecta como `Preparando` y conserva
la pausa en un punto seguro.

Durante la ejecución, `estimate_remaining_time` descuenta primero el tiempo transcurrido del
intervalo de preflight. Cuando una fase lleva al menos diez segundos y expone progreso real, combina
ese ritmo observado con el margen de las fases pendientes y explica el ajuste en la ayuda de la
fila. Si el límite superior ya se superó, abandona el intervalo y comunica el retraso sin una cuenta
atrás engañosa.

Antes de abrir un traductor, el procesador compara el idioma de destino con una detección
conservadora del contenido; en EPUB usa primero el idioma declarado por el paquete. Una coincidencia
segura omite por completo la preparación, traducción, reparación y evaluación de calidad de esa
operación. El flujo continúa con las fases posteriores reales, sin progreso sintético.

Cada `StageState` distingue disponibilidad y ejecución:

- `disabled`;
- `unavailable`;
- `pending`;
- `running`;
- `blocked_for_review`;
- `completed`;
- `failed`;
- `cancelled`;
- `paused`.

Las transiciones inválidas se rechazan en el dominio. La configuración se bloquea al empezar y una
modificación anterior invalida únicamente sus fases dependientes.

La ventana principal contiene una pila de páginas. La cola usa una tabla compacta de ancho completo
y deriva con `job_view_model` el siguiente paso, el contador y la única acción contextual de la
cabecera. Configuración, IA local, glosario, revisiones y editor EPUB
se presentan dentro de la pila. La flecha de vuelta guarda las configuraciones válidas sin añadir
un segundo pie de confirmación. Si hay una inconsistencia muestra su causa y conserva la página para
corregirla. Las opciones de una fase apagada quedan inhabilitadas y no participan en su validación
ni en el `ProcessRequest`. Los controladores de revisión conservan sus contratos y
checkpoints: la navegación interna sustituye al contenedor de ventana, no a la lógica de
aplicación.

`JobQueue`, dentro de `application`, es la propietaria única de la colección de trabajos. Centraliza
la identidad, el orden visible, la configuración y el snapshot estable de cada origen. La ventana no
mantiene copias paralelas de esos datos: consulta y actualiza la cola, renderiza sus trabajos y
persiste esa misma colección en SQLite.

`JobExecutionController`, también independiente de Qt, es el único encargado de aplicar los eventos
de ejecución a esa cola. Inicia, avanza, pausa, cancela, reintenta, completa o bloquea una fase para
revisión mediante las transiciones validadas de `StageState`. También impide que dos fases
automáticas de documentos distintos queden marcadas como activas simultáneamente.

Antes de iniciar trabajo físico, `prepare_queue_run` crea una instantánea inmutable de la ejecución:
fija el modo y los identificadores elegibles, transforma cada configuración de dominio en su
`ProcessRequest` y `AppSettings`, y valida conjuntamente rutas, solicitudes y espacio disponible.
La interfaz reutiliza ese resultado; no vuelve a interpretar la configuración ni repite la
validación al construir cada trabajador. Si la cola cambia entre la preparación y el comienzo,
`JobExecutionController` rechaza el plan obsoleto.

Cada trabajador recibe directamente su `PreparedRunItem`. La proyección temporal que conserva la
interfaz antigua ya no almacena otra copia de `ProcessRequest` ni de `AppSettings` en la ventana de
Parsezen. Las operaciones que necesitan esos datos —lanzamiento, cancelación, limpieza de
checkpoints y reanudación— los obtienen de la instantánea activa o los reconstruyen desde el
`DocumentJob` persistido.

## Planificador

El planificador es secuencial y no bloqueante:

- fija al comenzar si la ejecución procesa trabajo nuevo, reanuda pausados o reintenta fallidos;
- no mezcla reintentos antiguos con documentos nuevos salvo que se trate de una reanudación explícita;
- elige el primer trabajo disponible según el orden visible;
- ejecuta una sola fase pesada cada vez;
- continúa con el siguiente documento si el actual termina, falla, se cancela o necesita revisión;
- una revisión pendiente bloquea solo su documento;
- al aprobarla, el documento vuelve al punto exacto posterior a esa fase;
- nunca se repiten fases aprobadas salvo invalidación explícita.

La cola, el planificador y el coordinador de dominio son la fuente de verdad para identidad, orden,
configuración, selección del siguiente documento, progreso y estado de ejecución. Cada plan consume
un documento una sola vez: si falla, la ejecución continúa con el siguiente y sólo una acción
posterior de reintento vuelve a elegirlo.

`ProcessingRunner`, en la capa de presentación, es el único adaptador Qt que posee el trabajador
físico y su token de cancelación. Recibe una solicitud ya preparada, ejecuta el procesador estable en
el `QThreadPool` y publica eventos de fase, progreso, resultado, error, cancelación y finalización. La
ventana se limita a conectar esos eventos con `JobExecutionController`; ya no construye ni conserva
trabajadores, y ninguna regla del planificador depende de Qt.

Los eventos físicos se aplican primero al dominio. Solo después de que la transición sea válida se
actualiza la proyección visual activa. Así, un evento tardío o cronológicamente imposible no puede
dejar la tabla y la cola persistida describiendo estados distintos.

`JobOutcomeCoordinator` concentra los eventos terminales del trabajador. Decide si un resultado
completa el documento o bloquea una fase concreta para revisión, guarda antes la instantánea
cifrada necesaria para recuperar esa revisión y clasifica fallos, pausas y cancelaciones. Si no se
puede guardar la instantánea, la revisión sigue disponible en memoria y la interfaz avisa de que no
podrá recuperarse tras cerrar.

`ReviewFinalizationCoordinator` valida todas las decisiones aprobadas antes de escribirlas, completa
el trabajo y elimina después el material temporal cifrado. Un fallo de limpieza posterior no
convierte un resultado ya publicado en error; queda registrado para el mantenimiento seguro del
siguiente arranque. La ventana solo presenta el resultado y los avisos de estos coordinadores.

`ReviewPublicationCoordinator` ordena el último tramo completo: conserva primero el borrador
normalizado, valida y construye el resultado, reemplaza el archivo de forma atómica y delega
entonces la finalización del trabajo. Si la persistencia final falla, el borrador y la puerta de
revisión permanecen recuperables y la interfaz no presenta el documento como completado.

`final_integrity` añade una última barrera determinista entre el serializador y la publicación.
TXT y Markdown se vuelven a leer en UTF-8 y se comparan con el contenido aprobado; DOCX y EPUB
comparan el temporal byte a byte con el paquete validado y vuelven a comprobar su contenedor. Al
crear un EPUB, cada XHTML y recurso se compara además con lo escrito dentro del ZIP. El informe
persistible solo contiene controles y contadores estructurales, nunca texto ni rutas. Los helpers
de `output` ejecutan esta comprobación después de sincronizar el temporal y antes del enlace o
reemplazo atómico; una diferencia conserva intacto el resultado anterior.

Los fallos atraviesan el límite del trabajador como `ProcessingFailure`, con una clasificación
estable y sin contenido privado. `recovery_plan` deriva la explicación y las acciones válidas:
reintento de una sola fase/documento, configuración o IA local. Un reintento contextual usa un
`QueueRunPlan` explícito de un solo identificador, por lo que no arrastra trabajos nuevos ni otros
fallos de la cola y reutiliza los checkpoints válidos de las fases anteriores.

Si el trabajador combinado descubre una revisión al final del procesamiento, el coordinador vuelve a
abrir la fase exacta que la originó e invalida sólo las fases posteriores. Al recuperar una sesión,
una fase que estaba ejecutándose pasa a pausa segura, y una revisión cuyo material ya no es válido se
reinicia sin presentar resultados parciales como finales.

## Revisión por fase

`ReviewSession` pertenece a un trabajo, una fase y una revisión de configuración. Sus unidades
referencian artefactos inmutables:

- original;
- propuesta;
- edición manual opcional;
- decisión;
- contexto visual y marcador de aplicación cuando corresponda.

Tipos actuales:

- OCR y avisos de conversión;
- traducción;
- corrección;
- estructura.

La revisión de contenido usa una comparación dividida. La revisión estructural termina en el editor
de libro. Guardar y continuar después persiste decisiones y ediciones; aprobar materializa el
resultado y desbloquea la siguiente fase.

`PhaseReviewSequenceCoordinator` deriva un plan ordenado exclusivamente de los informes y cambios
reales del resultado: OCR, traducción, corrección y estructura. Cada aceptación se guarda como
`APPLIED` antes de completar su fase y abrir la siguiente. Por tanto, cerrar la aplicación entre dos
revisiones no repite OCR, traducción ni IA. Si Windows se interrumpe entre la escritura de la
decisión y la transición de la cola, la reconciliación de arranque termina esa única transición de
forma idempotente.

Una fase revisada conserva los artefactos elegidos y el contador de intentos del trabajo automático:
abrir una revisión sobre un resultado ya calculado no se registra como un reprocesamiento. Si la
publicación final o el editor no pueden terminar, se vuelve a exponer la última fase revisada sin
invalidar decisiones anteriores ni presentar el archivo como final.

Las propuestas de contenido se clasifican antes de presentarse. Una inserción o eliminación de
bloque, cambios en cifras, fechas, nombres propios o siglas, variaciones de párrafos y reescrituras
extensas son de riesgo alto y recomiendan conservar el original. La acción masiva respeta esa
recomendación. Si la validación global del documento falla aunque varios fragmentos hayan pasado su
validación local, un reensamblado incremental conserva las propuestas compatibles que puede validar
y restaura las incompatibles.

Cada `ReviewUnit` incorpora una gravedad (`critical`, `high`, `medium`, `low`). Las unidades se
ordenan de forma estable por esa gravedad dentro de su fase; el orden entre fases no cambia porque
representa dependencias reales. La gravedad, la recomendación y la decisión se persisten juntas.
Al recuperar una sesión, la presentación abre la primera unidad todavía sin resolver, cuenta por
separado las prioridades críticas/altas pendientes y salta las decisiones ya guardadas. Los atajos
de comparación actúan sobre los mismos controles y no introducen una vía de aprobación alternativa.

Antes de una revisión de contenido, los marcadores internos, el código y los destinos de enlace se
sustituyen por valores obligatorios y se restauran localmente. Si el modelo no conserva esos valores
tras el reintento, el fragmento se divide en párrafos, líneas u oraciones seguras y se recuperan solo
las correcciones que vuelven a validar. El recorrido de aceptación real usa las mismas decisiones
recomendadas que la interfaz: aplica cambios de riesgo bajo y conserva los de riesgo alto.

La traducción con Ollama divide con límites específicos más pequeños que los de una corrección y
acota también la densidad de cifras, enlaces y demás valores protegidos. Si una respuesta no supera
las comprobaciones de estructura, cobertura, idioma o conservación literal, reintenta de forma
progresiva por párrafo, línea, oración y fragmentos situados entre valores protegidos. Solo ensambla
resultados que vuelven a superar la validación; conserva únicamente la unidad mínima que sigue
fallando y la señala para revisión. Los encabezados aislados entregan al modelo solo su texto y
recuperan localmente su envoltura Markdown. La recuperación por líneas hace lo mismo con viñetas,
numeración, tareas y prefijos de cita antes de consultar al modelo. Los nombres de organizaciones
editoriales se tratan como nombres propios. También se conservan firmas personales breves repetidas
en mayúsculas o asociadas a un intervalo de años; la repetición evita aplicar esa excepción general
a cualquier título temático en mayúsculas. El informe de calidad se calcula sobre esta salida de
traducción, antes de que una corrección o una propuesta estructural cambien la división en bloques.
La evidencia se calcula sobre las diferencias reales para admitir una cita intacta en otro idioma
cuando la prosa que la contiene sí se ha traducido. Un encabezado detectado con alta confianza como
tercer idioma se restaura de forma determinista si el modelo lo altera, conservando el nivel
Markdown de destino. Las traducciones incompatibles de un mismo término repetido en encabezados se
señalan como fidelidad para revisión, pero no se sustituyen automáticamente por mera semejanza.

Cuando Traducir y Corregir usan Ollama, `runtime_mapping` mantiene la intención original pero el
orquestador ejecuta `CLEAN_AND_TRANSLATE`: una sola llamada lógica cubre ambas tareas. Si el PDF
aportó incidencias de extracción, una segunda pasada `REVIEW_CONTENT` procesa únicamente los bloques
de esas páginas o con daño inequívoco. Argos conserva la corrección separada porque no incluye un
editor generativo. Longitud y alineación continúan siendo avisos para revisión; no disparan una
reescritura automática.

Los reintentos usan instrucciones distintas para residuo de idioma, pérdida de valores protegidos y
alteración de estructura Markdown. El residuo de texto fuente mantiene su único reintento alineado y
acotado; OCR y estructura conservan sus recuperaciones específicas.

`semantic_blocks` proporciona una vista determinista común: bloque, identidad por contenido y
ocurrencia, página, rol y confianza. La identidad no depende de la posición absoluta, por lo que
insertar un bloque anterior no invalida decisiones sobre bloques posteriores intactos. Clasifica
preliminares, índice, encabezados, cuerpo, imágenes, tablas, notas y marcadores de procedencia.

La memoria terminológica se deriva una vez por documento y solo admite señales conservadoras:
atribuciones personales, organizaciones reconocibles y firmas repetidas en mayúsculas. Excluye
encabezados, números romanos y términos con una frecuencia acumulada excesiva. Se materializa como
protección opaca con menor prioridad que el glosario del usuario y su huella, nunca sus términos,
forma parte de los checkpoints EPUB. Una atribución debe ocupar su propia línea o usar un prefijo
explícito de autor; la palabra `by` dentro de una oración no congela etiquetas o conceptos técnicos.

Los checkpoints generales comparten un espacio entre variantes de glosario. Como la clave de cada
unidad incluye su contenido protegido, una entrada terminológica invalida únicamente los fragmentos
en los que aparece; cambiar su destino sigue siendo seguro porque la restauración ocurre después de
recuperar la respuesta opaca. Los checkpoints específicos de EPUB mantienen su huella de glosario
cuando almacenan resultados ya materializados.

La retención global admite 0, 7, 30 o 90 días. Con un plazo positivo, los checkpoints cifrados de
extracción, OCR, fragmentos y EPUB sobreviven a una publicación correcta y se podan por edad y
tamaño; con cero se limpian al terminar, salvo que todavía exista una revisión pendiente. Descartar
o cancelar explícitamente un trabajo sigue eliminando sus checkpoints exactos.

La propuesta estructural es una operación de encabezados por directivas. El código identifica
localmente las líneas candidatas, las numera y solo permite que el modelo devuelva pares línea-nivel;
nunca le acepta un bloque documental reescrito. Parsezen reconstruye cada cambio con las palabras
exactas de la línea de entrada y conserva el fragmento si la respuesta no es utilizable. Las
respuestas opcionales sin cambios no se consolidan como checkpoints validados. Además, el texto
completo de cada encabezado nuevo debe proceder de una única línea candidata del original.

Los checkpoints de fragmentos usan una clave ligada al modo y al contenido, independiente de su
posición ordinal. Al cargar una clave posicional de la versión anterior, se vuelve a validar el
resultado y se migra a la clave estable; así un cambio de planificación no obliga a repetir
fragmentos posteriores cuyo contenido no cambió.

## Conversión PDF

La extracción nativa mantiene geometría de caracteres y líneas. Reconstruye palabras con tracking
artificial a partir de distancias relativas, separa líneas que contienen dos columnas alejadas y
ordena bandas de columna sin mover títulos, pies o separadores de ancho completo. Los marcadores de
página se conservan durante todo el procesamiento y solo se retiran al publicar un resultado que no
necesita revisión. Cuando una palabra termina con guion al final de una página y continúa en
minúscula al principio de la siguiente, la unión se representa alrededor del marcador interno para
que la palabra publicada quede completa sin perder trazabilidad.

Los folios nativos aislados se retiran tanto del margen superior como del inferior. La detección
superior acepta además una banda exterior más profunda para numeraciones alternas de páginas pares
e impares y encabezados no centrados donde el folio aparece unido a una etiqueta de capítulo, pero
no números centrados que puedan identificar una sección. El folio puede preceder la etiqueta o
seguir a una etiqueta breve en el extremo exterior; el tamaño de fuente y la extensión horizontal
evitan confundirlo con un rótulo de figura dentro de la columna de texto. Si una página gráfica
necesita OCR, los números o encabezados deben estar detectados también como texto nativo en una de
esas zonas antes de retirarlos de las primeras o últimas líneas reconocidas. La condición combinada
evita eliminar una numeración de contenido que no esté respaldada por la geometría del PDF.

Las imágenes incrustadas se extraen directamente. Además, grupos acotados de curvas próximas se
renderizan como ilustraciones cuando representan una entidad gráfica y se deduplican frente a
imágenes solapadas. Los enlaces cuyo destino no puede anclarse a texto visible se reúnen en
`Destinos conservados` en vez de convertirse en etiquetas huérfanas. La versión del checkpoint de
página nativa forma parte de su clave para no reutilizar extracciones anteriores incompatibles.

El plan OCR añade una puntuación de legibilidad basada en densidad alfabética, fragmentación,
glifos sospechosos, texto espaciado y líneas rotadas. Una página de confianza baja se rasteriza con
la estrategia completa; al terminar, el texto OCR sustituye al nativo solo si supera umbrales
absolutos y mejora suficientemente su puntuación. Tablas y páginas sin texto tienen reglas
específicas. Un análisis correcto que no obtiene texto se guarda como checkpoint vacío para evitar
repetir OCR costoso al reanudar; una excepción del motor no se guarda como ausencia de texto. El
informe conserva las páginas analizadas, sustituidas y todavía dudosas.

## Libro normalizado y EPUB

`BookDocument` desacopla el editor del origen:

- metadatos;
- árbol de secciones;
- orden de lectura;
- XHTML de cada sección;
- nombre de capítulo de origen estable para resolver referencias después de reordenar;
- recursos;
- portada.

`BookEditor` ofrece operaciones inmutables para renombrar, editar, crear, dividir, unir, mover,
anidar y elevar secciones. Unir elimina una división, nunca su contenido.

El modelo limita cantidad y profundidad de secciones, exige un orden de lectura completo, rutas de
recursos relativas, nombres de origen únicos y una identidad UUID estable. Antes de publicar, el
editor vuelve a resolver las anclas contra la identidad de la sección y el orden vigente, por lo que
mover un capítulo no deja enlaces apuntando al nombre que ocupaba anteriormente. El editor permite
modificar título, autor e idioma
y resuelve las imágenes directamente desde artefactos cifrados, sin crear copias temporales sin
protección. Una revisión estructural manual abre siempre este editor, incluso cuando la IA concluye
que la estructura inicial ya era correcta. El mismo editor se abre para todo resultado EPUB y
agrupa título, autor, idioma y portada bajo un desplegable de metadatos.

La planificación Markdown→EPUB usa la clasificación de preliminares e índice. Los títulos del
índice se normalizan y se comparan con encabezados reales; esas coincidencias y los patrones de
capítulo son señales fuertes, mientras que los niveles Markdown procedentes de la geometría PDF
aportan la señal general. Los encabezados del índice nunca abren por sí solos un capítulo del cuerpo.

La publicación genera EPUB 3 con:

- `mimetype` sin compresión y en primera posición;
- paquete OPF;
- navegación anidada;
- XHTML validado;
- imágenes y portada;
- referencias locales comprobadas y contenido activo o remoto rechazado;
- estilos conservados únicamente después de retirar importaciones y URL remotas;
- validación final del ZIP, contenedor, OPF, manifiesto, orden de lectura, tabla de contenidos,
  recursos, XML y anclas antes de exponer el resultado;
- escritura atómica del archivo final.

La traducción directa conserva inicialmente el paquete y además prepara siempre su representación
editable. EPUB→EPUB sin otra operación también crea este resultado pendiente de personalización.
Las unidades semánticas pueden viajar agrupadas para reducir llamadas, pero la detección y
reparación de texto residual se ejecuta sobre cada unidad antes de guardar el checkpoint; así una
frase breve sin traducir no queda diluida entre metadatos, navegación y atributos ya traducidos.
La publicación desde el editor reconstruye el libro normalizado para garantizar un EPUB válido,
editable y coherente con los metadatos y la portada elegidos.

## Persistencia

SQLite usa WAL, `synchronous=FULL`, claves foráneas y transacciones:

- `jobs`: cola y configuración;
- `reviews`: decisiones por fase;
- `books`: libro editable pendiente;
- `result_snapshots`: referencia a la instantánea cifrada;
- `job_events`: eventos técnicos sin contenido.

El contenido documental no se guarda en SQLite. `ArtifactStore` escribe artefactos inmutables,
protegidos con DPAPI para la cuenta actual de Windows y acompañados de SHA-256 dentro del sobre
cifrado.

Antes de que una cola avance desde un documento que necesita revisión, `ResultSnapshotStore`
persiste:

- resultado y rutas;
- informes OCR y de traducción;
- propuesta de corrección;
- recursos;
- metadatos EPUB;
- información necesaria para Word.
- telemetría por etapa y conteos semánticos sin contenido.

`ProcessTelemetry` agrega duración y número de visitas por `ProcessStage`; no almacena nombres,
rutas, prompts, términos ni texto. La validación real usa el mismo resultado para informar tiempos,
páginas OCR sustituidas o dudosas y conteos de preliminares, índice y memoria terminológica.

`OutcomeSummary` es el contrato de cierre para interfaz y actividad. Mantiene separados el control
de integridad final, las incidencias detectadas por las etapas y las decisiones de revisión manual;
ninguna ausencia de avisos se presenta como una certificación semántica. El historial
`recent-jobs.json` conserva de forma atómica un máximo de 20 intentos, deduplicados por origen,
estado y resultado, con rutas y conteos pero sin contenido. El usuario puede borrarlo sin tocar
documentos ni resultados. No es una biblioteca durable.

La ventana agrega los resultados del conjunto iniciado en un único resumen de lote. Las
notificaciones del sistema se emiten solo si la ventana está minimizada o no está activa, y el icono
de bandeja se oculta después; no se crea un proceso residente.

Al abrir Parsezen se recupera exactamente la revisión. Al publicar el resultado se eliminan el libro,
las revisiones y sus artefactos temporales.

La sustitución completa de la cola ocurre dentro de una sola transacción. Los órdenes existentes se
desplazan primero a una zona temporal sin colisiones y solo después se aplican las posiciones
definitivas; cualquier error revierte también altas y bajas. Las operaciones sobre instantáneas se
serializan dentro de su repositorio. Al sustituirlas, descartarlas o fallar a mitad de escritura se
eliminan únicamente el manifiesto y los recursos privados de esa instantánea, nunca artefactos
hermanos.

Una revisión solo se recupera si tamaño y fecha de modificación del original coinciden con los que
tenía al prepararse. Si el archivo cambió, la propuesta se invalida y el trabajo vuelve a un punto
seguro; Parsezen nunca mezcla una revisión antigua con una versión nueva del documento.

Un error de lectura de SQLite crea primero una copia consistente del estado ilegible y mueve los
artefactos cifrados relacionados a una carpeta de recuperación asociada. Solo después inicia una
cola limpia. Si cualquiera de esas protecciones falla, se bloquean nuevas escrituras para no
sustituir silenciosamente una cola potencialmente recuperable. Los fallos transitorios de escritura
se reintentan y se muestran en el pie de la aplicación. Al cerrar con trabajo sin guardar se
solicita confirmación. Si la propia estructura de SQLite está dañada y no puede abrirse, la base,
sus archivos auxiliares y los artefactos cifrados se aíslan juntos antes de crear el estado limpio.

Al arrancar también se eliminan artefactos cifrados que ya no pertenecen a una revisión recuperable,
por ejemplo después de retirar un trabajo, perder su original o interrumpir una limpieza. Las
carpetas desconocidas y cualquier archivo ajeno al formato interno se conservan.

## Seguridad de datos

- El origen es inmutable.
- Una salida se construye fuera de su destino y se reemplaza solo al estar completa.
- Las colisiones producen un nombre nuevo.
- Una cancelación no publica un parcial.
- Los ZIP de DOCX y EPUB tienen límites de entradas, expansión, rutas y tamaño.
- XML usa analizadores sin entidades ni red.
- HTML editable elimina scripts, formularios, eventos y URL ejecutables.
- Los logs están sanitizados y rotan.
- Ollama solo acepta loopback y modelos locales; Argos y OCR son locales.
- La selección, la caché de recomendaciones y la validación previa bloquean variantes conocidas de
  razonamiento; la ejecución vuelve a rechazarlas como última barrera antes de leer el documento.

Los errores inesperados del ejecutor físico se registran únicamente por tipo técnico. No se incluyen
mensajes de excepción, rutas ni contenido documental; el usuario recibe un mensaje estable y seguro.

## Interfaz

La tabla usa `QAbstractTableModel` y un delegado de pintura, no un árbol de widgets por celda. Esto
mantiene coste y alturas estables al crecer la cola.

El sistema visual se centraliza en `presentation/design_system.py`:

- temas oscuro (predeterminado) y claro con tokens compartidos;
- superficies, bordes y estados semánticos;
- espaciado, radios, controles e iconos;
- Inter incluida bajo OFL;
- logo y símbolo derivados de maestros oficiales mediante
  `scripts/generate_brand_assets.py`.

Los maestros no se modifican. Los derivados reproducibles cubren las dos variantes de cabecera,
README, icono PNG e ICO multirresolución. La variante oscura conserva la geometría y transparencia
del maestro y aplica los colores de contraste de la referencia oficial oscura.

## Compatibilidad y migración

Parsezen es la primera identidad pública de este repositorio. La primera instalación usa una única
ruta `%LOCALAPPDATA%\Parsezen`; no importa sesiones ni checkpoints de prototipos anteriores. Los
documentos originales y resultados existentes son archivos normales y no requieren migración.

## Pruebas

La estrategia combina:

- dominio y transiciones;
- planificador;
- persistencia y corrupción;
- artefactos e instantáneas cifradas;
- conversiones y publicación atómica;
- matriz de flujos;
- corpus PDF privados con huellas sin contenido, memoria del árbol de procesos y rangos repetibles;
- modelos/delegados Qt en modo offscreen;
- contratos visuales en claro y oscuro a 320, 768 y 1.440 px;
- navegación, hover, teclado y geometría de controles interactivos;
- controladores Qt de procesamiento e IA local, incluidos sus fallos y cancelación;
- revisiones y editor EPUB;
- aceptación optativa con Ollama real sobre una muestra mínima de 20 páginas;
- EPUBCheck externo en CI.

El instalador se construye únicamente bajo demanda o para una etiqueta de versión.

La distribución pública usa un instalador Inno Setup sin Authenticode. El workflow de Windows parte
del lock CPU, valida código, cobertura y dependencias, genera avisos legales, crea el paquete con
PyInstaller y ejecuta el smoke test del binario antes de construir el instalador. El artefacto
incluye un checksum SHA-256 y el inventario exacto del entorno. En una etiqueta, GitHub añade además
una atestación de procedencia vinculada al commit; esta atestación no sustituye una firma reconocida
por Windows ni evita por sí sola los avisos de editor desconocido.

Las compilaciones manuales solo producen candidatos temporales. Una versión para usuarios se
publica como GitHub Release desde una etiqueta inmutable y adjunta el instalador, su checksum y el
inventario. El instalador conserva el `AppId` entre versiones, limpia el runtime `_internal` anterior
antes de actualizar y no toca `%LOCALAPPDATA%\Parsezen`, los originales ni los resultados.
