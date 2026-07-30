# Guía de uso de Parsezen

Esta guía describe Parsezen 1.1.0.

## La pantalla principal

Cada documento ocupa una fila. Las columnas muestran el recorrido real:

- **Documento**: origen, tamaño y páginas PDF.
- **Flujo**: traducción, corrección y personalización activadas.
- **Salida**: archivo final y su destino.
- **Siguiente paso**: fase actual, avance y acción necesaria.

Al añadir un documento, **Salida** pide la configuración imprescindible. La página interna muestra
arriba el plan efectivo y, a la izquierda, Resultado, Traducir, Corregir y, cuando la salida es EPUB,
Personalizar. Sus opciones se abren en el panel derecho sin abandonar el documento. Traducir y
Corregir llevan el interruptor en la propia navegación; dentro de Personalizar, el interruptor
activa únicamente la pre-organización de capítulos. Si el plan usa Ollama aparece además `IA local`,
con el estado, las operaciones que lo comparten y la elección heredada o específica. Parsezen valida
el conjunto al volver a la cola, lo guarda automáticamente
y evita dependencias circulares o combinaciones sin efecto. Si falta una decisión imprescindible,
explica cuál es y mantiene abierta la configuración; las opciones de una operación apagada quedan
inhabilitadas y no bloquean el procesamiento.

Cada acción se encuentra en la celda a la que pertenece: configurar el flujo o la salida, revisar,
ver un error o abrir el archivo. La pantalla principal no añade un inspector lateral ni repite la
información de la fila.

`Destino:` en la cabecera define el destino general y lo recuerda. Cada documento lo hereda y lo
muestra en el botón `Destino` de Resultado. Pulsa ese botón para elegir directamente otra carpeta
solo para ese documento. Las excepciones no cambian cuando se modifica el destino global.

Cuando una fila termina, pulsa `Abrir resultado` en **Siguiente paso**. Su menú secundario permite abrir
el archivo o su carpeta concreta. Las fases omitidas no ocupan columnas ni ofrecen acciones
imposibles.

## Añadir y ordenar documentos

- Arrastra TXT, Markdown, DOCX, PDF o EPUB a la zona discontinua, situada justo debajo de la
  última fila, o pulsa `examínalos` para seleccionarlos. La cola crece hasta seis filas
  visibles antes de usar desplazamiento.
- Arrastra una fila para cambiar el orden.
- La configuración, las acciones de estado y el resultado responden al pasar el puntero; el asa de
  ordenación muestra un cursor de arrastre.
- Usa la papelera del extremo derecho para retirar esa fila de la cola. En un documento pausado o
  pendiente de revisión, Parsezen pide confirmación antes de descartar su progreso o revisión; el
  archivo original nunca se modifica.
- Usa `···` o la tecla de menú para retirar un trabajo pendiente.
- En un PDF, elige todas las páginas o un intervalo inclusivo.

La configuración queda bloqueada al empezar para evitar que el resultado deje de corresponderse con
lo mostrado.

El botón contextual de la cabecera utiliza exclusivamente la configuración guardada de cada fila. Si un
documento no puede arrancar, Parsezen muestra el motivo en la interfaz actual en lugar de dejar el
fallo en segundo plano.

La interfaz sigue por defecto la apariencia de Windows. El botón vectorial de la cabecera abre las
opciones **Seguir el sistema**, **Claro** y **Oscuro**; la elección se aplica sin recargar y se
conserva para la siguiente sesión.

Con una ventana estrecha o zoom alto, la cabecera y la cola se reorganizan en varias filas. No se
eliminan operaciones: el flujo se integra en Documento, las acciones permanecen en su fila y las
comparaciones pasan a orientación vertical.

## Elegir un resultado

### Markdown

Adecuado para texto estructurado y Obsidian. Si activas imágenes, elige una carpeta estable cuando
vayas a mover el Markdown o a integrarlo en una bóveda. Esta es la única salida que solicita una
carpeta de recursos externa.

### EPUB

Adecuado para lectores electrónicos. Puedes:

