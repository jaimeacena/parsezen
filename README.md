![Parsezen](assets/branding/generated/parsezen-readme.png)

## ***Convierte documentos complejos en contenido útil.***

El nombre *Parsezen* proviene de *parse* (extraer y estructurar información) con *zen* (hacerlo de forma sencilla y fluida).

Parsezen transforma PDFs, documentos de Word y otros archivos con texto, imágenes y tablas en
Markdown limpio o en un EPUB organizado. El procesamiento directo comprueba el resultado y, solo si
encuentra señales concretas, puede proponerte una revisión local de los bloques afectados. También
puedes solicitar de antemano una revisión adicional. La traducción opcional usa por defecto un modelo
local de Ollama y mantiene Argos únicamente como alternativa manual. La IA se ejecuta en Ollama y
nunca envía el documento fuera del equipo. Es open source, privado y gratuito.


## Principales características

- **Obtén Markdown o EPUB listos para usar**. Obtén Markdown limpio y estructurado para tus notas, tu base de conocimiento o tus herramientas de IA, o crea EPUB por capítulos con control total sobre portada, metadatos, estructura, contenido y formato.

- **Procesamiento avanzado de documentos**. Extrae texto, imágenes y tablas, aplica OCR a páginas escaneadas, selecciona únicamente las páginas que te interesen y mejora el resultado con IA local.

- **Traducción y revisión trazables**. Combina traducción algorítmica o mediante IA con
  glosarios y memoria terminológica. El resultado distingue una corrección integrada de una
  verificación bilingüe independiente y cuenta bloques comprobados, revisados y pendientes. Las
  comprobaciones objetivas se ejecutan primero y la revisión con IA se concentra en la prosa donde
  todavía existe una señal concreta de texto sin traducir.

- **Escalable y preparado para trabajos largos**. Trabaja con varios documentos, consulta el tiempo estimado, pausa el proceso y reanúdalo cuando quieras o repite solo la fase que haya fallado. Una pausa conserva los checkpoints reutilizables y no se registra como cancelación.

- **Gratis, con IA local fácil de configurar**. Todo se procesa gratis en tu equipo, sin modificar
  los archivos originales. Instala Ollama y Parsezen comprueba el equipo y prepara sus componentes
  aprobados mediante una configuración guiada, sin comandos ni selector de modelos.

- **Dos traducciones locales.** Hy-MT2 Q4_K_M mediante Ollama es el valor inicial. Argos sigue disponible cuando
  se elige expresamente, pero Parsezen no cambia a él ni lo usa en pruebas como alternativa
  silenciosa. Ambos recorridos usan el glosario, la memoria terminológica y las mismas guardas de
  cifras, símbolos de moneda y porcentaje, enlaces, énfasis Markdown, estructura, idioma y
  cobertura.
  Las tablas se traducen por celdas alineadas sin exponer su estructura al modelo; una etiqueta
  breve que aún conserve inglés recibe una única reparación bilingüe y solo se acepta si mantiene
  intacta la tabla. Un título parcialmente traducido puede aislar una única secuencia demostrada de
  palabras residuales y reparar solo esa secuencia cuando existe una equivalencia bilingüe
  establecida; las expresiones desconocidas se conservan para revisión. Las líneas internas de una
  celda conservan sus saltos y reciben como contexto el
  texto completo de esa celda. Los ordinales ingleses, los campos entre llaves y símbolos con valor
  como `$`, `$$$` o `%` se protegen enteros antes de traducir. El plan revisado es una decisión aparte
  y puede comprobar después cualquiera de las dos salidas.

- **Local, privado y gratuito.** Tus documentos permanecen en tu equipo y los originales nunca se
  modifican. Parsezen verifica su identidad local en la preparación en segundo plano antes de procesarlos y solo habilita la IA cuando
  la configuración persistente de Ollama desactiva su nube. No necesitas suscripciones, cuotas ni
  pagos por uso.

- **Tú conservas el control.** Parsezen te muestra los cambios dudosos, te permite comparar el
  original con la propuesta y comprueba el resultado antes de publicarlo. Una recomendación nunca
  ejecuta IA por sí sola y puedes ignorarla sin perder el resultado ya creado.

