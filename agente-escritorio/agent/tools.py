"""Las cinco herramientas: contrato, despacho y fuente de datos.

La lista es cerrada — `config.TOOLS_DISPONIBLES` la define y el validador la hace
cumplir. Acá se declara qué recibe cada una y qué devuelve.

**Enviar correo no es una tool y no va a serlo.** `redactar_borrador` deja un
borrador y ahí termina; que el correo salga es una decisión de una persona
frente a su bandeja, no de un agente sobre un escritorio.

La separación que importa es entre el **contrato** (esto) y la **fuente** (de
dónde salen los eventos y los correos). Hoy la fuente es un mock; mañana es el
MCP de Google. El despacho, el core y el prompt no se enteran del cambio: solo
se construye el agente con otra fuente.

Las tools devuelven **texto para el modelo**. Los objetos (`Evento`) son para el
core, que necesita comparar horarios y no leer prosa.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from agent.config import Config
from agent.memory import Memoria

# --------------------------------------------------------------------------
# Objetos del dominio
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evento:
    id: str
    titulo: str
    inicio: datetime
    fin: datetime
    invitados: tuple[str, ...] = ()
    descripcion: str = ""

    def como_linea(self) -> str:
        horario = f"{self.inicio:%H:%M}–{self.fin:%H:%M}"
        con = f" · con {', '.join(self.invitados)}" if self.invitados else ""
        return f"{self.inicio:%Y-%m-%d} {horario} · {self.titulo}{con}"


@dataclass(frozen=True)
class Correo:
    id: str
    remitente: str
    asunto: str
    fecha: datetime
    extracto: str

    def como_linea(self) -> str:
        return (
            f"{self.fecha:%Y-%m-%d %H:%M} · {self.remitente} · {self.asunto}\n"
            f"    {self.extracto}"
        )


class ErrorDeHerramienta(Exception):
    """La tool no se puede ejecutar con lo que llegó. El modelo lo ve y reintenta."""


# --------------------------------------------------------------------------
# Fechas
# --------------------------------------------------------------------------


def parsear_momento(valor: Any, zona: ZoneInfo, *, campo: str) -> datetime:
    """Acepta «2026-08-20», «2026-08-20T10:00» o con offset; siempre sale con zona.

    Un datetime sin zona comparado contra uno con zona revienta en tiempo de
    ejecución, y lo haría dentro del chequeo de conflictos — el peor lugar.
    """
    if not isinstance(valor, str) or not valor.strip():
        raise ErrorDeHerramienta(f"«{campo}» debe ser una fecha ISO, llegó: {valor!r}")
    texto = valor.strip().replace("Z", "+00:00")
    try:
        momento = datetime.fromisoformat(texto)
    except ValueError:
        raise ErrorDeHerramienta(
            f"«{campo}» no es una fecha ISO válida: «{valor}». "
            "Usa AAAA-MM-DD o AAAA-MM-DDTHH:MM."
        ) from None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=zona)
    return momento.astimezone(zona)


def solapan(a_ini: datetime, a_fin: datetime, b_ini: datetime, b_fin: datetime) -> bool:
    """Dos rangos chocan si uno empieza antes de que el otro termine, y viceversa.

    Tocarse en el borde (una reunión termina 10:00, otra empieza 10:00) **no** es
    choque: es una agenda apretada, no un conflicto.
    """
    return a_ini < b_fin and b_ini < a_fin


# --------------------------------------------------------------------------
# Fuente de datos — hoy mock, mañana el MCP de Google
# --------------------------------------------------------------------------


class FuenteDatos(Protocol):
    """Lo que el agente necesita del mundo exterior. Cuatro métodos, ni uno más."""

    def ver_agenda(self, inicio: datetime, fin: datetime) -> list[Evento]: ...

    def crear_evento(self, evento: Evento) -> Evento: ...

    def buscar_correos(self, consulta: str, maximo: int) -> list[Correo]: ...

    def redactar_borrador(self, destinatario: str, asunto: str, cuerpo: str) -> str: ...


@dataclass
class FuenteMock:
    """Datos de mentira, marcados como tales, para probar el loop sin salir a internet.

    Los eventos se generan **relativos a hoy** en vez de con fechas fijas: un mock
    con fechas de 2026 escritas a mano deja de servir el día que alguien lo corre
    en 2027, y falla pareciendo que la agenda está vacía.
    """

    zona: ZoneInfo
    hoy: date | None = None
    eventos: list[Evento] = field(default_factory=list)
    correos: list[Correo] = field(default_factory=list)
    borradores: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        base = self.hoy or datetime.now(self.zona).date()

        def _en(dia_offset: int, hora: int, minuto: int = 0) -> datetime:
            return datetime.combine(
                base + timedelta(days=dia_offset), time(hora, minuto), self.zona
            )

        if not self.eventos:
            self.eventos = [
                Evento("evt-mock-1", "[MOCK] Comité de gerencia", _en(0, 9), _en(0, 10, 30)),
                Evento(
                    "evt-mock-2",
                    "[MOCK] Revisión de presupuesto",
                    _en(0, 15),
                    _en(0, 16),
                    ("finanzas@ejemplo.invalid",),
                ),
                Evento("evt-mock-3", "[MOCK] Directorio mensual", _en(1, 11), _en(1, 13)),
            ]
        if not self.correos:
            self.correos = [
                Correo(
                    "cor-mock-1",
                    "contraparte@ejemplo.invalid",
                    "[MOCK] Propuesta revisada",
                    _en(-1, 17, 40),
                    "Adjunto la versión con los cambios que conversamos.",
                ),
                Correo(
                    "cor-mock-2",
                    "finanzas@ejemplo.invalid",
                    "[MOCK] Cierre de mes",
                    _en(-2, 9, 5),
                    "Necesitamos tu visto bueno antes del viernes.",
                ),
            ]

    def ver_agenda(self, inicio: datetime, fin: datetime) -> list[Evento]:
        hallados = [e for e in self.eventos if solapan(e.inicio, e.fin, inicio, fin)]
        return sorted(hallados, key=lambda e: e.inicio)

    def crear_evento(self, evento: Evento) -> Evento:
        creado = Evento(
            id=f"evt-{uuid.uuid4().hex[:8]}",
            titulo=evento.titulo,
            inicio=evento.inicio,
            fin=evento.fin,
            invitados=evento.invitados,
            descripcion=evento.descripcion,
        )
        self.eventos.append(creado)
        return creado

    def buscar_correos(self, consulta: str, maximo: int) -> list[Correo]:
        agujas = [p for p in consulta.lower().split() if p]
        def calza(c: Correo) -> bool:
            heno = f"{c.remitente} {c.asunto} {c.extracto}".lower()
            return not agujas or any(a in heno for a in agujas)
        hallados = [c for c in self.correos if calza(c)]
        return sorted(hallados, key=lambda c: c.fecha, reverse=True)[:maximo]

    def redactar_borrador(self, destinatario: str, asunto: str, cuerpo: str) -> str:
        identificador = f"bor-{uuid.uuid4().hex[:8]}"
        self.borradores.append(
            {
                "id": identificador,
                "destinatario": destinatario,
                "asunto": asunto,
                "cuerpo": cuerpo,
            }
        )
        return identificador


# --------------------------------------------------------------------------
# Contrato de las tools
# --------------------------------------------------------------------------

CATALOGO: dict[str, dict[str, Any]] = {
    "ver_agenda": {
        "description": (
            "Consulta los eventos del calendario del ejecutivo en un rango. "
            "Úsala antes de opinar sobre disponibilidad, antes de proponer un "
            "horario y siempre que la pregunta dependa de qué hay agendado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha_inicio": {
                    "type": "string",
                    "description": "Inicio del rango: AAAA-MM-DD o AAAA-MM-DDTHH:MM.",
                },
                "fecha_fin": {
                    "type": "string",
                    "description": (
                        "Fin del rango, mismo formato. Si se omite y el inicio es "
                        "una fecha sin hora, se consulta ese día completo."
                    ),
                },
            },
            "required": ["fecha_inicio"],
        },
    },
    "crear_evento": {
        "description": (
            "Crea un evento en el calendario del ejecutivo. Es irreversible: el "
            "sistema le mostrará el detalle y esperará su confirmación explícita, "
            "y puede rechazarla — eso es normal, no un error. Propón el evento "
            "solo cuando el ejecutivo ya decidió hacerlo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "description": "Título del evento."},
                "inicio": {
                    "type": "string",
                    "description": "Inicio: AAAA-MM-DDTHH:MM.",
                },
                "fin": {"type": "string", "description": "Fin: AAAA-MM-DDTHH:MM."},
                "invitados": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Correos de los invitados. Vacío si no hay.",
                },
                "descripcion": {
                    "type": "string",
                    "description": "Detalle o agenda del evento. Opcional.",
                },
            },
            "required": ["titulo", "inicio", "fin"],
        },
    },
    "buscar_correos": {
        "description": (
            "Busca en el correo del ejecutivo y devuelve remitente, asunto, fecha "
            "y un extracto. Úsala antes de decir que no sabes de un tema. No "
            "devuelve el cuerpo completo de los mensajes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Palabras a buscar en remitente, asunto y cuerpo.",
                },
                "max_resultados": {
                    "type": "integer",
                    "description": "Cuántos traer como máximo (1 a 25). Por defecto 5.",
                },
            },
            "required": ["query"],
        },
    },
    "redactar_borrador": {
        "description": (
            "Crea un BORRADOR de correo en la cuenta del ejecutivo. Nunca envía: "
            "el borrador queda esperando a que él lo revise y lo mande. No ofrezcas "
            "enviar correos, porque no puedes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destinatario": {"type": "string", "description": "Correo del destinatario."},
                "asunto": {"type": "string", "description": "Asunto del correo."},
                "cuerpo": {"type": "string", "description": "Cuerpo del correo, ya redactado."},
            },
            "required": ["destinatario", "asunto", "cuerpo"],
        },
    },
    "recordar": {
        "description": (
            "Guarda una preferencia estable del ejecutivo para futuras sesiones "
            "(«no agenda los viernes en la tarde», «firma como JB»). No la uses "
            "para el detalle de un día ni para lo que ya está en el calendario."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hecho": {
                    "type": "string",
                    "description": "La preferencia, en una frase y en tercera persona.",
                },
                "categoria": {
                    "type": "string",
                    "description": "Tema: agenda, correo, contactos, general…",
                },
            },
            "required": ["hecho"],
        },
    },
}


def esquemas(config: Config) -> list[dict[str, Any]]:
    """Las tools que este cliente habilitó, en el formato que espera la API."""
    return [
        {"name": nombre, **CATALOGO[nombre]}
        for nombre in config.tools_habilitadas
        if nombre in CATALOGO
    ]


# --------------------------------------------------------------------------
# Despacho
# --------------------------------------------------------------------------


def _texto(entrada: dict[str, Any], clave: str, *, obligatorio: bool = True) -> str:
    valor = entrada.get(clave)
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if obligatorio:
            raise ErrorDeHerramienta(f"falta «{clave}»")
        return ""
    if not isinstance(valor, str):
        raise ErrorDeHerramienta(f"«{clave}» debe ser un texto, llegó {type(valor).__name__}")
    return valor.strip()


def evento_propuesto(entrada: dict[str, Any], zona: ZoneInfo) -> Evento:
    """Convierte el input del modelo en un `Evento`, sin crearlo.

    Vive separado del despacho porque el core lo necesita **antes** de ejecutar:
    para buscar conflictos y para mostrarle a la persona qué va a confirmar.
    """
    inicio = parsear_momento(entrada.get("inicio"), zona, campo="inicio")
    fin = parsear_momento(entrada.get("fin"), zona, campo="fin")
    if fin <= inicio:
        raise ErrorDeHerramienta("«fin» debe ser posterior a «inicio»")

    invitados = entrada.get("invitados") or []
    if not isinstance(invitados, list) or any(not isinstance(i, str) for i in invitados):
        raise ErrorDeHerramienta("«invitados» debe ser una lista de correos")

    return Evento(
        id="",
        titulo=_texto(entrada, "titulo"),
        inicio=inicio,
        fin=fin,
        invitados=tuple(i.strip() for i in invitados if i.strip()),
        descripcion=_texto(entrada, "descripcion", obligatorio=False),
    )


def ejecutar(
    nombre: str,
    entrada: dict[str, Any],
    *,
    config: Config,
    memoria: Memoria,
    fuente: FuenteDatos,
) -> str:
    """Corre una tool y devuelve el texto que ve el modelo.

    No atrapa excepciones: quien registra la acción y decide qué hacer con el
    error es el core, y necesita distinguir un fallo de un resultado vacío.
    """
    if nombre not in CATALOGO:
        raise ErrorDeHerramienta(f"la herramienta «{nombre}» no existe")
    if not config.tool_habilitada(nombre):
        raise ErrorDeHerramienta(f"la herramienta «{nombre}» no está habilitada")

    zona = config.zona()

    if nombre == "ver_agenda":
        crudo_inicio = _texto(entrada, "fecha_inicio")
        inicio = parsear_momento(crudo_inicio, zona, campo="fecha_inicio")
        tenia_hora = "T" in crudo_inicio or " " in crudo_inicio.strip()
        crudo_fin = _texto(entrada, "fecha_fin", obligatorio=False)
        if crudo_fin:
            fin = parsear_momento(crudo_fin, zona, campo="fecha_fin")
        elif tenia_hora:
            # Dieron un instante y ningún fin: se asume el día siguiente completo.
            fin = inicio + timedelta(days=1)
        else:
            # Dieron una fecha pelada: se consulta ese día de 00:00 a 00:00.
            inicio = inicio.replace(hour=0, minute=0, second=0, microsecond=0)
            fin = inicio + timedelta(days=1)
        if fin <= inicio:
            raise ErrorDeHerramienta("«fecha_fin» debe ser posterior a «fecha_inicio»")
        eventos = fuente.ver_agenda(inicio, fin)
        if not eventos:
            return f"Sin eventos entre {inicio:%Y-%m-%d %H:%M} y {fin:%Y-%m-%d %H:%M}."
        return "\n".join(e.como_linea() for e in eventos)

    if nombre == "crear_evento":
        propuesto = evento_propuesto(entrada, zona)
        creado = fuente.crear_evento(propuesto)
        return f"Evento creado ({creado.id}): {creado.como_linea()}"

    if nombre == "buscar_correos":
        consulta = _texto(entrada, "query")
        maximo = entrada.get("max_resultados", 5)
        if not isinstance(maximo, int) or isinstance(maximo, bool):
            raise ErrorDeHerramienta("«max_resultados» debe ser un número entero")
        maximo = max(1, min(maximo, 25))
        correos = fuente.buscar_correos(consulta, maximo)
        if not correos:
            return f"Sin correos que calcen con «{consulta}»."
        return "\n".join(c.como_linea() for c in correos)

    if nombre == "redactar_borrador":
        identificador = fuente.redactar_borrador(
            _texto(entrada, "destinatario"),
            _texto(entrada, "asunto"),
            _texto(entrada, "cuerpo"),
        )
        return (
            f"Borrador guardado ({identificador}). No se envió: queda en la "
            "carpeta de borradores del ejecutivo."
        )

    if nombre == "recordar":
        contenido = _texto(entrada, "hecho")
        categoria = _texto(entrada, "categoria", obligatorio=False) or "general"
        memoria.guardar_hecho(contenido, categoria)
        return f"Recordado [{categoria}]: {contenido}"

    raise AssertionError(f"tool sin despacho: {nombre}")  # pragma: no cover
