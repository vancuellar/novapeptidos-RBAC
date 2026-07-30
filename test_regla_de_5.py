"""LA REGLA DE 5 y la puerta anónima — Christián, 2026-07-30.

Consumo propio de distribuidores: el precio de distribuidor sólo baja en los
renglones con 5 o más piezas DEL MISMO PRODUCTO; de 1 a 4 se paga precio de
cliente. Y quien compra deslogueado con su propio código ya no cobra comisión ni
crédito de nivel por comprarse a sí mismo.

Estas pruebas corren la aritmética DE VERDAD (descuentos.py es puro a propósito),
no leen el texto de `create_order`. Lo que sí se revisa por lectura es que el
checkout use estas funciones y no una copia — que es el error que se paga caro:
la regla vive en un lado y el dinero se calcula en otro.
"""
import inspect
import os
from types import SimpleNamespace

# database.py exige MONGO_URL al importar; el cliente de motor es perezoso, así que
# nunca se conecta a nada. Va aquí y no prestado de otro archivo de pruebas: correr
# ESTE solo tiene que funcionar.
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')

import descuentos    # noqa: E402
import pyramid       # noqa: E402
import server        # noqa: E402


CLIENTE = 0.10          # precio de cliente: la promo automática de siempre
DIST = 0.30             # precio de distribuidor con la base nueva del canal


def renglon(pid, price, qty, name=None):
    return SimpleNamespace(product_id=pid, name=name or pid, price=price, quantity=qty)


def _reparte(items, tasas, tasa_base, topes=None):
    """Atajo: reparte con tope 50% para todo salvo lo que diga `topes`."""
    topes = topes or {}
    return descuentos.repartir(
        items, lambda it: it.product_id,
        lambda it: topes.get(it.product_id, 0.50), tasas, tasa_base)


# ---------------------------------------------------------------- la regla, sola
def test_cinco_piezas_del_mismo_producto_pagan_precio_de_distribuidor():
    assert descuentos.tasa_del_renglon(5, CLIENTE, DIST, True) == DIST


def test_menos_de_cinco_paga_precio_de_cliente():
    for piezas in (1, 2, 3, 4):
        assert descuentos.tasa_del_renglon(piezas, CLIENTE, DIST, True) == CLIENTE


def test_la_frontera_esta_en_cinco_exactamente():
    """Cuatro no, cinco sí. El mínimo se lee del módulo, no de un número suelto."""
    assert descuentos.MINIMO_PARA_PRECIO_DISTRIBUIDOR == 5
    n = descuentos.MINIMO_PARA_PRECIO_DISTRIBUIDOR
    assert descuentos.tasa_del_renglon(n - 1, CLIENTE, DIST, True) == CLIENTE
    assert descuentos.tasa_del_renglon(n, CLIENTE, DIST, True) == DIST
    assert descuentos.tasa_del_renglon(n + 40, CLIENTE, DIST, True) == DIST


def test_una_venta_normal_no_conoce_la_regla_de_5():
    """A un CLIENTE no se le pide mínimo: su descuento es parejo, como siempre."""
    for piezas in (1, 4, 5, 50):
        assert descuentos.tasa_del_renglon(piezas, CLIENTE, 0.0, False) == CLIENTE


def test_la_regla_nunca_deja_por_debajo_del_precio_de_cliente():
    """Quita el precio de MAYOREO, no el descuento que cualquiera tendría. Con un
    cupón del 20% en la mano, el renglón de una pieza paga 20%, no 10%."""
    assert descuentos.tasa_del_renglon(1, 0.20, DIST, True) == 0.20
    # Y si el cupón es mejor que el precio de distribuidor, gana el cupón.
    assert descuentos.tasa_del_renglon(9, 0.45, DIST, True) == 0.45


def test_cantidades_raras_no_tumban_el_calculo():
    assert descuentos.tasa_del_renglon(None, CLIENTE, DIST, True) == CLIENTE
    assert descuentos.tasa_del_renglon('x', CLIENTE, DIST, True) == CLIENTE
    assert descuentos.tasa_del_renglon(5, None, DIST, True) == DIST


# ------------------------------------------------------- es POR PRODUCTO, no por carrito
def test_es_por_producto_no_por_carrito():
    """Cinco piezas SURTIDAS no dan precio de distribuidor: son cinco del MISMO."""
    tasas = descuentos.tasas_por_producto(
        {'reta-20': 2, 'bpc-10': 2, 'tb-500': 1}, CLIENTE, DIST, True)
    assert set(tasas.values()) == {CLIENTE}


def test_un_carrito_mixto_lleva_dos_tasas_a_la_vez():
    tasas = descuentos.tasas_por_producto(
        {'reta-20': 5, 'bpc-10': 3}, CLIENTE, DIST, True)
    assert tasas == {'reta-20': DIST, 'bpc-10': CLIENTE}


