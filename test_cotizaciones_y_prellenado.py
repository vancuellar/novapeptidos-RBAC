"""LOS DATOS DEL CLIENTE Y LA LISTA DE COTIZACIONES — lo que no se puede romper.

Dos encargos de Christián del 2026-08-01, en sus palabras:

  1. «Cuando el cliente abre el link de la cotización, su nombre, email, teléfono,
     dirección, NADA se guardó. Necesito que corrijas esto si el distribuidor ya lo
     llenó por él.»

  2. «necesito que las cotizaciones generadas se guarden en el panel del distribuidor
     por si necesita reenviarlas, que no las tenga que volver a generar de cero. Y,
     una vez pagadas dejan de ser cotizaciones y se transforman en ventas.»

⛔ LA PARTE QUE MÁS IMPORTA ES LA DE PRIVACIDAD, y por eso va primero. `GET
/carrito/{token}` es una ruta PÚBLICA, sin sesión. Hay antecedente en la casa: ya
hubo una fuga por la que el domicilio de un cliente salía con sólo el número de
pedido. Así que aquí se prueba, tosca y explícitamente, que:

  · el carrito público NO devuelve correo, teléfono ni domicilio — ni aunque estén
    guardados;
  · quien pruebe TOKENS al azar no cosecha ni un dato personal;
  · quien tenga el token pero no la SEGUNDA LLAVE tampoco;
  · y que la respuesta que sí los entrega no lleva pegado el código del obsequio.
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
LISTA = '/api/distributor/quotes'

PRODUCTOS = [
    {'id': 'p-reta', 'sku': 'RETA-20MG', 'slug': 'retatrutida-20-mg', 'name': 'Retatrutida 20 mg',
     'category': 'metabolicos', 'commission_cap': 0.40, 'distributor_eligible': True,
     'price': 3000, 'presentation': '20 mg'},
    {'id': 'p-agua', 'sku': 'AGUA-30ML', 'slug': 'agua-30-ml', 'name': 'Agua bacteriostática',
     'category': 'accesorios', 'commission_cap': 0.40, 'distributor_eligible': True,
     'price': 300, 'presentation': '30 mL'},
]

DIST = {'id': 'u-dist', 'name': 'Mónica', 'email': 'monica@x.mx', 'role': 'distributor',
        'tier': 'junior0', 'distributor_code': 'MONICAF-30-AB12'}
OTRA = {'id': 'u-otra', 'name': 'Laura', 'email': 'laura@x.mx', 'role': 'distributor',
        'tier': 'junior0', 'distributor_code': 'LAURAF-30-CD34'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}

# Los datos REALES del ejemplo que mandó Christián, tal como Mónica los teclea.
NOMBRE = 'Christian Cuellar'
CORREO = 'christiancuellar@gmail.com'
TELEFONO = '9982440119'
DOMICILIO = 'Frac. Selvamar, Priv. La Ceiba, Casa 6, Solidaridad, Quintana Roo, 77727'

CARRITO = {
    'client_name': NOMBRE, 'client_email': CORREO,
    'client_phone': TELEFONO, 'client_address': DOMICILIO,
    'discount': 0.20, 'folio': 'COT-260801-0001',
    'items': [{'product_id': 'p-reta', 'quantity': 3}],
    'gifts': [{'tipo': 'producto', 'product_id': 'p-agua', 'cantidad': 1}],
}


# ======================================================================
#  Una base de mentira que SÍ filtra — porque aquí se prueba quién ve qué
# ======================================================================
# La de `test_regalos_y_carrito.py` devuelve todos los documentos en cada `find`, y
# con eso la prueba de «sólo ve las suyas» pasaría aunque el filtro no existiera.
def _casa(doc, filtro):
    for llave, valor in (filtro or {}).items():
        if llave == '$or':
            if not any(_casa(doc, sub) for sub in valor):
                return False
            continue
        if llave == '$and':
            if not all(_casa(doc, sub) for sub in valor):
                return False
            continue
        actual = doc.get(llave)
        if isinstance(valor, dict):
            if '$in' in valor and actual not in valor['$in']:
                return False
            if '$nin' in valor and actual in valor['$nin']:
                return False
        elif actual != valor:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, campo, direccion=1, *a, **k):
        self._docs.sort(key=lambda d: str(d.get(campo) or ''), reverse=direccion < 0)
        return self

    def limit(self, n, *a, **k):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, n=None, *a, **k):
        return [dict(d) for d in (self._docs[:n] if n else self._docs)]


class _Coll:
    def __init__(self, docs=None):
        self._docs = docs if docs is not None else []

    def find(self, filtro=None, proy=None, *a, **k):
        return _Cursor([d for d in self._docs if _casa(d, filtro)])

    def aggregate(self, *a, **k):
        return _Cursor([])

    async def find_one(self, filtro=None, *a, **k):
        for d in self._docs:
            if _casa(d, filtro):
                return dict(d)
        return None

    async def count_documents(self, *a, **k):
        return 0

    async def update_one(self, filtro=None, cambio=None, *a, **k):
        for d in self._docs:
            if _casa(d, filtro):
                d.update((cambio or {}).get('$set') or {})
                break
        return None

    async def insert_one(self, doc, *a, **k):
        self._docs.append(dict(doc))
        return None


class _FakeDB:
    def __init__(self):
        self._colls = {'products': _Coll(list(PRODUCTOS)),
                       'users': _Coll([dict(DIST), dict(OTRA), dict(CLIENTE)]),
                       'orders': _Coll([]),
                       server.COLECCION_CARRITOS: _Coll([])}

    def __getattr__(self, name):
        return self[name]

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _Coll([])
        return self._colls[name]


async def _no_exige(*a, **k):
    return None


@pytest.fixture
def como(monkeypatch):
    bd = _FakeDB()
    monkeypatch.setattr(server, 'db', bd)
    monkeypatch.setattr(server, '_exigir_acuerdo', _no_exige)
    # El freno por ritmo vive en memoria del proceso y se arrastra entre pruebas.
    monkeypatch.setattr(server, '_PRELLENADOS_PEDIDOS', {})
    monkeypatch.setattr(server, '_CARRITOS_ARMADOS', {})

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


def _sin_sesion():
    server.app.dependency_overrides.clear()
    return TestClient(server.app)


# ======================================================================
#  ENCARGO 1 — LOS DATOS VIAJAN, PERO SÓLO A QUIEN ABRE ESE ENLACE
# ======================================================================
def test_los_cuatro_datos_se_guardan_al_compartir(como):
    """Hasta hoy sólo se guardaba el nombre: los otros tres se tecleaban, se pintaban
    en la hoja y se tiraban a la basura."""
    cli = como(DIST)
    assert cli.post(COMPARTIR, json=CARRITO).status_code == 200
    guardado = cli.bd[server.COLECCION_CARRITOS]._docs[0]
    assert guardado['client_name'] == NOMBRE
    assert guardado['client_email'] == CORREO
    assert guardado['client_phone'] == TELEFONO
    assert guardado['client_address'] == DOMICILIO


def test_el_carrito_PUBLICO_no_entrega_correo_telefono_ni_domicilio(como):
    """⛔ LA PRUEBA DE LA FUGA. `GET /carrito/{token}` no pide sesión: cualquiera con
    un token puede llamarla. Se lee su respuesta como TEXTO PLANO y ninguno de los
    tres datos privados puede aparecer. Tosca a propósito — no depende de que nadie
    mantenga al día una lista de campos."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    crudo = json.dumps(_sin_sesion().get(f'{ABRIR}/{token}').json(), ensure_ascii=False)
    for dato, como_se_llama in ((CORREO, 'el correo'), (TELEFONO, 'el teléfono'),
                                (DOMICILIO, 'el domicilio')):
        assert dato not in crudo, f'el carrito público FILTRA {como_se_llama} del cliente'
    for campo in ('client_email', 'client_phone', 'client_address', 'prefill_key'):
        assert campo not in crudo, f'el carrito público trae el campo {campo}'
    # El NOMBRE sí sale, y salía desde el primer día: es el «Cotización para Ana».
    assert NOMBRE in crudo


