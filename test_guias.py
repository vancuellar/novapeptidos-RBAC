"""DE QUÉ PAQUETERÍA ES UNA GUÍA — y que el gemelo de la pantalla no se separe.

⛔ POR QUÉ IMPORTA (Christián, 2026-07-31): «el rastreo también debe aplicar para Aidee
y para todos los futuros clientes». Un pedido con número de guía pero SIN paquetería no
se puede rastrear: no hay liga a dónde mandar al cliente ni a quién preguntarle por los
eventos. La pantalla que captura guías ya adivina la paquetería mientras se teclea, pero
la ruta que guarda el envío se puede llamar sin pasar por ahí.

La prueba que más vale de este archivo es la ÚLTIMA: compara regla por regla este
archivo contra `src/lib/paqueteria.js` del repo de la pantalla. Son dos lenguajes y dos
repos, así que no pueden ser un solo archivo — pero sí pueden estar obligados a decir lo
mismo, y eso es lo que se comprueba aquí.
"""
import os
import re

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import guias


# Números de verdad, con su paquetería. Los dos primeros son los pedidos REALES del
# 2026-07-30: el de Aidee y el de Brenda, los dos de FedEx y los dos de 12 dígitos.
@pytest.mark.parametrize('numero,quien', [
    ('875122824121', 'FedEx'),              # Aidee — el que destapó todo esto
    ('875164874865', 'FedEx'),              # Brenda
    ('1Z999AA10123456784', 'UPS'),
    ('RR123456789MX', 'Correos de México'),
    ('JVGL01234567890', 'DHL'),
    ('JJD01234567890', 'DHL'),
    ('PQ123456789', 'Paquete Express'),
    ('RP123456789', 'Redpack'),
    ('123456789012345', 'FedEx'),           # 15 dígitos
    ('12345678901234567890', 'FedEx'),      # 20 dígitos
    ('12345678901', 'DHL'),                 # 11 dígitos
    ('1234567890', 'Estafeta'),             # 10 — ambiguo, se sugiere Estafeta
])
def test_reconoce_los_formatos_de_verdad(numero, quien):
    assert guias.paqueteria_de(numero) == quien


def test_el_numero_se_limpia_como_lo_pega_la_gente():
    """Se pega como llegó por WhatsApp: con espacios cada cuatro dígitos, o con
    guiones. Si eso no se limpia, no casa ninguna regla y el pedido queda sin
    paquetería justo cuando alguien SÍ hizo su trabajo."""
    assert guias.paqueteria_de('8751 2282 4121') == 'FedEx'
    assert guias.paqueteria_de('8751-2282-4121') == 'FedEx'
    assert guias.paqueteria_de(' 875122824121 ') == 'FedEx'
    assert guias.paqueteria_de('1z999aa10123456784') == 'UPS'


def test_lo_ambiguo_se_marca_como_ambiguo():
    """Diez dígitos los usan Estafeta Y DHL. Se sugiere una, pero avisando: quien
    captura tiene que poder corregirla, y una sugerencia disfrazada de certeza manda
    al cliente a rastrear al sitio equivocado."""
    assert guias.detectar('1234567890') == {'quien': 'Estafeta', 'seguro': False}
    assert guias.detectar('875122824121')['seguro'] is True


def test_lo_que_no_dice_nada_no_se_inventa():
    """Preferimos no saber a adivinar mal: una paquetería equivocada es peor que
    ninguna, porque manda al cliente a una página que no conoce su guía."""
    for basura in ('', None, '123', 'hola', '   ', 'ABC'):
        assert guias.paqueteria_de(basura) == ''
        assert guias.detectar(basura) == {}


def test_los_nombres_son_los_que_entiende_la_liga_de_rastreo():
    """⛔ Los nombres tienen que caer en `server.CARRIER_TRACKING_URLS` o la liga sale
    vacía y el cliente se queda sin a dónde ir. Se comprueba de verdad, armando la
    liga con cada nombre que este archivo puede devolver."""
    import server
    for nombre in guias.PAQUETERIAS:
        liga = server.build_tracking_url(nombre, '123456789012')
        assert liga.startswith('http'), f'«{nombre}» no arma liga de rastreo'


# =========================================================================
#  EL GEMELO: este archivo y el de la pantalla no se pueden separar
# =========================================================================
def _ruta_del_js():
    """El `paqueteria.js` del repo de la pantalla, que vive al lado de éste."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    for candidato in ('novapeptidos-UI.nosync', 'novapeptidos-UI'):
        ruta = os.path.join(os.path.dirname(aqui), candidato,
                            'src', 'lib', 'paqueteria.js')
        if os.path.exists(ruta):
            return ruta
    return ''


def test_las_reglas_son_LAS_MISMAS_que_las_de_la_pantalla():
    """⛔ SI SE CAMBIA UNA REGLA, SE CAMBIA EN LOS DOS.

    La detección pasa en la pantalla (para sugerir mientras se teclea) y aquí (como red
    de seguridad). Son dos lenguajes y dos repos: no pueden ser un solo archivo. Lo que
    sí se puede es obligarlos a decir lo mismo, y es lo que hace esta prueba: lee el JS
    de verdad y compara la lista de paqueterías y las expresiones, en orden.

    Se salta —no falla— si el repo de la pantalla no está al lado: en el servidor no
    está, y no tendría sentido tumbar el despliegue del backend por eso.
    """
    ruta = _ruta_del_js()
    if not ruta:
        pytest.skip('el repo de la pantalla no está en esta copia')
    js = open(ruta, encoding='utf-8').read()

    # 1. La lista de paqueterías, en el mismo orden.
    bloque = re.search(r'PAQUETERIAS\s*=\s*\[(.*?)\]', js, re.S).group(1)
    del_js = tuple(re.findall(r"'([^']+)'", bloque))
    assert del_js == guias.PAQUETERIAS, (
        f'la lista se separó:\n  pantalla: {del_js}\n  backend : {guias.PAQUETERIAS}')

    # 2. Las reglas: misma expresión, mismo dueño y misma confianza, en el mismo orden.
    reglas_js = re.findall(
        r'\{\s*re:\s*/(.+?)/\s*,\s*quien:\s*'r"'([^']+)'"r'\s*,\s*seguro:\s*(true|false)\s*\}',
        js)
    assert reglas_js, 'no pude leer las reglas del JS: ¿cambió su forma?'
    esperado = [(expr, quien, seguro == 'true') for expr, quien, seguro in reglas_js]
    # En JS las expresiones no llevan el `(?=...)` escapado distinto ni banderas; se
    # comparan tal cual porque las dos son sintaxis compatible.
    actual = [(expr, quien, seguro) for expr, quien, seguro in guias.REGLAS]
    assert actual == esperado, (
        'las reglas se separaron entre la pantalla y el backend:\n'
        f'  pantalla: {esperado}\n  backend : {actual}')
