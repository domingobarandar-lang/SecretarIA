"""El loop y las tres garantías que no dependen del prompt.

Si algún día alguien mueve la confirmación al system prompt «porque es más
simple», estos tests se ponen en rojo. Ese es su trabajo.
"""

from datetime import timedelta

import pytest

from agent import core
from agent.config import validar
from agent.core import Agente, ErrorDelAgente, SolicitudConfirmacion, ventana
from agent.tools import Evento

from tests.conftest import (
    AHORA,
    ZONA,
    ClienteFalso,
    ClienteQueRevienta,
    datos_config,
    en,
    respuesta,
    texto,
    uso_de_tool,
)


def armar(config, memoria, fuente, respuestas, **extra) -> Agente:
    return Agente(
        config,
        memoria,
        cliente=ClienteFalso(respuestas),
        fuente=fuente,
        ahora=lambda: AHORA,
        **extra,
    )


# ==========================================================================
# Garantía 1 — crear_evento no corre sin un sí explícito
# ==========================================================================


def test_sin_confirmador_el_evento_no_se_crea(config, memoria, fuente):
    """Falla cerrado: nadie enchufó una forma de preguntar, así que no se actúa."""
    antes = len(fuente.eventos)
    agente = armar(
        config,
        memoria,
        fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Reunión",
                    "inicio": "2026-08-25T10:00",
                    "fin": "2026-08-25T11:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("No pude crearlo.")]),
        ],
    )
    agente.responder("agenda una reunión")
    assert len(fuente.eventos) == antes


def test_el_default_del_confirmador_es_no():
    assert core.denegar_siempre(
        SolicitudConfirmacion("crear_evento", Evento("e", "X", en(9), en(10)))
    ) is False


def test_un_no_del_usuario_no_crea_nada_y_queda_registrado(config, memoria, fuente):
    antes = len(fuente.eventos)
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Reunión", "inicio": "2026-08-25T10:00", "fin": "2026-08-25T11:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Entendido, no lo creé.")]),
        ],
        confirmar=lambda _: False,
    )
    turno = agente.responder("agenda una reunión")

    assert len(fuente.eventos) == antes
    assert "no lo creé" in turno.texto
    (accion,) = memoria.acciones()
    assert accion.tool == "crear_evento"
    assert accion.confirmada is False
    assert "no confirmó" in accion.resultado


def test_un_si_del_usuario_lo_crea_y_queda_confirmado(config, memoria, fuente):
    antes = len(fuente.eventos)
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Reunión", "inicio": "2026-08-25T10:00", "fin": "2026-08-25T11:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Listo, quedó agendada.")]),
        ],
        confirmar=lambda _: True,
    )
    agente.responder("agenda una reunión")

    assert len(fuente.eventos) == antes + 1
    (accion,) = memoria.acciones()
    assert accion.confirmada is True


def test_las_tools_de_lectura_no_piden_confirmacion(config, memoria, fuente):
    """Preguntar por todo entrena a la persona a decir que sí sin mirar."""
    preguntas = []
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("ver_agenda", {"fecha_inicio": f"{fuente.hoy}"})],
                stop_reason="tool_use",
            ),
            respuesta([texto("Tiene dos reuniones hoy.")]),
        ],
        confirmar=lambda s: preguntas.append(s) or True,
    )
    agente.responder("¿qué tengo hoy?")
    assert preguntas == []


# ==========================================================================
# Garantía 2 — los conflictos los busca el core, no el modelo
# ==========================================================================


def test_el_core_encuentra_el_choque_aunque_el_modelo_nunca_mire_la_agenda(
    config, memoria, fuente
):
    """El modelo va directo a crear_evento. El choque igual aparece en la confirmación."""
    vistas: list[SolicitudConfirmacion] = []
    choque = fuente.eventos[0]  # 09:00–10:30 de hoy

    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Encima del comité",
                    "inicio": f"{fuente.hoy}T09:30",
                    "fin": f"{fuente.hoy}T10:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ok.")]),
        ],
        confirmar=lambda s: vistas.append(s) or False,
    )
    agente.responder("agéndame algo a las 9:30")

    assert len(vistas) == 1
    assert choque.titulo in [c.titulo for c in vistas[0].conflictos]
    assert "Choca con" in vistas[0].como_texto()


