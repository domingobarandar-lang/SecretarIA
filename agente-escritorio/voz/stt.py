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
from pathlib import Path

from voz import ErrorDeVoz

log = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent.parent
CARPETA_MODELOS = RAIZ / "models"

#: Whisper habla ISO-639-1 de dos letras; la config trae etiquetas tipo `es-CL`.
def _idioma_corto(etiqueta: str) -> str:
    return (etiqueta or "es").split("-")[0].lower()


def _origen_modelo(tamano: str) -> str:
    """Qué le pasamos a `WhisperModel`: una carpeta local si ya está en
    `models/whisper-<tamano>/`, o el nombre corto de siempre.

    `faster-whisper` no toca la red si lo que recibe es un directorio que ya
    existe — resuelve el modelo de ahí directo. Si no existe, le pasamos el
    nombre y es él quien lo baja de HuggingFace y lo cachea, como siempre.
    Esto es lo que permite instalar en una caja sin internet (por pendrive,
    por ejemplo): basta con dejar la carpeta ya armada en `models/` antes de
    la primera corrida. Se arma con `scripts/descargar_whisper.sh`.
    """
    carpeta = CARPETA_MODELOS / f"whisper-{tamano}"
    return str(carpeta) if carpeta.is_dir() else tamano


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

        # Si no está pre-cargado en models/, la primera carga baja el modelo de
        # HuggingFace (~500 MB para `small`) y lo deja cacheado. Se avisa porque
        # si no, el primer arranque parece colgado varios minutos y la reacción
        # natural es matarlo.
        origen = _origen_modelo(self._tamano)
        if origen == self._tamano:
            log.info("cargando Whisper «%s» (la primera vez lo descarga)", self._tamano)
        else:
            log.info("cargando Whisper «%s» desde %s (sin red)", self._tamano, origen)
        self._modelo = WhisperModel(origen, device="cpu", compute_type="int8")
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
