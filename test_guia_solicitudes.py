"""LA SOLICITUD DE GUÍA — el distribuidor pide, Christián aprueba (2026-08-03).

«Un botón "solicitar guía" junto al cliente al que le falte número de guía,
siempre y cuando ya haya pagado.» Estas pruebas fijan la bolsa completa:

  · los candados de `guia_solicitudes.py`, sin red: sin pagar no se pide, con
    guía no se pide, y una solicitud a la vez por pedido;
  · el distribuidor solicita — y SOLO de sus pedidos (referred_by o 403);
  · aprobar ES comprar: pasa por `comprar_guia_del_pedido` (el MISMO camino del
    pago automático) y si un freno detiene la compra la solicitud SIGUE
    pendiente — aprobar sin comprar sería mentirle al distribuidor;
  · si el pedido ya tenía guía al aprobar, no se compra dos veces;
  · el rechazo no compra nada y deja volver a pedir;
  · las puertas: cliente 403, y el «ver como» del admin no escribe (espiar un
    panel jamás puede convertirse en gastar dinero en nombre de otro).
"""
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import guia_solicitudes
import server


# --------------------------------------------------------- base de datos falsa
def _coincide(doc, filtro):
    for k, v in (filtro or {}).items():
        if k == '$or':
            if not any(_coincide(doc, sub) for sub in v):
                return False
        elif '.' in k:
            campo, sub = k.split('.', 1)
            filas = doc.get(campo) or []
            if not any(isinstance(r, dict) and r.get(sub) == v for r in filas):
                return False
        elif isinstance(v, dict):
            if '$ne' in v and doc.get(k) == v['$ne']:
                return False
            if '$in' in v and doc.get(k) not in v['$in']:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    async def to_list(self, n=None):
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

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, filtro, cambio, upsert=False):
        for d in self.docs:
            if _coincide(d, filtro):
                d.update((cambio or {}).get('$set') or {})
                return type('R', (), {'matched_count': 1, 'modified_count': 1})()
        return type('R', (), {'matched_count': 0, 'modified_count': 0})()


class _FakeDB:
    def __init__(self, **colecciones):
        self._c = {k: _Coll(v) for k, v in colecciones.items()}

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return self.__getattr__(name)


# ------------------------------------------------------------------- el elenco
ADMIN = {'id': 'u-admin', 'name': 'Christián', 'email': 'admin@exygenlabs.com', 'role': 'admin'}
DIST = {'id': 'u-d', 'name': 'Mónica', 'email': 'monica@x.mx', 'role': 'distributor',
        'distributor_code': 'MONICAF', 'tier': 'junior0'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}

O_PAGADA = {'id': 'o1', 'order_number': 'EX-1', 'referred_by': 'u-d',
            'status': 'confirmado', 'paid': True, 'total': 5000,
            'created_at': '2026-08-01T00:00:00', 'items': [], 'customer': {}}
O_FIADA = {'id': 'o2', 'order_number': 'EX-2', 'referred_by': 'u-d',
           'status': 'entregado', 'paid': False, 'total': 2500,
           'created_at': '2026-08-02T00:00:00', 'items': [], 'customer': {}}
O_CON_GUIA = {'id': 'o3', 'order_number': 'EX-3', 'referred_by': 'u-d',
              'status': 'enviado', 'paid': True, 'total': 1200,
              'tracking_number': 'YA-123',
              'created_at': '2026-08-02T01:00:00', 'items': [], 'customer': {}}
O_AJENA = {'id': 'o4', 'order_number': 'EX-4', 'referred_by': 'u-otra',
           'status': 'confirmado', 'paid': True, 'total': 900,
           'created_at': '2026-08-02T02:00:00', 'items': [], 'customer': {}}


