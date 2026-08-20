"""ESCONDER UN PRODUCTO ES UN DATO, NO UN DESPLIEGUE.

Christián, 2026-08-05: «esconde todos los otros compuestos de los que no tenemos
stock».

El backend ya respetaba `hidden` en todo lo suyo —la lista pública lo filtra, la
ficha da 404, el checkout lo rechaza—, pero EL SITIO no lee de aquí su catálogo: lo
lee de `fallbackCatalog.js`, que viaja dentro del bundle. Por eso, hasta hoy,
esconder algo obligaba a borrarlo A MANO de ese archivo y volver a desplegar (así se
escondieron los 17 anteriores). Con `/api/catalogo/ocultos` el sitio pregunta y
filtra, y esconder vuelve a ser lo que debe ser: un dato.
"""
import asyncio
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import server


class _Cursor:
    def __init__(self, filas):
        self.filas = filas

    async def to_list(self, length=None):
        return self.filas[:length]


class _Coleccion:
    def __init__(self, filas):
        self.filas = filas

    def find(self, filtro=None, proyeccion=None):
        f = filtro or {}
        if f.get('hidden') is True:
            return _Cursor([d for d in self.filas if d.get('hidden')])
        return _Cursor(self.filas)


class _Explota:
    def find(self, *a, **k):
        raise RuntimeError('mongo se cayo')


FILAS = [
    {'id': 'i1', 'sku': 'SKU-ESCONDIDO', 'slug': 'escondido', 'hidden': True},
    {'id': 'i2', 'sku': 'SKU-VISIBLE', 'slug': 'visible'},
    {'id': 'i3', 'sku': 'OTRO-ESCONDIDO', 'slug': 'otro', 'hidden': True},
]


def _con(coleccion, monkeypatch):
    class _Db:
        products = coleccion
    monkeypatch.setattr(server, 'db', _Db())


def test_los_ocultos_viajan_por_sus_TRES_nombres(monkeypatch):
    """sku, slug e id: cada pantalla del sitio tiene a la mano uno distinto, y si
    falta el suyo el producto se le cuela pintado."""
    _con(_Coleccion(FILAS), monkeypatch)
    r = asyncio.run(server.catalogo_ocultos())
    assert r['skus'] == ['OTRO-ESCONDIDO', 'SKU-ESCONDIDO']
    assert r['slugs'] == ['escondido', 'otro']
    assert r['ids'] == ['i1', 'i3']


def test_lo_visible_NO_aparece_en_la_lista(monkeypatch):
    _con(_Coleccion(FILAS), monkeypatch)
    r = asyncio.run(server.catalogo_ocultos())
    assert 'SKU-VISIBLE' not in r['skus'] and 'visible' not in r['slugs']


def test_si_la_consulta_truena_el_sitio_ve_el_catalogo_COMPLETO(monkeypatch):
    """⛔ FALLA ABIERTO. Mejor enseñar de más un rato que dejar la tienda vacía por
    un error nuestro. Es la misma regla de vender siempre."""
    _con(_Explota(), monkeypatch)
    assert asyncio.run(server.catalogo_ocultos()) == {'skus': [], 'slugs': [], 'ids': []}


def test_sin_ningun_oculto_contesta_vacio_y_no_esconde_nada(monkeypatch):
    _con(_Coleccion([{'id': 'i2', 'sku': 'SKU-VISIBLE', 'slug': 'visible'}]), monkeypatch)
    assert asyncio.run(server.catalogo_ocultos()) == {'skus': [], 'slugs': [], 'ids': []}
