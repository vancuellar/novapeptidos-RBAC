"""TODO EL QUE COMPRA ES CLIENTE, TENGA CUENTA O NO (Christián, 2026-07-31).

El checkout deja comprar como invitado. La lista de clientes del Panel se armaba
únicamente con `users.role == 'user'`, así que quien compraba sin abrir cuenta NO
EXISTÍA para la casa: ni ficha, ni historial, ni a quién volver a venderle.

Le pasó de verdad el 2026-07-30 con Brenda ($4,827) y con Aidee ($2,830): las dos
compraron con el código de María, las dos pagaron, a las dos se les puso guía, la
comisión se pagó — y ninguna de las dos aparecía en Clientes.

Lo que se prueba aquí:
  · el invitado SALE en la lista, con su distintivo y su historial completo;
  · el correo es la llave, sin distinguir mayúsculas ni espacios;
  · el que ya tiene cuenta no sale DOS veces: se marca como posible duplicado y la
    decisión de fusionar es de Christián, no del programa;
  · el reporte de duplicados encuentra los tres casos y no inventa ninguno;
  · nada de esto se lo abre a nadie que no sea admin.
"""
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import server
from test_ficha_cliente import _FakeDB


ADMIN = {'id': 'u-admin', 'name': 'Christián', 'email': 'admin@exygenlabs.com', 'role': 'admin'}
MARIA = {'id': 'u-maria', 'name': 'María', 'email': 'maria@x.mx', 'role': 'distributor',
         'distributor_code': 'MONICAF-7451'}
# Una clienta CON cuenta, para que la lista siga trayendo a los de siempre.
PAZ = {'id': 'u-paz', 'name': 'Paz Cambray', 'email': 'pazcambray22@gmail.com', 'role': 'user',
       'created_at': '2026-07-20T00:00:00', 'phone': '5511110000'}
# Una cuenta SIN confirmar cuyo correo coincide con el de una compra de invitado:
# la adopción automática no la toca (y con razón), así que es un DUPLICADO a mano.
JAZ = {'id': 'u-jaz', 'name': 'Jazmín Padilla', 'email': 'jazzpad91@gmail.com', 'role': 'user',
       'email_verified': False, 'created_at': '2026-07-25T00:00:00'}
# Dos cuentas de la misma persona, ligadas por el correo alterno.
CASA_1 = {'id': 'u-casa-1', 'name': 'Christián (alterno)', 'email': 'otro@exygenlabs.com',
          'role': 'user', 'alt_emails': 'admin@exygenlabs.com', 'created_at': '2026-06-01T00:00:00'}

BRENDA = {'full_name': 'Brenda Iliana Oseguera Gonzalez', 'email': 'BreniOG73@Yahoo.com.mx  ',
          'phone': '4425217088', 'address': 'Prolongacion el Roble 73', 'city': 'El Marqués',
          'state': 'Querétaro', 'postal_code': '76807'}
AIDEE = {'full_name': 'aidee liliana garcia hernandez', 'email': 'lilygarciahdz@hotmail.com',
         'phone': '3312345678', 'address': 'Calle 9', 'city': 'Guadalajara',
         'state': 'Jalisco', 'postal_code': '44100'}

# Brenda: DOS compras como invitada, con datos corregidos la segunda vez.
P1 = {'id': 'p1', 'order_number': 'EX-20260730-5930', 'referred_by': 'u-maria',
      'status': 'enviado', 'paid': True, 'total': 4827, 'created_at': '2026-07-30T10:00:00',
      'payment_method': 'spei', 'customer': BRENDA,
      'items': [{'name': 'Retatrutida 10mg', 'quantity': 2}],
      'commissions': [{'distributor_id': 'u-maria', 'amount': 1200, 'role': 'seller'}]}
P2 = {'id': 'p2', 'order_number': 'EX-20260731-1111', 'referred_by': 'u-maria',
      'status': 'pendiente', 'paid': False, 'total': 1000, 'created_at': '2026-07-31T10:00:00',
      'payment_method': 'spei',
      'customer': {**BRENDA, 'email': 'brenIog73@yahoo.com.MX', 'phone': '4420000000'},
      'items': [{'name': 'Retatrutida 10mg', 'quantity': 1}]}