def test_con_la_segunda_llave_salen_los_cuatro_datos(como):
    cli = como(DIST)
    hecho = cli.post(COMPARTIR, json=CARRITO).json()
    token, clave = hecho['token'], hecho['prefill_key']
    assert clave, 'no se repartió la segunda llave'
    r = _sin_sesion().post(f'{ABRIR}/{token}/datos', json={'clave': clave})
    assert r.status_code == 200, r.text
    assert r.json() == {'full_name': NOMBRE, 'email': CORREO,
                        'phone': TELEFONO, 'address': DOMICILIO}
    # Y no se queda en ninguna caché por el camino.
    assert r.headers.get('cache-control') == 'no-store'


def test_la_llave_viaja_en_el_FRAGMENTO_del_enlace(como):
    """`#d=` y no `?d=`: el fragmento es la única parte de una dirección que el
    navegador NO manda al servidor. Así la llave no queda escrita en los registros de
    acceso ni se le filtra a terceros por la cabecera `Referer` — que es justo lo que
    separa este diseño de «meter los datos en la URL»."""
    hecho = como(DIST).post(COMPARTIR, json=CARRITO).json()
    assert hecho['url'].endswith(f"#d={hecho['prefill_key']}")
    assert '?' not in hecho['url'], 'la llave no puede ir en la parte que sí viaja'


