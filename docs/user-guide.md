# Guía de uso de Parsezen

Esta guía describe la versión actual de Parsezen.

## La pantalla principal

Cada documento ocupa una fila. Las columnas muestran el recorrido real:

- **Documento**: origen, tamaño y páginas PDF.
- **Flujo**: acciones automáticas en orden y, solo cuando corresponda, tu intervención posterior.
- **Salida**: archivo final y su destino.
- **Estado**: fase actual, avance y acción necesaria.

Al añadir un documento, **Salida** abre una página breve dentro de la aplicación. **Markdown** y
**EPUB** se eligen con dos tarjetas visuales; **Revisión automática con IA** usa un interruptor. **Traducir** y,
para PDF, **Páginas** y **OCR** mantienen el patrón `Etiqueta — Valor actual — ›`. No hay secciones
avanzadas, pie de botones ni scroll en el tamaño normal. Cuando hay traducción, una única línea tras
la interfaz mantiene los detalles de funcionamiento en la ayuda contextual, sin añadir un resumen
técnico permanente bajo las opciones.

**Traducir** parte en **No traducir**. Al elegir un idioma aparecen dos filas idénticas:
**Traductor** —Argos o IA local— y **Glosario**. La revisión automática con IA parte activada en los
documentos nuevos. Cada elección válida se guarda inmediatamente; **Volver** y Escape solo regresan
a la cola. Si falta la IA exigida por una elección, Parsezen mantiene la intención y abre el gestor
local. Una configuración guardada conserva sus decisiones.

**Procesamiento directo** convierte o traduce, comprueba la salida y puede recomendar después una
revisión dirigida si encuentra señales concretas. **Revisión automática con IA** examina el
texto completo y, si el resultado es EPUB, también su estructura. No se pueden combinar corrección
y estructura por separado.

En **Flujo**, `→` separa fases y `y` une acciones realizadas en la misma pasada. La IA traduce,
corrige, verifica u organiza; `Tu revisión` se reserva para una decisión humana y forma parte de la
misma secuencia. Un EPUB termina siempre en `Tu revisión final`; otros resultados solo incluyen
`Tu revisión si hay cambios` cuando la configuración puede producir propuestas.

Cada acción se encuentra en la celda a la que pertenece: configurar el flujo o la salida, revisar,
ver un error o abrir el archivo. La pantalla principal no añade un inspector lateral ni repite la
información de la fila.

`Destino ·` en la cabecera define el único destino general y lo recuerda. Cada documento lo hereda;
para cambiarlo usa los ajustes generales. No existen excepciones por documento ni otro selector de
destino en la página de configuración.

Cuando una fila termina, pulsa `Abrir resultado` en **Estado**. Su menú secundario permite abrir
el archivo o su carpeta concreta. Las fases omitidas no ocupan columnas ni ofrecen acciones
imposibles.

## Añadir y ordenar documentos

- En vacío, arrastra TXT, Markdown, DOCX, PDF o EPUB a la zona situada bajo la cabecera o pulsa
  `Seleccionar archivos`. Después aparece como `Añadir` en la barra situada sobre la tabla, junto a
  la acción principal del lote. La cola crece hasta seis filas visibles antes de usar desplazamiento.
- Arrastra una fila para cambiar el orden.
- La configuración, las acciones de estado y el resultado responden al pasar el puntero; el asa de
  ordenación muestra un cursor de arrastre.
- Usa la papelera del extremo derecho para retirar esa fila de la cola. En un documento pausado o
  pendiente de revisión, Parsezen pide confirmación antes de descartar su progreso o revisión; el
  archivo original nunca se modifica.
- Usa `···` o la tecla de menú para retirar un trabajo pendiente.
- En un PDF, **Páginas** permite usar todas o pedir un intervalo inclusivo en un diálogo breve; al
  aplicarlo la fila muestra directamente valores como `25–140`. **OCR** permite elegir Automático o
  Todas las páginas.

La configuración queda bloqueada al empezar para evitar que el resultado deje de corresponderse con
lo mostrado.

El botón contextual situado sobre la cola utiliza exclusivamente la configuración guardada de cada
fila. Si un documento no puede arrancar, Parsezen muestra el motivo en la interfaz actual en lugar de
dejar el fallo en segundo plano.

