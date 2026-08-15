"""Piezas compartidas por los tests del loop.

El cliente falso no es una comodidad: es lo que hace que la suite corra con cero
tokens y sin salir a internet. Ningún test construye un cliente real, así que no
existe la forma de que una corrida despistada llame a la API de verdad.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.config import TOOLS_DISPONIBLES, validar
from agent.memory import Memoria
from agent.tools import Evento, FuenteMock

ZONA = ZoneInfo("America/Santiago")

#: Un jueves, para que los tests de días bloqueados tengan de dónde agarrarse.
AHORA = datetime(2026, 8, 20, 9, 0, tzinfo=ZONA)


def datos_config(**cambios: Any) -> dict[str, Any]:
    base = {
        "device_id": "box-test",
        "ejecutivo": {
            "nombre": "Nombre Apellido",
            "cargo": "Gerente General",
            "empresa": "Empresa S.A.",
            "email": "nombre@empresa.cl",
            "zona_horaria": "America/Santiago",
        },
        "agente": {"nombre": "Vera"},
        "tools_habilitadas": list(TOOLS_DISPONIBLES),
    }
    base.update(cambios)
    return base


@pytest.fixture()
def config():
    return validar(datos_config())


@pytest.fixture()
def memoria():
    with Memoria(":memory:", session_id="sesion-test") as m:
        yield m


@pytest.fixture()
def fuente():
    """Agenda mock anclada al día de `AHORA`, para que los rangos sean predecibles."""
    return FuenteMock(zona=ZONA, hoy=AHORA.date())


def en(hora: int, minuto: int = 0, dia: int = 0) -> datetime:
    """Un instante relativo al día de `AHORA`, en la zona del ejecutivo."""
    from datetime import timedelta

    return datetime.combine(
        AHORA.date() + timedelta(days=dia), datetime.min.time(), ZONA
    ).replace(hour=hora, minute=minuto)


def evento(titulo: str, desde: datetime, hasta: datetime, ident: str = "evt-x") -> Evento:
    return Evento(ident, titulo, desde, hasta)


# --------------------------------------------------------------------------
# Cliente falso
# --------------------------------------------------------------------------


def respuesta(
    bloques: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    entrada_tokens: int = 10,
    salida_tokens: int = 5,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=bloques,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=entrada_tokens, output_tokens=salida_tokens),
    )


def texto(contenido: str) -> dict[str, Any]:
    return {"type": "text", "text": contenido}


def uso_de_tool(nombre: str, entrada: dict[str, Any], ident: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": ident, "name": nombre, "input": entrada}


class ClienteFalso:
    """Devuelve respuestas preparadas y guarda cada petición para inspeccionarla."""

    def __init__(self, respuestas: list[SimpleNamespace]) -> None:
        self._respuestas = list(respuestas)
        self.peticiones: list[dict[str, Any]] = []
        self.messages = self  # así `cliente.messages.create(...)` resuelve acá

    def create(self, **peticion: Any) -> SimpleNamespace:
        self.peticiones.append(peticion)
        if not self._respuestas:
            raise AssertionError(
                "el agente pidió más respuestas de las preparadas "
                f"({len(self.peticiones)} llamadas)"
            )
        return self._respuestas.pop(0)


class ClienteQueRevienta:
    def __init__(self, problema: Exception) -> None:
        self._problema = problema
        self.messages = self

    def create(self, **_: Any) -> SimpleNamespace:
        raise self._problema