def test_probar_tokens_al_azar_no_cosecha_ni_un_dato(como):
    """El escenario que pidió Christián: alguien prueba tokens a ver qué cae."""
    como(DIST).post(COMPARTIR, json=CARRITO)
    anonimo = _sin_sesion()
    for inventado in ('a' * 32, 'tokendeprueba', '0123456789abcdef0123456789abcdef'):
        assert anonimo.get(f'{ABRIR}/{inventado}').status_code == 404
        r = anonimo.post(f'{ABRIR}/{inventado}/datos', json={'clave': 'loquesea'})
        assert r.status_code == 404
        assert CORREO not in r.text and DOMICILIO not in r.text


def test_con_el_token_pero_sin_la_llave_no_sale_nada(como):
    """El caso más realista: el enlace se reenvía por WhatsApp SIN el fragmento (hay
    aplicaciones que lo recortan). Ahí se ve la cotización, pero los datos no."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    anonimo = _sin_sesion()
    assert anonimo.get(f'{ABRIR}/{token}').status_code == 200        # la cotización sí
    for intento in ('', 'no-es-la-llave', 'x' * 32):
        r = anonimo.post(f'{ABRIR}/{token}/datos', json={'clave': intento})
        # 404 y no 403: a quien anda probando no se le confirma que el token exista.
        assert r.status_code == 404, r.text
        assert CORREO not in r.text and DOMICILIO not in r.text


def test_sin_datos_privados_no_se_reparte_ninguna_llave(como):
    """Si el distribuidor sólo puso el nombre, no hay nada privado que abrir: el
    enlace sale limpio. Un secreto que no abre nada es un secreto de más rodando por
    WhatsApp."""
    hecho = como(DIST).post(COMPARTIR, json={
        'client_name': 'Ana', 'discount': 0.0,
        'items': [{'product_id': 'p-reta', 'quantity': 1}], 'gifts': []}).json()
    assert hecho['prefill_key'] == ''
    assert '#' not in hecho['url']


def test_un_carrito_vencido_ya_no_entrega_datos(como):
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    clave = cli.bd[server.COLECCION_CARRITOS]._docs[0]['prefill_key']
    cli.bd[server.COLECCION_CARRITOS]._docs[0]['expires_at'] = '2020-01-01T00:00:00+00:00'
    r = _sin_sesion().post(f'{ABRIR}/{token}/datos', json={'clave': clave})
    assert r.status_code == 410
    assert CORREO not in r.text


def test_los_datos_no_traen_pegado_el_codigo_del_obsequio(como):
    """⛔ La regla que persigue a todo esto: el código del obsequio no se le enseña al
    cliente en ningún lado, y ésta es una ruta NUEVA por la que podría escaparse."""
    cli = como(DIST)
    hecho = cli.post(COMPARTIR, json=CARRITO).json()
    guardado = cli.bd[server.COLECCION_CARRITOS]._docs[0]
    codigo = guardado['gift_code']
    crudo = _sin_sesion().post(f'{ABRIR}/{hecho["token"]}/datos',
                               json={'clave': hecho['prefill_key']}).text
    assert codigo not in crudo
    assert regalos.PREFIJO_OBSEQUIO not in crudo
    assert 'gift_code' not in crudo and 'prefill_key' not in crudo
    assert 'distributor_id' not in crudo


def test_el_freno_por_ritmo_corta_a_quien_prueba_llaves(como):
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    anonimo = _sin_sesion()
    codigos = [anonimo.post(f'{ABRIR}/{token}/datos', json={'clave': 'mal'}).status_code
               for _ in range(server.PRELLENADOS_POR_HORA + 3)]
    assert 429 in codigos, 'nadie frenó los intentos'


def test_el_recorte_arma_desde_cero_y_no_copia_lo_de_adentro():
    """Módulo puro: un campo nuevo en el documento no se cuela por parecido."""
    fuera = regalos.datos_de_contacto({
        'client_name': 'Ana', 'client_email': 'a@x.mx', 'gift_code': 'DGIFT-SECRETO',
        'client_notas_internas': 'costo 400', 'prefill_key': 'llave'})
    assert set(fuera) == {'full_name', 'email', 'phone', 'address'}
    assert 'DGIFT' not in json.dumps(fuera)


# ======================================================================
#  ENCARGO 2 — LA LISTA DE COTIZACIONES, Y EL PASO A VENTA
# ======================================================================
def _guardar_pedido(bd, token, **campos):
    bd.orders._docs.append({'order_number': 'EX-20260801-0001', 'total': 7500,
                            'status': 'pendiente', 'paid': False,
                            'shared_cart_token': token, **campos})


def test_sin_sesion_no_hay_lista(como):
    assert como(None).get(LISTA).status_code == 401


def test_un_cliente_no_ve_cotizaciones_de_nadie(como):
    assert como(CLIENTE).get(LISTA).status_code == 403


def test_la_cotizacion_queda_guardada_con_folio_cliente_fecha_y_total(como):
    """Lo que pidió Christián para la tabla: folio, cliente, fecha, total, estado."""
    cli = como(DIST)
    cli.post(COMPARTIR, json=CARRITO)
    r = cli.get(LISTA)
    assert r.status_code == 200, r.text
    fila = r.json()['quotes'][0]
    assert fila['folio'] == 'COT-260801-0001'
    assert fila['full_name'] == NOMBRE
    assert fila['created_at']
    # 3 × $3,000 = $9,000 de lista, −20% = $7,200. El número lo puso el servidor.
    assert fila['list_total'] == 9000 and fila['discount'] == 1800
    assert fila['total'] == 7200 + fila['shipping']
    assert fila['lines'] == 1 and fila['gifts'] == 1
    assert fila['estado'] == server.ESTADO_COTIZACION
    assert r.json()['cotizaciones'] == 1 and r.json()['ventas'] == 0


def test_el_distribuidor_SOLO_ve_las_suyas(como):
    """⛔ «El distribuidor sólo ve las suyas. Nunca las de otro.» El filtro es el id
    del token: no hay ningún parámetro donde escribir el de otra persona."""
    como(DIST).post(COMPARTIR, json=CARRITO)
    laura = como(OTRA)
    laura.post(COMPARTIR, json={**CARRITO, 'folio': 'COT-DE-LAURA',
                                'client_name': 'Cliente de Laura',
                                'client_email': 'laura-cliente@x.mx'})
    mias = como(DIST).get(LISTA).json()['quotes']
    assert len(mias) == 1
    assert mias[0]['folio'] == 'COT-260801-0001'
    crudo = json.dumps(mias, ensure_ascii=False)
    assert 'Laura' not in crudo and 'laura-cliente@x.mx' not in crudo


def test_una_cotizacion_con_pedido_sin_cobrar_todavia_no_es_venta(como):
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    _guardar_pedido(cli.bd, token)
    fila = cli.get(LISTA).json()['quotes'][0]
    assert fila['estado'] == server.ESTADO_PEDIDO
    assert fila['order_number'] == 'EX-20260801-0001'


def test_una_cotizacion_PAGADA_deja_de_ser_cotizacion_y_es_VENTA(como):
    """⛔ El corazón del encargo 2: «una vez pagadas dejan de ser cotizaciones y se
    transforman en ventas». Y UN SOLO RENGLÓN: no queda la cotización por un lado y
    la venta por otro."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    _guardar_pedido(cli.bd, token, status='confirmado', paid=True,
                    paid_at='2026-08-01T12:00:00+00:00')
    cuerpo = cli.get(LISTA).json()
    assert len(cuerpo['quotes']) == 1, 'la cotización y la venta se duplicaron'
    fila = cuerpo['quotes'][0]
    assert fila['estado'] == server.ESTADO_VENTA
    assert fila['order_number'] == 'EX-20260801-0001'
    assert fila['order_total'] == 7500 and fila['paid_at']
    assert cuerpo['cotizaciones'] == 0 and cuerpo['ventas'] == 1
    assert cuerpo['vendido'] == 7500


