"""Envíos: cuánto pesa un pedido y quién paga la guía.

Aquí vive la ARITMÉTICA del envío, sin red y sin base de datos, para que se pueda
probar de verdad. Hablar con Skydropx es cosa de `skydropx.py`; decidir cuánto se
le cobra al cliente es cosa de este archivo.

⛔ HOY ESTO NO COBRA NADA. Los dos interruptores de abajo nacen APAGADOS a
propósito (Christian, 2026-07-28). Con ellos apagados el checkout se comporta
EXACTAMENTE como antes de que existiera este archivo: no se cotiza, no se cobra
envío y no se compra guía. El día que Christian decida encenderlo, se prenden
aquí y nada más.
"""

# --------------------------------------------------------------------- switches
# ⛔ APAGADOS. No los prendas sin que Christian lo pida.
#
# `COTIZAR_EN_CHECKOUT`: el checkout pide precios reales de Estafeta por CP y peso,
# los enseña, y el envío elegido se suma al total del pedido.
#
# `COMPRAR_GUIA_AL_PAGAR`: cuando el pago se confirma (tarjeta, SPEI, OXXO o
# cripto), el servidor compra la guía solo y la deja en el pedido.
#
# Son DOS y no uno porque son dos decisiones distintas: se puede querer comprar la
# guía automáticamente sin cobrarle el envío al cliente (la casa lo absorbe), pero
# no al revés — cobrar un envío que nadie compra es cobrar por nada.
COTIZAR_EN_CHECKOUT = False
COMPRAR_GUIA_AL_PAGAR = False


# ------------------------------------------------------------------- el peso
# ⚠️ PENDIENTE DE CHRISTIAN: capturar el peso REAL de cada producto.
#
# El catálogo todavía no trae peso (`weight_kg` existe en el modelo pero viene
# vacío). Mientras tanto se usan estos valores por omisión, que salen de lo que
# de verdad hay en la caja, no de una adivinanza redonda:
#
#   · un vial liofilizado es vidrio chico con tapón de hule: ~10 g el vial, más
#     su cajita y su espuma;
#   · el agua bacteriostática viene en frasco de 30 ml: el líquido solo ya pesa
#     30 g, más el vidrio;
#   · jeringas y agujas casi no pesan, pero abultan;
#   · un kit/stack trae varias piezas juntas.
#
# Estos números NO son el precio: si se quedan cortos, la paquetería cobra la
# diferencia al recibir el paquete y la casa la absorbe. Por eso conviene que el
# dueño capture los reales en cuanto tenga la báscula enfrente.
PESO_VIAL_KG = 0.05
PESO_AGUA_KG = 0.10
PESO_INSUMO_KG = 0.02          # jeringas, agujas, toallitas
PESO_KIT_KG = 0.30             # kits, stacks, combos

# La caja, el relleno, la bolsa y el sobre de la guía. Va UNA vez por pedido, no
# por producto: mandar diez viales no lleva diez cajas.
PESO_EMPAQUE_KG = 0.30

# Las paqueterías facturan un mínimo aunque el paquete pese menos. Cotizar 0.4 kg
# y que cobren 1 kg es la forma clásica de que la cotización salga barata y la
# factura no.
PESO_MINIMO_KG = 1.0

# Caja estándar en la que sale todo hoy (cm). Skydropx exige medidas para cotizar.
# ⚠️ PENDIENTE: si Christian empieza a usar varias cajas, esto se vuelve una tabla.
CAJA_LARGO_CM = 30
CAJA_ANCHO_CM = 20
CAJA_ALTO_CM = 15


def _es(texto: str, *palabras: str) -> bool:
    t = (texto or '').lower()
    return any(p in t for p in palabras)


def peso_de_pieza(doc: dict | None, nombre: str = '') -> float:
    """Cuánto pesa UNA pieza de este producto, en kg.

    Si el catálogo trae `weight_kg` capturado, manda ese — siempre. Si no, se
    deduce del tipo de presentación. Nunca devuelve cero: un renglón que pesa
    cero es un renglón que la paquetería no va a cobrar en cero.
    """
    doc = doc or {}
    try:
        capturado = float(doc.get('weight_kg') or 0)
    except (TypeError, ValueError):
        capturado = 0
    if capturado > 0:
        return capturado

    texto = ' '.join(str(doc.get(k) or '') for k in ('name', 'slug', 'category', 'presentation'))
    texto = f'{texto} {nombre or ""}'
    if _es(texto, 'bacteriost', 'agua', 'water', 'solvente', 'diluyente'):
        return PESO_AGUA_KG
    if _es(texto, 'jering', 'aguja', 'syringe', 'needle', 'toallit', 'alcohol'):
        return PESO_INSUMO_KG
    if _es(texto, 'kit', 'stack', 'combo', 'paquete'):
        return PESO_KIT_KG
    return PESO_VIAL_KG


