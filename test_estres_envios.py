"""STRESS TEST del doble cotizador. Orden de Christián (2026-07-31).

Lo que se aprieta aquí es lo que se rompe en la vida real, no en el papel:

  1. **EL TOPE DE 2 PETICIONES POR SEGUNDO.** Las dos paqueterías lo tienen. Cotizar
     un pedido a la vez nunca lo tocaba; cotizar TRES al mismo tiempo (dos pestañas del
     panel más el checkout) mandaba seis peticiones en el mismo segundo y devolvía 429 —
     y un 429 a media compra de guía es un pedido que no sale.
  2. **COTIZACIONES EN PARALELO.** Que dos despachos simultáneos no se pisen, no se
     mezclen las tarifas y cada uno se quede con las suyas.
  3. **UN PROVEEDOR CAÍDO / LENTO.** Que el otro despache igual, y que uno que no
     contesta no cuelgue al que sí.
  4. **REINTENTOS SIN GUÍA DUPLICADA.** Es el que cuesta dinero de verdad.

⛔ AQUÍ NO SE COMPRA NI UNA GUÍA REAL. Comprar cuesta dinero Y manda a la paquetería a
recoger a una dirección de verdad. Todo son dobles de prueba. La primera compra real será
con los pedidos de Aidee y Brenda, y sólo cuando Christián lo ordene.
"""
import os
import threading
import time

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import enviosinternacionales as EI
import paqueterias
import ritmo
import skydropx

from test_paqueterias import (DESTINO, PAQUETE, TARIFAS_EI, TARIFAS_SKY, ApiFalsa,
                              FakeResp, _falsear)


@pytest.fixture(autouse=True)
def _limpio():
    """Cada prueba arranca sin token y sin cuenta de peticiones gastadas."""
    for mod in (skydropx, EI):
        mod.olvidar_token()
        mod.RITMO.olvidar()
    yield
    for mod in (skydropx, EI):
        mod.olvidar_token()
        mod.RITMO.olvidar()


@pytest.fixture()
def dos(monkeypatch):
    """Los dos proveedores encendidos y contestando."""
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'sky-id')
    monkeypatch.setenv('SKYDROPX_CLIENT_SECRET', 'sky-secreto')
    monkeypatch.setenv('ENVIOSINT_CLIENT_ID', 'ei-id')
    monkeypatch.setenv('ENVIOSINT_CLIENT_SECRET', 'ei-secreto')
    return (_falsear(monkeypatch, skydropx, ApiFalsa(TARIFAS_SKY)),
            _falsear(monkeypatch, EI, ApiFalsa(TARIFAS_EI)))


@pytest.fixture()
def con_remitente(monkeypatch):
    for k, v in {'NAME': 'Trabajador', 'ADDRESS1': 'Calle 1', 'CITY': 'Playa del Carmen',
                 'PROVINCE': 'Quintana Roo', 'ZIP': '77710', 'COLONIA': 'Centro',
                 'PHONE': '9841234567', 'EMAIL': 'envios@exygenlabs.com'}.items():
        monkeypatch.setenv(f'SKYDROPX_FROM_{k}', v)
    return True


# ==========================================================================
#  1. EL TOPE DE 2 PETICIONES POR SEGUNDO
# ==========================================================================
def test_el_freno_respeta_el_tope_aunque_le_pidan_todo_de_golpe():
    """20 peticiones a 2/seg no pueden salir en menos de ~9.5 segundos de reloj.

    Se mide con el reloj de verdad pero con un tope ALTO (20/seg) para que la prueba
    no tarde: la aritmética es la misma y el error se vería igual.
    """
    r = ritmo.Ritmo(20, 'prueba')
    arranque = time.monotonic()
    for _ in range(40):
        r.esperar()
    transcurrido = time.monotonic() - arranque
    # 40 peticiones a 20/seg = 2 segundos de ventana, menos la primera ráfaga gratis.
    assert transcurrido >= 0.9, f'el freno no frenó: {transcurrido:.2f}s'


def test_el_freno_deja_pasar_la_rafaga_legitima():
    """Con tope de 2/seg, las DOS primeras salen sin esperar. Frenar la primera sería
    castigar a todos por un límite que todavía no se alcanza."""
    r = ritmo.Ritmo(2, 'prueba')
    assert r.esperar() == 0.0
    assert r.esperar() == 0.0
    # la tercera SÍ tiene que esperar
    assert r.esperar() > 0


