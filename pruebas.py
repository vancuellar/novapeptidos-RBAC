"""PEDIDOS DE PRUEBA: marcarlos, y barrerlos sin llevarse una venta de verdad.

⛔ POR QUÉ EXISTE ESTE ARCHIVO (Christián, 2026-08-01). *«Asegúrate de borrar los
pedidos de prueba cuando termines de hacer las pruebas. De otra manera queda mucha
basura en el sitio.»* La basura es real: para comprobar el carrito compartible se hizo
una compra DE VERDAD en producción y el pedido se quedó ahí, encima de los doce pedidos
de prueba que ya se habían borrado a mano el 2026-07-29.

Lo que NO se puede es limpiar a lo bruto. Entre los pedidos de prueba vive la única
venta real de esos días (Paz Cambray) y un `delete_many` la borra sin deshacer. Así que
la limpieza tiene dos partes separadas a propósito:

  1. **Marcar.** Quien ensucia pone la etiqueta `es_prueba` en SU pedido. Es sólo una
     etiqueta: no borra, no esconde, se quita igual de fácil.
  2. **Barrer.** El barrido sólo mira los pedidos etiquetados, y de ésos aparta
     cualquiera que enseñe una señal de venta real. Ante la duda, no se borra.

Este archivo es la parte que se puede probar sin base de datos: qué señales hacen que un
pedido deje de ser basura. El borrado en sí NO se escribe aquí — el barrido se lo pasa
al lote de siempre (`/admin/orders/lote`, acción `borrar`, `forzar=False`), que es donde
vive el candado de los pedidos pagados y donde se devuelven puntos e inventario. Un
camino de borrado nuevo sería un candado menos.
"""
from cobrado import ESTADOS_PAGADOS, esta_pagado

# Los motivos viajan como CLAVE, no como frase: el Panel habla tres idiomas y la
# traducción vive allá. Si se mandara el texto ya escrito, el Panel en inglés
# enseñaría "surtido".
MOTIVOS = ('pagado', 'surtido', 'comprobante', 'guia', 'fantasma')


def senales_de_venta_real(order) -> list:
    """Todo lo que hace dudar de que este pedido sea basura de una prueba.

    Si la lista sale vacía, el pedido no tocó dinero ni bodega. Si trae algo —lo que
    sea— el barrido lo deja en paz y le dice al admin por qué.
    """
    if not order:
        return ['fantasma']
    motivos = []
    # El dinero, con la única regla que tiene el backend (cobrado.py).
    if esta_pagado(order):
        motivos.append('pagado')
    # ⚠️ Y ADEMÁS el estado, que NO es lo mismo. Un pedido entregado y fiado
    # (`paid: False`) da `esta_pagado() == False` y es una venta real de las de doler:
    # la mercancía ya salió. Ése fue el pedido de Alanís. Sin esta línea el barrido se
    # lo llevaría por no estar pagado.
    if order.get('status') in ESTADOS_PAGADOS:
        motivos.append('surtido')
    if order.get('paid_at') or order.get('spei_receipt_at'):
        motivos.append('comprobante')
    if str(order.get('tracking_number') or '').strip():
        motivos.append('guia')
    return motivos


def se_puede_barrer(order) -> bool:
    """¿El barrido puede llevarse este pedido?

    Las dos condiciones se piden JUNTAS: que alguien lo haya marcado como prueba
    (nadie barre lo que no marcó) y que no enseñe ninguna señal de venta real.
    """
    return bool(order) and order.get('es_prueba') is True and not senales_de_venta_real(order)
