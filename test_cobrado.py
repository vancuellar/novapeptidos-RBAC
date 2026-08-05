"""PAGADO ≠ ENTREGADO: que ninguna pantalla cuente como ingreso lo que no se cobró.

⛔ POR QUÉ EXISTE ESTE ARCHIVO. El 2026-07-29 se separó `paid` del `status` del pedido y
se arregló `/admin/stats`… y nada más. La venta de Alanís (EX-20260729-9934, $3,857,
ENTREGADA y sin pagar) seguía pintada como ingreso en la gráfica de ventas, en
analytics, en el embudo, en el reporte de marketing y en los tableros de distribuidor:
el tablero decía $7,204 cobrados cuando en la cuenta había $3,347.

La lección es que "el dinero se suma en un solo lugar" era falso: se suma en NUEVE. Así
que estas pruebas no comprueban la función que decide (`cobrado.esta_pagado`, que ya
tenía pruebas y no era el problema) sino CADA PANTALLA que reporta dinero, con dos
pedidos: uno cobrado y uno fiado. Si mañana alguien agrega un reporte nuevo y suma
`total` a secas, que al menos ninguno de los viejos se le vuelva a caer.

Las dos reglas que se protegen:
  1. Un pedido con `paid: False` explícito NUNCA suma como ingreso. Sale como deuda.
  2. Un pedido VIEJO sin el campo `paid` sigue contando igual que siempre (se infiere
     del estado). No hubo migración sobre la base de producción y no debe haberla.
"""
import asyncio
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

from datetime import datetime, timedelta, timezone

import cobrado
import director
import marketing
import pyramid
import server


def _correr(corutina):
    return asyncio.new_event_loop().run_until_complete(corutina)


def _hoy(dias_atras=0):
    return (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()


# --------------------------------------------------------------------------
# Doble de Mongo. Sólo lo que usan los reportes: filtros planos, $ne, $in,
# $nin, $gte y $or. No pretende ser Mongo; pretende ser suficiente para que
# las pruebas ejerciten el ENDPOINT DE VERDAD y no una copia de su aritmética.
# --------------------------------------------------------------------------
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


def _valor(doc, ruta):
    actual = doc
    for parte in ruta.split('.'):
        if isinstance(actual, list):
            # 'commissions.distributor_id' → cualquiera de las filas
            return [x.get(parte) for x in actual if isinstance(x, dict)]
        if not isinstance(actual, dict):
            return None
        actual = actual.get(parte)
    return actual


def _cumple(valor, cond):
    if isinstance(cond, dict):
        for op, esperado in cond.items():
            if op == '$ne' and valor == esperado:
                return False
            if op == '$in':
                hay = valor if isinstance(valor, list) else [valor]
                if not any(v in esperado for v in hay):
                    return False
            if op == '$nin' and valor in esperado:
                return False
            if op == '$gte' and not (valor is not None and valor >= esperado):
                return False
        return True
    if isinstance(valor, list):
        return cond in valor
    return valor == cond


def _coincide(doc, query):
    for clave, cond in (query or {}).items():
        if clave == '$or':
            if not any(_coincide(doc, sub) for sub in cond):
                return False
            continue
        if not _cumple(_valor(doc, clave), cond):
            return False
    return True


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

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _coincide(d, query or {}))

    async def update_one(self, query, update):
        class _R:
            modified_count = 0
        for d in self.docs:
            if _coincide(d, query):
                d.update(update.get('$set', {}))
                for k, v in (update.get('$inc') or {}).items():
                    d[k] = (d.get(k) or 0) + v
                _R.modified_count = 1
                break
        return _R()

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class _Base:
    def __init__(self, **colecciones):
        for nombre in ('orders', 'users', 'products', 'events', 'discount_codes',
                       'points', 'protocols'):
            setattr(self, nombre, _Coleccion(colecciones.get(nombre)))


