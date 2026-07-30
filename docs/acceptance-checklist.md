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
- La pantalla usa exactamente Documento, Flujo, Salida y Siguiente paso, en ese orden, además de
  tracks sin encabezado para reordenar y retirar.
- Un documento nuevo muestra `Sin salida` y ofrece `Configurar` en Siguiente paso; todavía no es
  ejecutable ni cuenta como revisión.
- `Configurar` abre una página maestro-detalle con el plan efectivo, navegación persistente en
  escritorio y lista-detalle en compacto. Traducir y Corregir tienen un único interruptor;
  Personalizar aparece solo para EPUB y su único interruptor controla la pre-organización de
  capítulos.
- `IA local` aparece dentro de Configurar solo cuando el plan la necesita, identifica qué
  operaciones comparten el perfil y permite heredar el predeterminado o elegir una excepción.
- La flecha de vuelta guarda de forma atómica una configuración válida; si existe un error de
  validación, permanece en la página y lo muestra sin descartar cambios.
- Apagar Traducir o Corregir inhabilita todas sus opciones. Título, autor, idioma y portada se
  modifican exclusivamente en el editor final EPUB.
- El método de traducción se elige mediante un selector binario y el glosario integrado permite
  añadir o retirar filas sin abrir otra ventana.
- Si el contenido ya está en el idioma de destino con suficiente confianza, no se invoca ningún
  traductor ni aparece una fase de traducción ficticia.
- Los flujos de mismo formato exigen una transformación útil, salvo EPUB→EPUB: la personalización
  final es por sí misma una operación válida.
- Todo resultado EPUB abre el editor final y permite cambiar título, autor, idioma y portada con
  independencia del formato de origen.
- Desde la preparación inmutable de la ejecución hasta que terminan validación, lectura, conversión,
  OCR y recursos, la fila muestra `Preparando` sin estados visuales intermedios.
- El botón Destino muestra la ruta global heredada y permite elegir otra directamente; una excepción
  por documento sobrevive al reinicio y no cambia al modificar el destino general.
- Aplicar a documentos del mismo formato sólo afecta a trabajos compatibles y conserva sus rangos
  de páginas.
- Los elementos accionables muestran respuesta de hover y cursor de enlace; el asa de ordenación
  muestra cursor de arrastre.
- Cada fila conserva opciones independientes.
- Reordenar cambia el orden real; retirar una fila no afecta a las demás.
- Cada fila muestra una papelera vectorial que solo la retira de la cola.
- Un documento pausado o pendiente de revisión se puede retirar tras confirmar; se descarta el
  checkpoint o la revisión sin tocar el original.
- Configuración, modelos, glosario, revisiones y editor EPUB permanecen dentro de la ventana
  principal y vuelven a la página que los abrió.
- Los controles son compactos y los desplegables tienen un chevrón visible; la rueda desplaza la
  página sin cambiar accidentalmente una selección.
- Los botones y secciones indican foco o selección mediante fondo, sin contornos añadidos ni estados
  de ratón que permanezcan marcados después de cerrar su menú.
- `Procesar solo un intervalo` usa el mismo interruptor que el resto de ajustes y conserva visibles
  los campos de páginas solo cuando está activado.
- `Aplicar misma configuración al resto de documentos` muestra el texto completo, queda separado
  de las secciones del flujo mediante un divisor sutil y mantiene las excepciones de rango y OCR.
- Al empezar, las opciones del trabajo quedan bloqueadas.
- Solo una tarea pesada figura como activa.
- Un error o una revisión pendiente permite avanzar al siguiente documento. La fase fallida ofrece
  `Ver error`, con el mensaje concreto conservado por el trabajo y acciones acordes a su causa.
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
- El preanálisis no guarda nombres, rutas ni contenido y solo pide confirmación adicional para un
  plan largo o con riesgo alto; la revisión manual se explica aparte del tiempo automático.
- Tras diez segundos de progreso medible, la fila puede actualizar el tiempo restante y explica si
  descontó tiempo o lo recalculó con el ritmo real; al superar el máximo inicial deja de mostrar una
  cuenta atrás precisa.
- Al terminar un documento, `Ver resumen` distingue integridad técnica, incidencias detectadas y
  revisión manual, sin presentar la ausencia de señales como equivalencia semántica.
- El cierre del lote cuenta documentos listos, pendientes de revisión, fallidos, pausados y
  cancelados; ofrece actividad cuando existen resultados, errores o cancelaciones consultables.
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
- El progreso avanza por páginas durante extracción y OCR.
- Una página con OCR vacío no hace fallar el documento completo.
- Reanudar después de un OCR correcto pero vacío no vuelve a reconocer esa página.
- Un fallo del motor OCR no se confunde con un resultado vacío y se vuelve a intentar al reanudar.
- Una página a dos columnas conserva primero la columna izquierda y después la derecha, sin desplazar
  títulos o separadores de ancho completo.
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
- Reanudar no repite páginas verificadas.

