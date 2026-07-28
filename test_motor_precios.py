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

def test_el_inventario_se_valida_SUMANDO_los_renglones_del_mismo_producto():
    """Pedir 40 dos veces son 80, no 40 y 40.

    El carrito puede mandar el MISMO producto en varios renglones. Revisando renglón por
    renglón cada uno pasa por su cuenta y el pedido se lleva el doble —o el triple— de lo
    que hay: el inventario queda en negativo y la pérdida no tiene tope, porque siempre se
    puede añadir otro renglón. Lo encontró el barrido adversarial del 28-jul, y es el
    MISMO hueco que se creyó cerrado el 25-jul: entonces se tapó el "pediste 99,999", no
    el "pediste 40 dos veces"."""
    import server as _srv
    pflags = _catalogo(stock=40)
    agrupado = _srv._agrupar_por_producto(
        [_Item('OREXINA-10MG', 'Orexin A 10 mg', 40),
         _Item('OREXINA-10MG', 'Orexin A 10 mg', 40)], pflags)
    assert len(agrupado) == 1 and list(agrupado.values())[0]['total'] == 80, agrupado
    # y el checkout compara contra ESE total agrupado, no renglón por renglón
    src = _fuente()
    m = re.search(r"faltantes = \[\].*?status_code=409", src, re.S)
    assert m, 'no encontré la validación de inventario del checkout'
    assert '_agrupar_por_producto(' in m.group(0), (
        'el checkout volvió a validar renglón por renglón: dos renglones del mismo '
        'producto se llevan el doble del inventario')


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
    _srv.db = type('DB', (), {'products': productos})()
    try:
        return asyncio.new_event_loop().run_until_complete(corutina(_srv))
    finally:
        _srv.db = original


def test_dos_pedidos_a_la_vez_no_se_llevan_la_misma_ultima_pieza():
    """La carrera entre revisar y descontar.

    El stock se miraba arriba y se restaba mucho después, y entre las dos cosas cabía
    otro pedido entero: dos clientes veían la última pieza, los dos pasaban la revisión y
    el inventario terminaba en −1. No hace falta mala fe, basta con dos personas
    comprando a la vez — que es justo lo que pasa cuando un anuncio pega."""
    doc = {'id': 'uuid-1', 'sku': 'SKU-1', 'name': 'Orexin A', 'stock': 1}

    async def dos_pedidos(_srv):
        pedido = {'uuid-1': {'total': 1, 'nombre': 'Orexin A', 'doc': doc}}
        primero = await _srv._reservar_inventario(pedido)
        segundo = await _srv._reservar_inventario(pedido)
        return primero, segundo

    (res1, ago1), (res2, ago2) = _con_db_falsa([doc], dos_pedidos)
    assert not ago1 and res1 == [('uuid-1', 1)], 'el primero debería llevarse la pieza'
    assert ago2 == ['Orexin A'], 'el segundo se llevó una pieza que ya no existía'
    assert doc['stock'] == 0, f'el inventario quedó en {doc["stock"]}'


def test_si_un_renglon_no_alcanza_se_devuelve_lo_ya_apartado():
    """Un pedido que no salió no puede dejar piezas secuestradas: quedarían invisibles
    para todos los demás clientes hasta que alguien contara a mano."""
    a = {'id': 'uuid-A', 'sku': 'A', 'name': 'Sí hay', 'stock': 10}
    b = {'id': 'uuid-B', 'sku': 'B', 'name': 'No hay', 'stock': 1}

    async def pedido_imposible(_srv):
        return await _srv._reservar_inventario({
            'uuid-A': {'total': 5, 'nombre': 'Sí hay', 'doc': a},
            'uuid-B': {'total': 4, 'nombre': 'No hay', 'doc': b},
        })

    reservado, agotados = _con_db_falsa([a, b], pedido_imposible)
    assert agotados == ['No hay']
    assert reservado == [], 'dice que apartó algo de un pedido que no salió'
    assert a['stock'] == 10, f'se quedó con 5 piezas apartadas: {a["stock"]}'
    assert b['stock'] == 1


def test_la_reserva_encuentra_el_producto_tanto_por_id_como_por_sku():
    """La llave con la que se agrupa es el `id`, pero el catálogo de respaldo guarda los
    productos por SKU. Si la reserva sólo buscara por una, no restaría nada y devolvería
    "no hay" en un producto que sí hay."""
    doc = {'sku': 'SOLO-SKU', 'name': 'Sin UUID', 'stock': 3}

    async def reservar(_srv):
        return await _srv._reservar_inventario(
            {'SOLO-SKU': {'total': 2, 'nombre': 'Sin UUID', 'doc': doc}})

    reservado, agotados = _con_db_falsa([doc], reservar)
    assert not agotados and reservado == [('SOLO-SKU', 2)]
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
