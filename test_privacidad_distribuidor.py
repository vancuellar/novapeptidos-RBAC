"""EL CLIENTE NO SE ENTERA DE QUIÉN ES EL CÓDIGO — probado con dientes.

⛔ ORDEN DE CHRISTIÁN (2026-07-31): «Los clientes no pueden ver que el código de
descuento es de María». Ni su nombre, ni su correo, ni su id. Quien atiende al
cliente es la atención de la casa (`emails.ATENCION_NOMBRE`).

Por qué existe este archivo, y no una revisión a ojo: el rastro del distribuidor
no se asomaba en un solo lugar, se asomaba en cuatro, y tres de ellos eran datos
que el servidor MANDABA sin que ninguna pantalla los pintara —o sea, invisibles
salvo abriendo la consola del navegador:

  1. el correo de cotización, que saludaba «María preparó esta cotización» y
     mandaba la RESPUESTA al correo personal de María;
  2. el pedido que devuelve el checkout, con `referred_by` y el reparto de
     comisiones pegados;
  3. el mismo pedido en `/orders/me` y en `/orders/{numero}` —esta última SIN
     SESIÓN, la más expuesta de todas;
  4. y el texto del propio código (`MARIAN-15-XXXX`), que se decidió aparte.

Las pruebas leen el SOBRE COMPLETO —el JSON o el HTML como texto plano— y truenan
si aparece el nombre, el correo o el id. Es tosco a propósito: así no depende de
que nadie se acuerde de actualizar una lista de campos permitidos el día que
agregue uno nuevo.

Lo que NO se toca: el admin y el propio distribuidor siguen viendo todo lo suyo
por sus rutas (`/admin/...`, `/distributor/...`), que no pasan por este candado.
"""
import json
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import emails
import server


# La distribuidora del caso real, con todo lo que la delata.
DIST = {'id': 'u-maria-9f3a', 'name': 'Maria Neunfeld', 'email': 'maria@exygenlabs.com',
        'role': 'distributor', 'tier': 'junior0', 'distributor_code': 'MARI-3537',
        'customer_discount_rate': 0.15}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}

# Las cadenas que NUNCA pueden aparecer en algo que ve un cliente.
LA_DELATAN = ('Maria Neunfeld', 'maria@exygenlabs.com', 'u-maria-9f3a')

CODIGO = 'MARIAN-15-R4YV'

PRODUCTO = {'id': 'p-reta', 'sku': 'RETA-20MG', 'slug': 'retatrutida-20-mg',
            'name': 'Retatrutida 20 mg', 'category': 'metabolicos',
            'commission_cap': 0.40, 'distributor_eligible': True,
            'price': 3000, 'presentation': '20 mg', 'stock': 50}

PEDIDO = {
    'id': 'o-1', 'order_number': 'EX-20260731-1111', 'user_id': CLIENTE['id'],
    'items': [{'product_id': 'p-reta', 'name': 'Retatrutida 20 mg', 'price': 3000,
               'quantity': 1, 'presentation': '20 mg', 'image_url': ''}],
    'customer': {'full_name': 'Cliente', 'email': 'cli@x.mx', 'phone': '81',
                 'address': 'x', 'city': 'Mérida', 'state': 'Yucatán',
                 'postal_code': '97000', 'country': 'MX'},
    'payment_method': 'spei', 'subtotal': 3000, 'discount': 450, 'discount_rate': 0.15,
    'shipping': 0, 'total': 2550, 'status': 'pendiente', 'created_at': '2026-07-31T10:00:00',
    # LO QUE DELATA: quién lo refirió y cuánto ganó cada quien.
    'referred_by': DIST['id'], 'commission': 450,
    'commissions': [{'distributor_id': DIST['id'], 'role': 'seller',
                     'rate': 0.30, 'amount': 450}],
}


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
        self._docs = list(docs)

    def find(self, *a, **k):
        return _Cursor(self._docs)

    def aggregate(self, *a, **k):
        return _Cursor([])

    async def find_one(self, filtro=None, *a, **k):
        for d in self._docs:
            if all(d.get(k2) == v for k2, v in (filtro or {}).items()
                   if not isinstance(v, dict)):
                return dict(d)
        return None

    async def count_documents(self, *a, **k):
        return len(self._docs)

    async def insert_one(self, *a, **k):
        return None

    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    """Sólo lo que estas rutas tocan: el catálogo, los pedidos, los usuarios y
    los códigos. Todo lo demás sale vacío."""

    def __getattr__(self, nombre):
        if nombre == 'products':
            return _Coll([PRODUCTO])
        if nombre == 'orders':
            return _Coll([PEDIDO])
        if nombre == 'users':
            return _Coll([DIST, CLIENTE])
        if nombre == 'discount_codes':
            return _Coll([{'id': 'c-1', 'code': CODIGO, 'distributor_id': DIST['id'],
                           'discount_rate': 0.15, 'active': True,
                           'created_at': '2026-07-01T00:00:00', 'expires_at': '2099-01-01T00:00:00'}])
        return _Coll()


