"""La fuente real: calendario y correo del ejecutivo por los MCP de Google.

Implementa la misma interfaz `FuenteDatos` que `FuenteMock`. El core, el prompt y
las cinco tools no se enteran del cambio: se construye el agente con otra fuente.

## Por qué el modelo no habla MCP directamente

La API de Gemini trae un tipo de tool `mcp_server`: se le pasa la URL y el modelo
llama al servidor solo. **No se usa acá, y es la decisión de diseño más importante
de este archivo.**

El MCP de Calendar publica ocho herramientas, entre ellas `delete_event` y
`update_event`. Entregarle ese catálogo al modelo significaría que el agente puede
borrar reuniones del ejecutivo: por fuera de las cinco tools declaradas, por fuera
de la confirmación humana del core y por fuera de la tabla `acciones`. Las tres
garantías del producto se saltarían de una sola vez, y ninguna prueba funcional lo
notaría — el agente respondería perfecto hasta el día que borre algo.

Acá el MCP es una **fuente de datos detrás del contrato**, llamada por nuestro
código. `_METODOS_PERMITIDOS` lo hace estructural y no una promesa: pedir un método
que no esté en esa lista es un error de programación, no una llamada de red.

## Lo que este archivo todavía no puede afirmar

Los nombres de herramientas, sus parámetros y sus respuestas salen de la referencia
oficial de Google, **no de una corrida contra el servidor**: los MCP de Workspace
están en Developer Preview y hacen falta credenciales OAuth del ejecutivo, que son
🔴 (ver la constitución de la fábrica). Por eso `conectar()` hace un `tools/list` y
compara contra lo que este archivo espera: si Google cambió algo, el agente **no
arranca** y dice exactamente qué no calza. Un desajuste que no se ve al arrancar se
ve cuando alguien pregunta por su agenda, y ahí ya es tarde.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from agent.tools import Correo, ErrorDeHerramienta, Evento

log = logging.getLogger("secretaria.google")

URL_TOKEN = "https://oauth2.googleapis.com/token"

URLS_POR_DEFECTO = {
    "calendar": "https://calendarmcp.googleapis.com/mcp/v1",
    "gmail": "https://gmailmcp.googleapis.com/mcp/v1",
}

#: Versión del protocolo MCP que este cliente habla.
PROTOCOLO = "2025-06-18"

#: Lo único que SecretarIA tiene permitido pedirle a cada servidor. Es una lista
#: de dos por servidor contra los ocho y nueve que publican: leer y crear, nada
#: que destruya. `_llamar` la hace cumplir antes de tocar la red.
_METODOS_PERMITIDOS: dict[str, tuple[str, ...]] = {
    "calendar": ("list_events", "create_event"),
    "gmail": ("search_threads", "create_draft"),
}

#: Parámetros que este archivo le manda a cada herramienta. `conectar()` los cruza
#: contra el esquema que declara el servidor: si un nombre cambió, se sabe al
#: arrancar y no en medio de una conversación.
_PARAMETROS_QUE_USAMOS: dict[str, tuple[str, ...]] = {
    "list_events": ("startTime", "endTime", "timeZone", "orderBy", "pageSize"),
    "create_event": ("summary", "startTime", "endTime", "timeZone", "description", "attendees"),
    "search_threads": ("query", "pageSize", "view"),
    "create_draft": ("to", "subject", "body"),
}

#: Drive no está. No hay ninguna de las cinco tools que lo toque, y un servidor
#: conectado «por si acaso» es superficie de ataque sin caso de uso.
SERVIDORES = tuple(_METODOS_PERMITIDOS)


class ErrorDeGoogle(Exception):
    """No se pudo hablar con Google. El core lo convierte en un error de herramienta."""


# --------------------------------------------------------------------------
# Credenciales — viven en el dispositivo y no salen de acá
# --------------------------------------------------------------------------


@dataclass
class Credenciales:
    """El refresh token del ejecutivo, guardado en la placa.

    Se cambia por un access token cada vez que caduca. El refresh token nunca
    viaja a otro lado que no sea el endpoint de tokens de Google.
    """

    client_id: str
    client_secret: str
    refresh_token: str

    _token: str = field(default="", repr=False)
    _vence: float = field(default=0.0, repr=False)

    @classmethod
    def desde_entorno(cls) -> "Credenciales":
        faltan = [
            v
            for v in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")
            if not os.environ.get(v, "").strip()
        ]
        if faltan:
            raise ErrorDeGoogle(
                f"Faltan credenciales de Google en el entorno: {', '.join(faltan)}. "
                "Ver .env.example."
            )
        return cls(
            client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"].strip(),
            client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"].strip(),
            refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"].strip(),
        )

    def access_token(self, http: Any, *, ahora: float | None = None) -> str:
        momento = time.monotonic() if ahora is None else ahora
        if self._token and momento < self._vence:
            return self._token

        respuesta = http.post(
            URL_TOKEN,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if respuesta.status_code != 200:
            raise ErrorDeGoogle(
                f"Google rechazó el refresh token ({respuesta.status_code}). "
                "Hay que volver a autorizar el dispositivo."
            )
        datos = respuesta.json()
        self._token = datos.get("access_token", "")
        if not self._token:
            raise ErrorDeGoogle("Google devolvió una respuesta sin access_token")
        # 60 segundos de colchón: un token que vence a mitad de request es un
        # 401 que el ejecutivo lee como «se cayó».
        self._vence = momento + max(0, int(datos.get("expires_in", 3600)) - 60)
        return self._token


# --------------------------------------------------------------------------
# Transporte MCP — JSON-RPC sobre HTTP
# --------------------------------------------------------------------------


def _cuerpo_de(respuesta: Any) -> dict[str, Any]:
    """Lee la respuesta, venga como JSON o como un stream de eventos.

    El transporte «streamable HTTP» del MCP puede contestar `application/json` o
    `text/event-stream` para el mismo request, a elección del servidor. Aceptar
    solo uno funciona hasta el día que el servidor cambia de humor.
    """
    tipo = (respuesta.headers.get("content-type") or "").lower()
    if "text/event-stream" not in tipo:
        return respuesta.json()

    for linea in respuesta.text.splitlines():
        if linea.startswith("data:"):
            crudo = linea[len("data:") :].strip()
            if crudo and crudo != "[DONE]":
                return json.loads(crudo)
    raise ErrorDeGoogle("el servidor MCP devolvió un stream sin datos")


class ClienteMCP:
    """Un servidor MCP: handshake, catálogo y llamadas. Sincrónico a propósito.

    El core es sincrónico y el REPL también. Meter asyncio acá obligaría a
    envolver cada llamada en un `asyncio.run` o a teñir de `async` el proyecto
    entero, y no compra nada: el agente hace una llamada a la vez y espera.
    """

    def __init__(self, nombre: str, url: str, credenciales: Credenciales, http: Any) -> None:
        self.nombre = nombre
        self.url = url
        self._credenciales = credenciales
        self._http = http
        self._sesion: str | None = None
        self._siguiente_id = 0
        self._candado = threading.Lock()
        self.catalogo: dict[str, dict[str, Any]] = {}

    # -- protocolo --------------------------------------------------------

    def _encabezados(self) -> dict[str, str]:
        cabeceras = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._credenciales.access_token(self._http)}",
            "MCP-Protocol-Version": PROTOCOLO,
        }
        if self._sesion:
            cabeceras["Mcp-Session-Id"] = self._sesion
        return cabeceras

    def _rpc(self, metodo: str, parametros: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._candado:
            self._siguiente_id += 1
            identificador = self._siguiente_id

        peticion = {"jsonrpc": "2.0", "id": identificador, "method": metodo}
        if parametros is not None:
            peticion["params"] = parametros

        respuesta = self._http.post(self.url, headers=self._encabezados(), json=peticion)

        sesion = respuesta.headers.get("Mcp-Session-Id")
        if sesion:
            self._sesion = sesion

        if respuesta.status_code >= 400:
            raise ErrorDeGoogle(
                f"{self.nombre} respondió {respuesta.status_code} a «{metodo}»: "
                f"{respuesta.text[:300]}"
            )

        cuerpo = _cuerpo_de(respuesta)
        if "error" in cuerpo:
            detalle = cuerpo["error"]
            raise ErrorDeGoogle(
                f"{self.nombre} rechazó «{metodo}»: "
                f"{detalle.get('message', detalle)}"
            )
        return cuerpo.get("result") or {}

    def _notificar(self, metodo: str) -> None:
        self._http.post(
            self.url,
            headers=self._encabezados(),
            json={"jsonrpc": "2.0", "method": metodo},
        )

    def conectar(self) -> None:
        """Handshake + catálogo + verificación del contrato."""
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOLO,
                "capabilities": {},
                "clientInfo": {"name": "secretaria", "version": "1.0"},
            },
        )
        self._notificar("notifications/initialized")

        catalogo = self._rpc("tools/list")
        self.catalogo = {t["name"]: t for t in catalogo.get("tools", []) if t.get("name")}
        self._verificar_contrato()

    def _verificar_contrato(self) -> None:
        """Que el servidor tenga lo que este archivo cree que tiene.

        Falla al arrancar y con nombres concretos. La alternativa es descubrirlo
        cuando alguien pregunta por su agenda y recibe una disculpa genérica.
        """
        problemas: list[str] = []
        for metodo in _METODOS_PERMITIDOS[self.nombre]:
            declarada = self.catalogo.get(metodo)
            if declarada is None:
                problemas.append(
                    f"«{metodo}» no existe en {self.nombre} "
                    f"(hay: {', '.join(sorted(self.catalogo)) or 'ninguna'})"
                )
                continue
            esquema = (declarada.get("inputSchema") or {}).get("properties") or {}
            if not esquema:
                continue  # el servidor no publica esquema: no hay nada que cruzar
            desconocidos = [p for p in _PARAMETROS_QUE_USAMOS[metodo] if p not in esquema]
            if desconocidos:
                problemas.append(
                    f"«{metodo}» ya no acepta: {', '.join(desconocidos)} "
                    f"(acepta: {', '.join(sorted(esquema))})"
                )
        if problemas:
            raise ErrorDeGoogle(
                f"El MCP de {self.nombre} no calza con lo que SecretarIA espera "
                f"({len(problemas)}):\n  - " + "\n  - ".join(problemas)
            )

    # -- llamadas ---------------------------------------------------------

    def llamar(self, metodo: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        if metodo not in _METODOS_PERMITIDOS[self.nombre]:
            # No es un error del usuario ni del modelo: es código pidiendo algo
            # que este producto decidió no poder hacer.
            raise AssertionError(
                f"«{metodo}» no está en la lista permitida de {self.nombre}: "
                f"{_METODOS_PERMITIDOS[self.nombre]}"
            )

        resultado = self._rpc("tools/call", {"name": metodo, "arguments": argumentos})

        if resultado.get("isError"):
            raise ErrorDeGoogle(f"{self.nombre}.{metodo}: {_texto_del_resultado(resultado)}")

        estructurado = resultado.get("structuredContent")
        if isinstance(estructurado, dict):
            return estructurado

        crudo = _texto_del_resultado(resultado)
        try:
            devuelto = json.loads(crudo) if crudo else {}
        except json.JSONDecodeError:
            raise ErrorDeGoogle(
                f"{self.nombre}.{metodo} devolvió algo que no es JSON: {crudo[:200]}"
            ) from None
        return devuelto if isinstance(devuelto, dict) else {"resultado": devuelto}


def _texto_del_resultado(resultado: dict[str, Any]) -> str:
    partes = [
        b.get("text", "")
        for b in resultado.get("content") or []
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(p for p in partes if p)


# --------------------------------------------------------------------------
# Traducción: lo que devuelve Google -> los objetos del dominio
# --------------------------------------------------------------------------


def _momento(valor: Any, zona: ZoneInfo) -> datetime:
    """`{"dateTime": ...}` o `{"date": ...}` de Google -> datetime con zona.

    Un evento de día completo no trae hora. Se toma medianoche en la zona del
    ejecutivo: se dibuja mal en una lista, pero no rompe la comparación de
    conflictos, que es lo que el core hace con esto.
    """
    if isinstance(valor, dict):
        crudo = valor.get("dateTime") or valor.get("date") or ""
    else:
        crudo = valor or ""
    if not crudo:
        raise ErrorDeGoogle(f"Google devolvió un evento sin fecha usable: {valor!r}")

    momento = datetime.fromisoformat(str(crudo).replace("Z", "+00:00"))
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=zona)
    return momento.astimezone(zona)


def a_evento(crudo: dict[str, Any], zona: ZoneInfo) -> Evento:
    invitados = tuple(
        a.get("email", "")
        for a in crudo.get("attendees") or []
        if isinstance(a, dict) and a.get("email")
    )
    return Evento(
        id=crudo.get("id", ""),
        titulo=crudo.get("summary") or "(sin título)",
        inicio=_momento(crudo.get("start"), zona),
        fin=_momento(crudo.get("end"), zona),
        invitados=invitados,
        descripcion=crudo.get("description") or "",
    )


def a_correos(crudo: dict[str, Any], zona: ZoneInfo, maximo: int) -> list[Correo]:
    """Gmail devuelve hilos; el ejecutivo piensa en mensajes.

    Se toma el **último** mensaje de cada hilo, que es el que motiva la pregunta
    («¿qué me escribió finanzas?» se responde con lo último, no con el arranque
    de una cadena de quince).
    """
    correos: list[Correo] = []
    for hilo in crudo.get("threads") or []:
        mensajes = [m for m in hilo.get("messages") or [] if isinstance(m, dict)]
        if not mensajes:
            continue
        ultimo = mensajes[-1]
        try:
            fecha = _momento(ultimo.get("date"), zona)
        except (ErrorDeGoogle, ValueError):
            continue  # un mensaje sin fecha legible no tumba la búsqueda entera
        correos.append(
            Correo(
                id=ultimo.get("id") or hilo.get("id", ""),
                remitente=ultimo.get("sender") or "(desconocido)",
                asunto=ultimo.get("subject") or "(sin asunto)",
                fecha=fecha,
                extracto=(ultimo.get("snippet") or "").strip(),
            )
        )
    correos.sort(key=lambda c: c.fecha, reverse=True)
    return correos[:maximo]


# --------------------------------------------------------------------------
# La fuente
# --------------------------------------------------------------------------


@dataclass
class FuenteGoogle:
    """`FuenteDatos` contra los MCP de Google. Misma interfaz que `FuenteMock`."""

    zona: ZoneInfo
    calendar: ClienteMCP
    gmail: ClienteMCP

    def ver_agenda(self, inicio: datetime, fin: datetime) -> list[Evento]:
        datos = self.calendar.llamar(
            "list_events",
            {
                "startTime": inicio.isoformat(),
                "endTime": fin.isoformat(),
                "timeZone": str(self.zona),
                "orderBy": "startTime",
                "pageSize": 100,
            },
        )
        eventos = []
        for crudo in datos.get("events") or []:
            try:
                eventos.append(a_evento(crudo, self.zona))
            except ErrorDeGoogle as problema:
                log.warning("evento ilegible, se omite: %s", problema)
        return sorted(eventos, key=lambda e: e.inicio)

    def crear_evento(self, evento: Evento) -> Evento:
        argumentos: dict[str, Any] = {
            "summary": evento.titulo,
            "startTime": evento.inicio.isoformat(),
            "endTime": evento.fin.isoformat(),
            "timeZone": str(self.zona),
        }
        if evento.descripcion:
            argumentos["description"] = evento.descripcion
        if evento.invitados:
            argumentos["attendees"] = [{"email": c} for c in evento.invitados]

        creado = self.calendar.llamar("create_event", argumentos)
        try:
            return a_evento(creado, self.zona)
        except ErrorDeGoogle:
            # El evento se creó; lo que no se pudo fue releer la respuesta. Decir
            # que falló haría que el ejecutivo lo cree de nuevo y quede duplicado.
            log.warning("evento creado pero la respuesta vino incompleta: %r", creado)
            return Evento(
                id=creado.get("id", "(sin id)"),
                titulo=evento.titulo,
                inicio=evento.inicio,
                fin=evento.fin,
                invitados=evento.invitados,
                descripcion=evento.descripcion,
            )

    def buscar_correos(self, consulta: str, maximo: int) -> list[Correo]:
        datos = self.gmail.llamar(
            "search_threads",
            {
                "query": consulta,
                # El servidor tope 50; el contrato de la tool tope 25.
                "pageSize": max(1, min(maximo, 25)),
                "view": "THREAD_VIEW_MINIMAL",
            },
        )
        return a_correos(datos, self.zona, maximo)

    def redactar_borrador(self, destinatario: str, asunto: str, cuerpo: str) -> str:
        """Crea un borrador. **Nunca envía** — y acá eso ya no depende de nosotros.

        El MCP de Gmail no publica ninguna herramienta de envío: `create_draft` sí,
        `send` no existe. La garantía del producto quedó respaldada por el
        transporte, no solo por nuestra disciplina.
        """
        creado = self.gmail.llamar(
            "create_draft",
            {"to": [destinatario], "subject": asunto, "body": cuerpo},
        )
        identificador = creado.get("id")
        if not identificador:
            raise ErrorDeHerramienta(
                "Gmail aceptó el borrador pero no devolvió su identificador; "
                "conviene revisarlo en la carpeta de borradores."
            )
        return str(identificador)


def conectar(zona: ZoneInfo, *, http: Any = None, credenciales: Credenciales | None = None) -> FuenteGoogle:
    """Arma la fuente real. Falla acá, al arrancar, o no falla nunca en silencio."""
    if http is None:  # pragma: no cover - depende de la red
        import httpx

        http = httpx.Client(timeout=30.0)

    llaves = credenciales or Credenciales.desde_entorno()
    clientes = {}
    for nombre in SERVIDORES:
        url = os.environ.get(f"GOOGLE_MCP_{nombre.upper()}", "").strip() or URLS_POR_DEFECTO[nombre]
        cliente = ClienteMCP(nombre, url, llaves, http)
        cliente.conectar()
        clientes[nombre] = cliente
        log.info("%s conectado (%s tools declaradas)", nombre, len(cliente.catalogo))

    return FuenteGoogle(zona=zona, calendar=clientes["calendar"], gmail=clientes["gmail"])
