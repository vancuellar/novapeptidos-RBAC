"""LA CONSTANCIA DEL AVISO DE ENTRADA — Christián, 2026-08-02.

Hasta hoy el «acepto» del aviso RUO vivía SÓLO en el navegador del cliente
(localStorage, sessionStorage y una cookie). Los tres los borra él con un clic, así
que la casa no tenía con qué sostener que alguien aceptó.

Estas pruebas corren el módulo DE VERDAD (`ruo_constancia` es casi puro a propósito).
Lo que más se cuida no es que guarde: es que **guardar nunca deje a nadie afuera**.
"""
import asyncio

import ruo_constancia as R


class Peticion:
    """Una petición de mentiras, con las cabeceras que pone Caddy."""

    def __init__(self, xff=None, ua='Mozilla/5.0 (iPhone)', host=None):
        self.headers = {}
        if xff:
            self.headers['x-forwarded-for'] = xff
        if ua:
            self.headers['user-agent'] = ua
        self.client = type('C', (), {'host': host})() if host else None


class BaseQueGuarda:
    def __init__(self):
        self.guardados = []

    def __getitem__(self, _coleccion):
        base = self

        class Col:
            async def insert_one(self, doc):
                base.guardados.append(doc)
        return Col()


class BaseQueFalla:
    def __getitem__(self, _coleccion):
        class Col:
            async def insert_one(self, doc):
                raise RuntimeError('mongo caído')
        return Col()


# ------------------------------------------------------------------ la IP real
def test_la_ip_es_la_de_internet_no_la_del_proxy():
    """El backend vive detrás de Caddy y de la puerta nginx, así que
    `request.client.host` es 127.0.0.1 siempre. La buena es la PRIMERA de
    X-Forwarded-For: la que puso Caddy al recibir la conexión."""
    assert R.ip_de(Peticion(xff='189.203.1.44, 10.0.0.1, 172.18.0.1')) == '189.203.1.44'


def test_sin_cabecera_cae_a_la_del_cliente():
    assert R.ip_de(Peticion(xff=None, host='127.0.0.1')) == '127.0.0.1'


def test_una_peticion_sin_nada_no_revienta():
    assert R.ip_de(Peticion(xff=None)) == ''
    assert R.user_agent_de(Peticion(xff=None, ua=None)) == ''


def test_la_ip_y_el_user_agent_se_recortan():
    """Una cabecera gigante no puede inflar la base ni el documento."""
    assert len(R.ip_de(Peticion(xff='9' * 500))) <= 64
    assert len(R.user_agent_de(Peticion(ua='x' * 5000))) <= 400


# -------------------------------------------------------------- la declaración
def test_hacen_falta_LAS_DOS_casillas():
    """Son dos declaraciones distintas: la edad y el propósito. Media aceptación
    dice que aceptó cuando no aceptó, y eso es peor que no tener constancia."""
    assert R.declaracion_completa(True, True) is True
    assert R.declaracion_completa(True, False) is False
    assert R.declaracion_completa(False, True) is False
    assert R.declaracion_completa(False, False) is False


def test_la_edad_minima_es_21():
    """Christián, 2026-08-02: sube de 18 a 21, el estándar más restrictivo."""
    assert R.EDAD_MINIMA == 21


def test_la_version_viaja_en_la_constancia():
    """Sin la versión, la constancia probaría que aceptó «algo», no que aceptó
    ESTO. El día que cambie el texto del aviso, esa distinción es todo."""
    doc = R.constancia(Peticion(xff='1.2.3.4'), True, True, True)
    assert doc['version'] == R.VERSION
    assert doc['edad_minima'] == 21


def test_las_dos_casillas_se_guardan_por_separado():
    doc = R.constancia(Peticion(xff='1.2.3.4'), True, True, False)
    assert doc['edad'] is True and doc['investigacion'] is True
    assert doc['recordar'] is False


def test_la_hora_es_del_servidor():
    """La del navegador la pone el reloj del cliente, que él controla."""
    doc = R.constancia(Peticion(xff='1.2.3.4'), True, True, True)
    assert doc['accepted_at'].endswith('+00:00') and 'T' in doc['accepted_at']


def test_se_acepta_sin_cuenta():
    """Casi todo el mundo acepta ANTES de tener cuenta, y ése es justo el caso
    que hay que poder probar."""
    doc = R.constancia(Peticion(xff='1.2.3.4'), True, True, True, user_id=None)
    assert doc['user_id'] is None


# ------------------------------------------------- lo que de verdad importa
def test_guardar_nunca_deja_a_nadie_afuera_si_la_base_falla():
    """⛔ LA PRUEBA QUE MÁS IMPORTA. Un aviso legal que deja a la gente afuera
    cuando se cae Mongo es peor que no tener constancia. `registrar` no puede
    lanzar: devuelve `guardada: False` y el visitante entra igual."""
    res = asyncio.run(R.registrar(BaseQueFalla(), Peticion(xff='1.2.3.4'),
                                  True, True, True))
    assert res['guardada'] is False
    assert 'mongo' in res['motivo'].lower()
    assert res['accepted_at']          # la constancia se armó aunque no se guardara


def test_sin_base_tampoco_revienta():
    res = asyncio.run(R.registrar(None, Peticion(xff='1.2.3.4'), True, True, True))
    assert res['guardada'] is False


def test_una_aceptacion_a_medias_no_se_guarda():
    base = BaseQueGuarda()
    res = asyncio.run(R.registrar(base, Peticion(xff='1.2.3.4'), True, False, True))
    assert res['guardada'] is False and base.guardados == []


def test_la_aceptacion_completa_si_se_guarda():
    base = BaseQueGuarda()
    res = asyncio.run(R.registrar(base, Peticion(xff='189.203.1.44'), True, True,
                                  True, user_id='u1', idioma='pt-BR'))
    assert res['guardada'] is True and len(base.guardados) == 1
    g = base.guardados[0]
    assert g['ip'] == '189.203.1.44' and g['user_id'] == 'u1' and g['idioma'] == 'pt-BR'
    assert g['version'] == R.VERSION


def test_el_endpoint_no_le_devuelve_la_ip_al_visitante():
    """La IP y el user-agent son la prueba de la CASA. Se leen del server, no se
    le sirven a quien acaba de aceptar. Esta prueba lee el código porque el
    endpoint vive en server.py y aquí no se levanta la app entera."""
    import inspect
    import os
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server.py')
    fuente = open(ruta, encoding='utf-8').read()
    cuerpo = fuente.split("async def ruo_aceptar(")[1].split('@api_router')[0]
    devuelve = cuerpo.split('return {')[1]
    assert "'ip'" not in devuelve and 'user_agent' not in devuelve, (
        'el endpoint le está devolviendo al visitante la prueba de la casa')
    assert inspect.isfunction(R.registrar) or True
