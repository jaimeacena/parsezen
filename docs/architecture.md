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

La prioridad es lexicográfica: integridad y privacidad preceden a fidelidad; fidelidad precede a
recuperación; recuperación precede a rendimiento y comodidad. Una optimización de una prioridad
inferior no puede degradar otra superior.

## Cómo leer esta arquitectura

Este documento describe el sistema **implementado** y las decisiones estructurales vigentes. No es un
backlog ni un diario de experimentos. La orientación para agentes está en
[`agent-operating-model.md`](agent-operating-model.md), el estado y el siguiente gate en
[`work-plan.md`](work-plan.md), y los criterios de publicación en
[`acceptance-checklist.md`](acceptance-checklist.md).

Para evitar releer todo el documento, se entra por la pregunta:

| Pregunta | Sección |
|---|---|
| ¿Qué posee cada estado y en qué dirección dependen las capas? | Capas |
| ¿Cómo se configura, explica y ejecuta un documento? | Trabajo independiente y Planificador |
| ¿Cómo se conserva o decide una incertidumbre? | Revisión por fase |
| ¿Cómo se extrae y contrasta un PDF? | Conversión PDF |
| ¿Cómo se representan partes, capítulos y EPUB? | Libro normalizado y EPUB |
| ¿Qué sobrevive a un cierre y qué se cifra? | Persistencia y Seguridad de datos |
| ¿Cómo se proyecta el dominio sin duplicarlo? | Sistema de presentación e Interfaz |
| ¿Qué evidencia evita regresiones? | Pruebas |

Los nombres de clases y módulos identifican propietarios actuales; las cifras de un experimento o el
orden de trabajo futuro pertenecen al plan o a una política. Si una implementación cambia, esta
arquitectura y sus pruebas se actualizan en el mismo incremento.

## Columna vertebral del sistema

Parsezen no es una colección de conversores. Es una cadena de evidencia que transforma una intención
inmutable en una publicación autorizada:

```text
DocumentSource + JobConfiguration
                ↓ compilan una vez
           ExecutionPlan
                ↓ gobierna
 PreparedDocument → TransformedDocument → [ReviewSession] → [BookDocument]
          │                 │                   │               │
          └──── informes, cobertura, hashes y decisiones ──────┘
                                ↓
                    FinalIntegrityReport
                                ↓
                    publicación atómica
                                ↓
                       OutcomeSummary
```

Los elementos entre corchetes aparecen solo cuando el plan requiere revisión o una salida EPUB.

La información solo puede avanzar si mantiene los contratos anteriores. Una fase puede añadir
evidencia, una propuesta o una decisión, pero no borrar una duda ni redefinir silenciosamente el
origen. Las guardas deterministas autorizan integridad estructural; los informes localizan señales;
la revisión humana decide ambigüedad; el libro mayor final autoriza la escritura. Ninguno sustituye a
los demás.

Esta espina dorsal impone cinco reglas de síntesis:

1. **un plan**: preflight, runtime, explicación e interfaz consumen el mismo `ExecutionPlan`;
2. **un propietario por estado**: dominio posee verdad durable, aplicación coordina, presentación
   proyecta y los procesadores transforman;
3. **una fuente autoritativa por identidad lógica**: snapshots y checkpoints son proyecciones
   ligadas al contenido, no otro estado mutable en competencia;
4. **una incertidumbre explícita**: un fallback seguro sigue contando como pendiente semántico;
5. **una publicación**: solo el candidato completo, validado y sincronizado sustituye el resultado.

La optimización de recursos sucede dentro de estas reglas: reutilización por contenido, invalidación
solo de dependientes, comprobaciones baratas antes de IA, IA sobre unidades mínimas y atención humana
solo cuando puede cambiar una decisión.

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

`processing.py` mantiene la fachada Qt-free `process_document()`. Los contratos inmutables de
solicitud, preparación, transformación, resultado y telemetría viven en `pipeline/contracts.py`, de
modo que aplicación, infraestructura y presentación ya no necesitan importar el orquestador para
intercambiar datos. `pipeline/prepare.py` convierte el origen, resuelve el rango PDF, preserva
recursos, recopila calidad inicial y construye el documento semántico sin transformar texto ni
publicar archivos. `pipeline/transform.py` concentra traducción, corrección, verificación bilingüe,
calidad lingüística y propuestas de revisión; solo devuelve un contrato en memoria y no conoce el
staging final. `pipeline/publish.py` recibe los contratos preparados y transformados, construye la
salida elegida, aplica integridad y usa las primitivas atómicas existentes; no traduce ni invoca IA.
La ruta EPUB→EPUB separa además traducción del paquete, preparación
editable/revisión y publicación. Estos límites usan contratos concretos y evitan una jerarquía
genérica de procesadores que no aportaría comportamiento actual.

Las revisiones pendientes se recuperan mediante manifests cifrados v2 bajo generaciones
`artifacts/<job>/<generation>/`. El puntero SQLite cambia solo después de escribir textos, recursos y
manifest; cada texto lógico se guarda una vez y el manifest contiene referencias, estado mínimo de
publicación e identidad fuerte del origen cuando ya fue calculada en preflight. Se mantiene lectura
v1, mientras la escritura es siempre v2. Tras un cierre abrupto solo se eliminan generaciones
completas y no referenciadas; archivos desconocidos se conservan. Los recursos se materializan al
recuperar porque `ProcessResult` aún los expone como bytes; hacerlos lazy requeriría estrechar ese
contrato en una fase posterior y no compensa introducir un proxy ahora.

Los dos procesadores más grandes conservan una fachada estable, pero sus subsistemas con invariantes
propias ya no están mezclados:

- `improvement.py` coordina fragmentos y revisión bilingüe; `improvement_contracts.py` contiene sus
  tipos compartidos, `local_ai_transport.py` es el único transporte de transformación hacia Ollama y
  `ai_markdown_safety.py` protege, reconcilia y valida Markdown sin depender de red ni selección de
  modelos;
- `pdf_conversion.py` coordina extracción, OCR y renderizado; `pdf_layout.py` contiene el modelo
  inmutable de página y `pdf_checkpoints.py` serializa y valida su reanudación sin abrir PDFs ni
  invocar OCR.

El límite de dos millones de caracteres pertenece únicamente a las superficies interactivas de
revisión. El análisis semántico determinista y el libro mayor de integridad no lo reutilizan: un PDF
largo sin transformación de IA puede estructurarse, empaquetarse y verificarse completo aunque su
Markdown no sea práctico de abrir en el editor interno. Los límites más estrictos de traducción y de
IA permanecen independientes y se aplican antes de cualquier petición al modelo.

OCR y Argos conservan protocolos, límites y máquinas de estado independientes. Comparten únicamente
las primitivas mecánicas de `workers/private_channel.py`: JSON acotado sin pickle, listener local
autenticado, arranque oculto, espera cancelable y cierre terminate/kill. Esta pieza no conoce páginas,
Markdown, diagnósticos ni resultados, y por tanto no constituye un framework común de workers.

En presentación, `local_ai_workflow.py` posee tanto el estado visible como el flujo de descubrir,
recomendar, instalar, seleccionar y borrar modelos. Recibe dependencias y callbacks tipados, conecta
explícitamente las señales del controlador y proyecta sus cambios al workspace. `main_window.py`
permanece como raíz de composición, pero ya no replica ese estado ni reenvía métodos mediante
`__getattr__`; solo consume la interfaz pública del coordinador y hereda de `QMainWindow`.

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
herramientas desplazable. El mensaje calcula su altura desde el ancho disponible y su pantalla liga
la vigencia al trabajo que lo originó; el historial durable pertenece a Actividad. Layout, estado y
contenido específico continúan en su pantalla.

La configuración de IA local se presenta como dos filas compactas de capacidades fijas
(`Traducción IA` y `Revisión IA`).
`presentation/component_setup.py` solo proyecta estados evaluados por `component_readiness.py`:
`Preparado`, `Descargable` o `Equipo insuficiente`. No lista modelos arbitrarios, no acepta tags ni
endpoints y solo emite una solicitud de preparación; la descarga y la comprobación local pertenecen
a los coordinadores externos. El diálogo admite un catálogo o una instantánea de estados explícitos
para mantener la capa Qt fuera de la decisión de disponibilidad.

El shell tiene composiciones de escritorio, intermedia y compacta en 960 y 640 px, y todos los
flujos esenciales aceptan reflow hasta 320 px. Una sola región superior muestra marca, destino y
acciones de cola, o bien volver, título y Ajustes en una página interna. Las etiquetas de acción se
reducen a iconos en compacto sin reordenarse ni crear una segunda fila. Ajustes, actividad,
apariencia, diagnóstico e IA local comparten un único menú global. El editor EPUB conserva los dos
paneles en escritorio y, al
reducir el ancho, agrupa herramientas secundarias en un menú en vez de introducir desplazamiento
horizontal. El contrato, paleta y matriz de contraste están en `docs/ui-design-system.md`.

## Trabajo independiente

`DocumentJob` contiene:

- identidad y orden;
- ruta, formato, tamaño y modificación fijados al preparar el origen;
- configuración mínima por documento;
- el plan de producto (`STANDARD` o `LOCAL_AI_REVIEWED`);
- una instantánea compatible del modelo/contexto globales y, cuando existe política de
  componentes, de los pares independientes de traducción y revisión;
- revisión de configuración;
- estado de cada fase;
- resultado, avisos y error.

`OutputConfiguration.configured` separa un documento recién añadido de un trabajo ejecutable.
Mientras sea falso, el planificador no lo incluye y `PUBLISH` no participa. La presentación abre
una página de ajustes dentro de la pila de la ventana principal. El formato se proyecta como dos
tarjetas visuales exclusivas, la revisión adicional con IA como interruptor y el resto de decisiones como una
fila de etiqueta, valor y chevron. Los documentos nuevos presentan
`STANDARD`; una configuración guardada conserva su plan. IA local y OCR automático
son valores iniciales. Traductor y glosario dependen del idioma; Páginas y OCR aparecen solo para
PDF. El intervalo se edita en un diálogo efímero y la fila conserva únicamente su valor resumido.
Solo Markdown y EPUB son resultados de producto. La página no incorpora scroll ni vuelve a
proyectar destino. Tampoco añade un resumen técnico permanente del recorrido: el interruptor conserva
una descripción accesible y contextual sobre la corrección o verificación efectiva. La presentación
no duplica reglas del procesador.

