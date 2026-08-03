"""LA CONSTANCIA DEL AVISO DE ENTRADA — quién aceptó el RUO, cuándo y desde dónde.

⛔ POR QUÉ EXISTE (Christián, 2026-08-02). El aviso de entrada se guardaba SÓLO en
el navegador del cliente: `localStorage`, `sessionStorage` y una cookie. Los tres
viven en su computadora y los tres los borra él con un clic. O sea que la casa no
tenía NADA con qué sostener que alguien aceptó: si mañana un cliente dice que nunca
vio el aviso, no hay con qué contradecirlo.

Se detectó comparando contra Certified-PepMex y contra Nexaph, que sí lo dicen
expreso en sus términos: «Agreement acceptance is recorded digitally and constitutes
legal acceptance».

QUÉ SE GUARDA, Y POR QUÉ CADA COSA:

  · `accepted_at` — la hora del SERVIDOR, no la del navegador. La del cliente la
    pone su reloj, que él controla; ésta no.
  · `ip` y `user_agent` — los elementos de atribución del art. 1298-A del Código
    de Comercio. No identifican a nadie con certeza y no se pretende que lo hagan.
  · `version` — QUÉ texto aceptó. Sin esto la constancia no sirve para nada el día
    que el aviso cambie: probaría que aceptó «algo», no que aceptó ESTO.
  · `edad` y `investigacion` — las DOS casillas por separado, porque son dos
    declaraciones distintas y así consta cuál hizo. Antes iban en una sola frase.
  · `recordar` — si pidió que se le recuerde. No es parte de la declaración; se
    guarda para saber por qué a esa persona no le volvió a salir el aviso.
  · `user_id` — sólo si había sesión. La mayoría acepta ANTES de tener cuenta, y
    ése es justo el caso que hay que poder probar.

⛔ NO ES UN CANDADO. Guardar la constancia nunca puede impedir entrar al sitio: si
la base falla, el cliente pasa igual y la falla se anota. Un aviso legal que deja a
la gente afuera cuando se cae Mongo es peor que no tener constancia.

Módulo casi puro a propósito: las reglas se prueban sin base de datos.
"""
from datetime import datetime, timezone

COLECCION = 'ruo_aceptado'

# La firma del texto que se está aceptando. ⛔ SÚBELA cuando cambie el TEXTO del
# aviso (la edad, lo que declara cada casilla), no cuando se mueva un color: la
# constancia dice «aceptó esta versión», y una versión que no distingue textos
# distintos no prueba nada.
#
#   v1  — casilla única, «18 años o más y con fines de investigación».
#   v2  — 2026-08-02: DOS casillas separadas y la edad sube a 21 (Christián, tras
#         ver que Nexaph y el estándar de EUA piden 21).
VERSION = 'v2-2026-08-02'
EDAD_MINIMA = 21


def ip_de(request) -> str:
    """La IP REAL de quien acepta, no la del proxy.

    Misma lógica que `acuerdo.ip_de` y por la misma razón: el backend vive detrás
    de Caddy y de la puerta nginx, así que `request.client.host` es siempre
    127.0.0.1. La buena es la PRIMERA de `X-Forwarded-For`.
    """
    cabeceras = getattr(request, 'headers', None) or {}
    reenviada = (cabeceras.get('x-forwarded-for') or '').split(',')[0].strip()
    if reenviada:
        return reenviada[:64]
    cliente = getattr(request, 'client', None)
    return (getattr(cliente, 'host', '') or '')[:64]


def user_agent_de(request) -> str:
    cabeceras = getattr(request, 'headers', None) or {}
    return (cabeceras.get('user-agent') or '')[:400]


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def declaracion_completa(edad: bool, investigacion: bool) -> bool:
    """Las DOS casillas, o no hay aceptación.

    El botón del navegador ya lo impide, pero el navegador no es la autoridad:
    quien llame al endpoint a mano puede mandar lo que quiera, y una constancia
    a medias es peor que ninguna — dice que aceptó cuando no aceptó.
    """
    return bool(edad) and bool(investigacion)


def huella(textos: dict) -> str:
    """SHA-256 de lo que se le enseñó, para poder comparar dos constancias de un
    vistazo sin leer los párrafos enteros."""
    import hashlib
    crudo = '␟'.join(f'{k}={textos.get(k, "")}' for k in sorted(textos or {}))
    return hashlib.sha256(crudo.encode('utf-8')).hexdigest()


def constancia(request, edad: bool, investigacion: bool, recordar: bool,
               user_id: str = None, idioma: str = '', textos: dict = None) -> dict:
    """El documento que se guarda. Sin base de datos: así se prueba de verdad."""
    textos = {k: str(v)[:600] for k, v in (textos or {}).items()}
    return {
        'accepted_at': ahora(),
        'version': VERSION,
        'edad_minima': EDAD_MINIMA,
        'edad': bool(edad),
        'investigacion': bool(investigacion),
        'recordar': bool(recordar),
        'ip': ip_de(request),
        'user_agent': user_agent_de(request),
        'idioma': (idioma or '')[:16],
        'user_id': user_id or None,
        # ⛔ EL TEXTO EXACTO QUE SE LE ENSEÑÓ, no sólo su número de versión
        # (hallazgo de la revisión de Codex, 2026-08-03). `VERSION` la escribe el
        # backend y el TEXTO vive en el i18n del frontend: cualquiera podía editar
        # las frases sin subir la versión, y entonces la constancia probaba que
        # aceptó «la v2» sin que nadie pudiera reconstruir qué decía la v2.
        # Se guarda lo que el navegador dice haber pintado — con su huella, para
        # comparar dos constancias sin leerlas enteras.
        'textos': textos,
        'huella_textos': huella(textos) if textos else '',
    }


async def registrar(db, request, edad: bool, investigacion: bool, recordar: bool,
                    user_id: str = None, idioma: str = '', textos: dict = None) -> dict:
    """Guarda la constancia y devuelve lo que se guardó.

    ⛔ NUNCA revienta hacia arriba. Si la base no está o falla, se devuelve el
    documento con `guardada: False` y el cliente entra igual. La constancia es
    para la casa, no un peaje para el visitante.
    """
    if not declaracion_completa(edad, investigacion):
        return {'guardada': False, 'motivo': 'faltan casillas'}
    doc = constancia(request, edad, investigacion, recordar, user_id, idioma, textos)
    if db is None:
        return {**doc, 'guardada': False, 'motivo': 'sin base'}
    try:
        await db[COLECCION].insert_one(dict(doc))
        return {**doc, 'guardada': True}
    except Exception as e:                                    # noqa: BLE001
        return {**doc, 'guardada': False, 'motivo': str(e)[:200]}


def _fila(doc: dict) -> dict:
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != '_id'}


async def por_ip(db, ip: str, limite: int = 50) -> list:
    """Las constancias de una IP, de la más nueva a la más vieja.

    Es la búsqueda que sirve cuando hay un problema: llega una queja, se tiene la
    IP del pedido, y se quiere ver si esa persona pasó por el aviso y cuándo.
    """
    if db is None or not ip:
        return []
    docs = await db[COLECCION].find({'ip': ip}).to_list(limite)
    docs.sort(key=lambda d: d.get('accepted_at', ''), reverse=True)
    return [_fila(d) for d in docs]
