"""EL RASTREO EN NUESTRA PÁGINA — probado con dientes.

⛔ ORDEN DE CHRISTIÁN (2026-07-31): «quiero que el cliente rastree su pedido DENTRO de
exygenlabs.com; quiero que vivan en nuestra página el mayor tiempo posible».

El iframe que él propuso NO se puede: FedEx y Estafeta mandan `x-frame-options:
SAMEORIGIN` y `content-security-policy: frame-ancestors 'self'`, o sea que el marco sale
EN BLANCO dentro de nuestro sitio. Eso no se prueba aquí porque es un hecho del servidor
de ellos, no de nuestro código; queda comprobado con `curl -I` y escrito en `rastreo.py`.
Lo que sí se prueba es la salida honesta: pedir los eventos a la API y pintarlos nosotros.

LO QUE SE PRUEBA AQUÍ, y por qué cada cosa:

  1. ⛔ LA PRIVACIDAD, QUE YA COSTÓ UNA VEZ. Esta ruta es PÚBLICA (el invitado no tiene
     cuenta) y el pedido trae adentro el distribuidor que lo refirió, su comisión y lo
     que la guía le costó a la casa. Se lee el SOBRE COMPLETO como texto plano y truena
     si algo de eso se asoma. Tosco a propósito: así no depende de que nadie se acuerde
     de actualizar una lista el día que agregue un campo nuevo.

  2. ⛔ LA CUOTA DEL DESPACHO. El tope es de 2 peticiones por segundo POR CUENTA y lo
     comparten las compras de guía, que son las que sí cuestan dinero. Un cliente
     recargando la pestaña NO puede gastárselo: se prueba que mil consultas del mismo
     pedido son UNA sola llamada a la paquetería.

  3. ⛔ QUE LA BARRA NO RETROCEDA. Los carriers mandan eventos administrativos tardíos
     después de la entrega. Si la barra se calculara con el ÚLTIMO evento, el cliente
     vería su paquete «des-entregarse» y escribiría a preguntar qué pasó.

  4. ⛔ QUE «EN SUCURSAL» NO SE PINTE COMO «ENTREGADO». `delivered_to_branch` es un
     paquete esperando a que lo recojan. Decirle entregado hace que alguien deje de
     buscar algo que sí tiene que ir por él.

  5. ⛔ QUE LA PAQUETERÍA CAÍDA NO TUMBE LA PÁGINA. El cliente ya pagó: tiene derecho a
     ver su pedido aunque FedEx tenga un mal día. Nunca 5xx por culpa de ellos.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import rastreo
import server


# El pedido REAL de Brenda (2026-07-30): guía de FedEx comprada con Envíos
# Internacionales. Con los campos internos PEGADOS, que es como sale de la base — si el
# cajón público los dejara pasar, estas pruebas lo cachan.
BRENDA = {
    'id': 'fbded9c5-e0dc-4840-bf87-8e1a0cfaaa75',
    'order_number': 'EX-20260730-5930',
    'status': 'enviado',
    'carrier': 'FedEx',
    'tracking_number': '875164874865',
    'tracking_url': 'https://www.fedex.com/fedextrack/?trknbr=875164874865',
    'shipped_at': '2026-07-31T17:26:45',
    'eta': '2026-08-03',
    # ⛔ Todo lo de abajo es de la casa y NO puede salir en la respuesta.
    'label_provider': 'enviosinternacionales',
    'label_url': 'https://app.enviosinternacionales.com/s/s?id=firmada',
    'shipping_cost': 192.9,
    'shipping_absorbed': 192.9,
    'referred_by': 'u-maria',
    'commission': 723.15,
    'customer': {'full_name': 'Brenda Iliana Oseguera Gonzalez'},
}

# Un pedido pagado al que todavía no se le compra guía. No es un error: es el rato entre
# que entra el dinero y que sale el paquete.
SIN_GUIA = {'id': 'o-sin', 'order_number': 'EX-20260731-0001', 'status': 'pagado',
            'carrier': '', 'tracking_number': '', 'label_provider': ''}


def _evento(estado, descripcion, lugar, fecha):
    return {'estado': estado, 'descripcion': descripcion, 'lugar': lugar, 'fecha': fecha}


# Lo que devuelve la API de la paquetería para una guía en camino.
EVENTOS = [
    _evento('created', 'Guía generada', 'Querétaro', '2026-07-31T17:26:00'),
    _evento('picked_up', 'Recolectado', 'Querétaro', '2026-07-31T20:10:00'),
    _evento('in_transit', 'En tránsito', 'Ciudad de México', '2026-08-01T04:30:00'),
]


# --------------------------------------------------------- base de datos falsa
class _Coll:
    def __init__(self, docs=()):
        self._docs = list(docs)

    async def find_one(self, filtro=None, *a, **k):
        for d in self._docs:
            if all(d.get(k2) == v for k2, v in (filtro or {}).items()):
                return dict(d)
        return None


class _FakeDB:
    def __init__(self):
        import copy
        self.orders = _Coll(copy.deepcopy([BRENDA, SIN_GUIA]))

    def __getattr__(self, nombre):
        return _Coll()


class _Paqueteria:
    """Un proveedor de mentiras que anota CUÁNTAS veces le preguntaron. Ese contador es
    justo lo que prueba que la caché sirve."""

    def __init__(self, eventos=(), encendido=True, revienta=False):
        self.eventos = list(eventos)
        self.encendido = encendido
        self.revienta = revienta
        self.preguntas = []

    def enabled(self):
        return self.encendido

    def rastrear(self, numero, carrier=''):
        self.preguntas.append((numero, carrier))
        if self.revienta:
            raise RuntimeError('la paqueteria no contesta')
        return list(self.eventos)


@pytest.fixture
def mundo(monkeypatch):
    """Deja el mundo en pie y devuelve las piezas para espiarlas."""
    rastreo.limpiar_cache()
    db = _FakeDB()
    monkeypatch.setattr(rastreo, 'db', db)
    proveedores = {}
    monkeypatch.setattr(rastreo.paqueterias, 'modulo', lambda c: proveedores.get(c))

    class Mundo:
        pass

    m = Mundo()
    m.db, m.proveedores = db, proveedores
    m.cliente = TestClient(server.app)
    return m


# =========================================================================
#  1. LA PRIVACIDAD: esta ruta es pública y no puede soltar nada de la casa
# =========================================================================
def test_no_se_asoma_el_distribuidor_ni_lo_que_costo_la_guia(mundo):
    """El sobre COMPLETO, leído como texto plano. Si aparece el distribuidor, su
    comisión, lo que costó la guía o con qué proveedor se compró, truena."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(EVENTOS)
    r = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo')
    assert r.status_code == 200
    sobre = json.dumps(r.json(), ensure_ascii=False)
    for prohibido in ('u-maria', 'referred_by', 'commission', '723.15',
                      'shipping_cost', '192.9', 'shipping_absorbed',
                      'label_provider', 'enviosinternacionales', 'label_url',
                      'app.enviosinternacionales.com'):
        assert prohibido not in sobre, f'se asomó «{prohibido}» en el rastreo público'