`STANDARD` se resume como procesamiento directo; `LOCAL_AI_REVIEWED`, como revisión adicional con IA
local, activa la revisión de texto y, únicamente para EPUB, la revisión de estructura. No existen
combinaciones independientes de corrección y estructura. Cada elección válida reemplaza de forma
atómica la configuración del documento y se persiste inmediatamente; una elección incompleta por
falta de IA conserva su valor visible y dirige a `Componentes de IA local` sin publicar una
configuración inválida.

La revisión adicional es una decisión reversible y nunca se aplica a configuraciones ya guardadas ni
a trabajos nuevos sin que la persona la active. La evaluación local no demostró recall suficiente
para presentarla como una mejora universal, por lo que `STANDARD` sigue siendo el valor inicial. El
informe de validación registra por separado las
propuestas de contenido y estructura, sus decisiones recomendadas, las incidencias objetivas y el
tiempo; una propuesta aceptable no se interpreta por sí sola como evidencia de mejor calidad.

`application.processing_explanation.processing_flow` es la única proyección del recorrido visible.
Deriva pasos compactos, pasos detallados y una política humana (`NONE`, `IF_CHANGES` o
`BEFORE_PUBLISHING`) sin depender de Qt. La cola integra la decisión humana como último paso de la
misma secuencia y solo la distribuye en dos líneas cuando el ancho lo exige, sin convertirla en una
nota secundaria ni perder el conector entre pasos. La tabla asigna a Flujo la mayor proporción del
ancho semántico y conserva anchos específicos para las acciones. Preflight reutiliza la misma ruta
con motor y formato. La salida EPUB exige una
revisión final, mientras Markdown solo la anticipa cuando el plan puede producir propuestas. Las
incidencias excepcionales siguen apareciendo dinámicamente en Estado.

Cada paso automático declara además las `StageKind` que representa. La presentación deriva de esas
referencias los tonos `default`, `current`, `completed`, `future` y `failed`; la revisión humana usa
un estado actual específico cuando el trabajo queda bloqueado para decidir. Así, una etiqueta
combinada puede abarcar varias fases internas sin comparar textos ni duplicar el planificador.

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
`LOCAL_AI_REVIEWED` se mantiene para quien quiera solicitar desde el principio una revisión
proactiva a escala del documento; su selección sigue siendo adaptativa y la cobertura declara los
bloques realmente revisados. La recomendación posterior limita el coste a señales concretas del
recorrido normal sin pretender que ambos flujos sean equivalentes.

`TranslationMethod` ofrece exactamente dos motores locales: `OFFLINE` usa Argos y `LOCAL_AI` usa el
componente local de traducción preparado por Parsezen. IA local es la opción inicial y contextual;
Argos es la alternativa manual, ligera y predecible. No existe degradación automática del documento
completo de IA local a Argos: elegirlo como motor exige una acción explícita y la validación integral
solo lo prueba al recibir `--translation-engine argos`. Hay una excepción de reparación estrecha: si
un único título permanece literalmente en el idioma de origen después del reintento de IA, un paquete
Argos directo ya instalado puede proponer solo ese título. No descarga paquetes, no cambia el motor
elegido y la propuesta debe superar las mismas guardas antes de incorporarse. La elección del motor es independiente de
`ProcessingPlan`: el
plan revisado puede actuar después de cualquiera de los dos. `AIProfileConfiguration` conserva el par
global antiguo para compatibilidad y puede llevar snapshots independientes de traducción y revisión;
el runtime deriva de ellos los settings de cada fase y cae al par global en cargas antiguas. No hay
excepciones editables por documento ni mutaciones de la instantánea al cambiar preferencias. Si el
valor inicial necesita IA y falta un componente local válido, la configuración conserva la intención,
muestra la causa junto al control y dirige a `Componentes de IA local` antes de guardar.

La selección y procedencia de perfiles globales distintos para traducción y revisión se rige por
`docs/local-ai-model-policy.md`. La política no fija tags: los snapshots solo guardan identidad sin
contenido y el árbitro OCR visual permanece como una capacidad independiente, no asignada implícitamente
a ninguno de los perfiles textuales.

`LinguisticReviewCoverage` registra sin texto documental cómo se obtuvo la confianza lingüística:
sin revisión semántica, corrección integrada en la traducción, verificación bilingüe independiente o
verificación dirigida. Separa el total de bloques traducidos, los comprobados por las heurísticas,
los revisados semánticamente, los verificados de forma independiente y las incidencias restantes.
`TranslationQualityReport` conserva además los totales de bloques de origen y salida para no ocultar
la parte no alineada. La cobertura viaja en `ProcessResult`, las instantáneas cifradas y el resumen
sin contenido de actividad; nunca convierte una comprobación automática en verificación semántica.
La revisión residual toma sus unidades de las incidencias `SOURCE_TEXT` ya demostradas y no escala
por una puntuación agregada, longitud o terminología rara. Tablas, índices, código, imágenes y
procedencia quedan fuera de la corrección genérica; solo la prosa señalada puede generar nuevas
peticiones bilingües.

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
las revisiones y la presentación de actividad. `domain.process_lifecycle` declara en una sola tabla
cada `ProcessStage` y su `StageKind`, `AttemptPhase` y etiqueta segura de diagnóstico; la suite exige
que ningún valor físico quede sin las cuatro proyecciones. Una traducción con Ollama emite
`TRANSLATING` y no reutiliza el estado visual de corrección.

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
nombres, tamaño original ni texto. Esta información se proyecta en la cola y en sus ayudas sin
interrumpir `Procesar` con una confirmación informativa. Los problemas de configuración continúan
bloqueando antes de crear el trabajador.

`should_run_early_check` limita la comprobación representativa a PDFs cuyo coste o incertidumbre
pueden justificarla: al menos 120 unidades, o 60 con OCR forzado, IA local o salida EPUB.
`run_early_check` inspecciona un máximo de nueve posiciones del intervalo y elige hasta cinco que
cubren extremos, densidad textual, contenido visual y tablas; cuando las características se
concentran en los extremos, completa la muestra con las posiciones restantes más distribuidas.
Ejecuta cada una mediante el mismo `process_document`, con salidas en un directorio temporal, pero
usa una solicitud de sondeo: convierte y valida todas las páginas elegidas, omite construcción EPUB,
revisión final y reestructuración, y solo prueba la traducción en la primera, central y última de la
muestra. De este modo se comprueba el motor sin multiplicar hasta cinco veces las pasadas caras. Una
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

La ventana principal contiene una pila de páginas. Cabecera global, títulos internos, mensajes y cola
comparten un rail exterior centrado de 1.280 px; los formularios conservan límites interiores más
estrechos cuando su lectura lo requiere. La cola usa una tabla compacta: su panel ajusta la altura a
una, varias o seis filas visibles y después desplaza solo el contenido. En vacío, la importación se
ancla bajo la cabecera y las acciones duplicadas quedan ocultas; tras añadir documentos, `Añadir` y
la única acción principal del lote aparecen en esa misma cabecera, junto al destino. El contador se
mantiene como encabezado del contenido sobre la tabla. `job_view_model` deriva el
estado, el contador y la acción contextual. Los mensajes terminales conservan los identificadores del
lote: se recalculan si se retira parte y desaparecen al eliminar el último trabajo relacionado.
Configuración, IA local, revisiones y editor EPUB se presentan dentro de la pila con una única
cabecera interna (`Volver`, título y Ajustes); IA local se abre desde Ajustes o desde acciones
contextuales que requieren sus componentes;
solo el editor compacto de glosario y el selector de intervalo son diálogos modales acotados sobre su
contexto. Como no existen cambios pendientes, la flecha de vuelta y Escape cierran
configuración sin confirmación. Si hay una inconsistencia muestra su causa y conserva la página para
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

`domain.execution_plan.ExecutionPlan` es el único plan fino autoritativo. Se compila desde
`DocumentSource + JobConfiguration` y contiene solo el conjunto cerrado de pasos Convert,
TranslateAI, TranslateOffline, RepairTranslation, ReviewContent, ReviewStructure, PreserveEpub,
BuildEpub y WriteMarkdown. Preflight, explicación, selección de perfiles IA y pipeline consumen esa
misma secuencia; el adaptador temporal para solicitudes planas delega siempre en el mismo compilador.

`ProcessRequest` y `ProcessResult` conservan por compatibilidad sus campos planos, pero exponen vistas
inmutables agrupadas para origen, traducción, revisión, publicación y calidad. Estas vistas se calculan
desde el contrato existente y no duplican estado, por lo que `dataclasses.replace()` y los snapshots
siguen teniendo una única fuente de verdad mientras los consumidores migran progresivamente.

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

`application.queue_session.QueueSession` posee el estado efímero de esa ejecución secuencial: plan
preparado, runtimes por trabajo, documento activo, conjunto iniciado, revisión dirigida, petición de
pausa y motivo terminal. Sus transiciones de inicio, selección, continuación y cierre no dependen de
Qt. `ParsezenMainWindow` conserva la composición y la presentación de avisos, pero ya no mantiene
copias de `_is_processing`, `_batch_running`, `_pause_requested` o `_current_job_id`.

Tres fachadas de aplicación mantienen la coordinación fuera de Qt: `QueueRunCoordinator` reclama el
siguiente trabajo y resuelve el ciclo de la sesión; `ReviewFlowCoordinator` materializa, reconcilia y
publica revisiones mediante los servicios especializados existentes; y `WorkspaceRecoveryController`
restaura, verifica, respalda y poda el estado recuperable. La ventana conserva signals, slots,
diálogos y proyección visual, sin introducir un bus de eventos o una capa MVVM.

