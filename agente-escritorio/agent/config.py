"""Carga y validación de `device.config.json`.

Un agente no se configura tocando código: todo lo específico del cliente vive
en `device.config.json`. Cambiar de ejecutivo = cambiar ese archivo.

El esquema vive **una sola vez**, acá. Si un campo no está declarado en
`ESQUEMA`, no existe: una llave desconocida es error, no aviso — un `limits`
por `limites` que pasa en silencio es un límite que nadie aplica.

La validación es todo-o-nada: si algo está mal, el proceso no arranca a medias.
Se reportan **todos** los problemas de una vez, no el primero.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RAIZ = Path(__file__).resolve().parent.parent
RUTA_POR_DEFECTO = RAIZ / "device.config.json"
RUTA_EJEMPLO = RAIZ / "device.config.example.json"

#: Las cinco tools de esta versión. Lista cerrada: el config no puede inventar una.
TOOLS_DISPONIBLES = (
    "ver_agenda",
    "crear_evento",
    "buscar_correos",
    "redactar_borrador",
    "recordar",
)

#: Tools que producen un efecto que no se puede deshacer desde el agente.
#: El core exige confirmación humana explícita antes de ejecutarlas.
ACCIONES_IRREVERSIBLES = ("crear_evento",)

_RE_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
_RE_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_RE_IDIOMA = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")
_RE_FECHA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DIAS_SEMANA = (
    "lunes",
    "martes",
    "miercoles",
    "jueves",
    "viernes",
    "sabado",
    "domingo",
)

_SIN_TILDES = str.maketrans("áéíóú", "aeiou")


class ErrorDeConfig(Exception):
    """`device.config.json` no se puede usar. Trae la lista completa de problemas."""

    def __init__(self, ruta: Path | str, errores: list[str]) -> None:
        self.ruta = str(ruta)
        self.errores = list(errores)
        super().__init__(str(self))

    def __str__(self) -> str:
        cabecera = (
            f"Configuración inválida en «{self.ruta}» "
            f"({len(self.errores)} problema{'s' if len(self.errores) != 1 else ''}):"
        )
        detalle = "\n".join(f"  - {e}" for e in self.errores)
        return f"{cabecera}\n{detalle}\n  Referencia: {RUTA_EJEMPLO.name}"


# --------------------------------------------------------------------------
# Formatos
# --------------------------------------------------------------------------


def _formato_email(valor: str) -> str | None:
    if not _RE_EMAIL.match(valor):
        return f"«{valor}» no parece un correo (ejemplo: nombre@empresa.cl)"
    return None


def _formato_hora(valor: str) -> str | None:
    if not _RE_HORA.match(valor):
        return f"«{valor}» no es una hora HH:MM de 24 horas (ejemplo: 09:00)"
    return None


def _formato_zona_horaria(valor: str) -> str | None:
    try:
        ZoneInfo(valor)
    except ZoneInfoNotFoundError:
        try:
            import tzdata  # noqa: F401
        except ImportError:
            return (
                "no se pudo resolver ninguna zona horaria: falta la base de datos "
                "IANA. En Windows se instala con «pip install tzdata»"
            )
        return f"«{valor}» no es una zona horaria IANA (ejemplo: America/Santiago)"
    except (ValueError, KeyError):
        return f"«{valor}» no es una zona horaria IANA (ejemplo: America/Santiago)"
    return None


def _formato_dia_bloqueado(valor: str) -> str | None:
    if _RE_FECHA_ISO.match(valor):
        return None
    if valor.strip().lower().translate(_SIN_TILDES) in DIAS_SEMANA:
        return None
    return (
        f"«{valor}» no es un día de la semana ({', '.join(DIAS_SEMANA)}) "
        "ni una fecha AAAA-MM-DD"
    )


_FORMATOS: dict[str, Callable[[str], str | None]] = {
    "email": _formato_email,
    "hora": _formato_hora,
    "zona_horaria": _formato_zona_horaria,
    "dia_bloqueado": _formato_dia_bloqueado,
}


def _normalizar_tratamiento(valor: str) -> str:
    return "tu" if valor in ("tu", "tú") else valor


# --------------------------------------------------------------------------
# Esquema — única fuente de verdad de qué acepta device.config.json
# --------------------------------------------------------------------------

ESQUEMA: dict[str, dict[str, Any]] = {
    "device_id": {
        "tipo": "texto",
        "requerido": True,
        "patron": _RE_DEVICE_ID,
        "ejemplo": "box-001",
    },
    "ejecutivo": {
        "tipo": "bloque",
        "requerido": True,
        "campos": {
            "nombre": {"tipo": "texto", "requerido": True, "no_vacio": True},
            "cargo": {"tipo": "texto", "default": ""},
            "empresa": {"tipo": "texto", "default": ""},
            "email": {"tipo": "texto", "requerido": True, "formato": "email"},
            "zona_horaria": {
                "tipo": "texto",
                "requerido": True,
                "formato": "zona_horaria",
            },
        },
    },
    "agente": {
        "tipo": "bloque",
        "requerido": True,
        "campos": {
            "nombre": {"tipo": "texto", "requerido": True, "no_vacio": True},
            "idioma": {
                "tipo": "texto",
                "default": "es-CL",
                "patron": _RE_IDIOMA,
                "ejemplo": "es-CL",
            },
            "tratamiento": {
                "tipo": "texto",
                "default": "usted",
                "opciones": ("usted", "tu", "tú"),
                "normaliza": _normalizar_tratamiento,
            },
            "tono": {
                "tipo": "texto",
                "default": "formal, directo, sin rodeos",
                "no_vacio": True,
            },
            "largo_respuesta": {
                "tipo": "texto",
                "default": "breve",
                "opciones": ("breve", "normal", "detallado"),
            },
        },
    },
    "tools_habilitadas": {
        "tipo": "lista_texto",
        "requerido": True,
        "no_vacia": True,
        "elementos_en": TOOLS_DISPONIBLES,
        "unicos": True,
    },
    "reglas": {
        "tipo": "bloque",
        "campos": {
            "confirmar_antes_de_acciones_irreversibles": {
                "tipo": "booleano",
                "default": True,
                "debe_ser": True,
                "motivo": (
                    "«reglas.confirmar_antes_de_acciones_irreversibles» solo acepta true: "
                    "la confirmación humana de acciones irreversibles vive en el core y "
                    "no se puede desactivar por configuración"
                ),
            },
            "no_agendar_antes_de": {
                "tipo": "texto",
                "default": "09:00",
                "formato": "hora",
                "nulo_permitido": True,
            },
            "no_agendar_despues_de": {
                "tipo": "texto",
                "default": "19:00",
                "formato": "hora",
                "nulo_permitido": True,
            },
            "dias_bloqueados": {
                "tipo": "lista_texto",
                "default": (),
                "formato_elemento": "dia_bloqueado",
                "unicos": True,
            },
            "nunca_revelar": {"tipo": "lista_texto", "default": ()},
        },
    },
    "limites": {
        "tipo": "bloque",
        "campos": {
            "max_iteraciones_loop": {
                "tipo": "entero",
                "default": 8,
                "min": 1,
                "max": 50,
            },
            "max_mensajes_historial": {
                "tipo": "entero",
                "default": 12,
                "min": 2,
                "max": 200,
            },
            "timeout_llamada_modelo_seg": {
                "tipo": "entero",
                "default": 20,
                "min": 5,
                "max": 300,
            },
        },
    },
}

_NOMBRE_TIPO = {
    "texto": "un texto",
    "entero": "un número entero",
    "booleano": "true o false",
    "lista_texto": "una lista de textos",
    "bloque": "un objeto",
}


class _Invalido:
    """Marca un valor que no pasó la validación."""


_INVALIDO = _Invalido()


def _tipo_recibido(valor: Any) -> str:
    if valor is None:
        return "null"
    if isinstance(valor, bool):
        return "true/false"
    if isinstance(valor, int) or isinstance(valor, float):
        return "un número"
    if isinstance(valor, str):
        return "un texto"
    if isinstance(valor, list):
        return "una lista"
    if isinstance(valor, dict):
        return "un objeto"
    return type(valor).__name__


def _default(spec: dict[str, Any]) -> Any:
    if spec["tipo"] == "bloque":
        return {n: _default(s) for n, s in spec["campos"].items()}
    if spec["tipo"] == "lista_texto":
        return list(spec.get("default", ()))
    return spec.get("default")


# --------------------------------------------------------------------------
# Validación
# --------------------------------------------------------------------------


def _validar_bloque(
    prefijo: str,
    datos: dict[str, Any],
    campos: dict[str, dict[str, Any]],
    errores: list[str],
) -> dict[str, Any]:
    for clave in datos:
        if clave not in campos:
            parecidas = difflib.get_close_matches(clave, list(campos), n=1, cutoff=0.6)
            sugerencia = f"; ¿quisiste decir «{parecidas[0]}»?" if parecidas else ""
            errores.append(
                f"llave desconocida «{prefijo}{clave}»{sugerencia} "
                f"(permitidas: {', '.join(campos)})"
            )

    resultado: dict[str, Any] = {}
    for nombre, spec in campos.items():
        ruta = f"{prefijo}{nombre}"
        if nombre not in datos:
            if spec.get("requerido"):
                errores.append(f"falta el campo obligatorio «{ruta}»")
            resultado[nombre] = _default(spec)
            continue
        valor = _validar_valor(ruta, datos[nombre], spec, errores)
        resultado[nombre] = _default(spec) if isinstance(valor, _Invalido) else valor
    return resultado


def _validar_valor(
    ruta: str, valor: Any, spec: dict[str, Any], errores: list[str]
) -> Any:
    tipo = spec["tipo"]

    if valor is None and spec.get("nulo_permitido"):
        return None

    if tipo == "bloque":
        if not isinstance(valor, dict):
            errores.append(
                f"«{ruta}» debe ser un objeto, llegó {_tipo_recibido(valor)}"
            )
            return _INVALIDO
        return _validar_bloque(f"{ruta}.", valor, spec["campos"], errores)

    if tipo == "texto":
        if not isinstance(valor, str):
            errores.append(f"«{ruta}» debe ser un texto, llegó {_tipo_recibido(valor)}")
            return _INVALIDO
        if spec.get("no_vacio") and not valor.strip():
            errores.append(f"«{ruta}» no puede estar vacío")
            return _INVALIDO
        if "opciones" in spec and valor not in spec["opciones"]:
            errores.append(
                f"«{ruta}» debe ser uno de: {', '.join(spec['opciones'])}; llegó «{valor}»"
            )
            return _INVALIDO
        if "patron" in spec and not spec["patron"].match(valor):
            errores.append(
                f"«{ruta}» tiene un formato inválido: «{valor}» "
                f"(ejemplo válido: {spec.get('ejemplo', '')})"
            )
            return _INVALIDO
        if "formato" in spec:
            problema = _FORMATOS[spec["formato"]](valor)
            if problema:
                errores.append(f"«{ruta}»: {problema}")
                return _INVALIDO
        normaliza = spec.get("normaliza")
        return normaliza(valor) if normaliza else valor

    if tipo == "entero":
        if isinstance(valor, bool) or not isinstance(valor, int):
            errores.append(
                f"«{ruta}» debe ser un número entero, llegó {_tipo_recibido(valor)}"
            )
            return _INVALIDO
        if not (spec["min"] <= valor <= spec["max"]):
            errores.append(
                f"«{ruta}» debe estar entre {spec['min']} y {spec['max']}, llegó {valor}"
            )
            return _INVALIDO
        return valor

    if tipo == "booleano":
        if not isinstance(valor, bool):
            errores.append(f"«{ruta}» debe ser true o false, llegó {_tipo_recibido(valor)}")
            return _INVALIDO
        if "debe_ser" in spec and valor is not spec["debe_ser"]:
            errores.append(spec["motivo"])
            return _INVALIDO
        return valor

    if tipo == "lista_texto":
        if not isinstance(valor, list):
            errores.append(
                f"«{ruta}» debe ser una lista de textos, llegó {_tipo_recibido(valor)}"
            )
            return _INVALIDO
        if spec.get("no_vacia") and not valor:
            errores.append(f"«{ruta}» no puede ser una lista vacía")
            return _INVALIDO
        limpio: list[str] = []
        hay_error = False
        for i, elemento in enumerate(valor):
            if not isinstance(elemento, str):
                errores.append(
                    f"«{ruta}[{i}]» debe ser un texto, llegó {_tipo_recibido(elemento)}"
                )
                hay_error = True
                continue
            permitidos = spec.get("elementos_en")
            if permitidos is not None and elemento not in permitidos:
                parecidas = difflib.get_close_matches(
                    elemento, list(permitidos), n=1, cutoff=0.6
                )
                sugerencia = f"; ¿quisiste decir «{parecidas[0]}»?" if parecidas else ""
                errores.append(
                    f"«{ruta}[{i}]» = «{elemento}» no existe{sugerencia} "
                    f"(disponibles: {', '.join(permitidos)})"
                )
                hay_error = True
                continue
            if "formato_elemento" in spec:
                problema = _FORMATOS[spec["formato_elemento"]](elemento)
                if problema:
                    errores.append(f"«{ruta}[{i}]»: {problema}")
                    hay_error = True
                    continue
            if spec.get("unicos") and elemento in limpio:
                errores.append(f"«{ruta}» tiene «{elemento}» repetido")
                hay_error = True
                continue
            limpio.append(elemento)
        return _INVALIDO if hay_error else limpio

    raise AssertionError(f"tipo de esquema no soportado: {tipo}")


def _validaciones_cruzadas(datos: dict[str, Any]) -> list[str]:
    """Reglas que involucran a más de un campo. Corren con la estructura ya sana."""
    errores: list[str] = []

    inicio = datos["reglas"]["no_agendar_antes_de"]
    fin = datos["reglas"]["no_agendar_despues_de"]
    if inicio and fin and inicio >= fin:
        errores.append(
            f"«reglas.no_agendar_antes_de» ({inicio}) debe ser anterior a "
            f"«reglas.no_agendar_despues_de» ({fin}); así la ventana de agenda queda vacía"
        )

    tools = datos["tools_habilitadas"]
    if "crear_evento" in tools and "ver_agenda" not in tools:
        errores.append(
            "«tools_habilitadas» incluye «crear_evento» pero no «ver_agenda»: "
            "crear un evento exige verificar conflictos antes, así que ver_agenda "
            "también tiene que estar habilitada"
        )

    return errores


# --------------------------------------------------------------------------
# Objeto de configuración (solo lectura)
# --------------------------------------------------------------------------


class NodoConfig(Mapping):
    """Diccionario de solo lectura con acceso por punto: `config.agente.nombre`."""

    def __init__(self, datos: dict[str, Any]) -> None:
        object.__setattr__(self, "_datos", dict(datos))

    def __getitem__(self, clave: str) -> Any:
        valor = self._datos[clave]
        if isinstance(valor, dict):
            return NodoConfig(valor)
        if isinstance(valor, list):
            return tuple(valor)
        return valor

    def __iter__(self) -> Iterator[str]:
        return iter(self._datos)

    def __len__(self) -> int:
        return len(self._datos)

    def __getattr__(self, nombre: str) -> Any:
        if nombre.startswith("_"):
            raise AttributeError(nombre)
        try:
            return self[nombre]
        except KeyError:
            raise AttributeError(
                f"«{nombre}» no existe en la configuración "
                f"(disponibles: {', '.join(self._datos)})"
            ) from None

    def __setattr__(self, nombre: str, valor: Any) -> None:
        raise TypeError("la configuración es de solo lectura")

    def __repr__(self) -> str:
        return f"NodoConfig({self._datos!r})"

    def como_dict(self) -> dict[str, Any]:
        """Copia mutable y profunda, para logs o para serializar."""
        return json.loads(json.dumps(self._datos))


class Config(NodoConfig):
    """La configuración completa del dispositivo, ya validada."""

    def __init__(self, datos: dict[str, Any], ruta: Path) -> None:
        super().__init__(datos)
        object.__setattr__(self, "_ruta", Path(ruta))

    @property
    def ruta(self) -> Path:
        return self._ruta

    def tool_habilitada(self, nombre: str) -> bool:
        return nombre in self._datos["tools_habilitadas"]

    def es_irreversible(self, nombre: str) -> bool:
        """Si es irreversible, el core exige confirmación humana antes de ejecutarla."""
        return nombre in ACCIONES_IRREVERSIBLES

    def zona(self) -> ZoneInfo:
        return ZoneInfo(self._datos["ejecutivo"]["zona_horaria"])

    def __repr__(self) -> str:
        return f"Config(device_id={self._datos['device_id']!r}, ruta={self._ruta!s})"


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------


def validar(datos: Any, ruta: Path | str = "<memoria>") -> Config:
    """Valida un dict ya parseado y devuelve la `Config`. Lanza `ErrorDeConfig`."""
    if not isinstance(datos, dict):
        raise ErrorDeConfig(
            ruta, [f"el archivo debe contener un objeto JSON, llegó {_tipo_recibido(datos)}"]
        )

    errores: list[str] = []
    limpio = _validar_bloque("", datos, ESQUEMA, errores)
    if errores:
        raise ErrorDeConfig(ruta, errores)

    errores = _validaciones_cruzadas(limpio)
    if errores:
        raise ErrorDeConfig(ruta, errores)

    return Config(limpio, Path(ruta) if not isinstance(ruta, Path) else ruta)


def cargar_config(ruta: str | os.PathLike[str] | None = None) -> Config:
    """Lee y valida `device.config.json`.

    Precedencia de la ruta: argumento > variable de entorno `DEVICE_CONFIG` >
    `device.config.json` en la raíz del proyecto.
    """
    if ruta is not None:
        destino = Path(ruta)
    else:
        del_entorno = os.environ.get("DEVICE_CONFIG")
        destino = Path(del_entorno) if del_entorno else RUTA_POR_DEFECTO

    if not destino.is_file():
        raise ErrorDeConfig(
            destino,
            [
                "el archivo no existe. Copia la plantilla y complétala: "
                f"copy {RUTA_EJEMPLO.name} {RUTA_POR_DEFECTO.name}"
            ],
        )

    try:
        crudo = json.loads(destino.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ErrorDeConfig(
            destino, [f"JSON malformado en la línea {e.lineno}, columna {e.colno}: {e.msg}"]
        ) from None
    except UnicodeDecodeError:
        raise ErrorDeConfig(destino, ["el archivo no está codificado en UTF-8"]) from None

    return validar(crudo, destino)
