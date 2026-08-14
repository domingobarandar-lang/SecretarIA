"""La memoria es lo que hace que el asistente no vuelva a preguntar lo mismo.
La tabla `acciones` además es evidencia: se prueba su forma, no solo que escriba.
"""

import sqlite3

import pytest

from agent.memory import Memoria, formatear_hechos


@pytest.fixture()
def memoria():
    with Memoria(":memory:", session_id="sesion-1") as m:
        yield m


# -- esquema ---------------------------------------------------------------


def columnas(ruta, tabla) -> dict[str, str]:
    con = sqlite3.connect(ruta)
    try:
        return {f[1]: f[2] for f in con.execute(f"PRAGMA table_info({tabla})")}
    finally:
        con.close()


def test_crea_las_tres_tablas_con_las_columnas_exigidas(tmp_path):
    ruta = tmp_path / "memoria.db"
    Memoria(ruta).cerrar()

    assert set(columnas(ruta, "conversaciones")) == {
        "id",
        "session_id",
        "rol",
        "contenido",
        "timestamp",
        "tokens",
    }
    assert set(columnas(ruta, "hechos")) == {
        "id",
        "categoria",
        "contenido",
        "timestamp",
        "fuente",
    }
    assert set(columnas(ruta, "acciones")) == {
        "id",
        "session_id",
        "tool",
        "input",
        "resultado",
        "timestamp",
        "confirmada",
    }


def test_abrir_dos_veces_la_misma_base_no_rompe(tmp_path):
    ruta = tmp_path / "memoria.db"
    primera = Memoria(ruta, session_id="s")
    primera.guardar_mensaje("usuario", "hola")
    primera.cerrar()

    segunda = Memoria(ruta, session_id="s")
    assert len(segunda.historial()) == 1
    segunda.cerrar()


def test_crea_la_carpeta_de_la_base_si_no_existe(tmp_path):
    ruta = tmp_path / "sub" / "carpeta" / "memoria.db"
    Memoria(ruta).cerrar()
    assert ruta.exists()


def test_ruta_por_variable_de_entorno(tmp_path, monkeypatch):
    ruta = tmp_path / "otra.db"
    monkeypatch.setenv("RUTA_MEMORIA", str(ruta))
    with Memoria() as m:
        assert m.ruta == str(ruta)
    assert ruta.exists()


def test_session_id_se_genera_solo_y_es_distinto_por_instancia():
    a, b = Memoria(":memory:"), Memoria(":memory:")
    assert a.session_id and b.session_id
    assert a.session_id != b.session_id
    a.cerrar()
    b.cerrar()


# -- conversaciones --------------------------------------------------------


def test_mensajes_vuelven_en_orden_cronologico(memoria):
    memoria.guardar_mensaje("usuario", "¿qué tengo mañana?")
    memoria.guardar_mensaje("asistente", "Tres reuniones.")
    memoria.guardar_mensaje("herramienta", '{"eventos": []}')

    contenidos = [m.contenido for m in memoria.historial()]
    assert contenidos == ["¿qué tengo mañana?", "Tres reuniones.", '{"eventos": []}']
    assert [m.rol for m in memoria.historial()] == ["usuario", "asistente", "herramienta"]


def test_historial_con_limite_devuelve_los_ultimos_en_orden(memoria):
    for i in range(10):
        memoria.guardar_mensaje("usuario", f"mensaje {i}")

    ultimos = memoria.historial(limite=3)
    assert [m.contenido for m in ultimos] == ["mensaje 7", "mensaje 8", "mensaje 9"]


def test_historial_vacio_es_lista_vacia(memoria):
    assert memoria.historial() == []


def test_historial_no_mezcla_sesiones(memoria):
    memoria.guardar_mensaje("usuario", "de la primera")
    anterior = memoria.session_id
    nueva = memoria.nueva_sesion()

    assert nueva != anterior
    assert memoria.historial() == []

    memoria.guardar_mensaje("usuario", "de la segunda")
    assert [m.contenido for m in memoria.historial()] == ["de la segunda"]
    assert [m.contenido for m in memoria.historial(session_id=anterior)] == ["de la primera"]


def test_rol_invalido_se_rechaza_en_python(memoria):
    with pytest.raises(ValueError) as info:
        memoria.guardar_mensaje("robot", "hola")
    assert "rol inválido" in str(info.value)


def test_rol_invalido_tambien_lo_rechaza_la_base(memoria):
    """Defensa en profundidad: aunque alguien esquive el método, el CHECK aguanta."""
    with pytest.raises(sqlite3.IntegrityError):
        memoria._con.execute(
            "INSERT INTO conversaciones (session_id, rol, contenido, timestamp)"
            " VALUES ('s', 'robot', 'hola', '2026-08-13T00:00:00+00:00')"
        )


def test_tokens_se_suman_por_sesion(memoria):
    memoria.guardar_mensaje("usuario", "hola", tokens=12)
    memoria.guardar_mensaje("asistente", "buenas", tokens=30)
    assert memoria.tokens_sesion() == 42

    memoria.nueva_sesion()
    assert memoria.tokens_sesion() == 0


def test_limite_negativo_se_rechaza(memoria):
    with pytest.raises(ValueError):
        memoria.historial(limite=-1)


