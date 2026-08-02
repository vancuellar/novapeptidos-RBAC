"""Pruebas del envío con Skydropx PRO (API v2): cotizar, cobrar y comprar la guía.

Lo que cuidan, en orden de cuánto duele si se rompe:

  1. QUE EL PRECIO LO PONGA EL SERVIDOR. Un envío que el navegador manda no se
     cobra jamás, ni aunque venga en la petición. Es la regla más cara de esta
     casa: creerle un precio al navegador ya costó dinero (2026-07-27).
  2. Que solo se le enseñen las paqueterías permitidas (Estafeta y Paquetexpress)
     y solo las que cumplen el plazo, aunque la API devuelva veintitantas.
  3. La política de envío: compra mínima primero, tope del 5% después.
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
        if isinstance(v, dict) and '$nin' in v:
            if doc.get(k) in v['$nin']:
                return False
            continue
        if isinstance(v, dict) and '$lt' in v:
            try:
                if not float(doc.get(k) or 0) < float(v['$lt']):
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if isinstance(v, dict) and '$ne' in v:
            actual = doc.get(k)
            # ⛔ EN UN CAMPO DE LISTA, `$ne` SIGNIFICA «NINGÚN ELEMENTO ES IGUAL».
            # El doble comparaba la lista entera contra el valor, así que
            # {'emails_sent': {'$ne': 'pagado'}} SIEMPRE daba verdadero y el candado
            # de «un correo por evento» no bloqueaba nada: la prueba pasaba en verde
            # mientras el mismo correo salía dos veces.
            if isinstance(actual, list):
                if v['$ne'] in actual:
                    return False
            elif actual == v['$ne']:
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
                # `$addToSet`: lo usa el candado de los correos (`emails_sent`), que
                # es lo que impide que el mismo aviso salga dos veces.
                for k, val in (cambio.get('$addToSet') or {}).items():
                    lista = list(d.get(k) or [])
                    if val not in lista:
                        lista.append(val)
                    d[k] = lista
                return _Res(1)
        if upsert:
            # El doble ignoraba `upsert` y devolvía 0 en silencio. Los ajustes de
            # envío (remitente, cajas) se guardan así: sin esto, la prueba decía que
            # se habían guardado y el documento no existía.
            nuevo = {k: v for k, v in (filtro or {}).items() if not isinstance(v, dict)}
            nuevo.update(cambio.get('$set') or {})
            self.docs.append(nuevo)
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
    """TODAS las paqueterías compiten (Christian, 2026-07-30) — el único cedazo que
    queda es el plazo. Afimex se cae por venir sin precio, no por su nombre."""
    _falsear_skydropx(monkeypatch)
    opciones = skydropx.cotizar('64000', BULTO)
    assert {o['paqueteria'] for o in opciones} == {'Estafeta', 'Paquetexpress', 'FedEx', 'DHL'}
    # de barato a caro, y ninguna de las que se pasan del plazo
    assert [o['precio'] for o in opciones] == [52.45, 165.27, 168.33, 179.2, 186.9, 222.64]


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
    """VACÍA = todas compiten (Christian, 2026-07-30). Poner nombres vuelve a
    restringir, EN MINÚSCULAS, como manda `provider_name` la API PRO."""
    assert skydropx.PAQUETERIAS_PERMITIDAS == ()
    assert skydropx.permitida('paquetexpress') is True
    assert skydropx.permitida('DHL') is True
    assert skydropx.permitida('UPS') is True                 # cualquiera pasa
    monkeypatch.setattr(skydropx, 'PAQUETERIAS_PERMITIDAS', ('estafeta', 'dhl'))
    assert skydropx.permitida('DHL') is True
    assert skydropx.permitida('Estafeta') is True            # el nombre bonito también
    assert skydropx.permitida('paquetexpress') is False


def test_el_filtro_se_aplica_a_lo_que_devuelve_la_api(monkeypatch, con_llave):
    """La API PRO ignora el `carriers` que se le mande (comprobado en vivo: devolvió
    las 27 igual). Así que el único candado que sirve es el nuestro, al recibir."""
    api = _falsear_skydropx(monkeypatch)
    opciones = skydropx.cotizar('64000', BULTO)
    pedido = _peticiones(api, '/quotations')[0]['cuerpo']['quotation']
    assert pedido['address_to']['postal_code'] == '64000'
    # DHL ya compite (2026-07-30); Afimex sigue fuera pero por venir SIN precio,
    # y Paquetexpress Nacional por pasarse del plazo. El candado sigue siendo
    # nuestro al recibir, porque la API ignora el `carriers` que se le manda.
    salieron = {o['paqueteria_id'] for o in opciones}
    assert 'dhl' in salieron
    assert 'afimex' not in salieron
    assert 'nacional' not in {o['servicio_codigo'] for o in opciones}


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
    assert len(opciones) == 6
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
#  3. La regla del 5% y su tope
#
#  ⛔ POLÍTICA NUEVA (Christián, 2026-07-31), en sus palabras: «gratis siempre y
#  cuando el ticket supere los $2,500 de compra mínima y/o [el envío] no sea mayor a
#  5% del total de la compra. Primero se debe cumplir la compra mínima.»
#
#  Son DOS candados y el orden importa: la mínima manda, y sólo después se mira el 5%.
#  El tope venía siendo del 10% desde el 2026-07-28.
# ==========================================================================
def test_el_tope_bajo_del_10_al_5_por_ciento():
    """El número que decide todo. Escrito aquí para que nadie lo mueva de callado."""
    assert envios.TOPE_ENVIO_SOBRE_COMPRA == 0.05


def test_la_compra_minima_se_escribe_en_pesos_y_NO_se_deriva_del_tope():
    """Antes el umbral era `SHIPPING_FLAT / TOPE` (250 / 10% = 2,500). Con el 5% esa
    cuenta lo habría movido solo a $5,000 sin que nadie lo pidiera. Christián lo dictó
    en pesos, así que en pesos vive."""
    assert envios.COMPRA_MINIMA_ENVIO_GRATIS == 2500
    assert server.FREE_SHIPPING_FROM == 2500


def test_compra_chica_paga_su_envio_completo():
    # $879 de mercancía con $250 de envío: absorberlo se come el 28% del ingreso.
    assert envios.cobro_de_envio_al_cliente(250, 879, 2500) == 250


def test_PRIMERO_la_compra_minima_por_barato_que_salga_el_envio():
    """El orden que subrayó Christián. Una guía de $5 en un pedido de $500 es el 1%
    —cabe de sobra en el 5%— y AUN ASÍ se cobra: no llegó a la compra mínima."""
    assert envios.cobro_de_envio_al_cliente(5, 500, 2500) == 5


def test_compra_grande_con_envio_barato_va_gratis():
    # $3,000 y una guía de $120 → es el 4%, cabe en el 5%: lo absorbe la casa.
    assert envios.cobro_de_envio_al_cliente(120, 3000, 2500) == 0
    # Y con la guía de $250 de siempre, el 5% no la tapa hasta los $5,000.
    assert envios.cobro_de_envio_al_cliente(250, 5000, 2500) == 0


def test_el_piso_de_absorcion_es_250_o_el_5_por_ciento_lo_mayor():
    """LA REGLA DEL 2026-08-02 (Christián): desde la mínima, la casa absorbe la guía
    hasta $250 o el 5% de la compra, LO QUE SEA MAYOR. Con guías reales de $139-$250
    eso es «gratis parejo desde $2,500» — la franja parcial de antes ya no existe."""
    assert envios.PISO_ABSORCION_MXN == 250
    assert envios.cobro_de_envio_al_cliente(250, 2500, 2500) == 0      # piso: 250
    assert envios.cobro_de_envio_al_cliente(250, 3000, 2500) == 0
    assert envios.cobro_de_envio_al_cliente(250, 4000, 2500) == 0
    assert envios.cobro_de_envio_al_cliente(165, 2600, 2500) == 0      # la guía real
    # El piso es exacto: $251 de guía en una compra chica ya deja $1 al cliente.
    assert envios.cobro_de_envio_al_cliente(251, 2500, 2500) == 1


def test_arriba_del_piso_el_cliente_paga_SOLO_la_diferencia():
    """El candado del paquete monstruoso (su ejemplo: compra de $40,000 con guía de
    $4,000 → la casa absorbe hasta $2,000 y el cliente pone el resto). En compras
    donde el 5% no llega a $250, manda el piso de $250."""
    assert envios.CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE is False
    assert envios.cobro_de_envio_al_cliente(4000, 40000, 2500) == 2000   # casa: 2000 (5%)
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500) == 350      # casa: 250 (piso)


def test_la_casa_nunca_absorbe_mas_del_piso():
    """El otro lado de la misma regla: por cara que salga la guía, la casa se queda
    topada en max($250, 5% de la compra)."""
    assert envios.cobro_de_envio_al_cliente(2000, 3000, 2500) == 1750   # casa: 250
    assert envios.cobro_de_envio_al_cliente(2000, 10000, 2500) == 1500  # casa: 500 (5%)


def test_la_tarifa_plana_es_un_PRECIO_y_solo_manda_abajo_de_la_minima():
    """`tarifa_plana` existe porque lo que se COBRA abajo de la mínima no tiene por qué
    ser lo que la guía CUESTA. Sin ella (el camino de Skydropx) el cliente paga la
    guía real, que es justo lo que se cotizó."""
    # Abajo de la mínima manda la tarifa, no el costo.
    assert envios.cobro_de_envio_al_cliente(400, 879, 2500, tarifa_plana=219) == 219
    assert envios.cobro_de_envio_al_cliente(80, 879, 2500, tarifa_plana=219) == 219
    # Arriba de la mínima la tarifa no pinta nada: ahí manda el piso contra el costo real.
    assert envios.cobro_de_envio_al_cliente(120, 3000, 2500, tarifa_plana=219) == 0
    assert envios.cobro_de_envio_al_cliente(600, 3000, 2500, tarifa_plana=219) == 350
    # Sin tarifa: el comportamiento de siempre, se cobra la guía completa.
    assert envios.cobro_de_envio_al_cliente(400, 879, 2500) == 400
    # Y una tarifa con basura no puede dejar el envío en un número raro.
    assert envios.cobro_de_envio_al_cliente(400, 879, 2500, tarifa_plana='x') == 400
    assert envios.cobro_de_envio_al_cliente(400, 879, 2500, tarifa_plana=-9) == 400


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


def test_el_tope_vive_en_un_solo_lugar():
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
    """El pedido dice que el envío cuesta $1. El servidor cobra la POLÍTICA de la
    casa (2026-08-02): $250 parejo abajo de la mínima. Y la guía real ($52.45, la
    más barata de las permitidas) queda guardada como COSTO, que es otro número."""
    _falsear_skydropx(monkeypatch)
    payload = _pedido(shipping_mentiroso=1)
    cobrado, guardado = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 250                      # la política, no el navegador
    assert guardado['cost'] == 52.45           # lo que la guía cuesta de verdad
    assert guardado['express'] is False


def test_un_id_de_cotizacion_inventado_no_regala_el_envio(db, con_llave, monkeypatch):
    _falsear_skydropx(monkeypatch)
    payload = _pedido(quote_id='me-lo-invente', shipping_mentiroso=0)
    cobrado, _ = asyncio.run(server._envio_del_pedido(payload, 1000, PFLAGS))
    assert cobrado == 250                      # la política; no cobra cero por creerle


def test_el_cliente_ya_no_escoge_paqueteria_escoge_el_tipo(db, con_llave, monkeypatch):
    """LA ESTRATEGIA DEL 2026-08-02: el id de cotización que mande el navegador ya
    no decide el precio — el cliente escoge ESTÁNDAR o EXPRESS y la casa escoge la
    paquetería. Express: +$150 SIEMPRE, y el servicio apuntado es uno de 1-2 días."""
    _falsear_skydropx(monkeypatch)
    normal = _pedido()
    cobrado, guardado = asyncio.run(server._envio_del_pedido(normal, 1000, PFLAGS))
    assert cobrado == 250
    exp = _pedido()
    exp.shipping_express = True
    cobrado_exp, guardado_exp = asyncio.run(server._envio_del_pedido(exp, 1000, PFLAGS))
    assert cobrado_exp == 400                  # 250 + 150
    assert guardado_exp['express'] is True
    assert 0 < int(guardado_exp['days'] or 0) <= envios.DIAS_MAXIMOS_EXPRESS
    # Y en una compra con envío incluido, el extra se cobra igual.
    incluido, _ = asyncio.run(server._envio_del_pedido(exp, 5000, PFLAGS))
    assert incluido == 150


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


def test_una_cotizacion_de_paqueteria_no_permitida_no_se_cobra(db, con_llave, monkeypatch):
    """Si un día se vuelve a restringir la lista, el candado del cobro sigue vivo:
    aunque alguien meta la tarifa a mano en la base, no se cobra ni se compra."""
    monkeypatch.setattr(skydropx, 'PAQUETERIAS_PERMITIDAS', ('estafeta',))
    quote = asyncio.run(server._guardar_cotizacion(
        '64000', {'peso_kg': 1.0}, [{'paqueteria': 'DHL', 'servicio': 'x',
                                     'servicio_codigo': 'x', 'dias': 1, 'precio': 900.0}]))
    assert asyncio.run(server._cotizacion_valida(
        quote['opciones'][0]['opcion_id'], '64000', 1.0)) is None


def test_si_la_paqueteria_no_contesta_la_politica_cobra_igual(db, con_llave, monkeypatch):
    """Antes, con Skydropx caída, el pedido salía con envío $0 — la casa regalaba la
    guía por una falla ajena. Con la política del 2026-08-02 el cobro no depende de
    que un tercero conteste: manda la regla de la casa con su costo estimado."""
    _falsear_skydropx(monkeypatch, fallos={'/quotations': RuntimeError('caida')})
    cobrado, guardado = asyncio.run(server._envio_del_pedido(_pedido(), 1000, PFLAGS))
    assert cobrado == 250                     # la tarifa de la casa, no $0
    assert guardado['cost'] == server.COSTO_GUIA_ESTIMADO
    # Y arriba de la mínima sigue saliendo incluido, como promete la página.
    gratis, _ = asyncio.run(server._envio_del_pedido(_pedido(), 5000, PFLAGS))
    assert gratis == 0


def test_la_cotizacion_en_el_checkout_va_SIEMPRE_prendida():
    """⛔ `COTIZAR_EN_CHECKOUT` no se apaga.

    Orden de Christián del 2026-08-01: «Yo jamás lo apagué. Préndelo y SIEMPRE debe
    estar prendido.» Nació apagado el 28-jul como precaución y se quedó así. El costo
    de ese olvido fue real: `COMPRAR_GUIA_AL_PAGAR` sí estaba prendido, así que la
    casa compraba la guía de cada pedido y no se la cobraba a nadie.

    Si Skydropx se cae no hace falta apagar nada: el módulo se degrada solo (ver el
    caso de abajo, sin credenciales). Apagar el interruptor es decidir NO COBRAR, y
    eso sólo lo decide él.
    """
    assert envios.COTIZAR_EN_CHECKOUT is True


def test_apagado_el_envio_se_comporta_EXACTAMENTE_como_antes(db, monkeypatch):
    """Y si algún día se apaga, el checkout no se rompe: cae a la tarifa plana.

    El interruptor va prendido en la vida real (caso de arriba); aquí se apaga a
    propósito para comprobar que la degradación sigue siendo limpia — cero cargo por
    cotización y nada guardado.
    """
    monkeypatch.setattr(envios, 'COTIZAR_EN_CHECKOUT', False)
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
    assert {o['carrier'].lower() for o in r['options']} == {'estafeta', 'paquetexpress', 'fedex', 'dhl'}
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


def _en_segundo_plano(monkeypatch):
    """Atrapa lo que se manda a segundo plano y devuelve la lista para correrlo.

    ⛔ POR QUÉ NO ALCANZA `create_task = lambda c: c`. Ese truco servía mientras lo que
    colgaba de la confirmación eran funciones sueltas; desde que el pago manda a
    `_confirmar_y_avisar` —que primero compra la guía y DESPUÉS manda un solo correo—
    lo que se agenda es una corrutina de verdad, y una corrutina que nadie espera no
    corre nunca. La prueba pasaba en verde sin haber ejecutado nada.
    """
    pendientes = []

    def agendar(coro):
        pendientes.append(coro)
        return coro
    monkeypatch.setattr(server.asyncio, 'create_task', agendar)
    return pendientes


def _correr_pendientes(pendientes):
    async def todo():
        for coro in list(pendientes):
            if asyncio.iscoroutine(coro):
                await coro
    asyncio.run(todo())


def _cazar_guias(monkeypatch, compradas):
    """Sustituye la compra de guía por una que sólo anota. Acepta `avisar=`.

    Desde el 2026-07-31 la compra ya no cuelga directo de la confirmación: pasa por
    `_confirmar_y_avisar`, que primero compra (sin avisar por su cuenta) y DESPUÉS
    manda un solo correo con el pago y el rastreo juntos. La firma lleva `avisar`.
    """
    async def falsa(o, avisar=True):
        compradas.append(o.get('order_number'))
        return None
    monkeypatch.setattr(server, 'comprar_guia_del_pedido', falsa)


def test_los_tres_metodos_de_pasarela_pasan_por_la_confirmacion(db, monkeypatch):
    """Tarjeta, OXXO y cripto confirman por webhook y todos caen en
    `_confirm_paid_order`. Se prueba que ESA es la que dispara la guía."""
    compradas = []
    _cazar_guias(monkeypatch, compradas)
    monkeypatch.setattr(server, 'avisar_al_cliente', _async_nada)
    # De la confirmación también cuelgan el aviso interno y el aviso a Meta
    # (Conversions API). Aquí no se prueban y no deben salir a internet.
    # Async de verdad: ahora que las tareas de segundo plano SÍ se corren, un doble
    # síncrono revienta con «NoneType can't be used in await».
    monkeypatch.setattr(server, 'send_purchase_alert', _async_nada)
    monkeypatch.setattr(server.meta_capi, 'enviar_compra', lambda *a, **k: None)
    pendientes = _en_segundo_plano(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('tarjeta')))
    asyncio.run(server._confirm_paid_order('EX-20260728-0001'))
    _correr_pendientes(pendientes)
    assert compradas == ['EX-20260728-0001']
    assert db.orders.docs[0]['status'] == 'confirmado'


def test_spei_compra_su_guia_cuando_el_admin_confirma_el_deposito(db, monkeypatch):
    """SPEI no tiene webhook: lo confirma el admin a mano. Es el cuarto método y
    tiene que comprar guía igual que los otros tres."""
    compradas = []
    _cazar_guias(monkeypatch, compradas)
    monkeypatch.setattr(server, 'avisar_al_cliente', _async_nada)
    monkeypatch.setattr(server, 'notify', _async_nada)
    pendientes = _en_segundo_plano(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    from models import OrderStatusUpdate
    asyncio.run(server.update_order_status('o1', OrderStatusUpdate(status='confirmado'),
                                           admin={'id': 'admin'}))
    _correr_pendientes(pendientes)
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


def test_sin_capturar_nada_el_remitente_va_VACIO_no_inventado(monkeypatch):
    """⛔ Ni un dato de ejemplo. Antes había un 'PENDIENTE-CONFIGURAR' de relleno;
    ahora el remitente se captura en Admin → Envíos y lo que no se capturó va vacío.
    Un campo con texto de mentira acaba impreso en una guía."""
    monkeypatch.setattr(skydropx, '_DEL_PANEL', {})
    for _c, env in skydropx.CAMPOS_REMITENTE:
        monkeypatch.delenv(env, raising=False)
    r = skydropx.remitente()
    assert r['name'] == '' and r['address1'] == '' and r['zip'] == ''
    assert skydropx.remitente_configurado() is False
    # Lo único que se rellena solo es lo que no es de nadie: la empresa y el país.
    assert r['company'] == 'Exygen Labs' and r['country'] == 'MX'


def test_el_remitente_se_captura_desde_el_panel_y_el_entorno_le_gana(monkeypatch):
    """Misma regla que las llaves de cobro: el .env manda sobre lo pegado en el panel."""
    monkeypatch.setattr(skydropx, '_DEL_PANEL', {})
    for _c, env in skydropx.CAMPOS_REMITENTE:
        monkeypatch.delenv(env, raising=False)
    skydropx.cargar_remitente_del_panel({
        'name': 'Trabajador del Panel', 'address1': 'Calle 1 #2', 'colonia': 'Centro',
        'city': 'Monterrey', 'province': 'Nuevo León', 'zip': '64000',
        'phone': '8112345678', 'email': 'envios@exygenlabs.com'})
    assert skydropx.remitente_configurado() is True
    assert skydropx.remitente()['name'] == 'Trabajador del Panel'
    assert skydropx.origen_del_remitente() == 'panel'

    monkeypatch.setenv('SKYDROPX_FROM_NAME', 'El del servidor')
    assert skydropx.remitente()['name'] == 'El del servidor'
    assert skydropx.origen_del_remitente() == 'servidor'


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
#  8. EL TOPE TAMBIÉN EN LA TARIFA PLANA (Christian, 2026-07-28; tope al 5% el 07-31)
#
#  «Envío gratis arriba de $2,500 PERO con tope: si una compra por 2,500 genera un
#  costo de envío de $500 ni en pedo lo pago.»
#
#  El camino de la tarifa plana (`shipping_for`) tenía su PROPIA cuenta —"gratis
#  arriba de $2,500"— que nunca miraba lo que la guía costaba de verdad. El tope
#  existía sólo en el camino de Skydropx. Ahora hay UNA sola regla.
# ==========================================================================
def test_la_tarifa_plana_usa_LA_MISMA_regla_del_tope():
    """`shipping_for` no decide nada por su cuenta: delega en la regla de envios.py.
    Lo único que pone son los tres números de la casa — costo de la guía, compra
    mínima y tarifa plana."""
    for compra in (0, 179, 879, 2499, 2500, 3000, 5000, 50000):
        assert server.shipping_for(compra) == envios.cobro_de_envio_al_cliente(
            server.COSTO_GUIA_ESTIMADO, compra, server.FREE_SHIPPING_FROM,
            tarifa_plana=server.SHIPPING_FLAT)


def test_la_tarifa_que_se_cobra_y_el_costo_de_la_guia_son_DOS_numeros():
    """Se separaron el 2026-07-31. Mezclados, bajar el precio al cliente movía solo
    —y en silencio— el punto donde el envío sale gratis: con $200 de "costo" falso el
    gratis empezaría en $4,000 en vez de $5,000 sin que nadie lo decidiera."""
    assert server.SHIPPING_FLAT == 250          # lo que se COBRA abajo de la mínima
    assert server.COSTO_GUIA_ESTIMADO == 250    # lo que la guía CUESTA
    # Bajar la tarifa NO mueve el punto donde el envío es gratis de verdad, ni el
    # piso de absorción: una guía de $600 deja $350 al cliente, con la tarifa que sea.
    assert envios.cobro_de_envio_al_cliente(250, 5000, 2500, tarifa_plana=200) == 0
    assert envios.cobro_de_envio_al_cliente(600, 4000, 2500, tarifa_plana=200) == 350


def test_los_numeros_de_envio_se_pueden_mover_sin_desplegar():
    """Christián dijo «quizás $200 o $219» — un QUIZÁS, no una orden. El día que
    decida se cambia en el .env del servidor, no en el código."""
    assert server._pesos_de_entorno('NO_EXISTE_ESTA_VARIABLE_DE_ENVIO', 250) == 250
    os.environ['ENVIO_DE_PRUEBA'] = '219'
    try:
        assert server._pesos_de_entorno('ENVIO_DE_PRUEBA', 250) == 219
        # Basura o negativo: manda el valor de fábrica. Un envío en $0 por un dedazo
        # en el .env es dinero que se va sin que nadie se entere.
        os.environ['ENVIO_DE_PRUEBA'] = 'doscientos'
        assert server._pesos_de_entorno('ENVIO_DE_PRUEBA', 250) == 250
        os.environ['ENVIO_DE_PRUEBA'] = '-50'
        assert server._pesos_de_entorno('ENVIO_DE_PRUEBA', 250) == 250
        os.environ['ENVIO_DE_PRUEBA'] = '   '
        assert server._pesos_de_entorno('ENVIO_DE_PRUEBA', 250) == 250
    finally:
        os.environ.pop('ENVIO_DE_PRUEBA', None)


def test_un_pedido_de_179_NUNCA_lleva_envio_gratis():
    """$250 de guía sobre $179 de mercancía es el 140% del pedido."""
    assert server.shipping_for(179) == server.SHIPPING_FLAT
    assert envios.cobro_de_envio_al_cliente(250, 179, server.FREE_SHIPPING_FROM) == 250
    # El tope de absorción sólo pinta ARRIBA de la mínima; abajo el pedido paga su
    # tarifa. El piso de $250 no regala nada aquí (regla del 2026-08-02).
    assert envios.tope_que_absorbe_la_casa(179) == 250


def test_una_guia_cara_ya_no_se_regala_por_pasar_el_umbral():
    """$2,600 de compra con una guía REAL de $500: la casa absorbe su piso ($250,
    que es mayor que el 5% = $130) y el cliente paga los otros $250. Antes
    `shipping_for` devolvía 0 para cualquier compra arriba de $2,500."""
    assert server.shipping_for(2600, costo_real=500) == 250
    # La guía normal cabe completa en el piso: gratis parejo desde la mínima.
    assert server.shipping_for(2600, costo_real=250) == 0
    assert server.shipping_for(2600, costo_real=120) == 0


def test_lo_que_la_casa_absorbe_y_cuanto_se_pasa_del_tope():
    # Pedido de $179, guía de $250, envío regalado: la casa se come los $250 enteros
    # — cabe en el piso, así que no cuenta como "fuera de tope" (ya no se grita).
    assert envios.envio_que_absorbe_la_casa(250, 0) == 250
    assert envios.absorcion_fuera_de_tope(250, 179, 0) == 0
    # Una guía de $500 regalada en un pedido de $179: se pasa del piso por $250.
    assert envios.absorcion_fuera_de_tope(500, 179, 0) == 250
    # Pedido de $3,000 con guía de $250 regalada: cabe en el piso, cero exceso.
    assert envios.absorcion_fuera_de_tope(250, 3000, 0) == 0
    # Y si el cliente la pagó completa, la casa no absorbe nada.
    assert envios.envio_que_absorbe_la_casa(250, 250) == 0
    assert envios.absorcion_fuera_de_tope(250, 179, 250) == 0


def test_las_funciones_del_tope_no_revientan_con_basura():
    # Con basura, el tope cae al PISO de la casa ($250), nunca a un número roto.
    assert envios.tope_que_absorbe_la_casa(None) == 250
    assert envios.tope_que_absorbe_la_casa('x') == 250
    assert envios.envio_que_absorbe_la_casa(None, None) == 0
    assert envios.envio_que_absorbe_la_casa('a', 'b') == 0
    assert envios.absorcion_fuera_de_tope(None, None, None) == 0


def test_el_pedido_guarda_lo_que_la_casa_absorbe_aunque_no_cobre_envio():
    """Con el cobro apagado el pedido guardaba costo $0 y absorbido $0: los $250
    que la casa se come no existían en ningún reporte."""
    assert server.COBRAR_ENVIO is True               # política nueva: $250 parejo
    # Pedido de $179: el cliente paga los $250, la casa no absorbe nada.
    cobrado = server.shipping_for(179)
    costo_guia = server.COSTO_GUIA_ESTIMADO
    assert cobrado == 250
    assert envios.envio_que_absorbe_la_casa(costo_guia, cobrado) == 0
    # Pedido de $3,000: GRATIS PAREJO (2026-08-02) — la guía de $250 cabe en el
    # piso de absorción y la casa se la come entera, registrada y dentro de regla.
    assert server.shipping_for(3000) == 0
    assert envios.envio_que_absorbe_la_casa(costo_guia, 0) == 250
    assert envios.absorcion_fuera_de_tope(costo_guia, 3000, 0) == 0
    # Pedido de $5,000: igual de gratis, igual de registrado.
    assert server.shipping_for(5000) == 0
    assert envios.absorcion_fuera_de_tope(costo_guia, 5000, 0) == 0


def test_el_express_suma_su_extra_siempre():
    """EXPRESS (Christián, 2026-08-02): +$150 encima del estándar, SIEMPRE — también
    cuando el envío estándar va incluido. Y sus dos números viven en envios.py."""
    assert envios.EXTRA_EXPRESS_MXN == 150
    assert envios.DIAS_MAXIMOS_EXPRESS == 2
    assert envios.tope_guia_automatica(False) == 400
    assert envios.tope_guia_automatica(True) == 600


# ==========================================================================
#  9. La caja: lo que abulta cuesta más que lo que pesa
#
#  ⛔ ESTA ES LA SECCIÓN DEL PROBLEMA DE LOS $600. El 2026-07-30 Christián mandó
#  dos viales a Nuevo León y le cobraron casi $600 en el mostrador. Las paqueterías
#  cobran por el MAYOR entre el peso real y el volumétrico (L×A×H÷5000), y hasta ese
#  día aquí había una sola caja de 30×20×15 para todo: 1.8 kg volumétricos para un
#  paquete que pesa 0.25. Se cotizaba aire.
# ==========================================================================
def test_dos_viales_van_en_la_caja_CHICA_no_en_la_de_siempre():
    items = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=2)]
    p = envios.paquete_del_pedido(items, {'a': {'name': 'BPC-157'}})
    assert p['caja'] == 'chica'
    assert (p['largo_cm'], p['ancho_cm'], p['alto_cm']) == (20, 15, 10)
    # 0.6 kg volumétricos: por debajo del mínimo de 1 kg, o sea lo más barato posible.
    assert p['peso_volumetrico_kg'] == 0.6
    assert p['peso_kg'] == envios.PESO_MINIMO_KG


def test_la_caja_vieja_cobraba_casi_el_DOBLE_de_peso_volumetrico():
    """La de 30×20×15 son 1.8 kg volumétricos; la chica, 0.6. Tres veces menos."""
    chica, mediana = envios.caja_para(0.2), envios.caja_para(2.0)
    assert envios.peso_volumetrico(mediana) == 1.8
    assert envios.peso_volumetrico(chica) == 0.6
    assert envios.peso_volumetrico(chica) < envios.PESO_MINIMO_KG


def test_un_pedido_grande_sube_de_caja_solo():
    muchos = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=40)]
    p = envios.paquete_del_pedido(muchos, {'a': {'name': 'BPC-157'}})
    assert p['caja'] == 'mediana'           # 2.0 kg de contenido
    assert p['peso_kg'] == 2.3              # 2.0 + los 0.30 de la caja
    gigante = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=100)]
    assert envios.paquete_del_pedido(gigante, {'a': {'name': 'BPC-157'}})['caja'] == 'grande'


def test_las_cajas_se_cambian_desde_el_panel_sin_desplegar(monkeypatch):
    monkeypatch.setattr(envios, '_CAJAS_DEL_PANEL', [])
    assert envios.cajas() == envios.CAJAS               # de fábrica
    envios.cargar_cajas_del_panel([
        {'nombre': 'sobre', 'largo_cm': 25, 'ancho_cm': 18, 'alto_cm': 3,
         'peso_max_kg': 0.5, 'peso_caja_kg': 0.05}])
    assert envios.caja_para(0.2)['nombre'] == 'sobre'
    assert envios.peso_volumetrico(envios.caja_para(0.2)) == 0.27


def test_una_caja_con_medidas_en_cero_NO_se_guarda(monkeypatch):
    """Cotizar contra una caja de 0×0×0 es cotizar contra basura."""
    monkeypatch.setattr(envios, '_CAJAS_DEL_PANEL', [])
    assert envios.cargar_cajas_del_panel([{'nombre': 'mala', 'largo_cm': 0,
                                           'ancho_cm': 10, 'alto_cm': 10}]) == 0
    assert envios.cajas() == envios.CAJAS               # se quedó con las de fábrica


def test_el_peso_del_contenido_no_incluye_la_caja():
    items = [OrderItem(product_id='a', name='BPC-157', price=100, quantity=2)]
    assert envios.peso_del_contenido(items, {'a': {'name': 'BPC-157'}}) == 0.1


# ==========================================================================
#  10. Despachar desde el Panel: cotizar de verdad y comprar con un clic
#
#  Es lo que faltaba de toda la integración. Hasta hoy la única forma de mandar un
#  paquete era ir al mostrador y pagar lo que dijeran.
# ==========================================================================
def _admin():
    return {'id': 'admin', 'role': 'admin'}


def _cotizar_pedido(db, order_id='o1'):
    return asyncio.run(server.admin_cotizar_envio(order_id, admin=_admin()))


def test_el_admin_ve_TODAS_las_paqueterias_no_solo_las_tres_del_cliente(
        db, con_llave, con_remitente, monkeypatch):
    """⛔ Quien paga la guía es la casa. Al cliente se le enseñan tres paqueterías y
    solo las de 2-5 días; al admin se le enseña TODO lo que cotizó Skydropx, porque
    ocultarle la opción de $51 a quien paga es exactamente cómo un envío llega a $600."""
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    assert r['enabled'] is True
    carriers = {o['carrier'] for o in r['options']}
    assert 'DHL' in carriers                       # al cliente NO se le enseña
    assert {'Estafeta', 'FedEx', 'Paquetexpress'} <= carriers
    # Y viene ordenado de más barato a más caro, con la más barata marcada.
    precios = [o['price'] for o in r['options']]
    assert precios == sorted(precios)
    assert precios[0] == 51.25
    # Pero se sigue viendo cuál SÍ cumple la promesa que se le hizo al cliente.
    nacional = next(o for o in r['options'] if o['price'] == 51.25)
    assert nacional['para_el_cliente'] is False    # 7 días: rompe los 2-5 prometidos


def test_la_cotizacion_del_admin_usa_el_peso_y_la_caja_REALES(db, con_llave, monkeypatch):
    api = _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    assert r['paquete']['caja'] == 'chica'          # un vial
    cuerpo = _peticiones(api, '/quotations')[0]['cuerpo']['quotation']
    assert cuerpo['parcel'] == {'length': 20, 'width': 15, 'height': 10, 'weight': 1.0}
    assert cuerpo['address_to']['postal_code'] == '64000'


def test_sin_credenciales_el_panel_lo_dice_y_no_revienta(db, monkeypatch):
    monkeypatch.delenv('SKYDROPX_CLIENT_ID', raising=False)
    monkeypatch.delenv('SKYDROPX_CLIENT_SECRET', raising=False)
    monkeypatch.setattr(server.secretos, '_CACHE', {})
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    assert r['enabled'] is False and r['options'] == []
    assert 'SKYDROPX_CLIENT_ID' in r['detail']


def test_comprar_la_guia_desde_el_panel_la_deja_en_el_pedido(
        db, con_llave, con_remitente, monkeypatch):
    api = _falsear_skydropx(monkeypatch)
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    barata = r['options'][0]
    from models import ComprarGuiaRequest
    pedido = asyncio.run(server.admin_comprar_guia(
        'o1', ComprarGuiaRequest(option_id=barata['id']), admin=_admin()))

    assert pedido['tracking_number'] == '7712345678'
    assert pedido['label_url'] == 'https://skydropx.test/guia.pdf'
    assert pedido['status'] == 'enviado'
    assert pedido['shipping_cost'] == 51.25          # lo que le costó A LA CASA
    # ⛔ Se compró contra el rate_id de LA COTIZACIÓN, no contra nada que mandó el panel.
    envio = _peticiones(api, '/shipments')[0]['cuerpo']['shipment']
    assert envio['rate_id'] == 'r-pqx-nac'


def test_el_precio_de_la_guia_lo_pone_el_SERVIDOR(db, con_llave, con_remitente, monkeypatch):
    """El panel solo dice CUÁL opción. El precio sale de la cotización guardada."""
    _falsear_skydropx(monkeypatch)
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    cara = max(r['options'], key=lambda o: o['price'])
    from models import ComprarGuiaRequest
    pedido = asyncio.run(server.admin_comprar_guia(
        'o1', ComprarGuiaRequest(option_id=cara['id']), admin=_admin()))
    guardada = next(o for o in db[server.COLECCION_COTIZACIONES].docs[0]['opciones']
                    if o['opcion_id'] == cara['id'])
    assert pedido['shipping_cost'] == guardada['precio']


def test_una_opcion_de_OTRO_pedido_no_compra_guia(db, con_llave, con_remitente, monkeypatch):
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    asyncio.run(db.orders.insert_one(_orden('spei', id='o2', order_number='EX-2')))
    r = _cotizar_pedido(db, 'o1')
    from models import ComprarGuiaRequest
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.admin_comprar_guia(
            'o2', ComprarGuiaRequest(option_id=r['options'][0]['id']), admin=_admin()))
    assert e.value.status_code == 400


def test_sin_remitente_el_panel_TAMPOCO_compra(db, con_llave, monkeypatch):
    monkeypatch.setattr(skydropx, '_DEL_PANEL', {})
    for _c, env in skydropx.CAMPOS_REMITENTE:
        monkeypatch.delenv(env, raising=False)
    api = _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    assert r['remitente_completo'] is False
    from models import ComprarGuiaRequest
    n = len(api.llamadas)
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.admin_comprar_guia(
            'o1', ComprarGuiaRequest(option_id=r['options'][0]['id']), admin=_admin()))
    assert e.value.status_code == 400 and 'remitente' in e.value.detail.lower()
    assert len(api.llamadas) == n              # ni le habló a la paquetería


def test_un_pedido_que_ya_tiene_guia_no_se_compra_otra(db, con_llave, con_remitente, monkeypatch):
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_orden('spei')))
    r = _cotizar_pedido(db)
    asyncio.run(db.orders.update_one({'id': 'o1'}, {'$set': {'tracking_number': '999'}}))
    from models import ComprarGuiaRequest
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.admin_comprar_guia(
            'o1', ComprarGuiaRequest(option_id=r['options'][0]['id']), admin=_admin()))
    assert e.value.status_code == 409


# ==========================================================================
#  11. El remitente y las cajas se capturan en el Panel
# ==========================================================================
def test_el_panel_guarda_el_remitente_y_ahi_si_se_puede_comprar(db, monkeypatch):
    monkeypatch.setattr(skydropx, '_DEL_PANEL', {})
    for _c, env in skydropx.CAMPOS_REMITENTE:
        monkeypatch.delenv(env, raising=False)
    from models import RemitenteUpdate
    assert skydropx.remitente_configurado() is False
    asyncio.run(server.admin_guardar_remitente(RemitenteUpdate(
        name='Trabajador de Prueba', address1='Calle 1 #2', colonia='Centro',
        city='Monterrey', province='Nuevo León', zip='64000',
        phone='8112345678', email='envios@exygenlabs.com'), admin=_admin()))
    assert skydropx.remitente_configurado() is True
    assert skydropx.remitente()['zip'] == '64000'


def test_el_remitente_NO_va_horneado_en_el_codigo():
    """⛔ Es el domicilio de un trabajador. Un dato personal en el repositorio es un
    dato personal publicado en GitHub."""
    import re
    fuente = open('skydropx.py', encoding='utf-8').read()
    assert '@gmail.com' not in fuente
    assert not re.search(r'\b\d{10}\b', fuente)          # ningún teléfono
    # Lo ÚNICO que puede venir relleno es lo que no es de nadie: la empresa y el país.
    assert set(skydropx.REMITENTE_POR_OMISION) == {'company', 'country', 'reference'}


def test_la_config_de_envios_no_devuelve_las_llaves_de_skydropx(db, con_llave):
    r = asyncio.run(server.admin_envios_config(admin=_admin()))
    assert r['credenciales_puestas'] is True
    assert 'id-de-prueba' not in str(r) and 'secreto-de-prueba' not in str(r)
    assert [c['nombre'] for c in r['cajas']] == ['chica', 'mediana', 'grande']


# ==========================================================================
#  12. El cliente se entera de su guía por correo
# ==========================================================================
def test_al_comprar_la_guia_le_llega_el_rastreo_al_cliente(
        db, con_llave, con_remitente, monkeypatch):
    """El correo de pago confirmado PROMETE el número de guía por correo, en los tres
    idiomas. Hasta hoy ese correo no existía."""
    _falsear_skydropx(monkeypatch)
    enviados = []
    monkeypatch.setattr(server, 'send_shipped_email',
                        lambda o, lang=None: enviados.append(o.get('tracking_number')))
    monkeypatch.setattr(server.asyncio, 'create_task', lambda c: c)
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    orden = _orden('tarjeta')
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.comprar_guia_del_pedido(orden))
    assert enviados == ['7712345678']


def test_capturar_la_guia_A_MANO_tambien_le_avisa_al_cliente(db, monkeypatch):
    """El admin puede seguir pegando una guía comprada en el mostrador. El cliente
    tiene que enterarse igual."""
    avisados = []
    monkeypatch.setattr(server, 'avisar_del_envio',
                        lambda o: avisados.append(o.get('tracking_number')) or _async_nada())
    asyncio.run(db.orders.insert_one(_orden('spei')))
    from models import OrderShippingUpdate
    asyncio.run(server.update_order_shipping('o1', OrderShippingUpdate(
        carrier='Estafeta', tracking_number='ABC123'), admin=_admin()))
    assert avisados == ['ABC123']


def test_la_guia_capturada_SIN_paqueteria_la_deduce_el_servidor(db, monkeypatch):
    """⛔ UNA GUÍA SIN PAQUETERÍA NO SE PUEDE RASTREAR (Christián, 2026-07-31).

    La pantalla que captura guías ya adivina la paquetería mientras se teclea, pero
    esta ruta se puede llamar sin pasar por ahí —el distribuidor, un script, la app de
    mañana— y entonces el pedido queda con número y sin transportista: ni liga de
    rastreo, ni eventos, ni forma de saber a quién preguntarle. El servidor la deduce
    del propio número, que es exactamente lo que hace la pantalla (`guias.py`).
    """
    monkeypatch.setattr(server, 'avisar_del_envio', lambda o: _async_nada())
    asyncio.run(db.orders.insert_one(_orden('spei')))
    from models import OrderShippingUpdate
    # 12 dígitos: FedEx y sólo FedEx. Sin `carrier` en el cuerpo, a propósito.
    asyncio.run(server.update_order_shipping('o1', OrderShippingUpdate(
        tracking_number='875122824121'), admin=_admin()))
    guardado = asyncio.run(db.orders.find_one({'id': 'o1'}))
    assert guardado['carrier'] == 'FedEx'
    # Y con paquetería ya hay liga a dónde mandar al cliente.
    assert guardado['tracking_url'].startswith('https://www.fedex.com/')


def test_lo_que_SI_capturaron_a_mano_manda_sobre_la_deduccion(db, monkeypatch):
    """La deducción sólo rellena huecos. Si quien captura eligió la paquetería —porque
    sabe algo que el formato no dice— no se le corrige."""
    monkeypatch.setattr(server, 'avisar_del_envio', lambda o: _async_nada())
    asyncio.run(db.orders.insert_one(_orden('spei')))
    from models import OrderShippingUpdate
    asyncio.run(server.update_order_shipping('o1', OrderShippingUpdate(
        carrier='DHL', tracking_number='875122824121'), admin=_admin()))
    assert asyncio.run(db.orders.find_one({'id': 'o1'}))['carrier'] == 'DHL'


def test_el_correo_del_rastreo_existe_en_los_TRES_idiomas():
    import emails
    for lang in ('es', 'en', 'pt'):
        assert '{number}' in emails.SHIPPED_SUBJECTS[lang]
        greet, body, cta, footer = emails.SHIPPED_BODIES[lang]
        assert '{tracking}' in body and '{carrier}' in body and cta


def test_sin_numero_de_guia_no_se_manda_correo_de_envio(db, monkeypatch):
    """Un correo que dice "ya salió" sin número de guía es peor que no mandarlo."""
    mandados = []
    monkeypatch.setattr(server, 'send_shipped_email', lambda *a, **k: mandados.append(1))
    assert asyncio.run(server.avisar_del_envio({'order_number': 'EX-1'})) is False
    assert mandados == []


def test_el_aviso_interno_dice_lo_que_costo_la_guia():
    """El número que duele es el que la casa NO cobra."""
    import emails
    orden = _orden('spei')
    orden.update({'shipping': 0, 'carrier': 'Estafeta', 'shipping_cost': 168.33})
    html_aviso = emails._aviso_compra_html(orden, 'https://exygenlabs.com/admin')
    assert 'Costo de la guía' in html_aviso
    assert '168' in html_aviso
    assert 'la casa pone' in html_aviso        # los $168 que nadie pagó


# ==========================================================================
#  13. El DISTRIBUIDOR captura la guía de SUS pedidos
#
#  Christián, 2026-07-30: María atiende a sus clientes y despacha sus paquetes.
#  Hasta hoy el número de guía sólo lo podía teclear el admin, así que cada
#  envío suyo tenía que pasar por Christián para que el cliente se enterara.
#
#  ⛔ LO CARO NO ES QUE ELLA CAPTURE: es que capture en el pedido de OTRO. Todo
#  lo que sigue prueba el candado EN EL SERVIDOR, no en la pantalla.
# ==========================================================================
import auth as _auth
from models import DistributorShippingUpdate

MARIA = {'id': 'maria', 'role': 'distributor', 'name': 'María'}
OTRA = {'id': 'otra', 'role': 'distributor', 'name': 'Otra Distribuidora'}


def _pedido_de(dist_id, **extra):
    return _orden('spei', referred_by=dist_id, **extra)


def _capturar(order_number, dist, **campos):
    return asyncio.run(server.distributor_order_shipping(
        order_number, DistributorShippingUpdate(**campos), dist=dist))


def test_la_distribuidora_captura_la_guia_de_SU_pedido(db, monkeypatch):
    """Lo que María necesita: paquetería y número. La URL de rastreo se arma sola."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    ficha = _capturar('EX-20260728-0001', MARIA,
                      carrier='Estafeta', tracking_number='ABC123')
    assert ficha['carrier'] == 'Estafeta'
    assert ficha['tracking_number'] == 'ABC123'
    assert 'ABC123' in ficha['tracking_url']        # armada por el servidor
    # Capturar una guía ES que ya salió: el pedido pasa solo a 'enviado'.
    assert ficha['status'] == 'enviado' and ficha['shipped_at']
    # Y sigue viendo SU comisión, no el margen de la casa.
    assert 'my_commission' in ficha


