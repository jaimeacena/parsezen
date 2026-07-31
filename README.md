![Parsezen](assets/branding/generated/parsezen-readme.png)

## ***Convierte documentos complejos en contenido útil.***

El nombre *Parsezen* proviene de *parse* (extraer y estructurar información) con *zen* (hacerlo de forma sencilla y fluida).

Parsezen transforma PDFs, documentos de Word y otros archivos con texto, imágenes y tablas en Markdown limpio o en un EPUB organizado. Durante el proceso puedes traducir, corregir y estructurar el contenido. Es open source, privado y 100 % gratuito.


## Principales características

- **Obtén Markdown o EPUB listos para usar**. Obtén Markdown limpio y estructurado para tus notas, tu base de conocimiento o tus herramientas de IA, o crea EPUB por capítulos con control total sobre portada, metadatos, estructura, contenido y formato.

- **Procesamiento avanzado de documentos**. Extrae texto, imágenes y tablas, aplica OCR a páginas escaneadas, selecciona únicamente las páginas que te interesen y mejora el resultado con IA local.
  
- **Traducción y revisión precisas**. Combina el procesamiento con traducción (algorítmica o mediante IA), usando glosarios y memoria terminológica para mantener nombres y términos consistentes. Revisa los cambios dudosos y valida el resultado antes de finalizar.
  
- **Escalable y preparado para trabajos largos**. Trabaja con varios documentos, consulta el tiempo estimado, pausa el proceso y reanúdalo cuando quieras o repite solo la fase que haya fallado.
  
- **Gratis, con IA local fácil de configurar**. Todo se procesa gratis en tu equipo, sin modificar los archivos originales. Instala Ollama, comprueba si tu equipo es compatible y elige un modelo adecuado mediante una configuración guiada y sin comandos.


## Cómo usar Parsezen

1. **[Descarga la última versión](https://github.com/jaimeacena/parsezen/releases/latest).** Necesitas Windows de 64 bits, pero no tienes que instalar Python.
   
2. **Añade tu documento.** Puedes trabajar con PDF, Word, EPUB, Markdown y archivos de texto.

3. **Configura el resultado.** Elige Markdown o EPUB y activa la traducción, corrección o personalización que necesites.
   
4. **Procesa y revisa.** Parsezen extrae y organiza el contenido, conserva las imágenes y tablas compatibles y te muestra cualquier decisión pendiente antes de publicar.

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

## Licencia

Parsezen es gratuito y se distribuye bajo licencia [MIT](LICENSE).