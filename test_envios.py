"""Pruebas del envío con Skydropx PRO (API v2): cotizar, cobrar y comprar la guía.

Lo que cuidan, en orden de cuánto duele si se rompe:

  1. QUE EL PRECIO LO PONGA EL SERVIDOR. Un envío que el navegador manda no se
     cobra jamás, ni aunque venga en la petición. Es la regla más cara de esta
     casa: creerle un precio al navegador ya costó dinero (2026-07-27).
  2. Que solo se le enseñen las paqueterías permitidas (Estafeta y Paquetexpress)
     y solo las que cumplen el plazo, aunque la API devuelva veintitantas.
  3. La regla del 10% y su tope.
  4. Que la guía se compre sola con los CUATRO métodos de pago.
  5. Que sin credenciales y sin remitente nada reviente — y que sin remitente NO
     se compre.
  6. Que la cotización en diferido de la API PRO no cuelgue el checkout.

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


@pytest.fixture(autouse=True)
def _sin_token_viejo():
    """El token vive en memoria del módulo: se tira antes y después de cada prueba.

    Si no, una prueba le dejaría el token a la siguiente y las que cuentan llamadas
    al OAuth medirían cualquier cosa.
    """
    skydropx.olvidar_token()
    yield
    skydropx.olvidar_token()


@pytest.fixture()
def con_llave(monkeypatch):
    """Skydropx encendido y el checkout cotizando.

    Son DOS credenciales porque la API PRO usa OAuth2: con una sola no se enciende.
    """
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'id-de-prueba')
    monkeypatch.setenv('SKYDROPX_CLIENT_SECRET', 'secreto-de-prueba')
    monkeypatch.setattr(envios, 'COTIZAR_EN_CHECKOUT', True)
    return True


@pytest.fixture()
def con_remitente(monkeypatch):
    for k, v in {'NAME': 'Trabajador de Prueba', 'ADDRESS1': 'Calle 1 #2',
                 'CITY': 'Monterrey', 'PROVINCE': 'Nuevo León', 'ZIP': '64000',
                 'COLONIA': 'Centro', 'PHONE': '8112345678',
                 'EMAIL': 'envios@exygenlabs.com'}.items():
        monkeypatch.setenv(f'SKYDROPX_FROM_{k}', v)
    return True


# ==========================================================================
#  Doble de Skydropx PRO
#
#  Copiado de una respuesta REAL del 2026-07-28 (Mérida 97000 → CDMX 06000, 1 kg):
#  precios y plazos son los que devolvió el servicio. Se conservan las tarifas sin
#  precio (`success: false`) porque en la corrida real salieron 15 de 27 así, y el
#  cliente jamás debe verlas.
# ==========================================================================
def _tarifa(rid, proveedor, mostrar, servicio, codigo, dias, total, ok=True):
    return {'success': ok, 'id': rid, 'provider_name': proveedor,
            'provider_display_name': mostrar, 'provider_service_name': servicio,
            'provider_service_code': codigo, 'days': dias,
            'status': 'price_found_internal' if ok else 'no_coverage',
            'currency_code': 'MXN' if ok else None,
            'amount': total if ok else None, 'total': total if ok else None,
            'error_messages': None, 'requires_origin_verification': False}


TARIFAS_V2 = [
    _tarifa('r-dhl', 'dhl', 'DHL', 'Express', 'express', 1, '222.64'),
    _tarifa('r-est-ter', 'estafeta', 'Estafeta', 'Terrestre', 'estafeta_standard', 5, '168.33'),
    _tarifa('r-fedex-sav', 'fedex', 'FedEx', 'Express Saver', 'fedex_express_saver', 4, '52.45'),
    _tarifa('r-fedex-ovn', 'fedex', 'FedEx', 'Standard Overnight', 'standard_overnight', 2, '179.20'),
    _tarifa('r-est-exp', 'estafeta', 'Estafeta', 'Servicio Express', 'estafeta_next_day', 3, '186.90'),
    _tarifa('r-pqx-2d', 'paquetexpress', 'Paquetexpress', 'Express Second Day',
            'express_second_day', 2, '165.27'),
    # ⚠️ La trampa real: la más barata de todas, pero 7 días. Rompe la promesa del
    # sitio y por eso NO se le enseña al cliente.
    _tarifa('r-pqx-nac', 'paquetexpress', 'Paquetexpress', 'Nacional', 'nacional', 7, '51.25'),
    _tarifa('r-afimex', 'afimex', 'Afimex', 'Standard', 'standard', 0, None, ok=False),
]

PAQUETES_V2 = [{'package_number': 1, 'weight': '1.0', 'length': '30.0',
                'width': '20.0', 'height': '15.0'}]

# Lo que devuelve POST /shipments. ⚠️ NO VERIFICADO CONTRA UNA COMPRA REAL: comprar
# una guía cuesta dinero. La forma sale del JSON:API que devuelve GET /shipments.
GUIA_OK = {'data': {'id': 'ship-1', 'type': 'shipments', 'attributes': {'status': 'processing'}},
           'included': [{'id': 'lbl-1', 'type': 'labels', 'attributes': {
               'tracking_number': '7712345678', 'label_url': 'https://skydropx.test/guia.pdf',
               'tracking_url_provider': 'https://estafeta.test/7712345678'}}]}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)[:200]

    def json(self):
        return self._p


class ApiFalsa:
    """Un Skydropx PRO de mentira: OAuth, cotización EN DIFERIDO y /shipments.

    ⛔ Reproduce lo que de verdad hace la API: `POST /quotations` contesta al
    instante con las tarifas sin precio, y hay que volver a preguntar hasta que
    `is_completed` sea true. Si la prueba no pasa por ahí, no prueba nada.
    """

    def __init__(self, tarifas=None, consultas_para_completar=1, fallos=None,
                 shipment=None, nunca_completa=False, un_401_en=''):
        self.tarifas = TARIFAS_V2 if tarifas is None else tarifas
        self.faltan = consultas_para_completar
        self.fallos = fallos or {}
        self.shipment = shipment if shipment is not None else GUIA_OK
        self.nunca_completa = nunca_completa
        self.un_401_en = un_401_en
        self.llamadas = []
        self.tokens = 0

    # -- utilidades
    def _ruta(self, url):
        return url.split('/api/v1', 1)[-1] if '/api/v1' in url else url

    def _pendientes(self):
        return [dict(t, success=False, status='pending', amount=None, total=None)
                for t in self.tarifas]

    def _cotizacion(self, completa):
        return {'id': 'q-1', 'is_completed': completa, 'packages': PAQUETES_V2,
                'requires_origin_verification': True,
                'rates': self.tarifas if completa else self._pendientes()}

    def _quizas_falla(self, ruta):
        salida = self.fallos.get(ruta)
        if isinstance(salida, Exception):
            raise salida
        if isinstance(salida, int):
            return FakeResp({'message': 'no'}, salida)
        return None

    # -- los dos verbos
    def post(self, url, headers=None, json=None, timeout=None):
        ruta = self._ruta(url)
        self.llamadas.append({'ruta': ruta, 'metodo': 'POST', 'cuerpo': json,
                              'headers': headers})
        if ruta == '/oauth/token':
            self.tokens += 1
            return FakeResp({'access_token': f'token-{self.tokens}', 'expires_in': 7200,
                             'token_type': 'Bearer'})
        if self.un_401_en == ruta:
            self.un_401_en = ''
            return FakeResp({'message': 'no autorizado'}, 401)
        fallo = self._quizas_falla(ruta)
        if fallo is not None:
            return fallo
        if ruta == '/quotations':
            return FakeResp(self._cotizacion(self.faltan <= 0 and not self.nunca_completa))
        if ruta == '/shipments':
            return FakeResp(self.shipment)
        return FakeResp({}, 404)

    def get(self, url, headers=None, timeout=None):
        ruta = self._ruta(url)
        self.llamadas.append({'ruta': ruta, 'metodo': 'GET', 'headers': headers})
        if self.un_401_en == ruta:
            self.un_401_en = ''
            return FakeResp({'message': 'no autorizado'}, 401)
        fallo = self._quizas_falla(ruta)
        if fallo is not None:
            return fallo
        if ruta.startswith('/quotations/'):
            self.faltan -= 1
            return FakeResp(self._cotizacion(self.faltan <= 0 and not self.nunca_completa))
        if ruta.startswith('/shipments/'):
            return FakeResp(self.shipment)
        return FakeResp({}, 404)


def _falsear_skydropx(monkeypatch, **kw):
    """Sustituye requests.post y requests.get: NUNCA se toca el servicio real."""
    api = ApiFalsa(**kw)
    monkeypatch.setattr(skydropx.requests, 'post', api.post)
    monkeypatch.setattr(skydropx.requests, 'get', api.get)
    monkeypatch.setattr(skydropx, 'ESPERA_ENTRE_CONSULTAS_S', 0.01)
    return api


def _peticiones(api, ruta=None):
    """Lo que se le pidió a la paquetería, sin contar el trámite del token."""
    return [l for l in api.llamadas
            if l['ruta'] != '/oauth/token' and (ruta is None or l['ruta'] == ruta)]


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
#  2. Solo las paqueterías permitidas, y solo las que llegan a tiempo
# ==========================================================================
BULTO = {'peso_kg': 1, 'alto_cm': 15, 'ancho_cm': 20, 'largo_cm': 30}


def test_solo_se_le_enseñan_las_paqueterias_permitidas(monkeypatch, con_llave):
    """DHL y Afimex salen en la respuesta real de la API. El cliente no las ve."""
    _falsear_skydropx(monkeypatch)
    opciones = skydropx.cotizar('64000', BULTO)
    assert {o['paqueteria'] for o in opciones} == {'Estafeta', 'Paquetexpress', 'FedEx'}
    # de barato a caro, y ninguna de las que se pasan del plazo
    assert [o['precio'] for o in opciones] == [52.45, 165.27, 168.33, 179.2, 186.9]


def test_la_mas_barata_de_7_dias_NO_se_le_ofrece_al_cliente(monkeypatch, con_llave):
    """Paquetexpress Nacional cuesta $51.25 —la más barata de todas— pero tarda 7
    días y el sitio promete 2-5. Ordenar solo por precio la pondría hasta arriba.

    La que gana es FedEx Express Saver: $52.45 en 4 días, un peso más cara y tres
    días antes."""
    _falsear_skydropx(monkeypatch)
    opciones = skydropx.cotizar('64000', BULTO)
    assert 'nacional' not in [o['servicio_codigo'] for o in opciones]
    assert opciones[0]['servicio_codigo'] == 'fedex_express_saver'
    assert all(o['dias'] <= skydropx.DIAS_MAXIMOS_ENTREGA for o in opciones)
    assert skydropx.DIAS_MAXIMOS_ENTREGA == 5


def test_el_plazo_maximo_se_cambia_en_un_solo_renglon(monkeypatch, con_llave):
    _falsear_skydropx(monkeypatch)
    monkeypatch.setattr(skydropx, 'DIAS_MAXIMOS_ENTREGA', 7)
    opciones = skydropx.cotizar('64000', BULTO)
    assert opciones[0]['servicio_codigo'] == 'nacional'      # ahora sí, y es la barata
    assert opciones[0]['precio'] == 51.25


def test_un_plazo_desconocido_no_se_castiga():
    """0 días quiere decir «no lo dijeron», no «llega hoy»: no se tira por eso."""
    assert skydropx.dentro_del_plazo(0) is True
    assert skydropx.dentro_del_plazo(None) is True
    assert skydropx.dentro_del_plazo('basura') is True
    assert skydropx.dentro_del_plazo(99) is False


def test_la_lista_de_permitidas_se_amplia_con_un_renglon(monkeypatch):
    """Y viene EN MINÚSCULAS, que es como manda `provider_name` la API PRO."""
    assert skydropx.PAQUETERIAS_PERMITIDAS == ('estafeta', 'paquetexpress', 'fedex')
    assert skydropx.permitida('paquetexpress') is True
    assert skydropx.permitida('Estafeta') is True            # el nombre bonito también
    assert skydropx.permitida('dhl') is False
    monkeypatch.setattr(skydropx, 'PAQUETERIAS_PERMITIDAS', ('estafeta', 'dhl'))
    assert skydropx.permitida('DHL') is True
    assert skydropx.permitida('paquetexpress') is False


def test_el_filtro_se_aplica_a_lo_que_devuelve_la_api(monkeypatch, con_llave):
    """La API PRO ignora el `carriers` que se le mande (comprobado en vivo: devolvió
    las 27 igual). Así que el único candado que sirve es el nuestro, al recibir."""
    api = _falsear_skydropx(monkeypatch)
    opciones = skydropx.cotizar('64000', BULTO)
    pedido = _peticiones(api, '/quotations')[0]['cuerpo']['quotation']
    assert pedido['address_to']['postal_code'] == '64000'
    fuera = {'dhl', 'afimex'}
    assert fuera & {t['provider_name'] for t in TARIFAS_V2}       # sí venían
    assert not fuera & {o['paqueteria_id'] for o in opciones}     # y no salieron


def test_una_tarifa_sin_precio_no_es_una_opcion(monkeypatch, con_llave):
    """En la corrida real 15 de 27 volvieron con `success: false` y `total: null`."""
    _falsear_skydropx(monkeypatch, tarifas=[
        _tarifa('x', 'estafeta', 'Estafeta', 'Terrestre', 'estafeta_standard', 3, None, ok=False),
        _tarifa('y', 'estafeta', 'Estafeta', 'Express', 'estafeta_next_day', 3, '0')])
    assert skydropx.cotizar('64000', {'peso_kg': 1}) == []


# ==========================================================================
#  2b. La cotización es EN DIFERIDO — y no puede colgar el checkout
# ==========================================================================
def test_la_cotizacion_se_espera_hasta_que_este_completa(monkeypatch, con_llave):
    """La API contesta al instante con las tarifas vacías. Si no se vuelve a
    preguntar, el checkout enseñaría una lista sin precios."""
    api = _falsear_skydropx(monkeypatch, consultas_para_completar=3)
    opciones = skydropx.cotizar('64000', BULTO)
    assert len(opciones) == 5
    assert len(_peticiones(api, '/quotations/q-1')) == 3       # preguntó tres veces


def test_si_la_paqueteria_tarda_demasiado_el_checkout_sigue_sin_envio(monkeypatch, con_llave):
    """⛔ Un carrito congelado cuesta más que un envío. Pasado el tope se devuelve
    vacío y el checkout se comporta como hoy."""
    _falsear_skydropx(monkeypatch, nunca_completa=True)
    monkeypatch.setattr(skydropx, 'ESPERA_MAX_COTIZACION_S', 0.05)
    assert skydropx.cotizar('64000', BULTO) == []


def test_media_cotizacion_no_se_le_enseña_al_cliente(monkeypatch, con_llave):
    """Sin `is_completed` las tarifas que ya llegaron son las que alcanzaron, no las
    mejores: enseñarlas sería cobrar por la que ganó la carrera."""
    _falsear_skydropx(monkeypatch, nunca_completa=True)
    monkeypatch.setattr(skydropx, 'ESPERA_MAX_COTIZACION_S', 0.05)
    cot = skydropx.cotizacion({'zip': '64000'}, BULTO)
    assert cot['completa'] is False and cot['opciones'] == []


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


def test_arriba_del_tope_el_cliente_paga_SOLO_la_diferencia():
    """DECIDIDO por Christian el 2026-07-28: "el cliente paga la diferencia y la
    casa absorbe hasta el 10% del costo del envío máximo". Pedido de $3,000 con
    envío de $600: la casa pone $300 (su 10%) y el cliente paga los otros $300."""
    assert envios.CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE is False
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500) == 300


def test_la_casa_nunca_absorbe_mas_del_10_por_ciento():
    """Es el otro lado de la misma regla: por cara que salga la guía, la casa se
    queda topada en el 10% de la compra."""
    assert envios.cobro_de_envio_al_cliente(2000, 3000, 2500) == 1700   # casa: 300
    assert envios.cobro_de_envio_al_cliente(310, 3000, 2500) == 10      # casa: 300


def test_la_otra_definicion_se_prende_cambiando_una_sola_linea(monkeypatch):
    monkeypatch.setattr(envios, 'CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE', True)
    # Con la regla contraria, ese mismo pedido pagaría los $600 completos.
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500) == 600


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
    """El pedido dice que el envío cuesta $1. El servidor cobra los $52.45 que él
    mismo cotizó y guardó. Es la regla que ya costó dinero cuando no existía."""
    _falsear_skydropx(monkeypatch)
    payload = _pedido(shipping_mentiroso=1)
    cobrado, guardado = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 52                       # el precio real, no el del navegador
    assert guardado['cost'] == 52.45
    assert guardado['carrier'] == 'FedEx'


def test_un_id_de_cotizacion_inventado_no_regala_el_envio(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch)
    payload = _pedido(quote_id='me-lo-invente', shipping_mentiroso=0)
    cobrado, _ = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 52                       # recotiza; no cobra cero por creerle


def test_la_cotizacion_guardada_manda_sobre_la_recotizacion(db, con_llave, monkeypatch):
    """El cliente eligió el Express de Estafeta ($186.90). Se le cobra ESO, no la
    más barata: eligió y el servidor respeta lo que él mismo le enseñó."""
    _falsear_skydropx(monkeypatch)
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', envios.paquete_del_pedido(_pedido().items, PFLAGS),
        skydropx.cotizar('64000', {'peso_kg': 1})))
    express = next(o for o in quote['opciones'] if o['precio'] == 186.9)
    payload = _pedido(quote_id=express['opcion_id'])
    cobrado, guardado = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 187
    assert guardado['service_code'] == 'estafeta_next_day'


def test_una_cotizacion_de_OTRO_codigo_postal_no_sirve(db, con_llave, monkeypatch):
    """Cotizar a la esquina y mandar a Tijuana. Se tira y se recotiza."""
    _falsear_skydropx(monkeypatch)
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
    """Aunque alguien la meta a mano en la base, DHL no se cobra ni se compra."""
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', {'peso_kg': 1.0}, [{'paqueteria': 'DHL', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 1, 'precio': 900.0}]))
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 1.0)) is None


def test_si_la_paqueteria_no_contesta_no_se_inventa_un_cargo(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch, fallos={'/quotations': RuntimeError('caida')})
    cobrado, guardado = asyncio.run(server._envio_del_pedido(_pedido(), 1000, PFLAGS))
    assert cobrado == 0 and guardado == {}


def test_apagado_el_envio_se_comporta_EXACTAMENTE_como_hoy(db):
    """Sin prender nada: cero cargo y ninguna cotización guardada."""
    assert envios.COTIZAR_EN_CHECKOUT is False      # ⛔ nace apagado
    assert envios.COMPRAR_GUIA_AL_PAGAR is False    # ⛔ nace apagado
    assert server.envio_se_cotiza() is False
    # Sin cotización viva se cae a la tarifa plana de $250, que es la política nueva.
    cobrado, guardado = asyncio.run(server._envio_del_pedido(_pedido(), 1000, PFLAGS))
    assert cobrado == 250 and guardado == {}


def test_sin_credenciales_no_se_cotiza_aunque_este_prendido(monkeypatch):
    for k in ('SKYDROPX_CLIENT_ID', 'SKYDROPX_CLIENT_SECRET'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(envios, 'COTIZAR_EN_CHECKOUT', True)
    assert skydropx.enabled() is False
    assert server.envio_se_cotiza() is False


def test_con_UNA_sola_credencial_tampoco_se_enciende(monkeypatch):
    """El OAuth2 pide las dos. Media credencial es no tener credencial."""
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'solo-el-id')
    monkeypatch.delenv('SKYDROPX_CLIENT_SECRET', raising=False)
    assert skydropx.enabled() is False


def test_la_ruta_de_cotizacion_no_rompe_el_checkout_sin_llave(db, monkeypatch):
    for k in ('SKYDROPX_CLIENT_ID', 'SKYDROPX_CLIENT_SECRET'):
        monkeypatch.delenv(k, raising=False)
    from models import ShippingQuoteRequest
    r = asyncio.run(server.shipping_quote(ShippingQuoteRequest(postal_code='64000')))
    assert r == {'enabled': False, 'options': []}


def test_la_ruta_de_cotizacion_no_devuelve_precios_de_otras_paqueterias(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch)
    from models import ShippingQuoteRequest
    r = asyncio.run(server.shipping_quote(ShippingQuoteRequest(
        postal_code='64000', items=[OrderItem(product_id='a', name='BPC-157', price=1, quantity=1)])))
    assert r['enabled'] is True
    assert {o['carrier'].lower() for o in r['options']} == {'estafeta', 'paquetexpress', 'fedex'}
    assert all(o['days'] <= skydropx.DIAS_MAXIMOS_ENTREGA for o in r['options'])
    # y el precio NO viaja de vuelta como algo cobrable: cada opción es un ID opaco
    assert all(o['id'] and len(o['id']) > 20 for o in r['options'])


# ==========================================================================
#  5. La guía se compra sola — con los CUATRO métodos de pago
# ==========================================================================
def _orden(metodo, **extra):
    base = {
        'id': 'o1', 'order_number': 'EX-20260728-0001', 'status': 'pendiente',
        'payment_method': metodo, 'total': 1180, 'shipping': 180,
        'items': [{'product_id': 'a', 'name': 'BPC-157', 'quantity': 1, 'price': 1000}],
        'customer': {'full_name': 'Ana', 'email': 'ana@x.com', 'phone': '+528111111111',
                     'address': 'Calle 1', 'city': 'Monterrey', 'state': 'Nuevo León',
                     'postal_code': '64000', 'country': 'MX'},
        'shipping_quote': {'carrier': 'Estafeta', 'service_code': 'estafeta_standard',
                           'cost': 168.33,
                           'paquete': {'peso_kg': 1.0, 'largo_cm': 30, 'ancho_cm': 20, 'alto_cm': 15}},
    }
    base.update(extra)
    return base


@pytest.mark.parametrize('metodo', ['tarjeta', 'spei', 'oxxo', 'cripto'])
def test_la_guia_se_compra_sola_en_los_cuatro_metodos_de_pago(
        metodo, db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
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


def test_comprar_la_guia_manda_el_cuerpo_QUE_LA_API_PRO_ACEPTA(
        db, con_llave, con_remitente, monkeypatch):
    """La forma se comprobó contra la API REAL el 2026-07-28 sin comprar nada: se le
    mandó todo esto con un `package_number` mal a propósito y lo único que reclamó
    fue ese número. O sea: el resto del cuerpo lo dio por bueno.

    ⛔ Se cotiza primero y se compra contra el `rate_id` de ESA cotización, con su
    mismo `package_number`. Si no coinciden, la API rechaza la compra."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))

    envio = _peticiones(api, '/shipments')[0]['cuerpo']['shipment']
    assert envio['rate_id'] == 'r-est-ter'                 # el servicio que se eligió
    assert envio['packages'][0]['package_number'] == 1     # el de la cotización
    assert envio['packages'][0]['package_type'] == '4G'
    assert envio['packages'][0]['consignment_note'] == '31181701'
    # Los datos de la persona: la API los exige y los imprime en la guía.
    for lado in ('address_from', 'address_to'):
        for campo in ('name', 'street1', 'phone', 'email', 'reference'):
            assert envio[lado][campo], f'{lado}.{campo} vacío: la API lo rechaza'
    assert envio['address_to']['name'] == 'Ana'
    assert envio['address_from']['email'] == 'envios@exygenlabs.com'


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
    api = _falsear_skydropx(monkeypatch)
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    n = len(api.llamadas)
    ya_con_guia = db.orders.docs[0]
    assert asyncio.run(server.comprar_guia_del_pedido(ya_con_guia)) is None
    assert len(api.llamadas) == n          # no volvió a hablarle a Skydropx


