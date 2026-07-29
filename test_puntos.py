"""Pruebas del canje de puntos: que los mismos puntos no se gasten dos veces.

1 punto = 1 peso de mercancía, así que un punto gastado dos veces es dinero que sale.
Había DOS formas de gastarlos dos veces y las dos se cierran aquí:

  1. LA CARRERA. El saldo se leía en una consulta y se restaba mucho después, ya
     grabado el pedido. Dos checkouts del mismo cliente a la vez leían 1,000 puntos,
     los dos los canjeaban enteros y el saldo terminaba en −1,000.
  2. CANCELAR Y RECONFIRMAR. Cancelar devolvía lo canjeado y dejaba la marca
     `points_refunded`; reactivar el pedido no la miraba. El cliente se quedaba con
     el pedido Y con los puntos.

El doble de base de abajo copia la semántica de Mongo que importa para esto: un
`update_one` con condición **no toca nada** si la condición ya no se cumple, y lo
dice en `matched_count`. Sin eso la prueba no probaría nada.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


# ==========================================================================
#  Doble de la base con las condiciones que usa el canje
# ==========================================================================
def _match(doc, filtro):
    for k, v in (filtro or {}).items():
        if isinstance(v, dict):
            if '$gte' in v and not (doc.get(k) or 0) >= v['$gte']:
                return False
            if '$ne' in v and doc.get(k) == v['$ne']:
                return False
            if '$in' in v and doc.get(k) not in v['$in']:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class _Res:
    def __init__(self, n):
        self.matched_count = self.modified_count = n


class FakeCol:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, filtro, proj=None):
        for d in self.docs:
            if _match(d, filtro):
                return dict(d)
        return None

    async def update_one(self, filtro, cambio, upsert=False):
        for d in self.docs:
            if _match(d, filtro):
                d.update(cambio.get('$set') or {})
                for k, n in (cambio.get('$inc') or {}).items():
                    d[k] = (d.get(k) or 0) + n
                return _Res(1)
        return _Res(0)


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, nombre):
        return self.cols.setdefault(nombre, FakeCol())

    def __getattr__(self, nombre):
        return self[nombre]


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    fake.cols['users'] = FakeCol([{'id': 'u1', 'points_balance': 1000}])
    monkeypatch.setattr(server, 'db', fake)
    return fake


def saldo(db):
    return db.cols['users'].docs[0]['points_balance']


# ==========================================================================
#  1. La carrera: dos pedidos, los mismos puntos
# ==========================================================================
def test_los_mismos_puntos_no_se_pueden_apartar_dos_veces(db):
    """El pedido de al lado ya se los llevó: el segundo NO aparta nada."""
    assert asyncio.run(server._apartar_puntos('u1', 1000)) is True
    assert saldo(db) == 0
    assert asyncio.run(server._apartar_puntos('u1', 1000)) is False
    assert saldo(db) == 0                    # y NUNCA en negativo


def test_apartar_de_mas_no_deja_el_saldo_en_negativo(db):
    assert asyncio.run(server._apartar_puntos('u1', 1001)) is False
    assert saldo(db) == 1000


def test_apartar_lo_justo_si_alcanza(db):
    assert asyncio.run(server._apartar_puntos('u1', 400)) is True
    assert saldo(db) == 600


def test_cero_puntos_no_toca_el_saldo(db):
    assert asyncio.run(server._apartar_puntos('u1', 0)) is True
    assert asyncio.run(server._apartar_puntos('u1', None)) is True
    assert saldo(db) == 1000


def test_un_pedido_que_no_llego_a_existir_devuelve_los_puntos(db):
    asyncio.run(server._apartar_puntos('u1', 300))
    asyncio.run(server._devolver_puntos('u1', 300))
    assert saldo(db) == 1000


def test_el_canje_se_aparta_ANTES_de_grabar_el_pedido():
    """Guardia de código: el `$inc` a secas que había después de grabar es
    exactamente lo que dejaba pasar la carrera. Si alguien lo devuelve, esto truena."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def create_order(')[1].split('\n@api_router')[0]
    assert '_apartar_puntos(' in cuerpo, 'el canje ya no se aparta en un solo paso'
    assert "{'$inc': {'points_balance': -points_used}}" not in cuerpo, (
        'volvió el descuento de puntos sin condición: dos pedidos a la vez '
        'pueden gastar el mismo saldo')
    # y se aparta antes de insertar el pedido, no después
    assert cuerpo.index('_apartar_puntos(') < cuerpo.index('db.orders.insert_one(')


# ==========================================================================
#  2. Cancelar y reconfirmar
# ==========================================================================
def _pedido(**extra):
    d = {'id': 'o1', 'order_number': 'EXY-1', 'user_id': 'u1', 'points_used': 400,
         'points_refunded': True}
    d.update(extra)
    return d


def test_reactivar_un_pedido_cancelado_vuelve_a_cobrar_los_puntos(db):
    orden = _pedido()
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.recobrar_puntos_canjeados(orden))
    assert saldo(db) == 600
    assert db.cols['orders'].docs[0]['points_refunded'] is False


def test_reactivar_dos_veces_solo_cobra_una(db):
    orden = _pedido()
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.recobrar_puntos_canjeados(orden))
    asyncio.run(server.recobrar_puntos_canjeados(dict(orden, points_refunded=False)))
    assert saldo(db) == 600


def test_un_pedido_que_nunca_se_cancelo_no_se_toca(db):
    orden = _pedido(points_refunded=False)
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.recobrar_puntos_canjeados(orden))
    assert saldo(db) == 1000


def test_si_ya_no_tiene_los_puntos_no_se_deja_el_saldo_en_negativo(db):
    db.cols['users'].docs[0]['points_balance'] = 100        # se los gastó mientras tanto
    orden = _pedido()
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.recobrar_puntos_canjeados(orden))
    assert saldo(db) == 100
    # la marca vuelve a su sitio para que un humano lo resuelva, no se pierde
    assert db.cols['orders'].docs[0]['points_refunded'] is True


def test_la_reactivacion_se_cobra_antes_de_depositar_puntos_nuevos():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def update_order_status(')[1].split('\n@api_router')[0]
    assert 'recobrar_puntos_canjeados(order)' in cuerpo
    assert cuerpo.index('recobrar_puntos_canjeados(') < cuerpo.index('award_order_points(')