def test_la_distribuidora_puede_pegar_su_propia_url_de_rastreo(db, monkeypatch):
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    ficha = _capturar('EX-20260728-0001', MARIA, carrier='Mensajería Local',
                      tracking_number='XY-9', tracking_url='https://mensajeria.test/XY-9')
    assert ficha['tracking_url'] == 'https://mensajeria.test/XY-9'


def test_el_pedido_de_OTRA_distribuidora_da_403(db, monkeypatch):
    """⛔ EL CANDADO. Esconder el formulario en la pantalla no sirve: el número de
    pedido ajeno se teclea en la barra de direcciones."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('otra')))
    with pytest.raises(server.HTTPException) as e:
        _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ROBADA')
    assert e.value.status_code == 403
    # Y el pedido ajeno quedó INTACTO: ni guía, ni estatus movido.
    quedo = asyncio.run(db.orders.find_one({'order_number': 'EX-20260728-0001'}))
    assert not quedo.get('tracking_number')
    assert quedo['status'] == 'pendiente'


def test_un_pedido_SIN_codigo_de_nadie_tampoco_es_suyo(db, monkeypatch):
    """Un pedido que entró sin código de distribuidor no le pertenece a ninguno."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_orden('spei')))       # sin referred_by
    with pytest.raises(server.HTTPException) as e:
        _capturar('EX-20260728-0001', MARIA, tracking_number='X')
    assert e.value.status_code == 403


