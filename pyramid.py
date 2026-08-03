"""Esquema de distribuidores en pirámide — 6 niveles, override DIFERENCIAL.

Diseño cerrado con Christian (2026-07-22/23). Reglas:

  Niveles y tasa (la tasa ES la comisión Y el descuento MÁXIMO que puede dar el
  distribuidor a su cliente, de 0 hasta ese %). Desde el 2026-07-30 la entrada
  es 30% para todos (BASE_RATE) y de ahí se sube:
    junior0 30% · junior1 30% · senior 30% · master 35% · elite 40% · diamond 43%
  (la tabla TIER_RATES guarda la escalera original —junior0 20%, junior1 25%— y
  el piso se aplica en `tier_rate`: así se ve de un vistazo qué se movió.)

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
import cobrado

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
MANUAL_CAP = 0.50  # tope de la tasa que el admin puede poner A MANO (= COMMISSION_CAP)

# ⛔ TASA BASE DEL CANAL — 30%. Decisión de Christián, 2026-07-30:
# «Todos los distribuidores van a empezar a partir de ahora a recibir un 30% de
# comisión (menos el % que hayan otorgado de descuento) y de ahí irán subiendo».
#
# Es un PISO, no un techo ni una tasa nueva: la escalera de arriba (master 35%,
# elite 40%, diamond 43%) queda intacta y los ascensos siguen igual. Lo único que
# cambia es de dónde se arranca: los escalones que valían menos de 30 (junior0 20%
# y junior1 25%) se reanclan en 30. Vive en UNA constante justo para que mover la
# base mañana sea un número, no una cirugía.
#
# ⚠️ Los dos escalones de abajo quedan empatados en 30: subir de junior0 a junior1
# ya no sube la comisión (sí el nivel y lo que se ve en el panel). Es la
# consecuencia directa de «todos empiezan en 30» sin tocar los niveles altos.
BASE_RATE = 0.30

# ⛔ TECHO DE LA ESCALERA VISIBLE — 35%. Decisión de Christián, 2026-08-03.
#
# POR QUÉ. Con la vara del ROI CON TODO adentro (comisión, cashback, flete de China,
# guía y empaque, gastos fijos y la comisión de la pasarela) 63 de 188 productos no
# llegaban al piso de 5×. Se midieron tres salidas: bajar el cashback de 3% a 1%
# rescataba 2 productos, topar la comisión en 35% rescataba 5, y las dos juntas 11.
# Eligió el techo y dejó el cashback en 3%: el cashback lo ve TODO cliente y
# recortarlo se sentía en toda la tienda para rescatar dos renglones.
#
# ⛔ Y LO QUE HACE QUE ESTO NO SEA UN RECORTE A SECAS (misma fecha): Elite y Diamond
# NO desaparecen — se vuelven **niveles secretos desbloqueables**, como ya lo era el
# Diamond. Lo que el distribuidor VE al entrar tope en Master 35%; arriba de eso hay
# dos escalones que no se anuncian y que Christián otorga. Un techo que todos ven es
# un límite; un techo con dos puertas escondidas detrás es una meta.
TECHO_VISIBLE = 0.35

# Los niveles que NO se anuncian en la escalera y que sí cobran su tasa completa.
# El Diamond ya era secreto desde el 2026-07-23; el Elite se suma hoy.
NIVELES_SECRETOS = frozenset({'elite', 'diamond'})

# Y una excepción por persona, por orden expresa de Christián: María Neunfeld.
# Se identifica por CORREO y no por nombre, porque el nombre se teclea distinto
# («María  Neunfeld», «maria neunfeld») y un tope de comisión no puede depender de
# cómo alguien escribió un acento. Si mañana hay más, se añaden AQUÍ y no se
# dispersan por el código: una lista corta y a la vista es lo que permite auditar
# quién cobra por encima del techo.
SIN_TECHO = frozenset({'marianeunfeld0@gmail.com'})


def exenta_del_techo(dist):
    """¿Este distribuidor cobra por encima del techo visible?

    Dos caminos: haber desbloqueado un nivel secreto (Elite o Diamond), o estar en
    la lista de excepciones por persona.
    """
    d = dist or {}
    if normalize_tier(d.get('tier')) in NIVELES_SECRETOS:
        return True
    return str(d.get('email') or '').strip().lower() in SIN_TECHO

# Alias de niveles viejos guardados en la base antes de la pirámide de 6 niveles.
TIER_ALIASES = {'junior': 'junior0'}


def normalize_tier(tier):
    """Nivel guardado -> nivel válido de la pirámide (junior0 si no se reconoce)."""
    tier = TIER_ALIASES.get(tier, tier)
    return tier if tier in TIER_RATES else DEFAULT_TIER


def effective_rate(dist):
    """Tasa REAL de un distribuidor: la MAYOR entre la de su nivel y la que el
    admin le puso a mano (`commission_rate`).

    Christian puede subirle la comisión a alguien sin moverlo de nivel. Esa tasa
    manual manda: es su comisión Y su descuento máximo, y de ahí salen sus códigos
    automáticos. Tope: MANUAL_CAP (el mismo que valida el panel de admin).

    El piso de 30% (BASE_RATE) entra por `tier_rate`, así que una manual por
    DEBAJO de 30 ya no baja a nadie: el 2026-07-30 las manuales viejas de María,
    Alanís y Javier se reanclaron en 30 (ver `reanclar_comisiones_en_la_base` en
    server.py, que guarda los valores anteriores para poder revertir)."""
    tier_r = tier_rate(normalize_tier((dist or {}).get('tier')))
    manual = float((dist or {}).get('commission_rate') or 0)
    bruta = min(MANUAL_CAP, max(tier_r, manual))
    # El techo de la escalera VISIBLE (35%) se aplica al final, para que sea el
    # último filtro. Se lo saltan quien desbloqueó un nivel secreto (Elite,
    # Diamond) y las excepciones por persona.
    return bruta if exenta_del_techo(dist) else min(TECHO_VISIBLE, bruta)

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
    """Tasa (comisión = descuento máximo) de un nivel. junior0 por defecto.

    Nunca por debajo de BASE_RATE: desde el 2026-07-30 todo distribuidor arranca
    en 30% y de ahí sube. Los niveles por encima de la base salen tal cual de la
    tabla."""
    tabla = TIER_RATES.get(TIER_ALIASES.get(tier, tier) or DEFAULT_TIER, TIER_RATES[DEFAULT_TIER])
    return max(BASE_RATE, tabla)


def max_discount(tier):
    """Descuento máximo que ese nivel puede dar a su cliente = su comisión."""
    return tier_rate(tier)


# Descuentos que el sistema le ofrece a cada nivel (códigos auto-generados):
# empiezan en 15% (= tope de la página) y suben de 5% en 5% hasta 5% DEBAJO de
# su comisión. Diamond (43%) termina en 38%. Christian, 2026-07-23.
DISCOUNT_FLOOR = 15   # %


def discount_tiers_for(commission_rate):
    """Lista de descuentos (fracciones) disponibles para una comisión dada.
    Ej: 0.30 (Senior) → [0.15, 0.20, 0.25]; 0.43 (Diamond) → [0.15..0.35, 0.38]."""
    cap = round((commission_rate or 0) * 100) - 5
    if cap < DISCOUNT_FLOOR:
        return [DISCOUNT_FLOOR / 100] if cap >= 0 else []
    tiers = list(range(DISCOUNT_FLOOR, cap + 1, 5))
    if cap not in tiers:          # Diamond: 43-5=38 no es múltiplo de 5
        tiers.append(cap)
    return [t / 100 for t in tiers]


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
    s_rate = effective_rate(seller)
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
        u_rate = effective_rate(up)
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
    `commission` si la orden es anterior a la pirámide y fue su venta directa.

    ⛔ SIN COBRAR NO HAY COMISIÓN QUE PAGAR (Christián, 2026-07-29). Antes bastaba con
    que la orden no estuviera cancelada, así que una venta ENTREGADA Y FIADA generaba
    una comisión pagable con dinero que la casa todavía no tiene. La comisión sale del
    cobro, no de la entrega: en cuanto el pedido se marca pagado, aparece.
    """
    total = 0
    for o in orders:
        if not cobrado.esta_pagado(o):
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


