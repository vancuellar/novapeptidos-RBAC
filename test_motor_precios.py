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
    src = _fuente()
    m = re.search(r"faltantes = \[\].*?status_code=409", src, re.S)
    assert m, 'no encontré la validación de inventario del checkout'
    cuerpo = m.group(0)
    assert 'pedido_por_producto' in cuerpo, (
        'el checkout sigue validando renglón por renglón: dos renglones del mismo '
        'producto se llevan el doble del inventario')
    assert "+= int(it.quantity)" in cuerpo, 'no está sumando las cantidades'


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
