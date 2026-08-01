"""OBSEQUIOS DEL DISTRIBUIDOR Y CARRITO COMPARTIBLE — lo que no se puede romper.

Encargo de Christián del 2026-08-01, pensando en Mónica. Aquí se prueban las TRES
promesas que se le hicieron, y ninguna es de diseño:

  1. ⛔ EL CÓDIGO DEL OBSEQUIO NO SE FILTRA JAMÁS. Ni en el carrito compartido, ni
     en la respuesta de compartir, ni en el pedido que se le enseña al cliente. Hay
     una prueba que SE LO INTENTA PESCAR —lee el JSON entero como texto y busca el
     código real que se guardó— y tiene que salir con las manos vacías.

  2. ⛔ EL REGALO NO ROMPE EL ROI. Regalar es descontar: el vial de cortesía sale
     del mismo margen que el descuento y se mide contra el mismo tope. Un regalo que
     no cabe se cae; la venta nunca.

  3. ⛔ EL PRECIO LO PONE EL SERVIDOR. Del enlace sólo viaja un token opaco: ni el
     precio, ni el descuento, ni el valor del regalo. Ya pasó una vez que se podía
     comprar mandando precio $0.
"""
import json
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import regalos
import server


COMPARTIR = '/api/distributor/cart/share'
ABRIR = '/api/carrito'

# Catálogo de mentira: un producto caro que sí aguanta descuento, el agua (insumo,
# tope CERO — el regalo típico de Mónica) y uno chiquito para los casos de borde.
PRODUCTOS = [
    {'id': 'p-reta', 'sku': 'RETA-20MG', 'slug': 'retatrutida-20-mg', 'name': 'Retatrutida 20 mg',
     'category': 'metabolicos', 'commission_cap': 0.40, 'distributor_eligible': True,
     'price': 3000, 'presentation': '20 mg'},
    {'id': 'p-agua', 'sku': 'AGUA-30ML', 'slug': 'agua-30-ml', 'name': 'Agua bacteriostática',
     'category': 'accesorios', 'commission_cap': 0.40, 'distributor_eligible': True,
     'price': 300, 'presentation': '30 mL'},
    {'id': 'p-chico', 'sku': 'CHI-1MG', 'slug': 'chico-1-mg', 'name': 'Chico 1 mg',
     'category': 'bienestar', 'commission_cap': 0.25, 'distributor_eligible': True,
     'price': 200, 'presentation': '1 mg'},
]

DIST = {'id': 'u-dist', 'name': 'Mónica', 'email': 'monica@x.mx', 'role': 'distributor',
        'tier': 'junior0', 'distributor_code': 'MONICAF-30-AB12'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}


# ======================================================================
#  EL MÓDULO PURO — la aritmética, sin base de datos ni red
# ======================================================================
def _precio(pid):
    return next((p['price'] for p in PRODUCTOS if p['id'] == pid), 0)


class _Item:
    def __init__(self, pid, price, qty):
        self.product_id, self.price, self.quantity = pid, price, qty


def test_un_regalo_de_producto_vale_su_precio_de_lista():
    valor = regalos.valor_de_obsequios(
        [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 2}], _precio)
    assert valor == 600


def test_regalar_el_envio_vale_lo_que_cuesta_la_guia():
    """No vale «$0 porque ya era gratis»: vale lo que la casa va a pagar de guía."""
    assert regalos.valor_de_obsequios([{'tipo': 'envio'}], _precio, costo_envio=250) == 250


def test_un_tipo_inventado_no_vale_nada():
    assert regalos.valor_de_obsequios([{'tipo': 'oro', 'product_id': 'p-reta'}], _precio) == 0


def test_el_piso_manda_el_mas_estricto_de_los_dos_topes():
    """El tope por producto (40%) y el techo de la casa (40%) sobre $10,000 dan
    $4,000. Con un techo del 10% mandaría el techo."""
    items = [_Item('p-reta', 3000, 3), _Item('p-chico', 200, 5)]   # $10,000 de lista
    topes = {'p-reta': 0.40, 'p-chico': 0.25}
    piso = regalos.piso_de_rentabilidad(items, lambda it: topes[it.product_id], techo=0.40)
    assert piso == 3850              # 9000*.40 + 1000*.25
    apretado = regalos.piso_de_rentabilidad(items, lambda it: topes[it.product_id], techo=0.10)
    assert apretado == 1000          # 10,000 * 10%


