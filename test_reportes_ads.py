"""El archivo de reportes semanales de publicidad, y su candado.

Dos cosas se prueban aquí:

1. **El archivo funciona**: se publica una semana, se lista de la más nueva a la
   más vieja, las cifras se conservan, la retención avisa pero NO borra, y una
   semana con nombre inventado no puede salirse de la carpeta.

2. **EL CANDADO** (lo que importa de verdad): el video del reporte trae gasto de
   publicidad, ventas y embudo. Sin sesión no se baja. Con sesión de cliente
   tampoco. Con sesión de distribuidor tampoco. Ni por la ruta de listar ni por
   la del MP4, que es la que lleva el token en la URL y por eso da más miedo.

No toca Mongo ni la red: la base se sustituye por una falsa y la carpeta del
archivo apunta a un directorio temporal.
"""
import json
import os

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')
os.environ.pop('META_TOKEN', None)
os.environ.pop('META_AD_ACCOUNT', None)

import pytest
from datetime import date
from fastapi.testclient import TestClient

import auth
import reportes_ads
import server


# ------------------------------------------------------------ usuarios de prueba
ADMIN = {'id': 'u-admin', 'name': 'Christián', 'email': 'admin@exygenlabs.com', 'role': 'admin'}
MARIA = {'id': 'u-maria', 'name': 'María', 'email': 'maria@x.mx', 'role': 'distributor',
         'extra_roles': ['marketing']}
DIST = {'id': 'u-dist', 'name': 'Dist', 'email': 'dist@x.mx', 'role': 'distributor'}
CLIENTE = {'id': 'u-cli', 'name': 'Cliente', 'email': 'cli@x.mx', 'role': 'user'}

TODOS = {u['id']: u for u in (ADMIN, MARIA, DIST, CLIENTE)}


class _Cursor:
    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def to_list(self, *a, **k):
        return []


class _Users:
    def find(self, *a, **k):
        return _Cursor()

    async def find_one(self, filtro=None, *a, **k):
        return dict(TODOS.get((filtro or {}).get('id'), {})) or None


class _Coll(_Users):
    async def find_one(self, *a, **k):
        return None

    async def count_documents(self, *a, **k):
        return 0

    async def update_one(self, *a, **k):
        return None

    def aggregate(self, *a, **k):
        return _Cursor()


class _FakeDB:
    def __getattr__(self, name):
        return _Users() if name == 'users' else _Coll()


CIFRAS_W31 = {
    'gasto_usd': 237.09, 'impresiones': 76666, 'clics': 2338,
    'conversaciones_wa': 110, 'costo_conversacion_usd': 2.16,
    'compras_atribuidas': 0, 'visitas': 1362, 'fichas': 118, 'compras_sitio': 5,
}


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    """Carpeta temporal como archivo de reportes."""
    monkeypatch.setenv('REPORTES_ADS_DIR', str(tmp_path / 'reportes-ads'))
    return tmp_path / 'reportes-ads'


@pytest.fixture
def como(monkeypatch, archivo):
    """`como(usuario)` -> cliente HTTP autenticado como ese usuario."""
    monkeypatch.setattr(server, 'db', _FakeDB())

    def _factory(user):
        server.app.dependency_overrides[auth.get_current_user] = lambda: dict(user)
        return TestClient(server.app)

    yield _factory
    server.app.dependency_overrides.clear()


@pytest.fixture
def sin_sesion(monkeypatch, archivo):
    """Cliente HTTP SIN ninguna identidad inyectada: la puerta de verdad."""
    monkeypatch.setattr(server, 'db', _FakeDB())
    server.app.dependency_overrides.clear()
    yield TestClient(server.app)
    server.app.dependency_overrides.clear()


def _publica_w31(video=b'MP4-de-mentiras'):
    return reportes_ads.publicar(
        '2026-W31',
        {'desde': '2026-07-25', 'hasta': '2026-07-31', 'duracion_seg': 279.1,
         'titulo': 'Semana del 25 al 31 de julio 2026',
         'resumen': '$237 en anuncios, 110 conversaciones de WhatsApp y cero compras atribuidas.',
         'cifras': CIFRAS_W31},
        video=video, texto='# Reporte\nTexto de prueba.')


