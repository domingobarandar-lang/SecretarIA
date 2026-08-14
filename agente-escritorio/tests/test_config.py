"""La configuración es el contrato con el cliente: si está mal, no se arranca."""

import json

import pytest

from agent.config import (
    RUTA_EJEMPLO,
    TOOLS_DISPONIBLES,
    Config,
    ErrorDeConfig,
    cargar_config,
    validar,
)


def base() -> dict:
    """Config mínima válida: solo lo obligatorio."""
    return {
        "device_id": "box-001",
        "ejecutivo": {
            "nombre": "Nombre Apellido",
            "email": "nombre@empresa.cl",
            "zona_horaria": "America/Santiago",
        },
        "agente": {"nombre": "Vera"},
        "tools_habilitadas": list(TOOLS_DISPONIBLES),
    }


def escribir(tmp_path, datos, nombre="device.config.json"):
    ruta = tmp_path / nombre
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return ruta


def errores_de(datos) -> list[str]:
    with pytest.raises(ErrorDeConfig) as info:
        validar(datos)
    return info.value.errores


# -- camino feliz ----------------------------------------------------------


def test_config_minima_valida_carga():
    config = validar(base())
    assert isinstance(config, Config)
    assert config.device_id == "box-001"
    assert config.ejecutivo.nombre == "Nombre Apellido"
    assert config.agente.nombre == "Vera"


def test_el_ejemplo_versionado_es_valido():
    """El .example es la plantilla que copia el cliente: tiene que pasar."""
    datos = json.loads(RUTA_EJEMPLO.read_text(encoding="utf-8"))
    config = validar(datos, RUTA_EJEMPLO)
    assert config.device_id
    assert set(config.tools_habilitadas) == set(TOOLS_DISPONIBLES)


def test_defaults_se_aplican_cuando_faltan_los_bloques_opcionales():
    config = validar(base())
    assert config.agente.idioma == "es-CL"
    assert config.agente.tratamiento == "usted"
    assert config.agente.largo_respuesta == "breve"
    assert config.reglas.confirmar_antes_de_acciones_irreversibles is True
    assert config.reglas.no_agendar_antes_de == "09:00"
    assert config.reglas.no_agendar_despues_de == "19:00"
    assert config.reglas.dias_bloqueados == ()
    assert config.limites.max_iteraciones_loop == 8
    assert config.limites.max_mensajes_historial == 12
    assert config.limites.timeout_llamada_modelo_seg == 20


def test_valores_explicitos_ganan_al_default():
    datos = base()
    datos["limites"] = {"max_iteraciones_loop": 3}
    config = validar(datos)
    assert config.limites.max_iteraciones_loop == 3
    assert config.limites.max_mensajes_historial == 12  # el resto sigue en default


def test_tratamiento_con_tilde_se_normaliza():
    datos = base()
    datos["agente"]["tratamiento"] = "tú"
    assert validar(datos).agente.tratamiento == "tu"


def test_helpers_de_tools():
    datos = base()
    datos["tools_habilitadas"] = ["ver_agenda", "recordar"]
    config = validar(datos)
    assert config.tool_habilitada("ver_agenda")
    assert not config.tool_habilitada("buscar_correos")
    assert config.es_irreversible("crear_evento")
    assert not config.es_irreversible("ver_agenda")


def test_zona_horaria_resuelve():
    config = validar(base())
    assert config.zona().key == "America/Santiago"


# -- campos obligatorios ---------------------------------------------------


def test_falta_campo_obligatorio_de_primer_nivel():
    datos = base()
    del datos["device_id"]
    assert errores_de(datos) == ["falta el campo obligatorio «device_id»"]


def test_falta_campo_obligatorio_anidado():
    datos = base()
    del datos["ejecutivo"]["email"]
    assert errores_de(datos) == ["falta el campo obligatorio «ejecutivo.email»"]


def test_falta_bloque_obligatorio_completo():
    datos = base()
    del datos["agente"]
    assert errores_de(datos) == ["falta el campo obligatorio «agente»"]


def test_se_reportan_todos_los_errores_juntos_no_solo_el_primero():
    datos = base()
    del datos["device_id"]
    del datos["ejecutivo"]["nombre"]
    datos["limites"] = {"max_iteraciones_loop": 0}
    errores = errores_de(datos)
    assert len(errores) == 3
    assert any("device_id" in e for e in errores)
    assert any("ejecutivo.nombre" in e for e in errores)
    assert any("limites.max_iteraciones_loop" in e for e in errores)