La interfaz sigue por defecto la apariencia de Windows. En **Ajustes → Apariencia** puedes elegir
**Seguir el sistema**, **Claro** u **Oscuro**; la elección se aplica sin recargar y se conserva para
la siguiente sesión. El engranaje solo muestra un indicador cuando algún documento tiene una acción
pendiente; **Actividad reciente** es la primera opción del menú.

Con una ventana intermedia, estrecha o con zoom alto, la cabecera y la cola se reorganizan sin
recortar controles. No se eliminan operaciones: el flujo se integra en Documento, las acciones
permanecen en su fila y las comparaciones pasan a orientación vertical.

## Elegir un resultado

### Markdown

Adecuado para texto estructurado, Obsidian y sistemas de conocimiento. Parsezen genera un único
archivo canónico y conserva las imágenes compatibles sin pedir decisiones de organización antes de
procesar.

### EPUB

Adecuado para lectores electrónicos. Integra los recursos compatibles dentro del libro. Antes de
publicar aparece siempre una confirmación breve con título, autor, idioma, portada y número de
capítulos. Puedes publicar desde ahí o abrir el editor completo para cambiar portada, estructura y
contenido.

La organización automática es deliberadamente conservadora: reconoce contenedores explícitos como
`Part`, `Parte`, `Book`, `Libro`, `Volume` o `Volumen` solo cuando les siguen al menos dos títulos
explícitos de `Chapter`/`Capítulo` contiguos. En ese caso crea un único nivel padre-hijos y mantiene
el número, las palabras y el orden del origen. Un caso ambiguo, un solo capítulo o un epílogo/apéndice
posterior se conserva plano; el spine sigue el árbol en preorden.

TXT y DOCX siguen siendo formatos de entrada, pero no añaden salidas paralelas: el resultado siempre
es Markdown o EPUB.

## Traducir

Al elegir un idioma en **Traducción**, aparecen dos motores que funcionan dentro del equipo:

- **Contextual · IA local**: opción inicial. Usa el modelo general instalado en Ollama; para un PC
  estándar conviene comenzar con uno de aproximadamente 4B parámetros. El resultado depende del
  modelo elegido y Parsezen conserva cada fragmento original que no supera sus comprobaciones.
- **Rápida y ligera · Argos**: alternativa manual de consumo predecible. No necesita un modelo
  conversacional, pero nunca se activa automáticamente ni se usa en pruebas sin pedirlo
  expresamente.

La ayuda situada bajo el traductor se actualiza con el motor, la revisión y el formato elegidos.
Indica si el recorrido usa Argos u Ollama, si la comprobación bilingüe es independiente, cuántas
pasadas habrá y un coste cualitativo bajo, medio o alto. No es una estimación monetaria: todo sigue
siendo local y gratuito; resume tiempo, cómputo y memoria relativos.

La elección del traductor es independiente del nivel de revisión. Parsezen no cambia de traductor
dentro del documento: si eliges Argos y revisión semántica, Argos traduce primero y Ollama revisa
después. Si Ollama no está disponible, el trabajo se detiene con un diagnóstico recuperable en vez
de degradarse silenciosamente a Argos. El modelo solo propone
sustituciones breves: Parsezen las aplica una a una cuando conservan cifras, enlaces, nombres,
párrafos y estructura. Una propuesta rechazada no elimina otras correcciones seguras ni permite
reescribir el resto del documento. El idioma y el glosario se comparten entre ambos recorridos.

Si Parsezen puede determinar con suficiente confianza que el contenido ya está en el idioma de
destino, omite la traducción. En EPUB compara el idioma declarado por el propio libro con una muestra
suficiente de su texto; si se contradicen, confía en el contenido y evita omitir una traducción
necesaria. Cuando la coincidencia es segura no muestra una fase de traducción ni genera un informe de
calidad ficticio.

Cada motor trabaja en fragmentos verificables. Si una propuesta pierde contenido, cambia
valores protegidos o no parece estar en el idioma solicitado, Parsezen vuelve a intentar solo
las partes necesarias. Cuando ninguna alternativa es segura, conserva ese fragmento original y lo
incluye en la revisión en vez de publicar silenciosamente una transformación dudosa. El informe de
traducción se vuelve a calcular sobre la propuesta final para que sus incidencias pendientes no
describan una versión anterior. En títulos, conserva localmente la sintaxis estructural mientras traduce
solo las palabras; los nombres editoriales se mantienen como nombres propios. Una cita en otro
idioma puede permanecer intacta si el resto de su frase se ha traducido. Si un encabezado pertenece
claramente a un tercer idioma y el modelo lo deforma, Parsezen recupera automáticamente el texto
original sin deshacer su nivel. Si un mismo término aparece traducido de formas parecidas pero
incompatibles, lo muestra en la revisión en lugar de elegir una por semejanza.

