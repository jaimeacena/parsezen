# Historial de cambios

## Próxima versión

- EPUB→EPUB conserva ahora texto visible situado junto a `code`, fórmulas, SVG u otros nodos
  protegidos sin enviar ese marcado al traductor. La selección cubre también texto XHTML seguro que
  no estaba dentro de las etiquetas de bloque habituales.
- La calidad de la traducción publicada y la del candidato de revisión quedan separadas. La
  verificación bilingüe revisa todas las regiones de riesgo, sus vecinas y una muestra distribuida;
  si una incidencia no se puede anclar, amplía la comprobación al documento completo. Los fragmentos
  reciben además el contexto acotado de sus encabezados.
- La publicación EPUB directa es transaccional. Una revisión sin cambios conserva el paquete exacto
  y una edición limitada al cuerpo de capítulos mantiene byte por byte OPF, navegación, CSS, fuentes,
  imágenes y el resto de recursos; cambios estructurales vuelven al constructor normalizado.
- La comprobación temprana convierte todas las páginas representativas, pero prueba traducción en un
  máximo de tres y omite revisión, reestructuración y construcción EPUB finales. Ollama y OCR añaden
  deadlines totales acotados y métricas privadas de tiempo, tokens y rendimiento sin contenido.
- La revisión documental elimina contexto y botones redundantes: resume el progreso como `fase ·
  resueltas/total`, muestra prioridad o sugerencia solo cuando aportan información, agrupa localizar y
  restaurar en el menú de cada panel, oculta `Anterior` sin destino y reserva la acción masiva para
  fases con varias correcciones. El editor EPUB conserva sin cambios su flujo y sus dos paneles.
- La interfaz reduce cabeceras y ayudas repetidas, reúne ajustes, actividad y apariencia en un único
  acceso global y añade una composición intermedia que evita recortes. La entrada vacía se ancla bajo
  la cabecera y usa un botón convencional de selección local; con uno o varios documentos la cola
  ajusta su altura a las filas y reúne sobre el mismo rail `Añadir` y la acción principal del lote. El
  rail centra también la cabecera y los títulos internos; `Estado` sustituye a `Siguiente paso`, las
  columnas priorizan el documento y los avisos crecen con su texto y desaparecen al retirar el trabajo
  que los originó. El menú global prioriza Actividad y solo señala incidencias accionables. Desaparecen
  el contador y el procesamiento de la cabecera global. El editor EPUB mantiene sus dos paneles y
  todas sus herramientas, agrupando las secundarias cuando falta ancho en vez de usar desplazamiento
  horizontal.
- Los benchmarks locales miden ahora tiempo por página, OCR y recursos PDF, y añaden un perfil
  sintético para DPAPI, snapshots, recuperación, arranque en frío, disco temporal y tamaños de
  instalación/instalador. No se aplica ninguna optimización sin una referencia medida.
- La evaluación del review gate conserva el modelo actual: separar ejecución y revisión eliminaría
  un estado de etapa, pero añadiría otra proyección durable y dependencia del scheduler sin reducir
  las reglas de recuperación. La decisión y sus métricas quedan documentadas.
- Se documenta el modelo de amenazas local: los payloads documentales recuperables usan DPAPI, los
  metadatos operativos de SQLite, configuración e historial dependen del perfil de Windows, y un
  atacante que ya opera como el mismo usuario queda fuera de alcance. También se inventariaron los
  datos en claro y sus vías de limpieza antes de plantear cualquier migración criptográfica.
- El streaming de Ollama deja de reutilizar el timeout de lectura como duración total accidental.
  Las generaciones activas separan el timeout de inactividad de `httpx` de un deadline total
  derivado y acotado; cada consumidor puede imponer uno más estricto.
- OCR y Argos comparten ahora las primitivas mecánicas de su canal privado autenticado, proceso
  oculto, espera cancelable y mensajes JSON acotados. Sus protocolos, límites y máquinas de estado
  siguen separados.
- `process_document()` conserva su fachada, mientras sus contratos inmutables pasan a
  `pipeline/contracts.py` y la preparación a `pipeline/prepare.py`; los consumidores internos dejan
  de importar el orquestador solo por tipos. `pipeline/publish.py` concentra build, integridad y
  escritura atómica sin conocer los algoritmos de traducción o IA.
- `pipeline/transform.py` concentra traducción, corrección, verificación bilingüe, calidad y
  borradores de revisión sin publicar archivos. La fachada coordina ahora explícitamente validación,
  preparación, transformación y publicación.
