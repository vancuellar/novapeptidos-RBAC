"""EL PAGO DE LAS COMISIONES — solicitar, deber y pagar (Christián, 2026-08-01).

«Hoy no hay dónde ver qué se le debe a cada quien ni qué ya se pagó.» Estas
pruebas fijan la bolsa completa:

  · la aritmética de `comisiones.py`, sin red;
  · el distribuidor solicita — y NO puede pedir de más ni pedir dos veces;
  · el admin registra el pago — y NO puede pagar de más (la bolsa no es una
    fuente de dinero que nadie autorizó);
  · la solicitud pagada se convierte en el RECIBO (un documento, no dos);
  · el rechazo no mueve un peso y deja volver a pedir;
  · las puertas: cliente 403, sin sesión 401, y el «ver como» no escribe.
"""
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import comisiones
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
                return type('R', (), {'matched_count': 1})()
        return type('R', (), {'matched_count': 0})()


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

# Una venta COBRADA con $1,000 de comisión y una FIADA con $500 (que no cuenta:
# sin cobrar no hay comisión que pagar — regla del 2026-07-29).
O_PAGADA = {'id': 'o1', 'order_number': 'EX-1', 'referred_by': 'u-d', 'status': 'entregado',
            'paid': True, 'total': 5000, 'created_at': '2026-07-20T00:00:00',
            'commissions': [{'distributor_id': 'u-d', 'amount': 1000, 'role': 'seller'}]}
O_FIADA = {'id': 'o2', 'order_number': 'EX-2', 'referred_by': 'u-d', 'status': 'entregado',
           'paid': False, 'total': 2500, 'created_at': '2026-07-25T00:00:00',
           'commissions': [{'distributor_id': 'u-d', 'amount': 500, 'role': 'seller'}]}


