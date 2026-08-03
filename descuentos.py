"""LA REGLA DE 5 — consumo propio de los distribuidores.

Decisión de Christián (2026-07-30), cerrada:

  Cuando un distribuidor compra PARA SÍ MISMO, su descuento de distribuidor (su
  tasa efectiva) sólo aplica a los renglones con CINCO O MÁS PIEZAS DEL MISMO
  PRODUCTO. Los renglones de 1 a 4 piezas pagan PRECIO DE CLIENTE (la promo
  automática de siempre, 10% — o 15% por volumen, o su cupón si trae uno mejor).

  Es POR PRODUCTO, no por carrito: cinco de Retatrutida 20 mg, no cinco piezas
  surtidas. Por eso se cuenta sobre el producto YA RESUELTO contra el catálogo
  (`_agrupar_por_producto` en server.py), no sobre el texto que mandó el carrito:
  el mismo producto viaja a veces con su UUID y a veces con su SKU, y contado por
  el texto son dos montones de dos y de tres que nunca llegan a cinco.

⛔ POR QUÉ EXISTE. El precio de distribuidor es un precio de MAYOREO cobrado por
adelantado (ese descuento ES su comisión). Sin un mínimo, el canal se volvía una
tienda con 40% de descuento para tres personas: pedidos de una pieza, con su
costo de envío y de manejo completos, contra el margen más flaco del negocio.

⛔ Y LA PUERTA ANÓNIMA. Hasta hoy, un distribuidor que cerraba su sesión y compraba
con SU PROPIO código se llevaba las tres cosas a la vez: el descuento, la comisión
encima, y el crédito de venta de nivel. Salía más barato comprar deslogueado que
entrando a su cuenta — exactamente al revés de lo que se quería. `mismo_correo`
cierra esa puerta: si el correo del comprador es el del dueño del código, es
compra propia y se trata como tal (sin comisión, sin crédito, con la regla de 5).

Sólo se empareja por CORREO EXACTO (sin distinguir mayúsculas). Dos correos
distintos de la misma persona no se pueden adivinar, y adivinarlos mal le quitaría
su comisión a alguien que sí vendió: ante la duda, no es compra propia.

Módulo PURO a propósito: no toca la base ni la petición. Así se puede probar de
verdad —y no leyendo el texto de `create_order`, que es lo que había.
"""

# Piezas del MISMO producto que hacen falta para pagar precio de distribuidor.
MINIMO_PARA_PRECIO_DISTRIBUIDOR = 5

# ---------------------------------------------------------------------------
# EL ESCALÓN DE VOLUMEN (Christián, 2026-08-02)
#
# Desde TRES piezas del mismo producto, 12% — lo tenga quien lo tenga, cliente
# suelto o distribuidor comprando para sí.
#
# ⛔ POR QUÉ. Certified-PepMex cobra por escalones desde la TERCERA pieza (su
# Semaglutida 10 mg: 1-2 $2,300 · 3-5 $2,100 · 6-8 $1,960 · 9+ $1,800) y lo
# anuncia en Meta como «¡20% por apertura en el país!», sin decir que ese 20%
# exige comprar NUEVE. Nosotros teníamos un hueco justo ahí: nada hasta la
# cuarta pieza y de golpe 40% en la quinta (la REGLA DE 5). El cliente que
# quería tres o cuatro veía mejor precio con ellos.
#
# 12% y no 15% ni 20%: medido con la vara del ROI CON TODO (comisión, cashback,
# flete de China, guía y empaque, gastos fijos y pasarela), 12% deja 11 de 187
# productos bajo el piso de 5x contra los 9 que ya hay hoy sin ningún escalón.
# Al 20% son 18 — el doble. Y con 12% quedamos abajo de Certified en TODOS sus
# tramos: su Semaglutida de 10 mg a tres piezas sale en $2,100 y la nuestra en
# $2,014.
#
# NO toca la REGLA DE 5: el salto de la quinta pieza al precio de distribuidor
# sigue igual. Esto sólo llena el hueco de tres y cuatro, y de paso empuja a la
# quinta, que es donde la casa quiere al cliente.
PIEZAS_PARA_ESCALON_VOLUMEN = 3
ESCALON_VOLUMEN = 0.12