- **Interfaz concentrada en el trabajo.** La entrada inicial queda cerca de una única cabecera y,
  al haber documentos, esa misma barra reúne destino, `Añadir` y la acción principal. El contador
  permanece como título del contenido. La cola crece con
  uno o varios archivos sin convertir las filas en un panel vacío. `Procesar` arranca directamente:
  las estimaciones permanecen en la cola y solo un error real interrumpe la preparación. Revisiones y
  editor EPUB se adaptan al ancho disponible y agrupan herramientas secundarias sin recortar
  funciones. Durante una revisión solo permanecen visibles la comparación, las dos decisiones y el
  avance; localizar, restaurar y aprobar en lote aparecen únicamente cuando son útiles.
  La alternativa segura aparece ya seleccionada, de modo que `Siguiente` basta cuando no quieres
  cambiarla; las decisiones anteriores siguen intactas al reanudar.

- **Pensado para trabajos largos.** Puedes procesar varios documentos, consultar el tiempo
  aproximado, pausar, continuar más tarde y reintentar únicamente la fase que haya fallado. En EPUB,
  también se conserva de forma privada cada subfragmento de IA ya validado y cada decisión segura de
  mantener la traducción dentro de un capítulo.

- **EPUB sin pasos innecesarios.** Antes de publicar confirmas título, autor, idioma, portada y
  capítulos. El editor completo sigue disponible cuando quieres ajustar estructura o contenido. En
  un EPUB de origen, guardar sin cambios conserva el archivo exacto y una edición solo textual
  mantiene byte por byte navegación, estilos, fuentes, imágenes y demás recursos. Cuando la primera
  página de un PDF se usa como portada, sus imágenes extraídas no se repiten dentro del cuerpo. La
  página de portada queda además identificada para lectores EPUB 3 y lectores heredados, evitando
  que un conversor compatible la añada de nuevo al inicio.

- **Jerarquía conservadora.** Partes y capítulos explícitos se agrupan sin alterar el orden; un
  índice impreso o los marcadores internos del PDF también pueden confirmar partes, capítulos o
  secciones no numerados. Los índices planos o con sangría irregular recuperan una relación de dos
  niveles únicamente cuando varias partes o apéndices explícitos delimitan grupos coherentes; un
  rótulo estructural sin folio solo se incorpora si está junto a las filas paginadas de su índice.
  Solo se acepta un marcador cuando su título coincide de forma única en su página. Un nivel con más
  de 128 rótulos se trata como una colección de secciones, no como cientos de archivos EPUB:
  Parsezen asciende al nivel superior y mantiene esos rótulos como destinos dentro del capítulo. Si
  el cuerpo no conserva un encabezado recuperable, su contenido permanece en el libro pero no se
  inventa una entrada navegable. Las selecciones sobreviven a la confirmación, el editor y la
  publicación final, y publicar directamente conserva el mismo árbol `Parte → Capítulo → Sección`
  que abrir primero el editor. Las marcas privadas que transportan la evidencia nunca aparecen en el
  libro.

- **Esquema global verificable.** La revisión estructural combina índice, páginas, geometría,
  encabezados y roles semánticos, pero solo aplica directivas de nivel sobre palabras ya existentes.
  Antes de decidir, muestra los árboles actual y propuesto.

- **Contraste local selectivo.** En regiones realmente ambiguas de un PDF, Parsezen puede comparar
  la capa de texto, un segundo extractor nativo y el OCR. Si todavía discrepan, usa un modelo visual
  local instalado solo sobre un recorte pequeño y acepta su lectura bajo guardas estrictas. Antes de
  recurrir al modelo, una grafía OCR puede aceptarse por consenso si la misma forma aparece también
  en otra línea nativa del documento, domina de manera inequívoca a las variantes cercanas y cambia
  un único término. Este consenso nunca elimina diacríticos ni altera cifras. Si ninguna evidencia
  resuelve una línea, Parsezen conserva un recorte visual exacto en su posición y omite solo esa
  lectura textual; con demasiadas discrepancias, conserva la página completa. La duda nunca queda
  publicada silenciosamente como texto fiable. Cantidades monetarias, URLs y expresiones ordinarias
  como `3D` o `H2O` permanecen como texto y no activan OCR solo por mezclar letras y cifras.
  Cuando una fuente dañada afecta de forma sistemática al menos a ocho líneas, el segundo extractor
  se limita a las cajas exactas de esas líneas y solo transfiere glifos corroborados: conserva los
  límites de palabra, la puntuación y las cifras de la capa nativa.