def test_un_pedido_cancelado_no_cuenta_como_venta(como):
    """`esta_pagado` es la misma respuesta que usan todos los reportes: una
    devolución deja el pedido cancelado y el dinero ya salió de vuelta."""
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    _guardar_pedido(cli.bd, token, status='cancelado', paid=True)
    assert cli.get(LISTA).json()['quotes'][0]['estado'] == server.ESTADO_PEDIDO


def test_el_pedido_de_OTRO_carrito_no_le_cambia_el_estado(como):
    cli = como(DIST)
    cli.post(COMPARTIR, json=CARRITO)
    _guardar_pedido(cli.bd, 'token-de-otro-carrito', status='confirmado', paid=True)
    assert cli.get(LISTA).json()['quotes'][0]['estado'] == server.ESTADO_COTIZACION


def test_la_lista_trae_el_enlace_listo_para_reenviar(como):
    """«que no las tenga que volver a generar de cero»: el enlace se rearma con su
    fragmento, así que reenviar es copiar y pegar."""
    cli = como(DIST)
    hecho = cli.post(COMPARTIR, json=CARRITO).json()
    fila = cli.get(LISTA).json()['quotes'][0]
    assert fila['url'] == hecho['url']
    assert fila['token'] == hecho['token']


def test_la_lista_NO_filtra_el_codigo_del_obsequio_ni_a_su_dueña(como):
    """Mónica tampoco tiene por qué verlo: si lo ve, se lo puede pasar al cliente."""
    cli = como(DIST)
    cli.post(COMPARTIR, json=CARRITO)
    codigo = cli.bd[server.COLECCION_CARRITOS]._docs[0]['gift_code']
    crudo = cli.get(LISTA).text
    assert codigo not in crudo
    assert regalos.PREFIJO_OBSEQUIO not in crudo
    assert 'gift_code' not in crudo


