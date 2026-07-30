"""La foto del Motor de Precios: que NUNCA se pueda ver sin ser admin.

Lo que se cuida aquí no es que el tablero se vea bonito: es que el costo de cada
producto, el nombre de cada proveedor y el margen no salgan al mundo. La primera
versión de esto era un archivo en `novapeptidos-UI/public/`, o sea la carpeta que se
sirve tal cual en exygenlabs.com — cualquiera con el enlace veía a cuánto compramos y
a quién. Estas pruebas existen para que esa puerta no se vuelva a abrir.
"""
import inspect
import os
import re

import pytest

# database.py exige MONGO_URL al importar y este archivo también importa `server`.
# Corriendo la suite completa alguien más ya la había declarado, así que el archivo
# sólo pasaba acompañado: solo, reventaba con KeyError.
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

BASE = os.path.dirname(os.path.abspath(__file__))
SITIO = os.path.join(BASE, '..', 'novapeptidos-UI')


def _fuente():
    with open(os.path.join(BASE, 'server.py'), encoding='utf-8') as fh:
        return fh.read()


def test_las_dos_rutas_exigen_admin():
    """Leer y escribir la foto piden `get_current_admin`, no `get_current_user`.

    Con `get_current_user` bastaría CUALQUIER cuenta —incluido un distribuidor, que es
    justo quien no debe ver el margen que le queda a la casa— así que la diferencia
    entre las dos dependencias es toda la protección que tiene esto."""
    src = _fuente()
    for metodo in ('get', 'put'):
        patron = re.compile(
            r"@api_router\." + metodo + r"\('/admin/motor-precios'\)\s*\n"
            r"async def \w+\([^)]*\)", re.S)
        m = patron.search(src)
        assert m, f'no encontré la ruta {metodo.upper()} /admin/motor-precios'
        assert 'get_current_admin' in m.group(0), (
            f'{metodo.upper()} /admin/motor-precios NO exige admin: {m.group(0)}')


def test_la_foto_no_vive_en_la_carpeta_publica_del_sitio():
    """`novapeptidos-UI/public/` se publica ENTERA. Un JSON ahí es un JSON abierto.

    Esta prueba no mira el código: mira el disco. Es la única forma de cachar que
    alguien vuelva a dejar el archivo donde no va."""
    publico = os.path.join(SITIO, 'public', 'motor-precios.json')
    assert not os.path.exists(publico), (
        'La foto del motor está en la carpeta pública del sitio: lleva costos y '
        'proveedores, y ahí queda servida en abierto en exygenlabs.com.')


def test_el_panel_la_pide_por_la_api_y_no_como_archivo():
    """El componente del Panel tiene que pedirla por `api` (que manda el token),
    nunca con un `fetch` a un archivo suelto — eso sería la misma fuga por otro lado."""
    ruta = os.path.join(SITIO, 'src', 'components', 'admin', 'MotorPrecios.js')
    if not os.path.exists(ruta):
        pytest.skip('el Panel no está en esta copia del repo')
    with open(ruta, encoding='utf-8') as fh:
        js = fh.read()
    assert "api.get('/admin/motor-precios')" in js, 'el Panel no la pide por la API'
    assert 'motor-precios.json' not in js, (
        'el Panel todavía apunta a un archivo suelto en vez de a la API')


def test_no_guarda_una_foto_sin_fecha():
    """Una foto sin `generado` no se puede fechar, y una foto que no se puede fechar
    se lee como si fuera de hoy. Ese es exactamente el engaño que el tablero evita."""
    src = _fuente()
    m = re.search(r"async def admin_motor_precios_guardar\(.*?\n(?=@|\Z)", src, re.S)
    assert m, 'no encontré el guardado'
    assert "payload.get('generado')" in m.group(0)
    assert 'status_code=400' in m.group(0)


# ---------- Aprobar un producto desde el Panel ----------

def test_aprobar_y_leer_decisiones_exigen_admin():
    """Aprobar un producto es dar de alta mercancía. No basta con estar logueado."""
    src = _fuente()
    for patron in (r"@api_router\.get\('/admin/motor-precios/decisiones'\)",
                   r"@api_router\.put\('/admin/motor-precios/decisiones/\{llave\}'\)"):
        m = re.search(patron + r"\s*\nasync def \w+\([^)]*\)", _fuente(), re.S)
        assert m, f'no encontré la ruta {patron}'
        assert 'get_current_admin' in m.group(0), m.group(0)
    assert 'motor_decisiones' in src


def test_un_producto_vetado_no_se_puede_aprobar():
    """El veto se comprueba EN EL SERVIDOR, no sólo en la pantalla.

    La lista del Panel ya viene filtrada, pero un botón no es una compuerta: cualquiera
    con sesión de admin puede llamar a la ruta a mano. Y lo que está en juego no es un
    renglón feo — la lista de Lumi trae testosterona, winstrol y trembolona."""
    m = re.search(r"async def admin_motor_decidir\(.*?\n(?=@|\Z)", _fuente(), re.S)
    assert m, 'no encontré el guardado de decisiones'
    cuerpo = m.group(0)
    assert "'vetados'" in cuerpo or '"vetados"' in cuerpo, (
        'no consulta la lista de vetados de la foto')
    assert 'status_code=409' in cuerpo, 'no rechaza un producto vetado'
    assert "decision == 'aprobado'" in cuerpo


def test_solo_acepta_tres_decisiones():
    """aprobado / descartado / pendiente. Cualquier otra cosa se rechaza: un valor libre
    aquí acaba siendo un estado que ningún script sabe interpretar."""
    cuerpo = re.search(r"async def admin_motor_decidir\(.*?\n(?=@|\Z)", _fuente(), re.S).group(0)
    assert "('aprobado', 'descartado', 'pendiente')" in cuerpo
    assert 'status_code=400' in cuerpo


# ---------- El pedido: renglones repetidos del mismo producto ----------

