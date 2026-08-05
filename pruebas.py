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


# ==========================================================================
#  USUARIOS DE PRUEBA — el barrido de las cuentas que dejan las corridas E2E
# ==========================================================================
#
# Christián, 2026-08-05: «¿Puedes borrar los usuarios de prueba que se llaman E2E
# Guia (prueba)?» — y enseguida, mejor: «o dame un botón para borrarlos y yo lo
# hago». Esto es ese botón.
#
# Cada corrida de `npm run e2e:*` crea su cuenta desechable y la deja ahí. Con el
# tiempo el padrón de clientes se llena de gente que no existe, y eso ensucia
# cualquier número de negocio que se cuente por usuarios.
#
# ⛔ MISMA FILOSOFÍA QUE EL BARRIDO DE PEDIDOS: no se borra por parecerse a una
# prueba, se borra por NO tener nada real colgando. El nombre es sólo el filtro de
# entrada; el candado de verdad es que no haya un pedido detrás.

# El correo que se inventan los guiones E2E. Es el ÚNICO patrón que entra al
# barrido: una cuenta que no lo cumpla no se mira siquiera, por más sospechosa que
# parezca. Los guiones usan `e2e.<algo>@` (con o sin `+etiqueta` de Gmail).
PATRON_CORREO_E2E = r'(^|\+)e2e\.'

# Roles que NUNCA se barren, aunque el correo cuadre y no tengan pedidos. Un admin
# o una distribuidora con el correo raro sigue siendo una persona con acceso.
ROLES_INTOCABLES = ('admin', 'distributor', 'marketing')


def razones_para_conservar(user, pedidos) -> list:
    """Por qué NO se puede borrar esta cuenta. Vacío = se puede barrer.

    `pedidos` son los pedidos de ese usuario, ya consultados por quien llama.
    """
    razones = []
    if (user or {}).get('role') in ROLES_INTOCABLES:
        razones.append(f"tiene rol {user.get('role')}")
    for o in (pedidos or []):
        # Se reusa EXACTAMENTE el mismo juez que protege a los pedidos: si un pedido
        # suyo no se podría barrer, su dueño tampoco. Un candado, no dos copias.
        motivos = senales_de_venta_real(o)
        if motivos:
            razones.append(f"su pedido {o.get('order_number')} es venta real "
                           f"({', '.join(motivos)})")
    if pedidos and not razones:
        # Sin señales de venta, pero con pedidos: se conserva igual. Borrar al dueño
        # dejaría pedidos huérfanos, y un pedido sin cliente no se puede ni leer.
        razones.append(f'tiene {len(pedidos)} pedido(s) colgando')
    return razones
