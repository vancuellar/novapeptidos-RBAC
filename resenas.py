"""LAS RESEÑAS DE GOOGLE — las de verdad, traídas del Perfil de Empresa.

Christián, 2026-08-05: «necesito que me construyas una sección en el home que
muestre nuestros reviews de Facebook y/o Google» → «Sí tiene Google Business,
conéctalo a la API».

⛔ SÓLO GOOGLE, Y NO ES POR FLOJERA. Meta cerró el acceso a las reseñas de páginas:
hace falta meter la app a revisión y casi siempre se rechaza. Google sí lo publica
por API y devuelve hasta CINCO reseñas (ése es el tope de Google, no nuestro).

⛔ SE GUARDAN EN CACHÉ, Y ES LO QUE HACE QUE ESTO NO CUESTE. La portada la ve
cualquiera: una llamada a Google por visita sería pagarle a Google por cada curioso
—y encima la portada quedaría atada a que su API conteste rápido—. Las reseñas
cambian una vez cada varios días; el caché dura horas.

⛔ Y SI ALGO FALLA, LA SECCIÓN NO SALE. Nunca se rompe la portada por esto: sin
llaves, sin red o con una respuesta rara, se devuelve vacío y el sitio no pinta la
sección. Una portada sin testimonios se ve bien; una portada rota, no.

⚠️ LO QUE PIDEN LOS TÉRMINOS DE GOOGLE y hay que respetar al pintarlas: el nombre
del autor, su foto y una liga a la reseña en Google. Por eso viajan esos campos y no
sólo el texto — no se pueden enseñar como testimonios anónimos de la casa.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

# La API nueva de Places (v1). Se piden SÓLO los campos que se usan: Google cobra
# por «SKU» según lo que pidas, y traer de más es pagar de más.
URL = 'https://places.googleapis.com/v1/places/{place_id}'
CAMPOS = 'reviews,rating,userRatingCount,displayName'

TIMEOUT_S = 8

# Cuánto vive el caché. Doce horas: las reseñas nuevas aparecen el mismo día y
# Google no se lleva más de dos llamadas diarias.
CACHE_S = 12 * 60 * 60

# Guardado en memoria del proceso. No hace falta Mongo: si el contenedor se
# reinicia se vuelve a pedir una vez y ya.
_CACHE = {'cuando': 0.0, 'datos': None}


def _config() -> tuple:
    import secretos
    return (secretos.valor('GOOGLE_PLACES_API_KEY'), secretos.valor('GOOGLE_PLACE_ID'))


def enabled() -> bool:
    """Sin las dos llaves, la sección no existe y el sitio se comporta como antes."""
    return all(_config())


def _limpia(r: dict) -> dict:
    """Una reseña de Google, con lo justo para pintarla y dar el crédito que exigen."""
    autor = r.get('authorAttribution') or {}
    return {
        'autor': autor.get('displayName') or '',
        'foto': autor.get('photoUri') or '',
        'perfil': autor.get('uri') or '',
        'estrellas': int(r.get('rating') or 0),
        'texto': ((r.get('originalText') or {}).get('text')
                  or (r.get('text') or {}).get('text') or '').strip(),
        'cuando': r.get('relativePublishTimeDescription') or '',
        'liga': r.get('googleMapsUri') or '',
    }


def traer(forzar: bool = False) -> dict:
    """`{'resenas': [...], 'promedio': 4.9, 'cuantas': 37}`. Vacío si algo falla.

    Bloquea (va a la red), así que quien la llame desde el servidor la manda a otro
    hilo — igual que el PDF de las guías.
    """
    vacio = {'resenas': [], 'promedio': 0, 'cuantas': 0}
    llave, place_id = _config()
    if not (llave and place_id):
        return vacio
    ahora = time.time()
    if not forzar and _CACHE['datos'] is not None and ahora - _CACHE['cuando'] < CACHE_S:
        return _CACHE['datos']
    try:
        r = requests.get(
            URL.format(place_id=place_id),
            headers={'X-Goog-Api-Key': llave, 'X-Goog-FieldMask': CAMPOS},
            timeout=TIMEOUT_S)
        if r.status_code != 200:
            logger.warning('Reseñas: Google contesto %s (%s)', r.status_code, r.text[:200])
            # ⛔ Se devuelve lo ÚLTIMO BUENO si lo hay. Que Google tenga una mala
            # tarde no tiene por qué vaciarle los testimonios a la portada.
            return _CACHE['datos'] or vacio
        cuerpo = r.json()
    except Exception as e:
        logger.warning('Reseñas: no se pudo preguntarle a Google (%s)', e)
        return _CACHE['datos'] or vacio
    limpias = [_limpia(x) for x in (cuerpo.get('reviews') or [])]
    # Sin texto no hay testimonio que enseñar: una estrella suelta no dice nada.
    limpias = [x for x in limpias if x['texto'] and x['estrellas']]
    datos = {
        'resenas': limpias,
        'promedio': round(float(cuerpo.get('rating') or 0), 1),
        'cuantas': int(cuerpo.get('userRatingCount') or 0),
    }
    _CACHE.update({'cuando': ahora, 'datos': datos})
    return datos
