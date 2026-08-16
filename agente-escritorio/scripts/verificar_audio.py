"""Verifica que el micrófono capte y que los parlantes suenen. Antes de la voz.

Cuando el ciclo de voz no responde, la pregunta es siempre la misma: ¿falló el
audio o falló el agente? Este script contesta la primera mitad sin cargar un
modelo, sin gastar un token y sin depender de nada del proyecto. Si acá el
micrófono no capta, no hay wake word que valga.

También imprime el índice de cada dispositivo, que es lo que se escribe en
`voz.dispositivo_entrada` y `voz.dispositivo_salida` de device.config.json
cuando la caja tiene más de un micrófono conectado.

    python scripts/verificar_audio.py            # listar, grabar, reproducir
    python scripts/verificar_audio.py --listar   # solo listar dispositivos
    python scripts/verificar_audio.py --entrada 3 --salida 5
"""

from __future__ import annotations

import argparse
import array
import math
import sys
import time
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WAV_PRUEBA = RAIZ / "prueba.wav"

# La consola de Windows abre en cp1252 y este script imprime barras de bloque,
# líneas de caja y ✓/⚠. Sin esto revienta con UnicodeEncodeError antes de listar
# un solo dispositivo — y el diagnóstico de audio no puede morir por su propia
# salida. `errors="replace"` cubre la terminal que igual no sepa dibujarlos.
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover - stdout ya redirigido
        pass

FRECUENCIA = 16000  # 16 kHz: lo que usan el wake word y el STT
TROZO = 1024
SEGUNDOS_POR_DEFECTO = 4

# Por debajo de esto, lo grabado es ruido de fondo y no una voz. El umbral es
# generoso a propósito: acá interesa distinguir «el micrófono está mudo» de
# «el micrófono anda», no medir calidad.
RMS_MINIMO = 250


def _pyaudio():
    try:
        import pyaudio
    except ImportError:
        print(
            "Falta PyAudio. Instálalo con:\n"
            "  .venv\\Scripts\\python.exe -m pip install pyaudio",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return pyaudio


def _nivel(datos: bytes) -> float:
    """RMS de un bloque de 16 bits mono. 0 = silencio absoluto."""
    muestras = array.array("h")
    muestras.frombytes(datos[: len(datos) - (len(datos) % 2)])
    if not muestras:
        return 0.0
    return math.sqrt(sum(m * m for m in muestras) / len(muestras))


def _barra(rms: float, ancho: int = 40) -> str:
    """Barra logarítmica: el oído es logarítmico y una lineal no muestra nada."""
    if rms < 1:
        llenos = 0
    else:
        llenos = min(ancho, int(ancho * math.log10(rms) / math.log10(32768)))
    return "█" * llenos + "·" * (ancho - llenos)


def _nombre(crudo: str) -> str:
    """Repara «micrÃ³fono» -> «micrófono».

    PyAudio en Windows entrega los nombres en UTF-8 pero ya decodificados como
    cp1252. Este script existe para que una persona reconozca su micrófono en la
    lista, así que un nombre ilegible no es cosmético. Solo se revierte cuando el
    round-trip calza; si no, se devuelve tal cual y no se estropea nada.
    """
    try:
        return crudo.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return crudo


def listar(pa) -> None:
    print("\nDispositivos de audio\n" + "─" * 72)
    try:
        entrada_def = pa.get_default_input_device_info()["index"]
    except (OSError, IOError):
        entrada_def = None
    try:
        salida_def = pa.get_default_output_device_info()["index"]
    except (OSError, IOError):
        salida_def = None

    for i in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(i)
            api = pa.get_host_api_info_by_index(info["hostApi"])["name"]
        except (OSError, IOError):
            continue
        roles = []
        if info["maxInputChannels"] > 0:
            roles.append("ENTRADA" + (" ←default" if i == entrada_def else ""))
        if info["maxOutputChannels"] > 0:
            roles.append("SALIDA" + (" ←default" if i == salida_def else ""))
        if not roles:
            continue
        print(f"  [{i:>2}] {_nombre(info['name'])[:44]:<44} {api:<12} {' · '.join(roles)}")
    print("─" * 72)
    print("El número entre corchetes es lo que va en voz.dispositivo_entrada / _salida.\n")


def grabar(pa, pyaudio, indice, segundos: int) -> bytes:
    print(f"Grabando {segundos} segundos. HABLE AHORA — diga cualquier cosa.\n")
    try:
        flujo = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=FRECUENCIA,
            input=True,
            input_device_index=indice,
            frames_per_buffer=TROZO,
        )
    except (OSError, IOError) as problema:
        print(f"\nNo se pudo abrir el micrófono: {problema}", file=sys.stderr)
        print("Revisa que Windows tenga permiso de micrófono para el terminal.", file=sys.stderr)
        raise SystemExit(1)

    trozos = []
    pico = 0.0
    fin = time.monotonic() + segundos
    try:
        while time.monotonic() < fin:
            datos = flujo.read(TROZO, exception_on_overflow=False)
            trozos.append(datos)
            rms = _nivel(datos)
            pico = max(pico, rms)
            sys.stdout.write(f"\r  {_barra(rms)}  rms {int(rms):>5}")
            sys.stdout.flush()
    finally:
        flujo.stop_stream()
        flujo.close()

    print(f"\n\n  Pico de la grabación: {int(pico)}")
    if pico < RMS_MINIMO:
        print(
            "  ⚠ Eso es prácticamente silencio. El micrófono está mudo, apagado,\n"
            "    o Windows lo tiene bloqueado para este programa.\n"
            "    Prueba otro índice con --entrada N."
        )
    else:
        print("  ✓ El micrófono capta.")
    return b"".join(trozos)


