"""La voz: micrófono, wake word, transcripción y habla.

La dependencia va en un solo sentido — `voz/` importa de `agent/`, nunca al
revés. El cerebro no sabe que esto existe y la suite del cerebro corre entera sin
micrófono, sin parlante y sin los modelos. Esa asimetría es a propósito: es lo
que deja que `main.py` siga arrancando en una caja muda.

Los tres modelos (Piper, Whisper) se cargan **perezosamente**, al primer uso. Un
`import voz` no debe costar segundos ni reservar 200 MB de RAM: hay tests que
importan este paquete y no van a tocar un modelo nunca.
"""

from __future__ import annotations

__all__ = ["ErrorDeVoz"]


class ErrorDeVoz(Exception):
    """La voz no puede operar: falta un modelo, un dispositivo o un paquete.

    Es la única excepción que este paquete deja salir hacia `main_voz.py`. El
    contrato es el mismo que el de `ErrorDeConfig` y `ErrorDeGoogle`: el mensaje
    se le muestra tal cual a una persona, así que dice qué falta y cómo se
    arregla, no en qué línea se rompió.
    """
