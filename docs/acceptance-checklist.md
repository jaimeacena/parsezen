# Aceptación antes de publicar Parsezen

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
  tarjetas visuales exclusivas, Revisión con IA un interruptor, y Traducir, Traductor, Glosario,
  Páginas y OCR una fila `Etiqueta — Valor — ›`. No hay segmentos, encabezados ni pie, y se ve entera
  sin scroll al tamaño normal.
- El bloque de configuración está centrado horizontalmente; los menús de cada fila aparecen bajo su
  valor, alineados a la derecha, y no saltan al margen izquierdo de la ventana.
- Los documentos nuevos parten con revisión completa activada. Una configuración ya guardada conserva
  su plan. Argos y OCR automático siguen siendo valores iniciales sin preguntas técnicas.
- Revisión completa con IA local activa una pasada proactiva de texto y, solo en EPUB, también de
  estructura; no existen interruptores independientes ni un resumen técnico del recorrido en esta
  página.
- La IA local muestra un estado breve y deja el modelo general heredado en la ayuda contextual, sin
  excepciones por documento. Si falta, la acción principal abre su configuración.
- Cada elección válida se aplica y persiste inmediatamente. No hay botones Cancelar o Crear/Guardar;
  Volver y Escape cierran sin confirmación porque no existen cambios pendientes.
- `Traducir — No traducir` oculta Traductor y Glosario. Al elegir cualquier idioma aparecen ambas
  filas; Argos es el motor inicial y puede sustituirse por IA local. No aparecen proveedores ni
  direcciones configurables.
- Elegir traducción con IA o revisión semántica exige un modelo instalado anunciado por Ollama
  `/api/tags`; Argos directo no exige modelo. Argos con revisión usa Ollama después de traducir.
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
- Un fallo de IA abre su gestor; uno de destino prioriza revisar la salida; uno de integridad
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
  descontó tiempo o lo recalculó con el ritmo real; al superar el máximo inicial deja de mostrar una
  cuenta atrás precisa.
- Al terminar un documento, `Ver resumen` distingue integridad técnica, incidencias detectadas y
  revisión manual, sin presentar la ausencia de señales como equivalencia semántica.
- El cierre del lote cuenta documentos listos, pendientes de revisión, fallidos, pausados y
  cancelados; ofrece actividad cuando existen resultados, errores o cancelaciones consultables.
- Los avisos largos crecen sin recortar texto; una pausa y una interrupción usan copias distintas. Al
  retirar trabajos, el aviso se recalcula con los supervivientes y desaparece con el último.
- Las notificaciones de Windows aparecen al terminar o requerir atención solo con Parsezen
  minimizado o inactivo.
- Actividad reciente conserva como máximo 20 intentos, no contiene texto documental, puede abrir
  resultado o carpeta y se borra sin eliminar ningún documento.

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
  entrada de su misma fila, conserva el orden multicolumna y genera elementos de lista independientes;
  una columna numérica ambigua o una tabla ordinaria permanece intacta.
- Un encabezado de sección sin folio queda fuera de la lista anterior y no se fusiona con su última
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
- Una tabla PDF con celdas multilínea se publica como XHTML semántico y conserva los saltos internos;
  sus etiquetas no aparecen impresas como texto. Una tabla HTML con atributos, scripts o estructura
  ajena al generador permanece inerte.
- Una revisión que elimina una celda HTML vacía, su columna o un salto interno se rechaza aunque otra
  tabla adquiera casualmente la forma perdida y los recuentos globales sigan coincidiendo.
- Una tabla escaneada solo usa una estructura OCR si conserva cobertura, proporción y todas las
  cifras, filas y columnas en el mismo orden; una salida inflada o que omite una columna vacía
  mantiene la capa nativa y su orden multicolumna.
- El texto con tracking artificial recupera palabras normales sin eliminar espacios léxicos.
- Una palabra dividida al final de una página se publica unida si continúa en minúscula en la
  siguiente, sin perder el marcador interno durante la revisión.
- Un folio nativo de los márgenes superior o inferior no reaparece unido al texto OCR cercano.
- Un folio alterno situado en la banda superior exterior se retira, mientras un número de sección
  centrado en la misma altura se conserva.
- Un folio unido a un encabezado corrido de capítulo se retira como una unidad en extracción nativa
  y OCR tanto si aparece antes como después de la etiqueta, sin eliminar el título de capítulo
  centrado que abre la sección ni un rótulo de figura dentro de la columna.