def reproducir(pa, pyaudio, indice, datos: bytes, frecuencia: int, canales: int = 1) -> None:
    try:
        flujo = pa.open(
            format=pyaudio.paInt16,
            channels=canales,
            rate=frecuencia,
            output=True,
            output_device_index=indice,
        )
    except (OSError, IOError) as problema:
        print(f"No se pudo abrir el parlante: {problema}", file=sys.stderr)
        raise SystemExit(1)
    try:
        flujo.write(datos)
    finally:
        flujo.stop_stream()
        flujo.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prueba de micrófono y parlantes")
    parser.add_argument("--listar", action="store_true", help="solo listar dispositivos")
    parser.add_argument("--entrada", type=int, default=None, help="índice del micrófono")
    parser.add_argument("--salida", type=int, default=None, help="índice del parlante")
    parser.add_argument("--segundos", type=int, default=SEGUNDOS_POR_DEFECTO)
    args = parser.parse_args(argv)

    pyaudio = _pyaudio()
    pa = pyaudio.PyAudio()
    try:
        listar(pa)
        if args.listar:
            return 0

        # 1. Parlantes con audio conocido-bueno. Si esto no suena, no tiene
        #    sentido interpretar nada de lo que venga después.
        if WAV_PRUEBA.is_file():
            print(f"1) Reproduciendo {WAV_PRUEBA.name} (la voz de Piper)...")
            with wave.open(str(WAV_PRUEBA), "rb") as wav:
                reproducir(
                    pa,
                    pyaudio,
                    args.salida,
                    wav.readframes(wav.getnframes()),
                    wav.getframerate(),
                    wav.getnchannels(),
                )
            print("   ¿Se escuchó una voz diciendo «Buenos días»?\n")
        else:
            print(f"1) (sin {WAV_PRUEBA.name}, se salta la prueba de voz)\n")

        # 2. Micrófono.
        print("2) Prueba de micrófono")
        grabado = grabar(pa, pyaudio, args.entrada, args.segundos)

        # 3. Vuelta completa: lo grabado sale por los parlantes.
        print("\n3) Reproduciendo lo que grabó...")
        reproducir(pa, pyaudio, args.salida, grabado, FRECUENCIA)
        print("   ¿Se escuchó su propia voz?\n")
        print("Si las tres respuestas son sí, el audio está listo.")
    finally:
        pa.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
