"""El rol 'marketing' (María, la que lleva los anuncios) SOLO entra a difusión.

Lo que se protege aquí es la puerta, no el contenido: que el backend deje pasar
al rol 'marketing' únicamente al embudo, a marketing y a Meta, y que le cierre
con 403 todo lo demás (pedidos, clientes, stock, cobros, motor de precios y
distribuidores) AUNQUE le pegue a la API directo, sin pasar por el frontend.
Esconder pestañas en React no es seguridad; esto sí.

No toca Mongo: la identidad se inyecta con dependency_overrides y la base se
sustituye por una falsa que devuelve vacío. Para los 403 ni siquiera hace falta:
la puerta truena antes de llegar a la base.
"""
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')
# Sin token de Meta las rutas de anuncios usan el último CSV (aquí: vacío) en
# vez de salir a la red. En pruebas no debe haber red jamás.
os.environ.pop('META_TOKEN', None)
os.environ.pop('META_AD_ACCOUNT', None)

import pytest
from fastapi.testclient import TestClient

import auth
import server


# ------------------------------------------------ base de datos falsa (vacía)
class _Cursor:
    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return []


class _Coll:
    def find(self, *a, **k):
        return _Cursor()

    def aggregate(self, *a, **k):
        return _Cursor()

    async def find_one(self, *a, **k):
        return None

    async def count_documents(self, *a, **k):
        return 0


class _FakeDB:
    def __getattr__(self, name):
        return _Coll()


MARIA = {'id': 'u-maria', 'name': 'María', 'email': 'marianeunfeld0@gmail.com',
         'role': 'marketing'}
ADMIN = {'id': 'u-admin', 'name': 'Christian', 'email': 'admin@exygenlabs.com',
         'role': 'admin'}
DIST = {'id': 'u-dist', 'name': 'Dist', 'email': 'dist@x.mx', 'role': 'distributor'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}


@pytest.fixture
def como(monkeypatch):
    """`como(usuario)` devuelve un cliente HTTP ya autenticado como ese usuario."""
    monkeypatch.setattr(server, 'db', _FakeDB())

    def _factory(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


# --------------------------------------------- lo que SÍ puede ver marketing
def test_marketing_entra_al_embudo(como):
    r = como(MARIA).get('/api/admin/funnel')
    assert r.status_code == 200
    assert 'embudo' in r.json()


def test_marketing_entra_a_marketing(como):
    r = como(MARIA).get('/api/admin/marketing/resumen')
    assert r.status_code == 200
    assert 'campanas' in r.json()


def test_marketing_entra_a_meta(como):
    r = como(MARIA).get('/api/admin/meta/dashboard')
    assert r.status_code == 200


def test_marketing_pasa_la_puerta_de_la_radiografia_de_campana(como):
    # 404 (no hay esa campaña en la base vacía), NO 403: la puerta la dejó pasar.
    r = como(MARIA).get('/api/admin/marketing/campana/12345')
    assert r.status_code == 404


def test_marketing_puede_subir_el_csv_de_meta(como):
    # El CSV es basura a propósito: un 400 demuestra que pasó la puerta y fue el
    # contenido (parte de SU chamba de anuncios) lo que se rechazó, no el rol.
    r = como(MARIA).post('/api/admin/meta/import', json={'csv': 'no,es,de,meta'})
    assert r.status_code == 400


# ------------------------------------------ lo que NO puede ver: 403 por ruta
def test_marketing_no_ve_pedidos(como):
    assert como(MARIA).get('/api/admin/orders').status_code == 403


def test_marketing_no_ve_clientes(como):
    assert como(MARIA).get('/api/admin/customers').status_code == 403


def test_marketing_no_toca_stock(como):
    r = como(MARIA).put('/api/admin/stock', json={'key': 'x', 'qty': 1})
    assert r.status_code == 403


def test_marketing_no_ve_cobros(como):
    assert como(MARIA).get('/api/admin/credenciales').status_code == 403


def test_marketing_no_ve_motor_precios(como):
    assert como(MARIA).get('/api/admin/motor-precios').status_code == 403


def test_marketing_no_ve_distribuidores(como):
    assert como(MARIA).get('/api/admin/distributors').status_code == 403


def test_marketing_tampoco_entra_a_lo_demas_del_admin(como):
    # Un barrido extra sobre rutas sensibles que no caen en los seis grupos.
    c = como(MARIA)
    assert c.get('/api/admin/stats').status_code == 403        # ventas
    assert c.get('/api/admin/analytics').status_code == 403    # ingresos
    assert c.get('/api/admin/series').status_code == 403       # serie de ventas
    assert c.get('/api/admin/intentos').status_code == 403     # carritos
    assert c.post('/api/admin/view-as/u-cli').status_code == 403  # impersonar


# ------------------------------------- el admin de verdad sigue entrando a todo
def test_admin_sigue_entrando_a_difusion(como):
    c = como(ADMIN)
    assert c.get('/api/admin/funnel').status_code == 200
    assert c.get('/api/admin/marketing/resumen').status_code == 200
    assert c.get('/api/admin/meta/dashboard').status_code == 200


def test_admin_sigue_entrando_al_resto(como):
    c = como(ADMIN)
    assert c.get('/api/admin/orders').status_code == 200
    assert c.get('/api/admin/customers').status_code == 200
    assert c.get('/api/admin/distributors').status_code == 200


# ----------------------------------- los otros roles NO heredaron la difusión
def test_distribuidor_y_cliente_no_ven_difusion(como):
    for quien in (DIST, CLIENTE):
        c = como(quien)
        assert c.get('/api/admin/funnel').status_code == 403
        assert c.get('/api/admin/marketing/resumen').status_code == 403
        assert c.get('/api/admin/meta/dashboard').status_code == 403
