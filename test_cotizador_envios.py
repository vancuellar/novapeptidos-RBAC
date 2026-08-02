"""EL COTIZADOR DE ENVÍOS — y sobre todo: que al distribuidor NO se le escape un costo.

⛔ POR QUÉ EXISTE (Christián, 2026-08-01). Hasta hoy el envío sólo se cotizaba dentro
del checkout o sobre un pedido ya hecho, y la pregunta de todos los días —«¿cuánto
cuesta mandar esto a tal código postal?»— llega ANTES de que exista el pedido. El
cotizador contesta esa pregunta desde el Panel y desde el tablero del distribuidor.

Las pruebas que más valen de este archivo son las del bloque 2: le PIDEN al servidor la
cotización con una sesión de distribuidor y luego leen el sobre completo —el JSON como
texto plano— buscando lo que la casa paga. Es tosco a propósito, igual que en
`test_privacidad_distribuidor.py`: así no depende de que nadie se acuerde de actualizar
una lista de campos prohibidos el día que agregue uno nuevo.

Y el bloque 4 cuida la otra regla que ya costó dinero: EL PRECIO LO PONE EL SERVIDOR.
En modo 'items' el peso y el importe salen del catálogo aunque el navegador mande otros;
y la cotización de esta pantalla vive en su PROPIA colección, para que un peso capturado
a mano no pueda colarse a pagar un envío en el checkout.
"""
import os
import re

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import envios
import paqueterias
import server


ADMIN = {'id': 'u-admin', 'name': 'Christián', 'email': 'admin@exygenlabs.com',
         'role': 'admin'}
DIST = {'id': 'u-maria', 'name': 'Maria Neunfeld', 'email': 'maria@exygenlabs.com',
        'role': 'distributor', 'tier': 'junior0'}
OTRO_DIST = {'id': 'u-otro', 'name': 'Otro', 'email': 'otro@exygenlabs.com',
             'role': 'distributor'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}

PRODUCTO = {'id': 'p-reta', 'sku': 'RETA-20MG', 'slug': 'retatrutida-20-mg',
            'name': 'Retatrutida 20 mg', 'category': 'metabolicos',
            'price': 3000, 'presentation': '20 mg', 'stock': 50,
            'distributor_eligible': True}

# Un renglón de carrito como lo manda la pantalla. `price` va a propósito con un
# número ABSURDO: el servidor tiene que ignorarlo y usar el del catálogo.
def renglon(qty=1, price=1):
    return {'product_id': 'p-reta', 'name': 'Retatrutida 20 mg', 'price': price,
            'quantity': qty, 'presentation': '20 mg', 'image_url': ''}


# Tres tarifas DESORDENADAS a propósito: la más barata va en medio. Si el cotizador
# se apoyara en que alguien más las ordenó, recomendaría la equivocada.
TARIFAS = [
    {'paqueteria': 'FedEx', 'paqueteria_id': 'fedex', 'servicio': 'Express',
     'servicio_codigo': 'exp', 'dias': 2, 'precio': 179.20, 'rate_id': 'r-fx',
     'proveedor': 'skydropx', 'proveedor_nombre': 'Skydropx'},
    {'paqueteria': 'Paquetexpress', 'paqueteria_id': 'paquetexpress',
     'servicio': 'Terrestre', 'servicio_codigo': 'ter', 'dias': 3, 'precio': 139.11,
     'rate_id': 'r-px', 'proveedor': 'skydropx', 'proveedor_nombre': 'Skydropx'},
    {'paqueteria': 'Estafeta', 'paqueteria_id': 'estafeta', 'servicio': 'Día Siguiente',
     'servicio_codigo': 'ds', 'dias': 1, 'precio': 200.49, 'rate_id': 'r-es',
     'proveedor': 'enviosinternacionales', 'proveedor_nombre': 'Envíos Internacionales'},
]

PROVEEDORES = [
    {'clave': 'skydropx', 'nombre': 'Skydropx', 'activo': True, 'tarifas': 2,
     'mejor': 139.11, 'detalle': ''},
    {'clave': 'enviosinternacionales', 'nombre': 'Envíos Internacionales',
     'activo': True, 'tarifas': 1, 'mejor': 200.49, 'detalle': ''},
]