def test_el_freno_aguanta_muchos_hilos_a_la_vez():
    """⛔ EL CASO QUE IMPORTA: FastAPI corre estas rutas en su pool de HILOS, así que dos
    despachos del panel son dos hilos de verdad. Sin candado, dos hilos leen el mismo
    hueco libre y salen juntos — y el tope se pasa sin que nadie lo note."""
    r = ritmo.Ritmo(10, 'prueba')
    salidas = []
    candado = threading.Lock()

    def trabajar():
        for _ in range(5):
            r.esperar()
            with candado:
                salidas.append(time.monotonic())

    hilos = [threading.Thread(target=trabajar) for _ in range(6)]
    arranque = time.monotonic()
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert len(salidas) == 30
    # En NINGUNA ventana de un segundo puede haber más de 10 (con un pelo de holgura
    # por el redondeo del reloj).
    salidas.sort()
    for i, t in enumerate(salidas):
        en_la_ventana = sum(1 for otro in salidas[i:] if otro - t < 1.0)
        assert en_la_ventana <= 11, (
            f'salieron {en_la_ventana} peticiones en un segundo, el tope es 10')
    assert time.monotonic() - arranque >= 1.8


def test_cada_proveedor_lleva_su_propia_cuenta():
    """El tope es por CUENTA, no del mundo: gastar los permisos de Skydropx no puede
    frenar a enviosinternacionales. Compartir un freno haría lento al doble cotizador
    justo por ser doble."""
    assert skydropx.RITMO is not EI.RITMO
    skydropx.RITMO.esperar()
    skydropx.RITMO.esperar()
    assert EI.RITMO.esperar() == 0.0, 'el freno de uno frenó al otro'


def test_cotizar_de_verdad_pasa_por_el_freno(dos):
    """No basta con que el freno exista: tiene que estar en el camino de las peticiones."""
    gastadas = []
    original = skydropx.RITMO.esperar
    skydropx.RITMO.esperar = lambda: (gastadas.append(1), original())[1]
    try:
        paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    finally:
        skydropx.RITMO.esperar = original
    # token + POST /quotations, mínimo. Si esto da 0, el freno quedó de adorno.
    assert len(gastadas) >= 2, 'las peticiones a Skydropx no pasan por el freno'


