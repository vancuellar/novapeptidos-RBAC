"""Esquema de distribuidores en pirámide (Master / Senior / Junior).

Diseño cerrado por Christian (handoff §4ter, 2026-07-21). Reglas del reparto:

  (i)  Cada distribuidor gana su TASA de nivel sobre la venta que hace con su
       código. Además, cada distribuidor por ENCIMA de él (hasta 2 niveles) gana
       una SOBRECOMISIÓN FIJA de 3.5% sobre esa misma venta.
         - Vende Junior: Jr 22.5 + su Senior 3.5 + el Master 3.5 = 29.5
         - Vende Senior: Sr 26 + su Master 3.5 = 29.5
         - Vende Master: se queda la bolsa entera (30–40%)
  (ii) Todo sub nuevo entra como JUNIOR; a Senior/Master solo se llega por ascenso.
       El descuento al cliente sale SOLO de la tajada del vendedor (nunca toca el
       3.5% de arriba) — eso lo controla el checkout, no este módulo.
  (iii) La sobrecomisión de 3.5% es FIJA e intocable, sin importar el nivel del de
       arriba. Lo que no se reparte (p.ej. un Junior sin Senior arriba) se queda
       con la casa (Christian).

Este módulo es PURO (sin base de datos): recibe diccionarios y devuelve el reparto
en pesos, redondeado y bloqueado al momento de la orden. Los reportes suman lo
guardado, así que cambiar tasas o niveles nunca toca ventas pasadas.
"""

# Tasa base por nivel (proporción de la venta). Un distribuidor puede traer una
# `commission_rate` explícita que MANDA sobre la del nivel (Master fundador 40%,
# élite 45% que otorga Christian a mano).
TIER_RATES = {
    'junior': 0.225,
    'senior': 0.26,
    'master': 0.30,
}
DEFAULT_TIER = 'junior'

# Sobrecomisión fija por cada nivel de arriba, y cuántos niveles suben.
OVERRIDE_RATE = 0.035
MAX_OVERRIDE_LEVELS = 2

# Tope duro: ninguna tajada individual pasa de esto (coincide con COMMISSION_CAP
# del servidor). Master élite = 0.45; dejamos 0.50 como techo absoluto.
HARD_CAP = 0.50


# Ascensos: ventas PROPIAS acumuladas para calificar al siguiente nivel. El
# ascenso lo APRUEBA Christian (§4ter); la barra solo muestra el avance. Son
# constantes fáciles de cambiar — valores de arranque, ajustables.
PROMOTE_TARGETS = {'junior': 100000, 'senior': 500000}
MASTER_STEP_SALES = 500000    # cada $500k de ventas netas → +0.5% de comisión
MASTER_STEP_RATE = 0.005
MASTER_MAX_RATE = 0.40        # el 45% élite lo otorga Christian a mano, fuera de la barra


def tier_rate(tier):
    """Tasa base de un nivel dado (junior por defecto si no se reconoce)."""
    return TIER_RATES.get(tier or DEFAULT_TIER, TIER_RATES[DEFAULT_TIER])


def level_progress(tier, lifetime_sales, commission_rate=None):
    """Avance hacia el siguiente nivel. Junior/Senior: hacia el umbral de ventas
    que califica al ascenso. Master: hacia el próximo +0.5% de comisión (cada
    $500k) hasta el tope. Devuelve un dict listo para pintar la barra."""
    tier = tier if tier in TIER_RATES else DEFAULT_TIER
    sales = max(0.0, float(lifetime_sales or 0))
    if tier in PROMOTE_TARGETS:
        target = PROMOTE_TARGETS[tier]
        return {
            'current': tier,
            'next': 'senior' if tier == 'junior' else 'master',
            'kind': 'promotion',
            'target': target,
            'current_value': sales,
            'progress': min(1.0, sales / target) if target else 1.0,
            'remaining': max(0.0, target - sales),
        }
    # Master
    rate = commission_rate if commission_rate is not None else TIER_RATES['master']
    if rate >= MASTER_MAX_RATE:
        return {'current': 'master', 'next': None, 'kind': 'maxed', 'target': None,
                'current_value': sales, 'progress': 1.0, 'remaining': 0.0, 'rate': rate}
    into_block = sales % MASTER_STEP_SALES
    return {
        'current': 'master', 'next': None, 'kind': 'rate_step',
        'target': MASTER_STEP_SALES, 'current_value': into_block,
        'progress': into_block / MASTER_STEP_SALES,
        'remaining': MASTER_STEP_SALES - into_block,
        'rate': rate, 'next_rate': round(min(MASTER_MAX_RATE, rate + MASTER_STEP_RATE), 4),
    }


def seller_rate(dist):
    """Tasa del vendedor: su `commission_rate` explícita si la tiene (fundador/
    élite), si no la de su nivel. Acotada al tope duro."""
    r = dist.get('commission_rate')
    if r is None:
        r = tier_rate(dist.get('tier'))
    return max(0.0, min(HARD_CAP, float(r)))


def compute_commission_breakdown(paid_merchandise, seller, upline_chain=None):
    """Reparte la comisión de UNA venta hecha con el código de `seller`.

    - `paid_merchandise`: mercancía pagada (después de descuento y canje), en MXN.
    - `seller`: dict del distribuidor cuyo código se usó (con 'id', 'tier' y/o
      'commission_rate').
    - `upline_chain`: distribuidores por encima del vendedor, del más cercano al
      más lejano. Solo se usan los primeros MAX_OVERRIDE_LEVELS.

    Devuelve una lista de dicts: {distributor_id, role, rate, amount}. `role` es
    'seller' o 'override'. La suma es la comisión total de la orden.
    """
    if not seller or paid_merchandise <= 0:
        return []
    base = float(paid_merchandise)
    rate = seller_rate(seller)
    out = [{
        'distributor_id': seller['id'],
        'role': 'seller',
        'rate': rate,
        'amount': round(base * rate),
    }]
    seen = {seller['id']}
    for up in (upline_chain or [])[:MAX_OVERRIDE_LEVELS]:
        if not up or up.get('id') in seen:
            continue          # nunca pagar dos veces al mismo ni al propio vendedor
        seen.add(up['id'])
        out.append({
            'distributor_id': up['id'],
            'role': 'override',
            'rate': OVERRIDE_RATE,
            'amount': round(base * OVERRIDE_RATE),
        })
    return out


def seller_amount(breakdown):
    """La tajada del vendedor (para el campo `commission` de la orden, compat)."""
    for row in breakdown:
        if row['role'] == 'seller':
            return row['amount']
    return 0


def total_amount(breakdown):
    """Suma de todo lo repartido en la orden (vendedor + sobrecomisiones)."""
    return sum(row['amount'] for row in breakdown)


def earnings_for(distributor_id, orders):
    """Cuánto ganó un distribuidor en una lista de órdenes: suma su tajada en el
    `commissions` de cada orden (como vendedor O como upline). Ignora canceladas.

    Compatibilidad: si una orden vieja no trae `commissions` pero sí `commission`
    y `referred_by` == este distribuidor, cuenta ese `commission` (venta directa)."""
    total = 0
    for o in orders:
        if o.get('status') == 'cancelado':
            continue
        rows = o.get('commissions')
        if rows:
            total += sum(r.get('amount', 0) for r in rows if r.get('distributor_id') == distributor_id)
        elif o.get('referred_by') == distributor_id:
            total += o.get('commission', 0)
    return total
