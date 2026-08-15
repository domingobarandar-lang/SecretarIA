"""Pide el permiso del ejecutivo una vez y guarda la llave de largo plazo.

Esto se corre **una sola vez por dispositivo**, en un computador con navegador —
no en la placa, que no tiene pantalla. Lo que produce es un `refresh_token`: una
llave que no caduca y que la placa cambia sola por un permiso temporal cada vez
que lo necesita.

    python scripts/autorizar-google.py

Abre el navegador, el ejecutivo entra con **su** cuenta de Google y acepta. El
script imprime la línea que hay que pegar en el `.env` de la placa.

**El refresh token es la credencial más sensible del producto**: con ella se lee
el correo y el calendario del ejecutivo. No se sube a git (el `.env` está
excluido), no se manda por chat y no sale del dispositivo.
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.google_mcp import PERMISOS, URL_TOKEN  # noqa: E402

URL_AUTORIZAR = "https://accounts.google.com/o/oauth2/v2/auth"

_PAGINA = """<!doctype html><meta charset="utf-8">
<title>SecretarIA</title>
<body style="font-family:system-ui;max-width:34rem;margin:4rem auto;line-height:1.6">
<h1>{titulo}</h1><p>{detalle}</p><p>Ya puede cerrar esta pestaña.</p>
"""


class _Recepcion(http.server.BaseHTTPRequestHandler):
    """Recibe el código que Google devuelve por el navegador."""

    codigo: str | None = None
    error: str | None = None
    estado_esperado: str = ""
    listo: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - lo nombra la stdlib
        partes = urllib.parse.urlparse(self.path)
        parametros = urllib.parse.parse_qs(partes.query)

        estado = (parametros.get("state") or [""])[0]
        if estado != _Recepcion.estado_esperado:
            # Alguien mandó al navegador a esta dirección por su cuenta.
            _Recepcion.error = "el «state» no coincide; se descarta la respuesta"
        elif "error" in parametros:
            _Recepcion.error = parametros["error"][0]
        else:
            _Recepcion.codigo = (parametros.get("code") or [""])[0] or None
            if not _Recepcion.codigo:
                _Recepcion.error = "Google no devolvió ningún código"

        if _Recepcion.error:
            cuerpo = _PAGINA.format(
                titulo="No se pudo autorizar", detalle=_Recepcion.error
            )
        else:
            cuerpo = _PAGINA.format(
                titulo="Listo",
                detalle="SecretarIA quedó autorizada. Vuelva a la terminal.",
            )

        datos = cuerpo.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)
        _Recepcion.listo.set()

    def log_message(self, *_: object) -> None:
        """Silencio: la consola es para el ejecutivo, no para el log de un server."""


def url_de_consentimiento(client_id: str, redirect: str, estado: str) -> str:
    """`access_type=offline` + `prompt=consent` es lo que hace que venga el refresh token.

    Sin `prompt=consent`, una cuenta que ya autorizó antes recibe **solo** un
    permiso temporal y el script termina sin la llave que vino a buscar.
    """
    parametros = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(PERMISOS),
        "access_type": "offline",
        "prompt": "consent",
        "state": estado,
        "include_granted_scopes": "true",
    }
    return f"{URL_AUTORIZAR}?{urllib.parse.urlencode(parametros)}"


def canjear(client_id: str, client_secret: str, codigo: str, redirect: str) -> dict:
    import httpx

    respuesta = httpx.post(
        URL_TOKEN,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": codigo,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=30.0,
    )
    if respuesta.status_code != 200:
        raise SystemExit(
            f"Google rechazó el canje ({respuesta.status_code}): {respuesta.text[:400]}"
        )
    return respuesta.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autoriza SecretarIA en la cuenta del ejecutivo.")
    parser.add_argument("--client-id", help="si se omite, se lee de GOOGLE_OAUTH_CLIENT_ID")
    parser.add_argument("--client-secret", help="si se omite, se lee de GOOGLE_OAUTH_CLIENT_SECRET")
    parser.add_argument("--puerto", type=int, default=8765)
    argumentos = parser.parse_args(argv)

    import os

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass

    client_id = argumentos.client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = (
        argumentos.client_secret or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    )
    if not client_id or not client_secret:
        print(
            "Faltan las credenciales del cliente OAuth.\n"
            "  Póngalas en el .env como GOOGLE_OAUTH_CLIENT_ID y GOOGLE_OAUTH_CLIENT_SECRET,\n"
            "  o pásenlas con --client-id y --client-secret.",
            file=sys.stderr,
        )
        return 2

    redirect = f"http://localhost:{argumentos.puerto}/"
    estado = secrets.token_urlsafe(24)

    _Recepcion.estado_esperado = estado
    _Recepcion.listo = threading.Event()
    servidor = http.server.HTTPServer(("127.0.0.1", argumentos.puerto), _Recepcion)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()

    destino = url_de_consentimiento(client_id, redirect, estado)
    print("Permisos que se van a pedir:")
    for permiso in PERMISOS:
        print(f"  · {permiso.rsplit('/', 1)[-1]}")
    print()
    print(f"Esta dirección tiene que estar registrada como URI de redirección: {redirect}")
    print("Abriendo el navegador. Si no se abre, copie y pegue esto:")
    print(f"\n{destino}\n")
    webbrowser.open(destino)

    # Cinco minutos: alcanza para leer la pantalla de permisos sin apuro, y no
    # deja un puerto abierto toda la tarde si alguien se distrae.
    if not _Recepcion.listo.wait(timeout=300):
        print("Se agotó la espera (5 minutos) sin respuesta.", file=sys.stderr)
        return 1
    servidor.shutdown()

    if _Recepcion.error or not _Recepcion.codigo:
        print(f"No se autorizó: {_Recepcion.error}", file=sys.stderr)
        return 1

    datos = canjear(client_id, client_secret, _Recepcion.codigo, redirect)
    refresh = datos.get("refresh_token")
    if not refresh:
        print(
            "Google no devolvió refresh_token. Suele pasar cuando la cuenta ya había\n"
            "autorizado esta app: revoque el acceso en https://myaccount.google.com/permissions\n"
            "y vuelva a correr esto.",
            file=sys.stderr,
        )
        return 1

    concedidos = set((datos.get("scope") or "").split())
    faltantes = [p for p in PERMISOS if p not in concedidos]

    print("\n" + "=" * 70)
    print("Pegue esta línea en el .env de la placa:\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh}")
    print("=" * 70)

    if faltantes:
        # No es fatal: el ejecutivo pudo desmarcar una casilla. Pero hay que
        # decirlo ahora y con nombre, no cuando falle una herramienta.
        print("\nOJO: quedaron permisos sin conceder. Estas cosas no van a funcionar:")
        for permiso in faltantes:
            corto = permiso.rsplit("/", 1)[-1]
            print(f"  · {corto}" + ("   <- crear eventos" if corto == "calendar.events" else ""))
        return 1

    print("\nTodos los permisos concedidos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
