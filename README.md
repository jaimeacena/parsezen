![Parsezen](assets/branding/generated/parsezen-readme.png)

## ***Convierte documentos complejos en contenido útil.***

El nombre *Parsezen* proviene de *parse* (extraer y estructurar información) con *zen* (hacerlo de forma sencilla y fluida).

Parsezen transforma PDFs, documentos de Word y otros archivos con texto, imágenes y tablas en
Markdown limpio o en un EPUB organizado. El procesamiento directo comprueba el resultado y, solo si
encuentra señales concretas, puede proponerte una revisión local de los bloques afectados. También
puedes elegir de antemano una revisión completa; la traducción opcional usa Argos o un modelo local
de Ollama y nunca envía el documento fuera del equipo. Es open source, privado y gratuito.


## Principales características

- **Obtén Markdown o EPUB listos para usar**. Obtén Markdown limpio y estructurado para tus notas, tu base de conocimiento o tus herramientas de IA, o crea EPUB por capítulos con control total sobre portada, metadatos, estructura, contenido y formato.

- **Procesamiento avanzado de documentos**. Extrae texto, imágenes y tablas, aplica OCR a páginas escaneadas, selecciona únicamente las páginas que te interesen y mejora el resultado con IA local.

- **Traducción y revisión trazables**. Combina traducción algorítmica o mediante IA con
  glosarios y memoria terminológica. El resultado distingue una corrección integrada de una
  verificación bilingüe independiente y cuenta bloques comprobados, revisados y pendientes.

- **Escalable y preparado para trabajos largos**. Trabaja con varios documentos, consulta el tiempo estimado, pausa el proceso y reanúdalo cuando quieras o repite solo la fase que haya fallado.

- **Gratis, con IA local fácil de configurar**. Todo se procesa gratis en tu equipo, sin modificar los archivos originales. Instala Ollama, comprueba si tu equipo es compatible y elige un modelo adecuado mediante una configuración guiada y sin comandos.

- **Dos traducciones locales.** Elige Argos cuando priorices rapidez y consumo predecible, o el
  modelo local de Ollama cuando quieras una traducción dependiente de su contexto. Ambos recorridos usan el glosario,
  la memoria terminológica y las mismas guardas de cifras, enlaces, estructura, idioma y cobertura.
  El plan revisado es una decisión aparte y puede comprobar después cualquiera de las dos salidas.

- **Local, privado y gratuito.** Tus documentos permanecen en tu equipo y los originales nunca se
  modifican. Parsezen verifica su identidad local antes de procesarlos y solo habilita la IA cuando
  la configuración persistente de Ollama desactiva su nube. No necesitas suscripciones, cuotas ni
  pagos por uso.

- **Tú conservas el control.** Parsezen te muestra los cambios dudosos, te permite comparar el
  original con la propuesta y comprueba el resultado antes de publicarlo. Una recomendación nunca
  ejecuta IA por sí sola y puedes ignorarla sin perder el resultado ya creado.

- **Pensado para trabajos largos.** Puedes procesar varios documentos, consultar el tiempo
  aproximado, pausar, continuar más tarde y reintentar únicamente la fase que haya fallado. En EPUB,
  también se conserva de forma privada cada subfragmento de IA ya validado y cada decisión segura de
  mantener la traducción dentro de un capítulo.

- **EPUB sin pasos innecesarios.** Antes de publicar confirmas título, autor, idioma, portada y
  capítulos. El editor completo sigue disponible cuando quieres ajustar estructura o contenido.

- **Jerarquía conservadora.** Solo anida un contenedor explícito cuando encuentra al menos dos
  capítulos inequívocos contiguos; los casos ambiguos permanecen planos y conservan su orden.

- **Esquema global verificable.** La revisión estructural combina índice, páginas, geometría,
  encabezados y roles semánticos, pero solo aplica directivas de nivel sobre palabras ya existentes.
  Antes de decidir, muestra los árboles actual y propuesto.

- **IA local sin complicaciones.** Parsezen te ayuda a instalar Ollama, comprobar tu equipo y elegir
  un modelo adecuado sin que tengas que utilizar comandos.

## Cómo usar Parsezen

1. **[Descarga la última versión](https://github.com/jaimeacena/parsezen/releases/latest).** Necesitas Windows de 64 bits, pero no tienes que instalar Python.

2. **Añade tu documento.** Puedes trabajar con PDF, Word, EPUB, Markdown y archivos de texto.
3. **Configura el resultado.** Dentro de Parsezen, dos tarjetas claras permiten elegir Markdown o
   EPUB; traducción, páginas y OCR usan filas breves `Etiqueta — Valor — ›`, y la revisión con IA un
   único interruptor. Al elegir un idioma aparecen traductor y glosario; un intervalo se resume como
   `25–140`. Bajo el traductor se explica el recorrido efectivo, el número de pasadas y un coste
   cualitativo que cambia con el formato y la revisión elegidos. Cada elección válida se guarda al
   instante, sin pie de acciones ni scroll en el tamaño normal.
4. **Procesa y revisa.** Parsezen extrae y organiza el contenido, conserva las imágenes y tablas
   compatibles y te muestra cualquier decisión pendiente antes de publicar.

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
probarse sin arrastrar la interfaz o SQLite. Las decisiones y comandos de verificación están en la
[documentación de arquitectura](docs/architecture.md).

## Licencia

Parsezen es gratuito y se distribuye bajo licencia [MIT](LICENSE).
