"""Panel de anuncios de Meta dentro del Admin de Exygen.

DOS fuentes, misma salida — el panel no cambia cuando se cambie de una a la otra:

  1. CSV del Administrador de Anuncios (lo que hay HOY). Christian programa el
     reporte diario por correo y sube el archivo, o lo pega. Cero permisos, cero
     token, cero pelear con Meta.
  2. API de Marketing EN VIVO (cuando Christian consiga el token). Basta con poner
     META_TOKEN y META_AD_ACCOUNT en el entorno: `fetch_live()` devuelve las mismas
     filas y el panel ni se entera.

Meta bloqueó a Christian para crear la app de desarrollador ("dispositivo que no
usas habitualmente"), por eso el CSV va primero. Ver el handoff del 2026-07-25.

Módulo PURO salvo `fetch_live` (que sí sale a internet): recibe texto, devuelve
dicts. Así se puede probar sin Mongo y sin red.
"""
import csv
import io
import os
import re

# Las columnas del CSV en español (Administrador de Anuncios en es-MX) y en inglés,
# porque Christian puede exportar en cualquiera de los dos idiomas.
COLUMNS = {
    'campaign': ['nombre de la campaña', 'campaign name'],
    'status': ['entrega de la campaña', 'campaign delivery', 'delivery'],
    'results': ['resultados', 'results'],
    'result_type': ['indicador de resultado', 'result indicator'],
    'cost_per_result': ['costo por resultados', 'cost per results', 'cost per result'],
    'budget': ['presupuesto del conjunto de anuncios', 'ad set budget'],
    'budget_type': ['tipo de presupuesto del conjunto de anuncios', 'ad set budget type'],
    'spend': ['importe gastado (usd)', 'importe gastado (mxn)', 'importe gastado',
              'amount spent (usd)', 'amount spent (mxn)', 'amount spent'],
    'impressions': ['impresiones', 'impressions'],
    'reach': ['alcance', 'reach'],
    'clicks': ['clics en el enlace', 'link clicks', 'clics', 'clicks'],
    'cpc': ['cpc (costo por clic en el enlace)', 'cpc (cost per link click)', 'cpc'],
    'purchases': ['compras', 'purchases', 'compras en el sitio web', 'website purchases'],
    'purchase_value': ['valor de conversión de compras', 'purchases conversion value'],
    'date_start': ['inicio del informe', 'reporting starts'],
    'date_end': ['fin del informe', 'reporting ends'],
}

# Moneda: el CSV la trae en el nombre de la columna del gasto.
def _currency(headers):
    for h in headers:
        m = re.search(r'\((usd|mxn|eur)\)', (h or '').lower())
        if m:
            return m.group(1).upper()
    return 'USD'


def _num(v):
    """'1,234.56' / '' / None -> float. Nunca revienta."""
    if v is None:
        return 0.0
    s = str(v).strip().replace(',', '').replace('$', '')
    if not s or s in ('-', '--'):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _index(headers):
    """Mapa campo -> nombre real de la columna, sin importar idioma ni mayúsculas."""
    low = {(h or '').strip().lower(): h for h in headers}
    out = {}
    for field, names in COLUMNS.items():
        for n in names:
            if n in low:
                out[field] = low[n]
                break
    return out


def parse_csv(text):
    """CSV del Administrador de Anuncios -> lista de campañas normalizadas.

    Tolera columnas que falten (Meta deja fuera las que no pediste) y filas vacías.
    Devuelve [] si el archivo no trae la columna del nombre de campaña."""
    if not text or not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿')))
    headers = reader.fieldnames or []
    idx = _index(headers)
    if 'campaign' not in idx:
        return []
    cur = _currency(headers)
    rows = []
    for raw in reader:
        name = (raw.get(idx['campaign']) or '').strip()
        if not name:
            continue
        g = lambda f: raw.get(idx[f]) if f in idx else None   # noqa: E731
        spend = _num(g('spend'))
        clicks = _num(g('clicks'))
        results = _num(g('results'))
        rtype = (g('result_type') or '').strip()
        # Meta pone los clics en 'Resultados' cuando la campaña optimiza por clic
        # y no manda una columna de clics aparte. No perdamos ese dato.
        if not clicks and 'link_click' in rtype:
            clicks = results
        rows.append({
            'campaign': name,
            'status': (g('status') or '').strip().lower(),
            'currency': cur,
            'spend': round(spend, 2),
            'impressions': int(_num(g('impressions'))),
            'reach': int(_num(g('reach'))),
            'clicks': int(clicks),
            'results': results,
            'result_type': rtype,
            'cpc': round(spend / clicks, 4) if clicks else 0.0,
            'purchases': int(_num(g('purchases'))),
            'purchase_value': round(_num(g('purchase_value')), 2),
            'budget': _num(g('budget')),
            'budget_type': (g('budget_type') or '').strip(),
            'date_start': (g('date_start') or '').strip(),
            'date_end': (g('date_end') or '').strip(),
        })
    return rows