def test_los_insumos_no_aportan_margen_para_regalar():
    """El agua tiene tope CERO: un carrito de pura agua no puede regalar nada."""
    piso = regalos.piso_de_rentabilidad([_Item('p-agua', 300, 10)], lambda it: 0.0)
    assert piso == 0


def test_el_regalo_se_suma_al_descuento_no_se_mide_solo():
    """⛔ LA REGLA. Descuento $3,000 + regalo $600 contra un permitido de $3,200: NO
    cabe, aunque el regalo solito sí habría cabido."""
    v = regalos.cabe_el_obsequio(3000, 600, 3200)
    assert not v['cabe'] and v['exceso'] == 400
    assert regalos.cabe_el_obsequio(2000, 600, 3200)['cabe']


def test_un_carrito_sin_descuento_puede_regalar_hasta_el_tope():
    assert regalos.cabe_el_obsequio(0, 3200, 3200)['cabe']
    assert not regalos.cabe_el_obsequio(0, 3300, 3200)['cabe']


def test_lo_que_no_existe_en_el_catalogo_se_tira():
    limpios = regalos.limpiar_obsequios(
        [{'tipo': 'producto', 'product_id': 'p-inventado'},
         {'tipo': 'producto', 'product_id': 'p-agua'}],
        lambda pid: pid in {'p-agua', 'p-reta'})
    assert limpios == [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 1}]


def test_el_envio_no_se_puede_regalar_dos_veces():
    limpios = regalos.limpiar_obsequios([{'tipo': 'envio'}, {'tipo': 'envio'}], lambda p: True)
    assert len(limpios) == 1


def test_las_piezas_de_un_regalo_estan_topadas():
    limpios = regalos.limpiar_obsequios(
        [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 900}], lambda p: True)
    assert limpios[0]['cantidad'] == regalos.MAX_PIEZAS_OBSEQUIO


# ---------------------------------------------------------------- la lista blanca
def test_la_vista_publica_arma_desde_cero_y_no_copia_lo_de_adentro():
    """⛔ El candado NO es «acuérdate de borrar `gift_code`»: es que sólo sale lo que
    está escrito en la lista blanca. Un campo nuevo en el documento no se filtra."""
    doc = {'token': 't1', 'gift_code': 'DGIFT-SECRETO', 'distributor_id': 'u-dist',
           'campo_que_alguien_agregue_manana': 'costo interno 123',
           'lines': [{'product_id': 'p-reta', 'name': 'Retatrutida 20 mg', 'quantity': 1,
                      'unit_price': 2100, 'list_price': 3000, 'amount': 2100,
                      'commission_cap': 0.40}],
           'gifts': [{'tipo': 'producto', 'name': 'Agua bacteriostática', 'quantity': 1,
                      'gift_code': 'DGIFT-SECRETO'}]}
    fuera = json.dumps(regalos.vista_publica(doc), ensure_ascii=False)
    assert 'DGIFT' not in fuera
    assert 'gift_code' not in fuera
    assert 'distributor_id' not in fuera
    assert 'manana' not in fuera
    assert 'commission_cap' not in fuera
    assert 'Agua bacteriostática' in fuera      # el regalo SÍ se ve, su código no


# ======================================================================
#  LAS RUTAS — con una base de datos de mentira
# ======================================================================
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Coll:
    def __init__(self, docs=None):
        self._docs = docs if docs is not None else []

    def find(self, *a, **k):
        return _Cursor(self._docs)

    def aggregate(self, *a, **k):
        return _Cursor([])

    async def find_one(self, filtro=None, *a, **k):
        filtro = filtro or {}
        for d in self._docs:
            if all(d.get(k2) == v for k2, v in filtro.items() if not isinstance(v, dict)):
                return dict(d)
        return None

    async def count_documents(self, *a, **k):
        return 0

    async def update_one(self, *a, **k):
        return None

    async def insert_one(self, doc, *a, **k):
        self._docs.append(dict(doc))
        return None


class _FakeDB:
    """Una base con memoria en `shared_carts`: hace falta para poder guardar un
    carrito y volver a abrirlo, que es de lo que se trata todo esto."""

    def __init__(self):
        self._colls = {'products': _Coll(list(PRODUCTOS)),
                       'users': _Coll([dict(DIST), dict(CLIENTE)]),
                       server.COLECCION_CARRITOS: _Coll([])}

    def __getattr__(self, name):
        return self[name]

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _Coll([])
        return self._colls[name]