La proyección estructural del workspace es dirigida por eventos: cambios de cola, configuración,
resultado, revisión, IA o integridad la actualizan de forma explícita. Un timer de un segundo queda
limitado al tiempo restante y al reintento de la persistencia diferida; no vuelve a proyectar la cola,
pronósticos ni informes de integridad. `QueuePersistenceCoordinator` conserva su throttling durable.

`ProcessingRunner`, en la capa de presentación, es el único adaptador Qt que posee el trabajador
físico y su token de cancelación. Recibe una solicitud ya preparada, ejecuta el procesador estable en
el `QThreadPool` y publica eventos de fase, progreso, resultado, error, cancelación y finalización. La
ventana conecta esos eventos con `QueueSession` y los coordinadores de resultados; ya no construye
ni conserva trabajadores, y ninguna regla del planificador depende de Qt.

La planificación EPUB separa jerarquía semántica de profundidad visual. El índice impreso puede
confirmar `Parte → Capítulo → Sección` aunque los `h1`/`h2` extraídos no reflejen esa relación; los
marcadores internos del PDF actúan como segunda señal. Los capítulos numerados de nivel principal
confirmados por el índice son límites obligatorios, mientras que los niveles densos y los cortes por
tamaño no se convierten por sí solos en nuevas raíces del menú. Los títulos ornamentales fragmentados
solo se recomponen con evidencia única del mismo documento. `BookSection` conserva el árbol y el
spine en preorden; las subsecciones continúan como destinos internos del XHTML.

Los índices visualmente planos o con sangría mixta solo se normalizan a dos niveles cuando contienen
al menos dos contenedores explícitos y cuatro entradas dependientes. La negrita completa funciona como
raíz únicamente si toda la tabla carece de niveles; en tablas mixtas mandan la sangría y los rótulos
estructurales. Partes, apéndices y grupos de tablas sin folio se incorporan como filas raíz solo si son
adyacentes a una entrada paginada de la misma página. Antes de analizar bloques, un encabezado ATX
adyacente a prosa recibe únicamente el salto en blanco necesario para exponer una frontera que
CommonMark ya considera válida; no se modifica ninguna palabra. Una coincidencia de índice sin
encabezado corporal recuperable nunca crea contenido ni un destino sintético.

El procesador registra telemetría física con eventos `processing_*`; el runner registra el ciclo del
intento como `processing_attempt_*`. El dominio no escribe logs y Diagnóstico se limita a representar
tokens seguros. Los nombres distintos evitan que inicio, cierre o fallo parezcan el mismo evento
emitido dos veces por capas diferentes.

`ProcessTelemetry` mide duración y visitas de cada etapa y el transporte de Ollama agrega, solo en
memoria, tiempo de pared, tokens de entrada/salida, carga declarada por Ollama y tokens por segundo.
Las generaciones tienen tanto timeout de inactividad como un deadline total acotado; OCR aplica un
deadline total por página y termina el proceso privado si lo supera. Ninguna de estas métricas incluye
texto, prompts, respuestas o rutas y las métricas de Ollama no entran en las instantáneas.
El validador opt-in de flujos reales registra además los ordinales de los fragmentos conservados, sin
su texto, para localizar el punto exacto de revisión sin degradar la privacidad del informe.

La pausa entra siempre por `ProcessingRunner.pause()`: marca al trabajador antes de activar su token
de cancelación cooperativa y cierra la fase activa como `paused` en la traza del intento. La ventana
proyecta ese mismo motivo en el dominio, conserva los checkpoints, no crea una fila de actividad
`cancelled` y deja que el planificador elija después `RunMode.RESUME`. La cancelación real mantiene el
recorrido separado y sí elimina el trabajo temporal exacto.

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
separa la lista de intentos y el detalle formateado en dos paneles de un `QSplitter`; cambia a eje
vertical en anchos compactos. Solo ofrece `Volver al documento` cuando el origen coincide con un
trabajo fallido todavía presente en la cola. Los intentos históricos no se convierten en una
biblioteca ni en una acción de reintento.
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

Para un EPUB directo, el borrador conserva también el paquete original cifrado, la ruta real de cada
documento del spine y una huella de toda la estructura no editable en sitio. Si no cambia nada, la
publicación reutiliza exactamente los bytes ya validados. Si solo cambia el cuerpo de uno o varios
capítulos, fusiona esos cuerpos sobre su XHTML original y reconstruye el ZIP copiando byte por byte
todos los demás miembros. Cambios de metadatos, portada, recursos, orden, jerarquía o títulos invalidan
esa huella y vuelven de forma segura al constructor EPUB normalizado. El archivo definitivo se toca
una sola vez, después de preparar y validar el candidato completo.

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

La recomendación inicial sigue siendo provisional, pero aparece preseleccionada al abrir una unidad:
se conserva el original cuando las guardas lo recomiendan y se usa la propuesta en el resto de casos
seleccionables. La persona puede avanzar directamente con `Siguiente`, cambiar la elección o editar
la propuesta; el botón confirmado muestra su estado y el panel elegido se tiñe sin dibujar un borde
de selección alrededor de todo el panel. La
salida por guardar, volver o cerrar conserva una edición o una elección cambiada del caso visible;
abrir y salir sin interacción no marca la recomendación como resuelta. `Guardar y salir` cifra el
estado y permite reanudar en la primera unidad pendiente. La presentación omite el resumen del caso
cuando solo repetiría el progreso: enseña prioridad alta o crítica, aviso, etiqueta o sugerencia
concreta únicamente cuando aportan una decisión. `Anterior` se oculta si no existe un destino real,
las acciones de localizar y restaurar viven en el menú del panel y la acción masiva solo aparece en
fases de corrección con varias unidades.

Las unidades OCR se anclan al tramo completo de la página dentro del Markdown ya transformado,
desde su marcador privado hasta el marcador siguiente. Así, en un flujo con traducción, la persona
edita el texto final y la decisión se aplica sobre la misma instantánea que se publicará; nunca se
intenta insertar texto fuente obsoleto dentro del resultado traducido. El contenido de ese tramo
forma parte del identificador estable de la unidad. Una revisión OCR anterior cuyo candidato ya no
coincida se reemplaza una sola vez por la versión actual y descarta sus elecciones incompatibles.
La traducción conserva además las mayúsculas completas de cada tramo reconocido que ya estaba
íntegramente en mayúsculas. La prosa mixta conserva las convenciones naturales del idioma de destino;
los títulos, índices y rótulos de tabla alineados preservan también las mayúsculas iniciales
deliberadas de sus términos, incluidas las separadas por comas, barras o saltos de línea. La regla no
se activa por una sola letra ni aplica una conversión global a Title Case.
Al materializar de nuevo un candidato con la misma identidad, todos sus artefactos de contexto se
renuevan. Una elección automática compatible puede continuar, pero una edición humana cuyo artefacto
ya fue limpiado vuelve a quedar pendiente en vez de abrir una referencia inexistente.

Antes de preparar el editor EPUB, el ensamblado vuelve a reconciliar todas las decisiones OCR y de
traducción ya aplicadas. Si no puede demostrar que una decisión está presente ni volver a anclarla,
bloquea la publicación. Cada borrador de libro persiste además la huella SHA-256 del Markdown revisado
que lo originó: solo se reutiliza mientras esa huella coincida y se reconstruye cuando una decisión
humana modifica el texto.

La aplicación de una secuencia de revisiones también es idempotente dentro de la sesión activa. Si
una decisión anterior —por ejemplo, OCR— ya dejó exactamente el texto elegido por una decisión
posterior —por ejemplo, traducción—, esa segunda decisión se reconoce como aplicada y reserva el
fragmento correspondiente. Solo se considera desincronización cuando no se puede anclar ni el texto
anterior ni el resultado elegido.

Las incidencias por crecimiento anómalo muestran para revisar el resto completo de la página PDF
sospechosa —con un límite defensivo— en vez del extracto truncado del informe. La sustitución conserva
los separadores estructurales que rodean el tramo editable, por lo que una decisión no puede pegar dos
títulos o párrafos contiguos. El detector de texto fuente compara también letras sin espacios ni
puntuación para reconocer residuos en los que un OCR defectuoso haya unido palabras.

La traducción local agrupa las tiradas de tres o más títulos de un índice y admite fragmentos de
prosa de hasta un límite conservador de 2.400 caracteres. Los títulos aislados siguen recibiendo su
tratamiento enfocado. Las guardas protegen párrafos, encabezados, tablas, enlaces, cifras, marcadores
y la secuencia semántica de cursivas, negritas y tachados de cada bloque. Los delimitadores
equivalentes (`*`/`_`) son intercambiables, pero quitar, añadir, anidar de otra forma o trasladar el
énfasis a otro párrafo se rechaza. Si una respuesta agrupada no supera las guardas, el mismo intento
se degrada automáticamente a segmentos más pequeños. Así se evita una petición distinta por cada
entrada breve de un índice sin debilitar la recuperación segura.
Los pares de delimitadores de énfasis se sustituyen temporalmente por marcadores emparejados que
dejan visible al modelo el texto interior. Tras restaurarlos, se reconcilian únicamente los espacios
adyacentes y se elimina cualquier énfasis simple inventado fuera de los tramos fuente. Si un bloque
denso supera el presupuesto de valores protegidos y no contiene un límite oracional seguro, puede
dividirse entre tramos de énfasis consecutivos; nunca dentro de uno. La clave de checkpoint cambia
con este contrato para no reutilizar respuestas obtenidas con delimitadores desprotegidos.

Las tablas generadas se traducen por lotes alineados y aplican antes y después del modelo un léxico
convencional acotado para encabezados, signos, partes y clasificaciones. La misma normalización puede
actualizar un checkpoint válido sin repetir su petición. Los reintentos enfocados se contabilizan
separados de los lotes normales. Una tabla Markdown conserva literalmente delimitadores, alineación
y espaciado; la tabla XHTML intermedia conserva además cada entidad HTML byte por byte. Los códigos
numéricos de esas entidades no cuentan como cifras visibles del documento. Un salto interno heredado
de una celda es válido después de la normalización PDF, mientras uno añadido a una celda de una sola
línea se rechaza. Una celda con varias líneas traduce cada tramo alineado por separado y aporta a todos
ellos el texto visible completo de la celda como contexto acotado; los saltos estructurales restantes
permanecen fuera del modelo. En traducciones
inglés→español, el ordinal numérico completo se protege antes de la petición y, una vez restaurado, se
localiza junto con las abreviaturas de era copiadas literalmente de forma determinista fuera de código,
enlaces y URL. Los campos de plantilla de una o dos llaves también se conservan literalmente.