def test_sin_choque_la_solicitud_va_limpia(config, memoria, fuente):
    vistas: list[SolicitudConfirmacion] = []
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Hueco libre",
                    "inicio": f"{fuente.hoy}T12:00",
                    "fin": f"{fuente.hoy}T12:30",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ok.")]),
        ],
        confirmar=lambda s: vistas.append(s) or False,
    )
    agente.responder("agéndame algo al mediodía")
    assert vistas[0].conflictos == ()


def test_si_la_agenda_falla_igual_se_pregunta(config, memoria, fuente):
    """No poder revisar conflictos no puede convertirse en no poder decidir."""

    class AgendaRota:
        def ver_agenda(self, *_):
            raise RuntimeError("sin red")

        def crear_evento(self, evento):
            return evento

        def buscar_correos(self, *_):
            return []

        def redactar_borrador(self, *_):
            return "x"

    vistas = []
    agente = armar(
        config, memoria, AgendaRota(),
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "X", "inicio": "2026-08-25T10:00", "fin": "2026-08-25T11:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ok.")]),
        ],
        confirmar=lambda s: vistas.append(s) or False,
    )
    agente.responder("agenda algo")
    assert len(vistas) == 1
    assert vistas[0].conflictos == ()


def test_las_reglas_del_ejecutivo_se_avisan_pero_no_bloquean(memoria, fuente):
    """Un tope de horario es preferencia suya; él está mirando la confirmación."""
    config = validar(datos_config(reglas={"no_agendar_antes_de": "09:00"}))
    vistas = []
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "Temprano",
                    "inicio": f"{fuente.hoy}T07:00",
                    "fin": f"{fuente.hoy}T07:30",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ok.")]),
        ],
        confirmar=lambda s: vistas.append(s) or True,
    )
    agente.responder("agéndame a las 7")

    assert any("antes de las 09:00" in a for a in vistas[0].advertencias)
    assert len(fuente.eventos) == 4  # se creó igual: la persona dijo que sí


def test_un_dia_bloqueado_se_avisa(memoria, fuente):
    config = validar(datos_config(reglas={"dias_bloqueados": ["jueves"]}))
    vistas = []
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("crear_evento", {
                    "titulo": "En jueves",
                    "inicio": "2026-08-20T12:00",
                    "fin": "2026-08-20T13:00",
                })],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ok.")]),
        ],
        confirmar=lambda s: vistas.append(s) or False,
    )
    agente.responder("agenda algo")
    assert any("jueves" in a for a in vistas[0].advertencias)


# ==========================================================================
# Garantía 3 — la auditoría se escribe también cuando la tool falla
# ==========================================================================


def test_una_tool_que_falla_igual_deja_rastro(config, memoria, fuente):
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("ver_agenda", {"fecha_inicio": "mañana por la tarde"})],
                stop_reason="tool_use",
            ),
            respuesta([texto("No entendí la fecha.")]),
        ],
    )
    agente.responder("¿qué tengo?")

    (accion,) = memoria.acciones()
    assert accion.tool == "ver_agenda"
    assert accion.resultado.startswith("ERROR")


def test_el_error_vuelve_al_modelo_como_tool_result_y_el_proceso_sigue(
    config, memoria, fuente
):
    cliente = ClienteFalso(
        [
            respuesta(
                [uso_de_tool("ver_agenda", {"fecha_inicio": "cuando sea"})],
                stop_reason="tool_use",
            ),
            respuesta([texto("Reformulo: ¿qué día?")]),
        ]
    )
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    turno = agente.responder("¿qué tengo?")

    segunda = cliente.peticiones[1]["messages"][-1]
    bloque = segunda["content"][0]
    assert bloque["type"] == "tool_result"
    assert bloque["is_error"] is True
    assert "ERROR" in bloque["content"]
    assert turno.texto.startswith("Reformulo")


def test_una_tool_que_revienta_de_verdad_no_tumba_el_proceso(config, memoria):
    class FuenteQueRevienta:
        def ver_agenda(self, *_):
            raise ZeroDivisionError("boom")

        def crear_evento(self, e):
            return e

        def buscar_correos(self, *_):
            return []

        def redactar_borrador(self, *_):
            return "x"

    agente = armar(
        config, memoria, FuenteQueRevienta(),
        [
            respuesta(
                [uso_de_tool("ver_agenda", {"fecha_inicio": "2026-08-20"})],
                stop_reason="tool_use",
            ),
            respuesta([texto("Hubo un problema técnico.")]),
        ],
    )
    turno = agente.responder("¿qué tengo?")
    assert "problema" in turno.texto
    assert memoria.acciones()[0].resultado.startswith("ERROR inesperado")


