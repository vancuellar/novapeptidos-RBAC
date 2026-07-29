"""Verificación del ID token de Microsoft (Outlook / cuentas Microsoft).

El mismo diseño que google_auth.py, calcado a propósito:
  1. El sitio manda al navegador a login.microsoftonline.com con nuestro
     CLIENT ID (público, no es secreto) pidiendo un *ID token*.
  2. Microsoft regresa al navegador con el token firmado.
  3. El navegador nos lo manda; aquí se verifica la firma contra las llaves
     públicas de Microsoft y se comprueban emisor, audiencia y expiración.
  4. Si todo cuadra, se confía en el correo que viene dentro.

No se necesita client secret ni guardar nada de Microsoft. Si
MICROSOFT_CLIENT_ID no está configurado, el endpoint queda apagado y el
botón no se muestra en el sitio: nada de botones muertos.
"""

import os
import time

import httpx
from jose import jwt
from jose.exceptions import JWTError

MICROSOFT_CLIENT_ID = os.environ.get('MICROSOFT_CLIENT_ID', '').strip()
# El tenant 'common' acepta cuentas personales (outlook.com, hotmail.com,
# live.com) Y cuentas de trabajo/escuela. Las llaves y el emisor cambian por
# tenant, por eso el emisor se valida por FORMA y no contra una lista fija.
CERTS_URL = 'https://login.microsoftonline.com/common/discovery/v2.0/keys'
ISSUER_PREFIX = 'https://login.microsoftonline.com/'
ISSUER_SUFFIX = '/v2.0'

# Las llaves públicas de Microsoft rotan; se cachean unas horas para no
# pedirlas en cada inicio de sesión.
_certs_cache = {'keys': None, 'fetched_at': 0}
_CERTS_TTL = 3600


def microsoft_enabled() -> bool:
    return bool(MICROSOFT_CLIENT_ID)


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


async def verify_microsoft_token(credential: str) -> dict:
    """Devuelve los datos del usuario si el token es válido. Lanza ValueError si no.

    Se comprueba: firma con las llaves públicas de Microsoft, `aud` = nuestro
    client id, `iss` con la forma de Microsoft (y el tenant del propio token),
    expiración, y que venga un correo utilizable.
    """
    if not microsoft_enabled():
        raise ValueError('El inicio de sesión con Outlook no está configurado en el servidor.')
    if not credential:
        raise ValueError('Falta la credencial de Microsoft.')

    certs = await _get_certs()
    try:
        claims = jwt.decode(
            credential,
            certs,
            algorithms=['RS256'],
            audience=MICROSOFT_CLIENT_ID,
            options={'verify_at_hash': False},
        )
    except JWTError as exc:
        raise ValueError('La credencial de Microsoft no es válida.') from exc

    iss = claims.get('iss', '')
    tid = claims.get('tid', '')
    if not (iss.startswith(ISSUER_PREFIX) and iss.endswith(ISSUER_SUFFIX) and tid and tid in iss):
        raise ValueError('La credencial no viene de Microsoft.')

    # Las cuentas personales traen el correo en `email`; algunas de
    # trabajo/escuela solo en `preferred_username` (que ahí es el correo).
    email = (claims.get('email') or '').strip()
    if not email:
        candidato = (claims.get('preferred_username') or '').strip()
        if '@' in candidato:
            email = candidato
    if not email:
        raise ValueError('La cuenta de Microsoft no expone un correo.')

    return {
        'email': email.lower(),
        'name': claims.get('name') or email.split('@')[0],
        'microsoft_sub': claims.get('sub', ''),
    }
