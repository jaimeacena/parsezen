# Sistema de interfaz de Parsezen

Este documento registra la auditoría, las decisiones y el contrato visual de la interfaz nativa
PySide6. La referencia funcional continúa siendo el dominio y la arquitectura descritos en
`docs/architecture.md`; el sistema visual no introduce estados de negocio paralelos.

## Alcance y supuestos

- La persona usuaria trabaja en Windows con documentos locales y necesita poder terminar el flujo
  sin conocer la arquitectura interna.
- Seguridad del original, recuperación del trabajo y decisiones conservadoras de revisión tienen
  prioridad sobre la reducción de pasos.
- La aplicación es nativa, no una web: las “rutas” son páginas de la pila de la ventana principal y
  diálogos integrados.
- La marca teal y la tipografía Inter se conservan, pero los colores de marca se usan mediante
  roles semánticos.
- No se cambian contratos de procesamiento, formatos, persistencia ni orden de fases.
- Un ancho de 320 px es un caso de reflow y zoom, no el tamaño recomendado para editar un libro
  largo. Ninguna acción esencial puede desaparecer en ese ancho.

## Arquitectura e inventario

La presentación activa se organiza así:

```text
ParsezenMainWindow
└── ParsezenWorkspace
    ├── cola de documentos + destino + acción contextual
    ├── hoja modal de configuración: Resultado / Traducción / Más opciones
    ├── IA local y modelos
    ├── glosario
    ├── revisión por fase
    ├── revisión final
    ├── editor EPUB
    └── diagnóstico y revisiones de calidad
```

Piezas principales:

- `presentation/design_system.py`: única fuente de colores, escalas, tema y vectores comunes.
- `presentation/components.py`: controles compartidos accesibles.
- `presentation/main_window.py`: composición de la aplicación y proyección del estado de dominio.
- `presentation/processing_runner.py`: ejecución física, cancelación y eventos de procesamiento.
- `presentation/local_ai_controller.py`: descubrimiento, recomendaciones e instalación de IA local.
- `presentation/workspace.py`: layout, navegación interna, foco y reflow global.
- `presentation/job_table.py`: proyección responsive de la cola.
- `presentation/job_configuration_dialog.py`: transacción de configuración por documento.
- `presentation/phase_review_dialog.py`, `presentation/revision_dialog.py` y
  `presentation/markdown_review.py`: decisiones y comparaciones.
- `presentation/book_editor_dialog.py`: metadatos, portada, estructura y contenido EPUB.
- `presentation/model_manager.py`: estado, búsqueda y selección de IA local.

La lógica de aplicación y dominio no importa PySide6. Los widgets reciben proyecciones y emiten
intenciones; no inventan estados de cola, revisión o publicación.

## Línea base del 29 de julio de 2026

Antes de modificar:

| Comprobación | Resultado |
| --- | --- |
| Ruff check y format | correctos |
| Mypy | correcto, 87 módulos |
| Pytest | 1.157 aprobadas, 3 omitidas |
| Build Windows | correcto |
| Smoke test del paquete | correcto |
| Arranque fuente, media de 3 | 1.325,1 ms |
| Paquete | 1.090.521.213 bytes, 7.520 archivos |
| Ejecutable | 80.832.609 bytes |
| Python de producto | 87 archivos, 46.871 líneas, 1,78 MB |

Avisos preexistentes del build: deprecaciones de Torch, un `SyntaxWarning` de Stanza y avisos de
colección de TensorBoard. No proceden de esta refactorización.

Inventario visual inicial:

- 248 apariciones hexadecimales y 185 tonos distintos en el repositorio de producto.
- Dos hojas globales parcialmente solapadas: el tema oliva heredado y el sistema teal.
- Preferencia binaria claro/oscuro, sin opción de seguir Windows.
- Anchos mínimos efectivos aproximados: ventana principal 1.120 px, workspace 829 px,
  configuración 666 px y gestor de modelos 1.186 px.
- Contrastes insuficientes en claro: texto atenuado 3,18:1, enlace teal 3,07:1,
  texto blanco sobre acción primaria 3,22:1 y bordes cercanos a 1,67:1.