# --------------------------------------------------------- base de datos falsa
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, campo=None, direccion=-1):
        self._docs = sorted(self._docs, key=lambda d: d.get(campo, ''),
                            reverse=direccion < 0)
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, n=None, *a, **k):
        return [dict(d) for d in (self._docs[:n] if n else self._docs)]


class _Coll:
    def __init__(self, docs=()):
        self.docs = list(docs)

    def find(self, filtro=None, *a, **k):
        # Sólo se filtra por igualdad simple. Los operadores de Mongo (`$or`, `$in`)
        # se dejan pasar: aquí no se está probando Mongo.
        return _Cursor([d for d in self.docs
                        if all(d.get(k2) == v for k2, v in (filtro or {}).items()
                               if not k2.startswith('$') and not isinstance(v, dict))])

    def aggregate(self, *a, **k):
        return _Cursor([])

    async def find_one(self, filtro=None, *a, **k):
        for d in self.docs:
            if all(d.get(k2) == v for k2, v in (filtro or {}).items()
                   if not isinstance(v, dict)):
                return dict(d)
        return None

    async def count_documents(self, *a, **k):
        return len(self.docs)

    async def insert_one(self, doc, *a, **k):
        self.docs.append(dict(doc))
        return None

    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    def __init__(self):
        self._colls = {'products': _Coll([PRODUCTO])}

    def _coll(self, nombre):
        return self._colls.setdefault(nombre, _Coll())

    def __getattr__(self, nombre):
        if nombre.startswith('_'):
            raise AttributeError(nombre)
        return self._coll(nombre)

    def __getitem__(self, nombre):
        return self._coll(nombre)


@pytest.fixture
def entorno(monkeypatch):
    """Cliente HTTP por rol, con la paquetería fingida y un registro de llamadas.

    La paquetería se finge porque estas pruebas NO son de Skydropx: son de qué se
    hace con sus tarifas y de quién puede ver qué.
    """
    fake = _FakeDB()
    monkeypatch.setattr(server, 'db', fake)
    llamadas = []

    def _cotizar(destino, paquete, **kw):
        llamadas.append({'destino': dict(destino), 'paquete': dict(paquete), **kw})
        return {'opciones': [dict(t) for t in TARIFAS], 'proveedores': PROVEEDORES,
                'cotizaciones': {}}

    monkeypatch.setattr(paqueterias, 'cotizar_en_todos', _cotizar)
    monkeypatch.setattr(paqueterias, 'cuantos_activos', lambda: 2)

    def _como(user):
        server.app.dependency_overrides.clear()
        if user is not None:
            server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield {'como': _como, 'db': fake, 'llamadas': llamadas}
    server.app.dependency_overrides.clear()


RUTA_ADMIN = '/api/admin/shipping/cotizador'
RUTA_DIST = '/api/distributor/shipping/cotizador'
HISTORIAL_ESPERADO = server.HISTORIAL_COTIZADOR


def cotiza(entorno, quien, ruta, **campos):
    cuerpo = {'postal_code': '06700', 'state': 'CDMX', 'city': 'Ciudad de México',
              'country': 'MX', 'mode': 'items', 'items': [renglon()]}
    cuerpo.update(campos)
    return entorno['como'](quien).post(ruta, json=cuerpo)


# =============================================================================
#  1) CONTESTA LA PREGUNTA: qué cuesta mandar esto a ese CP
# =============================================================================
def test_el_admin_recibe_las_tarifas_de_mas_barata_a_mas_cara(entorno):
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert d['enabled'] is True and d['cobertura'] is True
    precios = [o['price'] for o in d['opciones']]
    assert precios == sorted(precios), 'las opciones no salieron ordenadas por precio'
    assert precios[0] == 139.11