- Las instantáneas de revisión nuevas usan un manifest v2 mínimo y generaciones cifradas: cada
  texto lógico se almacena una sola vez, los recursos se referencian por id y la telemetría no se
  conserva. La lectura v1 sigue disponible y el arranque elimina solo generaciones huérfanas
  verificables.
- La cola y sus informes se proyectan por eventos. El polling general de 200 ms se sustituye por un
  refresco de un segundo limitado a tiempo restante y reintento de persistencia diferida.
- La nueva `QueueSession` Qt-free posee el plan preparado, runtimes, documento activo, pausa y cierre
  del lote. La ventana principal deja de duplicar las banderas de su máquina de estados.
- El flujo de IA local posee ahora su estado de presentación y expone operaciones explícitas. La
  ventana deja de duplicar estado y elimina el proxy dinámico basado en `__getattr__` y `Any`.
- Una tabla exhaustiva proyecta cada etapa física a fase de dominio, fase de actividad y etiqueta de
  diagnóstico. Los logs distinguen trabajo físico (`processing_*`) del intento Qt
  (`processing_attempt_*`) para eliminar eventos terminales conceptualmente duplicados.
- Checkpoints y artefactos comparten primitivas pequeñas para DPAPI y reemplazo atómico sin cambiar
  sus formatos ni políticas. El SHA-256 del original se captura una vez durante la preparación en
  segundo plano y se reutiliza en validación y caches, evitando bloquear la interfaz al añadirlo.
- Pausar usa ahora el motivo terminal explícito del ejecutor físico: worker, cronología y dominio
  terminan en `PAUSED`, se conservan checkpoints y no aparece una cancelación falsa en Actividad.
- Traducción y revisión dejan una cobertura lingüística explícita: distingue corrección integrada,
  verificación bilingüe independiente y revisión dirigida, y muestra bloques comprobados, revisados,
  no revisados e incidencias pendientes sin persistir contenido documental.
- La revisión estructural planifica el esquema con un inventario global de índice, páginas,
  geometría, encabezados y roles semánticos. Ollama solo devuelve directivas de nivel; la revisión
  muestra los árboles actual y propuesto y Parsezen conserva literalmente el texto.
- La configuración del traductor explica dinámicamente el recorrido real, las pasadas, la
  independencia de la verificación y el coste cualitativo según motor, plan y formato.
- Los documentos nuevos quedan ligados a un SHA-256 local. La preparación rechaza cualquier cambio
  del original, incluso si conserva tamaño y fecha, y PDF/EPUB reutilizan ese digest entre checkpoints
  para evitar lecturas completas redundantes.
- La IA exige la configuración persistente de Ollama sin nube; el entorno del proceso cliente ya no
  acredita un servidor previamente iniciado. Las releases validan etiqueta y versión, usan permisos
  mínimos por job y la auditoría falla ante dependencias omitidas sin justificación canónica.
- La configuración pasa a ser una página compacta dentro de la ventana principal. El formato recupera
  dos tarjetas visuales para Markdown y EPUB; la revisión con IA usa un único interruptor. Traducir,
  Traductor, Glosario, Páginas y OCR conservan el patrón plano `Etiqueta — Valor — ›`, sin controles
  segmentados, encabezados ni scroll en el tamaño normal. Traductor y Glosario solo aparecen al
  traducir; el rango PDF se pide en un diálogo y después se muestra como `25–140`.
- La página de configuración queda centrada en el espacio disponible y cada menú se abre alineado con
  el valor y el chevron de la fila que lo activa, también tras redimensionar la ventana.
- Cada elección válida se guarda inmediatamente. Desaparecen Cancelar y Crear/Guardar del pie;
  Volver y Escape solo cierran la página. La IA ausente abre su gestor desde la elección que la
  necesita y el glosario conserva su editor compacto independiente.

## 1.2.0 - 2026-08-12

- Configurar deja de sustituir la cola por un formulario largo: una ventana compacta pide únicamente
  Markdown/EPUB y traducción. Procesamiento directo, Argos y OCR automático son valores implícitos;
  revisión completa, IA para traducir, glosario, páginas y OCR forzado quedan en `Más opciones`, que
  se abre sola cuando una configuración existente ya usa alguna excepción.
- El modo directo escala de forma progresiva: tras las comprobaciones puede recomendar, sin ejecutar
  IA, una revisión local de hasta 64 bloques afectados. La recomendación no persiste texto, sobrevive
  al reinicio y, si se acepta, conserva intactos tanto los bloques no señalados como el resultado
  publicado hasta que la propuesta se confirme. Fallar o cancelar devuelve el trabajo a Completado.
