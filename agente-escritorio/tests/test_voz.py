"""Pruebas de la voz. Ninguna abre un micrófono ni carga un modelo.

Lo que se prueba acá es lo que se puede equivocar en silencio: el corte del
turno, el reconocimiento del nombre y la limpieza del Markdown. Que Piper suene
bien o que Whisper entienda un acento no se prueba con pytest — se prueba con
`python main_voz.py --probar-voz` y una persona escuchando.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.config import ErrorDeConfig, validar
from tests.conftest import datos_config
from voz import ErrorDeVoz
from voz.audio import MS_TRAMA, MUESTRAS_TRAMA
from voz.despertar import buscar_nombre, normalizar
from voz.escucha import Escucha
from voz.stt import _idioma_corto
from voz.tts import Locutor, _limpiar

RAIZ = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# La dependencia va en un solo sentido
# --------------------------------------------------------------------------


def test_el_cerebro_no_importa_la_voz():
    """`agent/` no sabe que `voz/` existe, y esa asimetría es la garantía.

    Es lo que hace que `main.py` siga arrancando en una caja sin micrófono y que
    la suite del core corra sin Piper, sin Whisper y sin los 63 MB del modelo. Un
    `import voz` en el core no rompería ningún test funcional el día que se
    escriba: rompería el arranque de la caja de un cliente que no tiene audio.
    """
    culpables = []
    for archivo in sorted((RAIZ / "agent").glob("*.py")) + [RAIZ / "main.py"]:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                nombres = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom):
                nombres = [nodo.module or ""]
            else:
                continue
            if any(n == "voz" or n.startswith("voz.") for n in nombres):
                culpables.append(f"{archivo.name}:{nodo.lineno}")

    assert not culpables, (
        "el core importa de voz/ en " + ", ".join(culpables) + ". "
        "La dependencia va en un solo sentido: la voz usa al agente, no al revés."
    )


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------


def test_una_config_sin_bloque_voz_sigue_siendo_valida():
    """Retrocompatible: las cajas ya instaladas no tienen «voz» en su archivo."""
    config = validar(datos_config())
    assert config.voz.habilitada is False
    assert config.voz.modelo == "es_MX-claude-high"
    assert config.voz.dispositivo_entrada is None


def test_la_voz_viene_apagada_por_defecto():
    """Encenderla es un acto deliberado: hay cajas sin micrófono ni parlante."""
    assert validar(datos_config()).voz.habilitada is False


@pytest.mark.parametrize(
    "bloque, esperado",
    [
        ({"velocidad": 999}, "entre 50 y 200"),
        ({"modelo": "voz-rara"}, "formato inválido"),
        ({"modelo_stt": "gigante"}, "debe ser uno de"),
        ({"dispositivo_entrada": "primero"}, "número entero"),
        ({"silencio_fin_turno_ms": 10}, "entre 300 y 5000"),
    ],
)
def test_el_bloque_voz_rechaza_valores_imposibles(bloque, esperado):
    with pytest.raises(ErrorDeConfig) as fallo:
        validar(datos_config(voz=bloque))
    assert esperado in str(fallo.value)


def test_una_llave_mal_escrita_sugiere_la_correcta():
    """Un typo silencioso sería un micrófono que nadie selecciona nunca."""
    with pytest.raises(ErrorDeConfig) as fallo:
        validar(datos_config(voz={"dispositivo_entra": 1}))
    assert "¿quisiste decir «dispositivo_entrada»?" in str(fallo.value)


# --------------------------------------------------------------------------
# El nombre: ¿le hablaron al agente?
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dicho, orden",
    [
        ("Vera, ¿qué tengo mañana?", "¿qué tengo mañana?"),
        ("vera que tengo hoy", "que tengo hoy"),
        ("Hola Vera, agenda una reunión", "agenda una reunión"),
        ("Oye Vera", ""),
        ("Vera.", ""),
        # Whisper escribe los nombres propios como le suenan. Estos tres son
        # transcripciones reales del mismo «Vera» dicho a un micrófono barato.
        ("Bera, ¿qué tengo hoy?", "¿qué tengo hoy?"),
        ("Vela, cancela eso", "cancela eso"),
        ("Verá, revisa el correo", "revisa el correo"),
    ],
)
def test_reconoce_el_nombre_y_devuelve_la_orden(dicho, orden):
    llamado, resto = buscar_nombre(dicho, "Vera")
    assert llamado is True
    assert resto == orden


@pytest.mark.parametrize(
    "dicho",
    [
        "",
        "qué tengo mañana",
        "la cena estuvo buena",
        "pásame el informe por favor",
        # El nombre aparece, pero tarde: es una conversación entre dos personas
        # sobre el agente, no una orden. Meterse ahí sería peor que no oír.
        "ayer le pregunté a la asistente Vera por la agenda",
    ],
)
def test_no_despierta_cuando_no_lo_llamaron(dicho):
    llamado, resto = buscar_nombre(dicho, "Vera")
    assert llamado is False
    assert resto == ""


def test_el_nombre_sale_de_la_config_no_del_codigo():
    """Cambiar de cliente es cambiar el archivo. También para el wake word."""
    assert buscar_nombre("Sofía, agenda algo", "Sofía") == (True, "agenda algo")
    assert buscar_nombre("Sofía, agenda algo", "Vera")[0] is False


def test_la_orden_conserva_tildes_y_signos():
    """Lo que sigue va derecho al modelo: los signos son parte de la pregunta."""
    _, resto = buscar_nombre("Vera, ¿cuándo es la reunión?", "Vera")
    assert resto == "¿cuándo es la reunión?"


def test_normalizar_saca_tildes_y_mayusculas():
    assert normalizar("¿SÍ?") == "¿si?"
    assert normalizar("Confirmo.") == "confirmo."


# --------------------------------------------------------------------------
# El corte del turno
# --------------------------------------------------------------------------


class AudioFalso:
    """Un micrófono de mentira: entrega las tramas que se le digan."""

    def __init__(self, cuantas: int) -> None:
        self.tramas_pedidas = 0
        self._cuantas = cuantas

    def tramas(self):
        for _ in range(self._cuantas):
            self.tramas_pedidas += 1
            yield b"\x00" * (MUESTRAS_TRAMA * 2)


class DetectorFalso:
    """Dice «hay voz» según un guion escrito a mano, trama por trama."""

    def __init__(self, guion: str) -> None:
        self._guion = guion
        self.indice = 0

    def is_speech(self, _trama, _frecuencia) -> bool:
        letra = self._guion[self.indice] if self.indice < len(self._guion) else "."
        self.indice += 1
        return letra == "V"


def _escucha(guion: str, **cambios):
    # 300 ms es el mínimo que acepta el esquema: 15 tramas de 20 ms.
    config = validar(datos_config(voz={"silencio_fin_turno_ms": 300, **cambios}))
    audio = AudioFalso(cuantas=len(guion) + 50)
    return Escucha(audio, config.voz, detector=DetectorFalso(guion)), audio


def test_el_turno_cierra_tras_el_silencio_configurado():
    """200 ms de silencio = 10 tramas de 20 ms. Ni antes ni mucho después."""
    escucha, _ = _escucha("...VVVVVVVVVV" + "." * 20)
    pcm = escucha.turno()
    assert pcm is not None
    # 13 tramas de voz y pre-roll + las 15 de silencio que cerraron el turno.
    assert len(pcm) // (MUESTRAS_TRAMA * 2) == pytest.approx(28, abs=3)


def test_un_ruido_suelto_no_abre_un_turno():
    """Una trama con voz es un portazo. Hacen falta tres seguidas."""
    escucha, _ = _escucha("V.V.V." + "." * 40)
    assert escucha.turno(espera_max_seg=0.001) is None


def test_el_pre_roll_conserva_lo_anterior_a_la_primera_silaba():
    """Sin esto «Vera» se transcribe «era» y el agente no despierta jamás."""
    escucha, _ = _escucha("." * 20 + "VVVVV" + "." * 20)
    pcm = escucha.turno()
    tramas = len(pcm) // (MUESTRAS_TRAMA * 2)
    assert tramas > 5 + 10, "se perdió el audio previo al inicio de la voz"


def test_el_turno_se_corta_en_el_tope_aunque_no_haya_silencio():
    """Un ruido continuo no puede dejar al agente grabando para siempre."""
    escucha, _ = _escucha("V" * 400, max_segundos_escucha=3)
    pcm = escucha.turno()
    tope = 3 * 1000 // MS_TRAMA
    assert len(pcm) // (MUESTRAS_TRAMA * 2) <= tope + 12


def test_sin_nadie_hablando_devuelve_none_y_no_es_un_error():
    """El caso normal de un agente esperando en un escritorio vacío."""
    escucha, _ = _escucha("." * 100)
    assert escucha.turno(espera_max_seg=0.001) is None


# --------------------------------------------------------------------------
# Lo que se dice y lo que no
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escrito, hablado",
    [
        ("**Reunión** a las 10", "Reunión a las 10"),
        ("- Uno\n- Dos", "Uno Dos"),
        ("## Título", "Título"),
        ("Usa `crear_evento` para eso", "Usa crear_evento para eso"),
        ("Ver [la agenda](https://x.cl)", "Ver la agenda"),
        ("Antes\n```\ncodigo()\n```\ndespués", "Antes después"),
    ],
)
def test_no_se_leen_los_asteriscos_en_voz_alta(escrito, hablado):
    assert _limpiar(escrito) == hablado


def test_un_texto_solo_de_markdown_no_hace_hablar_al_agente():
    assert _limpiar("```\nsolo codigo\n```") == ""


# --------------------------------------------------------------------------
# Piper y Whisper: lo que se puede comprobar sin cargarlos
# --------------------------------------------------------------------------


def test_la_velocidad_en_porcentaje_se_invierte_para_piper():
    """Piper quiere `length_scale`, que crece cuando la voz se hace más lenta."""
    config = validar(datos_config(voz={"velocidad": 200}))
    assert Locutor(config.voz, audio=None)._length_scale == pytest.approx(0.5)

    config = validar(datos_config(voz={"velocidad": 50}))
    assert Locutor(config.voz, audio=None)._length_scale == pytest.approx(2.0)

    config = validar(datos_config())
    assert Locutor(config.voz, audio=None)._length_scale == pytest.approx(1.0)


def test_un_modelo_que_no_esta_dice_como_bajarlo(tmp_path, monkeypatch):
    """El error de onnxruntime por un archivo ausente no le sirve a nadie."""
    monkeypatch.setattr("voz.tts.CARPETA_MODELOS", tmp_path)
    config = validar(datos_config(voz={"modelo": "es_ES-davefx-medium"}))

    with pytest.raises(ErrorDeVoz) as fallo:
        Locutor(config.voz, audio=None)._cargar()

    mensaje = str(fallo.value)
    assert "descargar_voz.sh es_ES-davefx-medium" in mensaje
    assert "63 MB" in mensaje


@pytest.mark.parametrize(
    "etiqueta, corto", [("es-CL", "es"), ("es", "es"), ("en-US", "en"), ("", "es")]
)
def test_el_idioma_de_whisper_sale_del_de_la_config(etiqueta, corto):
    assert _idioma_corto(etiqueta) == corto
