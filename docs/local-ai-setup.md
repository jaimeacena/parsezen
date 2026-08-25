# IA local en Parsezen

Parsezen integra Ollama como único servidor de modelos conversacionales. No permite proveedores ni
direcciones configurables: la API está fijada a `127.0.0.1:11434`.

Ollama solo es necesario para traducción con IA, corrección o la pre-organización automática de
capítulos. Conversión, EPUB, OCR, personalización manual y traducción con Argos pueden usarse sin él.
La traducción nueva usa IA local por defecto; Argos permanece como elección manual y nunca actúa como
alternativa silenciosa. Parsezen fija Hy-MT2 Q4_K_M para traducir y LFM Q6_K para revisar; no pide a
la persona elegir un modelo conversacional.

La cabecera comunica su estado y abre `Componentes de IA local`. Esa vista fija muestra exactamente
las tarjetas `Traducción IA` y `Revisión IA`, con los estados `Preparado`, `Descargable` o `Equipo
insuficiente`. No ofrece un selector de tags, endpoints, búsqueda, recomendaciones automáticas ni
borrado arbitrario. Los documentos conservan una instantánea del perfil efectivo y los cambios solo
alcanzan trabajos pendientes todavía editables.

## Recorrido guiado

La interfaz muestra una única acción pertinente:

1. **Instalar Ollama**: usa WinGet o el instalador oficial verificado.
2. **Iniciar**: abre el proceso local y espera a que responda.
3. **Proteger y reiniciar**: activa `disable_ollama_cloud` y reinicia el servidor.
4. **Componentes de IA local**: abre las dos tarjetas de capacidades aprobadas.

El usuario no necesita abrir una consola, una aplicación de chat ni un navegador.

## Componentes aprobados

Cada tarjeta representa una capacidad concreta, no un modelo conversacional intercambiable. La
preparación se decide con el catálogo versionado de Parsezen y comprobaciones locales de hardware,
configuración solo-local y metadatos de Ollama. La verificación solo consulta `GET /api/version`,
`GET /api/tags` y `POST /api/show`; no envía documentos, prompts ni respuestas.

Un componente solo aparece como `Preparado` cuando su manifest, digest, formato, familia,
cuantización, contexto, capacidades y versión de Ollama coinciden. Si el modelo fijado no está en
`/api/tags`, la tarjeta puede mostrar `Descargable` cuando los demás requisitos se cumplen; la vista
emite únicamente la capacidad (`translation` o `review`) para que una capa posterior autorizada
gestione la preparación. Nunca acepta un nombre de modelo, URL o endpoint introducido por el usuario.

Los tags `:cloud` y `-cloud`, las variantes no fijadas y los modelos que no cumplen los requisitos
quedan fuera. La traducción offline con Argos permanece disponible como elección explícita y no es un
reemplazo silencioso de un componente de IA.

## Solo local

Los modelos con `:cloud` o `-cloud` se excluyen. Parsezen requiere además que las funciones cloud de
Ollama estén desactivadas.

Configuración de Windows:

```json
{
  "disable_ollama_cloud": true
}
```

Se guarda en `%USERPROFILE%\.ollama\server.json`. Parsezen conserva otras claves y reemplaza el
archivo de forma atómica. La variable `OLLAMA_NO_CLOUD=1` se aplica además cuando Parsezen inicia
Ollama, pero no se acepta el entorno del cliente como prueba del estado de un servidor que ya estaba
activo. Si falta la configuración persistente, Parsezen exige proteger y reiniciar Ollama.

## Ventana de contexto

Cada manifest fija 8.192 tokens para su fase, dentro del máximo anunciado por el artefacto. Parsezen
fragmenta los documentos largos y envía la ventana como `options.num_ctx`; la interfaz no ofrece un
control para elevarla ni permite que una preferencia antigua sustituya el contrato especializado.

## Preparación de componentes

La interfaz no instala ni selecciona modelos arbitrarios. El instalador recibe únicamente la
capacidad del catálogo, descarga su fuente fija mediante Ollama y vuelve a comprobar el alias final.
Una descarga solo termina correctamente si digest, formato, familia, cuantización, contexto,
plantilla, parámetros, licencia y capacidades coinciden con el manifest. LFM muestra sus condiciones
de licencia y exige confirmación explícita antes de descargar.

## Privacidad de las peticiones

- `POST /api/generate` y `POST /api/chat` se dirigen únicamente a loopback.
- Las respuestas llegan en streaming para poder cancelar entre fragmentos.
- No se registra el prompt ni la respuesta.
- El glosario se protege durante la petición y se cifra mientras una revisión sea recuperable.
- Los checkpoints de fragmentos validados se cifran para la cuenta de Windows.

## Diagnóstico manual opcional

El recorrido normal no requiere estos comandos. Para diagnóstico:

```powershell
winget install --id Ollama.Ollama --exact
Invoke-RestMethod http://127.0.0.1:11434/api/version
ollama list
ollama ps
```

Después vuelve a Parsezen y pulsa `Actualizar estados`. La pantalla vuelve a evaluar las dos tarjetas
sin conservar nombres de tags ni rutas del documento.