# -- hechos ----------------------------------------------------------------


def test_guardar_y_leer_hechos(memoria):
    memoria.guardar_hecho("prefiere reuniones después de las 10", "agenda")
    memoria.guardar_hecho("no agenda los viernes en la tarde", "agenda")

    hechos = memoria.hechos()
    assert len(hechos) == 2
    assert hechos[0].contenido == "no agenda los viernes en la tarde"  # más reciente primero
    assert hechos[0].categoria == "agenda"
    assert hechos[0].fuente == "recordar"


def test_hecho_repetido_no_se_duplica_y_refresca_la_fuente(memoria):
    primero = memoria.guardar_hecho("odia las reuniones de 30 minutos", "agenda")
    segundo = memoria.guardar_hecho(
        "odia las reuniones de 30 minutos", "agenda", fuente="inferido"
    )

    assert primero == segundo
    assert len(memoria.hechos()) == 1
    assert memoria.hechos()[0].fuente == "inferido"


def test_el_mismo_texto_en_otra_categoria_es_otro_hecho(memoria):
    memoria.guardar_hecho("prefiere la mañana", "agenda")
    memoria.guardar_hecho("prefiere la mañana", "correo")
    assert len(memoria.hechos()) == 2


def test_hechos_se_filtran_por_categoria(memoria):
    memoria.guardar_hecho("prefiere la mañana", "agenda")
    memoria.guardar_hecho("firma como «JB»", "correo")

    assert [h.contenido for h in memoria.hechos(categoria="correo")] == ["firma como «JB»"]


def test_hecho_vacio_se_rechaza(memoria):
    with pytest.raises(ValueError):
        memoria.guardar_hecho("   ", "agenda")


def test_hechos_sobreviven_al_cierre(tmp_path):
    ruta = tmp_path / "memoria.db"
    with Memoria(ruta) as m:
        m.guardar_hecho("no agenda los viernes en la tarde", "agenda")
    with Memoria(ruta) as m:  # otra sesión: los hechos no son de la sesión
        assert len(m.hechos()) == 1


def test_formatear_hechos_para_el_prompt(memoria):
    memoria.guardar_hecho("prefiere reuniones después de las 10", "agenda")
    texto = formatear_hechos(memoria.hechos())
    assert texto == "- [agenda] prefiere reuniones después de las 10"
    assert formatear_hechos([]) == ""


# -- acciones (auditoría) --------------------------------------------------


def test_registrar_accion_guarda_el_input_como_json(memoria):
    memoria.registrar_accion(
        "crear_evento",
        {"titulo": "Directorio", "inicio": "2026-08-20T10:00:00-04:00"},
        "creado: evento#123",
        confirmada=True,
    )

    (accion,) = memoria.acciones()
    assert accion.tool == "crear_evento"
    assert accion.entrada["titulo"] == "Directorio"
    assert accion.resultado == "creado: evento#123"
    assert accion.confirmada is True
    assert accion.session_id == "sesion-1"


def test_accion_sin_confirmar_queda_marcada_como_tal(memoria):
    memoria.registrar_accion("ver_agenda", {"fecha_inicio": "2026-08-20"}, "3 eventos")
    assert memoria.acciones()[0].confirmada is False


def test_accion_sin_input_no_rompe(memoria):
    memoria.registrar_accion("ver_agenda", None, "ok")
    assert memoria.acciones()[0].entrada == {}


def test_acciones_mas_recientes_primero(memoria):
    memoria.registrar_accion("ver_agenda", {}, "ok")
    memoria.registrar_accion("recordar", {}, "ok")
    assert [a.tool for a in memoria.acciones()] == ["recordar", "ver_agenda"]


def test_las_acciones_no_se_pierden_al_cambiar_de_sesion(memoria):
    """La auditoría es del dispositivo, no de la conversación."""
    memoria.registrar_accion("ver_agenda", {}, "ok")
    de_antes = memoria.session_id
    memoria.nueva_sesion()
    memoria.registrar_accion("recordar", {}, "ok")

    assert len(memoria.acciones()) == 2
    assert len(memoria.acciones(solo_sesion=True)) == 1
    assert len(memoria.acciones(session_id=de_antes)) == 1


def test_registra_el_error_de_una_tool_como_resultado(memoria):
    """Una tool que falla igual deja rastro: sin esto la auditoría miente."""
    memoria.registrar_accion("buscar_correos", {"query": "x"}, "ERROR: timeout")
    assert memoria.acciones()[0].resultado.startswith("ERROR")


# -- observabilidad --------------------------------------------------------


def test_resumen_de_sesion(memoria):
    memoria.guardar_mensaje("usuario", "hola", tokens=5)
    memoria.guardar_mensaje("asistente", "buenas", tokens=7)
    memoria.registrar_accion("ver_agenda", {}, "ok")

    assert memoria.resumen_sesion() == {
        "session_id": "sesion-1",
        "mensajes": 2,
        "acciones": 1,
        "tokens": 12,
    }


def test_el_context_manager_cierra_la_conexion():
    with Memoria(":memory:") as m:
        m.guardar_mensaje("usuario", "hola")
    with pytest.raises(sqlite3.ProgrammingError):
        m.historial()
