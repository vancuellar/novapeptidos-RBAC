"""EL MOTOR DEL CHAT ES INTERCAMBIABLE — probado por el lado que importa.

Christián preguntó (2026-07-31) si convenía pasar de Gemini a GPT. El diagnóstico
del día fue que NO era el modelo (los rechazos salían del prompt, con
`finish_reason=STOP` y sin un solo `safety_rating` disparado), pero la pregunta
seguía siendo cara de contestar porque el proveedor estaba cableado dentro de
`ai_assistant.py`.

Lo que se prueba aquí no es que GPT conteste —no hay llave, y una prueba que
llame a una API de verdad miente el día que no haya red— sino lo que sí puede
romperse solo:

  · que sin configurar nada el motor siga siendo Gemini, o sea que este archivo
    no cambió el comportamiento de nadie;
  · que pedir un motor sin llave truene con un mensaje que diga qué falta, en vez
    de cambiarse solo a otro en silencio — un cambio de motor callado mueve el
    precio por consulta y la voz del asistente sin que nadie se entere;
  · que `stream_reply` de verdad desvíe la llamada cuando el motor no es Gemini.
    Es el único cable entre las dos piezas; si se corta, todo lo demás pasa y el
    chat se queda en Gemini para siempre.

Y desde el 2026-08-01, EL RESPALDO (Christián: «si el proveedor nuevo falla, que
caiga de vuelta al anterior en vez de dejar al cliente sin respuesta»). Ahí lo
que se prueba es la frontera, que es donde están los bichos:

  · falla del proveedor (429, red caída, mudo) -> contesta el respaldo;
  · falta la llave -> NO hay respaldo, truena. Son dos fallas distintas y
    taparlas juntas dejaría el chat corriendo meses en el motor equivocado;
  · ya salió texto -> NO se cambia de motor a media frase;
  · el respaldo usa SU propio nombre de modelo, no el del motor que se cayó —
    ése es el bicho que mataría al respaldo justo el día que hace falta.
"""
import importlib

import pytest


import asyncio

# Todo lo que puede pisar la decisión de motor. Se limpia SIEMPRE antes de cada
# prueba: si el `.env` de la máquina trae una de éstas, las pruebas medirían el
# entorno de quien las corre en vez del código.
ENTORNO = ('AI_PROVIDER', 'AI_PROVIDER_FALLBACK', 'AI_MODEL_NAME',
           'AI_MODEL_NAME_GEMINI', 'AI_MODEL_NAME_OPENAI', 'AI_MODEL_NAME_KIMI',
           'AI_MODEL_NAME_CLAUDE', 'OPENAI_API_KEY', 'MOONSHOT_API_KEY',
           'ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
           'OPENAI_BASE_URL', 'KIMI_BASE_URL')


@pytest.fixture()
def motor(monkeypatch):
    """`motor('openai')` recarga el módulo con ese proveedor puesto."""
    def _con(proveedor=None, **entorno):
        for k in ENTORNO:
            monkeypatch.delenv(k, raising=False)
        if proveedor:
            monkeypatch.setenv('AI_PROVIDER', proveedor)
        for k, v in entorno.items():
            monkeypatch.setenv(k, v)
        import modelo_ia
        m = importlib.reload(modelo_ia)
        # `secretos` guarda un cache en memoria que el entorno pisa, pero si una
        # prueba anterior lo llenó, `llave()` lo encontraría. Se vacía.
        import secretos
        secretos._CACHE.clear()
        return m
    yield _con
    import modelo_ia
    importlib.reload(modelo_ia)


def _texto(generador):
    """Corre un generador asíncrono y devuelve la lista de trozos."""
    async def _correr():
        return [t async for t in generador]
    return asyncio.run(_correr())


def _revienta(mensaje):
    """Un motor que se cae ANTES de decir nada."""
    async def _motor(*a, **k):
        raise RuntimeError(mensaje)
        yield ''                          # pragma: no cover - lo hace generador
    return _motor


