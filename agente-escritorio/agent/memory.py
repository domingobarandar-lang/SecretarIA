"""Memoria del agente en SQLite local.

Tres tablas, tres propósitos distintos:

- `conversaciones`: el hilo de mensajes por sesión. De acá sale la ventana
  deslizante que se le manda al modelo.
- `hechos`: lo que el ejecutivo prefiere y no hay que volver a preguntar
  ("no agenda los viernes en la tarde"). Sobrevive a la sesión: es lo que
  guarda la tool `recordar`.
- `acciones`: auditoría. Qué tool corrió, con qué input, qué devolvió y si
  hubo confirmación humana. Es requisito de cumplimiento (Ley 21.719 fiscaliza
  evidencia operativa, no políticas), no un extra: no se apaga.

La base vive en el dispositivo. Nada de esto sale de la caja.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RAIZ = Path(__file__).resolve().parent.parent
RUTA_POR_DEFECTO = RAIZ / "datos" / "memoria.db"

ROLES = ("usuario", "asistente", "herramienta", "sistema")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS conversaciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    rol         TEXT    NOT NULL CHECK (rol IN ('usuario','asistente','herramienta','sistema')),
    contenido   TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    tokens      INTEGER NOT NULL DEFAULT 0 CHECK (tokens >= 0)
);
CREATE INDEX IF NOT EXISTS ix_conversaciones_sesion
    ON conversaciones (session_id, id);

CREATE TABLE IF NOT EXISTS hechos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    categoria   TEXT    NOT NULL,
    contenido   TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    fuente      TEXT    NOT NULL
);
-- Repetir un hecho refresca su fecha; no lo duplica.
CREATE UNIQUE INDEX IF NOT EXISTS ux_hechos_categoria_contenido
    ON hechos (categoria, contenido);

CREATE TABLE IF NOT EXISTS acciones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    input       TEXT    NOT NULL,
    resultado   TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    confirmada  INTEGER NOT NULL DEFAULT 0 CHECK (confirmada IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_acciones_sesion
    ON acciones (session_id, id);
"""


