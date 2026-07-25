"""Recuperación de carritos: intentos de compra que no se cerraron.

Reglas de Christian (2026-07-25), al pie de la letra:

  - Se manda **UNA SOLA** oferta por intento. Nunca dos.
  - **Nada de cupones abajo de $2,500.** En carritos chicos solo un seguimiento,
    una vez, sin descuento — no vale la pena regalar margen.
  - El cupón **solo es válido si la compra es del mismo monto o mayor** al que
    tenía el carrito cuando se mandó la oferta. Si no, el cliente quita productos,
    usa el cupón y nos deja peor que antes.
  - El tamaño de la oferta depende del monto del carrito.

Y encima siguen mandando las reglas de descuento de siempre: el descuento se
recorta al tope de CADA producto (primero el ROI) y los insumos no entran. Este
módulo solo decide QUÉ ofrecer; quién lo cobra es `server.create_order`.

Módulo PURO: recibe números, devuelve la oferta. Sin base de datos, sin red.
"""

# Abajo de esto no se regala nada. Christian, 2026-07-25.
MIN_FOR_OFFER = 2500

# Escalera de ofertas por monto del carrito. El descuento automático del sitio es
# 10% (y 15% desde $35,000), así que una oferta que no lo supere no se siente:
# por eso el primer escalón ya empieza en 15%.
OFFER_TIERS = [
    {'min': 10000, 'rate': 0.20, 'perks': ['agua_10ml', 'envio_gratis']},
    {'min': 5000,  'rate': 0.15, 'perks': ['agua_10ml', 'envio_gratis']},
    {'min': 2500,  'rate': 0.15, 'perks': ['agua_3ml', 'envio_gratis']},
]

# El agua no es un regalo cualquiera: la NECESITAN para reconstituir, así que
# quita fricción de verdad. Por eso va en los tres escalones — pero la de 3 mL
# ($199) en el chico y la de 10 mL ($349) de $5,000 para arriba, para que el que
# compra más se lleve el mejor trato y no al revés. Christian, 2026-07-25.
PERK_TEXT = {
    'agua_3ml': 'agua bacteriostática de 3 mL de cortesía',
    'agua_10ml': 'agua bacteriostática de 10 mL de cortesía',
    'envio_gratis': 'envío gratis',
}

# El SKU que hay que meter en la caja al preparar el pedido.
PERK_SKU = {'agua_3ml': 'AGUABACTERIOST-3ML', 'agua_10ml': 'AGUABACTERIOST-10ML'}

# Cuánto esperamos antes de escribirle. Ni encima (parece acoso) ni tarde (ya compró
# en otro lado). Una hora es el estándar de la industria para el primer contacto.
WAIT_MINUTES = 60
COUPON_DAYS = 7        # la oferta caduca: es lo que empuja a cerrar


def offer_for(cart_total):
    """Qué se le ofrece a un carrito de `cart_total` pesos.

    Devuelve dict siempre — nunca None — para que quien llame no tenga que
    adivinar:
      {'kind': 'cupon', 'rate': .., 'perk': .., 'min_order': ..}  -> mandar cupón
      {'kind': 'seguimiento'}                                      -> solo escribirle
      {'kind': 'nada'}                                             -> carrito vacío
    """
    total = float(cart_total or 0)
    if total <= 0:
        return {'kind': 'nada'}
    if total < MIN_FOR_OFFER:
        # Carrito chico: un recordatorio y ya. Sin descuento.
        return {'kind': 'seguimiento'}
    for tier in OFFER_TIERS:
        if total >= tier['min']:
            return {
                'kind': 'cupon',
                'rate': tier['rate'],
                'perks': list(tier['perks']),
                'perk_text': ' + '.join(PERK_TEXT[p] for p in tier['perks']),
                # EL CANDADO: el cupón no sirve si compra menos de lo que ya traía.
                'min_order': round(total),
            }
    return {'kind': 'seguimiento'}


def should_contact(intento, now_minutes):
    """¿Ya toca escribirle? Solo si no se le ha escrito NUNCA y ya pasó la espera.

    `intento`: el registro guardado. `now_minutes`: minutos transcurridos desde
    que abandonó. Se separa del reloj para poder probarlo."""
    if intento.get('contacted'):
        return False                      # una sola vez, siempre
    if intento.get('status') != 'pendiente':
        return False                      # ya compró o se descartó
    if not (intento.get('email') or '').strip():
        return False                      # sin correo no hay a dónde escribir
    return now_minutes >= WAIT_MINUTES


def coupon_is_valid_for(coupon, order_merchandise):
    """El candado del monto, del lado del cobro: un cupón de recuperación solo
    aplica si la compra es >= al carrito que lo genero."""
    minimo = float(coupon.get('min_order') or 0)
    return float(order_merchandise or 0) >= minimo