@pytest.fixture
def como(monkeypatch):
    """`como(usuario)` = cliente HTTP con esa sesión. `como(None)` es un visitante
    sin sesión: eso es lo que ve cualquiera en internet."""
    monkeypatch.setattr(server, 'db', _FakeDB())

    def _factory(user):
        if user is None:
            server.app.dependency_overrides.clear()
        else:
            server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


def sin_rastro(texto, donde):
    """Truena nombrando la cadena que se coló y dónde. El mensaje importa: la
    prueba la va a leer alguien seis meses después de que se rompa."""
    for delata in LA_DELATAN:
        assert delata not in texto, f'{donde}: se coló «{delata}» en algo que ve el cliente'


# =============================================================================
#  1) EL VALIDADOR PÚBLICO DEL CÓDIGO  — lo primero, porque no pide sesión
# =============================================================================
RUTA_CODIGO = '/api/discount-code/'


def test_el_validador_no_pide_sesion(como):
    """No es un descuido: el carrito lo consulta antes de que nadie entre. Por eso
    mismo, lo que devuelva es público de facto."""
    assert como(None).get(RUTA_CODIGO + CODIGO).status_code == 200


def test_el_validador_solo_devuelve_el_porcentaje(como):
    d = como(None).get(RUTA_CODIGO + CODIGO).json()
    assert d['discount_rate'] == 0.15
    assert set(d) <= {'code', 'discount_rate', 'min_order'}, \
        f'campos de más en el validador público: {sorted(set(d) - {"code", "discount_rate", "min_order"})}'


def test_el_validador_no_delata_a_quien_es_el_codigo(como):
    r = como(None).get(RUTA_CODIGO + CODIGO)
    sin_rastro(r.text, 'GET /discount-code')


def test_el_validador_tampoco_delata_con_el_codigo_unico(como):
    """El camino viejo: el `distributor_code` de la ficha del distribuidor. Resuelve
    por otra rama del código y también tiene que salir pelón."""
    r = como(None).get(RUTA_CODIGO + DIST['distributor_code'])
    assert r.status_code == 200
    sin_rastro(r.text, 'GET /discount-code (código único)')


def test_un_codigo_inventado_no_dice_nada(como):
    r = como(None).get(RUTA_CODIGO + 'NOEXISTE-99-XXXX')
    assert r.status_code == 404
    sin_rastro(r.text, 'GET /discount-code (inventado)')


# =============================================================================
#  2) EL PEDIDO QUE VE EL CLIENTE
# =============================================================================
def test_el_pedido_del_cliente_va_sin_los_campos_del_distribuidor():
    limpio = server.pedido_para_el_cliente(PEDIDO)
    for campo in server.CAMPOS_DEL_DISTRIBUIDOR:
        assert campo not in limpio, f'{campo} sigue viajando al cliente'
    # Y lo que sí necesita para leerse como un pedido sigue ahí.
    assert limpio['order_number'] == PEDIDO['order_number']
    assert limpio['total'] == 2550


