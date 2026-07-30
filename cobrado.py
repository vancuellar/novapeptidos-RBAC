"""¿ENTRÓ EL DINERO DE ESTE PEDIDO? Una sola respuesta para todo el sistema.

⛔ PAGADO Y ENTREGADO SON COSAS DISTINTAS (Christián, 2026-07-29). El `status` de un
pedido cuenta el viaje de la MERCANCÍA (pendiente → confirmado → enviado → entregado);
el dinero lo cuenta `paid`. Se separaron porque Christián entrega en persona y a veces
cobra después: la venta de Alanís (EX-20260729-9934, $3,857) salió ENTREGADA y SIN
PAGAR, y el tablero la sumaba como ingreso.

⛔ POR QUÉ ESTO VIVE EN SU PROPIO ARCHIVO. La primera versión puso `esta_pagado` dentro
de `server.py` y sólo arregló `/admin/stats`. El dinero, en cambio, se suma en OCHO
lugares más —la gráfica de series, analytics, el embudo, el reporte de marketing, los
tableros de distribuidor, las fichas de cliente— y tres de ellos viven en otros módulos
(`marketing.py`, `pyramid.py`, `director.py`), que no pueden importar `server.py` sin
un ciclo. Así que la regla se mudó aquí: un solo archivo que todos pueden importar, y
ningún reporte con su propia idea de qué es un ingreso.

REGLA DE LOS PEDIDOS VIEJOS: los que nacieron antes del 2026-07-29 no traen el campo
`paid`, así que para ellos se INFIERE del estado (confirmado/enviado/entregado = pagado).
Así ningún reporte histórico cambia de golpe y no hace falta una migración que toque la
base de producción. Cuando el campo SÍ viene, manda sobre el estado.
"""

# Estados en los que, si el pedido no dice otra cosa, se da por cobrado.
ESTADOS_PAGADOS = ('confirmado', 'enviado', 'entregado')


def esta_pagado(order) -> bool:
    """¿Entró el dinero de este pedido?"""
    if not order:
        return False
    if order.get('status') == 'cancelado':
        # Una devolución deja el pedido cancelado: el dinero ya salió de vuelta.
        return False
    if 'paid' in order:
        return bool(order['paid'])
    return order.get('status') in ESTADOS_PAGADOS


def esta_vivo(order) -> bool:
    """¿Este pedido sigue en pie? (todo lo que no está cancelado)."""
    return bool(order) and order.get('status') != 'cancelado'


def cobrado_de(order) -> float:
    """Cuánto de este pedido es INGRESO: su total si ya se cobró, cero si no."""
    return float(order.get('total') or 0) if esta_pagado(order) else 0.0


def por_cobrar_de(order) -> float:
    """Cuánto de este pedido es DEUDA: entregado o en camino pero sin cobrar.

    Sin este número un pedido fiado desaparecería del tablero: ni suma en ingresos
    ni aparece en ningún lado.
    """
    return float(order.get('total') or 0) if (esta_vivo(order) and not esta_pagado(order)) else 0.0


def solo_cobrados(orders):
    """Los pedidos cuyo dinero SÍ entró. Es el filtro de cualquier reporte de ingreso."""
    return [o for o in (orders or []) if esta_pagado(o)]
