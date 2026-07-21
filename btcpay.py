"""Pagos en cripto con BTCPay Server AUTOALOJADO.

Filosofía (decisión de Christian, 2026-07-21): igual que SPEI, las llaves son
nuestras y nadie puede congelar el dinero. BTCPay corre en un servidor propio;
aquí solo hablamos con su API (Greenfield) por HTTP.

Se enciende con variables de entorno; si faltan, `enabled()` es False y el
checkout no ofrece cripto (mismo patrón que Google Sign-In):
  BTCPAY_URL         p.ej. https://pay.exygenlabs.com
  BTCPAY_STORE_ID    id de la tienda en BTCPay
  BTCPAY_API_KEY     API key Greenfield (permiso btcpay.store.cancreateinvoice)
  BTCPAY_WEBHOOK_SECRET  secreto compartido para firmar los webhooks
"""
import hashlib
import hmac
import os

import requests

TIMEOUT = 20


def _cfg():
    return {
        'url': (os.environ.get('BTCPAY_URL', '') or '').rstrip('/'),
        'store': os.environ.get('BTCPAY_STORE_ID', ''),
        'key': os.environ.get('BTCPAY_API_KEY', ''),
        'secret': os.environ.get('BTCPAY_WEBHOOK_SECRET', ''),
    }


def enabled() -> bool:
    c = _cfg()
    return bool(c['url'] and c['store'] and c['key'])


def create_invoice(order_number: str, amount: float, redirect_url: str, buyer_email: str = '') -> dict:
    """Crea una factura en BTCPay para un pedido. La factura vive ~15 min y su
    metadata lleva nuestro order_number para reconciliar en el webhook."""
    c = _cfg()
    resp = requests.post(
        f"{c['url']}/api/v1/stores/{c['store']}/invoices",
        headers={'Authorization': f"token {c['key']}", 'Content-Type': 'application/json'},
        json={
            'amount': f'{amount:.2f}',
            'currency': 'MXN',
            'metadata': {'orderId': order_number, 'buyerEmail': buyer_email},
            'checkout': {
                'redirectURL': redirect_url,
                'expirationMinutes': 15,
                'redirectAutomatically': True,
            },
        },
        timeout=TIMEOUT,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f'BTCPay {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    return {'invoice_id': data['id'], 'checkout_url': data.get('checkoutLink', '')}


def verify_webhook(raw_body: bytes, signature_header: str) -> bool:
    """Valida la firma HMAC-SHA256 del webhook (cabecera BTCPay-Sig: 'sha256=...').
    Sin secreto configurado NO se acepta ningún webhook (fail-closed)."""
    secret = _cfg()['secret']
    if not secret or not signature_header:
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


# Estados de BTCPay que significan "el dinero ya llegó y es definitivo".
SETTLED_EVENTS = {'InvoiceSettled', 'InvoicePaymentSettled'}
