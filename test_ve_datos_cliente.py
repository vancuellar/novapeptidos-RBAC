"""QUIÉN VE LOS DATOS DE CONTACTO DEL CLIENTE. Interruptor por distribuidor.

La historia, porque explica por qué esto es un interruptor y no una regla:

  · 2026-07-23, Christián: un distribuidor NO ve «correo, teléfono, domicilio, ni qué
    compuestos compró su cliente».
  · 2026-07-31, Christián: ábreselo a MARÍA — ella atiende a sus clientes y necesita
    poder llamarles. **Sólo a ella.** Los demás se quedan como estaban.

Por eso las pruebas de aquí no comprueban «se ve» o «no se ve», sino que se vea
EXACTAMENTE para quien está encendido, y que encenderlo no afloje ningún otro candado:

  1. El distribuidor SIN interruptor sigue sin ver contacto (la regla del 23 de julio).
  2. María, CON interruptor, ve el contacto de SUS clientes.
  3. ⛔ El interruptor NO abre los pedidos de OTRO distribuidor: eso sigue siendo 403.
     Encender la visibilidad no puede convertirse en encender el acceso.
  4. El admin ve el contacto siempre, sin depender de ningún interruptor.
  5. El margen de la casa no viaja ni con el interruptor encendido.
"""
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


PEDIDO = {
    'id': 'o-1', 'order_number': 'EX-1', 'referred_by': 'maria',
    'status': 'confirmado', 'created_at': '2026-07-31T00:00:00',
    'total': 4827.0, 'subtotal': 5637.0, 'discount': 810.0,
    'items': [{'name': 'Retatrutida 20 mg', 'quantity': 1, 'price': 2999.0}],
    'carrier': 'Estafeta', 'tracking_number': '7712345678',
    'tracking_url': 'https://estafeta.test/7712345678',
    'customer': {
        'full_name': 'Brenda Iliana Oseguera Gonzalez',
        'email': 'brenda@ejemplo.mx', 'phone': '4425217088',
        'address': 'Prolongacion el Roble 73', 'address_2': 'Int. 24 B',
        'city': 'San Juan del Rio', 'state': 'Queretaro',
        'postal_code': '76807', 'country': 'MX',
        'notes': 'Se recibe en vigilancia',
    },
}

MARIA = {'id': 'maria', 'name': 'Maria', 'role': 'distributor',
         server.CAMPO_VE_CLIENTE: True}
OTRO = {'id': 'otro', 'name': 'Otro', 'role': 'distributor'}

CAMPOS_DE_CONTACTO = ('customer_email', 'customer_phone', 'customer_address',
                      'customer_city', 'customer_postal_code', 'customer_full_name')


# ==========================================================================
#  1 y 2. El interruptor decide, y sólo el interruptor
# ==========================================================================
def test_un_distribuidor_SIN_interruptor_no_ve_contacto():
    """La regla del 2026-07-23 sigue viva para todos menos los encendidos."""
    ficha = server._detalle_de_pedido(PEDIDO, OTRO['id'], dist=OTRO)
    for campo in CAMPOS_DE_CONTACTO:
        assert campo not in ficha, f'{campo} se coló sin interruptor'


def test_maria_CON_interruptor_ve_el_contacto_de_sus_clientes():
    ficha = server._detalle_de_pedido(PEDIDO, MARIA['id'], dist=MARIA)
    assert ficha['customer_email'] == 'brenda@ejemplo.mx'
    assert ficha['customer_phone'] == '4425217088'
    assert ficha['customer_address'] == 'Prolongacion el Roble 73'
    assert ficha['customer_postal_code'] == '76807'
    # y el nombre COMPLETO, no sólo el de pila
    assert ficha['customer_full_name'] == 'Brenda Iliana Oseguera Gonzalez'


def test_el_campo_ausente_no_se_finge_vacio():
    """Cuando no puede verlo, la clave NO viaja. Mandarla en blanco haría que la
    pantalla enseñara un campo vacío y diera a entender que el dato no existe."""
    ficha = server._detalle_de_pedido(PEDIDO, OTRO['id'], dist=OTRO)
    assert 'customer_email' not in ficha
    # `customer_name` (el de la ficha de siempre) sí sigue yendo: nunca fue secreto.
    assert ficha['customer_name'] == 'Brenda Iliana Oseguera Gonzalez'


def test_apagar_el_interruptor_lo_vuelve_a_cerrar():
    """Se puede revertir sin tocar código: es lo que hace que sea un interruptor."""
    maria_apagada = dict(MARIA, **{server.CAMPO_VE_CLIENTE: False})
    ficha = server._detalle_de_pedido(PEDIDO, maria_apagada['id'], dist=maria_apagada)
    for campo in CAMPOS_DE_CONTACTO:
        assert campo not in ficha


def test_ve_datos_del_cliente_lee_el_interruptor():
    assert server.ve_datos_del_cliente(MARIA) is True
    assert server.ve_datos_del_cliente(OTRO) is False
    assert server.ve_datos_del_cliente(None) is False


# ==========================================================================
#  3. ⛔ EL INTERRUPTOR NO ABRE PEDIDOS AJENOS
# ==========================================================================
def test_el_interruptor_NO_da_acceso_a_pedidos_de_otro_distribuidor():
    """⛔ LA PRUEBA QUE MÁS IMPORTA. Ver más de lo tuyo no puede convertirse en ver lo
    de otros. El candado de `referred_by` es lo que decide a qué pedidos se asoma
    siquiera, y vive en el SERVIDOR — no en la pantalla, que se brinca tecleando el
    número de pedido ajeno en la barra de direcciones."""
    from fastapi import HTTPException
    import asyncio

    ajeno = dict(PEDIDO, referred_by='otro-distribuidor')

    class ColFalsa:
        async def find_one(self, filtro, proj=None):
            return dict(ajeno)

    class DBFalsa:
        orders = ColFalsa()

        def __getitem__(self, n):
            return ColFalsa()

    original = server.db
    server.db = DBFalsa()
    try:
        with pytest.raises(HTTPException) as e:
            asyncio.run(server.distributor_order_detail('EX-1', dist=MARIA))
        assert e.value.status_code == 403
    finally:
        server.db = original