- La configuración distingue Procesamiento directo y Revisión semántica con IA local, muestra el
  recorrido y las pasadas reales y mantiene el primero como opción recomendada. La revisión sigue
  siendo explícita: las pruebas privadas mostraron correcciones conservadoras, pero no una mejora
  universal que justifique imponer IA a todos los documentos.
- La validación local añade perfiles comparables de conversión y traducción directa/revisada,
  registra el trabajo de texto e IA previsto y separa las propuestas de contenido y estructura sin
  guardar títulos, rutas ni fragmentos documentales.
- La mejora con IA separa contratos, transporte local y guardas Markdown; PDF separa su modelo de
  página y el codec de checkpoints; y la ventana delega el flujo de modelos en un coordinador de
  presentación sin alterar su única base `QMainWindow`.
- La validación real puede aislar traducción con IA para comparar modelos. Qwen 3.5 deja de
  confundirse con los tags de razonamiento de Qwen 3, mientras la selección efectiva continúa
  limitada a `/api/tags`; la comparativa local no sustituye automáticamente el modelo equilibrado.
- La traducción permite elegir de forma explícita entre Argos offline e IA local con Ollama. El plan
  Revisado permanece independiente y ambos motores comparten glosario y guardas de fidelidad.
- La cola persistida y `JobQueue` son ahora la única fuente de verdad; se retiran la sesión JSON y
  los modelos de lote duplicados. La ejecución física conserva solo estado transitorio.
- El producto publica únicamente Markdown o EPUB. TXT y DOCX continúan admitidos como entrada, pero
  se retiran las rutas de salida y las instantáneas específicas de formatos antiguos.
- El procesamiento separa preparación, transformación y publicación, incluida la preparación del
  editor EPUB. El OCR limita cada lote a dos páginas y cuatro hilos para contener el pico de memoria
  sin reducir resolución ni precisión de tablas.
- Las referencias privadas de rendimiento pueden calibrarse ahora con varias repeticiones: rechazan
  resultados estructuralmente inestables y calculan los márgenes desde el peor tiempo y pico de
  memoria observado, evitando falsos positivos por variación del OCR.
- La arquitectura separa ahora los casos de uso de configuración de cola, persistencia, recuperación
  y materialización de revisiones de la ventana Qt. Los diálogos antiguos viven definitivamente en
  `presentation`, se eliminan las fachadas anteriores y una prueba automática impide nuevas
  dependencias de Qt o infraestructura dentro del dominio y la aplicación.
- El preflight y sus pronósticos se calculan fuera del hilo gráfico; restaurar revisiones largas
  evita el diff cuadrático y los EPUB copian en streaming los recursos binarios sin cambios.
- Los errores durables conservan solo plantillas sin contenido y `llmfit` exige hashes fijados por
  versión, recibe un entorno mínimo y deja de heredar credenciales o proxies.
- Los locks actualizan `cryptography` a la versión corregida y el workflow de Windows audita sus
  dependencias, valida que la etiqueta pertenece a `main` y prepara la Release en borrador.
- El nuevo icono oficial con contorno blanco identifica ahora la aplicación, el instalador, las
  cabeceras clara y oscura y el README mediante derivados transparentes reproducibles.
- Errores de procesamiento explicados por fase en la cola y en Actividad reciente, con trabajo
  reutilizable, recorrido temporal, recuperación contextual mientras el documento siga en la cola y
  diagnóstico copiable sin nombres, rutas, contenido ni secretos.
- El preanálisis mantiene carga, duración y advertencias en la cola sin interponer una confirmación
  informativa al pulsar `Procesar`; la validación real y la comprobación temprana continúan activas.
- Salida Markdown portable configurable como archivo único o índice con un archivo por capítulo,
  con metadatos y referencias a páginas PDF opcionales, enlaces de recursos relativos y
  regeneración coherente después de una revisión.
- La comprobación temprana elige ahora hasta cinco páginas distribuidas por diversidad real de texto,
  imágenes y tablas; las páginas visuales inicial y final preservadas cuentan como advertencia, no
  como bloqueo material, mientras que los fallos interiores repetidos sí bloquean. Las
  transformaciones siguen activadas y reutilizan la extracción/OCR en el recorrido completo.
- Las tablas PDF se publican como Markdown, HTML o texto estructurado según su complejidad; los
  casos inseguros generan revisión y un recorte visual de respaldo cuando se conservan imágenes.
- Las tablas HTML generadas para celdas multilínea llegan ahora al EPUB como tablas XHTML reales.
  Solo se admite la estructura cerrada, sin atributos y saneada que produce Parsezen; cualquier
  HTML arbitrario continúa desactivado y se muestra como texto inerte.
