# Modelo operativo para agentes

Este documento es la puerta de entrada para trabajar en Parsezen. No describe cada detalle del
producto: explica cómo reconstruir su estado, decidir con evidencia, realizar el cambio mínimo y
dejar el sistema más comprensible que antes. El estado y el orden de trabajo actuales viven en
[`work-plan.md`](work-plan.md); el diseño implementado, en [`architecture.md`](architecture.md).

## Misión y función objetivo

La misión principal es convertir documentos —sobre todo PDF— en EPUB fieles, precisos, legibles y
recuperables, con traducción local opcional. El sistema optimiza en este orden:

1. **no perder, inventar, filtrar ni corromper contenido**;
2. **conservar significado, estructura, formato y procedencia demostrables**;
3. **hacer visible la incertidumbre y mantener reversible toda decisión dudosa**;
4. **permitir reanudar e invalidar solo el trabajo dependiente**;
5. **reducir tiempo, memoria, llamadas de IA y atención humana**;
6. **reducir pasos de interfaz sin ocultar decisiones**.

Un objetivo inferior nunca justifica degradar uno superior. Una salida más rápida que pierde una
cifra es peor; una salida válida que oculta 65 dudas no está «verificada»; una propuesta elegante sin
evidencia no es una mejora.

## Ruta de entrada de cinco minutos

Un agente comienza siempre por esta secuencia y se detiene cuando ya tiene contexto suficiente:

1. leer `AGENTS.md`, este documento y el bloque **Ahora** de `work-plan.md`;
2. inspeccionar rama, estado, diff y último commit sin modificar nada;
3. identificar el contrato afectado y su fuente de verdad en la tabla siguiente;
4. leer solo la sección correspondiente de arquitectura, política o interfaz;
5. localizar código y pruebas por nombre de dominio, no recorriendo todo el repositorio;
6. formular una hipótesis falsable, el riesgo principal y la comprobación que decidirá el cambio.

No se enumera recursivamente `local-benchmarks/`: contiene documentos, renders, entornos y resultados
privados muy voluminosos. Se entra por un manifest o informe conocido, se consultan primero sus
metadatos agregados y solo se abre contenido concreto cuando la tarea lo requiere. Tampoco se vuelven
a leer documentos extensos que no pertenecen al área cambiada.

## Fuentes de verdad

Cada pregunta tiene un propietario documental. Los demás documentos pueden enlazarlo o resumirlo,
pero no crear una política paralela.

| Pregunta | Fuente autoritativa | Proyecciones o evidencia |
|---|---|---|
| ¿Qué puede hacer un agente y qué está prohibido? | `AGENTS.md` | seguridad y políticas específicas |
| ¿Cuál es la misión y qué ofrece el producto? | `README.md` | guía de uso |
| ¿Qué está implementado y quién posee cada estado? | `docs/architecture.md` | código y pruebas |
| ¿Qué toca ahora y qué queda después? | `docs/work-plan.md` | resultados privados fechados |
| ¿Qué debe demostrarse antes de publicar? | `docs/acceptance-checklist.md` | ejecución de pruebas y evidencias |
| ¿Qué IA puede usarse y con qué alcance? | `docs/local-ai-model-policy.md` | manifests y benchmarks privados |
| ¿Cómo se proyecta el sistema a la persona? | `docs/ui-design-system.md` | guía de uso y pruebas visuales |
| ¿Qué datos se protegen y cuál es la amenaza? | `SECURITY.md` | pruebas de persistencia y privacidad |
| ¿Qué cambió entre versiones? | `CHANGELOG.md` | historial Git y releases |

Si código, prueba y documento discrepan, no se elige la versión más conveniente: se comprueba el
comportamiento real, se clasifica la discrepancia y se corrigen juntos la fuente autoritativa y sus
proyecciones. Un resultado privado puede cambiar una decisión de producto, pero no reescribe por sí
solo la arquitectura implementada.

## Torre de abstracciones

Parsezen se entiende de arriba abajo como una única cadena de compromisos:

```text
Misión y prioridades
        ↓ restringen
Invariantes de producto y seguridad
        ↓ compilan
Intención del documento + ExecutionPlan
        ↓ gobiernan
Preparar → Traducir → Refinar → Estructurar → Publicar
        ↓ producen
Artefactos, informes, cobertura y propuestas trazables
        ↓ atraviesan
Guardas deterministas → revisión humana cuando aporta información
        ↓ autorizan
Publicación atómica + libro mayor de integridad
        ↓ proyectan
OutcomeSummary, cola, actividad e interfaz
        ↓ alimentan
Corpus, holdouts, decisiones y siguiente experimento
```

