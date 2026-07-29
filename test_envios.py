"""Pruebas del envío con Skydropx: cotizar, cobrar y comprar la guía.

Lo que cuidan, en orden de cuánto duele si se rompe:

  1. QUE EL PRECIO LO PONGA EL SERVIDOR. Un envío que el navegador manda no se
     cobra jamás, ni aunque venga en la petición. Es la regla más cara de esta
     casa: creerle un precio al navegador ya costó dinero (2026-07-27).
  2. Que solo se le enseñe ESTAFETA al cliente, aunque la API devuelva de todo.
  3. La regla del 10% y su tope.
  4. Que la guía se compre sola con los CUATRO métodos de pago.
  5. Que sin llave y sin remitente nada reviente — y que sin remitente NO se compre.

⛔ Nunca se llama a Skydropx de verdad: todas las respuestas son dobles de prueba.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import envios
import server
import skydropx
from models import CustomerInfo, OrderCreate, OrderItem


# ==========================================================================
#  Doble de la base: lo mínimo que usan las rutas de envío
# ==========================================================================
def _match(doc, filtro):
    for k, v in (filtro or {}).items():
        if k == '$or':
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        if isinstance(v, dict) and '$in' in v:
            if doc.get(k) not in v['$in']:
                return False
            continue
        if isinstance(v, dict) and '$ne' in v:
            if doc.get(k) == v['$ne']:
                return False
            continue
        if '.' in k:                      # 'opciones.opcion_id' → busca dentro de la lista
            campo, sub = k.split('.', 1)
            valores = doc.get(campo) or []
            if not any(isinstance(x, dict) and x.get(sub) == v for x in valores):
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class _Res:
    def __init__(self, n):
        self.modified_count = n
        self.matched_count = n


class FakeCol:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def find_one(self, filtro, proj=None):
        for d in self.docs:
            if _match(d, filtro):
                return {k: v for k, v in d.items() if k != '_id'}
        return None

    def find(self, filtro=None, proj=None):
        docs = [d for d in self.docs if _match(d, filtro or {})]

        class Cursor:
            async def to_list(self, n=None):
                return [dict(d) for d in docs]
        return Cursor()

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
    monkeypatch.setattr(server, 'db', fake)
    return fake


@pytest.fixture()
def con_llave(monkeypatch):
    """Skydropx encendido y el checkout cotizando."""
    monkeypatch.setenv('SKYDROPX_API_KEY', 'llave-de-prueba')
    monkeypatch.setattr(envios, 'COTIZAR_EN_CHECKOUT', True)
    return True


@pytest.fixture()
def con_remitente(monkeypatch):
    for k, v in {'NAME': 'Trabajador de Prueba', 'ADDRESS1': 'Calle 1 #2',
                 'CITY': 'Monterrey', 'PROVINCE': 'Nuevo León', 'ZIP': '64000'}.items():
        monkeypatch.setenv(f'SKYDROPX_FROM_{k}', v)
    return True


# Lo que devolvería Skydropx: varias paqueterías, a propósito.
TARIFAS_CRUDAS = [
    {'id': 'r-dhl', 'provider': 'DHL', 'service_level_name': 'Express',
     'service_level_code': 'dhl_exp', 'days': 1, 'total_pricing': '410.00', 'currency_local': 'MXN'},
    {'id': 'r-est-eco', 'provider': 'Estafeta', 'service_level_name': 'Terrestre',
     'service_level_code': 'est_ter', 'days': 4, 'total_pricing': '180.00', 'currency_local': 'MXN'},
    {'id': 'r-fedex', 'provider': 'FedEx', 'service_level_name': 'Día siguiente',
     'service_level_code': 'fx_next', 'days': 1, 'total_pricing': '520.00', 'currency_local': 'MXN'},
    {'id': 'r-est-dia', 'provider': 'ESTAFETA', 'service_level_name': 'Día Siguiente',
     'service_level_code': 'est_dia', 'days': 1, 'total_pricing': '320.00', 'currency_local': 'MXN'},
]


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)[:200]

    def json(self):
        return self._p


def _falsear_skydropx(monkeypatch, por_ruta):
    """Sustituye requests.post: NUNCA se toca el servicio real."""
    llamadas = []

    def fake_post(url, headers=None, json=None, timeout=None):
        ruta = '/' + url.rstrip('/').rsplit('/', 1)[-1]
        llamadas.append({'ruta': ruta, 'cuerpo': json, 'headers': headers})
        salida = por_ruta.get(ruta)
        if isinstance(salida, Exception):
            raise salida
        return FakeResp(salida)

    monkeypatch.setattr(skydropx.requests, 'post', fake_post)
    return llamadas


# ==========================================================================
#  1. El peso lo calcula el servidor
# ==========================================================================
def test_peso_usa_el_capturado_cuando_existe():
    assert envios.peso_de_pieza({'weight_kg': 0.42, 'name': 'BPC-157'}) == 0.42


def test_peso_por_omision_distingue_la_presentacion():
    """⚠️ Hasta que Christian capture los reales, el tipo de producto manda."""
    assert envios.peso_de_pieza({'name': 'Agua bacteriostática 30 ml'}) == envios.PESO_AGUA_KG
    assert envios.peso_de_pieza({'name': 'Jeringas de insulina'}) == envios.PESO_INSUMO_KG
    assert envios.peso_de_pieza({'name': 'Stack de recomposición'}) == envios.PESO_KIT_KG
    assert envios.peso_de_pieza({'name': 'BPC-157 10 mg'}) == envios.PESO_VIAL_KG


def test_peso_del_pedido_suma_piezas_empaque_y_respeta_el_minimo():
    items = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=2)]
    # 2 viales (0.10) + empaque (0.30) = 0.40 → la paquetería cobra 1 kg mínimo.
    assert envios.peso_del_pedido(items, {'a': {'name': 'BPC-157'}}) == envios.PESO_MINIMO_KG
    muchos = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=40)]
    assert envios.peso_del_pedido(muchos, {'a': {'name': 'BPC-157'}}) == 2.3   # 2.0 + 0.30


def test_peso_de_un_carrito_vacio_es_cero_y_no_revienta():
    assert envios.peso_del_pedido([], {}) == 0.0
    assert envios.peso_del_pedido(None, None) == 0.0


# ==========================================================================
#  2. Solo Estafeta
# ==========================================================================
def test_solo_se_le_enseña_estafeta_al_cliente(monkeypatch):
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    monkeypatch.setenv('SKYDROPX_API_KEY', 'k')
    opciones = skydropx.cotizar('64000', {'peso_kg': 1, 'alto_cm': 15, 'ancho_cm': 20, 'largo_cm': 30})
    assert [o['paqueteria'] for o in opciones] == ['Estafeta', 'ESTAFETA']   # DHL y FedEx fuera
    assert [o['precio'] for o in opciones] == [180.0, 320.0]                 # y de barato a caro


def test_la_lista_de_permitidas_se_amplia_con_un_renglon(monkeypatch):
    monkeypatch.setattr(skydropx, 'PAQUETERIAS_PERMITIDAS', ('estafeta', 'dhl'))
    assert skydropx.permitida('DHL') is True
    assert skydropx.permitida('FedEx') is False


def test_se_le_pide_a_la_api_solo_lo_permitido_y_ademas_se_filtra_al_recibir(monkeypatch):
    """Doble candado: se lo pedimos, y si nos manda de más igual lo tiramos.
    La lista de permitidas es NUESTRA regla, no un favor de la paquetería."""
    llamadas = _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    monkeypatch.setenv('SKYDROPX_API_KEY', 'k')
    skydropx.cotizar('64000', {'peso_kg': 1, 'alto_cm': 15, 'ancho_cm': 20, 'largo_cm': 30})
    assert llamadas[0]['cuerpo']['carriers'] == [{'name': 'estafeta'}]


def test_una_tarifa_sin_precio_no_es_una_opcion(monkeypatch):
    _falsear_skydropx(monkeypatch, {'/quotations': [
        {'id': 'x', 'provider': 'Estafeta', 'total_pricing': '0'}]})
    monkeypatch.setenv('SKYDROPX_API_KEY', 'k')
    assert skydropx.cotizar('64000', {'peso_kg': 1}) == []


# ==========================================================================
#  3. La regla del 10% y su tope
# ==========================================================================
def test_compra_chica_paga_su_envio_completo():
    # $879 de mercancía con $250 de envío: absorberlo se come el 28% del ingreso.
    assert envios.cobro_de_envio_al_cliente(250, 879, 2500) == 250


def test_compra_grande_con_envio_barato_va_gratis():
    # $3,000 y el envío cuesta $250 → es el 8.3%, cabe en el 10%: lo absorbe la casa.
    assert envios.cobro_de_envio_al_cliente(250, 3000, 2500) == 0


def test_el_tope_del_10_por_ciento_es_exacto():
    assert envios.cobro_de_envio_al_cliente(300, 3000, 2500) == 0      # justo el 10%
    assert envios.cobro_de_envio_al_cliente(301, 3000, 2500) > 0       # un peso más, ya no


def test_arriba_del_tope_hoy_paga_el_envio_completo():
    """⚠️ PENDIENTE DE CHRISTIAN. Hoy: el cliente paga los $600 enteros.
    Si algún día decide que solo pague el excedente, se cambia UNA línea en
    envios.py y esta prueba es la que lo dice."""
    assert envios.CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE is True
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500) == 600


def test_la_otra_definicion_se_prende_cambiando_una_sola_linea(monkeypatch):
    monkeypatch.setattr(envios, 'CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE', False)
    # $3,000 con envío de $600: la casa absorbe su 10% ($300), el cliente paga $300.
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500) == 300


def test_el_envio_se_mide_sobre_lo_que_el_cliente_PAGA():
    """Un pedido de lista $3,000 con 35% de descuento paga $1,950: NO va gratis.
    Medirlo sobre el precio de lista regalaría el envío cobrando mucho menos."""
    assert envios.cobro_de_envio_al_cliente(250, 1950, 2500) == 250


def test_la_regla_no_revienta_con_basura():
    assert envios.cobro_de_envio_al_cliente(None, None, 2500) == 0
    assert envios.cobro_de_envio_al_cliente('x', 'y', 2500) == 0
    assert envios.cobro_de_envio_al_cliente(-50, 5000, 2500) == 0


def test_el_10_por_ciento_vive_en_un_solo_lugar():
    """Escrito dos veces se desalinea en silencio — ya pasó el 2026-07-27."""
    assert server.TOPE_ENVIO_SOBRE_COMPRA is envios.TOPE_ENVIO_SOBRE_COMPRA


# ==========================================================================
#  4. ⛔ EL PRECIO LO PONE EL SERVIDOR
# ==========================================================================
def _pedido(cp='64000', quote_id=None, shipping_mentiroso=0):
    return OrderCreate(
        items=[OrderItem(product_id='a', name='BPC-157', price=1000, quantity=1)],
        customer=CustomerInfo(full_name='Ana', email='ana@x.com', phone='+528111111111',
                              address='Calle 1', postal_code=cp),
        payment_method='spei',
        shipping=shipping_mentiroso,
        shipping_quote_id=quote_id,
    )


PFLAGS = {'a': {'id': 'a', 'name': 'BPC-157'}}


def test_el_envio_que_manda_el_navegador_se_ignora(db, con_llave, monkeypatch):
    """El pedido dice que el envío cuesta $1. El servidor cobra los $180 que él
    mismo cotizó y guardó. Es la regla que ya costó dinero cuando no existía."""
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    payload = _pedido(shipping_mentiroso=1)
    cobrado, guardado = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 180                      # el precio real, no el del navegador
    assert guardado['cost'] == 180.0
    assert guardado['carrier'] == 'Estafeta'


def test_un_id_de_cotizacion_inventado_no_regala_el_envio(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    payload = _pedido(quote_id='me-lo-invente', shipping_mentiroso=0)
    cobrado, _ = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 180                      # recotiza; no cobra cero por creerle


def test_la_cotizacion_guardada_manda_sobre_la_recotizacion(db, con_llave, monkeypatch):
    """El cliente eligió el servicio de día siguiente ($320). Se le cobra ESO,
    no la más barata: eligió y el servidor respeta lo que él mismo le enseñó."""
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', envios.paquete_del_pedido(_pedido().items, PFLAGS),
        skydropx.cotizar('64000', {'peso_kg': 1})))
    dia_siguiente = next(o for o in quote['opciones'] if o['precio'] == 320.0)
    payload = _pedido(quote_id=dia_siguiente['opcion_id'])
    cobrado, guardado = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 320
    assert guardado['service_code'] == 'est_dia'


def test_una_cotizacion_de_OTRO_codigo_postal_no_sirve(db, con_llave, monkeypatch):
    """Cotizar a la esquina y mandar a Tijuana. Se tira y se recotiza."""
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    quote = asyncio.run(server._guardar_cotizacion(
        '01000', {'peso_kg': 1.0}, [{'paqueteria': 'Estafeta', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 3, 'precio': 1.0}]))
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 1.0)) is None


def test_una_cotizacion_de_OTRO_peso_no_sirve(db, con_llave, monkeypatch):
    """Cotizar un vial y despachar cuarenta."""
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', {'peso_kg': 1.0}, [{'paqueteria': 'Estafeta', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 3, 'precio': 1.0}]))
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 2.3)) is None


def test_una_cotizacion_vencida_no_sirve(db, con_llave):
    """Guardar el id barato de hoy y usarlo el mes que viene."""
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', {'peso_kg': 1.0}, [{'paqueteria': 'Estafeta', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 3, 'precio': 1.0}]))
    doc = db[server.COLECCION_COTIZACIONES].docs[0]
    doc['expires_at'] = '2020-01-01T00:00:00+00:00'
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 1.0)) is None


def test_una_cotizacion_de_paqueteria_no_permitida_no_se_cobra(db, con_llave):
    """Aunque alguien la meta a mano en la base, FedEx no se cobra ni se compra."""
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', {'peso_kg': 1.0}, [{'paqueteria': 'FedEx', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 1, 'precio': 900.0}]))
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 1.0)) is None


def test_si_la_paqueteria_no_contesta_no_se_inventa_un_cargo(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch, {'/quotations': RuntimeError('caida')})
    cobrado, guardado = asyncio.run(server._envio_del_pedido(_pedido(), 1000, PFLAGS))
    assert cobrado == 0 and guardado == {}


def test_apagado_el_envio_se_comporta_EXACTAMENTE_como_hoy(db):
    """Sin prender nada: cero cargo y ninguna cotización guardada."""
    assert envios.COTIZAR_EN_CHECKOUT is False      # ⛔ nace apagado
    assert envios.COMPRAR_GUIA_AL_PAGAR is False    # ⛔ nace apagado
    assert server.envio_se_cotiza() is False
    cobrado, guardado = asyncio.run(server._envio_del_pedido(_pedido(), 1000, PFLAGS))
    assert cobrado == 0 and guardado == {}


def test_sin_llave_no_se_cotiza_aunque_este_prendido(monkeypatch):
    monkeypatch.delenv('SKYDROPX_API_KEY', raising=False)
    monkeypatch.setattr(envios, 'COTIZAR_EN_CHECKOUT', True)
    assert skydropx.enabled() is False
    assert server.envio_se_cotiza() is False


def test_la_ruta_de_cotizacion_no_rompe_el_checkout_sin_llave(db, monkeypatch):
    monkeypatch.delenv('SKYDROPX_API_KEY', raising=False)
    from models import ShippingQuoteRequest
    r = asyncio.run(server.shipping_quote(ShippingQuoteRequest(postal_code='64000')))
    assert r == {'enabled': False, 'options': []}


def test_la_ruta_de_cotizacion_no_devuelve_precios_de_otras_paqueterias(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch, {'/quotations': TARIFAS_CRUDAS})
    from models import ShippingQuoteRequest
    r = asyncio.run(server.shipping_quote(ShippingQuoteRequest(
        postal_code='64000', items=[OrderItem(product_id='a', name='BPC-157', price=1, quantity=1)])))
    assert r['enabled'] is True
    assert {o['carrier'].lower() for o in r['options']} == {'estafeta'}
    # y el precio NO viaja de vuelta como algo cobrable: cada opción es un ID opaco
    assert all(o['id'] and len(o['id']) > 20 for o in r['options'])


# ==========================================================================
#  5. La guía se compra sola — con los CUATRO métodos de pago
# ==========================================================================
GUIA_OK = {'data': {'id': 'ship-1', 'type': 'shipments',
                    'relationships': {'rates': {'data': [
                        {'id': '9001', 'type': 'rates', 'attributes': {
                            'provider': 'Estafeta', 'service_level_name': 'Terrestre',
                            'service_level_code': 'est_ter', 'days': 4,
                            'total_pricing': '180.00', 'currency_local': 'MXN'}}]}}}}
ETIQUETA_OK = {'data': {'id': 'lbl-1', 'type': 'labels', 'attributes': {
    'tracking_number': '7712345678', 'label_url': 'https://skydropx.test/guia.pdf',
    'tracking_url_provider': 'https://estafeta.test/7712345678'}}}


def _orden(metodo, **extra):
    base = {
        'id': 'o1', 'order_number': 'EX-20260728-0001', 'status': 'pendiente',
        'payment_method': metodo, 'total': 1180, 'shipping': 180,
        'items': [{'product_id': 'a', 'name': 'BPC-157', 'quantity': 1, 'price': 1000}],
        'customer': {'full_name': 'Ana', 'email': 'ana@x.com', 'phone': '+528111111111',
                     'address': 'Calle 1', 'city': 'Monterrey', 'state': 'Nuevo León',
                     'postal_code': '64000', 'country': 'MX'},
        'shipping_quote': {'carrier': 'Estafeta', 'service_code': 'est_ter', 'cost': 180,
                           'paquete': {'peso_kg': 1.0, 'largo_cm': 30, 'ancho_cm': 20, 'alto_cm': 15}},
    }
    base.update(extra)
    return base


@pytest.mark.parametrize('metodo', ['tarjeta', 'spei', 'oxxo', 'cripto'])
def test_la_guia_se_compra_sola_en_los_cuatro_metodos_de_pago(
        metodo, db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch, {'/shipments': GUIA_OK, '/labels': ETIQUETA_OK})
    orden = _orden(metodo)
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden))

    assert hecho['tracking_number'] == '7712345678'
    assert hecho['carrier'] == 'Estafeta'
    assert hecho['label_url'] == 'https://skydropx.test/guia.pdf'
    assert hecho['tracking_url'] == 'https://estafeta.test/7712345678'
    assert hecho['status'] == 'enviado'
    guardado = db.orders.docs[0]
    assert guardado['tracking_number'] == '7712345678'      # y quedó EN el pedido
    assert guardado['label_provider'] == 'skydropx'


def test_los_tres_metodos_de_pasarela_pasan_por_la_confirmacion(db, monkeypatch):
    """Tarjeta, OXXO y cripto confirman por webhook y todos caen en
    `_confirm_paid_order`. Se prueba que ESA es la que dispara la guía."""
    compradas = []
    monkeypatch.setattr(server, 'comprar_guia_del_pedido',
                        lambda o: compradas.append(o.get('order_number')))
    monkeypatch.setattr(server, 'send_payment_confirmed_email', lambda *a, **k: None)
    monkeypatch.setattr(server.asyncio, 'create_task', lambda c: c)
    asyncio.run(db.orders.insert_one(_orden('tarjeta')))
    asyncio.run(server._confirm_paid_order('EX-20260728-0001'))
    assert compradas == ['EX-20260728-0001']
    assert db.orders.docs[0]['status'] == 'confirmado'


def test_spei_compra_su_guia_cuando_el_admin_confirma_el_deposito(db, monkeypatch):
    """SPEI no tiene webhook: lo confirma el admin a mano. Es el cuarto método y
    tiene que comprar guía igual que los otros tres."""
    compradas = []
    monkeypatch.setattr(server, 'comprar_guia_del_pedido',
                        lambda o: compradas.append(o.get('order_number')))
    monkeypatch.setattr(server, 'send_payment_confirmed_email', lambda *a, **k: None)
    monkeypatch.setattr(server, 'notify', _async_nada)
    monkeypatch.setattr(server.asyncio, 'create_task', lambda c: c)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    from models import OrderStatusUpdate
    asyncio.run(server.update_order_status('o1', OrderStatusUpdate(status='confirmado'),
                                           admin={'id': 'admin'}))
    assert compradas == ['EX-20260728-0001']


async def _async_nada(*a, **k):
    return None


def test_no_se_compra_dos_veces_la_misma_guia(db, con_llave, con_remitente, monkeypatch):
    """El webhook de una pasarela puede llegar repetido. Una guía repetida es un
    paquete pagado dos veces."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    llamadas = _falsear_skydropx(monkeypatch, {'/shipments': GUIA_OK, '/labels': ETIQUETA_OK})
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    n = len(llamadas)
    ya_con_guia = db.orders.docs[0]
    assert asyncio.run(server.comprar_guia_del_pedido(ya_con_guia)) is None
    assert len(llamadas) == n              # no volvió a hablarle a Skydropx


