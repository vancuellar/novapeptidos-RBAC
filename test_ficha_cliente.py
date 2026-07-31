"""LA FICHA DE UN CLIENTE — la misma información, se abra desde donde se abra.

Lo que se prueba aquí NO es que la ficha se vea bonita: es QUIÉN puede abrirla y QUÉ
le toca ver. El id del cliente viaja en la URL, así que cualquiera con sesión puede
teclear el de otro. El candado tiene que estar en el servidor:

  · sin sesión                       → 401
  · un cliente cualquiera            → 403 (la ficha no es para él)
  · un distribuidor, cliente ajeno   → 403
  · un distribuidor, cliente suyo    → 200, pero SÓLO sus pedidos con él y sin
                                       puntos, cupones, notas ni quién lo refirió
  · el admin                         → 200 con todo
  · el invitado sin cuenta (Aidee)   → abre igual, con lo que hay en sus pedidos

No toca Mongo: la base se sustituye por una falsa en memoria y la identidad se
inyecta con dependency_overrides.
"""
import json
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import server


# ------------------------------------------------------- base falsa en memoria
def _coincide(doc, filtro):
    """Igualdad simple, más `$or`, que es lo que usan las consultas de la ficha.

    El `$or` hace falta de verdad: `_usuario_por_correo` busca por el correo principal
    O por uno alterno (`{'$or': [{'email': e}, {'alt_emails': e}]}`), y sin esto la base
    falsa devolvía None siempre — la prueba pasaba por el camino equivocado.

    `alt_emails` puede ser texto suelto o lista: la comparación acepta las dos, igual
    que Mongo."""
    for k, v in (filtro or {}).items():
        if k == '$or':
            if not any(_coincide(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict):        # {'$ne': ...} y compañía: aquí no hacen falta
            return False
        actual = doc.get(k)
        if isinstance(actual, list):
            if v not in actual:
                return False
        elif actual != v:
            return False
    return True


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
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, filtro=None, proj=None):
        return _Cursor([d for d in self.docs if _coincide(d, filtro)])

    async def find_one(self, filtro=None, proj=None):
        for d in self.docs:
            if _coincide(d, filtro):
                return dict(d)
        return None

    async def update_one(self, filtro, cambio, upsert=False):
        for d in self.docs:
            if _coincide(d, filtro):
                d.update((cambio or {}).get('$set') or {})
                return type('R', (), {'matched_count': 1})()
        if upsert:
            nuevo = dict(filtro)
            nuevo.update((cambio or {}).get('$set') or {})
            self.docs.append(nuevo)
            return type('R', (), {'matched_count': 0})()
        return type('R', (), {'matched_count': 0})()


class _FakeDB:
    def __init__(self, **colecciones):
        self._c = {k: _Coll(v) for k, v in colecciones.items()}

    def __getattr__(self, name):
        return self._c.setdefault(name, _Coll())


# ------------------------------------------------------------------- el elenco
ADMIN = {'id': 'u-admin', 'name': 'Christián', 'email': 'admin@exygenlabs.com', 'role': 'admin'}
DIST_A = {'id': 'u-dist-a', 'name': 'María', 'email': 'maria@x.mx', 'role': 'distributor',
          'distributor_code': 'MARIA'}
DIST_B = {'id': 'u-dist-b', 'name': 'Otro', 'email': 'otro@x.mx', 'role': 'distributor',
          'distributor_code': 'OTRO'}
# Ana trae un correo ALTERNO, como la cuenta de la casa después de la fusión: quien
# compró como invitada con esa dirección es ELLA, no otra persona.
CLI_A = {'id': 'u-cli-a', 'name': 'Ana Cliente', 'email': 'ana@x.mx', 'role': 'user',
         'alt_emails': ['ana.vieja@x.mx'],
         'referred_by': 'u-dist-a', 'points_balance': 120, 'created_at': '2026-01-01T00:00:00',
         'phone': '+52 55 1111 1111'}
