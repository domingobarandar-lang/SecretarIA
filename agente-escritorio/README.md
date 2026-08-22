# SecretarIA — agente de escritorio

Asistente ejecutivo personal que corre en una caja sobre el escritorio. Este
módulo es el cerebro **y la voz**: opera sobre el **calendario** y el
**correo** del ejecutivo, por terminal (`main.py`) o hablando (`main_voz.py`,
ver «El ciclo de voz» en [CLAUDE.md](CLAUDE.md)). Sin pantalla, sin botón
físico, sin servidor web.

Razona con **Gemini 3.1 Pro** y lee los datos por los **MCP oficiales de Google**.

Todo lo específico de un cliente vive en `device.config.json`. Cambiar de
ejecutivo es cambiar ese archivo — no se toca código.

## Instalación

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

copy device.config.example.json device.config.json   # y completarlo
copy .env.example .env                               # y poner la API key
```

En Linux / Raspberry Pi:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp device.config.example.json device.config.json
cp .env.example .env
```

Ni `.env` ni `device.config.json` se suben a git.

## Las dos palancas del `.env`

| Variable | Qué decide | Default |
|---|---|---|
| `LLM_PROVIDER` | Quién razona: `gemini` o `anthropic` | **ninguno — sin esto no arranca** |
| `FUENTE_DATOS` | De dónde salen agenda y correo: `mock` o `google` | `mock` |

Que una falle cerrada y la otra no es a propósito. Un proveedor equivocado
**factura en silencio** y el aviso llega con la cuenta. La fuente mock, en cambio,
se anuncia sola: sus eventos empiezan con `[MOCK]` y sus correos son de
`ejemplo.invalid`. Igual sale en el saludo del REPL, para no depender de que
alguien lo note.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

No gastan tokens, no salen a internet y no tocan ninguna cuenta: `config.py` lee
archivos y `memory.py` usa una SQLite en memoria.

## Instalar sin internet (pendrive)

Para un PC de prueba que no tiene internet confiable: `scripts/instalar_pendrive.ps1`
instala todo desde una copia local — wheels de Windows, la voz de Piper y el
modelo de Whisper — sin tocar la red. Se arma la copia (en otra máquina, con
internet) con:

```bash
pip download --dest wheels --platform win_amd64 --python-version 311 \
  --implementation cp --abi cp311 --only-binary=:all: -r requirements.txt
./scripts/descargar_voz.sh
./scripts/descargar_whisper.sh
```

Eso deja `wheels/` y `models/` listos para copiar al pendrive junto con el
resto del proyecto. En el PC de destino, con Python 3.11+ ya instalado:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar_pendrive.ps1
```

Crea `.venv`, instala todo desde `wheels/` (nunca desde PyPI), arma
`device.config.json`/`.env` desde las plantillas si no existen, y deja la voz
encendida. Detalle de por qué esto funciona sin red: `voz/stt.py` carga Whisper
desde `models/whisper-<tamaño>/` si esa carpeta existe, en vez de pedírselo a
HuggingFace.

## Configuración

| Campo | Obligatorio | Default |
|---|---|---|
| `device_id` | sí | — |
| `ejecutivo.nombre` / `.email` / `.zona_horaria` | sí | — |
| `ejecutivo.cargo` / `.empresa` | no | `""` |
| `agente.nombre` | sí | — |
| `agente.idioma` | no | `es-CL` |
| `agente.tratamiento` | no | `usted` (`usted` \| `tu`) |
| `agente.tono` | no | `formal, directo, sin rodeos` |
| `agente.largo_respuesta` | no | `breve` (`breve` \| `normal` \| `detallado`) |
| `tools_habilitadas` | sí | — (lista cerrada de cinco) |
| `reglas.confirmar_antes_de_acciones_irreversibles` | no | `true` — **solo acepta `true`** |
| `reglas.no_agendar_antes_de` / `_despues_de` | no | `09:00` / `19:00` (`null` = sin límite) |
| `reglas.dias_bloqueados` | no | `[]` (días de semana o fechas `AAAA-MM-DD`) |
| `reglas.nunca_revelar` | no | `[]` |
| `limites.max_iteraciones_loop` | no | `8` (1–50) |
| `limites.max_mensajes_historial` | no | `12` (2–200) |
| `limites.timeout_llamada_modelo_seg` | no | `20` (5–300) |

Si el archivo tiene un problema, el agente **no arranca** y muestra la lista
completa:

```
Configuración inválida en «device.config.json» (2 problemas):
  - falta el campo obligatorio «ejecutivo.email»
  - llave desconocida «limits»; ¿quisiste decir «limites»?; permitidas: ...
  Referencia: device.config.example.json
```

## Las cinco tools

`ver_agenda` · `crear_evento` · `buscar_correos` · `redactar_borrador` · `recordar`

`crear_evento` verifica conflictos con `ver_agenda` antes y **pide confirmación
explícita**; esa garantía vive en el código, no en el prompt. Enviar correo no es
una tool: solo se crean borradores.

## Google Workspace — el modelo no habla MCP

La agenda y el correo salen de los MCP oficiales de Google
(`calendarmcp.googleapis.com`, `gmailmcp.googleapis.com`), pero **el modelo nunca
los ve**. Son una fuente de datos detrás de `FuenteDatos`, llamada por nuestro
código; el modelo sigue viendo las cinco tools y nada más.

No es un detalle de implementación. La API de Gemini permite pasarle un servidor
MCP directo al modelo, y el MCP de Calendar publica `delete_event`. Ese atajo
pondría al agente en condiciones de borrar reuniones del ejecutivo por fuera de la
lista cerrada, de la confirmación humana y de la tabla `acciones` — las tres
garantías del producto salteadas de una vez, sin que ninguna prueba lo note.

De las diecisiete herramientas que publican los dos servidores, SecretarIA usa
**cuatro**: `list_events`, `create_event`, `search_threads`, `create_draft`. El
resto es inalcanzable por construcción (`_METODOS_PERMITIDOS`), no por olvido.

Drive no se conecta: ninguna de las cinco tools lo usa.

Dos cosas que dejaron de depender de nuestra disciplina:

- **El MCP de Gmail no publica ninguna herramienta de envío.** «Solo borradores»
  ahora también lo garantiza el transporte.
- **Crear eventos exige agregar `calendar.events` a la pantalla de consentimiento.**
  Los scopes que Google documenta por defecto son de solo lectura.

## Estado

El cerebro está completo y corre: configuración, memoria, prompt, las cinco tools,
el loop agéntico, el adapter de proveedor y el REPL. **246 tests en verde**, sin
gastar tokens y sin salir a internet.

`integrations/google_mcp.py` está escrito y probado contra dobles, pero **todavía
no se corrió contra los servidores de verdad**: los MCP de Workspace están en
Developer Preview y hacen falta credenciales OAuth del ejecutivo. Por eso el
cliente hace un `tools/list` al conectar y compara contra lo que espera: si Google
cambió un nombre o un parámetro, el agente **no arranca** y dice cuál. Un desajuste
que no se ve al arrancar se ve cuando alguien pregunta por su agenda.
