"""Pruebas del ACUERDO DE DISTRIBUIDOR (aceptación electrónica).

Lo que estas pruebas defienden, en orden de importancia:

  1. ⛔ QUE APAGADO NO ESTORBE. Es la mitad del encargo. Hoy el sistema está
     apagado y NADA puede cambiar para ningún distribuidor: ni un bloqueo, ni
     una pantalla, ni un peso menos de comisión. Si alguien rompe eso, aquí
     truena.
  2. Que ENCENDIDO sí exija: sin firmar no se generan códigos, no se cotiza y
     no se devengan comisiones nuevas.
  3. Que la prueba quede bien levantada: versión, huella del texto, fecha, IP y
     user-agent (Código de Comercio arts. 93, 93 Bis y 1298-A).
  4. Que una versión nueva vuelva a pedir la firma.
  5. Que el candado esté COLGADO de verdad en las rutas (no basta con que la
     función exista: hay que probar que server.py la llama).

La base de datos se sustituye por un doble en memoria: estas pruebas no tocan
Mongo ni la red, igual que el resto del repo. Las funciones de acuerdo.py son
`async` (la base lo es), pero el repo no tiene pytest-asyncio y no se le va a
meter una dependencia por esto: se corren con `corre()`, que es `asyncio.run`.

Correr:  pytest test_acuerdo.py -q
"""
import asyncio
import inspect
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest

import acuerdo
import pyramid


def corre(coro):
    """Ejecuta una corrutina en una prueba normal (sin pytest-asyncio)."""
    return asyncio.run(coro)


# --------------------------------------------------------------- dobles de prueba
class _Coleccion:
    """Lo mínimo de una colección de Mongo que usa acuerdo.py."""

    def __init__(self):
        self.filas = []

    def find(self, filtro, proyeccion=None):
        return _Cursor([f for f in self.filas if _casa(f, filtro)])

    async def find_one(self, filtro, proyeccion=None):
        for f in self.filas:
            if _casa(f, filtro):
                return dict(f)
        return None

    async def insert_one(self, doc):
        self.filas.append(dict(doc))


class _Cursor:
    def __init__(self, filas):
        self.filas = filas

    async def to_list(self, n):
        return [dict(f) for f in self.filas[:n]]


def _casa(fila, filtro):
    for k, v in (filtro or {}).items():
        if isinstance(v, dict) and '$in' in v:
            if fila.get(k) not in v['$in']:
                return False
        elif fila.get(k) != v:
            return False
    return True


class _Db:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, nombre):
        return self.cols.setdefault(nombre, _Coleccion())


@pytest.fixture
def db():
    return _Db()


@pytest.fixture
def apagado(monkeypatch):
    """El estado de HOY: la variable no existe."""
    monkeypatch.delenv(acuerdo.ENV_INTERRUPTOR, raising=False)


@pytest.fixture
def encendido(monkeypatch):
    monkeypatch.setenv(acuerdo.ENV_INTERRUPTOR, 'true')


DIST = {'id': 'd1', 'role': 'distributor', 'email': 'maria@x.com', 'name': 'María',
        'distributor_code': 'MARI-4821'}
UPLINE = {'id': 'd2', 'role': 'distributor', 'email': 'ana@x.com', 'name': 'Ana'}
ADMIN = {'id': 'a1', 'role': 'admin', 'email': 'admin@x.com', 'name': 'Christián'}
CLIENTE = {'id': 'u1', 'role': 'user', 'email': 'juan@x.com', 'name': 'Juan'}


class _Request:
    def __init__(self, headers=None, host='127.0.0.1'):
        self.headers = headers or {}
        self.client = type('C', (), {'host': host})()


# ======================================================================
#  1. APAGADO NO ESTORBA  — la prueba que más importa hoy
# ======================================================================
def test_apagado_por_omision(apagado):
    """Sin la variable, el sistema está apagado. Es el valor seguro."""
    assert acuerdo.activo() is False


@pytest.mark.parametrize('valor', ['', 'false', 'False', 'no', '0', 'tru', 'off', 'apagado'])
def test_una_errata_no_enciende_el_sistema(monkeypatch, valor):
    """Un `.env` con `ACUERDO_DISTRIBUIDOR_ACTIVO=tru` deja el sistema APAGADO.
    El valor seguro es el de por omisión, y sólo una palabra clara enciende."""
    monkeypatch.setenv(acuerdo.ENV_INTERRUPTOR, valor)
    assert acuerdo.activo() is False