def peso_del_pedido(items, pflags: dict | None = None) -> float:
    """Peso facturable de un carrito completo, en kg.

    `items` son los renglones (objetos con `.product_id/.quantity/.name` o dicts) y
    `pflags` el catálogo ya resuelto por id y por SKU, tal como lo arma el checkout.
    """
    pflags = pflags or {}
    total = 0.0
    for it in items or []:
        get = (lambda k: getattr(it, k, None)) if not isinstance(it, dict) else it.get
        pid = get('product_id') or ''
        try:
            qty = max(0, int(get('quantity') or 0))
        except (TypeError, ValueError):
            qty = 0
        total += peso_de_pieza(pflags.get(pid), get('name') or '') * qty
    if total <= 0:
        return 0.0                      # carrito vacío: no hay paquete que cotizar
    return round(max(PESO_MINIMO_KG, total + PESO_EMPAQUE_KG), 2)


def paquete_del_pedido(items, pflags: dict | None = None) -> dict:
    """Lo que Skydropx necesita saber del bulto: peso y medidas."""
    return {
        'peso_kg': peso_del_pedido(items, pflags),
        'largo_cm': CAJA_LARGO_CM,
        'ancho_cm': CAJA_ANCHO_CM,
        'alto_cm': CAJA_ALTO_CM,
    }


# ------------------------------------------------- quién paga el envío
TOPE_ENVIO_SOBRE_COMPRA = 0.10

# ⚠️⚠️ FALTA UNA DEFINICIÓN DE CHRISTIAN — Y ES DE UNA SOLA LÍNEA.
#
# La regla acordada: arriba de $2,500 el envío va por cuenta de la casa, PERO solo
# mientras no pase del 10% de la compra. Lo que NO está definido es qué pasa cuando
# sí lo pasa: un pedido de $3,000 con un envío de $600 (el 20%).
#
#   True  → el cliente paga los $600 completos. Es lo que está puesto hoy, por ser
#           lo que más protege el ROI, que es como se decide todo en esta casa.
#   False → la casa absorbe su 10% ($300) y el cliente paga solo el excedente
#           ($300). Es más amable con el cliente y cuesta la mitad.
#
# Para cambiarlo: esta línea, nada más. No hay otro lugar donde se decida.
CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE = True


def cobro_de_envio_al_cliente(costo_envio: float, mercancia_pagada: float,
                              envio_gratis_desde: float) -> float:
    """Cuánto de la guía se le cobra al cliente. Devuelve pesos, nunca negativos.

    Tres casos, en este orden:

      1. Compra chica (abajo del umbral): paga su envío completo. Absorber $250 en
         un pedido de $879 se come el 28% del ingreso.
      2. Compra grande y envío barato (≤ 10% de lo que pagó): GRATIS, la casa lo
         absorbe. Es la promesa que se le hace al cliente.
      3. Compra grande y envío caro (> 10%): ver la constante de arriba. Hoy paga
         el envío completo. ⚠️ Falta la definición de Christian.

    Se mide sobre lo que el cliente PAGA de mercancía (ya con descuento y ya con
    los puntos aplicados), no sobre el precio de lista: si no, un código grande
    dejaría el envío gratis cobrando mucho menos. Primero el ROI.
    """
    try:
        costo = max(0.0, float(costo_envio or 0))
    except (TypeError, ValueError):
        costo = 0.0
    try:
        mercancia = max(0.0, float(mercancia_pagada or 0))
    except (TypeError, ValueError):
        mercancia = 0.0
    if costo <= 0:
        return 0.0
    if mercancia < float(envio_gratis_desde or 0):
        return round(costo)
    tope = mercancia * TOPE_ENVIO_SOBRE_COMPRA
    if costo <= tope:
        return 0.0
    if CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE:
        return round(costo)
    return round(costo - tope)


# ------------------------------------------------- vigencia de una cotización
# Una cotización guardada solo vale unos minutos. No es burocracia: es lo que
# impide que alguien cotice hoy a $150, guarde el id y lo use dentro de un mes
# cuando la tarifa ya subió. Y el servidor SIEMPRE vuelve a mirar la cotización
# guardada; jamás cree el número que manda el navegador.
VIGENCIA_COTIZACION_MIN = 30