def summarize(rows):
    """Los totales que manda el panel."""
    spend = sum(r['spend'] for r in rows)
    clicks = sum(r['clicks'] for r in rows)
    impressions = sum(r['impressions'] for r in rows)
    purchases = sum(r['purchases'] for r in rows)
    value = sum(r['purchase_value'] for r in rows)
    return {
        'campaigns': len(rows),
        'active': sum(1 for r in rows if r['status'] == 'active'),
        'currency': rows[0]['currency'] if rows else 'USD',
        'spend': round(spend, 2),
        'impressions': impressions,
        'reach': sum(r['reach'] for r in rows),
        'clicks': clicks,
        'cpc': round(spend / clicks, 4) if clicks else 0.0,
        'cpm': round(spend / impressions * 1000, 2) if impressions else 0.0,
        'purchases': purchases,
        'purchase_value': round(value, 2),
        'cpa': round(spend / purchases, 2) if purchases else 0.0,
        'roas': round(value / spend, 2) if spend else 0.0,
        'date_start': min((r['date_start'] for r in rows if r['date_start']), default=''),
        'date_end': max((r['date_end'] for r in rows if r['date_end']), default=''),
    }


# ---- Lo que el panel le DICE a Christian (la parte que sirve para decidir) ----
# Umbrales de la propia documentación de Meta y de lo acordado con Christian el
# 2026-07-25: sin ~50 conversiones por semana un conjunto no sale de aprendizaje.
LEARNING_WEEKLY_CONVERSIONS = 50
MIN_CLICKS_TO_JUDGE = 100    # con menos clics, el dato todavía no dice nada


def advise(summary, site_visits=0, site_orders=0, site_revenue=0.0, fx=18.0):
    """Recomendaciones en español claro. `fx` = pesos por dólar (el gasto de Meta
    viene en USD y las ventas del sitio en MXN).

    Devuelve [{level, title, body}] — level: 'alto' | 'medio' | 'ok'."""
    out = []
    spend_mxn = summary['spend'] * (fx if summary['currency'] == 'USD' else 1)
    clicks = summary['clicks']

    if summary['spend'] <= 0:
        out.append({'level': 'alto', 'title': 'No hay gasto registrado',
                    'body': 'Sin dinero invertido no hay nada que medir. Sube el reporte '
                            'más reciente o revisa que la campaña esté activa.'})
        return out

    # 1. ¿Alcanza para que Meta aprenda?
    daily = summary['spend'] / max(1, _days(summary))
    if summary['purchases'] < LEARNING_WEEKLY_CONVERSIONS:
        out.append({'level': 'alto', 'title': 'Meta todavía no aprende',
                    'body': f'Necesita ~{LEARNING_WEEKLY_CONVERSIONS} conversiones por semana para '
                            f'salir de la fase de aprendizaje y hoy lleva {summary["purchases"]}. '
                            f'Con ${daily:.2f} al día nunca las va a juntar optimizando por COMPRA. '
                            'Optimiza por "Agregar al carrito" (pasa mucho más seguido) y déjalo '
                            'correr 14 días sin tocarlo.'})

    # 2. ¿El clic es caro o barato?
    if clicks >= MIN_CLICKS_TO_JUDGE:
        cpc_mxn = summary['cpc'] * (fx if summary['currency'] == 'USD' else 1)
        if summary['cpc'] <= 0.10:
            out.append({'level': 'ok', 'title': 'El clic te sale baratísimo',
                        'body': f'${summary["cpc"]:.3f} {summary["currency"]} por clic (~${cpc_mxn:.2f} MXN). '
                                'El problema NO son los anuncios: la gente sí llega. '
                                'La palanca está en el sitio, no en el presupuesto.'})
        elif summary['cpc'] > 1.0:
            out.append({'level': 'medio', 'title': 'El clic está caro',
                        'body': f'${summary["cpc"]:.2f} {summary["currency"]} por clic. Prueba otro '
                                'público u otro texto antes de subir presupuesto.'})
    else:
        out.append({'level': 'medio', 'title': 'Todavía no hay datos suficientes',
                    'body': f'Con {clicks} clics cualquier conclusión es adivinanza. '
                            f'Hacen falta al menos {MIN_CLICKS_TO_JUDGE}.'})

    # 3. Lo que de verdad importa: clics que llegan vs. clics que compran.
    if clicks >= MIN_CLICKS_TO_JUDGE:
        if site_orders == 0:
            out.append({'level': 'alto', 'title': f'{clicks} clics y ninguna venta',
                        'body': 'Llega gente y no compra. Antes de gastar un peso más, revisa el '
                                'Embudo: dónde se caen (producto, carrito o checkout). Subir la '
                                'conversión de 1% a 3% triplica las ventas con el MISMO dinero.'})
        else:
            conv = site_orders / clicks * 100
            out.append({'level': 'ok' if conv >= 1 else 'medio',
                        'title': f'Convierten {conv:.1f} de cada 100 clics',
                        'body': f'{site_orders} ventas de {clicks} clics. '
                                f'Costo por venta: ${spend_mxn / site_orders:,.0f} MXN.'})

    # 4. ¿Se recupera el dinero? OJO: solo se puede afirmar cuando Meta ATRIBUYE la
    # compra al anuncio. Si no, lo que vendió el sitio pudo venir de WhatsApp, de
    # boca en boca o de una venta directa — decir "recuperas $39 por peso" con eso
    # sería mentirle a Christian y hacer que suba presupuesto por una señal falsa.
    if summary['purchases'] > 0 and spend_mxn > 0:
        val_mxn = summary['purchase_value'] * (fx if summary['currency'] == 'USD' else 1)
        roas = val_mxn / spend_mxn
        out.append({'level': 'ok' if roas >= 2 else 'alto',
                    'title': f'Por cada peso que metes, recuperas ${roas:.2f}',
                    'body': f'{summary["purchases"]} compras que Meta atribuye a los anuncios: '
                            f'${val_mxn:,.0f} MXN contra ${spend_mxn:,.0f} MXN gastados. '
                            + ('Redituable: aquí sí tiene sentido subir presupuesto.' if roas >= 2
                               else 'Todavía no es redituable. No subas el presupuesto aún.')})
    elif site_revenue > 0:
        out.append({'level': 'medio', 'title': 'Estas ventas NO vienen de los anuncios',
                    'body': f'El sitio vendió ${site_revenue:,.0f} MXN en el periodo, pero Meta no '
                            'atribuye ninguna de esas compras a un anuncio. Pudieron llegar por '
                            'WhatsApp, de boca en boca o venta directa. NO subas el presupuesto con '
                            'este dato: primero hay que confirmar que el píxel esté registrando '
                            'compras, si no seguimos a ciegas.'})

    # 5. Campañas que se están comiendo el dinero sin dar nada.
    return out