# --------------------------------------------------- nada cambió para nadie
def test_sin_configurar_nada_sigue_siendo_gemini(motor):
    """La prueba de que este archivo no cambió nada para nadie."""
    assert motor().proveedor() == 'gemini'


def test_un_motor_desconocido_no_tumba_el_chat(motor):
    """Una errata en la variable de entorno no puede dejar al asesor mudo."""
    assert motor('gpt4').proveedor() == 'gemini'


def test_sin_configurar_nada_no_hay_respaldo(motor):
    """La cadena de hoy es UN motor. Si esto creciera solo, el chat estaría
    llamando a un proveedor que Christián no encendió — y pagándolo."""
    assert motor().cadena() == ['gemini']


def test_el_modelo_de_gemini_es_el_que_ya_corria(motor):
    """⛔ El valor de producción. Si alguien lo cambia aquí, cambia el motor en
    vivo sin que nadie lo pida y sin que se note hasta que llegue la factura."""
    assert motor().modelo('gemini') == 'gemini-3.5-flash'


def test_los_motores_nuevos_nacen_apagados(motor):
    """Sin llave no están encendidos — el patrón de `enviosinternacionales.py`."""
    m = motor()
    assert not m.encendido('openai')
    assert not m.encendido('kimi')
    assert not m.encendido('claude')


def test_se_encienden_al_pegar_la_llave(motor):
    m = motor('openai', OPENAI_API_KEY='sk-de-mentiras')
    assert m.proveedor() == 'openai' and m.encendido()


def test_kimi_se_enciende_al_pegar_la_llave(motor):
    m = motor('kimi', MOONSHOT_API_KEY='sk-de-mentiras')
    assert m.proveedor() == 'kimi' and m.encendido()
    assert m.modelo() == 'kimi-latest'


# -------------------------------------------- falta la llave: se ve, no se tapa
@pytest.mark.parametrize('proveedor,llave', [('openai', 'OPENAI_API_KEY'),
                                             ('kimi', 'MOONSHOT_API_KEY'),
                                             ('claude', 'ANTHROPIC_API_KEY')])
def test_sin_llave_truena_diciendo_cual_falta(motor, proveedor, llave):
    """⛔ NO se cambia solo a Gemini. Un cambio de motor en silencio mueve el
    precio por consulta y la voz del asistente sin que nadie se entere; es peor
    que un error que dice exactamente qué pegar."""
    m = motor(proveedor)
    with pytest.raises(m.FaltaConfiguracion) as e:
        _texto(m.responder('sistema', 'hola'))
    assert llave in str(e.value)


def test_openai_no_inventa_un_nombre_de_modelo(motor):
    """Los nombres de modelo de OpenAI cambian seguido y no se verificaron. Vacío
    y con un error claro es más honesto que un 404 confuso en la primera consulta."""
    m = motor('openai', OPENAI_API_KEY='sk-de-mentiras')
    assert m.modelo() == ''
    with pytest.raises(m.FaltaConfiguracion) as e:
        _texto(m.responder('sistema', 'hola'))
    assert 'AI_MODEL_NAME_OPENAI' in str(e.value)


def test_una_llave_ausente_NO_activa_el_respaldo(motor, monkeypatch):
    """⛔ LA REGLA QUE SEPARA LAS DOS FALLAS. Un `.env` mal pegado tiene que
    doler; si el respaldo lo tapara, el chat correría meses en el motor
    equivocado y la factura del bueno nunca llegaría porque nunca se usó."""
    import ai_assistant
    m = motor('openai', AI_PROVIDER_FALLBACK='gemini', GEMINI_API_KEY='g-de-mentiras')
    assert m.cadena() == ['openai', 'gemini']

    async def _gemini_no_se_llama(system, mensaje):
        raise AssertionError('el respaldo tapó una llave que falta')
        yield ''                          # pragma: no cover

    monkeypatch.setattr(ai_assistant, '_gemini', _gemini_no_se_llama)
    with pytest.raises(m.FaltaConfiguracion):
        _texto(ai_assistant.stream_reply({'system_message': 'S'}, 'hola'))