def test_la_auditoria_guarda_el_input_que_llego(config, memoria, fuente):
    agente = armar(
        config, memoria, fuente,
        [
            respuesta(
                [uso_de_tool("buscar_correos", {"query": "cierre", "max_resultados": 2})],
                stop_reason="tool_use",
            ),
            respuesta([texto("Encontré uno.")]),
        ],
    )
    agente.responder("busca correos de cierre")
    (accion,) = memoria.acciones()
    assert accion.entrada["query"] == "cierre"
    assert accion.entrada["max_resultados"] == 2


# ==========================================================================
# Ventana deslizante
# ==========================================================================


def test_la_ventana_nunca_empieza_con_assistant():
    mensajes = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert ventana(mensajes, 2)[0]["role"] == "user"


def test_la_ventana_no_deja_un_tool_result_huerfano():
    """Cortar entre el tool_use y su tool_result es un 400 garantizado."""
    mensajes = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "listo"}]},
        {"role": "user", "content": "gracias"},
    ]
    # Los últimos 3 empiezan con el tool_result cuyo tool_use quedó fuera; el
    # recorte tiene que seguir avanzando hasta un punto válido, aunque devuelva
    # menos de lo pedido.
    assert ventana(mensajes, 3) == [{"role": "user", "content": "gracias"}]


def test_la_ventana_puede_quedar_vacia_antes_que_invalida():
    """Preferir un recorte vacío a uno que la API rechaza."""
    mensajes = [
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]},
    ]
    assert ventana(mensajes, 2) == []


def test_la_ventana_devuelve_todo_si_cabe():
    mensajes = [{"role": "user", "content": "a"}]
    assert ventana(mensajes, 10) == mensajes


def test_el_limite_de_historial_sale_de_la_config(memoria, fuente):
    config = validar(datos_config(limites={"max_mensajes_historial": 2}))
    cliente = ClienteFalso([respuesta([texto("ok")])] * 4)
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    for i in range(4):
        agente.responder(f"mensaje {i}")
    assert all(len(p["messages"]) <= 2 for p in cliente.peticiones)


# ==========================================================================
# El loop
# ==========================================================================


def test_el_tope_de_iteraciones_corta(memoria, fuente):
    """Un modelo que pide tools para siempre tiene que chocar contra un techo."""
    config = validar(datos_config(limites={"max_iteraciones_loop": 3}))
    siempre_tool = respuesta(
        [uso_de_tool("ver_agenda", {"fecha_inicio": "2026-08-20"})], stop_reason="tool_use"
    )
    cliente = ClienteFalso([siempre_tool] * 3)
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)

    turno = agente.responder("dale vueltas")
    assert turno.detenido_por_tope is True
    assert turno.iteraciones == 3
    assert len(cliente.peticiones) == 3


def test_varias_tools_en_un_turno_se_responden_todas(config, memoria, fuente):
    cliente = ClienteFalso(
        [
            respuesta(
                [
                    uso_de_tool("ver_agenda", {"fecha_inicio": f"{fuente.hoy}"}, "t1"),
                    uso_de_tool("buscar_correos", {"query": "cierre"}, "t2"),
                ],
                stop_reason="tool_use",
            ),
            respuesta([texto("Ahí va todo.")]),
        ]
    )
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    agente.responder("dime todo")

    resultados = cliente.peticiones[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in resultados] == ["t1", "t2"]
    assert len(memoria.acciones()) == 2


def test_los_tokens_se_acumulan_por_sesion(config, memoria, fuente):
    agente = armar(
        config, memoria, fuente,
        [respuesta([texto("a")], entrada_tokens=100, salida_tokens=20)],
    )
    turno = agente.responder("hola")
    assert turno.tokens == 120
    assert agente.tokens_sesion == 120


def test_un_fallo_del_modelo_es_un_error_del_agente_no_una_caida(config, memoria, fuente):
    agente = Agente(
        config,
        memoria,
        cliente=ClienteQueRevienta(TimeoutError("tardó demasiado")),
        fuente=fuente,
        ahora=lambda: AHORA,
    )
    with pytest.raises(ErrorDelAgente) as info:
        agente.responder("hola")
    assert "no pude hablar con el modelo" in str(info.value)


def test_un_mensaje_vacio_se_rechaza(config, memoria, fuente):
    agente = armar(config, memoria, fuente, [])
    with pytest.raises(ErrorDelAgente):
        agente.responder("   ")