def test_apagado_el_interruptor_no_se_compra_ninguna_guia(db, con_llave, con_remitente, monkeypatch):
    api = _falsear_skydropx(monkeypatch)
    assert asyncio.run(server.comprar_guia_del_pedido(_orden('tarjeta'))) is None
    assert api.llamadas == []


# ==========================================================================
#  6. El remitente: sin dirección real, no se compra
# ==========================================================================
def test_sin_remitente_configurado_el_sistema_se_NIEGA_a_comprar(db, con_llave, monkeypatch):
    """⚠️ PENDIENTE DE CHRISTIAN: la dirección va a ser la de un trabajador y
    todavía no la tenemos. Comprar con una inventada es pagar una recolección en
    una dirección que no existe."""
    for k in ('NAME', 'ADDRESS1', 'CITY', 'PROVINCE', 'ZIP', 'PHONE', 'EMAIL'):
        monkeypatch.delenv(f'SKYDROPX_FROM_{k}', raising=False)
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))

    assert skydropx.remitente_configurado() is False
    assert asyncio.run(server.comprar_guia_del_pedido(orden)) is None
    assert api.llamadas == []                               # ni le habló a Skydropx
    assert 'remitente' in db.orders.docs[0]['label_error'].lower()


def test_el_remitente_de_ejemplo_grita_que_esta_pendiente(monkeypatch):
    for k in ('NAME', 'ADDRESS1', 'CITY', 'PROVINCE', 'ZIP', 'PHONE', 'EMAIL'):
        monkeypatch.delenv(f'SKYDROPX_FROM_{k}', raising=False)
    r = skydropx.remitente()
    assert skydropx.REMITENTE_PENDIENTE in r['address1']
    assert skydropx.REMITENTE_PENDIENTE in r['name']