# ------------------------------------------------- el proveedor falló: respaldo
def test_un_respaldo_sin_llave_no_es_un_respaldo(motor):
    """Ponerlo en la cadena sólo cambiaría un error por dos."""
    m = motor('openai', AI_PROVIDER_FALLBACK='gemini')
    assert m.cadena() == ['openai']


def test_el_respaldo_se_puede_apagar(motor):
    m = motor('openai', AI_PROVIDER_FALLBACK='ninguno', GEMINI_API_KEY='g')
    assert m.cadena() == ['openai']


def test_si_el_motor_nuevo_se_cae_contesta_el_de_respaldo(motor, monkeypatch):
    """LO QUE PIDIÓ CHRISTIÁN: que un fallo del proveedor nuevo no deje al cliente
    sin respuesta. Se simula el 429 que hoy tumba el chat."""
    import ai_assistant
    m = motor('openai', OPENAI_API_KEY='sk-x', AI_MODEL_NAME_OPENAI='gpt-x',
              GEMINI_API_KEY='g-de-mentiras')
    assert m.cadena() == ['openai', 'gemini']

    monkeypatch.setattr(m, 'responder', _revienta('429 rate_limit_exceeded'))

    visto = {}

    async def _gemini(system, mensaje):
        visto['system'] = system
        yield 'contesta el respaldo'

    monkeypatch.setattr(ai_assistant, '_gemini', _gemini)
    assert _texto(ai_assistant.stream_reply({'system_message': 'EL SOBRE'}, 'hola')) \
        == ['contesta el respaldo']
    # ⛔ El respaldo recibe EL MISMO sobre: el candado por rol no depende de quién
    # conteste, porque el costo nunca entró al sobre (ver chat_negocio.py).
    assert visto['system'] == 'EL SOBRE'


def test_si_el_motor_nuevo_se_queda_mudo_tambien_contesta_el_respaldo(motor, monkeypatch):
    """Un bloqueo duro no lanza excepción: entrega cero trozos. Sin esto el
    cliente vería una burbuja vacía, que se lee como que la página se rompió."""
    import ai_assistant
    m = motor('kimi', MOONSHOT_API_KEY='sk-x', GEMINI_API_KEY='g')

    async def _mudo(*a, **k):
        return
        yield ''                          # pragma: no cover

    monkeypatch.setattr(m, 'responder', _mudo)

    async def _gemini(system, mensaje):
        yield 'contesta el respaldo'

    monkeypatch.setattr(ai_assistant, '_gemini', _gemini)
    assert _texto(ai_assistant.stream_reply({'system_message': 'S'}, 'hola')) \
        == ['contesta el respaldo']


def test_si_ya_salio_texto_NO_se_cambia_de_motor(motor, monkeypatch):
    """⛔ El cliente ya está leyendo. Empalmarle encima la respuesta de otro
    modelo produce un Frankenstein a media frase: peor que un error honesto."""
    import ai_assistant
    m = motor('openai', OPENAI_API_KEY='sk-x', AI_MODEL_NAME_OPENAI='gpt-x',
              GEMINI_API_KEY='g')

    async def _se_cae_a_medias(*a, **k):
        yield 'La Retatrutida 10 mg cuesta'
        raise RuntimeError('se cayó la red')

    monkeypatch.setattr(m, 'responder', _se_cae_a_medias)

    async def _gemini_no_se_llama(system, mensaje):
        raise AssertionError('se empalmó otro motor a media frase')
        yield ''                          # pragma: no cover

    monkeypatch.setattr(ai_assistant, '_gemini', _gemini_no_se_llama)
    with pytest.raises(RuntimeError, match='se cayó la red'):
        _texto(ai_assistant.stream_reply({'system_message': 'S'}, 'hola'))


