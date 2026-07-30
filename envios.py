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

# --------------------------------------------------------------------- la caja
# ⛔ LA CAJA ES LO QUE MÁS ENCARECE UN ENVÍO CHICO, NO EL PESO.
#
# Las paqueterías cobran por el mayor de dos números: lo que pesa y lo que ABULTA
# (peso volumétrico = largo × ancho × alto ÷ 5000, el divisor estándar en México).
#
# Hasta el 2026-07-30 aquí había UNA sola caja de 30×20×15 para todo. Eso son
# 9,000 cm³ → 1.8 kg volumétricos. O sea: un pedido de dos viales que pesa 400 g de
# verdad se cotizaba como 1.8 kg, casi el doble del mínimo facturable. Se pagaba aire.
#
# Ahora la caja se elige por lo que de verdad va adentro. Dos viales caben de sobra
# en la chica (20×15×10 = 0.6 kg volumétricos), y ahí el que manda es el mínimo de
# 1 kg de la paquetería, que es lo más barato que se puede cotizar.
#
# `nombre` es solo para que se lea en el panel y en la bitácora; `peso_max_kg` es
# hasta cuánto contenido aguanta esa caja antes de pasar a la siguiente.
CAJAS = (
    {'nombre': 'chica',   'largo_cm': 20, 'ancho_cm': 15, 'alto_cm': 10,
     'peso_max_kg': 1.0, 'peso_caja_kg': 0.15},
    {'nombre': 'mediana', 'largo_cm': 30, 'ancho_cm': 20, 'alto_cm': 15,
     'peso_max_kg': 3.0, 'peso_caja_kg': 0.30},
    {'nombre': 'grande',  'largo_cm': 40, 'ancho_cm': 30, 'alto_cm': 20,
     'peso_max_kg': 999.0, 'peso_caja_kg': 0.60},
)

# El divisor volumétrico. 5000 es el que usan Estafeta, FedEx y Paquetexpress en
# territorio nacional. Vive aquí para que se vea, no para que se adivine.
DIVISOR_VOLUMETRICO = 5000

# Compatibilidad hacia atrás: quien pida "la caja" sin decir cuál, recibe la mediana,
# que es la que había antes de que esto fuera una tabla.
CAJA_LARGO_CM = CAJAS[1]['largo_cm']
CAJA_ANCHO_CM = CAJAS[1]['ancho_cm']
CAJA_ALTO_CM = CAJAS[1]['alto_cm']


# Medidas puestas desde el Panel de Admin (Admin → Envíos). El día que Christián
# empiece a usar otras cajas las cambia ahí y no hay que tocar código ni desplegar.
# ⛔ Vacío = manda la tabla de arriba. Se rellena con `cargar_cajas_del_panel`.
_CAJAS_DEL_PANEL: list = []


def cargar_cajas_del_panel(cajas) -> int:
    """Sustituye la tabla de cajas por la que guardó el admin. Devuelve cuántas quedaron.

    Se valida aquí y no en el panel porque aquí es donde duele: una caja con medidas
    en cero hace que la paquetería cotice contra basura, y una lista vacía tiene que
    devolver el control a la tabla de arriba en vez de dejar el sitio sin cajas.
    """
    global _CAJAS_DEL_PANEL
    buenas = []
    for c in (cajas or []):
        if not isinstance(c, dict):
            continue
        try:
            medidas = {k: float(c.get(k) or 0) for k in ('largo_cm', 'ancho_cm', 'alto_cm')}
        except (TypeError, ValueError):
            continue
        if any(v <= 0 for v in medidas.values()):
            continue
        try:
            tope = float(c.get('peso_max_kg') or 0) or 999.0
        except (TypeError, ValueError):
            tope = 999.0
        try:
            propio = max(0.0, float(c.get('peso_caja_kg') or 0))
        except (TypeError, ValueError):
            propio = 0.0
        buenas.append(dict(medidas, nombre=str(c.get('nombre') or 'caja'),
                           peso_max_kg=tope, peso_caja_kg=propio))
    _CAJAS_DEL_PANEL = sorted(buenas, key=lambda c: c['peso_max_kg'])
    return len(_CAJAS_DEL_PANEL)


def cajas() -> tuple:
    """Las cajas vigentes: las del panel si las hay, si no las de fábrica."""
    return tuple(_CAJAS_DEL_PANEL) if _CAJAS_DEL_PANEL else CAJAS


def caja_para(peso_contenido_kg: float) -> dict:
    """La caja más chica en la que cabe este pedido.

    Se elige por peso porque es el único dato que tenemos de todos los productos.
    No es perfecto —diez jeringas pesan poco y abultan— pero se equivoca hacia la
    caja chica, que es la que cotiza barato; y si un día no cabe, se sube el
    `peso_max_kg` desde el panel.
    """
    try:
        peso = max(0.0, float(peso_contenido_kg or 0))
    except (TypeError, ValueError):
        peso = 0.0
    disponibles = cajas()
    for c in disponibles:
        if peso <= float(c.get('peso_max_kg') or 0):
            return dict(c)
    return dict(disponibles[-1])


def peso_volumetrico(caja: dict) -> float:
    """Lo que la paquetería va a decir que pesa esta caja por lo que abulta."""
    caja = caja or {}
    try:
        vol = (float(caja.get('largo_cm') or 0) * float(caja.get('ancho_cm') or 0)
               * float(caja.get('alto_cm') or 0))
    except (TypeError, ValueError):
        return 0.0
    return round(vol / DIVISOR_VOLUMETRICO, 2)


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