def test_se_marca_cual_gana_y_es_una_sola(entorno):
    """Marcar la ganadora no es adorno: es lo que evita que alguien contrate la
    segunda opción por leer la tabla de corrido."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    ganadoras = [o for o in d['opciones'] if o['recomendada']]
    assert len(ganadoras) == 1
    assert ganadoras[0]['carrier'] == 'Paquetexpress'
    assert ganadoras[0] is d['opciones'][0]


def test_cada_renglon_trae_paqueteria_servicio_dias_y_precio(entorno):
    o = cotiza(entorno, ADMIN, RUTA_ADMIN).json()['opciones'][0]
    assert o['carrier'] and o['service'] and o['days'] >= 0 and o['price'] > 0


def test_el_distribuidor_tambien_puede_cotizar(entorno):
    d = cotiza(entorno, DIST, RUTA_DIST).json()
    assert d['enabled'] is True
    assert [o['price'] for o in d['opciones']] == sorted(o['price'] for o in d['opciones'])


def test_un_cliente_no_entra_a_ninguno_de_los_dos(entorno):
    assert cotiza(entorno, CLIENTE, RUTA_DIST).status_code == 403
    assert cotiza(entorno, CLIENTE, RUTA_ADMIN).status_code == 403


# =============================================================================
#  2) ⛔ EL DISTRIBUIDOR NO VE COSTOS — se intenta sacárselo y tiene que fallar
# =============================================================================
# Lo que la casa paga y con quién lo paga. Si cualquiera de estas cadenas aparece
# en el sobre que recibe un distribuidor, el trabajo está mal hecho.
LO_QUE_NO_PUEDE_VER = ('casa', 'costo_guia', 'absorbe', 'tope_absorcion',
                       'fuera_de_tope', 'proveedor', 'ahorro', 'rate_id',
                       'se_compra_sola', 'margen', 'costo')


def test_el_sobre_del_distribuidor_no_trae_ni_una_palabra_de_costo(entorno):
    """Se lee el JSON COMPLETO como texto, no campo por campo. Un campo nuevo que
    alguien agregue mañana a la respuesta del admin cae aquí solo."""
    crudo = cotiza(entorno, DIST, RUTA_DIST).text
    for prohibida in LO_QUE_NO_PUEDE_VER:
        assert prohibida not in crudo, \
            f'se coló «{prohibida}» en lo que ve el distribuidor: {crudo[:400]}'


def test_el_admin_si_ve_lo_que_la_casa_absorbe(entorno):
    """El espejo de la prueba de arriba: si el admin tampoco lo viera, la prueba
    anterior pasaría por la razón equivocada (porque el dato no existe)."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert d['casa']['costo_guia'] == 139.11
    assert d['casa']['absorbe'] > 0
    assert d['proveedores'] and 'ahorro' in d


def test_el_distribuidor_no_puede_entrar_por_la_puerta_del_admin(entorno):
    """El recorte no sirve de nada si la ruta completa está abierta: se prueba que
    con una sesión de distribuidor la ruta del admin contesta 403."""
    assert cotiza(entorno, DIST, RUTA_ADMIN).status_code == 403


def test_el_recorte_lo_hace_el_servidor_no_la_pantalla(entorno):
    """Las llaves que llegan al navegador del distribuidor son EXACTAMENTE las de la
    lista blanca. Cualquier otra es un dato servido que alguien podría leer en la
    consola aunque la pantalla no lo pinte."""
    d = cotiza(entorno, DIST, RUTA_DIST).json()
    assert set(d) == {'enabled', 'cobertura', 'pais', 'detail', 'peso_kg',
                      'quoted_at', 'opciones', 'cobro'}
    assert set(d['opciones'][0]) == {'carrier', 'service', 'days', 'price', 'recomendada'}
    assert set(d['cobro']) == {'mercancia', 'cliente_paga', 'gratis',
                               'envio_gratis_desde', 'tarifa_plana', 'falta_para_gratis',
                               'productos_sin_precio'}


def test_el_historial_del_distribuidor_es_solo_suyo(entorno):
    """A dónde y cuánto vende otro distribuidor no es asunto de nadie más."""
    cotiza(entorno, DIST, RUTA_DIST)
    cotiza(entorno, OTRO_DIST, RUTA_DIST)
    cotiza(entorno, ADMIN, RUTA_ADMIN)
    mio = entorno['como'](DIST).get('/api/distributor/shipping/cotizador/historial').json()
    assert len(mio['historial']) == 1
    assert {h['user_id'] for h in mio['historial']} == {DIST['id']}
    delcasa = entorno['como'](ADMIN).get('/api/admin/shipping/cotizador/historial').json()
    assert len(delcasa['historial']) == 3, 'el admin sí ve todas'