@pytest.mark.parametrize('valor', ['true', 'True', 'TRUE ', ' 1', 'si', 'sí', 'yes'])
def test_las_formas_de_encender(monkeypatch, valor):
    """Se recortan espacios y no importan mayúsculas: pegar el valor con un
    espacio de más no puede dejarlo apagado sin que nadie se entere."""
    monkeypatch.setenv(acuerdo.ENV_INTERRUPTOR, valor)
    assert acuerdo.activo() is True


def test_apagado_no_bloquea_a_nadie(apagado, db):
    """Ni al distribuidor que no ha firmado (que hoy son TODOS)."""
    assert corre(acuerdo.bloquea(db, DIST)) is False
    assert corre(acuerdo.bloquea(db, ADMIN)) is False
    assert corre(acuerdo.bloquea(db, CLIENTE)) is False


def test_apagado_no_pide_pantalla(apagado, db):
    estado = corre(acuerdo.estado_para(db, DIST))
    assert estado['activo'] is False
    assert estado['aplica'] is False
    assert estado['requiere_aceptacion'] is False


def test_apagado_no_toca_una_sola_comision(apagado, db):
    """El caso que costaría dinero de verdad: que un despliegue empezara a
    tragarse comisiones sin que nadie lo pidiera."""
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000},
             {'distributor_id': 'd2', 'role': 'override', 'amount': 800}]
    assert corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas)) == filas


def test_apagado_ni_consulta_la_base(apagado):
    """Y no sólo devuelve lo mismo: NO PREGUNTA. Si tocara la base, este doble
    —que revienta al usarse— lo delataría. Es la prueba de que el camino de hoy
    no paga ni un viaje de red."""
    class _Explota:
        def __getitem__(self, _):
            raise AssertionError('con el interruptor apagado no se consulta la base')

    filas = [{'distributor_id': 'd1', 'amount': 1}]
    assert corre(acuerdo.filtrar_comisiones_sin_acuerdo(_Explota(), filas)) == filas
    assert corre(acuerdo.bloquea(_Explota(), DIST)) is False


# ======================================================================
#  2. ENCENDIDO SÍ EXIGE
# ======================================================================
def test_encendido_bloquea_al_que_no_firmo(encendido, db):
    assert corre(acuerdo.bloquea(db, DIST)) is True


def test_encendido_no_bloquea_al_admin(encendido, db):
    """La Empresa no firma consigo misma. Y si lo hiciera, un interruptor mal
    puesto dejaría a Christián sin su propio panel."""
    assert corre(acuerdo.bloquea(db, ADMIN)) is False


def test_encendido_no_bloquea_a_un_cliente(encendido, db):
    """El acuerdo es del canal de distribución. Un comprador normal ni se entera."""
    assert corre(acuerdo.bloquea(db, CLIENTE)) is False
    assert corre(acuerdo.estado_para(db, CLIENTE))['requiere_aceptacion'] is False


def test_encendido_pide_la_pantalla_al_distribuidor(encendido, db):
    estado = corre(acuerdo.estado_para(db, DIST))
    assert estado['activo'] is True
    assert estado['aplica'] is True
    assert estado['requiere_aceptacion'] is True
    assert estado['aceptado'] is False
    assert estado['version_anterior'] is None      # nunca ha firmado nada


def test_tras_firmar_deja_de_bloquear(encendido, db):
    corre(acuerdo.registrar(db, DIST, ip='187.1.2.3', user_agent='Mozilla'))
    assert corre(acuerdo.bloquea(db, DIST)) is False
    estado = corre(acuerdo.estado_para(db, DIST))
    assert estado['aceptado'] is True
    assert estado['requiere_aceptacion'] is False


def test_sin_firmar_no_se_devenga_comision(encendido, db):
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000}]
    assert corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas)) == []