def _days(summary):
    """Días que cubre el reporte (para el gasto diario). 1 si no se puede saber."""
    a, b = summary.get('date_start'), summary.get('date_end')
    if not a or not b:
        return 1
    try:
        from datetime import date
        d1 = date(*[int(x) for x in a.split('-')])
        d2 = date(*[int(x) for x in b.split('-')])
        return max(1, (d2 - d1).days + 1)
    except (ValueError, TypeError):
        return 1


def dead_weight(rows, min_spend=1.0):
    """Campañas activas que gastan y no producen: candidatas a apagar."""
    out = []
    for r in rows:
        if r['status'] != 'active' or r['spend'] < min_spend:
            continue
        if r['clicks'] == 0 and r['purchases'] == 0:
            out.append({'campaign': r['campaign'], 'spend': r['spend'],
                        'razon': 'gasta y no genera ni un clic'})
        elif r['impressions'] < 100:
            out.append({'campaign': r['campaign'], 'spend': r['spend'],
                        'razon': f'casi no se muestra ({r["impressions"]} impresiones)'})
    return out


# ---------------- Fuente 2: API de Marketing EN VIVO ----------------
GRAPH = 'https://graph.facebook.com/v21.0'


def live_configured():
    return bool(os.environ.get('META_TOKEN') and os.environ.get('META_AD_ACCOUNT'))


async def fetch_live(days=30):
    """Trae las mismas filas que `parse_csv`, pero de la API de Meta.

    Solo lectura: usa `ads_read`/`read_insights`. Si no hay token, devuelve [].
    Cuando Christian consiga el token, esto es lo único que se enciende."""
    if not live_configured():
        return []
    import httpx
    acct = os.environ['META_AD_ACCOUNT']
    if not acct.startswith('act_'):
        acct = 'act_' + acct
    params = {
        'access_token': os.environ['META_TOKEN'],
        'level': 'campaign',
        'date_preset': 'last_30d' if days > 7 else 'last_7d',
        'fields': ('campaign_name,spend,impressions,reach,clicks,cpc,'
                   'actions,action_values,account_currency'),
        'limit': 200,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f'{GRAPH}/{acct}/insights', params=params)
        r.raise_for_status()
        data = r.json().get('data', [])
    rows = []
    for d in data:
        acts = {a['action_type']: _num(a.get('value')) for a in d.get('actions', [])}
        vals = {a['action_type']: _num(a.get('value')) for a in d.get('action_values', [])}
        spend = _num(d.get('spend'))
        clicks = int(_num(d.get('clicks')))
        rows.append({
            'campaign': d.get('campaign_name', ''),
            'status': 'active',
            'currency': d.get('account_currency', 'USD'),
            'spend': round(spend, 2),
            'impressions': int(_num(d.get('impressions'))),
            'reach': int(_num(d.get('reach'))),
            'clicks': clicks,
            'results': acts.get('link_click', clicks),
            'result_type': 'actions:link_click',
            'cpc': round(spend / clicks, 4) if clicks else 0.0,
            'purchases': int(acts.get('purchase', 0)),
            'purchase_value': round(vals.get('purchase', 0.0), 2),
            'budget': 0.0,
            'budget_type': '',
            'date_start': d.get('date_start', ''),
            'date_end': d.get('date_stop', ''),
        })
    return rows
