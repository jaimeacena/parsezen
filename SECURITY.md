# Seguridad

## Versiones compatibles

La versión estable más reciente de Parsezen recibe correcciones de seguridad. Las versiones
anteriores pueden dejar de recibirlas cuando se publica una actualización.

## Comunicar una vulnerabilidad

No publiques detalles explotables ni documentos de prueba en una incidencia abierta. Usa
**Security → Report a vulnerability** en el repositorio de GitHub para enviar un aviso privado.

Incluye una descripción del impacto, la versión afectada y pasos mínimos para reproducirlo sin
contenido documental privado. Se confirmará la recepción antes de publicar detalles o una
corrección.

## Privacidad

Parsezen procesa los documentos localmente. Una incidencia nunca debe adjuntar documentos,
prompts, respuestas completas, rutas personales, bases de datos de estado ni checkpoints.

## Modelo de amenazas local

La frontera de seguridad actual es la cuenta de Windows. Parsezen protege con DPAPI para el usuario
actual el contenido intermedio que necesita conservar: checkpoints, textos y recursos de revisión y
artefactos de reanudación. Un archivo protegido copiado fuera del perfil no debería poder descifrarse
desde otra cuenta. El contenido documental no se envía a servicios remotos: Ollama usa únicamente
loopback, Argos y OCR se ejecutan localmente y solo pueden descargarse paquetes públicos de idioma o
modelos solicitados por la persona.

Quedan fuera del alcance actual un proceso malicioso que ya se ejecute como la misma cuenta de
Windows, un administrador con control de la sesión, la lectura de memoria en vivo y el acceso físico a
los originales o resultados que la propia persona guarda sin cifrado de Parsezen. DPAPI no pretende
proteger frente al usuario que puede abrir la aplicación y los documentos.

### Metadatos conservados sin cifrar

Parsezen depende de los permisos del perfil de Windows para los siguientes archivos. No contienen el
cuerpo completo del documento, pero sí pueden revelar información sensible:

| Almacén | Metadatos en texto claro | Contenido excluido o protegido |
| --- | --- | --- |
| `state.db` (SQLite) | Rutas de origen, resultado, salida, imágenes y portada; tamaño, fecha y SHA-256 del origen; título, autor, idioma, glosario, rango de páginas, OCR, plan, modelo y contexto; estados, tiempos, avisos, errores seguros, decisiones e identificadores de artefactos; metadatos y títulos de navegación de un libro; métricas numéricas de proceso. | Textos completos, propuestas, ediciones y recursos binarios de revisión viven en artefactos DPAPI. Los eventos durables usan mensajes acotados sin extractos documentales. |
| `recent-jobs.json` | Hasta 20 rutas de origen/resultado, estado y hora; cronología técnica, contadores, advertencias numéricas y referencia de diagnóstico segura. | No guarda texto, prompts ni respuestas del documento. |
| `settings.json` | Modelo local, contexto, carpetas de salida e imágenes, timeout y retención. | No guarda documentos ni credenciales. |
| Logs locales | Eventos y tipos técnicos sanitizados, duraciones y contadores. | No deben incluir rutas sensibles, texto documental, prompts ni respuestas completas. |
| Checkpoints y `artifacts/` | Identificadores opacos, nombres técnicos y estructura mínima de directorios. | Sus payloads documentales están protegidos con DPAPI para el usuario actual. |

Los originales y los Markdown/EPUB publicados son archivos normales elegidos por la persona y no se
cifran ni se eliminan automáticamente. Los modelos de Ollama y paquetes de Argos tampoco son datos
documentales de Parsezen.

### Decisión y limpieza

La lectura offline de metadatos por alguien con acceso efectivo a la misma cuenta de Windows queda
fuera del alcance de esta versión. Cifrar indiscriminadamente SQLite no resolvería ese atacante y
añadiría riesgos de migración, indexado y recuperación tras corrupción. Si este objetivo cambia, se
diseñará antes una separación entre índices mínimos, payloads DPAPI, claves, historial reciente y
recuperación; no se hará como una migración criptográfica implícita.

La interfaz permite retirar un trabajo —incluidas sus revisiones y artefactos asociados— y usar
`Borrar actividad` para eliminar el historial reciente. Los checkpoints expiran según la retención
configurada. Los originales y resultados publicados se borran, si se desea, desde el sistema de
archivos porque pertenecen a la persona y no al estado recuperable de Parsezen.