## Validación real

La política de modelos de IA local fija los manifests, adaptadores y gates de los componentes
aprobados. La pantalla no muestra candidatos ni permite cambiar tags fuera de esa política.

`Validar con IA real.cmd` recorre una muestra sintética de 20 páginas mediante los componentes
instalados. Los
casos que traducen usan IA local por defecto; Argos solo se prueba al añadir explícitamente
`--translation-engine argos` y nunca se usa para recuperarse de un fallo de Ollama. El informe local
solo contiene fases, tiempos, tamaños, contadores de calidad y revisión, y tipos de error. La
aprobación automática aplica únicamente los cambios que la app clasifica como conservadores. El
informe no se incorpora al repositorio ni contiene texto documental. `OK` exige que no queden
incidencias PDF bloqueantes, fragmentos de traducción conservados ni regresiones estructurales EPUB.
Los avisos PDF y de traducción son señales no bloqueantes por diseño: permanecen cuantificados para
la revisión, pero no se convierten artificialmente en rechazos. En EPUB de entrada, la estructura se
compara con la salida para distinguir defectos heredados de degradaciones nuevas. Una ejecución que
termina y genera un archivo válido pero no supera ese control se presenta como `REVISAR`. Para un
corpus especializado puedes repetir
`--glossary "origen=destino"`; esos términos se usan en la transformación y se omiten del informe.

La misma herramienta permite comparaciones reproducibles sin documentos privados:

```powershell
Validar con IA real.cmd --model parsezen/hymt-translation:Q4_K_M --translation-engine local_ai --profile translation --pages 1 4
```

`--profile critical` ejecuta traducción, corrección y estructura; `--profile translation` aísla la
traducción para comparar modelos especializados. `--profile review` compara conversión directa y
revisión semántica sin traducir; `--profile translation-review` compara traducción directa y
revisada con el mismo motor. Los informes conservan únicamente recuentos, fases y tiempos, nunca
texto documental. Como referencia histórica —no como opciones actuales—, en la estación objetivo de
8 GB de VRAM la muestra sintética de cuatro páginas del 12 de agosto de 2026 dio estos resultados:

| Modelo | Perfil completo tras carga | Solo traducción en caliente |
|---|---:|---:|
| Qwen 3 4B Instruct Q8 | 20,2 s | 12,5 s |
| Qwen 3.5 9B | 42,3 s | 24,3 s |
| TranslateGemma 4B Q8 | no aplicable | 15,1 s en caliente; 30,1 s con carga |

La conclusión de producto es deliberadamente conservadora: las comparaciones sirven para revisar
manifests y adaptadores locales, no para añadir recomendaciones generales ni otro selector. Un
componente solo queda preparado después de aparecer y verificarse en `/api/tags`.

Una prueba representativa no garantiza una traducción perfecta. Mantén un corpus local privado de
documentos y revisa cualquier actualización de Ollama o de modelo antes de usarla en trabajos
importantes.

### Evaluación de la revisión semántica

El 12 de agosto de 2026 se hicieron cinco comparaciones de recorridos directos y revisados sobre
tres muestras privadas de diez páginas, elegidas en documentos largos por diversidad de texto,
imágenes y tablas. Se
usó el mismo Qwen 3 4B Instruct local y una ejecución por caso. Los informes no conservaron títulos,
rutas ni fragmentos:

| Recorrido | Directo | Revisado | Incidencias de traducción | Propuestas seguras / rechazadas |
|---|---:|---:|---:|---:|
| Conversión, muestra A | 21,9 s | 180,0 s | no aplicable | 2 / 2 |
| Conversión, muestra B | 2,7 s | 138,2 s | no aplicable | 9 / 1 |
| Argos, muestra A | 34,0 s | 171,8 s | 1 → 0 | 10 / 1 |
| Argos, muestra B | 29,6 s | 152,7 s | 2 → 2 | 13 / 0 |
| Traducción con IA, muestra A | 110,9 s | 122,3 s | 0 → 0 | 10 / 0 |

La revisión encontró propuestas conservadoras, pero no demostró una mejora semántica universal. En
conversión multiplicó mucho el tiempo; tras Argos costó alrededor de cinco veces y solo una de las
dos muestras redujo el residuo medible. Con traducción por IA, la corrección se fusionó con la
traducción y el incremento observado fue menor, pero una sola muestra no permite generalizar ni
separar la calidad debida a cada operación. El recuento de propuestas tampoco equivale a calidad.

Por ello, Procesamiento directo sigue siendo el valor recomendado. La revisión semántica permanece
disponible como capa explícita para documentos valiosos, conversiones difíciles o salidas de Argos
que justifiquen el coste. Los informes nuevos separan además propuestas de contenido y de estructura
para que futuras comparaciones no mezclen ambos efectos.

El flujo normal aplica ahora esa conclusión de forma progresiva: las comprobaciones deterministas
pueden recomendar una revisión posterior limitada a los bloques con señales, pero no inician Ollama.
La persona decide si ejecutarla y confirma cualquier cambio. La revisión completa sigue disponible
desde la configuración cuando el valor o la dificultad del documento justifican revisar todo el texto.
