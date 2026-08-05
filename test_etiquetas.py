"""IMPRIMIR LA GUÍA DESDE EL PANEL — con el candado de rol probado con dientes.

⛔ ORDEN DE CHRISTIÁN (2026-07-31): «¿Puedes hacer que recibamos la guía para imprimir
en nuestro panel de distribuidor o admin panel? Recuerda que quiero manejar TODO desde
nuestra app». El botón se puso en la ficha del pedido, que se abre desde ocho lugares —
o sea, en el panel del admin Y en el del distribuidor.

LO QUE SE PRUEBA AQUÍ, y por qué cada cosa:

  1. ⛔ EL CANDADO. Un distribuidor NO puede sacar la etiqueta de un pedido ajeno.
     No es un detalle de privacidad menor: una etiqueta de FedEx trae impreso el
     NOMBRE Y EL DOMICILIO COMPLETO del cliente de otro. Esconder el botón en la
     pantalla no sirve de nada — el número de pedido ajeno se teclea en la barra de
     direcciones. Por eso el candado vive en el servidor y por eso se prueba.

  2. La liga cruda del proveedor NUNCA viaja al distribuidor: él recibe el PDF por
     nuestra ruta. Esa URL es la cuenta de envíos de la casa.

  3. Una liga firmada CADUCADA no puede verse como un botón roto: se vuelve a pedir
     la etiqueta por número de rastreo y se sirve la nueva, sin que nadie se entere.

  4. No depende de la plataforma: si el pedido se despachó con Envíos Internacionales,
     se le pregunta a ÉSE, no a Skydropx.

  5. El PDF que todavía no publica la paquetería contesta 409 «generando», no un
     error feo: es un «espérate tantito» y la pantalla lo reintenta sola.
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import auth
import etiquetas
import guias
import server


PDF_VIEJO = b'%PDF-1.4 la etiqueta guardada'
PDF_FRESCO = b'%PDF-1.4 la etiqueta rescatada'
# Lo que devuelve una liga firmada que ya caducó: 200, pero HTML de error. Imprimir
# esto saca una hoja con la palabra «Forbidden» y el paquete se va sin etiqueta.
HTML_CADUCADO = b'<html><body>Link expired</body></html>'

MARIA = {'id': 'u-maria', 'name': 'Maria', 'role': 'distributor'}
OTRO = {'id': 'u-otro', 'name': 'Otro', 'role': 'distributor'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'role': 'user'}
ADMIN = {'id': 'u-admin', 'name': 'Christian', 'role': 'admin'}

# El pedido REAL de Brenda: comprado con Envíos Internacionales, guía de FedEx.
BRENDA = {
    'id': 'fbded9c5-e0dc-4840-bf87-8e1a0cfaaa75',
    'order_number': 'EX-20260730-5930',
    'referred_by': MARIA['id'],
    'status': 'enviado', 'created_at': '2026-07-30T18:00:00',
    'total': 4827.0, 'subtotal': 5637.0, 'items': [],
    'carrier': 'FedEx', 'tracking_number': '875164874865',
    'label_url': 'https://app.enviosinternacionales.com/s/s?id=firmada',
    'label_provider': 'enviosinternacionales',
    'customer': {'full_name': 'Brenda Iliana Oseguera Gonzalez'},
}

# Un pedido de OTRO distribuidor. Es el que nunca debe abrirse.
AJENO = dict(BRENDA, id='o-ajeno', order_number='EX-20260730-0001',
             referred_by=OTRO['id'], tracking_number='999999999999')

# Guía TECLEADA a mano: no la compramos nosotros, no hay PDF que traer.
A_MANO = {'id': 'o-mano', 'order_number': 'EX-20260730-0002', 'referred_by': MARIA['id'],
          'status': 'enviado', 'created_at': '2026-07-30T18:00:00', 'items': [],
          'total': 0, 'subtotal': 0,
          'carrier': 'Estafeta', 'tracking_number': '7712345678',
          'label_url': '', 'label_provider': '', 'customer': {'full_name': 'X'}}

def pedidos_frescos():
    """Copias nuevas en cada prueba: el rescate ESCRIBE el `label_url` y una prueba
    que ensucia a la siguiente es una prueba que engaña."""
    import copy
    return copy.deepcopy([BRENDA, AJENO, A_MANO])


# --------------------------------------------------------- base de datos falsa
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Coll:
    def __init__(self, docs=()):
        self._docs = list(docs)
        self.escrituras = []

    def find(self, *a, **k):
        return _Cursor(self._docs)

    async def find_one(self, filtro=None, *a, **k):
        # `$or` porque la ruta acepta número de pedido O id: la ficha unificada se
        # abre desde ocho lugares y no todas traen la misma llave en la mano.
        ramas = (filtro or {}).get('$or') or [filtro or {}]
        for d in self._docs:
            for rama in ramas:
                if rama and all(d.get(k2) == v for k2, v in rama.items()):
                    return dict(d)
        return None

    async def update_one(self, filtro, cambio, *a, **k):
        self.escrituras.append((filtro, cambio))
        for d in self._docs:
            if all(d.get(k2) == v for k2, v in (filtro or {}).items()):
                d.update((cambio or {}).get('$set') or {})
        return None


class _FakeDB:
    def __init__(self):
        self.orders = _Coll(pedidos_frescos())

    def __getattr__(self, nombre):
        return _Coll()


class _Respuesta:
    def __init__(self, contenido, status=200):
        self.content = contenido
        self.status_code = status


class _Paqueteria:
    """Un proveedor de mentiras que anota qué le preguntaron."""

    def __init__(self, url='', encendido=True):
        self.url = url
        self.encendido = encendido
        self.preguntas = []

    def enabled(self):
        return self.encendido

    def etiqueta_por_rastreo(self, numero):
        self.preguntas.append(numero)
        return {'label_url': self.url} if self.url else {}


@pytest.fixture
def mundo(monkeypatch):
    """Deja el mundo en pie y devuelve las piezas para poder espiarlas."""
    db = _FakeDB()
    monkeypatch.setattr(etiquetas, 'db', db)
    bajadas = []

    def _get(url, **kw):
        bajadas.append(url)
        return _Respuesta(PDF_VIEJO)

    monkeypatch.setattr(etiquetas.requests, 'get', _get)
    proveedores = {}
    monkeypatch.setattr(etiquetas.paqueterias, 'modulo', lambda c: proveedores.get(c))

    class Mundo:
        pass

    m = Mundo()
    m.db, m.bajadas, m.proveedores = db, bajadas, proveedores
    m.responder = lambda fn: monkeypatch.setattr(etiquetas.requests, 'get', fn)
    return m


@pytest.fixture
def como():
    """`como(usuario)` = cliente HTTP con esa sesión."""
    def _factory(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


RUTA_DIST = '/api/distributor/orders/{}/etiqueta'
RUTA_ADMIN = '/api/admin/orders/{}/etiqueta'


# =============================================================================
#  1) EL CANDADO — lo primero, porque es lo único que no puede fallar nunca
# =============================================================================
def test_un_distribuidor_NO_saca_la_etiqueta_de_un_pedido_ajeno(mundo, como):
    """⛔ LA PRUEBA MADRE. Una etiqueta trae el nombre y el domicilio completo del
    cliente de otro distribuidor. Que el botón no se pinte no basta: el número de
    pedido ajeno se teclea en la barra de direcciones."""
    r = como(MARIA).get(RUTA_DIST.format(AJENO['order_number']))
    assert r.status_code == 403
    # Y no se alcanzó a pedir NADA a la paquetería: el candado corta antes.
    assert mundo.bajadas == []


def test_tampoco_por_el_id_interno_del_pedido(mundo, como):
    """La ruta acepta número de pedido o id. Las dos puertas, el mismo candado."""
    assert como(MARIA).get(RUTA_DIST.format(AJENO['id'])).status_code == 403


def test_un_cliente_normal_no_entra(mundo, como):
    assert como(CLIENTE).get(RUTA_DIST.format(BRENDA['order_number'])).status_code == 403


def test_un_pedido_que_no_existe_es_404(mundo, como):
    assert como(MARIA).get(RUTA_DIST.format('EX-NO-EXISTE')).status_code == 404


def test_sin_sesion_no_hay_etiqueta(mundo):
    server.app.dependency_overrides.clear()
    r = TestClient(server.app).get(RUTA_DIST.format(BRENDA['order_number']))
    assert r.status_code in (401, 403)


# =============================================================================
#  2) LO QUE SÍ: su propia guía, en PDF, por nuestra ruta
# =============================================================================
def test_el_distribuidor_imprime_la_guia_de_SU_pedido(mundo, como):
    r = como(MARIA).get(RUTA_DIST.format(BRENDA['order_number']))
    assert r.status_code == 200
    assert r.headers['content-type'] == 'application/pdf'
    assert r.content == PDF_VIEJO
    # El nombre del archivo lleva el número de pedido: quien imprime cinco seguidas
    # tiene que poder saber cuál es cuál sin abrirlas.
    assert BRENDA['order_number'] in r.headers['content-disposition']


def test_el_admin_saca_la_de_cualquier_pedido(mundo, como):
    r = como(ADMIN).get(RUTA_ADMIN.format(AJENO['order_number']))
    assert r.status_code == 200
    assert r.content == PDF_VIEJO


def test_al_distribuidor_no_le_viaja_la_liga_del_proveedor(mundo, como):
    """La URL firmada es la cuenta de envíos de la casa. Él recibe el PAPEL, no la liga."""
    ficha = server._detalle_de_pedido(BRENDA, MARIA['id'], dist=MARIA)
    assert 'label_url' not in ficha
    assert ficha['tiene_etiqueta'] is True


def test_una_guia_tecleada_a_mano_SI_ofrece_boton_y_el_servidor_explica(mundo, como):
    """⛔ SE INVIRTIÓ EL 2026-08-05, y por una queja concreta de Christián: «no puedo
    imprimir las guías, teníamos un botón específico para eso y ya no está».

    La regla vieja («si no la compramos nosotros, no hay PDF») sonaba prudente y era
    falsa: `etiquetas._rescatar()` le pregunta a la paquetería POR NÚMERO DE RASTREO,
    así que una guía comprada en la cuenta de la casa y luego capturada a mano —el pan
    de cada día— sí tiene papel. El botón se escondía justo donde habría funcionado.

    Ahora el botón sale siempre que haya número. Cuando de verdad no hay PDF, el
    servidor lo dice con todas sus letras en vez de desaparecer: 409 `estado: manual`.
    Un botón invisible se ve igual que una app rota; un botón que explica, no.
    """
    ficha = server._detalle_de_pedido(A_MANO, MARIA['id'], dist=MARIA)
    assert ficha['tiene_etiqueta'] is True          # el botón se pinta
    assert ficha['etiqueta_comprada'] is False      # pero sabemos que no es nuestra


# =============================================================================
#  3) LA LIGA QUE CADUCÓ — no puede verse como un botón roto
# =============================================================================
def test_si_la_liga_firmada_caduco_se_rescata_sola(mundo, como):
    fresca = 'https://app.enviosinternacionales.com/s/s?id=nueva'
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(url=fresca)

    def _get(url, **kw):
        mundo.bajadas.append(url)
        # La vieja ya no sirve: 200 con HTML de error, que es lo que hacen de verdad.
        return _Respuesta(PDF_FRESCO if url == fresca else HTML_CADUCADO)

    mundo.responder(_get)
    r = como(MARIA).get(RUTA_DIST.format(BRENDA['order_number']))
    assert r.status_code == 200
    assert r.content == PDF_FRESCO
    # Y la liga nueva quedó escrita: la próxima impresión ya no sale a preguntar.
    assert any(c.get('$set', {}).get('label_url') == fresca
               for _f, c in mundo.db.orders.escrituras)


def test_un_html_de_error_nunca_se_manda_a_la_impresora(mundo, como):
    """Sin PDF y sin rescate posible: 409, no una hoja que dice «Forbidden»."""
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(url='')
    mundo.responder(lambda url, **kw: _Respuesta(HTML_CADUCADO))
    r = como(MARIA).get(RUTA_DIST.format(BRENDA['order_number']))
    assert r.status_code == 409
    assert r.json()['detail']['estado'] == 'generando'


# =============================================================================
#  4) NO DEPENDE DE LA PLATAFORMA
# =============================================================================
def test_se_le_pregunta_al_proveedor_QUE_DESPACHO_ese_pedido(mundo, como):
    """Brenda se despachó con Envíos Internacionales. Preguntarle a Skydropx por esa
    guía es, en el mejor caso, no encontrarla."""
    ei = _Paqueteria(url='https://ei.test/nueva')
    sky = _Paqueteria(url='https://sky.test/otra')
    mundo.proveedores.update({'enviosinternacionales': ei, 'skydropx': sky})
    mundo.responder(lambda url, **kw: _Respuesta(
        PDF_FRESCO if url.startswith('https://ei.test') else HTML_CADUCADO))

    r = como(MARIA).get(RUTA_DIST.format(BRENDA['order_number']))
    assert r.status_code == 200
    assert ei.preguntas == [BRENDA['tracking_number']]
    assert sky.preguntas == [], 'se le preguntó a la paquetería equivocada'


def test_un_proveedor_apagado_no_truena_feo(mundo, como):
    mundo.proveedores['enviosinternacionales'] = _Paqueteria(url='x', encendido=False)
    mundo.responder(lambda url, **kw: _Respuesta(HTML_CADUCADO))
    assert como(MARIA).get(RUTA_DIST.format(BRENDA['order_number'])).status_code == 409


def test_una_paqueteria_que_revienta_tampoco(mundo, como):
    class Explota(_Paqueteria):
        def etiqueta_por_rastreo(self, numero):
            raise RuntimeError('la API se cayó')

    mundo.proveedores['enviosinternacionales'] = Explota()
    mundo.responder(lambda url, **kw: _Respuesta(HTML_CADUCADO))
    assert como(MARIA).get(RUTA_DIST.format(BRENDA['order_number'])).status_code == 409


# =============================================================================
#  5) EL PDF QUE TODAVÍA NO EXISTE — «espérate tantito», no un error
# =============================================================================
def test_sin_numero_de_guia_no_hay_nada_que_imprimir(mundo, como):
    sin_guia = dict(BRENDA, id='o-sin', order_number='EX-SIN', tracking_number='',
                    label_url='')
    mundo.db.orders._docs.append(sin_guia)
    r = como(MARIA).get(RUTA_DIST.format('EX-SIN'))
    assert r.status_code == 409
    assert 'guía' in r.json()['detail']['mensaje']


def test_el_pdf_que_aun_no_publican_contesta_generando(mundo, como):
    """Comprobado en la primera compra real: la paquetería devuelve el rastreo al
    instante y el PDF unos segundos después."""
    recien = dict(BRENDA, id='o-recien', order_number='EX-RECIEN', label_url='',
                  label_provider='skydropx')
    mundo.db.orders._docs.append(recien)
    mundo.proveedores['skydropx'] = _Paqueteria(url='')
    r = como(MARIA).get(RUTA_DIST.format('EX-RECIEN'))
    assert r.status_code == 409
    assert r.json()['detail']['estado'] == 'generando'


def test_cuando_por_fin_lo_publican_se_imprime_sin_tocar_nada_mas(mundo, como):
    recien = dict(BRENDA, id='o-recien2', order_number='EX-RECIEN2', label_url='',
                  label_provider='skydropx')
    mundo.db.orders._docs.append(recien)
    mundo.proveedores['skydropx'] = _Paqueteria(url='https://sky.test/ya')
    mundo.responder(lambda url, **kw: _Respuesta(PDF_FRESCO))
    r = como(MARIA).get(RUTA_DIST.format('EX-RECIEN2'))
    assert r.status_code == 200
    assert r.content == PDF_FRESCO


# =============================================================================
#  6) TENER GUÍA NO ES HABER ENVIADO  (Christián, 2026-08-05)
# =============================================================================
#  «No puede aparecer un envío como enviado a menos que en verdad se haya enviado.
#   Puede aparecer como guía generada, pero por ejemplo el de Fabiola aún no lo
#   envío yo».
#
#  El caso Fabiola: guía comprada e impresa, paquete todavía en la mesa. Antes el
#  sistema lo daba por salido y se lo decía al cliente por correo. Estas pruebas son
#  el candado de que no vuelva a pasar.
def test_con_guia_y_sin_salir_la_etapa_es_GUIA_GENERADA():
    """El cajón de Fabiola: hay número, no ha salido."""
    assert guias.etapa_de_envio(
        {'status': 'confirmado', 'tracking_number': 'ABC123'}) == 'guia_generada'


def test_solo_lo_que_de_verdad_salio_dice_ENVIADO():
    assert guias.etapa_de_envio({'status': 'enviado'}) == 'enviado'
    # `shipped_at` manda aunque el estado se haya quedado atrás: la fecha de salida
    # es un hecho, el estado es una etiqueta que alguien pudo no mover.
    assert guias.etapa_de_envio(
        {'status': 'confirmado', 'shipped_at': '2026-08-01T10:00:00'}) == 'enviado'


def test_sin_numero_no_hay_guia_que_enseñar():
    assert guias.etapa_de_envio({'status': 'confirmado'}) == 'sin_guia'
    assert guias.etapa_de_envio({}) == 'sin_guia'


def test_ya_salio_es_la_pregunta_de_los_correos_no_tiene_guia():
    """Todo lo que le PROMETE movimiento a alguien pregunta por aquí."""
    assert guias.ya_salio({'tracking_number': 'ABC123'}) is False
    assert guias.ya_salio({'tracking_number': 'ABC123', 'status': 'enviado'}) is True


def test_capturar_la_guia_NO_empuja_el_pedido_a_enviado():
    """⛔ EL CANDADO DE LA REGLA. Si alguien vuelve a meter el auto-'enviado' en la
    ruta que guarda el envío, esto se pone rojo.

    Se lee el código a propósito: el bug vivía en dos líneas que empujaban el estado
    y estampaban `shipped_at` sin que nadie dijera que el paquete salió.
    """
    import inspect
    cuerpo = inspect.getsource(server._guardar_envio)
    assert "update['status'] = 'enviado'" not in cuerpo, (
        'capturar una guía volvió a marcar el pedido como enviado')


# =============================================================================
#  7) SI NO SABEMOS DE QUIÉN ES LA GUÍA, SE LE PREGUNTA A TODOS
# =============================================================================
#  El caso de Fabiola (2026-08-05): la compra de la guía falló a medias, alguien
#  capturó el número a mano y el pedido quedó SIN `label_provider`. El rescate
#  asumía Skydropx y ahí no estaba — pero el número SÍ aparecía en la lista de
#  Envíos Internacionales. El papel existía y nadie se lo pedía a quien lo tenía.
def test_sin_proveedor_conocido_se_barren_todos_hasta_dar_con_el(monkeypatch):
    preguntados = []

    class _Mod:
        def __init__(self, tiene): self.tiene = tiene
        def enabled(self): return True
        def etiqueta_por_rastreo(self, tn):
            preguntados.append(self.tiene)
            return {'label_url': 'https://papel/ok'} if self.tiene == 'quien_la_tiene' else {}

    mods = {'skydropx': _Mod('no_la_tiene'), 'enviosinternacionales': _Mod('quien_la_tiene')}
    monkeypatch.setattr(etiquetas.paqueterias, 'modulo', lambda c: mods.get(c))
    monkeypatch.setattr(etiquetas.paqueterias, 'encendidos', lambda: [
        {'clave': 'skydropx', 'nombre': 'Skydropx', 'activo': True},
        {'clave': 'enviosinternacionales', 'nombre': 'EI', 'activo': True}])

    assert etiquetas._rescatar('', 'TRK-DE-FABIOLA') == 'https://papel/ok'
    assert preguntados == ['no_la_tiene', 'quien_la_tiene'], 'debe seguir buscando'


def test_con_proveedor_conocido_NO_se_le_pregunta_a_nadie_mas(monkeypatch):
    """El barrido es sólo para cuando no se sabe: no se anda molestando a todos."""
    preguntados = []

    class _Mod:
        def __init__(self, clave): self.clave = clave
        def enabled(self): return True
        def etiqueta_por_rastreo(self, tn):
            preguntados.append(self.clave)
            return {'label_url': 'https://papel/' + self.clave}

    monkeypatch.setattr(etiquetas.paqueterias, 'modulo', lambda c: _Mod(c))
    monkeypatch.setattr(etiquetas.paqueterias, 'encendidos',
                        lambda: [{'clave': 'skydropx', 'nombre': 'S', 'activo': True}])
    assert etiquetas._rescatar('enviosinternacionales', 'TRK') == 'https://papel/enviosinternacionales'
    assert preguntados == ['enviosinternacionales']
