"""Pruebas de la Conversions API de Meta (meta_capi.py).

Lo que se cuida aquí:
  - Que NADA personal salga en claro (todo hasheado en SHA-256).
  - Que el `event_id` sea el mismo que manda el navegador — si no, cada compra
    contaría DOBLE y el ROAS saldría inflado al doble.
  - Que sin token no truene ni mande nada.
  - Que un pedido pagado por WhatsApp (sin fbclid) igual se pueda mandar.
"""
import hashlib
import os

import pytest

import meta_capi


PEDIDO = {
    'order_number': 'EX-20260730-2906',
    'total': 3499.5,
    'paid_at': '2026-07-30T18:22:05.000Z',
    'customer': {
        'full_name': 'Juan Pérez López',
        'email': '  Juan.Perez@Gmail.COM ',
        'phone': '(999) 123-4567',
        'city': 'Mérida',
        'state': 'Yucatán',
        'postal_code': '97000-123',
        'country': 'MX',
    },
    'attribution': {'visitor_id': 'v-abc123', 'fbclid': ''},
    'items': [
        {'product_id': 'reta-30', 'name': 'Retatrutida 30mg', 'price': 2499.5, 'quantity': 1},
        {'product_id': 'bac-water', 'name': 'Agua bacteriostática', 'price': 500.0, 'quantity': 2},
    ],
}


def _sha(v):
    return hashlib.sha256(v.encode('utf-8')).hexdigest()


# ------------------------------------------------------- normalización
def test_email_se_normaliza_antes_de_hashear():
    """Meta empareja por el hash: 'Juan.Perez@Gmail.COM ' y 'juan.perez@gmail.com'
    tienen que dar el MISMO hash o el cliente no se reconoce."""
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['user_data']['em'] == _sha('juan.perez@gmail.com')


def test_telefono_mexicano_lleva_lada_pais():
    """Sin el 52 delante, Meta no empareja a nadie."""
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['user_data']['ph'] == _sha('529991234567')


def test_codigo_postal_solo_cinco_digitos():
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['user_data']['zp'] == _sha('97000')


def test_nombre_y_apellido_se_parten():
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['user_data']['fn'] == _sha('juan')
    assert ev['user_data']['ln'] == _sha('pérezlópez')


def test_nada_personal_viaja_en_claro():
    """El candado que importa: ni un correo, ni un teléfono, ni un nombre legible."""
    crudo = str(meta_capi.construir_evento(PEDIDO))
    for secreto in ('juan.perez@gmail.com', 'Juan.Perez', '9991234567', 'Pérez', 'Mérida'):
        assert secreto.lower() not in crudo.lower()


def test_campos_vacios_no_se_mandan():
    """Meta penaliza los campos presentes pero vacíos."""
    pedido = {**PEDIDO, 'customer': {'full_name': '', 'email': 'a@b.com', 'phone': ''}}
    ud = meta_capi.construir_evento(pedido)['data'][0]['user_data']
    assert 'ph' not in ud and 'fn' not in ud
    assert ud['em'] == _sha('a@b.com')


# ------------------------------------------------------- deduplicación
def test_event_id_es_el_del_navegador():
    """track.js manda `purchase-<número>` como eventID. Si estos dos dejan de
    coincidir, cada venta cuenta DOS veces."""
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['event_id'] == 'purchase-EX-20260730-2906'
    assert meta_capi.event_id('EX-20260730-2906') == 'purchase-EX-20260730-2906'


# ------------------------------------------------------- contenido
def test_monto_moneda_y_articulos():
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['custom_data']['value'] == 3499.5
    assert ev['custom_data']['currency'] == 'MXN'
    assert ev['custom_data']['order_id'] == 'EX-20260730-2906'
    assert ev['custom_data']['num_items'] == 3
    assert ev['custom_data']['content_ids'] == ['reta-30', 'bac-water']
    assert ev['event_name'] == 'Purchase'


def test_hora_es_la_del_pago_no_la_de_hoy():
    """Meta rechaza eventos viejos y atribuye por fecha: si mandamos 'ahora',
    la compra se le carga al día equivocado."""
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert ev['event_time'] == 1785435725      # 2026-07-30T18:22:05Z


def test_sin_fbclid_igual_se_manda():
    """El caso de WhatsApp: no hay cookie ni fbclid, pero el correo y el teléfono
    hasheados alcanzan para que Meta empareje."""
    ev = meta_capi.construir_evento(PEDIDO)['data'][0]
    assert 'fbc' not in ev['user_data']
    assert ev['user_data']['em']


def test_con_fbclid_se_reconstruye_el_fbc():
    pedido = {**PEDIDO, 'attribution': {'fbclid': 'IwAR123', 'visitor_id': 'v-1'}}
    ev = meta_capi.construir_evento(pedido)['data'][0]
    assert ev['user_data']['fbc'] == 'fb.1.1785435725000.IwAR123'


# ------------------------------------------------------- casos borde
@pytest.mark.parametrize('pedido', [
    {},
    {'order_number': 'EX-1', 'total': 0},
    {'total': 100},
])
def test_pedido_incompleto_no_produce_evento(pedido):
    """Un Purchase sin monto o sin número le enseña basura a Meta."""
    assert meta_capi.construir_evento(pedido) is None


def test_test_event_code_solo_si_esta_puesto():
    assert 'test_event_code' not in meta_capi.construir_evento(PEDIDO)
    cuerpo = meta_capi.construir_evento(PEDIDO, test='TEST12345')
    assert cuerpo['test_event_code'] == 'TEST12345'


# ------------------------------------------------------- el envío
def test_sin_token_no_manda_y_no_truena(monkeypatch):
    """Cobrar es lo importante; medir va después y jamás debe tumbar un webhook."""
    monkeypatch.delenv('META_CAPI_TOKEN', raising=False)
    monkeypatch.delenv('META_TOKEN', raising=False)
    import asyncio
    r = asyncio.run(meta_capi.enviar_compra(PEDIDO))
    assert r == {'enviado': False, 'motivo': 'sin token'}


def test_token_del_panel_es_el_plan_b(monkeypatch):
    monkeypatch.delenv('META_CAPI_TOKEN', raising=False)
    monkeypatch.setenv('META_TOKEN', 'el-del-panel')
    assert meta_capi.token() == 'el-del-panel'
    monkeypatch.setenv('META_CAPI_TOKEN', 'el-de-capi')
    assert meta_capi.token() == 'el-de-capi'


def test_pixel_por_defecto_es_el_del_sitio(monkeypatch):
    monkeypatch.delenv('META_PIXEL_ID', raising=False)
    assert meta_capi.pixel_id() == '2487053198462294'


def test_pedido_incompleto_no_sale_a_internet(monkeypatch):
    """Si el pedido no sirve, ni se intenta la llamada."""
    monkeypatch.setenv('META_CAPI_TOKEN', 'x')
    import asyncio
    r = asyncio.run(meta_capi.enviar_compra({'order_number': '', 'total': 0}))
    assert r['motivo'] == 'pedido incompleto'