def test_el_upline_que_si_firmo_cobra_igual(encendido, db):
    """No se castiga a quien cumplió por lo que dejó de hacer el de abajo."""
    corre(acuerdo.registrar(db, UPLINE, ip='187.1.2.4'))
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000},
             {'distributor_id': 'd2', 'role': 'override', 'amount': 800}]
    quedan = corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas))
    assert [f['distributor_id'] for f in quedan] == ['d2']
    assert quedan[0]['amount'] == 800


def test_firmados_los_dos_cobran_los_dos(encendido, db):
    corre(acuerdo.registrar(db, DIST))
    corre(acuerdo.registrar(db, UPLINE))
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000},
             {'distributor_id': 'd2', 'role': 'override', 'amount': 800}]
    assert corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas)) == filas


def test_el_reparto_filtrado_sigue_dando_la_tajada_del_vendedor(encendido, db):
    """El filtro devuelve algo que `pyramid.seller_amount` sigue entendiendo:
    si el vendedor cae, su `commission` en la orden queda en 0, no en None."""
    corre(acuerdo.registrar(db, UPLINE))
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000},
             {'distributor_id': 'd2', 'role': 'override', 'amount': 800}]
    quedan = corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas))
    assert pyramid.seller_amount(quedan) == 0
    assert pyramid.total_amount(quedan) == 800


# ======================================================================
#  3. LA PRUEBA QUE SE LEVANTA (arts. 93, 93 Bis y 1298-A)
# ======================================================================
def test_la_aceptacion_guarda_todo_lo_que_pide_la_ley(encendido, db):
    fila = corre(acuerdo.registrar(db, DIST, ip='187.190.1.2',
                                   user_agent='Mozilla/5.0 (iPhone)', origen='panel'))
    assert fila['user_id'] == 'd1'                       # quién
    assert fila['email'] == 'maria@x.com'
    assert fila['name'] == 'María'
    assert fila['distributor_code'] == 'MARI-4821'
    assert fila['version'] == acuerdo.VERSION            # sobre qué
    assert fila['documento_hash'] == acuerdo.hash_documento()
    assert fila['accepted_at'].startswith('20')          # cuándo (ISO, UTC)
    assert fila['accepted_at'].endswith('+00:00')
    assert fila['ip'] == '187.190.1.2'                   # desde dónde
    assert fila['user_agent'] == 'Mozilla/5.0 (iPhone)'
    assert fila['casilla_no_premarcada'] is True
    assert fila['origen'] == 'panel'


def test_se_guarda_en_la_coleccion_que_pidio_christian(encendido, db):
    corre(acuerdo.registrar(db, DIST))
    assert acuerdo.COLECCION == 'acuerdos_aceptados'
    assert len(db['acuerdos_aceptados'].filas) == 1


def test_firmar_dos_veces_no_duplica_ni_mueve_la_fecha(encendido, db):
    """La primera manifestación de voluntad es la que vale. Un doble clic o un
    reintento del navegador no puede reescribir la fecha de la prueba."""
    primera = corre(acuerdo.registrar(db, DIST, ip='1.1.1.1'))
    segunda = corre(acuerdo.registrar(db, DIST, ip='9.9.9.9'))
    assert len(db[acuerdo.COLECCION].filas) == 1
    assert segunda['accepted_at'] == primera['accepted_at']
    assert segunda['ip'] == '1.1.1.1'


def test_la_huella_cambia_si_el_texto_cambia():
    """Es lo que responde «¿este documento es el que firmé?» sin creerle a nadie."""
    assert acuerdo.hash_documento() != acuerdo.hash_documento(acuerdo.TEXTO + ' ')
    assert len(acuerdo.hash_documento()) == 64


def test_la_ip_sale_del_x_forwarded_for():
    """`request.client.host` es siempre 127.0.0.1 (hay dos proxies delante) y no
    prueba nada. La buena es la PRIMERA de X-Forwarded-For, que puso Caddy."""
    r = _Request({'x-forwarded-for': '187.190.1.2, 10.0.0.5, 172.17.0.1'})
    assert acuerdo.ip_de(r) == '187.190.1.2'


def test_sin_cabecera_cae_a_la_ip_directa():
    assert acuerdo.ip_de(_Request(host='203.0.113.7')) == '203.0.113.7'