Cada nivel comprime al anterior sin contradecirlo. La interfaz no inventa estado; un informe no
autoriza una publicación; una métrica no sustituye una comparación semántica; una revisión no cambia
el original; un experimento no modifica el producto hasta superar su gate.

### Espina dorsal de un documento

Para razonar sobre cualquier recorrido, el agente sigue estas identidades:

1. `DocumentSource` fija el origen.
2. `JobConfiguration` expresa la intención de la persona.
3. `ExecutionPlan` es la única secuencia fina de trabajo.
4. `PreparedDocument` conserva conversión, recursos y evidencia inicial.
5. `TransformedDocument` conserva texto transformado, calidad y propuestas.
6. `ReviewSession`, cuando el plan lo necesita, añade decisiones humanas sin sustituir artefactos
   silenciosamente.
7. `BookDocument`, cuando la salida es EPUB, representa el libro editable y su jerarquía.
8. `FinalIntegrityReport` decide si el candidato puede publicarse.
9. `OutcomeSummary` explica el resultado sin convertir ausencia de avisos en certeza semántica.

Un cambio debe indicar qué identidad recibe, cuál produce, qué puede invalidar y quién consume su
resultado. Si necesita una copia paralela del mismo estado, probablemente cruza mal una frontera.

## Estados comunes del conocimiento

Toda afirmación importante usa uno de estos estados:

- **IMPLEMENTADO**: existe en producto y cuenta con pruebas proporcionales al riesgo;
- **VERIFICADO EN CORPUS**: además superó un corpus o holdout identificado, en condiciones registradas;
- **EXPERIMENTAL**: está aislado y no altera el flujo normal ni se promociona automáticamente;
- **PLANEADO**: dirección aceptada, todavía sin afirmar que funciona;
- **RECHAZADO**: se probó y la evidencia desaconseja repetirlo sin una hipótesis nueva;
- **BLOQUEADO**: falta una autoridad, dato o decisión externa concreta.

«Verde», «listo», «mejor» y «verificado» no se usan sin alcance. Se dice qué corpus, qué gates, qué
fecha o versión y qué quedó fuera. Un caso conservado por seguridad cuenta como no resuelto, no como
éxito semántico.

## Bucle de trabajo acumulativo

### 1. Orientar

- traducir la petición a una propiedad observable del sistema;
- consultar `work-plan.md` para no reabrir una decisión cerrada;
- identificar propietario, consumidores, persistencia, UI y pruebas del contrato;
- distinguir diagnóstico, cambio de producto, experimento privado y trabajo editorial.

### 2. Establecer la línea base

- comprobar el estado real antes de editar;
- conservar cambios ajenos y resultados existentes;
- ejecutar primero la prueba mínima que reproduce la duda;
- registrar únicamente metadatos seguros: versiones, conteos, hashes, tiempos y estados.

### 3. Elegir una hipótesis

Una unidad de trabajo debe poder expresarse así:

> Si cambiamos **X** por la evidencia **Y**, mejorará **Z** sin degradar **A/B/C**; lo decidiremos con
> **esta comprobación**.

No se mezclan en un mismo experimento cambios de extracción, traducción y estructura. Si el resultado
mejora, debe ser posible atribuirlo a una causa.

### 4. Implementar el mínimo corte vertical

El corte correcto atraviesa las capas necesarias —contrato, caso de uso, persistencia, proyección y
prueba—, pero no añade una abstracción para una necesidad futura. Se reutilizan el plan, los informes,
las guardas y los almacenes existentes antes de crear otro estado.

### 5. Verificar en escalera

Se sube solo mientras el peldaño anterior pasa:

1. prueba focal o reproducción;
2. pruebas del módulo y sus contratos vecinos;
3. lint y formato;
4. suite completa cuando el cambio afecta producto;
5. canarios sintéticos;
6. corpus privado de desarrollo;
7. holdouts no usados para diseñar la regla;
8. revisión humana solo para juicios que el sistema no puede demostrar;
9. recorrido EPUB final e integridad comparativa cuando corresponda.

Un cambio solo documental no requiere fingir una validación del pipeline; sí exige revisar enlaces,
contradicciones, formato y diff. Una modificación de producto no se da por terminada con pruebas
focales si cambia contratos compartidos.

### 6. Acumular

Cada trabajo deja un paquete mental compacto, normalmente en el resumen de la tarea y, si cambia una
decisión duradera, en su documento autoritativo:

- intención y riesgo;
- estado anterior y evidencia usada;
- cambio realizado;
- comprobaciones ejecutadas y resultado;
- límites y casos no resueltos;
- decisión: promover, mantener experimental, rechazar o aplazar;
- siguiente condición que justificaría reabrirla.

