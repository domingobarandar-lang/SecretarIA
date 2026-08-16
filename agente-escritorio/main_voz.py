"""REPL de voz. Entra audio, sale audio — el mismo agente que `main.py`.

Este archivo es a la voz lo que `main.py` es al terminal: la única parte que
habla con una persona. El core no sabe que existe. De hecho la prueba de que la
separación se respetó es que `main.py` sigue corriendo igual en una caja sin
micrófono, y que la suite del cerebro no importa nada de `voz/`.

Se reutiliza de `main.py` el arranque completo — env, log, fuente de datos y el
conjunto de respuestas afirmativas — porque duplicarlo significaría que un día
la voz y el terminal no estén de acuerdo en qué cuenta como un «sí». Y lo que
cuenta como un «sí» es la compuerta de las acciones irreversibles.

    python main_voz.py                 # el ciclo completo
    python main_voz.py --probar-voz    # solo dice una frase y se va
"""

from __future__ import annotations

import argparse
import logging
import sys

from agent.config import ErrorDeConfig, cargar_config
from agent.core import Agente, ErrorDelAgente, SolicitudConfirmacion
from agent.llm import ErrorDeLLM, crear_cliente
from agent.memory import Memoria
from integrations.google_mcp import ErrorDeGoogle
from main import AFIRMATIVAS, SALIDAS, _armar_fuente, _cargar_env, configurar_log
from voz import ErrorDeVoz
from voz.audio import Audio
from voz.despertar import buscar_nombre, normalizar
from voz.escucha import Escucha
from voz.stt import Transcriptor
from voz.tts import Locutor

log = logging.getLogger(__name__)

#: Cuánto sigue atendiendo después de contestar, antes de volver a exigir el
#: nombre. Es lo que permite encadenar «¿y mañana?» sin repetir «Vera» cada vez.
SEGUNDOS_DE_ATENCION = 12.0

#: Cuánto espera un sí o un no antes de darlo por no. Ver `preguntar_por_voz`.
SEGUNDOS_PARA_CONFIRMAR = 15.0

#: Las mismas afirmativas del terminal, normalizadas: Whisper escribe «Sí.» con
#: mayúscula, tilde y punto, y eso tiene que seguir siendo un sí.
_AFIRMATIVAS_NORM = {normalizar(p) for p in AFIRMATIVAS}
_SALIDAS_NORM = {normalizar(p) for p in SALIDAS}


def _limpiar_dictado(texto: str) -> str:
    """Deja el texto en su forma comparable: sin tildes, sin puntuación, sin espacios."""
    return normalizar(texto).strip(" .,;:¿?¡!-—").strip()


class CicloDeVoz:
    """El bucle: escuchar, entender, pensar, hablar.

    Vive en una clase y no en una función suelta porque el confirmador que se le
    pasa al core necesita el micrófono y el parlante, y pasarlos por argumento
    a través de tres capas era peor que tener estado.
    """

    def __init__(self, config, agente, locutor, transcriptor, escucha) -> None:
        self.config = config
        self.agente = agente
        self.locutor = locutor
        self.transcriptor = transcriptor
        self.escucha = escucha
        self.nombre = config.agente.nombre

    # -- la compuerta humana, en su versión hablada ------------------------

    def preguntar_por_voz(self, solicitud: SolicitudConfirmacion) -> bool:
        """Describe la acción irreversible en voz alta y espera un sí explícito.

        Vale la misma regla que en el terminal, y acá pesa más: **cualquier cosa
        que no sea un sí claro es un no**. Un silencio es no, una transcripción
        vacía es no, «no sé» es no. Whisper se equivoca, y la asimetría del error
        no es simétrica — no crear un evento que se quería crear se arregla
        repitiendo la frase; crear uno que nadie pidió, en la agenda de un
        ejecutivo y con invitados, ya salió por correo a otras personas.

        Se imprime además por pantalla: si alguien está mirando la caja, tiene
        que poder leer exactamente lo mismo que se dijo.
        """
        texto = solicitud.como_texto()
        print()
        print(texto)
        self.locutor.decir(f"{texto}. ¿Lo confirmo?")

        pcm = self.escucha.turno(espera_max_seg=SEGUNDOS_PARA_CONFIRMAR)
        if pcm is None:
            print("[sin respuesta — no se hace nada]")
            self.locutor.decir("No escuché una respuesta. No hago nada.")
            return False

        respuesta = self.transcriptor.transcribir(pcm)
        print(f"< {respuesta}")
        if _limpiar_dictado(respuesta) in _AFIRMATIVAS_NORM:
            return True

        self.locutor.decir("Entendí que no. Lo dejo así.")
        return False

    # -- un turno ----------------------------------------------------------

    def responder(self, texto: str) -> None:
        try:
            turno = self.agente.responder(texto)
        except ErrorDelAgente as problema:
            log.error("turno fallido: %s", problema)
            print(f"[no pude responder] {problema}", file=sys.stderr)
            self.locutor.decir("Disculpe, no pude completar eso.")
            return

        print()
        print(turno.texto)
        print()
        self.locutor.decir(turno.texto)

    def _oir(self, espera_max_seg: float | None = None) -> str | None:
        """Un turno hablado ya transcrito. `None` = nadie habló."""
        pcm = self.escucha.turno(espera_max_seg=espera_max_seg)
        if pcm is None:
            return None
        texto = self.transcriptor.transcribir(pcm)
        if texto:
            print(f"< {texto}")
        return texto

    # -- el bucle ----------------------------------------------------------

    def correr(self) -> None:
        """Dormido hasta que lo nombren; despierto un rato después de contestar.

        No hace falta silenciar el micrófono mientras el agente habla: `decir()`
        es bloqueante y la captura se abre recién cuando terminó de sonar, así
        que nunca se oye a sí mismo. Si algún día la síntesis pasa a un hilo
        aparte, esto deja de ser cierto y hay que apagar la captura a mano.
        """
        despierto = False
        while True:
            if despierto:
                texto = self._oir(espera_max_seg=SEGUNDOS_DE_ATENCION)
                if texto is None:
                    log.debug("nadie siguió hablando: a dormir")
                    despierto = False
                    print(f"[{self.nombre} volvió a esperar su nombre]")
                    continue
                if not texto.strip():
                    continue
                # Aunque esté despierto se acepta el nombre de nuevo: la gente
                # lo repite igual, y sin esto «Vera, agenda...» le llegaría al
                # modelo con su propio nombre pegado adelante.
                _, resto = buscar_nombre(texto, self.nombre)
                orden = resto or texto
            else:
                texto = self._oir()
                if not texto:
                    continue
                llamado, resto = buscar_nombre(texto, self.nombre)
                if not llamado:
                    log.debug("no era para mí: %r", texto)
                    continue
                despierto = True
                if not resto:
                    # La llamaron y nada más. Contestar y quedarse esperando.
                    self.locutor.decir("¿Sí?")
                    continue
                orden = resto

            if _limpiar_dictado(orden) in _SALIDAS_NORM:
                self.locutor.decir("Hasta luego.")
                return

            self.responder(orden)
            despierto = True


