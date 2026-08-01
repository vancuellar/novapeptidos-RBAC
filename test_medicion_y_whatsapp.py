"""LO QUE NO SE MIDE NO SE PUEDE DECIDIR. Las dos preguntas de la semana.

Christián, 2026-07-31, antes de la semana de publicidad:

  1. «¿la mayoría entra por teléfono?» — el sitio NO guardaba el aparato, así que
     eso sólo se deducía de dónde se compran los anuncios. Y peor: el 8.7% de
     visita→ficha, que es EL número que va a comparar para saber si adelgazar la
     portada móvil sirvió, era un promedio de teléfonos y monitores revueltos.

  2. «¿las 110 conversaciones de WhatsApp se volvieron ventas?» — $237 USD, 110
     conversaciones a $39 MXN, CERO compras atribuidas por Meta, y nadie sabía si
     eran cero de verdad o cero por falta de medición. Sin eso, subir o bajar el
     presupuesto es adivinar.

Lo que se protege aquí es que las dos respuestas sigan siendo honestas. En
particular, las tres formas conocidas de mentir sin querer:

  · meter en 'computadora' las sesiones viejas que no traen aparato (inventar);
  · contar el aparato por EVENTO y no por SESIÓN (multiplicar a quien navega mucho);
  · dar por bueno un cupón de campaña que vendió pero no dejó rastro, porque el
    único vínculo que existía —`discount_codes.used_order`— sólo lo llenan los
    cupones de UN SOLO uso, y un código de campaña no se quema nunca.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import models
import server


# ==========================================================================
#  Doble de la base
# ==========================================================================
def _match(doc, filtro):
    for k, v in (filtro or {}).items():
        if isinstance(v, dict):
            if '$ne' in v and doc.get(k) == v['$ne']:
                return False
            if '$gte' in v and not (doc.get(k) or '') >= v['$gte']:
                return False
            if '$in' in v and doc.get(k) not in v['$in']:
                return False
            if '$nin' in v and doc.get(k) in v['$nin']:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, n):
        return [dict(d) for d in self.docs[:n]]


class FakeCol:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    def find(self, filtro=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, filtro)])

    async def find_one(self, filtro, proj=None):
        for d in self.docs:
            if _match(d, filtro):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, nombre):
        return self.cols.setdefault(nombre, FakeCol())

    def __getattr__(self, nombre):
        return self[nombre]


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(server, 'db', fake)
    return fake


# El panel filtra por fecha, así que un pedido de prueba tiene que traer la suya:
# sin `created_at` cae fuera del rango y la prueba pasaría por la razón equivocada.
HOY = server.now_iso()


def ev(sid, tipo, device='', w=0, **extra):
    return {'session_id': sid, 'type': tipo, 'device': device, 'screen_w': w,
            'created_at': HOY, **extra}


def fila(filas, dispositivo):
    return next(f for f in filas if f['dispositivo'] == dispositivo)


# ==========================================================================
#  1. LOS TRES DATOS NUEVOS LLEGAN Y SE GUARDAN
# ==========================================================================
def test_el_evento_acepta_aparato_ancho_y_ref():
    e = models.TrackEvent(type='visit', session_id='s1', device='telefono',
                          screen_w=375, ref_code='monicaf-15-r4yv')
    assert (e.device, e.screen_w) == ('telefono', 375)


def test_los_eventos_viejos_siguen_entrando_sin_los_campos_nuevos():
    """Nada de lo que ya se medía se rompe: los tres nacen vacíos."""
    e = models.TrackEvent(type='visit', session_id='s1')
    assert (e.device, e.screen_w, e.ref_code) == ('', 0, '')


def test_el_ref_del_distribuidor_se_guarda_en_mayusculas(db):
    asyncio.run(server.track_event(models.TrackEvent(
        type='visit', session_id='s1', ref_code='  monicaf-15-r4yv  ')))
    assert db.cols['events'].docs[0]['ref_code'] == 'MONICAF-15-R4YV'


def test_un_aparato_inventado_no_crea_una_categoria_nueva(db):
    """El navegador manda lo que quiera; el panel sólo entiende tres palabras.

    Si esto no se filtrara, cualquiera podría meter categorías basura en el corte
    que Christián va a comparar contra el 8.7%.
    """
    asyncio.run(server.track_event(models.TrackEvent(
        type='visit', session_id='s1', device='<script>reloj')))
    assert db.cols['events'].docs[0]['device'] == ''


def test_un_ancho_absurdo_se_recorta_pero_no_tira_el_evento(db):
    """Medir nunca debe perder un dato bueno por culpa de uno raro."""
    asyncio.run(server.track_event(models.TrackEvent(
        type='visit', session_id='s1', device='telefono', screen_w=99999999)))
    d = db.cols['events'].docs[0]
    assert d['screen_w'] == server.SCREEN_MAX
    assert d['device'] == 'telefono'          # el resto del evento sobrevive


def test_el_endpoint_de_eventos_no_pide_la_peticion():
    """🔒 Sin IP y sin User-Agent, y que se note en la firma.

    La promesa de privacidad de Christián es «nada de datos personales, ni huella
    digital, ni IP en claro». La forma de garantizarla no es acordarse de no
    guardarla: es que la función NO RECIBA la petición. Si alguien le añade un
    `Request`, esta prueba truena y obliga a discutirlo.
    """
    import inspect
    assert list(inspect.signature(server.track_event).parameters) == ['payload']


# ==========================================================================
#  2. EL EMBUDO PARTIDO POR DISPOSITIVO
# ==========================================================================
def test_el_aparato_es_de_la_sesion_no_del_evento():
    """Una sesión son cinco eventos: contarlos todos multiplicaría por cinco.

    Es la misma regla que ya usa el origen (utm). Sin ella, el escritorio que
    recorre seis fichas pesaría seis veces más que el teléfono que sólo mira la
    portada — justo al revés de lo que se quiere medir.
    """
    evs = [ev('s1', 'visit', 'telefono', 375),
           ev('s1', 'product_view', 'telefono', 375),
           ev('s1', 'product_view', 'telefono', 375)]
    filas, _, _ = server._embudo_por_dispositivo(evs)
    assert fila(filas, 'telefono')['visitas'] == 1
    assert fila(filas, 'telefono')['visita_a_ficha'] == 100.0


def test_las_sesiones_sin_aparato_no_se_meten_en_computadora():
    """⛔ LA LÍNEA HONESTA. Lo de antes del 2026-07-31 no dice aparato.

    Repartirlo en 'computadora' —o en cualquiera— sería inventar el dato con el que
    se va a decidir el presupuesto. Se cuenta aparte y a la vista.
    """
    evs = [ev('vieja', 'visit'), ev('nueva', 'visit', 'telefono', 375)]
    filas, _, sin = server._embudo_por_dispositivo(evs)
    assert sin == 1
    assert sum(f['visitas'] for f in filas) == 1       # la vieja NO se repartió


def test_el_embudo_de_cada_aparato_cuenta_sus_propios_pasos():
    """El corte que contesta si adelgazar la portada móvil sirvió."""
    evs = [
        ev('t1', 'visit', 'telefono', 375), ev('t2', 'visit', 'telefono', 390),
        ev('t3', 'visit', 'telefono', 414), ev('t4', 'visit', 'telefono', 375),
        ev('t1', 'product_view', 'telefono', 375),
        ev('c1', 'visit', 'computadora', 1440),
        ev('c1', 'product_view', 'computadora', 1440),
        ev('c1', 'purchase', 'computadora', 1440),
    ]
    filas, _, _ = server._embudo_por_dispositivo(evs)
    tel, comp = fila(filas, 'telefono'), fila(filas, 'computadora')
    assert (tel['visitas'], tel['visita_a_ficha'], tel['conversion']) == (4, 25.0, 0)
    assert (comp['visitas'], comp['visita_a_ficha'], comp['conversion']) == (1, 100.0, 100.0)
    # ...y el promedio de los dos (5 visitas, 2 fichas = 40%) no es NINGUNO de los
    # dos: eso es exactamente lo que escondía el 8.7% de la semana pasada.
    assert tel['visita_a_ficha'] != comp['visita_a_ficha']


def test_el_ancho_dice_si_la_portada_se_ve_a_375_o_a_1400():
    evs = [ev('t1', 'visit', 'telefono', 375), ev('c1', 'visit', 'computadora', 1400)]
    _, anchos, _ = server._embudo_por_dispositivo(evs)
    por_rango = {a['rango']: a['sesiones'] for a in anchos}
    assert por_rango['hasta 480 px (teléfono)'] == 1
    assert por_rango['más de 1024 px (monitor)'] == 1


def test_sin_eventos_no_truena_ni_inventa_porcentajes():
    filas, anchos, sin = server._embudo_por_dispositivo([])
    assert sin == 0
    assert all(f['visitas'] == 0 and f['visita_a_ficha'] == 0 for f in filas)
    assert all(a['sesiones'] == 0 for a in anchos)


# ==========================================================================
#  3. EL CÓDIGO DE WHATSAPP
# ==========================================================================
def test_el_codigo_no_delata_a_quien_lo_reparte():
    """🔒 REGLA DEL 2026-07-31: los códigos ya no dicen de quién son.

    Mónica reparte estos códigos en el chat, pero el prefijo público es de la CASA
    (`WA-`) y del ANUNCIO. Si algún día alguien mete el nombre del distribuidor en
    el texto, truena aquí.
    """
    code = server._texto_wa('Retatrutida', 'JUL')
    assert code.startswith('WA-')
    for nombre in ('MONICA', 'MONICAF', 'MARIA', 'NEUNFELD', 'ALANIS', 'JAVIER'):
        assert nombre not in code


def test_el_codigo_es_corto_para_que_se_pueda_teclear():
    """Lo teclea un cliente que lo leyó en un chat. Cada letra de más es una
    venta que se cae con «Codigo no valido» y nadie se entera."""
    assert server._texto_wa('Retatrutida y tirzepatida en un mismo vial', 'JUL') == 'WA-RETATRUT-JUL'
    assert len(server._texto_wa('Asesoría en todo el proceso')) <= 12


def test_un_anuncio_sin_nombre_no_produce_un_codigo_roto():
    assert server._texto_wa('', '') == 'WA-GRAL'
    assert server._texto_wa('¡!¿?', 'JUL') == 'WA-GRAL-JUL'


def test_el_codigo_de_campana_es_reutilizable_y_el_de_conversacion_no(db):
    """Los dos modos, y por qué son distintos.

    Sin `cantidad`: UN código por anuncio, reutilizable, el mismo en los cien
    chats. Con `cantidad`: uno por conversación, de un solo uso.
    """
    r = asyncio.run(server.admin_crear_codigos_whatsapp(
        server.WhatsAppCode(campana='Reta', mes='JUL'), admin={'id': 'a'}))
    assert r['codigos'] == ['WA-RETA-JUL'] and r['reutilizable'] is True
    assert db.cols['discount_codes'].docs[0]['single_use'] is False

    r2 = asyncio.run(server.admin_crear_codigos_whatsapp(
        server.WhatsAppCode(campana='Reta', mes='JUL', cantidad=3), admin={'id': 'a'}))
    assert len(r2['codigos']) == 3 and len(set(r2['codigos'])) == 3
    assert all(c.startswith('WA-RETA-JUL-') for c in r2['codigos'])
    assert r2['reutilizable'] is False


def test_pedir_dos_veces_el_mismo_anuncio_no_parte_sus_ventas_en_dos(db):
    """Dos clics del botón no pueden crear dos códigos gemelos: las ventas del
    mismo anuncio acabarían repartidas en dos renglones del panel."""
    for _ in range(2):
        asyncio.run(server.admin_crear_codigos_whatsapp(
            server.WhatsAppCode(campana='Reta', mes='JUL'), admin={'id': 'a'}))
    assert len(db.cols['discount_codes'].docs) == 1


def test_el_cupon_de_whatsapp_no_es_una_puerta_trasera_al_50(db):
    """El techo de la casa es 40% y aquí también. Si no, el panel prometería un
    descuento que el checkout (`tasa_de_cupon`) volvería a topar al cobrar."""
    r = asyncio.run(server.admin_crear_codigos_whatsapp(
        server.WhatsAppCode(campana='Reta', discount_rate=0.90), admin={'id': 'a'}))
    assert r['discount_rate'] == server.TECHO_DESCUENTO == 0.40


def test_sin_anuncio_no_se_crea_el_codigo(db):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.admin_crear_codigos_whatsapp(
            server.WhatsAppCode(campana='  '), admin={'id': 'a'}))
    assert e.value.status_code == 400


# ==========================================================================
#  4. EL CÍRCULO SE CIERRA: CONVERSACIÓN → VENTA
# ==========================================================================
def test_el_pedido_guarda_el_TEXTO_del_cupon():
    """⛔ LA PIEZA QUE FALTABA, y sin la cual nada de esto mide.

    El vínculo cupón→venta vivía SÓLO en el cupón (`used_order`), que se llena al
    QUEMARLO. Un código de campaña multiuso no se quema nunca, así que podía vender
    y aun así salir en el panel como «mandado y jamás usado».
    """
    assert 'coupon_code' in models.Order.model_fields
    assert models.Order.model_fields['coupon_code'].default == ''


def test_el_checkout_escribe_el_cupon_en_el_pedido():
    """Guardia de código: que la línea siga ahí y siga saliendo del cupón.

    Se comprueba sobre el texto de `create_order` porque montar un checkout entero
    con doble de base aquí probaría el doble, no el checkout.
    """
    src = open(server.__file__, encoding='utf-8').read()
    cuerpo = src.split('async def create_order')[1].split('\nasync def ')[0]
    assert "coupon_code=(coupon.get('code') or '') if coupon else ''," in cuerpo


def test_el_panel_cuenta_conversaciones_codigos_usados_y_pesos(db, monkeypatch):
    """La respuesta completa: 110 conversaciones → cuántas compraron y cuánto."""
    monkeypatch.setattr(server, '_meta_filas', _meta_falso(conversaciones=110, gasto=237.0))
    db.cols['discount_codes'] = FakeCol([
        {'code': 'WA-RETA-JUL', 'campana_wa': 'reta', 'created_by': 'whatsapp',
         'single_use': False, 'expires_at': '2026-08-30T00:00:00+00:00'},
        {'code': 'WA-ASESOR-JUL', 'campana_wa': 'asesor', 'created_by': 'whatsapp',
         'single_use': False, 'expires_at': '2026-08-30T00:00:00+00:00'},
    ])
    db.cols['orders'] = FakeCol([
        {'order_number': 'EX-1', 'coupon_code': 'WA-RETA-JUL', 'total': 5000, 'created_at': HOY,
         'status': 'entregado', 'paid': True, 'first_order': True},
        {'order_number': 'EX-2', 'coupon_code': 'WA-RETA-JUL', 'total': 3000, 'created_at': HOY,
         'status': 'entregado', 'paid': True, 'first_order': False},
    ])
    r = asyncio.run(server.admin_whatsapp(days=7, admin={'id': 'a'}))

    assert r['conversaciones'] == 110
    assert r['costo_conversacion_usd'] == 2.15
    assert r['ventas'] == 2
    assert r['cobrado_mxn'] == 8000
    assert r['medible'] is True
    reta = next(c for c in r['campanas'] if c['campana'] == 'reta')
    # UN código entregado, usado (aunque multiuso), DOS pedidos: son cosas distintas
    # y el panel no las confunde.
    assert (reta['entregados'], reta['usados'], reta['pedidos']) == (1, 1, 2)
    assert reta['clientes_nuevos'] == 1
    asesor = next(c for c in r['campanas'] if c['campana'] == 'asesor')
    assert (asesor['usados'], asesor['pedidos'], asesor['cobrado_mxn']) == (0, 0, 0)


def test_un_pedido_cancelado_no_cuenta_como_venta_de_whatsapp(db, monkeypatch):
    monkeypatch.setattr(server, '_meta_filas', _meta_falso(conversaciones=10, gasto=10.0))
    db.cols['discount_codes'] = FakeCol([
        {'code': 'WA-RETA-JUL', 'campana_wa': 'reta', 'created_by': 'whatsapp',
         'single_use': False, 'expires_at': ''}])
    db.cols['orders'] = FakeCol([
        {'order_number': 'EX-9', 'coupon_code': 'WA-RETA-JUL', 'total': 9000, 'created_at': HOY,
         'status': 'cancelado', 'paid': False, 'first_order': True}])
    r = asyncio.run(server.admin_whatsapp(days=7, admin={'id': 'a'}))
    assert r['ventas'] == 0 and r['cobrado_mxn'] == 0


def test_cero_ventas_sin_codigos_repartidos_NO_significa_que_no_vendan(db, monkeypatch):
    """⛔ LA TRAMPA MÁS CARA DE ESTE PANEL.

    Un 0% de conversión con códigos repartidos dice «WhatsApp no vende: bájale».
    El mismo 0% SIN códigos repartidos no dice nada — es justo la situación de
    esta semana, y confundirlas costaría el presupuesto. Por eso `medible` viaja
    al lado y el panel tiene que enseñarlo.
    """
    monkeypatch.setattr(server, '_meta_filas', _meta_falso(conversaciones=110, gasto=237.0))
    r = asyncio.run(server.admin_whatsapp(days=7, admin={'id': 'a'}))
    assert r['conversaciones'] == 110
    assert r['conversion'] == 0
    assert r['medible'] is False


# ==========================================================================
#  5. LOS ENLACES QUE CHRISTIÁN PEGA EN META
# ==========================================================================
def test_los_enlaces_mandan_a_la_FICHA_de_retatrutida_no_a_la_portada():
    """Retatrutida se lleva 68 de las 118 vistas de producto (58%). Mandar ese
    clic a la portada le cobra dos toques más al visitante."""
    enlaces = server._enlaces_de_retatrutida()
    con_url = [e for e in enlaces if e['url'].startswith('http')]
    assert con_url, 'tiene que haber al menos un enlace completo que pegar'
    for e in con_url:
        assert '/producto/retatrutida/' in e['url']


def test_los_enlaces_vienen_etiquetados_o_la_venta_cae_en_sin_etiquetar():
    for e in server._enlaces_de_retatrutida():
        assert 'utm_source={{site_source_name}}' in e['url']   # macro oficial de Meta
        assert 'utm_medium=paid' in e['url']
        assert 'utm_campaign=' in e['url']


def test_lo_que_manda_la_macro_de_meta_lo_entiende_el_panel():
    """`{{site_source_name}}` se rellena sola con 'fb' / 'ig'. Si el cruce no los
    reconociera, todo el tráfico etiquetado caería igualmente en «no es de Meta»."""
    import marketing
    assert marketing.es_de_meta({'utm_source': 'fb'}) is True
    assert marketing.es_de_meta({'utm_source': 'ig'}) is True


# ==========================================================================
#  Utilidades
# ==========================================================================
def _meta_falso(conversaciones, gasto):
    async def _fake(days=30, forzar=False):
        return ([{'conversaciones': conversaciones, 'spend': gasto}],
                {'fuente': 'meta_en_vivo', 'actualizado': '', 'edad_segundos': 0, 'aviso': ''})
    return _fake
