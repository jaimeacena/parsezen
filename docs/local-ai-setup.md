# IA local en Parsezen

Parsezen integra Ollama como único servidor de modelos conversacionales. No permite proveedores ni
direcciones configurables: la API está fijada a `127.0.0.1:11434`.

Ollama solo es necesario para traducción con IA, corrección o la pre-organización automática de
capítulos. Conversión, EPUB, OCR, personalización manual y traducción con Argos pueden usarse sin él.

La cabecera comunica su estado y abre el gestor global. Allí `Instalados` permite elegir el modelo
predeterminado y `Añadir modelo` reúne recomendaciones y descarga. Los documentos que necesitan IA
usan una instantánea de ese modelo y contexto generales; no existen excepciones por documento. Los
cambios globales solo alcanzan trabajos pendientes todavía editables.

## Recorrido guiado

La interfaz muestra una única acción pertinente:

1. **Instalar Ollama**: usa WinGet o el instalador oficial verificado.
2. **Iniciar**: abre el proceso local y espera a que responda.
3. **Proteger y reiniciar**: activa `disable_ollama_cloud` y reinicia el servidor.
4. **Elegir modelo**: abre el gestor integrado.

El usuario no necesita abrir una consola, una aplicación de chat ni un navegador.

## Modelos

El gestor obtiene los modelos instalados desde `GET /api/tags`. Para recomendar:

- ejecuta localmente `llmfit`;
- detecta RAM, GPU y VRAM;
- solicita candidatos de chat;
- descarta embeddings, modelos cloud y modelos que no caben;
- verifica en el registro de Ollama los identificadores inferidos;
- presenta un modelo equilibrado, uno más rápido y otro de mayor capacidad.

La recomendación se almacena temporalmente para funcionar sin conexión. Si no está disponible, la
instalación manual por nombre sigue activa.

Parsezen usa identificadores canónicos de Ollama:

```text
modelo
modelo:tag
autor/modelo:tag
```

Si falta el tag, Ollama interpreta `latest`. Parsezen no crea alias, perfiles ni nombres de modelo
propios. Un modelo no queda seleccionado hasta que aparece realmente en `GET /api/tags`.

Las transformaciones documentales requieren un modelo capaz de devolver directamente el documento.
Parsezen no permite seleccionar ni restaurar variantes de razonamiento conocidas —como
`qwen3:4b`, DeepSeek R1 o QwQ— porque pueden dedicar la respuesta al razonamiento interno y no
devolver el texto transformado. La familia general Qwen 3.5 no se confunde con esos tags antiguos y
puede validarse normalmente. Los modelos excluidos siguen visibles en el gestor para poder
eliminarlos. La selección automática continúa limitada por la memoria real del equipo.

El catálogo oficial puede abrirse desde el gestor:

- [Catálogo de Ollama](https://ollama.com/library)
- [API de modelos instalados](https://docs.ollama.com/api/tags)
- [API de descarga](https://docs.ollama.com/api/pull)
- [API de chat](https://docs.ollama.com/api/chat)

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

El valor automático usa:

- límite anunciado por el modelo;
- tamaño de los pesos;
- VRAM y RAM disponibles;
- margen conservador para el sistema.

Normalmente elige 4.096, 8.192 o 16.384 tokens. Existe un valor personalizado entre 512 y 262.144,
limitado por lo que el modelo admite. Parsezen fragmenta documentos largos, por lo que elegir el
máximo rara vez mejora el resultado y sí aumenta memoria y latencia.

La ventana se envía como `options.num_ctx` en cada petición.

## Descarga y actualización de llmfit

`llmfit` es una herramienta MIT administrada, no una dependencia importada. Parsezen:

- consulta releases del repositorio oficial;
- acepta solo el artefacto de Windows y arquitectura esperados;
- exige HTTPS, límites de tamaño y un SHA-256 fijado en Parsezen para cada arquitectura;
- extrae únicamente el ejecutable y su licencia;
- verifica `--version` antes de activarlo;
- conserva la versión anterior si una actualización falla.

Una release nueva no se ejecuta hasta que su hash se incorpora y revisa en Parsezen. El subproceso
recibe una lista mínima de variables del sistema, sin claves de API, credenciales ni proxies. Se
guarda en `%LOCALAPPDATA%\Parsezen\llmfit`. No recibe documentos, rutas, prompts ni texto. La consulta
opcional al registro de Ollama solo contiene identificadores públicos de modelos.

Repositorio: [AlexsJones/llmfit](https://github.com/AlexsJones/llmfit).

## Privacidad de las peticiones

- `POST /api/chat` se dirige únicamente a loopback.
- Las respuestas llegan en streaming para poder cancelar entre fragmentos.
- No se registra el prompt ni la respuesta.
- El glosario se protege durante la petición y se cifra mientras una revisión sea recuperable.
- Los checkpoints de fragmentos validados se cifran para la cuenta de Windows.

## Preparación manual opcional

El recorrido normal no requiere estos comandos. Para diagnóstico:

```powershell
winget install --id Ollama.Ollama --exact
ollama pull qwen3:4b-instruct
Invoke-RestMethod http://127.0.0.1:11434/api/version
ollama list
ollama ps
```

Después vuelve a Parsezen y pulsa refrescar. Usa un tag concreto antes de publicar una versión si
quieres resultados reproducibles.

## Validación real

`Validar con IA real.cmd` recorre una muestra sintética de 20 páginas mediante el modelo elegido. El informe local
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
Validar con IA real.cmd --model qwen3:4b-instruct-2507-q8_0 --translation-engine local_ai --profile translation --pages 1 4
```

`--profile critical` ejecuta traducción, corrección y estructura; `--profile translation` aísla la
traducción para comparar modelos especializados. `--profile review` compara conversión directa y
revisión semántica sin traducir; `--profile translation-review` compara traducción directa y
revisada con el mismo motor. Los informes conservan únicamente recuentos, fases y tiempos, nunca
texto documental. En la estación objetivo de 8 GB de VRAM, la muestra
sintética de cuatro páginas del 12 de agosto de 2026 dio estos resultados, todos con control de
calidad superado:

| Modelo | Perfil completo tras carga | Solo traducción en caliente |
|---|---:|---:|
| Qwen 3 4B Instruct Q8 | 20,2 s | 12,5 s |
| Qwen 3.5 9B | 42,3 s | 24,3 s |
| TranslateGemma 4B Q8 | no aplicable | 15,1 s en caliente; 30,1 s con carga |

La conclusión de producto es deliberadamente conservadora: Qwen 3 4B sigue siendo la referencia
equilibrada en ese equipo; Qwen 3.5 queda permitido como opción de mayor capacidad y TranslateGemma
como candidato especializado, pero Parsezen no cambia el modelo predeterminado ni añade un segundo
selector basándose solo en esta muestra. Las recomendaciones generales siguen calculándose con
`llmfit` y un modelo solo queda seleccionado después de aparecer en `/api/tags`.

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
