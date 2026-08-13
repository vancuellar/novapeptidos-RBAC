"""Credenciales de pasarelas que Christian puede pegar desde el Admin.

Por qué existe
--------------
Las llaves de Mercado Pago (y las de cualquier otra pasarela) vivían solo en el
`.env` del servidor. Eso obliga a entrar por SSH cada vez que cambian, y
Christian trabaja desde el teléfono. Este módulo permite pegarlas desde el
panel de Admin.

Reglas que no se rompen:

  - **El `.env` manda.** Si la variable está en el entorno, se usa esa y la base
    de datos se ignora. Así un despliegue nunca queda a merced de lo que haya
    en Mongo, y se puede revertir quitando el valor del Admin.
  - **Solo escritura.** El valor NUNCA se devuelve al navegador. El Admin solo
    puede ver si está configurado y los últimos 4 caracteres, para confirmar que
    pegó el correcto sin exponerlo.
  - **Se guarda cifrado** con Fernet, usando una llave derivada del JWT_SECRET.
    No es un HSM: si alguien tiene la base Y el secreto de la app, lo abre. Pero
    evita que un dump de Mongo entregue las llaves de cobro en claro.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

COLECCION = 'gateway_secrets'

# Lo único que se acepta guardar. Una llave que no esté aquí se rechaza, para
# que el endpoint no se convierta en un almacén de cualquier cosa.
PERMITIDAS = {
    'MERCADOPAGO_ACCESS_TOKEN',
    'MERCADOPAGO_WEBHOOK_SECRET',
    'NOWPAYMENTS_API_KEY',
    'NOWPAYMENTS_IPN_SECRET',
    'BTCPAY_API_KEY',
    'BTCPAY_WEBHOOK_SECRET',
    # Paquetería (Skydropx PRO). No cobra: cotiza envíos y compra guías. Vive aquí
    # por la misma razón que las de cobro — para poder pegarla desde el teléfono.
    # Son DOS porque la API PRO usa OAuth2: se cambian por un token. La vieja
    # `SKYDROPX_API_KEY` era de la API anterior y ya no sirve para nada.
    'SKYDROPX_CLIENT_ID',
    'SKYDROPX_CLIENT_SECRET',
    # El SEGUNDO cotizador de guías (enviosinternacionales.com, revendedor de Skydropx).
    # Christián decidió el 2026-07-31 cotizar en los dos lados y contratar el más barato.
    # ⛔ Mientras no se peguen, `enviosinternacionales.enabled()` es False y todo el sitio
    # se comporta igual que hoy: se cotiza y se compra sólo con Skydropx.
    'ENVIOSINT_CLIENT_ID',
    'ENVIOSINT_CLIENT_SECRET',
    # Turnstile (Cloudflare): el escudo antibots del checkout. No cobra ni cotiza;
    # vive aquí por lo mismo que las demás — para poder pegarla desde el teléfono.
    # La PÚBLICA (sitekey) NO va aquí: esa se publica en el HTML a propósito, no es
    # secreto. Aquí sólo la que valida del lado del servidor.
    'TURNSTILE_SECRET_KEY',
    # Las reseñas de Google del Perfil de Empresa, para la portada. No cobran ni
    # cotizan; viven aquí por lo mismo que las demás. El PLACE_ID no es un secreto
    # (es público y sale en el mapa), pero se guarda junto a su llave para que las
    # dos se peguen en el mismo lugar y no haya que entrar al servidor por una.
    'GOOGLE_PLACES_API_KEY',
    'GOOGLE_PLACE_ID',
    # El motor del chat. No cobra ni cotiza: vive aquí por lo mismo que las
    # demás — para poder pegarlo desde el teléfono. Mientras no estén, el chat
    # sigue con Gemini exactamente como hoy (ver `modelo_ia.py`).
    #
    # ⛔ `GEMINI_API_KEY` NO está aquí a propósito: `ai_assistant` la lee UNA vez
    # al arrancar, así que pegarla desde el panel no la encendería y el Admin
    # mostraría "configurada" sobre una llave que no se usa. Ésa sigue en el
    # `.env` del servidor.
    # El correo. Vivían SÓLO en el `.env` del servidor, así que cambiar de
    # proveedor exigía entrar por SSH — y el 2026-08-01 eso dejó las cotizaciones
    # sin salir: `EMAIL_PROVIDER` viene en `ses` por omisión, esa cuenta está en
    # modo de pruebas, y Resend estaba contratado sin forma de conectarlo desde el
    # Panel. `EMAIL_PROVIDER` no es un secreto sino un interruptor (ses | resend),
    # pero viaja por aquí para que se pueda cambiar sin desplegar.
    'EMAIL_PROVIDER',
    'RESEND_API_KEY',
    'EMAIL_ENABLED',
    'EMAIL_FROM',
    'OPENAI_API_KEY',
    'MOONSHOT_API_KEY',
    'ANTHROPIC_API_KEY',
}


def _fernet() -> Fernet:
    semilla = os.environ.get('JWT_SECRET', 'nova-peptides-secret-key-change-me')
    llave = base64.urlsafe_b64encode(hashlib.sha256(semilla.encode()).digest())
    return Fernet(llave)


def cifrar(valor: str) -> str:
    return _fernet().encrypt(valor.encode()).decode()


def descifrar(blob: str) -> str | None:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return None


def pista(valor: str) -> str:
    """Lo único que se le enseña al Admin: los últimos 4 caracteres."""
    v = valor or ''
    return ('•' * 8 + v[-4:]) if len(v) > 4 else '•' * 8


async def guardar(db, nombre: str, valor: str) -> bool:
    if nombre not in PERMITIDAS:
        return False
    valor = (valor or '').strip()
    if not valor:
        await db[COLECCION].delete_one({'nombre': nombre})
        return True
    await db[COLECCION].update_one(
        {'nombre': nombre},
        {'$set': {'nombre': nombre, 'cifrado': cifrar(valor), 'pista': pista(valor)}},
        upsert=True,
    )
    return True


async def leer(db, nombre: str) -> str | None:
    """El valor efectivo: primero el entorno, luego la base."""
    del_entorno = os.environ.get(nombre)
    if del_entorno:
        return del_entorno
    if db is None:
        return None
    doc = await db[COLECCION].find_one({'nombre': nombre})
    return descifrar(doc['cifrado']) if doc and doc.get('cifrado') else None


# --------------------------------------------------------------------- cache
# Las pasarelas (mercadopago.py, btcpay.py) leen sus llaves desde funciones
# sincronas, y Mongo es async. En vez de reescribir esas rutas, se mantiene un
# cache en memoria que se recarga al arrancar y cada vez que el Admin guarda.
_CACHE: dict = {}


def valor(nombre: str) -> str:
    """El valor efectivo, sincrono. El entorno siempre manda sobre el cache."""
    return os.environ.get(nombre) or _CACHE.get(nombre) or ''


async def recargar(db) -> int:
    """Rellena el cache desde la base. Devuelve cuantas llaves cargo."""
    _CACHE.clear()
    if db is None:
        return 0
    async for d in db[COLECCION].find({}, {'_id': 0}):
        claro = descifrar(d.get('cifrado') or '')
        if claro and d.get('nombre') in PERMITIDAS:
            _CACHE[d['nombre']] = claro
    return len(_CACHE)


async def estado(db) -> list:
    """Para el Admin: qué está configurado y de dónde sale. Sin valores."""
    docs = {}
    if db is not None:
        async for d in db[COLECCION].find({}, {'_id': 0}):
            docs[d['nombre']] = d
    out = []
    for nombre in sorted(PERMITIDAS):
        env = bool(os.environ.get(nombre))
        doc = docs.get(nombre)
        out.append({
            'nombre': nombre,
            'configurado': env or bool(doc),
            'origen': 'servidor' if env else ('panel' if doc else None),
            'pista': (('•' * 8 + os.environ[nombre][-4:]) if env
                      else (doc.get('pista') if doc else None)),
            'editable': not env,
        })
    return out