# =============================================================================
#  3) LA POLÍTICA DE COBRO — se CONSULTA, no se reescribe
# =============================================================================
def test_abajo_de_la_minima_el_cliente_paga_la_tarifa_plana(entorno):
    """Un pedido de $3,000... no: de una pieza de $3,000 sí pasa la mínima. Aquí se
    cotiza un bulto manual con mercancía de $879, que es el caso de todos los días."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=1,
               merchandise_mxn=879).json()
    assert d['cobro']['cliente_paga'] == server.SHIPPING_FLAT
    assert d['cobro']['gratis'] is False
    assert d['cobro']['falta_para_gratis'] == round(server.FREE_SHIPPING_FROM - 879, 2)


def test_arriba_de_la_minima_y_envio_barato_sale_gratis(entorno):
    """$6,000 de mercancía: el 5% son $300 y la guía cuesta $139.11 — la casa lo
    absorbe entero y el cliente no paga envío."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=1,
               merchandise_mxn=6000).json()
    assert d['cobro']['cliente_paga'] == 0
    assert d['cobro']['gratis'] is True
    assert d['casa']['absorbe'] == 139.11


def test_arriba_de_la_minima_con_envio_caro_el_cliente_paga_la_diferencia(entorno,
                                                                          monkeypatch):
    """$3,000 de mercancía con una guía de $600: la casa pone su PISO ($250 — el
    mayor entre $250 y el 5%, regla del 2026-08-02) y el cliente los otros $350."""
    caras = [dict(TARIFAS[0], precio=600.0)]
    monkeypatch.setattr(paqueterias, 'cotizar_en_todos',
                        lambda d, p, **k: {'opciones': [dict(t) for t in caras],
                                           'proveedores': PROVEEDORES, 'cotizaciones': {}})
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=1,
               merchandise_mxn=3000).json()
    assert d['cobro']['cliente_paga'] == 350
    assert d['casa']['absorbe'] == 250.0
    assert d['casa']['fuera_de_tope'] == 0


def test_la_politica_es_la_misma_funcion_que_cobra_en_el_checkout(entorno):
    """No se copia la aritmética: se llama a la de `envios`. Si alguien la duplicara,
    el día que cambie la política esta pantalla mentiría."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=1,
               merchandise_mxn=2600).json()
    esperado = envios.cobro_de_envio_al_cliente(139.11, 2600, server.FREE_SHIPPING_FROM,
                                                tarifa_plana=server.SHIPPING_FLAT)
    assert d['cobro']['cliente_paga'] == esperado


def test_los_tres_numeros_de_la_casa_no_se_tocan(entorno):
    """El cotizador CONSULTA la política; no la mueve. Si alguien cambia estos
    números "para que el cotizador salga bonito", esto truena."""
    assert server.SHIPPING_FLAT == 250
    assert server.FREE_SHIPPING_FROM == 2500
    assert envios.TOPE_GUIA_AUTOMATICA_MXN == 400.0


# =============================================================================
#  4) ⛔ EL PRECIO LO PONE EL SERVIDOR
# =============================================================================
def test_en_modo_productos_el_peso_lo_calcula_el_servidor(entorno):
    """El navegador manda un peso ridículo junto con los productos. Se ignora: el
    bulto que se cotiza es el que sale del catálogo."""
    cotiza(entorno, ADMIN, RUTA_ADMIN, peso_kg=0.01, largo_cm=1, ancho_cm=1, alto_cm=1)
    pesado = entorno['llamadas'][-1]['paquete']
    del_catalogo = envios.paquete_del_pedido([renglon()], {'p-reta': PRODUCTO})
    assert pesado['peso_kg'] == del_catalogo['peso_kg'] > 0.01
    assert pesado['largo_cm'] > 1


def test_en_modo_productos_el_importe_tambien_sale_del_catalogo(entorno):
    """El renglón viaja con `price: 1`. Si el servidor le creyera, un carrito de $1
    "pasaría" la compra mínima al revés y la política se calcularía sobre humo."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, items=[renglon(qty=2, price=1)]).json()
    assert d['cobro']['mercancia'] == 6000        # 2 × $3,000 del catálogo


