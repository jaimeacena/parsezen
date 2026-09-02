# Aceptación antes de publicar Parsezen

Esta lista es el contrato de publicación, no el estado del proyecto ni una narración de desarrollo.
El orden actual de trabajo vive en [`work-plan.md`](work-plan.md) y el protocolo de evidencia en
[`agent-operating-model.md`](agent-operating-model.md). Marcar una condición exige una comprobación
ejecutada sobre el alcance correspondiente; ausencia de avisos, fallback seguro o archivo generado no
equivalen por sí solos a fidelidad.

## Cómo usar esta lista

La aceptación avanza en cascada y se detiene en el primer fallo material:

1. **integridad y privacidad**: original, loopback, artefactos, persistencia y publicación atómica;
2. **contratos automáticos**: dominio, pipeline, formatos, accesibilidad y regresiones;
3. **canarios y corpus**: extracción, traducción y estructura por separado;
4. **holdouts**: documentos no usados para construir la regla;
5. **recorrido integral**: resultado real, reapertura, revisión y EPUB final;
6. **distribución**: paquete limpio, versión, dependencias y actualización.

Cada ejecución conserva un registro breve y sin contenido con fecha, commit, entorno, alcance,
comandos, resultado, exclusiones y artefactos privados empleados. Los resultados de otro commit o una
muestra menor solo aportan línea base; no se trasladan como aprobación del candidato actual.

Para un cambio focal se ejecuta primero su prueba y el bloque afectado. Antes de publicar se recorre
la lista completa. Un cambio exclusivamente documental valida diff, enlaces, coherencia y formato,
pero no afirma que el pipeline fue reejecutado.

