"""EL ESCUDO ANTIBOTS — y sobre todo, que NUNCA se lleve una venta por delante.

Christián, 2026-08-05. La regla madre de la casa es VENDER SIEMPRE, así que estas
pruebas se preocupan más por lo que el escudo DEJA PASAR que por lo que detiene: un
bot que se cuela cuesta un renglón feo; un checkout caído cuesta el día entero.
"""
import turnstile


class _Respuesta:
    def __init__(self, cuerpo):
        self._cuerpo = cuerpo

    def json(self):
        return self._cuerpo


def _llave(monkeypatch, valor='secreta'):
    monkeypatch.setattr(turnstile, '_llave', lambda: valor)


# =============================================================================
#  ⛔ FALLA ABIERTO — lo que más importa
# =============================================================================
def test_sin_llave_configurada_todo_pasa(monkeypatch):
    """Mientras Christián no pegue la llave, el sitio se comporta EXACTAMENTE
    como antes. Encenderlo es pegar una llave, no desplegar otra vez."""
    _llave(monkeypatch, '')
    assert turnstile.verificar('lo-que-sea')['ok'] is True
    assert turnstile.enabled() is False


def test_si_cloudflare_no_contesta_LA_VENTA_PASA(monkeypatch):
    """⛔ EL CANDADO DE LA REGLA MADRE. Un escudo que se cae no puede llevarse el
    checkout con él: el día que Cloudflare tenga una mala tarde, Exygen cobra."""
    _llave(monkeypatch)

    def _revienta(*a, **k):
        raise TimeoutError('Cloudflare no contesta')

    monkeypatch.setattr(turnstile.requests, 'post', _revienta)
    r = turnstile.verificar('token-cualquiera')
    assert r['ok'] is True
    assert 'no se pudo preguntar' in r['motivo']


def test_una_llave_mal_pegada_NO_castiga_a_los_clientes(monkeypatch):
    """Un secreto mal copiado es error NUESTRO. Sería absurdo tirar todas las ventas
    por eso: se deja pasar y se grita en la bitácora."""
    _llave(monkeypatch)
    monkeypatch.setattr(turnstile.requests, 'post', lambda *a, **k: _Respuesta(
        {'success': False, 'error-codes': ['invalid-input-secret']}))
    assert turnstile.verificar('token')['ok'] is True


def test_una_respuesta_ilegible_tambien_deja_pasar(monkeypatch):
    _llave(monkeypatch)

    class _Basura:
        def json(self):
            raise ValueError('esto no es JSON')

    monkeypatch.setattr(turnstile.requests, 'post', lambda *a, **k: _Basura())
    assert turnstile.verificar('token')['ok'] is True


# =============================================================================
#  Y lo que sí detiene
# =============================================================================
def test_un_token_bueno_pasa(monkeypatch):
    _llave(monkeypatch)
    monkeypatch.setattr(turnstile.requests, 'post',
                        lambda *a, **k: _Respuesta({'success': True}))
    assert turnstile.verificar('token-bueno')['ok'] is True


def test_un_token_rechazado_por_cloudflare_NO_pasa(monkeypatch):
    _llave(monkeypatch)
    monkeypatch.setattr(turnstile.requests, 'post', lambda *a, **k: _Respuesta(
        {'success': False, 'error-codes': ['invalid-input-response']}))
    r = turnstile.verificar('token-falso')
    assert r['ok'] is False
    assert 'invalid-input-response' in r['motivo']


def test_con_el_escudo_encendido_un_pedido_SIN_token_no_paso_por_el_sitio(monkeypatch):
    """O es un guion pegándole a la API, o alguien con JavaScript apagado."""
    _llave(monkeypatch)
    r = turnstile.verificar('')
    assert r['ok'] is False and r['motivo'] == 'sin token'


def test_la_ip_del_cliente_viaja_a_cloudflare(monkeypatch):
    """Cloudflare afina su veredicto con la IP. Se le manda cuando se tiene."""
    _llave(monkeypatch)
    visto = {}

    def _post(url, data=None, timeout=None):
        visto.update(data or {})
        return _Respuesta({'success': True})

    monkeypatch.setattr(turnstile.requests, 'post', _post)
    turnstile.verificar('token', ip='189.203.1.1')
    assert visto['remoteip'] == '189.203.1.1'
    assert visto['secret'] == 'secreta'


def test_un_fallo_vale_el_umbral_COMPLETO_de_basura():
    """Un token que no valida basta por sí solo para apagarle el ruido al pedido,
    sin necesitar que además el nombre o el correo sean raros."""
    import basura
    assert turnstile.SENALES_SI_FALLA >= basura.MINIMO_SENALES


def test_el_tiempo_de_espera_es_corto():
    """Corre DENTRO del checkout, con el cliente esperando."""
    assert turnstile.TIMEOUT_S <= 5