def tasa_por_volumen(cantidad):
    """El descuento que gana un renglón por SU PROPIA cantidad, sin pedir nada.

    Se cuenta por PRODUCTO ya resuelto contra el catálogo, igual que la regla de
    5 — y por la misma razón: contado por el texto del carrito, dos renglones de
    dos piezas del mismo vial son dos montones que nunca llegan a tres.
    """
    try:
        cantidad = int(cantidad or 0)
    except (TypeError, ValueError):
        return 0.0
    return ESCALON_VOLUMEN if cantidad >= PIEZAS_PARA_ESCALON_VOLUMEN else 0.0


def mismo_correo(a, b):
    """¿Son el mismo correo? Exacto, sin distinguir mayúsculas ni espacios de más.

    Vacío NUNCA empareja: un pedido sin correo no puede convertir la venta de un
    distribuidor en compra propia suya.
    """
    a = (a or '').strip().lower()
    b = (b or '').strip().lower()
    return bool(a) and a == b


def motivo_de_compra_propia(user, referrer, correo_del_comprador):
    """¿Esta compra es de un distribuidor PARA SÍ MISMO? Y por qué lo sabemos.

    Devuelve '' cuando no lo es, o el motivo:
      · 'sesion' — el que compra es un distribuidor con su cuenta abierta. Es el
        camino de siempre (Christian, 2026-07-25).
      · 'correo' — compró SIN sesión pero con SU PROPIO código, y el correo del
        pedido es el suyo. Ésta es LA PUERTA ANÓNIMA que estaba abierta: por ahí
        se llevaba descuento + comisión + crédito de venta de nivel, las tres cosas
        a la vez, por hacer algo tan simple como cerrar sesión.

    El orden importa: con sesión abierta manda la sesión, aunque en el pedido haya
    escrito el correo de otra persona (le está comprando a alguien, con su cuenta).
    """
    if user and user.get('role') == 'distributor':
        return 'sesion'
    if referrer and mismo_correo(referrer.get('email'), correo_del_comprador):
        return 'correo'
    return ''


def tasa_del_renglon(cantidad, tasa_base, tasa_distribuidor, es_compra_propia):
    """Qué descuento PIDE un renglón (antes del tope de su producto).

    - Venta normal (no es compra propia): lo de siempre, el mayor entre la tasa
      base y la del comprador. Uniforme para todo el carrito.
    - Compra propia de un distribuidor: la tasa de distribuidor SÓLO si el
      renglón junta 5 o más piezas del mismo producto. Si no, precio de cliente.

    Y en LOS DOS casos, el piso incluye el ESCALÓN DE VOLUMEN: desde tres piezas
    del mismo producto, 12%. Es un descuento que el renglón gana por su propia
    cantidad, así que no depende de quién compre.

    Nunca devuelve menos que `tasa_base` ni menos que ese escalón: la regla de 5
    quita el precio de MAYOREO, no el descuento que cualquier cliente tendría por
    esa misma compra.
    """
    tasa_base = max(0.0, float(tasa_base or 0))
    tasa_distribuidor = max(0.0, float(tasa_distribuidor or 0))
    try:
        cantidad = int(cantidad or 0)
    except (TypeError, ValueError):
        cantidad = 0
    piso = max(tasa_base, tasa_por_volumen(cantidad))
    if not es_compra_propia:
        return max(piso, tasa_distribuidor)
    if cantidad >= MINIMO_PARA_PRECIO_DISTRIBUIDOR:
        return max(piso, tasa_distribuidor)
    return piso