# ==========================================================================
#  4. El admin no depende de ningún interruptor
# ==========================================================================
def test_el_admin_ve_el_contacto_siempre():
    ficha = server._detalle_de_pedido(PEDIDO, None, es_admin=True)
    assert ficha['customer_email'] == 'brenda@ejemplo.mx'
    assert ficha['customer_phone'] == '4425217088'


def test_el_admin_no_necesita_que_nadie_le_prenda_nada():
    """Aunque le pasen un distribuidor apagado, `es_admin` manda."""
    ficha = server._detalle_de_pedido(PEDIDO, OTRO['id'], dist=OTRO, es_admin=True)
    assert ficha['customer_email'] == 'brenda@ejemplo.mx'


# ==========================================================================
#  5. El margen de la casa NO viaja, ni con el interruptor encendido
# ==========================================================================
def test_ni_con_el_interruptor_viaja_el_margen_de_la_casa():
    """Abrirle el contacto del cliente a María no le abre lo que gana la casa.
    Se lee la ficha ENTERA como texto: un `grep` es tosco a propósito, porque no
    depende de que alguien se acuerde de actualizar una lista de campos prohibidos."""
    import json
    ficha = server._detalle_de_pedido(PEDIDO, MARIA['id'], dist=MARIA)
    crudo = json.dumps(ficha, ensure_ascii=False).lower()
    for prohibida in ('costo', 'proveedor', 'roi', 'margen', 'utilidad'):
        assert prohibida not in crudo, f'se coló "{prohibida}" en la ficha del distribuidor'


# ==========================================================================
#  6. EL AUTOLLENADO DEL COTIZADOR — el candado va en el SERVIDOR
# ==========================================================================
#  Esconder los campos en la pantalla no esconde nada: la respuesta se lee en la
#  consola del navegador con la sesión abierta. Lo que no se puede ver, NO VIAJA.
USUARIOS = [
    {'id': 'u-1', 'name': 'Brenda Oseguera', 'role': 'client', 'referred_by': 'maria',
     'email': 'brenda@ejemplo.mx', 'phone': '4425217088',
     'address': 'Prolongacion el Roble 73'},
]


class _ColUsuarios:
    def __init__(self, docs):
        self.docs = docs

    def find(self, filtro=None, proj=None):
        filtro = filtro or {}
        docs = [d for d in self.docs
                if all(d.get(k) == v for k, v in filtro.items())]

        class Cur:
            async def to_list(self, n=None):
                return [dict(d) for d in docs]
        return Cur()


class _DBFalsa:
    def __init__(self, usuarios, pedidos):
        self.users = _ColUsuarios(usuarios)
        self.orders = _ColUsuarios(pedidos)

    def __getitem__(self, n):
        return _ColUsuarios([])


def _clientes_para(quien):
    import asyncio
    original = server.db
    server.db = _DBFalsa(USUARIOS, [])
    try:
        return asyncio.run(server.cotizador_clientes(quien=quien))
    finally:
        server.db = original


def test_autollenado_a_maria_le_manda_el_contacto():
    r = _clientes_para(MARIA)
    assert r['puede_ver_contacto'] is True
    cli = r['clientes'][0]
    assert cli['email'] == 'brenda@ejemplo.mx'
    assert cli['phone'] == '4425217088'


def test_autollenado_a_un_distribuidor_sin_interruptor_solo_le_manda_el_nombre():
    """⛔ El candado del encargo. Sin interruptor, el correo/teléfono/domicilio NI
    SIQUIERA VIAJAN: no es que la pantalla no los pinte, es que no están."""
    otro_con_cliente = dict(OTRO)
    USUARIOS[0]['referred_by'] = 'otro'
    try:
        r = _clientes_para(otro_con_cliente)
        assert r['puede_ver_contacto'] is False
        cli = r['clientes'][0]
        assert cli['name'] == 'Brenda Oseguera'
        for campo in ('email', 'phone', 'address'):
            assert campo not in cli, f'{campo} viajó a un distribuidor sin interruptor'
    finally:
        USUARIOS[0]['referred_by'] = 'maria'


def test_autollenado_solo_trae_SUS_clientes():
    """El «sólo sus clientes» no depende del interruptor: se filtra antes que nada."""
    r = _clientes_para(OTRO)          # sus clientes son cero
    assert r['clientes'] == []


def test_autollenado_el_admin_ve_todo():
    admin = {'id': 'a-1', 'name': 'Christian', 'role': 'admin'}
    r = _clientes_para(admin)
    assert r['puede_ver_contacto'] is True
    assert r['clientes'][0]['email'] == 'brenda@ejemplo.mx'


def test_la_guia_y_el_rastreo_si_los_ve():
    """Lo que el distribuidor SIEMPRE necesitó: en qué va el paquete de su cliente."""
    ficha = server._detalle_de_pedido(PEDIDO, MARIA['id'], dist=MARIA)
    assert ficha['tracking_number'] == '7712345678'
    assert ficha['carrier'] == 'Estafeta'
    assert ficha['tracking_url'].endswith('7712345678')