# --------------------------------------------------------------------------
# Los dos pedidos de la historia real, tal como están en la base viva.
# --------------------------------------------------------------------------
PAZ = {
    'id': 'o-paz', 'order_number': 'EX-20260723-9064', 'user_id': 'u-paz',
    'status': 'entregado',              # ⚠️ sin campo `paid`: es un pedido VIEJO
    'total': 3347.0, 'created_at': _hoy(6), 'payment_method': 'directa',
    'customer': {'full_name': 'Paz Cambray', 'email': 'paz@ejemplo.com'},
    'items': [{'name': 'Tirzepatida 10 mg', 'price': 2119.0, 'quantity': 1},
              {'name': 'NAD+ 500 mg', 'price': 1259.0, 'quantity': 1}],
}
ALANIS = {
    'id': 'o-alanis', 'order_number': 'EX-20260729-9934', 'user_id': 'u-alanis',
    'status': 'entregado', 'paid': False,   # entregado Y FIADO: el bug de todo esto
    'total': 3857.0, 'created_at': _hoy(0), 'payment_method': 'directa',
    'customer': {'full_name': 'Alanis Fernanda Mendoza', 'email': 'alanis@ejemplo.com'},
    'items': [{'name': 'Retatrutida 40 mg', 'price': 4314.0, 'quantity': 1}],
}
COBRADO_REAL = 3347.0
FIADO = 3857.0


def _base_con_los_dos():
    return _Base(orders=[PAZ, ALANIS],
                 users=[{'id': 'u-paz', 'name': 'Paz', 'email': 'paz@ejemplo.com', 'role': 'user'},
                        {'id': 'u-alanis', 'name': 'Alanis', 'email': 'alanis@ejemplo.com',
                         'role': 'user'}])


def _con_base(base, corutina_fn):
    original = server.db
    server.db = base
    try:
        return _correr(corutina_fn())
    finally:
        server.db = original


# ---------------------------------------------------------------- la regla
def test_lo_entregado_y_fiado_no_es_ingreso_pero_si_es_deuda():
    assert cobrado.cobrado_de(ALANIS) == 0
    assert cobrado.por_cobrar_de(ALANIS) == FIADO


def test_un_pedido_viejo_sin_el_campo_sigue_contando_como_siempre():
    """No hay migración sobre la base de producción: los pedidos de antes del
    2026-07-29 no traen `paid` y su ingreso NO puede desaparecer del historial."""
    assert 'paid' not in PAZ
    assert cobrado.cobrado_de(PAZ) == COBRADO_REAL
    assert cobrado.por_cobrar_de(PAZ) == 0


def test_un_cancelado_no_es_ni_ingreso_ni_deuda():
    """El dinero se devolvió: contarlo como cuenta por cobrar sería inventar un activo."""
    cancelado = {**PAZ, 'status': 'cancelado'}
    assert cobrado.cobrado_de(cancelado) == 0
    assert cobrado.por_cobrar_de(cancelado) == 0


# ---------------------------------------------------------------- /admin/stats
def test_stats_separa_cobrado_de_por_cobrar():
    base = _base_con_los_dos()
    r = _con_base(base, lambda: server.admin_stats(admin={'email': 'a@b.c'}))
    assert r['revenue'] == COBRADO_REAL
    assert r['por_cobrar'] == FIADO


# ------------------------------------------------------------ /admin/analytics
def test_analytics_no_cuenta_la_venta_fiada_como_ingreso():
    """ERA EL BUG QUE VEÍA CHRISTIÁN: aquí salía $7,204 (los dos pedidos sumados)."""
    base = _base_con_los_dos()
    r = _con_base(base, lambda: server.admin_analytics(admin={'email': 'a@b.c'}))
    assert r['revenue_total'] == COBRADO_REAL
    assert r['por_cobrar'] == FIADO
    # La gráfica por mes: el mes trae los dos pedidos, pero sólo un ingreso.
    mes = [m for m in r['monthly'] if m['revenue'] or m['por_cobrar']]
    assert sum(m['revenue'] for m in mes) == COBRADO_REAL
    assert sum(m['por_cobrar'] for m in mes) == FIADO
    # El ticket promedio sale de lo COBRADO entre los pedidos COBRADOS (uno solo).
    assert r['avg_ticket'] == round(COBRADO_REAL)