- **Énfasis refluible desde PDF.** La extracción conserva negritas y cursivas completas y también
  los tramos tipográficos dentro de una frase. Una palabra partida al final de línea mantiene un
  único énfasis válido; la versión del checkpoint invalida resultados anteriores que no contenían
  esos tramos. Las versalitas que continúan una frase permanecen dentro del párrafo, y un glifo
  elevado mal codificado solo se corrige cuando su tamaño y posición demuestran el símbolo visual.
  Al traducir, las palabras enfatizadas siguen siendo traducibles y solo sus delimitadores quedan
  protegidos, incluida la combinación de negrita y cursiva; si un bloque contiene muchos tramos, se
  divide entre ellos sin cortar ninguno. Las direcciones de correo se conservan como valores opacos
  tanto con IA local como con Argos y no generan falsos avisos de texto sin traducir; una revisión
  tampoco puede modificar correos ni URL. Si una línea de lista queda intacta tras un rechazo seguro,
  puede reintentarse de forma aislada sin cambiar su viñeta ni su énfasis.

- **Índices EPUB fieles.** Las entradas conservan jerarquía, negrita, cursiva, enlaces internos y
  una columna de folios alineada —también en libros de más de mil páginas—, sin convertir el índice
  en una lista irregular. Los términos convencionales inequívocos se localizan sin gastar otra
  llamada de IA. Un folio con un glifo dudoso solo se corrige cuando lo confirman el OCR local o la
  secuencia de páginas vecinas.

- **IA local sin complicaciones.** Parsezen te ayuda a instalar Ollama, comprobar tu equipo y
  preparar los componentes fijados por Parsezen sin que tengas que elegir tags ni utilizar comandos.

## Cómo usar Parsezen