@pytest.fixture
def mundo(monkeypatch):
    # Copias, no referencias: varias pruebas ESCRIBEN sobre los pedidos (le
    # ponen guía, le quitan el pago) y sin esto se contaminan entre sí.
    bd = _FakeDB(users=[dict(ADMIN), dict(DIST), dict(CLIENTE)],
                 orders=[dict(O_PAGADA), dict(O_FIADA), dict(O_CON_GUIA), dict(O_AJENA)])
    monkeypatch.setattr(server, 'db', bd)

    def _como(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield bd, _como
    server.app.dependency_overrides.clear()


def _solicitudes(bd):
    return bd[server.COLECCION_SOLICITUDES_GUIA].docs


def _solicitar(como, numero='EX-1'):
    return como(DIST).post(f'/api/distributor/orders/{numero}/solicitar-guia')


# ------------------------------------------------------------- los candados
def test_los_candados_puros():
    ok, _ = guia_solicitudes.puede_solicitar(O_PAGADA, [])
    assert ok
    ok, motivo = guia_solicitudes.puede_solicitar(O_FIADA, [])
    assert not ok and 'pagado' in motivo
    ok, motivo = guia_solicitudes.puede_solicitar(O_CON_GUIA, [])
    assert not ok and 'ya tiene guía' in motivo
    ok, motivo = guia_solicitudes.puede_solicitar(O_PAGADA, [{'status': 'solicitada'}])
    assert not ok and 'en camino' in motivo
    # Una rechazada NO estorba: se puede volver a pedir.
    ok, _ = guia_solicitudes.puede_solicitar(O_PAGADA, [{'status': 'rechazada'}])
    assert ok
    ok, motivo = guia_solicitudes.puede_solicitar(dict(O_PAGADA, status='cancelado'), [])
    assert not ok


# ------------------------------------------------- el panel del distribuidor
def test_solicitar_deja_la_solicitud_y_la_campanita(mundo):
    bd, como = mundo
    r = _solicitar(como)
    assert r.status_code == 200
    assert _solicitudes(bd)[0]['status'] == 'solicitada'
    assert _solicitudes(bd)[0]['order_id'] == 'o1'
    assert any(n.get('type') == 'guia_solicitada' for n in bd.notifications.docs)


def test_no_hay_dos_solicitudes_del_mismo_pedido(mundo):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    assert _solicitar(como).status_code == 400
    assert len(_solicitudes(bd)) == 1


def test_sin_pagar_no_se_solicita(mundo):
    bd, como = mundo
    r = _solicitar(como, 'EX-2')
    assert r.status_code == 400 and 'pagado' in r.json()['detail']
    assert _solicitudes(bd) == []


def test_con_guia_no_se_solicita(mundo):
    bd, como = mundo
    assert _solicitar(como, 'EX-3').status_code == 400


def test_el_pedido_de_otro_es_403(mundo):
    bd, como = mundo
    assert _solicitar(como, 'EX-4').status_code == 403
    assert _solicitudes(bd) == []


def test_el_cliente_no_entra(mundo):
    bd, como = mundo
    r = como(CLIENTE).post('/api/distributor/orders/EX-1/solicitar-guia')
    assert r.status_code == 403


def test_ver_como_no_escribe(mundo):
    bd, como = mundo
    r = como(dict(DIST, view_as=True)).post('/api/distributor/orders/EX-1/solicitar-guia')
    assert r.status_code == 403
    assert _solicitudes(bd) == []


def test_la_lista_del_distribuidor_trae_lo_que_el_boton_necesita(mundo):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    filas = {f['order_number']: f for f in como(DIST).get('/api/distributor/orders').json()}
    assert filas['EX-1']['paid'] is True and filas['EX-1']['guia_solicitada'] is True
    assert filas['EX-2']['paid'] is False and filas['EX-2']['guia_solicitada'] is False
    assert filas['EX-3']['tracking_number'] == 'YA-123'


# ------------------------------------------------------- el panel del admin
def test_el_admin_ve_las_solicitudes_con_el_estado_del_pedido(mundo):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    r = como(ADMIN).get('/api/admin/guia-solicitudes')
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo['pendientes'] == 1
    fila = cuerpo['solicitudes'][0]
    assert fila['order_number'] == 'EX-1' and fila['order_paid'] is True


def test_aprobar_compra_por_el_camino_de_siempre_y_asigna(mundo, monkeypatch):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']

    async def _compra(order, avisar=True):
        # La compra REAL escribe el número en el pedido y avisa al cliente
        # (`avisar=True` es lo que dispara su correo); aquí se simula el efecto.
        assert avisar is True
        await bd.orders.update_one({'id': order['id']},
                                   {'$set': {'tracking_number': 'TRK-9', 'status': 'enviado'}})
        return {'tracking_number': 'TRK-9'}

    monkeypatch.setattr(server, 'comprar_guia_del_pedido', _compra)
    r = como(ADMIN).post('/api/admin/guia-solicitudes/aprobar', json={'solicitud_id': sid})
    assert r.status_code == 200 and r.json()['tracking_number'] == 'TRK-9'
    assert _solicitudes(bd)[0]['status'] == 'aprobada'
    pedido = [o for o in bd.orders.docs if o['id'] == 'o1'][0]
    assert pedido['tracking_number'] == 'TRK-9'
    assert any(n.get('type') == 'guia_aprobada' for n in bd.notifications.docs)


def test_si_un_freno_detiene_la_compra_la_solicitud_sigue_pendiente(mundo, monkeypatch):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']

    async def _freno(order, avisar=True):
        await bd.orders.update_one({'id': order['id']},
                                   {'$set': {'label_hold': 'sobre_tope'}})
        return None

    monkeypatch.setattr(server, 'comprar_guia_del_pedido', _freno)
    r = como(ADMIN).post('/api/admin/guia-solicitudes/aprobar', json={'solicitud_id': sid})
    assert r.status_code == 502 and 'sigue pendiente' in r.json()['detail']
    assert _solicitudes(bd)[0]['status'] == 'solicitada'


def test_aprobar_un_pedido_que_ya_tiene_guia_no_compra_dos_veces(mundo, monkeypatch):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']
    await_no_compra = {'llamada': False}

    async def _no_deberia(order, avisar=True):
        await_no_compra['llamada'] = True
        return None

    monkeypatch.setattr(server, 'comprar_guia_del_pedido', _no_deberia)
    # Entre solicitar y aprobar, otro camino (el webhook del pago) compró la guía.
    [o for o in bd.orders.docs if o['id'] == 'o1'][0]['tracking_number'] = 'OTRA-1'
    r = como(ADMIN).post('/api/admin/guia-solicitudes/aprobar', json={'solicitud_id': sid})
    assert r.status_code == 200 and r.json()['ya_tenia_guia'] is True
    assert await_no_compra['llamada'] is False
    assert _solicitudes(bd)[0]['status'] == 'aprobada'


def test_aprobar_un_pedido_que_dejo_de_estar_pagado_es_409(mundo, monkeypatch):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']
    [o for o in bd.orders.docs if o['id'] == 'o1'][0]['paid'] = False
    r = como(ADMIN).post('/api/admin/guia-solicitudes/aprobar', json={'solicitud_id': sid})
    assert r.status_code == 409
    assert _solicitudes(bd)[0]['status'] == 'solicitada'


def test_rechazar_no_compra_y_deja_volver_a_pedir(mundo):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']
    r = como(ADMIN).post('/api/admin/guia-solicitudes/rechazar',
                         json={'solicitud_id': sid, 'motivo': 'Falta confirmar el CP'})
    assert r.status_code == 200
    assert _solicitudes(bd)[0]['status'] == 'rechazada'
    aviso = next(n for n in bd.notifications.docs if n.get('type') == 'guia_rechazada')
    assert 'Falta confirmar el CP' in aviso.get('body', '')
    # Y el pedido sigue sin guía: rechazar jamás gasta.
    assert not [o for o in bd.orders.docs if o['id'] == 'o1'][0].get('tracking_number')
    # Puede volver a solicitar.
    assert _solicitar(como).status_code == 200


def test_el_ver_como_del_admin_no_aprueba_ni_rechaza(mundo):
    bd, como = mundo
    assert _solicitar(como).status_code == 200
    sid = _solicitudes(bd)[0]['id']
    espia = dict(ADMIN, view_as=True)
    assert como(espia).post('/api/admin/guia-solicitudes/aprobar',
                            json={'solicitud_id': sid}).status_code == 403
    assert como(espia).post('/api/admin/guia-solicitudes/rechazar',
                            json={'solicitud_id': sid}).status_code == 403
    assert _solicitudes(bd)[0]['status'] == 'solicitada'