Después del reintento alineado de una tabla, las celdas que aún repiten léxico inglés de su original
se aíslan como microunidades bilingües. Cada una recibe como máximo una reparación enfocada con el
original y la propuesta; solo se traslada el texto si supera las mismas guardas de cobertura, cifras,
idioma y estructura. Si la señal continúa, se conserva la celda original y el informe registra solo
el recuento. Así un rótulo dudoso no obliga a revisar de nuevo la tabla ni permite que el modelo
reescriba sus filas.

Antes de traducir inglés→español, la preparación puede añadir una memoria terminológica astrológica
solo cuando el documento contiene al menos tres señales inequívocas del dominio. La memoria usa
sintagmas completos con artículos y concordancia, nunca sustituciones aisladas como `chart` o
`agenda`; libros centrados en decanos, zodiaco y exaltación activan el mismo dominio aunque no usen
vocabulario de carta natal. `exaltation` queda fijado como `exaltación` y `decan` como `decano` dentro
de ese dominio. El glosario explícito del usuario siempre tiene prioridad. Las entradas efectivas
forman parte de la identidad de reanudación sin registrar su contenido.

La detección tabular reconoce además palabras funcionales inglesas copiadas, morfología residual
inequívoca y rótulos de índice minúsculos que permanecen idénticos. Una sustitución léxica parcial no
se considera una traducción determinista si todavía deja esas señales: vuelve al modelo como una
unidad contextualizada y, si no valida, conserva la celda. Las colecciones HTML de rótulos breves ya
traducidos se evalúan por sus nodos visibles para que el detector global de idioma no rechace una
tabla correcta solo por carecer de párrafos largos. El detector de frases residuales divide también
las tablas XHTML de índice en celdas: un rótulo invariante o ya español no convierte el envoltorio
completo en una falsa alarma, pero una celda inglesa intacta sigue localizándose.

La identidad de caché puede incorporar el contexto jerárquico del capítulo, pero la revisión de su
contrato se selecciona siempre a partir de la carga semántica real. De ese modo, añadir contexto no
puede hacer que una tabla se clasifique como prosa ni ocultar una invalidación específica de tablas;
el hash sigue ligando la respuesta tanto al contenido como a su contexto efectivo.

Antes de fragmentar, `semantic_blocks.reconcile_document_evidence` contrasta el propio documento.
Solo corrige una variante rara cuando otra grafía casi idéntica domina de forma clara, o cuando una
entrada del índice y un encabezado único del cuerpo coinciden con distancia acotada. Código, enlaces,
comentarios y referencias quedan fuera. Esta reconciliación corrige el dato canónico antes de
traducir, no una traducción ya generada, y el registro conserva exclusivamente el número de cambios.
La capa de valores opacos protege además fórmulas, DOI, ISBN y citas numéricas, junto con cifras,
enlaces, términos y símbolos de moneda o porcentaje. Una cantidad como `$50k` y una intensificación
visual como `$$$` conservan exactamente todos sus signos; el detector distingue esos usos de una
fórmula delimitada por dólares. Ollama nunca puede reescribir esos identificadores o símbolos.

El informe bilingüe añade una señal EN→ES deliberadamente estrecha cuando el original contiene
lenguaje inequívocamente fuerte y la salida no conserva ninguna marca de intensidad comparable. Es
un aviso de fidelidad y registro, no una reescritura automática: localiza el bloque para revisión y
mantiene la traducción publicada si el revisor no propone una corrección válida.
El informe cuenta todas las incidencias pero conserva como máximo veinte pares de extractos privados.
Cuando hay más, ese cupo prioriza idioma y alineación, después fidelidad y después residuos o longitud;
los números de segmento continúan cubriendo las incidencias alineadas ocultas. Así el límite de datos
no hace desaparecer de la revisión un riesgo crítico situado al final de un libro.

La cobertura también protege portadas y títulos breves: una respuesta con una expansión relativa
extrema y al menos 80 letras nuevas se considera contenido añadido aunque el original no alcance el
umbral de un párrafo largo. Tras un reintento fallido, una tirada de títulos se divide y cada título
se traduce de forma enfocada; si una unidad sigue sin superar las guardas, se conserva su original en
vez de publicar texto inventado. La prosa no se recompone con traducciones parciales tras fallar por
cobertura: puede degradarse a oraciones independientes, pero el bloque completo solo se acepta si
todas superan las guardas. Un único fallo conserva el bloque original entero para evitar resultados
mixtos difíciles de detectar.
Si ese fallo residual afecta a un solo título, el par Argos directo ya presente puede actuar como
segundo traductor de esa microunidad. La ausencia del paquete, cualquier descarga necesaria, un resto
del idioma fuente o el fallo de una guarda conservan la propuesta anterior; no existe fallback Argos
para párrafos, tablas ni capítulos.
Cuando un título ya traducido contiene una única tirada contigua de al menos dos palabras que el
informe demuestra como residuo del original, la reparación final puede localizar solo esa tirada si
existe una equivalencia establecida y general para el par de idiomas. La secuencia debe aparecer una
sola vez en ambas versiones y no puede ser un nombre propio probable. Los residuos sin equivalencia
validada mantienen el reintento bilingüe convencional o se conservan para revisión; no se acepta una
paráfrasis generativa breve solo porque haya eliminado las palabras fuente ni existe una sustitución
específica por libro.

En títulos y rótulos breves de traducciones inglés↔español, los cardinales inequívocos del dos al
diecinueve conservan además su valor semántico cuando están escritos con palabras. La guarda separa
los vocabularios por idioma, admite una cifra equivalente y formas como `both`/`ambos`, pero rechaza,
por ejemplo, `PART SEVEN` → `PARTE SEIS`; no interpreta el adverbio inglés `once` como el número
español ni bloquea reformulaciones naturales de la prosa. En esos mismos rótulos, una duración
numérica inglesa exige una unidad española con concordancia singular/plural, de modo que `30 DAY`
no pueda publicarse como `30 DÍA`.
Cuando un rótulo residual se vuelve a traducir, esos cardinales se sustituyen por marcadores opacos
con una equivalencia canónica ligada al par de idiomas. El modelo decide la redacción circundante,
pero no puede convertir `SEVEN` en otro valor; el marcador se restaura como `SIETE` antes de validar.
Una serie astrológica inglesa con la forma completa `planeta in signo + número romano` también tiene
una composición EN→ES inequívoca. Si el modelo copia literalmente esa unidad o traduce solo alguno de
sus componentes, la normalización local lleva planeta, preposición y signo a la forma española y
conserva el romano y su envoltura Markdown. La regla exige la serie completa en prosa. Admite un romano
con énfasis separado y un rótulo de colocación sin ordinal solo cuando abre inequívocamente una línea o
página; respeta código, URL, destinos de enlace y usos interiores no secuenciados.

La degradación estructural puede bajar de párrafo a línea y después a oración cuando una subunidad
sigue conteniendo varias listas o citas; cada nivel es transaccional y solo se incorpora si su
reensamblado completo valida. Un marcador de lista añadido a una etiqueta que no era lista se retira
antes de validar, pero nunca se elimina un marcador presente en el origen. Una etiqueta exacta del
léxico establecido conserva su énfasis Markdown y se resuelve sin Ollama. En rótulos breves cargados
de cifras, solo las islas con letras llegan al modelo; números, entidades y separadores permanecen
opacos y se reinsertan literalmente.

La organización estructural usa el índice únicamente como mapa de referencia. Las filas que el
análisis semántico clasifica como índice no se ofrecen como candidatas editables al modelo; solo una
coincidencia posterior en el cuerpo puede convertirse en encabezado. De este modo un índice extenso
no termina publicado como cientos de capítulos.

Los marcadores internos del propio PDF aportan una segunda fuente independiente. Cada entrada solo
se reconcilia si su título, cifras y página encuentran una coincidencia única de una a tres líneas en
la capa extraída. El nivel confirmado viaja junto al encabezado mediante un comentario privado
protegido por las guardas de traducción. La ruta directa y la preparación del editor conservan esos
comentarios hasta obtener el mismo `EpubPlan`; solo entonces `epub_builder` los retira antes de
renderizar XHTML y la superficie pública genera su Markdown sin provenance. Una huella legacy del
texto ya depurado sigue siendo válida únicamente para reabrir borradores anteriores, no para volver a
planificar sin evidencia.

El plan Markdown→EPUB comparte una única jerarquía entre la vista previa, el contenido XHTML y la
navegación del lector. Solo los niveles uno y dos pueden convertirse en el nivel de corte global;
los encabezados más profundos permanecen como secciones enlazables dentro de su capítulo. Los rótulos
explícitos de capítulo o parte siguen siendo límites aunque aparezcan en otro nivel, pero una cabecera
corrida con el mismo texto solo abre el capítulo la primera vez. Un nivel con más de 128 encabezados
no se usa como corte automático: se prueba el nivel superior y los cortes explícitos y por tamaño
siguen disponibles. Así una obra larga puede conservar centenares de secciones navegables sin crear
un XHTML por sección. Si un índice impreso o el outline interno aporta un conjunto suficiente de
coincidencias, el menú incluye esas coincidencias y los límites fuertes; una evidencia demasiado
escasa no oculta la jerarquía superficial. Sin una autoridad fiable se exponen hasta 250 encabezados
de los niveles cercanos al corte. Los demás permanecen visibles en XHTML. Cada destino recibe un
identificador estable generado localmente y los ejemplos dentro de bloques de código quedan fuera.

