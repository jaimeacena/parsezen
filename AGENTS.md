# Instrucciones para agentes

## Entrada y orientación

- Lee primero `docs/agent-operating-model.md` y el bloque **Ahora** de `docs/work-plan.md`. Usa
  `docs/architecture.md` como verdad del diseño implementado y `docs/acceptance-checklist.md` como
  contrato de verificación; no reconstruyas el estado del proyecto desde el historial de una tarea.
- Antes de modificar, inspecciona estado, diff, archivos, historial, rama, configuración e
  instrucciones. Identifica la fuente de verdad, el riesgo principal y la comprobación que decidirá
  el cambio.
- No enumeres recursivamente `local-benchmarks/` ni sus entornos o renders. Entra por un manifest o
  informe conocido, consulta primero métricas agregadas y abre solo el material privado imprescindible.
- Distingue siempre `IMPLEMENTADO`, `VERIFICADO EN CORPUS`, `EXPERIMENTAL`, `PLANEADO`, `RECHAZADO` y
  `BLOQUEADO`. No llames «verde», «listo» o «mejor» a algo sin indicar alcance y evidencia.

## Reglas de trabajo

- Trabaja siempre dentro del checkout existente de `parsezen`; no crees otro repositorio.
- Avanza por incrementos verificables y no implementes fases futuras sin una petición explícita.
- Formula una hipótesis por incremento. Separa detección, propuesta, guarda y publicación para que un
  resultado pueda atribuirse a una causa; una muestra pequeña puede descartar, pero no aprobar.
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
- Verifica en escalera: reproducción o prueba focal, contratos vecinos, lint/formato y suite completa
  cuando cambie producto; después usa canarios, corpus, holdouts y revisión humana según el riesgo.
- Actualiza primero la fuente autoritativa: arquitectura para comportamiento implementado, plan de
  trabajo para progreso, política para decisiones de IA, aceptación para gates y README/guía para su
  proyección a la persona. No dupliques el mismo estado en varios documentos.
- Al cerrar, registra intención, evidencia, cambio, verificaciones, límites, decisión y condición para
  reabrirla. Promueve referencias humanas y conserva rechazos como controles; no repitas variantes sin
  un mecanismo nuevo.
- No afirmes que algo funciona sin haber ejecutado la comprobación correspondiente.
