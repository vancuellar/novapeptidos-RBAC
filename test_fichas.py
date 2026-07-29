"""Pruebas del almacén privado de fichas técnicas.

Lo que importa aquí no es que la descarga funcione, sino que NO funcione cuando
no debe: el slug viene del navegador y el token lo puede manipular cualquiera.
"""
import importlib
import os
import time

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """ficha_store apuntando a una carpeta temporal con dos fichas de mentira."""
    (tmp_path / 'FICHA-TECNICA-BPC-157.pdf').write_bytes(b'%PDF-1.4 bpc')
    (tmp_path / 'FICHA-TECNICA-TB-500.pdf').write_bytes(b'%PDF-1.4 tb')
    (tmp_path / 'secreto.txt').write_text('esto no es una ficha')
    monkeypatch.setenv('FICHA_DIR', str(tmp_path))
    monkeypatch.setenv('JWT_SECRET', 'secreto-de-prueba')
    import ficha_store
    return importlib.reload(ficha_store)


# ------------------------------------------------------------------ descubrir

def test_lista_solo_las_fichas(store):
    assert store.slugs_disponibles() == ['bpc-157', 'tb-500']


def test_existe_lo_que_hay_y_no_lo_que_no(store):
    assert store.existe('bpc-157')
    assert not store.existe('semaglutida')


def test_para_slugs_filtra_lo_que_no_tiene_ficha(store):
    r = store.para_slugs(['bpc-157', 'semaglutida', None, ''])
    assert [x['product_slug'] for x in r] == ['bpc-157']


# ------------------------------------- presentación del catálogo → compuesto
# En la base cada presentación es su propio producto ("retatrutida-40-mg") y la
# ficha es del compuesto ("retatrutida"). Sin recortar el sufijo no emparejaba
# ni uno de los 193 productos del catálogo. Encontrado en vivo el 2026-07-29.

@pytest.fixture()
def store2(tmp_path, monkeypatch):
    """Fichas con nombres que sí llevan número propio, que es donde duele."""
    for n in ('BPC-157', 'THYMOSIN-ALPHA-1', 'SNAP-8', 'NAD-PLUS'):
        (tmp_path / f'FICHA-TECNICA-{n}.pdf').write_bytes(b'%PDF-1.4')
    monkeypatch.setenv('FICHA_DIR', str(tmp_path))
    import ficha_store
    return importlib.reload(ficha_store)


@pytest.mark.parametrize('comprado,ficha', [
    ('bpc-157-10-mg', 'bpc-157'),
    ('bpc-157', 'bpc-157'),              # ya viene limpio
    ('nad-plus-500-mg', 'nad-plus'),
    ('nad-plus-1-5-mg', 'nad-plus'),     # decimal: 1.5 mg
    ('bpc-157-60mg', 'bpc-157'),         # unidad pegada
    # El número es parte del NOMBRE: no se puede recortar de más.
    ('thymosin-alpha-1-5-mg', 'thymosin-alpha-1'),
    ('thymosin-alpha-1-10-mg', 'thymosin-alpha-1'),
    ('snap-8-10-mg', 'snap-8'),
    ('snap-8-100-mg', 'snap-8'),
])
def test_la_presentacion_encuentra_su_compuesto(store2, comprado, ficha):
    assert store2.compuesto_de(comprado) == ficha
    assert [x['product_slug'] for x in store2.para_slugs([comprado])] == [ficha]


@pytest.mark.parametrize('comprado', [
    'pinealon-5-mg',        # compuesto sin ficha: no debe inventar
    'cardiogen-20-mg',
    'bpc',                  # un pedazo del nombre no vale
    'bpc-157-x-mg',
    '', None, 123,
])
def test_no_inventa_una_ficha_que_no_existe(store2, comprado):
    assert store2.compuesto_de(comprado) is None


def test_el_recorte_no_entrega_la_ficha_de_otro(store2):
    """Recortar de más no puede acabar en el compuesto del vecino: se exige
    que el resultado exista en disco."""
    assert store2.compuesto_de('snap-8-10-mg') == 'snap-8'
    assert store2.compuesto_de('snap-10-mg') is None