# Aidee: una sola compra, también como invitada.
P3 = {'id': 'p3', 'order_number': 'EX-20260730-2906', 'referred_by': 'u-maria',
      'status': 'enviado', 'paid': True, 'total': 2830, 'created_at': '2026-07-30T12:00:00',
      'payment_method': 'spei', 'customer': AIDEE,
      'items': [{'name': 'Semaglutida 5mg', 'quantity': 3}],
      'commissions': [{'distributor_id': 'u-maria', 'amount': 780, 'role': 'seller'}]}
# Paz sí tiene cuenta: su pedido trae `user_id` y NO debe salir como invitada.
P4 = {'id': 'p4', 'order_number': 'EX-20260723-9064', 'user_id': 'u-paz', 'status': 'entregado',
      'paid': True, 'total': 3347, 'created_at': '2026-07-23T00:00:00', 'payment_method': 'spei',
      'customer': {'full_name': 'Paz Cambray', 'email': 'pazcambray22@gmail.com'},
      'items': [{'name': 'BPC-157 5mg', 'quantity': 1}]}
# Jazmín compró como invitada CON el correo de su cuenta sin confirmar.
P5 = {'id': 'p5', 'order_number': 'EX-20260728-7777', 'status': 'entregado', 'paid': True,
      'total': 900, 'created_at': '2026-07-28T00:00:00', 'payment_method': 'tarjeta',
      'customer': {'full_name': 'Jazmin P', 'email': 'jazzpad91@gmail.com'},
      'items': [{'name': 'NAD+ 500mg', 'quantity': 1}]}
# Un pedido de mostrador SIN correo: no hay llave, no hay ficha. No revienta nada.
P6 = {'id': 'p6', 'order_number': 'EX-20260726-0001', 'status': 'entregado', 'paid': True,
      'total': 500, 'created_at': '2026-07-26T00:00:00', 'payment_method': 'efectivo',
      'customer': {'full_name': 'Mostrador'}, 'items': [{'name': 'BPC-157 5mg', 'quantity': 1}]}


