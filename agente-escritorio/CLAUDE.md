# SecretarIA — agente de escritorio (el cerebro)

Asistente ejecutivo personal que corre **dentro de una caja de ~10 cm** en el
escritorio del ejecutivo: Raspberry Pi 5 en producto, Orange Pi Zero 3 en demo.
La placa es el servidor — este repositorio vive descargado en ella y el agente
corre ahí. **Solo el razonamiento del modelo sale por API.** Las credenciales
OAuth del ejecutivo nunca salen del dispositivo.

Este módulo es el cerebro **y la voz**: entra audio o texto, sale audio o texto.
Pantalla y botón de mute siguen siendo otro módulo, otra sesión.

**El 2026-08-16 la voz entró acá** (decisión de José). Antes estaba prohibida y
vivía en «otro módulo, otra sesión»; se movió adentro porque el ciclo de voz
necesita el mismo `Agente.responder()` y separarlos obligaba a inventar un
protocolo entre dos procesos que corren en la misma placa. Lo que **no** cambió:
el cerebro no depende de la voz. `main.py` sigue siendo un REPL de texto puro y
la suite corre entera sin micrófono, sin parlante y sin los 63 MB del modelo.

## Dónde vive esto

Proyecto independiente en `C:\Users\user\Proyectos_chaski\SecretarIA`, hermano de
`Fabrica__Agentes/`. Lo único que se lee raro: **el git está un nivel más arriba
de este archivo** — en `SecretarIA/`, no en `agente-escritorio/`. Los commits van
ahí.


## Stack — cerrado, no se discute

| Pieza | Decisión |
|---|---|
| Lenguaje | Python 3.11+ (la Pi 5 con Bookworm trae 3.11: no usar sintaxis 3.12+) |
| Modelo | `gemini-3.1-pro-preview` vía `google-genai` (Interactions API) |
| Proveedor | `LLM_PROVIDER` = `gemini` \| `anthropic`. **Sin default: no arranca sin la variable** |
| Memoria | SQLite local. No Supabase, no Redis, no Postgres |
| Google | MCP **oficiales** de Google: `calendarmcp` y `gmailmcp`. Drive no |
| Config | `device.config.json` — define TODO el comportamiento por cliente |
| Framework | **ninguno**. Loop propio. Nada de LangChain, CrewAI, LlamaIndex |

**El proveedor se cambió el 2026-08-14** (decisión de José). Anthropic quedó como
segunda opción viva, no como resto arqueológico: `agent/llm.py` traduce y los dos
caminos están probados. Dos cosas que hay que saber de Gemini 3.1 Pro:

- **No tiene free tier.** US$ 2 por millón de tokens de entrada, US$ 12 de salida.
  Las keys gratis del pool de la fábrica **no sirven** para este modelo.
- **Firma su razonamiento.** Los pasos `thought` tienen que volver junto al
  `function_call` que justifican. Si se pierden, el agente no falla: repite
  llamadas y gasta el doble, sin error y sin aviso. El round-trip lo hace
  `agent/llm.py` y hay un test que lo vigila.

El formato interno del core **sigue siendo el de bloques** (`tool_use` /
`tool_result`), que es el de Anthropic. No se cambió al pasar a Gemini y eso fue
deliberado: es el contrato contra el que están escritas las garantías y sus
pruebas. Traducir en el borde cuesta un archivo; cambiar el idioma interno costaba
el core entero.

## Prohibido

- Agregar tools fuera de las cinco de abajo.
- Enviar correo. Solo borradores — **a propósito**, no es un pendiente.
- Pantalla y botón físico de mute. La voz sí — ver «El ciclo de voz» abajo.
- Que el core importe algo de `voz/`. La dependencia va en un solo sentido:
  la voz usa al agente, el agente no sabe que la voz existe.
- Mandar audio del ejecutivo a una API. El wake word y el STT corren **locales**.
  Lo único que sale por red es el razonamiento del modelo, igual que siempre.