- Errores de trabajo mostrados en modales bloqueantes y foco no restaurado de forma uniforme.
- Barras y pies rígidos, pestañas que desbordaban, paneles comparativos horizontales y tablas que
  dependían de un ancho de escritorio.

Las capturas iniciales se conservaron fuera del repositorio en
`%TEMP%\parsezen-ui-refactor-captures`.

## Problemas priorizados

| Prioridad | Problema | Impacto | Solución |
| --- | --- | --- | --- |
| P0 | Contraste insuficiente y foco inconsistente | lectura y teclado | paletas AA, foco de 2 px y roles de estado |
| P0 | Reflow impedido por mínimos internos | tareas inaccesibles con zoom o ventana estrecha | layouts sin restricción, grids responsive y paneles verticales |
| P0 | Dos lenguajes visuales | estados ambiguos y mantenimiento costoso | una única fuente de tokens y eliminación del tema y shell heredados |
| P1 | Error/validación modal | pérdida de contexto | mensaje inline recuperable, foco en el control y reintento |
| P1 | Información esencial oculta al compactar | tarea incompleta | composición de columnas, texto accesible y acciones conservadas |
| P1 | Tema sin modo sistema y posible destello | incoherencia de preferencia | aplicación previa a construir la ventana, persistencia y escucha de Windows |
| P2 | Iconos tipográficos ambiguos | reconocimiento irregular | vectores pintados con Qt, nombre accesible y tooltip |
| P2 | Alturas y densidad arbitrarias | jerarquía inestable | escala de controles de 32/40/44 px |

## Principios de producto

1. Una acción principal por contexto; las alternativas permanecen visibles como secundarias.
2. No se elimina información al compactar: se reagrupa y se elide visualmente con nombre accesible
   completo.
3. Color, icono y texto trabajan juntos para estados de error, aviso, éxito y fase.
4. Los formularios mantienen etiquetas visibles y validación junto al contenido que debe corregirse.
5. Las revisiones conservan siempre la comparación y la elección; en compacto pasan de dos columnas
   a una secuencia vertical.
6. La configuración sigue siendo una transacción compacta sobre la cola: resultado y traducción son
   las únicas decisiones principales; las excepciones se revelan de forma progresiva.

## Densidad y elevación

La evolución visual de julio de 2026 elimina la elevación como valor predeterminado:

- el lienzo, la superficie y la superficie elevada se diferencian por pequeños cambios de
  luminosidad;
- `divider` organiza listas y secciones sin convertir cada bloque en una tarjeta;
- `border` se reserva para límites funcionales que deben reconocerse, como campos y foco;
- las cabeceras internas usan un divisor inferior, no un contenedor redondeado;
- las filas de documentos y modelos usan divisores horizontales y una selección suavemente teñida;
- los botones de icono y las acciones globales secundarias son `ghost` hasta hover o foco;
- los estados destructivos no son rojos de forma permanente;
- los grupos de formulario se construyen con etiqueta superior, ritmo vertical y ayuda contextual.

Las recetas de sombra están centralizadas en `ShadowTokens`, pero el nivel normal es cero. Solo un
overlay que pierda jerarquía frente al contenido puede usar la elevación sutil; paneles, filas y
formularios no la utilizan.

## Tokens base

### Tipografía

Inter se carga desde los recursos del producto y cae a Segoe UI si no está disponible.

| Rol | Tamaño |
| --- | --- |
| cuerpo | 10 pt |
| cuerpo pequeño | 9 pt |
| etiqueta | 10 pt |
| título | 18 pt |
| display | 24 pt |
| pesos | 400 / 550 / 650 |

### Escalas

| Familia | Valores |
| --- | --- |
| espacio | 2, 4, 8, 12, 16, 24, 32, 48 px |
| control | compacto 32, normal 38, cómodo 44 px |
| icono | 16, 20, 24 px |
| radio | 6, 10, 14 px y píldora |
| breakpoint | 640, 960 y 1.280 px |
| movimiento | 100, 180 y 280 ms |
| capas | 0, 10, 100, 200 y 300 |

