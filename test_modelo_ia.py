"""EL MOTOR DEL CHAT ES INTERCAMBIABLE — probado por el lado que importa.

Christián preguntó (2026-07-31) si convenía pasar de Gemini a GPT. El diagnóstico
del día fue que NO era el modelo (los rechazos salían del prompt, con
`finish_reason=STOP` y sin un solo `safety_rating` disparado), pero la pregunta
seguía siendo cara de contestar porque el proveedor estaba cableado dentro de
`ai_assistant.py`.

Lo que se prueba aquí no es que GPT conteste —no hay llave, y una prueba que
llame a una API de verdad miente el día que no haya red— sino las TRES cosas que
sí pueden romperse solas:

  · que sin configurar nada el motor siga siendo Gemini, o sea que este archivo
    no cambió el comportamiento de nadie;
  · que pedir un motor sin llave truene con un mensaje que diga qué falta, en vez
    de cambiarse solo a otro en silencio — un cambio de motor callado mueve el
    precio por consulta y la voz del asistente sin que nadie se entere;
  · que `stream_reply` de verdad desvíe la llamada cuando el motor no es Gemini.
    Es el único cable entre las dos piezas; si se corta, todo lo demás pasa y el
    chat se queda en Gemini para siempre.
"""
import importlib

import pytest


@pytest.fixture()
def motor(monkeypatch):
    """`motor('openai')` recarga el módulo con ese proveedor puesto."""
    def _con(proveedor=None, **entorno):
        for k in ('AI_PROVIDER', 'AI_MODEL_NAME', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
            monkeypatch.delenv(k, raising=False)
        if proveedor:
            monkeypatch.setenv('AI_PROVIDER', proveedor)
        for k, v in entorno.items():
            monkeypatch.setenv(k, v)
        import modelo_ia
        return importlib.reload(modelo_ia)
    yield _con
    import modelo_ia
    importlib.reload(modelo_ia)


def test_sin_configurar_nada_sigue_siendo_gemini(motor):
    """La prueba de que este archivo no cambió nada para nadie."""
    assert motor().proveedor() == 'gemini'


def test_un_motor_desconocido_no_tumba_el_chat(motor):
    """Una errata en la variable de entorno no puede dejar al asesor mudo."""
    assert motor('kimi').proveedor() == 'gemini'


def test_los_motores_nuevos_nacen_apagados(motor):
    """Sin llave no están encendidos — el patrón de `enviosinternacionales.py`."""
    m = motor()
    assert not m.encendido('openai')
    assert not m.encendido('claude')


def test_se_encienden_al_pegar_la_llave(motor):
    m = motor('openai', OPENAI_API_KEY='sk-de-mentiras')
    assert m.proveedor() == 'openai' and m.encendido()


@pytest.mark.parametrize('proveedor,llave', [('openai', 'OPENAI_API_KEY'),
                                             ('claude', 'ANTHROPIC_API_KEY')])
def test_sin_llave_truena_diciendo_cual_falta(motor, proveedor, llave):
    """⛔ NO se cambia solo a Gemini. Un cambio de motor en silencio mueve el
    precio por consulta y la voz del asistente sin que nadie se entere; es peor
    que un error que dice exactamente qué pegar."""
    m = motor(proveedor)

    async def _correr():
        async for _ in m.responder('sistema', 'hola'):
            pass

    import asyncio
    with pytest.raises(RuntimeError) as e:
        asyncio.run(_correr())
    assert llave in str(e.value)


def test_openai_no_inventa_un_nombre_de_modelo(motor):
    """Los nombres de modelo de OpenAI cambian seguido y no se verificaron. Vacío
    y con un error claro es más honesto que un 404 confuso en la primera consulta."""
    m = motor('openai', OPENAI_API_KEY='sk-de-mentiras')
    assert m.modelo() == ''

    async def _correr():
        async for _ in m.responder('sistema', 'hola'):
            pass

    import asyncio
    with pytest.raises(RuntimeError) as e:
        asyncio.run(_correr())
    assert 'AI_MODEL_NAME' in str(e.value)


def test_el_cable_existe_de_verdad(motor, monkeypatch):
    """El único punto de contacto entre las dos piezas: si `stream_reply` no
    desvía, todo lo de arriba pasa en verde y el chat se queda en Gemini."""
    import ai_assistant
    m = motor('claude', ANTHROPIC_API_KEY='sk-de-mentiras')

    visto = {}

    async def _falso(system, mensaje):
        visto['system'] = system
        yield 'contesto yo'

    monkeypatch.setattr(m, 'responder', _falso)

    async def _correr():
        return [t async for t in ai_assistant.stream_reply(
            {'system_message': 'EL SOBRE'}, 'hola')]

    import asyncio
    assert asyncio.run(_correr()) == ['contesto yo']
    assert visto['system'] == 'EL SOBRE'


def test_las_llaves_se_pueden_pegar_desde_el_admin():
    """Christián trabaja desde el teléfono: la llave se pega en Admin -> Cobros,
    igual que las de las pasarelas. Si no está en PERMITIDAS, el endpoint la
    rechaza y hay que entrar por SSH."""
    import secretos
    assert 'OPENAI_API_KEY' in secretos.PERMITIDAS
    assert 'ANTHROPIC_API_KEY' in secretos.PERMITIDAS