def test_solo_salen_los_campos_de_la_lista_blanca(mundo):
    """El cajón se arma campo por campo. El día que alguien agregue un dato interno al
    pedido, NO puede colarse solo: esta prueba fija exactamente qué sale."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(EVENTOS)
    d = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo').json()
    assert set(d) == {'numero', 'paqueteria', 'rastreo', 'url_paqueteria', 'paso',
                      'incidencia', 'entrega_estimada', 'enviado_en', 'entregado_en',
                      'eventos'}


def test_si_sale_lo_que_el_cliente_si_debe_ver(mundo):
    """Lo que el cliente ya sabe por su correo: paquetería, número de guía y eventos."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(EVENTOS)
    d = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo').json()
    assert d['paqueteria'] == 'FedEx'
    assert d['rastreo'] == '875164874865'
    assert d['entrega_estimada'] == '2026-08-03'
    assert [e['descripcion'] for e in d['eventos']] == [
        'Guía generada', 'Recolectado', 'En tránsito']
    assert d['eventos'][2]['lugar'] == 'Ciudad de México'


# =========================================================================
#  2. LA CUOTA: un cliente recargando no puede quedarse con el cupo del despacho
# =========================================================================
def test_mil_recargas_son_una_sola_llamada_a_la_paqueteria(mundo):
    """⛔ El tope es de 2 peticiones por segundo Y LO COMPARTE la compra de guías. Sin
    caché, una pestaña recargando deja sin cupo al despacho, que es lo que sí vende."""
    prov = _Paqueteria(EVENTOS)
    mundo.proveedores['enviosinternacionales'] = prov
    for _ in range(25):
        assert mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo').status_code == 200
    assert len(prov.preguntas) == 1, 'la caché no frenó: se preguntó de más'


