"""BARRER LOS PEDIDOS DE PRUEBA SIN LLEVARSE UNA VENTA DE VERDAD.

⛔ POR QUÉ EXISTE ESTE ARCHIVO (Christián, 2026-08-01): *«Asegúrate de borrar los
pedidos de prueba cuando termines de hacer las pruebas. De otra manera queda mucha
basura en el sitio.»*

La línea que no se cruza no es "que barra bien": es que **no pueda** llevarse una venta
real. Entre los pedidos de prueba viven las ventas de verdad de esos mismos días —Paz
Cambray (entregada, sin campo `paid`) y Alanís (entregada y FIADA, `paid: False`)— y
las dos se ven distinto: si el barrido preguntara sólo "¿está pagado?", se llevaría la
de Alanís, porque su dinero todavía no entra pero su mercancía ya salió.

Se comprueban las dos mitades:
  · la decisión pura (`pruebas.senales_de_venta_real`), sin base de datos;
  · el endpoint de verdad, con un doble de Mongo, incluido que el borrado se lo delega
    al lote de siempre con `forzar=False` — o sea que el candado viejo sigue puesto.
"""
import asyncio
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

from datetime import datetime, timedelta, timezone

import pruebas
import server


def _correr(corutina):
    return asyncio.new_event_loop().run_until_complete(corutina)


def _hoy(dias_atras=0):
    return (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()


# --------------------------------------------------------------------------
# Doble de Mongo. Sólo lo que tocan estos dos endpoints.
# --------------------------------------------------------------------------
def _coincide(doc, query):
    for clave, esperado in (query or {}).items():
        if doc.get(clave) != esperado:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


class _Coleccion:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, query=None, _proj=None):
        return _Cursor([d for d in self.docs if _coincide(d, query or {})])

    async def find_one(self, query=None, _proj=None):
        for d in self.docs:
            if _coincide(d, query or {}):
                return dict(d)
        return None

    async def update_one(self, query, update):
        for d in self.docs:
            if _coincide(d, query):
                d.update(update.get('$set', {}))
                break

    async def delete_one(self, query):
        self.docs = [d for d in self.docs if not _coincide(d, query)]

    async def delete_many(self, query):
        self.docs = [d for d in self.docs if not _coincide(d, query)]


class _Base:
    def __init__(self, orders=None):
        self.orders = _Coleccion(orders)
        self.points = _Coleccion()


ADMIN = {'email': 'admin@exygenlabs.com'}


def _con_base(base, corutina_fn):
    """Corre el endpoint de verdad contra el doble de Mongo.

    `revoke_order_points` y `restore_order_stock` se sustituyen por espías: lo que
    interesa aquí es QUÉ pedidos se borran, no la aritmética de puntos —eso ya lo
    cuidan las pruebas del lote en test_puntos.py.
    """
    devueltos = []
    original_db = server.db
    original_puntos = server.revoke_order_points
    original_stock = server.restore_order_stock

    async def _espia_puntos(order):
        devueltos.append(('puntos', order.get('order_number')))

    async def _espia_stock(order):
        devueltos.append(('stock', order.get('order_number')))

    server.db = base
    server.revoke_order_points = _espia_puntos
    server.restore_order_stock = _espia_stock
    try:
        return _correr(corutina_fn()), devueltos
    finally:
        server.db = original_db
        server.revoke_order_points = original_puntos
        server.restore_order_stock = original_stock