def test_un_pedido_que_no_existe_da_404(db):
    with pytest.raises(server.HTTPException) as e:
        _capturar('EX-NO-EXISTE', MARIA, tracking_number='X')
    assert e.value.status_code == 404


def test_un_cliente_normal_NO_captura_guias():
    """El candado del rol vive en la dependencia, antes de tocar el pedido."""
    for rol in ('user', 'marketing'):
        with pytest.raises(server.HTTPException) as e:
            asyncio.run(_auth.get_current_distributor(user={'id': 'x', 'role': rol}))
        assert e.value.status_code == 403


def test_el_VER_COMO_del_admin_NO_escribe_guias(db, monkeypatch):
    """Espiar el panel de María es SOLO LECTURA. Si el 'ver como' pudiera capturar,
    dejaría de ser una mirada y sería una firma con la mano de otro."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    espiando = dict(MARIA, view_as=True, view_as_admin='admin')
    with pytest.raises(server.HTTPException) as e:
        _capturar('EX-20260728-0001', espiando, carrier='Estafeta', tracking_number='NO')
    assert e.value.status_code == 403
    quedo = asyncio.run(db.orders.find_one({'order_number': 'EX-20260728-0001'}))
    assert not quedo.get('tracking_number')


def test_la_distribuidora_NO_puede_mover_el_estatus_ni_el_dinero():
    """El modelo del distribuidor no tiene dónde recibir `status`, `total`, `paid`
    ni `shipping_cost`: aunque los mande, se caen antes de llegar al pedido."""
    campos = set(DistributorShippingUpdate.model_fields)
    assert campos == {'carrier', 'tracking_number', 'tracking_url'}
    entrada = DistributorShippingUpdate(**{'carrier': 'Estafeta', 'tracking_number': '1',
                                           'status': 'entregado', 'total': 1,
                                           'paid': True, 'shipping_cost': 0})
    assert not hasattr(entrada, 'status') and not hasattr(entrada, 'total')


def test_capturar_no_marca_el_pedido_como_pagado(db, monkeypatch):
    """Mover la mercancía NO cobra: un pedido puede ir en camino y seguir debiendo."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('maria', paid=False)))
    _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ABC123')
    quedo = asyncio.run(db.orders.find_one({'order_number': 'EX-20260728-0001'}))
    assert quedo.get('paid') is False and not quedo.get('paid_at')


