"""Pagos con TARJETA vía Mercado Pago (Checkout Pro).

⚠️ POR QUÉ EXISTE ESTE ARCHIVO
Hasta el 2026-07-26 el checkout tenía un formulario de tarjeta que pedía número,
vencimiento y CVC, los validaba en el navegador y **los tiraba a la basura**: nada
se enviaba a ningún lado y NADIE COBRABA. El cliente veía "Pedido recibido" y se
iba creyendo que había pagado. Esto lo reemplaza.

CÓMO FUNCIONA (Checkout Pro)
El cliente no teclea su tarjeta en nuestro sitio: se le manda a la página de
Mercado Pago, paga ahí, y regresa. **Los datos de la tarjeta nunca tocan nuestro
servidor**, que es justo lo que uno quiere — ni PCI, ni tarjetas en nuestra base.

Se enciende con variables de entorno; sin ellas `enabled()` es False y el checkout
no ofrece tarjeta (mismo patrón que NOWPayments y BTCPay):
  MERCADOPAGO_ACCESS_TOKEN    el access token de PRODUCCIÓN (empieza con APP_USR-)
  MERCADOPAGO_WEBHOOK_SECRET  la clave secreta del webhook, para verificar la firma

Módulo aparte y sin Mongo a propósito: se puede probar sin base de datos.
"""
import hashlib
import hmac
import os
import re
from urllib.parse import urlparse

import requests

API = 'https://api.mercadopago.com'
TIMEOUT = 20

# Mercado Pago solo da por bueno el pago cuando queda 'approved'. 'in_process' es
# revisión manual y 'authorized' es dinero apartado pero NO cobrado: ninguno de
# los dos confirma el pedido.
SETTLED_STATUSES = {'approved'}


def enabled() -> bool:
    return bool(os.environ.get('MERCADOPAGO_ACCESS_TOKEN'))


def _headers():
    return {'Authorization': f"Bearer {os.environ['MERCADOPAGO_ACCESS_TOKEN']}",
            'Content-Type': 'application/json'}


def _es_publico(url: str) -> bool:
    """Mercado Pago rechaza la preferencia si le mandas una URL de localhost."""
    host = (urlparse(url).hostname or '').lower()
    return bool(host) and host not in ('localhost', '127.0.0.1') and not host.endswith('.local')


def create_preference(order_number: str, items, total: float, payer_email: str,
                      success_url: str, failure_url: str, webhook_url: str) -> dict:
    """Crea la preferencia de pago y devuelve la URL a la que mandamos al cliente.

    `items` va con el detalle real del carrito para que el cliente vea en Mercado
    Pago lo mismo que tenía en el nuestro. El monto que manda es el TOTAL que
    calculó el servidor — nunca lo que diga el navegador.
    """
    detalle = [{
        'title': (it.get('name') or 'Producto')[:250],
        'quantity': int(it.get('quantity') or 1),
        'unit_price': round(float(it.get('price') or 0), 2),
        'currency_id': 'MXN',
    } for it in (items or [])]
    # El total manda: si por descuentos o envío no cuadra con la suma de los
    # renglones, se cobra el total y se explica en un renglón de ajuste. Así el
    # cliente nunca paga de más ni de menos por un redondeo.
    suma = round(sum(i['unit_price'] * i['quantity'] for i in detalle), 2)
    ajuste = round(float(total) - suma, 2)
    if abs(ajuste) >= 0.01:
        detalle.append({
            'title': 'Envío y descuentos' if ajuste >= 0 else 'Descuento',
            'quantity': 1,
            'unit_price': ajuste,
            'currency_id': 'MXN',
        })

    cuerpo = {
        'items': detalle,
        'external_reference': order_number,
        'statement_descriptor': 'EXYGEN LABS',
        'binary_mode': True,       # o aprobado o rechazado: nada de 'pendiente' eterno
        'back_urls': {'success': success_url, 'pending': success_url, 'failure': failure_url},
        'auto_return': 'approved',
        'metadata': {'order_number': order_number},
    }
    if payer_email:
        cuerpo['payer'] = {'email': payer_email}
    if _es_publico(webhook_url):
        cuerpo['notification_url'] = webhook_url

    resp = requests.post(f'{API}/checkout/preferences', headers=_headers(),
                         json=cuerpo, timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f'MercadoPago {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    return {'preference_id': str(data.get('id', '')),
            'checkout_url': data.get('init_point') or data.get('sandbox_init_point', '')}


def get_payment(payment_id: str) -> dict:
    """Consulta el pago en Mercado Pago. El webhook solo trae el id: el estado
    SIEMPRE se pregunta a la API, nunca se cree lo que venga en el cuerpo."""
    resp = requests.get(f'{API}/v1/payments/{payment_id}', headers=_headers(), timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f'MercadoPago {resp.status_code}: {resp.text[:300]}')
    return resp.json()


def verify_webhook(signature_header: str, request_id: str, data_id: str) -> bool:
    """Valida la firma del webhook.

    Mercado Pago manda `x-signature: ts=<epoch>,v1=<hmac>`. El HMAC-SHA256 se
    calcula sobre la plantilla `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`
    con la clave secreta del webhook. Sin secreto no pasa nada — igual que en
    NOWPayments y BTCPay.

    OJO: el `data.id` va en MINÚSCULAS en la plantilla. Con mayúsculas la firma
    no cuadra y todos los pagos se rechazarían en silencio.
    """
    secreto = os.environ.get('MERCADOPAGO_WEBHOOK_SECRET', '')
    if not secreto or not signature_header or not data_id:
        return False
    partes = dict(
        p.split('=', 1) for p in signature_header.split(',')
        if '=' in p
    )
    ts = (partes.get('ts') or '').strip()
    v1 = (partes.get('v1') or '').strip()
    if not ts or not v1:
        return False
    plantilla = f'id:{str(data_id).lower()};request-id:{request_id or ""};ts:{ts};'
    esperado = hmac.new(secreto.encode(), plantilla.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


def extract_payment_id(query: dict, body: dict) -> str:
    """Saca el id del pago. Mercado Pago lo manda de varias formas según el aviso:
    `?data.id=`, `?id=`, o en el cuerpo `{"data": {"id": ...}}`."""
    for clave in ('data.id', 'id'):
        if query.get(clave):
            return str(query[clave])
    data = (body or {}).get('data') or {}
    if data.get('id'):
        return str(data['id'])
    if (body or {}).get('id'):
        return str(body['id'])
    return ''


def is_payment_event(query: dict, body: dict) -> bool:
    """Mercado Pago avisa de muchas cosas (merchant_order, plan, suscripción…).
    Solo nos interesan los avisos de PAGO."""
    tipo = (query.get('type') or query.get('topic')
            or (body or {}).get('type') or (body or {}).get('topic') or '')
    return str(tipo).lower() in ('payment', 'payments')