Las descripciones visibles de las imágenes también se traducen. La ruta privada y el papel de cada
recurso permanecen protegidos, por lo que cambiar `Cover` por `Portada` no puede romper ni sustituir
la imagen del libro.

Si una corrección parcial deja repetido el complemento de un título, Parsezen revisa de nuevo solo
esa línea completa y conserva la versión anterior cuando la alternativa no supera todos los
controles. No amplía esa reparación al autor ni a los párrafos contiguos.

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

Todos los documentos usan el modelo y contexto generales. Los cambios actualizan los trabajos que
aún se pueden editar; un trabajo iniciado conserva su instantánea. Parsezen impide eliminar un
modelo del que todavía depende un trabajo sin terminar.

## Revisión semántica con IA local

La revisión semántica es una pasada proactiva y opcional que prepara una propuesta conservadora:

- corrige errores gramaticales o de conversión;
- retira ruido evidente;
- no debe resumir, reordenar ni inventar contenido;
- deja cada diferencia para revisión cuando así se haya configurado.

Las propuestas que añaden o eliminan bloques, cambian cifras, fechas, nombres propios, siglas,
párrafos o una parte sustancial del texto se consideran arriesgadas y conservan el original por
defecto. La aprobación masiva aplica las correcciones conservadoras, pero no sustituye esos
originales arriesgados. Si varios fragmentos son válidos por separado pero su combinación no lo es,
Parsezen conserva los fragmentos que siguen siendo seguros en lugar de descartar toda la propuesta.

Con Argos, el traductor termina primero y la revisión bilingüe trabaja después sobre el resultado en
el idioma final. Con traducción por IA, traducción y corrección se combinan en una pasada principal;
esa corrección no constituye una segunda versión independiente. En EPUB, la estructura añade otra
pasada. Las guardas de contenido siguen siendo las mismas.

Parsezen conserva esta diferencia en el propio resultado y la muestra al revisar y en Actividad
reciente. El resumen separa bloques comprobados automáticamente, bloques revisados semánticamente,
bloques con verificación bilingüe independiente, bloques sin revisión semántica e incidencias que
siguen pendientes. Las comprobaciones heurísticas nunca se presentan como una certificación.

La revisión completa aparece activada en documentos nuevos, pero sigue siendo una elección visible:
puede tardar varias veces más y una propuesta segura no demuestra por sí sola que el texto sea mejor.
Desactívala para un recorrido directo con comprobaciones locales y revisión dirigida solo si aparecen
señales concretas. La fila de la cola resume el flujo elegido sin añadir decisiones técnicas a la
página de configuración.

### Revisión recomendada después del modo directo

Al terminar un procesamiento directo, Parsezen usa sus comprobaciones locales para buscar indicios
acotables: daño de conversión, incidencias PDF repetidas, texto original residual o varias
incoherencias de traducción. Una advertencia aislada y débil no basta. Si las señales se pueden
asociar a bloques concretos, la fila cambia a `Revisión sugerida` y ofrece `Revisar con IA`.

La sugerencia solo guarda tipos, cantidades, posiciones y huellas no reversibles del resultado;
no guarda extractos del documento. No
inicia Ollama, no cambia el resultado y no impide abrirlo. Si aceptas, Parsezen envía al modelo local
un máximo de 64 bloques afectados, excluye código, imágenes y metadatos internos y conserva el resto
exactamente como estaba. En una traducción, compara cada bloque con su original cuando puede
alinearlos. Los cambios se presentan como propuesta antes de sustituir el resultado.

Puedes ignorar la recomendación. Si cancelas la revisión o Ollama falla, el trabajo vuelve a
`Completado` y el archivo anterior permanece intacto. La recomendación también se conserva al cerrar
Parsezen; al retomarla, el texto se reconstruye desde los archivos locales y sus huellas se vuelven
a validar. Los bloques se pueden volver a localizar tras el empaquetado EPUB, pero si su contenido
cambió fuera de Parsezen deberá procesarse de nuevo.

## EPUB y estructura