def test_el_mismo_producto_en_dos_renglones_suma():
    """Tres y dos del mismo vial son CINCO. El agrupado ya llega sumado desde
    `_agrupar_por_producto`; aquí se comprueba que la regla lo respeta."""
    grupos = {'reta-20': {'total': 3 + 2, 'nombre': 'Retatrutida 20 mg'}}
    tasas = descuentos.tasas_por_producto(
        {k: g['total'] for k, g in grupos.items()}, CLIENTE, DIST, True)
    assert tasas['reta-20'] == DIST
    assert descuentos.faltantes_para_precio_distribuidor(grupos) == []


# ------------------------------------------------------------------- el dinero
def test_carrito_mixto_cobra_cada_renglon_con_su_tasa():
    items = [renglon('reta-20', 1000, 5), renglon('bpc-10', 500, 3)]
    tasas = {'reta-20': DIST, 'bpc-10': CLIENTE}
    dinero, tasa, capados, lineas = _reparte(items, tasas, CLIENTE)
    assert dinero == round(1000 * 5 * DIST) + round(500 * 3 * CLIENTE)   # 1500 + 150
    assert tasa == DIST                    # la mayor concedida, la que se guarda
    assert capados == []
    assert [(l['product_id'], l['applied_rate']) for l in lineas] == [
        ('reta-20', DIST), ('bpc-10', CLIENTE)]


def test_el_tope_del_producto_sigue_mandando_sobre_la_regla_de_5():
    """Cinco piezas dan precio de distribuidor, pero si el producto sólo aguanta
    20% se recorta a 20%. El ROI de la casa manda sobre todo lo demás."""
    items = [renglon('flaco', 1000, 5)]
    dinero, tasa, capados, lineas = _reparte(
        items, {'flaco': DIST}, CLIENTE, topes={'flaco': 0.20})
    assert dinero == 1000 * 5 * 0.20
    assert capados[0]['applied_rate'] == 0.20 and capados[0]['asked_rate'] == DIST
    assert tasa == DIST     # se PIDIÓ 30 aunque se diera 20: los puntos leen lo pedido


def test_los_insumos_siguen_sin_descuento_aunque_lleve_cincuenta():
    items = [renglon('agua', 80, 50)]
    dinero, _, capados, _ = _reparte(items, {'agua': DIST}, CLIENTE, topes={'agua': 0.0})
    assert dinero == 0
    assert capados[0]['applied_rate'] == 0.0


def test_un_carrito_parejo_se_comporta_igual_que_antes_de_la_regla():
    """Regresión: sin compra propia, todo el carrito lleva la misma tasa y
    `discount_rate` vale lo mismo que valía antes. Nada de lo viejo se movió."""
    items = [renglon('a', 1000, 1), renglon('b', 2000, 7)]
    tasas = descuentos.tasas_por_producto({'a': 1, 'b': 7}, CLIENTE, 0.0, False)
    dinero, tasa, capados, _ = _reparte(items, tasas, CLIENTE)
    assert tasa == CLIENTE
    assert dinero == round(1000 * CLIENTE) + round(2000 * 7 * CLIENTE)
    assert capados == []


def test_un_carrito_vacio_no_revienta():
    assert descuentos.repartir([], lambda it: '', lambda it: 0.5, {}, CLIENTE) == (
        0, CLIENTE, [], [])


# ------------------------------------------------------------- el aviso del carrito
def test_avisa_cuantas_piezas_faltan():
    grupos = {'reta-20': {'total': 3, 'nombre': 'Retatrutida 20 mg'}}
    aviso = descuentos.faltantes_para_precio_distribuidor(grupos)
    assert aviso == [{'product_id': 'reta-20', 'name': 'Retatrutida 20 mg',
                      'quantity': 3, 'faltan': 2, 'minimo': 5}]


def test_no_avisa_de_lo_que_ya_llego_a_cinco():
    grupos = {'a': {'total': 5, 'nombre': 'A'}, 'b': {'total': 12, 'nombre': 'B'}}
    assert descuentos.faltantes_para_precio_distribuidor(grupos) == []


def test_el_aviso_pone_primero_al_que_esta_mas_cerca():
    grupos = {'lejos': {'total': 1, 'nombre': 'Lejos'},
              'cerca': {'total': 4, 'nombre': 'Cerca'}}
    aviso = descuentos.faltantes_para_precio_distribuidor(grupos)
    assert [a['product_id'] for a in aviso] == ['cerca', 'lejos']


# --------------------------------------------------------------- la puerta anónima
DIST_DOC = {'id': 'd1', 'role': 'distributor', 'email': 'Maria@Ejemplo.com',
            'tier': 'junior0'}