No se anima contenido cuando Windows tiene desactivadas las animaciones de cliente.

## Paleta semántica

Los nombres describen función, no pigmento. Los componentes no contienen colores concretos.

### Tema claro

| Rol | Valor |
| --- | --- |
| fondo / superficie / superficie sutil | `#F3F6F8` / `#FCFDFD` / `#EEF2F4` |
| texto primario / secundario / atenuado | `#0B1F2A` / `#3D5663` / `#5F7480` |
| divisor / borde / foco | `#D5DFE3` / `#718A95` / `#08767D` |
| acción primaria / hover / active | `#08767D` / `#095E64` / `#064C51` |
| información / éxito / aviso / error | `#005E8A` / `#0B7548` / `#8A4B00` / `#B4232A` |

### Tema oscuro

| Rol | Valor |
| --- | --- |
| fondo / superficie / superficie elevada | `#0C1419` / `#121D23` / `#202E35` |
| texto primario / secundario / atenuado | `#E7F0F2` / `#B5C4CA` / `#8FA3AC` |
| divisor / borde / foco | `#2B3D45` / `#587684` / `#5ED1D3` |
| acción primaria / hover / active | `#35C4C8` / `#5ED1D3` / `#23A4A8` |
| información / éxito / aviso / error | `#63C3F0` / `#63D39A` / `#F6B85E` / `#FF9696` |

El oscuro utiliza superficies azul-gris, no negro puro, y texto `#E7F0F2`, no blanco puro. Las
categorías OCR, traducción, corrección y estructura tienen roles propios y no reutilizan
éxito/error.

### Matriz principal de contraste

Valores calculados con luminancia WCAG:

| Combinación | Claro | Oscuro |
| --- | ---: | ---: |
| texto primario / fondo | 15,56:1 | 16,06:1 |
| texto secundario / fondo | 7,14:1 | 10,37:1 |
| texto atenuado / fondo | 4,50:1 | 7,08:1 |
| texto inverso / acción primaria | 5,39:1 | 8,04:1 |
| borde / superficie | 3,57:1 | 3,54:1 |
| foco / fondo | 4,96:1 | 10,21:1 |
| texto de tooltip / overlay | 15,82:1 | 16,47:1 |

Los estados no dependen solo del color: incluyen copia, icono, borde, selección o patrón.

## Temas y preferencias

- `Sistema` es el valor inicial cuando no existe una preferencia.
- `Claro` y `Oscuro` fuerzan una apariencia concreta.
- La elección se guarda en `QSettings`.
- El cambio se aplica sin recarga a widgets abiertos, vistas Markdown e iconos pintados.
- El tema se instala antes de construir la ventana para evitar el destello incorrecto.
- Un cambio de esquema de Windows se escucha solo si la preferencia es `Sistema`.
- Qt recibe una paleta completa para controles nativos, enlaces, selección, placeholders y estados
  deshabilitados.
- Con alto contraste de Windows se retira la hoja de estilos y se usa la paleta nativa.

## Componentes compartidos

- `ChevronComboBox`: selector nativo con chevron vectorial, navegación de teclado y rueda desactivada
  cuando no tiene foco.
- `Switch`: interruptor con `Space`, foco visible, nombre accesible y estados on/off/disabled.
- `StatusMessage`: información, éxito, aviso o error inline; admite acción de recuperación y recibe
  foco cuando requiere atención.
- `HorizontalToolStrip`: conserva todas las herramientas del editor, permite desplazamiento
  horizontal controlado y evita ensanchar la ventana.
- Botones principales, secundarios, destructivos e icon buttons consumen la hoja semántica común.

Los controles nativos se mantienen cuando ya resuelven correctamente semántica, teclado y
accesibilidad.

## Responsive y reflow

Desde 320 px:

- la cabecera pasa a tres filas y conserva IA, destino, apariencia y acción principal;
- la cola combina Documento + Flujo y mantiene la acción contextual y eliminar;
- la zona de añadir documentos apila icono, instrucción, enlace y formatos;
- la configuración apila las dos tarjetas de resultado y mantiene un único selector de traducción;
- los formularios envuelven etiqueta y campo;
- las acciones de imágenes se apilan;
- los comparadores y el editor EPUB cambian a orientación vertical;
- el gestor de modelos distribuye nombre, tamaño, menú y acción en filas;
- los pies pasan a grids y ninguna acción esencial queda fuera del viewport.

