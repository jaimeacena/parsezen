# Instrucciones para agentes

- Trabaja siempre dentro del checkout existente de `parsezen`; no crees otro repositorio.
- Antes de modificar, inspecciona estado, archivos, historial, ramas, configuración e instrucciones.
- Avanza por incrementos verificables y no implementes fases futuras sin una petición explícita.
- Mantén la arquitectura pequeña; evita capas, interfaces y abstracciones sin una necesidad actual.
- Mantén PySide6 fuera de la lógica de procesamiento.
- Mantén el sistema visual nuevo en `presentation/design_system.py`; `theme.py` es únicamente una
  compatibilidad transitoria para diálogos aún no extraídos. Conserva el branding oficial, la
  tipografía legible, el foco visible y el layout sin scroll horizontal.
- Nunca envíes documentos a servicios remotos. La IA usa únicamente Ollama mediante su API nativa
  fija en `127.0.0.1`; no añadas proveedores remotos ni compatibilidad con OpenAI.
- Mantén la traducción sin LLM gratuita y offline con Argos; solo sus paquetes públicos de idioma
  pueden descargarse y nunca el contenido del usuario.
- El selector activo de modelos debe proceder de Ollama `GET /api/tags`; el asistente de instalación
  puede recomendar con el `llmfit` local administrado o aceptar un nombre de catálogo validado, pero
  nunca un endpoint. Un modelo solo puede quedar seleccionado después de aparecer en `/api/tags`.
  Muestra nombres amigables y excluye siempre tags `:cloud`/`-cloud`.
- Mantén el OCR completamente local, con servicios remotos y plugins externos desactivados; una
  capa de texto útil tiene prioridad sobre una interpretación OCR incierta.
- Mantén EPUB en `epub_conversion.py`: paquete/índice con `zipfile` y `defusedxml`, XHTML→Markdown
  mediante MarkItDown y traducción EPUB→EPUB modificando solo XML protegido. Conserva recursos
  binarios byte por byte; no añadas EbookLib/AGPL sin una decisión explícita de licencia del producto.
- Mantén la reanudación EPUB en `epub_checkpoints.py`: partes semánticas independientes de capítulos,
  caché privada ligada al contenido y opciones, escritura atómica, validación antes de reutilizar y
  limpieza solo después de publicar correctamente. La cola durable pertenece a
  `infrastructure/state_store.py`.
- Mantén separados los rechazos críticos y el informe de traducción: los primeros protegen la salida;
  el segundo solo orienta una revisión local con extractos limitados. No crees puntuaciones, archivos
  sidecar ni logs con texto documental.
- La reparación automática de traducción solo reintenta una vez bloques alineados con texto original
  residual. Acepta el reemplazo únicamente si supera las guardas compartidas; longitud, idioma global y
  alineación dudosa siguen siendo avisos para revisar, no motivos para reescribir contenido.
- Usa EPUBCheck 5.3.0 solo como validación interna comparativa; no añadas Java ni EPUBCheck a las
  dependencias o al instalador sin una decisión explícita.
- No registres contenido documental, prompts completos, respuestas completas ni rutas sensibles.
- Conserva el contenido existente y evalúa las decisiones previas antes de cambiarlas.
- Ejecuta pytest, `ruff check .` y `ruff format --check .` para cada cambio aplicable.
- Actualiza README y arquitectura cuando cambien comportamiento, límites o decisiones.
- No afirmes que algo funciona sin haber ejecutado la comprobación correspondiente.
