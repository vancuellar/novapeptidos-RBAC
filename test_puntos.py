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

    async def delete_one(self, filtro):
        for i, d in enumerate(self.docs):
            if _match(d, filtro):
                self.docs.pop(i)
                return _Res(1)
        return _Res(0)

    async def delete_many(self, filtro):
        antes = len(self.docs)
        self.docs = [d for d in self.docs if not _match(d, filtro)]
        return _Res(antes - len(self.docs))

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


# ---------- comisión sobre mercancía pagada en puntos (el agujero del canje parcial) ----------
# La regla del 100% ya existía; estas pruebas cuidan el hueco de en medio: puntos
# pagando el 99% y $1 en efectivo NO puede pagar la comisión completa.

import pyramid


def _reparto():
    return [{'distributor_id': 'd1', 'role': 'seller', 'amount': 1000},
            {'distributor_id': 'd2', 'role': 'upline', 'amount': 200}]


def test_sin_puntos_la_comision_no_se_toca():
    rows = pyramid.prorratear_por_dinero(_reparto(), 10000, 10000)
    assert [r['amount'] for r in rows] == [1000, 200]


def test_mitad_en_puntos_mitad_de_comision():
    rows = pyramid.prorratear_por_dinero(_reparto(), 5000, 10000)
    assert [r['amount'] for r in rows] == [500, 100]


def test_un_peso_en_dinero_casi_no_paga_comision():
    # 9,999 de 10,000 en puntos: la comisión del vendedor cae a la milésima parte.
    rows = pyramid.prorratear_por_dinero(_reparto(), 1, 10000)
    assert sum(r['amount'] for r in rows) <= 1


def test_todo_en_puntos_deja_cero_renglones():
    rows = pyramid.prorratear_por_dinero(_reparto(), 0, 10000)
    assert rows == []


def test_los_renglones_en_cero_se_quitan():
    rows = pyramid.prorratear_por_dinero(_reparto(), 100, 10000)  # 1%
    # el upline de $200 queda en $2; el vendedor de $1000 en $10 — ninguno en 0
    assert all(r['amount'] > 0 for r in rows)


def test_el_checkout_prorratea_cuando_hay_puntos():
    # El endpoint es demasiado grande para armarlo aquí; se comprueba que el
    # prorrateo esté DENTRO del camino del checkout y ANTES de fijar la comisión.
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('pagado_todo_con_puntos = ')[1].split('order = Order(')[0]
    assert 'prorratear_por_dinero(' in cuerpo
    assert cuerpo.index('prorratear_por_dinero(') < cuerpo.index('seller_amount(')


# ---------- Borrado y archivado EN LOTE (Christián, 2026-07-29) ----------
# Christián quería limpiar los 12 pedidos de prueba de un golpe. El riesgo real no es
# borrar de más: es que entre esos 12 vive UNA venta de verdad (Paz Cambray, entregada)
# y la lista tiene "seleccionar todo".

