"""Programa de lealtad: puntos por compra, canjeables por producto.

Reglas de negocio (Christian, 2026-07-20):
- Cada compra PAGADA genera puntos; se canjean como dinero en compras futuras.
- Los distribuidores NO participan: ni ganan ni canjean.
- Tasa: 3% de la mercancía realmente pagada (bajó de 5% por orden de Christian, 2026-07-21) (después de descuentos y de puntos
  canjeados, sin contar el envío). 1 punto = 1 peso al canjear.
- Los puntos se DEPOSITAN cuando el pago se verifica (confirmado/enviado/
  entregado), no al crear el pedido: si no, un pedido SPEI que nunca se paga
  regalaría puntos.
- ⛔ CON EL DESCUENTO MÁXIMO (40%) NO SE GANAN PUNTOS (Christian, 2026-07-27,
  a raíz de una venta directa a Paz Cambray). El 40% ya es el techo de lo que
  la casa puede regalar en un pedido; sumarle 3% de puntos encima es descontar
  dos veces sobre el mismo margen. Es la misma idea que el tope por producto,
  donde descuento + comisión se limitan JUNTOS y no cada uno por su lado.
"""

EARN_RATE = 0.03
PAID_STATUSES = ('confirmado', 'enviado', 'entregado')
# El descuento más alto que existe. A partir de aquí el pedido deja de generar
# puntos. Se compara con >= y con holgura porque el ratio viaja como float.
MAX_DISCOUNT = 0.40


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


def earns_points(discount_rate) -> bool:
    """¿Este pedido genera puntos, según el descuento que se le dio?
    Con el máximo (40%) no. Se mide sobre el descuento CONCEDIDO, que es lo que
    Christian llama "obtener el 40%" — no sobre el efectivo renglón por renglón,
    que puede bajar por el tope de cada producto."""
    try:
        rate = float(discount_rate or 0)
    except (TypeError, ValueError):
        return True
    return rate < MAX_DISCOUNT - 1e-9


def earn(paid_amount, is_eligible, discount_rate=0.0) -> int:
    """Puntos que genera una compra: 3% de lo pagado en mercancía, entero
    hacia abajo. Cero si el monto es cero, si la cuenta no participa o si el
    pedido llevó el descuento máximo."""
    if not is_eligible or not earns_points(discount_rate):
        return 0
    try:
        paid = float(paid_amount or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(paid * EARN_RATE))