def test_la_puerta_anonima_se_cierra_cuando_el_correo_es_el_suyo():
    """Sin sesión, con SU código y SU correo: es compra propia, no una venta."""
    assert descuentos.motivo_de_compra_propia(None, DIST_DOC, 'maria@ejemplo.com') == 'correo'
    # Y no le importan mayúsculas ni espacios de más.
    assert descuentos.motivo_de_compra_propia(None, DIST_DOC, '  MARIA@EJEMPLO.COM ') == 'correo'


def test_una_venta_de_verdad_sigue_siendo_una_venta():
    """Su código, pero el correo es de OTRA persona: venta normal, con comisión."""
    assert descuentos.motivo_de_compra_propia(None, DIST_DOC, 'cliente@otro.com') == ''


def test_sin_correo_nunca_hay_compra_propia():
    """Un pedido sin correo no puede quitarle la comisión a nadie."""
    for vacio in ('', '   ', None):
        assert descuentos.motivo_de_compra_propia(None, DIST_DOC, vacio) == ''
    assert descuentos.motivo_de_compra_propia(None, {'id': 'x', 'email': ''}, '') == ''


def test_con_sesion_de_distribuidor_es_compra_propia_por_la_sesion():
    yo = {'id': 'd1', 'role': 'distributor'}
    assert descuentos.motivo_de_compra_propia(yo, None, 'quien@sea.com') == 'sesion'


def test_un_cliente_con_sesion_no_es_compra_propia():
    cliente = {'id': 'u1', 'role': 'user', 'email': 'cliente@otro.com'}
    assert descuentos.motivo_de_compra_propia(cliente, DIST_DOC, 'cliente@otro.com') == ''


# ------------------------------------------------- que el checkout use ESTO y no una copia
def _cuerpo_de_create_order():
    src = inspect.getsource(server)
    return src.split('async def create_order(')[1].split('\n@api_router')[0]


def test_el_checkout_aplica_la_regla_de_5_y_no_una_copia():
    cuerpo = _cuerpo_de_create_order()
    assert 'descuentos.tasas_por_producto(' in cuerpo, \
        'el checkout no aplica la regla de 5 por producto'
    assert 'descuentos.repartir(' in cuerpo, \
        'el checkout reparte el descuento por su cuenta: la regla y el dinero se van a separar'
    assert 'descuentos.motivo_de_compra_propia(' in cuerpo, \
        'la puerta anónima sigue abierta en el checkout'


def test_la_compra_propia_no_paga_comision_ni_da_credito_de_nivel():
    """`referrer` es lo único que atribuye la venta y paga comisión. En compra
    propia tiene que quedar en None ANTES de que se calculen las comisiones."""
    cuerpo = _cuerpo_de_create_order()
    corte = cuerpo.index('if referrer and not pagado_todo_con_puntos')
    antes = cuerpo[:corte]
    assert 'if compra_propia:\n        referrer = None' in antes, \
        'la compra propia se sigue atribuyendo (y pagando) a quien compró'


def test_la_tasa_del_renglon_es_la_que_limita_la_comision():
    """El tope por producto (descuento + comisión juntos) se calcula contra la tasa
    DE ESE RENGLÓN. Con la regla de 5 ya no hay una sola tasa para todo el carrito."""
    cuerpo = _cuerpo_de_create_order()
    assert '_disc_of(it, _pedida_de(it))' in cuerpo


# --------------------------------------------------- la base del canal, de punta a punta
def test_el_precio_de_distribuidor_sale_de_la_base_nueva():
    """La tasa de distribuidor que usa la regla de 5 es su tasa efectiva, y desde
    el 2026-07-30 la de entrada es 30%."""
    assert pyramid.effective_rate({'tier': 'junior0'}) == pyramid.BASE_RATE == 0.30
    assert descuentos.tasa_del_renglon(5, CLIENTE, pyramid.effective_rate(DIST_DOC), True) == 0.30


def test_una_manual_vieja_de_40_se_reancla_en_la_base():
    """Comprobación de la migración: bajarle la manual a 30 deja la efectiva en 30
    (y no la deja caer al 20 viejo del nivel)."""
    antes = {'tier': 'junior0', 'commission_rate': 0.40}
    despues = {'tier': 'junior0', 'commission_rate': pyramid.BASE_RATE}
    assert pyramid.effective_rate(antes) == 0.40
    assert pyramid.effective_rate(despues) == 0.30


def test_la_migracion_guarda_el_antes_para_poder_revertir():
    cuerpo = inspect.getsource(server.reanclar_comisiones_en_la_base)
    assert 'commission_rate_previo' in cuerpo, 'sin el valor anterior no hay marcha atrás'
    assert "db.migraciones" in cuerpo
    assert '$setOnInsert' in cuerpo, 'la migración se puede aplicar dos veces'
