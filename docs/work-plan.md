# Plan de trabajo y estado de calidad

Este es el único plan vivo de Parsezen. Su función es decir a un agente qué está demostrado, cuál es
la frontera activa y qué condición permite avanzar. No sustituye la arquitectura ni la lista de
aceptación. Se actualiza cuando cambia un estado, un gate o el orden de trabajo; no después de cada
edición menor.

Última actualización: **2 de septiembre de 2026**. Baseline histórica de esta iniciativa: `main` en
`865ef2a`. El estado ejecutable se obtiene siempre del checkout y su diff actual, no de esta línea.

## Norte

Obtener EPUB refluibles desde PDF con la máxima fidelidad práctica:

- texto, cifras, símbolos, énfasis, imágenes, tablas y enlaces conservados o señalados;
- traducción natural y fiel cuando se solicita;
- partes, capítulos y secciones navegables según la evidencia del libro;
- ninguna incertidumbre presentada como certeza;
- originales inmutables, trabajo recuperable y publicación atómica;
- coste local proporcional al riesgo real del documento.

## Lectura del estado

Los estados se usan con el significado de `agent-operating-model.md`:

- **IMPLEMENTADO** no significa universalmente perfecto;
- **VERIFICADO EN CORPUS** siempre está limitado a una muestra;
- **EXPERIMENTAL** no puede modificar el flujo normal ni promocionar cambios;
- **RECHAZADO** solo se reabre con evidencia o mecanismo nuevo.

## Estado del sistema

| Área | Estado | Evidencia actual | Límite conocido |
|---|---|---|---|
| contratos, cola, recuperación y publicación | IMPLEMENTADO | suite automatizada, fallos inyectados e integridad final | cualquier cambio transversal exige suite completa |
| extracción PDF/OCR y formato | VERIFICADO EN CORPUS | corpus privado diverso, canarios y reanudación determinista | un PDF nuevo puede introducir otra geometría; no existe «verde universal» |
| traductor Hy-MT2 EN→ES | VERIFICADO EN CORPUS | 41 referencias humanas; 41/41 puertas duras | naturalidad y sentido no quedan demostrados solo por las guardas |
| memoria terminológica acotada | VERIFICADO EN CORPUS | mejora en 9 de 13 casos de dominio sin perder puertas duras | solo sintagmas inequívocos; el glosario humano tiene prioridad |
| reparación semántica residual automática | RECHAZADO con los modelos actuales | 67 señales, 19 decisiones humanas y descarte de parches por consenso | 1/12 propuestas iniciales aprobada; el piloto nuevo produjo 0/13 consensos útiles |
| estructura Parte → Capítulo → Sección | VERIFICADO EN CORPUS | dos holdouts no usados para la regla, 925 páginas, 57 capítulos y 289 destinos equivalentes en ruta directa/editor | evidencia insuficiente conserva jerarquía plana; no se inventan destinos |
| paquete EPUB e integridad | IMPLEMENTADO | validación interna, comparación de payload y EPUBCheck optativo | EPUBCheck no sustituye fidelidad editorial |
| recorrido completo sobre los libros clave | VERIFICADO EN CORPUS para estructura; traducción aún abierta (1/6 centinelas) | *36 Faces*: 317 páginas, 297 bloques, 42 capítulos, 46 imágenes e integridad final; mejor candidato conservado con 25 avisos | quedan cinco libros; dos regeneraciones posteriores con 45 avisos se rechazaron como nueva base editorial |

## Decisiones vigentes

1. **No cambiar ahora el traductor base.** Hy-MT2 sigue siendo la mejor base local evaluada; cambios
   globales de prompt y `repeat_penalty` degradaron o no mejoraron de forma estable el corpus.
2. **No activar reparación semántica automática.** Que LFM esté instalado y pueda generar propuestas
   protegidas no autoriza a aceptarlas sin una persona.