# --------------------------------------------------------------------------
# Los pedidos de la historia real, tal como se ven en la base.
# --------------------------------------------------------------------------
PAZ = {                                  # venta real VIEJA: ni siquiera trae `paid`
    'id': 'o-paz', 'order_number': 'EX-20260723-9064', 'status': 'entregado',
    'total': 3347.0, 'created_at': _hoy(9), 'es_prueba': True,   # marcada por error
    'customer': {'full_name': 'Paz Cambray', 'email': 'paz@ejemplo.com'},
}
ALANIS = {                               # venta real ENTREGADA Y FIADA
    'id': 'o-alanis', 'order_number': 'EX-20260729-9934', 'status': 'entregado',
    'paid': False, 'total': 3857.0, 'created_at': _hoy(3), 'es_prueba': True,
    'customer': {'full_name': 'Alanis Fernanda Mendoza', 'email': 'alanis@ejemplo.com'},
}
BASURA = {                               # el pedido que dejó la prueba del carrito
    'id': 'o-basura', 'order_number': 'EX-20260801-1111', 'status': 'pendiente',
    'paid': False, 'total': 1259.0, 'created_at': _hoy(0), 'es_prueba': True,
    'customer': {'full_name': 'Prueba Carrito', 'email': 'prueba@exygenlabs.com'},
}
CLIENTE_NUEVO = {                        # pedido de verdad, recién puesto y SIN marcar
    'id': 'o-nuevo', 'order_number': 'EX-20260801-2222', 'status': 'pendiente',
    'paid': False, 'total': 2999.0, 'created_at': _hoy(0),
    'customer': {'full_name': 'Cliente Real', 'email': 'cliente@ejemplo.com'},
}


# ------------------------------------------------------- la decisión, sin base de datos
def test_un_pedido_de_prueba_limpio_se_puede_barrer():
    assert pruebas.senales_de_venta_real(BASURA) == []
    assert pruebas.se_puede_barrer(BASURA) is True


def test_una_venta_entregada_sin_pagar_NO_se_barre():
    """Alanís: `paid: False` y sin embargo la mercancía ya salió. Si el barrido
    preguntara sólo por el dinero, se la llevaría."""
    assert 'surtido' in pruebas.senales_de_venta_real(ALANIS)
    assert pruebas.se_puede_barrer(ALANIS) is False


def test_una_venta_vieja_sin_el_campo_paid_NO_se_barre():
    assert 'paid' not in PAZ
    assert pruebas.senales_de_venta_real(PAZ)
    assert pruebas.se_puede_barrer(PAZ) is False


def test_un_comprobante_o_una_guia_bastan_para_no_tocarlo():
    con_comprobante = {**BASURA, 'spei_receipt_at': _hoy(0)}
    con_guia = {**BASURA, 'tracking_number': '7712345678'}
    assert pruebas.senales_de_venta_real(con_comprobante) == ['comprobante']
    assert pruebas.senales_de_venta_real(con_guia) == ['guia']
    assert not pruebas.se_puede_barrer(con_comprobante)
    assert not pruebas.se_puede_barrer(con_guia)


def test_lo_que_nadie_marco_no_se_barre_aunque_este_limpio():
    """El pedido de un cliente de verdad, pendiente de pago, se ve IGUAL de limpio que
    la basura de una prueba. Lo único que los distingue es la etiqueta."""
    assert pruebas.senales_de_venta_real(CLIENTE_NUEVO) == []
    assert pruebas.se_puede_barrer(CLIENTE_NUEVO) is False


def test_un_pedido_que_ya_no_existe_no_se_da_por_bueno():
    assert pruebas.senales_de_venta_real(None) == ['fantasma']
    assert pruebas.se_puede_barrer(None) is False


# ------------------------------------------------------------------- marcar y desmarcar
def test_marcar_pone_la_etiqueta_y_dice_si_se_podra_barrer():
    base = _Base(orders=[dict(CLIENTE_NUEVO)])
    r, _ = _con_base(base, lambda: server.admin_marcar_prueba(
        'o-nuevo', server.MarcaDePrueba(es_prueba=True), admin=ADMIN))
    assert r['es_prueba'] is True and r['se_puede_barrer'] is True
    assert base.orders.docs[0]['es_prueba'] is True


def test_marcar_una_venta_real_avisa_que_el_barrido_no_la_tocara():
    base = _Base(orders=[dict(ALANIS)])
    r, _ = _con_base(base, lambda: server.admin_marcar_prueba(
        'o-alanis', server.MarcaDePrueba(es_prueba=True), admin=ADMIN))
    assert r['se_puede_barrer'] is False and 'surtido' in r['motivos']