# ============================================================ el archivo en sí
def test_la_semana_iso_sale_de_la_fecha():
    assert reportes_ads.semana_de(date(2026, 7, 31)) == '2026-W31'
    assert reportes_ads.semana_de('2026-07-25') == '2026-W30'
    # Enero de un año que arranca a media semana: el año ISO puede no ser el del día.
    assert reportes_ads.semana_de(date(2027, 1, 1)) == '2026-W53'


def test_publicar_y_leer_una_semana(archivo):
    d = _publica_w31()
    assert d['semana'] == '2026-W31'
    assert d['tiene_video'] and d['tiene_texto']
    assert d['tamano_bytes'] == len(b'MP4-de-mentiras')
    assert d['cifras']['conversaciones_wa'] == 110
    assert d['duracion_seg'] == 279.1
    assert reportes_ads.texto_de('2026-W31').startswith('# Reporte')


def test_el_video_queda_en_su_carpeta_por_año_y_semana(archivo):
    _publica_w31()
    assert (archivo / '2026' / '2026-W31' / 'video.mp4').is_file()
    assert (archivo / '2026' / '2026-W31' / 'datos.json').is_file()


def test_se_listan_de_la_mas_nueva_a_la_mas_vieja(archivo):
    for s in ('2026-W29', '2026-W31', '2026-W30'):
        reportes_ads.publicar(s, {'cifras': {'gasto_usd': 1}}, video=b'x')
    assert [r['semana'] for r in reportes_ads.listar()] == ['2026-W31', '2026-W30', '2026-W29']


def test_una_cifra_que_falta_es_nula_no_cero(archivo):
    reportes_ads.publicar('2026-W30', {'cifras': {'gasto_usd': 10}}, video=b'x')
    c = reportes_ads.uno('2026-W30')['cifras']
    assert c['gasto_usd'] == 10
    # Cero se leería como "no hubo conversaciones"; nulo es "no se midió".
    assert c['conversaciones_wa'] is None


def test_publicar_dos_veces_la_misma_semana_reemplaza(archivo):
    _publica_w31()
    reportes_ads.publicar('2026-W31', {'cifras': {'gasto_usd': 99}}, video=b'nuevo')
    assert len(reportes_ads.listar()) == 1
    assert reportes_ads.uno('2026-W31')['cifras']['gasto_usd'] == 99


def test_una_semana_inventada_no_se_sale_de_la_carpeta(archivo):
    for basura in ('../../etc', '2026-W31/../..', 'W31', '', None, '2026-31'):
        assert reportes_ads.carpeta(basura) is None
        assert reportes_ads.ruta_video(basura) is None
        with pytest.raises(ValueError):
            reportes_ads.publicar(basura, {})


def test_un_reporte_sin_video_sigue_contando(archivo):
    """Lo que se compara son las cifras: una semana sin video no se pierde."""
    reportes_ads.publicar('2026-W28', {'cifras': CIFRAS_W31})
    d = reportes_ads.uno('2026-W28')
    assert d['tiene_video'] is False
    assert d['cifras']['gasto_usd'] == 237.09


# ---------------------------------------------------------------- retención
def test_la_retencion_por_omision_es_un_año(archivo):
    assert reportes_ads.retencion() == {'semanas': 52, 'borrado_automatico': False}


def test_la_retencion_se_puede_cambiar(archivo):
    assert reportes_ads.guardar_retencion(12)['semanas'] == 12
    assert reportes_ads.retencion()['semanas'] == 12


def test_retencion_fuera_de_rango_se_rechaza(archivo):
    for v in (0, -1, 999, 'muchas'):
        with pytest.raises(ValueError):
            reportes_ads.guardar_retencion(v)


