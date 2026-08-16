"""Decide si le hablaron al agente, buscando su nombre en lo que se transcribió.

**Por qué no hay un modelo de wake word.** Lo estándar sería openWakeWord o
Porcupine: un modelito que detecta una palabra sin transcribir nada, y que
gastaría muchísima menos CPU que lo de acá. El problema es que esos modelos se
entrenan **por palabra**. Hay uno para «alexa» y uno para «hey jarvis», no hay
uno para «Vera» — y el nombre del agente sale de `device.config.json`, que es la
pieza que promete que cambiar de cliente no cuesta una línea de código. Un modelo
por nombre significaría entrenar uno por cliente antes de poder venderle la caja.

Así que el nombre se busca en el texto ya transcrito. Se paga con CPU (Whisper
corre en cada frase que alguien diga cerca, no solo en las dirigidas al agente) y
se gana que «Vera», «Nora» o «Sofía» funcionen sin tocar nada. Si algún día la
placa no da abasto, acá es donde se cambia la estrategia: el resto del ciclo no
sabe cómo se tomó esta decisión.

La comparación es difusa a propósito. Whisper escribe nombres propios como le
suenan — «Vera» sale «Bera», «Vela», «Vera,» — y exigir igualdad exacta sería un
agente que no despierta la mitad de las veces sin decir jamás por qué.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

#: Cuán parecido tiene que sonar. Bajarlo hace que despierte con cualquier cosa;
#: subirlo, que no despierte. 0.72 deja pasar «bera» y «vela» pero no «cena».
PARECIDO_MINIMO = 0.72

#: Solo se busca el nombre al principio de la frase. A una persona se la llama
#: por el nombre al empezar, no en la palabra catorce; mirar la frase entera
#: hace que el agente se meta en conversaciones ajenas donde alguien lo nombró.
PALABRAS_DE_VENTANA = 3

#: Muletillas que preceden al nombre y no son parte de la orden.
_CORTESIAS = {"hola", "oye", "hey", "ya", "eh", "disculpa", "perdon", "buenas"}

_RE_PALABRA = re.compile(r"[^\W\d_]+", re.UNICODE)


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y sin puntuación. «¿Vera?» y «vera» son lo mismo."""
    sin_tildes = "".join(
        c
        for c in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(c) != "Mn"
    )
    return sin_tildes.strip()


def _parecido(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def buscar_nombre(texto: str, nombre: str) -> tuple[bool, str]:
    """¿Lo llamaron? Y si sí, ¿qué le pidieron?

    Devuelve `(despierto, resto)`. El `resto` es lo que viene después del
    nombre, que suele ser la orden completa: a «Vera, ¿qué tengo mañana?» hay
    que contestarle en un solo turno, no responder «¿sí?» y hacer que la
    persona repita la pregunta.
    """
    if not texto or not nombre:
        return False, ""

    objetivo = normalizar(nombre).split()[0] if nombre.strip() else ""
    if not objetivo:
        return False, ""

    plano = normalizar(texto)
    palabras = _RE_PALABRA.findall(plano)

    for indice, palabra in enumerate(palabras[:PALABRAS_DE_VENTANA]):
        if palabra in _CORTESIAS:
            continue
        if _parecido(palabra, objetivo) < PARECIDO_MINIMO:
            continue
        # Se corta sobre el texto original y no sobre el normalizado: lo que
        # sigue va derecho al modelo, y merece llegar con sus tildes y sus
        # signos de pregunta — de eso depende que entienda que es una pregunta.
        posicion = _posicion_de_palabra(texto, indice)
        if posicion is None:
            return True, ""
        # Se limpia el separador que va entre el nombre y la orden —«Vera, ...»,
        # «Vera: ...»— pero **no** los signos de apertura: «¿» y «¡» son la
        # primera letra de lo que sigue, y quitarlos le llega al modelo como una
        # afirmación donde había una pregunta.
        resto = texto[posicion:].lstrip(" ,.;:-—").strip()
        # «Vera?» deja un «?» suelto, que no es una orden: es el nombre dicho con
        # entonación. Sin esto el modelo recibe un turno que consiste en un signo.
        return True, resto if _RE_PALABRA.search(resto) else ""

    return False, ""


def _posicion_de_palabra(texto: str, indice: int) -> int | None:
    """Dónde termina la palabra número `indice` del texto original.

    Se recorre el texto sin normalizar para que los índices calcen: quitar
    tildes no cambia el largo, pero la puntuación sí, y un desfase de dos
    caracteres se come la primera letra de la orden.
    """
    for numero, encontrada in enumerate(_RE_PALABRA.finditer(texto)):
        if numero == indice:
            return encontrada.end()
    return None