def test_al_capturar_la_distribuidora_le_sale_el_correo_al_cliente(db, monkeypatch):
    """El punto de todo esto: que el cliente reciba su rastreo sin esperar al admin."""
    avisados = []
    monkeypatch.setattr(server, 'avisar_del_envio',
                        lambda o: avisados.append(o.get('tracking_number')) or _async_nada())
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ABC123')
    assert avisados == ['ABC123']


def test_corregir_la_guia_no_manda_un_SEGUNDO_correo(db, monkeypatch):
    """Se equivocó de dígito y la corrige: el cliente no recibe dos "ya salió"."""
    avisados = []
    monkeypatch.setattr(server, 'avisar_del_envio',
                        lambda o: avisados.append(o.get('tracking_number')) or _async_nada())
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ABC123')
    _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ABC124')
    assert avisados == ['ABC123']


def test_el_correo_del_cliente_es_EL_MISMO_venga_del_admin_o_del_distribuidor():
    """Los dos caminos escriben por la MISMA función. Si un día se arregla el correo,
    queda arreglado para los dos; y nadie puede añadirle un campo a uno solo."""
    import inspect
    for fn in (server.update_order_shipping, server.distributor_order_shipping):
        assert '_guardar_envio' in inspect.getsource(fn)
    cuerpo = inspect.getsource(server.distributor_order_shipping)
    assert 'permitir_status=False' in cuerpo, 'el distribuidor no mueve el estatus a mano'
    assert 'deny_view_as' in cuerpo
    assert "o.get('referred_by') != dist['id']" in cuerpo