3. **No seguir generando paráfrasis de los mismos rechazos.** Once de doce propuestas revisadas no
   alcanzaron aprobación tras varias rondas. Otra variante sin mecanismo nuevo gastaría atención sin
   aumentar evidencia.
4. **Conservar los 65 casos como conjunto diagnóstico.** No equivalen a 65 errores: mezclan avisos
   reales, posibles falsos positivos y casos que no admiten una propuesta segura.
5. **Promover solo referencias humanas completas.** La única corrección aprobada se conserva en el
   corpus privado; ningún caso dudoso se aplica al producto ni a un libro.
6. **Mantener separación entre detección, propuesta y publicación.** Mejorar una no cambia el estado
   de las otras.
7. **No insistir con parches por consenso en los modelos instalados.** Ni Qwen 4B ni LFM produjeron
   un segundo flujo estructurado capaz de coincidir con Qwen 3.5; se obtuvieron 0/13 candidatos.

## Traducción: evidencia cerrada y mantenimiento seguro

Objetivo del incremento: entender por qué el piloto residual consume revisión y producir una mejora
general que reduzca dudas o aumente propuestas correctas sin tocar bloques limpios.

### Pasos T1 y T2 — CERRADOS: evidencia y cuello de botella

El 31 de agosto de 2026 se validaron los hashes, las decisiones y la cadena de revisiones de los 12
casos que llegaron a tener propuesta. El informe privado, reproducible y sin texto documental ni
notas humanas conserva 19 eventos: las 19 propuestas superaron las guardas mecánicas, pero solo una
fue aprobada. La aceptación fue 1/12 en la primera ronda y 0/7 en rondas posteriores; el estado final
es una aprobación, seis correcciones aún rechazadas y cinco casos sin decisión concluyente.

La conclusión demostrada es que **la generación de propuestas es el cuello de botella observado**.
Las guardas protegen estructura, cifras, idioma y residuos, pero no demuestran calidad semántica. Las
decisiones actuales no proporcionan verdad humana directa suficiente para separar, caso por caso,
fallo de detector y fallo de selección de contexto; esos estados permanecen **NO DEMOSTRADOS** en vez
de forzar una etiqueta. Dos modelos locales clasificaron las 13 notas en una taxonomía cerrada, pero
solo coincidieron de forma completa en una: esas categorías son orientativas y no se usarán como
gate ni como explicación causal.

### Paso T3 — RECHAZADO: descarte barato de parches por consenso

Se probará una sola hipótesis nueva: sustituir la reescritura completa del bloque por parches mínimos
`old → new` propuestos de forma independiente por dos modelos locales. Un candidato solo sobrevive al
piloto si ambos producen exactamente el mismo parche, `old` aparece una sola vez, el cambio queda
acotado a la instrucción humana y vuelven a pasar todas las guardas. El desacuerdo se convierte en
«sin propuesta», nunca en una elección automática.

El descarte se ejecuta solo sobre evidencia ya revisada y controles limpios; no cambia el producto ni
solicita nueva atención humana. Se medirá:

- consenso exacto y tasa de abstención;
- extensión y número de parches;
- conservación de estructura, cifras, enlaces y valores protegidos;
- modificación nula de controles limpios;
- casos donde la nota no puede expresarse como parche inequívoco.

El descarte terminó con 0/13 parches por consenso y cero modificaciones del único control limpio.
Qwen 3.5 produjo nueve respuestas estructuradas de catorce, pero Qwen 4B solo una y LFM ninguna; no
existió una pareja capaz de sostener el contrato dual. El resultado no demuestra que los parches sean
intrínsecamente imposibles, pero sí rechaza implementarlos con los modelos instalados. No se relaja el
consenso, no se prueba otra plantilla y no se abre T4.

### Paso T4 — NO ABIERTO: gate antes de volver a pedir revisión

Solo se crea una nueva tanda humana si:

- todas las guardas duras pasan;
- ningún control limpio cambia de forma material;
- las propuestas son distintas por una causa técnica nueva, no por otra redacción del prompt;
- la evaluación ciega interna muestra una posibilidad razonable de alcanzar los gates de la política;
- la tanda es pequeña, diversa, deduplicada y cómoda desde móvil.

No hubo ninguna propuesta por consenso exacto. El experimento queda cerrado sin trasladar su coste al
usuario.

La aprobación de producción sigue exigiendo la muestra completa definida en
`local-ai-model-policy.md`: precisión mínima del 98 %, cero falsos positivos críticos, modificación de
bloques limpios como máximo del 1 % y recall mínimo del 80 % sobre errores sembrados.

### Salida de traducción — MANTENIMIENTO SEGURO

La fase pasa de frontera activa a mantenimiento bajo este contrato:

- Hy-MT2 conserva sus puertas duras y referencias humanas;
- las señales residuales se clasifican con precisión suficiente o quedan presentadas como revisión
  explícita, sin sobreafirmar cobertura;
- cualquier corrector automático cumple los gates o permanece desactivado;
- un holdout nuevo confirma que las reglas son generales;
- el coste de llamadas y revisión no crece de forma desproporcionada.

Hy-MT2 continúa como traductor base; las puertas duras y las referencias humanas se conservan. Las
señales semánticas no demostrables se presentan como revisión explícita y ningún corrector automático
queda activado. Los tres holdouts del piloto confirman que perseguir otra ronda de generación tendría
peor retorno que validar el recorrido integral. Traducción se reabre solo con un mecanismo o modelo
nuevo, no con otra variante de prompt.

## Ahora: validar el sistema completo

### S1 — CERRADO: regresión estructural sobre holdouts no usados

El 31 de agosto de 2026 se ejecutaron dos libros con índices y jerarquías distintas que no se usaron
para diseñar estas correcciones: Tafti 2 (221 páginas) y Ancient Astrology, volumen II (704 páginas).
La comparación descubrió dos incoherencias generales y no específicas de un título:

- el editor eliminaba antes de tiempo la evidencia privada de página/outline y podía volver a partir
  de forma distinta un documento ya planificado;
- el EPUB directo conservaba los archivos correctos, pero aplanaba en su navegación la relación
  superior `Parte → Capítulo` que el editor sí conocía.

Ambas rutas conservan ahora la evidencia únicamente hasta terminar la planificación y construyen la
misma jerarquía mediante niveles de índice demostrados o roles explícitos conservadores. La repetición
final produjo 57 capítulos en ambas rutas, 289 destinos con la misma profundidad, cero capítulos por
debajo de 1 000 bytes, cero saltos de encabezado, cero incidencias PDF bloqueantes y dos EPUB con
integridad verificada. Las secuencias de planificación fueron idénticas y no hubo capítulos añadidos,
eliminados o divididos al pasar por el editor. Los marcadores privados no aparecen en el XHTML.

El informe privado agregado queda en
`local-benchmarks/epub-structure-corpus-v1/structure-holdout-20260831-final-sanitized.json`. El gate
estructural queda cerrado para esta muestra, no como afirmación de perfección universal.

### E1 — ACTIVO: recorrido integral sobre los seis libros clave

El descarte representativo del 1 de septiembre ejecutó 97 páginas de los seis libros, con traducción
EN→ES en cuatro casos. Los seis EPUB superaron integridad de contenedor, conservación de recursos,
ausencia de marcadores privados y jerarquía sin saltos; tampoco hubo incidencias PDF bloqueantes ni
fallos de OCR obligatorio. Esta es evidencia técnica del recorrido acotado, no aprobación editorial
de libros completos.