def test_analytics_no_pone_el_producto_fiado_en_el_ranking_de_ingreso():
    """El ranking de productos ordenaba por dinero que no había entrado: la
    Retatrutida de Alanís aparecía como el producto que más vendía."""
    base = _base_con_los_dos()
    r = _con_base(base, lambda: server.admin_analytics(admin={'email': 'a@b.c'}))
    nombres = [p['name'] for p in r['top_products']]
    assert 'Retatrutida 40 mg' not in nombres
    assert 'Tirzepatida 10 mg' in nombres
    # Y el corte por método de pago tampoco: sumaba las dos ventas directas.
    assert sum(p['revenue'] for p in r['by_payment']) == COBRADO_REAL
    # El pedido fiado SÍ sigue contado como pedido: la venta existe.
    assert r['by_status']['entregado'] == 2


# --------------------------------------------------------------- /admin/series
def test_la_grafica_de_ventas_pinta_el_ingreso_cobrado_y_la_deuda_aparte():
    base = _base_con_los_dos()
    r = _con_base(base, lambda: server.admin_series(bucket='day', days=30,
                                                   admin={'email': 'a@b.c'}))
    assert r['totales']['ingreso'] == COBRADO_REAL
    assert r['totales']['por_cobrar'] == FIADO
    # Los dos pedidos siguen contando como pedidos (la conversión no cambia).
    assert r['totales']['pedidos'] == 2
    # El ticket NO es 7204/2 ni 3347/2: es lo cobrado entre los pedidos cobrados.
    assert r['totales']['ticket'] == round(COBRADO_REAL)
    hoy = [f for f in r['serie'] if f['periodo'] == datetime.now(timezone.utc).strftime('%Y-%m-%d')][0]
    assert hoy['ingreso'] == 0 and hoy['por_cobrar'] == FIADO


def test_en_la_grafica_el_pedido_viejo_sigue_dando_ingreso():
    base = _Base(orders=[PAZ])
    r = _con_base(base, lambda: server.admin_series(bucket='month', days=60,
                                                   admin={'email': 'a@b.c'}))
    assert r['totales']['ingreso'] == COBRADO_REAL
    assert r['totales']['por_cobrar'] == 0


# --------------------------------------------------------------- /admin/funnel
def _evento(tipo, sesion, **extra):
    return {'type': tipo, 'session_id': sesion, 'created_at': _hoy(0), **extra}


def test_el_embudo_verifica_el_ingreso_contra_los_pedidos():
    """El evento `purchase` lo escribe el navegador con el monto pegado, así que el
    embudo cobraba de palabra. Ahora se le pregunta a la base quién pagó."""
    base = _Base(
        orders=[PAZ, ALANIS],
        events=[_evento('visit', 's1'), _evento('visit', 's2'),
                _evento('purchase', 's1', value=COBRADO_REAL,
                        order_number=PAZ['order_number']),
                _evento('purchase', 's2', value=FIADO,
                        order_number=ALANIS['order_number'])])
    r = _con_base(base, lambda: server.admin_funnel(days=30, admin={'email': 'a@b.c'}))
    assert r['ingreso'] == round(COBRADO_REAL)
    assert r['por_cobrar'] == round(FIADO)
    # Las dos compras siguen siendo dos pasos del embudo: la gente sí compró.
    assert dict((p['paso'], p['sesiones']) for p in r['embudo'])['purchase'] == 2
    # Y por origen tampoco se infla el ingreso de un canal con fiado.
    assert sum(o['ingreso'] for o in r['por_origen']) == round(COBRADO_REAL)