@pytest.fixture
def como(monkeypatch):
    bd = _FakeDB()
    monkeypatch.setattr(server, 'db', bd)
    # El acuerdo de distribuidor nace apagado; se deja explícito para que esta
    # prueba no dependa de un interruptor de otro módulo.
    monkeypatch.setattr(server, '_exigir_acuerdo', _no_exige)

    def _factory(user):
        if user is None:
            server.app.dependency_overrides.clear()
        else:
            server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        cli = TestClient(server.app)
        cli.bd = bd
        return cli

    yield _factory
    server.app.dependency_overrides.clear()


async def _no_exige(*a, **k):
    return None


CARRITO = {'client_name': 'Ana', 'discount': 0.20, 'folio': 'COT-260801-0001',
           'items': [{'product_id': 'p-reta', 'quantity': 3}],
           'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 1}]}


# --------------------------------------------------------------------- la puerta
def test_compartir_sin_sesion_no_pasa(como):
    assert como(None).post(COMPARTIR, json=CARRITO).status_code == 401


def test_un_cliente_no_puede_compartir_carritos(como):
    assert como(CLIENTE).post(COMPARTIR, json=CARRITO).status_code == 403


def test_el_distribuidor_si_arma_su_carrito(como):
    r = como(DIST).post(COMPARTIR, json=CARRITO)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo['token'] and cuerpo['url'].endswith(cuerpo['token'])
    assert cuerpo['ref'] == 'MONICAF-30-AB12'          # la venta se le atribuye a ella
    assert len(cuerpo['lines']) == 1
    assert cuerpo['gifts'] == [{'tipo': 'producto', 'name': 'Agua bacteriostática 30 mL',
                                'quantity': 1}]


def test_el_cliente_abre_el_carrito_SIN_cuenta(como):
    """La promesa de Christián: «que sobreviva a que el cliente lo abra en su
    teléfono, sin cuenta»."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    server.app.dependency_overrides.clear()            # y ahora sin sesión ninguna
    r = TestClient(server.app).get(f'{ABRIR}/{token}')
    assert r.status_code == 200, r.text
    assert r.json()['gifts'][0]['name'] == 'Agua bacteriostática 30 mL'


def test_un_token_inventado_no_abre_nada(como):
    como(DIST)
    server.app.dependency_overrides.clear()
    assert TestClient(server.app).get(f'{ABRIR}/nomeloseporsupuesto').status_code == 404


# ============================================================================
#  ⛔ LA PRUEBA QUE INTENTA PESCAR EL CÓDIGO DEL OBSEQUIO — Y TIENE QUE FALLAR
# ============================================================================
def test_el_codigo_del_obsequio_NO_se_filtra_por_ningun_lado(como):
    """Se guarda un carrito con regalo, se anota el código REAL que el servidor
    generó, y luego se lee como TEXTO PLANO todo lo que el cliente puede ver.

    Tosco a propósito, igual que la prueba del costo en el cotizador: no depende de
    que nadie mantenga al día una lista de campos permitidos.
    """
    cli = como(DIST)
    compartida = cli.post(COMPARTIR, json=CARRITO)
    assert compartida.status_code == 200, compartida.text
    token = compartida.json()['token']

    guardado = cli.bd[server.COLECCION_CARRITOS]._docs[0]
    codigo = guardado['gift_code']
    assert codigo.startswith(regalos.PREFIJO_OBSEQUIO), 'el carrito no guardó código de obsequio'

    # 1) La respuesta que ve la propia Mónica al compartir (de ahí sale el WhatsApp).
    de_ella = json.dumps(compartida.json(), ensure_ascii=False)
    # 2) Lo que ve el CLIENTE al abrir el enlace, sin sesión.
    server.app.dependency_overrides.clear()
    del_cliente = TestClient(server.app).get(f'{ABRIR}/{token}')
    assert del_cliente.status_code == 200
    visto = json.dumps(del_cliente.json(), ensure_ascii=False)

    for donde, crudo in (('la respuesta de compartir', de_ella),
                         ('el carrito que abre el cliente', visto)):
        assert codigo not in crudo, f'{donde} FILTRA el código del obsequio'
        assert regalos.PREFIJO_OBSEQUIO not in crudo, f'{donde} trae un código {regalos.PREFIJO_OBSEQUIO}-*'
        assert 'gift_code' not in crudo, f'{donde} trae el campo gift_code'


def test_ni_el_costo_ni_el_proveedor_viajan_en_el_carrito_compartido(como):
    """La otra regla de oro: el distribuidor —y menos su cliente— no ven costos."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    server.app.dependency_overrides.clear()
    crudo = json.dumps(TestClient(server.app).get(f'{ABRIR}/{token}').json(),
                       ensure_ascii=False).lower()
    for palabra in ('costo', 'proveedor', 'supplier', 'margen', 'roi', 'commission_cap',
                    'distributor_id', 'utilidad'):
        assert palabra not in crudo, f'el carrito compartido trae "{palabra}"'


# ------------------------------------------------------------------ el ROI
def test_un_regalo_que_no_cabe_se_rechaza_con_la_cuenta_hecha(como):
    """Un carrito chiquito (un producto de $200, tope 25% = $50 de margen) al que se
    le quiere colgar un regalo de $1,500 de agua. No cabe, y el 409 lo explica."""
    r = como(DIST).post(COMPARTIR, json={
        'discount': 0.0,
        'items': [{'product_id': 'p-chico', 'quantity': 1}],
        'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 5}],
    })
    assert r.status_code == 409, r.text
    detalle = r.json()['detail']
    assert detalle['error'] == 'regalo_sin_margen'
    assert detalle['entregado'] > detalle['permitido']
    assert detalle['exceso'] > 0


def test_un_regalo_que_si_cabe_pasa(como):
    """Tres Retatrutidas ($9,000, tope 40% = $3,600) sin descuento aguantan de sobra
    un frasco de agua de $300."""
    r = como(DIST).post(COMPARTIR, json={
        'discount': 0.0,
        'items': [{'product_id': 'p-reta', 'quantity': 3}],
        'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 1}],
    })
    assert r.status_code == 200, r.text
    assert r.json()['gifts']


def test_el_descuento_y_el_regalo_compiten_por_el_mismo_margen(como):
    """⛔ LA PRUEBA DEL APILADO. El MISMO regalo que cabía sin descuento deja de caber
    cuando el descuento ya se comió el tope. Es lo que impide que «regalo» sea la
    puerta trasera del descuento."""
    carrito = {'items': [{'product_id': 'p-chico', 'quantity': 20}],   # $4,000, tope 25%
               'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 1}]}
    cli = como(DIST)
    # Sin descuento el margen entero ($1,000) está libre: el frasco de $300 cabe.
    assert cli.post(COMPARTIR, json={**carrito, 'discount': 0.0}).status_code == 200
    # Con el descuento al tope, el descuento YA se llevó esos $1,000: el MISMO frasco
    # ya no cabe. Regalo y descuento salen de la misma bolsa.
    apretado = cli.post(COMPARTIR, json={**carrito, 'discount': 0.25})
    assert apretado.status_code == 409, apretado.text
    assert apretado.json()['detail']['exceso'] == 300


# ---------------------------------------------------------- el precio y el envío
def test_el_precio_lo_pone_el_servidor_no_el_enlace(como):
    """Del enlace sólo viaja un token: no hay dónde escribir un precio. El unitario
    sale del catálogo con el descuento ya recortado por el tope del producto."""
    cli = como(DIST)
    cuerpo = cli.post(COMPARTIR, json={
        'discount': 0.20, 'items': [{'product_id': 'p-reta', 'quantity': 2}], 'gifts': []}).json()
    fila = cuerpo['lines'][0]
    assert fila['list_price'] == 3000
    assert fila['unit_price'] == 2400          # 3000 − 20%
    assert cuerpo['list_total'] == 6000
    assert cuerpo['discount'] == 1200
    assert round(cuerpo['discount_rate'], 2) == 0.20


def test_el_renglon_de_envio_sale_de_la_politica_de_la_casa(como):
    """El envío se PIDE al servidor, no se escribe en la pantalla: dos Retatrutidas
    con 20% pagan $4,800 — arriba de la compra mínima — y ahí la casa absorbe hasta
    su tope. Un pedido chiquito paga la tarifa plana."""
    cli = como(DIST)
    grande = cli.post(COMPARTIR, json={
        'discount': 0.20, 'items': [{'product_id': 'p-reta', 'quantity': 2}], 'gifts': []}).json()
    chico = cli.post(COMPARTIR, json={
        'discount': 0.0, 'items': [{'product_id': 'p-chico', 'quantity': 1}], 'gifts': []}).json()
    assert grande['total'] == grande['list_total'] - grande['discount'] + grande['shipping']
    assert chico['shipping'] == server.SHIPPING_FLAT and chico['shipping_free'] is False
    # ⛔ El envío SUMA, nunca resta.
    assert chico['total'] == chico['list_total'] + chico['shipping']


def test_regalar_el_envio_lo_deja_en_cero_y_lo_dice(como):
    cuerpo = como(DIST).post(COMPARTIR, json={
        'discount': 0.0,
        'items': [{'product_id': 'p-reta', 'quantity': 1}],
        'gifts': [{'tipo': 'envio'}],
    }).json()
    assert cuerpo['shipping'] == 0 and cuerpo['shipping_free'] is True
    assert cuerpo['gifts'] == [{'tipo': 'envio', 'name': '', 'quantity': 1}]


# ======================================================================
#  EL CHECKOUT — qué hace el token cuando el cliente paga
# ======================================================================
class _Pedido:
    """Lo mínimo de un `OrderCreate` que mira `_obsequios_del_pedido`."""

    def __init__(self, token, codigo=None):
        self.shared_cart_token = token
        self.distributor_code = codigo


def _correr(corutina):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(corutina)


@pytest.fixture
def bd_con_carrito(monkeypatch):
    """Un carrito ya guardado, tal como quedaría después de que Mónica lo comparte."""
    bd = _FakeDB()
    monkeypatch.setattr(server, 'db', bd)
    doc = {'token': 'tok-vivo', 'gift_code': 'DGIFT-NOSEVE',
           'distributor_id': 'u-dist', 'ref': 'MONICAF-30-AB12',
           'items': [{'product_id': 'p-reta', 'quantity': 3}],
           'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 2},
                     {'tipo': 'envio'}],
           'discount_asked': 0.20, 'expires_at': '2099-01-01T00:00:00+00:00'}
    bd[server.COLECCION_CARRITOS]._docs.append(doc)
    return bd


def test_el_token_trae_las_cortesias_y_la_atribucion(bd_con_carrito):
    pedido = _Pedido('tok-vivo')
    doc, renglones, envio = _correr(server._obsequios_del_pedido(pedido))
    assert doc['token'] == 'tok-vivo'
    assert envio is True
    assert len(renglones) == 1
    assert renglones[0].product_id == 'p-agua' and renglones[0].quantity == 2
    # ⛔ Nace SIN PRECIO: se lo pone el catálogo dentro de `create_order`, nunca el enlace.
    assert renglones[0].price == 0.0
    # Y la venta se le acredita a quien mandó el enlace, aunque el cliente no escriba nada.
    assert pedido.distributor_code == 'MONICAF-30-AB12'


def test_un_codigo_tecleado_por_el_cliente_le_gana_al_del_enlace(bd_con_carrito):
    """Un enlace no le quita a nadie el código que el cliente puso a propósito."""
    pedido = _Pedido('tok-vivo', codigo='OTRO-CODIGO')
    _correr(server._obsequios_del_pedido(pedido))
    assert pedido.distributor_code == 'OTRO-CODIGO'


def test_un_token_vencido_no_regala_nada(bd_con_carrito):
    bd_con_carrito[server.COLECCION_CARRITOS]._docs[0]['expires_at'] = '2020-01-01T00:00:00+00:00'
    doc, renglones, envio = _correr(server._obsequios_del_pedido(_Pedido('tok-vivo')))
    assert (doc, renglones, envio) == (None, [], False)


def test_un_token_inventado_no_regala_nada(bd_con_carrito):
    assert _correr(server._obsequios_del_pedido(_Pedido('me-lo-invente'))) == (None, [], False)


def test_sin_token_el_checkout_no_cambia_en_nada(bd_con_carrito):
    assert _correr(server._obsequios_del_pedido(_Pedido(''))) == (None, [], False)


# ======================================================================
#  GUARDIAS DE CÓDIGO — lo que no se puede deshacer por descuido
# ======================================================================
# Montar un checkout entero para comprobar una línea sale carísimo y se rompe con
# cualquier cambio de otra cosa. Es el mismo recurso que ya usa `test_regla_de_5.py`:
# se lee el TEXTO de la función y se exige que la regla siga escrita ahí.
def _cuerpo_de_create_order():
    with open(server.__file__, encoding='utf-8') as fh:
        src = fh.read()
    return src.split('async def create_order(')[1].split('\n@api_router')[0]


def test_el_regalo_se_resta_del_total():
    assert 'after_discount = subtotal - discount - gift_discount' in _cuerpo_de_create_order()


def test_las_cortesias_no_reciben_descuento_ni_pagan_comision():
    """`_eligible` es el embudo por el que pasan el descuento Y las comisiones. Si una
    cortesía entrara ahí, se le descontaría encima de estar regalada y además se le
    pagaría comisión a alguien por un producto que nadie cobró."""
    cuerpo = _cuerpo_de_create_order()
    assert 'def _eligible(item):' in cuerpo
    tras = cuerpo.split('def _eligible(item):')[1]
    assert '_es_cortesia(item)' in tras.split('return bool(')[0], \
        'las cortesías ya no se están excluyendo de _eligible'


def test_el_regalo_se_vuelve_a_medir_contra_el_roi_al_cobrar():
    """No basta con revisarlo al compartir: el cliente puede quitarle renglones al
    carrito antes de pagar, y ahí el mismo regalo ya no cabe."""
    cuerpo = _cuerpo_de_create_order()
    assert 'regalos.piso_de_rentabilidad' in cuerpo
    assert 'regalos.cabe_el_obsequio' in cuerpo


def test_el_regalo_que_no_cabe_tumba_el_regalo_y_no_la_venta():
    """Regla de la casa: nunca se bloquea una venta. El 409 vive en la ruta de
    compartir (la pantalla de Mónica), no en la caja."""
    cuerpo = _cuerpo_de_create_order()
    assert 'CORTESÍA RECHAZADA AL COBRAR' in cuerpo
    assert 'raise HTTPException' not in cuerpo.split('regalos.cabe_el_obsequio')[1].split('after_discount')[0]


def test_el_pedido_NO_guarda_el_codigo_del_obsequio():
    """⛔ El pedido se le enseña al cliente —ficha, correo, su cuenta—, así que lo que
    se escriba ahí es cosa que él puede leer. El rastro para auditar va por el token."""
    cuerpo = _cuerpo_de_create_order()
    assert 'gift_code' not in cuerpo, 'create_order está escribiendo el código del obsequio en el pedido'
    assert 'shared_cart_token=' in cuerpo


def test_el_codigo_del_obsequio_solo_se_toca_donde_debe():
    """Un `grep` sobre todo el servidor: `gift_code` sólo puede aparecer al CREARLO y
    en los comentarios que explican por qué no sale. Si mañana alguien lo mete en una
    respuesta, esta prueba truena antes que el cliente lo vea."""
    with open(server.__file__, encoding='utf-8') as fh:
        renglones = [(n, ln) for n, ln in enumerate(fh, 1) if 'gift_code' in ln]
    assert renglones, 'ya nadie genera el código del obsequio'
    for n, ln in renglones:
        limpio = ln.strip()
        assert limpio.startswith('#') or "'gift_code': regalos.nuevo_codigo_de_obsequio()" in limpio, \
            f'server.py:{n} toca gift_code fuera de donde se crea: {limpio}'


def test_la_ruta_de_pedidos_sigue_siendo_create_order():
    """⛔ ESTO PASÓ DE VERDAD (2026-08-01). Al meter `_obsequios_del_pedido` justo
    encima de `create_order`, el ayudante quedó ENTRE el decorador `@post('/orders')`
    y la función: FastAPI registró el ayudante como la ruta del checkout y `POST
    /api/orders` empezó a contestar 422 pidiendo un parámetro de query.

    No lo cazó ninguna prueba —las de checkout leen el TEXTO de la función, no la
    ruta— sino la compra de prueba en el navegador. Esta línea lo caza en un segundo.
    """
    ruta = next(r for r in server.app.routes
                if getattr(r, 'path', '') == '/api/orders' and 'POST' in getattr(r, 'methods', set()))
    assert ruta.endpoint.__name__ == 'create_order', \
        f'/api/orders quedó apuntando a {ruta.endpoint.__name__}, no al checkout'


def test_la_ruta_publica_del_carrito_no_pide_sesion():
    """El cliente de Mónica no tiene cuenta y no se le va a pedir una para ver lo que
    ella le mandó. Si alguien le cuelga un `Depends` de autenticación, truena aquí."""
    ruta = next(r for r in server.app.routes if getattr(r, 'path', '') == '/api/carrito/{token}')
    assert not [d for d in ruta.dependant.dependencies if 'user' in (d.call.__name__ or '')], \
        'la ruta del carrito compartido dejó de ser pública'