def test_la_lista_no_trae_costos_ni_proveedores(como):
    cli = como(DIST)
    cli.post(COMPARTIR, json=CARRITO)
    crudo = cli.get(LISTA).text.lower()
    for palabra in ('costo', 'proveedor', 'supplier', 'margen', 'commission_cap', 'utilidad'):
        assert palabra not in crudo, f'la lista de cotizaciones trae "{palabra}"'


def test_una_cotizacion_vieja_sin_foto_se_tasa_sola(como):
    """Los carritos creados antes de que existiera la lista no traen la foto del
    total. Se les saca una al vuelo en vez de enseñar un renglón en blanco."""
    cli = como(DIST)
    cli.bd[server.COLECCION_CARRITOS]._docs.append({
        'token': 'tok-viejo', 'gift_code': 'DGIFT-VIEJO', 'distributor_id': 'u-dist',
        'ref': 'MONICAF-30-AB12', 'folio': 'COT-VIEJA', 'client_name': 'Antiguo',
        'items': [{'product_id': 'p-reta', 'quantity': 1}], 'gifts': [],
        'discount_asked': 0.0, 'created_at': '2026-07-01T00:00:00+00:00',
        'expires_at': '2099-01-01T00:00:00+00:00'})
    fila = cli.get(LISTA).json()['quotes'][0]
    assert fila['folio'] == 'COT-VIEJA'
    assert fila['list_total'] == 3000 and fila['total'] > 0


# ======================================================================
#  REENVIAR POR CORREO — sin rearmar, y con los precios de HOY
# ======================================================================
def test_reenviar_una_cotizacion_que_no_es_suya_no_se_puede(como):
    cli = como(DIST)
    token = cli.post(COMPARTIR, json=CARRITO).json()['token']
    assert como(OTRA).post(f'/api/distributor/quotes/{token}/email',
                           json={'email': 'x@x.mx'}).status_code == 404


def test_reenviar_usa_el_enlace_del_carrito_guardado(como, monkeypatch):
    """No se arma un `?pedido=` nuevo: se manda EL MISMO carrito, con sus cortesías y
    su prellenado. Y el precio lo vuelve a poner el servidor con el catálogo de hoy."""
    enviados = {}

    async def _falso(destino, cotizacion, language=None, reply_to=None):
        enviados['destino'] = destino
        enviados['cotizacion'] = cotizacion
        return True, ''

    monkeypatch.setattr(server, 'send_quote_email', _falso)
    cli = como(DIST)
    hecho = cli.post(COMPARTIR, json=CARRITO).json()
    r = cli.post(f'/api/distributor/quotes/{hecho["token"]}/email',
                 json={'email': 'cliente@x.mx'})
    assert r.status_code == 200, r.text
    assert enviados['destino'] == 'cliente@x.mx'
    assert enviados['cotizacion']['link'] == hecho['url']
    # El precio sale del catálogo, no del documento guardado: 3 × $3,000 − 20%.
    assert enviados['cotizacion']['total'] == 7200
    assert 'gift_code' not in json.dumps(enviados['cotizacion'], ensure_ascii=False)