Todo EPUB recibe la estructura técnica mínima necesaria para ser válido. Con procesamiento directo no
se decide nada más antes de procesar. Con revisión semántica, Ollama prepara un esquema de conjunto a
partir del índice, la geometría y páginas de origen, los encabezados existentes y los roles
semánticos. No puede reescribir el texto: devuelve solo pares seguros de línea y nivel, que Parsezen
aplica con las palabras exactas del original. Una respuesta inválida conserva el documento entero.
La confirmación final es siempre ligera; el editor completo solo se abre por petición o cuando un
bloqueo impide publicar. EPUB→EPUB también sigue esta misma ruta.

La planificación inicial distingue portada editorial, preliminares, índice y cuerpo. Para decidir
los capítulos combina los títulos encontrados en el índice con los niveles derivados de la
geometría del PDF; el índice no se confunde con un capítulo del cuerpo. Cuando existe una propuesta
estructural, la revisión muestra arriba el árbol actual y el propuesto antes de presentar las
decisiones concretas de encabezado.

## Procesamiento y cola

Parsezen ejecuta una sola tarea pesada cada vez. La barra de la cola reúne el total, las revisiones
reales y la acción principal disponible. Antes de empezar, muestra un intervalo aproximado para el
trabajo automático. Esta estimación mejora con ejecuciones similares realizadas en el equipo y no
guarda nombres, rutas ni contenido. Al pulsar `Procesar`, la preparación comienza directamente; solo
una configuración inválida o un problema real impide crear el trabajador.

Un documento que llega a revisión queda pendiente, pero no detiene los demás: Parsezen continúa con
el siguiente trabajo elegible y reúne las decisiones en una única superficie `Revisión del
documento` cuando vuelves a revisarlo.

En PDFs de 120 páginas o más —o desde 60 cuando el flujo incluye OCR forzado, IA local o EPUB—,
Parsezen examina hasta nueve candidatos baratos y elige como máximo cinco páginas distintas: inicio,
final y ejemplos de contenido denso, visual o tabular. La muestra pasa por el mismo flujo real,
pero evita el trabajo final redundante: convierte las cinco páginas, no construye el EPUB ni ejecuta
la revisión o reestructuración definitiva y prueba la traducción como máximo en tres posiciones
distribuidas. Sus salidas temporales se eliminan. Si una señal
material se repite en varias páginas, el trabajo completo no comienza y la fila explica qué
configuración conviene revisar. Si la muestra es segura, continúa automáticamente. La extracción y
el OCR válidos se reutilizan al procesar el documento completo; cambiar el archivo o su
configuración obliga a comprobarlo otra vez.

La fila activa conserva el documento, la fase y el progreso medible. Tras diez segundos con progreso
cuantificable, el tiempo restante puede ajustarse al ritmo real de esa fase y se resume como
`Quedan ~3–12 min` o `Queda <1 min`. Su ayuda explica si solo
se descontó el tiempo transcurrido o si la observación amplió el intervalo. Si se supera el máximo
inicial, muestra `Más tiempo del previsto` en lugar de fingir una precisión que ya no existe.

Desde que Parsezen fija y valida la ejecución, y mientras lee, convierte, aplica OCR o prepara
recursos, la fila activa muestra siempre `Preparando`. Después, **Estado** muestra
`Traduciendo`, `Corrigiendo`, `Personalizando` o `Publicando` según la fase real, además del porcentaje
cuando existe una medida. Si un documento necesita
revisión, su fase muestra `Revisar` y la cola continúa con otro. Un error también queda aislado; la
celda muestra `Ver error` y abre su explicación completa sin perder los resultados anteriores.
Según la causa, ofrece `Reintentar esta fase`, `Revisar configuración`, `Revisar destino` o
`Abrir IA local`. El reintento actúa solo sobre ese documento y conserva las fases, decisiones y
checkpoints ya válidos. La explicación indica qué fase falló, qué trabajo sigue siendo reutilizable
y mantiene las acciones de recuperación en la propia pantalla; Parsezen no abre un modal ni muestra
trazas técnicas automáticamente.

Antes de exponer un resultado, Parsezen vuelve a comprobar el archivo temporal. En texto verifica
codificación, contenido y estructura; en DOCX y EPUB comprueba además que el paquete sea válido e
idéntico al ya aprobado. Una fila completada muestra `Integridad final comprobada`; su ayuda
enumera los controles y un inventario sin contenido documental. Si el control detecta una
diferencia, no sustituye el resultado anterior.

