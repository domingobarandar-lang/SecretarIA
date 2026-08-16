"""El REPL: la compuerta humana en su forma de terminal, y el guardia de sintaxis.

`preguntar_por_consola` es el último eslabón de la garantía más importante del
producto. Si acepta un «no» como un sí, todo lo que hace el core da igual.
"""

import ast
from pathlib import Path

import pytest

import main

RAIZ = Path(__file__).resolve().parent.parent


# -- la compuerta humana ---------------------------------------------------


def _solicitud():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from agent.core import SolicitudConfirmacion
    from agent.tools import Evento

    zona = ZoneInfo("America/Santiago")
    inicio = datetime(2026, 8, 20, 10, tzinfo=zona)
    return SolicitudConfirmacion(
        "crear_evento", Evento("e", "Reunión", inicio, inicio.replace(hour=11))
    )


@pytest.mark.parametrize("respuesta", ["sí", "si", "s", "SÍ", " ok ", "dale", "confirmo"])
def test_un_si_explicito_confirma(monkeypatch, capsys, respuesta):
    monkeypatch.setattr("builtins.input", lambda _="": respuesta)
    assert main.preguntar_por_consola(_solicitud()) is True


@pytest.mark.parametrize("respuesta", ["no", "n", "", "   ", "quizás", "más tarde", "sí pero no"])
def test_cualquier_cosa_que_no_sea_un_si_es_un_no(monkeypatch, capsys, respuesta):
    monkeypatch.setattr("builtins.input", lambda _="": respuesta)
    assert main.preguntar_por_consola(_solicitud()) is False


@pytest.mark.parametrize("interrupcion", [EOFError, KeyboardInterrupt])
def test_cortar_la_sesion_no_confirma(monkeypatch, capsys, interrupcion):
    """Ctrl+C y el fin del stdin son un no. El silencio nunca es un sí."""

    def cortar(_=""):
        raise interrupcion()

    monkeypatch.setattr("builtins.input", cortar)
    assert main.preguntar_por_consola(_solicitud()) is False


def test_la_solicitud_se_le_muestra_a_la_persona_antes_de_preguntar(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _="": "no")
    main.preguntar_por_consola(_solicitud())
    visto = capsys.readouterr().out
    assert "Voy a crear este evento" in visto
    assert "Reunión" in visto


# -- arranque --------------------------------------------------------------


def test_sin_proveedor_declarado_no_arranca(monkeypatch, capsys):
    """Sin `LLM_PROVIDER` no se elige uno: se para. Adivinar acá cuesta plata."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "hay-key-igual")
    monkeypatch.setattr(main, "_cargar_env", lambda: None)
    assert main.main([str(RAIZ / "device.config.example.json")]) == 2
    assert "LLM_PROVIDER" in capsys.readouterr().err


@pytest.mark.parametrize(
    "proveedor,variable",
    [("gemini", "GEMINI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")],
)
def test_sin_la_key_del_proveedor_no_arranca(monkeypatch, capsys, proveedor, variable):
    monkeypatch.setenv("LLM_PROVIDER", proveedor)
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(main, "_cargar_env", lambda: None)
    assert main.main([str(RAIZ / "device.config.example.json")]) == 2
    assert variable in capsys.readouterr().err


def test_con_config_invalida_no_arranca(monkeypatch, capsys, tmp_path):
    ruta = tmp_path / "mala.json"
    ruta.write_text('{"device_id": "sin lo demas"}', encoding="utf-8")
    monkeypatch.setattr(main, "_cargar_env", lambda: None)
    assert main.main([str(ruta)]) == 2
    assert "Configuración inválida" in capsys.readouterr().err


# -- de dónde salen los datos ----------------------------------------------


def test_por_defecto_los_datos_son_de_mentira_y_lo_dice(config):
    fuente, etiqueta = main._armar_fuente(config)
    assert fuente is None  # el core arma su FuenteMock
    assert "mock" in etiqueta and "no es su agenda real" in etiqueta


def test_una_fuente_inventada_no_arranca(config, monkeypatch):
    monkeypatch.setenv("FUENTE_DATOS", "supabase")
    with pytest.raises(main.ErrorDeGoogle) as info:
        main._armar_fuente(config)
    assert "mock, google" in str(info.value)


def test_pedir_google_sin_credenciales_falla_al_arrancar(config, monkeypatch):
    """Nunca a mitad de conversación: ahí ya hay alguien esperando una respuesta."""
    monkeypatch.setenv("FUENTE_DATOS", "google")
    with pytest.raises(main.ErrorDeGoogle) as info:
        main._armar_fuente(config)
    assert "GOOGLE_OAUTH" in str(info.value)


# -- guardia de sintaxis ---------------------------------------------------


def test_todo_el_codigo_parsea_como_python_311():
    """La Pi 5 con Bookworm trae 3.11; este entorno corre 3.12.

    Sin esta prueba, sintaxis de 3.12+ (por ejemplo `def f[T]()`, PEP 695) pasa
    la suite entera acá y revienta recién en la placa. `feature_version` no cubre
    todo lo que cambia entre versiones —es un cedazo, no una garantía— pero
    atrapa justo lo que se cuela por escribir en un intérprete más nuevo.
    """
    archivos = [
        *(RAIZ / "agent").glob("*.py"),
        *(RAIZ / "integrations").glob("*.py"),
        *(RAIZ / "scripts").glob("*.py"),
        *(RAIZ / "tests").glob("*.py"),
        *(RAIZ / "voz").glob("*.py"),
        RAIZ / "main.py",
        RAIZ / "main_voz.py",
    ]
    assert len(archivos) >= 8, "el guardia no está mirando los archivos del proyecto"

    for archivo in archivos:
        codigo = archivo.read_text(encoding="utf-8")
        try:
            ast.parse(codigo, filename=str(archivo), feature_version=(3, 11))
        except SyntaxError as problema:
            pytest.fail(f"{archivo.name} usa sintaxis posterior a 3.11: {problema}")