def test_una_compra_cuyo_pedido_ya_no_existe_no_es_ingreso_ni_deuda():
    """⛔ EL CASO REAL DE LA BASE VIVA: hay cinco eventos de compra ($11,027) de los
    pedidos de prueba que se borraron. El embudo los sumaba como ingreso. Un pedido
    borrado no tiene a quién cobrarle, así que tampoco es cuenta por cobrar."""
    base = _Base(orders=[PAZ],
                 events=[_evento('visit', 's1'), _evento('visit', 's9'),
                         _evento('purchase', 's1', value=COBRADO_REAL,
                                 order_number=PAZ['order_number']),
                         _evento('purchase', 's9', value=5509.0,
                                 order_number='EX-20260725-7587')])
    r = _con_base(base, lambda: server.admin_funnel(days=30, admin={'email': 'a@b.c'}))
    assert r['ingreso'] == round(COBRADO_REAL)
    assert r['por_cobrar'] == 0
    assert r['ingreso_sin_pedido'] == 5509
    assert sum(o['ingreso'] + o['por_cobrar'] for o in r['por_origen']) == round(COBRADO_REAL)


def test_el_embudo_le_cree_a_un_evento_viejo_sin_numero_de_pedido():
    """⚠️ ESTA PRUEBA CAMBIÓ EL 2026-08-04 y el cambio ES el arreglo.

    Antes exigía que un evento `purchase` SIN número de pedido sumara su monto al
    ingreso ($1,000). Ese «creerle al navegador» es lo que hacía que el panel
    dijera $87,193 con $9,973 en la cuenta: el monto lo escribe el navegador, el
    evento sobrevive a que el pedido se borre, y una recarga de la página de
    gracias lo mandaba otra vez.

    La distinción que se conserva: el evento sigue contando como PASO del embudo
    —esa persona sí llegó hasta el final, y descartarla sería borrar historia—,
    pero ya no cuenta como DINERO, porque no hay pedido que lo respalde. El
    ingreso sale de `orders`, que es donde vive el dinero de verdad."""
    base = _Base(orders=[], events=[_evento('visit', 's1'),
                                    _evento('purchase', 's1', value=1000.0)])
    r = _con_base(base, lambda: server.admin_funnel(days=30, admin={'email': 'a@b.c'}))
    assert r['ingreso'] == 0
    # Pero la persona sí aparece en el paso de compra del embudo.
    paso = next(p for p in r['embudo'] if p['paso'] == 'purchase')
    assert paso['personas'] == 1
    # Y lo que el navegador dijo y nadie respalda queda A LA VISTA, no escondido.
    assert r['ingreso_sin_pedido'] == 1000


# ------------------------------------------------------ fichas de cliente
def test_la_ficha_del_cliente_no_dice_que_pago_lo_que_debe():
    base = _base_con_los_dos()
    lista = _con_base(base, lambda: server.admin_customers(admin={'email': 'a@b.c'}))
    por_nombre = {u['name']: u for u in lista}
    assert por_nombre['Alanis']['total_spent'] == 0
    assert por_nombre['Alanis']['por_cobrar'] == FIADO
    assert por_nombre['Paz']['total_spent'] == COBRADO_REAL
    assert por_nombre['Paz']['por_cobrar'] == 0


def test_el_detalle_del_cliente_separa_pagado_de_deuda():
    base = _base_con_los_dos()
    r = _con_base(base, lambda: server.admin_customer_detail('u-alanis',
                                                             admin={'email': 'a@b.c'}))
    assert r['paid_total'] == 0
    assert r['paid_count'] == 0
    assert r['por_cobrar'] == FIADO
    assert r['orders'][0]['pagado'] is False


# ------------------------------------------------------------- distribuidores
DIST = {'id': 'd-1', 'name': 'Vendedor', 'email': 'v@ejemplo.com', 'role': 'distributor',
        'tier': 'senior', 'distributor_code': 'VEND-1', 'commission_rate': 0.30}


def _venta_de_dist(base_order, comision):
    return {**base_order, 'referred_by': DIST['id'], 'commission': comision,
            'commissions': [{'distributor_id': DIST['id'], 'role': 'seller',
                             'amount': comision}]}