def _probar_voz(locutor: Locutor, config) -> int:
    """Dice una frase y termina. Es el paso más chico que prueba la cadena entera:
    modelo en disco, Piper cargando, parlante abierto y con el índice correcto."""
    frase = (
        f"Hola. Soy {config.agente.nombre}, su asistente. "
        "Si escucha esta frase completa, la voz quedó instalada."
    )
    print(f"Diciendo: {frase}")
    locutor.decir(frase)
    print("Si no se escuchó nada, revise voz.dispositivo_salida en device.config.json.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SecretarIA por voz")
    parser.add_argument("config", nargs="?", default=None, help="ruta de device.config.json")
    parser.add_argument(
        "--probar-voz", action="store_true", help="dice una frase de prueba y termina"
    )
    args = parser.parse_args(argv)

    _cargar_env()
    configurar_log()

    # La consola de Windows abre en cp1252 y acá se imprime lo que dijo una
    # persona, que trae tildes por definición.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover
            pass

    try:
        config = cargar_config(args.config)
    except ErrorDeConfig as problema:
        print(problema, file=sys.stderr)
        return 2

    if not config.voz.habilitada:
        print(
            "La voz está apagada. Pon «voz»: {«habilitada»: true} en "
            f"{config.ruta.name} y vuelve a intentar.\n"
            "Los índices de micrófono y parlante los lista: "
            "python scripts/verificar_audio.py --listar",
            file=sys.stderr,
        )
        return 2

    audio = None
    memoria = None
    try:
        audio = Audio(
            entrada=config.voz.dispositivo_entrada, salida=config.voz.dispositivo_salida
        )
        locutor = Locutor(config.voz, audio)

        if args.probar_voz:
            return _probar_voz(locutor, config)

        # El orden importa: primero lo que puede fallar por configuración o por
        # credenciales, y recién después la descarga de Whisper. Fallar tarde,
        # con 500 MB ya bajados, es hacerle perder el tiempo a quien instala.
        cliente = crear_cliente()
        fuente, etiqueta_fuente = _armar_fuente(config)

        transcriptor = Transcriptor(config)
        escucha = Escucha(audio, config.voz)

        memoria = Memoria()
        ciclo = CicloDeVoz(config, None, locutor, transcriptor, escucha)
        agente = Agente(
            config,
            memoria,
            cliente=cliente,
            fuente=fuente,
            confirmar=ciclo.preguntar_por_voz,
        )
        ciclo.agente = agente

        print(f"{config.agente.nombre} — {config.device_id} · sesión {memoria.session_id[:8]}")
        print(f"Modelo: {agente.modelo} · Datos: {etiqueta_fuente}")
        print(f"Voz: {config.voz.modelo} · Oído: whisper-{config.voz.modelo_stt}")
        print("Preparando el reconocimiento de voz...")
        transcriptor.precargar()
        print(f"Listo. Diga «{config.agente.nombre}» para empezar. Ctrl+C para salir.")
        print()

        ciclo.correr()
    except (ErrorDeLLM, ErrorDeGoogle, ErrorDeVoz) as problema:
        print(problema, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
    finally:
        if memoria is not None:
            resumen = memoria.resumen_sesion()
            memoria.cerrar()
            print(
                f"Sesión cerrada · {resumen['mensajes']} mensajes · "
                f"{resumen['acciones']} acciones",
                file=sys.stderr,
            )
        if audio is not None:
            audio.cerrar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