- La traducción y la revisión comparan además la secuencia ordenada de etiquetas de cada tabla HTML.
  Una propuesta no puede eliminar una columna cuya primera celda esté vacía ni compensar esa
  pérdida alterando otra tabla con la misma forma global.
- Las tablas cuyas reglas quedaron rasterizadas se reconstruyen localmente combinando esas reglas
  visuales con las posiciones de la capa de texto. El resultado solo se acepta si conserva exactamente
  letras, cifras y signos; columnas numéricas, celdas vacías, texto posterior y enlaces ya no pueden
  perderse dentro de una caja aproximada. El OCR solo puede sustituir una tabla si supera además sus
  guardas de cobertura y tamaño y conserva en orden las filas y columnas de cada tabla nativa. Los
  índices de hasta cuatro columnas mantienen un orden de lectura
  contiguo y vuelven a asociar cada folio separado con su entrada antes de publicarla como una fila
  semántica, aun cuando el OCR visual se rechaza. Los folios de cuatro cifras ya no quedan como
  párrafos sueltos; una cifra con un glifo alfabético dudoso exige consenso entre fila, secuencia y
  OCR, y el OCR solo puede restaurar espacios ausentes sin reemplazar las letras de la capa nativa.
- Las fronteras de esas tablas se sitúan ahora en el hueco real entre glifos, no a mitad de los
  comienzos de columna, para no partir palabras anchas entre celdas. Dos tablas consecutivas en una
  página se separan por su rótulo explícito y conservan geometrías independientes.
- Los fragmentos tabulares cortos al final de una página también se recuperan cuando un rótulo
  `Table/Tabla/Cuadro n` y las reglas exteriores ofrecen evidencia suficiente. Las divisiones
  interiores se infieren entre filas reales, una celda vacía en la primera fila no elimina su
  columna y los guiones discrecionales o de final de línea se recomponen antes de publicar.
- Las respuestas en streaming de Ollama respetan ahora un límite total de tiempo además del tiempo
  entre tokens, y su presupuesto de salida escala de forma más estricta con el fragmento. Una
  generación local desbocada se cancela y conserva el contenido anterior en vez de monopolizar el
  flujo indefinidamente.
- La publicación de una revisión vuelve a validar la combinación completa de decisiones: un cambio
  estructural que altere cifras pasa a riesgo alto y ninguna mezcla entre original y propuesta puede
  inventar o perder ocurrencias numéricas fuera de ambas versiones. El archivo anterior permanece
  intacto si el ensamblado no supera esta guarda.
- El arranque deja de importar por anticipado la pila de conversión y MarkItDown: la importación del
  punto de entrada baja de unos 2,6 s y 67 MiB de pico a unos 0,1 s y 5 MiB en el equipo de prueba.
- La revisión pasa a mostrar el progreso de la fase activa sobre sus decisiones materializadas,
  conserva ediciones activas al guardar y salir, y reanuda en la siguiente unidad pendiente.
- Las incidencias de traducción comparan ahora extractos legibles y equivalentes, sin ampliar una
  frase truncada a toda la página. El resultado dudoso deja de presentarse como recomendación y las
  reparaciones locales seguras se conservan aunque otro fragmento todavía requiera revisión.
- La corrección de traducciones Argos compara ahora original y resultado mediante Ollama local, que
  propone sustituciones JSON mínimas: cada cambio se valida y aplica por separado, una propuesta mala
  no descarta las buenas y una respuesta truncada nunca puede reescribir el resto del documento.
- Una segunda pasada bilingüe acotada revisa mezclas residuales de idioma y variantes ortográficas
  raras sobre un máximo de cuatro líneas o párrafos Markdown alineados por bloque; los nombres propios
  no activan falsos positivos y las correcciones que requieren una inserción dentro de una palabra
  pasan ya por las mismas guardas que el resto de parches. La detección de idioma se aplica al
  documento completo y no rechaza una microunidad correcta solo por carecer de contexto suficiente;
  cada forma detectada usa una solicitud de cero o un parche y el analizador descarta cambios cuyo
  texto nuevo no reduzca realmente su recuento.
- Las oraciones completas que Argos haya conservado por error se retraducen como unidades aisladas y
  solo se sustituyen si superan todas las guardas compartidas. Bibliografías, índices y catálogos de
  nombres se excluyen por entrada o fila, nunca por página completa, por lo que la prosa mezclada
  sigue siendo revisable. La salida aislada requiere evidencia local positiva del idioma solicitado
  y sus límites se calculan sobre las posiciones Unicode originales, sin recortes ante caracteres
  cuyo plegado cambia de longitud; las referencias conservadas siguen sometidas a los controles de
  fidelidad, longitud y estructura.