# ==========================================================================
#  2. COTIZACIONES EN PARALELO
# ==========================================================================
def test_diez_cotizaciones_en_paralelo_no_se_mezclan(dos, monkeypatch):
    """Cada despacho se queda con SUS tarifas. Si el estado se compartiera mal, un
    pedido acabaría comprando la guía cotizada para otro."""
    monkeypatch.setenv('SKYDROPX_REQ_POR_SEG', '50')
    skydropx.RITMO.por_segundo = 50
    EI.RITMO.por_segundo = 50
    resultados, errores = [], []

    def cotizar():
        try:
            resultados.append(paqueterias.cotizar_en_todos(DESTINO, PAQUETE))
        except Exception as e:                      # pragma: no cover
            errores.append(e)

    hilos = [threading.Thread(target=cotizar) for _ in range(10)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert not errores, errores
    assert len(resultados) == 10
    for r in resultados:
        assert [o['rate_id'] for o in r['opciones']] == ['ei-est', 'sky-est', 'sky-fdx', 'ei-fdx']
        assert r['opciones'][0]['precio'] == 139.00


def test_el_token_se_pide_UNA_vez_aunque_cotizen_diez_a_la_vez(dos, monkeypatch):
    """Diez despachos simultáneos no pueden gastar diez tokens: el token vive dos horas
    y pedirlo de más es regalar la mitad del cupo de peticiones."""
    sky, _ei = dos
    monkeypatch.setenv('SKYDROPX_REQ_POR_SEG', '50')
    skydropx.RITMO.por_segundo = 50
    EI.RITMO.por_segundo = 50
    hilos = [threading.Thread(target=lambda: paqueterias.cotizar_en_todos(DESTINO, PAQUETE))
             for _ in range(10)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    # Puede haber alguna carrera al arranque, pero diez es inaceptable.
    assert sky.tokens <= 3, f'se pidieron {sky.tokens} tokens para 10 cotizaciones'


# ==========================================================================
#  3. UN PROVEEDOR CAÍDO, LENTO, O QUE CONTESTA BASURA
# ==========================================================================
def test_si_uno_truena_el_otro_despacha_igual(dos, monkeypatch):
    _falsear(monkeypatch, EI, ApiFalsa(TARIFAS_EI, revienta=True))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert [o['rate_id'] for o in comp['opciones']] == ['sky-est', 'sky-fdx']


def test_los_dos_caidos_no_revientan_el_despacho(monkeypatch):
    """Sin tarifas de nadie se devuelve vacío y con el motivo escrito. El admin compra
    la guía a mano, como toda la vida. Lo que NO puede pasar es una excepción."""
    monkeypatch.setenv('SKYDROPX_CLIENT_ID', 'x')
    monkeypatch.setenv('SKYDROPX_CLIENT_SECRET', 'y')
    monkeypatch.setenv('ENVIOSINT_CLIENT_ID', 'x')
    monkeypatch.setenv('ENVIOSINT_CLIENT_SECRET', 'y')
    _falsear(monkeypatch, skydropx, ApiFalsa(TARIFAS_SKY, revienta=True))
    _falsear(monkeypatch, EI, ApiFalsa(TARIFAS_EI, revienta=True))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    assert comp['opciones'] == []
    assert all('no respondio' in p['detalle'] for p in comp['proveedores'])
    assert paqueterias.ahorro(comp)['ahorro_mxn'] == 0.0


def test_un_proveedor_que_nunca_termina_no_cuelga_al_otro(dos, monkeypatch):
    """⛔ El que se cuelga es el que más caro sale: mientras uno espera, el despacho
    entero espera. Se le pone tope y se sigue con lo que sí llegó."""
    class NuncaTermina(ApiFalsa):
        def post(self, url, headers=None, json=None, timeout=None):
            ruta = self._ruta(url)
            if ruta == '/oauth/token':
                return FakeResp({'access_token': 'tok', 'expires_in': 7200})
            if ruta == '/quotations':
                return FakeResp({'id': 'q-lento', 'is_completed': False,
                                 'packages': [], 'rates': []})
            return FakeResp({}, 404)

        def get(self, url, headers=None, timeout=None):
            return FakeResp({'id': 'q-lento', 'is_completed': False,
                             'packages': [], 'rates': []})

    _falsear(monkeypatch, EI, NuncaTermina(TARIFAS_EI))
    monkeypatch.setattr(EI, 'ESPERA_MAX_COTIZACION_S', 0.05)
    arranque = time.monotonic()
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE, espera_max=0.05)
    assert time.monotonic() - arranque < 8, 'el proveedor lento colgó el despacho'
    assert [o['rate_id'] for o in comp['opciones']] == ['sky-est', 'sky-fdx']


def test_una_tarifa_con_precio_basura_no_llega_a_comprarse(dos, monkeypatch):
    """Una tarifa sin precio (`success: false`) no es una opción: es ruido. Comprar
    contra ella sería comprar a ciegas."""
    basura = [dict(TARIFAS_SKY[0], success=False, total=None, amount=None),
              dict(TARIFAS_SKY[1], total='0')]
    _falsear(monkeypatch, skydropx, ApiFalsa(basura))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    de_sky = [o for o in comp['opciones'] if o['proveedor'] == 'skydropx']
    assert de_sky == [], 'una tarifa sin precio se coló como opción comprable'


# ==========================================================================
#  4. REINTENTOS SIN GUÍA DUPLICADA  (el que cuesta dinero)
# ==========================================================================
def test_el_reintento_del_401_no_compra_dos_guias(dos, con_remitente, monkeypatch):
    """Un token vencido en mitad de la compra hace que se reintente UNA vez. Ese
    reintento no puede convertirse en una segunda guía."""
    class Un401AlComprar(ApiFalsa):
        def __init__(self, tarifas):
            super().__init__(tarifas)
            self.ya_fallo = False

        def post(self, url, headers=None, json=None, timeout=None):
            ruta = self._ruta(url)
            if ruta in ('/shipments', '/shipments/') and not self.ya_fallo:
                self.ya_fallo = True
                return FakeResp({'message': 'token vencido'}, 401)
            return super().post(url, headers=headers, json=json, timeout=timeout)

    api = _falsear(monkeypatch, EI, Un401AlComprar(TARIFAS_EI))
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    barata = comp['opciones'][0]
    assert barata['proveedor'] == EI.CLAVE
    guia = paqueterias.comprar_guia(barata, DESTINO, PAQUETE)
    assert guia['tracking_number'] == '77123'
    # Se reintentó, sí — pero las DOS peticiones llevan `unique_shipment`, que es lo que
    # hace que la paquetería devuelva la MISMA guía en vez de emitir otra.
    assert all(c['shipment']['unique_shipment'] is True for c in api.compras)


def test_todas_las_compras_al_revendedor_piden_el_seguro(dos, con_remitente):
    """`unique_shipment` es el seguro contra guías duplicadas: su API cachea la
    respuesta del `rate_id` 96 horas. Si algún camino de compra se olvidara de pedirlo,
    un reintento costaría una guía de más."""
    _sky, api = dos
    comp = paqueterias.cotizar_en_todos(DESTINO, PAQUETE)
    for opcion in [o for o in comp['opciones'] if o['proveedor'] == EI.CLAVE]:
        paqueterias.comprar_guia(opcion, DESTINO, PAQUETE)
    assert api.compras, 'no se compró nada: la prueba no probó nada'
    assert all(c['shipment']['unique_shipment'] is True for c in api.compras)


def test_guia_para_no_compra_dos_veces_si_lo_llaman_dos_veces(dos, con_remitente):
    """El camino automático corre desde cuatro lados (tarjeta, OXXO, cripto, SPEI). El
    candado de verdad vive en `comprar_guia_del_pedido` (si el pedido ya tiene guía, no
    se compra otra); aquí se comprueba que cada llamada compra UNA y sólo una."""
    _sky, api = dos
    paqueterias.guia_para(DESTINO, PAQUETE)
    assert len(api.compras) == 1
    paqueterias.guia_para(DESTINO, PAQUETE)
    assert len(api.compras) == 2, 'cada llamada compra exactamente una guía'


# ==========================================================================
#  4.bis  COMPRAR LA GUÍA NO LE MUEVE UN PESO AL CLIENTE
# ==========================================================================
def test_comprar_la_guia_no_le_cobra_nada_al_cliente(dos, con_remitente, monkeypatch):
    """⛔ ORDEN DE CHRISTIÁN (2026-07-31, por el pedido de Brenda): «a Brenda NO se le
    cobra ni se le manda ningún costo de envío — lo absorbe la casa por completo».

    Lo que compra la casa y lo que paga el cliente son DOS cuentas distintas y no se
    tocan: la guía escribe `shipping_cost` (lo que le cuesta a la casa) y NUNCA
    `shipping` (lo que pagó el cliente) ni `total`. Sin esta prueba, alguien podría
    "arreglar" el pedido sumándole el envío real y cobrarle de más a una clienta a la
    que ya se le prometió envío gratis.
    """
    import asyncio
    import server
    from test_envios import FakeDB

    pedido = {'id': 'o-b', 'order_number': 'EX-20260730-5930', 'status': 'confirmado',
              'shipping': 0.0, 'total': 4827.0, 'shipping_absorbed': 250.0,
              'customer': {'full_name': 'Brenda', 'postal_code': '76807',
                           'address': 'Calle 1', 'city': 'San Juan del Rio',
                           'state': 'Queretaro', 'phone': '4425217088',
                           'email': 'b@ejemplo.mx'},
              'items': [{'product_id': 'p1', 'quantity': 1, 'name': 'Retatrutida'}]}

    fake = FakeDB()
    asyncio.run(fake.orders.insert_one(dict(pedido)))
    monkeypatch.setattr(server, 'db', fake)
    monkeypatch.setattr(server, '_catalogo_de', lambda items: _async({}))
    monkeypatch.setattr(server, 'avisar_del_envio', lambda o: _async(True))

    cot = asyncio.run(server.admin_cotizar_envio('o-b', admin={'email': 'a@x.mx'}))
    opcion = next(o for o in cot['options'] if o['para_el_cliente'])

    class Payload:
        option_id = opcion['id']

    asyncio.run(server.admin_comprar_guia('o-b', Payload(), admin={'email': 'a@x.mx'}))
    fresco = asyncio.run(fake.orders.find_one({'id': 'o-b'}))

    # Lo del CLIENTE, intacto:
    assert fresco['shipping'] == 0.0, 'se le cobró envío a la clienta'
    assert fresco['total'] == 4827.0, 'le cambió el total'
    # Lo de la CASA, escrito:
    assert fresco['shipping_cost'] > 0
    assert fresco['tracking_number']


def _async(valor):
    """Envuelve un valor en algo que se pueda `await`ear, para sustituir funciones async."""
    async def _f():
        return valor
    return _f()


# ==========================================================================
#  4.ter  LOS TOPES DE LA API: NOMBRE 30, REFERENCIA 40
# ==========================================================================
#  ⛔ COMPROBADO EN VIVO EL 2026-07-31, EN LA PRIMERA COMPRA REAL. La cotización pasa
#  sin quejarse y es la COMPRA la que rebota con 422:
#     «Dirección de destino nombre es demasiado largo (máximo son 30 caracteres)»
#     «Address to reference es demasiado largo (40 caracteres máximo)»
#  O sea que el error aparece con el pedido YA PAGADO y el cliente esperando su guía.
def test_un_nombre_largo_se_recorta_sin_volverse_ilegible():
    """«Brenda Iliana Oseguera Gonzalez» son 31 y caben 30. Cortar a lo bruto dejaría
    «...Gonzale» — un apellido mal escrito en una guía. Se quitan los nombres de en
    medio, que es lo que sobra."""
    corto = skydropx._nombre_corto('Brenda Iliana Oseguera Gonzalez')
    assert len(corto) <= skydropx.MAX_NOMBRE
    assert corto == 'Brenda Oseguera Gonzalez'
    # y no se toca lo que ya cabe
    assert skydropx._nombre_corto('Juan Perez') == 'Juan Perez'


def test_un_nombre_larguisimo_se_recorta_igual():
    largo = 'Maria Guadalupe Fernanda Villanueva De La Torre Hernandez'
    corto = skydropx._nombre_corto(largo)
    assert len(corto) <= skydropx.MAX_NOMBRE, corto


def test_la_direccion_de_envio_respeta_los_dos_topes():
    d = {'name': 'Brenda Iliana Oseguera Gonzalez',
         'address1': 'Prolongacion el Roble 73', 'address2': 'Int. 24 B',
         'city': 'San Juan del Rio', 'colonia': 'Paseos de la Venta',
         'phone': '4425217088', 'email': 'b@x.mx',
         'reference': 'Fracc. Paseos de la Venta, se recibe en Vigilancia de 9am a 7pm'}
    envio = skydropx._direccion_envio(d)
    assert len(envio['name']) <= skydropx.MAX_NOMBRE, envio['name']
    assert len(envio['reference']) <= skydropx.MAX_REFERENCIA, envio['reference']
    # y sigue diciendo algo útil, no una cadena cortada a la mitad de una palabra
    assert envio['reference'].startswith('Fracc. Paseos de la Venta')


# ==========================================================================
#  5. LA GUÍA SIEMPRE TRAE NÚMERO DE RASTREO
# ==========================================================================
def test_la_compra_devuelve_numero_de_rastreo(dos, con_remitente):
    """Sin número de rastreo la guía no sirve: el cliente no puede seguir su paquete y
    el correo que se le promete sale vacío."""
    guia = paqueterias.guia_para(DESTINO, PAQUETE)
    assert guia['tracking_number'] == '77123'
    assert guia['label_url'].endswith('.pdf')
    assert guia['proveedor'] == EI.CLAVE
    assert guia['costo'] == 139.00


def test_si_la_guia_tarda_en_traer_numero_se_vuelve_a_preguntar(dos, con_remitente,
                                                               monkeypatch):
    """La paquetería puede contestar el envío creado y el número unos segundos después.
    Rendirse al primer intento dejaría el pedido 'enviado' sin rastreo."""
    class TardaElNumero(ApiFalsa):
        def __init__(self, tarifas):
            super().__init__(tarifas)
            self.consultas = 0

        def post(self, url, headers=None, json=None, timeout=None):
            ruta = self._ruta(url)
            if ruta in ('/shipments', '/shipments/'):
                self.compras.append({'ruta': ruta, **(json or {})})
                return FakeResp({'data': {'id': 'ship-1'}})     # todavía sin número
            return super().post(url, headers=headers, json=json, timeout=timeout)

        def get(self, url, headers=None, timeout=None):
            if '/shipments/' in self._ruta(url):
                self.consultas += 1
                if self.consultas < 2:
                    return FakeResp({'data': {'id': 'ship-1'}})
                return FakeResp({'data': {'id': 'ship-1'},
                                 'included': [{'attributes': {
                                     'tracking_number': '99999',
                                     'label_url': 'https://x.test/g.pdf'}}]})
            return super().get(url, headers=headers, timeout=timeout)

    api = _falsear(monkeypatch, EI, TardaElNumero(TARIFAS_EI))
    monkeypatch.setattr(EI, 'ESPERA_MAX_GUIA_S', 5)
    guia = paqueterias.guia_para(DESTINO, PAQUETE)
    assert guia['tracking_number'] == '99999'
    assert api.consultas >= 2, 'no volvió a preguntar por el número'