CLI_B = {'id': 'u-cli-b', 'name': 'Beto Cliente', 'email': 'beto@x.mx', 'role': 'user',
         'referred_by': 'u-dist-b', 'points_balance': 0, 'created_at': '2026-02-01T00:00:00'}

CONTACTO_ANA = {'full_name': 'Ana Cliente', 'email': 'ana@x.mx', 'phone': '+52 55 1111 1111',
                'address': 'Calle 1', 'city': 'CDMX', 'state': 'CDMX', 'postal_code': '01000'}
CONTACTO_AIDEE = {'full_name': 'Aidee Invitada', 'email': 'aidee@correo.com',
                  'phone': '+52 55 9999 9999', 'address': 'Calle 9', 'city': 'Monterrey',
                  'state': 'NL', 'postal_code': '64000'}

# Pedido de Ana CON el código de María.
O1 = {'id': 'o1', 'order_number': 'EX-1', 'user_id': 'u-cli-a', 'referred_by': 'u-dist-a',
      'status': 'entregado', 'paid': True, 'total': 2000, 'created_at': '2026-07-01T00:00:00',
      'payment_method': 'spei', 'customer': CONTACTO_ANA,
      'items': [{'name': 'Retatrutida 10mg', 'quantity': 2}],
      'commissions': [{'distributor_id': 'u-dist-a', 'amount': 400, 'role': 'seller'}]}
# Pedido de Ana POR SU CUENTA: es de ella, pero NO es asunto de María.
O2 = {'id': 'o2', 'order_number': 'EX-2', 'user_id': 'u-cli-a', 'status': 'entregado',
      'paid': False, 'total': 500, 'created_at': '2026-07-10T00:00:00',
      'payment_method': 'tarjeta', 'customer': CONTACTO_ANA,
      'items': [{'name': 'Retatrutida 10mg', 'quantity': 1},
                {'name': 'BPC-157 5mg', 'quantity': 1}]}
# Pedido de Beto, cliente del OTRO distribuidor.
O3 = {'id': 'o3', 'order_number': 'EX-3', 'user_id': 'u-cli-b', 'referred_by': 'u-dist-b',
      'status': 'entregado', 'paid': True, 'total': 900, 'created_at': '2026-07-05T00:00:00',
      'payment_method': 'spei', 'customer': {'full_name': 'Beto Cliente', 'email': 'beto@x.mx'},
      'items': [{'quantity': 1}]}
# Aidee: compró con el código de María SIN abrir cuenta.
O4 = {'id': 'o4', 'order_number': 'EX-4', 'referred_by': 'u-dist-a', 'status': 'entregado',
      'paid': True, 'total': 2830, 'created_at': '2026-07-30T00:00:00', 'payment_method': 'spei',
      'customer': CONTACTO_AIDEE, 'items': [{'name': 'Semaglutida 5mg', 'quantity': 3}],
      'commissions': [{'distributor_id': 'u-dist-a', 'amount': 780, 'role': 'seller'}]}