- Ninguna corrección bilingüe puede aumentar palabras suficientemente largas tomadas literalmente
  del original, tampoco en encabezados en mayúsculas; la pasada residual debe reducir además la señal
  que motivó su selección, por lo que arreglar un anglicismo mientras se introduce otro deja de ser
  una propuesta aceptable.
- La validación acumulativa conserva solo el bloque que haya superado el límite de reescritura tras
  varias correcciones sucesivas, vuelve a comprobar el documento completo y mantiene las mejoras
  independientes seguras en vez de descartar toda la revisión bilingüe.
- Una reanudación ya no repite dos respuestas bilingües rechazadas: guarda únicamente la decisión
  segura de conservar la traducción validada, nunca el contenido propuesto que incumplió las guardas.
- El informe de traducción se recalcula sobre la propuesta posterior a la corrección y la estructura,
  por lo que deja de contar incidencias ya resueltas y puede señalar una regresión introducida después.
- Los folios situados al final de entradas de lista o índice permanecen fuera del texto enviado a
  Argos; las guardas compartidas rechazan también cualquier corrección que los desplace a otra
  posición, aunque conserve todas las cifras.
- Argos conserva también la caja de los números romanos en encabezados, sin proteger por error el
  pronombre `I` de la prosa normal.
- Si una sustitución parcial introduce una frase repetida en un título bilingüe, Parsezen vuelve a
  evaluar únicamente el título completo y acepta la reparación solo si supera las mismas guardas de
  fidelidad, estructura e idioma.
- La restauración de títulos conservados en un tercer idioma vuelve a comprobar todas las cifras del
  resultado reparado; si una coincidencia ambigua de un índice desplaza una referencia, conserva la
  reparación previa en vez de publicar el valor alterado.
- Las continuaciones puramente numéricas de un índice ya no se interpretan como listas CommonMark:
  una línea que empiece por una referencia alta como `202.` conserva ese valor literal en el EPUB en
  vez de ser renumerada según una entrada anterior.
- El OCR de páginas gráficas completas descarta rótulos aislados cuya escala sea muy inferior a la
  tipografía dominante, pero conserva siempre la imagen original y el resto del texto reconocido.
- Una versión antigua de un checkpoint OCR etiquetado se vuelve a calcular en vez de exponerse como
  texto del documento; los controles no válidos para XML se neutralizan tanto al limpiar OCR como en
  la frontera de publicación EPUB.
- En las primeras páginas, un título OCR con un único término corto espurio puede recuperar la
  variante nativa repetida solo cuando existe un donante tipográfico fiable y único; cualquier
  ambigüedad conserva el OCR para revisión.
- Si el OCR une el rótulo `Tabla n` con la primera fila, Parsezen vuelve a separarlo antes de evaluar
  la tabla para conservar el título y generar celdas XHTML reales.
- Los encabezados generados en EPUB evitan partirse internamente entre dos páginas compatibles con
  paginación CSS, mantienen una separación superior no colapsable al abrir un capítulo y ajustan
  palabras excepcionalmente largas sin desbordar el ancho de lectura.
- Una revisión regenerada sobre un resultado antiguo vuelve ahora a su fase correcta, invalida solo
  las decisiones posteriores dependientes y reutiliza toda la transformación ya completada.
- Los paneles de traducción terminan ahora en la misma frontera de frase y explican que solo debe
  traducirse el fragmento editable visible; volver a la fase anterior acepta correctamente la
  confirmación del usuario.
- Los EPUB parciales conservan como texto las etiquetas de enlaces cuyo destino queda fuera del
  intervalo seleccionado; quitar un ancla durante la edición tampoco bloquea la publicación.
- La revisión final del EPUB explicita que el original no se modifica, guarda el borrador con
  `Guardar y salir` y confirma antes de `Descartar cambios` cuando existen cambios.

## 1.1.0 - 2026-07-30

- Control final determinista antes de publicar: TXT y Markdown se comparan con el contenido
  aprobado, DOCX y EPUB vuelven a validar su contenedor y los EPUB contrastan capítulos y recursos
  con el paquete generado. Una discrepancia conserva intacto el resultado anterior.
- Recuperación contextual de fallos con acciones para reintentar solo la fase y el documento
  afectados, revisar configuración o destino y abrir la IA local, sin mezclar otros trabajos de la
  cola ni descartar checkpoints válidos.
- Preanálisis explicable antes de procesar: la cola anticipa carga, revisiones y tiempo automático sin
  exigir una segunda confirmación; los problemas reales siguen bloqueando antes de crear el worker.
