"""Pagos en cripto vía NOWPayments — la vía rápida (sin servidor ni nodo propio).

NOWPayments aloja la página de pago, acepta 350+ monedas y puede auto-convertir
a stablecoin (USDT) para que no haya riesgo de precio. Cobra ~0.5%. Requiere un
KYB con la entidad real y una API key.

Se enciende con variables de entorno; sin ellas `enabled()` es False y el
checkout no ofrece esta vía (mismo patrón que BTCPay y Google Sign-In):
  NOWPAYMENTS_API_KEY     API key de la cuenta
  NOWPAYMENTS_IPN_SECRET  secreto para verificar los webhooks (IPN)
"""
import hashlib
import hmac
import json
import os

import requests

API = 'https://api.nowpayments.io/v1'
TIMEOUT = 20


def enabled() -> bool:
    return bool(os.environ.get('NOWPAYMENTS_API_KEY'))


def create_invoice(order_number: str, amount: float, success_url: str, cancel_url: str, ipn_url: str) -> dict:
    """Crea una factura alojada. Devuelve la URL a la que mandamos al cliente.
    price_currency = MXN; NOWPayments cotiza la cripto al momento del pago."""
    resp = requests.post(
        f'{API}/invoice',
        headers={'x-api-key': os.environ['NOWPAYMENTS_API_KEY'], 'Content-Type': 'application/json'},
        json={
            'price_amount': round(float(amount), 2),
            'price_currency': 'mxn',
            'order_id': order_number,
            'ipn_callback_url': ipn_url,
            'success_url': success_url,
            'cancel_url': cancel_url,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f'NOWPayments {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    return {'invoice_id': str(data.get('id', '')), 'checkout_url': data.get('invoice_url', '')}


def verify_ipn(raw_body: bytes, signature_header: str) -> bool:
    """Valida la firma del webhook: HMAC-SHA512 del JSON con las llaves ORDENADAS
    (así lo firma NOWPayments), con el IPN secret. Sin secreto → nada pasa."""
    secret = os.environ.get('NOWPAYMENTS_IPN_SECRET', '')
    if not secret or not signature_header:
        return False
    try:
        payload = json.loads(raw_body.decode() or '{}')
    except ValueError:
        return False
    ordered = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    expected = hmac.new(secret.encode(), ordered.encode(), hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


# El pago está completo y es definitivo cuando llega a 'finished'.
SETTLED_STATUSES = {'finished'}