- Servidor web, FastAPI, endpoints. Se opera por terminal o por voz, no por HTTP.
- Inventar firmas de un SDK. Si hay duda, se lee el SDK instalado y se verifica.
- System prompts con el nombre de un cliente adentro: se construyen desde la config.
- **Usar el tool `mcp_server` de la API de Gemini.** Ver abajo: es el atajo que
  parece obvio y abre el único agujero que este producto no puede tener.

## Las cinco tools

| Tool | Qué hace | Riesgo |
|---|---|---|
| `ver_agenda(fecha_inicio, fecha_fin)` | Lee eventos del calendario | lectura |
| `crear_evento(titulo, inicio, fin, invitados, descripcion)` | Crea el evento | **irreversible** |
| `buscar_correos(query, max_resultados)` | Busca en Gmail: remitente, asunto, fecha, extracto | lectura |
| `redactar_borrador(destinatario, asunto, cuerpo)` | Crea un DRAFT. **Nunca envía** | escritura reversible |
| `recordar(hecho, categoria)` | Guarda una preferencia del ejecutivo | escritura local |

### El modelo no habla MCP, y esa es la línea que no se cruza

La API de Gemini trae un tipo de tool `mcp_server`: se le pasa la URL y el modelo
llama al servidor solo. Es cómodo, es oficial, y acá está **prohibido**.

El MCP de Calendar publica ocho herramientas, entre ellas `delete_event`. Dárselo
al modelo significaría que el agente puede borrar reuniones del ejecutivo: por
fuera de las cinco tools, por fuera de la confirmación humana y por fuera de la
tabla `acciones`. Las tres garantías salteadas de una vez, y ninguna prueba
funcional lo notaría — el agente respondería perfecto hasta el día que borre algo.

El MCP vive detrás de `FuenteDatos`, llamado por nuestro código. De las diecisiete
herramientas que publican los dos servidores se usan **cuatro**: `list_events`,
`create_event`, `search_threads`, `create_draft`. La lista `_METODOS_PERMITIDOS`
de `integrations/google_mcp.py` lo hace estructural: pedir cualquier otra cosa es
un `AssertionError` antes de tocar la red, no una llamada que sale.

Dos regalos del cambio, que ya no dependen de nuestra disciplina:

- **El MCP de Gmail no publica ninguna herramienta de envío.** «Solo borradores»
  lo garantiza ahora también el transporte.
- **Crear eventos exige agregar `calendar.events` a la pantalla de consentimiento**
  a mano: los scopes documentados por defecto son de solo lectura. La frontera
  leer/escribir quedó también en el OAuth.

`crear_evento` tiene dos obligaciones que no dependen del prompt:
1. **Verifica conflictos llamando a `ver_agenda` antes.** Por eso el validador
   rechaza una config que habilite `crear_evento` sin `ver_agenda`.
2. **No se ejecuta sin un sí explícito del usuario.** El agente describe exactamente
   qué va a crear y espera confirmación.

**Esa lógica vive en el core, no en el system prompt.** El prompt se puede olvidar;
el código no. Por lo mismo `reglas.confirmar_antes_de_acciones_irreversibles`
solo acepta `true`: si se pudiera apagar por configuración, no sería una garantía.

## device.config.json — la pieza central

Cambiar de cliente = cambiar ese archivo, **cero código**. El esquema vive una sola
vez, en [agent/config.py](agent/config.py), y ahí se agregan campos nuevos.

- Se valida al arrancar. Falta un campo obligatorio → **no arranca**, con la lista
  completa de problemas, no el primero.
- Una llave desconocida (`limits` por `limites`) es **error, no aviso**, y sugiere
  la correcta. Un typo silencioso es un límite que nadie aplica.
- El archivo real está en `.gitignore`. Se versiona solo `device.config.example.json`.
- Ruta: argumento > variable `DEVICE_CONFIG` > `device.config.json` en la raíz.

