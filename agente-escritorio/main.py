"""REPL de terminal. Entra texto, sale texto.

Este archivo es la única parte del proyecto que habla con una persona, y por eso
es la única que hace `input()` y `print()`. El core no imprime ni pregunta: recibe
una función de confirmación y la llama. Esa separación es lo que permite probar
la compuerta humana sin un humano.

Micrófono, parlante, pantalla y botón de mute son otro módulo y otra sesión.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from agent.config import ErrorDeConfig, cargar_config
from agent.core import Agente, ErrorDelAgente, SolicitudConfirmacion
from agent.memory import Memoria

SALIDAS = {"salir", "chao", "exit", "quit", ":q"}
AFIRMATIVAS = {"si", "sí", "s", "ya", "dale", "ok", "confirmo", "yes", "y"}


def _cargar_env() -> None:
    """Lee `.env` si existe. Sin `python-dotenv` instalado, no es un error fatal."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    ruta = Path(__file__).resolve().parent / ".env"
    if ruta.is_file():
        load_dotenv(ruta)


def configurar_log() -> None:
    nivel = os.environ.get("LOG_NIVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, nivel, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        stream=sys.stderr,  # stderr para no ensuciar la conversación en stdout
    )


def preguntar_por_consola(solicitud: SolicitudConfirmacion) -> bool:
    """La compuerta humana, en su versión de terminal.

    Cualquier cosa que no sea un sí explícito es un no — incluido Ctrl+C y el
    final del stdin. Es la misma regla que el default del core, escrita donde
    la persona la vive.
    """
    print()
    print(solicitud.como_texto())
    try:
        respuesta = input("¿Confirma? (sí/no) > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return respuesta in AFIRMATIVAS


def main(argv: list[str] | None = None) -> int:
    argumentos = list(argv if argv is not None else sys.argv[1:])
    ruta_config = argumentos[0] if argumentos else None

    _cargar_env()
    configurar_log()

    try:
        config = cargar_config(ruta_config)
    except ErrorDeConfig as problema:
        print(problema, file=sys.stderr)
        return 2

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Falta ANTHROPIC_API_KEY. Copia .env.example a .env y complétala.",
            file=sys.stderr,
        )
        return 2

    memoria = Memoria()
    agente = Agente(config, memoria, confirmar=preguntar_por_consola)

    print(f"{config.agente.nombre} — {config.device_id} · sesión {memoria.session_id[:8]}")
    print(f"Modelo: {agente.modelo}. Escribe «salir» para terminar.")
    print()

    try:
        while True:
            try:
                entrada = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not entrada:
                continue
            if entrada.lower() in SALIDAS:
                break

            try:
                turno = agente.responder(entrada)
            except ErrorDelAgente as problema:
                print(f"[no pude responder] {problema}", file=sys.stderr)
                continue

            print()
            print(turno.texto)
            print()
    finally:
        resumen = memoria.resumen_sesion()
        memoria.cerrar()
        print(
            f"Sesión cerrada · {resumen['mensajes']} mensajes · "
            f"{resumen['acciones']} acciones · {agente.tokens_sesion} tokens",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