Los 46 bloques traducidos produjeron nueve señales `SOURCE_TEXT`. La revisión humana privada quedó
completa y ligada por hashes al lote exacto: cinco propuestas se aceptaron y cuatro requieren una
corrección editorial. Las seis señales de *36 Faces* compartían prácticamente la misma forma
mecánica —residuo dentro de énfasis—, pero cuatro fueron aceptables y dos no. Por tanto, cursiva,
longitud o coincidencia léxica no ofrecen una regla general con precisión suficiente. El sistema sí
acotó los cuatro bloques problemáticos, pero el candidato previo a revisión no supera todavía el gate
editorial y no se convertirá ninguna respuesta humana en una sustitución específica por libro.

El informe reproducible sin texto queda en
`local-benchmarks/end-to-end-pilots/e1-representative-20260901-sanitized.json`. Las decisiones y los
fragmentos permanecen en el corpus privado; los casos aceptados son controles y las correcciones son
evidencia diagnóstica, no lógica ejecutable.

El primer centinela completo, *36 Faces*, procesó 317 páginas, 297 bloques traducidos, 46 imágenes y
42 capítulos con integridad final, cero incidencias PDF bloqueantes y cero fallos de OCR obligatorio.
La línea base produjo 55 avisos de texto fuente y uno de fidelidad. Dos correcciones humanas del lote
representativo compartían un patrón general inequívoco —una serie `planeta in signo + romano` copiada
en inglés— que no aparecía sin traducir en ninguno de los cinco controles aprobados. La normalización
local de ese patrón, incluidos los casos donde el romano había quedado fuera del énfasis, cambió solo
esos dos casos, mantuvo sus guardas y redujo en el libro completo los residuos de 55 a 42 sin alterar
capítulos ni integridad. El mecanismo queda **IMPLEMENTADO** y verificado en esta muestra privada; no
es una tabla de sustituciones por título ni una corrección general de naturalidad.

El centinela aún conservaba 43 avisos y exigía revisión amplia. Además, el límite de veinte extractos
dejaba inicialmente oculta la única incidencia de fidelidad tras avisos de menor prioridad. El informe
mantiene ahora el mismo límite privado, pero prioriza idioma, alineación y fidelidad antes de residuos
o longitud; el total y los segmentos alineados siguen siendo exhaustivos.

La muestra privada posterior de diez casos quedó **COMPLETA** y ligada a su paquete exacto: cinco
aprobados y cinco con corrección. Las notas separaron un título completamente sin traducir, dos
rótulos/elecciones terminológicas, un romano mal extraído y una falsa alarma sobre un índice ya
español. No se convirtieron en respuestas por libro. Se implementaron cinco mecanismos generales:
consenso I/II/III para glifos astrológicos dañados; localización de rótulos de colocación; memoria
`exaltation`→`exaltación` activable también por decanos/zodiaco; análisis de índices XHTML por celda; y
respaldo Argos, ya instalado y sin descarga, exclusivamente para un título que el reintento de IA deja
intacto.

La cadena de decisiones, fuentes y propuestas pasó sus hashes 10/10. En la muestra, los nuevos patrones
de rótulo coincidieron con tres correcciones y cero aprobaciones; la falsa alarma del índice pasó a cero
incidencias y una prueba real Hy-MT→Argos resolvió el título residual con cero avisos. La extracción
nativa completa de *36 Faces* encontró 74 reparaciones sobre 42 páginas, todas confinadas al patrón
astrológico demostrado; se inspeccionaron visualmente los dos fallos que originaron la regla, no las 74
líneas.

El 2 de septiembre se repitió el centinela completo con esos mecanismos. Las 317 páginas volvieron a
producir 297 bloques traducidos, 46 imágenes y 42 capítulos; el EPUB terminó con integridad, cero
incidencias PDF bloqueantes y cero fallos de OCR obligatorio. Los cinco identificadores que la persona
había marcado para corrección dejaron de aparecer y la incidencia de fidelidad bajó de una a cero, pero
la ausencia del identificador no demostró por sí sola que el defecto hubiese desaparecido: la reparación
de extracción cambió el hash de `SCORPIO IE` a `SCORPIO II`, mientras el mismo encabezado seguía en
inglés bajo dos identificadores nuevos. Los avisos totales bajaron de 43 a 41 y todos los restantes eran
`SOURCE_TEXT`. Por tanto, el candidato quedó **VERIFICADO EN ESTE CENTINELA** como mejora segura, pero
no como traducción editorialmente cerrada.