## Automática

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src/parsezen
python -m pytest
python -m pytest -m acceptance
python -m pytest --cov=parsezen --cov-report=term-missing --cov-fail-under=88
python -m pytest tests/test_visual_regressions.py
python scripts/sync_version.py --check
```

`Validar Parsezen.cmd` ejecuta la aceptación corta sin documentos privados ni IA real.

## Interfaz y cola

- El logo oficial se ve nítido y los acentos aparecen correctamente a 100 %, 125 % y 150 %.
- Sin preferencia guardada, la apariencia sigue a Windows. Sistema, claro y oscuro se aplican sin
  recarga, persisten y usan el logo oficial correspondiente sin destello inicial.
- Texto normal, acciones, bordes relevantes y foco cumplen los contrastes documentados en ambos
  temas; error, aviso, éxito y fase no se comunican solo mediante color.
- Cada contexto tiene como máximo una acción primaria dominante; iconos y acciones globales
  secundarias permanecen planos hasta hover o foco, y el peligro no aparece en rojo permanente.
- La cola usa una única cabecera con marca, destino, `Añadir`, acción principal y Ajustes; en vacío
  oculta las dos acciones ya cubiertas por el selector de archivos. Las páginas internas sustituyen
  esa cabecera por Volver, título y Ajustes, sin duplicarla.
- Configuración, IA local, revisiones y editor se agrupan mediante espaciado, títulos y divisores:
  no reaparecen tarjetas anidadas ni contornos fuertes alrededor de cada fila.
- A 320 px no hay scroll horizontal de página, superposición ni acciones esenciales fuera del
  viewport en cola, configuración, IA local, revisiones o editor EPUB.
- Todo el flujo principal se puede recorrer con teclado; el foco es visible, entra en errores
  recuperables y vuelve al control que abrió una página interna.
- Los botones de icono tienen nombre accesible y tooltip; los formularios mantienen etiquetas
  visibles, error asociado y valores después de una validación fallida.
- La zona de añadir permanece justo bajo la última fila, comunica selector y arrastre y crea una
  fila por documento mediante ambos métodos.
- TXT, Markdown, DOCX, PDF y EPUB muestran icono, nombre, tamaño y configuración compatible.
- Cada formato muestra un distintivo propio y Salida no duplica el icono del documento.
- La pantalla usa exactamente Documento, Flujo, Salida y Estado, en ese orden, además de
  tracks sin encabezado para reordenar y retirar.
- Un documento nuevo muestra `Sin salida` y ofrece `Configurar` en Estado; todavía no es
  ejecutable ni cuenta como revisión.
- `Configurar` abre una página compacta dentro de la ventana principal. Markdown y EPUB usan dos
  tarjetas visuales exclusivas, Revisión adicional con IA un interruptor, y Traducir, Traductor, Glosario,
  Páginas y OCR una fila `Etiqueta — Valor — ›`. No hay segmentos, encabezados ni pie, y se ve entera
  sin scroll al tamaño normal.
- El bloque de configuración está centrado horizontalmente; los menús de cada fila aparecen bajo su
  valor, alineados a la derecha, y no saltan al margen izquierdo de la ventana.
- Los documentos nuevos parten en procesamiento directo. Una configuración ya guardada conserva su
  plan. IA local y OCR automático siguen siendo valores iniciales sin preguntas técnicas.
- Revisión adicional con IA local activa una pasada proactiva de texto y, solo en EPUB, también de
  estructura; no existen interruptores independientes ni un resumen técnico del recorrido en esta
  página.
- Flujo usa `Convertir`, `OCR`, `Traducir`, `Corregir contenido`, `Verificar traducción`,
  `Organizar EPUB` y `Personalizar EPUB`; no usa `Revisar con IA` para describir trabajo automático.
- `→` separa fases y `y` une acciones integradas. El idioma de destino siempre aparece al traducir y
  el motor queda en la ayuda contextual.
- Flujo presenta una única secuencia semántica. Termina en `Tu revisión si hay cambios` en recorridos
  revisados no EPUB y en `Tu revisión final` en toda salida EPUB; si se parte en dos líneas, todos los
  pasos mantienen el mismo estilo y cada paso salvo el último conserva su flecha de continuación.
- En la tabla amplia, Flujo dispone de más ancho que Documento, Salida o Estado; la redistribución no
  provoca scroll horizontal ni impide mostrar acciones como `Revisar y publicar`.
- Antes de procesar, todos los pasos de Flujo mantienen el mismo peso. Durante la ejecución, el paso
  actual usa el acento, los completados muestran `✓`, los futuros se atenúan y `Tu revisión` solo usa
  el tono de aviso cuando requiere una acción. Un fallo afecta únicamente a su paso.
- La progresión visual deriva de fases semánticas, no de comparar etiquetas. `Traducir` y `Verificar
  traducción` permanecen como pasos distintos porque usan pasadas independientes; cada fila progresa
  de forma independiente.
- Varias filas calculan su flujo de manera independiente; añadir documentos no mezcla idiomas,
  motores, OCR ni políticas de revisión.
- La IA local muestra un estado breve y deja el modelo general heredado en la ayuda contextual, sin
  excepciones por documento. Si falta, la acción principal abre su configuración.
- Cada elección válida se aplica y persiste inmediatamente. No hay botones Cancelar o Crear/Guardar;
  Volver y Escape cierran sin confirmación porque no existen cambios pendientes.
- `Traducir — No traducir` oculta Traductor y Glosario. Al elegir cualquier idioma aparecen ambas
  filas; IA local es el motor inicial y Argos como motor completo solo se activa mediante una elección
  explícita. No aparecen proveedores ni direcciones configurables.
- Elegir traducción con IA o revisión semántica exige un modelo instalado anunciado por Ollama
  `/api/tags`; Argos directo no exige modelo. Argos con revisión usa Ollama después de traducir.
- Las pruebas reales que traducen usan por defecto IA local y el modelo instalado elegido para un PC
  estándar. Nunca degradan el documento completo a Argos por ausencia del modelo o por un fallo:
  probar Argos como motor requiere indicarlo expresamente. Solo un título residual puede usar un par
  Argos ya instalado como reparación local, sin descargas y bajo las guardas compartidas.
- Un resultado directo solo muestra `Revisión sugerida` cuando comprobaciones objetivas permiten
  acotar bloques concretos. La recomendación persiste tipos, cantidades, posiciones y huellas
  no reversible, pero ningún extracto, y nunca inicia Ollama automáticamente.
- `Revisar con IA` procesa como máximo 64 bloques señalados, excluye código, imágenes y procedencia y
  deja el archivo publicado intacto hasta confirmar una propuesta. Cancelar o fallar restaura el
  estado completado y la salida anterior.
- La revisión dirigida se puede iniciar tras reiniciar la aplicación: reconstruye el contenido desde
  archivos locales, valida de nuevo las huellas, reubica objetivos únicos tras empaquetar EPUB y
  conserva la alineación bilingüe cuando existe. Un bloque modificado o ambiguo se rechaza antes de
  invocar el modelo.
- El glosario se edita en una ventana compacta y su fila muestra `Ninguno` o el número de términos.
- Si el contenido ya está en el idioma de destino con suficiente confianza, no se invoca ningún
  traductor ni aparece una fase de traducción ficticia.
- Los flujos de mismo formato exigen una transformación útil, salvo EPUB→EPUB: la personalización
  final es por sí misma una operación válida.
- Todo resultado EPUB abre una confirmación breve de título, autor, idioma, portada y capítulos. El
  editor completo solo se abre a petición o ante un bloqueo.
- Desde la preparación inmutable de la ejecución hasta que terminan validación, lectura, conversión,
  OCR y recursos, la fila muestra `Preparando` sin estados visuales intermedios.
- El destino general heredado permanece en la cabecera, no se repite en la configuración y no puede
  sustituirse por documento. Cambiarlo actualiza todos los trabajos editables.
- Los elementos accionables muestran respuesta de hover y cursor de enlace; el asa de ordenación
  muestra cursor de arrastre.
- Cada fila conserva opciones independientes.
- Reordenar cambia el orden real; retirar una fila no afecta a las demás.
- Cada fila muestra una papelera vectorial que solo la retira de la cola.
- Un documento pausado o pendiente de revisión se puede retirar tras confirmar; se descarta el
  checkpoint o la revisión sin tocar el original.
- Configuración, modelos, revisiones y editor EPUB permanecen dentro de la aplicación y vuelven al
  contexto que los abrió. El glosario usa una ventana modal compacta sobre la configuración.
- Los controles son compactos y los desplegables tienen un chevrón visible; la rueda desplaza la
  página sin cambiar accidentalmente una selección.
- Los botones y secciones indican foco o selección mediante fondo, sin contornos añadidos ni estados
  de ratón que permanezcan marcados después de cerrar su menú.
- `Páginas` ofrece Todas o abre un diálogo para el intervalo; después muestra directamente un valor
  como `25–140`, sin campos Desde/Hasta permanentes. `OCR` ofrece Automático o Todas las páginas.
- Al empezar, las opciones del trabajo quedan bloqueadas.
- Si el original cambia después de añadirlo —también con el mismo tamaño y fecha restaurada— la
  preparación lo rechaza y pide retirarlo y volverlo a añadir; ningún resultado mezcla versiones.
- Solo una tarea pesada figura como activa.
- Un error o una revisión pendiente permite avanzar al siguiente documento. La fase fallida ofrece
  `Ver error`: durante la sesión muestra el detalle concreto y, tras reabrir, una explicación estable
  sin texto del documento, con acciones acordes a su causa.
- `Reintentar esta fase` ejecuta solo el documento fallido aunque existan trabajos nuevos u otros
  fallos en la cola, y conserva checkpoints y decisiones anteriores válidos.
- Un fallo de IA abre `Componentes de IA local`; uno de destino prioriza revisar la salida; uno de integridad
  explica que el resultado definitivo no fue sustituido.
- Todo resultado completado ha superado una comprobación del temporal previa a la publicación y
  muestra `Integridad final comprobada` sin exponer texto ni rutas.
- Alterar el temporal durante esa comprobación impide la publicación, elimina el temporal y
  conserva intacto cualquier resultado anterior.
- Varias revisiones del mismo documento aparecen en orden de fase y cada una desbloquea solo la
  siguiente.
- Una combinación de decisiones que introduzca o pierda ocurrencias numéricas fuera de los límites
  del original y la propuesta se rechaza antes de escribir, sin sustituir el resultado anterior.
- Una continuación numérica de un índice que empiece por `202.` conserva `202` en el XHTML y no se
  renumera por una lista CommonMark anterior.
- Cerrar entre dos revisiones conserva la última decisión y no incrementa los intentos de las fases
  automáticas ya calculadas.
- Una interrupción después de guardar una decisión pero antes de actualizar la cola se reconcilia
  sin repetir trabajo ni saltarse la siguiente revisión.
- La cabecera distingue procesar, pausar y revisar; durante el procesamiento, la fila y la fase
  expresan de forma inequívoca la actividad sin un pie duplicado.
- `Procesar` inicia el plan por documento sin depender de controles ocultos de la interfaz anterior;
  cualquier problema de validación se muestra al usuario.
- Cada trabajo ejecutable muestra un intervalo de tiempo automático antes de empezar; tras varias
  ejecuciones compatibles indica que la estimación está calibrada en este equipo.
- El preanálisis no guarda nombres, rutas ni contenido, proyecta la estimación en la cola y no añade
  una confirmación informativa entre `Procesar` y la preparación real.
- Tras diez segundos de progreso medible, la fila puede actualizar el tiempo restante y explica si
  descontó tiempo o lo recalculó con el ritmo real; el texto visible usa `Quedan ~X–Y min` y, al
  superar el máximo inicial, deja de mostrar una cuenta atrás precisa.
- Al terminar un documento, `Ver resumen` distingue integridad técnica, incidencias detectadas y
  revisión manual, sin presentar la ausencia de señales como equivalencia semántica.
- El cierre del lote cuenta documentos listos, pendientes de revisión, fallidos, pausados y
  cancelados; ofrece actividad cuando existen resultados, errores o cancelaciones consultables.
- Los avisos largos crecen sin recortar texto; una pausa y una interrupción usan copias distintas. Al
  retirar trabajos, el aviso se recalcula con los supervivientes y desaparece con el último.
- Las notificaciones de Windows aparecen al terminar o requerir atención solo con Parsezen
  minimizado o inactivo.
- Actividad reciente conserva como máximo 20 intentos, no contiene texto documental, puede abrir
  resultado o carpeta y se borra sin eliminar ningún documento. En escritorio distribuye lista y
  detalle en dos paneles; en compacto los apila sin scroll horizontal.

## PDF y OCR

- Procesar un rango usa exactamente esas páginas.
- Un PDF de 120 páginas o más, o de 60 con OCR forzado, IA local o EPUB, comprueba automáticamente
  primera, central y última página del intervalo antes del trabajo completo.
- Una muestra segura continúa automáticamente; incidencias materiales repetidas en al menos dos de
  tres páginas impiden iniciar el documento completo y ofrecen una acción concreta.
- La extracción y el OCR válidos de la muestra se reutilizan en el rango completo, pero sus
  transformaciones y salidas temporales se eliminan.
- Cambiar configuración, tamaño o fecha del origen invalida la muestra superada y obliga a repetirla.
- Durante la comprobación temprana la fila dice `Preparando` y `Pausar` sigue disponible.
- Abrir o contar páginas durante el preflight no bloquea pintura, foco ni interacción de la ventana.
- El progreso avanza por páginas durante extracción y OCR.
- Una página con OCR vacío no hace fallar el documento completo.
- Reanudar después de un OCR correcto pero vacío no vuelve a reconocer esa página.
- Un fallo del motor OCR no se confunde con un resultado vacío y se vuelve a intentar al reanudar.
- En una página gráfica completa, una línea OCR aislada de una a cuatro letras y escala muy inferior
  a la tipografía dominante no aparece como texto, pero la imagen original sí; varias etiquetas
  cortas de escala uniforme se conservan.
- Un checkpoint OCR con una cabecera de versión anterior se invalida y recalcula; ni su cabecera ni
  sus separadores de control aparecen en Markdown o EPUB.
- Un título de portada dependiente de OCR recupera una variante nativa repetida solo ante un donante
  fiable y único, una única palabra sustantiva corta espuria, conectores compatibles y cifras
  idénticas; dos variantes, palabras largas u omisiones sustantivas no se corrigen automáticamente.
- El consenso de títulos no modifica el payload OCR guardado ni ningún otro bloque de la página.
- Un rótulo `Tabla n` unido por OCR a la cabecera se conserva como párrafo separado y la cuadrícula
  restante se publica como tabla XHTML, no como un párrafo con barras verticales.
- Una página de dos a cuatro columnas conserva cada columna completa de izquierda a derecha, sin
  desplazar títulos o separadores de ancho completo.
- Un índice con al menos tres folios separados a la derecha vuelve a asociar cada número con la
  entrada de su misma fila, conserva el orden multicolumna y genera una tabla de índice con folios
  alineados, sangría, énfasis y enlaces internos; una columna numérica ambigua o una tabla ordinaria
  permanece intacta.
- Un índice de un libro largo conserva folios de cuatro cifras como `1023` en la celda derecha sin
  convertirlos en párrafos sueltos ni ampliar a cuatro cifras la detección general de márgenes.
- Una entrada en mayúsculas cuya fuente PDF omite espacios lógicos, como
  `II-NUTRICIÓNEFICAZ`, se publica como `II - NUTRICIÓN EFICAZ` cuando los huecos entre glifos lo
  corroboran; la prosa mixta no se reespacia por esta heurística.
- Un folio mixto como `62S` solo se convierte en `628` cuando la etiqueta de su fila, la secuencia
  numérica vecina y el OCR local dejan un único candidato. El OCR puede reponer un espacio ausente
  entre palabras de esa fila, pero no sustituye las letras nativas aunque su propia grafía difiera.
- Una cantidad con `$` prefijado, una URL alfanumérica y expresiones ordinarias como `3D` o `H2O` no
  activan OCR numérico. Un folio formado solo por glifos compatibles con cifras sí conserva evidencia
  visual si OCR y capa nativa no permiten demostrar su lectura.
- Un solo carácter de sustitución o una pareja de puntuación nativa corrupta activa el contraste
  local. Si OCR, segunda extracción y arbitraje visual no coinciden, el informe identifica la página
  para revisión y la lectura dañada no se considera confirmada silenciosamente.
- Una cifra pegada a la prosa se muestra como llamada de nota en superíndice solo si existe una
  definición pequeña con el mismo número en la banda inferior de esa página. Sin esa correspondencia
  no se modifica; traducción y revisión deben conservar también el dígito superíndice confirmado.
- Un cero final pequeño y elevado delante de un signo zodiacal inequívoco puede restaurarse como
  símbolo de grado, pero un valor imposible de 30 a 99 grados no se cambia por plausibilidad. Si OCR,
  segunda extracción y arbitraje no confirman el valor, la página queda señalada y conserva su lámina.
- Un romano visual `II`/`III` codificado como `n`, `it`, `in` o `ui` en un rótulo astrológico solo se
  recupera cuando el título de decano anterior o dos hermanos próximos confirman el mismo ordinal. Un
  `IE` de título solo pasa a `II:` si los otros dos títulos del signo demuestran el miembro ausente;
  prosa, rótulos sin consenso y otros dominios permanecen intactos.
- En esa página no reemplazada por OCR, la lectura OCR inconclusa sigue disponible para diagnóstico,
  imágenes e informe, pero no puede añadir al resultado rótulos, párrafos, listas ni tablas que no
  procedan de la capa nativa.
- Un encabezado de sección sin folio queda fuera de la tabla anterior y no se fusiona con su última
  entrada durante la conversión CommonMark→XHTML.
- Argos no recibe el folio final de una entrada de índice y tanto la traducción como la corrección
  conservan ese mismo folio al final de la entrada; desplazarlo se rechaza aunque la cifra siga
  presente en el bloque.
- Una tabla con capa de texto y reglas solo rasterizadas recupera filas y columnas mediante análisis
  visual local; conserva exactamente letras, cifras y signos, incluidas columnas solo numéricas y
  celdas vacías, sin absorber prosa posterior. Un falso candidato o una tabla con enlaces no
  transferibles permanece en el flujo de texto normal y no activa OCR por sí solo.
- Una palabra ancha junto al límite de columna permanece completa en su celda. Si una página contiene
  dos tablas separadas por un rótulo explícito, cada una usa sus propias columnas y ninguna se fusiona
  con la otra.
- Un fragmento corto con rótulo explícito y solo reglas exteriores recupera sus filas sin cortar los
  glifos que sobresalen de esas reglas. Una celda vacía en cualquier columna de la primera fila no
  elimina esa columna y un guión tipográfico al salto de línea no reaparece como una palabra partida.
- Una tabla abierta dentro de un escaneo completo puede usar dos reglas exteriores de aproximadamente
  el 60 % del ancho si encierra al menos cuatro filas, dos columnas repetidas y rótulos de fila
  inequívocos. Se publica como XHTML, conserva cada carácter, añade un único recorte visual y crea una
  incidencia de revisión sobre la asociación inferida.
- Una matriz abierta y dispersa con una regla bajo la cabecera recupera entre cuatro y ocho columnas
  únicamente cuando los mismos huecos aparecen en al menos dos líneas de cabecera, cada cabecera y
  primera celda están presentes y la cobertura de caracteres es exacta. Las celdas vacías o con guion
  no se inventan ni se desplazan.
- Dos columnas de prosa entre separadores, un índice, reglas desalineadas, enlaces o una región sin
  rótulos repetidos permanecen en el flujo normal. Rebajar el ancho de regla para escaneos no puede
  convertir estos controles en tablas.
- Una tabla PDF con celdas multilínea se publica como XHTML semántico. Las continuaciones visuales
  inequívocas se unen con espacios antes de traducir; frases cerradas, listas y rótulos independientes
  conservan sus saltos. Sus etiquetas no aparecen impresas como texto. Una tabla HTML con atributos,
  scripts o estructura ajena al generador permanece inerte.
- La traducción de una tabla simple modifica únicamente texto de celdas: conserva byte por byte su
  envoltura Markdown/XHTML y sus entidades HTML. El código de una entidad numérica no se compara como
  una cifra visible; un salto heredado se conserva y un salto añadido por la IA se rechaza.
- Una celda que conserva un rótulo breve del idioma fuente después del reintento de tabla recibe una
  sola reparación bilingüe aislada. Solo se acepta si elimina la señal y supera todas las guardas; de
  lo contrario se conserva el original sin reescribir filas vecinas.
- Una revisión que elimina una celda HTML vacía, su columna o un salto interno se rechaza aunque otra
  tabla adquiera casualmente la forma perdida y los recuentos globales sigan coincidiendo.
- Una propuesta que añade un envoltorio `html` o cambia cualquier etiqueta HTML ajena a una tabla
  generada se rechaza completa y esas etiquetas nunca aparecen como texto visible en el EPUB.
- Una tabla escaneada solo usa una estructura OCR si conserva cobertura, proporción y todas las
  cifras, filas y columnas en el mismo orden; una salida inflada o que omite una columna vacía
  mantiene la capa nativa y su orden multicolumna.
- El texto con tracking artificial recupera palabras normales sin eliminar espacios léxicos.
- Una palabra dividida al final de una página se publica unida si continúa en minúscula en la
  siguiente, sin perder el marcador interno durante la revisión.
- Una cantidad escrita con palabras en un título breve conserva su valor entre inglés y español:
  `PART SEVEN` no puede convertirse en `PARTE SEIS`; `TWO` puede publicarse como `DOS`, `AMBOS` o `2`
  según el contexto. El adverbio inglés `once` no se confunde con el número español y la prosa puede
  reformular cantidades sin activar una falsa alarma global.
- La retraducción enfocada protege el cardinal de un rótulo mediante un marcador ligado a ambos
  idiomas: el modelo recibe una cantidad opaca y Parsezen restaura `SEVEN` como `SIETE`, no como la
  palabra inglesa original ni como otro valor.
- Una duración numérica de un título conserva la concordancia: `30 DAY CHALLENGE` puede traducirse como
  `DESAFÍO DE 30 DÍAS`, pero no como `DESAFÍO DE 30 DÍA`, incluso con énfasis Markdown intermedio.
- Una traducción puede cambiar `*cursiva*` por `_cursiva_`, pero no puede quitar, añadir, reanidar ni
  trasladar a otro bloque cursivas, negritas o tachados Markdown. El primer fallo reintenta y un
  segundo fallo degrada a unidades menores o conserva el fragmento ya validado.
- Las palabras dentro de cursivas o negritas permanecen visibles y traducibles para el modelo mientras
  sus delimitadores se protegen por pares. Un bloque con muchos tramos puede dividirse solo entre
  tramos completos; no deja marcadores temporales, espacios añadidos ni énfasis inventado.
- Si un título parcialmente traducido conserva una única secuencia contigua de al menos dos palabras
  fuente, la reparación final solo la reinserta sin reescribir el resto del título cuando cuenta con
  una equivalencia bilingüe establecida. Una coincidencia múltiple, un nombre propio probable o una
  expresión desconocida se conserva para revisión.
- Un folio nativo de los márgenes superior o inferior no reaparece unido al texto OCR cercano.
- Un folio alterno situado en la banda superior exterior se retira, mientras un número de sección
  centrado en la misma altura se conserva.
- Un folio unido a un encabezado corrido de capítulo se retira como una unidad en extracción nativa
  y OCR tanto si aparece antes como después de la etiqueta, sin eliminar el título de capítulo
  centrado que abre la sección ni un rótulo de figura dentro de la columna.
- Los grupos de al menos dos curvas que forman ilustraciones se conservan como imágenes, incluyen sus
  etiquetas compactas próximas y no duplican recursos incrustados solapados; dos reglas finas aisladas
  no se convierten en una ilustración.
- Un escaneo denso con un rótulo inequívoco de figura numerada conserva una única lámina completa
  junto al texto refluido. Una mención en prosa, un índice o una página decorativa no activa esta
  excepción; si la lámina no puede renderizarse, la página queda señalada para revisión.
- Una composición gráfica de página completa con etiquetas espaciales fragmentadas se conserva como
  lámina y no publica una secuencia lineal de OCR sin sentido; la prosa y las tablas recuperables no
  activan esta excepción.
- Una página dudosa abre la imagen a la izquierda y el texto editable a la derecha.
- Una página con imagen completa y texto útil puede entrar en la auditoría OCR acotada sin que el OCR
  sustituya automáticamente la capa nativa. El presupuesto no supera el 25 % del intervalo ni seis
  páginas; PDFium solo se ejecuta en esas páginas inciertas.
- Una fuente dañada en al menos ocho líneas puede repararse con lecturas PDFium limitadas a la caja de
  cada línea. La propuesta conserva cifras y separadores nativos, rechaza cambios léxicos amplios y
  solo une un límite de palabra cuando la forma completa se repite como evidencia independiente.
- Una discrepancia breve entre capa nativa y OCR usa como máximo dos arbitrajes visuales locales por
  página y ocho por documento. Una ligadura rara se prioriza; la propuesta no puede cambiar átomos
  coincidentes ni inventar cifras, y la ausencia o fallo del modelo visual no bloquea la conversión.
- Los canarios sintéticos conservan índice, columnas, negrita, cursiva y fórmulas. El Markdown es
  idéntico al repetir la conversión con checkpoints de página y sin ellos.
- Una línea PDF con prosa normal y un término interior en negrita, cursiva o ambas conserva solo ese
  tramo como énfasis Markdown. Una normalización posterior de notas no desplaza el tramo y una palabra
  enfatizada partida entre líneas se reúne dentro de un único par de delimitadores.
- Un checkpoint PDF anterior a la incorporación de tramos tipográficos se invalida; uno nuevo los
  serializa, valida y reproduce sin alterar el resultado.
- Los marcadores técnicos no aparecen en el Markdown o EPUB final.
- La publicación EPUB sustituye caracteres prohibidos por XML 1.0 antes de construir el paquete y
  mantiene intactos, byte por byte, todos los recursos binarios.
- Reanudar no repite páginas verificadas.

## Revisiones

- OCR, traducción, texto y estructura avanzan por fases dentro de una única superficie `Revisión
  del documento`; la cola continúa con otros documentos mientras uno espera decisiones.
- Solo aparecen las opciones de la fase actual.
- Original y propuesta son selecciones excluyentes.
- Una unidad nueva preselecciona la recomendación segura sin resolverla: `Siguiente` la confirma,
  mientras cerrar sin interacción la mantiene pendiente.
- El caso común no repite posición, prioridad media ni la necesidad obvia de confirmar; solo una
  prioridad alta o crítica, un aviso, una etiqueta o una sugerencia concreta añaden contexto.
- La elección confirmada se ve en el botón y en el tintado del panel, sin borde turquesa alrededor
  de todo el panel.
- Editar la propuesta la selecciona.
- Restaurar recupera la propuesta inicial.
- `Ir al inicio` y `Restaurar propuesta` permanecen operables desde el menú `…` del panel; no crean
  una segunda fila de botones bajo el contenido.
- En OCR y traducción el origen dice `Solo contexto` y no ofrece botón para seleccionarlo.
- OCR permite `No hay texto que añadir` sin perder recursos ni anclas.
- Aprobar todas las traducciones o correcciones resuelve solo la fase actual; nunca aprueba la
  estructura.
- La acción masiva solo se muestra para dos o más correcciones y `Anterior` solo cuando existe un
  caso o una fase previa; el pie mantiene `Guardar y salir` y una única acción primaria.
- Las propuestas que cambian cifras, fechas, nombres, párrafos o demasiado contenido seleccionan el
  original por defecto y la aprobación masiva no las acepta.
- Un tramo OCR íntegramente en mayúsculas continúa en mayúsculas después de traducirse. La prosa
  mixta sigue las convenciones normales del idioma de destino; en títulos, índices y rótulos de
  tabla se conservan además las mayúsculas iniciales deliberadas de términos alineados, incluso tras
  comas, barras o saltos de línea. Las letras aisladas no activan esta regla.
- Una decisión OCR o de traducción confirmada aparece en el texto enviado al editor y en el EPUB
  publicado. Cambiarla invalida cualquier borrador EPUB anterior; una decisión que no pueda anclarse
  bloquea la publicación en vez de mostrar un éxito falso.
- La pre-organización no convierte un párrafo largo en encabezado aunque conserve todas sus palabras
  en una sola línea.
- Un fallo de validación del reensamblado conserva los fragmentos seguros y restaura solamente los
  incompatibles.
- Una lista que sigue agrupada después de dividir un bloque se degrada de forma transaccional hasta
  líneas independientes. Una viñeta añadida a una etiqueta sin lista se retira, pero los marcadores
  existentes se conservan exactamente.
- Una etiqueta establecida con énfasis Markdown se localiza sin modelo. En una etiqueta breve con
  cifras, solo sus fragmentos alfabéticos se traducen; cifras y separadores nunca llegan a Ollama.
- Una serie astrológica copiada con la forma `planeta in signo + romano` localiza sus componentes
  inequívocos y conserva romano y énfasis, incluso si el modelo ya tradujo solo planeta o signo y aunque
  el romano use énfasis separado. Una etiqueta sin romano solo se localiza cuando abre un rótulo de
  línea/página; usos interiores, código, URL y destinos de enlace permanecen intactos.
- Tres señales astrológicas inequívocas activan el léxico del dominio tanto en tratados de carta natal
  como en libros de decanos; `exaltation` se conserva como `exaltación`, `decan` como `decano` y un
  glosario explícito puede sustituir esas elecciones.
- Un índice XHTML se comprueba por celdas: contenido español o invariante no genera una alarma por el
  envoltorio completo y una celda inglesa intacta sí permanece localizable.
- Si el reintento de IA deja sin traducir un único título, un par Argos directo ya instalado puede
  proponer esa microunidad. No descarga paquetes, no toca el resto del bloque y cualquier residuo o
  fallo de estructura, cifras, idioma o cobertura conserva el título anterior.
- La corrección de una traducción Argos compara el original y el resultado, aplica solo sustituciones
  exactas validadas y conserva una corrección segura aunque otra propuesta del mismo bloque falle.
- Una respuesta JSON truncada recupera exclusivamente objetos completos; ninguna respuesta de
  Ollama puede reserializar texto, cifras, enlaces o párrafos que no formen parte de una sustitución.
- Si una sustitución parcial duplica una secuencia de al menos tres palabras en un título, la
  reparación focalizada reemplaza solo el título completo, elimina la repetición y conserva byline,
  bloques vecinos, sintaxis y valores protegidos.
- Una cita intacta en un tercer idioma no convierte en residual una frase cuya prosa sí se tradujo.
- Si una traducción supera el límite de extractos del informe, una incidencia posterior de idioma,
  alineación o fidelidad desplaza un aviso de menor prioridad; el total y los segmentos revisables no
  disminuyen.
- Un encabezado claramente escrito en un tercer idioma recupera sus palabras originales si el
  modelo las altera, sin perder el nivel estructural de destino.
- Dos cognados incompatibles para un mismo término repetido generan una incidencia de fidelidad y
  no una sustitución automática potencialmente ambigua.
- La anchura de cada tramo del progreso se deriva del número real de unidades de esa fase, no de un
  reparto fijo.
- El progreso global cuenta sesiones materializadas: fases anteriores completas, fase actual parcial
  y fases futuras a cero; a 320 px usa una etiqueta corta sin recorte.
- Guardar y cerrar Parsezen recupera la misma unidad y edición al volver.
- Reabrir una revisión salta las decisiones ya guardadas, empieza en el primer caso pendiente e
  indica por separado cuántas prioridades críticas o altas quedan.
- `Alt+O`, `Alt+P` y `Ctrl+Intro` permiten decidir y avanzar sin abandonar el teclado.
- Aplicar el último cambio publica únicamente ese documento.
- Una fase posterior no aparece completada antes de aprobar su dependencia.
- `Anterior` en la primera unidad pide confirmación, conserva decisiones previas, invalida downstream
  sin aumentar intentos y no relanza OCR, traducción ni IA.
- Los fragmentos revisados sobreviven a la normalización de espacios y finales de línea del editor;
  varias decisiones se anclan sobre una misma instantánea y se aplican sin invalidarse entre sí.
- La cobertura bilingüe cuenta solo bloques con una respuesta de revisión válida; dos respuestas
  inválidas conservan la traducción, no crean un checkpoint de éxito y muestran cero bloques
  verificados.
- El adaptador de revisión LFM conserva su plantilla de razonamiento aprobada; no la cierra ni la
  omite para forzar JSON, y una respuesta estructuralmente válida sigue pasando todas las guardas
  antes de aceptarse.

## Confirmación y editor EPUB

- Todo EPUB muestra primero título, autor, idioma, portada y número de capítulos en una confirmación
  ligera desde la que se puede publicar, guardar o abrir el editor completo.
- El editor completo no se abre en la ruta normal salvo petición explícita o bloqueo.
- Muestra el mismo modelo de libro para origen PDF, Markdown, DOCX y EPUB.
- Renombrar actualiza la navegación.
- `Separar desde aquí` conserva ambas partes desde el párrafo del cursor.
- Unir quita la división sin eliminar contenido.
- Reordenar, anidar y elevar actualizan árbol y orden de lectura.
- Part/Parte/Book/Libro/Volume/Volumen solo agrupan una racha contigua de al menos dos Chapter/
  Capítulo explícitos; los casos ambiguos quedan planos y el spine es preorden con un solo nivel.
- Una cabecera de capítulo repetida en muchas páginas solo abre un capítulo; una entrada de índice de
  nivel tres o más permanece como subsección y no fragmenta el spine.
- Un nivel con más de 128 encabezados automáticos no crea un XHTML por sección: el plan asciende a un
  nivel de corte menos granular, conserva los límites `Parte/Capítulo` inequívocos y mantiene todos
  los encabezados en el cuerpo.
- El EPUB directo enlaza capítulos y subsecciones mediante destinos estables y válidos. Los falsos
  encabezados dentro de código no entran en la navegación y, si existe un índice impreso, un título
  menor no listado permanece visible en el XHTML sin inflar el menú del lector.
- Publicar directamente y preparar el editor desde el mismo Markdown producen igual número y orden
  de documentos, igual selección de destinos y la misma profundidad `Parte → Capítulo → Sección`.
  Los marcadores privados de página, outline e inclusión permanecen hasta finalizar ambos planes y
  no aparecen en el Markdown público ni en ningún XHTML.
- Un outline PDF solo confirma un título con coincidencia única en su página y cifras idénticas. Un
  conjunto suficiente filtra la navegación a los destinos confirmados; coincidencias aisladas no
  ocultan la jerarquía superficial. El holdout de 620 páginas conserva sus seis partes y 72 capítulos
  en 81 documentos de lectura, con 203 destinos y cero enlaces rotos. El tratado técnico de 628
  páginas conserva 66 documentos de lectura y cero enlaces rotos; la profundidad de los destinos
  emparejados coincide en más del 90 % con el outline y en más del 99 % con el índice impreso.
- Una página ornamental `Part/Parte` dividida en varias líneas solo se recompone con un donante único
  del índice o cuando todos sus fragmentos producen una sola lectura. Un marcador `PII` ambiguo se
  conserva, y dos donantes incompatibles no causan ningún cambio.
- Un capítulo numerado de nivel principal confirmado por el índice abre un XHTML aunque siga a una
  página de parte muy breve; las comillas iniciales y una errata OCR menor no impiden el límite si el
  número es idéntico y existe un único candidato. Las menciones repetidas permanecen como listas.
- Un capítulo semántico grande no se corta a los 120 000 caracteres creando una falsa raíz. Un
  `CHAPTER N` seguido de subtítulo conserva un solo nodo combinado en el editor y en el EPUB final.
- Confirmar, guardar, reabrir o publicar desde el editor conserva exactamente la selección de
  subsecciones del constructor: no reincorpora cabeceras repetidas o títulos menores descartados.
  Una indicación privada usada en el borrador no aparece en ningún XHTML del EPUB publicado.
- Dos entradas únicas y consecutivas del índice con niveles padre-hijo anidan sus secciones aunque
  no estén numeradas; una entrada duplicada o un tramo sin correspondencia no crea parentescos.
- Un índice plano o con sangría mixta solo anida entradas cuando al menos dos partes, libros o
  apéndices explícitos delimitan grupos suficientes. Una palabra en negrita dentro de una fila no la
  eleva; una fila completamente destacada solo actúa como raíz cuando toda la tabla es plana.
- Un rótulo estructural sin folio, adyacente en la misma página a las filas paginadas, se conserva
  como raíz de la tabla del índice. El título `Índice` o `Table of Contents` no se absorbe como fila.
- Un encabezado ATX válido pegado a la línea anterior puede abrir un capítulo confirmado por el
  índice sin alterar sus palabras. Si no existe encabezado corporal recuperable, el contenido se
  conserva y no se inventa una entrada navegable.
- Negrita, cursiva, subrayado, encabezados, listas, alineación y enlaces sobreviven a la publicación.
- Un encabezado no se divide internamente entre dos páginas en lectores que respetan las reglas CSS
  de paginación ni desborda horizontalmente ante una palabra excepcionalmente larga.
- Un Markdown mayor que el límite del editor interno aún puede completar análisis semántico,
  capitulación, empaquetado EPUB e integridad; el editor interactivo conserva su límite y explica
  cómo abrir el resultado externamente.
- Una portada generada aparece una sola vez al convertir el EPUB con un lector que materializa la
  portada del paquete; la página XHTML está marcada como portada EPUB 3 y en la guía OPF.
- Las barras de estructura y contenido ocupan una sola fila, usan controles homogéneos y ningún
  control aparece truncado.
- Guardar y volver conserva el libro.
- El resultado supera las pruebas internas de paquete y EPUBCheck.
- Una traducción EPUB agrupada vuelve a traducir una frase breve residual por unidad sin repetir ni
  alterar las unidades ya correctas, y guarda el checkpoint reparado.

## Recuperación

1. Inicia cuatro documentos con configuraciones distintas.
2. Deja uno pendiente de OCR, otro de traducción y otro en la confirmación EPUB; comprueba que los
   demás siguen procesándose.
3. Configura un EPUB Revisado cuya propuesta estructural no cambie el texto y comprueba que aparece
   la confirmación ligera sin forzar el editor completo.
4. Guarda el editor para continuar después, reinicia Parsezen y verifica que conserva estructura,
   metadatos, contenido e imágenes.
5. Genera el EPUB definitivo y comprueba que la fila solo pasa a completada después de que el
   archivo pueda abrirse.
6. Cierra la aplicación.
7. Vuelve a abrirla.

Debe recuperarse:

- orden y configuración;
- estado por fase;
- resultados intermedios cifrados;
- decisiones y ediciones;
- recursos y libro;
- siguiente acción correcta.

Un resultado parcial no debe mostrarse como final. Al completar una revisión, su material temporal
cifrado debe eliminarse.

- Al restaurar una cola mixta, solo los trabajos pendientes de revisión calculan el hash completo;
  un origen ausente sigue visible y ofrece su recuperación local.
- Cada transición durable produce un evento versionado con metadatos técnicos acotados. Un fallo
  incluye fase, estados, revisión de configuración, intento y código; un snapshot incluye su
  generación. La inspección de SQLite no revela texto, prompts ni rutas.
- Una escritura diferida o reintentada no pierde transiciones, y retirar un trabajo no deja eventos
  pendientes que bloqueen la persistencia de la cola.

## IA local

- Sin Ollama, aparece la instalación guiada.
- Con Ollama detenido, `Iniciar` lo pone disponible.
- Sin modo solo local, se exige proteger y reiniciar.
- `OLLAMA_NO_CLOUD` en el proceso de Parsezen no acredita por sí solo un servidor activo; falta de
  `server.json` protegido mantiene bloqueado el envío de contenido.
- `IA local`, desde Ajustes o un aviso contextual, abre una vista fija con exactamente las filas
  `Traducción IA` y `Revisión IA`, aunque la detección aún no haya terminado.
- Cada fila muestra solo `Preparado`, `Descargable` o `Equipo insuficiente`; no hay búsqueda,
  selector de tags, endpoint, recomendación general ni borrado arbitrario.
- La vista no lee ni escribe documentos. La comprobación de catálogo solo usa la API local de Ollama
  (`/api/version`, `/api/tags` y `/api/show`) y no conserva el contenido de sus respuestas.
- La vista consulta automáticamente al abrirse. Pulsar `Comprobar de nuevo` emite otra petición de
  refresco; pulsar `Descargar componente` emite
  únicamente `translation` o `review`, sin iniciar una descarga genérica.
- Un componente solo pasa a `Preparado` cuando su manifest y digest coinciden con `/api/tags` y
  `/api/show`; los tags cloud y los ausentes no se aceptan como seleccionables.
- `Preparado` acredita instalación, identidad y contrato local, no calidad semántica universal ni
  permiso para aprobar propuestas sin revisión.

`Validar con IA real.cmd` añade un recorrido optativo. No se publica su informe local. Una ejecución
que genera un archivo válido pero conserva fragmentos, incidencias de idioma o avisos PDF debe
mostrar `REVISAR`, no `OK`.

- `--translation-engine local_ai --profile critical` recorre traducción, corrección y estructura con
  los componentes especializados fijados para cada fase; ninguno sustituye silenciosamente a otro.
- `--translation-engine local_ai --profile translation` aísla la traducción y sigue pasando
  `AppSettings` al procesador aunque no haya revisión posterior.
- `--profile review` compara procesamiento directo y revisión semántica; `--profile
  translation-review` conserva la misma traducción base en ambos brazos, añade el revisor configurado
  solo al brazo revisado y registra sus pasadas previstas sin texto documental.
- El informe comparativo no contiene nombres, rutas, prompts, respuestas ni texto documental.

Para un corpus privado largo, registra primero al menos dos ejecuciones del mismo intervalo con
`scripts/benchmark_documents.py record --pages INICIO FIN --runs 2` y comprueba después el
manifiesto con `check`. La referencia solo es válida si todas las repeticiones producen exactamente
la misma estructura; el límite de recursos parte del peor caso medido. El recorrido real debe usar
el mismo intervalo y terminar con EPUBCheck 5.3.0 sin errores nuevos.

## Distribución

- Recursos generados coinciden con el manifiesto de branding.
- El ejecutable usa el símbolo, y la cabecera/README el logo completo.
- Metadatos, accesos directos e instalador dicen `Parsezen`.
- No quedan nombres, módulos, URLs ni recursos de identidades anteriores.
- El paquete x64 arranca en un perfil limpio de Windows.
- Desinstalar no elimina documentos del usuario.
- Una actualización elimina el runtime `_internal` anterior, conserva el `AppId` y no toca estado,
  originales ni resultados.
- La etiqueta debe pertenecer a `main`; el workflow audita el lock CPU y crea una Release en borrador
  con instalador, SHA-256 e inventario generados en esa misma ejecución.
- Una etiqueta pública genera una atestación de procedencia; la documentación explica que el
  instalador no tiene firma Authenticode y Windows puede mostrar `Editor desconocido`.

La construcción del instalador se ejecuta solo cuando se solicita explícitamente o al preparar una
release.
