"""El COTIZADOR del distribuidor — la regla de oro, probada.

⛔ REGLA DE ORO (Christián, 2026-07-30): ni el distribuidor ni el cliente ven
JAMÁS el costo real, el proveedor ni el ROI. El cotizador vive en el navegador
del distribuidor, así que TODO lo que le manda el servidor es público de facto:
lo que viaje por `/api/distributor/quote-caps` lo puede leer cualquiera que abra
la consola del navegador con su sesión.

Por eso estas pruebas no miran el diseño ni los totales: miran LA PUERTA y EL
SOBRE.

  · La puerta: sin sesión, 401. Con sesión de cliente, 403.
  · El sobre: se lee el payload ENTERO como texto y truena si aparece la palabra
    costo, proveedor, ROI o margen. Un `grep` es tosco a propósito — no depende
    de que alguien se acuerde de actualizar una lista de campos permitidos el día
    que agregue uno nuevo.
  · Y el tope: el que sale por aquí es el MISMO que aplica el checkout, porque
    los dos llaman a `tope_de_descuento`. Si el cotizador prometiera más, el
    cliente vería otro total al pagar.
"""
import json
import os
import re

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import server


RUTA = '/api/distributor/quote-caps'

# Catálogo de mentira con un caso de cada cosa que recorta.
PRODUCTOS = [
    {'id': 'p-reta', 'sku': 'RETA-20MG', 'slug': 'retatrutida-20-mg', 'name': 'Retatrutida 20 mg',
     'category': 'metabolicos', 'commission_cap': 0.40, 'distributor_eligible': True},
    {'id': 'p-agua', 'sku': 'AGUA-30ML', 'slug': 'agua-30-ml', 'name': 'Agua bacteriostática 30 mL',
     'category': 'accesorios', 'commission_cap': 0.40, 'distributor_eligible': True},
    {'id': 'p-hgh', 'sku': 'HGH-40IU', 'slug': 'hgh-40-iu', 'name': 'HGH 40 IU',
     'category': 'hormona-crecimiento', 'commission_cap': 0.35, 'distributor_eligible': True},
    {'id': 'p-frag', 'sku': 'FRAG-5MG', 'slug': 'hgh-fragment-5-mg', 'name': 'HGH Fragment 176-191 5 mg',
     'category': 'hormona-crecimiento', 'commission_cap': 0.30, 'distributor_eligible': True},
    {'id': 'p-veto', 'sku': 'LIRA-30MG', 'slug': 'liraglutida-30-mg', 'name': 'Liraglutida 30 mg',
     'category': 'metabolicos', 'commission_cap': 0.50, 'distributor_eligible': False},
    {'id': 'p-flaco', 'sku': 'SOMA-10IU', 'slug': 'somatropina-10-iu', 'name': 'Somatropina 10 IU',
     'category': 'hormona-crecimiento', 'commission_cap': 0.25, 'distributor_eligible': True},
    {'id': 'p-oculto', 'sku': 'DYS-500', 'slug': 'dysport-500-u', 'name': 'Dysport 500 U',
     'category': 'estetica', 'commission_cap': 0.40, 'distributor_eligible': True,
     'hidden': True},
    # El que trae basura en el tope: no debe tumbar la ruta.
    {'id': 'p-raro', 'sku': None, 'slug': 'producto-raro', 'name': 'Producto raro',
     'category': 'bienestar', 'commission_cap': 'no-es-un-numero', 'distributor_eligible': True},
    # Tope POR DEBAJO del techo de cliente: el único caso en que el catálogo
    # público publica un número. Hoy no existe ninguno así, pero el día que exista
    # el carrito tiene que enterarse o le promete al cliente lo que no hay.
    {'id': 'p-apretado', 'sku': 'APR-1MG', 'slug': 'apretado-1-mg', 'name': 'Apretado 1 mg',
     'category': 'bienestar', 'commission_cap': 0.08, 'distributor_eligible': True},
]

DIST = {'id': 'u-dist', 'name': 'Dist', 'email': 'dist@x.mx',
        'role': 'distributor', 'tier': 'junior0'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}
# Cliente con trato especial (el caso de Paz Cambray, 40% aunque sea sólo cliente).
CONSENTIDO = {'id': 'u-paz', 'name': 'Paz', 'email': 'paz@x.mx', 'role': 'user',
              'personal_discount_rate': 0.40}


# --------------------------------------------------------- base de datos falsa
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Coll:
    def __init__(self, docs=()):
        self._docs = docs

    def find(self, *a, **k):
        return _Cursor(self._docs)

    def aggregate(self, *a, **k):
        return _Cursor([])

    async def find_one(self, filtro=None, *a, **k):
        slug = (filtro or {}).get('slug')
        for d in self._docs:
            if slug and d.get('slug') == slug:
                return dict(d)
        return None

    async def count_documents(self, *a, **k):
        return 0

    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    def __getattr__(self, name):
        return _Coll(PRODUCTOS if name == 'products' else ())


