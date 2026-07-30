# Historial de cambios

## En desarrollo

- Sin cambios todavía.

## 1.1.0 - 2026-07-30

- Control final determinista antes de publicar: TXT y Markdown se comparan con el contenido
  aprobado, DOCX y EPUB vuelven a validar su contenedor y los EPUB contrastan capítulos y recursos
  con el paquete generado. Una discrepancia conserva intacto el resultado anterior.
- Recuperación contextual de fallos con acciones para reintentar solo la fase y el documento
  afectados, revisar configuración o destino y abrir la IA local, sin mezclar otros trabajos de la
  cola ni descartar checkpoints válidos.
- Preanálisis explicable antes de procesar: la cola anticipa carga, revisiones y tiempo automático;
  los trabajos largos o de riesgo alto muestran una comprobación concisa antes de arrancar.
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
