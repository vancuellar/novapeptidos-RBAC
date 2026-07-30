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

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import server


RUTA = '/api/distributor/quote-caps'

# Catálogo de mentira con un caso de cada cosa que recorta.
PRODUCTOS = [
    {'id': 'p-reta', 'sku': 'RETA-20MG', 'name': 'Retatrutida 20 mg',
     'category': 'metabolicos', 'commission_cap': 0.40, 'distributor_eligible': True},
    {'id': 'p-agua', 'sku': 'AGUA-30ML', 'name': 'Agua bacteriostática 30 mL',
     'category': 'accesorios', 'commission_cap': 0.40, 'distributor_eligible': True},
    {'id': 'p-hgh', 'sku': 'HGH-40IU', 'name': 'HGH 40 IU',
     'category': 'hormona-crecimiento', 'commission_cap': 0.35, 'distributor_eligible': True},
    {'id': 'p-frag', 'sku': 'FRAG-5MG', 'name': 'HGH Fragment 176-191 5 mg',
     'category': 'hormona-crecimiento', 'commission_cap': 0.30, 'distributor_eligible': True},
    {'id': 'p-veto', 'sku': 'LIRA-30MG', 'name': 'Liraglutida 30 mg',
     'category': 'metabolicos', 'commission_cap': 0.50, 'distributor_eligible': False},
    {'id': 'p-flaco', 'sku': 'SOMA-10IU', 'name': 'Somatropina 10 IU',
     'category': 'hormona-crecimiento', 'commission_cap': 0.25, 'distributor_eligible': True},
    {'id': 'p-oculto', 'sku': 'DYS-500', 'name': 'Dysport 500 U',
     'category': 'estetica', 'commission_cap': 0.40, 'distributor_eligible': True,
     'hidden': True},
    # El que trae basura en el tope: no debe tumbar la ruta.
    {'id': 'p-raro', 'sku': None, 'name': 'Producto raro',
     'category': 'bienestar', 'commission_cap': 'no-es-un-numero', 'distributor_eligible': True},
]

DIST = {'id': 'u-dist', 'name': 'Dist', 'email': 'dist@x.mx',
        'role': 'distributor', 'tier': 'junior0'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}


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

    async def find_one(self, *a, **k):
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


def test_tope_de_descuento_nunca_pasa_del_tope_duro():
    assert server.tope_de_descuento({'commission_cap': 9.0}) == server.COMMISSION_CAP
    assert server.tope_de_descuento({'commission_cap': -1}) == 0.0
    assert server.tope_de_descuento({}) == server.COMMISSION_CAP
    assert server.tope_de_descuento(None) == server.COMMISSION_CAP