def test_si_se_caen_los_dos_el_error_sube(motor, monkeypatch):
    """No se inventa una respuesta ni se traga el error: el endpoint es quien
    decide qué mensaje ve el usuario, y necesita saber qué pasó."""
    import ai_assistant
    m = motor('openai', OPENAI_API_KEY='sk-x', AI_MODEL_NAME_OPENAI='gpt-x',
              GEMINI_API_KEY='g')
    monkeypatch.setattr(m, 'responder', _revienta('429 quota'))
    monkeypatch.setattr(ai_assistant, '_gemini', _revienta('503 google caído'))
    with pytest.raises(RuntimeError, match='google'):
        _texto(ai_assistant.stream_reply({'system_message': 'S'}, 'hola'))


def test_el_respaldo_usa_SU_modelo_no_el_del_otro(motor):
    """⛔ El bicho que mataría al respaldo justo el día que hace falta: con una
    sola `AI_MODEL_NAME`, caer de GPT a Gemini le mandaría a Google el nombre de
    un modelo de OpenAI y el respaldo moriría con un 404."""
    m = motor('openai', AI_MODEL_NAME='gpt-5.2-mini', OPENAI_API_KEY='sk-x',
              GEMINI_API_KEY='g')
    assert m.modelo('openai') == 'gpt-5.2-mini'
    assert m.modelo('gemini') == 'gemini-3.5-flash'


def test_el_cable_existe_de_verdad(motor, monkeypatch):
    """El único punto de contacto entre las dos piezas: si `stream_reply` no
    desvía, todo lo de arriba pasa en verde y el chat se queda en Gemini."""
    import ai_assistant
    m = motor('claude', ANTHROPIC_API_KEY='sk-de-mentiras')

    visto = {}

    async def _falso(system, mensaje, cual=None):
        visto['system'] = system
        visto['cual'] = cual
        yield 'contesto yo'

    monkeypatch.setattr(m, 'responder', _falso)
    assert _texto(ai_assistant.stream_reply({'system_message': 'EL SOBRE'}, 'hola')) \
        == ['contesto yo']
    assert visto['system'] == 'EL SOBRE'
    assert visto['cual'] == 'claude'


# ------------------------------------------ lo que ve el usuario cuando falla
def test_el_aviso_no_nombra_al_proveedor(motor):
    """El nombre del proveedor es un detalle nuestro. Decirle a un cliente "el
    plan gratuito de Google" lo vuelve mentira el día que cambie el motor."""
    m = motor()
    for (ambito, clase) in m.AVISOS:
        for idioma in ('es', 'en', 'pt'):
            texto = m.aviso(ambito, clase, idioma).lower()
            for marca in ('google', 'gemini', 'openai', 'gpt', 'kimi',
                          'moonshot', 'anthropic', 'claude'):
                assert marca not in texto, f'{ambito}/{clase}/{idioma} nombra a {marca}'


def test_los_avisos_estan_en_los_tres_idiomas(motor):
    m = motor()
    for clave, textos in m.AVISOS.items():
        assert set(textos) == {'es', 'en', 'pt'}, f'a {clave} le falta un idioma'
        assert all(t.strip() for t in textos.values())


def test_un_idioma_que_no_manejamos_cae_en_espanol(motor):
    m = motor()
    assert m.aviso('tienda', 'saturado', 'fr-FR') == m.aviso('tienda', 'saturado', 'es')
    assert m.aviso('panel', 'generico', None) == m.aviso('panel', 'generico', 'es')


def test_el_codigo_largo_del_sitio_tambien_sirve(motor):
    """El sitio manda `pt-BR` y `en-US`, no `pt` ni `en`."""
    m = motor()
    assert m.aviso('tienda', 'saturado', 'pt-BR') == m.aviso('tienda', 'saturado', 'pt')
    assert m.aviso('panel', 'sin_llave', 'en-US') == m.aviso('panel', 'sin_llave', 'en')


@pytest.mark.parametrize('error,clase', [
    ('429 RESOURCE_EXHAUSTED', 'saturado'),
    ('429 rate_limit_exceeded', 'saturado'),
    ('GEMINI_API_KEY is not configured.', 'sin_llave'),
    ('MOONSHOT_API_KEY no está configurada.', 'sin_llave'),
    ('Falta AI_MODEL_NAME_OPENAI', 'sin_llave'),
    ('se cayó la red', 'generico'),
])
def test_cada_falla_cae_en_su_cajon(motor, error, clase):
    """Los cuatro proveedores dicen 429 para lo mismo; sólo Google le pone además
    su propio nombre. Si esto se equivoca, el usuario lee un mensaje que no
    corresponde y Christián persigue el problema que no es."""
    assert motor().clase_de_error(RuntimeError(error)) == clase