def test_apagado_el_interruptor_no_se_compra_ninguna_guia(db, con_llave, con_remitente, monkeypatch):
    llamadas = _falsear_skydropx(monkeypatch, {'/shipments': GUIA_OK, '/labels': ETIQUETA_OK})
    assert asyncio.run(server.comprar_guia_del_pedido(_orden('tarjeta'))) is None
    assert llamadas == []


# ==========================================================================
#  6. El remitente: sin dirección real, no se compra
# ==========================================================================
def test_sin_remitente_configurado_el_sistema_se_NIEGA_a_comprar(db, con_llave, monkeypatch):
    """⚠️ PENDIENTE DE CHRISTIAN: la dirección va a ser la de un trabajador y
    todavía no la tenemos. Comprar con una inventada es pagar una recolección en
    una dirección que no existe."""
    for k in ('NAME', 'ADDRESS1', 'CITY', 'PROVINCE', 'ZIP'):
        monkeypatch.delenv(f'SKYDROPX_FROM_{k}', raising=False)
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    llamadas = _falsear_skydropx(monkeypatch, {'/shipments': GUIA_OK, '/labels': ETIQUETA_OK})
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))

    assert skydropx.remitente_configurado() is False
    assert asyncio.run(server.comprar_guia_del_pedido(orden)) is None
    assert llamadas == []                                   # ni le habló a Skydropx
    assert 'remitente' in db.orders.docs[0]['label_error'].lower()


