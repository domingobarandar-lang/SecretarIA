# SecretarIA — agente de escritorio

Asistente ejecutivo personal que corre en una caja sobre el escritorio. Este
módulo es el cerebro: opera sobre el **calendario** y el **correo** del ejecutivo
desde un REPL de terminal. Sin voz, sin pantalla, sin servidor web.

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

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

No gastan tokens, no salen a internet y no tocan ninguna cuenta: `config.py` lee
archivos y `memory.py` usa una SQLite en memoria.

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

## Estado

El cerebro está completo y corre: configuración, memoria, prompt, las cinco
tools, el loop agéntico y el REPL. **181 tests en verde**, sin gastar tokens.

Los datos de agenda y correo son **mock** (van marcados `[MOCK]` y usan el
dominio `ejemplo.invalid`). Lo único que falta es `integrations/google_mcp.py`,
que implementa la misma interfaz que el mock contra el MCP de Google Workspace —
cambiar de fuente no toca el loop, el prompt ni las tools.
