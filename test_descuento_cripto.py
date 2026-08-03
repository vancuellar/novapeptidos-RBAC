"""EL 5% POR PAGAR EN CRIPTO — Christián, 2026-08-03.

Lo financia la comisión de Mercado Pago que el pedido NO paga (4.05% + $4.64 con
IVA), así que cuesta de verdad menos del 1%. Por eso no cuenta contra el techo del
40% de descuento comercial: son dos dineros de origen distinto.

Lo que más cuidan estas pruebas es que el anuncio y el cobro digan lo mismo. Un
banner que promete 5% y una caja que cobra el precio entero es publicidad engañosa,
y con la LFPC eso no es un detalle.
"""
import os
import re

import descuento_cripto as C


# --------------------------------------------------------------- la regla, sola
def test_la_tasa_es_5_por_ciento():
    assert C.TASA == 0.05
    assert C.METODO == 'cripto'


def test_solo_aplica_a_cripto():
    assert C.aplica('cripto') is True
    for otro in ('tarjeta', 'spei', 'oxxo', '', None, 'CRIPTOMONEDA'):
        assert C.aplica(otro) is False, otro


def test_el_metodo_se_lee_sin_importar_mayusculas_ni_espacios():
    for escrito in ('CRIPTO', ' cripto ', 'Cripto'):
        assert C.aplica(escrito) is True, escrito


def test_descuenta_el_5_por_ciento():
    assert C.descuento(10000, 'cripto') == 500
    assert C.descuento(2129, 'cripto') == 106      # 106.45 -> 106


def test_no_descuenta_nada_con_otro_metodo():
    for otro in ('tarjeta', 'spei', 'oxxo', None):
        assert C.descuento(10000, otro) == 0


def test_se_calcula_sobre_la_mercancia_YA_DESCONTADA():
    """⛔ Si entrara el subtotal en vez de la mercancía ya descontada, el 5% se
    calcularía sobre un precio que nadie va a pagar y la casa regalaría de más en
    cada pedido con descuento. Con 40% de descuento comercial sobre $10,000, la
    base son $6,000 y el 5% son $300, no $500."""
    assert C.descuento(6000, 'cripto') == 300


def test_cantidades_raras_no_tumban_el_calculo():
    for basura in (None, 0, -500, ''):
        assert C.descuento(basura, 'cripto') == 0


# ------------------------------------------------ el anuncio y el cobro coinciden
def test_lo_que_se_anuncia_es_lo_que_se_cobra():
    """La razón de ser de `texto_del_ahorro`: la promesa del banner y el cobro de la
    caja salen de la misma cuenta. Si algún día se separan, se separan en el módulo
    y no en una plantilla que nadie revisa."""
    for base in (899, 2500, 10000, 47321):
        assert C.texto_del_ahorro(base) == C.descuento(base, 'cripto')


# ------------------------------------------------------- la fuga, si la hubiera
def test_detecta_el_pedido_que_prometio_cripto_y_pago_de_otro_modo():
    v = C.revisar_al_cobrar('cripto', 'spei', 500)
    assert v['coincide'] is False and v['fuga_mxn'] == 500


def test_no_hay_fuga_cuando_el_metodo_coincide():
    assert C.revisar_al_cobrar('cripto', 'cripto', 500)['fuga_mxn'] == 0
    assert C.revisar_al_cobrar('tarjeta', 'tarjeta', 0)['coincide'] is True


def test_pagar_en_cripto_sin_haberlo_prometido_no_es_fuga():
    """Al revés no cuesta dinero: pagó por donde a la casa le conviene y no se le
    dio descuento. Se reporta como no-coincidencia, pero la fuga es cero."""
    v = C.revisar_al_cobrar('tarjeta', 'cripto', 0)
    assert v['coincide'] is False and v['fuga_mxn'] == 0


# ------------------------------------------------------- cómo quedó en el checkout
def _server():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.py')
    return open(ruta, encoding='utf-8').read()


def test_el_checkout_usa_el_modulo_y_no_una_copia():
    """El error que se paga caro: la regla vive en un lado y el dinero se calcula en
    otro. Si alguien escribe `* 0.05` a mano en el checkout, esto se pone rojo."""
    s = _server()
    assert 'descuento_cripto.descuento(' in s, 'el checkout no llama al módulo'
    cuerpo = s.split('async def create_order(')[1].split('\n@api_router')[0]
    assert not re.search(r'0\.05\s*\*|\*\s*0\.05', cuerpo), (
        'hay un 5% escrito a mano en create_order: usa descuento_cripto')


def test_el_descuento_de_cripto_NO_toca_el_envio():
    """La guía se le paga completa a la paquetería, cobre lo que cobre la pasarela.
    En el checkout el descuento se resta de `paid_merchandise` y sólo DESPUÉS se le
    suma `shipping`, así que el envío nunca entra en la base del 5%."""
    s = _server()
    i = s.index('crypto_discount = descuento_cripto.descuento(')
    trozo = s[i:i + 400]
    assert 'paid_merchandise - crypto_discount' in trozo
    assert trozo.index('paid_merchandise - crypto_discount') < trozo.index('total = paid_merchandise + shipping')


def test_se_guarda_aparte_del_descuento_comercial():
    """Mezclarlos haría imposible saber cuánto costó de verdad la promoción."""
    s = _server()
    assert 'crypto_discount=crypto_discount,' in s
    modelos = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'models.py'), encoding='utf-8').read()
    assert 'crypto_discount: float = 0' in modelos