def test_desmarcar_deja_el_pedido_fuera_del_barrido():
    base = _Base(orders=[dict(BASURA)])
    _con_base(base, lambda: server.admin_marcar_prueba(
        'o-basura', server.MarcaDePrueba(es_prueba=False), admin=ADMIN))
    assert base.orders.docs[0]['es_prueba'] is False
    r, _ = _con_base(base, lambda: server.admin_barrer_pruebas(
        server.BarridoDePruebas(simulacro=False), admin=ADMIN))
    assert r['borrados'] == 0 and len(base.orders.docs) == 1


# -------------------------------------------------------------------------- el barrido
def test_el_simulacro_no_toca_nada():
    base = _Base(orders=[dict(PAZ), dict(ALANIS), dict(BASURA), dict(CLIENTE_NUEVO)])
    r, devueltos = _con_base(base, lambda: server.admin_barrer_pruebas(
        server.BarridoDePruebas(simulacro=True), admin=ADMIN))
    assert r['simulacro'] is True and r['borrados'] == 0
    assert r['numeros'] == [BASURA['order_number']]
    assert len(base.orders.docs) == 4 and devueltos == []


def test_el_barrido_se_lleva_la_basura_y_deja_las_ventas_de_verdad():
    base = _Base(orders=[dict(PAZ), dict(ALANIS), dict(BASURA), dict(CLIENTE_NUEVO)])
    r, devueltos = _con_base(base, lambda: server.admin_barrer_pruebas(
        server.BarridoDePruebas(simulacro=False), admin=ADMIN))
    assert r['borrados'] == 1 and r['numeros'] == [BASURA['order_number']]
    quedan = {d['order_number'] for d in base.orders.docs}
    assert quedan == {PAZ['order_number'], ALANIS['order_number'],
                      CLIENTE_NUEVO['order_number']}
    # y devolvió lo que ese pedido se había llevado (puntos e inventario)
    assert ('puntos', BASURA['order_number']) in devueltos
    assert ('stock', BASURA['order_number']) in devueltos


def test_el_barrido_dice_por_que_no_toco_cada_uno():
    base = _Base(orders=[dict(PAZ), dict(ALANIS), dict(BASURA)])
    r, _ = _con_base(base, lambda: server.admin_barrer_pruebas(
        server.BarridoDePruebas(simulacro=True), admin=ADMIN))
    por_numero = {p['order_number']: p['motivos'] for p in r['protegidos']}
    assert set(por_numero) == {PAZ['order_number'], ALANIS['order_number']}
    assert all(m for m in por_numero.values()), 'un protegido sin motivo'
    # Los motivos viajan como CLAVE (el Panel habla tres idiomas y traduce allá).
    for motivos in por_numero.values():
        assert all(m in pruebas.MOTIVOS for m in motivos)


def test_sin_pedidos_marcados_el_barrido_no_hace_nada():
    base = _Base(orders=[dict(CLIENTE_NUEVO)])
    r, _ = _con_base(base, lambda: server.admin_barrer_pruebas(
        server.BarridoDePruebas(simulacro=False), admin=ADMIN))
    assert r['marcados'] == 0 and r['borrados'] == 0 and len(base.orders.docs) == 1


# ------------------------------------------------------------------ el candado de fondo
def test_el_barrido_no_borra_por_su_cuenta_sino_por_el_lote_y_sin_forzar():
    """El candado de los pedidos pagados vive en `/admin/orders/lote`. Si el barrido se
    escribiera su propio `delete_one`, ese candado dejaría de protegerlo."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_barrer_pruebas(')[1].split('\n@api_router')[0]
    assert 'admin_orders_lote(' in cuerpo, 'el barrido dejó de usar el lote de siempre'
    assert 'forzar=False' in cuerpo, 'el barrido puede forzar el borrado de un pagado'
    assert 'delete_one' not in cuerpo and 'delete_many' not in cuerpo, \
        'el barrido se escribió su propio borrado y se saltó el candado'


def test_el_barrido_solo_mira_los_marcados():
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_barrer_pruebas(')[1].split('\n@api_router')[0]
    assert "{'es_prueba': True}" in cuerpo, 'el barrido dejó de filtrar por la etiqueta'
    assert 'senales_de_venta_real' in cuerpo, 'se fue el segundo filtro'