def test_el_lote_no_borra_una_venta_pagada_sin_forzar():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_orders_lote(')[1].split('\n@api_router')[0]
    assert 'ESTADOS_PAGADOS' in cuerpo, 'se fue el candado de los pedidos pagados'
    assert 'payload.forzar' in cuerpo, 'se puede borrar un pedido pagado sin forzar'
    assert 'protegidos.append' in cuerpo, 'no avisa cuáles se protegieron'


def test_los_estados_pagados_son_los_tres():
    assert server.ESTADOS_PAGADOS == ('confirmado', 'enviado', 'entregado')


def test_borrar_en_lote_devuelve_puntos_e_inventario():
    """Un `delete_many` a secas dejaría los puntos regalados y el inventario corto.
    El lote tiene que usar el MISMO camino que el borrado de uno."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_orders_lote(')[1].split('\n@api_router')[0]
    assert 'revoke_order_points(order)' in cuerpo
    assert 'restore_order_stock(order)' in cuerpo
    assert 'db.points.delete_many' in cuerpo
    assert 'delete_many({' not in cuerpo.split('db.points')[0], 'borra órdenes en bloque'


def test_archivar_no_borra():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_orders_lote(')[1].split('\n@api_router')[0]
    rama = cuerpo.split("else:")[-1]
    assert 'update_one' in rama and 'archived' in rama
    assert 'delete_one' not in rama, 'archivar está borrando'


def test_la_lista_esconde_los_archivados_por_omision():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split("async def admin_orders(")[1].split('\n@api_router')[0]
    assert "{'archived': {'$ne': True}}" in cuerpo
    assert 'archivados' in cuerpo, 'no se pueden ver los archivados'


# ---------- Venta directa: el techo es 40%, no 60% (auditor de Codex, 2026-07-29) ----------

def test_la_venta_directa_no_puede_pasar_del_maximo_de_la_casa():
    """El `min` estaba en 0.60 y el máximo de la casa es 0.40. En un pedido de
    $374,360 la diferencia son $74,872 que salen de más."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_create_order(')[1].split('\n@api_router')[0]
    assert 'min(0.60' not in cuerpo, 'volvió el techo del 60%'
    assert 'min(loyalty.MAX_DISCOUNT' in cuerpo
    import loyalty
    assert loyalty.MAX_DISCOUNT == 0.40


def test_la_venta_directa_respeta_el_tope_por_producto():
    """El descuento se calculaba plano sobre el subtotal, ignorando el techo de cada
    producto — el que protege el 5× de la casa. El checkout público sí lo respeta."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_create_order(')[1].split('\n@api_router')[0]
    assert 'commission_cap' in cuerpo, 'no lee el tope del producto'
    assert 'NO_DISCOUNT_CATEGORIES' in cuerpo, 'los insumos vuelven a llevar descuento'
    assert 'round(subtotal * rate)' not in cuerpo, 'volvió el descuento plano'


# ---------- Pagado ≠ entregado (Christián, 2026-07-29) ----------
# La venta de Alanís salió ENTREGADA y SIN PAGAR, y el tablero la contaba como
# ingreso. Un reporte que dice que cobraste lo que no cobraste es peor que no tenerlo.

def test_un_pedido_entregado_pero_no_pagado_no_cuenta_como_ingreso():
    assert server.esta_pagado({'status': 'entregado', 'paid': False}) is False
    assert server.esta_pagado({'status': 'entregado', 'paid': True}) is True


def test_los_pedidos_viejos_sin_el_campo_siguen_infiriendo_del_estado():
    """No hay migración sobre la base de producción: los pedidos de antes no traen
    `paid` y deben seguir contando igual que siempre."""
    for est in ('confirmado', 'enviado', 'entregado'):
        assert server.esta_pagado({'status': est}) is True, est
    for est in ('pendiente', 'cancelado'):
        assert server.esta_pagado({'status': est}) is False, est


def test_un_cancelado_nunca_cuenta_aunque_diga_pagado():
    """Una devolución deja el pedido cancelado; el dinero ya salió de vuelta."""
    assert server.esta_pagado({'status': 'cancelado', 'paid': True}) is False


def test_el_tablero_separa_cobrado_de_por_cobrar():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split("async def admin_stats(")[1].split('\n@api_router')[0]
    assert 'esta_pagado(o)' in cuerpo, 'el ingreso ya no distingue lo cobrado'
    assert "'por_cobrar'" in cuerpo, 'un pedido fiado desaparecería del tablero'
    assert "'paid': 1" in cuerpo, 'la consulta no trae el campo y el ingreso saldría mal'


def test_marcar_el_pago_no_toca_el_estado_de_entrega():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_marcar_pago(')[1].split('\n@api_router')[0]
    assert "'paid'" in cuerpo and "'paid_at'" in cuerpo
    assert "'status'" not in cuerpo.split('cambio =')[1].split('}')[0], \
        'marcar el pago está moviendo el estado de entrega'