def test_COTIZAR_Y_COMPRAR_guias_sigue_siendo_SOLO_del_admin():
    """⛔ Comprar guía es DINERO DE LA CASA. El distribuidor captura la guía que ya
    tiene; no le abre la chequera a nadie."""
    import inspect
    for fn in (server.admin_cotizar_envio, server.admin_comprar_guia):
        assert 'get_current_admin' in str(inspect.signature(fn))
    rutas = [r.path for r in server.app.routes
             if getattr(r, 'path', '').startswith('/api/distributor')]
    assert not any('guia' in p or 'cotizar' in p for p in rutas), \
        f'se coló una ruta de compra de guías en el panel del distribuidor: {rutas}'


def test_la_lista_de_pedidos_del_distribuidor_LLEVA_la_guia(db, monkeypatch):
    """⛔ REGRESIÓN. Iba sólo la paquetería, no el número: la columna "Envío" del
    panel leía un `tracking_number` que nunca llegaba, así que TODOS los pedidos se
    veían "Sin guía todavía" —incluso los que ya iban en camino."""
    monkeypatch.setattr(server, 'avisar_del_envio', _async_nada)
    asyncio.run(db.orders.insert_one(_pedido_de('maria')))
    _capturar('EX-20260728-0001', MARIA, carrier='Estafeta', tracking_number='ABC123')
    fila = asyncio.run(server.distributor_orders(dist=MARIA))[0]
    assert fila['tracking_number'] == 'ABC123'
    assert 'ABC123' in fila['tracking_url']
    assert fila['carrier'] == 'Estafeta'
    # Y la privacidad del cliente sigue en pie: ni correo, ni teléfono, ni domicilio.
    for prohibido in ('customer_email', 'customer_phone', 'address', 'items'):
        assert prohibido not in fila
