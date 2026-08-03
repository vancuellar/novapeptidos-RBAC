"""La SOLICITUD de guía de envío: el distribuidor la pide, Christián la aprueba.

Encargo de Christián (2026-08-03): «Necesito que un distribuidor pueda solicitarlo
y yo lo apruebo: un botón "solicitar guía" junto al cliente al que le falte número
de guía, siempre y cuando ya haya pagado; y que una vez generada la guía le asigne
el número automáticamente (correo al cliente y todo) como si se hubiera hecho
desde la compra.»

Aquí viven los CANDADOS, sin red y sin base de datos, para poderlos probar de
verdad (mismo trato que `comisiones.py`, que es el molde de esta bolsa). Las
rutas viven en `server.py`; los documentos, en la colección `label_requests`.

Las tres reglas de la casa:

  · EL DISTRIBUIDOR NUNCA GASTA. Solicitar no compra nada: una guía cuesta
    dinero de verdad y la única compuerta hacia la compra es la aprobación de
    Christián. Por eso aquí no hay ninguna función que "compre": la compra es
    `comprar_guia_del_pedido` de server.py, la MISMA del pago automático, para
    que el correo al cliente, el candado de doble compra y los frenos de gasto
    sean exactamente los mismos.

  · SIN COBRAR NO HAY GUÍA. La regla de qué cuenta como pagado es UNA en todo
    el backend: `cobrado.esta_pagado`. Ni esta bolsa ni ninguna otra escribe
    la suya propia.

  · UNA SOLICITUD A LA VEZ POR PEDIDO. Una segunda solicitud del mismo pedido
    mientras hay una en camino sólo duplicaría campanitas — y dos aprobaciones
    del mismo pedido son dos intentos de gastar el mismo dinero.
"""

from cobrado import esta_pagado

ESTADO_SOLICITADA = 'solicitada'   # el distribuidor la pidió; Christián no ha dicho
ESTADO_APROBADA = 'aprobada'       # Christián aprobó y la guía SE COMPRÓ
ESTADO_RECHAZADA = 'rechazada'     # Christián la negó, con motivo; se puede volver a pedir

ESTADOS = (ESTADO_SOLICITADA, ESTADO_APROBADA, ESTADO_RECHAZADA)


def solicitud_pendiente(solicitudes):
    """La solicitud SIN resolver de un pedido, si la hay. A lo más una a la vez."""
    for s in (solicitudes or []):
        if s.get('status') == ESTADO_SOLICITADA:
            return s
    return None


def puede_solicitar(order, solicitudes):
    """¿Puede el distribuidor solicitar la guía de ESTE pedido? → (True, '') o
    (False, motivo). El motivo va en palabras del panel, porque es lo que la
    pantalla enseña. El candado de «es SU pedido» no vive aquí: lo aplica la
    ruta contra `referred_by`, igual que el resto del panel."""
    if not order:
        return False, 'Ese pedido no existe.'
    if order.get('status') == 'cancelado':
        return False, 'Ese pedido está cancelado.'
    if (order.get('tracking_number') or '').strip():
        return False, 'Ese pedido ya tiene guía.'
    if not esta_pagado(order):
        return False, 'La guía se solicita cuando el pedido ya está pagado.'
    if solicitud_pendiente(solicitudes):
        return False, 'Ya hay una solicitud en camino para este pedido.'
    return True, ''