- incluir o excluir imágenes;
- integrar las imágenes dentro del propio EPUB, sin elegir otra carpeta;
- conservar los estilos compatibles del paquete cuando el origen también es EPUB;
- cambiar título, autor, idioma y portada en el editor final con cualquier formato de origen;
- revisar los capítulos y el contenido antes de generar el libro definitivo.

### TXT y DOCX

TXT→TXT y DOCX→DOCX están disponibles cuando se aplica traducción o corrección. Word conserva el
paquete original y sus recursos compatibles. Las imágenes de Word se integran dentro del DOCX; una
traducción de distinta longitud puede cambiar qué fragmento hereda un estilo dentro de un párrafo.

## Traducir

Hay dos métodos:

- **Algoritmo local**: Argos Translate, offline y sin modelo conversacional.
- **IA local**: Ollama, útil cuando importa más la fluidez.

Selecciona el idioma y, si lo necesitas, añade o elimina términos en el glosario integrado en la
misma pantalla. La traducción ocurre antes de la corrección para que revises el texto en el idioma
final.

Si Parsezen puede determinar con suficiente confianza que el contenido ya está en el idioma de
destino, omite la traducción. En EPUB usa primero el idioma declarado por el propio libro. En ese
caso no muestra una fase de traducción ni genera un informe de calidad ficticio.

La IA trabaja en fragmentos verificables. Si una propuesta pierde contenido, cambia valores
protegidos o no parece estar en el idioma solicitado, Parsezen la divide y vuelve a intentar solo
las partes necesarias. Cuando ninguna alternativa es segura, conserva ese fragmento original y lo
incluye en la revisión en vez de publicar silenciosamente una transformación dudosa. El informe de
traducción corresponde a la traducción en sí, no a cambios de bloques introducidos después por
Corregir o Personalizar. En títulos, conserva localmente la sintaxis estructural mientras traduce
solo las palabras; los nombres editoriales se mantienen como nombres propios. Una cita en otro
idioma puede permanecer intacta si el resto de su frase se ha traducido. Si un encabezado pertenece
claramente a un tercer idioma y el modelo lo deforma, Parsezen recupera automáticamente el texto
original sin deshacer su nivel. Si un mismo término aparece traducido de formas parecidas pero
incompatibles, lo muestra en la revisión en lugar de elegir una por semejanza.

Antes de traducir, Parsezen construye una memoria terminológica conservadora para todo el
documento. Protege firmas atribuidas, organizaciones y nombres propios que se repiten; el glosario
manual mantiene siempre la prioridad. La detección no trata una frase con mayúsculas, un número
romano ni un término demasiado frecuente como nombre propio solo por repetirse, por lo que los
títulos de obras siguen pudiendo traducirse. Una atribución de autor debe aparecer como tal en su
propia línea; una palabra equivalente a «por» dentro de la prosa no protege etiquetas técnicas.

`IA local`, en la cabecera, abre una única página para instalar o iniciar Ollama. `Instalados` es la
vista inicial cuando ya existen modelos y permite elegir el predeterminado. `Añadir modelo` reúne la
búsqueda por nombre, el enlace al catálogo y las recomendaciones apropiadas para el equipo. La
misma página permite ajustar el contexto y explica cuántos trabajos pendientes heredan sus valores.

Un documento usa el predeterminado salvo que elijas `Usar otro modelo` en su sección contextual.
Los cambios globales solo actualizan trabajos pendientes que continúan heredando; las elecciones
específicas y cualquier trabajo ya iniciado permanecen estables. Parsezen impide eliminar un modelo
del que todavía depende un trabajo sin terminar.

## Corregir errores y ruido

`Corregir errores y ruido` prepara una propuesta conservadora:

- corrige errores gramaticales o de conversión;
- retira ruido evidente;
- no debe resumir, reordenar ni inventar contenido;
- deja cada diferencia para revisión cuando así se haya configurado.

Las propuestas que añaden o eliminan bloques, cambian cifras, fechas, nombres propios, siglas,
párrafos o una parte sustancial del texto se consideran arriesgadas y conservan el original por
defecto. La aprobación masiva aplica las correcciones conservadoras, pero no sustituye esos
originales arriesgados. Si varios fragmentos son válidos por separado pero su combinación no lo es,
Parsezen conserva los fragmentos que siguen siendo seguros en lugar de descartar toda la propuesta.