- Comprobación temprana de PDFs largos o inciertos sobre tres páginas representativas, con
  continuación automática cuando la muestra es segura, bloqueo accionable solo ante señales
  repetidas y reutilización de la extracción/OCR en el trabajo completo.
- Estimaciones adaptativas y privadas calibradas con hasta 200 duraciones locales comparables, sin
  guardar nombres, rutas, texto ni identificadores reversibles de modelos.
- Tiempo restante explicado y actualizado con el avance real, cierre global del lote, notificaciones
  de Windows solo en segundo plano y actividad reciente acotada a 20 intentos.
- Resumen final por documento que separa integridad técnica, incidencias detectadas y revisión
  manual para no presentar la validación de formato como una garantía de calidad semántica.
- Revisión por excepción mejorada: reanuda en la primera decisión pendiente, cuantifica prioridades
  altas, evita recorrer decisiones guardadas y añade atajos de teclado para comparar y avanzar.
- Lenguaje visual aligerado en toda la aplicación: superficies jerárquicas sin tarjetas anidadas,
  divisores suaves, formularios con etiquetas superiores, botones secundarios e iconos discretos,
  listas de documentos y modelos más densas y turquesa reservado a acción, foco, selección y
  progreso. La cola, Configurar, IA local, revisiones y editor EPUB comparten ahora la misma
  elevación mínima en claro y oscuro.
- Sistema visual semántico único con paletas claro/oscuro AA, preferencia de sistema, persistencia,
  cambio sin recarga, foco visible, reducción de movimiento y alto contraste nativo.
- Reflow real desde 320 px para cola, configuración, gestor de modelos, revisiones y editor EPUB,
  sin ocultar acciones esenciales ni depender de scroll horizontal de página.
- Componentes compartidos accesibles para interruptores, selectores, avisos recuperables y tiras de
  herramientas; errores de trabajo y validación conservan ahora el contexto en vez de bloquearlo
  con un modal.
- Iconografía vectorial independiente de fuentes, controles de densidad homogénea y barras de
  desplazamiento coherentes en ambos ejes y temas.
- Eliminados el tema, el shell y los paneles heredados: la ventana activa compone directamente los
  servicios de dominio y los componentes ya no contienen colores arbitrarios.
- Procesamiento e IA local aislados en controladores Qt explícitos, sin formulario oculto ni estado
  duplicado capaz de bloquear o alterar la ejecución.
- Regresión visual automatizada en claro y oscuro a 320, 768 y 1.440 px, con contratos de geometría,
  hover, foco, teclado e interruptores sin recorte.
- Pantalla principal de cuatro columnas —Documento, Flujo, Salida y Siguiente paso— con resumen y
  acción contextual en la cabecera, selección clara y sin pie global redundante.
- Configuración transaccional maestro-detalle, con resumen del plan, navegación lista-detalle en
  compacto, interruptores específicos para Traducir, Corregir y la pre-organización de Personalizar,
  destino directo, aplicación explícita a documentos compatibles, selector binario de traducción,
  glosario compacto y validación conjunta.
- Perfil de IA único por documento para compartir modelo y contexto entre traducción, corrección y
  estructura, herencia explícita del predeterminado global, excepciones estables y lectura compatible
  de sesiones anteriores. El gestor prioriza modelos instalados, muestra el alcance del
  predeterminado y protege modelos usados por trabajos sin terminar.
- Las variantes de razonamiento conocidas no se pueden seleccionar, recomendar ni ejecutar para
  transformar documentos; Parsezen exige una variante Instruct y mantiene visible cualquier modelo
  incompatible ya instalado únicamente para poder eliminarlo.
- Navegación interna para configuración, modelos, glosario, revisiones, diagnóstico y editor EPUB;
  cada flujo usa su propio pie y vuelve a la cola sin abrir ventanas auxiliares.
- Destino general recordado con restauración explícita y excepciones persistentes por documento.
- Recursos externos solo para Markdown; EPUB y DOCX integran las imágenes en su archivo.
- Estado cronológico por documento con fase exacta, progreso, revisión o error accionable.
- `Preparando` continuo desde la fijación y validación previa de la ejecución hasta la lectura,
  conversión, OCR y preparación física de recursos del documento.
- Orden único Preparar → Traducir → Corregir → Personalizar → Publicar; la traducción con IA ya no se
  presenta erróneamente como corrección durante su ejecución.
- Las traducciones redundantes se omiten cuando el idioma de destino ya coincide con el contenido
  detectado o con el idioma declarado por un EPUB, sin crear progreso ni avisos ficticios.