No se crean diarios narrativos por cada intento. Las referencias humanas aprobadas se acumulan en el
corpus privado; los rechazos se conservan como controles negativos; las conclusiones agregadas y sin
contenido se registran en política o plan.

## Economía de evidencia

El orden de recursos es deliberado:

```text
reutilizar evidencia existente
        ↓
comprobación determinista barata
        ↓
muestra representativa y acotada
        ↓
modelo local especializado sobre la mínima unidad
        ↓
revisión humana de casos informativos
        ↓
ejecución completa solo al superar los descartes anteriores
```

- Los checkpoints se ligan a contenido y opciones para no repetir trabajo válido.
- La invalidación avanza solo hacia dependientes; nunca reinicia todo por comodidad.
- Una muestra pequeña descarta; no aprueba por sí sola.
- Los holdouts no se usan para redactar reglas específicas de un libro.
- La atención humana se reserva para ambigüedad semántica, no para confirmar guardas mecánicas.
- Una tanda humana es pequeña, diversa y deduplicada. Si las propuestas fallan de forma repetida, se
  detiene la generación de variantes y se diagnostica el mecanismo antes de pedir otra revisión.
- Precisión prima recall: ante duda se conserva la base validada y se hace visible el pendiente.

## Protocolo de experimentos y aprendizaje

Un experimento privado separa siempre:

1. **desarrollo**: casos que permiten entender y construir la hipótesis;
2. **controles limpios**: contenido que no debería cambiar;
3. **errores sembrados o referencias humanas**: verdad esperada explícita;
4. **holdout**: documentos que no participaron en la regla;
5. **informe compartible**: solo identidad opaca, versiones, hashes y métricas agregadas.

El resultado responde por separado a detección, propuesta y aceptación. «No propuso cambios» no es
precisión; «pasó las guardas» no es corrección semántica; «generó un EPUB» no demuestra fidelidad.
Una propuesta entra en el corpus aprobado solo tras decisión humana inequívoca. Los casos rechazados
no se vuelven a presentar con otra paráfrasis salvo que exista una causa nueva y comprobable.

## Mapa de impacto para cambios

Antes de editar, el agente recorre solo las columnas relevantes:

| Cambio | Propietario | Dependientes que revisar | Evidencia mínima |
|---|---|---|---|
| fases o configuración | dominio / `ExecutionPlan` | preflight, runtime, explicación, cola, persistencia | contratos + matriz de flujos |
| extracción PDF/OCR | procesador PDF | checkpoints, semántica, revisión, EPUB | canarios + corpus + reanudación |
| traducción o guardas | transformador / calidad | checkpoints, revisión, cobertura, política IA | pares humanos + limpios + holdout |
| jerarquía EPUB | semántica / `BookDocument` | editor, TOC, spine, enlaces | árbol esperado + EPUBCheck |
| publicación | publicador / integridad | reemplazo atómico, recuperación, actividad | fallo inyectado + contenedor final |
| persistencia | repositorios | recuperación, invalidación, limpieza | round-trip + corrupción/interrupción |
| interfaz | presentación | accesibilidad, reflow, foco, guía | tests Qt + visuales + teclado |
| política o documentación | documento autoritativo | enlaces y proyecciones | diff + coherencia cruzada |

## Criterio de parada

Se detiene una línea de trabajo cuando ocurre cualquiera de estas condiciones:

- el gate definido ya se cumple y el siguiente cambio solo optimiza una métrica secundaria;
- dos intentos equivalentes fallan sin aportar una hipótesis nueva;
- la mejora solo funciona para un documento y no existe una regla general demostrable;
- el coste de revisión o ejecución crece más que la información obtenida;
- continuar requiere una decisión de producto, licencia, privacidad o contenido que solo puede tomar
  la persona usuaria.

Detenerse no borra el aprendizaje: se registra el resultado como **RECHAZADO**, **EXPERIMENTAL** o
**BLOQUEADO**, se preserva la evidencia útil y se vuelve a la siguiente frontera del plan.

## Mantenimiento de este sistema documental

- `architecture.md` describe solo comportamiento implementado y decisiones estructurales vigentes.
- `work-plan.md` es breve, fechado y contiene estado actual, próximo gate y temas aparcados.
- `acceptance-checklist.md` contiene criterios verificables, no una narración histórica.
- las políticas conservan decisiones, alcance y evidencia que las justifica.
- `README.md` y `user-guide.md` proyectan el producto para personas, no el backlog interno.
- cuando una decisión cambia, se actualiza primero su fuente autoritativa y luego sus proyecciones.
- cuando solo cambia el progreso, se actualiza el plan, no se reescribe la arquitectura.