La primera auditoría residual aisló cinco bloques con rótulos astrológicos donde el modelo había
traducido el signo pero no el planeta ni `in`. La normalización admite ahora esas mezclas parciales.
Coincidió con los cinco bloques y con cero controles aceptados; en el EPUB completo redujo de 26 a cero
las colocaciones parciales visibles y los avisos bajaron de 41 a 25, sin cambiar 317 páginas, 297
bloques, 42 capítulos, 46 imágenes, integridad, OCR obligatorio ni incidencias PDF bloqueantes. El
mecanismo queda **VERIFICADO EN ESTE CENTINELA**.

La auditoría posterior de las veinte formas privadas expuestas separó trece errores probables —doce
defectos distintos porque el encabezado aparece duplicado— y siete falsos positivos o contenidos que
deben conservarse. El encabezado inglés y los residuos de tablas no admiten la misma regla mecánica.
Una ampliación del respaldo de títulos no produjo ninguna mejora medible en el EPUB y se descartó; no
se reescriben celdas ya traducidas sin evidencia semántica. El siguiente gate es una muestra humana
mínima de diez casos que cubra el encabezado, celdas realmente residuales, terminología de tablas y dos
controles bibliográficos/editoriales. Solo si distingue otra causa general separable se abre un
incremento automático; en caso contrario se calibra el informe y se continúa con los otros cinco
libros sin convertir respuestas en lógica por título.

La muestra quedó materializada como la tanda privada 2 del centinela: siete propuestas corregidas y
tres controles sin cambio, todos ligados por hashes a su fuente y candidato. Cuatro formas inicialmente
consideradas se excluyeron porque el recorte privado no permitía validar de manera completa estructura
o idioma; no se pide una decisión que después no pueda aplicarse con seguridad. La primera revisión
humana terminó con cinco aprobaciones y cinco correcciones solicitadas. Las decisiones, fuentes,
candidatos y propuestas pasaron sus hashes 10/10. La ronda correctiva de cinco casos incorpora las
indicaciones sobre mayúsculas editoriales, saltos visuales, puntuación y una celda incompleta. La
segunda revisión terminó con 5/5 aprobaciones y su linaje y hashes volvieron a coincidir. El lote queda
**COMPLETO** con diez referencias aprobadas: siete cumplen las puertas de publicación automáticas y
tres permanecen solo como diagnóstico —dos eliminan saltos visuales de tabla que la guarda estructural
anterior trataba como semánticos y una es una bibliografía breve cuyo idioma no puede decidirse de
forma fiable—. Ninguna respuesta se convirtió en una sustitución por libro.

Las dos observaciones de tabla sí revelaron una causa general de extracción. Una celda PDF une ahora
solo continuaciones visuales inequívocas —inicio en minúscula, puntuación abierta o palabra funcional
de continuación— y conserva frases cerradas, listas y rótulos separados. En el intervalo real de 24
páginas que originó la hipótesis, las tres continuaciones objetivo quedaron unidas y permanecieron 50
saltos internos estructurales. `decan` se añadió además a la memoria astrológica acotada: en una
regeneración completa, los residuos exactos `DECAN` bajaron de 94 a uno y `DECANO` aumentó de 526 a
835. Ambos mecanismos quedan **IMPLEMENTADOS** y verificados en ese alcance, no como aprobación de la
traducción completa.