def test_sin_telefono_ni_correo_del_remitente_TAMPOCO_se_compra(monkeypatch, con_remitente):
    """La API PRO los exige en /shipments: sin ellos la compra muere en un 422,
    después de que el cliente ya pagó. Mejor negarse antes."""
    monkeypatch.delenv('SKYDROPX_FROM_PHONE', raising=False)
    assert skydropx.remitente_configurado() is False


def test_con_remitente_configurado_si_compra(con_remitente):
    assert skydropx.remitente_configurado() is True


def test_una_falla_de_skydropx_no_tumba_un_pedido_ya_pagado(db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch, fallos={'/shipments': RuntimeError('502 de Skydropx')})
    orden = _orden('cripto')
    asyncio.run(db.orders.insert_one(orden))
    assert asyncio.run(server.comprar_guia_del_pedido(orden)) is None   # no revienta
    assert db.orders.docs[0]['label_error']                             # y queda dicho


def test_la_guia_respeta_el_servicio_que_eligio_el_cliente(db, con_llave, con_remitente, monkeypatch):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _orden('tarjeta')
    orden['shipping_quote']['service_code'] = 'estafeta_next_day'   # eligió el caro
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    envio = _peticiones(api, '/shipments')[0]['cuerpo']['shipment']
    assert envio['rate_id'] == 'r-est-exp'          # el que eligió, no el barato


