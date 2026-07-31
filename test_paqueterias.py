"""EL DOBLE COTIZADOR: cotizar en dos lados y contratar el más barato.

Lo que cuidan estas pruebas, en orden de cuánto duele si se rompe:

  1. QUE SIN LLAVES DE enviosinternacionales NO CAMBIE ABSOLUTAMENTE NADA. Christián
     todavía no abre esa cuenta. Si prender este código cambiara el comportamiento de
     hoy, habría que apagarlo — un despacho que se rompe por una integración que nadie
     pidió todavía es la peor forma de estrenar una función.
  2. QUE SE COMPRE CON EL PROVEEDOR QUE COTIZÓ. Un `rate_id` sólo vale en la casa que
     lo emitió: comprarlo en la otra es una guía mal pagada.
  3. QUE GANE EL MÁS BARATO, venga de quien venga.
  4. QUE UN PROVEEDOR CAÍDO NO TUMBE EL DESPACHO. Si uno truena, se despacha con el otro.

⛔ Nunca se llama a ninguna paquetería de verdad: todo son dobles de prueba.
"""
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import enviosinternacionales as EI
import paqueterias
import skydropx


# ==========================================================================
#  Dobles: dos proveedores que hablan la misma API con precios distintos
# ==========================================================================
def _tarifa(rid, proveedor, mostrar, servicio, codigo, dias, total, ok=True):
    return {'success': ok, 'id': rid, 'provider_name': proveedor,
            'provider_display_name': mostrar, 'provider_service_name': servicio,
            'provider_service_code': codigo, 'days': dias,
            'currency_code': 'MXN' if ok else None,
            'amount': total if ok else None, 'total': total if ok else None,
            'requires_origin_verification': False}


# Skydropx: lo que de verdad devolvió el servicio el 2026-07-28.
TARIFAS_SKY = [
    _tarifa('sky-est', 'estafeta', 'Estafeta', 'Terrestre', 'estafeta_standard', 3, '168.33'),
    _tarifa('sky-fdx', 'fedex', 'FedEx', 'Standard Overnight', 'standard_overnight', 2, '179.20'),
]
# El revendedor: MÁS BARATO en Estafeta, más caro en FedEx. Justo el caso que hace que
# valga la pena preguntar en los dos lados en vez de casarse con uno.
TARIFAS_EI = [
    _tarifa('ei-est', 'estafeta', 'Estafeta', 'Terrestre', 'estafeta_standard', 3, '139.00'),
    _tarifa('ei-fdx', 'fedex', 'FedEx', 'Standard Overnight', 'standard_overnight', 2, '210.00'),
]

PAQUETES = [{'package_number': 1, 'weight': '1.0'}]
GUIA_OK = {'data': {'id': 'ship-1'},
           'included': [{'attributes': {'tracking_number': '77123',
                                        'label_url': 'https://x.test/g.pdf'}}]}

DESTINO = {'name': 'Cliente', 'address1': 'Calle 1', 'city': 'Monterrey',
           'province': 'Nuevo León', 'zip': '64000', 'country': 'MX',
           'phone': '8112345678', 'email': 'c@test.mx', 'colonia': 'Centro'}
PAQUETE = {'peso_kg': 1.0, 'largo_cm': 20, 'ancho_cm': 15, 'alto_cm': 10}


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, str(payload)[:200]

    def json(self):
        return self._p