def test_varias_presentaciones_dan_una_sola_ficha(store2):
    r = store2.para_slugs(['bpc-157-5-mg', 'bpc-157-10-mg', 'bpc-157-20-mg'])
    assert [x['product_slug'] for x in r] == ['bpc-157']


# ------------------------------------------------------- no salirse de la carpeta

@pytest.mark.parametrize('malo', [
    '../../etc/passwd',
    '..',
    '../secreto',
    'bpc-157/../../x',
    'BPC-157',          # mayusculas: el slug siempre va en minuscula
    'bpc 157',
    'bpc_157',
    '/bpc-157',
    '',
    None,
])
def test_slug_malicioso_no_resuelve(store, malo):
    assert not store.existe(malo)
    assert store.ruta_de(malo) is None


def test_no_entrega_un_archivo_que_no_es_ficha(store):
    assert not store.existe('secreto')


# ------------------------------------------------------------ enlaces firmados

def test_enlace_valido_devuelve_su_slug(store):
    assert store.validar_enlace(store.emitir_enlace('bpc-157')) == 'bpc-157'


def test_no_se_emite_enlace_de_algo_que_no_existe(store):
    assert store.emitir_enlace('semaglutida') is None


def test_enlace_caducado_se_rechaza(store):
    assert store.validar_enlace(store.emitir_enlace('bpc-157', horas=-1)) is None


def test_cero_horas_caduca_de_inmediato(store):
    """`horas or ENLACE_HORAS` convertiria el 0 en 48; no debe pasar."""
    assert store.validar_enlace(store.emitir_enlace('bpc-157', horas=0)) is None


def test_firma_alterada_se_rechaza(store):
    t = store.emitir_enlace('bpc-157')
    assert store.validar_enlace(t[:-4] + 'dead') is None


def test_no_se_puede_cambiar_el_slug_del_token(store):
    """Con el token de una ficha no se baja otra."""
    t = store.emitir_enlace('bpc-157')
    assert store.validar_enlace(t.replace('bpc-157', 'tb-500', 1)) is None


def test_no_se_puede_estirar_la_caducidad(store):
    slug, _, firma = store.emitir_enlace('bpc-157').split('.')
    lejano = int(time.time()) + 999_999
    assert store.validar_enlace(f'{slug}.{lejano}.{firma}') is None


@pytest.mark.parametrize('basura', ['', 'a.b.c', 'bpc-157', 'bpc-157.123', 'x' * 400,
                                    'bpc-157.no-es-numero.aaaa', '...'])
def test_tokens_basura_se_rechazan(store, basura):
    assert store.validar_enlace(basura) is None


def test_otro_secreto_invalida_el_token(store, monkeypatch, tmp_path):
    """Un token firmado con otra llave no sirve: la firma va contra JWT_SECRET."""
    t = store.emitir_enlace('bpc-157')
    monkeypatch.setenv('JWT_SECRET', 'llave-distinta')
    monkeypatch.setenv('FICHA_DIR', str(tmp_path))
    import ficha_store
    otro = importlib.reload(ficha_store)
    assert otro.validar_enlace(t) is None


# ------------------------------------------- quién ve el catálogo completo
# Christian, 2026-07-29: el distribuidor vende todo el catálogo y necesita la
# ficha ANTES de que exista el pedido. Lo que no puede pasar es que esa puerta
# se le abra a un cliente normal.

def test_el_distribuidor_ve_todas_las_fichas():
    from server import _ve_el_catalogo_completo
    assert _ve_el_catalogo_completo({'role': 'distributor'})


def test_el_admin_tambien():
    from server import _ve_el_catalogo_completo
    assert _ve_el_catalogo_completo({'role': 'admin'})


@pytest.mark.parametrize('quien', [
    {'role': 'user'},
    {'role': 'client'},
    {'role': ''},
    {'role': None},
    {},
    {'role': 'Distributor'},   # el rol se compara exacto, no sin mayúsculas
    {'role': 'distribuidor'},  # el rol real está en inglés
])
def test_un_cliente_solo_ve_lo_que_compro(quien):
    from server import _ve_el_catalogo_completo
    assert not _ve_el_catalogo_completo(quien)