def test_falta_configuracion_siempre_es_sin_llave(motor):
    m = motor()
    assert m.clase_de_error(m.FaltaConfiguracion('lo que sea')) == 'sin_llave'


def test_las_llaves_se_pueden_pegar_desde_el_admin():
    """Christián trabaja desde el teléfono: la llave se pega en Admin -> Cobros,
    igual que las de las pasarelas. Si no está en PERMITIDAS, el endpoint la
    rechaza y hay que entrar por SSH."""
    import secretos
    assert 'OPENAI_API_KEY' in secretos.PERMITIDAS
    assert 'MOONSHOT_API_KEY' in secretos.PERMITIDAS
    assert 'ANTHROPIC_API_KEY' in secretos.PERMITIDAS


# ------------------------------ el motor se elige SOLO con las llaves (3-ago)
# «¿Por qué sigue diciendo que se agotó la cuota si ya subí las claves?» Porque
# `AI_PROVIDER` sólo se puede poner en el `.env` del servidor: pegar la llave en
# el panel no cambiaba de motor. Y después: «quita lo de Gemini» + «usa Kimi
# primero, luego GPT, luego Claude».

def test_sin_llaves_de_pago_el_motor_sigue_siendo_gemini(motor):
    """Nadie se queda sin chat: si lo único pegado es Gemini, corre Gemini."""
    m = motor(None, GEMINI_API_KEY='g')
    assert m.cadena() == ['gemini']


def test_con_kimi_pegado_kimi_manda_y_gemini_queda_de_respaldo(motor):
    """El orden que pidió Christián: Kimi (centavos) es el motor de casa, y el
    plan gratis de Gemini pasa a ser la red de abajo, no el de arriba."""
    m = motor(None, MOONSHOT_API_KEY='k', GEMINI_API_KEY='g')
    assert m.cadena() == ['kimi', 'gemini']


def test_el_orden_es_kimi_luego_gpt_luego_claude(motor):
    # Con las tres llaves de pago manda Kimi.
    m = motor(None, MOONSHOT_API_KEY='k', OPENAI_API_KEY='o',
              AI_MODEL_NAME_OPENAI='gpt-x', ANTHROPIC_API_KEY='a', GEMINI_API_KEY='g')
    assert m.cadena()[0] == 'kimi'
    # Sin Kimi, GPT.
    m = motor(None, OPENAI_API_KEY='o', AI_MODEL_NAME_OPENAI='gpt-x',
              ANTHROPIC_API_KEY='a', GEMINI_API_KEY='g')
    assert m.cadena()[0] == 'openai'
    # Sin Kimi ni GPT, Claude.
    m = motor(None, ANTHROPIC_API_KEY='a', GEMINI_API_KEY='g')
    assert m.cadena() == ['claude', 'gemini']


def test_un_motor_sin_nombre_de_modelo_no_se_vuelve_el_de_casa(motor):
    """GPT va sin nombre por omisión: tomarlo de motor cambiaría «cuota agotada»
    por «falta AI_MODEL_NAME», que es peor porque nadie sabe qué hacer con eso."""
    m = motor(None, OPENAI_API_KEY='o', GEMINI_API_KEY='g')
    assert m.cadena() == ['gemini']


def test_elegirlo_a_mano_sigue_mandando(motor):
    m = motor('gemini', MOONSHOT_API_KEY='k', GEMINI_API_KEY='g')
    assert m.cadena() == ['gemini', 'kimi']


def test_el_respaldo_se_puede_seguir_apagando(motor):
    m = motor(None, AI_PROVIDER_FALLBACK='ninguno', ANTHROPIC_API_KEY='a',
              GEMINI_API_KEY='g')
    assert m.cadena() == ['claude']
