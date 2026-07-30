"""A QUIÉN COMPRARLE y LA CAMPANITA DE LAS VENTAS — Christián, 2026-07-30.

Dos encargos del mismo día, los dos con la misma idea detrás: «quiero poder entrar a mi
Admin Panel y ahí tener todo lo que necesito».

  1. Cuando un pedido trae piezas sin existencia, el aviso dice A QUIÉN comprarle —
     nombre, teléfono y costo por vial del proveedor más barato— y lo dice claro cuando
     NO hay proveedor registrado, en vez de callarse.
  2. Cada venta suena la campanita del admin y la del distribuidor, en su idioma.
"""
import inspect
import os

# database.py exige MONGO_URL al importar; el cliente de motor es perezoso, así que
# nunca se conecta a nada. Mismo apaño que en test_core.py.
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')

import avisos_de_venta as A       # noqa: E402
import emails                     # noqa: E402
import server                     # noqa: E402


# ------------------------------------------------------ a quién comprarle
MAPA = {
    'reta120': {'proveedor': 'li la', 'telefono': '', 'costo_vial_usd': 40.0,
                'whatsapp': '', 'verificado': False},
    'RETATRUTIDA-20MG': {'proveedor': 'Lily', 'telefono': '+85254335867',
                         'costo_vial_usd': 13.6, 'whatsapp': 'https://wa.me/85254335867',
                         'verificado': True},
    'sinprov': {'proveedor': None, 'telefono': '', 'costo_vial_usd': None},
}


def test_al_renglon_sobre_pedido_se_le_pega_su_proveedor():
    fuera = server._con_proveedor(
        [{'product_id': 'RETATRUTIDA-20MG', 'name': 'Retatrutida 20 mg',
          'pedidas': 5, 'en_mano': 2, 'por_surtir': 3}], MAPA)
    assert fuera[0]['proveedor'] == 'Lily'
    assert fuera[0]['telefono'] == '+85254335867'
    assert fuera[0]['costo_vial_usd'] == 13.6
    assert fuera[0]['sin_proveedor'] is False
    # Y no se pierde nada de lo que ya traía.
    assert fuera[0]['por_surtir'] == 3 and fuera[0]['en_mano'] == 2


def test_un_producto_sin_proveedor_se_marca_no_se_calla():
    """El hueco se anuncia. Callarlo deja a alguien buscando con el pedido ya vendido."""
    fuera = server._con_proveedor([{'product_id': 'sinprov', 'name': 'HGH 40 IU',
                                    'por_surtir': 2}], MAPA)
    assert fuera[0]['sin_proveedor'] is True and fuera[0]['proveedor'] is None


def test_un_producto_que_no_esta_en_la_lista_tambien_se_marca():
    fuera = server._con_proveedor([{'product_id': 'no-existe', 'name': 'X',
                                    'por_surtir': 1}], MAPA)
    assert fuera[0]['sin_proveedor'] is True


def test_el_proveedor_se_encuentra_por_id_o_por_sku():
    """El carrito manda a veces el UUID y a veces el SKU: los dos tienen que pegar."""
    fuera = server._con_proveedor(
        [{'product_id': 'otro-uuid', 'sku': 'RETATRUTIDA-20MG', 'por_surtir': 1}], MAPA)
    assert fuera[0]['proveedor'] == 'Lily'


def test_sin_lista_subida_no_se_inventa_un_proveedor():
    fuera = server._con_proveedor([{'product_id': 'x', 'por_surtir': 1}], {})
    assert fuera[0]['sin_proveedor'] is True


def test_el_correo_interno_dice_a_quien_comprarle():
    orden = {
        'order_number': 'EX-TEST-1', 'total': 5000, 'customer': {},
        'items': [{'name': 'Retatrutida 120 mg', 'quantity': 3}],
        'backorder_items': server._con_proveedor(
            [{'product_id': 'RETATRUTIDA-20MG', 'name': 'Retatrutida 20 mg',
              'pedidas': 5, 'en_mano': 2, 'por_surtir': 3}], MAPA),
    }
    cuerpo = emails._aviso_compra_html(orden, 'https://exygenlabs.com/admin')
    assert 'HAY QUE MANDAR PEDIR' in cuerpo
    assert 'COMPRAR A: Lily' in cuerpo
    assert '+85254335867' in cuerpo
    assert '13.60 USD/vial' in cuerpo


def test_el_correo_interno_avisa_cuando_no_hay_proveedor():
    orden = {
        'order_number': 'EX-TEST-2', 'total': 1000, 'customer': {}, 'items': [],
        'backorder_items': server._con_proveedor(
            [{'product_id': 'sinprov', 'name': 'HGH 40 IU', 'por_surtir': 2}], MAPA),
    }
    cuerpo = emails._aviso_compra_html(orden, 'https://exygenlabs.com/admin')
    assert 'Sin proveedor registrado' in cuerpo


def test_el_correo_interno_avisa_cuando_el_proveedor_no_tiene_telefono():
    """li la es el más barato de la Retatrutida 120 mg y NO tiene teléfono en la base.
    El correo lo tiene que decir, no dejar un hueco donde iba el número."""
    orden = {
        'order_number': 'EX-TEST-3', 'total': 1000, 'customer': {}, 'items': [],
        'backorder_items': server._con_proveedor(
            [{'product_id': 'reta120', 'name': 'Retatrutida 120 mg', 'por_surtir': 3}], MAPA),
    }
    cuerpo = emails._aviso_compra_html(orden, 'https://exygenlabs.com/admin')
    assert 'COMPRAR A: li la' in cuerpo
    assert 'sin teléfono' in cuerpo