def test_una_venta_fiada_no_paga_comision_al_distribuidor():
    """⛔ LA REGLA DEL DINERO: la comisión sale del COBRO, no de la entrega. Si no,
    la casa paga con dinero que todavía no tiene."""
    cobrada = _venta_de_dist(PAZ, 1000.0)
    fiada = _venta_de_dist(ALANIS, 1157.0)
    assert pyramid.earnings_for(DIST['id'], [cobrada, fiada]) == 1000.0
    assert server._my_amount(fiada, DIST['id']) == 0
    assert server._my_amount(cobrada, DIST['id']) == 1000.0


def test_el_tablero_del_distribuidor_separa_ventas_de_deuda():
    base = _Base(orders=[_venta_de_dist(PAZ, 1000.0), _venta_de_dist(ALANIS, 1157.0)],
                 users=[DIST,
                        {'id': 'u-paz', 'name': 'Paz', 'role': 'user', 'referred_by': DIST['id']},
                        {'id': 'u-alanis', 'name': 'Alanis', 'role': 'user',
                         'referred_by': DIST['id']}])
    r = _con_base(base, lambda: server.distributor_summary(dist=DIST))
    assert r['sales_total'] == COBRADO_REAL
    assert r['por_cobrar'] == FIADO
    assert r['earnings_total'] == 1000.0
    # Y el nivel no se gana entregando: `team_sales` también cuenta sólo lo cobrado.
    assert r['team_sales'] == COBRADO_REAL


def test_la_ficha_admin_del_distribuidor_no_infla_sus_ventas():
    orders = [_venta_de_dist(PAZ, 1000.0), _venta_de_dist(ALANIS, 1157.0)]
    users = [DIST]
    roll = server._distributor_rollup(DIST, users, orders)
    assert roll['sales_total'] == COBRADO_REAL
    assert roll['por_cobrar'] == FIADO
    assert roll['earnings'] == 1000.0
    # Los dos pedidos siguen siendo dos ventas hechas: sólo el dinero cambia.
    assert roll['sales_count'] == 2


# --------------------------------------------------------------- marketing
def _con_utm(pedido):
    return {**pedido, 'first_order': True,
            'attribution': {'utm_source': 'facebook', 'utm_campaign': 'Verano'}}


def test_el_roas_de_una_campana_no_se_infla_con_ventas_fiadas():
    filas = [{'campaign': 'Verano', 'spend': 100.0, 'currency': 'USD', 'link_clicks': 200}]
    r = marketing.cruzar(filas, [_con_utm(PAZ), _con_utm(ALANIS)])
    fila = r['campanas'][0]
    assert fila['ingreso_mxn'] == round(COBRADO_REAL)
    assert fila['por_cobrar_mxn'] == round(FIADO)
    # Los dos pedidos y los dos clientes nuevos siguen contando: el anuncio funcionó.
    assert fila['pedidos'] == 2 and fila['clientes_nuevos'] == 2


def test_el_canal_de_distribuidores_no_cuenta_comision_de_lo_que_no_se_cobro():
    pedidos = [_venta_de_dist(PAZ, 1000.0), _venta_de_dist(ALANIS, 1157.0)]
    r = marketing.canales(pedidos, gasto_meta=0.0)
    d = r['distribuidores']
    assert d['ingreso_mxn'] == round(COBRADO_REAL)
    assert d['por_cobrar_mxn'] == round(FIADO)
    assert d['comisiones_mxn'] == 1000


def test_el_director_solo_aprende_de_ventas_cobradas():
    b = director.briefing(campanas=[], pedidos=[PAZ, ALANIS], productos=[])
    assert b['ventas']['ingreso'] == round(COBRADO_REAL)
    assert b['ventas']['ticket_promedio'] == round(COBRADO_REAL)
    vendidos = [p['nombre'] for p in b['ventas']['mas_vendidos']]
    assert 'Retatrutida 40 mg' not in vendidos