- Los grupos de curvas que forman ilustraciones se conservan como imágenes y no duplican recursos
  incrustados solapados.
- Una página dudosa abre la imagen a la izquierda y el texto editable a la derecha.
- Los marcadores técnicos no aparecen en el Markdown o EPUB final.
- La publicación EPUB sustituye caracteres prohibidos por XML 1.0 antes de construir el paquete y
  mantiene intactos, byte por byte, todos los recursos binarios.
- Reanudar no repite páginas verificadas.

## Revisiones

- OCR, traducción, texto y estructura avanzan por fases dentro de una única superficie `Revisión
  del documento`; la cola continúa con otros documentos mientras uno espera decisiones.
- Solo aparecen las opciones de la fase actual.
- Original y propuesta son selecciones excluyentes.
- Una unidad nueva no marca ninguna elección: la recomendación se muestra como provisional.
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
- La pre-organización no convierte un párrafo largo en encabezado aunque conserve todas sus palabras
  en una sola línea.
- Un fallo de validación del reensamblado conserva los fragmentos seguros y restaura solamente los
  incompatibles.
- La corrección de una traducción Argos compara el original y el resultado, aplica solo sustituciones
  exactas validadas y conserva una corrección segura aunque otra propuesta del mismo bloque falle.
- Una respuesta JSON truncada recupera exclusivamente objetos completos; ninguna respuesta de
  Ollama puede reserializar texto, cifras, enlaces o párrafos que no formen parte de una sustitución.
- Si una sustitución parcial duplica una secuencia de al menos tres palabras en un título, la
  reparación focalizada reemplaza solo el título completo, elimina la repetición y conserva byline,
  bloques vecinos, sintaxis y valores protegidos.
- Una cita intacta en un tercer idioma no convierte en residual una frase cuya prosa sí se tradujo.
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
- Negrita, cursiva, subrayado, encabezados, listas, alineación y enlaces sobreviven a la publicación.
- Un encabezado no se divide internamente entre dos páginas en lectores que respetan las reglas CSS
  de paginación ni desborda horizontalmente ante una palabra excepcionalmente larga.
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

## IA local

- Sin Ollama, aparece la instalación guiada.
- Con Ollama detenido, `Iniciar` lo pone disponible.
- Sin modo solo local, se exige proteger y reiniciar.
- `OLLAMA_NO_CLOUD` en el proceso de Parsezen no acredita por sí solo un servidor activo; falta de
  `server.json` protegido mantiene bloqueado el envío de contenido.
- Sin modelos, el gestor mantiene recomendación y entrada manual.
- `IA local` abre el gestor aunque la detección aún no haya terminado.
- Estado de Ollama, lista única, contexto e instalación manual se reúnen en esa página.
- El gestor muestra `Instalados` y `Añadir modelo`; búsqueda, instalación por nombre, recomendaciones
  y catálogo aparecen dentro de Añadir modelo.
- Cambiar el predeterminado actualiza solo los trabajos pendientes heredados. Una elección
  específica o un trabajo iniciado no se modifica.
- El gestor indica cuántos trabajos heredan el predeterminado y bloquea la eliminación de modelos
  usados por trabajos sin terminar.
- Los tres recomendados tienen roles distintos y caben en el equipo.
- `llmfit` solo se activa si versión, arquitectura y SHA-256 están fijados por Parsezen; su proceso no
  recibe claves de API, credenciales ni proxies heredados.
- Instalar muestra progreso y permite cancelar.
- Un identificador canónico queda seleccionado solo tras aparecer en `/api/tags`.
- Los modelos cloud nunca aparecen.
- El contexto elegido se envía realmente.

`Validar con IA real.cmd` añade un recorrido optativo. No se publica su informe local. Una ejecución
que genera un archivo válido pero conserva fragmentos, incidencias de idioma o avisos PDF debe
mostrar `REVISAR`, no `OK`.

- `--translation-engine local_ai --profile critical` recorre traducción, corrección y estructura con
  el mismo modelo instalado.
- `--translation-engine local_ai --profile translation` aísla la traducción y sigue pasando
  `AppSettings` al procesador aunque no haya revisión posterior.
- `--profile review` compara procesamiento directo y revisión semántica; `--profile
  translation-review` compara traducción directa y revisada con el mismo motor y registra sus
  pasadas previstas sin texto documental.
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