def cap_breakdown(rows, merchandise, cap_rate):
    """Escala TODO el reparto para que la suma no pase de cap_rate*merchandise.

    Regla de Christian (2026-07-23): cada producto aguanta una comisión máxima
    (escalera por ROI, guardada en el producto). Si la estructura de pirámide
    pide más, todos los participantes se reparten el tope a prorrata."""
    if not rows or merchandise <= 0:
        return rows
    tope = max(0.0, float(cap_rate)) * float(merchandise)
    total = sum(r.get('amount', 0) for r in rows)
    if total <= tope or total <= 0:
        return rows
    escala = tope / total
    out = []
    for r in rows:
        r = dict(r)
        r['amount'] = round(r['amount'] * escala)
        r['capped'] = True
        out.append(r)
    return out


def prorratear_por_dinero(rows, paid_merchandise, after_discount):
    """Escala el reparto a la fracción de la mercancía que se pagó EN DINERO.

    La misma regla del canje al 100% (Christian, 2026-07-28) pero sin el escalón:
    de mercancía cobrada en puntos no se paga comisión. Con puntos pagando el 99%
    y $1 en efectivo, la comisión salía completa sobre dinero que nunca entró —
    el mismo agujero del canje total, repartido en dos pedidos. La comisión se
    prorratea por lo que sí entró; sin puntos la fracción es 1 y no cambia nada.
    Los renglones que quedan en $0 se quitan: una comisión de cero no se reporta."""
    if not rows or after_discount <= 0:
        return rows
    fraccion = max(0.0, min(1.0, float(paid_merchandise) / float(after_discount)))
    if fraccion >= 1.0:
        return rows
    out = []
    for r in rows:
        r = dict(r)
        r['amount'] = round(r['amount'] * fraccion)
        if r['amount'] > 0:
            out.append(r)
    return out


def discount_tiers_de(dist):
    """Los niveles de descuento que ESTE distribuidor puede otorgar.

    Normalmente = discount_tiers_for(su comisión), que topa 5% abajo de ella.
    Christián puede autorizar a UNA persona un tope personal mayor con
    `max_discount_override` en su cuenta (María al 30%, 2026-07-30): se
    agregan los escalones que falten, nunca arriba de su propia comisión —
    otorgar el tope completo significa que esa venta le deja 0 de comisión."""
    base = discount_tiers_for(effective_rate(dist))
    extra = float((dist or {}).get('max_discount_override') or 0)
    if not extra:
        return base
    tope = round(min(extra, effective_rate(dist)), 4)
    tiers = {round(t, 4) for t in base}
    tiers |= {round(x / 100, 4) for x in range(DISCOUNT_FLOOR, int(round(tope * 100)) + 1, 5)}
    tiers.add(tope)
    return sorted(t for t in tiers if t <= tope + 1e-9)
