"""Pruebas de las credenciales de pasarelas que se pegan desde el Admin.

Lo que importa: que el valor NUNCA salga hacia el navegador, que el .env mande
siempre sobre lo que haya en la base, y que no se pueda guardar cualquier cosa.
"""
import asyncio
import importlib

import pytest


@pytest.fixture()
def sec(monkeypatch):
    monkeypatch.setenv('JWT_SECRET', 'secreto-de-prueba')
    for k in ('MERCADOPAGO_ACCESS_TOKEN', 'MERCADOPAGO_WEBHOOK_SECRET'):
        monkeypatch.delenv(k, raising=False)
    import secretos
    m = importlib.reload(secretos)
    m._CACHE.clear()
    return m


class FakeColeccion:
    def __init__(self):
        self.docs = {}

    async def update_one(self, filtro, cambio, upsert=False):
        self.docs[filtro['nombre']] = dict(cambio['$set'])

    async def delete_one(self, filtro):
        self.docs.pop(filtro['nombre'], None)

    async def find_one(self, filtro, proj=None):
        return self.docs.get(filtro['nombre'])

    def find(self, filtro=None, proj=None):
        docs = list(self.docs.values())

        class Cursor:
            def __aiter__(self):
                async def gen():
                    for d in docs:
                        yield d
                return gen()
        return Cursor()


class FakeDB:
    def __init__(self):
        self.col = FakeColeccion()

    def __getitem__(self, nombre):
        return self.col


# ------------------------------------------------------------------- cifrado

def test_el_valor_no_se_guarda_en_claro(sec):
    blob = sec.cifrar('APP_USR-secreto-123')
    assert 'APP_USR' not in blob
    assert sec.descifrar(blob) == 'APP_USR-secreto-123'


def test_con_otra_llave_no_se_descifra(sec, monkeypatch):
    blob = sec.cifrar('APP_USR-secreto-123')
    monkeypatch.setenv('JWT_SECRET', 'otra-llave')
    import secretos
    otro = importlib.reload(secretos)
    assert otro.descifrar(blob) is None


def test_la_pista_solo_deja_ver_cuatro(sec):
    p = sec.pista('APP_USR-1234567890abcd')
    assert p.endswith('abcd')
    assert 'APP_USR' not in p
    assert '1234567890' not in p


# ------------------------------------------------------- guardar y recuperar

def test_guarda_y_lee(sec):
    async def _cuerpo():
        db = FakeDB()
        assert await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', 'APP_USR-abc')
        assert await sec.leer(db, 'MERCADOPAGO_ACCESS_TOKEN') == 'APP_USR-abc'
    asyncio.run(_cuerpo())


def test_una_llave_no_permitida_se_rechaza(sec):
    async def _cuerpo():
        db = FakeDB()
        assert await sec.guardar(db, 'AWS_SECRET_ACCESS_KEY', 'x') is False
        assert db.col.docs == {}
    asyncio.run(_cuerpo())


def test_mandar_vacio_borra(sec):
    async def _cuerpo():
        db = FakeDB()
        await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', 'APP_USR-abc')
        await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', '')
        assert await sec.leer(db, 'MERCADOPAGO_ACCESS_TOKEN') is None
    asyncio.run(_cuerpo())


def test_el_entorno_manda_sobre_la_base(sec, monkeypatch):
    async def _cuerpo():
        db = FakeDB()
        await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', 'del-panel')
        monkeypatch.setenv('MERCADOPAGO_ACCESS_TOKEN', 'del-servidor')
        assert await sec.leer(db, 'MERCADOPAGO_ACCESS_TOKEN') == 'del-servidor'
    asyncio.run(_cuerpo())


# ------------------------------------------------------------------- cache

def test_el_cache_se_llena_y_lo_lee_valor(sec):
    async def _cuerpo():
        db = FakeDB()
        await sec.guardar(db, 'MERCADOPAGO_WEBHOOK_SECRET', 'firma-123')
        assert sec.valor('MERCADOPAGO_WEBHOOK_SECRET') == ''      # todavia sin recargar
        assert await sec.recargar(db) == 1
        assert sec.valor('MERCADOPAGO_WEBHOOK_SECRET') == 'firma-123'
    asyncio.run(_cuerpo())


def test_el_entorno_manda_tambien_en_el_cache(sec, monkeypatch):
    async def _cuerpo():
        db = FakeDB()
        await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', 'del-panel')
        await sec.recargar(db)
        monkeypatch.setenv('MERCADOPAGO_ACCESS_TOKEN', 'del-servidor')
        assert sec.valor('MERCADOPAGO_ACCESS_TOKEN') == 'del-servidor'
    asyncio.run(_cuerpo())


# ------------------------------------------------------------------- estado

def test_el_estado_nunca_trae_el_valor(sec):
    async def _cuerpo():
        db = FakeDB()
        await sec.guardar(db, 'MERCADOPAGO_ACCESS_TOKEN', 'APP_USR-supersecreto')
        filas = await sec.estado(db)
        plano = str(filas)
        assert 'supersecreto' not in plano
        assert 'APP_USR' not in plano
        fila = next(f for f in filas if f['nombre'] == 'MERCADOPAGO_ACCESS_TOKEN')
        assert fila['configurado'] is True
        assert fila['origen'] == 'panel'
        assert fila['editable'] is True
    asyncio.run(_cuerpo())


def test_lo_que_viene_del_servidor_no_es_editable(sec, monkeypatch):
    async def _cuerpo():
        monkeypatch.setenv('MERCADOPAGO_ACCESS_TOKEN', 'APP_USR-del-env')
        filas = await sec.estado(FakeDB())
        fila = next(f for f in filas if f['nombre'] == 'MERCADOPAGO_ACCESS_TOKEN')
        assert fila['origen'] == 'servidor'
        assert fila['editable'] is False
        assert 'del-env' not in str(filas)
    asyncio.run(_cuerpo())