def _ahora() -> str:
    """Instante actual en ISO 8601 UTC. Texto ordenable, sin ambigüedad de huso."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Mensaje:
    id: int
    session_id: str
    rol: str
    contenido: str
    timestamp: str
    tokens: int


@dataclass(frozen=True)
class Hecho:
    id: int
    categoria: str
    contenido: str
    timestamp: str
    fuente: str


@dataclass(frozen=True)
class Accion:
    id: int
    session_id: str
    tool: str
    entrada: dict[str, Any]
    resultado: str
    timestamp: str
    confirmada: bool


class Memoria:
    """Acceso a la base local. Una instancia por sesión de conversación."""

    def __init__(
        self,
        ruta: str | os.PathLike[str] | None = None,
        session_id: str | None = None,
    ) -> None:
        if ruta is None:
            del_entorno = os.environ.get("RUTA_MEMORIA")
            ruta = del_entorno if del_entorno else RUTA_POR_DEFECTO

        self.ruta = str(ruta)
        en_memoria = self.ruta == ":memory:" or self.ruta.startswith("file::memory:")
        if not en_memoria:
            Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)

        if sqlite3.sqlite_version_info < (3, 35):
            raise RuntimeError(
                "se necesita SQLite 3.35 o superior (por «RETURNING»); "
                f"este Python trae {sqlite3.sqlite_version}"
            )

        self._con = sqlite3.connect(self.ruta, timeout=5.0, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        if not en_memoria:
            # WAL: lecturas del panel sin bloquear al agente que escribe.
            self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.executescript(_ESQUEMA)

        self.session_id = session_id or uuid.uuid4().hex

    # -- ciclo de vida ----------------------------------------------------

    def nueva_sesion(self, session_id: str | None = None) -> str:
        """Empieza otra conversación en la misma base. Devuelve el nuevo id."""
        self.session_id = session_id or uuid.uuid4().hex
        return self.session_id

    def cerrar(self) -> None:
        self._con.close()

    def __enter__(self) -> "Memoria":
        return self

    def __exit__(self, *_excepcion: object) -> None:
        self.cerrar()

    # -- conversaciones ---------------------------------------------------

    def guardar_mensaje(
        self,
        rol: str,
        contenido: str,
        tokens: int = 0,
        session_id: str | None = None,
    ) -> int:
        """Agrega un mensaje al hilo. `contenido` puede ser texto o JSON serializado."""
        if rol not in ROLES:
            raise ValueError(f"rol inválido «{rol}»; esperaba uno de: {', '.join(ROLES)}")
        cursor = self._con.execute(
            "INSERT INTO conversaciones (session_id, rol, contenido, timestamp, tokens)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id or self.session_id, rol, contenido, _ahora(), int(tokens)),
        )
        return int(cursor.lastrowid)

    def historial(
        self, limite: int | None = None, session_id: str | None = None
    ) -> list[Mensaje]:
        """Los últimos `limite` mensajes de la sesión, en orden cronológico.

        La ventana deslizante se pide acá, pero **quién decide dónde cortar es el
        core**: cortar entre un `tool_use` y su `tool_result` deja al modelo con
        una llamada sin respuesta.
        """
        sql = (
            "SELECT * FROM conversaciones WHERE session_id = ? ORDER BY id DESC"
        )
        parametros: list[Any] = [session_id or self.session_id]
        if limite is not None:
            if limite < 0:
                raise ValueError("el límite no puede ser negativo")
            sql += " LIMIT ?"
            parametros.append(limite)
        filas = self._con.execute(sql, parametros).fetchall()
        return [_a_mensaje(f) for f in reversed(filas)]

    def tokens_sesion(self, session_id: str | None = None) -> int:
        fila = self._con.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS total FROM conversaciones"
            " WHERE session_id = ?",
            (session_id or self.session_id,),
        ).fetchone()
        return int(fila["total"])

    # -- hechos -----------------------------------------------------------

    def guardar_hecho(
        self, contenido: str, categoria: str = "general", fuente: str = "recordar"
    ) -> int:
        """Guarda una preferencia del ejecutivo. Repetirla refresca la fecha."""
        if not contenido.strip():
            raise ValueError("el hecho no puede estar vacío")
        limpio = contenido.strip()
        # `lastrowid` no sirve acá: en un upsert que termina en UPDATE, SQLite deja
        # el rowid del último INSERT real. El id se pregunta.
        fila = self._con.execute(
            "INSERT INTO hechos (categoria, contenido, timestamp, fuente)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (categoria, contenido) DO UPDATE SET"
            "   timestamp = excluded.timestamp, fuente = excluded.fuente"
            " RETURNING id",
            (categoria, limpio, _ahora(), fuente),
        ).fetchone()
        return int(fila["id"])

    def hechos(self, categoria: str | None = None, limite: int = 100) -> list[Hecho]:
        """Los hechos más recientes primero. Es lo que se le inyecta al prompt."""
        if categoria is None:
            filas = self._con.execute(
                "SELECT * FROM hechos ORDER BY id DESC LIMIT ?", (limite,)
            ).fetchall()
        else:
            filas = self._con.execute(
                "SELECT * FROM hechos WHERE categoria = ? ORDER BY id DESC LIMIT ?",
                (categoria, limite),
            ).fetchall()
        return [_a_hecho(f) for f in filas]

    # -- acciones (auditoría) --------------------------------------------

    def registrar_accion(
        self,
        tool: str,
        entrada: dict[str, Any] | None,
        resultado: str,
        confirmada: bool = False,
        session_id: str | None = None,
    ) -> int:
        """Deja constancia de una tool ejecutada. Se llama pase lo que pase."""
        cursor = self._con.execute(
            "INSERT INTO acciones (session_id, tool, input, resultado, timestamp, confirmada)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id or self.session_id,
                tool,
                json.dumps(entrada or {}, ensure_ascii=False, sort_keys=True, default=str),
                resultado,
                _ahora(),
                1 if confirmada else 0,
            ),
        )
        return int(cursor.lastrowid)

    def acciones(
        self, limite: int = 50, session_id: str | None = None, solo_sesion: bool = False
    ) -> list[Accion]:
        """Las acciones más recientes primero. Por defecto, de todas las sesiones."""
        if solo_sesion or session_id is not None:
            filas = self._con.execute(
                "SELECT * FROM acciones WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id or self.session_id, limite),
            ).fetchall()
        else:
            filas = self._con.execute(
                "SELECT * FROM acciones ORDER BY id DESC LIMIT ?", (limite,)
            ).fetchall()
        return [_a_accion(f) for f in filas]

    # -- observabilidad ---------------------------------------------------

    def resumen_sesion(self, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or self.session_id
        mensajes = self._con.execute(
            "SELECT COUNT(*) AS n FROM conversaciones WHERE session_id = ?", (sid,)
        ).fetchone()["n"]
        acciones = self._con.execute(
            "SELECT COUNT(*) AS n FROM acciones WHERE session_id = ?", (sid,)
        ).fetchone()["n"]
        return {
            "session_id": sid,
            "mensajes": int(mensajes),
            "acciones": int(acciones),
            "tokens": self.tokens_sesion(sid),
        }


def _a_mensaje(fila: sqlite3.Row) -> Mensaje:
    return Mensaje(
        id=fila["id"],
        session_id=fila["session_id"],
        rol=fila["rol"],
        contenido=fila["contenido"],
        timestamp=fila["timestamp"],
        tokens=fila["tokens"],
    )


def _a_hecho(fila: sqlite3.Row) -> Hecho:
    return Hecho(
        id=fila["id"],
        categoria=fila["categoria"],
        contenido=fila["contenido"],
        timestamp=fila["timestamp"],
        fuente=fila["fuente"],
    )


def _a_accion(fila: sqlite3.Row) -> Accion:
    try:
        entrada = json.loads(fila["input"])
    except json.JSONDecodeError:
        entrada = {"_crudo": fila["input"]}
    return Accion(
        id=fila["id"],
        session_id=fila["session_id"],
        tool=fila["tool"],
        entrada=entrada if isinstance(entrada, dict) else {"_valor": entrada},
        resultado=fila["resultado"],
        timestamp=fila["timestamp"],
        confirmada=bool(fila["confirmada"]),
    )


def formatear_hechos(hechos: Iterable[Hecho]) -> str:
    """Los hechos como bloque de texto para el system prompt. Vacío si no hay."""
    lineas = [f"- [{h.categoria}] {h.contenido}" for h in hechos]
    return "\n".join(lineas)