Al terminar, `Ver resumen` separa cuatro conceptos que no deben confundirse:

- **Integridad técnica**: el archivo definitivo existe y superó las comprobaciones del formato.
- **Incidencias detectadas**: señales objetivas encontradas durante OCR, conversión o traducción,
  incluso si después se resolvieron.
- **Confianza lingüística**: recorrido de corrección o verificación y cobertura por bloques; no es
  una garantía de calidad literaria.
- **Revisión manual**: decisiones que una persona realizó o que todavía se esperan.

La barra sobre la cola resume también el lote completo. Si Parsezen está minimizado o en segundo
plano, Windows muestra un aviso al terminar o al requerir atención; no duplica ese aviso mientras la
ventana está activa.

`Pausar` solicita detenerse en el siguiente punto seguro. Al volver, Parsezen usa los checkpoints
cifrados en lugar de repetir trabajo válido. Un lote pausado dice `Procesamiento pausado`; uno
interrumpido definitivamente dice `Procesamiento detenido`. El aviso se recalcula al retirar parte
del lote y desaparece al eliminar el último documento relacionado; el registro histórico permanece
solo en **Actividad reciente**.

En **Ajustes → Conservar trabajo temporal** puedes elegir `No conservar al finalizar`, 7, 30 o
90 días. Los checkpoints siguen ligados al documento y a su configuración exacta, están cifrados
para la cuenta actual de Windows y no contienen telemetría con texto. `Borrar caché` los elimina de
forma inmediata.

**Ajustes → Actividad reciente** conserva como máximo 20 intentos terminados o fallidos para poder
volver a abrir su resumen, resultado o carpeta. Es una lista privada y acotada, no una biblioteca:
guarda rutas, estado, fecha y conteos técnicos, pero no texto documental. `Borrar actividad` elimina
solo ese historial; nunca elimina originales ni resultados. En una ventana amplia, la lista queda a
la izquierda y el detalle seleccionado a la derecha; en una ventana estrecha ambos paneles se apilan
sin introducir desplazamiento horizontal.

Una fila fallida se identifica como `Error en preparación`, `Error en comprobación temprana`,
`Error en traducción`, `Error en corrección`, `Error en personalización` o `Error en publicación`,
con su hora local. Al seleccionarla
puedes leer **Qué ocurrió**, **Tu trabajo** y el **Recorrido** de fases, estados y horas. Si el
documento fallido todavía está en la cola aparece `Volver al documento`, que devuelve a la fila actual
para usar `Reintentar esta fase` o la acción secundaria adecuada. Si solo queda en el historial, la vista
lo explica y no ofrece un reintento ficticio. `Copiar diagnóstico` proporciona un texto técnico breve
con la versión, fase, código, horas y referencias opacas del intento; no incluye nombre, ruta,
mensaje del fallo, contenido, prompts, respuestas, trazas ni secretos de configuración.

## Revisar

La revisión se divide por fase y solo muestra los casos dudosos de la sesión actual. No se muestran
pestañas que no correspondan al problema actual.

1. Compara el original de la izquierda con la propuesta de la derecha.
2. Si aparece una prioridad, aviso o sugerencia, revísala; el caso común no añade texto auxiliar.
3. Pulsa el botón de la versión que quieras confirmar. Si editas la derecha, confirma la edición.
4. El menú `…` de cada panel reúne `Ir al inicio` y, en la propuesta, `Restaurar propuesta`.
5. Pulsa `Siguiente`. El último elemento muestra directamente la acción final de la fase.

La cabecera muestra solo la fase y el progreso global real, por ejemplo `Corrección · 3/12`. Cuenta
únicamente unidades de sesiones materializadas: las fases anteriores completas, la fase actual
parcial y las futuras a cero. No suma cambios estructurales futuros ni incidencias del informe que no
pudieron convertirse en una decisión. La prioridad, el aviso o una sugerencia concreta aparecen en
una línea adicional únicamente cuando cambian la decisión que conviene tomar.

