"""EL VIGÍA — y sobre todo, que no grite cuando la casa hizo lo que quiso.

El 2026-08-05 le mandó a Christián una alarma FALSA («la tienda está caída») porque
ese día él dejó a la venta sólo las 13 presentaciones que tiene en bodega y escondió
las otras 192, a propósito. La tienda estaba perfecta.

⛔ Una alarma que grita cuando la casa hizo lo que QUISO se aprende a ignorar, y un
vigía al que nadie le hace caso es peor que no tener vigía. Pero recortar el piso a
secas lo habría dejado ciego, así que estas pruebas cuidan las DOS caras: que calle
cuando debe callar y que grite cuando debe gritar.
"""
import importlib.util
import json
import os
import sys

_spec = importlib.util.spec_from_file_location(
    'vigilante_mod', os.path.join(os.path.dirname(__file__), 'vigilante.py'))
vig = importlib.util.module_from_spec(_spec)
sys.modules['vigilante_mod'] = vig
_spec.loader.exec_module(vig)


def _respuestas(monkeypatch, a_la_venta, escondidos, codigo=200):
    """Finge la API: N productos a la venta y M escondidos."""
    def _traer(url, *a, **k):
        if url.endswith('/catalogo/ocultos'):
            return 200, json.dumps({'skus': [f'S{i}' for i in range(escondidos)]}), 5
        prods = [{'sku': f'P{i}'} for i in range(a_la_venta)]
        return codigo, json.dumps({'products': prods}), 5
    monkeypatch.setattr(vig, '_traer', _traer)


def _fallas(monkeypatch, a_la_venta, escondidos, codigo=200):
    _respuestas(monkeypatch, a_la_venta, escondidos, codigo)
    fallas = []
    vig.revisar_catalogo(fallas, {})
    return fallas


# =============================================================================
#  CALLA cuando la casa escondió a propósito
# =============================================================================
def test_el_caso_de_christian_13_a_la_venta_y_192_escondidos_NO_es_alarma(monkeypatch):
    """El 5-ago exacto. La tienda vende, sólo que poco catálogo — decisión suya."""
    assert _fallas(monkeypatch, 13, 192) == []


def test_un_solo_producto_a_la_venta_tampoco_alarma_si_el_resto_esta_escondido(monkeypatch):
    """Si mañana decide dejar UNO, es su tienda. Sigue vendiendo."""
    assert _fallas(monkeypatch, 1, 204) == []


# =============================================================================
#  Y GRITA cuando de verdad hay avería
# =============================================================================
def test_CERO_productos_si_es_la_tienda_caida(monkeypatch):
    f = _fallas(monkeypatch, 0, 205)
    assert f and 'NI UN producto' in f[0]


def test_un_catalogo_TRUNCADO_sigue_gritando(monkeypatch):
    """Que se pierdan 192 sin que nadie los escondiera SÍ es avería: entonces
    tampoco aparecen en la lista de escondidos, y la suma se desploma."""
    f = _fallas(monkeypatch, 13, 0)
    assert f and 'truncado' in f[0]


def test_si_el_catalogo_no_contesta_grita(monkeypatch):
    f = _fallas(monkeypatch, 13, 192, codigo=502)
    assert f and 'no contesta' in f[0]


def test_si_la_lista_de_escondidos_falla_NO_se_inventa_un_numero(monkeypatch):
    """Sin esa lista sólo se sabe lo que está a la venta. Se avisa (truncado) en vez
    de callar: ante la duda, el vigía habla."""
    def _traer(url, *a, **k):
        if url.endswith('/catalogo/ocultos'):
            return 500, 'boom', 5
        return 200, json.dumps({'products': [{'sku': f'P{i}'} for i in range(13)]}), 5
    monkeypatch.setattr(vig, '_traer', _traer)
    fallas = []
    vig.revisar_catalogo(fallas, {})
    assert fallas and 'truncado' in fallas[0]