def test_limpiar_el_pedido_no_toca_el_original():
    """Un `pop` sobre el documento de la base borraría la comisión de verdad: el
    distribuidor dejaría de cobrar por haber consultado su propio pedido."""
    server.pedido_para_el_cliente(PEDIDO)
    assert PEDIDO['referred_by'] == DIST['id']
    assert PEDIDO['commissions'][0]['amount'] == 450


def test_el_rastreo_publico_no_delata_al_distribuidor(como):
    """`/orders/{numero}` NO PIDE SESIÓN —el que compró como invitado no tiene
    cuenta— así que es la ruta más expuesta de las tres."""
    r = como(None).get(f'/api/orders/{PEDIDO["order_number"]}')
    assert r.status_code == 200
    sin_rastro(r.text, 'GET /orders/{numero}')
    d = r.json()
    assert 'referred_by' not in d and 'commissions' not in d


def test_mis_pedidos_no_delatan_al_distribuidor(como):
    r = como(CLIENTE).get('/api/orders/me')
    assert r.status_code == 200
    sin_rastro(r.text, 'GET /orders/me')
    assert all('commissions' not in o for o in r.json())


def test_el_cliente_no_ve_cuanto_gano_nadie(como):
    """No es sólo la identidad: cuánto se lleva el canal es dinero de la casa."""
    d = como(None).get(f'/api/orders/{PEDIDO["order_number"]}').json()
    assert 'commission' not in d
    assert '450' not in json.dumps(d.get('commissions', []))


# =============================================================================
#  3) EL CORREO DE COTIZACIÓN  — el que más delataba
# =============================================================================
COTIZACION = {
    'folio': 'COT-260731-1234', 'client_name': 'Juan Pérez',
    'code': CODIGO, 'link': 'https://exygenlabs.com/checkout?pedido=p-reta:1',
    'lines': [{'name': 'Retatrutida 20 mg', 'quantity': 1, 'unit_price': 2550,
               'amount': 2550, 'list_price': 3000}],
    'list_total': 3000, 'savings': 450, 'total': 2550,
}


def _html(lang='es', **extra):
    return emails._quote_email_html(emails.QUOTE_COPY[lang], dict(COTIZACION, **extra))


@pytest.mark.parametrize('lang', ['es', 'en', 'pt'])
def test_la_cotizacion_la_firma_la_casa_en_los_tres_idiomas(lang):
    assert emails.ATENCION_NOMBRE in _html(lang)


@pytest.mark.parametrize('lang', ['es', 'en', 'pt'])
def test_la_cotizacion_no_nombra_al_distribuidor(lang):
    sin_rastro(_html(lang), f'correo de cotización ({lang})')


def test_aunque_le_metan_el_nombre_a_la_fuerza_no_sale():
    """EL CANDADO ESTÁ EN EL QUE ARMA EL HTML, no en quien lo llama. Si mañana
    alguien vuelve a meter el nombre del distribuidor en el diccionario de la
    cotización, no hay por dónde salga."""
    html = _html(advisor='Maria Neunfeld', distributor_email='maria@exygenlabs.com')
    sin_rastro(html, 'correo de cotización con el nombre inyectado')
    assert emails.ATENCION_NOMBRE in html


def test_la_atencion_de_la_casa_no_lleva_el_apellido_de_la_broma():
    """«Galindo» era una broma privada de Christián, no un apellido. No puede
    aparecer en nada que vea un cliente."""
    assert 'Galindo' not in emails.ATENCION_NOMBRE
    for lang in ('es', 'en', 'pt'):
        assert 'Galindo' not in _html(lang)


def test_el_correo_sigue_diciendo_lo_que_importa():
    """Tapar al distribuidor no puede tapar la cotización: el código, el total y
    el enlace tienen que seguir ahí o el correo no sirve para nada."""
    html = _html()
    assert CODIGO in html
    assert 'Juan Pérez'.upper() in html or 'Juan Pérez' in html
    assert 'exygenlabs.com/checkout' in html