def test_el_inventario_se_reparte_SUMANDO_los_renglones_del_mismo_producto():
    """Pedir 40 dos veces son 80, no 40 y 40.

    El carrito puede mandar el MISMO producto en varios renglones. Sin agrupar, cada uno
    se reparte por su cuenta contra el MISMO inventario: el desglose de "cuántas salen ya
    y cuántas hay que mandar pedir" sale mal por partida doble, y el Panel manda pedir de
    menos. (Cuando el inventario todavía rechazaba pedidos, este mismo hueco dejaba salir
    el doble de piezas de las que había — barrido adversarial del 28-jul.)"""
    import server as _srv
    pflags = _catalogo(stock=40)
    agrupado = _srv._agrupar_por_producto(
        [_Item('OREXINA-10MG', 'Orexin A 10 mg', 40),
         _Item('OREXINA-10MG', 'Orexin A 10 mg', 40)], pflags)
    assert len(agrupado) == 1 and list(agrupado.values())[0]['total'] == 80, agrupado
    # y el checkout reparte contra ESE total agrupado, no renglón por renglón
    src = _fuente()
    ini = src.index('async def create_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]
    pos_agrupa = cuerpo.index('_agrupar_por_producto(')
    pos_aparta = cuerpo.index('_reservar_inventario(')
    assert pos_agrupa < pos_aparta, (
        'el checkout aparta ANTES de agrupar: dos renglones del mismo producto se '
        'reparten cada uno contra el inventario completo')


def test_el_descuento_de_inventario_avisa_cuando_no_encuentra_el_renglon():
    """Un descuento que no ocurre y no avisa se ve igual que uno que sí ocurrió.

    La llave de `db.stock` es `<slug>::<presentación>`, pero el carrito manda a veces un
    UUID y a veces el SKU. Cuando no coincidía, `update_one` devolvía cero modificados sin
    quejarse: el pedido salía y las piezas nunca bajaban."""
    src = _fuente()
    assert 'async def _descontar_inventario_vivo(' in src
    m = re.search(r"async def _descontar_inventario_vivo\(.*?\n(?=async def|\Z)", src, re.S)
    cuerpo = m.group(0)
    assert 'matched_count' in cuerpo, 'no comprueba si de verdad encontró el renglón'
    assert 'logger.warning' in cuerpo, 'falla en silencio'
    # y las dos direcciones —cobrar y cancelar— tienen que usar el MISMO resolvedor,
    # o cada ciclo de pedido+cancelación desbalancea el inventario.
    assert src.count('_descontar_inventario_vivo(') >= 3


# ---------- Canje total de puntos ----------

def test_si_los_puntos_pagan_todo_no_hay_comision_ni_puntos_nuevos():
    """Regla de Christian (2026-07-28): el canje al 100% se permite, pero ese pedido no
    paga comisión ni deposita puntos.

    Los puntos ya se ganaron y son del cliente, así que puede gastarlos completos. Lo que
    no puede pasar es que ADEMÁS salga una comisión calculada sobre el precio íntegro: en
    un pedido donde no entró un peso por la mercancía, eso es dinero que sale. El barrido
    adversarial lo puso en números: 80 viales, $0 de ingreso, $187,180 de comisión y
    $74,896 de costo — $262,076 de pérdida en un solo pedido."""
    src = _fuente()
    assert 'pagado_todo_con_puntos' in src, 'no existe la regla del canje total'
    m = re.search(r"pagado_todo_con_puntos = .*?\n(.*?)\n    if referrer", src, re.S)
    assert m, 'no encontré dónde se aplica'
    assert 'points_earned = 0' in m.group(0), 'sigue depositando puntos'
    assert 'if referrer and not pagado_todo_con_puntos:' in src, (
        'la comisión se sigue pagando aunque los puntos hayan cubierto todo')


# ---------- El pedido: el mismo producto con DOS nombres (UUID y SKU) ----------

class _Item:
    """Un renglón del carrito, con lo poco que mira el inventario."""

    def __init__(self, product_id, name, quantity):
        self.product_id, self.name, self.quantity = product_id, name, quantity


def _catalogo(stock=40):
    """Orexin A, con su UUID y su SKU — las dos llaves que acepta el checkout."""
    doc = {'id': 'a1b2c3d4-uuid', 'sku': 'OREXINA-10MG', 'name': 'Orexin A 10 mg',
           'stock': stock, 'price': 9359.0}
    return {doc['id']: doc, doc['sku']: doc}


def test_el_mismo_producto_con_UUID_y_con_SKU_es_UN_solo_producto():
    """El hueco que quedó vivo después del 28-jul.

    Sumar por `it.product_id` no basta: el MISMO producto viaja a veces con su UUID y a
    veces con su SKU, y el checkout acepta las dos llaves a propósito (el carrito manda
    cualquiera). Agrupando por el texto, `40 del UUID` y `40 del SKU` son dos productos
    distintos, cada uno pasa la prueba contra el MISMO inventario (40 ≤ 40 y otra vez
    40 ≤ 40)... y el descuento sí los junta, porque busca por id O sku. Ochenta piezas de
    las cuarenta que hay, y sin tope: siempre se puede añadir otro renglón."""
    import server as _srv
    pflags = _catalogo(stock=40)
    agrupado = _srv._agrupar_por_producto(
        [_Item('a1b2c3d4-uuid', 'Orexin A 10 mg', 40),
         _Item('OREXINA-10MG', 'Orexin A 10 mg', 40)], pflags)
    assert len(agrupado) == 1, f'los cuenta como dos productos distintos: {agrupado}'
    assert agrupado['a1b2c3d4-uuid']['total'] == 80, agrupado
    # y con eso el checkout ya ve que pide 80 de las 40 que hay
    hay = int(agrupado['a1b2c3d4-uuid']['doc']['stock'])
    assert agrupado['a1b2c3d4-uuid']['total'] > hay, 'el pedido pasaría con 80 de 40'


def test_un_producto_que_no_se_resuelve_no_se_junta_con_otro():
    """Sin ficha en el catálogo no hay a quién parecerse: cada uno va por su lado y el
    checkout no le inventa un límite (a esas alturas ya lo rechazó por huérfano)."""
    import server as _srv
    agrupado = _srv._agrupar_por_producto(
        [_Item('no-existe-1', 'X', 2), _Item('no-existe-2', 'Y', 3)], {})
    assert set(agrupado) == {'no-existe-1', 'no-existe-2'}


# ---------- La carrera entre revisar y descontar ----------

class _Productos:
    """Lo mínimo de una colección de Mongo para probar la reserva: `$or`, `$gte` y `$inc`."""

    def __init__(self, docs):
        self.docs = docs

    @staticmethod
    def _casa(doc, query):
        for clave, valor in query.items():
            if clave == '$or':
                if not any(all(doc.get(k) == v for k, v in cond.items()) for cond in valor):
                    return False
            elif isinstance(valor, dict) and '$gte' in valor:
                if int(doc.get(clave) or 0) < valor['$gte']:
                    return False
            elif doc.get(clave) != valor:
                return False
        return True

    async def find_one(self, query, *a, **k):
        for doc in self.docs:
            if self._casa(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query, cambio):
        for doc in self.docs:
            if self._casa(doc, query):
                for campo, delta in cambio.get('$inc', {}).items():
                    doc[campo] = int(doc.get(campo) or 0) + delta
                return type('R', (), {'matched_count': 1, 'modified_count': 1})()
        return type('R', (), {'matched_count': 0, 'modified_count': 0})()


def _con_db_falsa(docs, corutina):
    """Corre `corutina` con `server.db.products` apuntando a un catálogo de mentiras."""
    import asyncio
    import server as _srv
    productos = _Productos(docs)
    original = _srv.db
    # La reserva mira los DOS inventarios. Sin renglón de inventario vivo manda el
    # contador del catálogo, que es justo lo que estas pruebas ejercitan.
    _srv.db = type('DB', (), {'products': productos, 'stock': _StockLeible([])})()
    try:
        return asyncio.new_event_loop().run_until_complete(corutina(_srv))
    finally:
        _srv.db = original


def test_dos_pedidos_a_la_vez_no_dejan_el_inventario_en_NEGATIVO():
    """La carrera entre mirar y descontar.

    El stock se miraba arriba y se restaba mucho después, y entre las dos cosas cabía otro
    pedido entero: dos clientes veían la última pieza, los dos la tomaban y el inventario
    terminaba en −1. No hace falta mala fe, basta con dos personas comprando a la vez.

    Con el envío partido el segundo cliente YA NO SE RECHAZA —ninguna venta se bloquea por
    inventario— pero su pieza sale como POR SURTIR, no de una bodega que no la tiene. Un
    inventario en negativo no es un dato: es una mentira con signo."""
    doc = {'id': 'uuid-1', 'sku': 'SKU-1', 'name': 'Orexin A', 'stock': 1}

    async def dos_pedidos(_srv):
        pedido = {'uuid-1': {'total': 1, 'nombre': 'Orexin A', 'doc': doc}}
        primero = await _srv._reservar_inventario(pedido)
        segundo = await _srv._reservar_inventario(pedido)
        return primero, segundo

    (res1, _v1, falta1), (res2, _v2, falta2) = _con_db_falsa([doc], dos_pedidos)
    assert res1 == [('uuid-1', 1)] and not falta1, 'el primero debería llevarse la pieza'
    assert res2 == [], 'el segundo se llevó una pieza que ya no existía'
    assert falta2 == [{'product_id': 'uuid-1', 'name': 'Orexin A', 'pedidas': 1,
                       'en_mano': 0, 'por_surtir': 1}], falta2
    assert doc['stock'] == 0, f'el inventario quedó en {doc["stock"]}'


def test_lo_que_NO_alcanza_se_manda_pedir_y_lo_demas_sale_igual():
    """⛔ LA REGLA MADRE (Christián, 2026-07-30): ninguna venta se bloquea por inventario.
    «Si piden 40 y solo tengo 20, se mandan los 20 y se mandan pedir los otros 20.»

    Aquí llegó a haber un rechazo: si UN renglón no alcanzaba, se devolvía todo lo ya
    apartado y el pedido moría. Eso tiraba la venta entera por una pieza. Ahora el renglón
    que sí hay sale completo, el que no alcanza sale partido, y nada se secuestra."""
    a = {'id': 'uuid-A', 'sku': 'A', 'name': 'Sí hay', 'stock': 10}
    b = {'id': 'uuid-B', 'sku': 'B', 'name': 'Solo hay una', 'stock': 1}

    async def pedido(_srv):
        return await _srv._reservar_inventario({
            'uuid-A': {'total': 5, 'nombre': 'Sí hay', 'doc': a},
            'uuid-B': {'total': 4, 'nombre': 'Solo hay una', 'doc': b},
        })

    reservado, _vivo, por_surtir = _con_db_falsa([a, b], pedido)
    assert ('uuid-A', 5) in reservado, 'el renglón completo se cayó por culpa del otro'
    assert ('uuid-B', 1) in reservado, 'no se llevó la única pieza que sí había'
    assert a['stock'] == 5 and b['stock'] == 0, (a['stock'], b['stock'])
    assert por_surtir == [{'product_id': 'uuid-B', 'name': 'Solo hay una', 'pedidas': 4,
                           'en_mano': 1, 'por_surtir': 3}], por_surtir


def test_la_reserva_encuentra_el_producto_tanto_por_id_como_por_sku():
    """La llave con la que se agrupa es el `id`, pero el catálogo de respaldo guarda los
    productos por SKU. Si la reserva sólo buscara por una, no restaría nada y el pedido
    saldría entero "por surtir" con la bodega llena."""
    doc = {'sku': 'SOLO-SKU', 'name': 'Sin UUID', 'stock': 3}

    async def reservar(_srv):
        return await _srv._reservar_inventario(
            {'SOLO-SKU': {'total': 2, 'nombre': 'Sin UUID', 'doc': doc}})

    reservado, _vivo, por_surtir = _con_db_falsa([doc], reservar)
    assert reservado == [('SOLO-SKU', 2)] and not por_surtir
    assert doc['stock'] == 1


def test_la_venta_directa_tambien_descuenta_lo_que_se_llevo():
    """No descontaba, y `restore_order_stock` SÍ devuelve al cancelar o al borrar: cada
    venta directa cancelada le regalaba al inventario piezas que nunca salieron. Es la
    misma asimetría que dejó Orexin A en 43 cuando tenía 40, viva en el otro camino."""
    src = _fuente()
    ini = src.index('async def admin_create_order(')
    cuerpo = src[ini:src.index('async def', ini + 10)]
    assert "'$inc': {'stock':" in cuerpo, (
        'la venta directa no baja el inventario, pero cancelarla sí lo sube')
    assert '_descontar_inventario_vivo(' in cuerpo, 'no baja el inventario VIVO'
    assert "{'sku': item.product_id}" in cuerpo, \
        'busca distinto que la devolución: el inventario se desbalancea'


# ---------- El inventario vivo: la llave que nunca casaba ----------

class _Stock:
    """Lo mínimo de `db.stock`: buscar por `key`, exigir `qty >= n`, `$inc` y `$set`."""

    def __init__(self, filas):
        self.filas = filas

    @staticmethod
    def _casa(doc, query):
        for clave, valor in query.items():
            if isinstance(valor, dict) and '$gte' in valor:
                if int(doc.get(clave) or 0) < valor['$gte']:
                    return False
            elif isinstance(valor, dict) and '$in' in valor:
                if doc.get(clave) not in valor['$in']:
                    return False
            elif doc.get(clave) != valor:
                return False
        return True

    async def update_one(self, query, cambio):
        for doc in self.filas:
            if self._casa(doc, query):
                for campo, delta in cambio.get('$inc', {}).items():
                    doc[campo] = int(doc.get(campo) or 0) + delta
                for campo, valor in cambio.get('$set', {}).items():
                    doc[campo] = valor
                return type('R', (), {'matched_count': 1, 'modified_count': 1})()
        return type('R', (), {'matched_count': 0, 'modified_count': 0})()

    async def delete_many(self, query):
        antes = len(self.filas)
        self.filas = [d for d in self.filas if not self._casa(d, query)]
        return type('R', (), {'deleted_count': antes - len(self.filas)})()


def _con_stock(filas, corutina):
    import asyncio
    import server as _srv
    stock = _Stock(filas)
    original = _srv.db
    _srv.db = type('DB', (), {'stock': stock})()
    try:
        asyncio.new_event_loop().run_until_complete(corutina(_srv))
    finally:
        _srv.db = original
    return stock


_DOC_BRONCHOGEN = {'id': 'd6a0a69f-uuid', 'sku': 'BRONCHOGEN-10MG',
                   'slug': 'bronchogen-10-mg', 'presentation': '10 mg'}


@pytest.mark.parametrize('slug,pres,familia', [
    ('bronchogen-10-mg', '10 mg', 'bronchogen'),
    ('hgh-24-iu', '24 IU', 'hgh'),
    ('lemon-bottle-10-ml', '10 mL', 'lemon-bottle'),
    ('hgh-fragment-176-191-15-mg', '15 mg', 'hgh-fragment-176-191'),
    ('snap-8-100-mg', '100 mg', 'snap-8'),
    ('sin-presentacion', '', 'sin-presentacion'),
])
def test_la_familia_del_slug(slug, pres, familia):
    import server as _srv
    assert _srv._familia_del_slug(slug, pres) == familia


def test_la_llave_del_inventario_vivo_incluye_la_del_panel():
    """El Panel guarda `fallback-bronchogen::10 mg`; en `db.products` ese producto es
    `bronchogen-10-mg` con su UUID y su SKU. Ninguna de las tres llaves que se probaban
    puede ser jamás esa cadena."""
    import server as _srv
    llaves = _srv.llaves_de_inventario_vivo('d6a0a69f-uuid', _DOC_BRONCHOGEN)
    assert 'fallback-bronchogen::10 mg' in llaves, llaves


def test_el_inventario_vivo_SI_baja_en_un_producto_con_presentaciones():
    """⛔ EL RIESGO DE VENDER LO QUE YA NO HAY. En todo producto con presentaciones —o
    sea, casi todo el catálogo— `_descontar_inventario_vivo` no encontraba renglón y
    `db.stock.qty` no bajaba NUNCA. Y `db.stock` es lo que la ficha del sitio usa para
    pintar "EN MANO / entrega inmediata": se anunciaba existencia física de piezas ya
    vendidas, indefinidamente y sin que nada lo dijera."""
    filas = [{'key': 'fallback-bronchogen::10 mg', 'qty': 20, 'in_hand': True}]
    stock = _con_stock(filas, lambda s: s._descontar_inventario_vivo(
        'd6a0a69f-uuid', _DOC_BRONCHOGEN, -3))
    assert stock.filas[0]['qty'] == 17, stock.filas


def test_el_inventario_vivo_nunca_queda_en_negativo():
    """Un inventario vivo negativo no es un dato: es una mentira con signo, y se pinta en
    la ficha del producto como si fuera real."""
    filas = [{'key': 'fallback-bronchogen::10 mg', 'qty': 2}]
    stock = _con_stock(filas, lambda s: s._descontar_inventario_vivo(
        'd6a0a69f-uuid', _DOC_BRONCHOGEN, -5))
    assert stock.filas[0]['qty'] == 0, stock.filas


def test_devolver_al_cancelar_usa_la_MISMA_llave_que_al_vender():
    """Si vender y cancelar buscan de formas distintas, el inventario se desbalancea con
    cada ciclo — ya pasó y dejó Orexin A en 43 cuando tenía 40."""
    filas = [{'key': 'fallback-bronchogen::10 mg', 'qty': 20}]
    stock = _con_stock(filas, lambda s: s._descontar_inventario_vivo(
        'BRONCHOGEN-10MG', _DOC_BRONCHOGEN, -4))
    assert stock.filas[0]['qty'] == 16
    _con_stock(stock.filas, lambda s: s._descontar_inventario_vivo(
        'd6a0a69f-uuid', _DOC_BRONCHOGEN, 4))
    assert stock.filas[0]['qty'] == 20, stock.filas


def test_el_checkout_le_pasa_al_inventario_vivo_el_slug_y_la_presentacion():
    """Sin `slug` ni `presentation` en la proyección no se puede armar la llave del
    Panel, y el arreglo de arriba queda muerto en el camino real."""
    src = _fuente()
    ini = src.index('_pdocs = await db.products.find(')
    trozo = src[ini:ini + 900]
    assert "'slug': 1" in trozo and "'presentation': 1" in trozo, trozo
    # Y el peso, por la misma razón: sin él el envío se cotiza con el peso por
    # omisión aunque el producto ya tenga el suyo capturado, y la paquetería cobra
    # la diferencia al recibir el paquete.
    assert "'weight_kg': 1" in trozo, trozo


def test_borrar_un_producto_se_lleva_su_renglon_de_inventario_vivo():
    """Una llave huérfana en `db.stock` es existencia de algo que ya no existe: el Panel
    la sigue mostrando y no hay contra qué reconciliarla."""
    filas = [{'key': 'fallback-bronchogen::10 mg', 'qty': 20},
             {'key': 'fallback-otro::5 mg', 'qty': 7}]
    import server as _srv
    stock = _Stock(filas)
    assert _srv.llaves_de_inventario_vivo('d6a0a69f-uuid', _DOC_BRONCHOGEN)
    import asyncio
    asyncio.new_event_loop().run_until_complete(stock.delete_many(
        {'key': {'$in': _srv.llaves_de_inventario_vivo('d6a0a69f-uuid', _DOC_BRONCHOGEN)}}))
    assert [f['key'] for f in stock.filas] == ['fallback-otro::5 mg'], stock.filas


# ---------- La venta directa: el precio lo pone el servidor ----------

def test_la_venta_directa_retasa_contra_el_catalogo():
    """⛔ El checkout público se blindó el 2026-07-27 y la VENTA DIRECTA se quedó fuera:
    sumaba `i.price` tal como venía en la petición, así que se podía registrar un pedido
    de $0 —y de paso disparar los puntos de lealtad y el descuento de inventario— con
    sólo mandar el precio que uno quisiera."""
    src = _fuente()
    ini = src.index('async def admin_create_order(')
    fin = src.index('async def ', ini + 10)
    cuerpo = src[ini:fin]
    assert 'db.products.find(' in cuerpo, 'la venta directa no consulta el catálogo'
    assert 'i.price = float(real)' in cuerpo, 'la venta directa no retasa el renglón'
    # y retasa ANTES de sumar: el orden es toda la protección
    assert cuerpo.index('i.price = float(real)') < cuerpo.index('subtotal = sum('), cuerpo


def test_la_venta_directa_no_acepta_un_producto_que_no_existe():
    src = _fuente()
    ini = src.index('async def admin_create_order(')
    cuerpo = src[ini:src.index('async def ', ini + 10)]
    assert '_huerfanos' in cuerpo, 'un producto sin ficha no se puede tasar y sí se vendía'
    assert 'Cantidad inválida' in cuerpo, 'una cantidad negativa seguía sumando al total'


def test_el_inventario_vivo_del_panel_no_admite_cantidades_negativas():
    src = _fuente()
    ini = src.index('async def set_stock(')
    cuerpo = src[ini:src.index('@api_router', ini)]
    assert "max(0, int(payload['qty']))" in cuerpo, cuerpo


# ---------- Envío: la política es deliberada ----------

def test_el_envio_se_cobra_parejo_y_nunca_se_resta():
    """Christian cobra $250 parejo (2026-07-28). Lo que se comprueba aquí no es el número, es
    que el envío sólo pueda SUMAR: no existe ningún descuento de $250 en ninguna parte,
    así que no hay forma de que se aplique donde no corresponde ni dos veces."""
    src = _fuente()
    assert 'COBRAR_ENVIO = True' in src, 'cambió la política de envío sin decirlo'
    assert 'shipping = shipping_for(paid_merchandise) if COBRAR_ENVIO else 0' in src
    assert 'total = paid_merchandise + shipping' in src, 'el envío tiene que SUMAR'
    # Skydropx no cambia la política: nace apagado y el envío sigue sin cobrarse.
    import envios
    assert envios.COTIZAR_EN_CHECKOUT is False, 'la cotización en vivo sigue apagada'
    assert '- 250' not in src and '-250' not in src.replace('-2500', ''), \
        'apareció una resta de 250: el envío no es un descuento'


# ---------- La foto no puede pasar por nueva cuando está vieja ----------

def test_la_frescura_la_calcula_el_servidor_no_la_mac():
    """El Panel enseñaba la foto y nada decía qué tan vieja era.

    `generado` lo escribe la Mac de Christian con su reloj local y sin zona horaria:
    si esa Mac corre el script sin reconstruir la base, o tiene el reloj atrasado, una
    foto vieja se ve nueva. El 2026-07-28 la foto era de las 16:46 y la base de las
    18:24 — el Panel estaba enseñando un catálogo que ya no existía. Lo único que el
    servidor sabe de verdad es cuándo la recibió.
    """
    from datetime import datetime, timedelta, timezone
    import server

    ahora = datetime.now(timezone.utc)
    fresca = server._frescura_de_la_foto((ahora - timedelta(hours=2)).isoformat())
    assert fresca['vencida'] is False and 1.5 < fresca['horas'] < 2.5

    vieja = server._frescura_de_la_foto((ahora - timedelta(hours=30)).isoformat())
    assert vieja['vencida'] is True and vieja['horas'] > 24

    # Sin fecha o con basura: se asume vencida. Nunca "fresca por no saber".
    for basura in ('', None, 'ayer'):
        assert server._frescura_de_la_foto(basura)['vencida'] is True


def test_la_foto_sale_siempre_con_su_frescura():
    """La ruta no puede devolver la foto pelona: el dato de antigüedad va pegado."""
    src = _fuente()
    ini = src.index("async def admin_motor_precios(")
    cuerpo = src[ini:src.index('@api_router', ini)]
    assert "foto['frescura'] = _frescura_de_la_foto(" in cuerpo
    assert "return doc.get('valor') or {}" not in cuerpo, \
        'volvió a devolverse la foto sin decir de cuándo es'


# ---------- El contador sembrado prometía más piezas de las que hay ----------
#
# Hallazgo de Codex (2026-07-30). Hay DOS inventarios: `db.products.stock`, un contador
# que nace sembrado al dar de alta el producto (casi todo el catálogo en 40), y `db.stock`,
# las piezas reales que el Panel captura y que la ficha del sitio pinta como "en mano".
# El checkout validaba y apartaba contra el SEMBRADO, y al real solo le restaba DESPUÉS de
# grabar el pedido: si no alcanzaba, lo dejaba en 0 con una advertencia en la bitácora y la
# venta salía igual. En vivo, 191 de 193 productos anunciaban 40 con 20 piezas de verdad
# —Orexin A 10 mg entre ellos—, así que un pedido de 21 pasaba entero.

_DOC_OREXIN = {'id': 'orexin-uuid', 'sku': 'OREXINA-10MG', 'name': 'Orexin A 10 mg',
               'slug': 'orexin-a-10-mg', 'presentation': '10 mg', 'stock': 40}


class _StockLeible(_Stock):
    """`_Stock` más el `find_one` que necesita la revisión del checkout."""

    async def find_one(self, query, *a, **k):
        for doc in self.filas:
            if self._casa(doc, query):
                return dict(doc)
        return None


def _con_inventarios(filas, productos, corutina):
    """Corre `corutina` con los DOS inventarios de mentiras: catálogo y piezas reales."""
    import asyncio
    import server as _srv
    stock, prods = _StockLeible(filas), _Productos(productos)
    original = _srv.db
    _srv.db = type('DB', (), {'stock': stock, 'products': prods})()
    try:
        res = asyncio.new_event_loop().run_until_complete(corutina(_srv))
    finally:
        _srv.db = original
    return res, stock


def test_lo_disponible_es_lo_MENOR_entre_el_contador_y_las_piezas_reales():
    """Orexin A 10 mg: el catálogo decía 40, en la bodega había 20. Mandan las 20."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 20, 'in_hand': False}]
    hay, _ = _con_inventarios(filas, [dict(_DOC_OREXIN)],
                              lambda s: s._disponible_de('orexin-uuid', _DOC_OREXIN))
    assert hay == 20, f'el checkout sigue creyéndole al contador sembrado: {hay}'


def test_sin_renglon_de_inventario_real_manda_el_contador_del_catalogo():
    """"No sé cuántas hay" no es "no hay": un producto al que nadie le capturó inventario
    no puede quedarse sin venderse. Se avisa en la bitácora y sigue el contador."""
    hay, _ = _con_inventarios([], [dict(_DOC_OREXIN)],
                              lambda s: s._disponible_de('orexin-uuid', _DOC_OREXIN))
    assert hay == 40


def test_pedir_21_con_20_piezas_reales_se_ACEPTA_PARTIDO():
    """EL CASO DE CODEX, ahora con la regla de la casa: Orexin A 10 mg, contador 40,
    bodega 20, pedido de 21.

    Este caso ya vivió los dos extremos. Primero pasaba entero y se cobraban 21 piezas de
    las 20 que hay, sin decírselo a nadie. Después se puso un rechazo duro — y eso tiraba
    la venta, que es peor. Christián (2026-07-30): «se mandan los 20 y se mandan pedir los
    otros 20; aquí lo principal es vender». Salen 20 YA y 1 queda por surtir."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 20, 'in_hand': False}]
    doc = dict(_DOC_OREXIN)
    pedido = {'orexin-uuid': {'total': 21, 'nombre': 'Orexin A 10 mg', 'doc': doc}}

    (hay, (reservado, vivo, por_surtir)), stock = _con_inventarios(
        filas, [doc], lambda s: _mirar_y_apartar(s, 'orexin-uuid', doc, pedido))

    assert hay == 20, f'lo que hay en mano se leyó como {hay}'
    assert reservado == [('orexin-uuid', 20)], f'no apartó las 20 que sí hay: {reservado}'
    assert vivo == [('fallback-orexin-a::10 mg', 20)], vivo
    assert por_surtir == [{'product_id': 'orexin-uuid', 'name': 'Orexin A 10 mg',
                           'pedidas': 21, 'en_mano': 20, 'por_surtir': 1}], por_surtir
    assert stock.filas[0]['qty'] == 0, f'la bodega no quedó en cero: {stock.filas}'
    assert doc['stock'] == 20, f'el contador bajó por otro número: {doc["stock"]}'


async def _mirar_y_apartar(s, clave, doc, pedido):
    return await s._disponible_de(clave, doc), await s._reservar_inventario(pedido)


def test_pedir_20_con_20_piezas_reales_sale_COMPLETO_y_sin_por_surtir():
    """Cuando alcanza, no hay nada que mandar pedir y el cliente no ve ningún aviso."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 20, 'in_hand': False}]
    doc = dict(_DOC_OREXIN)
    pedido = {'orexin-uuid': {'total': 20, 'nombre': 'Orexin A 10 mg', 'doc': doc}}

    (reservado, vivo, por_surtir), stock = _con_inventarios(
        filas, [doc], lambda s: s._reservar_inventario(pedido))

    assert not por_surtir, por_surtir
    assert reservado == [('orexin-uuid', 20)] and vivo == [('fallback-orexin-a::10 mg', 20)]
    assert stock.filas[0]['qty'] == 0, stock.filas


def test_un_producto_en_CERO_se_vende_entero_sobre_pedido():
    """Retatrutida 120 mg y Vitamina D3 estaban en 0 y no se podían comprar. Con la regla
    madre eso no puede pasar: se vende completo, todo por surtir, y el cliente lo sabe
    antes de pagar."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 0, 'in_hand': False}]
    doc = dict(_DOC_OREXIN, stock=0)
    pedido = {'orexin-uuid': {'total': 3, 'nombre': 'Orexin A 10 mg', 'doc': doc}}

    (reservado, vivo, por_surtir), stock = _con_inventarios(
        filas, [doc], lambda s: s._reservar_inventario(pedido))

    assert reservado == [] and vivo == []
    assert por_surtir == [{'product_id': 'orexin-uuid', 'name': 'Orexin A 10 mg',
                           'pedidas': 3, 'en_mano': 0, 'por_surtir': 3}], por_surtir
    assert stock.filas[0]['qty'] == 0, 'dejó la bodega en negativo'
    assert doc['stock'] == 0


def test_los_dos_inventarios_bajan_SIEMPRE_por_el_MISMO_numero():
    """Si el contador del catálogo tiene menos que la bodega, manda el chico y a la bodega
    se le devuelve la diferencia. Cuando cada lado bajaba por su cuenta, cada ciclo de
    pedido y cancelación los desbalanceaba — Orexin A quedó en 43 con 40 (2026-07-27)."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 20, 'in_hand': False}]
    doc = dict(_DOC_OREXIN, stock=5)          # el catálogo dice 5 y la bodega 20
    pedido = {'orexin-uuid': {'total': 10, 'nombre': 'Orexin A 10 mg', 'doc': doc}}

    (reservado, vivo, por_surtir), stock = _con_inventarios(
        filas, [doc], lambda s: s._reservar_inventario(pedido))

    assert reservado == [('orexin-uuid', 5)] and vivo == [('fallback-orexin-a::10 mg', 5)]
    assert doc['stock'] == 0
    assert stock.filas[0]['qty'] == 15, f'los dos no bajaron por el mismo número: {stock.filas}'
    assert por_surtir[0]['en_mano'] == 5 and por_surtir[0]['por_surtir'] == 5


def test_dos_pedidos_a_la_vez_no_se_llevan_las_MISMAS_piezas_reales():
    """Mirar y restar en el mismo paso. Si no, dos clientes se llevan las mismas 20 piezas
    y la bodega termina en negativo. El segundo vende igual: lo suyo va por surtir."""
    filas = [{'key': 'fallback-orexin-a::10 mg', 'qty': 20}]
    doc = dict(_DOC_OREXIN)
    pedido = {'orexin-uuid': {'total': 20, 'nombre': 'Orexin A 10 mg', 'doc': doc}}

    async def dos(s):
        return await s._reservar_inventario(pedido), await s._reservar_inventario(pedido)

    ((r1, v1, f1), (r2, v2, f2)), stock = _con_inventarios(filas, [doc], dos)
    assert r1 and v1 and not f1, 'el primero debería llevarse las piezas'
    assert r2 == [] and v2 == [], 'el segundo se llevó piezas que ya no existían'
    assert f2 == [{'product_id': 'orexin-uuid', 'name': 'Orexin A 10 mg', 'pedidas': 20,
                   'en_mano': 0, 'por_surtir': 20}], f2
    assert stock.filas[0]['qty'] == 0, stock.filas


def test_cancelar_devuelve_LO_APARTADO_y_no_lo_pedido():
    """⛔ Con el envío partido, pedido y apartado dejaron de ser lo mismo. Un pedido de 40
    con 20 en bodega solo se llevó 20; devolver 40 al cancelar le REGALA 20 piezas al
    inventario. Es la misma asimetría que dejó Orexin A en 43 cuando tenía 40."""
    import server as _srv
    orden = {'id': 'o-1', 'stock_taken': {'orexin-uuid': 20},
             'items': [{'product_id': 'orexin-uuid', 'quantity': 40}]}
    assert _srv._piezas_a_devolver(orden) == {'orexin-uuid': 20}


def test_un_pedido_viejo_sin_stock_taken_se_devuelve_por_cantidad():
    """Los pedidos anteriores al envío partido se llevaban justo lo que pedían: devolverlos
    por cantidad es exactamente lo que hacían, y no puede romperse al leerlos hoy."""
    import server as _srv
    orden = {'id': 'o-viejo',
             'items': [{'product_id': 'orexin-uuid', 'quantity': 3},
                       {'product_id': 'orexin-uuid', 'quantity': 2}]}
    assert _srv._piezas_a_devolver(orden) == {'orexin-uuid': 5}


def test_el_checkout_no_rechaza_por_inventario_y_marca_lo_que_falta():
    """Candado sobre el código: la regla madre no puede volver a invertirse sin que esta
    prueba truene. El checkout no puede tener un 409 por inventario, tiene que apartar
    ANTES de grabar, y tiene que dejar el desglose en el pedido para el cliente y el
    Panel."""
    src = _fuente()
    ini = src.index('async def create_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]

    for prohibido in ('No tenemos suficiente de', 'Se agotó mientras comprabas'):
        assert prohibido not in cuerpo, (
            f'volvió el rechazo por inventario ({prohibido!r}): ninguna venta se bloquea '
            f'por falta de piezas — se parte y se manda pedir')
    assert "hay = int(d.get('stock') or 0)" not in cuerpo, \
        'volvió a leer el contador sembrado a secas'

    pos_apartar = cuerpo.index('_reservar_inventario(')
    pos_grabar = cuerpo.index('db.orders.insert_one(')
    assert pos_apartar < pos_grabar, \
        'aparta DESPUÉS de grabar: un fallo en medio deja el pedido sin piezas apartadas'
    for campo in ('order.backorder =', 'order.backorder_items =', 'order.stock_taken ='):
        assert campo in cuerpo, f'el pedido no guarda {campo!r}'


def test_el_aviso_de_sobre_pedido_viaja_en_la_respuesta():
    """El sitio pinta el aviso con lo que devuelve el checkout. Si estos campos no salen
    del modelo, el cliente paga sin enterarse de que su pedido llega en dos entregas."""
    from models import Order
    campos = Order.model_fields
    assert 'backorder' in campos and 'backorder_items' in campos, sorted(campos)
    assert campos['backorder'].default is False
    src = _fuente()
    ini = src.index('async def create_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]
    assert 'result = clean(order.model_dump())' in cuerpo, (
        'la respuesta ya no sale del pedido completo: el aviso no llegaría al navegador')


def test_el_inventario_real_ya_no_se_resta_dos_veces():
    """Se aparta en la reserva; si además se restara renglón por renglón después de
    grabar, cada pedido se llevaría el doble de piezas reales."""
    src = _fuente()
    ini = src.index('async def create_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]
    assert '_descontar_inventario_vivo(' not in cuerpo, \
        'el checkout aparta Y descuenta: el inventario real baja al doble'


# ---------- El aviso de envío partido en el correo ----------

_PEDIDO_PARTIDO = {
    'order_number': 'EX-TEST', 'subtotal': 1000, 'discount': 0, 'shipping': 0,
    'total': 1000, 'payment_method': 'spei',
    'items': [{'name': 'Orexin A 10 mg', 'price': 9359, 'quantity': 21}],
    'customer': {'full_name': 'Prueba', 'email': 'x@y.com', 'address': 'Calle 1'},
    'backorder': True,
    'backorder_items': [
        {'product_id': 'p1', 'name': 'Orexin A 10 mg', 'pedidas': 21,
         'en_mano': 20, 'por_surtir': 1},
        {'product_id': 'p2', 'name': 'Vitamina D3 10 mL', 'pedidas': 2,
         'en_mano': 0, 'por_surtir': 2},
    ],
}


@pytest.mark.parametrize('lang,frases', [
    ('es', ['DOS entregas', '20 de 21 salen ya, 1 sobre pedido', 'las 2 sobre pedido',
            '2 a 5 dias habiles', 'alrededor de una semana despues']),
    ('en', ['TWO deliveries', '20 of 21 ship now, 1 on backorder', 'all 2 on backorder',
            '2 to 5 business days', 'about a week later']),
    ('pt', ['DUAS entregas', '20 de 21 saem agora, 1 sob encomenda', 'as 2 sob encomenda',
            '2 a 5 dias uteis', 'cerca de uma semana depois']),
])
def test_el_correo_del_pedido_avisa_del_envio_partido_en_los_tres_idiomas(lang, frases):
    """El correo es el papel que le queda al cliente. Una entrega en dos partes que solo
    se anunció en una pantalla es una sorpresa una semana después."""
    import emails
    h = emails._order_email_html(_PEDIDO_PARTIDO, emails.ORDER_COPY[lang], 'https://x/y')
    for f in frases:
        assert f in h, f'falta {f!r} en el correo en {lang}'


def test_un_pedido_completo_no_lleva_aviso_de_sobre_pedido():
    """Si todo sale ya, el aviso no aparece: asustar sin motivo también cuesta ventas."""
    import emails
    completo = dict(_PEDIDO_PARTIDO, backorder=False, backorder_items=[])
    h = emails._order_email_html(completo, emails.ORDER_COPY['es'], 'https://x/y')
    assert 'DOS entregas' not in h and 'sobre pedido' not in h


# ---------- El aviso interno de compra ----------
#
# Christián (2026-07-30): un correo por cada pedido para saber qué preparar y qué mandar
# pedir. No es el correo del cliente con otro membrete: es una ORDEN DE TRABAJO.

_PEDIDO_AVISO = {
    'order_number': 'EX-20260730-9999', 'total': 167058.0, 'paid': False,
    'payment_method': 'spei', 'status': 'pendiente', 'shipping': 0,
    'items': [{'name': 'Orexin A 10 mg', 'presentation': '10 mg', 'quantity': 21,
               'price': 9359}],
    'customer': {'full_name': 'Aidee Liliana García', 'phone': '5555555555',
                 'email': 'cliente@example.com', 'address': 'Calle 1', 'city': 'CDMX',
                 'state': 'CDMX', 'postal_code': '01000', 'country': 'MX'},
    'backorder': True,
    'backorder_items': [{'product_id': 'p1', 'name': 'Orexin A 10 mg', 'pedidas': 21,
                         'en_mano': 20, 'por_surtir': 1}],
}


def test_el_aviso_de_compra_dice_QUE_MANDAR_PEDIR_y_va_primero():
    """Si el desglose va hasta abajo, quien prepara manda el paquete incompleto sin
    saberlo y nadie sale a comprarle al proveedor lo que falta."""
    import emails
    h = emails._aviso_compra_html(_PEDIDO_AVISO, 'https://exygenlabs.com/admin')
    assert 'HAY QUE MANDAR PEDIR' in h
    assert 'salen ya: <b>20</b>' in h and 'mandar pedir: <b>1</b>' in h
    assert h.index('HAY QUE MANDAR PEDIR') < h.index('QUÉ VA EN LA CAJA'), (
        'el desglose de lo que falta quedó debajo de la lista de empaque')


def test_el_aviso_de_compra_trae_todo_lo_que_hace_falta_para_actuar():
    import emails
    h = emails._aviso_compra_html(_PEDIDO_AVISO, 'https://exygenlabs.com/admin')
    for dato in ('EX-20260730-9999', 'Orexin A 10 mg', '×21', 'Aidee Liliana García',
                 '5555555555', 'cliente@example.com', 'Calle 1', 'CDMX', '01000',
                 'spei', 'pendiente', 'https://exygenlabs.com/admin'):
        assert dato in h, f'falta {dato!r} en el aviso'
    assert 'TODAVÍA NO' in h, 'no dice si ya pagó — es lo que decide si se manda o no'


def test_un_pedido_completo_no_lleva_el_bloque_de_mandar_pedir():
    import emails
    completo = dict(_PEDIDO_AVISO, backorder=False, backorder_items=[])
    h = emails._aviso_compra_html(completo, 'https://x/admin')
    assert 'HAY QUE MANDAR PEDIR' not in h


def test_el_aviso_va_al_correo_que_Christian_lee_y_es_configurable():
    """Estaba clavado en hola@exygenlabs.com, el buzón de la tienda: ahí un aviso que
    exige moverse se pierde entre los correos de clientes."""
    import importlib, os
    import emails
    assert emails.admin_notify_address() == 'exygenlabs@gmail.com'
    os.environ['ADMIN_NOTIFY_EMAIL'] = 'otro@exygenlabs.com'
    try:
        assert emails.admin_notify_address() == 'otro@exygenlabs.com'
    finally:
        del os.environ['ADMIN_NOTIFY_EMAIL']
    importlib.reload  # (no hace falta recargar: la función lee el entorno cada vez)


def test_el_asunto_avisa_del_sobre_pedido_y_distingue_pagado():
    import asyncio
    import emails
    vistos = []

    async def falso(subject, html_body):
        vistos.append(subject)

    original, emails.send_admin_notification = emails.send_admin_notification, falso
    os_flag = os.environ.get('EMAIL_ENABLED')
    os.environ['EMAIL_ENABLED'] = 'true'
    try:
        asyncio.new_event_loop().run_until_complete(
            emails.send_purchase_alert(_PEDIDO_AVISO, 'nuevo'))
        asyncio.new_event_loop().run_until_complete(
            emails.send_purchase_alert(dict(_PEDIDO_AVISO, paid=True), 'pagado'))
    finally:
        emails.send_admin_notification = original
        if os_flag is None:
            del os.environ['EMAIL_ENABLED']
        else:
            os.environ['EMAIL_ENABLED'] = os_flag

    assert vistos[0].startswith('Nuevo pedido EX-20260730-9999'), vistos
    assert vistos[1].startswith('PAGADO: pedido EX-20260730-9999'), vistos
    for s in vistos:
        assert 'CON PIEZAS SOBRE PEDIDO' in s, s


def test_el_checkout_y_el_webhook_disparan_el_aviso_sin_poder_tumbarlos():
    """En segundo plano y en los dos momentos: al entrar el pedido y al confirmarse el
    pago. Un aviso que puede tumbar un checkout no es un aviso, es un riesgo."""
    src = _fuente()
    ini = src.index('async def create_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]
    # `_avisar_de_la_compra` es `send_purchase_alert` + A QUIÉN COMPRARLE pegado a los
    # renglones sobre pedido (Christián, 2026-07-30). Sigue siendo en segundo plano.
    assert "asyncio.create_task(_avisar_de_la_compra(" in cuerpo, (
        'el checkout no avisa, o avisa esperando al proveedor de correo')
    ini = src.index('async def _confirm_paid_order(')
    cuerpo = src[ini:src.index('\nasync def', ini + 10)]
    assert "_avisar_de_la_compra(fresh, 'pagado')" in cuerpo, (
        'no avisa cuando entra el dinero: se prepara mercancía que nadie pagó')


# ---------- Quien usa el código ES cliente del distribuidor, tenga cuenta o no ----------
#
# EL CASO QUE LO DESTAPÓ (2026-07-30): el pedido EX-20260730-2906 de Aidee Liliana García
# —invitada, sin cuenta— traía el `referred_by` de María y su comisión de $780 bien
# registrada. Pero "Mis Clientes" se armaba SOLO con `users.referred_by`, así que María
# nunca la vio: el dinero se contó y la persona no. Un distribuidor que no ve a quién le
# vendió no puede volver a venderle.

_MARIA = '37f6feba-0000-4000-8000-000000000001'

_PEDIDO_AIDEE = {
    'id': 'o-aidee', 'order_number': 'EX-20260730-2906', 'user_id': None,
    'referred_by': _MARIA, 'status': 'entregado', 'paid': True, 'total': 2830.0,
    'created_at': '2026-07-30T10:00:00',
    'customer': {'full_name': 'Aidee Liliana García', 'email': 'lilygarciahdz@hotmail.com',
                 'phone': '8112345678'},
    'commissions': [{'distributor_id': _MARIA, 'role': 'seller', 'amount': 780.0}],
}


def test_un_comprador_INVITADO_con_el_codigo_aparece_como_cliente():
    """El caso de Aidee, exacto. Sin esto el distribuidor cobra la comisión de alguien a
    quien no puede volver a contactar."""
    import server as _srv
    invitados = _srv._compradores_invitados([_PEDIDO_AIDEE], correos_con_cuenta=set())
    assert len(invitados) == 1, invitados
    g = invitados[0]
    assert g['guest'] is True
    assert g['name'] == 'Aidee Liliana García'
    assert g['email'] == 'lilygarciahdz@hotmail.com'
    assert g['phone'] == '8112345678'
    assert [o['order_number'] for o in g['orders']] == ['EX-20260730-2906']


def test_el_invitado_que_YA_tiene_cuenta_no_sale_dos_veces():
    """Si no se descarta por correo, la misma persona aparece como cliente Y como
    invitada, y sus compras se cuentan dos veces en la lista."""
    import server as _srv
    invitados = _srv._compradores_invitados(
        [_PEDIDO_AIDEE], correos_con_cuenta={'lilygarciahdz@hotmail.com'})
    assert invitados == []


def test_los_pedidos_de_un_invitado_se_juntan_por_correo():
    """Dos compras del mismo invitado son UN cliente, no dos."""
    import server as _srv
    segundo = dict(_PEDIDO_AIDEE, id='o-2', order_number='EX-2', total=1000.0,
                   created_at='2026-07-31T10:00:00')
    invitados = _srv._compradores_invitados([_PEDIDO_AIDEE, segundo], set())
    assert len(invitados) == 1 and len(invitados[0]['orders']) == 2


def test_un_pedido_con_cuenta_no_se_cuenta_como_invitado():
    import server as _srv
    con_cuenta = dict(_PEDIDO_AIDEE, user_id='u-1')
    assert _srv._compradores_invitados([con_cuenta], set()) == []


def _hereda(users_doc, pedidos):
    """Corre la herencia de referido contra un `db.users` de mentiras."""
    import asyncio
    import server as _srv

    updates = []

    class _Users:
        async def update_one(self, query, cambio):
            updates.append((query, cambio))
            return type('R', (), {'matched_count': 1, 'modified_count': 1})()

    original = _srv.db
    _srv.db = type('DB', (), {'users': _Users()})()
    try:
        asyncio.new_event_loop().run_until_complete(
            _srv._heredar_referido_de_pedidos(users_doc, pedidos))
    finally:
        _srv.db = original
    return updates


def test_al_registrarse_el_invitado_HEREDA_el_distribuidor_de_sus_pedidos():
    """Si algún día Aidee se registra, queda ligada a María sola. Antes la cuenta nueva
    nacía huérfana y la relación se perdía justo cuando la persona por fin se registraba."""
    updates = _hereda({'id': 'u-aidee'}, [_PEDIDO_AIDEE])
    assert len(updates) == 1, updates
    query, cambio = updates[0]
    assert cambio['$set']['referred_by'] == _MARIA
    assert 'referred_from_guest_at' in cambio['$set']
    # y no puede pisar a alguien que ya tenía referido: la condición viaja en la consulta
    assert '$or' in query, query


def test_con_VARIOS_pedidos_hereda_el_del_MAS_RECIENTE():
    otro = dict(_PEDIDO_AIDEE, id='o-viejo', referred_by='otro-dist',
                created_at='2026-07-01T10:00:00')
    updates = _hereda({'id': 'u-aidee'}, [otro, _PEDIDO_AIDEE])
    assert updates[0][1]['$set']['referred_by'] == _MARIA


def test_una_cuenta_que_YA_trae_referido_no_se_le_cambia():
    """Si se registró con el código de otro, esa decisión es del cliente y manda sobre
    el historial."""
    assert _hereda({'id': 'u-aidee', 'referred_by': 'otro-dist'}, [_PEDIDO_AIDEE]) == []


def test_sin_pedidos_con_codigo_no_se_inventa_un_referido():
    sin_codigo = dict(_PEDIDO_AIDEE, referred_by=None)
    assert _hereda({'id': 'u-aidee'}, [sin_codigo]) == []


def test_la_adopcion_de_pedidos_dispara_la_herencia_del_referido():
    """Candado sobre el código: si alguien quita la llamada, la relación se vuelve a
    perder en silencio y solo se nota semanas después."""
    src = _fuente()
    ini = src.index('async def _adoptar_pedidos_de_invitado(')
    cuerpo = src[ini:src.index('\nasync def _heredar_referido', ini)]
    assert '_heredar_referido_de_pedidos(user, huerfanos)' in cuerpo, (
        'adoptar los pedidos ya no hereda el distribuidor: la cuenta nueva nace huérfana')


@pytest.mark.parametrize('ruta,funcion', [
    ('/distributor/clients', 'distributor_clients'),
    ('ficha de admin', 'admin_distributor_detail'),
])
def test_las_dos_listas_de_clientes_incluyen_a_los_invitados(ruta, funcion):
    """El admin y el distribuidor tienen que ver la MISMA realidad. Si una lista los
    incluye y la otra no, cada quien cuenta clientes distintos."""
    src = _fuente()
    try:
        ini = src.index(f'async def {funcion}(')
    except ValueError:
        ini = src.index("@api_router.get('/admin/distributors/{dist_id}')")
    cuerpo = src[ini:ini + 6000]
    assert '_compradores_invitados(' in cuerpo, f'{ruta} no lista a los invitados'


# ---------- Los pedidos de prueba NO le avisan a Christián ----------
#
# El aviso interno salió el mismo día que la suite E2E y Christián recibió el correo de un
# pedido que nadie compró: se puso a prepararlo. Un aviso que se equivoca es peor que no
# tenerlo — la próxima vez que llegue uno de verdad ya no se le va a creer.

@pytest.mark.parametrize('customer,esPrueba', [
    ({'full_name': 'E2E Tarjeta', 'email': 'e2e-no-responder@example.com',
      'notes': 'E2E TARJETA — se borra sola'}, True),
    ({'full_name': 'E2E Cripto', 'email': 'e2e-no-responder@example.com',
      'notes': 'E2E CRIPTO — se borra sola'}, True),
    ({'full_name': 'Auditoría E2E', 'email': 'e2e-no-responder@example.com'}, True),
    # el correo solo, aunque el nombre no diga nada
    ({'full_name': 'Quien sea', 'email': 'E2E-No-Responder@Example.com'}, True),
    # el marcador en las notas, aunque el correo sea otro
    ({'full_name': 'Quien sea', 'email': 'real@gmail.com', 'notes': 'E2E de humo'}, True),
    # UN CLIENTE DE VERDAD SÍ AVISA
    ({'full_name': 'Aidee Liliana García', 'email': 'lilygarciahdz@hotmail.com'}, False),
    ({'full_name': 'Christián Cuéllar', 'email': 'exygenlabs@gmail.com'}, False),
])
def test_solo_los_pedidos_de_verdad_disparan_el_aviso(customer, esPrueba):
    import emails
    assert emails.es_pedido_de_prueba({'customer': customer}) is esPrueba


def test_el_pedido_E2E_no_manda_correo_y_el_real_si():
    """La compuerta está DENTRO del envío, no en quien llama: el flujo E2E sigue igual en
    todo lo demás y no hay que acordarse de saltarse el aviso en cada sitio."""
    import asyncio
    import emails
    enviados = []

    async def falso(subject, html_body):
        enviados.append(subject)

    original, emails.send_admin_notification = emails.send_admin_notification, falso
    flag = os.environ.get('EMAIL_ENABLED')
    os.environ['EMAIL_ENABLED'] = 'true'
    try:
        base = dict(_PEDIDO_AVISO)
        e2e = dict(base, order_number='EX-E2E',
                   customer={'full_name': 'E2E Tarjeta',
                             'email': 'e2e-no-responder@example.com',
                             'notes': 'E2E TARJETA — se borra sola'})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(emails.send_purchase_alert(e2e, 'nuevo'))
        loop.run_until_complete(emails.send_purchase_alert(e2e, 'pagado'))
        loop.run_until_complete(emails.send_purchase_alert(base, 'nuevo'))
    finally:
        emails.send_admin_notification = original
        if flag is None:
            del os.environ['EMAIL_ENABLED']
        else:
            os.environ['EMAIL_ENABLED'] = flag

    assert len(enviados) == 1, f'el pedido de prueba mandó correo: {enviados}'
    assert enviados[0].startswith('Nuevo pedido EX-20260730-9999'), enviados


# ---------- La ficha de UN pedido: el candado vive en el servidor ----------

_PEDIDO_DETALLE = {
    'order_number': 'EX-20260730-2906', 'created_at': '2026-07-30T10:00:00',
    'status': 'entregado', 'referred_by': _MARIA, 'paid': True,
    'paid_at': '2026-07-30T11:00:00', 'payment_method': 'spei',
    'customer': {'full_name': 'Aidee Liliana García'},
    'items': [{'name': 'Semaglutida', 'presentation': '2 mg', 'quantity': 2, 'price': 1079.0}],
    'subtotal': 2158.0, 'discount': 300.0, 'discount_rate': 0.14,
    'distributor_code': 'MARIA10', 'points_used': 0, 'points_earned': 60,
    'shipping': 0, 'shipping_absorbed': 250.0, 'total': 2830.0,
    'commissions': [{'distributor_id': _MARIA, 'role': 'seller', 'amount': 780.0}],
    'carrier': 'Estafeta', 'tracking_number': '123', 'backorder': False,
}


def test_la_ficha_del_pedido_responde_que_compro_y_que_paso_con_su_dinero():
    import server as _srv
    d = _srv._detalle_de_pedido(_PEDIDO_DETALLE, _MARIA)
    assert d['items'][0] == {'name': 'Semaglutida', 'presentation': '2 mg', 'quantity': 2,
                             'unit_price': 1079.0, 'line_total': 2158.0}
    assert d['discount'] == 300.0 and d['discount_code'] == 'MARIA10'
    assert d['paid'] is True and d['paid_at'] == '2026-07-30T11:00:00'
    assert d['my_commission'] == 780.0
    assert d['points_earned'] == 60
    # Envío gratis NO quiere decir que no costó: se dice lo que la casa absorbió.
    assert d['shipping_free'] is True and d['shipping_absorbed'] == 250.0
    assert d['carrier'] == 'Estafeta' and d['tracking_number'] == '123'


def test_la_ficha_del_pedido_NUNCA_lleva_datos_de_pago():
    """No se guardan (la tarjeta se teclea en Mercado Pago), así que no hay nada que
    filtrar. La prueba existe para que a nadie se le ocurra añadirlos."""
    import server as _srv
    d = _srv._detalle_de_pedido(dict(_PEDIDO_DETALLE, card_preference_id='mp-123'), _MARIA)
    crudo = json.dumps(d).lower() if 'json' in dir() else str(d).lower()
    for prohibido in ('card', 'tarjeta', 'cvv', 'preference'):
        assert prohibido not in crudo, f'se coló {prohibido!r} en la ficha del pedido'


def test_el_candado_del_pedido_ajeno_vive_en_el_SERVIDOR():
    """Una ficha que solo se esconde en el navegador se abre tecleando el número de
    pedido de otro en la barra de direcciones, y ahí va el cliente ajeno completo."""
    src = _fuente()
    ini = src.index('async def distributor_order_detail(')
    cuerpo = src[ini:src.index('\n@api_router', ini)]
    assert "o.get('referred_by') != dist['id']" in cuerpo, (
        'no compara contra el referred_by: cualquier distribuidor ve cualquier pedido')
    assert 'status_code=403' in cuerpo, 'no rechaza el pedido ajeno'
    assert 'status_code=404' in cuerpo, 'un pedido que no existe debería dar 404'


def test_el_admin_ve_cualquier_pedido_pero_por_su_propia_ruta():
    """Dos rutas distintas a propósito: la del distribuidor filtra, la del admin no. Con
    una sola y un `if es_admin` dentro, el día que alguien mueva el if se abre todo."""
    src = _fuente()
    ini = src.index('async def admin_order_detail(')
    cuerpo = src[ini:src.index('\n\n# ----------------- Protocolos', ini)]
    assert 'get_current_admin' in cuerpo
    assert "referred_by' != " not in cuerpo