# ----------------------------------------------------- los puntos siguen al dinero
def test_una_entrega_fiada_no_regala_puntos():
    """1 punto = 1 peso. Depositar puntos por una venta que no se ha cobrado es
    regalar dinero de la casa dos veces."""
    fiado = {**ALANIS, 'points_earned': 100, 'points_awarded': False}
    base = _Base(orders=[fiado],
                 users=[{'id': 'u-alanis', 'name': 'Alanis', 'points_balance': 0}])
    _con_base(base, lambda: server.award_order_points(fiado))
    assert base.users.docs[0]['points_balance'] == 0
    assert base.orders.docs[0].get('points_awarded') is not True


def test_al_marcar_el_pago_los_puntos_se_depositan():
    fiado = {**ALANIS, 'points_earned': 100, 'points_awarded': False}
    base = _Base(orders=[fiado],
                 users=[{'id': 'u-alanis', 'name': 'Alanis', 'points_balance': 0}])
    _con_base(base, lambda: server.admin_marcar_pago(
        'o-alanis', server.MarcaDePago(pagado=True), admin={'email': 'a@b.c'}))
    assert base.orders.docs[0]['paid'] is True
    assert base.orders.docs[0]['status'] == 'entregado', 'marcar el pago movió la entrega'
    assert base.users.docs[0]['points_balance'] == 100


def test_desmarcar_el_pago_retira_los_puntos_pero_no_devuelve_los_canjeados():
    """⛔ Usar `revoke_order_points` aquí le devolvería al cliente los puntos que YA
    gastó en este pedido, con el pedido todavía en pie: los podría gastar dos veces."""
    pedido = {**ALANIS, 'paid': True, 'points_earned': 100, 'points_awarded': True,
              'points_used': 500}
    base = _Base(orders=[pedido],
                 users=[{'id': 'u-alanis', 'name': 'Alanis', 'points_balance': 100}])
    _con_base(base, lambda: server.admin_marcar_pago(
        'o-alanis', server.MarcaDePago(pagado=False), admin={'email': 'a@b.c'}))
    assert base.users.docs[0]['points_balance'] == 0        # se retiran los 100 ganados
    assert base.orders.docs[0].get('points_refunded') is not True   # NO devuelve los 500


# ------------------------- el espejo: que un cobro REAL sí cuente como ingreso
def test_una_pasarela_que_cobra_de_verdad_marca_el_pedido_como_pagado():
    """⛔ EL BUG ESPEJO. Los pedidos nacen con `paid: False` (default del modelo), así
    que si la pasarela sólo escribe `paid_at` una tarjeta REALMENTE COBRADA nunca
    contaría como ingreso — el tablero se iría a cero por el otro lado."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def _confirm_paid_order(')[1].split('\n@api_router')[0]
    assert "'paid': True" in cuerpo, 'la pasarela confirma el pedido sin marcarlo pagado'


def test_confirmar_el_pedido_a_mano_lo_marca_pagado_y_enviarlo_no():
    """'confirmado' es el paso del dinero (es lo que Christián marca cuando ve el SPEI
    en el banco). Mover la mercancía no cobra nada."""
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def update_order_status(')[1].split('\n@api_router')[0]
    rama = cuerpo.split("if payload.status == 'confirmado':")[1].split('await db.orders')[0]
    assert "update['paid'] = True" in rama
    assert "'entregado'" not in rama, 'entregar está marcando el pedido como pagado'


def test_la_venta_directa_nace_pagada_pero_se_puede_registrar_fiada():
    """La venta directa normal es de mano en mano. El interruptor existe para el caso
    de Alanís; sin él, TODA venta directa habría dejado de contar como ingreso."""
    assert server.ManualOrderCreate(user_id='u', items=[]).pagado is True
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def admin_create_order(')[1].split('\n@api_router')[0]
    assert 'paid=payload.pagado' in cuerpo, 'la venta directa ignora si ya le pagaron'
