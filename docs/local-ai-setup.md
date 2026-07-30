# IA local en Parsezen

Parsezen integra Ollama como único servidor de modelos conversacionales. No permite proveedores ni
direcciones configurables: la API está fijada a `127.0.0.1:11434`.

Ollama solo es necesario para traducción con IA, corrección o la pre-organización automática de
capítulos. Conversión, EPUB, OCR, personalización manual y traducción con Argos pueden usarse sin él.

La cabecera comunica su estado y abre el gestor global. Allí `Instalados` permite elegir el modelo
predeterminado y `Añadir modelo` reúne recomendaciones y descarga. Cada documento que necesita IA
muestra además una sección contextual: hereda ese predeterminado o puede guardar una excepción
propia. Los cambios globales solo alcanzan trabajos pendientes heredados.

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

Las transformaciones documentales requieren un tag orientado exclusivamente a instrucciones, por
ejemplo `qwen3:4b-instruct`. Parsezen no permite seleccionar ni restaurar variantes de razonamiento
conocidas —como `qwen3:4b`, DeepSeek R1 o QwQ— porque pueden dedicar la respuesta al razonamiento
interno y no devolver el documento transformado. Esos modelos siguen visibles en el gestor para
poder eliminarlos. La selección automática continúa limitada por la memoria real del equipo; el
ejemplo no sustituye esa comprobación.

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
archivo de forma atómica. También acepta `OLLAMA_NO_CLOUD=1` en el proceso servidor.

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
- exige HTTPS, límites de tamaño y SHA-256 publicado;
- extrae únicamente el ejecutable y su licencia;
- verifica `--version` antes de activarlo;
- conserva la versión anterior si una actualización falla.

Se guarda en `%LOCALAPPDATA%\Parsezen\llmfit`. No recibe documentos, rutas, prompts ni texto. La
consulta opcional al registro de Ollama solo contiene identificadores públicos de modelos.

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
informe no se incorpora al repositorio ni contiene texto documental. `OK` exige además que no queden
avisos PDF, incidencias de idioma, fragmentos de traducción conservados ni encabezados EPUB con
dimensiones propias de un párrafo; una ejecución que termina y genera un archivo válido pero no
supera ese control se presenta como `REVISAR`. Para un corpus especializado puedes repetir
`--glossary "origen=destino"`; esos términos se usan en la transformación y se omiten del informe.

Una prueba representativa no garantiza una traducción perfecta. Mantén un corpus local privado de
documentos y revisa cualquier actualización de Ollama o de modelo antes de usarla en trabajos
importantes.