Obligatorios: `device_id`, `ejecutivo.nombre`, `ejecutivo.email`,
`ejecutivo.zona_horaria`, `agente.nombre`, `tools_habilitadas`.
El resto tiene default (ver `ESQUEMA`); `reglas` y `limites` completos son opcionales.

## Memoria — tres tablas, tres propósitos

| Tabla | Para qué | Alcance |
|---|---|---|
| `conversaciones` | El hilo. De acá sale la ventana deslizante | por sesión |
| `hechos` | Preferencias del ejecutivo (tool `recordar`) | permanente |
| `acciones` | Auditoría: tool, input, resultado, si hubo confirmación | permanente |

`acciones` es **requisito de cumplimiento, no un extra**: la Ley 21.719 fiscaliza
evidencia operativa, no políticas. Se escribe siempre, incluso cuando la tool
falla — una auditoría con solo los éxitos miente. Lleva `session_id` para poder
reconstruir en qué conversación pasó cada cosa.

Un hecho repetido refresca su fecha, no se duplica (índice único
`categoria + contenido`). Requiere SQLite ≥ 3.35 (por `RETURNING`).

## Robustez — lo que no se negocia en el core

- Límite duro de iteraciones del loop, leído de `limites.max_iteraciones_loop`.
- Timeout en toda llamada al modelo y a cualquier API externa.
- `try/except` en **cada** tool: un error devuelve un `tool_result` con el error
  al modelo; nunca revienta el proceso.
- Ventana deslizante según `limites.max_mensajes_historial`. **Cuidado**: cortar
  entre un `tool_use` y su `tool_result` deja al modelo con una llamada sin
  respuesta y la API lo rechaza. El corte lo decide el core, no `memory.historial()`.
- Log estructurado de cada mensaje, tool call, resultado y latencia. `session_id`
  único por conversación. Contador de tokens por sesión.

## Estructura

```
agente-escritorio/
├── device.config.json          # el cliente (gitignored)
├── device.config.example.json  # la plantilla (versionada)
├── main.py                     # REPL de texto
├── main_voz.py                 # REPL de voz. Mismo agente, otra puerta
├── agent/
│   ├── config.py    # esquema + validación + objeto de solo lectura
│   ├── memory.py    # SQLite: conversaciones, hechos, acciones
│   ├── prompts.py   # system prompt construido desde la config
│   ├── tools.py     # definición y despacho de las cinco tools
│   ├── llm.py       # adapter Gemini/Anthropic. El core habla un solo idioma
│   └── core.py      # loop agéntico + confirmación humana
├── voz/
│   ├── audio.py     # PyAudio. La única capa que sabe qué es un frames_per_buffer
│   ├── escucha.py   # webrtcvad: cuándo empezó y cuándo terminó de hablar alguien
│   ├── stt.py       # faster-whisper local
│   ├── tts.py       # Piper local
│   └── despertar.py # ¿le hablaron al agente? Busca su nombre en lo transcrito
├── models/                     # las voces .onnx (gitignored, ~63 MB c/u)
├── integrations/google_mcp.py  # los MCP de Google detrás de FuenteDatos
└── tests/
```

## Cómo se corre

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest      # tests
.\.venv\Scripts\python.exe main.py        # REPL de texto