# -- typos y llaves desconocidas ------------------------------------------


def test_llave_desconocida_es_error_y_sugiere_la_correcta():
    datos = base()
    datos["limits"] = {"max_iteraciones_loop": 8}
    (error,) = errores_de(datos)
    assert "«limits»" in error
    assert "¿quisiste decir «limites»?" in error


def test_llave_desconocida_anidada():
    datos = base()
    datos["ejecutivo"]["telefono"] = "+56900000000"
    (error,) = errores_de(datos)
    assert "«ejecutivo.telefono»" in error


# -- tipos -----------------------------------------------------------------


def test_tipo_equivocado_en_texto():
    datos = base()
    datos["ejecutivo"]["nombre"] = 42
    (error,) = errores_de(datos)
    assert error == "«ejecutivo.nombre» debe ser un texto, llegó un número"


def test_texto_vacio_no_pasa():
    datos = base()
    datos["agente"]["nombre"] = "   "
    (error,) = errores_de(datos)
    assert "«agente.nombre» no puede estar vacío" == error


def test_booleano_no_acepta_texto():
    datos = base()
    datos["reglas"] = {"confirmar_antes_de_acciones_irreversibles": "true"}
    (error,) = errores_de(datos)
    assert "debe ser true o false" in error


def test_entero_no_acepta_booleano():
    datos = base()
    datos["limites"] = {"max_iteraciones_loop": True}
    (error,) = errores_de(datos)
    assert "debe ser un número entero" in error


def test_bloque_que_llega_como_lista():
    datos = base()
    datos["agente"] = ["Vera"]
    (error,) = errores_de(datos)
    assert error == "«agente» debe ser un objeto, llegó una lista"


def test_json_de_primer_nivel_debe_ser_objeto():
    with pytest.raises(ErrorDeConfig) as info:
        validar([1, 2, 3])
    assert "debe contener un objeto JSON" in info.value.errores[0]


# -- rangos y formatos -----------------------------------------------------


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("max_iteraciones_loop", 0),
        ("max_iteraciones_loop", 51),
        ("max_mensajes_historial", 1),
        ("timeout_llamada_modelo_seg", 4),
        ("timeout_llamada_modelo_seg", 301),
    ],
)
def test_limites_fuera_de_rango(campo, valor):
    datos = base()
    datos["limites"] = {campo: valor}
    (error,) = errores_de(datos)
    assert campo in error and "debe estar entre" in error


def test_email_invalido():
    datos = base()
    datos["ejecutivo"]["email"] = "no-es-un-correo"
    (error,) = errores_de(datos)
    assert "no parece un correo" in error


def test_zona_horaria_inexistente():
    datos = base()
    datos["ejecutivo"]["zona_horaria"] = "America/Santiaguito"
    (error,) = errores_de(datos)
    assert "zona horaria" in error


def test_device_id_con_espacios():
    datos = base()
    datos["device_id"] = "Box 001"
    (error,) = errores_de(datos)
    assert "formato inválido" in error


def test_hora_invalida():
    datos = base()
    datos["reglas"] = {"no_agendar_antes_de": "25:00"}
    (error,) = errores_de(datos)
    assert "no es una hora HH:MM" in error


def test_horas_nulas_significan_sin_restriccion():
    datos = base()
    datos["reglas"] = {"no_agendar_antes_de": None, "no_agendar_despues_de": None}
    config = validar(datos)
    assert config.reglas.no_agendar_antes_de is None


def test_dia_bloqueado_acepta_dia_de_semana_y_fecha():
    datos = base()
    datos["reglas"] = {"dias_bloqueados": ["viernes", "miércoles", "2026-09-18"]}
    config = validar(datos)
    assert config.reglas.dias_bloqueados == ("viernes", "miércoles", "2026-09-18")


def test_dia_bloqueado_invalido():
    datos = base()
    datos["reglas"] = {"dias_bloqueados": ["fridei"]}
    (error,) = errores_de(datos)
    assert "no es un día de la semana" in error


def test_idioma_con_formato_raro():
    datos = base()
    datos["agente"]["idioma"] = "castellano"
    (error,) = errores_de(datos)
    assert "«agente.idioma»" in error


def test_largo_respuesta_fuera_de_las_opciones():
    datos = base()
    datos["agente"]["largo_respuesta"] = "larguísimo"
    (error,) = errores_de(datos)
    assert "debe ser uno de: breve, normal, detallado" in error