Con **IA local**, si Traducir y Corregir están activados, ambas tareas se realizan en una única
transformación validada. Después solo se corrigen de nuevo los bloques pertenecientes a páginas que
el análisis PDF/OCR marcó como dudosas o que contienen daño de conversión inequívoco. Con el
algoritmo offline, Corregir continúa siendo una pasada separada.

## Personalizar

La pestaña aparece únicamente cuando el resultado es EPUB. Su único interruptor permite
**pre-organizar capítulos y jerarquías** antes de abrir el editor.

Todo EPUB recibe la estructura técnica mínima necesaria para ser válido. La organización de capítulos
no borra contenido. Todo resultado EPUB abre el editor final, aunque no se haya activado esa
pre-organización. El desplegable **Metadatos** reúne título, autor, idioma y portada. El resto del
editor permite separar una división desde el cursor, unirla con la anterior sin borrar texto,
reordenar jerarquías y editar encabezados, énfasis, listas, alineación y enlaces.

EPUB→EPUB puede abrir directamente Personalizar sin exigir traducción ni corrección. Al publicar
desde el editor, Parsezen reconstruye un libro normalizado: conserva el contenido y los recursos
seleccionados, pero puede simplificar estilos complejos.

La planificación inicial distingue portada editorial, preliminares, índice y cuerpo. Para decidir
los capítulos combina los títulos encontrados en el índice con los niveles derivados de la
geometría del PDF; el índice no se confunde con un capítulo del cuerpo.

## Procesamiento y cola

Parsezen ejecuta una sola tarea pesada cada vez. La cabecera reúne el total, las revisiones reales y
la acción global disponible. Antes de empezar, la cola muestra un intervalo aproximado para el
trabajo automático. Esta estimación mejora con ejecuciones similares realizadas en el equipo y no
guarda nombres, rutas ni contenido. Los planes largos o con una decisión de riesgo alto muestran un
resumen final de carga, revisiones previstas y aspectos que conviene comprobar.

En PDFs de 120 páginas o más —o desde 60 cuando el flujo incluye OCR forzado, IA local o EPUB—,
Parsezen comprueba primero la primera página, una central y la última del intervalo solicitado. Las
tres pasan por el mismo flujo real y sus salidas de muestra se eliminan. Si las señales materiales se
repiten en al menos dos páginas, el trabajo completo no comienza y la fila explica qué configuración
conviene revisar. Si la muestra es segura, continúa automáticamente. La extracción y el OCR válidos
de esas páginas se reutilizan al procesar el documento completo; cambiar el archivo o su
configuración obliga a comprobarlo otra vez.

La fila activa conserva el documento, la fase y el progreso medible. Tras diez segundos con progreso
cuantificable, el tiempo restante puede ajustarse al ritmo real de esa fase. Su ayuda explica si solo
se descontó el tiempo transcurrido o si la observación amplió el intervalo. Si se supera el máximo
inicial, muestra `Más tiempo del previsto` en lugar de fingir una precisión que ya no existe.

Desde que Parsezen fija y valida la ejecución, y mientras lee, convierte, aplica OCR o prepara
recursos, la fila activa muestra siempre `Preparando`. Después, **Siguiente paso** muestra
`Traduciendo`, `Corrigiendo`, `Personalizando` o `Publicando` según la fase real, además del porcentaje
cuando existe una medida. Si un documento necesita
revisión, su fase muestra `Revisar` y la cola continúa con otro. Un error también queda aislado; la
celda muestra `Ver error` y abre su explicación completa sin perder los resultados anteriores.
Según la causa, ofrece `Reintentar esta fase`, `Revisar configuración`, `Revisar destino` o
`Abrir IA local`. El reintento actúa solo sobre ese documento y conserva las fases, decisiones y
checkpoints ya válidos.

