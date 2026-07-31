"""EL CUPÓN DE UN SOLO USO, USADO DOS VECES. La carrera, reproducida y tapada.

Hallazgo del auditor de Codex (2026-07-31), COMPROBADO: el cupón se miraba al principio
del checkout (`not _c.get('used')`) y se marcaba usado hasta el final, ya grabado el
pedido. Entre esas dos líneas hay una docena de `await` —inventario, puntos, el insert
del pedido, los correos— y cada uno suelta el hilo. Dos checkouts simultáneos con el
MISMO cupón leían los dos `used: False`, los dos se llevaban el descuento, y un cupón de
un solo uso pagaba dos veces.

Es la TERCERA vez que aparece la misma carrera en esta casa: primero fue el inventario
(`_reservar_inventario`), luego los puntos (`_apartar_puntos`), y el cupón se había
quedado atrás con el patrón viejo. Por eso la última prueba de este archivo es un
guardia de código: si alguien vuelve a marcar el cupón sin condición, truena aquí.

El arreglo es el mismo candado de siempre: mirar y marcar EN EL MISMO PASO, con la
condición dentro del update.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


# ==========================================================================
#  Doble de la base con las condiciones que usa el canje del cupón
# ==========================================================================
def _match(doc, filtro):
    for k, v in (filtro or {}).items():
        if isinstance(v, dict):
            if '$ne' in v and doc.get(k) == v['$ne']:
                return False
            if '$gte' in v and not (doc.get(k) or 0) >= v['$gte']:
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

    async def find_one(self, filtro, proj=None):
        for d in self.docs:
            if _match(d, filtro):
                return dict(d)
        return None

    async def update_one(self, filtro, cambio, upsert=False):
        for d in self.docs:
            if _match(d, filtro):
                d.update(cambio.get('$set') or {})
                for k in (cambio.get('$unset') or {}):
                    d.pop(k, None)
                return _Res(1)
        return _Res(0)


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, nombre):
        return self.cols.setdefault(nombre, FakeCol())

    def __getattr__(self, nombre):
        return self[nombre]


CUPON = {'id': 'c1', 'code': 'GIFT-ABC', 'kind': 'coupon', 'discount_rate': 0.40,
         'active': True, 'used': False, 'single_use': True}


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    fake.cols['discount_codes'] = FakeCol([CUPON])
    monkeypatch.setattr(server, 'db', fake)
    return fake


def cupon(db):
    return db.cols['discount_codes'].docs[0]


# ==========================================================================
#  1. LA CARRERA
# ==========================================================================
def test_el_mismo_cupon_no_se_puede_quemar_dos_veces(db):
    """El pedido de al lado ya se lo llevó: el segundo NO lo vuelve a quemar."""
    assert asyncio.run(server._apartar_cupon(CUPON, 'EX-1')) is True
    assert cupon(db)['used'] is True
    assert cupon(db)['used_order'] == 'EX-1'
    # el segundo checkout, con el MISMO cupón leído antes de que el otro lo quemara
    assert asyncio.run(server._apartar_cupon(CUPON, 'EX-2')) is False
    # y no se lo robó al primero: el pedido dueño sigue siendo el que llegó primero
    assert cupon(db)['used_order'] == 'EX-1'


def test_dos_checkouts_de_verdad_en_paralelo_solo_uno_gana(db):
    """La carrera tal cual: los dos leen el cupón sin usar y los dos intentan quemarlo.

    Antes del arreglo los dos escribían `used: True` y los dos se quedaban con el
    descuento. Ahora la condición viaja dentro del update y sólo uno gana."""
    async def dos_a_la_vez():
        leido_a = await server.db.discount_codes.find_one({'id': 'c1'})
        leido_b = await server.db.discount_codes.find_one({'id': 'c1'})
        assert not leido_a['used'] and not leido_b['used'], 'los dos lo leyeron libre'
        return await asyncio.gather(server._apartar_cupon(leido_a, 'EX-A'),
                                    server._apartar_cupon(leido_b, 'EX-B'))

    ganaron = asyncio.run(dos_a_la_vez())
    assert sorted(ganaron) == [False, True], f'ganaron {ganaron}: el cupón se usó dos veces'
    assert cupon(db)['used'] is True


def test_un_cupon_ya_usado_no_se_puede_reciclar(db):
    cupon(db)['used'] = True
    assert asyncio.run(server._apartar_cupon(CUPON, 'EX-9')) is False


def test_un_cupon_reutilizable_no_se_quema(db):
    """Los que no son de un solo uso pasan siempre y no se marcan."""
    reusable = dict(CUPON, single_use=False)
    assert asyncio.run(server._apartar_cupon(reusable, 'EX-1')) is True
    assert cupon(db)['used'] is False


def test_sin_cupon_no_estorba(db):
    assert asyncio.run(server._apartar_cupon(None, 'EX-1')) is True


def test_un_pedido_que_no_llego_a_existir_revive_el_cupon(db):
    """Si el insert del pedido truena, el cupón vuelve a estar disponible: el cliente
    no puede perder su regalo por un fallo nuestro."""
    asyncio.run(server._apartar_cupon(CUPON, 'EX-1'))
    assert cupon(db)['used'] is True
    asyncio.run(server._devolver_cupon(CUPON))
    assert cupon(db)['used'] is False
    assert cupon(db)['active'] is True
    assert 'used_order' not in cupon(db)


# ==========================================================================
#  2. GUARDIA DE CÓDIGO — que no vuelva el patrón viejo
# ==========================================================================
def test_el_cupon_se_quema_ANTES_de_grabar_el_pedido():
    """El `$set` sin condición que había después de grabar es exactamente lo que dejaba
    pasar la carrera. Es la tercera vez que este bug aparece en esta casa (inventario,
    puntos, cupón): si alguien lo devuelve, esto truena."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def create_order(')[1].split('\n@api_router')[0]
    assert '_apartar_cupon(' in cuerpo, 'el cupón ya no se quema en un solo paso'
    assert "{'$set': {'used': True, 'active': False, 'used_order'" not in cuerpo, (
        'volvió el marcado del cupón sin condición: dos pedidos a la vez '
        'pueden usar el mismo cupón de un solo uso')


def test_el_cupon_se_aparta_antes_del_insert_del_pedido():
    """Y ANTES del insert, no después: si se quemara después, un pedido que truena al
    grabarse dejaría el cupón quemado sin venta."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def create_order(')[1].split('\n@api_router')[0]
    assert cuerpo.index('_apartar_cupon(') < cuerpo.index('db.orders.insert_one'), (
        'el cupón se quema DESPUÉS de grabar el pedido')
