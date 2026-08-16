"""Decide cuándo empezó y cuándo terminó de hablar una persona.

Sin esto habría que apretar una tecla para hablar, que es exactamente lo que un
asistente de escritorio no debe pedir. El detector es webrtcvad: no entiende
palabras, solo distingue «esto es voz humana» de «esto es el ventilador». Es
barato — corre en microsegundos por trama — y por eso puede quedar escuchando
todo el día sin calentar la placa, que es lo que Whisper no podría hacer.

El corte del turno es la decisión delicada de todo el ciclo de voz: si es muy
corto, el agente interrumpe a mitad de frase; si es muy largo, parece dormido.
Vive en `voz.silencio_fin_turno_ms` porque el número bueno depende de la persona
y del ruido de su oficina, no de nosotros.
"""

from __future__ import annotations

import collections
import logging
import time

from voz import ErrorDeVoz
from voz.audio import (
    FRECUENCIA_CAPTURA,
    MS_PRE_ROLL,
    MS_TRAMA,
    TRAMAS_PARA_EMPEZAR,
    Audio,
)

log = logging.getLogger(__name__)

#: 0 = deja pasar casi todo, 3 = solo voz muy clara. En 2 el ventilador y el
#: teclado no despiertan al agente, pero una frase dicha bajito sí entra.
AGRESIVIDAD_VAD = 2


def _vad(agresividad: int):
    try:
        import webrtcvad
    except ImportError as falta:
        raise ErrorDeVoz(
            "Falta webrtcvad, que es lo que detecta cuándo alguien está hablando.\n"
            "  .venv\\Scripts\\python.exe -m pip install webrtcvad-wheels"
        ) from falta
    return webrtcvad.Vad(agresividad)


class Escucha:
    """Entrega un turno hablado por vez: bytes de PCM 16 bits mono a 16 kHz."""

    def __init__(
        self,
        audio: Audio,
        config_voz,
        agresividad: int = AGRESIVIDAD_VAD,
        detector=None,
    ) -> None:
        """`detector` se inyecta igual que `cliente` y `fuente` en el core: es lo
        que permite probar el corte del turno —que es la lógica delicada de este
        archivo— con un detector de mentira y sin micrófono. En producción va
        `None` y se usa webrtcvad."""
        self._audio = audio
        self._vad = detector if detector is not None else _vad(agresividad)
        self._tramas_silencio_fin = max(
            1, int(config_voz.silencio_fin_turno_ms) // MS_TRAMA
        )
        self._max_tramas = int(config_voz.max_segundos_escucha) * 1000 // MS_TRAMA

    def turno(self, espera_max_seg: float | None = None) -> bytes | None:
        """Espera a que alguien hable y devuelve lo que dijo.

        `None` significa «pasó `espera_max_seg` y nadie habló», que no es un
        error: es el caso normal de un agente esperando en un escritorio vacío.
        Sin `espera_max_seg` espera para siempre.
        """
        # Guarda los últimos 200 ms de audio *anteriores* al primer sonido de
        # voz. Para cuando el detector se convence de que hay alguien hablando,
        # la primera sílaba ya pasó; sin este colchón, «Vera» se transcribe
        # «era» y el wake word no calza nunca.
        pre_roll = collections.deque(maxlen=max(1, MS_PRE_ROLL // MS_TRAMA))
        capturado: list[bytes] = []
        con_voz = 0
        sin_voz = 0
        hablando = False
        limite = None if espera_max_seg is None else time.monotonic() + espera_max_seg

        for trama in self._audio.tramas():
            # `is_speech` exige tramas de un largo exacto. La última lectura de
            # un flujo que se cierra puede venir corta: se descarta en vez de
            # dejar que webrtcvad levante un error críptico.
            if len(trama) != FRECUENCIA_CAPTURA * MS_TRAMA // 1000 * 2:
                continue

            hay_voz = self._vad.is_speech(trama, FRECUENCIA_CAPTURA)

            if not hablando:
                pre_roll.append(trama)
                con_voz = con_voz + 1 if hay_voz else 0
                if con_voz >= TRAMAS_PARA_EMPEZAR:
                    hablando = True
                    capturado = list(pre_roll)
                    log.debug("turno: empezó")
                elif limite is not None and time.monotonic() > limite:
                    return None
                continue

            capturado.append(trama)
            sin_voz = 0 if hay_voz else sin_voz + 1

            if sin_voz >= self._tramas_silencio_fin:
                log.debug("turno: cerró por silencio (%d tramas)", len(capturado))
                break
            if len(capturado) >= self._max_tramas:
                log.info(
                    "turno: cortado por el tope de %d s; se transcribe lo que hay",
                    self._max_tramas * MS_TRAMA // 1000,
                )
                break

        return b"".join(capturado) if capturado else None