def test_lo_que_se_pasa_de_la_retencion_se_avisa_pero_NO_se_borra(archivo):
    reportes_ads.guardar_retencion(2)
    for s in ('2026-W28', '2026-W29', '2026-W30', '2026-W31'):
        reportes_ads.publicar(s, {'cifras': {'gasto_usd': 1}}, video=b'x')
    a = reportes_ads.almacen()
    assert a['semanas'] == 4
    assert a['por_vencer'] == ['2026-W29', '2026-W28']
    # ⛔ Lo importante: siguen ahí. Nadie borra solo.
    assert reportes_ads.ruta_video('2026-W28') is not None


def test_borrar_es_manual_y_explicito(archivo):
    _publica_w31()
    assert reportes_ads.borrar('2026-W31') is True
    assert reportes_ads.listar() == []
    assert reportes_ads.borrar('2026-W31') is False


def test_el_almacen_proyecta_lo_que_ocupara_un_año(archivo):
    reportes_ads.publicar('2026-W31', {}, video=b'x' * 1000)
    assert reportes_ads.almacen()['proyeccion_anual_bytes'] == 52000


# ======================================================= EL CANDADO (la puerta)
def test_sin_sesion_no_se_lista_el_archivo(sin_sesion, archivo):
    _publica_w31()
    assert sin_sesion.get('/api/admin/marketing/reportes').status_code in (401, 403)


def test_sin_sesion_NO_se_baja_el_video(sin_sesion, archivo):
    _publica_w31()
    # Sin token: la ruta ni siquiera se arma.
    assert sin_sesion.get('/api/admin/marketing/reportes/2026-W31/video').status_code == 422
    # Con un token inventado: 401.
    r = sin_sesion.get('/api/admin/marketing/reportes/2026-W31/video?token=basura')
    assert r.status_code == 401


