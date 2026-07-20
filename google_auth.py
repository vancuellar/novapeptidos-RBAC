"""Verificación del ID token de Google Identity Services.

Cómo funciona el flujo (el más simple y seguro para una tienda):
  1. El navegador muestra el botón de Google con nuestro CLIENT ID (que es
     público, no es un secreto).
  2. Google devuelve al navegador un *ID token* firmado.
  3. El navegador nos manda ese token; aquí se verifica la firma contra las
     llaves públicas de Google y se comprueban emisor, audiencia y expiración.
  4. Si todo cuadra, se confía en el correo que viene dentro.

No se necesita client secret ni guardar nada de Google. Si
GOOGLE_CLIENT_ID no está configurado, el endpoint queda apagado y el botón
no se muestra en el sitio: nada de botones muertos.
"""

import os
import time

import httpx
from jose import jwt
from jose.exceptions import JWTError

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
CERTS_URL = 'https://www.googleapis.com/oauth2/v3/certs'
ISSUERS = ('https://accounts.google.com', 'accounts.google.com')

# Las llaves públicas de Google rotan; se cachean unas horas para no pedirlas
# en cada inicio de sesión.
_certs_cache = {'keys': None, 'fetched_at': 0}
_CERTS_TTL = 3600


def google_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID)


async def _get_certs() -> dict:
    now = time.time()
    if _certs_cache['keys'] and now - _certs_cache['fetched_at'] < _CERTS_TTL:
        return _certs_cache['keys']
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(CERTS_URL)
        resp.raise_for_status()
        keys = resp.json()
    _certs_cache['keys'] = keys
    _certs_cache['fetched_at'] = now
    return keys


async def verify_google_token(credential: str) -> dict:
    """Devuelve los datos del usuario si el token es válido. Lanza ValueError si no.

    Se comprueba: firma con las llaves públicas de Google, `aud` = nuestro
    client id, `iss` = Google, expiración, y que el correo esté verificado.
    """
    if not google_enabled():
        raise ValueError('Google Sign-In no está configurado en el servidor.')
    if not credential:
        raise ValueError('Falta la credencial de Google.')

    certs = await _get_certs()
    try:
        claims = jwt.decode(
            credential,
            certs,
            algorithms=['RS256'],
            audience=GOOGLE_CLIENT_ID,
            options={'verify_at_hash': False},
        )
    except JWTError as exc:
        raise ValueError('La credencial de Google no es válida.') from exc

    if claims.get('iss') not in ISSUERS:
        raise ValueError('La credencial de Google no viene de Google.')
    if not claims.get('email'):
        raise ValueError('La cuenta de Google no expone un correo.')
    # email_verified viene como bool o como cadena según el caso.
    verified = claims.get('email_verified')
    if verified in (False, 'false'):
        raise ValueError('El correo de esa cuenta de Google no está verificado.')

    return {
        'email': claims['email'].lower(),
        'name': claims.get('name') or claims['email'].split('@')[0],
        'google_sub': claims.get('sub', ''),
    }