def test_el_remitente_de_ejemplo_grita_que_esta_pendiente(monkeypatch):
    for k in ('NAME', 'ADDRESS1', 'CITY', 'PROVINCE', 'ZIP'):
        monkeypatch.delenv(f'SKYDROPX_FROM_{k}', raising=False)
    r = skydropx.remitente()
    assert skydropx.REMITENTE_PENDIENTE in r['address1']
    assert skydropx.REMITENTE_PENDIENTE in r['name']


def test_con_remitente_configurado_si_compra(con_remitente):
    assert skydropx.remitente_configurado() is True


def test_una_falla_de_skydropx_no_tumba_un_pedido_ya_pagado(db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch, {'/shipments': RuntimeError('502 de Skydropx')})
    orden = _orden('cripto')
    asyncio.run(db.orders.insert_one(orden))
    assert asyncio.run(server.comprar_guia_del_pedido(orden)) is None   # no revienta
    assert db.orders.docs[0]['label_error']                             # y queda dicho


def test_la_guia_respeta_el_servicio_que_eligio_el_cliente(db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    dos = {'data': {'id': 's1', 'relationships': {'rates': {'data': [
        {'id': '1', 'attributes': {'provider': 'Estafeta', 'service_level_code': 'est_ter',
                                   'service_level_name': 'Terrestre', 'days': 4, 'total_pricing': '180'}},
        {'id': '2', 'attributes': {'provider': 'Estafeta', 'service_level_code': 'est_dia',
                                   'service_level_name': 'Día Siguiente', 'days': 1, 'total_pricing': '320'}},
    ]}}}}
    llamadas = _falsear_skydropx(monkeypatch, {'/shipments': dos, '/labels': ETIQUETA_OK})
    orden = _orden('tarjeta')
    orden['shipping_quote']['service_code'] = 'est_dia'      # eligió el caro
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    etiqueta = next(l for l in llamadas if l['ruta'] == '/labels')
    assert etiqueta['cuerpo']['rate_id'] == '2'              # el que eligió, no el barato


# ==========================================================================
#  7. La llave: entorno, panel y lista blanca
# ==========================================================================
def test_la_llave_de_skydropx_se_puede_pegar_desde_el_admin():
    import secretos
    assert 'SKYDROPX_API_KEY' in secretos.PERMITIDAS


def test_la_llave_viaja_en_el_encabezado_que_pide_skydropx(monkeypatch):
    llamadas = _falsear_skydropx(monkeypatch, {'/quotations': []})
    monkeypatch.setenv('SKYDROPX_API_KEY', 'abc123')
    skydropx.cotizar('64000', {'peso_kg': 1})
    assert llamadas[0]['headers']['Authorization'] == 'Token token=abc123'
