"""LAS RESEÑAS DE GOOGLE — y sobre todo, que nunca tumben la portada.

Christián, 2026-08-05. Lo que estas pruebas cuidan no es que traiga reseñas, sino
que el día que Google no conteste la portada siga en pie y sin un hueco feo.
"""
import resenas


class _Resp:
    def __init__(self, cuerpo, status=200, text=''):
        self._c = cuerpo
        self.status_code = status
        self.text = text

    def json(self):
        return self._c


def _limpiar_cache():
    resenas._CACHE.update({'cuando': 0.0, 'datos': None})


def _config(monkeypatch, llave='k', place='ChIJxxxx'):
    monkeypatch.setattr(resenas, '_config', lambda: (llave, place))


CUERPO = {
    'rating': 4.8,
    'userRatingCount': 37,
    'reviews': [
        {'rating': 5,
         'originalText': {'text': 'Llegó rapidísimo y bien empacado.'},
         'relativePublishTimeDescription': 'hace 2 semanas',
         'googleMapsUri': 'https://maps.google.com/r/1',
         'authorAttribution': {'displayName': 'Ana P.', 'photoUri': 'https://foto/1',
                               'uri': 'https://perfil/1'}},
        # Sin texto: una estrella suelta no es un testimonio, no se enseña.
        {'rating': 5, 'authorAttribution': {'displayName': 'Mudo'}},
    ],
}


# =============================================================================
#  ⛔ NUNCA TUMBA LA PORTADA — lo que más importa
# =============================================================================
def test_sin_llaves_no_hay_seccion_y_no_truena(monkeypatch):
    _limpiar_cache()
    _config(monkeypatch, llave='', place='')
    assert resenas.traer() == {'resenas': [], 'promedio': 0, 'cuantas': 0}
    assert resenas.enabled() is False


def test_si_google_no_contesta_devuelve_vacio_sin_reventar(monkeypatch):
    _limpiar_cache()
    _config(monkeypatch)

    def _revienta(*a, **k):
        raise TimeoutError('Google no contesta')

    monkeypatch.setattr(resenas.requests, 'get', _revienta)
    assert resenas.traer()['resenas'] == []


def test_si_google_falla_se_queda_lo_ULTIMO_BUENO(monkeypatch):
    """Que Google tenga una mala tarde no tiene por qué vaciarle los testimonios a
    la portada: se sigue enseñando lo que ya se había traído."""
    _limpiar_cache()
    _config(monkeypatch)
    monkeypatch.setattr(resenas.requests, 'get', lambda *a, **k: _Resp(CUERPO))
    bueno = resenas.traer()
    assert len(bueno['resenas']) == 1

    monkeypatch.setattr(resenas.requests, 'get',
                        lambda *a, **k: _Resp({}, status=500, text='boom'))
    assert resenas.traer(forzar=True)['resenas'] == bueno['resenas']


# =============================================================================
#  Lo que sí trae
# =============================================================================
def test_trae_autor_foto_y_liga_como_exige_google(monkeypatch):
    """Los términos de Google piden crédito al autor y liga a la reseña: si no
    viajan esos campos, no se pueden pintar y estaríamos incumpliendo."""
    _limpiar_cache()
    _config(monkeypatch)
    monkeypatch.setattr(resenas.requests, 'get', lambda *a, **k: _Resp(CUERPO))
    r = resenas.traer()['resenas'][0]
    assert r['autor'] == 'Ana P.' and r['foto'] and r['liga']
    assert r['estrellas'] == 5 and 'rapidísimo' in r['texto']


def test_una_estrella_SIN_texto_no_se_enseña(monkeypatch):
    _limpiar_cache()
    _config(monkeypatch)
    monkeypatch.setattr(resenas.requests, 'get', lambda *a, **k: _Resp(CUERPO))
    assert len(resenas.traer()['resenas']) == 1       # la segunda venía muda


def test_el_promedio_y_el_total_son_los_de_google(monkeypatch):
    _limpiar_cache()
    _config(monkeypatch)
    monkeypatch.setattr(resenas.requests, 'get', lambda *a, **k: _Resp(CUERPO))
    d = resenas.traer()
    assert d['promedio'] == 4.8 and d['cuantas'] == 37


# =============================================================================
#  El caché, que es lo que hace que esto no cueste
# =============================================================================
def test_no_se_le_pregunta_a_google_en_cada_visita(monkeypatch):
    """⛔ La portada la ve cualquiera: una llamada por visita sería pagarle a Google
    por cada curioso, y atar la portada a que su API conteste rápido."""
    _limpiar_cache()
    _config(monkeypatch)
    llamadas = []
    monkeypatch.setattr(resenas.requests, 'get',
                        lambda *a, **k: llamadas.append(1) or _Resp(CUERPO))
    for _ in range(25):
        resenas.traer()
    assert len(llamadas) == 1, f'le pregunto a Google {len(llamadas)} veces'


def test_solo_se_le_piden_a_google_los_campos_que_se_usan(monkeypatch):
    """Google cobra según los campos que pidas: traer de más es pagar de más."""
    _limpiar_cache()
    _config(monkeypatch)
    visto = {}

    def _get(url, headers=None, timeout=None):
        visto.update(headers or {})
        return _Resp(CUERPO)

    monkeypatch.setattr(resenas.requests, 'get', _get)
    resenas.traer()
    assert visto['X-Goog-FieldMask'] == resenas.CAMPOS
    assert 'photos' not in resenas.CAMPOS and 'editorialSummary' not in resenas.CAMPOS