def test_los_proveedores_solo_salen_con_sesion_de_admin():
    """⛔ Nombres, teléfonos y costos NUNCA en abierto. Las dos rutas exigen admin."""
    rutas = {r.path: r for r in server.app.routes if getattr(r, 'path', '').endswith('/admin/proveedores')}
    assert rutas, 'no existe la ruta de proveedores'
    for ruta in rutas.values():
        deps = str(inspect.signature(ruta.endpoint))
        assert 'get_current_admin' in deps, f'{ruta.path} no exige sesión de admin'


def test_la_ficha_del_distribuidor_no_lleva_proveedores():
    """El detalle del distribuidor sale del MISMO armador, pero sin resolver
    proveedores: a él no le toca saber a quién le compramos ni a cuánto."""
    cuerpo = inspect.getsource(server.distributor_order_detail)
    assert '_pedido_con_proveedores' not in cuerpo
    assert '_pedido_con_proveedores' in inspect.getsource(server.admin_order_detail)


def test_el_panel_cuenta_los_pedidos_por_surtir():
    cuerpo = inspect.getsource(server.admin_stats)
    assert "'pedidos_por_surtir'" in cuerpo and "'piezas_por_pedir'" in cuerpo
    # Un cancelado no espera mercancía: no se cuenta.
    assert "!= 'cancelado'" in cuerpo


# ------------------------------------------------------------- la campanita
ORDEN = {'order_number': 'EX-20260730-2906', 'total': 2830, 'paid': True}


def test_el_admin_ve_el_pedido_el_monto_y_si_ya_entro_el_dinero():
    titulo, cuerpo = A.aviso_para_el_admin(ORDEN, 'Maria Neunfeld')
    assert titulo == 'Entró Un Pedido'
    assert 'EX-20260730-2906' in cuerpo
    assert '$2,830' in cuerpo
    assert 'Pagado' in cuerpo
    assert 'Maria Neunfeld' in cuerpo


def test_el_admin_ve_cuando_el_pedido_todavia_no_se_cobra():
    _, cuerpo = A.aviso_para_el_admin({**ORDEN, 'paid': False}, '')
    assert 'Por Cobrar' in cuerpo and 'Pagado' not in cuerpo


def test_el_admin_ve_si_hay_que_mandar_pedir():
    _, cuerpo = A.aviso_para_el_admin(
        {**ORDEN, 'backorder_items': [{'por_surtir': 3}]}, '')
    assert 'Con Piezas Por Mandar Pedir' in cuerpo


def test_el_distribuidor_ve_su_venta_y_su_comision():
    titulo, cuerpo = A.aviso_para_el_vendedor(ORDEN, 780)
    assert titulo == '¡Venta Con Tu Código!'
    assert 'EX-20260730-2906' in cuerpo and '$780' in cuerpo


def test_una_venta_del_equipo_se_dice_distinto():
    titulo, cuerpo = A.aviso_para_el_vendedor(ORDEN, 120, es_equipo=True)
    assert titulo == 'Venta De Tu Equipo' and '$120' in cuerpo


def test_cada_quien_en_su_idioma():
    """María abre la cuenta en pt-BR: su campanita le habla en portugués."""
    assert A.aviso_para_el_vendedor(ORDEN, 780, lang='pt-BR')[0] == 'Venda Com Seu Código!'
    assert A.aviso_para_el_vendedor(ORDEN, 780, lang='en-US')[0] == 'A Sale With Your Code!'
    assert A.aviso_para_el_admin(ORDEN, '', 'pt-BR')[0] == 'Entrou Um Pedido'
    assert A.aviso_para_el_admin(ORDEN, '', 'en')[0] == 'An Order Came In'


def test_un_idioma_que_no_conocemos_cae_al_espanol():
    for raro in ('', None, 'zz', 'fr-CA'):
        assert A.idioma(raro) == 'es'
    assert A.aviso_para_el_admin(ORDEN, '', 'fr')[0] == 'Entró Un Pedido'


def test_los_tres_idiomas_estan_completos():
    """Ni una clave suelta: un texto que falta sale como un KeyError en plena venta."""
    for tabla in (A._ADMIN, A._VENDEDOR):
        claves = {k: set(v) for k, v in tabla.items()}
        assert set(claves) == set(A.IDIOMAS)
        assert len(set(map(frozenset, claves.values()))) == 1


def test_la_venta_avisa_al_admin_y_a_quien_gano_comision():
    cuerpo = inspect.getsource(server.avisar_de_la_venta)
    assert "'role': 'admin'" in cuerpo, 'el admin no se entera de la venta'
    assert 'aviso_para_el_vendedor' in cuerpo
    # Idempotente: el mismo pedido no llena la campanita de repetidos.
    assert "dedup=f'venta:{numero}'" in cuerpo


def test_el_checkout_suena_la_campanita():
    src = inspect.getsource(server)
    cuerpo = src.split('async def create_order(')[1].split('\n@api_router')[0]
    assert 'avisar_de_la_venta(' in cuerpo


def test_cada_quien_ve_solo_sus_notificaciones():
    """Las personales van filtradas por `user_id`: nadie ve la comisión de otro."""
    cuerpo = inspect.getsource(server.my_notifications)
    assert "'kind': 'personal', 'user_id': user['id']" in cuerpo
    # Y el globito cuenta las posteriores a la última vez que abrió la campanita.
    assert "d.get('created_at', '') > seen_at" in cuerpo


def test_el_barrido_retroactivo_no_toca_cancelados_ni_archivados():
    cuerpo = inspect.getsource(server.avisos_de_ventas_atrasados)
    assert "'status': {'$ne': 'cancelado'}" in cuerpo
    assert "'archived': {'$ne': True}" in cuerpo
    assert '$setOnInsert' in cuerpo, 'el barrido se puede correr dos veces'