La profundidad `toc-level-N` del índice generado se conserva para cada título único que reaparece
exactamente en un límite del cuerpo. Esas coincidencias consecutivas pueden anidar incluso una parte
o sección no numerada con un único descendiente; una entrada duplicada, ausente o interrumpida no
extiende la jerarquía. Los rótulos explícitos siguen siendo el fallback conservador cuando no existe
esta evidencia. El borrador cifrado añade a sus encabezados una indicación privada de inclusión o
exclusión para transportar la misma selección por confirmación y editor. La publicación vuelve a
deduplicar cabeceras corridas, deriva los destinos del XHTML ya validado y elimina esas indicaciones
antes de escribir el EPUB. La navegación del EPUB directo y el árbol del editor aplican el mismo
anidado entre archivos; las secciones enlazables permanecen dentro del nodo de su capítulo. La
profundidad puede aumentar sin aumentar el número de documentos del spine ni exponer metadatos
internos.

La extracción PDF tampoco interpreta una frase larga en versalitas como título únicamente por sus
mayúsculas, negrita o centrado cuando usa el tamaño del cuerpo. Ese atajo se limita a rótulos breves;
los títulos largos siguen siendo válidos cuando su tamaño tipográfico aporta evidencia independiente.
Una línea breve en versalitas tampoco se eleva a encabezado si la siguiente comienza en minúscula y
la geometría de columna, tamaño e interlineado demuestra que ambas forman el mismo párrafo.
Los cambios fiables de fuente dentro de una línea se proyectan además como tramos semánticos de
negrita y cursiva. Los tramos se conservan por su texto confirmado, no por índices frágiles, para que
una normalización posterior de notas o espacios no desplace el formato. Si dos tramos iguales se
encuentran en una misma línea o no pueden anclarse de manera única, se conserva el texto sin inventar
énfasis. El checkpoint de página versiona estos tramos y descarta automáticamente una versión previa.

Una tabla nativa refluible y no ambigua tiene prioridad sobre una tabla OCR de página completa: así
se conservan los saltos internos de celda y no se aplana una estructura ya demostrada. La geometría
de los caracteres puede resolver además dos daños acotados sin inferencia semántica: un cero pequeño
y elevado tras uno o dos dígitos se recupera como grado, y una pareja de signos con codificación rota
solo se colapsa cuando sus cajas se superponen como un único glifo. En ambos casos se exige que todas
las apariciones candidatas de la línea compartan la evidencia; la reconstrucción usa el texto ya
reparado en vez de volver a leer los glifos originales. De lo contrario se conserva el texto.

`PhaseReviewSequenceCoordinator` deriva un plan ordenado exclusivamente de los informes y cambios
reales del resultado: OCR, traducción, corrección y estructura. Cada aceptación se guarda como
`APPLIED` antes de completar su fase y abrir la siguiente. Por tanto, cerrar la aplicación entre dos
revisiones no repite OCR, traducción ni IA. Si Windows se interrumpe entre la escritura de la
decisión y la transición de la cola, la reconciliación de arranque termina esa única transición de
forma idempotente. Si una corrección de la aplicación cambia el alcance verificable de una revisión
ya aplicada, la reconciliación vuelve a esa primera fase pendiente, descarta únicamente las
revisiones posteriores dependientes y conserva los intentos y la instantánea del procesamiento.

La presentación expone ese coordinador como una única superficie `Revisión del documento`, con
progreso global y fase actual; no obliga a elegir qué editor abrir. Los controles visibles forman un
recorrido único: elegir original o propuesta, `Siguiente` y la acción final propia de la fase. Un
documento bloqueado queda fuera de la selección automática, pero la cola continúa con el siguiente
trabajo elegible.

### Decisión sobre separar el review gate

Tras extraer el pipeline, la sesión de cola y las instantáneas v2 se reevaluó separar por completo
ejecución y gate. El modelo actual tiene nueve estados de etapa y veinte transiciones permitidas; solo
`BLOCKED_FOR_REVIEW` representa el gate. `ReviewSession` tiene cuatro estados y el coordinador que
reconcilia ambos ocupa 255 líneas con 15 ramas. Hay 24 referencias de producción a cada uno de
`BLOCKED_FOR_REVIEW` y `ReviewStatus`, y ocho pruebas contractuales cubren avance, crash entre
escrituras, pendiente, desfase, reapertura, invalidación dependiente y reinicio desde SQLite.

La alternativa no elimina esas reglas: la ejecución todavía necesitaría `READY` e `INVALIDATED`, y
un `ReviewGate` separado añadiría al menos `NONE/PENDING/APPLIED/DISMISSED`, su persistencia y una
asociación con la etapa. Si el gate vive solo en el repositorio de revisiones, `DocumentJob` y el
scheduler dejarían de poder decidir por sí mismos si un trabajo es ejecutable; si se copia al trabajo,
se conserva la misma duplicación con una migración mayor. Con la suite actual en verde y sin una fuente
de fallos demostrada, no se implementa el rediseño: reduciría un estado de etapa, pero no complejidad
neta. Se reconsiderará solo ante incidencias repetidas de reconciliación o si las revisiones dejan de
estar ligadas a una etapa concreta.

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
microunidad. Todas viajan en una única solicitud JSON que admite como máximo un parche por forma; el
analizador aplica y valida cada parche de manera independiente y descarta cualquier resultado cuyo
texto nuevo no reduzca el recuento de al menos una forma enfocada. Ollama decide si existe un error
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
real del residuo. Un elemento de lista intacto y único puede usar la misma reparación localizada;
su marcador, sangría y estructura de énfasis deben seguir siendo idénticos. Nombres propios,
organizaciones e identificadores web quedan fuera de esta selección. Las direcciones de correo se
protegen como valores opacos en Ollama y Argos y se excluyen del texto natural usado por el detector.
La revisión por parches no puede solapar ni introducir correos o URL, y la validación acumulada exige
que sus valores y cantidades permanezcan idénticos; una reordenación solo es posible fuera del parche
y bajo las guardas estructurales de enlaces. Las secciones
que por naturaleza conservan texto literal —bibliografías, índices y
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
En documentos PDF, el informe conserva todos los tramos delimitados por marcador de página, también
los que no contienen texto natural en uno de los dos lados. Así una portada gráfica, una página vacía
o una adición anómala no desplazan el emparejamiento de las incidencias posteriores.

Cuando dos respuestas bilingües consecutivas no superan las guardas, Parsezen conserva únicamente la
traducción ya validada y nunca persiste la respuesta rechazada como una revisión completada. Una
reanudación puede volver a intentar ese fragmento: así no confunde una preservación defensiva con una
verificación semántica satisfactoria. Los checkpoints de respuestas válidas siguen ligados al mismo
contenido y se eliminan junto con el trabajo privado después de publicar.

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
grupo bilingüe permanece byte por byte fuera de esa reparación. Un salto de línea blando de hasta
tres líneas se recompone como un único título antes de validar solo si las líneas intermedias terminan
en un separador de subtítulo. Las explicaciones, las repeticiones, los párrafos adicionales y una
segunda línea con cierre de frase se rechazan. Si el modelo repite el delimitador exacto que Parsezen creó para esa petición, se retira
únicamente ese eco; cualquier marcador distinto continúa siendo un rechazo duro.

La ruta de producto conserva Argos y añade IA local como alternativas explícitas. La interfaz no
presupone que una sea semánticamente mejor: presenta a Argos como ligera y a IA local como contextual
y dependiente del modelo. Ambas protegen el
glosario y pasan por las mismas guardas compartidas de cobertura, cifras, enlaces, énfasis Markdown,
estructura e idioma. El informe de calidad inicial se calcula sobre la salida del motor y vuelve a calcularse
sobre la propuesta completa después del plan Revisado. La evidencia admite una cita intacta en otro idioma cuando la prosa
que la contiene sí se ha traducido; las variantes incompatibles de un mismo término se señalan para
revisión y no se sustituyen por semejanza.

Los dos informes no son intercambiables: `translation_quality_report` describe exactamente la salida
publicada o la base todavía protegida por la puerta de revisión;
`review_translation_quality_report` describe el candidato que la persona está comparando. Las
recomendaciones y la materialización consultan el segundo, pero un candidato rechazado nunca
reescribe retrospectivamente la evidencia de la base. Ambos se recuperan por separado.

La verificación bilingüe es adaptativa. Revisa todos los bloques con señales deterministas de riesgo,
añade sus vecinos para recuperar contexto y distribuye una muestra de bloques limpios por el resto del
libro. Una incidencia devuelta por el modelo que no pueda anclarse a esa selección activa una pasada
completa en vez de silenciarse. Cada fragmento de traducción recibe además la ruta acotada de
encabezados que lo contiene; esta ruta forma parte de la clave del checkpoint y se usa solo como
contexto, nunca como texto que el modelo pueda publicar.

La cobertura se calcula con la unión real de segmentos para los que la revisión bilingüe general
obtuvo una respuesta JSON válida y protegida por sus guardas, no con el total del documento ni con las
peticiones meramente intentadas. Una respuesta inválida o rechazada conserva el texto anterior y
cuenta cero; las rutas auxiliares de rangos y líneas prioritarias se omiten del contador antes que
sobreafirmar su alcance. Si la selección adaptativa no alcanza todos los bloques, el resultado queda
clasificado como verificación dirigida y conserva el número restante como no revisado
semánticamente. Una desalineación que impide iniciar la pasada informa cero bloques revisados; no se
convierte en una verificación completa por haber solicitado la opción.

En una imagen Markdown interna, la traducción puede cambiar únicamente el texto alternativo visible
y el título opcional. La ruta privada, el número de apariciones y su rol de imagen se comparan por
separado y deben permanecer idénticos; así una descripción traducida no invalida un fragmento seguro
ni puede convertir el recurso en un enlace ordinario.

`runtime_mapping` proyecta `LOCAL_AI_REVIEWED` como `review_content=True` y añade
`review_structure=True` solo para EPUB. Si también hay traducción, la revisión actúa después del
motor elegido como editor bilingüe local y cada diferencia material se materializa para revisión.
Longitud y alineación continúan siendo avisos; no disparan una reescritura automática.