# Voz: una vez por caja nueva
./scripts/descargar_voz.sh                              # baja el modelo a models/
.\.venv\Scripts\python.exe scripts\verificar_audio.py   # ¿capta el micrófono?
.\.venv\Scripts\python.exe main_voz.py --probar-voz     # ¿suena el parlante?
.\.venv\Scripts\python.exe main_voz.py                  # el ciclo completo
```

## El ciclo de voz

Cuatro piezas, todas locales: **webrtcvad** decide cuándo alguien habló,
**Whisper** lo transcribe, el agente de siempre responde y **Piper** lo dice.
Nada de eso sale por red — lo único que sale sigue siendo el razonamiento del
modelo. No es una postura: el micrófono está siempre encendido, así que lo que
saldría no serían las órdenes, sería la oficina entera.

**El wake word no es un modelo, es el nombre de la config.** Lo estándar sería
openWakeWord o Porcupine, que gastan muchísima menos CPU. Pero esos modelos se
entrenan por palabra: existe uno para «alexa», no para «Vera». Y el nombre sale
de `device.config.json`, la pieza que promete que cambiar de cliente no cuesta
código — un modelo por nombre sería entrenar uno antes de vender cada caja. Así
que Whisper transcribe todo y `voz/despertar.py` busca el nombre, con
comparación difusa porque Whisper escribe «Bera» y «Vela» la mitad de las veces.
Se paga en CPU. Si la placa no da abasto, **ahí** es donde se cambia la
estrategia: el resto del ciclo no sabe cómo se tomó esa decisión.

La confirmación de `crear_evento` por voz sigue la misma regla que por terminal,
y acá pesa más: **lo que no es un sí claro es un no.** Silencio es no,
transcripción vacía es no. Whisper se equivoca, y el error no es simétrico — no
crear un evento se arregla repitiendo la frase; crear uno que nadie pidió ya le
salió por correo a otras personas.

## Estado

- **Tarea 1 — lista**: estructura, `config.py` con validación, `memory.py` con las
  tres tablas.
- **Tarea 2 — lista**: `prompts.py`, `tools.py` (contrato + fuente mock), `core.py`
  (loop, ventana, confirmación humana, auditoría) y `main.py` (REPL).
- **Tarea 3 — escrita, sin estrenar**: `agent/llm.py` (adapter Gemini/Anthropic) e
  `integrations/google_mcp.py` (`FuenteDatos` contra los MCP oficiales).
- **Voz — escrita, probada a medias (2026-08-16)**: `voz/` completo, `main_voz.py`
  y el bloque `voz` del esquema. **295 tests en verde**, sin micrófono y sin
  cargar un modelo. Piper sí está estrenado de verdad: sintetiza a 0.62× tiempo
  real en el PC de desarrollo. Lo que **falta probar con una persona** es el
  ciclo entero — despertar por el nombre, transcribir bien un acento chileno y
  confirmar un `crear_evento` hablando. Eso no lo prueba pytest.
- **Pendiente y bloqueado en 🔴**: correr el MCP contra los servidores de verdad.
  Falta (a) inscribirse en el Developer Preview Program de Google Workspace,
  (b) un proyecto de Google Cloud con las APIs y los MCP habilitados, (c) un
  cliente OAuth y el refresh token del ejecutivo, y (d) una `GEMINI_API_KEY` de
  tramo pagado. Todo eso es «crear cuentas / gastar dinero»: lo pide José.

El código del MCP está probado contra dobles, **no contra Google**. Por eso
`conectar()` hace un `tools/list` y lo cruza con lo que este proyecto espera: si
un nombre o un parámetro cambió, el agente no arranca y dice cuál. Es la
diferencia entre enterarse al encender y enterarse cuando alguien pregunta por su
agenda.

### La otra mitad de los tests

`tests/conftest.py` borra del entorno, antes de cada test, todo lo que podría
convertir un descuido en una llamada facturada o en un efecto sobre una cuenta
real: `LLM_PROVIDER`, las API keys, `FUENTE_DATOS` y las credenciales de Google.
Antes era una convención («ningún test construye un cliente real»); desde que el
proveedor y la fuente se deciden por entorno, tenía que ser estructural.

El `.venv` de desarrollo corre Python 3.12 y la placa trae 3.11. Un test
(`test_todo_el_codigo_parsea_como_python_311`) parsea todo el proyecto con
`ast.parse(feature_version=(3, 11))` para que la sintaxis nueva no se cuele
hasta la placa.