def test_un_producto_que_el_servidor_no_conoce_se_dice_en_voz_alta(entorno):
    """⛔ Su precio NO se toma del navegador —esa es la regla— así que suma cero y el
    importe de compra queda corto. Callarlo sería enseñar un «paga el cliente»
    equivocado con cara de bueno."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN,
               items=[dict(renglon(), product_id='p-fantasma')]).json()
    assert d['cobro']['productos_sin_precio'] == 1
    assert d['cobro']['mercancia'] == 0
    limpia = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert limpia['cobro']['productos_sin_precio'] == 0


def test_el_aviso_del_producto_desconocido_tambien_le_llega_al_distribuidor(entorno):
    d = cotiza(entorno, DIST, RUTA_DIST,
               items=[dict(renglon(), product_id='p-fantasma')]).json()
    assert d['cobro']['productos_sin_precio'] == 1


def test_el_bulto_capturado_a_mano_nunca_baja_del_minimo_que_cobran(entorno):
    """Un peso de 0.1 kg no existe para una paquetería: todas cobran mínimo 1 kg.
    Aceptarlo sería enseñar un precio que el mostrador no va a respetar."""
    cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=0.1)
    assert entorno['llamadas'][-1]['paquete']['peso_kg'] == envios.PESO_MINIMO_KG


def test_un_bulto_a_mano_sin_medidas_no_se_cotiza_contra_ceros(entorno):
    """Medidas en cero = la paquetería cotiza contra nada y recobra en el mostrador.
    Se cae a la caja que le toca a ese peso."""
    cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=3, largo_cm=0,
           ancho_cm=0, alto_cm=0)
    p = entorno['llamadas'][-1]['paquete']
    assert p['largo_cm'] > 0 and p['ancho_cm'] > 0 and p['alto_cm'] > 0


def test_la_cotizacion_del_cotizador_no_vive_donde_la_que_cobra(entorno):
    """⛔ EL CANDADO. En modo manual el peso lo teclea una persona; si estas
    cotizaciones compartieran colección con las del checkout, un id de aquí podría
    acabar pagando un envío de verdad. Están separadas y así se queda."""
    assert server.COLECCION_COTIZADOR != server.COLECCION_COTIZACIONES
    cotiza(entorno, ADMIN, RUTA_ADMIN, mode='manual', peso_kg=0.1)
    assert entorno['db'][server.COLECCION_COTIZADOR].docs, 'no se guardó el historial'
    assert not entorno['db'][server.COLECCION_COTIZACIONES].docs, \
        'el cotizador escribió en la colección que cobra el checkout'


# =============================================================================
#  5) CUANDO NO SE PUEDE: que lo diga con todas sus letras
# =============================================================================
def test_fuera_de_mexico_lo_dice_y_ni_siquiera_pregunta(entorno):
    """Skydropx sólo cubre México. Preguntarle por Madrid es dejar la rueda girando
    hasta que conteste que no."""
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, country='US', postal_code='90210').json()
    assert d['cobertura'] is False
    assert d['opciones'] == []
    assert 'México' in d['detail'] and 'cobertura' in d['detail'].lower()
    assert entorno['llamadas'] == [], 'se llamó a la paquetería para un destino sin cobertura'


def test_al_distribuidor_tambien_se_le_dice_lo_de_la_cobertura(entorno):
    d = cotiza(entorno, DIST, RUTA_DIST, country='ES', postal_code='28001').json()
    assert d['cobertura'] is False and d['detail']


@pytest.mark.parametrize('pais', ['MX', 'mx', 'México', 'MEXICO', ''])
def test_mexico_se_escribe_de_muchas_formas_y_todas_cuentan(entorno, pais):
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, country=pais).json()
    assert d['cobertura'] is True and d['opciones']


def test_sin_codigo_postal_se_pide_el_codigo_postal(entorno):
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, postal_code='067').json()
    assert d['opciones'] == [] and 'postal' in d['detail'].lower()


def test_sin_nada_que_mandar_se_dice_que_falta(entorno):
    d = cotiza(entorno, ADMIN, RUTA_ADMIN, items=[]).json()
    assert d['opciones'] == [] and d['detail']


def test_sin_credenciales_se_dice_donde_se_pegan(entorno, monkeypatch):
    monkeypatch.setattr(paqueterias, 'cuantos_activos', lambda: 0)
    monkeypatch.setattr(paqueterias, 'encendidos', lambda: [])
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert d['enabled'] is False
    assert 'SKYDROPX_CLIENT_ID' in d['detail']


def test_si_la_paqueteria_truena_no_truena_la_pantalla(entorno, monkeypatch):
    def _explota(*a, **k):
        raise RuntimeError('API caída')
    monkeypatch.setattr(paqueterias, 'cotizar_en_todos', _explota)
    r = cotiza(entorno, ADMIN, RUTA_ADMIN)
    assert r.status_code == 200
    assert r.json()['enabled'] is False and r.json()['detail']


def test_sin_tarifas_para_ese_cp_se_dice_por_que(entorno, monkeypatch):
    monkeypatch.setattr(paqueterias, 'cotizar_en_todos',
                        lambda d, p, **k: {'opciones': [], 'cotizaciones': {},
                                           'proveedores': [{'clave': 'skydropx',
                                                            'nombre': 'Skydropx',
                                                            'detalle': 'sin tarifas'}]})
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert d['opciones'] == [] and 'Skydropx' in d['detail']


# =============================================================================
#  6) EL HISTORIAL CORTO
# =============================================================================
def test_cada_consulta_queda_apuntada(entorno):
    cotiza(entorno, ADMIN, RUTA_ADMIN)
    h = entorno['como'](ADMIN).get('/api/admin/shipping/cotizador/historial').json()
    assert h['historial'][0]['postal_code'] == '06700'
    assert h['historial'][0]['carrier'] == 'Paquetexpress'
    assert h['historial'][0]['price'] == 139.11


def test_el_historial_es_corto_a_proposito(entorno):
    for _ in range(HISTORIAL_ESPERADO + 4):
        cotiza(entorno, ADMIN, RUTA_ADMIN)
    h = entorno['como'](ADMIN).get('/api/admin/shipping/cotizador/historial').json()
    assert len(h['historial']) == HISTORIAL_ESPERADO


def test_que_no_se_pueda_guardar_no_tumba_la_cotizacion(entorno, monkeypatch):
    """La cotización YA está hecha: tumbarla por no poder apuntar el historial sería
    cambiar una molestia por un error."""
    async def _revienta(*a, **k):
        raise RuntimeError('mongo caído')
    monkeypatch.setattr(entorno['db'][server.COLECCION_COTIZADOR], 'insert_one', _revienta)
    d = cotiza(entorno, ADMIN, RUTA_ADMIN).json()
    assert d['opciones']


# =============================================================================
#  7) LOS TEXTOS, EN LOS TRES IDIOMAS
# =============================================================================
# Regla de la casa: todo texto de cara al usuario se escribe en es-MX, en-US y pt-BR
# a la vez. Se comprueba desde aquí —igual que `test_guias.py` compara contra el repo
# de la pantalla— porque una llave que falta no se ve hasta que alguien cambia de
# idioma y le sale el nombre de la llave en pantalla.
def _ruta_i18n(archivo):
    aqui = os.path.dirname(os.path.abspath(__file__))
    for candidato in ('novapeptidos-UI.nosync', 'novapeptidos-UI'):
        ruta = os.path.join(os.path.dirname(aqui), candidato, 'src', 'i18n', archivo)
        if os.path.exists(ruta):
            return ruta
    return ''


def _llaves_del_cotizador(ruta):
    with open(ruta, encoding='utf-8') as f:
        return set(re.findall(r"'(cotizadorEnvio\.[A-Za-z0-9_.]+)'\s*:", f.read()))


def test_los_textos_del_cotizador_estan_en_los_tres_idiomas():
    base = _ruta_i18n('es-MX.js')
    if not base:
        pytest.skip('el repo de la pantalla no está al lado')
    esperadas = _llaves_del_cotizador(base)
    assert esperadas, 'no hay textos cotizadorEnvio.* en es-MX'
    for archivo in ('en-US.js', 'pt-BR.js'):
        ruta = _ruta_i18n(archivo)
        faltan = esperadas - _llaves_del_cotizador(ruta)
        assert not faltan, f'{archivo}: faltan {sorted(faltan)}'


def test_ningun_texto_del_cotizador_habla_de_costos_al_distribuidor():
    """Los textos del admin pueden nombrar el costo; los del distribuidor no. Se
    separan por nombre de llave (`cotizadorEnvio.casa.*` es sólo del admin)."""
    base = _ruta_i18n('es-MX.js')
    if not base:
        pytest.skip('el repo de la pantalla no está al lado')
    with open(base, encoding='utf-8') as f:
        texto = f.read()
    for llave, valor in re.findall(r"'(cotizadorEnvio\.[A-Za-z0-9_.]+)'\s*:\s*'([^']*)'",
                                   texto):
        if llave.startswith('cotizadorEnvio.casa.'):
            continue
        assert 'costo' not in valor.lower(), f'{llave} habla de costo fuera del bloque del admin'