class ApiFalsa:
    """Un proveedor de mentira con la forma de la API PRO: OAuth + cotización + guía."""

    def __init__(self, tarifas, revienta=False):
        self.tarifas, self.revienta = tarifas, revienta
        self.compras = []

    def _ruta(self, url):
        return url.split('/api/v1', 1)[-1] if '/api/v1' in url else url

    def post(self, url, headers=None, json=None, timeout=None):
        ruta = self._ruta(url)
        if ruta == '/oauth/token':
            return FakeResp({'access_token': 'tok', 'expires_in': 7200})
        if self.revienta:
            return FakeResp({'message': 'caido'}, 500)
        if ruta == '/quotations':
            return FakeResp({'id': 'q-1', 'is_completed': True, 'packages': PAQUETES,
                             'rates': self.tarifas})
        # ⛔ LAS DOS RUTAS SON DISTINTAS Y ASÍ TIENE QUE SER. Skydropx compra en
        # `/shipments`; enviosinternacionales.com en `/shipments/` CON diagonal (así lo
        # marca su OpenAPI: sin ella la ruta sólo acepta GET). El doble de prueba las
        # distingue a propósito, para que el día que alguien "limpie" la diagonal la
        # compra se caiga aquí y no con una guía de verdad de por medio.
        if ruta in ('/shipments', '/shipments/'):
            self.compras.append({'ruta': ruta, **(json or {})})
            return FakeResp(GUIA_OK)
        return FakeResp({}, 404)

    def get(self, url, headers=None, timeout=None):
        ruta = self._ruta(url)
        if self.revienta:
            return FakeResp({'message': 'caido'}, 500)
        if ruta.startswith('/quotations/'):
            return FakeResp({'id': 'q-1', 'is_completed': True, 'packages': PAQUETES,
                             'rates': self.tarifas})
        if ruta.startswith('/shipments/'):
            return FakeResp(GUIA_OK)
        return FakeResp({}, 404)


@pytest.fixture(autouse=True)
def _sin_tokens():
    skydropx.olvidar_token()
    EI.olvidar_token()
    yield
    skydropx.olvidar_token()
    EI.olvidar_token()


# ⛔ SE SUSTITUYE `modulo.requests` ENTERO, NO SUS FUNCIONES. `skydropx.requests` y
# `enviosinternacionales.requests` son EL MISMO objeto (los dos hacen `import requests`),
# así que parchar `requests.post` en uno se lo pisa al otro y el segundo doble se comía al
# primero: Skydropx devolvía las tarifas del revendedor. Cambiando el nombre `requests`
# dentro de cada módulo, cada uno se queda con el suyo — que es justo lo que hay que
# probar aquí, dos proveedores contestando cosas distintas al mismo tiempo.
def _falsear(monkeypatch, modulo, api):
    class _Requests:
        post, get = staticmethod(api.post), staticmethod(api.get)
    monkeypatch.setattr(modulo, 'requests', _Requests)
    monkeypatch.setattr(modulo, 'ESPERA_ENTRE_CONSULTAS_S', 0.01)
    return api


@pytest.fixture()
def sky(monkeypatch):
    """Skydropx encendido, como está hoy en producción."""
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'sky-id')
    monkeypatch.setenv('SKYDROPX_CLIENT_SECRET', 'sky-secreto')
    return _falsear(monkeypatch, skydropx, ApiFalsa(TARIFAS_SKY))


@pytest.fixture()
def ei(monkeypatch):
    """enviosinternacionales.com encendido. ⚠️ Hoy en la vida real está APAGADO."""
    monkeypatch.setenv('ENVIOSINT_CLIENT_ID', 'ei-id')
    monkeypatch.setenv('ENVIOSINT_CLIENT_SECRET', 'ei-secreto')
    return _falsear(monkeypatch, EI, ApiFalsa(TARIFAS_EI))


@pytest.fixture()
def con_remitente(monkeypatch):
    for k, v in {'NAME': 'Trabajador', 'ADDRESS1': 'Calle 1', 'CITY': 'Playa del Carmen',
                 'PROVINCE': 'Quintana Roo', 'ZIP': '77710', 'COLONIA': 'Centro',
                 'PHONE': '9841234567', 'EMAIL': 'envios@exygenlabs.com'}.items():
        monkeypatch.setenv(f'SKYDROPX_FROM_{k}', v)
    return True


# ==========================================================================
#  1. SIN LLAVES DEL SEGUNDO PROVEEDOR, TODO SE COMPORTA COMO HOY
# ==========================================================================
def test_sin_llaves_enviosinternacionales_esta_apagado():
    """Es el estado REAL de hoy: Christián no ha abierto la cuenta."""
    assert EI.enabled() is False