@pytest.fixture
def como(monkeypatch):
    monkeypatch.setattr(server, 'db', _FakeDB(
        users=[ADMIN, MARIA, PAZ, JAZ, CASA_1],
        orders=[P1, P2, P3, P4, P5, P6],
        discount_codes=[], points=[], client_notes=[],
    ))

    def _factory(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


ID_BRENDA = 'invitado:breniog73@yahoo.com.mx'
ID_AIDEE = 'invitado:lilygarciahdz@hotmail.com'


def _por_id(lista):
    return {c['id']: c for c in lista}


# --------------------------------------------- el invitado SALE en la lista
def test_brenda_y_aidee_ya_son_clientes(como):
    gente = _por_id(como(ADMIN).get('/api/admin/customers').json())
    assert ID_BRENDA in gente
    assert ID_AIDEE in gente
    # Y los de siempre siguen ahí.
    assert 'u-paz' in gente and gente['u-paz']['guest'] is False


def test_el_invitado_trae_su_distintivo(como):
    gente = _por_id(como(ADMIN).get('/api/admin/customers').json())
    assert gente[ID_AIDEE]['guest'] is True
    assert gente['u-paz']['guest'] is False


def test_el_historial_del_invitado_esta_completo(como):
    b = _por_id(como(ADMIN).get('/api/admin/customers').json())[ID_BRENDA]
    # Sus DOS pedidos, lo que pagó y lo que debe, separados.
    assert b['orders_count'] == 2
    assert b['total_spent'] == 4827
    assert b['por_cobrar'] == 1000
    # «Cliente desde» es su PRIMERA compra: no hay fecha de registro porque no hay registro.
    assert b['created_at'] == '2026-07-30T10:00:00'
    assert b['last_order_at'] == '2026-07-31T10:00:00'
    # Contacto: el teléfono NUEVO primero, porque la gente corrige sus datos al recomprar.
    assert b['phones'][0] == '4420000000'
    assert any('76807' in d for d in b['addresses'])
    assert b['name'] == 'Brenda Iliana Oseguera Gonzalez'


def test_el_correo_es_la_llave_sin_mayusculas_ni_espacios(como):
    """«BreniOG73@Yahoo.com.mx  » y «brenIog73@yahoo.com.MX» son UNA persona."""
    gente = como(ADMIN).get('/api/admin/customers').json()
    brendas = [c for c in gente if 'yahoo' in (c.get('email') or '')]
    assert len(brendas) == 1
    assert brendas[0]['email'] == 'breniog73@yahoo.com.mx'
    assert brendas[0]['orders_count'] == 2


def test_el_que_ya_tiene_cuenta_no_sale_dos_veces_como_invitado(como):
    """Paz compró CON su cuenta: su pedido no puede volver a salir como invitado."""
    gente = como(ADMIN).get('/api/admin/customers').json()
    assert not [c for c in gente if c['id'] == 'invitado:pazcambray22@gmail.com']


def test_un_pedido_sin_correo_no_inventa_un_cliente(como):
    """Sin correo no hay llave. Ni ficha fantasma ni error."""
    gente = como(ADMIN).get('/api/admin/customers').json()
    assert not [c for c in gente if (c.get('name') or '') == 'Mostrador']


def test_el_invitado_que_ya_tiene_cuenta_se_marca_pero_NO_se_fusiona(como):
    """⛔ NO SE FUSIONA A CIEGAS. Jazmín tiene cuenta sin confirmar y compró como
    invitada con ese mismo correo: se señala, y Christián decide."""
    gente = _por_id(como(ADMIN).get('/api/admin/customers').json())
    inv = gente['invitado:jazzpad91@gmail.com']
    assert inv['posible_duplicado_de']['id'] == 'u-jaz'
    # La cuenta sigue existiendo aparte, con su historial intacto (vacío).
    assert gente['u-jaz']['orders_count'] == 0


# ----------------------------------------------------- el reporte de duplicados
def test_el_reporte_encuentra_al_invitado_con_cuenta(como):
    r = como(ADMIN).get('/api/admin/clientes/duplicados').json()
    inv = [d for d in r['duplicados'] if d['tipo'] == 'invitado_con_cuenta']
    assert [d['llave'] for d in inv] == ['jazzpad91@gmail.com']
    assert inv[0]['cuenta']['id'] == 'u-jaz'
    assert inv[0]['invitado']['total_spent'] == 900
    # Y dice POR QUÉ sigue suelto: si confirma su correo, se adopta solo.
    assert inv[0]['motivo'] == 'correo_sin_confirmar'


def test_el_reporte_encuentra_las_dos_cuentas_del_mismo_correo(como):
    """El alterno de una es el principal de la otra: es la misma persona."""
    r = como(ADMIN).get('/api/admin/clientes/duplicados').json()
    rep = [d for d in r['duplicados'] if d['tipo'] == 'correo_repetido']
    assert [d['llave'] for d in rep] == ['admin@exygenlabs.com']
    assert {c['id'] for c in rep[0]['cuentas']} == {'u-admin', 'u-casa-1'}


def test_el_reporte_no_inventa_duplicados(como):
    """Paz y María no se parecen a nadie: no pueden aparecer."""
    r = como(ADMIN).get('/api/admin/clientes/duplicados').json()
    crudo = str(r['duplicados'])
    assert 'u-paz' not in crudo and 'u-maria' not in crudo


def test_el_telefono_repetido_tambien_se_reporta(monkeypatch):
    gemela = {'id': 'u-gemela', 'name': 'Otra Paz', 'email': 'otra@x.mx', 'role': 'user',
              'phone': '+52 55 1111 0000'}
    monkeypatch.setattr(server, 'db', _FakeDB(users=[ADMIN, PAZ, gemela], orders=[]))
    server.app.dependency_overrides[auth.get_current_user] = lambda: dict(ADMIN)
    try:
        r = TestClient(server.app).get('/api/admin/clientes/duplicados').json()
        tel = [d for d in r['duplicados'] if d['tipo'] == 'telefono_repetido']
        assert [d['llave'] for d in tel] == ['5511110000']
        assert {c['id'] for c in tel[0]['cuentas']} == {'u-paz', 'u-gemela'}
    finally:
        server.app.dependency_overrides.clear()


# ------------------------------------------------------------------ el candado
def test_la_lista_y_el_reporte_son_solo_del_admin(como):
    for ruta in ('/api/admin/customers', '/api/admin/clientes/duplicados'):
        assert como(MARIA).get(ruta).status_code == 403
        assert como(PAZ).get(ruta).status_code == 403


def test_sin_sesion_no_hay_nada(monkeypatch):
    monkeypatch.setattr(server, 'db', _FakeDB(users=[ADMIN], orders=[]))
    c = TestClient(server.app)
    assert c.get('/api/admin/customers').status_code == 401
    assert c.get('/api/admin/clientes/duplicados').status_code == 401
