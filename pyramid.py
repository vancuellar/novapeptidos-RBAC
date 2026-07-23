"""Esquema de distribuidores en pirámide — 6 niveles, override DIFERENCIAL.

Diseño cerrado con Christian (2026-07-22/23). Reglas:

  Niveles y tasa (la tasa ES la comisión Y el descuento MÁXIMO que puede dar el
  distribuidor a su cliente, de 0 hasta ese %):
    junior0 20% · junior1 25% · senior 30% · master 35% · elite 40% · diamond 45%

  Override DIFERENCIAL: en una venta, cada distribuidor por ENCIMA del vendedor
  gana la DIFERENCIA entre su tasa y la más alta ya pagada debajo de él. Así el
  total repartido = la tasa del nivel MÁS ALTO de la cadena (nunca más), y si un
  nivel se salta, el de arriba absorbe la diferencia. El total nunca pasa del 45%.

  El DESCUENTO al cliente sale SOLO de la tajada del VENDEDOR: si un Senior (30%)
  da 15% de descuento, se queda 15; los de arriba cobran su diferencial intacto.

  Ascensos: se necesitan VENTAS (acumuladas) Y RECLUTAS ACTIVOS (con ≥1 venta) en
  la red. El ascenso lo APRUEBA Christian al llegar a la meta; la barra muestra el
  avance de las dos cosas. Diamond es a mano (invitación).

Módulo PURO: recibe dicts, devuelve el reparto en pesos, bloqueado al crear la
orden. Los reportes suman lo guardado → cambiar tasas/niveles no toca ventas viejas.
"""

# Niveles en orden y su tasa (comisión = descuento máximo).
TIER_ORDER = ['junior0', 'junior1', 'senior', 'master', 'elite', 'diamond']
TIER_RATES = {
    'junior0': 0.20,
    'junior1': 0.25,
    'senior': 0.30,
    'master': 0.35,
    'elite': 0.40,
    'diamond': 0.43,   # nivel SECRETO desbloqueable (no aparece en la escalera)
}
DEFAULT_TIER = 'junior0'
HARD_CAP = 0.45   # ningún reparto individual ni total pasa de aquí

# Diamond es un nivel SECRETO: no está en la escalera visible (el tope que ven los
# distribuidores es Elite 40%). Se desbloquea al llegar a estas metas; el sistema
# avisa al admin y Christian lo otorga a mano. Christian, 2026-07-23.
DIAMOND_SALES = 50000000      # $50M de ventas de EQUIPO
DIAMOND_RECRUITS = 32         # MÁS de 32 activos en la red (estricto)


def diamond_qualifies(team_sales, active_recruits):
    """¿Este distribuidor (Elite) ya desbloqueó el Diamond secreto?"""
    return (team_sales or 0) >= DIAMOND_SALES and (active_recruits or 0) > DIAMOND_RECRUITS

# Ascensos: (nivel_origen -> nivel_destino, meta de ventas, base, reclutas activos).
# 'personal' = ventas propias; 'team' = ventas de toda su red (él + downline).
# Diamond es a mano: se lista pero no asciende solo.
# Escalera VISIBLE (Diamond NO está aquí: es secreto). El tope que ve un
# distribuidor es Elite. Diamond se desbloquea por separado (diamond_qualifies).
LEVEL_STEPS = [
    {'from': 'junior0', 'to': 'junior1', 'sales': 500000,    'basis': 'personal', 'recruits': 2,  'manual': False},
    {'from': 'junior1', 'to': 'senior',  'sales': 3000000,   'basis': 'personal', 'recruits': 4,  'manual': False},
    {'from': 'senior',  'to': 'master',  'sales': 10000000,  'basis': 'team',     'recruits': 8,  'manual': False},
    {'from': 'master',  'to': 'elite',   'sales': 30000000,  'basis': 'team',     'recruits': 16, 'manual': False},
]
CASHBACK_RATE = 0.04   # ventaja del canal, la paga Christian, FUERA de la bolsa


def tier_rate(tier):
    """Tasa (comisión = descuento máximo) de un nivel. junior0 por defecto."""
    return TIER_RATES.get(tier or DEFAULT_TIER, TIER_RATES[DEFAULT_TIER])