def test_el_user_agent_se_recorta():
    assert len(acuerdo.user_agent_de(_Request({'user-agent': 'x' * 900}))) == 400


def test_la_ip_se_recorta():
    assert len(acuerdo.ip_de(_Request({'x-forwarded-for': '9' * 300}))) == 64


# ======================================================================
#  4. VERSIÓN NUEVA = SE VUELVE A FIRMAR
# ======================================================================
def test_una_version_nueva_vuelve_a_pedir_la_firma(encendido, db, monkeypatch):
    """Cambio esencial (cl. 4-e del propio acuerdo): nueva aceptación expresa."""
    corre(acuerdo.registrar(db, DIST))
    assert corre(acuerdo.bloquea(db, DIST)) is False

    monkeypatch.setattr(acuerdo, 'VERSION', 'v3-2026-09-01')
    assert corre(acuerdo.bloquea(db, DIST)) is True
    estado = corre(acuerdo.estado_para(db, DIST))
    assert estado['requiere_aceptacion'] is True
    # No se le trata como si nunca hubiera firmado: la pantalla puede decir
    # «el acuerdo cambió» en vez de «tienes que firmar por primera vez».
    assert estado['version_anterior'] == 'v2-borrador-2026-07-30'


def test_firmar_la_nueva_no_borra_la_vieja(encendido, db, monkeypatch):
    """Firmar la v3 no destruye la prueba de que en su día firmó la v2."""
    corre(acuerdo.registrar(db, DIST))
    monkeypatch.setattr(acuerdo, 'VERSION', 'v3-2026-09-01')
    corre(acuerdo.registrar(db, DIST))
    historial = corre(acuerdo.historial_de(db, 'd1'))
    assert [h['version'] for h in historial] == ['v3-2026-09-01', 'v2-borrador-2026-07-30']


def test_con_version_nueva_tampoco_se_devenga_comision(encendido, db, monkeypatch):
    corre(acuerdo.registrar(db, DIST))
    monkeypatch.setattr(acuerdo, 'VERSION', 'v3-2026-09-01')
    filas = [{'distributor_id': 'd1', 'role': 'seller', 'amount': 5000}]
    assert corre(acuerdo.filtrar_comisiones_sin_acuerdo(db, filas)) == []


# ======================================================================
#  5. EL DOCUMENTO: que se lea, que no inyecte y que se pueda descargar
# ======================================================================
def test_el_documento_viaja_completo():
    doc = acuerdo.documento()
    assert doc['version'] == acuerdo.VERSION
    assert doc['borrador'] is True          # todavía trae [corchetes]
    for clausula in ('1. Partes y objeto', '20. Disputas', 'Research Use Only'):
        assert clausula in doc['html']
    # Las 20 cláusulas, ni una menos.
    assert doc['html'].count('<h2>') == 20


def test_el_html_no_deja_inyectar_nada():
    """El texto legal lo escribe un humano en markdown. Si un día pega una
    etiqueta, tiene que salir como texto y nunca ejecutarse."""
    html = acuerdo.md_a_html('# <script>alert(1)</script>\n\nhola **<b>x</b>**')
    assert '<script>' not in html
    assert '&lt;script&gt;' in html and '&lt;b&gt;' in html
    assert '<strong>' in html               # el markdown de verdad sí se aplica


def test_negritas_y_cursivas_se_convierten():
    assert acuerdo.md_a_html('**hola**') == '<p><strong>hola</strong></p>'
    assert acuerdo.md_a_html('*hola*') == '<p><em>hola</em></p>'
    assert acuerdo.md_a_html('## Titulo') == '<h2>Titulo</h2>'


def test_el_html_no_trae_html_ni_body():
    """Son bloques para que el sitio los pinte con SU estilo (claro/oscuro)."""
    html = acuerdo.md_a_html()
    assert '<html' not in html and '<body' not in html and '<style' not in html


