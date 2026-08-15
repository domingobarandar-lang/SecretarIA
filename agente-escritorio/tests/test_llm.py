"""El adapter multi-proveedor: la traducción y la puerta que falla cerrada.

Dos cosas se prueban acá y las dos fallan en silencio si nadie las mira:

1. **La puerta**. Sin `LLM_PROVIDER` el agente no arranca. Un default acá no se
   nota en ninguna prueba funcional —el agente responde igual de bien— y se
   descubre en la factura.
2. **El round-trip de la firma de razonamiento.** Gemini 3 firma su pensamiento y
   esa firma tiene que volver junto al `function_call` que justifica. Si se
   pierde, el agente sigue funcionando *casi* bien: repite llamadas a
   herramientas y gasta el doble. No hay excepción, no hay error, solo cuenta.

La traducción de salida se prueba contra los **tipos reales del SDK**, construidos
sin red con `model_validate`. Un diccionario escrito a mano probaría que mi idea
del formato es consistente conmigo misma, que es justo lo que no importa.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent import llm
from agent.llm import ErrorDeLLM, a_entrada_gemini, a_respuesta, crear_cliente

RUTA_LLM = __import__("pathlib").Path(llm.__file__)


# --------------------------------------------------------------------------
# La puerta: sin proveedor no se arranca
# --------------------------------------------------------------------------


def test_sin_proveedor_no_hay_cliente(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(ErrorDeLLM) as info:
        crear_cliente()
    # Tener las dos keys a mano no alcanza: la decisión se escribe.
    assert "LLM_PROVIDER" in str(info.value)


def test_un_proveedor_desconocido_no_se_parece_a_ninguno(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ErrorDeLLM) as info:
        crear_cliente()
    assert "gemini" in str(info.value) and "anthropic" in str(info.value)


@pytest.mark.parametrize(
    "proveedor,variable,otra",
    [
        ("gemini", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"),
    ],
)
def test_la_key_que_falta_es_la_del_proveedor_declarado(
    monkeypatch, proveedor, variable, otra
):
    """La key del *otro* proveedor no sirve de consuelo."""
    monkeypatch.setenv("LLM_PROVIDER", proveedor)
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv(otra, "key-del-otro")
    with pytest.raises(ErrorDeLLM) as info:
        crear_cliente()
    assert variable in str(info.value)


@pytest.mark.parametrize("vacia", ["", "   "])
def test_una_key_en_blanco_es_una_key_que_falta(monkeypatch, vacia):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", vacia)
    with pytest.raises(ErrorDeLLM):
        crear_cliente()


def test_el_default_de_cada_proveedor_es_el_modelo_que_se_decidio():
    assert llm.MODELOS_POR_DEFECTO["gemini"] == "gemini-3.1-pro-preview"
    assert llm._ClienteGemini.modelo_por_defecto == "gemini-3.1-pro-preview"


# --------------------------------------------------------------------------
# Traducción de ida: bloques -> steps
# --------------------------------------------------------------------------


def test_un_mensaje_de_texto_del_usuario_es_un_user_input():
    pasos = a_entrada_gemini([{"role": "user", "content": "hola"}])
    assert pasos == [{"type": "user_input", "content": [{"type": "text", "text": "hola"}]}]


def test_el_texto_del_asistente_y_su_llamada_viajan_en_orden():
    pasos = a_entrada_gemini(
        [
            {"role": "user", "content": "¿qué tengo hoy?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reviso."},
                    {"type": "tool_use", "id": "fc_1", "name": "ver_agenda", "input": {"d": 1}},
                ],
            },
        ]
    )
    assert [p["type"] for p in pasos] == ["user_input", "model_output", "function_call"]
    assert pasos[2] == {
        "type": "function_call",
        "id": "fc_1",
        "name": "ver_agenda",
        "arguments": {"d": 1},
    }


def test_un_tool_result_se_convierte_en_function_result_con_su_nombre():
    """El `name` no está en el bloque de origen: se deduce de la llamada anterior."""
    pasos = a_entrada_gemini(
        [
            {"role": "user", "content": "hola"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "fc_1", "name": "ver_agenda", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "fc_1", "content": "sin eventos"}
                ],
            },
        ]
    )
    assert pasos[-1] == {
        "type": "function_result",
        "call_id": "fc_1",
        "name": "ver_agenda",
        "result": [{"type": "text", "text": "sin eventos"}],
    }


def test_un_resultado_con_error_lo_declara():
    pasos = a_entrada_gemini(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "fc_9",
                        "content": "ERROR: fecha inválida",
                        "is_error": True,
                    }
                ],
            }
        ]
    )
    assert pasos[0]["is_error"] is True


def test_un_resultado_que_no_es_texto_se_serializa_sin_romper():
    pasos = a_entrada_gemini(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": {"a": [1, "ñ"]}}
                ],
            }
        ]
    )
    assert json.loads(pasos[0]["result"][0]["text"]) == {"a": [1, "ñ"]}


# --------------------------------------------------------------------------
# Traducción de vuelta: Interaction real -> bloques
# --------------------------------------------------------------------------


def interaccion(pasos: list[dict[str, Any]], **extra: Any):
    """Una `Interaction` de verdad del SDK, construida sin tocar la red."""
    from google.genai import interactions as tipos

    crudo = {"status": "completed", "model": "gemini-3.1-pro-preview", "steps": pasos}
    crudo.update(extra)
    return tipos.Interaction.model_validate(crudo)


def test_el_texto_del_modelo_llega_como_bloque_de_texto():
    r = a_respuesta(
        interaccion([{"type": "model_output", "content": [{"type": "text", "text": "Listo."}]}])
    )
    assert r.stop_reason == "end_turn"
    assert r.content == [{"type": "text", "text": "Listo."}]


def test_una_llamada_a_herramienta_se_traduce_y_manda_a_ejecutar():
    r = a_respuesta(
        interaccion(
            [
                {
                    "type": "function_call",
                    "id": "fc_7",
                    "name": "crear_evento",
                    "arguments": {"titulo": "Directorio"},
                }
            ],
            status="requires_action",
        )
    )
    assert r.stop_reason == "tool_use"
    assert r.content == [
        {"type": "tool_use", "id": "fc_7", "name": "crear_evento", "input": {"titulo": "Directorio"}}
    ]


def test_el_stop_reason_sale_del_contenido_no_del_sobre():
    """Un `status` que dice `requires_action` sin llamadas no manda a ejecutar nada."""
    r = a_respuesta(
        interaccion(
            [{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}],
            status="requires_action",
        )
    )
    assert r.stop_reason == "end_turn"


def test_los_tokens_se_normalizan_al_vocabulario_del_core():
    r = a_respuesta(
        interaccion(
            [{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}],
            usage={"total_input_tokens": 120, "total_output_tokens": 34, "total_tokens": 154},
        )
    )
    assert (r.usage.input_tokens, r.usage.output_tokens) == (120, 34)


def test_una_interaccion_sin_uso_no_revienta():
    r = a_respuesta(interaccion([]))
    assert r.usage.input_tokens == 0


def test_todo_lo_que_sale_es_json_puro():
    """Los bloques se reenvían tal cual en el próximo request: nada de pydantic."""
    r = a_respuesta(
        interaccion(
            [
                {"type": "thought", "signature": "c2ln", "summary": [{"type": "text", "text": "x"}]},
                {"type": "model_output", "content": [{"type": "text", "text": "Reviso."}]},
                {"type": "function_call", "id": "fc_1", "name": "ver_agenda", "arguments": {}},
            ]
        )
    )
    json.dumps(r.content)  # revienta si quedó un objeto del SDK adentro


# --------------------------------------------------------------------------
# La firma de razonamiento: el silencio caro
# --------------------------------------------------------------------------


def test_la_firma_del_razonamiento_sobrevive_la_vuelta_completa():
    """Sale de Gemini, pasa por el core como bloque opaco y vuelve idéntica.

    Si esto se rompe el agente no falla: repite llamadas y factura de más.
    """
    salida = a_respuesta(
        interaccion(
            [
                {"type": "thought", "signature": "ZmlybWEtcmVhbA==", "summary": []},
                {"type": "function_call", "id": "fc_1", "name": "ver_agenda", "arguments": {}},
            ],
            status="requires_action",
        )
    )
    firma = [b for b in salida.content if b["type"] == "thought"]
    assert firma and firma[0]["signature"] == "ZmlybWEtcmVhbA=="

    # …y ahora el camino de vuelta, tal como lo arma el core.
    pasos = a_entrada_gemini([{"role": "assistant", "content": salida.content}])
    devuelta = [p for p in pasos if p["type"] == "thought"]
    assert devuelta and devuelta[0]["signature"] == "ZmlybWEtcmVhbA=="
    # El orden importa: la firma justifica la llamada que viene después.
    assert [p["type"] for p in pasos] == ["thought", "function_call"]


def test_el_core_no_confunde_un_pensamiento_con_una_respuesta():
    """El bloque `thought` viaja invisible: el core solo lee `text` y `tool_use`."""
    from agent.core import _texto_de

    contenido = [
        {"type": "thought", "signature": "abc", "summary": [{"type": "text", "text": "pienso"}]},
        {"type": "text", "text": "Tiene dos reuniones."},
    ]
    assert _texto_de(contenido) == "Tiene dos reuniones."


# --------------------------------------------------------------------------
# La petición que sale hacia Gemini
# --------------------------------------------------------------------------


class GenAIFalso:
    """Un `genai.Client` de mentira: registra el cuerpo y devuelve lo preparado."""

    def __init__(self, respuestas: list[Any]) -> None:
        self._respuestas = list(respuestas)
        self.cuerpos: list[dict[str, Any]] = []
        self.interactions = self

    def create(self, **cuerpo: Any) -> Any:
        self.cuerpos.append(cuerpo)
        return self._respuestas.pop(0)


def test_la_peticion_se_arma_con_el_vocabulario_de_gemini(monkeypatch):
    monkeypatch.delenv("GEMINI_THINKING", raising=False)
    falso = GenAIFalso([interaccion([])])
    cliente = llm._ClienteGemini(falso)

    cliente.messages.create(
        model="gemini-3.1-pro-preview",
        max_tokens=1024,
        system="Eres Vera.",
        messages=[{"role": "user", "content": "hola"}],
        tools=[{"name": "ver_agenda", "description": "Lee la agenda", "input_schema": {"type": "object"}}],
        timeout=20.0,
    )

    cuerpo = falso.cuerpos[0]
    assert cuerpo["system_instruction"] == "Eres Vera."
    assert cuerpo["generation_config"]["max_output_tokens"] == 1024
    assert cuerpo["generation_config"]["thinking_level"] == "low"
    assert cuerpo["timeout"] == 20.0
    # `input_schema` de Anthropic es `parameters` en Gemini, y el envoltorio cambia.
    assert cuerpo["tools"] == [
        {
            "type": "function",
            "name": "ver_agenda",
            "description": "Lee la agenda",
            "parameters": {"type": "object"},
        }
    ]
    assert "max_tokens" not in cuerpo and "messages" not in cuerpo


def test_el_nivel_de_razonamiento_se_puede_subir(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING", "high")
    falso = GenAIFalso([interaccion([])])
    llm._ClienteGemini(falso).messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}]
    )
    assert falso.cuerpos[0]["generation_config"]["thinking_level"] == "high"


def test_un_nivel_de_razonamiento_inventado_es_un_error(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING", "maximo")
    falso = GenAIFalso([interaccion([])])
    with pytest.raises(ErrorDeLLM) as info:
        llm._ClienteGemini(falso).messages.create(
            model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )
    assert "minimal" in str(info.value)


def test_un_error_de_gemini_no_se_devuelve_como_respuesta_vacia():
    falso = GenAIFalso([interaccion([], errors=[{"message": "cuota agotada"}])])
    with pytest.raises(ErrorDeLLM):
        llm._ClienteGemini(falso).messages.create(
            model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}]
        )


def test_sin_tools_habilitadas_no_se_manda_la_llave():
    falso = GenAIFalso([interaccion([])])
    llm._ClienteGemini(falso).messages.create(
        model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}], tools=[]
    )
    assert "tools" not in falso.cuerpos[0]


# --------------------------------------------------------------------------
# El loop entero, contra el adapter de verdad
# --------------------------------------------------------------------------


def test_el_agente_completo_corre_sobre_gemini(config, memoria, fuente):
    """De punta a punta: el core no sabe que cambió de proveedor."""
    from agent.core import Agente
    from conftest import AHORA

    falso = GenAIFalso(
        [
            interaccion(
                [
                    {"type": "thought", "signature": "Zmlybwo=", "summary": []},
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "name": "ver_agenda",
                        "arguments": {"fecha_inicio": f"{fuente.hoy}"},
                    },
                ],
                status="requires_action",
            ),
            interaccion(
                [{"type": "model_output", "content": [{"type": "text", "text": "Tiene dos."}]}],
                usage={"total_input_tokens": 50, "total_output_tokens": 8},
            ),
        ]
    )
    agente = Agente(
        config,
        memoria,
        cliente=llm._ClienteGemini(falso),
        fuente=fuente,
        ahora=lambda: AHORA,
    )
    turno = agente.responder("¿qué tengo hoy?")

    assert turno.texto == "Tiene dos."
    assert turno.herramientas == ("ver_agenda",)
    assert agente.modelo == "gemini-3.1-pro-preview"

    # La segunda petición lleva la firma y el resultado de la herramienta.
    tipos = [p["type"] for p in falso.cuerpos[1]["input"]]
    assert tipos == ["user_input", "thought", "function_call", "function_result"]
    json.dumps(falso.cuerpos[1]["input"])  # tiene que ser serializable


# --------------------------------------------------------------------------
# Lo que el adapter tiene prohibido hacer
# --------------------------------------------------------------------------


def test_el_adapter_nunca_declara_el_tool_mcp_server_de_gemini():
    """La API de Gemini sabe hablar MCP sola. Usarlo acá sería un agujero.

    `mcp_server` le entrega al modelo el catálogo completo del servidor —en el de
    Calendar, `delete_event` incluido— por fuera de las cinco tools declaradas,
    de la confirmación humana y de la tabla `acciones`. El MCP se usa detrás de
    `FuenteDatos`, llamado por nuestro código. Ver `integrations/google_mcp.py`.
    """
    codigo = RUTA_LLM.read_text(encoding="utf-8")
    assert '"mcp_server"' not in codigo and "'mcp_server'" not in codigo
