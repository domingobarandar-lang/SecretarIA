"""El loop agéntico y las garantías que no dependen del prompt.

Tres cosas viven acá y no en `prompts.py`, por el mismo motivo: **el prompt se
olvida, el código no.**

1. **`crear_evento` no corre sin un sí explícito del usuario.** La confirmación
   es una función que se inyecta; si nadie la inyecta, el default **deniega**.
   Fallar cerrado importa: un agente desatendido que crea eventos porque nadie
   configuró el confirmador es exactamente el accidente que esta regla evita.
2. **Los conflictos los busca el core, no el modelo.** La regla del producto es
   "verifica la agenda antes de crear". Confiar en que el modelo llamó a
   `ver_agenda` sería auditarlo; en vez de eso el core consulta la agenda él
   mismo con el rango propuesto. Más corto y no se puede saltar.
3. **`acciones` se escribe pase lo que pase**, incluso cuando la tool revienta.
   Una auditoría que solo guarda los éxitos miente justo en el caso que se va a
   fiscalizar.

Y una cuarta que es de robustez pura: **el corte de la ventana lo decide el
core**. Cortar entre un `tool_use` y su `tool_result` deja al modelo con una
llamada sin respuesta y la API rechaza el request entero.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Sequence

from agent import llm, prompts, tools
from agent.config import ACCIONES_IRREVERSIBLES, DIAS_SEMANA, Config
from agent.memory import Memoria
from agent.tools import ErrorDeHerramienta, Evento, FuenteDatos, FuenteMock

log = logging.getLogger("secretaria")

#: El modelo lo pone el proveedor (ver `agent/llm.py`) y `MODELO` en el entorno lo
#: tapa. Este valor solo se alcanza con un cliente inyectado —los tests— porque en
#: una corrida real `llm.crear_cliente()` ya habría fallado sin `LLM_PROVIDER`.
#: Es inválido a propósito: si alguna vez llegara a una API de verdad, revienta en
#: vez de facturar contra un modelo que nadie eligió.
MODELO_SIN_RESOLVER = "sin-modelo-declarado"

#: `agente.largo_respuesta` no puede quedarse solo en el prompt: si el modelo lo
#: ignora, el techo de tokens lo hace cumplir igual. Es el mismo ajuste dicho dos
#: veces, una por las buenas y otra por las malas.
_TOPE_TOKENS = {"breve": 1024, "normal": 2048, "detallado": 4096}

_SIN_TILDES = str.maketrans("áéíóú", "aeiou")


class ErrorDelAgente(Exception):
    """El turno no se pudo completar. El REPL lo muestra y sigue vivo."""


@dataclass(frozen=True)
class SolicitudConfirmacion:
    """Lo que se le muestra a la persona antes de una acción irreversible.

    Lleva los conflictos ya resueltos: quien confirma tiene que ver contra qué
    choca el evento, no enterarse después.
    """

    herramienta: str
    evento: Evento
    conflictos: tuple[Evento, ...] = ()
    advertencias: tuple[str, ...] = ()

    def como_texto(self) -> str:
        lineas = ["Voy a crear este evento:", f"  {self.evento.como_linea()}"]
        if self.evento.descripcion:
            lineas.append(f"  {self.evento.descripcion}")
        if self.conflictos:
            lineas.append("Choca con:")
            lineas += [f"  ! {e.como_linea()}" for e in self.conflictos]
        for aviso in self.advertencias:
            lineas.append(f"  ! {aviso}")
        return "\n".join(lineas)


def denegar_siempre(_: SolicitudConfirmacion) -> bool:
    """Default del confirmador: **no**.

    Un agente al que nadie le enchufó una forma de preguntar no tiene permiso
    para actuar de forma irreversible. El silencio no es un sí.
    """
    return False


# --------------------------------------------------------------------------
# Ventana deslizante
# --------------------------------------------------------------------------


def _tiene_bloques(mensaje: dict[str, Any], tipo: str) -> bool:
    contenido = mensaje.get("content")
    if not isinstance(contenido, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == tipo for b in contenido)


def ventana(mensajes: Sequence[dict[str, Any]], maximo: int) -> list[dict[str, Any]]:
    """Los últimos `maximo` mensajes, recortados a un punto donde la API los acepte.

    Dos invariantes que la API exige y que un `mensajes[-N:]` pelado rompe:
    la conversación empieza con `user`, y ningún `tool_result` puede quedar sin
    el `tool_use` que lo pidió. Recortar de más es barato; recortar mal es un 400.
    """
    if maximo <= 0 or len(mensajes) <= maximo:
        recorte = list(mensajes)
    else:
        recorte = list(mensajes[-maximo:])

    while recorte:
        primero = recorte[0]
        huerfano = primero.get("role") == "user" and _tiene_bloques(primero, "tool_result")
        if primero.get("role") == "assistant" or huerfano:
            recorte.pop(0)
            continue
        break
    return recorte


# --------------------------------------------------------------------------
# El agente
# --------------------------------------------------------------------------


@dataclass
class Turno:
    """Lo que produjo un turno. El REPL muestra `texto`; el resto es para el log."""

    texto: str
    iteraciones: int = 0
    tokens: int = 0
    herramientas: tuple[str, ...] = ()
    detenido_por_tope: bool = False


class Agente:
    def __init__(
        self,
        config: Config,
        memoria: Memoria,
        *,
        cliente: Any = None,
        fuente: FuenteDatos | None = None,
        confirmar: Callable[[SolicitudConfirmacion], bool] | None = None,
        modelo: str | None = None,
        ahora: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.memoria = memoria
        self.fuente = fuente or FuenteMock(zona=config.zona())
        self.confirmar = confirmar or denegar_siempre
        self._ahora = ahora or (lambda: datetime.now(config.zona()))
        self._cliente = cliente if cliente is not None else llm.crear_cliente()
        self.modelo = (
            modelo
            or os.environ.get("MODELO")
            or getattr(self._cliente, "modelo_por_defecto", None)
            or MODELO_SIN_RESOLVER
        )
        self._mensajes: list[dict[str, Any]] = []
        self.tokens_sesion = 0

    # -- prompt -----------------------------------------------------------

    def system_prompt(self) -> str:
        """Se reconstruye cada turno: la hora avanza y los hechos cambian."""
        return prompts.construir(
            self.config, self.memoria.hechos(limite=50), ahora=self._ahora()
        )

    # -- el loop ----------------------------------------------------------

    def responder(self, texto_usuario: str) -> Turno:
        if not texto_usuario.strip():
            raise ErrorDelAgente("el mensaje no puede estar vacío")

        self.memoria.guardar_mensaje("usuario", texto_usuario)
        self._mensajes.append({"role": "user", "content": texto_usuario})

        tope = self.config.limites.max_iteraciones_loop
        usadas: list[str] = []
        tokens_turno = 0

        for iteracion in range(1, tope + 1):
            respuesta = self._llamar_al_modelo()
            tokens_turno += _tokens_de(respuesta)

            contenido = _contenido_como_lista(respuesta)
            self._mensajes.append({"role": "assistant", "content": contenido})

            texto = _texto_de(contenido)
            if texto:
                self.memoria.guardar_mensaje("asistente", texto, tokens=_tokens_de(respuesta))

            if getattr(respuesta, "stop_reason", None) != "tool_use":
                self.tokens_sesion += tokens_turno
                return Turno(
                    texto=texto or "(sin respuesta)",
                    iteraciones=iteracion,
                    tokens=tokens_turno,
                    herramientas=tuple(usadas),
                )

            resultados = []
            for bloque in contenido:
                if bloque.get("type") != "tool_use":
                    continue
                usadas.append(bloque.get("name", "?"))
                resultados.append(self._correr_tool(bloque))

            if not resultados:
                # `stop_reason` dijo tool_use pero no vino ninguna: sin resultados
                # que mandar, otro request repetiría el mismo turno para siempre.
                log.warning("stop_reason=tool_use sin bloques tool_use; corto el turno")
                self.tokens_sesion += tokens_turno
                return Turno(
                    texto=texto or "(sin respuesta)",
                    iteraciones=iteracion,
                    tokens=tokens_turno,
                    herramientas=tuple(usadas),
                )

            self._mensajes.append({"role": "user", "content": resultados})

        self.tokens_sesion += tokens_turno
        log.warning("tope de %s iteraciones alcanzado", tope)
        return Turno(
            texto=(
                "Me quedé dando vueltas sin llegar a una respuesta. "
                "¿Puede replantear lo que necesita?"
            ),
            iteraciones=tope,
            tokens=tokens_turno,
            herramientas=tuple(usadas),
            detenido_por_tope=True,
        )

    # -- una llamada al modelo -------------------------------------------

    def _llamar_al_modelo(self) -> Any:
        peticion: dict[str, Any] = {
            "model": self.modelo,
            "max_tokens": _TOPE_TOKENS.get(self.config.agente.largo_respuesta, 1024),
            "system": self.system_prompt(),
            "messages": ventana(self._mensajes, self.config.limites.max_mensajes_historial),
            "timeout": float(self.config.limites.timeout_llamada_modelo_seg),
        }
        esquemas = tools.esquemas(self.config)
        if esquemas:
            peticion["tools"] = esquemas

        partida = time.monotonic()
        try:
            respuesta = self._cliente.messages.create(**peticion)
        except Exception as error:  # el REPL sigue vivo; el turno no
            log.error(
                "fallo la llamada al modelo tras %.2fs: %s",
                time.monotonic() - partida,
                error,
            )
            raise ErrorDelAgente(f"no pude hablar con el modelo: {error}") from error

        log.info(
            "modelo respondio en %.2fs (stop=%s, tokens=%s)",
            time.monotonic() - partida,
            getattr(respuesta, "stop_reason", None),
            _tokens_de(respuesta),
        )
        return respuesta

    # -- una tool ---------------------------------------------------------

    def _correr_tool(self, bloque: dict[str, Any]) -> dict[str, Any]:
        nombre = bloque.get("name", "")
        entrada = bloque.get("input") or {}
        if not isinstance(entrada, dict):
            entrada = {}

        confirmada = False
        try:
            if nombre in ACCIONES_IRREVERSIBLES:
                permitido, solicitud = self._pedir_confirmacion(nombre, entrada)
                confirmada = permitido
                if not permitido:
                    aviso = "El ejecutivo no confirmó la acción; no se ejecutó nada."
                    self.memoria.registrar_accion(nombre, entrada, aviso, confirmada=False)
                    log.info("%s rechazada por el usuario", nombre)
                    return _resultado(bloque, aviso, error=False)
                log.info("%s confirmada: %s", nombre, solicitud.evento.como_linea())

            salida = tools.ejecutar(
                nombre,
                entrada,
                config=self.config,
                memoria=self.memoria,
                fuente=self.fuente,
            )
        except ErrorDeHerramienta as problema:
            texto = f"ERROR: {problema}"
            self.memoria.registrar_accion(nombre, entrada, texto, confirmada=confirmada)
            log.warning("%s rechazo la entrada: %s", nombre, problema)
            return _resultado(bloque, texto, error=True)
        except Exception as problema:  # una tool rota no puede tumbar el proceso
            texto = f"ERROR inesperado: {problema}"
            self.memoria.registrar_accion(nombre, entrada, texto, confirmada=confirmada)
            log.exception("%s revento", nombre)
            return _resultado(bloque, texto, error=True)

        self.memoria.registrar_accion(nombre, entrada, salida, confirmada=confirmada)
        self.memoria.guardar_mensaje("herramienta", f"{nombre}: {salida}")
        return _resultado(bloque, salida, error=False)

    # -- la compuerta humana ---------------------------------------------

    def _pedir_confirmacion(
        self, nombre: str, entrada: dict[str, Any]
    ) -> tuple[bool, SolicitudConfirmacion]:
        """Arma la solicitud —con conflictos y avisos ya resueltos— y pregunta.

        Que el chequeo de conflictos ocurra acá y no dentro de `tools.ejecutar`
        es deliberado: la persona tiene que ver el choque **antes** de decir sí.
        """
        propuesto = tools.evento_propuesto(entrada, self.config.zona())
        conflictos = tuple(self._conflictos(propuesto))
        solicitud = SolicitudConfirmacion(
            herramienta=nombre,
            evento=propuesto,
            conflictos=conflictos,
            advertencias=tuple(self._avisos_de_reglas(propuesto)),
        )
        return bool(self.confirmar(solicitud)), solicitud

    def _conflictos(self, propuesto: Evento) -> Iterable[Evento]:
        """El core mira la agenda por su cuenta. No pregunta si el modelo miró."""
        try:
            agendados = self.fuente.ver_agenda(propuesto.inicio, propuesto.fin)
        except Exception as problema:
            # Fallar acá no puede impedir la confirmación: se avisa y decide la persona.
            log.warning("no pude revisar conflictos: %s", problema)
            return ()
        return [
            e
            for e in agendados
            if tools.solapan(e.inicio, e.fin, propuesto.inicio, propuesto.fin)
        ]

    def _avisos_de_reglas(self, propuesto: Evento) -> Iterable[str]:
        """Las reglas del ejecutivo se muestran, no se imponen.

        Un tope de horario es una preferencia suya, y él es quien está mirando la
        confirmación: negarle una reunión a las 8:00 que acaba de pedir sería el
        agente sobrepasándose. Lo que no puede pasar es que la cruce en silencio.
        """
        reglas = self.config.reglas
        inicio = propuesto.inicio

        if reglas.no_agendar_antes_de and f"{inicio:%H:%M}" < reglas.no_agendar_antes_de:
            yield f"empieza antes de las {reglas.no_agendar_antes_de} declaradas"
        if reglas.no_agendar_despues_de and f"{inicio:%H:%M}" > reglas.no_agendar_despues_de:
            yield f"empieza después de las {reglas.no_agendar_despues_de} declaradas"

        # `DIAS_SEMANA` (sin tildes) es la misma lista que acepta el validador de
        # config: comparar contra otra fuente dejaría pasar un «miércoles»
        # declarado que el core no reconoce.
        dia_semana = DIAS_SEMANA[inicio.weekday()]
        for bloqueado in reglas.dias_bloqueados:
            crudo = bloqueado.strip().lower()
            if crudo.translate(_SIN_TILDES) == dia_semana:
                yield f"cae {crudo}, un día que el ejecutivo no atiende"
            elif crudo == f"{inicio:%Y-%m-%d}":
                yield f"cae el {crudo}, una fecha bloqueada"


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------


def _resultado(bloque: dict[str, Any], texto: str, *, error: bool) -> dict[str, Any]:
    resultado: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": bloque.get("id", ""),
        "content": texto,
    }
    if error:
        resultado["is_error"] = True
    return resultado


def _contenido_como_lista(respuesta: Any) -> list[dict[str, Any]]:
    """Normaliza los bloques del SDK a diccionarios.

    Se reenvían tal cual en el siguiente request, así que tienen que ser algo
    que `json` sepa serializar — no objetos del SDK.
    """
    bloques: list[dict[str, Any]] = []
    for bloque in getattr(respuesta, "content", None) or []:
        if isinstance(bloque, dict):
            bloques.append(bloque)
            continue
        volcado = getattr(bloque, "model_dump", None)
        bloques.append(volcado(exclude_none=True) if volcado else dict(bloque))
    return bloques


def _texto_de(contenido: list[dict[str, Any]]) -> str:
    partes = [b.get("text", "") for b in contenido if b.get("type") == "text"]
    return "\n".join(p for p in partes if p).strip()


def _tokens_de(respuesta: Any) -> int:
    uso = getattr(respuesta, "usage", None)
    if uso is None:
        return 0
    return int(getattr(uso, "input_tokens", 0) or 0) + int(
        getattr(uso, "output_tokens", 0) or 0
    )