Las barras horizontales solo aparecen dentro de tiras de herramientas explícitas. Las áreas de
contenido y tablas usan `ScrollBarAlwaysOff` horizontal.

## Accesibilidad

- foco visible de 2 px con contraste superior a 3:1;
- orden de tabulación explícito en el flujo principal;
- nombres accesibles para botones de icono, controles abreviados y acciones compactas;
- etiquetas visibles en formularios;
- selección de revisión expresada con control, texto y color;
- restauración del foco al cerrar una página interna;
- errores inline con foco y acción de recuperación;
- mensajes completos conservados en tooltip o nombre accesible cuando el texto se elide;
- interruptores operables con teclado;
- áreas de arrastre también activables con `Enter`, `Return` o `Space`;
- soporte de reducción de movimiento y paleta nativa de alto contraste.

Los controles compactos de 32 px se reservan a tiras densas; las acciones normales utilizan 40 px.

## Estados de datos y trabajo

La fuente de verdad continúa siendo `DocumentJob` y `StageState`. La presentación cubre:

- carga/preparación con progreso;
- actualización parcial de una fase;
- cola vacía con acción para añadir;
- configuración incompleta con acción contextual;
- resultado;
- error recuperable con revisión de configuración;
- revisión pendiente;
- pausa y cancelación;
- IA no disponible, sin modelos, analizando, instalando y preparada;
- éxito publicado.

No existe un loader indefinido nuevo. Las operaciones largas conservan progreso o una acción de
cancelación y los estados terminales mantienen explicación y siguiente paso.

## Excepciones y deuda conocida

- `epub_builder.py` conserva dos apariciones de `#777` dentro del CSS que se escribe en el EPUB.
  Es contenido interoperable del documento, no interfaz de Parsezen, y no puede depender del tema
  de la aplicación.
- `presentation/main_window.py` sigue siendo un coordinador amplio porque reúne navegación,
  proyección de cola y apertura de revisiones. El trabajo físico y la IA local ya están aislados en
  controladores propios; nuevas capacidades deben entrar en servicios equivalentes y no volver a
  crecer como estado oculto de widgets.
- PySide6 no ofrece un equivalente web de `forced-colors`; el modo de alto contraste se delega a la
  paleta estándar de Windows.

## Evidencia y regresión

Las pruebas automatizadas cubren tokens, contraste, tema de sistema, teclado del interruptor,
mensajes recuperables, persistencia de tema, reflow de la cola/configuración/editor/revisiones y
gestor de modelos. `tests/test_visual_regressions.py` renderiza la vista principal en claro y
oscuro a 320, 768 y 1.440 px, guarda capturas temporales por ejecución y comprueba geometría,
solapamientos, foco, hover y recorte de interruptores. Las capturas son evidencia diagnóstica del
test, no artefactos versionados ni una fuente de verdad manual.

Estado validado el 30 de julio de 2026 tras la evolución visual ligera:

- Ruff check y format: correctos.
- Mypy: correcto sobre 82 módulos de producto.
- Pytest: 1.076 pruebas aprobadas y 3 omitidas por depender de EPUBCheck u Ollama reales.
- Cobertura: 88,25 % sobre 22.417 sentencias, sin exclusiones nuevas.
- Matriz visual: 12 contratos aprobados, incluidos cola y gestor de modelos en ambos temas a
  320 px.
- Aceptación: 27 pruebas aprobadas.
- Arranque fuente e inspección manual: cola, Configurar e IA local comprobados en claro y oscuro.
- No se construyó ni empaquetó la aplicación durante esta fase.

Al modificar un token o componente base se deben ejecutar, como mínimo:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy src/parsezen
python -m pytest
python -m pytest --cov=parsezen --cov-report=term-missing --cov-fail-under=88
```

Para una entrega de Windows se añade el build de producción y el smoke test del paquete.
