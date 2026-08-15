"""Adapter LLM multi-proveedor. El core habla un solo idioma; acá se traduce.

`core.py` habla el formato de bloques: `system`, `messages` con `role`/`content`,
bloques `tool_use` / `tool_result`, y una respuesta con `stop_reason` y `content`.
Da la casualidad de que ese es el formato de Anthropic, porque de ahí venía el
core. **No se cambió al pasar a Gemini y eso fue deliberado**: el formato interno
no es una preferencia de proveedor, es el contrato contra el que están escritas
las garantías del core y sus pruebas. Traducir en el borde cuesta este archivo;
cambiar el idioma interno costaba el core entero y su suite.

Cambiar de proveedor = cambiar variables de entorno. Cero cambios de código.

    LLM_PROVIDER      gemini | anthropic   (obligatorio, sin default)
    GEMINI_API_KEY    key de Google AI Studio          (proveedor gemini)
    ANTHROPIC_API_KEY key de Anthropic                 (proveedor anthropic)
    MODELO            tapa el modelo por defecto del proveedor
    GEMINI_THINKING   minimal | low | medium | high    (default: low)

**NO HAY PROVEEDOR POR DEFECTO Y ESO ES LA DECISIÓN.** Es la misma regla dura que
la línea conversacional aprendió a los golpes: adivinar acá cuesta plata real y
la equivocación no se nota hasta que llega la cuenta. Con `gemini-3.1-pro-preview`
—que **no tiene free tier**— el descuido factura desde el primer turno.

Lo que este adapter deliberadamente **no** hace: usar el tipo de tool `mcp_server`
de la API de Gemini. Ver `integrations/google_mcp.py` para el motivo — en una
frase, ese atajo le entregaría al modelo el catálogo entero del MCP de Google
(`delete_event` incluido) por fuera de las cinco tools y de la compuerta humana.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Modelo por defecto de cada proveedor. `MODELO` en el entorno lo tapa.
MODELOS_POR_DEFECTO = {
    "gemini": "gemini-3.1-pro-preview",
    "anthropic": "claude-sonnet-4-20250514",
}

PROVEEDORES = tuple(MODELOS_POR_DEFECTO)

#: Nivel de razonamiento de Gemini 3. Los tokens de «thinking» se facturan a
#: precio de salida (US$ 12 por millón), y los turnos de SecretarIA son cortos y
#: con forma de herramienta —«¿qué tengo el jueves?»—, no de investigación.
#: `GEMINI_THINKING` lo sube el día que haga falta.
THINKING_POR_DEFECTO = "low"
THINKING_VALIDOS = ("minimal", "low", "medium", "high")


class ErrorDeLLM(Exception):
    """No se pudo construir el cliente del modelo. El REPL lo muestra y no arranca."""


# --------------------------------------------------------------------------
# La respuesta que ve el core
# --------------------------------------------------------------------------


@dataclass
class Uso:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Respuesta:
    """Lo mínimo que `core.py` le pide a una respuesta: `stop_reason`, `content`, `usage`."""

    stop_reason: str
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: Uso = field(default_factory=Uso)


# --------------------------------------------------------------------------
# Gemini — traducción de ida y de vuelta
# --------------------------------------------------------------------------


def _texto_de_contenido(contenido: Iterable[Any] | None) -> list[str]:
    textos = []
    for parte in contenido or []:
        dato = parte if isinstance(parte, dict) else getattr(parte, "model_dump", dict)()
        if dato.get("type") == "text" and dato.get("text"):
            textos.append(dato["text"])
    return textos


def a_entrada_gemini(mensajes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mensajes en formato de bloques -> lista de `steps` de la Interactions API.

    Los pasos `thought` viajan **verbatim**. Gemini 3 firma su razonamiento y esa
    firma tiene que volver junto al `function_call` que justifica; si se pierde,
    el modelo llama a la misma herramienta de nuevo. Como el core es sin estado
    del lado de Google (la memoria es SQLite local, a propósito), el round-trip
    de la firma es responsabilidad de este archivo y de nadie más.
    """
    pasos: list[dict[str, Any]] = []
    nombres: dict[str, str] = {}  # call_id -> nombre, para etiquetar el resultado

    for mensaje in mensajes:
        contenido = mensaje.get("content")

        if isinstance(contenido, str):
            pasos.append({"type": "user_input", "content": [{"type": "text", "text": contenido}]})
            continue

        for bloque in contenido or []:
            tipo = bloque.get("type")

            if tipo == "text":
                if mensaje.get("role") == "assistant":
                    pasos.append(
                        {"type": "model_output", "content": [{"type": "text", "text": bloque["text"]}]}
                    )
                else:
                    pasos.append(
                        {"type": "user_input", "content": [{"type": "text", "text": bloque["text"]}]}
                    )

            elif tipo == "thought":
                pasos.append({k: v for k, v in bloque.items() if k != "role"})

            elif tipo == "tool_use":
                nombres[bloque.get("id", "")] = bloque.get("name", "")
                pasos.append(
                    {
                        "type": "function_call",
                        "id": bloque.get("id", ""),
                        "name": bloque.get("name", ""),
                        "arguments": bloque.get("input") or {},
                    }
                )

            elif tipo == "tool_result":
                call_id = bloque.get("tool_use_id", "")
                salida = bloque.get("content")
                if not isinstance(salida, str):
                    salida = json.dumps(salida, ensure_ascii=False)
                paso: dict[str, Any] = {
                    "type": "function_result",
                    "call_id": call_id,
                    "result": [{"type": "text", "text": salida}],
                }
                if nombres.get(call_id):
                    paso["name"] = nombres[call_id]
                if bloque.get("is_error"):
                    paso["is_error"] = True
                pasos.append(paso)

    return pasos


