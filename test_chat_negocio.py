"""EL CHAT IA DE NEGOCIO — la regla de oro, probada.

⛔ REGLA DE ORO (Christián, 2026-07-30): costos reales, proveedores, márgenes y
ROI son territorio EXCLUSIVO del admin. Un distribuidor no los ve JAMÁS.

Estas pruebas no miran el diseño ni la redacción del asistente: miran LA PUERTA y
EL SOBRE, igual que `test_cotizador.py`.

  · La puerta: sin sesión 401; con sesión de cliente 403; en modo "ver como" 403
    (solo lectura); un distribuidor sólo ve SU conversación.
  · El sobre: se lee el CONTEXTO ENTERO que se le manda al modelo —el system
    prompt de verdad, no una función auxiliar que llamé aparte— y truena si en el
    de un distribuidor aparece un costo, un proveedor o un margen. El truco es
    que el doble de `stream_reply` DEVUELVE el system prompt: lo que se prueba es
    exactamente lo que habría viajado a Gemini.
  · Y el revés: en el del admin esos datos SÍ tienen que estar, o el chat no le
    sirve para lo único que él necesita.
"""
import os
import re

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import pytest
from fastapi.testclient import TestClient

import auth
import chat_negocio
import pyramid
import server


RUTA = '/api/business/chat'

PRODUCTOS = [
    {'id': 'p-reta', 'sku': 'RETA-20MG', 'name': 'Retatrutida 20 mg', 'price': 3000,
     'category': 'metabolicos', 'stock': 10, 'commission_cap': 0.40,
     'distributor_eligible': True},
    {'id': 'p-agua', 'sku': 'AGUA-30ML', 'name': 'Agua bacteriostatica 30 mL', 'price': 500,
     'category': 'accesorios', 'stock': 40, 'commission_cap': 0.40,
     'distributor_eligible': True},
    {'id': 'p-oculto', 'sku': 'DYS-500', 'name': 'Dysport 500 U', 'price': 9000,
     'category': 'estetica', 'stock': 0, 'commission_cap': 0.40,
     'distributor_eligible': True, 'hidden': True},
]

# Lo que la Mac sube al Panel: costo por vial y a quién le compramos. Es EL dato
# que nunca puede cruzar al lado del distribuidor.
PROVEEDORES = {
    'clave': 'proveedores_por_producto',
    'valor': {
        'generado': '2026-07-30T10:00:00-05:00',
        'por_producto': {
            'p-reta': {'nombre': 'Retatrutida 20 mg', 'proveedor': 'Kiki Peptides',
                       'telefono': '+8613800000000', 'costo_vial_usd': 12.5,
                       'viales_por_caja': 10, 'cuantos_lo_venden': 3},
            'RETA-20MG': {'nombre': 'Retatrutida 20 mg', 'proveedor': 'Kiki Peptides',
                          'telefono': '+8613800000000', 'costo_vial_usd': 12.5,
                          'viales_por_caja': 10, 'cuantos_lo_venden': 3},
        },
    },
}
MOTOR = {
    'clave': 'motor_precios',
    'valor': {'generado': '2026-07-30 10:00', 'productos': 75, 'a_la_venta': 70,
              'al_filo': {'abajo_del_piso': 4, 'piso_roi': 5},
              'pagando_de_mas': {'de_mas_usd_total': 830},
              'semaforo': {'ok': True, 'problemas': []}},
}

ADMIN = {'id': 'u-admin', 'name': 'Christian', 'email': 'admin@x.mx', 'role': 'admin'}
DIST = {'id': 'u-dist', 'name': 'Dist', 'email': 'dist@x.mx',
        'role': 'distributor', 'tier': 'junior0'}
OTRO_DIST = {'id': 'u-otro', 'name': 'Otro', 'email': 'otro@x.mx',
             'role': 'distributor', 'tier': 'senior'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}
# Admin espiando a un distribuidor: el token trae la marca `view_as`.
ESPIANDO = {**DIST, 'view_as': True, 'view_as_admin': 'u-admin'}


