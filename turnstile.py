"""TURNSTILE — el escudo antibots del checkout (Cloudflare).

Christián, 2026-08-05, después de los pedidos de broma: «entrale ahora pues».

QUÉ ES. Un recuadro invisible de Cloudflare que el navegador resuelve solo. El
cliente no ve un CAPTCHA ni tiene que buscar semáforos en fotos: en el 99% de los
casos no se entera de que existe. El navegador consigue un `token`, lo manda con el
pedido, y aquí se le pregunta a Cloudflare si ese token es de verdad.

⛔⛔ FALLA ABIERTO, SIEMPRE. Es la decisión más importante de este archivo.

Si Cloudflare tarda, se cae, o la llave no está configurada, **la venta pasa**. Un
escudo que se cae y se lleva el checkout con él cuesta infinitamente más que los
bots que deja entrar: la regla madre de la casa es VENDER SIEMPRE
(`exygen-vender-siempre-envio-partido`). El día que Cloudflare tenga una mala tarde,
Exygen sigue cobrando.

⛔ Y UN TOKEN MALO NO RECHAZA LA COMPRA: LA MARCA. Un token que no valida es
evidencia fuerte de bot, pero no es prueba — una red intermitente, una pestaña
abierta media hora o un bloqueador agresivo también lo rompen, y ésos son clientes.
Así que un fallo se traduce en SEÑALES de `basura.py`, que es el mismo camino de
siempre: no suena la campanita, no lo persigue la oferta y caduca solo a las 24 h si
nadie paga. Si era real y paga, todo sigue su curso.

Esto es a propósito y no es tibieza: contra el que «le pica a lo estúpido», apagarle
el ruido logra exactamente lo mismo que rechazarlo, y sin arriesgar una venta.
"""
import logging

import requests

logger = logging.getLogger(__name__)

URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

# Corto a propósito: esto corre DENTRO del checkout, con el cliente esperando. Más
# vale dejar pasar un pedido dudoso que hacerle mirar una rueda girando.
TIMEOUT_S = 4

# Cuántas señales de basura vale un token que no pasa. DOS = el umbral completo de
# `basura.py`, o sea que un fallo de Turnstile basta por sí solo para apagarle el
# ruido a ese pedido, sin necesitar que además el nombre o el correo sean raros.
SENALES_SI_FALLA = 2


def _llave() -> str:
    """La llave secreta, del entorno o del panel de Admin. Vacía = apagado."""
    import secretos
    return secretos.valor('TURNSTILE_SECRET_KEY')


def enabled() -> bool:
    """¿Hay con qué validar? Sin llave, todo el sitio se comporta como antes."""
    return bool(_llave())


def verificar(token: str, ip: str = '') -> dict:
    """¿Es de verdad este token? Devuelve `{'ok': bool, 'motivo': str}`.

    `ok=True` cuando Turnstile está apagado o cuando no se pudo preguntar: es el
    fallo abierto del que habla el encabezado. `ok=False` sólo cuando Cloudflare
    contestó y dijo que NO.
    """
    llave = _llave()
    if not llave:
        return {'ok': True, 'motivo': 'apagado'}
    if not (token or '').strip():
        # Con el escudo encendido, un pedido sin token no pasó por el formulario del
        # sitio: o es un guion pegándole a la API, o alguien con JavaScript apagado.
        return {'ok': False, 'motivo': 'sin token'}
    datos = {'secret': llave, 'response': token}
    if ip:
        datos['remoteip'] = ip
    try:
        r = requests.post(URL, data=datos, timeout=TIMEOUT_S)
        cuerpo = r.json()
    except Exception as e:
        # ⛔ AQUÍ VIVE EL FALLO ABIERTO. No se sabe, así que se deja pasar.
        logger.warning('Turnstile no contesto (%s): se deja pasar el pedido.', e)
        return {'ok': True, 'motivo': 'no se pudo preguntar'}
    if cuerpo.get('success'):
        return {'ok': True, 'motivo': ''}
    codigos = cuerpo.get('error-codes') or []
    # Un secreto mal pegado es un error NUESTRO de configuración, no un bot: sería
    # absurdo castigar a todos los clientes por eso. Se deja pasar y se grita en la
    # bitácora para que se arregle.
    if any(c in ('invalid-input-secret', 'missing-input-secret') for c in codigos):
        logger.error('Turnstile: la llave secreta esta mal (%s). Se deja pasar.', codigos)
        return {'ok': True, 'motivo': 'llave mal configurada'}
    return {'ok': False, 'motivo': ', '.join(codigos) or 'rechazado'}