## Revisiones

- Traducción, corrección, OCR y estructura se revisan por separado.
- Solo aparecen las opciones de la fase actual.
- Original y propuesta son selecciones excluyentes.
- La propuesta aparece seleccionada por defecto al avanzar a una unidad nueva.
- Editar la propuesta la selecciona.
- Restaurar recupera la propuesta inicial.
- Aprobar todas las traducciones o correcciones resuelve solo la fase actual; nunca aprueba la
  estructura.
- Las propuestas que cambian cifras, fechas, nombres, párrafos o demasiado contenido seleccionan el
  original por defecto y la aprobación masiva no las acepta.
- La pre-organización no convierte un párrafo largo en encabezado aunque conserve todas sus palabras
  en una sola línea.
- Un fallo de validación del reensamblado conserva los fragmentos seguros y restaura solamente los
  incompatibles.
- Una cita intacta en un tercer idioma no convierte en residual una frase cuya prosa sí se tradujo.
- Un encabezado claramente escrito en un tercer idioma recupera sus palabras originales si el
  modelo las altera, sin perder el nivel estructural de destino.
- Dos cognados incompatibles para un mismo término repetido generan una incidencia de fidelidad y
  no una sustitución automática potencialmente ambigua.
- La anchura de cada tramo del progreso se deriva del número real de unidades de esa fase, no de un
  reparto fijo.
- Guardar y cerrar Parsezen recupera la misma unidad y edición al volver.
- Reabrir una revisión salta las decisiones ya guardadas, empieza en el primer caso pendiente e
  indica por separado cuántas prioridades críticas o altas quedan.
- `Alt+O`, `Alt+P` y `Ctrl+Intro` permiten decidir y avanzar sin abandonar el teclado.
- Aplicar el último cambio publica únicamente ese documento.
- Una fase posterior no aparece completada antes de aprobar su dependencia.
- Los fragmentos revisados sobreviven a la normalización de espacios y finales de línea del editor;
  varias decisiones se anclan sobre una misma instantánea y se aplican sin invalidarse entre sí.

## Editor EPUB

- Muestra el mismo modelo de libro para origen PDF, Markdown, DOCX y EPUB.
- Renombrar actualiza la navegación.
- `Separar desde aquí` conserva ambas partes desde el párrafo del cursor.
- Unir quita la división sin eliminar contenido.
- Reordenar, anidar y elevar actualizan árbol y orden de lectura.
- Negrita, cursiva, subrayado, encabezados, listas, alineación y enlaces sobreviven a la publicación.
- Las barras de estructura y contenido ocupan una sola fila, usan controles homogéneos y ningún
  control aparece truncado.
- Guardar y volver conserva el libro.
- El resultado supera las pruebas internas de paquete y EPUBCheck.
- Una traducción EPUB agrupada vuelve a traducir una frase breve residual por unidad sin repetir ni
  alterar las unidades ya correctas, y guarda el checkpoint reparado.

## Recuperación

1. Inicia cuatro documentos con configuraciones distintas.
2. Deja uno pendiente de OCR, otro de traducción y otro en el editor EPUB.
3. Configura un EPUB con revisión estructural cuya propuesta automática no cambie el texto y
   comprueba que el editor se abre igualmente.
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
- Instalar muestra progreso y permite cancelar.
- Un identificador canónico queda seleccionado solo tras aparecer en `/api/tags`.
- Los modelos cloud nunca aparecen.
- El contexto elegido se envía realmente.

`Validar con IA real.cmd` añade un recorrido optativo. No se publica su informe local. Una ejecución
que genera un archivo válido pero conserva fragmentos, incidencias de idioma o avisos PDF debe
mostrar `REVISAR`, no `OK`.

## Distribución

- Recursos generados coinciden con el manifiesto de branding.
- El ejecutable usa el símbolo, y la cabecera/README el logo completo.
- Metadatos, accesos directos e instalador dicen `Parsezen`.
- No quedan nombres, módulos, URLs ni recursos de identidades anteriores.
- El paquete x64 arranca en un perfil limpio de Windows.
- Desinstalar no elimina documentos del usuario.
- Una actualización elimina el runtime `_internal` anterior, conserva el `AppId` y no toca estado,
  originales ni resultados.
- La Release adjunta instalador, SHA-256 e inventario de dependencias generados por el mismo workflow.
- Una etiqueta pública genera una atestación de procedencia; la documentación explica que el
  instalador no tiene firma Authenticode y Windows puede mostrar `Editor desconocido`.

La construcción del instalador se ejecuta solo cuando se solicita explícitamente o al preparar una
release.