def tasas_por_producto(cantidades, tasa_base, tasa_distribuidor, es_compra_propia):
    """{clave_de_producto: tasa pedida} a partir de {clave: piezas SUMADAS}.

    Las cantidades entran ya agrupadas por producto real. Agrupar es la mitad de
    la regla: dos renglones de tres piezas del mismo vial son seis, no dos veces
    tres.
    """
    return {
        clave: tasa_del_renglon(piezas, tasa_base, tasa_distribuidor, es_compra_propia)
        for clave, piezas in (cantidades or {}).items()
    }


def repartir(items, clave_de, tope_de, tasas_pedidas, tasa_base):
    """Aplica las tasas RENGLÓN POR RENGLÓN y recorta cada una con el tope de su
    producto. Es la aritmética del dinero del checkout, aquí y no dentro de
    `create_order`, para poder probarla sin base de datos.

    · `items`: los renglones del pedido (necesitan .product_id, .name, .price,
      .quantity).
    · `clave_de(item)`: el producto YA RESUELTO contra el catálogo — la misma
      llave con la que se agruparon las cantidades.
    · `tope_de(item)`: cuánto descuento aguanta ese producto. CERO para los que no
      participan (insumos, HGH neto). El ROI de la casa manda sobre todo lo demás.
    · `tasas_pedidas`: {clave: tasa} de `tasas_por_producto`.

    Devuelve (descuento_en_pesos, tasa_de_la_orden, capados, renglones):
      · `tasa_de_la_orden` es la MAYOR tasa pedida del carrito. Es lo que se guarda
        en `discount_rate`, que leen los puntos y los reportes; con un carrito
        parejo vale exactamente lo mismo que antes de la regla de 5.
      · `capados` son los renglones que recibieron MENOS de lo pedido, para poder
        explicárselo al cliente.
      · `renglones` es el desglose completo: qué pidió y qué recibió cada uno.
    """
    descuento = 0
    capados = []
    renglones = []
    for it in items:
        pedida = max(0.0, float(tasas_pedidas.get(clave_de(it), tasa_base) or 0))
        aplicada = min(pedida, max(0.0, float(tope_de(it) or 0)))
        descuento += round(float(it.price) * int(it.quantity) * aplicada)
        renglones.append({
            'product_id': it.product_id, 'name': it.name, 'quantity': int(it.quantity),
            'asked_rate': round(pedida, 4), 'applied_rate': round(aplicada, 4),
        })
        if aplicada < pedida - 1e-9:
            capados.append({
                'name': it.name, 'product_id': it.product_id,
                'applied_rate': round(aplicada, 4), 'asked_rate': round(pedida, 4),
            })
    tasa = max(tasas_pedidas.values(), default=tasa_base) if tasas_pedidas else tasa_base
    return descuento, tasa, capados, renglones


def faltantes_para_precio_distribuidor(grupos):
    """Los productos a los que les faltan piezas para el precio de distribuidor.

    Es el EMPUJÓN, no el portazo: la compra pasa igual, pero el carrito le puede
    decir «llevas 3 de 5, agrega 2 más y bajas al precio de distribuidor».

    `grupos`: {clave: {'total': piezas, 'nombre': texto}} — tal como los devuelve
    `_agrupar_por_producto`. Devuelve una lista ordenada por lo que falta (primero
    el que está más cerca de lograrlo).
    """
    out = []
    for clave, g in (grupos or {}).items():
        try:
            piezas = int((g or {}).get('total') or 0)
        except (TypeError, ValueError):
            continue
        if 0 < piezas < MINIMO_PARA_PRECIO_DISTRIBUIDOR:
            out.append({
                'product_id': clave,
                'name': (g or {}).get('nombre') or '',
                'quantity': piezas,
                'faltan': MINIMO_PARA_PRECIO_DISTRIBUIDOR - piezas,
                'minimo': MINIMO_PARA_PRECIO_DISTRIBUIDOR,
            })
    out.sort(key=lambda r: (r['faltan'], r['name']))
    return out
