# SecretarIA — agente de escritorio (el cerebro)

Asistente ejecutivo personal que corre **dentro de una caja de ~10 cm** en el
escritorio del ejecutivo: Raspberry Pi 5 en producto, Orange Pi Zero 3 en demo.
La placa es el servidor — este repositorio vive descargado en ella y el agente
corre ahí. **Solo el razonamiento del modelo sale por API.** Las credenciales
OAuth del ejecutivo nunca salen del dispositivo.

Este módulo es el cerebro y nada más: entra texto por el terminal, sale texto.
Micrófono, parlante, pantalla y botón de mute son otro módulo, otra sesión.

## Dónde vive esto — se lee raro y es a propósito

La carpeta está **físicamente dentro** de `fabrica__agentes/`, pero **ese repo no la
versiona**: su `.gitignore` excluye `SecretarIA/` entera. Lo que versiona este
código es el **git propio** que vive en `SecretarIA/` (un nivel más arriba). Los
commits van ahí.

Es para poder sacarla después con un `mv` sin que el historial de la fábrica
registre jamás que estuvo. De ahí las dos cosas que **no** hay que hacer: no
commitear nada de acá al repo de la fábrica, y no quitar la línea `SecretarIA/`
de su `.gitignore` sin mover la carpeta primero.

Las guardas de `docs/DECISIONES.md` que apuntan a `SecretarIA/agente-escritorio/…`
se resuelven contra el **disco**, no contra git: hoy pasan, y el día de la mudanza
van a fallar solas pidiendo que se actualicen las rutas. Esa alarma es
intencional — no apagarla por adelantado.

## Stack — cerrado, no se discute

| Pieza | Decisión |
|---|---|
| Lenguaje | Python 3.11+ (la Pi 5 con Bookworm trae 3.11: no usar sintaxis 3.12+) |
| SDK | `anthropic` oficial |
| Modelo | `claude-sonnet-4-20250514` |
| Memoria | SQLite local. No Supabase, no Redis, no Postgres |
| Google | MCP externo `taylorwilsdon/google_workspace_mcp` |
| Config | `device.config.json` — define TODO el comportamiento por cliente |
| Framework | **ninguno**. Loop propio. Nada de LangChain, CrewAI, LlamaIndex |

## Prohibido

- Agregar tools fuera de las cinco de abajo.
- Enviar correo. Solo borradores — **a propósito**, no es un pendiente.
- Audio, wake word, TTS, STT, pantalla.
- Servidor web, FastAPI, endpoints. Es un REPL de terminal.
- Inventar firmas del SDK de `anthropic`. Si hay duda, se pregunta y se verifica.
- System prompts con el nombre de un cliente adentro: se construyen desde la config.

## Las cinco tools

| Tool | Qué hace | Riesgo |
|---|---|---|
| `ver_agenda(fecha_inicio, fecha_fin)` | Lee eventos del calendario | lectura |
| `crear_evento(titulo, inicio, fin, invitados, descripcion)` | Crea el evento | **irreversible** |
| `buscar_correos(query, max_resultados)` | Busca en Gmail: remitente, asunto, fecha, extracto | lectura |
| `redactar_borrador(destinatario, asunto, cuerpo)` | Crea un DRAFT. **Nunca envía** | escritura reversible |
| `recordar(hecho, categoria)` | Guarda una preferencia del ejecutivo | escritura local |

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
├── agent/
│   ├── config.py    # esquema + validación + objeto de solo lectura
│   ├── memory.py    # SQLite: conversaciones, hechos, acciones
│   ├── prompts.py   # system prompt construido desde la config
│   ├── tools.py     # definición y despacho de las cinco tools
│   └── core.py      # loop agéntico + confirmación humana
├── integrations/google_mcp.py  # cliente del MCP de Google Workspace
└── tests/
```

## Cómo se corre

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest      # tests
.\.venv\Scripts\python.exe main.py        # REPL
```

## Estado

- **Tarea 1 — lista**: estructura, `config.py` con validación, `memory.py` con las
  tres tablas, tests de ambos en verde.
- **Tarea 2 — pendiente**: `core.py`, `tools.py` (con datos mock), `prompts.py`, `main.py`.
- **Después**: conexión real al MCP de Google, con el loop ya probado.