@pytest.fixture
def como(monkeypatch):
    """`como(usuario)` devuelve un cliente HTTP ya autenticado como ese usuario."""
    monkeypatch.setattr(server, 'db', _FakeDB(
        users=[ADMIN, DIST_A, DIST_B, CLI_A, CLI_B],
        orders=[O1, O2, O3, O4],
        discount_codes=[{'code': 'GIFT-ABC', 'kind': 'coupon', 'user_id': 'u-cli-a',
                         'discount_rate': 0.1, 'used': False, 'active': True}],
        points=[{'user_id': 'u-cli-a', 'type': 'gift', 'points': 120,
                 'created_at': '2026-07-02T00:00:00'}],
        client_notes=[],
    ))

    def _factory(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


def ficha(cliente, quien_id):
    return f'/api/clientes/{quien_id}/ficha'


# ------------------------------------------------------------- sin sesión: 401
def test_sin_sesion_no_hay_ficha(monkeypatch):
    monkeypatch.setattr(server, 'db', _FakeDB(users=[CLI_A], orders=[O1]))
    r = TestClient(server.app).get('/api/clientes/u-cli-a/ficha')
    assert r.status_code == 401


def test_sin_sesion_tampoco_la_nota(monkeypatch):
    monkeypatch.setattr(server, 'db', _FakeDB(users=[CLI_A]))
    r = TestClient(server.app).put('/api/clientes/u-cli-a/ficha')
    assert r.status_code in (401, 405)
    assert TestClient(server.app).put('/api/admin/clientes/u-cli-a/nota',
                                      json={'note': 'x'}).status_code == 401


# ------------------------------------------------ un cliente no abre fichas: 403
def test_un_cliente_no_abre_la_ficha_de_nadie(como):
    assert como(CLI_A).get('/api/clientes/u-cli-b/ficha').status_code == 403
    # Ni siquiera la suya: la ficha es una herramienta de quien vende.
    assert como(CLI_A).get('/api/clientes/u-cli-a/ficha').status_code == 403


# ----------------------------------------------------------------- el admin ve todo
def test_admin_ve_la_ficha_completa(como):
    r = como(ADMIN).get('/api/clientes/u-cli-a/ficha')
    assert r.status_code == 200
    d = r.json()
    assert d['scope'] == 'admin'
    assert d['client']['name'] == 'Ana Cliente'
    assert d['client']['email'] == 'ana@x.mx'
    assert d['client']['points_balance'] == 120
    assert d['client']['phones'] and d['client']['addresses']
    # De quién es referida, con nombre y código: el admin sí lo ve.
    assert d['client']['referred_by']['name'] == 'María'
    # LOS DOS pedidos de Ana, el de María y el que hizo por su cuenta.
    assert {o['order_number'] for o in d['orders']} == {'EX-1', 'EX-2'}
    assert d['totals']['paid_total'] == 2000        # el fiado NO es ingreso
    assert d['totals']['por_cobrar'] == 500
    assert d['coupons'][0]['code'] == 'GIFT-ABC'
    assert d['points_ledger']
    assert 'note' in d


def test_admin_abre_la_ficha_de_una_invitada(como):
    r = como(ADMIN).get('/api/clientes/invitado:aidee@correo.com/ficha')
    assert r.status_code == 200
    d = r.json()
    assert d['client']['guest'] is True
    assert d['client']['name'] == 'Aidee Invitada'
    assert d['client']['phones'] == ['+52 55 9999 9999']
    assert [o['order_number'] for o in d['orders']] == ['EX-4']
    assert d['totals']['paid_total'] == 2830


def test_cliente_que_no_existe_es_404(como):
    assert como(ADMIN).get('/api/clientes/u-no-existe/ficha').status_code == 404
    assert como(ADMIN).get('/api/clientes/invitado:nadie@x.mx/ficha').status_code == 404


# ------------------------------------------- el distribuidor: sólo lo suyo
def test_distribuidor_abre_a_su_cliente_pero_solo_ve_lo_suyo(como):
    r = como(DIST_A).get('/api/clientes/u-cli-a/ficha')
    assert r.status_code == 200
    d = r.json()
    assert d['scope'] == 'distributor'
    assert d['client']['name'] == 'Ana Cliente'        # el nombre nunca fue secreto
    # ⛔ El pedido que Ana hizo por su cuenta NO le toca, aunque la persona sea suya.
    assert [o['order_number'] for o in d['orders']] == ['EX-1']
    assert d['totals']['my_earnings'] == 400
    # Nada de datos internos ni de la casa.
    assert 'coupons' not in d and 'points_ledger' not in d and 'note' not in d
    assert 'points_balance' not in d['client']
    assert 'referred_by' not in d['client']
    assert 'personal_discount_rate' not in d['client']


# ------------------------- el interruptor de contacto manda TAMBIÉN en la ficha
#
# ⛔ POR AQUÍ SE ESCAPABA EL CONTACTO. La ficha del pedido y el autollenado ya
# recortaban correo, teléfono y domicilio al distribuidor sin el interruptor. Esta
# ruta —que se abre desde ocho lugares— los mandaba enteros: cualquier distribuidor
# abriendo a su propio cliente se llevaba su contacto completo.

CAMPOS_DE_CONTACTO = ('email', 'phones', 'addresses')


def test_distribuidor_sin_interruptor_no_ve_el_contacto_de_su_cliente(como):
    d = como(DIST_A).get('/api/clientes/u-cli-a/ficha').json()
    assert 'email' not in d['client']
    assert d['client']['phones'] == [] and d['client']['addresses'] == []
    # Y qué compuestos compra tampoco: estaba en la misma lista de lo prohibido.
    assert d['top_products'] == []
    # Un grep tosco a propósito: si el dato viaja en OTRA llave, esto lo caza igual.
    crudo = json.dumps(d, ensure_ascii=False).lower()
    for prohibida in ('ana@x.mx', '55 1111 1111', 'calle 1'):
        assert prohibida not in crudo, f'se coló "{prohibida}" en la ficha del distribuidor'


def test_con_el_interruptor_encendido_maria_si_ve_el_contacto(como):
    maria = dict(DIST_A)
    maria[server.CAMPO_VE_CLIENTE] = True
    d = como(maria).get('/api/clientes/u-cli-a/ficha').json()
    assert d['client']['email'] == 'ana@x.mx'
    assert d['client']['phones'] == ['+52 55 1111 1111']
    assert d['client']['addresses']
    # Pero el candado de «sólo lo suyo» NO se afloja con el interruptor.
    assert [o['order_number'] for o in d['orders']] == ['EX-1']
    assert 'points_balance' not in d['client'] and 'note' not in d


def test_el_interruptor_no_le_abre_al_cliente_de_otro(como):
    """Encender el interruptor da contacto de LOS SUYOS, no una llave maestra."""
    otro = dict(DIST_B)
    otro[server.CAMPO_VE_CLIENTE] = True
    assert como(otro).get('/api/clientes/u-cli-a/ficha').status_code == 403
    assert como(otro).get('/api/clientes/invitado:aidee@correo.com/ficha').status_code == 403


def test_el_admin_ve_el_contacto_sin_ningun_interruptor(como):
    d = como(ADMIN).get('/api/clientes/u-cli-a/ficha').json()
    assert d['client']['email'] == 'ana@x.mx'
    assert d['client']['phones'] and d['client']['addresses']


# ------------------------------------------ lo que suele llevar y desde cuándo
def test_la_ficha_dice_que_suele_llevar_y_desde_cuando(como):
    d = como(ADMIN).get('/api/clientes/u-cli-a/ficha').json()
    # Primera y última compra: dos preguntas distintas, dos fechas.
    assert d['totals']['first_order_at'] == '2026-07-01T00:00:00'
    assert d['totals']['last_order_at'] == '2026-07-10T00:00:00'
    # Del que más piezas se lleva al que menos.
    assert [p['name'] for p in d['top_products']] == ['Retatrutida 10mg', 'BPC-157 5mg']
    assert d['top_products'][0]['units'] == 3
    assert d['top_products'][0]['orders'] == 2


def test_al_distribuidor_solo_le_cuenta_lo_que_le_compro_a_el(como):
    """El resumen respeta el mismo recorte que la lista de pedidos."""
    maria = dict(DIST_A)
    maria[server.CAMPO_VE_CLIENTE] = True
    d = como(maria).get('/api/clientes/u-cli-a/ficha').json()
    # EX-2 (el que Ana hizo por su cuenta) no entra ni en el resumen ni en las fechas.
    assert [p['name'] for p in d['top_products']] == ['Retatrutida 10mg']
    assert d['top_products'][0]['units'] == 2
    assert d['totals']['first_order_at'] == d['totals']['last_order_at'] == '2026-07-01T00:00:00'


def test_distribuidor_no_abre_al_cliente_de_otro(como):
    assert como(DIST_A).get('/api/clientes/u-cli-b/ficha').status_code == 403
    assert como(DIST_B).get('/api/clientes/u-cli-a/ficha').status_code == 403


def test_distribuidor_abre_a_su_invitada(como):
    r = como(DIST_A).get('/api/clientes/invitado:aidee@correo.com/ficha')
    assert r.status_code == 200
    d = r.json()
    assert d['client']['guest'] is True
    assert d['totals']['my_earnings'] == 780


def test_la_invitada_de_uno_no_es_la_de_otro(como):
    assert como(DIST_B).get('/api/clientes/invitado:aidee@correo.com/ficha').status_code == 403


def test_distribuidor_no_abre_a_otro_distribuidor_como_cliente(como):
    assert como(DIST_A).get('/api/clientes/u-dist-b/ficha').status_code == 403
    assert como(DIST_A).get('/api/clientes/u-admin/ficha').status_code == 403


# --------------------------------------------------------------- la nota privada
def test_solo_el_admin_escribe_la_nota(como):
    assert como(DIST_A).put('/api/admin/clientes/u-cli-a/nota',
                            json={'note': 'me debe'}).status_code == 403
    assert como(CLI_A).put('/api/admin/clientes/u-cli-a/nota',
                           json={'note': 'me debe'}).status_code == 403


def test_la_nota_se_guarda_y_vuelve_en_la_ficha(como):
    c = como(ADMIN)
    assert c.put('/api/admin/clientes/u-cli-a/nota',
                 json={'note': 'Paga por SPEI los viernes'}).status_code == 200
    d = c.get('/api/clientes/u-cli-a/ficha').json()
    assert d['note'] == 'Paga por SPEI los viernes'
    # Y el distribuidor no la ve ni por casualidad.
    assert 'note' not in como(DIST_A).get('/api/clientes/u-cli-a/ficha').json()


def test_la_nota_de_una_invitada_tambien_se_guarda(como):
    c = como(ADMIN)
    assert c.put('/api/admin/clientes/invitado:aidee@correo.com/nota',
                 json={'note': 'Contactar por WhatsApp'}).status_code == 200
    d = c.get('/api/clientes/invitado:aidee@correo.com/ficha').json()
    assert d['note'] == 'Contactar por WhatsApp'


# ---------------------------------- el invitado que YA abrió cuenta es UNA persona
def test_invitado_con_cuenta_cae_en_su_ficha_de_usuario(como):
    r = como(ADMIN).get('/api/clientes/invitado:ana@x.mx/ficha')
    assert r.status_code == 200
    d = r.json()
    assert d['client']['guest'] is False
    assert d['client']['id'] == 'u-cli-a'


def test_el_correo_alterno_lleva_a_la_MISMA_ficha(como):
    """⛔ UNA PERSONA, UNA FICHA. Se buscaba sólo por `email`, así que quien compró
    como invitado con la dirección que después quedó de alterna abría una ficha
    aparte. La puerta de entrada mira las dos desde la fusión de cuentas; aquí igual."""
    d = como(ADMIN).get('/api/clientes/invitado:ana.vieja@x.mx/ficha').json()
    assert d['client']['guest'] is False
    assert d['client']['id'] == 'u-cli-a'


def test_las_mayusculas_no_hacen_otra_persona(como):
    """«Aidee@Correo.com» y «aidee@correo.com» son la misma."""
    d = como(ADMIN).get('/api/clientes/invitado:Aidee@Correo.com/ficha').json()
    assert d['client']['id'] == 'invitado:aidee@correo.com'
    assert [o['order_number'] for o in d['orders']] == ['EX-4']