Los reintentos usan instrucciones distintas para residuo de idioma, pérdida de valores protegidos y
alteración de estructura Markdown, incluido su énfasis. El residuo de texto fuente mantiene un único reintento por unidad
alineada y acotada. En PDFs, se intenta primero el párrafo exacto y, si una página mezcla resultado
traducido con frases conservadas, solo esas frases. Una reparación local que supera idioma,
cobertura y estructura se conserva aunque otra página siga necesitando revisión; las guardas finales
verifican que el ensamblado no altere cifras, enlaces, jerarquía o distribución. OCR y estructura
conservan sus recuperaciones específicas.

Si todos los fragmentos traducidos son válidos por separado pero su ensamblado completo incumple una
guarda estructural, Parsezen localiza de forma incremental la combinación incompatible. Conserva el
original únicamente en esos fragmentos, mantiene las traducciones que siguen siendo demostrablemente
seguras y obliga a revisar el residuo, en vez de perder todo el trabajo o publicar una estructura rota.

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
el orden multicolumna ya validado y publica las entradas como una tabla semántica restringida con
etiqueta y folio en celdas independientes. Conserva negrita, cursiva, sangría jerárquica y enlaces
internos de página; los folios quedan alineados a la derecha y no se confunden con decoración vertical
ni todo el índice termina fusionado en un párrafo. El contexto de índice admite folios decimales de
cuatro cifras sin ampliar la detección general de números de margen, que seguiría confundiendo años.
Si la fuente omite los espacios lógicos de una entrada en mayúsculas, la geometría conservada de sus
glifos repone solo los huecos visualmente inequívocos antes de clasificar y renderizar la fila.
Si un folio contiene letras o símbolos opacos, se generan únicamente lecturas numéricas compatibles
con formas habituales de fuentes dañadas. Solo se sustituye cuando la fila OCR local elige un
candidato único o cuando los folios limpios anterior y posterior acotan una única lectura; una sola
fila vecina únicamente permite repetir exactamente su folio.
Una línea preliminar formada únicamente por cuatro glifos numéricos separados y deformados puede
restaurarse como año de publicación, pero solo si el OCR local de esa página o una mención explícita
de publicación/copyright en los preliminares aporta un único año entre 1800 y 2199. Con cero o varios
candidatos, la capa nativa permanece intacta y se revisa.
Después de asociar la fila, el OCR puede restaurar fronteras entre palabras aunque contenga hasta dos
glifos distintos, pero nunca aporta ni reemplaza letras: la secuencia nativa permanece intacta.
Antes de traducir, el texto de cada entrada queda separado de su folio para impedir que el traductor
o la revisión posterior cambien su función o su posición. Un rótulo de sección sin folio se serializa
como un bloque independiente: nunca se convierte en una continuación perezosa de la entrada anterior
al interpretarse como CommonMark.
Los marcadores de
página se conservan durante todo el procesamiento y solo se retiran al publicar un resultado que no
necesita revisión. Cuando una palabra termina con guion al final de una página y continúa en
minúscula al principio de la siguiente, la unión se representa alrededor del marcador interno para
que la palabra publicada quede completa sin perder trazabilidad.

Una cifra pegada a una palabra o signo se presenta como llamada de nota en superíndice únicamente
cuando la misma página contiene una definición numerada, pequeña y situada en la banda inferior. Las
definiciones confirmadas recuperan su separador de lista; sin ambas evidencias, cifras y prosa quedan
intactas. Las guardas de traducción normalizan los dígitos en superíndice solo para contabilizarlos y
rechazan su pérdida igual que la de cualquier otra cifra visible.

Un cero final pequeño y elevado delante del nombre inequívoco de un signo zodiacal puede restaurarse
como símbolo de grado, porque su geometría demuestra el signo pero no el valor. Un grado de 30 a 99
dentro de un signo es físicamente imposible y activa el contraste local; Parsezen no transforma, por
ejemplo, `75°` en `15°` por mera plausibilidad. Si las lecturas independientes no resuelven la cifra y
la capa nativa sigue siendo útil, el OCR de esa página queda fuera del renderizado estructural. Aún
puede orientar conciliación, conservación de imágenes e informe de calidad, pero no originar texto,
listas o rótulos visibles. La página conserva su lámina y una incidencia de revisión.

En series astrológicas explícitas, ciertos mapas de fuente pueden exponer el romano visual `II`/`III`
como `n`, `it`, `in` o `ui`, y `II:` como `IE`. Parsezen no traduce esos glifos por parecido: exige un
rótulo centrado con estilo de encabezado y la evidencia estructural del mismo signo. Dos hermanos
próximos pueden confirmar una colocación, o los títulos I/II/III del decano delimitan el valor de sus
rótulos planetarios. Un título `IE` solo se repara si los otros dos miembros dejan exactamente un
ordinal ausente; sin ese consenso, el texto nativo permanece intacto.

Las enumeraciones nativas solo se convierten en listas ordenadas cuando una página contiene al menos
tres ordinales consecutivos que comienzan en uno y mantienen marcador, sangría, tamaño y proximidad
visual. Las líneas siguientes se incorporan al mismo ítem únicamente con una sangría colgante
explícita; los años, párrafos numerados aislados, índices y secciones distantes permanecen como texto.
Si el espaciado tipográfico fragmenta el rótulo anterior a dos puntos, sus fragmentos se recomponen
solo cuando la palabra completa aparece también como evidencia nativa en esa página. Una cursiva o
negrita común a varias líneas se mantiene como un único tramo para no crear marcadores Markdown en
medio de una palabra partida.

Los folios nativos aislados se retiran tanto del margen superior como del inferior. La detección
superior acepta además una banda exterior más profunda para numeraciones alternas de páginas pares
e impares y encabezados no centrados donde el folio aparece unido a una etiqueta de capítulo, pero
no números centrados que puedan identificar una sección. El folio puede preceder la etiqueta o
seguir a una etiqueta breve en el extremo exterior; el tamaño de fuente y la extensión horizontal
evitan confundirlo con un rótulo de figura dentro de la columna de texto. Si una página gráfica
necesita OCR, los números o encabezados deben estar detectados también como texto nativo en una de
esas zonas antes de retirarlos de las primeras o últimas líneas reconocidas. La condición combinada
evita eliminar una numeración de contenido que no esté respaldada por la geometría del PDF.
Un folio alfanumérico dañado en la banda exterior superior solo se omite si todos sus caracteres
alfabéticos son formas inequívocamente confundibles con `0` o `1`. Normalmente dos folios decimales de
otras páginas deben confirmar el mismo desfase entre página PDF y numeración impresa. En un rango
aislado se admite únicamente si además es pequeño, está en el rail exterior y su lectura queda a no
más de 64 posiciones de la página PDF; un rótulo centrado o un código distante permanece visible.

Las imágenes incrustadas se extraen directamente. Además, grupos acotados de al menos dos curvas
próximas se renderizan como ilustraciones cuando representan una entidad gráfica y se deduplican
frente a imágenes solapadas; dos reglas finas aisladas no bastan. La caja se amplía únicamente con
etiquetas compactas próximas para no recortar escalas ni ejes. Cada recurso conserva internamente su
caja: se inserta antes del primer texto que sigue a la imagen o inmediatamente antes de un pie
numerado solapado horizontalmente. En bandas con columnas, una continuación inequívoca del párrafo
exterior precede al pie lateral; los guiones nunca unen columnas distintas. Los enlaces cuyo destino
no puede anclarse a texto visible se reúnen en `Destinos conservados` en vez de convertirse en
etiquetas huérfanas. La versión del checkpoint de página nativa forma parte de su clave para no
reutilizar extracciones anteriores incompatibles.
Al publicar EPUB, un enlace interno cuyo destino quedó fuera del intervalo elegido conserva su
etiqueta como texto no interactivo. La misma degradación segura se aplica si el usuario elimina el
ancla en el editor: la validación final sigue rechazando cualquier enlace roto que sobreviviese.

Las tablas nativas se extraen con sus celdas y geometría antes de reconstruir las líneas que las
rodean. Una tabla simple usa sintaxis Markdown; una tabla más ancha o con celdas multilínea usa HTML
semántico. Los casos que exceden los límites seguros se degradan a filas etiquetadas y generan una
incidencia de revisión. Si la tabla procede de un escaneo y existe un recorte exacto, ese recorte
sustituye a las filas cuya asociación no puede demostrarse; la prosa exterior sigue siendo refluible.
Las líneas dentro de la caja de la tabla no se publican por duplicado y el resto de leyendas o notas
conserva su posición. Dentro de una celda PDF, una línea en minúscula o que continúa de forma
inequívoca una preposición, conjunción o puntuación abierta se une con un espacio antes de crear HTML.
Una frase cerrada, una lista o un rótulo independiente conserva el salto. Así el EPUB no hereda
envolturas visuales de columna como si fueran decisiones editoriales, sin aplanar separaciones
demostrables.

Una cuadrícula nativa de cuatro o más columnas no se considera semánticamente segura cuando contiene
al menos dos supuestas filas de continuación a las que les faltan dos o más celdas. Esa forma suele
indicar que el extractor confundió líneas envueltas dentro de una misma celda con registros nuevos.
En un PDF digital se publica como texto estructurado explícitamente revisable. En un escaneo se
publica solo el recorte de la tabla y una incidencia, nunca una cuadrícula textual que aparente ser
fiable.

Al construir un EPUB, ese HTML tabular pasa por un analizador XML local que exige exactamente
`table/thead/tbody/tr/th/td/br`. Las tablas documentales ordinarias no admiten atributos; el índice
solo admite las clases exactas generadas por Parsezen, `strong`/`em` y enlaces locales `#page-N`.
Las filas deben ser rectangulares y el texto estar escapado. Los bloques que cumplen el contrato se
insertan como XHTML semántico después de CommonMark; todo el demás HTML permanece desactivado. Así las
celdas multilínea no aparecen como etiquetas visibles y la excepción no abre una vía para scripts,
eventos o marcado documental arbitrario.

La guarda compartida de traducción conserva en orden `table/thead/tbody/tr/th/td/br`, incluidos los
pares que representan celdas vacías. Esta comprobación es local a la estructura y se aplica también
al reutilizar una revisión guardada; por ello dos cambios opuestos en tablas distintas no pueden
ocultar una pérdida mediante un simple recuento global.

