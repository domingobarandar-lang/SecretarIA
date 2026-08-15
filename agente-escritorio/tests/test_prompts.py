"""El prompt es producto: si trae el nombre de un cliente adentro, no es plantilla.

Lo que más se prueba acá no es lo que el prompt dice, sino lo que NO dice: nada
hardcodeado, y ninguna de las garantías que viven en el core.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import prompts
from agent.config import validar
from agent.memory import Hecho

from tests.conftest import AHORA, datos_config


def test_no_hay_ningun_cliente_escrito_en_el_modulo():
    """La prueba que justifica el módulo entero: el archivo no nombra a nadie."""
    fuente = (prompts.__file__)
    with open(fuente, encoding="utf-8") as f:
        codigo = f.read()
    for filtracion in ["Vera", "Nombre Apellido", "Empresa S.A.", "@empresa.cl"]:
        assert filtracion not in codigo, f"«{filtracion}» quedó escrito en prompts.py"


def test_el_prompt_se_arma_desde_la_config(config):
    texto = prompts.construir(config, ahora=AHORA)
    assert "Vera" in texto
    assert "Nombre Apellido" in texto
    assert "Gerente General" in texto
    assert "Empresa S.A." in texto
    assert "nombre@empresa.cl" in texto
    assert "America/Santiago" in texto


def test_cambiar_el_config_cambia_el_prompt_sin_tocar_codigo():
    otro = validar(
        datos_config(
            agente={"nombre": "Aurora", "tratamiento": "tu"},
            ejecutivo={
                "nombre": "Otra Persona",
                "email": "otra@otra.cl",
                "zona_horaria": "America/Santiago",
            },
        )
    )
    texto = prompts.construir(otro, ahora=AHORA)
    assert "Aurora" in texto and "Otra Persona" in texto
    assert "Vera" not in texto
    assert "de «tú»" in texto


def test_la_fecha_va_en_español_sin_depender_del_locale():
    momento = datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("America/Santiago"))
    assert prompts.fecha_en_palabras(momento) == "jueves 20 de agosto de 2026"


@pytest.mark.parametrize(
    "dia, esperado",
    [(17, "lunes"), (22, "sábado"), (23, "domingo")],
)
def test_todos_los_dias_tienen_nombre(dia, esperado):
    momento = datetime(2026, 8, dia, 12, 0, tzinfo=ZoneInfo("America/Santiago"))
    assert prompts.fecha_en_palabras(momento).startswith(esperado)


def test_el_prompt_lleva_la_hora_del_ejecutivo(config):
    texto = prompts.construir(config, ahora=AHORA)
    assert "09:00" in texto
    assert "jueves 20 de agosto de 2026" in texto


def test_las_reglas_de_agenda_entran_como_contexto(config):
    texto = prompts.construir(config, ahora=AHORA)
    assert "09:00" in texto and "19:00" in texto


def test_los_dias_bloqueados_se_nombran():
    otro = validar(datos_config(reglas={"dias_bloqueados": ["viernes", "2026-09-18"]}))
    texto = prompts.construir(otro, ahora=AHORA)
    assert "viernes" in texto and "2026-09-18" in texto


def test_lo_que_nunca_se_revela_entra_al_prompt():
    otro = validar(datos_config(reglas={"nunca_revelar": ["sueldos del equipo"]}))
    texto = prompts.construir(otro, ahora=AHORA)
    assert "sueldos del equipo" in texto
    assert "Nunca reveles" in texto


def test_los_hechos_de_memoria_entran_para_no_volver_a_preguntar(config):
    hechos = [Hecho(1, "agenda", "no agenda los viernes en la tarde", "t", "recordar")]
    texto = prompts.construir(config, hechos, ahora=AHORA)
    assert "no agenda los viernes en la tarde" in texto
    assert "no vuelvas a preguntarlo" in texto


def test_sin_hechos_no_aparece_el_bloque_vacio(config):
    texto = prompts.construir(config, [], ahora=AHORA)
    assert "no vuelvas a preguntarlo" not in texto


def test_solo_se_nombran_las_tools_habilitadas():
    otro = validar(datos_config(tools_habilitadas=["ver_agenda", "recordar"]))
    texto = prompts.construir(otro, ahora=AHORA)
    assert "`ver_agenda`" in texto
    assert "`recordar`" in texto
    assert "`buscar_correos`" not in texto
    assert "`crear_evento`" not in texto


def test_el_prompt_dice_que_no_puede_enviar_correo(config):
    texto = prompts.construir(config, ahora=AHORA)
    assert "no puedes enviar correo" in texto.lower()


def test_el_prompt_no_repite_las_garantias_del_core(config):
    """Si el prompt promete la confirmación, alguien va a creer que la sostiene él.

    La confirmación y el chequeo de conflictos viven en core.py. Acá el prompt
    solo avisa que el sistema puede pedir confirmación —contexto para que el
    modelo no se sorprenda—, sin describir el mecanismo como suyo.
    """
    texto = prompts.construir(config, ahora=AHORA).lower()
    assert "verifica conflictos" not in texto
    assert "llama a ver_agenda antes de crear" not in texto


def test_el_contexto_de_log_no_lleva_datos_del_ejecutivo(config):
    contexto = prompts.contexto_para_log(config)
    assert contexto["device_id"] == "box-test"
    plano = str(contexto)
    assert "nombre@empresa.cl" not in plano
    assert "Nombre Apellido" not in plano
