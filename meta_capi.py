"""Conversions API de Meta: avisarle SERVIDOR A SERVIDOR que una compra se pagó.

Por qué existe
--------------
El píxel del navegador solo ve al que compra CON navegador y CON cookies. La
primera venta real que trajo Meta (pedido EX-20260730-2906, 2026-07-30) llegó
por WhatsApp: sin utm, sin fbclid, sin cookie. Meta nunca la vio. Y una campaña
que no ve compras no puede optimizar a Compras — de ahí los 2,700 clics y cero
conversiones atribuidas.

Este módulo manda el evento `Purchase` desde el servidor, y lo manda cuando el
DINERO ENTRÓ de verdad (webhook de la pasarela), no cuando el cliente alcanza a
ver la pantalla de gracias. Es la única señal honesta.

Deduplicación (para que una compra no cuente doble)
---------------------------------------------------
Navegador y servidor pueden mandar la MISMA compra. Meta las une en una sola si
las dos traen el mismo `event_id`. Aquí el id es `purchase-<número de pedido>`:
único, y los dos lados lo conocen. `track.js` lo manda como `eventID`.

Credenciales — OJO, NO es el token del panel
--------------------------------------------
`META_CAPI_TOKEN` es el token de la Conversions API del PÍXEL (Administrador de
eventos → Conjunto de datos → Configuración → Generar token de acceso). Es otra
cosa que `META_TOKEN`, que es de la API de Marketing y solo LEE anuncios.

Si `META_CAPI_TOKEN` no está, se intenta con `META_TOKEN` — a veces alcanza,
cuando ese token trae `ads_management` y su dueño es admin del píxel. Si tampoco
alcanza, este módulo NO manda nada, deja el motivo en el log y NO rompe el
cobro: una compra jamás se cae porque a Meta no le llegó su avisito.

Módulo casi PURO: `construir_evento` no sale a internet y se puede probar sin
red ni Mongo. Solo `enviar_compra` habla con Meta.
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GRAPH = 'https://graph.facebook.com/v21.0'

# El píxel del sitio. Está a la vista en public/index.html, no es un secreto.
PIXEL_POR_DEFECTO = '2487053198462294'

# México. Sirve para completar teléfonos que el cliente escribió sin lada país:
# Meta exige el código de país o no empareja a nadie.
LADA_PAIS_DEFECTO = '52'


def pixel_id():
    return os.environ.get('META_PIXEL_ID') or PIXEL_POR_DEFECTO


def token():
    """El de CAPI manda; el del panel es el plan B."""
    return os.environ.get('META_CAPI_TOKEN') or os.environ.get('META_TOKEN') or ''


def configurado():
    return bool(token() and pixel_id())


def test_code():
    """Código de Test Events. Con él puesto, los eventos van a la pestaña de
    pruebas del Administrador de eventos y NO ensucian los datos reales."""
    return os.environ.get('META_CAPI_TEST_CODE') or ''


# ---------------------------------------------------------------- hasheo
def _sha(valor):
    """Meta solo acepta datos personales en SHA-256. Nunca viaja nada en claro."""
    if not valor:
        return None
    return hashlib.sha256(str(valor).encode('utf-8')).hexdigest()


def _norm_texto(v):
    """minúsculas, sin espacios de sobra, sin puntuación. Es lo que Meta espera."""
    if not v:
        return ''
    return re.sub(r'[^a-z0-9áéíóúüñ]', '', str(v).strip().lower())


def _norm_email(v):
    return str(v).strip().lower() if v else ''


def _norm_tel(v):
    """Solo dígitos, CON lada de país. Sin lada, Meta no empareja a nadie.

    Los teléfonos mexicanos que se escriben a mano vienen de 10 dígitos: se les
    pone el 52 delante. Si ya trae 12-13 dígitos se respeta lo que venga.
    """
    if not v:
        return ''
    d = re.sub(r'\D', '', str(v))
    if not d:
        return ''
    if len(d) == 10:
        d = LADA_PAIS_DEFECTO + d
    return d


def _nombre_partido(full_name):
    """'Juan Pérez López' -> ('juan', 'pérez lópez'). Un solo nombre: apellido vacío."""
    partes = [p for p in str(full_name or '').strip().split() if p]
    if not partes:
        return '', ''
    if len(partes) == 1:
        return partes[0], ''
    return partes[0], ' '.join(partes[1:])


def _fbc(attribution, cuando_ms):
    """El `fbc` es la cookie que Meta le pega al que llega por un anuncio.

    Si el pedido guardó `fbclid` se reconstruye en el formato que Meta exige
    (`fb.1.<milisegundos>.<fbclid>`). Sin fbclid no hay nada que reconstruir —
    justo el caso de WhatsApp — y ahí el emparejamiento queda en manos del
    correo y el teléfono hasheados.
    """
    fbclid = (attribution or {}).get('fbclid') or ''
    if not fbclid:
        return None
    return f'fb.1.{cuando_ms}.{fbclid}'


def _cuando(order):
    """Momento del evento en segundos unix: el del PAGO, no el de hoy.

    Meta rechaza eventos de más de 7 días, así que si `paid_at` no se puede leer
    se usa el reloj actual en vez de tirar el evento.
    """
    for campo in ('paid_at', 'created_at'):
        crudo = order.get(campo)
        if not crudo:
            continue
        try:
            txt = str(crudo).replace('Z', '+00:00')
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            continue
    return int(datetime.now(timezone.utc).timestamp())


# Dominios reservados por la RFC 2606 para pruebas. Las auditorías y los E2E
# (`npm run auditoria`, `e2e:cripto`, `e2e:tarjeta`) levantan pedidos REALES con
# un correo `@example.com`, los confirman y los borran. El pedido se borra de
# nuestra base, pero lo que ya se le mandó a Meta NO SE PUEDE BORRAR: cada
# corrida le enseñaría una compra que nunca existió, inflaría el ROAS del panel
# y acabaría haciendo que se suba el presupuesto por una señal falsa. Por eso
# estos pedidos nunca salen.
DOMINIOS_DE_PRUEBA = ('@example.com', '@example.org', '@example.net', '@test.invalid')


def es_pedido_de_prueba(order):
    correo = _norm_email((order.get('customer') or {}).get('email'))
    return correo.endswith(DOMINIOS_DE_PRUEBA)


def event_id(order_number):
    """La llave de la deduplicación. TIENE que ser idéntica a la del navegador
    (`track.js`), o Meta contará la misma compra dos veces."""
    return f'purchase-{order_number}'


# ---------------------------------------------------------------- el evento
def construir_evento(order, test=None):
    """Pedido (dict de Mongo) -> el cuerpo que se le manda a Meta.

    Puro: no toca red ni base. Devuelve None si el pedido no trae lo mínimo
    (número y total), porque un Purchase sin monto le enseña basura a Meta.
    """
    numero = order.get('order_number') or ''
    total = float(order.get('total') or 0)
    if not numero or total <= 0:
        return None
    if es_pedido_de_prueba(order):
        return None

    cliente = order.get('customer') or {}
    attribution = order.get('attribution') or {}
    ts = _cuando(order)
    nombre, apellido = _nombre_partido(cliente.get('full_name'))

    # Todo lo personal va hasheado. Los que salgan vacíos se quitan abajo: Meta
    # penaliza los campos presentes pero vacíos.
    user_data = {
        'em': _sha(_norm_email(cliente.get('email'))),
        'ph': _sha(_norm_tel(cliente.get('phone'))),
        'fn': _sha(_norm_texto(nombre)),
        'ln': _sha(_norm_texto(apellido)),
        'ct': _sha(_norm_texto(cliente.get('city'))),
        'st': _sha(_norm_texto(cliente.get('state'))),
        'zp': _sha(re.sub(r'\D', '', str(cliente.get('postal_code') or ''))[:5]),
        'country': _sha(_norm_texto(cliente.get('country') or 'MX')),
        # El id propio del visitante ayuda a Meta a juntar sesiones del mismo
        # comprador aunque cambie de aparato.
        'external_id': _sha(attribution.get('visitor_id') or order.get('user_id') or ''),
        'fbc': _fbc(attribution, ts * 1000),
    }
    user_data = {k: v for k, v in user_data.items() if v}

    items = order.get('items') or []
    contents = [{'id': str(i.get('product_id') or ''),
                 'quantity': int(i.get('quantity') or 1),
                 'item_price': float(i.get('price') or 0)}
                for i in items if i.get('product_id')]

    evento = {
        'event_name': 'Purchase',
        'event_time': ts,
        'event_id': event_id(numero),
        # La compra nace en el sitio aunque el cliente haya llegado por WhatsApp:
        # el pedido se levantó en el checkout de exygenlabs.com.
        'action_source': 'website',
        'event_source_url': f"{os.environ.get('SITE_URL', 'https://exygenlabs.com')}/pedido/{numero}",
        'user_data': user_data,
        'custom_data': {
            'currency': 'MXN',
            'value': round(total, 2),
            'order_id': numero,
            'content_type': 'product',
            'contents': contents,
            'content_ids': [c['id'] for c in contents],
            'num_items': sum(c['quantity'] for c in contents),
        },
    }
    cuerpo = {'data': [evento]}
    codigo = test if test is not None else test_code()
    if codigo:
        cuerpo['test_event_code'] = codigo
    return cuerpo


# ---------------------------------------------------------------- el envío
async def enviar_compra(order, test=None):
    """Le avisa a Meta que este pedido se pagó. Nunca lanza excepción.

    Devuelve un dict con lo que pasó, para poder verlo en las pruebas y en el
    log: {'enviado': bool, 'motivo': str, ...}. Cobrar es lo importante; medir
    va después y jamás debe tumbar un webhook.
    """
    if not configurado():
        logger.warning('Meta CAPI apagado: falta META_CAPI_TOKEN en el entorno. '
                       'La compra %s no se le avisó a Meta.',
                       (order or {}).get('order_number', '?'))
        return {'enviado': False, 'motivo': 'sin token'}

    if es_pedido_de_prueba(order or {}):
        return {'enviado': False, 'motivo': 'pedido de prueba'}

    cuerpo = construir_evento(order or {}, test=test)
    if not cuerpo:
        return {'enviado': False, 'motivo': 'pedido incompleto'}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f'{GRAPH}/{pixel_id()}/events',
                params={'access_token': token()},
                json=cuerpo,
            )
        if r.status_code >= 400:
            try:
                msg = r.json().get('error', {}).get('message', r.text[:300])
            except Exception:
                msg = r.text[:300]
            logger.error('Meta CAPI rechazó la compra %s: %s %s',
                         order.get('order_number'), r.status_code, msg)
            return {'enviado': False, 'motivo': msg, 'status': r.status_code}
        datos = r.json()
        logger.info('Meta CAPI: compra %s avisada (%s eventos recibidos)',
                    order.get('order_number'), datos.get('events_received'))
        return {'enviado': True, 'respuesta': datos}
    except Exception as e:      # red caída, DNS, lo que sea: se anota y ya
        logger.exception('Meta CAPI: no se pudo avisar la compra %s',
                         (order or {}).get('order_number', '?'))
        return {'enviado': False, 'motivo': str(e)}