def peso_del_contenido(items, pflags: dict | None = None) -> float:
    """Lo que pesa la MERCANCÍA sola, sin caja. En kg.

    Se separó del peso facturable el 2026-07-30: la caja ya no es una sola, así que
    primero hay que saber cuánto va adentro para poder elegir en cuál cabe.
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
    return round(total, 3)


def peso_del_pedido(items, pflags: dict | None = None) -> float:
    """Peso facturable de un carrito completo, en kg.

    `items` son los renglones (objetos con `.product_id/.quantity/.name` o dicts) y
    `pflags` el catálogo ya resuelto por id y por SKU, tal como lo arma el checkout.
    """
    contenido = peso_del_contenido(items, pflags)
    if contenido <= 0:
        return 0.0                      # carrito vacío: no hay paquete que cotizar
    caja = caja_para(contenido)
    propio = caja.get('peso_caja_kg')
    propio = PESO_EMPAQUE_KG if propio is None else propio
    return round(max(PESO_MINIMO_KG, contenido + propio), 2)


def paquete_del_pedido(items, pflags: dict | None = None) -> dict:
    """Lo que Skydropx necesita saber del bulto: peso y medidas de la caja que le toca.

    ⛔ Las medidas ya NO son fijas. Van las de la caja más chica en la que cabe el
    pedido, porque la paquetería cobra por el mayor entre lo que pesa y lo que
    abulta: mandar dos viales en una caja de 30×20×15 se cotiza como 1.8 kg cuando
    de verdad son 0.25.
    """
    contenido = peso_del_contenido(items, pflags)
    caja = caja_para(contenido)
    return {
        'peso_kg': peso_del_pedido(items, pflags),
        'largo_cm': caja['largo_cm'],
        'ancho_cm': caja['ancho_cm'],
        'alto_cm': caja['alto_cm'],
        # Para que se vea en el panel y en la bitácora POR QUÉ costó lo que costó.
        'caja': caja.get('nombre', ''),
        'peso_contenido_kg': contenido,
        'peso_volumetrico_kg': peso_volumetrico(caja),
    }


# ------------------------------------------------- quién paga el envío
TOPE_ENVIO_SOBRE_COMPRA = 0.10

# ✅ DECIDIDO POR CHRISTIAN el 2026-07-28, en sus palabras:
# "En un pedido de más de 2.5k pesos donde el envío pasa del 10%, el cliente paga
#  la diferencia y la casa absorbe hasta el 10% del costo del envío máximo."
#
# O sea: en un pedido de $3,000 con envío de $600, la casa pone $300 (su 10%) y el
# cliente paga los otros $300. La casa NUNCA absorbe más del 10% de la compra, y el
# cliente nunca paga el envío completo si ya pasó de $2,500.
#
# Para cambiarlo: esta línea, nada más. No hay otro lugar donde se decida.
CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE = False


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


def tope_que_absorbe_la_casa(mercancia_pagada: float) -> float:
    """El MÁXIMO de envío que la casa está dispuesta a comerse en un pedido.

    Es el 10% de lo que el cliente pagó de mercancía, y ni un peso más. Palabras de
    Christian: «si una compra por 2,500 genera un costo de envío de $500 ni en pedo
    lo pago». Un pedido de $179 tiene un tope de $17.90 — por eso nunca puede llevar
    envío gratis.
    """
    try:
        mercancia = max(0.0, float(mercancia_pagada or 0))
    except (TypeError, ValueError):
        mercancia = 0.0
    return round(mercancia * TOPE_ENVIO_SOBRE_COMPRA, 2)


def envio_que_absorbe_la_casa(costo_envio: float, cobrado_al_cliente: float) -> float:
    """Lo que la guía le cuesta a la casa DESPUÉS de lo que pagó el cliente.

    El espejo de `cobro_de_envio_al_cliente`. Existe porque el número que duele no
    es el que se cobra sino el que NO se cobra: hoy el checkout no cobra envío
    (`COBRAR_ENVIO = False` en server.py, decisión de Christian), así que la casa
    absorbe el 100% de cada guía y en el pedido eso se guardaba como $0 — un pedido
    de $179 se llevaba $250 de envío, el 140%, y no aparecía en ningún reporte.
    """
    try:
        costo = max(0.0, float(costo_envio or 0))
    except (TypeError, ValueError):
        costo = 0.0
    try:
        cobrado = max(0.0, float(cobrado_al_cliente or 0))
    except (TypeError, ValueError):
        cobrado = 0.0
    return round(max(0.0, costo - cobrado), 2)


def absorcion_fuera_de_tope(costo_envio: float, mercancia_pagada: float,
                            cobrado_al_cliente: float) -> float:
    """Cuánto se pasó la casa del tope del 10% en ESTE pedido. 0 si respetó la regla.

    No decide nada: mide. Sirve para que un envío que se traga el pedido se vea en
    la orden y en la bitácora en vez de desaparecer, mientras el cobro siga apagado.
    """
    absorbido = envio_que_absorbe_la_casa(costo_envio, cobrado_al_cliente)
    return round(max(0.0, absorbido - tope_que_absorbe_la_casa(mercancia_pagada)), 2)


# ------------------------------------------------- vigencia de una cotización
# Una cotización guardada solo vale unos minutos. No es burocracia: es lo que
# impide que alguien cotice hoy a $150, guarde el id y lo use dentro de un mes
# cuando la tarifa ya subió. Y el servidor SIEMPRE vuelve a mirar la cotización
# guardada; jamás cree el número que manda el navegador.
VIGENCIA_COTIZACION_MIN = 30