Dentro de cada fase, Parsezen presenta primero las incidencias críticas y altas, mantiene estable el
orden entre elementos de la misma gravedad y muestra solo las prioridades alta o crítica sobre la
comparación. Si guardaste una revisión a medias, se
abre directamente el primer caso pendiente; las decisiones ya tomadas siguen disponibles al volver
con `Anterior`, que solo aparece cuando existe un paso anterior. No se recorren de nuevo de forma
obligatoria. `Alt+O` conserva el resultado
actual cuando esa opción es válida, `Alt+P` elige la propuesta y `Ctrl+Intro` guarda la decisión y
avanza.

Las etiquetas cambian con la fase: OCR muestra `Página original · Solo contexto` y `Texto
reconocido`; traducción, `Extracto original · Contexto` y `Resultado actual · Editable`; corrección,
`Versión actual` y `Corrección propuesta`. En OCR, si después habrá traducción, la ayuda indica que
el texto ya aparece en el idioma del resultado y no necesitas traducirlo otra vez: corrígelo tal
como debe quedar publicado. OCR también ofrece `No hay texto que añadir`: conserva recursos y
anclas, pero no publica un falso texto reconocido. El origen de OCR y traducción es solo contexto y
no tiene botón para seleccionarlo.

En traducción, la ayuda aclara si la corrección ocurrió dentro de la misma pasada o mediante una
verificación bilingüe posterior, junto con los bloques comprobados, revisados, no revisados y las
incidencias pendientes. En estructura, un comparador de árboles permite entender el efecto global
sin recorrer primero todos los cambios individuales; en ventanas estrechas ambos árboles se apilan.

`Guardar y salir` cifra el material de la revisión, incluidos recursos y ediciones. Abrir un caso no
lo resuelve por sí solo: una elección cambiada o una edición se guarda al salir. Al abrir de nuevo
Parsezen, la misma fila vuelve a `Revisar` y continúa en la siguiente decisión pendiente, sin repetir
la conversión o la IA.

En traducción, cada panel muestra únicamente el fragmento dudoso: no presenta el resultado actual
como una sugerencia fiable ni permite aprobarlo en bloque. Debes corregirlo en el idioma solicitado
o usar `Confirmar` solo si realmente es correcto. `Aplicar seguras` aparece únicamente cuando una
fase de corrección contiene varias propuestas validadas; no ocupa espacio en una decisión individual.

Cuando un documento requiere varias revisiones, Parsezen las presenta en el orden en que se generó
el contenido: OCR, traducción, corrección y estructura. Cada paso queda aplicado antes de mostrar el
siguiente. Puedes cerrar tras cualquiera de ellos y continuar desde el paso pendiente; las
decisiones ya aceptadas no vuelven a preguntarse ni relanzan el procesamiento automático.

Antes de publicar, Parsezen vuelve a comprobar la combinación completa de elecciones. Si mezclar
originales y propuestas duplicara, inventara o perdiera una cifra que ninguna de las dos versiones
permite, la publicación se detiene y conserva el archivo anterior para que revises esa decisión.

`Anterior` dentro de la primera unidad de una fase vuelve a la fase previa real. Parsezen pide una
confirmación breve, conserva sus decisiones como punto de partida, invalida y recalcula solo las
revisiones posteriores sin aumentar intentos y no vuelve a ejecutar OCR, traducción ni IA.

## Confirmación y editor EPUB

Todo resultado EPUB abre primero una confirmación ligera. Revisa título, autor, idioma, presencia de
portada y número de capítulos. `Publicar EPUB` termina sin añadir pasos; `Abrir editor completo`
entra en las herramientas avanzadas y `Guardar y salir` conserva el borrador.

El panel izquierdo contiene la estructura y el derecho el contenido editable.

- `Separar desde aquí`: crea una sección en el párrafo donde está el cursor.
- `Unir`: quita la división y conserva el texto en la sección anterior.
- Flechas verticales: reordenan entre secciones hermanas.
- Flechas laterales: anidan o elevan el nivel.
- `Renombrar`: cambia el título del índice.
- Controles B, I, U y títulos: formato básico.
- Los controles `−`, porcentaje y `+` ajustan la escala visual del editor entre 70 % y 160 % sin
  modificar el contenido ni el tamaño tipográfico guardado en el libro.

`Guardar y salir` conserva metadatos, portada, estructura y el capítulo actual en el borrador cifrado
para reanudar después. `Descartar cambios` es una acción destructiva secundaria y pide confirmación
si hay cambios. `Generar EPUB definitivo` valida y publica el archivo antes de mostrarlo como
completado. Cuando editas un EPUB de origen, Parsezen conserva el paquete exacto si no cambias nada;
si solo modificas el texto de capítulos, mantiene byte por byte su navegación, estilos, fuentes,
imágenes y demás recursos. Los cambios estructurales o de metadatos usan la reconstrucción EPUB
normalizada.

