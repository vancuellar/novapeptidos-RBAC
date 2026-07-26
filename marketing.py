"""Cruce entre lo que Meta COBRA y lo que el sitio de verdad VENDE.

El panel de anuncios sabía dos cosas por separado: cuánto se gastó (Meta) y
cuánto se vendió (el sitio). Nunca las unía, así que la pregunta que de verdad
importa — **cuánto me costó cada cliente que sí compró** — no se podía contestar.

Aquí se une, y con tres reglas que evitan mentiras cómodas:

1. **Solo cuentan los CLIENTES NUEVOS.** Si alguien que ya compraba vuelve a
   comprar, esa venta no es un cliente que el anuncio haya conseguido. Contarla
   abarata el costo artificialmente y termina justificando gasto en campañas que
   no traen gente nueva.

2. **Lo que no se puede atribuir NO se reparte.** Un pedido que llegó de Meta sin
   etiqueta se va a su propia cubeta ("sin etiquetar") y se muestra aparte.
   Repartirlo entre las campañas haría que todas se vean mejor de lo que son.

3. **Con pocos datos no se juzga.** Debajo de cierto gasto o cierto número de
   clics, el resultado es ruido; se dice "todavía no alcanza", no "va mal".

Nota sobre monedas: Meta cobra en la moneda de la cuenta (normalmente USD) y el
sitio vende en pesos. Todo lo comparable se convierte a MXN con `fx`.
"""
import re
import unicodedata

# Debajo de esto, el número todavía no dice nada. Acordado con Christian el
# 2026-07-26: mejor decir "falta datos" que mandarlo a apagar algo que apenas
# arrancó (Meta ni siquiera sale de aprendizaje con tan poco).
MIN_GASTO_PARA_JUZGAR_MXN = 500.0
MIN_CLICS_PARA_JUZGAR = 50

# Cubetas propias: nunca se mezclan con una campaña real.
SIN_ETIQUETAR = 'sin etiquetar'


def slug(texto: str) -> str:
    """Nombre comparable: sin acentos, minúsculas, con guiones.

    Sirve para que "Retatrutida — Julio 2026" (como se llama la campaña en Meta)
    empate con `utm_campaign=retatrutida-julio-2026` (como viene en el enlace).
    Si no se normalizan los dos lados, jamás cruzan y todo cae en "sin etiquetar".
    """
    t = unicodedata.normalize('NFKD', str(texto or ''))
    t = ''.join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r'[^a-zA-Z0-9]+', '-', t).strip('-').lower()
    return t


def es_de_meta(attr: dict) -> bool:
    """¿Este pedido llegó por Facebook/Instagram?

    `fbclid` es la prueba dura: Meta se lo pega a los enlaces de sus anuncios
    aunque nadie los haya etiquetado. Si no está, se recurre al utm y, en último
    caso, al sitio de donde venía.
    """
    attr = attr or {}
    if attr.get('fbclid'):
        return True
    src = slug(attr.get('utm_source'))
    if src in ('facebook', 'instagram', 'meta', 'ig', 'fb'):
        return True
    ref = str(attr.get('referrer') or '').lower()
    return any(d in ref for d in ('facebook.', 'fb.', 'instagram.', 'l.facebook', 'lm.facebook'))


def campana_del_pedido(attr: dict) -> str:
    """A qué campaña pertenece un pedido. '' si no vino de Meta."""
    attr = attr or {}
    if not es_de_meta(attr):
        return ''
    return slug(attr.get('utm_campaign')) or SIN_ETIQUETAR


def _dividir(a, b):
    """División que devuelve None en vez de 0 cuando no hay denominador.

    Importa: un CAC de 0 se lee como "gratis" y un CAC nulo se lee como "todavía
    no trae clientes". Son cosas muy distintas y confundirlas cuesta dinero.
    """
    return round(a / b, 2) if b else None