def test_tambien_se_guarda_el_caso_sin_eventos(mundo):
    """El caso «todavía no hay nada» es el que MÁS se recarga (el cliente acaba de
    recibir el correo), o sea el que más cuota gastaría. También se cachea."""
    prov = _Paqueteria([])
    mundo.proveedores['enviosinternacionales'] = prov
    for _ in range(10):
        mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo')
    assert len(prov.preguntas) == 1


def test_se_le_pregunta_al_proveedor_que_compro_la_guia(mundo):
    """No depende de la plataforma: la guía de Brenda se compró con Envíos
    Internacionales, así que se le pregunta a ÉSE y no a Skydropx."""
    ei, sky = _Paqueteria(EVENTOS), _Paqueteria(EVENTOS)
    mundo.proveedores['enviosinternacionales'], mundo.proveedores['skydropx'] = ei, sky
    mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo')
    assert ei.preguntas == [('875164874865', 'FedEx')]
    assert sky.preguntas == []


# =========================================================================
#  3. LA BARRA NO RETROCEDE
# =========================================================================
def test_gana_el_paso_mas_avanzado_no_el_ultimo_evento():
    """⛔ Los carriers mandan eventos administrativos DESPUÉS de entregar. Con el
    último evento mandando, la barra retrocedería de «entregado» a «en tránsito» y el
    cliente creería que perdimos su paquete."""
    con_cola = EVENTOS + [
        _evento('delivered', 'Entregado', 'San Juan del Río', '2026-08-03T11:00:00'),
        _evento('in_transit', 'Ajuste administrativo', '', '2026-08-03T23:59:00'),
    ]
    assert rastreo.paso_de(con_cola) == 'entregado'


def test_en_sucursal_no_es_entregado():
    """`delivered_to_branch` es un paquete ESPERANDO a que lo recojan. Pintarlo como
    entregado hace que alguien deje de buscar algo que sí tiene que ir por él."""
    eventos = [_evento('delivered_to_branch', 'En sucursal', 'Querétaro', '2026-08-02T10:00:00')]
    assert rastreo.paso_de(eventos) == 'reparto'


def test_sin_eventos_manda_lo_que_sabe_la_casa():
    """Las primeras horas el carrier no reporta nada. No es un error: se enseña el paso
    que la casa sí conoce, no una pantalla vacía."""
    assert rastreo.paso_de([], 'enviado') == 'transito'
    assert rastreo.paso_de([], 'entregado') == 'entregado'
    assert rastreo.paso_de([], 'pagado') == 'recibido'


