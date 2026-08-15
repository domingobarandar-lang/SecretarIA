"""Las cinco tools: contrato, parseo de fechas y despacho.

El test que más vale acá es el que comprueba que enviar correo NO existe.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent import tools
from agent.config import TOOLS_DISPONIBLES, validar
from agent.tools import ErrorDeHerramienta, Evento

from tests.conftest import ZONA, datos_config, en


# -- contrato --------------------------------------------------------------


def test_el_catalogo_cubre_exactamente_las_tools_del_contrato():
    assert set(tools.CATALOGO) == set(TOOLS_DISPONIBLES)


def test_enviar_correo_no_existe_y_es_a_proposito():
    """No es un pendiente: la tool no está y el contrato lo dice en voz alta."""
    assert "enviar_correo" not in tools.CATALOGO
    assert "enviar" not in " ".join(tools.CATALOGO)
    descripcion = tools.CATALOGO["redactar_borrador"]["description"].lower()
    assert "nunca envía" in descripcion


def test_los_esquemas_respetan_las_tools_habilitadas():
    config = validar(datos_config(tools_habilitadas=["ver_agenda", "recordar"]))
    nombres = [e["name"] for e in tools.esquemas(config)]
    assert nombres == ["ver_agenda", "recordar"]


def test_cada_esquema_tiene_la_forma_que_pide_la_api(config):
    for esquema in tools.esquemas(config):
        assert set(esquema) == {"name", "description", "input_schema"}
        assert esquema["input_schema"]["type"] == "object"
        assert "properties" in esquema["input_schema"]
        assert len(esquema["description"]) > 60, esquema["name"]


def test_cada_propiedad_esta_descrita(config):
    """Un parámetro sin descripción es un parámetro que el modelo va a rellenar mal."""
    for esquema in tools.esquemas(config):
        for nombre, prop in esquema["input_schema"]["properties"].items():
            assert prop.get("description"), f"{esquema['name']}.{nombre} sin descripción"


# -- fechas ----------------------------------------------------------------


def test_una_fecha_sin_zona_sale_con_la_zona_del_ejecutivo():
    momento = tools.parsear_momento("2026-08-20T10:00", ZONA, campo="inicio")
    assert momento.tzinfo is not None
    assert momento.utcoffset() == datetime(2026, 8, 20, 10, tzinfo=ZONA).utcoffset()


def test_una_fecha_con_offset_se_convierte_a_la_zona_del_ejecutivo():
    momento = tools.parsear_momento("2026-08-20T18:00+00:00", ZONA, campo="inicio")
    assert momento.tzinfo is not None
    assert momento.hour == 14  # America/Santiago en agosto: UTC-4


def test_la_z_de_utc_se_entiende():
    momento = tools.parsear_momento("2026-08-20T18:00Z", ZONA, campo="inicio")
    assert momento.hour == 14


@pytest.mark.parametrize("basura", ["", "   ", "mañana", "20/08/2026", None, 42])
def test_una_fecha_invalida_da_un_error_que_el_modelo_puede_leer(basura):
    with pytest.raises(ErrorDeHerramienta) as info:
        tools.parsear_momento(basura, ZONA, campo="inicio")
    assert "inicio" in str(info.value)


def test_solapan_no_cuenta_el_borde():
    a_ini, a_fin = en(9), en(10)
    assert tools.solapan(a_ini, a_fin, en(9, 30), en(10, 30))
    assert not tools.solapan(a_ini, a_fin, en(10), en(11))
    assert not tools.solapan(a_ini, a_fin, en(8), en(9))


# -- fuente mock -----------------------------------------------------------


def test_el_mock_se_ancla_a_hoy_y_no_a_fechas_escritas_a_mano(fuente):
    """Un mock con fechas fijas caduca; este se genera relativo al día que le dan."""
    assert all(e.inicio.date() >= fuente.hoy for e in fuente.eventos)
    assert any(e.inicio.date() == fuente.hoy for e in fuente.eventos)


def test_los_datos_mock_estan_marcados_como_mock(fuente):
    assert all("[MOCK]" in e.titulo for e in fuente.eventos)
    assert all("[MOCK]" in c.asunto for c in fuente.correos)
    assert all("ejemplo.invalid" in c.remitente for c in fuente.correos)


# -- despacho --------------------------------------------------------------


def _correr(nombre, entrada, config, memoria, fuente):
    return tools.ejecutar(nombre, entrada, config=config, memoria=memoria, fuente=fuente)


def test_ver_agenda_con_fecha_pelada_trae_el_dia_completo(config, memoria, fuente):
    salida = _correr(
        "ver_agenda", {"fecha_inicio": f"{fuente.hoy}"}, config, memoria, fuente
    )
    assert "Comité de gerencia" in salida
    assert "Revisión de presupuesto" in salida
    assert "Directorio mensual" not in salida  # es de mañana


def test_ver_agenda_con_rango_explicito(config, memoria, fuente):
    manana = fuente.hoy + timedelta(days=1)
    salida = _correr(
        "ver_agenda",
        {"fecha_inicio": f"{manana}", "fecha_fin": f"{manana + timedelta(days=1)}"},
        config,
        memoria,
        fuente,
    )
    assert "Directorio mensual" in salida
    assert "Comité de gerencia" not in salida


def test_ver_agenda_vacia_lo_dice(config, memoria, fuente):
    lejos = fuente.hoy + timedelta(days=400)
    salida = _correr("ver_agenda", {"fecha_inicio": f"{lejos}"}, config, memoria, fuente)
    assert "Sin eventos" in salida


def test_ver_agenda_con_rango_invertido_se_rechaza(config, memoria, fuente):
    with pytest.raises(ErrorDeHerramienta):
        _correr(
            "ver_agenda",
            {"fecha_inicio": "2026-08-20", "fecha_fin": "2026-08-19"},
            config,
            memoria,
            fuente,
        )


def test_crear_evento_lo_agrega_a_la_fuente(config, memoria, fuente):
    antes = len(fuente.eventos)
    salida = _correr(
        "crear_evento",
        {"titulo": "Reunión nueva", "inicio": "2026-08-25T10:00", "fin": "2026-08-25T11:00"},
        config,
        memoria,
        fuente,
    )
    assert "Evento creado" in salida
    assert len(fuente.eventos) == antes + 1


def test_crear_evento_con_fin_antes_del_inicio_se_rechaza(config, memoria, fuente):
    with pytest.raises(ErrorDeHerramienta) as info:
        _correr(
            "crear_evento",
            {"titulo": "X", "inicio": "2026-08-25T11:00", "fin": "2026-08-25T10:00"},
            config,
            memoria,
            fuente,
        )
    assert "posterior" in str(info.value)


def test_crear_evento_sin_titulo_se_rechaza(config, memoria, fuente):
    with pytest.raises(ErrorDeHerramienta) as info:
        _correr(
            "crear_evento",
            {"inicio": "2026-08-25T10:00", "fin": "2026-08-25T11:00"},
            config,
            memoria,
            fuente,
        )
    assert "titulo" in str(info.value)


def test_invitados_tiene_que_ser_una_lista_de_textos(config, memoria, fuente):
    with pytest.raises(ErrorDeHerramienta):
        _correr(
            "crear_evento",
            {
                "titulo": "X",
                "inicio": "2026-08-25T10:00",
                "fin": "2026-08-25T11:00",
                "invitados": "alguien@ejemplo.invalid",
            },
            config,
            memoria,
            fuente,
        )


def test_buscar_correos_filtra(config, memoria, fuente):
    salida = _correr("buscar_correos", {"query": "presupuesto"}, config, memoria, fuente)
    assert "Sin correos" in salida
    salida = _correr("buscar_correos", {"query": "cierre"}, config, memoria, fuente)
    assert "Cierre de mes" in salida


def test_buscar_correos_acota_el_maximo(config, memoria, fuente):
    salida = _correr(
        "buscar_correos", {"query": "mock", "max_resultados": 1}, config, memoria, fuente
    )
    assert len(salida.strip().split("\n")) == 2  # una línea + el extracto


def test_redactar_borrador_no_envia_y_lo_dice(config, memoria, fuente):
    salida = _correr(
        "redactar_borrador",
        {"destinatario": "x@ejemplo.invalid", "asunto": "Hola", "cuerpo": "Texto."},
        config,
        memoria,
        fuente,
    )
    assert "No se envió" in salida
    assert len(fuente.borradores) == 1


def test_recordar_persiste_en_memoria(config, memoria, fuente):
    _correr(
        "recordar",
        {"hecho": "prefiere reuniones de 25 minutos", "categoria": "agenda"},
        config,
        memoria,
        fuente,
    )
    hechos = memoria.hechos()
    assert len(hechos) == 1
    assert hechos[0].categoria == "agenda"


def test_recordar_sin_categoria_cae_en_general(config, memoria, fuente):
    _correr("recordar", {"hecho": "usa dos monitores"}, config, memoria, fuente)
    assert memoria.hechos()[0].categoria == "general"


def test_una_tool_deshabilitada_no_se_puede_llamar(memoria, fuente):
    config = validar(datos_config(tools_habilitadas=["ver_agenda"]))
    with pytest.raises(ErrorDeHerramienta) as info:
        _correr("recordar", {"hecho": "x"}, config, memoria, fuente)
    assert "no está habilitada" in str(info.value)


def test_una_tool_inventada_no_se_puede_llamar(config, memoria, fuente):
    with pytest.raises(ErrorDeHerramienta) as info:
        _correr("enviar_correo", {"a": "x"}, config, memoria, fuente)
    assert "no existe" in str(info.value)


# -- evento propuesto (lo que usa el core antes de confirmar) --------------


def test_evento_propuesto_no_crea_nada(config, fuente):
    antes = len(fuente.eventos)
    propuesto = tools.evento_propuesto(
        {"titulo": "Café", "inicio": "2026-08-25T10:00", "fin": "2026-08-25T10:30"},
        config.zona(),
    )
    assert isinstance(propuesto, Evento)
    assert propuesto.id == ""
    assert len(fuente.eventos) == antes


def test_una_linea_de_evento_se_lee_de_un_vistazo():
    linea = Evento("e", "Directorio", en(11), en(13), ("a@b.invalid",)).como_linea()
    assert "11:00–13:00" in linea
    assert "Directorio" in linea
    assert "a@b.invalid" in linea