def test_la_conversacion_se_guarda_en_memoria(config, memoria, fuente):
    agente = armar(config, memoria, fuente, [respuesta([texto("Buenas.")])])
    agente.responder("hola")
    roles = [m.rol for m in memoria.historial()]
    assert roles == ["usuario", "asistente"]


# ==========================================================================
# La petición que sale
# ==========================================================================


def test_la_peticion_lleva_timeout_y_tope_de_tokens_de_la_config(memoria, fuente):
    config = validar(
        datos_config(
            agente={"nombre": "Vera", "largo_respuesta": "detallado"},
            limites={"timeout_llamada_modelo_seg": 45},
        )
    )
    cliente = ClienteFalso([respuesta([texto("ok")])])
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    agente.responder("hola")

    peticion = cliente.peticiones[0]
    assert peticion["timeout"] == 45.0
    assert peticion["max_tokens"] == 4096  # «detallado»


def test_solo_viajan_las_tools_habilitadas(memoria, fuente):
    config = validar(datos_config(tools_habilitadas=["ver_agenda", "recordar"]))
    cliente = ClienteFalso([respuesta([texto("ok")])])
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    agente.responder("hola")
    assert [t["name"] for t in cliente.peticiones[0]["tools"]] == ["ver_agenda", "recordar"]


def test_el_modelo_se_puede_tapar_por_entorno(config, memoria, fuente, monkeypatch):
    monkeypatch.setenv("MODELO", "modelo-de-prueba")
    agente = armar(config, memoria, fuente, [respuesta([texto("ok")])])
    assert agente.modelo == "modelo-de-prueba"


def test_sin_variable_de_entorno_manda_el_default(config, memoria, fuente, monkeypatch):
    monkeypatch.delenv("MODELO", raising=False)
    agente = armar(config, memoria, fuente, [respuesta([texto("ok")])])
    assert agente.modelo == core.MODELO_POR_DEFECTO


def test_los_bloques_reales_del_sdk_se_normalizan_a_diccionarios(config, memoria, fuente):
    """El resto de la suite usa diccionarios; el SDK devuelve objetos pydantic.

    Sin este test, un cambio en cómo se serializan los bloques pasaría toda la
    suite en verde y fallaría contra la API de verdad — que es el único lugar
    donde no se puede fallar en silencio, porque el bloque se reenvía tal cual.
    """
    from anthropic.types import TextBlock, ToolUseBlock

    cliente = ClienteFalso(
        [
            respuesta(
                [
                    TextBlock(type="text", text="Reviso su agenda."),
                    ToolUseBlock(
                        type="tool_use",
                        id="toolu_real",
                        name="ver_agenda",
                        input={"fecha_inicio": f"{fuente.hoy}"},
                    ),
                ],
                stop_reason="tool_use",
            ),
            respuesta([TextBlock(type="text", text="Tiene dos reuniones.")]),
        ]
    )
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    turno = agente.responder("¿qué tengo hoy?")

    # Lo que se reenvía tiene que ser JSON puro, no objetos del SDK.
    import json

    eco = cliente.peticiones[1]["messages"]
    json.dumps(eco)  # revienta si quedó un objeto pydantic adentro

    assistant = [m for m in eco if m["role"] == "assistant"][-1]
    tipos = [b["type"] for b in assistant["content"]]
    assert tipos == ["text", "tool_use"]
    assert assistant["content"][1]["id"] == "toolu_real"
    assert turno.texto == "Tiene dos reuniones."
    assert memoria.acciones()[0].tool == "ver_agenda"


def test_el_system_prompt_viaja_en_cada_peticion(config, memoria, fuente):
    cliente = ClienteFalso([respuesta([texto("ok")])])
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    agente.responder("hola")
    assert "Vera" in cliente.peticiones[0]["system"]


def test_los_hechos_nuevos_entran_al_prompt_del_turno_siguiente(config, memoria, fuente):
    cliente = ClienteFalso(
        [
            respuesta(
                [uso_de_tool("recordar", {"hecho": "odia las reuniones de 30 minutos"})],
                stop_reason="tool_use",
            ),
            respuesta([texto("Anotado.")]),
            respuesta([texto("Claro.")]),
        ]
    )
    agente = Agente(config, memoria, cliente=cliente, fuente=fuente, ahora=lambda: AHORA)
    agente.responder("recuerda que odio las reuniones de 30 minutos")
    assert "odia las reuniones de 30 minutos" not in cliente.peticiones[0]["system"]

    agente.responder("¿qué sabes de mí?")
    assert "odia las reuniones de 30 minutos" in cliente.peticiones[2]["system"]