Antes de exponer un resultado, Parsezen vuelve a comprobar el archivo temporal. En texto verifica
codificación, contenido y estructura; en DOCX y EPUB comprueba además que el paquete sea válido e
idéntico al ya aprobado. Una fila completada muestra `Integridad final comprobada`; su ayuda
enumera los controles y un inventario sin contenido documental. Si el control detecta una
diferencia, no sustituye el resultado anterior.

Al terminar, `Ver resumen` separa tres conceptos que no deben confundirse:

- **Integridad técnica**: el archivo definitivo existe y superó las comprobaciones del formato.
- **Incidencias detectadas**: señales objetivas encontradas durante OCR, conversión o traducción,
  incluso si después se resolvieron.
- **Revisión manual**: decisiones que una persona realizó o que todavía se esperan.

La cabecera resume también el lote completo. Si Parsezen está minimizado o en segundo plano, Windows
muestra un aviso al terminar o al requerir atención; no duplica ese aviso mientras la ventana está
activa.

`Pausar` solicita detenerse en el siguiente punto seguro. Al volver, Parsezen usa los checkpoints
cifrados en lugar de repetir trabajo válido.

En **Ajustes → Conservar trabajo temporal** puedes elegir `No conservar al finalizar`, 7, 30 o
90 días. Los checkpoints siguen ligados al documento y a su configuración exacta, están cifrados
para la cuenta actual de Windows y no contienen telemetría con texto. `Borrar caché` los elimina de
forma inmediata.

**Ajustes → Actividad reciente** conserva como máximo 20 intentos terminados o fallidos para poder
volver a abrir su resumen, resultado o carpeta. Es una lista privada y acotada, no una biblioteca:
guarda rutas, estado, fecha y conteos técnicos, pero no texto documental. `Borrar actividad` elimina
solo ese historial; nunca elimina originales ni resultados.

## Revisar

La revisión se divide por fase. No se muestran pestañas que no correspondan al problema actual.

1. Compara el original de la izquierda con la propuesta de la derecha.
2. Marca una versión. Si editas la derecha, se selecciona automáticamente.
3. Usa `Restaurar propuesta` si quieres descartar tu edición.
4. Pulsa `Siguiente`.
5. En el último elemento, pulsa `Aplicar todos los cambios`.

La propuesta aparece seleccionada al abrir cada caso. Si confías en todas las propuestas de la fase
actual, `Aprobar todas las traducciones` o `Aprobar todas las correcciones` resuelve únicamente esa
fase sin recorrerla caso por caso; nunca aprueba la estructura. La barra superior distribuye el
ancho de cada color según el número real de elementos de OCR, traducción, corrección y estructura, y
muestra el avance total de la secuencia.

Dentro de cada fase, Parsezen presenta primero las incidencias críticas y altas, mantiene estable el
orden entre elementos de la misma gravedad y muestra esa prioridad sobre la comparación. El orden
entre fases continúa siendo OCR, traducción, corrección y estructura para respetar sus dependencias.
La cabecera indica cuántas decisiones y prioridades altas quedan. Si guardaste una revisión a
medias, se abre directamente el primer caso pendiente; las decisiones ya tomadas siguen disponibles
al volver con `Anterior`, pero no se recorren de nuevo de forma obligatoria. `Alt+O` conserva el
original, `Alt+P` elige la propuesta y `Ctrl+Intro` guarda la decisión y avanza.

Para OCR, la izquierda muestra la página y no puede elegirse como texto. Corrige o acepta el texto de
la derecha.

`Guardar y continuar después` cifra el material de la revisión, incluidos recursos y ediciones. Al
abrir de nuevo Parsezen, la misma fila vuelve a `Revisar` sin repetir la conversión o la IA.

Cuando un documento requiere varias revisiones, Parsezen las presenta en el orden en que se generó
el contenido: OCR, traducción, corrección y estructura. Cada paso queda aplicado antes de mostrar el
siguiente. Puedes cerrar tras cualquiera de ellos y continuar desde el paso pendiente; las
decisiones ya aceptadas no vuelven a preguntarse ni relanzan el procesamiento automático.

## Editor EPUB

El panel izquierdo contiene la estructura y el derecho el contenido editable.

