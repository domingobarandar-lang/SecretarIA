"""Piper: texto adentro, voz afuera. Todo local, nada sale por red.

Piper corre el modelo ONNX en CPU. En la máquina de desarrollo sintetiza a ~0.6×
tiempo real con la voz `high`; en la Pi 5 es bastante más lento y puede que haya
que bajar a `medium`. Por eso la voz es un campo de config y no una constante:
cambiar de modelo es editar `device.config.json`, no tocar este archivo.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from voz import ErrorDeVoz
from voz.audio import Audio

log = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent.parent
CARPETA_MODELOS = RAIZ / "models"

#: Corta el texto en frases para poder sintetizar y hablar en paralelo. Piper ya
#: parte por oraciones internamente, pero necesitamos el corte acá arriba para
#: mandar la primera al parlante mientras la segunda todavía se está generando.
_RE_FRASE = re.compile(r"(?<=[.!?…])\s+")


def _limpiar(texto: str) -> str:
    """Saca del texto lo que se escribe pero no se dice.

    El agente responde en Markdown de vez en cuando — negritas, viñetas, algún
    bloque de código. Piper lee los asteriscos en voz alta, literalmente, y el
    resultado es «asterisco asterisco reunión asterisco asterisco». Se limpia acá
    y no en el prompt porque un prompt se puede desobedecer; esto no.
    """
    texto = re.sub(r"```.*?```", " ", texto, flags=re.DOTALL)  # bloques de código
    texto = re.sub(r"`([^`]*)`", r"\1", texto)
    texto = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", texto)  # **negrita**, *cursiva*
    texto = re.sub(r"^\s*[-*+]\s+", "", texto, flags=re.MULTILINE)  # viñetas
    texto = re.sub(r"^\s*#{1,6}\s+", "", texto, flags=re.MULTILINE)  # títulos
    texto = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", texto)  # [texto](url)
    return re.sub(r"\s+", " ", texto).strip()


class Locutor:
    """Habla. Carga el modelo de Piper la primera vez que se le pide algo."""

    def __init__(self, config_voz, audio: Audio) -> None:
        self._nombre_modelo = config_voz.modelo
        self._audio = audio
        # `velocidad` viene en porcentaje (100 = natural) y Piper quiere un
        # `length_scale`, que es lo inverso: más escala = más lento. La conversión
        # vive acá porque es el único lugar que habla con Piper.
        self._length_scale = 100.0 / float(config_voz.velocidad)
        self._voz = None
        self._syn = None

    @property
    def ruta_modelo(self) -> Path:
        return CARPETA_MODELOS / f"{self._nombre_modelo}.onnx"

    def _cargar(self):
        """Carga diferida. Son ~63 MB y 2.5 s: no se pagan hasta que hagan falta."""
        if self._voz is not None:
            return self._voz

        try:
            from piper import PiperVoice, SynthesisConfig
        except ImportError as falta:
            raise ErrorDeVoz(
                "Falta piper-tts, que es el motor de voz.\n"
                "  .venv\\Scripts\\python.exe -m pip install piper-tts"
            ) from falta

        # Se comprueba el archivo antes de llamar a Piper: su error por un modelo
        # ausente es un stack de onnxruntime que no le dice nada a nadie, y el
        # arreglo (correr un script) no aparece por ningún lado.
        if not self.ruta_modelo.is_file():
            raise ErrorDeVoz(
                f"No está el modelo de voz «{self._nombre_modelo}» en models/.\n"
                f"  Se baja con: ./scripts/descargar_voz.sh {self._nombre_modelo}\n"
                "  (pesa ~63 MB y no se versiona: se aprovisiona en cada caja nueva)"
            )

        log.info("cargando voz %s", self._nombre_modelo)
        self._voz = PiperVoice.load(self.ruta_modelo)
        self._syn = SynthesisConfig(length_scale=self._length_scale)
        return self._voz

    @property
    def frecuencia(self) -> int:
        """La del modelo, no una constante nuestra. Ver el comentario en audio.py."""
        return self._cargar().config.sample_rate

    def _trozos(self, texto: str) -> Iterator[bytes]:
        voz = self._cargar()
        for frase in _RE_FRASE.split(texto):
            frase = frase.strip()
            if not frase:
                continue
            for trozo in voz.synthesize(frase, self._syn):
                yield trozo.audio_int16_bytes

    def decir(self, texto: str) -> bool:
        """Sintetiza y reproduce. Devuelve si llegó a decir algo.

        El audio se va escribiendo al parlante mientras Piper sintetiza el resto:
        `_trozos` es un generador y `reproducir` lo consume perezosamente. Es lo
        que hace que la respuesta empiece a sonar de inmediato en vez de después
        de la síntesis completa.
        """
        limpio = _limpiar(texto)
        if not limpio:
            return False
        self._audio.reproducir(self._trozos(limpio), self.frecuencia)
        return True

    def a_wav(self, texto: str, destino: Path) -> Path:
        """Escribe la síntesis a un .wav. Para probar la voz sin depender del parlante."""
        import wave

        voz = self._cargar()
        with wave.open(str(destino), "wb") as archivo:
            voz.synthesize_wav(_limpiar(texto), archivo, self._syn)
        return destino