def cruzar(filas_meta, pedidos, sesiones_por_campana=None, fx=18.0):
    """El reporte por campaña.

    - `filas_meta`: lo que devuelve `meta_ads.fetch_live()` / `parse_csv()`.
    - `pedidos`: pedidos del periodo, ya filtrados (nada cancelado), cada uno con
      `attribution`, `first_order` y `total`.
    - `sesiones_por_campana`: {slug: nº de sesiones únicas}, del lado del sitio.
    - `fx`: pesos por dólar.
    """
    sesiones_por_campana = sesiones_por_campana or {}

    # --- lado del sitio: qué vendió cada campaña ---
    por_campana = {}
    for p in pedidos:
        c = campana_del_pedido(p.get('attribution'))
        if not c:
            continue
        d = por_campana.setdefault(c, {'pedidos': 0, 'clientes_nuevos': 0, 'ingreso': 0.0})
        d['pedidos'] += 1
        d['ingreso'] += float(p.get('total') or 0)
        if p.get('first_order'):
            d['clientes_nuevos'] += 1

    # --- lado de Meta: qué cobró cada campaña ---
    salida = []
    vistos = set()
    for r in filas_meta:
        nombre = r.get('campaign', '')
        c = slug(nombre)
        vistos.add(c)
        gasto_mxn = float(r.get('spend') or 0) * (fx if r.get('currency') == 'USD' else 1)
        real = por_campana.get(c, {'pedidos': 0, 'clientes_nuevos': 0, 'ingreso': 0.0})
        clics = int(r.get('link_clicks') or 0)
        salida.append(_fila(nombre, c, r, gasto_mxn, real,
                            sesiones_por_campana.get(c, 0), clics))

    # --- campañas que vendieron pero que Meta no reportó ---
    # Pasa cuando la campaña ya se apagó pero sus clientes siguieron comprando, o
    # cuando el utm está mal escrito. Se muestran con gasto 0 para que se vean:
    # esconderlas haría desaparecer ingreso real del reporte.
    for c, real in por_campana.items():
        if c in vistos or c == SIN_ETIQUETAR:
            continue
        salida.append(_fila(c, c, {}, 0.0, real, sesiones_por_campana.get(c, 0), 0))

    salida.sort(key=lambda f: (-f['gasto_mxn'], -f['ingreso']))

    sin_etq = por_campana.get(SIN_ETIQUETAR, {'pedidos': 0, 'clientes_nuevos': 0, 'ingreso': 0.0})
    gasto_total = sum(f['gasto_mxn'] for f in salida)
    nuevos_total = sum(f['clientes_nuevos'] for f in salida) + sin_etq['clientes_nuevos']
    ingreso_total = sum(f['ingreso'] for f in salida) + sin_etq['ingreso']

    return {
        'campanas': salida,
        # Aparte y a la vista: es la medida de cuánta ceguera hay. Si esto es
        # grande, el problema no es el reporte, son los anuncios sin etiquetar.
        'sin_etiquetar': {**sin_etq, 'ingreso': round(sin_etq['ingreso'])},
        'total': {
            'gasto_mxn': round(gasto_total),
            'clientes_nuevos': nuevos_total,
            'pedidos': sum(f['pedidos'] for f in salida) + sin_etq['pedidos'],
            'ingreso': round(ingreso_total),
            # Las dos cifras que Christian pidió, sobre TODO el gasto:
            'cac': _dividir(gasto_total, nuevos_total),
            'roas': _dividir(ingreso_total, gasto_total),
        },
    }


def _fila(nombre, c, r, gasto_mxn, real, sesiones, clics):
    ingreso = real['ingreso']
    nuevos = real['clientes_nuevos']
    cac = _dividir(gasto_mxn, nuevos)
    roas = _dividir(ingreso, gasto_mxn)
    return {
        'campana': nombre,
        'slug': c,
        # Sin id no se puede abrir la radiografía: los nombres se repiten y se
        # editan, el id no. Las campañas que solo existen del lado del sitio
        # (ya apagadas en Meta) vienen sin id y por eso no son clicables.
        'campaign_id': r.get('campaign_id', ''),
        # lo que cobró Meta
        'gasto_mxn': round(gasto_mxn),
        'gasto': round(float(r.get('spend') or 0), 2),
        'moneda': r.get('currency', 'MXN'),
        'impresiones': int(r.get('impressions') or 0),
        'alcance': int(r.get('reach') or 0),
        'clics_enlace': clics,
        'paginas_cargadas': int(r.get('landing_page_views') or 0),
        'cpc': _dividir(gasto_mxn, clics),
        # lo que pasó de verdad en el sitio
        'sesiones': sesiones,
        'pedidos': real['pedidos'],
        'clientes_nuevos': nuevos,
        'ingreso': round(ingreso),
        'cac': cac,
        'roas': roas,
        # lo que Meta dice de sí misma, para poder contrastar
        'meta_compras': int(r.get('purchases') or 0),
        'meta_valor': round(float(r.get('purchase_value') or 0), 2),
        'veredicto': veredicto(gasto_mxn, clics, nuevos, roas),
    }


def veredicto(gasto_mxn, clics, nuevos, roas):
    """Una palabra por campaña, con la honestidad de decir 'no sé' cuando no sabe."""
    if gasto_mxn < MIN_GASTO_PARA_JUZGAR_MXN and clics < MIN_CLICS_PARA_JUZGAR:
        return 'sin datos'
    if nuevos == 0:
        return 'no trae clientes'
    if roas is None:
        return 'sin datos'
    if roas >= 2:
        return 'gana'
    if roas >= 1:
        return 'apenas'
    return 'pierde'


# --------------------------------------------------------------------------
# TODOS LOS CANALES, no solo Meta
# --------------------------------------------------------------------------
# Christian: "¿medimos también las ventas de distribuidores y de otros canales
# como WhatsApp y el sitio web?". No se medían: el reporte solo veía Meta y las
# ventas de distribuidor vivían en otra pestaña, sin costo asociado.
#
# Ojo con una trampa: un pedido puede venir de un anuncio Y cerrarse con el
# código de un distribuidor. Son dos cosas distintas — CÓMO LLEGÓ y QUIÉN LO
# CERRÓ — y meterlas en la misma columna obligaría a elegir a quién no contar.
# Por eso se reportan en dos cortes, y el traslape se dice en voz alta.