def max_discount(tier):
    """Descuento máximo que ese nivel puede dar a su cliente = su comisión."""
    return tier_rate(tier)


def compute_commission_breakdown(merchandise, seller, upline_chain=None, discount_rate=0.0):
    """Reparte UNA venta hecha con el código de `seller`, sobre `merchandise` (MXN).

    - El vendedor gana (su tasa − descuento que dio), sobre la mercancía.
    - Cada upline gana la DIFERENCIA entre su tasa y la más alta ya pagada debajo.
    - `discount_rate` es lo que el vendedor decidió dar al cliente (0..su tasa);
      sale de SU tajada, no toca a los de arriba.

    Devuelve [{distributor_id, role, rate, amount(MXN), ...}]. La suma es la
    comisión total de la orden (sin el cashback, que va aparte)."""
    if not seller or merchandise <= 0:
        return []
    base = float(merchandise)
    s_rate = tier_rate(seller.get('tier'))
    disc = max(0.0, min(s_rate, float(discount_rate or 0)))
    rows = [{
        'distributor_id': seller['id'], 'role': 'seller', 'rate': s_rate,
        'discount': round(disc, 4), 'amount': round(base * (s_rate - disc)),
    }]
    seen = {seller['id']}
    highest = s_rate   # la tasa más alta ya cubierta debajo del upline en turno
    for up in (upline_chain or []):
        if not up or up.get('id') in seen:
            continue
        u_rate = tier_rate(up.get('tier'))
        diff = u_rate - highest
        if diff <= 0:
            continue   # no está más arriba que lo ya pagado: no cobra, seguimos
        rows.append({
            'distributor_id': up['id'], 'role': 'override', 'rate': u_rate,
            'diff': round(diff, 4), 'amount': round(base * diff),
        })
        seen.add(up['id'])
        highest = u_rate
    return rows


def seller_amount(breakdown):
    """La tajada del vendedor (ya con su descuento restado), para el campo
    `commission` de la orden (compatibilidad)."""
    for row in breakdown:
        if row['role'] == 'seller':
            return row['amount']
    return 0


def total_amount(breakdown):
    """Suma de todo lo repartido (vendedor + sobrecomisiones), sin cashback."""
    return sum(row['amount'] for row in breakdown)


def earnings_for(distributor_id, orders):
    """Cuánto ganó un distribuidor: su tajada en el `commissions` de cada orden
    (como vendedor O como upline), ignorando canceladas. Cae al campo viejo
    `commission` si la orden es anterior a la pirámide y fue su venta directa."""
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


def _step_from(tier):
    tier = tier if tier in TIER_RATES else DEFAULT_TIER
    for s in LEVEL_STEPS:
        if s['from'] == tier:
            return s
    return None   # diamond: no hay siguiente


def _bar(value, target):
    value = max(0.0, float(value or 0))
    target = float(target or 0)
    return {
        'value': value, 'target': target,
        'progress': min(1.0, value / target) if target else 1.0,
        'remaining': max(0.0, target - value),
        'done': value >= target,
    }


def level_progress(tier, personal_sales, team_sales, active_recruits):
    """Avance hacia el siguiente nivel: DOS metas, ventas y reclutas activos.
    Devuelve dict con las dos barras y si califica (las dos cumplidas)."""
    tier = tier if tier in TIER_RATES else DEFAULT_TIER
    step = _step_from(tier)
    if step is None:
        return {'current': tier, 'next': None, 'kind': 'top', 'rate': tier_rate(tier),
                'sales': None, 'recruits': None, 'qualifies': False, 'manual': False}
    sales_value = personal_sales if step['basis'] == 'personal' else team_sales
    sales = _bar(sales_value, step['sales'])
    recruits = _bar(active_recruits, step['recruits'])
    return {
        'current': tier, 'next': step['to'], 'kind': 'promotion',
        'rate': tier_rate(tier), 'next_rate': tier_rate(step['to']),
        'sales': {**sales, 'basis': step['basis']},
        'recruits': recruits,
        'qualifies': sales['done'] and recruits['done'],
        'manual': step['manual'],   # diamond requiere aprobación a mano
    }
