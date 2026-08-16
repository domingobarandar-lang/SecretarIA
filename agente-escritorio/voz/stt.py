"""Whisper local: audio adentro, texto afuera. El audio del ejecutivo no sale de acá.

Esto no es una preferencia técnica. La promesa del producto es que solo el
razonamiento del modelo sale por API; mandar el audio crudo a un servicio de
transcripción rompería esa frase por completo, y de la peor manera — el
micrófono está siempre encendido, así que lo que saldría no serían las órdenes,
sería la oficina entera.

`faster-whisper` corre el modelo en CPU con CTranslate2 cuantizado a int8. En la
placa, `small` es el techo razonable y `tiny` el piso donde los nombres propios
empiezan a salir mal. Es un campo de config, no una constante: la caja de demo y
la de producción no tienen por qué usar el mismo.
"""

from __future__ import annotations

import logging

from voz import ErrorDeVoz

log = logging.getLogger(__name__)

#: Whisper habla ISO-639-1 de dos letras; la config trae etiquetas tipo `es-CL`.
def _idioma_corto(etiqueta: str) -> str:
    return (etiqueta or "es").split("-")[0].lower()


class Transcriptor:
    """Audio a texto. Carga el modelo la primera vez que se le pide algo."""

    def __init__(self, config) -> None:
        self._tamano = config.voz.modelo_stt
        self._idioma = _idioma_corto(config.agente.idioma)
        self._modelo = None

    def _cargar(self):
        if self._modelo is not None:
            return self._modelo

        try:
            from faster_whisper import WhisperModel
        except ImportError as falta:
            raise ErrorDeVoz(
                "Falta faster-whisper, que es lo que transcribe.\n"
                "  .venv\\Scripts\\python.exe -m pip install faster-whisper"
            ) from falta

        # La primera carga baja el modelo de HuggingFace (~500 MB para `small`) y
        # lo deja cacheado. Se avisa porque si no, el primer arranque parece
        # colgado durante varios minutos y la reacción natural es matarlo.
        log.info("cargando Whisper «%s» (la primera vez lo descarga)", self._tamano)
        self._modelo = WhisperModel(self._tamano, device="cpu", compute_type="int8")
        return self._modelo

    def precargar(self) -> None:
        """Fuerza la carga ahora. La usa el arranque para que la descarga de
        varios cientos de MB ocurra mientras alguien mira la pantalla, y no en
        medio de la primera frase que le digan al agente."""
        self._cargar()

    def transcribir(self, pcm: bytes) -> str:
        """PCM de 16 bits mono a 16 kHz -> texto. Cadena vacía si no entendió nada."""
        if not pcm:
            return ""

        try:
            import numpy as np
        except ImportError as falta:  # pragma: no cover - llega con faster-whisper
            raise ErrorDeVoz("Falta numpy (viene con faster-whisper).") from falta

        # Whisper quiere float32 en [-1, 1]; el micrófono entrega int16. La
        # división es por 32768 y no por 32767: es la escala del formato.
        muestras = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        segmentos, _ = self._cargar().transcribe(
            muestras,
            language=self._idioma,
            beam_size=1,  # en CPU, beam 5 cuesta el triple y aquí no paga
            condition_on_previous_text=False,  # cada turno es independiente
        )
        return " ".join(s.text.strip() for s in segmentos).strip()