# -- tools -----------------------------------------------------------------


def test_tool_inexistente_no_se_puede_habilitar():
    datos = base()
    datos["tools_habilitadas"] = ["ver_agenda", "enviar_correo"]
    (error,) = errores_de(datos)
    assert "«enviar_correo» no existe" in error


def test_tool_con_typo_sugiere_la_correcta():
    datos = base()
    datos["tools_habilitadas"] = ["ver_agendas"]
    (error,) = errores_de(datos)
    assert "¿quisiste decir «ver_agenda»?" in error


def test_lista_de_tools_vacia():
    datos = base()
    datos["tools_habilitadas"] = []
    (error,) = errores_de(datos)
    assert "no puede ser una lista vacía" in error


def test_tool_repetida():
    datos = base()
    datos["tools_habilitadas"] = ["ver_agenda", "ver_agenda"]
    (error,) = errores_de(datos)
    assert "repetido" in error


# -- reglas cruzadas -------------------------------------------------------


def test_ventana_de_agenda_invertida():
    datos = base()
    datos["reglas"] = {"no_agendar_antes_de": "20:00", "no_agendar_despues_de": "19:00"}
    (error,) = errores_de(datos)
    assert "debe ser anterior a" in error


def test_crear_evento_exige_ver_agenda():
    datos = base()
    datos["tools_habilitadas"] = ["crear_evento", "recordar"]
    (error,) = errores_de(datos)
    assert "crear_evento" in error and "ver_agenda" in error


def test_la_confirmacion_humana_no_se_puede_apagar_por_config():
    datos = base()
    datos["reglas"] = {"confirmar_antes_de_acciones_irreversibles": False}
    (error,) = errores_de(datos)
    assert "no se puede desactivar por configuración" in error


# -- lectura desde disco ---------------------------------------------------


def test_carga_desde_archivo(tmp_path):
    ruta = escribir(tmp_path, base())
    config = cargar_config(ruta)
    assert config.ruta == ruta
    assert config.device_id == "box-001"


def test_archivo_inexistente_dice_que_copie_el_ejemplo(tmp_path):
    with pytest.raises(ErrorDeConfig) as info:
        cargar_config(tmp_path / "no-existe.json")
    assert "no existe" in info.value.errores[0]
    assert "device.config.example.json" in info.value.errores[0]


def test_json_malformado_apunta_a_la_linea(tmp_path):
    ruta = tmp_path / "device.config.json"
    ruta.write_text('{"device_id": "box-001",,}', encoding="utf-8")
    with pytest.raises(ErrorDeConfig) as info:
        cargar_config(ruta)
    assert "JSON malformado" in info.value.errores[0]


def test_variable_de_entorno_manda_cuando_no_hay_argumento(tmp_path, monkeypatch):
    ruta = escribir(tmp_path, base(), "otro-cliente.json")
    monkeypatch.setenv("DEVICE_CONFIG", str(ruta))
    assert cargar_config().ruta == ruta


def test_el_argumento_le_gana_a_la_variable_de_entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVICE_CONFIG", str(tmp_path / "no-existe.json"))
    ruta = escribir(tmp_path, base())
    assert cargar_config(ruta).ruta == ruta


# -- el objeto Config ------------------------------------------------------


def test_config_es_de_solo_lectura():
    config = validar(base())
    with pytest.raises(TypeError):
        config.device_id = "otro"
    with pytest.raises(TypeError):
        config.agente.nombre = "otro"


def test_atributo_inexistente_lista_los_disponibles():
    config = validar(base())
    with pytest.raises(AttributeError) as info:
        _ = config.no_existe
    assert "device_id" in str(info.value)


def test_las_listas_salen_como_tuplas_inmutables():
    config = validar(base())
    assert isinstance(config.tools_habilitadas, tuple)


def test_como_dict_devuelve_copia_mutable():
    config = validar(base())
    copia = config.como_dict()
    copia["device_id"] = "cambiado"
    assert config.device_id == "box-001"


def test_el_mensaje_de_error_lista_todos_los_problemas():
    datos = base()
    del datos["device_id"]
    del datos["agente"]
    with pytest.raises(ErrorDeConfig) as info:
        validar(datos)
    texto = str(info.value)
    assert "2 problemas" in texto
    assert "device_id" in texto and "agente" in texto