- `Separar desde aquí`: crea una sección en el párrafo donde está el cursor.
- `Unir`: quita la división y conserva el texto en la sección anterior.
- Flechas verticales: reordenan entre secciones hermanas.
- Flechas laterales: anidan o elevan el nivel.
- `Renombrar`: cambia el título del índice.
- Controles B, I, U y títulos: formato básico.
- Los controles `−`, porcentaje y `+` ajustan la escala visual del editor entre 70 % y 160 % sin
  modificar el contenido ni el tamaño tipográfico guardado en el libro.

`Guardar y continuar después` conserva el libro cifrado. `Generar EPUB definitivo` valida y publica
el archivo antes de mostrarlo como completado.

Si activaste la revisión manual de estructura, el editor aparece aunque Parsezen no haya encontrado
ningún cambio automático que proponer. Cancelar no publica nada; guardar conserva el punto exacto
para continuar después. El documento solo cambia a `Completado` cuando el EPUB definitivo ya se ha
validado y sustituido de forma segura.

## IA por primera vez

Cuando una acción requiere Ollama, sigue el botón que aparezca:

1. `Instalar Ollama`;
2. `Iniciar`;
3. `Proteger y reiniciar`, si el modo local no está activo;
4. `Elegir modelo`.

El gestor muestra en una lista única los modelos recomendados e instalados. Puedes filtrarlos,
buscar o escribir directamente un nombre del catálogo, por ejemplo `qwen3:4b-instruct`, y pulsar
`Instalar modelo`. Parsezen exige una variante dedicada a instrucciones y bloquea modelos de
razonamiento conocidos, como `qwen3:4b`, DeepSeek R1 o QwQ, porque pueden no devolver el documento
transformado. Si ya están instalados se muestran como no compatibles para que puedas eliminarlos.
La ventana de contexto incluye valores habituales y una opción personalizada. No uses etiquetas
cloud.

## Solución de problemas

### Un documento parece detenido

Comprueba la fase y la barra de progreso. Algunas operaciones iniciales descargan un modelo público
o preparan OCR/Argos y pueden tardar más la primera vez.

### Una revisión reaparece

No se ha publicado todavía el resultado definitivo. Abre `Revisar`, completa todas las decisiones y
espera a que la fila cambie a `Completado`.

### Parsezen se cerró durante una revisión

Abre de nuevo la aplicación. La cola, las decisiones y el libro pendiente se recuperan desde la
instantánea cifrada. Si el original se movió o se eliminó, Parsezen no puede continuar ese trabajo.
Si el original fue modificado, la revisión anterior se descarta para no mezclar dos versiones y el
documento continúa desde un punto seguro.

### La recuperación automática muestra un aviso

Parsezen seguirá intentando guardar si el problema es temporal. No cierres la aplicación mientras
aparezca el aviso si quieres conservar el estado de la cola. Si intentas cerrar con trabajo sin
guardar, Parsezen te pedirá confirmación.

Si la cola anterior no puede leerse, Parsezen conserva primero una copia local de su base de datos y
de los materiales cifrados de revisión, y después abre una cola limpia. El aviso muestra los nombres
de esas copias; los documentos y resultados no se modifican. Si la base está dañada y ni siquiera
puede abrirse, Parsezen la aísla junto con sus revisiones cifradas en una carpeta de recuperación.

### Error de OCR

Compara la página original. Tipografía decorativa, rotación, manuscritos y tablas complejas pueden
necesitar edición manual. Un OCR vacío en una página visual es un resultado válido, no un bloqueo del
documento completo.

### El EPUB no abre

Conserva el archivo y el identificador local del error. El resultado final no se sustituye por un
parcial. En desarrollo puede validarse con EPUBCheck.

### Ollama no responde

Usa la acción de refresco o `Iniciar`. Parsezen espera la API local; no requiere mantener abierta una
ventana de chat.

### No aparece un formato

La combinación no puede garantizarse. Por ejemplo, PDF no se reconstruye como PDF y EPUB con DRM no
puede editarse.

## Copias de seguridad

Los originales nunca se modifican, pero debes conservarlos y respaldar los resultados importantes.
La caché y las revisiones cifradas son mecanismos de continuidad, no una biblioteca ni una copia de
seguridad permanente.