@pytest.fixture
def mundo(monkeypatch):
    bd = _FakeDB(users=[ADMIN, DIST, CLIENTE], orders=[O_PAGADA, O_FIADA])
    monkeypatch.setattr(server, 'db', bd)

    def _como(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield bd, _como
    server.app.dependency_overrides.clear()


def _pagos(bd):
    return bd[server.COLECCION_PAGOS_COMISION].docs


# ------------------------------------------------------------- la aritmética
def test_pagado_suma_solo_los_recibos():
    pagos = [{'status': 'pagado', 'amount': 300}, {'status': 'solicitado', 'amount': 700},
             {'status': 'rechazado', 'amount': 900}]
    assert comisiones.pagado_de(pagos) == 300


def test_por_pagar_es_ganado_menos_pagado_y_nunca_negativo():
    assert comisiones.por_pagar(1000, [{'status': 'pagado', 'amount': 400}]) == 600
    assert comisiones.por_pagar(300, [{'status': 'pagado', 'amount': 400}]) == 0


def test_no_se_solicita_de_mas_ni_dos_veces():
    ok, _ = comisiones.puede_solicitar(600, 1000, [{'status': 'pagado', 'amount': 400}])
    assert ok
    ok, motivo = comisiones.puede_solicitar(601, 1000, [{'status': 'pagado', 'amount': 400}])
    assert not ok and '600' in motivo
    ok, motivo = comisiones.puede_solicitar(100, 1000, [{'status': 'solicitado', 'amount': 200}])
    assert not ok and 'en camino' in motivo


def test_no_se_paga_de_mas():
    ok, _ = comisiones.puede_pagar(1000, 1000, [])
    assert ok
    ok, motivo = comisiones.puede_pagar(1001, 1000, [])
    assert not ok and 'sin respaldo' in motivo


# ------------------------------------------------- el panel del distribuidor
def test_el_distribuidor_ve_su_bolsa(mundo):
    bd, como = mundo
    r = como(DIST).get('/api/distributor/comisiones')
    assert r.status_code == 200
    cuerpo = r.json()
    # $1,000 de la cobrada; los $500 de la fiada NO son ganancia todavía.
    assert cuerpo['ganado'] == 1000
    assert cuerpo['pagado'] == 0
    assert cuerpo['por_pagar'] == 1000
    assert cuerpo['solicitud_pendiente'] is None


def test_solicitar_sin_monto_pide_todo_el_saldo(mundo):
    bd, como = mundo
    r = como(DIST).post('/api/distributor/comisiones/solicitar', json={})
    assert r.status_code == 200
    assert r.json()['amount'] == 1000
    assert _pagos(bd)[0]['status'] == 'solicitado'
    # Y el admin recibió su campanita.
    assert any(n.get('type') == 'comision_solicitada' for n in bd.notifications.docs)


def test_no_se_puede_solicitar_de_mas_por_la_ruta(mundo):
    bd, como = mundo
    r = como(DIST).post('/api/distributor/comisiones/solicitar', json={'amount': 1500})
    assert r.status_code == 400
    assert _pagos(bd) == []


def test_no_hay_dos_solicitudes_en_camino(mundo):
    bd, como = mundo
    cli = como(DIST)
    assert cli.post('/api/distributor/comisiones/solicitar', json={'amount': 200}).status_code == 200
    r = cli.post('/api/distributor/comisiones/solicitar', json={'amount': 200})
    assert r.status_code == 400
    assert len(_pagos(bd)) == 1


# ------------------------------------------------------- el panel del admin
def test_el_admin_ve_la_deuda_de_toda_la_casa(mundo):
    bd, como = mundo
    r = como(ADMIN).get('/api/admin/comisiones')
    assert r.status_code == 200
    cuerpo = r.json()
    fila = next(x for x in cuerpo['distribuidores'] if x['id'] == 'u-d')
    assert fila['por_pagar'] == 1000
    assert cuerpo['por_pagar_total'] == 1000


def test_pagar_una_solicitud_la_convierte_en_recibo(mundo):
    bd, como = mundo
    como(DIST).post('/api/distributor/comisiones/solicitar', json={})
    r = como(ADMIN).post('/api/admin/comisiones/pagar', json={
        'distributor_id': 'u-d', 'amount': 1000, 'reference': 'SPEI 777'})
    assert r.status_code == 200
    assert len(_pagos(bd)) == 1              # un documento, no dos
    recibo = _pagos(bd)[0]
    assert recibo['status'] == 'pagado'
    assert recibo['reference'] == 'SPEI 777'
    assert recibo['paid_by'] == 'u-admin'
    # El saldo quedó en cero y el distribuidor recibió su aviso.
    assert como(DIST).get('/api/distributor/comisiones').json()['por_pagar'] == 0
    assert any(n.get('type') == 'comision_pagada' for n in bd.notifications.docs)


def test_pagar_sin_solicitud_tambien_deja_recibo(mundo):
    bd, como = mundo
    r = como(ADMIN).post('/api/admin/comisiones/pagar', json={
        'distributor_id': 'u-d', 'amount': 400, 'reference': 'efectivo'})
    assert r.status_code == 200
    assert _pagos(bd)[0]['status'] == 'pagado'
    assert como(DIST).get('/api/distributor/comisiones').json()['por_pagar'] == 600


def test_el_admin_no_puede_pagar_de_mas(mundo):
    bd, como = mundo
    r = como(ADMIN).post('/api/admin/comisiones/pagar', json={
        'distributor_id': 'u-d', 'amount': 1200})
    assert r.status_code == 400
    assert _pagos(bd) == []


def test_rechazar_no_mueve_saldo_y_deja_volver_a_pedir(mundo):
    bd, como = mundo
    cli = como(DIST)
    solicitud = cli.post('/api/distributor/comisiones/solicitar', json={}).json()
    r = como(ADMIN).post('/api/admin/comisiones/rechazar', json={
        'payout_id': solicitud['id'], 'motivo': 'Falta tu CLABE'})
    assert r.status_code == 200
    assert _pagos(bd)[0]['status'] == 'rechazado'
    # El override de sesión es global: hay que volver a «entrar» como la distribuidora.
    cli = como(DIST)
    cuerpo = cli.get('/api/distributor/comisiones').json()
    assert cuerpo['por_pagar'] == 1000       # ni un peso se movió
    # Y puede volver a solicitar.
    assert cli.post('/api/distributor/comisiones/solicitar', json={}).status_code == 200


# ------------------------------------------------------------------ las puertas
def test_un_cliente_no_entra(mundo):
    bd, como = mundo
    assert como(CLIENTE).get('/api/distributor/comisiones').status_code == 403
    assert como(CLIENTE).get('/api/admin/comisiones').status_code == 403
    assert como(DIST).get('/api/admin/comisiones').status_code == 403


def test_sin_sesion_no_hay_nada(mundo):
    bd, _ = mundo
    c = TestClient(server.app)
    assert c.get('/api/distributor/comisiones').status_code == 401
    assert c.post('/api/admin/comisiones/pagar',
                  json={'distributor_id': 'u-d', 'amount': 1}).status_code == 401