def test_un_cliente_NO_se_baja_el_video(sin_sesion, archivo):
    """El caso que da miedo: el token viaja en la URL, así que un cliente con
    sesión válida podría pegarle a mano. El candado es el ROL, no el token."""
    _publica_w31()
    token = auth.create_token(CLIENTE['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W31/video?token={token}')
    assert r.status_code == 403


def test_un_distribuidor_NO_se_baja_el_video(sin_sesion, archivo):
    _publica_w31()
    token = auth.create_token(DIST['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W31/video?token={token}')
    assert r.status_code == 403


def test_cliente_y_distribuidor_tampoco_listan_ni_leen_el_texto(como, archivo):
    _publica_w31()
    for quien in (CLIENTE, DIST):
        c = como(quien)
        assert c.get('/api/admin/marketing/reportes').status_code == 403
        assert c.get('/api/admin/marketing/reportes/2026-W31/texto').status_code == 403


def test_cliente_y_distribuidor_no_pueden_publicar_ni_borrar(como, archivo):
    _publica_w31()
    for quien in (CLIENTE, DIST, MARIA):   # María lleva difusión, pero no escribe aquí
        c = como(quien)
        assert c.post('/api/admin/marketing/reportes',
                      data={'semana': '2026-W32', 'datos': '{}'}).status_code == 403
        assert c.delete('/api/admin/marketing/reportes/2026-W31').status_code == 403
        assert c.put('/api/admin/marketing/reportes/retencion',
                     json={'semanas': 1}).status_code == 403


# ------------------------------------------------- quien SÍ puede: admin y difusión
def test_el_admin_ve_el_archivo_completo(como, archivo):
    _publica_w31()
    r = como(ADMIN).get('/api/admin/marketing/reportes')
    assert r.status_code == 200
    d = r.json()
    assert d['reportes'][0]['semana'] == '2026-W31'
    assert d['reportes'][0]['cifras']['gasto_usd'] == 237.09
    assert d['retencion']['semanas'] == 52
    assert d['almacen']['bytes'] > 0


def test_maria_difusion_tambien_ve_el_archivo(como, archivo):
    _publica_w31()
    assert como(MARIA).get('/api/admin/marketing/reportes').status_code == 200
    assert como(MARIA).get('/api/admin/marketing/reportes/2026-W31/texto').status_code == 200


def test_el_admin_si_se_baja_el_video(sin_sesion, archivo):
    _publica_w31()
    token = auth.create_token(ADMIN['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W31/video?token={token}')
    assert r.status_code == 200
    assert r.content == b'MP4-de-mentiras'
    assert r.headers['content-type'] == 'video/mp4'


def test_el_video_soporta_rangos_para_safari(sin_sesion, archivo):
    _publica_w31()
    token = auth.create_token(ADMIN['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W31/video?token={token}',
                       headers={'Range': 'bytes=0-3'})
    assert r.status_code == 206
    assert r.content == b'MP4-'


def test_descargar_manda_el_nombre_del_archivo(sin_sesion, archivo):
    _publica_w31()
    token = auth.create_token(ADMIN['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W31/video?token={token}&descargar=1')
    assert 'Reporte-Publicidad-2026-W31.mp4' in r.headers.get('content-disposition', '')


def test_una_semana_sin_video_da_404_no_500(sin_sesion, archivo):
    token = auth.create_token(ADMIN['id'])
    r = sin_sesion.get(f'/api/admin/marketing/reportes/2026-W99/video?token={token}')
    assert r.status_code == 404


# ------------------------------------------------------ publicar desde el pipeline
def test_el_pipeline_publica_la_semana_completa(como, archivo):
    datos = json.dumps({'desde': '2026-07-25', 'hasta': '2026-07-31',
                        'duracion_seg': 279.1, 'resumen': 'Cero compras atribuidas.',
                        'cifras': CIFRAS_W31})
    r = como(ADMIN).post(
        '/api/admin/marketing/reportes',
        data={'semana': '2026-W31', 'datos': datos, 'texto': '# Reporte\nhola'},
        files={'video': ('video.mp4', b'bytes-del-video', 'video/mp4')})
    assert r.status_code == 200
    d = r.json()['reporte']
    assert d['tiene_video'] and d['tiene_texto']
    assert d['cifras']['clics'] == 2338
    assert reportes_ads.ruta_video('2026-W31').read_bytes() == b'bytes-del-video'


def test_publicar_con_datos_que_no_son_json_se_rechaza(como, archivo):
    r = como(ADMIN).post('/api/admin/marketing/reportes',
                         data={'semana': '2026-W31', 'datos': 'no soy json'})
    assert r.status_code == 400


def test_publicar_una_semana_con_nombre_invalido_se_rechaza(como, archivo):
    r = como(ADMIN).post('/api/admin/marketing/reportes',
                         data={'semana': '../../etc', 'datos': '{}'})
    assert r.status_code == 400


def test_el_admin_cambia_la_retencion_y_no_se_borra_nada(como, archivo):
    _publica_w31()
    c = como(ADMIN)
    assert c.put('/api/admin/marketing/reportes/retencion', json={'semanas': 4}).json()['semanas'] == 4
    assert c.put('/api/admin/marketing/reportes/retencion', json={'semanas': 0}).status_code == 400
    assert reportes_ads.ruta_video('2026-W31') is not None


def test_el_admin_borra_una_semana_a_mano(como, archivo):
    _publica_w31()
    c = como(ADMIN)
    assert c.delete('/api/admin/marketing/reportes/2026-W31').status_code == 200
    assert c.delete('/api/admin/marketing/reportes/2026-W31').status_code == 404


def test_ver_como_no_publica_ni_borra(como, archivo):
    """'Ver como' es de SOLO lectura, también aquí."""
    _publica_w31()
    espia = {**ADMIN, 'view_as': True, 'view_as_admin': 'u-admin'}
    c = como(espia)
    assert c.post('/api/admin/marketing/reportes',
                  data={'semana': '2026-W32', 'datos': '{}'}).status_code == 403
    assert c.delete('/api/admin/marketing/reportes/2026-W31').status_code == 403
    assert c.get('/api/admin/marketing/reportes').status_code == 200