# --------------------------------------------------------- base de datos falsa
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return [dict(d) for d in self._docs]


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, filtro=None, *a, **k):
        filtro = filtro or {}
        return _Cursor([d for d in self.docs
                        if all(d.get(k2) == v for k2, v in filtro.items())])

    async def find_one(self, filtro=None, *a, **k):
        filtro = filtro or {}
        for d in self.docs:
            if all(d.get(k2) == v for k2, v in filtro.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return None

    async def update_one(self, *a, **k):
        return None


class _FakeDB:
    def __init__(self, con_costos=True):
        self._colls = {
            'products': _Coll(PRODUCTOS),
            'app_data': _Coll([PROVEEDORES, MOTOR] if con_costos else []),
            'business_chat_messages': _Coll(),
        }

    def __getattr__(self, name):
        return self._colls.setdefault(name, _Coll())


@pytest.fixture
def como(monkeypatch):
    """`como(usuario)` = cliente HTTP autenticado. `como(None)` es un visitante.

    Y `stream_reply` se sustituye por un doble que DEVUELVE EL SYSTEM PROMPT: la
    respuesta HTTP es, literalmente, el contexto que habría viajado al modelo. Así
    las pruebas del sobre miran lo que de verdad se manda, no una reconstrucción.
    """
    fake = _FakeDB()
    monkeypatch.setattr(server, 'db', fake)
    monkeypatch.setattr(chat_negocio, 'db', fake, raising=False)

    async def _doble(chat, mensaje):
        yield chat['system_message']

    monkeypatch.setattr(server, 'stream_reply', _doble)

    def _factory(user):
        if user is None:
            server.app.dependency_overrides.clear()
        else:
            server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        cliente = TestClient(server.app)
        cliente.db = fake
        return cliente

    yield _factory
    server.app.dependency_overrides.clear()


def _preguntar(cliente, texto='¿cuánto gano con 20% de descuento?', sesion='s-1'):
    return cliente.post(RUTA, json={'session_id': sesion, 'message': texto})


def _contexto(cliente, **kw):
    r = _preguntar(cliente, **kw)
    assert r.status_code == 200, r.text
    return r.text.lower()


# ------------------------------------------------------------------- la puerta
def test_sin_sesion_no_pasa(como):
    assert _preguntar(como(None)).status_code == 401


def test_un_cliente_no_pasa(como):
    """El chat de negocio NO es el chat público: un cliente no entra aquí."""
    assert _preguntar(como(CLIENTE)).status_code == 403


def test_el_distribuidor_si_pasa(como):
    assert _preguntar(como(DIST)).status_code == 200


def test_el_admin_si_pasa(como):
    assert _preguntar(como(ADMIN)).status_code == 200


def test_ver_como_es_solo_lectura(como):
    """Espiar un panel no puede gastar la cuota ni escribir en la conversación de
    otro. El "ver como" del admin se corta aquí, como en el resto del sistema."""
    assert _preguntar(como(ESPIANDO)).status_code == 403


def test_el_historial_es_solo_suyo(como):
    """Adivinar el id de sesión de otro no abre su chat: se filtra por `user_id`."""
    cliente = como(DIST)
    _preguntar(cliente, sesion='s-compartida')
    assert cliente.get('/api/business/history/s-compartida').json()

    server.app.dependency_overrides[auth.get_current_user] = lambda: dict(OTRO_DIST)
    ajeno = TestClient(server.app)
    assert ajeno.get('/api/business/history/s-compartida').json() == []


def test_el_historial_tambien_exige_distribuidor(como):
    assert como(CLIENTE).get('/api/business/history/s-1').status_code == 403
    assert como(None).get('/api/business/history/s-1').status_code == 401


# -------------------------------------------------------------------- el sobre
# ⛔ LO QUE JAMÁS PUEDE CRUZAR. Palabras completas (\b): el catálogo trae textos
# de verdad y "esteroidogénesis" contiene "roi" — sin el \b la prueba sería puro
# ruido, y a la tercera falsa alarma alguien la apaga, que es justo cuando deja
# de proteger.
#
# El barrido cubre el contexto ENTERO, instrucciones incluidas. Por eso el bloque
# que le prohíbe al modelo hablar de esto está redactado sin usar estas palabras
# (ver `CANDADO_DISTRIBUIDOR`): exceptuar un pedazo obligaría a la prueba a saber
# dónde empieza y dónde acaba, y esa es la clase de excepción por la que un día se
# cuela un dato de verdad.
PROHIBIDO = ('costo', 'costos', 'cost', 'proveedor', 'proveedores', 'provider',
             'supplier', 'roi', 'margen', 'margenes', 'margin', 'usd',
             'kiki', 'telefono', 'whatsapp')


def test_al_distribuidor_no_le_llega_ni_un_costo(como):
    """Se lee el CONTEXTO ENTERO como texto plano. Tosco a propósito: no depende
    de que nadie mantenga una lista de campos permitidos."""
    ctx = _contexto(como(DIST))
    for palabra in PROHIBIDO:
        assert not re.search(rf'\b{palabra}\b', ctx), \
            f'el contexto del distribuidor trae "{palabra}"'


def test_al_distribuidor_no_le_llega_el_numero_del_costo(como):
    """Ni la palabra ni el número: 12.5 USD/vial es el costo de la Retatrutida."""
    ctx = _contexto(como(DIST))
    assert '12.5' not in ctx and 'kiki' not in ctx


def test_el_bloque_de_costos_ni_siquiera_se_arma_para_un_distribuidor(como, monkeypatch):
    """El candado es un `if`, no una frase en el prompt. Si `bloque_costos` llegara
    a llamarse con un distribuidor, esto truena — aunque el texto saliera limpio
    por casualidad."""
    llamadas = []
    original = chat_negocio.bloque_costos
    monkeypatch.setattr(chat_negocio, 'bloque_costos',
                        lambda *a, **k: (llamadas.append(1), original(*a, **k))[1])
    _contexto(como(DIST))
    assert llamadas == [], 'se armó el bloque de costos para un distribuidor'
    _contexto(como(ADMIN))
    assert llamadas == [1], 'el admin sí debe recibir el bloque de costos'


def test_al_admin_si_le_llegan_los_costos_y_el_proveedor(como):
    """El revés de la moneda: sin esto el chat no le sirve para lo suyo."""
    ctx = _contexto(como(ADMIN))
    assert 'kiki peptides' in ctx
    assert '12.5' in ctx
    assert 'motor de precios' in ctx


# ---------------------------------------------------- los números de cada quien
def test_el_distribuidor_recibe_su_tasa_y_su_tope(como):
    """La tasa sale de la pirámide, no de un número escrito a mano en el prompt:
    si mañana cambia la base del canal, el chat cambia con ella."""
    ctx = _contexto(como(DIST))
    tasa = round(pyramid.effective_rate(DIST) * 100)
    tope = round(max(pyramid.discount_tiers_de(DIST)) * 100)
    assert f'{tasa}%' in ctx and f'{tope}%' in ctx
    assert 'tus numeros' in ctx


def test_cada_quien_ve_SU_tasa(como):
    """Un senior con 30% y un junior con 30% empatan hoy por la base del canal;
    lo que se prueba es que el número sale del usuario que pregunta."""
    de_uno = _contexto(como(DIST))
    de_otro = _contexto(como(OTRO_DIST))
    assert 'tus numeros' in de_uno and 'tus numeros' in de_otro


def test_el_catalogo_lleva_precio_publico_y_tope_por_producto(como):
    ctx = _contexto(como(DIST))
    assert 'retatrutida 20 mg' in ctx
    assert '$3,000' in ctx
    # El insumo va con tope 0: el agua bacteriostática nunca lleva descuento.
    assert 'agua bacteriostatica 30 ml: $500 mxn · descuento maximo aqui: 0%' in ctx


def test_lo_oculto_no_se_ofrece(como):
    assert 'dysport' not in _contexto(como(DIST))


def test_el_tope_es_el_mismo_que_el_del_checkout(como):
    """El tope que ve el chat sale de `tope_de_descuento`, LA MISMA función del
    checkout y del cotizador. Si aquí saliera más alto, el asesor prometería un
    descuento que la caja no respeta."""
    ctx = _contexto(como(DIST))
    tope = min(server.tope_de_descuento(PRODUCTOS[0]),
               max(pyramid.discount_tiers_de(DIST)))
    assert f'retatrutida 20 mg: $3,000 mxn · descuento maximo aqui: {round(tope * 100)}%' in ctx


# ------------------------------------------------------- las reglas de la casa
def test_las_reglas_vigentes_viajan_en_el_contexto(como):
    ctx = _contexto(como(DIST))
    assert 'regla de 5' in ctx
    assert '$2,500' in ctx          # envío gratis
    assert '30%' in ctx             # comisión base del canal
    assert 'ruo' in ctx or 'investigacion' in ctx


# ------------------------------------------------- cuando se acaba la cuota
def test_sin_cuota_no_truena_avisa(como, monkeypatch):
    """Con la cuota agotada (Gemini gratis: 20/día) el chat degrada con un mensaje
    claro, no con un error técnico ni con un 500."""
    async def _revienta(chat, mensaje):
        raise RuntimeError('429 RESOURCE_EXHAUSTED')
        yield ''                      # pragma: no cover - hace de esto un generador

    monkeypatch.setattr(server, 'stream_reply', _revienta)
    r = _preguntar(como(DIST))
    assert r.status_code == 200
    assert 'cuota' in r.text.lower()


def test_sin_llave_tampoco_truena(como, monkeypatch):
    async def _sin_llave(chat, mensaje):
        raise RuntimeError('GEMINI_API_KEY is not configured.')
        yield ''                      # pragma: no cover

    monkeypatch.setattr(server, 'stream_reply', _sin_llave)
    r = _preguntar(como(DIST))
    assert r.status_code == 200
    assert 'llave' in r.text.lower()


def test_la_conversacion_se_guarda_con_su_dueno(como):
    cliente = como(DIST)
    _preguntar(cliente, sesion='s-guardar')
    guardados = cliente.db.business_chat_messages.docs
    assert [m['role'] for m in guardados] == ['user', 'assistant']
    assert all(m['user_id'] == DIST['id'] for m in guardados)