- Traducción con IA más resistente en documentos reales: fragmentos más acotados, menos valores
  protegidos por petición y recuperación progresiva por párrafo, línea, oración y texto entre
  marcadores, conservando únicamente la parte que no puede verificarse en vez del fragmento entero.
- La recuperación por líneas separa y repone localmente viñetas, numeración, tareas y prefijos de
  cita, por lo que el modelo puede traducir su texto sin perder la estructura Markdown.
- Los títulos aislados se traducen sin exponer al modelo su sintaxis Markdown y los nombres
  editoriales se conservan como nombres propios, evitando encabezados omitidos y marcas traducidas
  literalmente.
- Las firmas personales repetidas en mayúsculas y los nombres acompañados por años de vida se
  conservan como nombres propios sin eximir títulos temáticos en mayúsculas.
- La memoria terminológica automática excluye encabezados, números romanos y términos demasiado
  frecuentes, con un presupuesto acumulado de apariciones que nunca limita el glosario explícito.
- El informe de calidad de traducción evalúa la salida traducida antes de Corregir y Personalizar,
  y evita tratar nombres o etiquetas breves como texto sin traducir salvo que contengan señales
  claras del idioma de origen.
- Las citas en un tercer idioma pueden permanecer intactas cuando el texto que las rodea sí se ha
  traducido; si el modelo deforma un encabezado claramente escrito en ese tercer idioma, Parsezen
  restaura automáticamente su texto y conserva el nivel estructural propuesto.
- El informe de traducción señala términos repetidos traducidos con cognados incompatibles para que
  la revisión detecte errores semánticos silenciosos sin imponer una sustitución heurística.
- La memoria terminológica solo interpreta una atribución como autor cuando aparece en una línea
  propia; etiquetas técnicas y usos normales de «by» dentro de la prosa siguen siendo traducibles.
- Corrección conservadora reforzada: cambios que alteran cifras, nombres, párrafos o una parte
  sustancial del texto conservan el original por defecto, también en la aprobación masiva.
- Corregir protege marcadores internos y destinos de enlace antes de consultar al modelo y, si un
  fragmento denso sigue fallando, recupera por separado sus párrafos o frases verificables.
- La pre-organización solo adopta encabezados compatibles y conserva exactamente el resto de las
  palabras y su orden: la IA ya no devuelve bloques reescritos, sino únicamente directivas de nivel
  para líneas candidatas identificadas localmente.
- Cada encabezado propuesto debe proceder íntegramente de una única línea candidata del original;
  un párrafo largo no puede convertirse en título aunque el modelo conserve todas sus palabras.
- Las respuestas sin cambios de Corregir o Personalizar no se guardan como checkpoints definitivos,
  para que una reanudación pueda volver a evaluarlas con una configuración mejor.
- Los checkpoints de IA se identifican por modo y contenido, no por posición; una mejora del
  planificador ya no invalida fragmentos posteriores idénticos y las claves anteriores se migran al
  reutilizarlas.
- Los checkpoints generales se comparten entre variantes de glosario: solo se recalculan los
  fragmentos cuyo texto protegido cambia, mientras los demás se reutilizan por contenido.
- La traducción directa de EPUB repara residuos de idioma por unidad semántica antes de guardar el
  checkpoint agrupado, para que los marcadores internos y otros bloques ya traducidos no oculten
  una frase breve conservada en el idioma de origen.
- Si una propuesta de corrección falla solo al reensamblar el documento, Parsezen recupera los
  fragmentos que siguen siendo seguros y mantiene el original donde sea necesario.
- Extracción PDF mejorada para palabras con espaciado artificial, páginas a varias columnas,
  separadores de ancho completo e ilustraciones vectoriales, conservando todos los marcadores de
  página y enlaces recuperables, y reparando palabras separadas por un salto físico de página.
- El texto nativo y OCR retira folios de ambos márgenes, incluidos los que alternan en la banda
  superior exterior de páginas pares e impares o aparecen unidos a un encabezado corrido de
  capítulo, antes o después de su etiqueta; conserva numeraciones centradas de sección y rótulos de
  figura dentro de la columna de texto, y para OCR exige confirmación de la capa nativa aunque el
  reconocimiento las haya unido a una línea cercana.
- Los trabajadores OCR liberan cada resultado visual antes de procesar el siguiente y el banco PDF
  mide la memoria del árbol completo de procesos, en lugar de ocultar el consumo del subproceso.
- Una página cuyo OCR termina correctamente sin texto guarda ese resultado vacío como checkpoint;
  al reanudar no repite el reconocimiento, mientras que un fallo real de OCR nunca se memoriza como
  resultado válido.