def test_apagado_no_cotiza_ni_estorba(sky, monkeypatch):
    """⛔ LA PRUEBA QUE MÁS IMPORTA. Con el segundo proveedor sin llaves, el resultado
    tiene que ser EXACTAMENTE el de Skydropx solo: las mismas tarifas y ni una llamada
    de más. Estrenar una integración no puede cambiar cómo se despacha hoy."""
    monkeypatch.delenv('ENVIOSINT_CLIENT_ID', raising=False)
    monkeypatch.delenv('ENVIOSINT_CLIENT_SECRET', raising=False)
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert [o['rate_id'] for o in comp['opciones']] == ['sky-est', 'sky-fdx']
    apagado = [p for p in comp['proveedores'] if p['clave'] == EI.CLAVE][0]
    assert apagado['activo'] is False and apagado['detalle'] == 'sin credenciales'
    # y no se le puede haber preguntado nada: no tiene con qué autenticarse
    assert paqueterias.ahorro(comp)['comparados'] == 1


def test_sin_ningun_proveedor_no_hay_cotizacion(monkeypatch):
    """Sin llaves de nadie no se revienta: se devuelve vacío y el pedido se despacha
    a mano, exactamente como antes de que existiera todo esto."""
    for v in ('SKYDROPX_CLIENT_ID', 'SKYDROPX_CLIENT_SECRET',
              'ENVIOSINT_CLIENT_ID', 'ENVIOSINT_CLIENT_SECRET'):
        monkeypatch.delenv(v, raising=False)
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert comp['opciones'] == []
    assert paqueterias.cuantos_activos() == 0


# ==========================================================================
#  2. CON LOS DOS: GANA EL MÁS BARATO, VENGA DE QUIEN VENGA
# ==========================================================================
def test_las_tarifas_de_los_dos_se_juntan_y_se_ordenan_por_precio(sky, ei):
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    precios = [o['precio'] for o in comp['opciones']]
    assert precios == sorted(precios), 'las tarifas tienen que salir de barata a cara'
    assert len(comp['opciones']) == 4, 'tienen que estar las de los DOS proveedores'
    # la más barata de todas es la del revendedor: $139 contra los $168.33 de Skydropx
    assert comp['opciones'][0]['rate_id'] == 'ei-est'
    assert comp['opciones'][0]['proveedor'] == EI.CLAVE


def test_cada_tarifa_dice_de_quien_es(sky, ei):
    """Sin esta etiqueta no se puede comprar en la casa correcta."""
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    de_quien = {o['rate_id']: o['proveedor'] for o in comp['opciones']}
    assert de_quien == {'ei-est': EI.CLAVE, 'ei-fdx': EI.CLAVE,
                        'sky-est': 'skydropx', 'sky-fdx': 'skydropx'}


def test_el_ahorro_de_preguntar_en_dos_lados_se_mide(sky, ei):
    """Para que el día que Christián se pregunte si valió la pena abrir la segunda
    cuenta, el número esté escrito y no en la intuición de nadie."""
    a = paqueterias.ahorro(paqueterias.cotizar_en_todos(DESTINO, PAQUETE))
    assert a['comparados'] == 2
    assert a['gana'] == EI.CLAVE
    assert a['ahorro_mxn'] == pytest.approx(168.33 - 139.00, abs=0.01)


# ==========================================================================
#  3. LA GUÍA SE COMPRA CON EL PROVEEDOR QUE LA COTIZÓ
# ==========================================================================
def test_la_guia_se_compra_en_la_casa_que_cotizo(sky, ei, con_remitente):
    """⛔ Un `rate_id` sólo vale en la casa que lo emitió. Comprar en Skydropx una
    tarifa del revendedor es un 404 en el mejor caso y una guía mal pagada en el peor."""
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    barata = comp['opciones'][0]          # la de enviosinternacionales
    guia = paqueterias.comprar_guia(barata, DESTINO, PAQUETE)
    assert guia['proveedor'] == EI.CLAVE
    assert len(ei.compras) == 1 and not sky.compras, 'se compró en la casa equivocada'
    assert ei.compras[0]['shipment']['rate_id'] == 'ei-est'


def test_comprar_una_tarifa_de_skydropx_va_a_skydropx(sky, ei, con_remitente):
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    de_sky = [o for o in comp['opciones'] if o['proveedor'] == 'skydropx'][0]
    paqueterias.comprar_guia(de_sky, DESTINO, PAQUETE)
    assert len(sky.compras) == 1 and not ei.compras