La misma regeneración completa produjo 45 avisos `SOURCE_TEXT`, frente a 25 del mejor candidato
anterior, concentrados en celdas largas de las páginas 273–294. El EPUB conservó 317 páginas, 297
bloques, 42 capítulos, 46 imágenes, integridad final y cero fallos de OCR obligatorio, pero la pasada
fresca reintrodujo prosa inglesa: no se acepta como nueva base. Un fallback transaccional que dividía
la celda por oraciones redujo una muestra de 22 bloques a cuatro avisos y otro intervalo de 29 bloques
a tres, pero la repetición completa terminó de nuevo con 45. El mecanismo se retiró y queda
**RECHAZADO**: una muestra acotada sirvió para proponerlo, pero el libro completo decidió en contra.

También se rechazó ampliar Argos a rótulos o tablas. El par EN→ES instalado dejó sin traducir varios
rótulos breves y llegó a corromper uno; una frase de prosa aislada correcta no compensa esa precisión
insuficiente. Argos permanece como motor completo elegido por la persona y como respaldo ya instalado
para un único título residual bajo guardas. No se repetirá esta línea sin un mecanismo nuevo.

**Criterio de parada para este centinela:** se conserva el candidato `es-13` como mejor evidencia
editorial conocida, se conservan `es-14`/`es-15` como controles negativos privados y no se generan más
variantes de *36 Faces*. El siguiente incremento es ejecutar otro libro completo de los cinco
restantes y comprobar si los mecanismos generales transfieren; solo un patrón repetido en más de un
libro reabrirá traducción tabular.

Cuando traducción y estructura hayan cerrado sus gates independientes:

1. congelar versiones, opciones y hashes de entrada;
2. ejecutar primero intervalos representativos y después los libros completos;
3. comparar extracción, traducción, estructura y EPUB por separado;
4. revisar únicamente las excepciones que queden;
5. confirmar lectura real en más de un lector EPUB;
6. registrar tiempos, memoria, reintentos y atención humana agregada;
7. conservar los libros como holdouts finales, no como fuente de nuevas reglas específicas.

### U1 — SIGUIENTE: pulido de experiencia

Solo después del gate integral, revisar si la interfaz expresa con claridad:

- qué hará Parsezen;
- qué está haciendo;
- qué evidencia obtuvo;
- qué quedó sin demostrar;
- cuál es la siguiente acción segura.

No añadir paneles técnicos permanentes. La explicación detallada y el diagnóstico aparecen bajo
demanda; la ruta principal conserva una acción dominante por contexto.

## Aparcado o rechazado

| Idea | Estado | Condición para reabrir |
|---|---|---|
| sustituir Hy-MT2 por otro modelo general | RECHAZADO por ahora | candidato local con procedencia y ventaja clara en corpus humano |
| prompt global con más contexto | RECHAZADO | mecanismo acotado que no repita la degradación de 13/14 casos |
| `repeat_penalty=1.05` global | RECHAZADO | evidencia estable y significativa en holdout |
| razonamiento abreviado de LFM | RECHAZADO | adaptador nuevo que mejore precisión y recall sin degradar referencias |
| corrección automática por longitud/idioma global | RECHAZADO | señal causal demostrable; esos avisos siguen siendo solo revisión |
| aprobar por pasar guardas estructurales | RECHAZADO | nunca: las guardas no demuestran sentido ni naturalidad |
| ejecutar revisión adicional proactiva por defecto | RECHAZADO por ahora | beneficio semántico general y coste aceptable demostrados |

## Registro mínimo al actualizar este plan

Una actualización debe responder:

- qué estado cambió y con qué evidencia fechada;
- qué decisión reemplaza, si alguna;
- qué gate se cerró o abrió;
- cuál es ahora la única frontera activa;
- qué no debe repetirse sin una hipótesis nueva.

Las métricas detalladas y el contenido permanecen en informes privados. Este plan conserva solo la
conclusión necesaria para dirigir el siguiente trabajo.