El editor no aparece por defecto: solo se abre si lo solicitas desde la confirmación o si una
condición bloqueante necesita revisión avanzada. El documento solo cambia a `Completado` cuando el
EPUB definitivo ya se ha validado y sustituido de forma segura.

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
Si el original fue modificado, la revisión anterior se descarta para no mezclar dos versiones. Quita
el trabajo de la cola y vuelve a añadir el documento para capturar explícitamente su nueva identidad.

### La recuperación automática muestra un aviso

Parsezen seguirá intentando guardar si el problema es temporal. No cierres la aplicación mientras
aparezca el aviso si quieres conservar el estado de la cola. Si intentas cerrar con trabajo sin
guardar, Parsezen te pedirá confirmación.

Si la cola anterior no puede leerse, Parsezen conserva primero una copia local de su base de datos y
de los materiales cifrados de revisión, y después abre una cola limpia. El aviso muestra los nombres
de esas copias; los documentos y resultados no se modifican. Si la base está dañada y ni siquiera
puede abrirse, Parsezen la aísla junto con sus revisiones cifradas en una carpeta de recuperación.

### Error de OCR

Compara la página original. Las tablas sencillas se publican como Markdown y las que necesitan más
fidelidad como HTML compatible. Si una tabla es demasiado compleja para reconstruirla con seguridad,
Parsezen conserva sus celdas como texto estructurado, la marca para revisión y, cuando incluyes
imágenes, añade también un recorte visual de respaldo. Los títulos, notas y unidades situados junto
a la tabla permanecen en el orden de lectura. Tipografía decorativa, rotación y manuscritos pueden
necesitar edición manual. Un OCR vacío en una página visual es un resultado válido, no un bloqueo del
documento completo. En una página formada por una imagen completa, Parsezen puede retirar del texto
una marca aislada mucho más pequeña que la tipografía reconocida, pero la imagen original siempre se
mantiene y las etiquetas cortas de tamaño uniforme no se filtran. Si el OCR pega el rótulo de una
tabla a su primera fila, Parsezen lo separa automáticamente para conservar ambos y publicar una tabla
real. Si las líneas de una tabla están dibujadas dentro de la imagen pero el texto sigue siendo
seleccionable, Parsezen combina ambas capas localmente y solo publica la cuadrícula cuando todas las
letras, cifras y signos quedan conservados. Al reanudar, una caché OCR de una versión anterior se
recalcula sin mostrar sus marcadores
técnicos como parte del libro. En una portada gráfica, un título repetido en una página preliminar
nativa puede corregir un único término corto introducido por OCR, pero solo si la coincidencia es
inequívoca; si existen variantes, la aplicación mantiene el texto reconocido para revisión.

En los índices, Parsezen reconoce una columna derecha de folios y vuelve a unir cada número con la
entrada situada en la misma fila. Las entradas se publican con título y folio en columnas alineadas,
conservan sangría, negrita, cursiva y enlaces internos, y mantienen el orden de origen en lugar de
agrupar primero todos los títulos y después todos los números. También reconoce folios de cuatro
cifras en libros largos. Si el texto nativo pega dos palabras o confunde un carácter de un folio,
solo aplica el espacio o la cifra que corroboren la fila visual, sus números vecinos y el OCR local;
una lectura ambigua permanece visible para revisión.
Durante la traducción y la corrección, cada folio sigue unido al final de su entrada y una propuesta
que lo desplace se descarta sin perder las demás correcciones seguras. Los rótulos internos que no
tienen página propia aparecen separados de la entrada anterior.

Cuando una página con capa de texto también contiene una imagen completa y presenta señales de riesgo
—por ejemplo, un índice, fórmulas o glifos extraños—, Parsezen puede contrastarla localmente con OCR y
un segundo extractor. Si una región breve sigue siendo ambigua y tienes instalado un modelo visual
compatible de tamaño estándar en Ollama, lo usa como árbitro solo para ese pequeño recorte. La capa
nativa sigue teniendo prioridad y ninguna página ni documento se envía fuera del ordenador. Sin un
modelo visual compatible, el flujo continúa normalmente y mantiene el caso dudoso para revisión.

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