def test_el_revendedor_compra_en_shipments_CON_diagonal(sky, ei, con_remitente):
    """⛔ Su OpenAPI declara `POST /api/v1/shipments/`; sin la diagonal esa ruta sólo
    acepta GET. Es un detalle de un carácter que cuesta una guía: queda congelado aquí."""
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    barata = comp['opciones'][0]
    paqueterias.comprar_guia(barata, DESTINO, PAQUETE)
    assert ei.compras[0]['ruta'] == '/shipments/'


def test_el_revendedor_pide_seguro_contra_guias_duplicadas(sky, ei, con_remitente):
    """`unique_shipment` hace que un reintento devuelva la MISMA guía en vez de comprar
    otra. Sin esto, una red que se corta en mal momento es una segunda guía pagada."""
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    paqueterias.comprar_guia(comp['opciones'][0], DESTINO, PAQUETE)
    assert ei.compras[0]['shipment']['unique_shipment'] is True


def test_un_proveedor_desconocido_no_compra_nada(sky):
    with pytest.raises(RuntimeError):
        paqueterias.comprar_guia({'proveedor': 'inventado', 'rate_id': 'x'},
                                 DESTINO, PAQUETE)


def test_guia_para_compra_sola_la_mas_barata_permitida(sky, ei, con_remitente):
    """El camino automático, el que corre cuando se confirma un pago."""
    guia = paqueterias.guia_para(DESTINO, PAQUETE)
    assert guia['proveedor'] == EI.CLAVE
    assert guia['costo'] == 139.00
    assert guia['tracking_number'] == '77123'


def test_sin_remitente_NO_se_compra_guia(sky, ei, monkeypatch):
    """Comprar con un remitente inventado es pagar una recolección en una dirección que
    no existe. Revienta a propósito."""
    for c, env in skydropx.CAMPOS_REMITENTE:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(skydropx, '_DEL_PANEL', {})
    with pytest.raises(RuntimeError):
        paqueterias.guia_para(DESTINO, PAQUETE)


# ==========================================================================
#  4. UN PROVEEDOR CAÍDO NO TUMBA EL DESPACHO
# ==========================================================================
def test_si_el_revendedor_truena_se_despacha_con_skydropx(sky, monkeypatch):
    monkeypatch.setenv('ENVIOSINT_CLIENT_ID', 'ei-id')
    monkeypatch.setenv('ENVIOSINT_CLIENT_SECRET', 'ei-secreto')
    _falsear(monkeypatch, EI, ApiFalsa(TARIFAS_EI, revienta=True))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert [o['rate_id'] for o in comp['opciones']] == ['sky-est', 'sky-fdx']
    caido = [p for p in comp['proveedores'] if p['clave'] == EI.CLAVE][0]
    assert caido['activo'] is True and 'no respondio' in caido['detalle']


def test_si_skydropx_truena_se_despacha_con_el_revendedor(ei, monkeypatch):
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'sky-id')
    monkeypatch.setenv('SKYDROPX_CLIENT_SECRET', 'sky-secreto')
    _falsear(monkeypatch, skydropx, ApiFalsa(TARIFAS_SKY, revienta=True))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert [o['rate_id'] for o in comp['opciones']] == ['ei-est', 'ei-fdx']


# ==========================================================================
#  5. EL REVENDEDOR SE CONFIGURA SIN TOCAR CÓDIGO
# ==========================================================================
def test_las_llaves_se_pueden_pegar_desde_el_admin():
    """Christián trabaja desde el teléfono: las llaves tienen que poder pegarse en
    Admin → Cobros, igual que las de cobro. Si no están en la lista de permitidas, el
    endpoint las rechaza y no hay forma de encender esto sin entrar por SSH."""
    import secretos
    assert 'ENVIOSINT_CLIENT_ID' in secretos.PERMITIDAS
    assert 'ENVIOSINT_CLIENT_SECRET' in secretos.PERMITIDAS


def test_el_remitente_es_el_MISMO_para_los_dos(con_remitente):
    """La casa despacha desde una sola dirección. Dos remitentes distintos según la
    paquetería es la forma perfecta de imprimir una guía con la dirección de nadie."""
    assert EI.remitente() == skydropx.remitente()