def a_respuesta(interaccion: Any) -> Respuesta:
    """`Interaction` de Gemini -> la respuesta de bloques que espera el core."""
    bloques: list[dict[str, Any]] = []

    for paso in getattr(interaccion, "steps", None) or []:
        dato = paso if isinstance(paso, dict) else paso.model_dump(exclude_none=True)
        tipo = dato.get("type")

        if tipo == "model_output":
            for texto in _texto_de_contenido(dato.get("content")):
                bloques.append({"type": "text", "text": texto})

        elif tipo == "thought":
            # Se guarda tal cual para devolverlo en el próximo request. El core lo
            # ignora —solo mira `text` y `tool_use`— así que viaja invisible.
            bloques.append(dato)

        elif tipo == "function_call":
            bloques.append(
                {
                    "type": "tool_use",
                    "id": dato.get("id", ""),
                    "name": dato.get("name", ""),
                    "input": dato.get("arguments") or {},
                }
            )

    uso = getattr(interaccion, "usage", None)
    tokens = Uso(
        input_tokens=int(getattr(uso, "total_input_tokens", 0) or 0),
        output_tokens=int(getattr(uso, "total_output_tokens", 0) or 0),
    )

    # `stop_reason` se deriva de lo que realmente vino, no del `status`: si hay una
    # llamada a herramienta hay que ejecutarla, diga lo que diga el sobre.
    hay_llamadas = any(b["type"] == "tool_use" for b in bloques)
    return Respuesta(
        stop_reason="tool_use" if hay_llamadas else "end_turn",
        content=bloques,
        usage=tokens,
    )


def _nivel_thinking() -> str:
    nivel = (os.environ.get("GEMINI_THINKING") or THINKING_POR_DEFECTO).strip().lower()
    if nivel not in THINKING_VALIDOS:
        raise ErrorDeLLM(
            f"GEMINI_THINKING=«{nivel}» no es válido. "
            f"Opciones: {', '.join(THINKING_VALIDOS)} (default: {THINKING_POR_DEFECTO})"
        )
    return nivel


class _ClienteGemini:
    """Presenta `.messages.create(**peticion)` y traduce a la Interactions API."""

    modelo_por_defecto = MODELOS_POR_DEFECTO["gemini"]

    def __init__(self, cliente: Any) -> None:
        self._cliente = cliente
        self.messages = self  # `cliente.messages.create(...)` resuelve acá

    def create(self, **peticion: Any) -> Respuesta:
        cuerpo: dict[str, Any] = {
            "model": peticion["model"],
            "input": a_entrada_gemini(peticion.get("messages") or []),
            "generation_config": {
                "max_output_tokens": peticion.get("max_tokens"),
                "thinking_level": _nivel_thinking(),
            },
        }
        if peticion.get("system"):
            cuerpo["system_instruction"] = peticion["system"]
        if peticion.get("tools"):
            cuerpo["tools"] = [
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {},
                }
                for t in peticion["tools"]
            ]
        if peticion.get("timeout") is not None:
            cuerpo["timeout"] = peticion["timeout"]

        interaccion = self._cliente.interactions.create(**cuerpo)

        errores = getattr(interaccion, "errors", None)
        if errores:
            raise ErrorDeLLM(f"Gemini devolvió un error: {errores}")

        return a_respuesta(interaccion)


class _ClienteAnthropic:
    """Paso directo: el core ya habla este formato."""

    modelo_por_defecto = MODELOS_POR_DEFECTO["anthropic"]

    def __init__(self, cliente: Any) -> None:
        self.messages = cliente.messages


# --------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------


def _key(proveedor: str) -> str:
    variable = "GEMINI_API_KEY" if proveedor == "gemini" else "ANTHROPIC_API_KEY"
    valor = os.environ.get(variable, "").strip()
    if not valor:
        raise ErrorDeLLM(
            f"Falta {variable} en el entorno para LLM_PROVIDER={proveedor}. "
            "Copia .env.example a .env y complétala."
        )
    return valor


def crear_cliente() -> Any:
    """Construye el cliente del proveedor declarado. Sin proveedor, **no arranca**."""
    proveedor = (os.environ.get("LLM_PROVIDER") or "").strip().lower()

    if not proveedor:
        raise ErrorDeLLM(
            "Falta LLM_PROVIDER en el entorno y este agente NO tiene proveedor por "
            f"defecto. Es una decisión explícita: {' o '.join(PROVEEDORES)}. "
            "Adivinar acá empieza a facturar sin que nadie lo pida."
        )
    if proveedor not in MODELOS_POR_DEFECTO:
        raise ErrorDeLLM(
            f"LLM_PROVIDER=«{proveedor}» no existe. Válidos: {', '.join(PROVEEDORES)}."
        )

    clave = _key(proveedor)

    if proveedor == "gemini":
        try:
            from google import genai
        except ImportError as falta:  # pragma: no cover
            raise ErrorDeLLM(
                "Falta el SDK de Google: pip install -r requirements.txt"
            ) from falta
        return _ClienteGemini(genai.Client(api_key=clave))

    try:
        import anthropic
    except ImportError as falta:  # pragma: no cover
        raise ErrorDeLLM("Falta el SDK de Anthropic: pip install -r requirements.txt") from falta
    return _ClienteAnthropic(anthropic.Anthropic(api_key=clave))