Antes de cada borrador traducido, revisado o reparado, las tablas HTML generadas se reconcilian con
su versión anterior validada. Si el modelo cambia atributos, clases o destinos de enlaces pero
conserva exactamente la misma secuencia de nodos, solo se trasladan los textos alineados sobre el
armazón XHTML original. Si también cambia esa secuencia, se conserva la tabla anterior completa. Esta
degradación impide que una revisión tardía convierta un índice válido en HTML visible o bloquee la
publicación del libro, sin aceptar marcado inventado ni ocultar el caso mediante una reparación
estructural libre.

La misma guarda compara en orden todas las etiquetas HTML visibles del resto del fragmento. Un
envoltorio como `html`, una etiqueta eliminada o cualquier marcado reserializado por el modelo
invalida la propuesta completa; el texto literal de esas etiquetas nunca puede llegar al lector.

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

Las tablas abiertas de un escaneo ABBYY siguen una vía más estricta. Solo se consideran si una imagen
de página completa contiene dos o tres reglas horizontales alineadas de al menos el 58 % del ancho,
una región acotada y suficientes relaciones laterales. Con dos reglas, los comienzos de fila deben ser
rótulos en negrita o mayúsculas y cada fila debe conservar sus dos celdas. El inicio continúa anclado
en la primera columna, pero el final se calcula con el último renglón de toda la anchura para no mover
una continuación larga de la columna derecha a la fila siguiente. Con tres reglas, los límites de una
matriz dispersa se derivan de huecos repetidos en dos líneas de cabecera. Los huecos que no contienen
texto solo reciben un guion si el raster demuestra dentro de esa celda un trazo horizontal corto; una
cabecera numérica recupera un signo de grado dañado únicamente cuando otras columnas establecen el
mismo patrón. Las filas se separan solo en espacios verticales claramente mayores que el interlineado.
El resultado se acepta únicamente si `pdfplumber` devuelve una cuadrícula rectangular y el
multiconjunto completo de caracteres nativos coincide exactamente con la región original antes de
añadir esas evidencias visuales. Prosa a dos columnas, índices, enlaces, reglas próximas o
desalineadas y cabeceras ambiguas se rechazan.

`_PdfTable.inferred_from_raster` conserva la procedencia de esa decisión en el checkpoint de página
versión 13. La publicación genera XHTML semántico, añade un recorte visual de la caja de la tabla y
crea una incidencia no bloqueante para revisar asociaciones. El OCR independiente continúa siendo
solo evidencia: si altera una cifra, una forma fila×columna o la cobertura, no sustituye la capa nativa.

Una tabla OCR solo sustituye la capa nativa si mantiene una proporción acotada, suficiente
solapamiento léxico, exactamente las mismas cifras sin folios y la secuencia completa de formas
fila×columna. Una salida inflada, incompleta, numéricamente distinta o que omite una columna vacía
se rechaza; la capa de texto útil permanece y conserva el orden multicolumna nativo. Si el
reconocedor une un rótulo
multilingüe `Tabla n` con la primera fila, la limpieza lo separa en su propio párrafo antes de
validar la forma Markdown; el rótulo se conserva y la tabla puede publicarse como XHTML semántico.
Si una tabla escaneada conserva la forma pero OCR y capa nativa discrepan en diacríticos, siglas o una
mayúscula interna anómala, el reemplazo completo queda prohibido y el árbitro visual puede resolver
solo átomos acotados. Mientras quede una discrepancia, el recorte exacto de esa tabla es la autoridad
publicada y su cuadrícula textual se omite con una incidencia explícita; la prosa exterior permanece
refluible. Una tabla raster de al menos veinte filas y cuatro columnas usa también este respaldo
conservador aunque la capa nativa parezca rectangular: en esa densidad una única celda truncada no
puede demostrarse de forma económica sin comparar visualmente cada registro.

La publicación Markdown parte siempre del texto canónico ya transformado. `markdown_export.py`
deriva después el archivo único o el índice con capítulos, metadatos y referencias de página, sin
volver a invocar OCR, Argos u Ollama. `output.py` publica conjuntamente índice, carpeta de capítulos
y recursos y vuelve a generar el conjunto cuando se acepta una revisión.

La extracción PDF recorre el rango en shards de 32 páginas y libera los objetos transitorios de
`pdfplumber` entre shards; cada modelo nativo se guarda además como checkpoint independiente antes
de continuar. La reconstrucción conserva el modelo compacto necesario, mientras que las imágenes
que cruzan la frontera hacia la preparación se derraman a un directorio temporal privado y se leen
solo al materializar la salida. Los checkpoints generales comprimen el payload antes de la
protección, pero siguen aceptando entradas históricas sin compresión.

El plan OCR añade una puntuación de legibilidad basada en densidad alfabética, fragmentación,
glifos sospechosos, texto espaciado y líneas rotadas. Una página de confianza baja se rasteriza con
la estrategia completa; al terminar, el texto OCR sustituye al nativo solo si supera umbrales
absolutos y mejora suficientemente su puntuación. Tablas y páginas sin texto tienen reglas
específicas. Un único carácter de sustitución o una pareja de puntuación nativa imposible como `·.`
basta para activar el contraste local; si las lecturas no lo resuelven, la página queda señalada para
revisión en vez de tratar ese glifo como texto fiable. Los tokens mixtos solo cuentan como posible
número roto cuando todas sus letras tienen
una forma numérica compatible; se excluyen URLs literales, cantidades con `$` prefijado e
identificadores ordinarios como `3D` o `H2O`. Un análisis correcto que no obtiene texto se guarda como checkpoint vacío para evitar
repetir OCR costoso al reanudar; una excepción del motor no se guarda como ausencia de texto. El
informe conserva las páginas analizadas, sustituidas y todavía dudosas. Las respuestas parciales
mantienen las páginas cacheadas. Un worker reutiliza su modelo para hasta ocho páginas solicitadas,
pero Docling sigue recibiendo rangos internos de una o dos páginas y libera cada resultado antes del
siguiente. Si el proceso falla, las páginas ya transmitidas no se repiten y las pendientes se
reintentan con un proceso nuevo; un grupo todavía problemático se degrada a páginas individuales y
las páginas opcionales que siguen fallando se marcan como poison pages. Las páginas requeridas
pendientes continúan bloqueando la conversión; ambas clases quedan separadas en el informe sin
introducir texto documental en el historial.

Una muestra proporcional de hasta el 25 % del intervalo y un máximo de seis páginas con imagen
completa y capa textual útil también pasa por OCR cuando contiene índices, tablas, fórmulas, muchas
cifras o codificaciones sospechosas. El OCR no
sustituye por ello la capa nativa: actúa como evidencia de auditoría. Solo esas páginas inciertas se
leen además con PDFium como segunda implementación nativa; una grafía OCR se acepta directamente
cuando PDFium coincide exactamente y supera las mismas guardas conservadoras.

Antes del OCR, una fuente nativa con daño repetido puede activar una reparación sistémica cuando al
menos ocho líneas admiten el mismo contraste seguro. PDFium lee entonces solo la caja geométrica de
cada línea. La sustitución exige conservar cifras, una similitud alta y pocos tokens cambiados; los
caracteres corroborados se transfieren sobre los separadores nativos para no introducir espacios ni
cambiar puntuación. Una unión de palabra solo se acepta si la forma unida se repite al menos dos veces
como evidencia independiente.

Si las dos capas siguen discrepando, un presupuesto de incertidumbre elige como máximo ocho recortes
por documento y dos por página, o cuatro cuando la página contiene una tabla inferida que ya exige
revisión. Prioriza glifos corruptos, fórmulas, ligaduras, diacríticos y mayúsculas internas anómalas.
Un folio numérico deformado o un signo `$` que no representa dinero recibe prioridad explícita aunque
el tokenizador visual lo separe de sus vecinos; una diferencia ortográfica genérica de baja prioridad
no convierte por sí sola una línea refluible en imagen.
Cuando el OCR ha entrelazado columnas, una reconciliación por tokens puede formar una segunda lectura
de la misma longitud sin cambiar el resto de la línea. El árbitro se descubre en Ollama local mediante
`/api/tags` y `/api/show`, excluye modelos cloud y los que exceden 6 GiB, y recibe solo el recorte y las
dos lecturas candidatas. Nunca ve una página ni un documento completos. Su propuesta se acepta
únicamente si conserva todos los átomos no disputados, no inventa cifras y queda a distancia mínima
de las evidencias; una tercera grafía solo puede diferir en una edición de ambas. Una lectura aceptada
actualiza también una celda estructurada cuando la correspondencia es única. Si no existe un modelo
visual estándar, responde de forma ambigua o falla, la conversión no publica la lectura discutida
como si estuviera probada. Conserva un recorte exacto de cada línea todavía ambigua, lo inserta según
su geometría y mantiene refluible el resto de la página. Si una página supera ocho líneas dudosas,
la lámina completa pasa a ser la autoridad visual. Estos respaldos suprimen adiciones OCR de la misma
región y generan una incidencia explícita; las tablas siguen su vía localizada independiente.

Antes del árbitro visual, el consenso intradocumental puede aceptar exactamente un átomo alfabético
de esa segunda lectura. La forma OCR debe estar a una edición de la nativa, aparecer también en otra
línea nativa del intervalo convertido, existir en ambas fuentes y superar de manera única el soporte
de cualquier vecino a una edición. La regla excluye cifras, cambios solo de caja y candidatos con
mayúsculas internas; la única forma corta admitida puede retirar un carácter minúsculo prefijado a
una palabra con mayúscula inicial. Un diacrítico solo cambia si la forma candidata conserva un
diacrítico explícito, por lo que la frecuencia documental nunca convierte una transliteración
acentuada en ASCII. La propuesta conserva el resto de la línea y actualiza una celda estructurada
solo si el átomo anterior aparece exactamente una vez en toda la tabla. El informe de calidad expone
únicamente el número de regiones aceptadas, sin palabras ni contexto documental.

