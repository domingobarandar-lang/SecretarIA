"""La fuente real, probada sin red, sin credenciales y sin tocar ninguna cuenta.

Lo que más importa acá no es que las llamadas funcionen: es que las llamadas que
**no** existen sigan sin existir. El MCP de Calendar publica `delete_event`; el de
Gmail publica etiquetado y papelera. SecretarIA usa cuatro herramientas de las
diecisiete y el resto tiene que ser inalcanzable por construcción, no por olvido.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.tools import Correo, Evento
from integrations import google_mcp as g
from integrations.google_mcp import (
    ClienteMCP,
    Credenciales,
    ErrorDeGoogle,
    FuenteGoogle,
    a_correos,
    a_evento,
)

ZONA = ZoneInfo("America/Santiago")
RUTA = Path(g.__file__)


# --------------------------------------------------------------------------
# Dobles de HTTP
# --------------------------------------------------------------------------


class RespuestaFalsa:
    def __init__(self, cuerpo: Any, *, status: int = 200, tipo: str = "application/json", cabeceras=None):
        self._cuerpo = cuerpo
        self.status_code = status
        self.headers = {"content-type": tipo, **(cabeceras or {})}
        self.text = cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo)

    def json(self) -> Any:
        return self._cuerpo if not isinstance(self._cuerpo, str) else json.loads(self._cuerpo)


class HttpFalso:
    """Contesta según el método JSON-RPC que le pidan. Registra todo."""

    def __init__(self, respuestas: dict[str, Any] | None = None) -> None:
        self.respuestas = respuestas or {}
        self.llamadas: list[dict[str, Any]] = []

    def post(self, url: str, *, headers=None, json=None, data=None) -> RespuestaFalsa:
        self.llamadas.append({"url": url, "headers": headers or {}, "json": json, "data": data})

        if url == g.URL_TOKEN:
            return RespuestaFalsa({"access_token": "tok-123", "expires_in": 3600})

        metodo = (json or {}).get("method", "")
        if metodo == "initialize":
            return RespuestaFalsa({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": g.PROTOCOLO}})
        if metodo.startswith("notifications/"):
            return RespuestaFalsa({}, status=202)
        if metodo == "tools/list":
            return RespuestaFalsa({"jsonrpc": "2.0", "id": 2, "result": catalogo_para(url)})
        if metodo == "tools/call":
            nombre = json["params"]["name"]
            preparada = self.respuestas.get(nombre, {})
            return RespuestaFalsa(
                {"jsonrpc": "2.0", "id": 3, "result": {"structuredContent": preparada}}
            )
        raise AssertionError(f"método inesperado: {metodo}")


def herramienta(nombre: str, parametros: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": nombre,
        "inputSchema": {"type": "object", "properties": {p: {"type": "string"} for p in parametros}},
    }


def catalogo_para(url: str) -> dict[str, Any]:
    """Lo que declara cada servidor — incluyendo lo que nosotros no usamos."""
    if "calendar" in url:
        return {
            "tools": [
                herramienta("list_events", g._PARAMETROS_QUE_USAMOS["list_events"]),
                herramienta("create_event", g._PARAMETROS_QUE_USAMOS["create_event"]),
                herramienta("delete_event", ("eventId",)),  # existe y no se toca
                herramienta("update_event", ("eventId",)),
            ]
        }
    return {
        "tools": [
            herramienta("search_threads", g._PARAMETROS_QUE_USAMOS["search_threads"]),
            herramienta("create_draft", g._PARAMETROS_QUE_USAMOS["create_draft"]),
            herramienta("trash_thread", ("threadId",)),
        ]
    }


def llaves() -> Credenciales:
    return Credenciales("id", "secreto", "refresh")


def fuente(respuestas: dict[str, Any] | None = None) -> tuple[FuenteGoogle, HttpFalso]:
    http = HttpFalso(respuestas)
    creds = llaves()
    calendar = ClienteMCP("calendar", g.URLS_POR_DEFECTO["calendar"], creds, http)
    gmail = ClienteMCP("gmail", g.URLS_POR_DEFECTO["gmail"], creds, http)
    calendar.conectar()
    gmail.conectar()
    return FuenteGoogle(zona=ZONA, calendar=calendar, gmail=gmail), http


# --------------------------------------------------------------------------
# Lo que no se puede hacer
# --------------------------------------------------------------------------


def test_borrar_un_evento_no_es_alcanzable_aunque_el_servidor_lo_publique():
    """`delete_event` está en el catálogo del servidor y sigue siendo imposible."""
    f, http = fuente()
    assert "delete_event" in f.calendar.catalogo  # el servidor sí lo ofrece

    antes = len(http.llamadas)
    with pytest.raises(AssertionError):
        f.calendar.llamar("delete_event", {"eventId": "x"})
    assert len(http.llamadas) == antes, "ni siquiera se intentó salir a la red"


@pytest.mark.parametrize("prohibido", ["delete_event", "update_event", "trash_thread", "send_message"])
def test_ningun_metodo_destructivo_esta_en_la_lista_permitida(prohibido):
    permitidos = {m for metodos in g._METODOS_PERMITIDOS.values() for m in metodos}
    assert prohibido not in permitidos


def test_la_lista_permitida_son_cuatro_y_solo_cuatro():
    permitidos = {m for metodos in g._METODOS_PERMITIDOS.values() for m in metodos}
    assert permitidos == {"list_events", "create_event", "search_threads", "create_draft"}


def test_drive_no_se_conecta():
    """Se declararon tres URLs. Drive no entra: ninguna de las cinco tools lo usa."""
    assert "drive" not in g.SERVIDORES
    assert "drive" not in g.URLS_POR_DEFECTO
    assert not any("drive" in u for u in g.URLS_POR_DEFECTO.values())
    assert "drivemcp.googleapis.com" not in RUTA.read_text(encoding="utf-8")


def test_el_archivo_no_menciona_ninguna_herramienta_de_envio():
    codigo = RUTA.read_text(encoding="utf-8")
    assert "send_message" not in codigo and '"send"' not in codigo


def test_el_modelo_nunca_recibe_el_mcp_como_tool():
    """El MCP vive detrás de `FuenteDatos`; el modelo solo ve las cinco tools."""
    from agent.config import TOOLS_DISPONIBLES
    from agent.tools import CATALOGO

    assert set(CATALOGO) == set(TOOLS_DISPONIBLES)
    assert not any("mcp" in n.lower() for n in CATALOGO)


# --------------------------------------------------------------------------
# El contrato con el servidor
# --------------------------------------------------------------------------


def test_si_falta_una_herramienta_el_agente_no_arranca(monkeypatch):
    class SinCrearEvento(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            respuesta = super().post(url, headers=headers, json=json, data=data)
            if (json or {}).get("method") == "tools/list":
                datos = respuesta.json()
                datos["result"]["tools"] = [
                    t for t in datos["result"]["tools"] if t["name"] != "create_event"
                ]
                return RespuestaFalsa(datos)
            return respuesta

    cliente = ClienteMCP("calendar", g.URLS_POR_DEFECTO["calendar"], llaves(), SinCrearEvento())
    with pytest.raises(ErrorDeGoogle) as info:
        cliente.conectar()
    assert "create_event" in str(info.value)


def test_si_google_renombra_un_parametro_el_agente_no_arranca():
    class ParametroRenombrado(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            respuesta = super().post(url, headers=headers, json=json, data=data)
            if (json or {}).get("method") == "tools/list":
                datos = respuesta.json()
                for t in datos["result"]["tools"]:
                    if t["name"] == "list_events":
                        t["inputSchema"]["properties"].pop("startTime")
                        t["inputSchema"]["properties"]["desde"] = {"type": "string"}
                return RespuestaFalsa(datos)
            return respuesta

    cliente = ClienteMCP("calendar", g.URLS_POR_DEFECTO["calendar"], llaves(), ParametroRenombrado())
    with pytest.raises(ErrorDeGoogle) as info:
        cliente.conectar()
    assert "startTime" in str(info.value)


def test_el_handshake_manda_la_version_del_protocolo_y_el_token():
    _, http = fuente()
    inicial = next(l for l in http.llamadas if (l["json"] or {}).get("method") == "initialize")
    assert inicial["headers"]["Authorization"] == "Bearer tok-123"
    assert inicial["headers"]["MCP-Protocol-Version"] == g.PROTOCOLO


def test_un_error_http_se_cuenta_con_el_codigo():
    class Rota(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            if url != g.URL_TOKEN:
                return RespuestaFalsa("permiso denegado", status=403, tipo="text/plain")
            return super().post(url, headers=headers, json=json, data=data)

    cliente = ClienteMCP("gmail", g.URLS_POR_DEFECTO["gmail"], llaves(), Rota())
    with pytest.raises(ErrorDeGoogle) as info:
        cliente.conectar()
    assert "403" in str(info.value)


def test_una_respuesta_por_stream_de_eventos_se_lee_igual():
    """El transporte puede contestar JSON o SSE para el mismo request."""
    class PorStream(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            respuesta = super().post(url, headers=headers, json=json, data=data)
            if url == g.URL_TOKEN or respuesta.status_code == 202:
                return respuesta
            cuerpo = f"event: message\ndata: {json_dumps(respuesta.json())}\n\n"
            return RespuestaFalsa(cuerpo, tipo="text/event-stream")

    def json_dumps(x):
        return json.dumps(x)

    cliente = ClienteMCP("gmail", g.URLS_POR_DEFECTO["gmail"], llaves(), PorStream())
    cliente.conectar()
    assert "create_draft" in cliente.catalogo


# --------------------------------------------------------------------------
# Credenciales
# --------------------------------------------------------------------------


def test_el_token_se_reusa_hasta_que_vence():
    http = HttpFalso()
    creds = llaves()
    assert creds.access_token(http, ahora=0.0) == "tok-123"
    creds.access_token(http, ahora=100.0)
    assert sum(1 for l in http.llamadas if l["url"] == g.URL_TOKEN) == 1
    creds.access_token(http, ahora=99_999.0)  # ya venció
    assert sum(1 for l in http.llamadas if l["url"] == g.URL_TOKEN) == 2


def test_un_refresh_token_rechazado_se_dice_en_castellano():
    class Rechaza(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            return RespuestaFalsa({"error": "invalid_grant"}, status=400)

    with pytest.raises(ErrorDeGoogle) as info:
        llaves().access_token(Rechaza())
    assert "autorizar" in str(info.value)


def test_sin_credenciales_en_el_entorno_se_nombran_las_que_faltan(monkeypatch):
    for v in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ErrorDeGoogle) as info:
        Credenciales.desde_entorno()
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" in str(info.value)


# --------------------------------------------------------------------------
# Traducción
# --------------------------------------------------------------------------


def test_un_evento_de_google_se_convierte_en_uno_del_dominio():
    e = a_evento(
        {
            "id": "evt_1",
            "summary": "Directorio",
            "start": {"dateTime": "2026-08-20T11:00:00-04:00"},
            "end": {"dateTime": "2026-08-20T13:00:00-04:00"},
            "attendees": [{"email": "a@x.cl"}, {"displayName": "sin correo"}],
            "description": "Anual",
        },
        ZONA,
    )
    assert e.titulo == "Directorio"
    assert e.invitados == ("a@x.cl",)
    assert e.inicio.hour == 11 and e.inicio.tzinfo is not None


def test_un_evento_de_dia_completo_no_rompe_la_comparacion_de_conflictos():
    e = a_evento({"id": "x", "summary": "Feriado", "start": {"date": "2026-08-20"}, "end": {"date": "2026-08-21"}}, ZONA)
    assert e.inicio.tzinfo is not None and e.fin > e.inicio


def test_un_evento_sin_fecha_se_denuncia():
    with pytest.raises(ErrorDeGoogle):
        a_evento({"id": "x", "summary": "roto", "start": {}, "end": {}}, ZONA)


def test_un_evento_sin_titulo_no_queda_en_blanco():
    e = a_evento({"start": {"dateTime": "2026-08-20T11:00:00-04:00"}, "end": {"dateTime": "2026-08-20T12:00:00-04:00"}}, ZONA)
    assert e.titulo == "(sin título)"


def test_de_cada_hilo_se_toma_el_ultimo_mensaje():
    """«¿Qué me escribió finanzas?» se responde con lo último, no con el arranque."""
    correos = a_correos(
        {
            "threads": [
                {
                    "id": "t1",
                    "messages": [
                        {"id": "m1", "sender": "a@x.cl", "subject": "Cierre", "date": "2026-08-01T09:00:00-04:00", "snippet": "primero"},
                        {"id": "m2", "sender": "a@x.cl", "subject": "Re: Cierre", "date": "2026-08-05T09:00:00-04:00", "snippet": "lo último"},
                    ],
                }
            ]
        },
        ZONA,
        5,
    )
    assert len(correos) == 1 and correos[0].extracto == "lo último"


def test_los_correos_salen_del_mas_nuevo_al_mas_viejo_y_respetan_el_tope():
    datos = {
        "threads": [
            {"id": f"t{i}", "messages": [{"id": f"m{i}", "sender": "a@x.cl", "subject": f"s{i}", "date": f"2026-08-{i:02d}T09:00:00-04:00", "snippet": ""}]}
            for i in range(1, 6)
        ]
    }
    correos = a_correos(datos, ZONA, 2)
    assert len(correos) == 2
    assert correos[0].fecha > correos[1].fecha


def test_un_hilo_con_fecha_ilegible_no_tumba_la_busqueda_entera():
    correos = a_correos(
        {
            "threads": [
                {"id": "malo", "messages": [{"id": "m", "date": "ayer por la tarde"}]},
                {"id": "bueno", "messages": [{"id": "m2", "sender": "b@x.cl", "subject": "ok", "date": "2026-08-05T09:00:00-04:00", "snippet": "sí"}]},
            ]
        },
        ZONA,
        5,
    )
    assert [c.id for c in correos] == ["m2"]


# --------------------------------------------------------------------------
# Las cuatro llamadas
# --------------------------------------------------------------------------


def test_ver_agenda_manda_el_rango_y_la_zona():
    f, http = fuente({"list_events": {"events": []}})
    f.ver_agenda(
        datetime(2026, 8, 20, 9, tzinfo=ZONA), datetime(2026, 8, 20, 18, tzinfo=ZONA)
    )
    argumentos = http.llamadas[-1]["json"]["params"]["arguments"]
    assert argumentos["timeZone"] == "America/Santiago"
    assert argumentos["startTime"].startswith("2026-08-20T09:00")
    assert argumentos["orderBy"] == "startTime"


def test_crear_evento_traduce_invitados_al_formato_de_google():
    f, http = fuente(
        {
            "create_event": {
                "id": "evt_nuevo",
                "summary": "Café",
                "start": {"dateTime": "2026-08-21T10:00:00-04:00"},
                "end": {"dateTime": "2026-08-21T11:00:00-04:00"},
            }
        }
    )
    creado = f.crear_evento(
        Evento("", "Café", datetime(2026, 8, 21, 10, tzinfo=ZONA), datetime(2026, 8, 21, 11, tzinfo=ZONA), ("b@x.cl",))
    )
    argumentos = http.llamadas[-1]["json"]["params"]["arguments"]
    assert argumentos["attendees"] == [{"email": "b@x.cl"}]
    assert argumentos["summary"] == "Café"
    assert creado.id == "evt_nuevo"


def test_un_evento_creado_con_respuesta_incompleta_no_se_reporta_como_fallo():
    """Decir «falló» haría que el ejecutivo lo cree de nuevo y quede duplicado."""
    f, _ = fuente({"create_event": {"id": "evt_x"}})  # sin start ni end
    creado = f.crear_evento(
        Evento("", "Café", datetime(2026, 8, 21, 10, tzinfo=ZONA), datetime(2026, 8, 21, 11, tzinfo=ZONA))
    )
    assert creado.id == "evt_x" and creado.titulo == "Café"


def test_redactar_borrador_devuelve_el_id_y_llama_a_create_draft():
    f, http = fuente({"create_draft": {"id": "draft_9"}})
    assert f.redactar_borrador("c@x.cl", "Hola", "Cuerpo") == "draft_9"
    llamada = http.llamadas[-1]["json"]["params"]
    assert llamada["name"] == "create_draft"
    assert llamada["arguments"]["to"] == ["c@x.cl"]


def test_buscar_correos_no_pide_mas_de_lo_que_el_contrato_permite():
    f, http = fuente({"search_threads": {"threads": []}})
    f.buscar_correos("finanzas", 999)
    assert http.llamadas[-1]["json"]["params"]["arguments"]["pageSize"] == 25


def test_un_error_declarado_por_la_herramienta_se_propaga():
    class ToolQueFalla(HttpFalso):
        def post(self, url, *, headers=None, json=None, data=None):
            if (json or {}).get("method") == "tools/call":
                return RespuestaFalsa(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "result": {"isError": True, "content": [{"type": "text", "text": "sin permiso"}]},
                    }
                )
            return super().post(url, headers=headers, json=json, data=data)

    creds = llaves()
    http = ToolQueFalla()
    calendar = ClienteMCP("calendar", g.URLS_POR_DEFECTO["calendar"], creds, http)
    gmail = ClienteMCP("gmail", g.URLS_POR_DEFECTO["gmail"], creds, http)
    calendar.conectar()
    gmail.conectar()
    with pytest.raises(ErrorDeGoogle) as info:
        FuenteGoogle(zona=ZONA, calendar=calendar, gmail=gmail).ver_agenda(
            datetime(2026, 8, 20, tzinfo=ZONA), datetime(2026, 8, 21, tzinfo=ZONA)
        )
    assert "sin permiso" in str(info.value)


# --------------------------------------------------------------------------
# La costura: el agente completo sobre la fuente real
# --------------------------------------------------------------------------


def test_el_agente_entero_corre_sobre_la_fuente_de_google(config, memoria):
    """La prueba de que `FuenteGoogle` y `FuenteMock` son intercambiables."""
    from agent.core import Agente
    from conftest import AHORA, ClienteFalso, respuesta, texto, uso_de_tool

    f, _ = fuente(
        {
            "list_events": {
                "events": [
                    {
                        "id": "evt_1",
                        "summary": "Comité",
                        "start": {"dateTime": "2026-08-20T09:00:00-04:00"},
                        "end": {"dateTime": "2026-08-20T10:00:00-04:00"},
                    }
                ]
            }
        }
    )
    cliente = ClienteFalso(
        [
            respuesta([uso_de_tool("ver_agenda", {"fecha_inicio": "2026-08-20"})], stop_reason="tool_use"),
            respuesta([texto("Tiene el Comité a las 9.")]),
        ]
    )
    agente = Agente(config, memoria, cliente=cliente, fuente=f, ahora=lambda: AHORA)
    turno = agente.responder("¿qué tengo el jueves?")

    assert turno.texto == "Tiene el Comité a las 9."
    resultado = cliente.peticiones[1]["messages"][-1]["content"][0]["content"]
    assert "Comité" in resultado


def test_la_fuente_real_implementa_los_mismos_cuatro_metodos_que_el_mock():
    from agent.tools import FuenteDatos, FuenteMock

    contrato = {m for m in dir(FuenteDatos) if not m.startswith("_")}
    assert contrato <= {m for m in dir(FuenteGoogle) if not m.startswith("_")}
    assert contrato <= {m for m in dir(FuenteMock) if not m.startswith("_")}
