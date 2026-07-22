"""Programa de lealtad: puntos por compra, canjeables por producto.

Reglas de negocio (Christian, 2026-07-20):
- Cada compra PAGADA genera puntos; se canjean como dinero en compras futuras.
- Los distribuidores NO participan: ni ganan ni canjean.
- Tasa: 3% de la mercancía realmente pagada (bajó de 5% por orden de Christian, 2026-07-21) (después de descuentos y de puntos
  canjeados, sin contar el envío). 1 punto = 1 peso al canjear.
- Los puntos se DEPOSITAN cuando el pago se verifica (confirmado/enviado/
  entregado), no al crear el pedido: si no, un pedido SPEI que nunca se paga
  regalaría puntos.
"""

EARN_RATE = 0.03
PAID_STATUSES = ('confirmado', 'enviado', 'entregado')


def eligible(user) -> bool:
    """Solo cuentas de cliente. Sin cuenta no hay donde abonar; los
    distribuidores quedan fuera por regla de negocio."""
    return bool(user) and user.get('role') != 'distributor'


def clamp_redeem(requested, balance, merchandise_total) -> int:
    """Cuántos puntos se pueden canjear de verdad: nunca más que el saldo y
    nunca más que la mercancía (el envío se paga en dinero)."""
    try:
        requested = int(requested or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(requested, int(balance or 0), int(merchandise_total or 0)))


def earn(paid_amount, is_eligible) -> int:
    """Puntos que genera una compra: 3% de lo pagado en mercancía, entero
    hacia abajo. Cero si el monto es cero o la cuenta no participa."""
    if not is_eligible:
        return 0
    try:
        paid = float(paid_amount or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(paid * EARN_RATE))
