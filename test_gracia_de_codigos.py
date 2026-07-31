"""ROTAR NO PUEDE DEJAR SIN DESCUENTO A QUIEN YA TRAE EL CÓDIGO EN LA MANO.

⛔ PREGUNTA DE CHRISTIÁN (2026-07-31), justo antes de rotarle los códigos a los tres
distribuidores para quitarles el nombre del texto: «¿los códigos que María ya repartió
siguen funcionando?». La respuesta de entonces era NO, y nadie lo había notado:
`_ensure_distributor_codes(force_rotate=True)` reescribía el texto DENTRO del mismo
documento, así que en el instante de la rotación el código que andaba circulando dejaba
de existir. El cliente lo tecleaba en el carrito y le decía «Codigo no valido», sin
aviso previo y sin manera de recuperarlo.

Su orden fue explícita: **no mates los viejos**. Lo que se prueba aquí es esa promesa,
en las dos familias de códigos —que son dos y viven en colecciones distintas, que es
justo por lo que la mitad del problema pasaba desapercibida—:

  · los AUTO (`discount_codes`), uno por nivel de descuento: el viejo se JUBILA
    (`superseded_at`) conservando SU caducidad y sigue cobrando;
  · el ÚNICO legacy (`users.distributor_code`), que sólo cabe uno por ficha: se MUDA a
    `discount_codes` como jubilado y recién entonces la ficha estrena texto nuevo.

Y las dos condiciones que hacen que la gracia sirva de algo: que el viejo dé EL MISMO
descuento y que atribuya AL MISMO distribuidor. Un código que sobrevive pero deja de
pagarle comisión a quien lo repartió no es un periodo de gracia, es una fuga silenciosa.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


# ==========================================================================
#  Doble de la base — lo justo que tocan estas rutas
# ==========================================================================
def _match(doc, filtro):
    for k, v in (filtro or {}).items():
        if isinstance(v, dict):
            if '$ne' in v and doc.get(k) == v['$ne']:
                return False
            if '$in' in v and doc.get(k) not in v['$in']:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class _Res:
    def __init__(self, n):
        self.matched_count = self.modified_count = n
        self.inserted_id = n


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, n=None):
        return [dict(d) for d in self._docs[:n]] if n else [dict(d) for d in self._docs]


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
        return _Res(1)

    async def update_one(self, filtro, cambio, upsert=False):
        for d in self.docs:
            if _match(d, filtro):
                d.update(cambio.get('$set') or {})
                return _Res(1)
        return _Res(0)


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, nombre):
        return self.cols.setdefault(nombre, FakeCol())

    def __getattr__(self, nombre):
        return self[nombre]


# La distribuidora del caso real, con su código viejo circulando.
MARIA = {'id': 'u-maria', 'name': 'Maria Neunfeld', 'email': 'maria@exygenlabs.com',
         'role': 'distributor', 'tier': 'junior0', 'commission_rate': 0.30,
         # La marca de SU ficha: sólo ella emite con el prefijo de la casa. Los
         # demás distribuidores siguen emitiendo con su nombre (Christián,
         # 2026-07-31). La gracia de abajo funciona igual con o sin marca — lo que
         # se prueba aquí es que el viejo no muere, no cómo se llama el nuevo.
         'code_prefix': 'MONICAF',
         'distributor_code': 'MARI-3537', 'customer_discount_rate': 0.10}

# El que ya anda en la calle: el que dos clientes usaron el 30-jul.
VIEJO = {'id': 'c-viejo', 'distributor_id': 'u-maria', 'code': 'MARIAN-15-R4YV',
         'discount_rate': 0.15, 'active': True, 'created_at': '2026-07-01T00:00:00',
         'expires_at': '2026-09-29T00:00:00'}


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    fake.cols['users'] = FakeCol([MARIA])
    fake.cols['discount_codes'] = FakeCol([VIEJO])
    monkeypatch.setattr(server, 'db', fake)
    return fake


def codigos(db):
    return db.cols['discount_codes'].docs


def rota(dist=None):
    return asyncio.run(server._ensure_distributor_codes(dist or MARIA, force_rotate=True))


# ==========================================================================
#  1. LOS AUTO: el viejo se jubila, no se muere
# ==========================================================================
def test_al_rotar_el_codigo_viejo_sigue_vivo(db):
    rota()
    viejo = [c for c in codigos(db) if c['code'] == 'MARIAN-15-R4YV']
    assert len(viejo) == 1, 'el texto viejo se perdió: lo reescribieron encima'
    assert viejo[0]['active'] is True
    assert viejo[0]['superseded_at'], 'quedó vivo pero sin marcar como jubilado'


def test_el_viejo_conserva_su_caducidad_original(db):
    """La gracia no se inventa: es lo que le quedaba de vida. Si la rotación le
    estirara la fecha, el código con el nombre pegado viviría 90 días MÁS cada vez
    que alguien pulsa el botón, y no se apagaría nunca."""
    rota()
    viejo = next(c for c in codigos(db) if c['code'] == 'MARIAN-15-R4YV')
    assert viejo['expires_at'] == VIEJO['expires_at']


def test_nace_uno_nuevo_del_mismo_nivel_y_con_el_prefijo_de_su_ficha(db):
    vigentes = rota()
    nuevo = next(c for c in vigentes if round(c['discount_rate'], 4) == 0.15)
    assert nuevo['code'] != 'MARIAN-15-R4YV'
    assert nuevo['code'].startswith('MONICAF-15-')     # ella SÍ trae `code_prefix`
    assert nuevo.get('superseded_at') is None


def test_a_quien_no_trae_marca_le_nacen_con_SU_nombre(db):
    """La corrección del 31-jul: el prefijo de la casa NO es de todos.

    Alanís no trae `code_prefix`, así que rotarle los códigos le devuelve
    `ALANIS-15-XXXX`. Si alguien vuelve a hacer la regla global —la primera
    versión lo hizo y hubo que deshacerlo a mano en la base— esto truena."""
    alanis = {'id': 'u-alanis', 'name': 'Alanis Fernanda Mendoza',
              'email': 'alanis@x.mx', 'role': 'distributor', 'tier': 'junior0',
              'commission_rate': 0.30, 'distributor_code': 'ALAN-2292',
              'customer_discount_rate': 0.10}
    db.cols['users'].docs.append(alanis)
    vigentes = rota(alanis)
    assert vigentes and all(c['code'].startswith('ALANIS-') for c in vigentes), \
        [c['code'] for c in vigentes]
    viejo, nuevo = asyncio.run(server._rotar_codigo_unico(alanis))
    assert viejo == 'ALAN-2292' and nuevo.startswith('ALAN-'), nuevo


def test_el_viejo_da_EL_MISMO_descuento_y_al_MISMO_distribuidor(db):
    """La condición que hace que la gracia sirva: el cliente paga lo mismo y la
    comisión le sigue llegando a quien repartió el código."""
    rota()
    dist, tasa = asyncio.run(server._resolve_code('MARIAN-15-R4YV'))
    assert dist and dist['id'] == 'u-maria'
    assert tasa == 0.15


def test_el_jubilado_no_sale_entre_los_que_hay_que_repartir(db):
    """Vigentes y previos van en listas distintas a propósito: el distribuidor tiene
    que saber cuál seguir dando y cuál nada más va a ver llegar."""
    vigentes = rota()
    assert 'MARIAN-15-R4YV' not in [c['code'] for c in vigentes]
    previos = asyncio.run(server._codigos_jubilados('u-maria'))
    assert [c['code'] for c in previos] == ['MARIAN-15-R4YV']


def test_una_segunda_lectura_no_apaga_la_gracia(db):
    """El barrido que desactiva los niveles que ya no aplican tiene que saltarse a los
    jubilados: el legacy del 10% no está en ningún escalón, así que sin la salvedad la
    primera visita al panel apagaba la gracia recién concedida."""
    rota()
    asyncio.run(server._ensure_distributor_codes(MARIA))     # el panel, sin rotar
    viejo = next(c for c in codigos(db) if c['code'] == 'MARIAN-15-R4YV')
    assert viejo['active'] is True


def test_rotar_dos_veces_no_pisa_al_primer_jubilado(db):
    rota()
    primero = next(c['code'] for c in codigos(db)
                   if round(c['discount_rate'], 4) == 0.15 and not c.get('superseded_at'))
    rota()
    vivos = {c['code'] for c in codigos(db) if c['active']}
    assert 'MARIAN-15-R4YV' in vivos and primero in vivos


# ==========================================================================
#  2. EL ÚNICO LEGACY: cabe uno solo en la ficha, así que se muda
# ==========================================================================
def test_el_codigo_unico_viejo_sigue_cobrando_despues_de_rotar(db):
    viejo, nuevo = asyncio.run(server._rotar_codigo_unico(MARIA))
    assert viejo == 'MARI-3537' and nuevo.startswith('MONICAF-')
    assert db.cols['users'].docs[0]['distributor_code'] == nuevo
    dist, tasa = asyncio.run(server._resolve_code('MARI-3537'))
    assert dist and dist['id'] == 'u-maria'
    assert tasa == 0.10, 'el legacy tiene que seguir dando SU descuento de siempre'


def test_el_enlace_de_referido_viejo_tampoco_se_rompe(db):
    """`?ref=MARI-3537` en un registro: antes sólo miraba `users.distributor_code`, o
    sea que al mudarlo el cliente entraba huérfano y la venta sin comisión."""
    asyncio.run(server._rotar_codigo_unico(MARIA))
    for codigo in ('MARI-3537', db.cols['users'].docs[0]['distributor_code']):
        dist = asyncio.run(server.resolve_distributor(codigo))
        assert dist and dist['id'] == 'u-maria', codigo


def test_el_legacy_jubilado_caduca_solo(db):
    asyncio.run(server._rotar_codigo_unico(MARIA))
    doc = next(c for c in codigos(db) if c['code'] == 'MARI-3537')
    assert doc['superseded_at'] and doc['expires_at'] > doc['created_at']
    assert doc['active'] is True


# ==========================================================================
#  3. GUARDIA DE CÓDIGO: que nadie vuelva a reescribir el texto encima
# ==========================================================================
def test_rotar_nunca_reescribe_el_texto_de_un_codigo_vivo(db):
    """Tosco a propósito, como el resto de los guardias de esta casa: se cuentan los
    textos ANTES y DESPUÉS. Si alguno desapareció, alguien volvió al patrón viejo de
    escribir el código nuevo encima del que estaba circulando."""
    antes = {c['code'] for c in codigos(db) if c['active']}
    rota()
    despues = {c['code'] for c in codigos(db) if c['active']}
    assert antes <= despues, f'se perdieron textos vivos: {sorted(antes - despues)}'


# ==========================================================================
#  4. JUBILAR DE VERDAD: los ocho MONICAF de Alanís y Javier
# ==========================================================================
#  Christián, 2026-07-31: «Jubila los códigos MONICAF de Javier y Alanís.»
#  Ojo con la palabra: hasta hoy «jubilado» en esta casa quería decir «ya no se
#  reparte pero SIGUE COBRANDO» (`superseded_at` a solas). Lo que él pidió ahora
#  es que DEJEN DE SERVIR, y eso son las dos marcas juntas. Lo que se prueba aquí
#  es la diferencia entre las dos cosas, que es justo donde se puede confundir
#  quien venga después.
JUBILADO = {'id': 'c-monicaf', 'distributor_id': 'u-alanis',
            'code': 'MONICAF-15-UTNG', 'discount_rate': 0.15,
            'active': False, 'superseded_at': '2026-07-31T23:00:00',
            'retired_at': '2026-07-31T23:00:00',
            'created_at': '2026-07-31T20:38:29', 'expires_at': '2026-10-29T20:38:29'}

SUYO = {'id': 'c-alanis15', 'distributor_id': 'u-alanis', 'code': 'ALANIS-15-MBET',
        'discount_rate': 0.15, 'active': True, 'superseded_at': None,
        'created_at': '2026-07-23T17:55:53', 'expires_at': '2026-10-21T17:55:53'}

ALANIS = {'id': 'u-alanis', 'name': 'Alanis Fernanda Mendoza',
          'email': 'alexfermc@hotmail.com', 'role': 'distributor', 'tier': 'junior0',
          'commission_rate': 0.30, 'distributor_code': 'ALAN-2292',
          'customer_discount_rate': 0.10}


@pytest.fixture()
def db_alanis(monkeypatch):
    fake = FakeDB()
    fake.cols['users'] = FakeCol([ALANIS])
    fake.cols['discount_codes'] = FakeCol([SUYO, JUBILADO])
    monkeypatch.setattr(server, 'db', fake)
    return fake


def test_un_codigo_jubilado_deja_de_dar_descuento(db_alanis):
    """La prueba de que la orden se cumplió: el checkout resuelve por
    `_resolve_code`, y `_resolve_code` sólo mira los activos."""
    dist, tasa = asyncio.run(server._resolve_code('MONICAF-15-UTNG'))
    assert dist is None and tasa == 0.0


def test_jubilar_uno_no_toca_el_que_lleva_su_nombre(db_alanis):
    dist, tasa = asyncio.run(server._resolve_code('ALANIS-15-MBET'))
    assert dist and dist['id'] == 'u-alanis' and tasa == 0.15


def test_un_codigo_jubilado_no_resucita_al_leer_sus_codigos(db_alanis):
    """⛔ EL CANDADO QUE JUSTIFICA PONER `superseded_at` ADEMÁS DE `active: False`.

    `_ensure_distributor_codes` REESCRIBE EN SU SITIO los documentos muertos de un
    nivel que sí aplica —«no hay gracia que preservar»— y los devuelve con texto
    nuevo y `active: True`. El 15% es un nivel vigente de Alanís, así que un
    `MONICAF-15-UTNG` apagado sólo con `active: False` volvía a la vida en la
    primera lectura de `/distributor/codes`, con otro texto y el mismo renglón.
    Con la marca de jubilado puesta, ese barrido lo salta."""
    asyncio.run(server._ensure_distributor_codes(ALANIS))
    doc = next(c for c in codigos(db_alanis) if c['id'] == 'c-monicaf')
    assert doc['code'] == 'MONICAF-15-UTNG', 'le reescribieron el texto encima'
    assert doc['active'] is False, 'revivió'


def test_el_jubilado_de_verdad_no_sale_como_uno_que_todavia_cobra(db_alanis):
    """`_codigos_jubilados` es la lista de «ya no se reparte pero SÍ cobra». Éste ya
    no cobra: enseñárselo al distribuidor ahí sería decirle que sirve."""
    previos = asyncio.run(server._codigos_jubilados('u-alanis'))
    assert 'MONICAF-15-UTNG' not in [c['code'] for c in previos]


def test_la_lista_de_jubilar_no_toca_ni_un_codigo_de_maria():
    """El permiso es por TEXTO EXACTO, no `startswith('MONICAF-')`. María pidió el
    prefijo de la casa y los suyos son de verdad: un barrido por prefijo le habría
    apagado los cuatro."""
    import jubilar_codigos_monicaf as j
    de_maria = {'MONICAF-7451', 'MONICAF-15-Q5QK', 'MONICAF-20-GY5G',
                'MONICAF-25-0ZA7', 'MONICAF-30-IMGI'}
    todos = {c for lista in j.A_JUBILAR.values() for c in lista}
    assert len(todos) == 8, todos
    assert not (todos & de_maria)
    assert 'marianeunfeld0@gmail.com' not in j.A_JUBILAR
    # Y tampoco los que llevan su nombre: ésos son los que se reparten.
    assert not any(c.startswith(('ALANIS-', 'JAVIER-', 'ALAN-', 'JAVI-', 'MARIAN-', 'MARI-'))
                   for c in todos)