1. **[Descarga la última versión](https://github.com/jaimeacena/parsezen/releases/latest).** Necesitas Windows de 64 bits, pero no tienes que instalar Python.

2. **Añade tu documento.** Puedes trabajar con PDF, Word, EPUB, Markdown y archivos de texto.
3. **Configura el resultado.** Dentro de Parsezen, dos tarjetas claras permiten elegir Markdown o
   EPUB; traducción, páginas y OCR usan filas breves `Etiqueta — Valor — ›`, y la revisión adicional con IA un
   único interruptor. Al elegir un idioma aparecen traductor y glosario; un intervalo se resume como
   `25–140`. Cada elección válida se guarda al instante, sin texto técnico permanente, pie de acciones
   ni scroll en el tamaño normal.
4. **Procesa y revisa.** Parsezen extrae y organiza el contenido, conserva las imágenes y tablas
   compatibles y mantiene también la lámina original cuando una página completa contiene una tabla
   OCR, una imagen girada o una figura numerada que no puede separarse con seguridad del escaneo.
   Las portadas sin capa textual útil, las contraportadas y los mosaicos de rótulos permanecen como
   láminas: una lectura OCR parcial o espacialmente falsa nunca los sustituye. Las ilustraciones
   discretas se insertan en su posición de lectura y permanecen unidas a su pie.
   En tablas abiertas de un escaneo puede reconstruir filas y columnas desde la geometría de la capa
   textual, pero solo si conserva todos los caracteres; añade un recorte visual y marca la asociación
   inferida para revisión. Una tabla nativa con varias filas parciales incompatibles se degrada de la
   misma manera; si procede de un escaneo, se publica su recorte visual y se omite la cuadrícula
   textual incierta sin perder la prosa exterior. Cuando la tabla nativa ya es fiable, conserva sus
   filas y saltos internos en vez de sustituirla por una versión OCR más plana. Una grafía aislada
   que siga en disputa recibe el mismo tratamiento localizado: solo su línea pasa a imagen y el
   resto de la página continúa siendo
   refluible. Las llamadas de nota se muestran como superíndices cuando una definición pequeña al pie
   de la misma página confirma su número; una cifra sin ese respaldo no se reinterpreta. Las
   enumeraciones con una secuencia visual
   inequívoca conservan números, sangrías y líneas
   envueltas como listas refluibles; una palabra espaciada de su rótulo solo se recompone si la misma
   grafía completa aparece en la propia página. Un folio pequeño con letras confundidas por cifras se
   retira cuando al menos dos páginas numéricas confirman la misma secuencia o, en un rango aislado,
   cuando su geometría exterior, tamaño y cercanía al número de página solo son compatibles con un
   folio. Cualquier decisión pendiente se muestra antes de publicar.
   Si una cifra imposible dentro de un signo zodiacal sigue sin confirmarse, el OCR se conserva solo
   como evidencia privada: no puede añadir rótulos ni listas al texto del libro. Parsezen mantiene la
   capa nativa, la lámina original y señala la página para compararla, sin adivinar el valor.

> Windows puede mostrar «Editor desconocido» porque Parsezen todavía no utiliza una firma comercial. Asegúrate de descargarlo desde este repositorio.


## Lo que debes saber

- Parsezen transforma el contenido de un PDF; no intenta reproducir exactamente el diseño de cada página.
- Los documentos escaneados, las tablas complejas y las maquetaciones poco habituales pueden requerir una revisión final.
- Los modelos de IA son opcionales y pueden ocupar varios gigabytes.
- Necesitas conexión a Internet para descargar Parsezen, Ollama, los modelos o las herramientas opcionales. Después, el procesamiento se realiza localmente.


## Ayuda

- [Guía de uso](docs/user-guide.md)
- [Configurar la IA local](docs/local-ai-setup.md)
- [Informar de un problema](https://github.com/jaimeacena/parsezen/issues)
- [Ver todos los cambios](CHANGELOG.md)

## Desarrollo

La aplicación separa dominio, casos de uso, persistencia local, procesadores y presentación Qt.
Los límites de importación se validan automáticamente para que cada módulo pueda evolucionar y
probarse sin arrastrar la interfaz o SQLite.

Para trabajar en el repositorio:

- el [modelo operativo para agentes](docs/agent-operating-model.md) explica cómo orientarse, decidir,
  verificar y acumular evidencia;
- el [plan de trabajo](docs/work-plan.md) conserva el estado actual y la siguiente frontera;
- la [arquitectura](docs/architecture.md) describe el diseño implementado;
- la [lista de aceptación](docs/acceptance-checklist.md) contiene los gates de publicación.

Cada trabajo compila una única secuencia de ejecución que comparten interfaz, preflight y procesador.
La ventana delega la ejecución de cola, la recuperación y el flujo de revisión en coordinadores sin
widgets. SQLite conserva un historial técnico versionado de transiciones y snapshots, siempre sin
texto documental, prompts ni rutas.

Los benchmarks privados no se versionan. `scripts/benchmark_documents.py profile` mide tiempo total
y por página, RSS, OCR y recursos de un PDF; `scripts/benchmark_runtime.py` usa solo un payload
sintético para medir DPAPI, snapshots, recuperación, arranque en frío, disco temporal y tamaños de la
instalación/instalador indicados. La validación opcional con documentos reales actualiza su informe
atómicamente después de cada caso, de modo que una interrupción conserva las métricas ya obtenidas.
La selección y aprobación de componentes especializados se define en la
[política de modelos de IA local](docs/local-ai-model-policy.md): Hy-MT2 Q4_K_M prepara la
`Traducción IA` y LFM Q6_K prepara la `Revisión IA`. `Preparado` acredita identidad, licencia,
privacidad y contrato local; la revisión sigue produciendo propuestas bajo guardas y confirmación
humana, no una aprobación semántica automática.
La pantalla de IA local muestra únicamente las capacidades fijas `Traducción IA` y `Revisión IA` y
sus estados locales; no ofrece un selector de tags, endpoints ni modelos arbitrarios.
Para comparar traducción EN→ES de forma optativa y sin descargar modelos, ejecuta
`python scripts/evaluate_translation_models.py --models TAG_A TAG_B --repetitions 2` con tags que ya
aparezcan instalados en Ollama. Usa el corpus sintético versionado del script y genera un informe
atómico de hashes e indicadores agregados, sin contenido, prompts, respuestas ni rutas.

## Licencia

Parsezen es gratuito y se distribuye bajo licencia [MIT](LICENSE).
