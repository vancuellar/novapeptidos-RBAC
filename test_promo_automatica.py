"""LA PROMO AUTOMÁTICA COMO PISO — la regla del 2026-08-01, probada.

Orden de Christián: si el distribuidor NO pone descuento propio, al cliente le
aplican los automáticos de la casa (10%, 15% por volumen y el envío gratis por
umbral) igual que si llegara solo al sitio. Un cliente recomendado no puede
quedar PEOR que un anónimo.

Tres puertas, tres pruebas:
  · `promo_automatica` — el número, solo.
  · `_armar_cotizacion` — el carrito compartido, el correo y el reenvío, que
    cotizan con esa función. Al 0% pedido, la promo entra sola.
  · `create_order` — la caja. Se lee el texto de la función (mismo recurso que
    `test_regla_de_5.py`): la promo como piso del código, y el carrito
    compartido cobrando el descuento que se COTIZÓ (`discount_asked`), no el
    del código `ref` — que era el hoyo por el que un carrito al 20% se cobraba
    al 10%.
"""
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


class _Renglon:
    def __init__(self, product_id, quantity=1):
        self.product_id = product_id
        self.quantity = quantity


CATALOGO = {
    # Aguanta 40%: recibe la promo completa.
    'p-reta': {'id': 'p-reta', 'name': 'Retatrutida 20 mg', 'price': 3000,
               'category': 'metabolicos', 'commission_cap': 0.40,
               'distributor_eligible': True},
    # Insumo: nunca lleva descuento, ni con la promo.
    'p-agua': {'id': 'p-agua', 'name': 'Agua bacteriostática 30 mL', 'price': 500,
               'category': 'accesorios', 'commission_cap': 0.40,
               'distributor_eligible': True},
}


# --------------------------------------------------------------- el número
def test_la_promo_es_10_abajo_del_umbral():
    assert server.promo_automatica(0) == 0.10
    assert server.promo_automatica(34999) == 0.10


def test_la_promo_es_15_desde_el_umbral():
    assert server.promo_automatica(server.UMBRAL_PROMO_15) == 0.15
    assert server.promo_automatica(100000) == 0.15


# ------------------------------------------- el cotizador y el carrito compartido
def test_cotizar_al_cero_aplica_la_promo_del_10():
    lineas, lista, total = server._armar_cotizacion(
        [_Renglon('p-reta', 2)], 0.0, 0.40, CATALOGO)
    assert lista == 6000
    assert total == 5400          # 10% de la casa, sin que nadie lo pidiera


def test_cotizar_al_cero_con_volumen_aplica_el_15():
    lineas, lista, total = server._armar_cotizacion(
        [_Renglon('p-reta', 12)], 0.0, 0.40, CATALOGO)
    assert lista == 36000         # arriba del umbral de $35,000
    assert total == 30600         # 15%


def test_el_insumo_no_recibe_promo():
    lineas, lista, total = server._armar_cotizacion(
        [_Renglon('p-reta', 1), _Renglon('p-agua', 1)], 0.0, 0.40, CATALOGO)
    agua = next(ln for ln in lineas if ln['product_id'] == 'p-agua')
    assert agua['unit_price'] == 500    # el tope por producto sigue mandando


def test_el_insumo_no_infla_el_umbral_de_la_promo():
    # $34,500 de mercancía descuentable + $1,000 de insumos NO llegan al 15%:
    # el umbral se mide sobre lo descuentable, igual que en la caja.
    items = [_Renglon('p-reta', 11), _Renglon('p-agua', 4)]   # 33,000 + 2,000
    lineas, lista, total = server._armar_cotizacion(items, 0.0, 0.40, CATALOGO)
    reta = next(ln for ln in lineas if ln['product_id'] == 'p-reta')
    assert reta['unit_price'] == 2700   # 10%, no 15%


def test_un_descuento_propio_se_respeta_tal_cual():
    lineas, lista, total = server._armar_cotizacion(
        [_Renglon('p-reta', 1)], 0.20, 0.40, CATALOGO)
    assert total == 2400          # el 20% que pidió, ni promo ni recorte


def test_la_promo_no_se_recorta_con_el_maximo_del_distribuidor():
    # Aunque el tope del distribuidor fuera menor que la promo (caso raro), la
    # promo es de la casa: el cliente la recibe igual.
    lineas, lista, total = server._armar_cotizacion(
        [_Renglon('p-reta', 1)], 0.0, 0.05, CATALOGO)
    assert total == 2700          # 10%


# ------------------------------------------------------------------- la caja
def _cuerpo_de_create_order():
    with open(server.__file__, encoding='utf-8') as fh:
        src = fh.read()
    return src.split('async def create_order(')[1].split('\n@api_router')[0]


def test_la_caja_usa_la_promo_como_piso_del_codigo():
    cuerpo = _cuerpo_de_create_order()
    assert 'max(code_discount, promo_automatica(discountable))' in cuerpo


def test_la_caja_cobra_el_descuento_que_se_cotizo_en_el_carrito():
    cuerpo = _cuerpo_de_create_order()
    assert "discount_asked" in cuerpo, \
        'el carrito compartido ya no cobra lo que se cotizó'
    assert 'promo_automatica(discountable)\n' in cuerpo or \
           'else promo_automatica(discountable))' in cuerpo


def test_la_promo_anonima_sigue_siendo_la_misma():
    cuerpo = _cuerpo_de_create_order()
    assert 'discount_rate = promo_automatica(discountable)' in cuerpo