@pytest.fixture
def como(monkeypatch):
    """`como(usuario)` = cliente HTTP autenticado como ese usuario. `como(None)`
    es un visitante sin sesión: la dependencia real decide, y contesta 401."""
    monkeypatch.setattr(server, 'db', _FakeDB())

    def _factory(user):
        if user is None:
            server.app.dependency_overrides.clear()
        else:
            server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


# ------------------------------------------------------------------- la puerta
def test_sin_sesion_no_pasa(como):
    assert como(None).get(RUTA).status_code == 401


def test_un_cliente_no_pasa(como):
    assert como(CLIENTE).get(RUTA).status_code == 403


def test_el_distribuidor_si_pasa(como):
    assert como(DIST).get(RUTA).status_code == 200


# -------------------------------------------------------------------- el sobre
# Lo que de verdad se protege: que por aquí no salga NADA del costo.
PROHIBIDO = ('cost', 'costo', 'proveedor', 'provider', 'supplier', 'roi',
             'margen', 'margin', 'utilidad', 'ganancia', 'compra', 'lab')


def test_el_costo_no_viaja(como):
    """Se lee el payload ENTERO como texto plano y se busca la palabra. Tosco a
    propósito: no depende de que nadie mantenga una lista de campos permitidos."""
    crudo = json.dumps(como(DIST).get(RUTA).json(), ensure_ascii=False).lower()
    for palabra in PROHIBIDO:
        assert palabra not in crudo, f'el payload del cotizador trae "{palabra}"'


def test_solo_salen_dos_campos_por_renglon(como):
    caps = como(DIST).get(RUTA).json()['caps']
    assert caps, 'el cotizador se quedaría sin topes'
    for fila in caps:
        assert set(fila) == {'product_id', 'discount_cap'}


def test_no_salen_ni_precios_ni_nombres(como):
    """Ni el nombre del producto: el catálogo público ya lo trae, y todo campo de
    más es una puerta por la que mañana entra un costo."""
    cuerpo = como(DIST).get(RUTA).json()
    assert set(cuerpo) == {'max_discount', 'caps'}


# --------------------------------------------------------------------- el tope
def _topes(cliente):
    return {f['product_id']: f['discount_cap'] for f in cliente.get(RUTA).json()['caps']}


def test_el_tope_de_cada_producto_es_el_del_checkout(como):
    t = _topes(como(DIST))
    assert t['p-reta'] == 0.40           # producto normal: su tope tal cual
    assert t['p-agua'] == 0.0            # insumo: NUNCA descuento
    assert t['p-hgh'] == 0.0             # familia HGH: precio neto
    assert t['p-frag'] == 0.30           # el Fragment SÍ participa
    assert t['p-veto'] == 0.0            # fuera del canal (no deja 5x neto)
    assert t['p-flaco'] == 0.25          # tope flaco: recorta al cotizador


def test_el_tope_viaja_con_el_id_y_con_el_sku(como):
    """El carrito nombra al producto a veces con su UUID y a veces con su SKU."""
    t = _topes(como(DIST))
    assert t['p-reta'] == t['RETA-20MG'] == 0.40


def test_lo_oculto_no_se_cotiza(como):
    t = _topes(como(DIST))
    assert 'p-oculto' not in t and 'DYS-500' not in t


def test_un_tope_con_basura_no_tumba_la_ruta(como):
    t = _topes(como(DIST))
    assert t['p-raro'] == server.COMMISSION_CAP


def test_su_tasa_es_la_misma_que_la_de_sus_codigos(como):
    """25% con la base de 30% del canal — el mismo número que ya ve en 'Mis
    Códigos'. Se compara contra la pirámide, no contra un 0.25 escrito a mano."""
    import pyramid
    cuerpo = como(DIST).get(RUTA).json()
    esperado = max(pyramid.discount_tiers_for(pyramid.effective_rate(DIST)))
    assert cuerpo['max_discount'] == esperado == 0.25


# ------------------------------------- la regla es UNA, no una copia por lado
def test_el_checkout_y_el_cotizador_usan_la_misma_funcion():
    """Si el checkout dejara de llamar a `es_hgh_neto`, las dos reglas se separan
    y el cotizador promete lo que la caja no respeta."""
    import inspect
    fuente = inspect.getsource(server.create_order)
    assert 'es_hgh_neto' in fuente


# ============================================================================
#  EL CATÁLOGO PÚBLICO NO PUBLICA MÁRGENES  (Christián, 2026-07-30)
# ============================================================================
# `/api/products` devolvía el documento entero, con `commission_cap` y
# `distributor_eligible` dentro, SIN sesión. No es el costo, pero dice cuánto
# margen aguanta cada producto y cuáles no dejan 5x. Ahora sale recortado.