def test_si_el_servicio_elegido_ya_no_existe_cae_en_el_mas_barato_PERMITIDO(
        db, con_llave, con_remitente, monkeypatch):
    """Nunca en una paquetería que el cliente no pidió ni en un plazo que no se le
    prometió: la de 7 días sigue descartada aunque sea la más barata."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _orden('tarjeta')
    orden['shipping_quote']['service_code'] = 'ya-no-existe'
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    envio = _peticiones(api, '/shipments')[0]['cuerpo']['shipment']
    assert envio['rate_id'] == 'r-fedex-sav'        # $52.45 en 4 días, no la de 7


# ==========================================================================
#  7. Las credenciales: OAuth2, entorno, panel y lista blanca
# ==========================================================================
def test_las_credenciales_de_skydropx_se_pueden_pegar_desde_el_admin():
    import secretos
    assert 'SKYDROPX_CLIENT_ID' in secretos.PERMITIDAS
    assert 'SKYDROPX_CLIENT_SECRET' in secretos.PERMITIDAS
    # La de la API vieja ya no sirve para nada: no se guarda.
    assert 'SKYDROPX_API_KEY' not in secretos.PERMITIDAS


def test_el_token_viaja_como_Bearer_y_no_como_la_llave_vieja(monkeypatch, con_llave):
    api = _falsear_skydropx(monkeypatch)
    skydropx.cotizar('64000', BULTO)
    oauth = next(l for l in api.llamadas if l['ruta'] == '/oauth/token')
    assert oauth['cuerpo']['grant_type'] == 'client_credentials'
    assert oauth['cuerpo']['client_id'] == 'id-de-prueba'
    for l in _peticiones(api):
        assert l['headers']['Authorization'] == 'Bearer token-1'


def test_el_token_se_guarda_y_no_se_pide_en_cada_cotizacion(monkeypatch, con_llave):
    """Pedir token en cada llamada duplicaría el tráfico. Dura 2 horas."""
    api = _falsear_skydropx(monkeypatch)
    skydropx.cotizar('64000', BULTO)
    skydropx.cotizar('64000', BULTO)
    assert api.tokens == 1


def test_un_token_vencido_se_renueva_solo(monkeypatch, con_llave):
    api = _falsear_skydropx(monkeypatch)
    skydropx.cotizar('64000', BULTO)
    skydropx._TOKEN['vence'] = 0            # como si hubieran pasado las 2 horas
    skydropx.cotizar('64000', BULTO)
    assert api.tokens == 2


def test_un_401_pide_token_nuevo_y_reintenta_UNA_vez(monkeypatch, con_llave):
    """Un token puede morir antes de tiempo (lo revocan desde el panel). Que eso
    tumbe una cotización sería tirar una venta por un trámite."""
    api = _falsear_skydropx(monkeypatch, un_401_en='/quotations')
    opciones = skydropx.cotizar('64000', BULTO)
    assert opciones and api.tokens == 2
    assert len(_peticiones(api, '/quotations')) == 2


def test_un_401_que_no_cede_no_se_reintenta_en_bucle(monkeypatch, con_llave):
    api = _falsear_skydropx(monkeypatch, fallos={'/quotations': 401})
    with pytest.raises(RuntimeError):
        skydropx.cotizar('64000', BULTO)
    assert len(_peticiones(api, '/quotations')) == 2      # dos, y ya


def test_la_url_por_omision_es_la_de_skydropx_PRO():
    """La vieja (api.skydropx.com/v1) no entiende OAuth2 ni cotiza en diferido."""
    assert skydropx.API == 'https://pro.skydropx.com/api/v1'


# ==========================================================================
#  8. EL TOPE DEL 10% TAMBIÉN EN LA TARIFA PLANA (Christian, 2026-07-28)
#
#  «Envío gratis arriba de $2,500 PERO con tope del 10%: si una compra por 2,500
#  genera un costo de envío de $500 ni en pedo lo pago.»
#
#  El camino de la tarifa plana (`shipping_for`, el que se usará el día que
#  `COBRAR_ENVIO` se ponga en True) tenía su PROPIA cuenta —"gratis arriba de
#  $2,500"— que nunca miraba lo que la guía costaba de verdad. La regla del 10%
#  existía sólo en el camino de Skydropx. Ahora hay UNA sola regla.
# ==========================================================================
def test_la_tarifa_plana_usa_LA_MISMA_regla_del_10_por_ciento():
    """`shipping_for` no decide nada por su cuenta: delega en la regla de envios.py."""
    for compra in (0, 179, 879, 2499, 2500, 3000, 50000):
        assert server.shipping_for(compra) == envios.cobro_de_envio_al_cliente(
            server.SHIPPING_FLAT, compra, server.FREE_SHIPPING_FROM)


def test_un_pedido_de_179_NUNCA_lleva_envio_gratis():
    """$250 de guía sobre $179 de mercancía es el 140% del pedido."""
    assert server.shipping_for(179) == server.SHIPPING_FLAT
    assert envios.cobro_de_envio_al_cliente(250, 179, server.FREE_SHIPPING_FROM) == 250
    assert envios.tope_que_absorbe_la_casa(179) == 17.9


def test_una_guia_cara_ya_no_se_regala_por_pasar_el_umbral():
    """$2,600 de compra con una guía REAL de $500: el 19%. No va gratis.
    Antes `shipping_for` devolvía 0 para cualquier compra arriba de $2,500,
    costara lo que costara la guía."""
    # La casa absorbe su 10% ($260) y el cliente paga los otros $240.
    assert server.shipping_for(2600, costo_real=500) == 240
    assert server.shipping_for(2600, costo_real=250) == 0      # el 9.6%: sí cabe


def test_lo_que_la_casa_absorbe_y_cuanto_se_pasa_del_tope():
    # Pedido de $179, guía de $250, cobro apagado: la casa se come los $250 enteros.
    assert envios.envio_que_absorbe_la_casa(250, 0) == 250
    assert envios.absorcion_fuera_de_tope(250, 179, 0) == 232.1     # 250 − 17.90
    # Pedido de $3,000 con guía de $250: cabe en el 10%, no se pasa de nada.
    assert envios.absorcion_fuera_de_tope(250, 3000, 0) == 0
    # Y si el cliente la pagó, la casa no absorbe nada.
    assert envios.envio_que_absorbe_la_casa(250, 250) == 0
    assert envios.absorcion_fuera_de_tope(250, 179, 250) == 0


def test_las_funciones_del_tope_no_revientan_con_basura():
    assert envios.tope_que_absorbe_la_casa(None) == 0
    assert envios.tope_que_absorbe_la_casa('x') == 0
    assert envios.envio_que_absorbe_la_casa(None, None) == 0
    assert envios.envio_que_absorbe_la_casa('a', 'b') == 0
    assert envios.absorcion_fuera_de_tope(None, None, None) == 0


def test_el_pedido_guarda_lo_que_la_casa_absorbe_aunque_no_cobre_envio():
    """Con el cobro apagado el pedido guardaba costo $0 y absorbido $0: los $250
    que la casa se come no existían en ningún reporte."""
    assert server.COBRAR_ENVIO is True               # política nueva: $250 parejo
    # Pedido de $179: el cliente paga los $250, la casa no absorbe nada.
    cobrado = server.shipping_for(179)
    costo_guia = server.SHIPPING_FLAT
    assert cobrado == 250
    assert envios.envio_que_absorbe_la_casa(costo_guia, cobrado) == 0
    # Pedido de $3,000: va gratis, y ahí SÍ lo absorbe la casa — y queda registrado.
    assert server.shipping_for(3000) == 0
    assert envios.envio_que_absorbe_la_casa(costo_guia, 0) == 250
    assert envios.absorcion_fuera_de_tope(costo_guia, 3000, 0) == 0   # 250 cabe en el 10%
