"""LA CAMPANITA CUANDO ENTRA UNA VENTA — Christián, 2026-07-30.

Cada venta genera notificación IN-APP para dos personas:

  · el ADMIN — «Entró Un Pedido EX-… · $2,830 · Pagado»;
  · el DISTRIBUIDOR cuyo código se usó — «¡Venta Con Tu Código! EX-… · Tu Comisión $X»,
    en SU idioma (María abre la cuenta en pt-BR).

⛔ POR QUÉ EXISTE. El sistema avisaba por correo y repartía comisiones, pero la
campanita —lo único que se ve al entrar— no decía nada de las ventas. El 2026-07-30
entraron dos pedidos con el código de María y ni Christián ni ella se enteraron dentro
de la app. Un aviso que llega sólo al correo se pierde entre los correos.

Módulo PURO: arma los TEXTOS, no toca la base. Quien los guarda es `notify()` en
server.py. Así se puede probar el texto exacto de las tres traducciones sin base de
datos, que es donde de verdad se rompen estas cosas.
"""

IDIOMAS = ('es', 'en', 'pt')
POR_OMISION = 'es'


def idioma(valor):
    """'pt-BR' -> 'pt'. Lo que no reconocemos cae al español, el idioma de casa."""
    lang = str(valor or '').strip().lower().replace('_', '-').split('-')[0]
    return lang if lang in IDIOMAS else POR_OMISION


def dinero(monto):
    """$2,830 — sin centavos. Es un aviso, no un estado de cuenta."""
    try:
        return f'${float(monto or 0):,.0f}'
    except (TypeError, ValueError):
        return '$0'


# Mayúscula A Cada Palabra en los títulos: es como Christián quiere los tableros.
_ADMIN = {
    'es': {'titulo': 'Entró Un Pedido',
           'pagado': 'Pagado', 'por_cobrar': 'Por Cobrar',
           'surtir': 'Con Piezas Por Mandar Pedir',
           'con_codigo': 'con el código de {quien}'},
    'en': {'titulo': 'An Order Came In',
           'pagado': 'Paid', 'por_cobrar': 'Unpaid',
           'surtir': 'With Units To Backorder',
           'con_codigo': 'with {quien}’s code'},
    'pt': {'titulo': 'Entrou Um Pedido',
           'pagado': 'Pago', 'por_cobrar': 'A Receber',
           'surtir': 'Com Unidades Para Encomendar',
           'con_codigo': 'com o código de {quien}'},
}

_VENDEDOR = {
    'es': {'titulo': '¡Venta Con Tu Código!', 'comision': 'tu comisión {monto}',
           'equipo': 'Venta De Tu Equipo', 'ganaste': 'ganaste {monto}'},
    'en': {'titulo': 'A Sale With Your Code!', 'comision': 'your commission {monto}',
           'equipo': 'A Sale From Your Team', 'ganaste': 'you earned {monto}'},
    'pt': {'titulo': 'Venda Com Seu Código!', 'comision': 'sua comissão {monto}',
           'equipo': 'Venda Da Sua Equipe', 'ganaste': 'você ganhou {monto}'},
}


def aviso_para_el_admin(order, vendedor_nombre='', lang=POR_OMISION):
    """El aviso de la campanita para Christián. Dice lo único que importa de un
    vistazo: cuál pedido, cuánto, si el dinero YA ENTRÓ, y si hay que mandar pedir."""
    c = _ADMIN[idioma(lang)]
    partes = [str(order.get('order_number') or ''), dinero(order.get('total'))]
    partes.append(c['pagado'] if order.get('paid') else c['por_cobrar'])
    if vendedor_nombre:
        partes.append(c['con_codigo'].format(quien=vendedor_nombre))
    if order.get('backorder_items'):
        partes.append(c['surtir'])
    return c['titulo'], ' · '.join(p for p in partes if p)


def aviso_para_el_vendedor(order, monto, es_equipo=False, lang=POR_OMISION):
    """El aviso para el distribuidor: su venta (o la de su equipo) y su tajada."""
    c = _VENDEDOR[idioma(lang)]
    titulo = c['equipo'] if es_equipo else c['titulo']
    cuerpo = c['ganaste' if es_equipo else 'comision'].format(monto=dinero(monto))
    return titulo, f'{order.get("order_number") or ""} · {cuerpo}'
