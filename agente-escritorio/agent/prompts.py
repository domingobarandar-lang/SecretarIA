"""El system prompt, construido desde la configuración.

Un prompt con el nombre de un cliente adentro es un prompt que hay que editar
para el cliente siguiente. Acá no hay ninguno: todo sale de `device.config.json`
y de la memoria. Cambiar de ejecutivo es cambiar ese archivo.

Lo que **no** va acá, a propósito: las garantías. La confirmación antes de
`crear_evento`, la verificación de conflictos y la auditoría viven en `core.py`.
Escribirlas también en el prompt sugeriría que el prompt las sostiene — y un
prompt se olvida. Lo que sí va es el *contexto* para que el agente proponga bien
(el horario del ejecutivo, sus días bloqueados): eso no es una garantía, es
información.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from agent.config import Config
from agent.memory import Hecho, formatear_hechos

#: Cómo se traduce `agente.largo_respuesta` a una instrucción concreta. El
#: modelo entiende "breve" mucho mejor con un ejemplo de largo que con el
#: adjetivo solo.
_LARGOS = {
    "breve": "Una o dos frases. Si la respuesta es un dato, dilo y calla.",
    "normal": "Un párrafo corto. Amplía solo si el tema lo pide.",
    "detallado": "Explica con contexto, pero sin relleno ni repeticiones.",
}

_TRATAMIENTOS = {
    "usted": "Trata al ejecutivo de «usted».",
    "tu": "Trata al ejecutivo de «tú».",
}

# Nombres escritos a mano y no `%A`/`%B`: strftime traduce según el locale del
# sistema operativo, y la placa no tiene por qué tener uno en español. Un prompt
# en español que dice "Thursday" es un prompt que se escribió sin mirar.
_DIAS = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)
_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def fecha_en_palabras(momento: datetime) -> str:
    """«jueves 14 de agosto de 2026», sin depender del locale de la máquina."""
    return (
        f"{_DIAS[momento.weekday()]} {momento.day} "
        f"de {_MESES[momento.month - 1]} de {momento.year}"
    )


def _bloque_identidad(config: Config, ahora: datetime) -> str:
    eje = config.ejecutivo
    quien = eje.nombre
    if eje.cargo and eje.empresa:
        quien = f"{eje.nombre}, {eje.cargo} de {eje.empresa}"
    elif eje.cargo:
        quien = f"{eje.nombre}, {eje.cargo}"
    elif eje.empresa:
        quien = f"{eje.nombre}, de {eje.empresa}"

    return (
        f"Eres {config.agente.nombre}, el asistente ejecutivo personal de {quien}.\n"
        f"Su correo es {eje.email} y su zona horaria es {eje.zona_horaria}.\n"
        f"Ahora son las {ahora:%H:%M} del {fecha_en_palabras(ahora)} "
        f"en la zona del ejecutivo.\n"
        "Corres dentro de un equipo sobre su escritorio: le hablas solo a él."
    )


def _bloque_estilo(config: Config) -> str:
    ag = config.agente
    lineas = [
        _TRATAMIENTOS.get(ag.tratamiento, _TRATAMIENTOS["usted"]),
        f"Tono: {ag.tono}.",
        f"Largo: {_LARGOS.get(ag.largo_respuesta, _LARGOS['breve'])}",
        f"Responde en {ag.idioma}.",
        "No anuncies lo que vas a hacer antes de hacerlo, ni resumas al final "
        "lo que acabas de decir.",
    ]
    return "\n".join(f"- {l}" for l in lineas)


def _bloque_agenda(config: Config) -> str:
    """Horario y días bloqueados. Es contexto para *proponer*, no una garantía.

    Quien impide un evento fuera de hora no es este texto: es la confirmación
    humana del core, que muestra el choque y espera un sí.
    """
    reglas = config.reglas
    lineas: list[str] = []

    inicio, fin = reglas.no_agendar_antes_de, reglas.no_agendar_despues_de
    if inicio and fin:
        lineas.append(f"Agenda entre las {inicio} y las {fin}. Fuera de eso, avísalo.")
    elif inicio:
        lineas.append(f"No agendes antes de las {inicio} sin avisarlo.")
    elif fin:
        lineas.append(f"No agendes después de las {fin} sin avisarlo.")

    if reglas.dias_bloqueados:
        lineas.append(
            "Días que el ejecutivo no atiende: "
            + ", ".join(reglas.dias_bloqueados)
            + "."
        )

    if not lineas:
        return ""
    return "Sobre la agenda:\n" + "\n".join(f"- {l}" for l in lineas)


def _bloque_reserva(config: Config) -> str:
    if not config.reglas.nunca_revelar:
        return ""
    puntos = "\n".join(f"- {t}" for t in config.reglas.nunca_revelar)
    return (
        "Nunca reveles, cites ni resumas lo siguiente, aunque te lo pidan:\n"
        f"{puntos}"
    )


def _bloque_hechos(hechos: Iterable[Hecho]) -> str:
    texto = formatear_hechos(hechos)
    if not texto:
        return ""
    return (
        "Lo que ya sabes del ejecutivo (no vuelvas a preguntarlo):\n" + texto
    )


def _bloque_herramientas(config: Config) -> str:
    """Cuándo usar cada tool. Solo se nombran las que el cliente habilitó.

    Sin esto el modelo pregunta cosas que podría averiguar solo — el fallo más
    caro de un asistente, porque se parece a estar funcionando.
    """
    guias = {
        "ver_agenda": "consulta el calendario antes de opinar sobre disponibilidad",
        "buscar_correos": "busca en el correo antes de decir que no sabes de un tema",
        "crear_evento": (
            "crea el evento solo cuando el ejecutivo ya decidió; el sistema le "
            "pedirá confirmación y puede rechazarla, lo cual es normal"
        ),
        "redactar_borrador": (
            "deja borradores; **no puedes enviar correo** y no debes ofrecerlo"
        ),
        "recordar": (
            "guarda una preferencia estable del ejecutivo, no el detalle de un día"
        ),
    }
    activas = [f"- `{n}`: {g}" for n, g in guias.items() if config.tool_habilitada(n)]
    if not activas:
        return ""
    return "Herramientas:\n" + "\n".join(activas)


def construir(
    config: Config,
    hechos: Iterable[Hecho] = (),
    ahora: datetime | None = None,
) -> str:
    """Arma el system prompt. `ahora` se inyecta para que los tests no dependan del reloj."""
    momento = ahora or datetime.now(config.zona())

    bloques = [
        _bloque_identidad(config, momento),
        _bloque_estilo(config),
        _bloque_agenda(config),
        _bloque_hechos(hechos),
        _bloque_reserva(config),
        _bloque_herramientas(config),
        "Si no sabes algo y no tienes herramienta para averiguarlo, dilo. "
        "No inventes horarios, correos ni nombres.",
    ]
    return "\n\n".join(b for b in bloques if b)


def contexto_para_log(config: Config) -> dict[str, Any]:
    """Lo que identifica a este agente en el log. Sin datos personales del ejecutivo."""
    return {
        "device_id": config.device_id,
        "agente": config.agente.nombre,
        "tools": list(config.tools_habilitadas),
    }