def test_una_incidencia_vieja_ya_resuelta_no_alarma():
    """Un intento fallido el martes y entregado el miércoles NO es un problema de hoy."""
    eventos = [
        _evento('delivery_attempt', 'Intento fallido', 'San Juan del Río', '2026-08-02T15:00:00'),
        _evento('delivered', 'Entregado', 'San Juan del Río', '2026-08-03T11:00:00'),
    ]
    assert rastreo.hay_incidencia(eventos) is False


def test_una_incidencia_de_verdad_se_avisa(mundo):
    """Un paquete retenido o devuelto sí se dice: es justo cuando el cliente necesita
    escribirnos, y esconderlo sólo hace que se entere más tarde."""
    devuelto = EVENTOS + [_evento('in_return', 'En devolución', 'CDMX', '2026-08-04T09:00:00')]
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(devuelto)
    d = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo').json()
    assert d['incidencia'] is True


# =========================================================================
#  4. LA PAQUETERÍA CAÍDA NO TUMBA LA PÁGINA
# =========================================================================
def test_si_la_paqueteria_truena_la_pagina_sigue_en_pie(mundo):
    """El cliente ya pagó: tiene derecho a ver su pedido aunque FedEx tenga mal día."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(revienta=True)
    r = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo')
    assert r.status_code == 200
    d = r.json()
    assert d['eventos'] == []
    assert d['rastreo'] == '875164874865'      # lo que sabe la casa sigue saliendo
    assert d['paso'] == 'transito'             # el pedido está 'enviado'


def test_sin_credenciales_tampoco_se_cae(mundo):
    """El día que caduquen las llaves, la ficha del pedido no puede dejar de abrir."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(EVENTOS, encendido=False)
    d = mundo.cliente.get('/api/orders/EX-20260730-5930/rastreo').json()
    assert d['eventos'] == []


def test_pedido_sin_guia_todavia_no_es_un_error(mundo):
    """Entre que entra el dinero y sale el paquete hay un rato. No es 404: el pedido SÍ
    existe, y la pantalla lo pinta como «preparando tu pedido»."""
    r = mundo.cliente.get('/api/orders/EX-20260731-0001/rastreo')
    assert r.status_code == 200
    d = r.json()
    assert d['rastreo'] == '' and d['eventos'] == [] and d['paso'] == 'recibido'


def test_pedido_que_no_existe_es_404(mundo):
    assert mundo.cliente.get('/api/orders/EX-NO-EXISTE/rastreo').status_code == 404


# =========================================================================
#  5. LA TRADUCCIÓN DEL JSON DE LA PAQUETERÍA
# =========================================================================
def test_se_entiende_el_json_de_la_paqueteria_tal_como_lo_manda():
    """Es JSON:API: los campos vienen dentro de `attributes`. Y el orden lo pone la
    fecha, no la API — que no promete ninguno."""
    import skydropx
    crudo = {'data': [
        {'id': 'b', 'type': 'tracking', 'attributes': {
            'event_description': 'En ruta de entrega - Cordoba', 'location': 'CORDOBA',
            'date': '2024-09-05T23:13:00', 'status': 'last_mile'}},
        {'id': 'a', 'type': 'tracking', 'attributes': {
            'event_description': 'Recolectado', 'location': 'QUERETARO',
            'date': '2024-09-04T10:00:00', 'status': 'picked_up'}},
    ]}
    eventos = skydropx._eventos_del_json(crudo)
    assert [e['estado'] for e in eventos] == ['picked_up', 'last_mile']
    assert eventos[1]['lugar'] == 'CORDOBA'
    assert eventos[1]['descripcion'] == 'En ruta de entrega - Cordoba'


def test_un_json_raro_no_revienta():
    """Si la API contesta algo que no esperábamos, se devuelve lo que se pueda y ya."""
    import skydropx
    assert skydropx._eventos_del_json({}) == []
    assert skydropx._eventos_del_json({'data': None}) == []
    assert skydropx._eventos_del_json({'data': ['basura', 42, None]}) == []