# Palabras completas, no pedazos: el catálogo trae descripciones de verdad y
# "esteroidogénesis" contiene "roi". Sin el \b la prueba sería puro ruido y a la
# tercera falsa alarma alguien la apaga — que es justo cuando deja de proteger.
MARGENES_PROHIBIDOS = ('commission_cap', 'distributor_eligible', 'commission',
                       'comision', 'comisión', 'cap', 'caps', 'margen', 'margin',
                       'roi', 'costo', 'cost', 'proveedor', 'provider', 'supplier')


def _sin_margenes(payload, donde):
    crudo = json.dumps(payload, ensure_ascii=False).lower()
    for palabra in MARGENES_PROHIBIDOS:
        assert not re.search(rf'\b{palabra}\b', crudo), f'{donde} trae "{palabra}"'


def _publico(como):
    """El catálogo tal como lo recibe alguien SIN sesión."""
    r = como(None).get('/api/products')
    assert r.status_code == 200
    return r.json()


def test_el_catalogo_publico_no_lleva_margenes(como):
    """Se lee el payload ENTERO como texto, igual que la prueba del cotizador."""
    _sin_margenes(_publico(como), 'el catálogo público')


def test_la_ficha_publica_de_un_producto_tampoco(como):
    r = como(None).get('/api/products/retatrutida-20-mg')
    assert r.status_code == 200
    _sin_margenes(r.json(), 'la ficha pública')


def test_la_prueba_de_fugas_sirve_de_verdad(como):
    """Candado de la candado: si el filtro dejara pasar `commission_cap`, la
    prueba de arriba TIENE que tronar. Sin esto no sabríamos si protege o si
    simplemente nunca encuentra nada."""
    crudo = _publico(como)
    crudo[0]['commission_cap'] = 0.40
    with pytest.raises(AssertionError):
        _sin_margenes(crudo, 'prueba')


def test_el_catalogo_publico_dice_que_NO_lleva_descuento(como):
    """Lo único que el carrito anónimo necesita: en qué renglones no hay descuento.
    Eso el cliente lo ve igual en su carrito; no revela ningún margen."""
    por_id = {p['id']: p for p in _publico(como)}
    assert por_id['p-agua']['descuentable'] is False      # insumo
    assert por_id['p-hgh']['descuentable'] is False       # familia HGH, precio neto
    assert por_id['p-veto']['descuentable'] is False      # fuera del canal
    assert por_id['p-reta']['descuentable'] is True
    assert por_id['p-frag']['descuentable'] is True       # el Fragment sí participa


def test_solo_publica_un_numero_cuando_el_tope_no_llega_al_techo_de_cliente(como):
    por_id = {p['id']: p for p in _publico(como)}
    # 8% aguanta menos que el techo de cliente: se publica, o el carrito miente.
    assert por_id['p-apretado']['max_descuento_cliente'] == 0.08
    # 25% y 40% aguantan de sobra: afuera no se dice cuánto. Ese silencio ES la regla.
    assert 'max_descuento_cliente' not in por_id['p-flaco']
    assert 'max_descuento_cliente' not in por_id['p-reta']
    # Y lo que no lleva descuento tampoco publica número: con la bandera basta.
    assert 'max_descuento_cliente' not in por_id['p-agua']


def test_el_numero_publicado_nunca_pasa_del_techo_de_cliente(como):
    """Aunque un producto aguantara 40%, afuera jamás se ve más de 15%."""
    for p in _publico(como):
        if 'max_descuento_cliente' in p:
            assert p['max_descuento_cliente'] <= server.TECHO_DESCUENTO_CLIENTE


def test_el_catalogo_publico_sigue_trayendo_lo_de_vender(como):
    """Quitar de más rompe la tienda: el precio, el nombre y el SKU siguen ahí."""
    p = {x['id']: x for x in _publico(como)}['p-reta']
    for campo in ('id', 'sku', 'slug', 'name', 'category'):
        assert campo in p, f'falta {campo} en el catálogo público'


def test_la_vista_publica_no_toca_el_documento_original():
    """Se copia: si mutara el dict, el checkout se quedaría sin el tope real."""
    doc = {'id': 'x', 'name': 'X', 'commission_cap': 0.40, 'distributor_eligible': True}
    server.vista_publica_de_producto(doc)
    assert doc['commission_cap'] == 0.40 and doc['distributor_eligible'] is True


# ------------------- los topes reales, sólo para quien los necesita
def test_ni_siquiera_un_cliente_con_trato_especial_recibe_los_topes(como):
    """Paz Cambray tiene 40% de descuento propio y aun así NO ve los topes: su
    carrito no usa su tasa (ver `selfRate` en CartContext, que sólo mira a los
    distribuidores), así que no le hacen falta. Menos gente con el dato, mejor."""
    assert como(CONSENTIDO).get(RUTA).status_code == 403


def test_tope_de_descuento_nunca_pasa_del_tope_duro():
    assert server.tope_de_descuento({'commission_cap': 9.0}) == server.COMMISSION_CAP
    assert server.tope_de_descuento({'commission_cap': -1}) == 0.0
    assert server.tope_de_descuento({}) == server.COMMISSION_CAP
    assert server.tope_de_descuento(None) == server.COMMISSION_CAP