Los tokens numéricos con mezclas inequívocas de letras y cifras tienen una guarda adicional. Solo se
reemplazan cuando la línea OCR alineada contiene exactamente uno de los valores numéricos compatibles;
si la alineación, la cifra o la sustitución no son únicas, la línea permanece sin reescritura y entra
en el respaldo visual anterior. El contador se agrega al mismo informe de consenso y nunca registra
el token ni su contexto.

Los canarios PDF sintéticos cubren índices con folios, columnas, énfasis y fórmulas. Una prueba
metamórfica exige que ejecutar la misma conversión con y sin checkpoints produzca Markdown idéntico,
lo que detecta divergencias de reanudación sin guardar contenido real de usuario.

Cada bloque Markdown conserva una provenance interna con sus páginas de origen. La unión de un
párrafo entre páginas solo se permite cuando la primera línea termina cerca del margen inferior,
la siguiente empieza cerca del margen superior en minúscula, mantiene columna y tamaño tipográfico
y no hay una frontera de frase; encabezados, listas y tablas no se reconcilian por esta vía.

En páginas gráficas completas, el reconocimiento puede incluir letras decorativas o marcas diminutas
aisladas. Antes de limpiar el Markdown se descartan solo las líneas de una a cuatro letras cuya altura
y anchura sean muy inferiores a la mediana de las celdas OCR de esa página. Esta regla no se aplica a
etiquetas cortas de escala homogénea y nunca elimina la imagen original. La versión del checkpoint OCR
forma parte de su cabecera para que una regla nueva no reutilice texto ruidoso anterior. Una cabecera
etiquetada de otra versión invalida la entrada y fuerza el reconocimiento: nunca se interpreta como
texto heredado del documento. Solo se mantiene compatibilidad con checkpoints antiguos sin cabecera,
anteriores al versionado explícito.

Una imagen que ocupa la página completa suele ser un escaneo y no se duplica si la capa OCR ofrece
una representación textual suficiente. Hay tres excepciones verificables: si la imagen está colocada
con una orientación distinta de su bitmap, si el OCR reconoce una tabla o si la capa nativa contiene
un rótulo inequívoco de figura numerada, se conserva además la lámina completa. El rótulo exige una
puntuación propia, una forma compacta sin más texto o una lectura ABBYY breve en mayúsculas y negrita;
una mención ordinaria como `Figure 62 shows…` no basta y los índices siguen excluidos. La capa textual
sigue siendo accesible y traducible, mientras que formularios, rejillas, diagramas, campos vacíos y
relaciones espaciales que el Markdown no puede expresar permanecen en la salida como referencia fiel.

La última página es una cuarta excepción acotada cuando contiene una imagen de página completa y su
capa nativa no alcanza el mínimo de texto útil. Se conserva la lámina aunque el OCR produzca muchas
letras, porque una contraportada visual no puede quedar reemplazada solo por una lectura aparentemente
densa. Ese recurso queda marcado como autoridad visual y el OCR no se publica junto a él. La misma
autoridad se aplica a una portada sin letras nativas útiles; una portada con título nativo permanece
refluible. Los índices detectados siguen excluidos y el filtro de imágenes casi vacías continúa activo.

Si la lámina exigida por un rótulo de figura no puede renderizarse, la página genera una incidencia de
revisión. El fallo no queda reducido al contador agregado de imágenes omitidas.

También se conserva como lámina, sin publicar su capa textual fragmentada, una composición gráfica
de página completa cuya supuesta lectura consiste en muchas etiquetas cortas dispersas, pocas frases
y una geometría no lineal. Un mosaico exige al menos dieciséis rótulos, mayoría de líneas breves y de
tres palabras o menos, casi ninguna frase y señales gráficas en cifras o mayúsculas. Una página de
prosa, un índice o una tabla recuperable quedan excluidos. Si una imagen está girada y la limpieza ya
retiró una pila vertical, el OCR de esa región no se vuelve a añadir como texto independiente.

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
El índice impreso puede demostrar niveles adicionales sin depender de esa inferencia. Tanto la
navegación directa como `create_book_from_markdown` conservan palabras, identificadores y orden,
aplican esa misma relación demostrada y calculan el spine en preorden.

La publicación genera EPUB 3 con:

- `mimetype` sin compresión y en primera posición;
- paquete OPF;
- navegación anidada;
- XHTML validado;
- imágenes y portada;
- supresión de las imágenes extraídas de la primera página cuando esa página ya se materializa como
  portada, para que el mismo contenido visual no reaparezca en el orden de lectura;
- semántica `epub:type="cover"` en la página de portada y referencia OPF `guide` equivalente para
  que lectores EPUB 3 y motores heredados reconozcan la misma página sin duplicarla;
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

La traducción directa conserva inicialmente el paquete y además prepara una representación editable
ligada a sus documentos reales del spine. EPUB→EPUB sin otra operación también crea este resultado
pendiente de personalización.
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
La publicación tras la confirmación valida siempre el paquete completo. Conserva el original cuando
no hay cambios, aplica parches de cuerpo cuando esa es la única diferencia y reconstruye el libro
normalizado cuando cualquier cambio afecta a la estructura del paquete, los metadatos, la portada o
los recursos.

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

Los nuevos `job_events` usan un payload cerrado y versionado. Las transiciones incluyen fase, estado
anterior y nuevo, revisión de configuración, identificador opaco del intento cuando está disponible y
código técnico de error; los cambios de puntero de revisión incluyen la generación del snapshot. El
coordinador de persistencia acumula las transiciones durante el throttling y las escribe en la misma
transacción que la proyección de cola. Los marcadores legacy siguen siendo legibles por compatibilidad,
pero no forman parte del stream tipado. Ningún evento admite mensajes, contenido, prompts o rutas.

El contenido documental no se guarda en SQLite. `ArtifactStore` escribe artefactos inmutables,
protegidos con DPAPI para la cuenta actual de Windows y acompañados de SHA-256 dentro del sobre
cifrado. Checkpoints EPUB, checkpoints generales y artefactos usan la misma primitiva
`infrastructure.user_data_protection`; la escritura temporal, `flush`, `fsync` y reemplazo atómico
comparten únicamente el mecanismo pequeño de `infrastructure.protected_file`. Los esquemas,
límites, nombres y políticas de retención siguen perteneciendo a cada almacén.

Antes de que una cola avance desde un documento que necesita revisión, `ResultSnapshotStore`
persiste:

- resultado y rutas;
- informes OCR y de traducción, incluida la cobertura lingüística sin contenido;
- propuesta de corrección;
- recursos;
- metadatos EPUB;
- telemetría por etapa y conteos semánticos sin contenido.

`ProcessTelemetry` agrega duración y número de visitas por `ProcessStage`; no almacena nombres,
rutas, prompts, términos ni texto. Las llamadas locales se agregan además por operación estable
(`translation_batch`, reparación, glosario, revisión, estructura u OCR visual) con solicitudes,
tokens y duración, de modo que un informe pueda localizar amplificación de llamadas sin observar el
documento. La validación real usa el mismo resultado para informar tiempos, páginas OCR sustituidas o
dudosas y conteos de preliminares, índice y memoria terminológica. Su informe privado se reemplaza
atómicamente después de cada caso, por lo que una interrupción larga no obliga a reconstruir ni
reprocesar las métricas de los documentos ya publicados.

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

El modelo de amenazas detallado y el inventario de metadatos en claro se mantienen en
[`SECURITY.md`](../SECURITY.md). La frontera actual es la cuenta de Windows: el contenido intermedio
se protege con DPAPI, mientras SQLite, configuración e historial conservan metadatos recuperables bajo
los permisos del perfil. Un atacante que ya actúa como ese mismo usuario queda fuera de alcance; no se
añade cifrado de SQLite sin diseñar primero índices, migración y recuperación.

- El origen es inmutable: al añadirlo se conservan rápidamente tamaño y fecha. La preparación fuera
  del event loop captura un `SourceIdentity` con tamaño, fecha y SHA-256, rechaza cambios durante la
  lectura y persiste el digest en el trabajo. Ese mismo digest verificado identifica los checkpoints
  PDF, EPUB y de transformación sin releer el archivo para cada caché.
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

En el streaming de Ollama, el timeout de lectura de `httpx` limita la inactividad entre datos; no es
un reloj total de la respuesta. El transporte admite además un límite total explícito e independiente
para consumidores que lo necesiten. Al no configurarlo, una generación activa puede durar más que el
timeout de lectura sin ser tratada como bloqueada; `num_predict` y el límite de salida siguen acotando
su tamaño.

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

El subcomando `profile` añade observación sin crear una referencia: tiempo total y por página, pico
RSS incremental del árbol de procesos, páginas y tiempo OCR, y cantidad/tamaño de recursos cuando se
activa `--include-images`. `profile --matrix-pages 100 500 1000` mide prefijos de documentos largos
con el mismo alcance de memoria `process-tree`; si el PDF tiene menos páginas, el rango se rechaza.
`scripts/benchmark_runtime.py PERFIL INSTALACIÓN` completa la matriz con un
payload sintético: tiempos DPAPI, escritura y recuperación de snapshot v2, disco privado/temporal,
arranque en frío de una ventana offscreen y tamaño de la instalación; `--installer` añade el tamaño
del instalador construido. El perfil resultante contiene solo números, no rutas ni payloads. Para que
el tamaño instalado sea representativo se debe pasar la carpeta del paquete construido, no `src/`.

La comparación optativa de traducción EN→ES vive en
`scripts/evaluate_translation_models.py`. Solo acepta tags que ya estén anunciados por el Ollama
local, usa un corpus sintético EN→ES versionado y llama a la misma entrada `improve_markdown` que la
traducción de producción, con un contexto y opciones compartidos entre modelos y repeticiones. Su
informe JSON atómico contiene únicamente IDs de caso opacos, hashes y métricas agregadas de gates,
cobertura, residuo, números, glosario, coincidencia exacta cuando procede y telemetría disponible;
no escribe documentos ni guarda prompts, respuestas, rutas o contenido del corpus.

Estas métricas son instrumentación, no una autorización automática para optimizar. Streaming global,
paralelismo de documentos, `mmap`, persistencia delta y pooling SQLite solo se considerarán contra una
referencia repetible que muestre un cuello de botella material.

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
