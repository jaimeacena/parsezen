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
presentation/ ───────→ application/ ───────→ domain/
      │                      │
      │ composición          └─────────────→ procesadores locales
      ↓                                       PDF/OCR, Markdown, DOCX,
infrastructure/                              EPUB, Argos y Ollama
SQLite, artefactos cifrados e instantáneas
```

Las flechas representan dependencias de código, no una cadena de llamadas. Las reglas son:

- `domain` contiene estado y reglas puras y solo depende de otros módulos de dominio;
- `application` contiene casos de uso y contratos estructurales, y no importa PySide6 ni
  infraestructura concreta;
- `infrastructure` implementa persistencia local sin depender de presentación o aplicación;
- `presentation` contiene todos los adaptadores Qt. `main_window.py` es además la raíz de composición
  que conecta los casos de uso con SQLite, artefactos e instantáneas;
- los procesadores específicos siguen siendo módulos pequeños y directos. No existe una interfaz
  universal de conversión: PDF, DOCX y EPUB tienen invariantes diferentes y abstraerlos hoy ocultaría
  requisitos reales. En particular, EPUB permanece en `epub_conversion.py` y su reanudación en
  `epub_checkpoints.py`.

`processing.py` es un orquestador Qt-free con fronteras explícitas de preparación, transformación y
publicación. La ruta EPUB→EPUB separa además traducción del paquete, preparación editable/revisión y
publicación. Estos límites comparten contratos concretos y evitan una jerarquía genérica de
procesadores que no aportaría comportamiento actual.

Los dos procesadores más grandes conservan una fachada estable, pero sus subsistemas con invariantes
propias ya no están mezclados:

- `improvement.py` coordina fragmentos y revisión bilingüe; `improvement_contracts.py` contiene sus
  tipos compartidos, `local_ai_transport.py` es el único transporte de transformación hacia Ollama y
  `ai_markdown_safety.py` protege, reconcilia y valida Markdown sin depender de red ni selección de
  modelos;
- `pdf_conversion.py` coordina extracción, OCR y renderizado; `pdf_layout.py` contiene el modelo
  inmutable de página y `pdf_checkpoints.py` serializa y valida su reanudación sin abrir PDFs ni
  invocar OCR.

En presentación, `local_ai_workflow.py` posee el flujo de descubrir, instalar, seleccionar y borrar
modelos. `main_window.py` sigue siendo la raíz de composición y propietaria del estado visual, pero
delega esas acciones en el coordinador y hereda únicamente de `QMainWindow`.

Los casos de uso extraídos de la ventana tienen propietarios explícitos:

- `QueueConfigurationService`: propagación de preferencias globales a todos los trabajos editables;
- `QueuePersistenceCoordinator`: escritura limitada, reintento y bloqueo seguro de la cola;
- `ReviewMaterializationService`: creación y reutilización de decisiones revisables;
- `recover_workspace`: clasificación pura del estado recuperado y de fuentes modificadas;
- `ArtifactRepository` y `QueueRepository`: contratos mínimos consumidos por aplicación.

`tests/test_architecture_boundaries.py` analiza las importaciones y convierte estos límites en una
regresión comprobable. La interfaz renderiza proyecciones inmutables y no inventa estados al margen
del dominio.

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
- configuración mínima por documento;
- el plan de producto (`STANDARD` o `LOCAL_AI_REVIEWED`);
- una instantánea del modelo y contexto globales;
- revisión de configuración;
- estado de cada fase;
- resultado, avisos y error.

`OutputConfiguration.configured` separa un documento recién añadido de un trabajo ejecutable.
Mientras sea falso, el planificador no lo incluye y `PUBLISH` no participa. La presentación abre
una página de ajustes dentro de la pila de la ventana principal. El formato se proyecta como dos
tarjetas visuales exclusivas, la revisión con IA como interruptor y el resto de decisiones como una
fila de etiqueta, valor y chevron. Los documentos nuevos presentan
`LOCAL_AI_REVIEWED` activado; una configuración guardada conserva su plan. Argos y OCR automático
son valores iniciales. Traductor y glosario dependen del idioma; Páginas y OCR aparecen solo para
PDF. El intervalo se edita en un diálogo efímero y la fila conserva únicamente su valor resumido.
Solo Markdown y EPUB son resultados de producto. La página no incorpora scroll ni vuelve a
proyectar destino. Al activar traducción, `translation_route_summary` deriva del motor, el plan y el
formato una explicación breve del recorrido efectivo, sus pasadas y su coste cualitativo; la
presentación no duplica reglas del procesador.

`STANDARD` se resume como procesamiento directo; `LOCAL_AI_REVIEWED`, como revisión completa con IA
local, activa la revisión de texto y, únicamente para EPUB, la revisión de estructura. No existen
combinaciones independientes de corrección y estructura. Cada elección válida reemplaza de forma
atómica la configuración del documento y se persiste inmediatamente; una elección incompleta por
falta de IA conserva su valor visible y dirige al gestor sin publicar una configuración inválida.

La revisión completa es el valor inicial visible para trabajos nuevos, pero sigue siendo una decisión
reversible y nunca se aplica a configuraciones ya guardadas. Una evaluación local confirmó que puede
producir correcciones conservadoras, no una mejora universal, por lo que la interfaz explica su coste
y conserva `STANDARD` como alternativa inmediata. El informe de validación registra por separado las
propuestas de contenido y estructura, sus decisiones recomendadas, las incidencias objetivas y el
tiempo; una propuesta aceptable no se interpreta por sí sola como evidencia de mejor calidad.

Al completar `STANDARD` sin otra revisión bloqueante, `review_recommendation` deriva una
`ReviewRecommendation` determinista de los informes ya calculados. Sus señales son daño de
conversión, residuo del texto de origen e incoherencia de traducción. La recomendación solo existe
cuando puede acotar unidades semánticas revisables y persiste exclusivamente tipos, contadores,
ordinales y huellas SHA-256 no reversibles del alcance y de cada objetivo, nunca texto. Una incidencia de longitud o
alineación aislada sigue siendo un aviso y no
activa por sí sola la escalada. El límite persistido y ejecutable es de 64 bloques.

La presentación proyecta esa metainformación como una acción voluntaria sobre un resultado ya
completado. `JobExecution.begin_targeted_review` habilita tarde la fase `REFINE` y
`review_completed_result` reabre el resultado local, vuelve a validar los ordinales y procesa solo
los bloques señalados. Código, imágenes y procedencia quedan fuera. Si hay traducción y ambos lados
siguen alineados, la pasada usa el contrato bilingüe; de lo contrario aplica las guardas
monolingües. Los bloques no seleccionados se reensamblan sin transformación y el archivo publicado
no cambia hasta que la propuesta supera la revisión normal. Cancelar o fallar deshabilita de nuevo
`REFINE` y restaura el estado completado. Tras reiniciar, la recomendación se conserva, pero el
contenido se reconstruye desde el original y el resultado locales en vez de persistirse en SQLite.
Las huellas por bloque permiten reubicar objetivos únicos cuando el empaquetado EPUB añade metadatos;
un objetivo cambiado o ambiguo impide aplicar ordinales obsoletos sobre otro contenido.

Esta escalada no es una fase implícita: detectar una señal nunca inicia Ollama. El plan
`LOCAL_AI_REVIEWED` se mantiene para quien quiera revisar proactivamente todo el documento desde el
principio; la recomendación posterior reduce el coste y la decisión del recorrido normal sin
pretender que ambos flujos sean equivalentes.

`TranslationMethod` ofrece exactamente dos motores locales: `OFFLINE` usa Argos y `LOCAL_AI` usa el
modelo de Ollama seleccionado. Argos es la opción inicial, ligera y predecible; IA local es la opción
contextual y dependiente del modelo. La elección del motor es independiente de `ProcessingPlan`: el
plan revisado puede actuar después de cualquiera de los dos. `AIProfileConfiguration` es una
instantánea de la única pareja modelo/contexto global. No hay excepciones por documento y los cambios
generales se propagan a todos los trabajos editables. La eliminación de un modelo se bloquea mientras
algún trabajo sin terminar dependa de él. Si el valor inicial necesita IA y falta un modelo local
válido, la configuración conserva la intención, muestra la causa junto al control y dirige al gestor
de modelos antes de guardar.

`LinguisticReviewCoverage` registra sin texto documental cómo se obtuvo la confianza lingüística:
sin revisión semántica, corrección integrada en la traducción, verificación bilingüe independiente o
verificación dirigida. Separa el total de bloques traducidos, los comprobados por las heurísticas,
los revisados semánticamente, los verificados de forma independiente y las incidencias restantes.
`TranslationQualityReport` conserva además los totales de bloques de origen y salida para no ocultar
la parte no alineada. La cobertura viaja en `ProcessResult`, las instantáneas cifradas y el resumen
sin contenido de actividad; nunca convierte una comprobación automática en verificación semántica.

El destino funciona del mismo modo: cada documento hereda la ruta general efectiva, pero no puede
sustituirla localmente. Cambiarla actualiza todos los trabajos editables. Páginas muestra `Todas` o
el rango ya elegido; OCR muestra `Automático` o `Todas las páginas`.

La configuración persistida v6 es un corte limpio. Al abrir una base con un esquema anterior se
descarta solo el estado reconstruible de Parsezen (cola, revisiones, libros, instantáneas, eventos y
métricas); los documentos de origen y resultados del usuario nunca se eliminan ni migran.

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
`PreflightRunner` ejecuta esta lectura en el `QThreadPool`, de modo que abrir y contar páginas de PDF
no bloquea el hilo gráfico. Los pronósticos oportunistas no vuelven a abrir PDF: se incorporan al
terminar el preflight autoritativo. La presentación proyecta ese tramo también como `Preparando` y
mantiene la misma etiqueta cuando
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
`run_early_check` inspecciona un máximo de nueve posiciones del intervalo y elige hasta cinco que
cubren extremos, densidad textual, contenido visual y tablas; cuando las características se
concentran en los extremos, completa la muestra con las posiciones restantes más distribuidas.
Ejecuta cada una mediante el mismo `process_document`, con salidas en un directorio temporal. Una
página inicial o final sin texto nativo que conserva imágenes se mantiene como advertencia y cuenta
en el informe, pero no como incidencia material de extracción; los fallos repetidos en páginas
interiores sí bloquean. Las incidencias de traducción conservan su umbral independiente.

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
conservadora del contenido. En EPUB contrasta el idioma declarado por el paquete con una muestra
textual suficiente: el texto prevalece cuando aporta evidencia clara y los metadatos siguen siendo
el respaldo para publicaciones demasiado breves. Una coincidencia segura omite por completo la
preparación, traducción, reparación y evaluación de calidad de esa operación. El flujo continúa con
las fases posteriores reales, sin progreso sintético.

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
cabecera. Configuración, IA local, revisiones y editor EPUB se presentan dentro de la pila; solo el
editor compacto de glosario y el selector de intervalo son diálogos modales acotados sobre su
contexto. Como no existen cambios pendientes, la flecha de vuelta y Escape cierran configuración sin
confirmación. Si hay una inconsistencia muestra su causa y conserva la página para
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

`application.job_runtime` conserva únicamente los objetos físicos transitorios del intento activo;
no guarda identidad, orden, configuración ni estados paralelos. No existe un contrato de sesión de
cola en JSON: SQLite y `JobQueue` son la única fuente autoritativa. `recent_activity.py` mantiene solo
el historial acotado y prescindible que se muestra en Actividad.

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

### Traza local de intentos y actividad reciente

`domain.attempt_activity` define la traza inmutable y acotada de un intento: fases semánticas
(`preparación`, `comprobación temprana`, `traducción`, `corrección`, `personalización` y
`publicación`), estado y marca temporal UTC. No expone etapas físicas del procesador. La traza admite
como máximo 32 transiciones y cada mensaje opcional tiene un límite independiente; una
`FailureSnapshot` conserva únicamente la fase fallida, un código seguro, una plantilla estable sin
texto de la excepción, una referencia opaca opcional y el token `ReusableWork` que describe qué puede
reutilizarse. El mensaje detallado de `ProcessingFailure` vive solo en la sesión actual.

`MainWindow` captura `(attempt_id, timeline, failure_snapshot)` antes de que el trabajador termine y
antes de avanzar al siguiente documento. Mantiene una sola instantánea por identificador de trabajo
que sigue en la cola, la poda al retirar ese trabajo y la reutiliza si una revisión EPUB se publica
más tarde. `RecentJob` recibe esa misma instantánea y el esquema 3 de actividad la persiste con un
límite total de 20 filas. No existe una segunda fuente de eventos ni una caché histórica sin límite.

La vista de Actividad deduplica transiciones repetidas solo al presentarlas, muestra horas locales y
solo ofrece `Volver al documento` cuando el origen coincide con un trabajo fallido todavía presente
en la cola. Los intentos históricos no se convierten en una biblioteca ni en una acción de reintento.
El diagnóstico copiable tiene una frontera más estricta que la explicación de la interfaz: contiene
la versión, fase, código, instante de finalización, tokens opacos y fase/estado/hora de cada evento.
Excluye nombres, rutas, mensajes, contenido, prompts, respuestas, trazas y secretos; la interfaz no
registra el mensaje que muestra.

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
estable y un detalle efímero para la interfaz actual. Ese detalle nunca se copia al historial;
`recovery_plan` deriva la explicación y las acciones válidas:
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
de libro. La presentación muestra el progreso de la sesión activa como decisiones resueltas sobre
las unidades realmente materializadas de esa fase; no usa como denominador el plan ponderado global,
los cambios estructurales futuros ni incidencias de un informe que no llegaron a ser decisiones.

La recomendación inicial es provisional y nunca marca un botón como elegido al abrir una unidad.
La persona debe pulsar un botón de elección o editar la propuesta; el botón confirmado muestra su
estado y el panel elegido se tiñe sin dibujar un borde de selección alrededor de todo el panel. La
salida por guardar, volver o cerrar conserva una edición o una elección cambiada del caso visible;
abrir y salir sin interacción no marca la recomendación como resuelta. `Guardar y salir` cifra el
estado y permite reanudar en la primera unidad pendiente.

Las unidades OCR se anclan al tramo completo de la página dentro del Markdown ya transformado,
desde su marcador privado hasta el marcador siguiente. Así, en un flujo con traducción, la persona
edita el texto final y la decisión se aplica sobre la misma instantánea que se publicará; nunca se
intenta insertar texto fuente obsoleto dentro del resultado traducido. El contenido de ese tramo
forma parte del identificador estable de la unidad. Una revisión OCR anterior cuyo candidato ya no
coincida se reemplaza una sola vez por la versión actual y descarta sus elecciones incompatibles.

`PhaseReviewSequenceCoordinator` deriva un plan ordenado exclusivamente de los informes y cambios
reales del resultado: OCR, traducción, corrección y estructura. Cada aceptación se guarda como
`APPLIED` antes de completar su fase y abrir la siguiente. Por tanto, cerrar la aplicación entre dos
revisiones no repite OCR, traducción ni IA. Si Windows se interrumpe entre la escritura de la
decisión y la transición de la cola, la reconciliación de arranque termina esa única transición de
forma idempotente. Si una corrección de la aplicación cambia el alcance verificable de una revisión
ya aplicada, la reconciliación vuelve a esa primera fase pendiente, descarta únicamente las
revisiones posteriores dependientes y conserva los intentos y la instantánea del procesamiento.

La presentación expone ese coordinador como una única superficie `Revisión del documento`, con
progreso global y fase actual; no obliga a elegir qué editor abrir. Un documento bloqueado queda
fuera de la selección automática, pero la cola continúa con el siguiente trabajo elegible.

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

`REVIEW_CONTENT` es fail-closed: rechaza inserciones completas, eliminaciones de bloques, expansiones
factuales o cualquier cobertura/similitud incompatible con el origen. La misma guarda se ejecuta al
construir el borrador y al materializarlo, por lo que una propuesta antigua o de caché no puede
convertirse en un panel revisable ni en un artefacto. Solo sobreviven correcciones alineadas y
acotadas. Los comentarios PZDOC, la sintaxis completa de cada imagen privada, sus recursos y el
texto ordinario `Comentario interno` se comparan exactamente; cualquier adición o mutación conserva
el origen y queda fuera de la revisión.

Cada `ReviewUnit` incorpora una gravedad (`critical`, `high`, `medium`, `low`). Las unidades se
ordenan de forma estable por esa gravedad dentro de su fase; el orden entre fases no cambia porque
representa dependencias reales. La gravedad, la recomendación y la decisión se persisten juntas.
Al recuperar una sesión, la presentación abre la primera unidad todavía sin resolver, cuenta por
separado las prioridades críticas/altas pendientes y salta las decisiones ya guardadas. Los atajos
de comparación actúan sobre los mismos controles y no introducen una vía de aprobación alternativa.

Las unidades de traducción se materializan anclando el extracto acotado al documento traducido. Si
el informe termina el extracto con una elipsis, se elimina cualquier palabra cortada y el anclaje se
completa solo hasta el final de la frase, con un límite estricto; nunca se amplía a toda la página o
al bloque Markdown. Los dos paneles se derivan de sus extractos en la misma frontera de frase, de
modo que el usuario solo traduce el contenido visible completo del panel editable. El contexto
original también termina en una frontera legible. El resultado
actual es editable, no se presenta como recomendación y el texto de origen nunca puede reemplazarlo.
El contenido exacto del fragmento forma parte de la identidad de la unidad para invalidar una
revisión antigua cuyo alcance fuese distinto. Un extracto que no se pueda anclar se omite sin
inventar contenido y, si procede, solo deja un diagnóstico local sanitizado. Al
reanudar, una sesión guardada se reutiliza únicamente cuando coincide su tipo, revisión de
configuración y secuencia ordenada de identificadores con el candidato recién materializado; si no,
se guarda un candidato nuevo compatible antes de reconciliar la cola.

Antes de una revisión de contenido, los marcadores internos, el código y los destinos de enlace se
sustituyen por valores obligatorios y se restauran localmente. Si el modelo no conserva esos valores
tras el reintento, el fragmento se divide en párrafos, líneas u oraciones seguras y se recuperan solo
las correcciones que vuelven a validar. El recorrido de aceptación real usa las mismas decisiones
recomendadas que la interfaz: aplica cambios de riesgo bajo y conserva los de riesgo alto.

La corrección posterior a una traducción de Argos usa un contrato bilingüe distinto. Ollama recibe
el original y la traducción alineados como datos documentales no confiables y solo puede proponer una
lista JSON acotada de sustituciones exactas sobre la traducción. El código aplica cada sustitución de
forma independiente y vuelve a comprobar cifras, enlaces, nombres, estructura, idioma y cobertura;
una propuesta insegura no invalida las correcciones seguras del mismo fragmento. Una respuesta
truncada solo permite recuperar objetos JSON completos, que pasan por las mismas guardas. El resto
del texto nunca se reserializa a partir de la respuesta del modelo. Las sustituciones mínimas pueden
ser también inserciones puras dentro de una palabra; se aceptan solo después de volver a validar el
fragmento completo, igual que cualquier sustitución o eliminación.

Tras la pasada general, un segundo recorrido bilingüe acotado vuelve a examinar únicamente las
unidades con frases intactas del idioma original, varias palabras fuente incrustadas o una variante
ortográfica rara a una edición de una forma claramente dominante. Dentro de cada unidad seleccionada
reduce la solicitud a un máximo de cuatro líneas alineadas que conservan esa señal; si el ajuste de
líneas difiere entre idiomas, usa en su lugar párrafos Markdown alineados por rango de caracteres.
La instrucción enumera como foco exclusivo un máximo de ocho formas detectadas localmente en esa
microunidad. Cada forma usa una solicitud mínima que solo admite cero o un parche; el analizador
descarta cualquier resultado cuyo texto nuevo no reduzca su recuento. Ollama decide si existe un error
objetivo y cada parche mantiene las mismas guardas de estructura,
cobertura, cifras y enlaces. La microunidad debe reducir su señal residual sin añadir palabras del
origen; la detección de idioma se aplaza al documento completo para que una línea breve con nombres
propios no produzca un falso rechazo. Los nombres propios y organizaciones se excluyen
de la señal de residuo exacto. Tanto esta pasada como la revisión bilingüe general rechazan cualquier
candidato que aumente palabras suficientemente largas tomadas literalmente del original, también en
encabezados en mayúsculas; la pasada residual debe además reducir de forma medible la señal que motivó
su selección.

Cuando la señal corresponde a una oración completa conservada literalmente, la revisión crea una
unidad independiente que contiene solo esa oración y admite como respuesta una única traducción.
Antes de sustituirla vuelve a comprobar estructura, cobertura, cifras, enlaces, contenido y reducción
real del residuo. Las secciones que por naturaleza conservan texto literal —bibliografías, índices y
catálogos densos de nombres— se reconocen por entrada o fila, no por página completa, y no activan
esta reparación ni una incidencia de texto fuente. La prosa mezclada con ellas sigue revisándose y
las guardas de fidelidad y longitud continúan evaluando el segmento completo para no ocultar pérdidas
o alteraciones estructurales. Una retraducción aislada requiere además evidencia local positiva del
idioma de destino; una salida indeterminada o en un tercer idioma conserva el texto anterior. La
localización de la frase convierte los índices Unicode plegados a sus posiciones originales, de modo
que caracteres como `ß` no desplacen ni recorten los límites de sustitución.

Cada propuesta se valida contra el estado inmediatamente anterior y el ensamblado completo vuelve
a validarse contra la traducción base del motor elegido. Si varias mejoras seguras sobre un mismo bloque superan de
forma acumulativa el límite de reescritura, Parsezen revierte solo ese bloque y vuelve a validar el
documento completo. Un único bloque acumulativamente inseguro no descarta ya cientos de correcciones
independientes, y las guardas de estructura, cifras, enlaces, idioma y cobertura siguen siendo
globales antes de publicar.

El informe de traducción se vuelve a calcular sobre la propuesta completa después de las revisiones
de contenido y estructura. Sigue siendo una orientación local no bloqueante, separada de las guardas
críticas, pero deja de mostrar incidencias que la corrección ya resolvió o de ocultar residuos que una
propuesta estructural hubiese introducido.

Cuando dos respuestas bilingües consecutivas no superan las guardas, el checkpoint guarda como
decisión explícita únicamente la traducción ya validada que se conserva; nunca persiste la respuesta
rechazada. De este modo una interrupción no repite solicitudes deterministas inseguras. La decisión
está ligada al mismo contenido y se elimina junto con el trabajo privado después de publicar.

Las referencias de página situadas al final de una entrada de lista se tratan como anclas
estructurales. Argos traduce la etiqueta sin recibir el folio y lo vuelve a encontrar en su posición
original; la validación compartida compara cada entrada alineada y rechaza tanto una traducción como
una sustitución de Ollama que mueva el folio, incluso si el conjunto global de cifras no cambia.
Los números romanos en encabezados Markdown también quedan fuera de Argos para conservar su caja y
su función ordinal, mientras que una `I` de la prosa normal sigue formando parte de la traducción.

Los títulos prioritarios tienen una recuperación adicional y acotada. Si una sustitución parcial
válida por sí sola introduce una secuencia repetida de tres o más palabras, se vuelve a enviar solo
el título completo, sin byline ni bloques vecinos. La respuesta sustituye la línea entera únicamente
si conserva su envoltura, valores protegidos, cobertura e idioma y reduce la repetición; el resto del
grupo bilingüe permanece byte por byte fuera de esa reparación.

La ruta de producto conserva Argos y añade IA local como alternativas explícitas. La interfaz no
presupone que una sea semánticamente mejor: presenta a Argos como ligera y a IA local como contextual
y dependiente del modelo. Ambas protegen el
glosario y pasan por las mismas guardas compartidas de cobertura, cifras, enlaces, estructura e
idioma. El informe de calidad inicial se calcula sobre la salida del motor y vuelve a calcularse
sobre la propuesta completa después del plan Revisado. La evidencia admite una cita intacta en otro idioma cuando la prosa
que la contiene sí se ha traducido; las variantes incompatibles de un mismo término se señalan para
revisión y no se sustituyen por semejanza.

`runtime_mapping` proyecta `LOCAL_AI_REVIEWED` como `review_content=True` y añade
`review_structure=True` solo para EPUB. Si también hay traducción, la revisión actúa después del
motor elegido como editor bilingüe local y cada diferencia material se materializa para revisión.
Longitud y alineación continúan siendo avisos; no disparan una reescritura automática.

Los reintentos usan instrucciones distintas para residuo de idioma, pérdida de valores protegidos y
alteración de estructura Markdown. El residuo de texto fuente mantiene un único reintento por unidad
alineada y acotada. En PDFs, se intenta primero el párrafo exacto y, si una página mezcla resultado
traducido con frases conservadas, solo esas frases. Una reparación local que supera idioma,
cobertura y estructura se conserva aunque otra página siga necesitando revisión; las guardas finales
verifican que el ensamblado no altere cifras, enlaces, jerarquía o distribución. OCR y estructura
conservan sus recuperaciones específicas.

Antes de renderizar XHTML, `epub_builder` escapa solo el marcador de las continuaciones densas y
puramente numéricas de un índice. Así CommonMark no las convierte en una lista ordenada ni sustituye
una referencia explícita por el ordinal consecutivo; las listas reales que contienen palabras y las
numeraciones cortas conservan su semántica.

Al materializar decisiones independientes, el borrador clasifica como riesgo alto cualquier cambio
estructural que toque cifras. La frontera de publicación comprueba además el documento ensamblado: para
cada valor, su número de apariciones debe permanecer entre las versiones original y propuesta. Esto
impide que una alineación ambigua de bloques repetidos combine dos decisiones localmente válidas en
una salida que invente o pierda una referencia numérica.

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

La propuesta estructural es una planificación documental de encabezados por directivas. Antes de
invocar Ollama, `semantic_blocks` recorre el documento completo y construye un inventario de
candidatas con su nivel actual, página, rol semántico y coincidencia con el índice. Se priorizan hasta
160 candidatas fuertes para mantener el inventario dentro del contexto local; tienen preferencia los
encabezados existentes, las coincidencias del índice y los bloques ya clasificados como título. El
modelo recibe ese inventario conjunto una sola vez y solo puede devolver pares línea-nivel; nunca se
acepta un bloque documental reescrito. Parsezen reconstruye cada cambio con las palabras exactas de
la línea original, limita a un nivel los movimientos de encabezados existentes y solo permite niveles
1–3 para candidatos nuevos. La propuesta completa vuelve a superar las guardas estructurales; una
respuesta inválida conserva el documento completo y no se guarda como trabajo correcto. Una respuesta
vacía es una decisión válida de no proponer cambios.

`markdown_outline_tree` proyecta, sin IA ni persistencia adicional, los árboles anterior y propuesto
en la revisión de estructura. Los árboles se presentan en paralelo o apilados en anchura compacta;
las decisiones siguen materializándose por cambio y el texto permanece inmutable.

Los checkpoints de fragmentos usan una clave ligada al modo y al contenido, independiente de su
posición ordinal. Al cargar una clave posicional de la versión anterior, se vuelve a validar el
resultado y se migra a la clave estable; así un cambio de planificación no obliga a repetir
fragmentos posteriores cuyo contenido no cambió.

## Conversión PDF

La extracción nativa mantiene geometría de caracteres y líneas. Reconstruye palabras con tracking
artificial a partir de distancias relativas, separa líneas que contienen columnas incluso cuando el
canal entre ellas es moderado y ordena de dos a cuatro bandas contiguas sin mover títulos, pies o
separadores de ancho completo. En una página de índice, una columna de folios separada se vuelve a
asociar por fila con sus entradas antes de retirar márgenes o ruido: exige al menos tres pares, una
alineación vertical inequívoca y suficiente cobertura de la columna numérica. La reparación conserva
el orden multicolumna ya validado y publica cada entrada como un elemento de lista, por lo que los
folios no se confunden con decoración vertical ni todo el índice termina fusionado en un párrafo.
Antes de traducir, el texto de cada entrada queda separado de su folio para impedir que el traductor
o la revisión posterior cambien su función o su posición. Un rótulo de sección sin folio se serializa
como un bloque independiente: nunca se convierte en una continuación perezosa del elemento de lista
anterior al interpretarse como CommonMark.
Los marcadores de
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
Al publicar EPUB, un enlace interno cuyo destino quedó fuera del intervalo elegido conserva su
etiqueta como texto no interactivo. La misma degradación segura se aplica si el usuario elimina el
ancla en el editor: la validación final sigue rechazando cualquier enlace roto que sobreviviese.

Las tablas nativas se extraen con sus celdas y geometría antes de reconstruir las líneas que las
rodean. Una tabla simple usa sintaxis Markdown; una tabla más ancha o con celdas multilínea usa HTML
semántico. Los casos que exceden los límites seguros se degradan a filas etiquetadas, generan una
incidencia de revisión y, si se conservaron imágenes, incorporan un recorte del original. Las líneas
dentro de la caja de la tabla no se publican por duplicado y el resto de leyendas o notas conserva
su posición.

Al construir un EPUB, ese HTML tabular pasa por un analizador XML local que exige exactamente
`table/thead/tbody/tr/th/td/br`, sin atributos, con filas rectangulares y texto escapado. Los bloques
que cumplen el contrato se insertan como XHTML semántico después de CommonMark; todo el demás HTML
permanece desactivado. Así las celdas multilínea no aparecen como etiquetas visibles y la excepción
no abre una vía para scripts, eventos o marcado documental arbitrario.

La guarda compartida de traducción conserva en orden `table/thead/tbody/tr/th/td/br`, incluidos los
pares que representan celdas vacías. Esta comprobación es local a la estructura y se aplica también
al reutilizar una revisión guardada; por ello dos cambios opuestos en tablas distintas no pueden
ocultar una pérdida mediante un simple recuento global.

Cuando una tabla conserva una capa de texto pero sus reglas solo existen en la imagen rasterizada,
la repetición de celdas lado a lado activa un barrido local de reglas horizontales. Los límites de
columna se derivan de la geometría textual y la cuadrícula solo se acepta si cada letra, cifra y signo
del área reaparece exactamente una vez. La caja queda limitada por reglas reales, conserva columnas
solo numéricas y celdas vacías, y se rechaza ante enlaces que no puedan trasladarse o geometría
incoherente. Un falso candidato vuelve al flujo de texto normal y no activa OCR por sí solo.

Las divisiones verticales se calculan dentro del hueco real entre el último glifo de una columna y
el primero de la siguiente. Los rótulos explícitos `Table/Tabla/Cuadro n` situados entre dos grupos
de reglas horizontales separan tablas consecutivas antes de inferir sus columnas; cada región debe
superar por sí misma cobertura exacta, rectangularidad y densidad. El checkpoint de página cambia
de versión cuando se modifica esta geometría para no reutilizar celdas calculadas con límites
anteriores.

Un fragmento corto puede contener solo las reglas exteriores o una cabecera y una única fila. Solo
se completa si la región tiene un rótulo explícito, texto realmente dispuesto lado a lado y límites
de fila no solapados. Cualquier columna puede conservarse aunque su primera celda esté vacía; la
cobertura exacta y las demás guardas siguen siendo obligatorias. La caja exterior se amplía hasta
los glifos de las celdas cuando estos sobresalen ligeramente de la regla rasterizada, evitando cortar
la primera o la última letra.
La cobertura se comprueba contra las celdas extraídas sin alterar; solo al publicarlas se recomponen
los guiones tipográficos que dividen una palabra entre dos líneas.

Una tabla OCR solo sustituye la capa nativa si mantiene una proporción acotada, suficiente
solapamiento léxico, exactamente las mismas cifras sin folios y la secuencia completa de formas
fila×columna. Una salida inflada, incompleta, numéricamente distinta o que omite una columna vacía
se rechaza; la capa de texto útil permanece y conserva el orden multicolumna nativo. Si el
reconocedor une un rótulo
multilingüe `Tabla n` con la primera fila, la limpieza lo separa en su propio párrafo antes de
validar la forma Markdown; el rótulo se conserva y la tabla puede publicarse como XHTML semántico.

La publicación Markdown parte siempre del texto canónico ya transformado. `markdown_export.py`
deriva después el archivo único o el índice con capítulos, metadatos y referencias de página, sin
volver a invocar OCR, Argos u Ollama. `output.py` publica conjuntamente índice, carpeta de capítulos
y recursos y vuelve a generar el conjunto cuando se acepta una revisión.

El plan OCR añade una puntuación de legibilidad basada en densidad alfabética, fragmentación,
glifos sospechosos, texto espaciado y líneas rotadas. Una página de confianza baja se rasteriza con
la estrategia completa; al terminar, el texto OCR sustituye al nativo solo si supera umbrales
absolutos y mejora suficientemente su puntuación. Tablas y páginas sin texto tienen reglas
específicas. Un análisis correcto que no obtiene texto se guarda como checkpoint vacío para evitar
repetir OCR costoso al reanudar; una excepción del motor no se guarda como ausencia de texto. El
informe conserva las páginas analizadas, sustituidas y todavía dudosas.

En páginas gráficas completas, el reconocimiento puede incluir letras decorativas o marcas diminutas
aisladas. Antes de limpiar el Markdown se descartan solo las líneas de una a cuatro letras cuya altura
y anchura sean muy inferiores a la mediana de las celdas OCR de esa página. Esta regla no se aplica a
etiquetas cortas de escala homogénea y nunca elimina la imagen original. La versión del checkpoint OCR
forma parte de su cabecera para que una regla nueva no reutilice texto ruidoso anterior. Una cabecera
etiquetada de otra versión invalida la entrada y fuerza el reconocimiento: nunca se interpreta como
texto heredado del documento. Solo se mantiene compatibilidad con checkpoints antiguos sin cabecera,
anteriores al versionado explícito.

La reconstrucción PDF conserva aparte el OCR crudo y puede contrastar los primeros cuatro folios con
títulos nativos repetidos entre los doce primeros. Solo sustituye una línea cuando la página depende
del OCR, el donante nativo es centrado o tipográficamente prominente, su calidad es alta, la similitud
es casi total y toda la diferencia se reduce a un único término sustantivo corto más conectores. Dos
donantes distintos, cifras diferentes, una palabra larga o una omisión sustantiva cancelan la
reparación. El checkpoint OCR no se modifica: el consenso se aplica únicamente al texto renderizado.

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
modificar título, autor e idioma y resuelve las imágenes directamente desde artefactos cifrados, sin
crear copias temporales sin protección. Todo resultado EPUB pasa primero por
`EpubConfirmationDialog`: muestra los datos esenciales, la portada y el número de capítulos. Desde
ahí se publica directamente o se solicita el editor completo; un bloqueo conserva el borrador para
revisión en vez de publicar.

La planificación Markdown→EPUB usa la clasificación de preliminares e índice. Los títulos del
índice se normalizan y se comparan con encabezados reales; esas coincidencias y los patrones de
capítulo son señales fuertes, mientras que los niveles Markdown procedentes de la geometría PDF
aportan la señal general. Los encabezados del índice nunca abren por sí solos un capítulo del cuerpo.

La jerarquía explícita usa un helper independiente de Qt que clasifica solo títulos numerados de
contenedor (`Part`, `Parte`, `Book`, `Libro`, `Volume`, `Volumen`, `Section`, `Sección` o `Tomo`) y
de capítulo (`Chapter`, `Capítulo` y variantes razonables). Un contenedor solo se convierte en padre
cuando le sigue una racha contigua de al menos dos capítulos explícitos; un epílogo, apéndice o
título ambiguo posterior queda como raíz. Si no hay evidencia suficiente, todo permanece plano.
`create_book_from_markdown` conserva palabras, identificadores y orden, limita el resultado a un
nivel de anidado y calcula el spine en preorden.

La publicación genera EPUB 3 con:

- `mimetype` sin compresión y en primera posición;
- paquete OPF;
- navegación anidada;
- XHTML validado;
- imágenes y portada;
- referencias locales comprobadas y contenido activo o remoto rechazado;
- estilos conservados únicamente después de retirar importaciones y URL remotas;
- encabezados indivisibles mediante `break-inside` y su compatibilidad paginada, ajuste de palabras
  largas dentro del ancho disponible y ausencia de salto inmediatamente después cuando el lector lo
  admite; un relleno superior no colapsable impide además que el primer encabezado de un capítulo
  quede pegado al borde en motores paginados;
- saneamiento final de metadatos, Markdown y XHTML que sustituye únicamente caracteres prohibidos
  por XML 1.0, sin unir palabras ni modificar recursos binarios;
- validación final del ZIP, contenedor, OPF, manifiesto, orden de lectura, tabla de contenidos,
  recursos, XML y anclas antes de exponer el resultado;
- escritura atómica del archivo final.

La traducción directa conserva inicialmente el paquete y además prepara siempre su representación
editable. EPUB→EPUB sin otra operación también crea este resultado pendiente de personalización.
Las unidades semánticas pueden viajar agrupadas para reducir llamadas, pero la detección y
reparación de texto residual se ejecuta sobre cada unidad antes de guardar el checkpoint; así una
frase breve sin traducir no queda diluida entre metadatos, navegación y atributos ya traducidos.
Las etiquetas, atributos, espacios de nombres y enlaces se sustituyen por marcadores de código
obligatorios antes de cualquier traducción o reparación con IA. Ollama recibe solo el texto visible;
Parsezen restaura el XML original en el mismo orden y vuelve a validar firma estructural y XHTML.
Cada subfragmento de Ollama validado se cifra además en el espacio general de trabajo antes de
completar la unidad semántica; una pausa dentro de un capítulo grande reutiliza esas respuestas sin
esperar a que termine el lote exterior. La intención del plan forma parte de ambas claves para
impedir que un resultado estándar se reutilice como resultado revisado.
La publicación tras la confirmación —directa o después del editor— reconstruye el libro normalizado
para garantizar un EPUB válido, editable y coherente con los metadatos y la portada elegidos.

Tanto la confirmación como el editor ofrecen `Guardar y salir`. La primera conserva los metadatos;
el segundo guarda además portada, estructura y sección visible. Escape, volver y el rechazo externo
siguen el camino recuperable. `Descartar cambios` en el editor solo abandona sin guardar después de
confirmar cuando el borrador está sucio; ninguna salida modifica el original.

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
- informes OCR y de traducción, incluida la cobertura lingüística sin contenido;
- propuesta de corrección;
- recursos;
- metadatos EPUB;
- telemetría por etapa y conteos semánticos sin contenido.

`ProcessTelemetry` agrega duración y número de visitas por `ProcessStage`; no almacena nombres,
rutas, prompts, términos ni texto. La validación real usa el mismo resultado para informar tiempos,
páginas OCR sustituidas o dudosas y conteos de preliminares, índice y memoria terminológica.

`OutcomeSummary` es el contrato de cierre para interfaz y actividad. Mantiene separados el control
de integridad final, las incidencias detectadas por las etapas, la cobertura lingüística y las
decisiones de revisión manual; ninguna ausencia de avisos se presenta como una certificación
semántica. El historial
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

- El origen es inmutable: al añadirlo se conservan tamaño, fecha y SHA-256. La preparación vuelve a
  comprobar su contenido y detiene el trabajo si ha cambiado; el mismo digest verificado identifica
  los checkpoints PDF, EPUB y de transformación sin releer el archivo para cada caché.
- Una salida se construye fuera de su destino y se reemplaza solo al estar completa.
- Las colisiones producen un nombre nuevo.
- Una cancelación no publica un parcial.
- Los ZIP de DOCX y EPUB tienen límites de entradas, expansión, rutas, tamaño por miembro y ratio de
  compresión; los recursos EPUB sin cambios se copian en streaming.
- XML usa analizadores sin entidades ni red.
- HTML editable elimina scripts, formularios, eventos y URL ejecutables.
- Los logs están sanitizados y rotan.
- Ollama solo acepta loopback y modelos locales; Argos y OCR son locales. La autorización para
  enviar texto exige `server.json` con la nube desactivada: el entorno del cliente no acredita el
  estado de un servidor Ollama que ya estuviera activo.
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

El icono oficial conserva su contorno protector blanco y solo elimina el fondo negro exterior
conectado al lienzo. Ese mismo símbolo se compone con los wordmarks claro y oscuro existentes. Los
derivados reproducibles y transparentes cubren las dos variantes de cabecera, README, icono PNG e
ICO multirresolución; por tanto, ventana, barra de tareas, instalador y documentación comparten una
única identidad visible sobre fondos claros y oscuros.

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

`scripts/benchmark_documents.py record --runs N` puede calibrar una referencia privada con varias
ejecuciones. Exige que huella Markdown y contadores estructurales coincidan en todas ellas y conserva
el peor tiempo y pico de memoria observados antes de añadir el margen de regresión. Así, una salida
no determinista impide crear la referencia y la variación normal de OCR no se confunde con un cambio
funcional. Los manifiestos y resultados viven en `local-benchmarks/`, fuera de Git.

El instalador se construye únicamente bajo demanda o para una etiqueta de versión.

La distribución pública usa un instalador Inno Setup sin Authenticode. El workflow de Windows parte
del lock CPU, valida código, cobertura y vulnerabilidades, genera avisos legales, crea el paquete con
PyInstaller y ejecuta el smoke test del binario antes de construir el instalador. El artefacto
incluye un checksum SHA-256 y el inventario exacto del entorno. En una etiqueta, GitHub añade además
una atestación de procedencia vinculada al commit; esta atestación no sustituye una firma reconocida
por Windows ni evita por sí sola los avisos de editor desconocido.

Las compilaciones manuales solo producen candidatos temporales. Una versión para usuarios se
publica como GitHub Release en borrador desde una etiqueta inmutable perteneciente a `main` y adjunta
automáticamente el instalador, su checksum y el inventario generados en la misma ejecución. El
nombre de esa etiqueta debe coincidir exactamente con `v` y la versión de `pyproject.toml`. El job de
compilación conserva permisos de solo lectura; escritura de contenido, OIDC y atestaciones se
habilitan únicamente en el job posterior que publica una etiqueta validada. El
instalador conserva el `AppId` entre versiones, limpia el runtime `_internal` anterior
antes de actualizar y no toca `%LOCALAPPDATA%\Parsezen`, los originales ni los resultados.
