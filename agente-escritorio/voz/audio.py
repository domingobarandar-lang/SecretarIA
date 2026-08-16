"""El hardware: abrir el micrófono, abrir el parlante, y cerrarlos siempre.

Esta es la única capa que importa PyAudio. Arriba de acá nadie sabe qué es un
`frames_per_buffer` ni un `paInt16`, y eso es lo que permite probar el ciclo de
voz con un micrófono falso — un objeto que devuelve bytes de un archivo.

Dos frecuencias conviven en el proyecto y no hay que confundirlas:

- **16 kHz para entrada.** Es lo que exigen webrtcvad (que solo acepta 8/16/32/48)
  y lo que Whisper espera. No es una preferencia: alimentarlo a otra frecuencia
  transcribe basura sin dar error.
- **22.05 kHz para salida.** Es lo que produce el modelo de Piper. Sale de su
  `.onnx.json`, no se fija acá — una voz de otra calidad trae otra frecuencia y
  reproducirla a 22050 la deja hablando como un pato.
"""

from __future__ import annotations

import logging
from typing import Iterable, Iterator

from voz import ErrorDeVoz

log = logging.getLogger(__name__)

#: Frecuencia de captura. La imponen webrtcvad y Whisper, no nosotros.
FRECUENCIA_CAPTURA = 16000

#: Duración de una trama de captura. webrtcvad **solo** acepta 10, 20 o 30 ms:
#: cualquier otro largo es un `Error` suyo, no una degradación.
MS_TRAMA = 20

#: 320 muestras de 16 bits = 640 bytes por trama a 16 kHz.
MUESTRAS_TRAMA = FRECUENCIA_CAPTURA * MS_TRAMA // 1000

#: Cuánto audio previo al primer sonido de voz se conserva. Sin esto el
#: detector se come la primera sílaba: para cuando decide que hay voz, la voz ya
#: empezó. 200 ms es de sobra y no agrega latencia — es memoria, no espera.
MS_PRE_ROLL = 200

#: Tramas de voz seguidas para dar por empezado el turno. Una sola trama con voz
#: es un portazo o un clic del mouse; tres seguidas ya son alguien hablando.
TRAMAS_PARA_EMPEZAR = 3


def _pyaudio():
    """Importa PyAudio y traduce su ausencia a un mensaje accionable."""
    try:
        import pyaudio
    except ImportError as falta:
        raise ErrorDeVoz(
            "Falta PyAudio, que es lo que abre el micrófono y el parlante.\n"
            "  Windows: .venv\\Scripts\\python.exe -m pip install pyaudio\n"
            "  Raspberry Pi: sudo apt install portaudio19-dev && pip install pyaudio"
        ) from falta
    return pyaudio


class Audio:
    """Dueño del `PyAudio()`. Se abre una vez por proceso y se cierra al final.

    PyAudio reserva recursos de PortAudio al instanciarse, no al abrir un flujo.
    Crear uno por frase pierde ~100 ms cada vez y, en Windows, termina dejando
    handles colgados. Por eso el ciclo de voz crea **uno** y lo pasa.
    """

    def __init__(self, entrada: int | None = None, salida: int | None = None) -> None:
        self._pyaudio = _pyaudio()
        self._pa = self._pyaudio.PyAudio()
        self.entrada = entrada
        self.salida = salida

    def cerrar(self) -> None:
        self._pa.terminate()

    def __enter__(self) -> "Audio":
        return self

    def __exit__(self, *_) -> None:
        self.cerrar()

    # -- salida ------------------------------------------------------------

    def reproducir(self, trozos: Iterable[bytes], frecuencia: int) -> None:
        """Escribe PCM de 16 bits mono al parlante, a medida que va llegando.

        Recibe un iterable y no un `bytes` a propósito: Piper entrega el audio
        por trozos mientras sintetiza, y escribirlos apenas llegan es lo que hace
        que el agente empiece a hablar en menos de un segundo en vez de esperar a
        tener la respuesta entera. Con una respuesta larga la diferencia entre
        las dos formas son varios segundos de silencio incómodo.
        """
        try:
            flujo = self._pa.open(
                format=self._pyaudio.paInt16,
                channels=1,
                rate=frecuencia,
                output=True,
                output_device_index=self.salida,
            )
        except (OSError, IOError) as problema:
            raise ErrorDeVoz(
                f"No se pudo abrir el parlante (índice {self.salida}): {problema}\n"
                "Lista los dispositivos con: python scripts/verificar_audio.py --listar"
            ) from problema
        try:
            for trozo in trozos:
                flujo.write(trozo)
        finally:
            flujo.stop_stream()
            flujo.close()

    # -- entrada -----------------------------------------------------------

    def tramas(self) -> Iterator[bytes]:
        """Tramas de 20 ms desde el micrófono, hasta que quien consume se aburra.

        Generador infinito: lo corta el `break` del que itera, y el `finally`
        cierra el flujo pase lo que pase. Que el micrófono quede abierto después
        de un turno no es un leak cualquiera en este producto — es un micrófono
        encendido en el escritorio de alguien.
        """
        try:
            flujo = self._pa.open(
                format=self._pyaudio.paInt16,
                channels=1,
                rate=FRECUENCIA_CAPTURA,
                input=True,
                input_device_index=self.entrada,
                frames_per_buffer=MUESTRAS_TRAMA,
            )
        except (OSError, IOError) as problema:
            raise ErrorDeVoz(
                f"No se pudo abrir el micrófono (índice {self.entrada}): {problema}\n"
                "En Windows revisa Configuración → Privacidad → Micrófono.\n"
                "Lista los dispositivos con: python scripts/verificar_audio.py --listar"
            ) from problema
        try:
            while True:
                # `exception_on_overflow=False`: si el proceso se distrajo
                # sintetizando, PortAudio descarta muestras viejas y sigue. Un
                # overflow es audio perdido, no una sesión que se cae.
                yield flujo.read(MUESTRAS_TRAMA, exception_on_overflow=False)
        finally:
            flujo.stop_stream()
            flujo.close()