- Los límites de salida de Ollama se ajustan al tamaño real de cada fragmento para cortar respuestas
  desproporcionadas sin reducir el contexto disponible para traducir o corregir.
- La validación real admite intervalos PDF y registra páginas, OCR, avisos, traducción conservada,
  imágenes, capítulos y propuestas seguras o rechazadas sin incluir identidad ni texto documental
  en el informe; su aprobación autónoma respeta siempre la recomendación conservadora de la app y
  distingue entre ejecución técnica completada y control automático de calidad superado.
- El control real de calidad también señala encabezados EPUB con dimensiones propias de un párrafo,
  de modo que una pre-organización visualmente dañina no puede quedar clasificada como correcta.
- El validador real admite equivalencias `--glossary` repetibles para ensayos terminológicos
  especializados sin incluir los términos en el informe.
- Gestor de IA local con estado compacto, filtros y búsqueda reales, lista única sin modelos
  duplicados, búsqueda e instalación dentro de Recomendados y contexto personalizable.
- Zona de arrastre simplificada con borde discontinuo visible, icono vectorial y los cinco formatos
  de entrada admitidos.
- Revisión con propuesta seleccionada de forma segura, progreso por fases y aprobación masiva
  opcional; los trabajos pendientes de revisión pueden retirarse con confirmación.
- Editor EPUB reorganizado en estructura y contenido, con barras compactas de una sola fila,
  iconografía vectorial, separación desde el cursor, unión sin pérdida de texto, listas, alineación,
  enlaces y limpieza de formato.
- Todo resultado EPUB abre un editor final con metadatos y portada agrupados, barras compactas y
  pre-organización opcional; EPUB→EPUB puede personalizarse sin exigir otra transformación.
- Acciones directas por documento para abrir cada resultado o su carpeta.
- Limpieza segura al arrancar de artefactos cifrados que ya no pertenecen a una revisión recuperable.
- Protección frente a originales modificados durante una revisión y avisos de persistencia duradera.
- Cola de dominio única para identidad, orden, configuración y snapshot de cada documento.
- Reordenación SQLite completamente transaccional e instantáneas de revisión serializadas, con
  limpieza limitada a sus propios artefactos cifrados incluso ante sustituciones o fallos.
- Coordinador de ejecución independiente de Qt para progreso, pausas, errores, revisiones y recuperación.
- Planificador de aplicación para trabajo nuevo, reanudaciones, reintentos y selección secuencial.
- Preparación inmutable de cada ejecución con configuración por documento y una única validación.
- Ejecutor físico Qt extraído de la ventana, con ciclo de vida y cancelación centralizados.
- Lanzamiento directo desde la ejecución preparada, sin duplicar configuración en el estado visual.
- Eventos de trabajador validados por el dominio antes de actualizar la proyección de compatibilidad.
- Resultados, revisiones, errores y cancelaciones resueltos fuera de la ventana por coordinadores de aplicación.
- Revisiones de OCR, traducción, corrección y estructura persistidas y reanudables de forma independiente.
- Recuperación idempotente entre revisiones sin repetir fases automáticas ya calculadas.
- Fallos inesperados del ejecutor clasificados en logs sin exponer rutas ni contenido personal.
- Editor EPUB endurecido con metadatos editables, imágenes locales cifradas, límites estructurales y
  validación de XHTML, recursos y estilos antes de publicar.
- Revisión estructural manual garantizada aunque la propuesta automática no cambie el documento.
- Publicación revisada coordinada fuera de la ventana: primero persiste el borrador, después
  reemplaza atómicamente el resultado y solo entonces completa y limpia el trabajo.
- Diagnóstico alineado con la cola SQLite recuperable que utiliza la interfaz de producción.
- Adaptadores del procesador renombrados como mapeos de ejecución estables.

## 1.0.0

Primera versión pública de Parsezen.

- Cola secuencial de documentos con configuración y estado independientes.
- Conversión local entre TXT, Markdown, DOCX, PDF y EPUB según las capacidades del formato.
- OCR, traducción offline, integración guiada con Ollama y recomendaciones adaptadas al equipo.
- Revisiones separadas de OCR, traducción, corrección y estructura.
- Editor EPUB normalizado con capítulos, jerarquía, contenido y formato básico.
- Pausa, checkpoints, recuperación e instantáneas cifradas por documento.
- Publicación atómica, originales inmutables y logs sanitizados.
- Interfaz clara basada en la identidad oficial de Parsezen.
- Aplicación, ejecutable e instalador para Windows x64.
