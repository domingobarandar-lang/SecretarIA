"""El script que pide el permiso del ejecutivo, probado sin abrir un navegador.

Se prueban las dos cosas que, si están mal, producen un fallo mudo:

- **`access_type=offline` y `prompt=consent`.** Sin ellos Google devuelve un
  permiso temporal y ningún `refresh_token`. El script termina «bien», sin la
  llave que vino a buscar, y el error aparece recién en la placa.
- **El `state`.** Es lo único que separa una respuesta de Google de alguien
  mandando el navegador a `localhost:8765` con un código propio.
"""

from __future__ import annotations

import importlib.util
import urllib.parse
from pathlib import Path

import pytest

from integrations.google_mcp import PERMISOS

RAIZ = Path(__file__).resolve().parent.parent


def _cargar():
    ruta = RAIZ / "scripts" / "autorizar-google.py"
    spec = importlib.util.spec_from_file_location("autorizar_google", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


autorizar = _cargar()


def _parametros(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_la_url_pide_una_llave_de_largo_plazo():
    p = _parametros(autorizar.url_de_consentimiento("id-1", "http://localhost:8765/", "abc"))
    assert p["access_type"] == "offline"
    assert p["prompt"] == "consent"
    assert p["response_type"] == "code"


def test_se_piden_todos_los_permisos_declarados_en_un_solo_lugar():
    p = _parametros(autorizar.url_de_consentimiento("id-1", "http://localhost:8765/", "abc"))
    assert set(p["scope"].split()) == set(PERMISOS)


def test_el_permiso_de_escribir_en_el_calendario_va_incluido():
    """Es el que la guía de Google no lista y el que `crear_evento` necesita."""
    assert "https://www.googleapis.com/auth/calendar.events" in PERMISOS


def test_el_state_viaja_en_la_url():
    p = _parametros(autorizar.url_de_consentimiento("id-1", "http://localhost:8765/", "el-estado"))
    assert p["state"] == "el-estado"


def test_la_direccion_de_vuelta_es_la_que_se_registra_en_google():
    p = _parametros(autorizar.url_de_consentimiento("id-1", "http://localhost:8765/", "x"))
    assert p["redirect_uri"] == "http://localhost:8765/"


def test_sin_credenciales_del_cliente_no_hace_nada(monkeypatch, capsys):
    monkeypatch.setattr(autorizar, "webbrowser", type("X", (), {"open": staticmethod(lambda _: None)}))
    assert autorizar.main(["--puerto", "8799"]) == 2
    assert "GOOGLE_OAUTH_CLIENT_ID" in capsys.readouterr().err