def canal_de_origen(attr: dict) -> str:
    """CÓMO llegó la persona al sitio."""
    attr = attr or {}
    if es_de_meta(attr):
        return 'meta'
    src = slug(attr.get('utm_source'))
    ref = str(attr.get('referrer') or '').lower()
    if src in ('whatsapp', 'wa', 'whats') or 'wa.me' in ref or 'whatsapp' in ref:
        return 'whatsapp'
    if src in ('google', 'googleads', 'adwords') or 'google.' in ref:
        return 'google'
    if src in ('tiktok',) or 'tiktok' in ref:
        return 'tiktok'
    if src in ('email', 'correo', 'mail', 'newsletter'):
        return 'correo'
    if src:
        return src
    if ref:
        return 'otro sitio'
    return 'directo'


def canales(pedidos, gasto_meta_mxn=0.0):
    """Ventas por canal, con el costo donde se conoce.

    Costos que SÍ sabemos: lo que se le pagó a Meta, y las comisiones que se le
    pagaron a los distribuidores. Los demás canales no tienen costo directo — no
    es que sean gratis, es que su costo no está en ningún lado, y decir 0 sería
    mentir menos que inventar una cifra.
    """
    origen = {}
    for o in pedidos:
        c = canal_de_origen(o.get('attribution'))
        d = origen.setdefault(c, {'canal': c, 'pedidos': 0, 'clientes_nuevos': 0, 'ingreso': 0.0})
        d['pedidos'] += 1
        d['ingreso'] += float(o.get('total') or 0)
        if o.get('first_order'):
            d['clientes_nuevos'] += 1

    filas = []
    for c, d in origen.items():
        costo = gasto_meta_mxn if c == 'meta' else None
        filas.append({**d, 'ingreso': round(d['ingreso']),
                      'costo': None if costo is None else round(costo),
                      'cac': _dividir(costo, d['clientes_nuevos']) if costo is not None else None,
                      'roas': _dividir(d['ingreso'], costo) if costo else None})
    filas.sort(key=lambda f: -f['ingreso'])

    # --- distribuidores: su costo real es la comisión pagada ---
    dist = {'pedidos': 0, 'clientes_nuevos': 0, 'ingreso': 0.0, 'comisiones': 0.0}
    por_dist = {}
    for o in pedidos:
        if not o.get('referred_by'):
            continue
        # La comisión guardada en la orden, no la tasa de hoy: cambiar tasas
        # nunca debe reescribir lo que costó una venta pasada.
        com = sum(float(r.get('amount') or 0) for r in (o.get('commissions') or [])) \
            or float(o.get('commission') or 0)
        dist['pedidos'] += 1
        dist['ingreso'] += float(o.get('total') or 0)
        dist['comisiones'] += com
        if o.get('first_order'):
            dist['clientes_nuevos'] += 1
        d = por_dist.setdefault(o['referred_by'], {'distributor_id': o['referred_by'],
                                                   'pedidos': 0, 'clientes_nuevos': 0,
                                                   'ingreso': 0.0, 'comisiones': 0.0})
        d['pedidos'] += 1
        d['ingreso'] += float(o.get('total') or 0)
        d['comisiones'] += com
        if o.get('first_order'):
            d['clientes_nuevos'] += 1

    # Cuántos pedidos de distribuidor llegaron además por un anuncio: es el
    # traslape, y hay que enseñarlo para que nadie sume las dos tablas.
    traslape = sum(1 for o in pedidos
                   if o.get('referred_by') and canal_de_origen(o.get('attribution')) == 'meta')

    return {
        'por_origen': filas,
        'distribuidores': {
            'pedidos': dist['pedidos'],
            'clientes_nuevos': dist['clientes_nuevos'],
            'ingreso': round(dist['ingreso']),
            'comisiones': round(dist['comisiones']),
            'cac': _dividir(dist['comisiones'], dist['clientes_nuevos']),
            'roas': _dividir(dist['ingreso'], dist['comisiones']),
            'detalle': sorted(({**d, 'ingreso': round(d['ingreso']),
                                'comisiones': round(d['comisiones']),
                                'cac': _dividir(d['comisiones'], d['clientes_nuevos'])}
                               for d in por_dist.values()),
                              key=lambda d: -d['ingreso']),
        },
        'traslape_meta_distribuidor': traslape,
    }


def enlace(base, campana, source='facebook', medium='paid', contenido=''):
    """El enlace etiquetado que hay que pegar en el anuncio.

    Sin esto no hay nada que cruzar: si el anuncio manda a exygenlabs.com a
    secas, la venta que produzca cae en "sin etiquetar" para siempre y esa
    campaña nunca podrá tener un costo por cliente.
    """
    c = slug(campana)
    q = f'utm_source={source}&utm_medium={medium}&utm_campaign={c}'
    if contenido:
        q += f'&utm_content={slug(contenido)}'
    base = (base or 'https://exygenlabs.com').rstrip('/')
    return f'{base}/?{q}' if '?' not in base else f'{base}&{q}'