def test_la_copia_descargable_lleva_el_acta():
    copia = acuerdo.copia_imprimible({
        'name': 'María', 'email': 'maria@x.com', 'distributor_code': 'MARI-4821',
        'version': acuerdo.VERSION, 'accepted_at': '2026-07-30T10:00:00+00:00',
        'ip': '187.190.1.2', 'user_agent': 'Mozilla/5.0',
        'documento_hash': acuerdo.hash_documento(),
    })
    assert copia.startswith('<!doctype html>')
    for dato in ('María', 'maria@x.com', '187.190.1.2', '2026-07-30T10:00:00+00:00',
                 acuerdo.hash_documento(), '93 Bis', 'Research Use Only'):
        assert dato in copia
    # Autocontenida: se abre igual dentro de diez años, sin red y sin scripts.
    assert 'http://' not in copia and 'https://' not in copia
    assert '<script' not in copia


def test_la_copia_se_entrega_aunque_no_haya_firmado():
    """Leer lo que se le pide firmar no puede depender de haberlo firmado."""
    copia = acuerdo.copia_imprimible(None)
    assert 'Research Use Only' in copia
    assert 'Acta de aceptación' not in copia


def test_el_acta_escapa_lo_que_venga_de_fuera():
    """El user-agent lo escribe el navegador: es entrada del exterior."""
    copia = acuerdo.copia_imprimible({'name': '<img onerror=x>', 'user_agent': '<script>'})
    assert '<img onerror' not in copia and '<script>' not in copia


def test_el_nombre_del_archivo_trae_la_version():
    nombre = acuerdo.nombre_de_archivo()
    assert acuerdo.VERSION in nombre and nombre.endswith('.html')


# ======================================================================
#  6. A QUIÉN APLICA
# ======================================================================
@pytest.mark.parametrize('rol,aplica', [
    ('distributor', True), ('admin', False), ('user', False), ('marketing', False),
])
def test_solo_al_canal_de_distribucion(rol, aplica):
    assert acuerdo.es_distribuidor({'id': 'x', 'role': rol}) is aplica


def test_sin_usuario_no_aplica():
    assert acuerdo.es_distribuidor(None) is False
    assert acuerdo.es_distribuidor({}) is False


def test_sin_sesion_el_texto_se_lee_igual(encendido, db):
    """La pantalla de ACTIVACIÓN enseña el acuerdo antes de que exista sesión:
    nadie firma a ciegas. Pero sin sesión no sale ninguna aceptación."""
    estado = corre(acuerdo.estado_para(db, None))
    assert 'Research Use Only' in estado['html']
    assert estado['aceptacion'] is None
    assert estado['requiere_aceptacion'] is False


# ======================================================================
#  7. EL CANDADO ESTÁ COLGADO DE LAS RUTAS
# ======================================================================
# Que la función exista y funcione no sirve de nada si server.py no la llama.
# Esto se leyó del código fuente a propósito: es lo único que detecta que
# alguien borre un `await _exigir_acuerdo(dist)` en una refactorización.
import server  # noqa: E402  (después de fijar MONGO_URL)


@pytest.mark.parametrize('ruta', [
    'list_discount_codes',        # generar códigos
    'rotate_discount_codes',      # rotarlos
    'distributor_quote_caps',     # cotizar
])
def test_las_tres_acciones_llevan_candado(ruta):
    fuente = inspect.getsource(getattr(server, ruta))
    assert '_exigir_acuerdo' in fuente, f'{ruta} se quedó sin el candado del acuerdo'


def test_la_creacion_de_pedido_filtra_las_comisiones():
    fuente = inspect.getsource(server.create_order)
    assert 'filtrar_comisiones_sin_acuerdo' in fuente


def test_la_activacion_exige_la_casilla():
    fuente = inspect.getsource(server.activate_account)
    assert 'acepta_acuerdo' in fuente and 'acuerdo.registrar' in fuente


def test_firmar_es_escritura_y_el_ver_como_no_escribe():
    """El "ver como" del admin es SOLO LECTURA. Éste es el endpoint donde más
    caro saldría el descuido: firmar un contrato en nombre de otro."""
    fuente = inspect.getsource(server.aceptar_acuerdo)
    assert 'deny_view_as' in fuente


def test_el_expediente_completo_es_solo_del_admin():
    fuente = inspect.getsource(server.listar_aceptaciones)
    assert 'get_current_admin' in fuente


def test_la_copia_exige_sesion():
    """401 sin token: la copia lleva datos personales (IP incluida)."""
    fuente = inspect.getsource(server.descargar_acuerdo)
    assert 'get_current_user' in fuente
