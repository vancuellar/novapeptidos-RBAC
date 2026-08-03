"""Envíos: cuánto pesa un pedido y quién paga la guía.

Aquí vive la ARITMÉTICA del envío, sin red y sin base de datos, para que se pueda
probar de verdad. Hablar con Skydropx es cosa de `skydropx.py`; decidir cuánto se
le cobra al cliente es cosa de este archivo.

✅ ESTO SÍ COBRA. Los dos interruptores de abajo están PRENDIDOS desde el
2026-08-01: el checkout cotiza con Skydropx y le cobra el envío al cliente, y al
confirmarse el pago el servidor compra la guía solo.

⛔ `COTIZAR_EN_CHECKOUT` no se apaga — la razón, con nombre y fecha, está en el
comentario que acompaña al interruptor. En corto: estuvo apagado desde el 28-jul
mientras el de comprar la guía sí estaba prendido, así que la casa pagó el envío
de cada pedido sin cobrarlo. Este encabezado decía «hoy esto no cobra nada» y
llevaba días siendo falso; se corrigió el 2026-08-01.
"""
import re

# --------------------------------------------------------------------- switches
# `COTIZAR_EN_CHECKOUT`: el checkout pide precios reales de Estafeta por CP y peso,
# los enseña, y el envío elegido se suma al total del pedido.
#
# `COMPRAR_GUIA_AL_PAGAR`: cuando el pago se confirma (tarjeta, SPEI, OXXO o
# cripto), el servidor compra la guía solo y la deja en el pedido.
#
# Son DOS y no uno porque son dos decisiones distintas: se puede querer comprar la
# guía automáticamente sin cobrarle el envío al cliente (la casa lo absorbe), pero
# no al revés — cobrar un envío que nadie compra es cobrar por nada.
#
# ⛔ `COTIZAR_EN_CHECKOUT` VA SIEMPRE PRENDIDO. NO SE APAGA.
#
# Orden de Christián del 2026-08-01, con estas palabras: «Yo jamás lo apagué.
# Préndelo y SIEMPRE debe estar prendido.»
#
# Nació apagado el 2026-07-28 (commit 3b28b35) como precaución mientras se
# estrenaba la integración con Skydropx, y ahí se quedó olvidado. El costo de ese
# olvido: durante esos días **la casa absorbió el envío de cada pedido** — se
# compraba la guía (`COMPRAR_GUIA_AL_PAGAR` sí estaba prendido) pero al cliente no
# se le cobraba un peso. Con guías de $165 a $250, eso es margen regalado en cada
# venta, y no se veía por ningún lado porque el checkout se comportaba «normal».
#
# Si alguna vez hay que apagarlo —una caída de Skydropx, por ejemplo— NO hace falta
# tocar esto: sin llave o sin respuesta de la paquetería el módulo ya se degrada
# solo y el checkout sigue vendiendo (ver `skydropx.py` y `envio_se_cotiza()`).
# Apagar el interruptor es otra cosa: es decidir no cobrar. Y eso lo decide él.
COTIZAR_EN_CHECKOUT = True
COMPRAR_GUIA_AL_PAGAR = True


# ------------------------------------------------ el tope de gasto de la guía
# ⛔ CUÁNTO PUEDE GASTAR EL SERVIDOR SOLO, SIN PREGUNTARLE A NADIE.
# Orden de Christián (2026-07-31). La compra automática es cómoda hasta el día que
# una guía sale en $900 y nadie se entera hasta ver el estado de cuenta. Arriba de
# este número el servidor NO compra: deja el pedido esperando y le avisa a Christián
# para que él dé el visto bueno desde el Panel.
#
# No es un límite de la paquetería: es el límite de LA CONFIANZA que se le da al
# automatismo. Subirlo es una decisión de dinero, por eso vive escrito aquí y no
# escondido dentro de una función.
TOPE_GUIA_AUTOMATICA_MXN = 400.0

# El mismo freno para un pedido EXPRESS (Christián, 2026-08-02): las guías de
# 1-2 días cuestan más y $400 las dejaría casi todas esperando su visto bueno.
# El estándar se queda en $400.
TOPE_GUIA_AUTOMATICA_EXPRESS_MXN = 600.0


def tope_guia_automatica(es_express: bool) -> float:
    """Cuánto puede gastar el servidor solo en UNA guía, según el tipo de envío."""
    return TOPE_GUIA_AUTOMATICA_EXPRESS_MXN if es_express else TOPE_GUIA_AUTOMATICA_MXN


# ------------------------------------------------------------------- el peso
# ⛔ AQUÍ SE DISTINGUE **DECLARADO** DE **ESTIMADO**, Y NO SE MEZCLAN NUNCA.
#
#   · DECLARADO = `weight_kg` del catálogo. Es lo que dio la BÁSCULA. Manda
#     siempre, sobre cualquier cuenta de este archivo.
#   · ESTIMADO  = lo que calculan las tablas de abajo a partir de la
#     presentación. Sirve para cotizar y para no dejar de vender; **no sustenta
#     una decisión de dinero**. `origen_del_peso()` dice cuál de los dos es, y
#     `/api/admin/envios/pesos` los enseña por separado en el Panel.
#
# ⛔ EL ESTIMADO NO ES UNA ADIVINANZA REDONDA: ES FÍSICA, Y SE PUEDE AUDITAR.
#
# Un vial de péptido liofilizado es, por peso, CASI PURO VIDRIO. El polvo son
# miligramos —10 mg de péptido pesan 0.01 g— y el frasco pesa entre 4 y 22 g
# según su tamaño. Por eso el peso se calcula del FORMATO DEL VIAL, no del
# compuesto: dos productos distintos en el mismo frasco pesan lo mismo.
#
# FUENTE DE LA MASA DEL VIDRIO (dato de fabricante, no cálculo de la casa):
# tabla «Dimensions, brimful capacity and mass» de la norma ISO 8362-1, tal como
# la publica SCHOTT Pharma en su ficha de Core Vials (StandardLine), columna
# «Mass ≈ g». Son los formatos R, los mismos que usa toda la industria.
VIAL_G = {
    '2R': 4.4, '3R': 5.5, '4R': 5.7, '6R': 7.9, '8R': 8.7, '10R': 9.5,
    '15R': 12.0, '20R': 16.2, '25R': 18.9, '30R': 21.9, '50R': 34.5,
    '100R': 60.0,
}

# El cuello del frasco, que decide qué tapón y qué sello le tocan. Sale de la
# misma tabla ISO (d2: 13 mm hasta el 10R, 20 mm del 15R en adelante).
VIAL_CUELLO_MM = {
    '2R': 13, '3R': 13, '4R': 13, '6R': 13, '8R': 13, '10R': 13,
    '15R': 20, '20R': 20, '25R': 20, '30R': 20, '50R': 20, '100R': 20,
}

# La capacidad al ras (ml) de cada formato, de la misma tabla. Es lo que decide
# en qué frasco cabe un líquido.
VIAL_ML = {
    '2R': 4.0, '3R': 5.0, '4R': 6.0, '6R': 10.0, '8R': 11.5, '10R': 13.5,
    '15R': 19.0, '20R': 26.0, '25R': 32.5, '30R': 37.5, '50R': 62.0,
    '100R': 123.0,
}

# ⚠️ ESTIMADO (no hay ficha de fabricante con gramos publicada). Tapón de hule
# butilo + sello de aluminio con botón de polipropileno, calculados por
# geometría y densidad: butilo ≈1.2 g/cm³, aluminio 2.7 g/cm³.
#   · cuello de 13 mm: tapón ≈0.9 g + sello ≈0.35 g
#   · cuello de 20 mm: tapón ≈2.6 g + sello ≈0.65 g
# Se redondea HACIA ARRIBA a propósito: quedarse corto se paga con recobro.
CIERRE_G = {13: 1.3, 20: 3.2}

# Etiqueta y burbuja. La etiqueta es papel/BOPP sobre el frasco; la burbuja es lo
# que se le enrolla a CADA pieza para que no se golpeen entre ellas.
ETIQUETA_G = {13: 0.2, 20: 0.3}
BURBUJA_POR_PIEZA_G = 2.0

# ⚠️ ESTIMADOS SIN MEDIR, y a propósito por lo alto: jeringas, agujas y toallitas
# se venden por paquete y no por pieza, y un kit trae dentro varias cosas. Aquí
# quedarse largo no cuesta nada (el mínimo facturable de 1 kg se los come) y
# quedarse corto sí. El día que haya báscula, se capturan y estos números dejan
# de usarse.
PESO_INSUMO_KG = 0.02          # jeringas, agujas, toallitas
PESO_KIT_KG = 0.30             # kits, stacks, combos

# El formato en el que va un producto cuando NO se le pudo leer la presentación.
# 3R (5 ml) es el frasco típico del péptido liofilizado de esta casa.
FORMATO_POR_OMISION = '3R'

# La cita, para que el estimado se pueda defender sin buscarla otra vez.
FUENTE_DEL_PESO = (
    'Masa del vidrio: ISO 8362-1, tabla «Dimensions, brimful capacity and mass», '
    'publicada por SCHOTT Pharma (Core Vials StandardLine). Tapón, sello, etiqueta '
    'y burbuja: calculados por geometría y densidad, redondeados hacia arriba. '
    'NADA de esto es una báscula: es un ESTIMADO.'
)


def peso_de_vial(formato: str) -> float:
    """Lo que pesa UN vial vacío de ese formato, ya vestido. En kg.

    Vidrio (ISO) + cierre + etiqueta + la burbuja con que se envuelve. Sin el
    polvo, que se suma aparte porque depende del producto.
    """
    formato = formato if formato in VIAL_G else FORMATO_POR_OMISION
    cuello = VIAL_CUELLO_MM[formato]
    gramos = (VIAL_G[formato] + CIERRE_G[cuello] + ETIQUETA_G[cuello]
              + BURBUJA_POR_PIEZA_G)
    return round(gramos / 1000.0, 4)


# Lo que pesa un vial de 3R vestido y vacío: el número por omisión de la casa.
# Se conserva el nombre viejo porque lo usan las pruebas y el resto del código.
PESO_VIAL_KG = peso_de_vial(FORMATO_POR_OMISION)

# El agua bacteriostática de 10 ml: frasco de 15R (19 ml al ras, cuello de 20 mm)
# más 10 g de líquido, que es lo que más pesa del renglón.
PESO_AGUA_KG = round(peso_de_vial('15R') + 10 / 1000.0, 4)

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


def peso_facturable(paquete: dict) -> float:
    """El peso por el que la paquetería va a COBRAR: el mayor entre real y volumétrico.

    ⛔ NO ES EL PESO QUE SE MANDA A COTIZAR. A Skydropx se le manda el peso real Y
    las medidas, y ella hace este mismo máximo por su cuenta — hacerlo aquí antes
    de mandarlo lo contaría dos veces. Esto es para MIRAR: en el panel y en la
    bitácora, «pesa 1 kg» y «te van a cobrar 1.8 kg» son dos frases distintas, y
    hasta hoy sólo se veía la primera.
    """
    paquete = paquete or {}
    def _n(k):
        try:
            return max(0.0, float(paquete.get(k) or 0))
        except (TypeError, ValueError):
            return 0.0
    return round(max(_n('peso_kg'), _n('peso_volumetrico_kg')), 2)


def _es(texto: str, *palabras: str) -> bool:
    t = (texto or '').lower()
    return any(p in t for p in palabras)


# «10 mg», «1,000 IU», «10ml», «0.1 mg». Se acepta la coma de los miles porque la
# maestra escribe «5,000IU» y sin esto se leería como 5.
_MEDIDA = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*(mg|mcg|µg|ug|g|ml|iu|ui)\b', re.I)


def contenido_declarado(texto: str) -> tuple:
    """Lee la presentación y devuelve (miligramos, mililitros). (0, 0) si no dice.

    Es la única lectura de texto que hay en el peso, y se hace UNA vez y aquí
    para que no se copie mal en tres lados.
    """
    mg = ml = 0.0
    for crudo, unidad in _MEDIDA.findall(texto or ''):
        try:
            n = float(crudo.replace(',', ''))
        except ValueError:
            continue
        u = unidad.lower()
        if u == 'mg':
            mg = max(mg, n)
        elif u in ('mcg', 'µg', 'ug'):
            mg = max(mg, n / 1000.0)
        elif u == 'g':
            mg = max(mg, n * 1000.0)
        elif u == 'ml':
            ml = max(ml, n)
        # IU/UI no dicen masa ni volumen: son unidades de actividad. Se ignoran a
        # propósito y el producto cae en el formato por omisión.
    return mg, ml


def formato_de_vial(mg: float = 0.0, ml: float = 0.0) -> str:
    """En qué frasco ISO va este contenido.

    Un líquido manda sobre el polvo: si son 10 ml, el frasco tiene que caberlos.
    Para el polvo se va por el volumen que ocupa el pastel liofilizado, que es lo
    que obliga a subir de frasco arriba de cierta cantidad. Se escoge el frasco
    que aguanta CON HOLGURA (≈70% de la capacidad al ras); errar hacia el frasco
    grande pesa de más, y pesar de más nunca produce un recobro.
    """
    try:
        ml = max(0.0, float(ml or 0))
    except (TypeError, ValueError):
        ml = 0.0
    try:
        mg = max(0.0, float(mg or 0))
    except (TypeError, ValueError):
        mg = 0.0

    if ml > 0:
        for formato in ('2R', '3R', '4R', '6R', '8R', '10R', '15R', '20R',
                        '25R', '30R', '50R', '100R'):
            if ml <= VIAL_ML[formato] * 0.7:
                return formato
        return '100R'

    if mg <= 0:
        return FORMATO_POR_OMISION
    # Los cortes salen de cuánto llena el liofilizado: hasta 30 mg cabe de sobra
    # en el 3R de 5 ml (el frasco estándar del péptido); de ahí para arriba el
    # polvo y su excipiente piden más volumen.
    if mg <= 30:
        return FORMATO_POR_OMISION
    if mg <= 120:
        return '6R'
    if mg <= 600:
        return '10R'
    return '20R'


def peso_estimado_de_pieza(doc: dict | None, nombre: str = '') -> float:
    """El peso CALCULADO de una pieza, en kg. Ignora `weight_kg` a propósito.

    Vive aparte de `peso_de_pieza` para que el Panel pueda enseñar el declarado y
    el estimado lado a lado y se vea de cuánto se equivocaba la cuenta.
    """
    doc = doc or {}
    texto = ' '.join(str(doc.get(k) or '')
                     for k in ('name', 'slug', 'category', 'presentation'))
    texto = f'{texto} {nombre or ""}'
    if _es(texto, 'jering', 'aguja', 'syringe', 'needle', 'toallit', 'alcohol'):
        return PESO_INSUMO_KG
    if _es(texto, 'kit', 'stack', 'combo', 'paquete'):
        return PESO_KIT_KG

    mg, ml = contenido_declarado(str(doc.get('presentation') or '') or texto)
    es_liquido = _es(texto, 'bacteriost', 'agua', 'water', 'solvente', 'diluyente')
    if es_liquido and ml <= 0:
        ml = 10.0                      # el frasco que se vende, si no lo dijo
    formato = formato_de_vial(mg=mg, ml=ml)
    peso = peso_de_vial(formato)
    # El contenido: el líquido pesa 1 g por ml; el polvo, sus miligramos.
    peso += (ml * 1.0 + mg / 1000.0) / 1000.0
    return round(peso, 4)


def peso_de_pieza(doc: dict | None, nombre: str = '') -> float:
    """Cuánto pesa UNA pieza de este producto, en kg.

    Si el catálogo trae `weight_kg` capturado, manda ese — siempre, porque ese es
    el de la báscula. Si no, se ESTIMA del formato del frasco que le toca a su
    presentación. Nunca devuelve cero: un renglón que pesa cero es un renglón que
    la paquetería no va a cobrar en cero.
    """
    doc = doc or {}
    try:
        capturado = float(doc.get('weight_kg') or 0)
    except (TypeError, ValueError):
        capturado = 0
    if capturado > 0:
        return capturado
    return peso_estimado_de_pieza(doc, nombre)


def origen_del_peso(doc: dict | None) -> str:
    """'declarado' si lo pesó una báscula, 'estimado' si lo calculó esta casa.

    ⛔ ES EL DATO QUE SEPARA UN NÚMERO DE UNA OPINIÓN. Un estimado cotiza y
    despacha, pero no sustenta una decisión de dinero.
    """
    try:
        return 'declarado' if float((doc or {}).get('weight_kg') or 0) > 0 else 'estimado'
    except (TypeError, ValueError):
        return 'estimado'


def piezas_sin_peso_declarado(items, pflags: dict | None = None) -> list:
    """Qué renglones de este pedido van con peso ESTIMADO. Lista de nombres.

    El checkout no se detiene por esto —vender siempre—, pero el pedido tiene que
    poder decir cuáles de sus pesos son cuenta y cuáles son báscula.
    """
    pflags = pflags or {}
    faltan = []
    for it in items or []:
        get = (lambda k: getattr(it, k, None)) if not isinstance(it, dict) else it.get
        doc = pflags.get(get('product_id') or '')
        if origen_del_peso(doc) == 'estimado':
            nombre = str(get('name') or (doc or {}).get('name') or '').strip()
            if nombre and nombre not in faltan:
                faltan.append(nombre)
    return faltan


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
    bulto = {
        'peso_kg': peso_del_pedido(items, pflags),
        'peso_volumetrico_kg': peso_volumetrico(caja),
    }
    return {
        'peso_kg': bulto['peso_kg'],
        'largo_cm': caja['largo_cm'],
        'ancho_cm': caja['ancho_cm'],
        'alto_cm': caja['alto_cm'],
        # Para que se vea en el panel y en la bitácora POR QUÉ costó lo que costó.
        'caja': caja.get('nombre', ''),
        'peso_contenido_kg': contenido,
        'peso_volumetrico_kg': bulto['peso_volumetrico_kg'],
        # Lo que la paquetería va a cobrar de verdad: el mayor de los dos de arriba.
        'peso_facturable_kg': peso_facturable(bulto),
        # ⛔ Qué renglones fueron con peso CALCULADO y no con báscula. Viaja con la
        # cotización y se guarda en el pedido para que un recobro por sobrepeso se
        # pueda ir a buscar al producto que lo causó, en vez de discutirlo de memoria.
        'pesos_estimados': piezas_sin_peso_declarado(items, pflags),
    }


def paquete_manual(peso_kg, largo_cm=0, ancho_cm=0, alto_cm=0) -> dict:
    """Un bulto CUALQUIERA, capturado a mano en el cotizador. Mismas llaves que arriba.

    ⛔ ESTE ES EL ÚNICO CAMINO EN EL QUE EL PESO LO PONE UNA PERSONA, y sólo puede
    existir porque lo que produce es una respuesta en pantalla: «¿cuánto costaría
    mandar esto?». NUNCA toca un pedido ni un cobro — el checkout pesa contra el
    catálogo con `paquete_del_pedido` y no mira nada de aquí. El día que alguien
    quiera cobrar con este peso, la respuesta es no.

    Lo que no venga o venga en basura se cae a algo defendible en vez de reventar:
    el mínimo que cobran todas las paqueterías (1 kg) y las medidas de la caja que
    le tocaría a ese peso. Una medida en cero haría que la paquetería cotizara
    contra nada y luego recobrara en el mostrador.
    """
    def _num(v):
        try:
            n = float(v or 0)
        except (TypeError, ValueError):
            return 0.0
        return n if n > 0 else 0.0

    peso = max(PESO_MINIMO_KG, _num(peso_kg))
    caja = caja_para(peso)
    bulto = {
        'largo_cm': _num(largo_cm) or caja['largo_cm'],
        'ancho_cm': _num(ancho_cm) or caja['ancho_cm'],
        'alto_cm': _num(alto_cm) or caja['alto_cm'],
    }
    paquete = {
        'peso_kg': round(peso, 2),
        **bulto,
        'caja': 'capturado a mano',
        'peso_contenido_kg': round(peso, 2),
        'peso_volumetrico_kg': peso_volumetrico(bulto),
    }
    paquete['peso_facturable_kg'] = peso_facturable(paquete)
    return paquete


# =========================================================================
#  EL EMPAQUE DE VERDAD: cuántas piezas caben en lo que Christián TIENE
# =========================================================================
# ⛔ ESTO NACE DE UN RECOBRO. Todo pedido se cotiza con la caja que le toca por
# peso, y como el peso de un vial es chiquito de verdad (9 g de vidrio, no los
# 0.05 kg redondos que se suponían antes del 2026-08-03), casi todo cae en la
# misma caja de 1 kg. Cuando el paquete de verdad no cabe ahí, la paquetería lo
# vuelve a pesar y le cobra la diferencia a la casa (recobro por sobrepeso), que es
# el cargo que aparece semanas después y que nadie cotizó. Por eso el bulto que se
# despacha sale de ESTA tabla —lo que Christián tiene en la bodega— y no del peso.
#
# ⛔ LO QUE HAY HOY, DICHO POR CHRISTIÁN (2026-07-31), SIN ADORNOS:
#
#     «Solo existe UN empaque: la bolsa blanca stand-up de 12×15×1 cm, y caben
#      unos 4 viales cómodamente. Nunca he mandado un pedido tan grande que
#      necesite caja.»
#
# O sea: NO HAY CAJAS. No las tiene, no sabe cuánto miden y no se van a inventar
# aquí — una medida inventada es exactamente lo que produce el recobro que esto
# viene a evitar. Por eso la tabla trae UN solo renglón y todo lo que no cabe en él
# NO se compra solo: se le pregunta al dueño qué empaque va a usar.
#
# ⛔ ES CONFIGURACIÓN, NO PROGRAMACIÓN. El día que compre cajas, captura sus
# medidas en el Panel (Admin → Envíos) y ese rango empieza a comprar solo, sin
# tocar código y sin desplegar. Mismo mecanismo que las cajas de cotización:
# `cargar_empaques_del_panel` empuja lo que guardó el admin y manda sobre esta tabla.
#
# `peso_facturable_kg` es 1 kg porque es el MÍNIMO que cobran todas las paqueterías:
# la bolsa con cuatro viales pesa ~200 g de verdad, así que ahí hay colchón de sobra
# y nunca puede haber recobro por peso en este rango.
EMPAQUES = (
    {'nombre': 'bolsa stand-up', 'hasta_piezas': 4,
     'largo_cm': 12, 'ancho_cm': 15, 'alto_cm': 1,
     'peso_facturable_kg': 1.0},
)

# Medidas de empaque puestas desde el Panel. Vacío = manda la tabla de arriba.
_EMPAQUES_DEL_PANEL: list = []


def cargar_empaques_del_panel(empaques) -> int:
    """Sustituye la tabla de empaques por la que capturó el admin. Devuelve cuántos quedaron.

    Se valida aquí, que es donde duele: un empaque con medidas en cero o sin tope de
    piezas haría que el servidor comprara guías contra basura. Lo que no sirve se tira
    en silencio; si no queda ninguno bueno, se devuelve el control a la tabla de fábrica
    en vez de dejar al sitio sin empaques (que sería no poder despachar nada).
    """
    global _EMPAQUES_DEL_PANEL
    buenos = []
    for e in (empaques or []):
        if not isinstance(e, dict):
            continue
        try:
            medidas = {k: float(e.get(k) or 0) for k in ('largo_cm', 'ancho_cm', 'alto_cm')}
        except (TypeError, ValueError):
            continue
        if any(v <= 0 for v in medidas.values()):
            continue
        try:
            tope = int(e.get('hasta_piezas') or 0)
        except (TypeError, ValueError):
            continue
        if tope <= 0:
            continue                # un empaque sin tope no dice nada: qué cabe es el dato
        try:
            peso = float(e.get('peso_facturable_kg') or 0)
        except (TypeError, ValueError):
            peso = 0.0
        buenos.append(dict(medidas, nombre=str(e.get('nombre') or 'empaque'),
                           hasta_piezas=tope,
                           peso_facturable_kg=max(PESO_MINIMO_KG, peso)))
    _EMPAQUES_DEL_PANEL = sorted(buenos, key=lambda e: e['hasta_piezas'])
    return len(_EMPAQUES_DEL_PANEL)


def empaques() -> tuple:
    """Los empaques vigentes: los del panel si los hay, si no los de fábrica."""
    return tuple(_EMPAQUES_DEL_PANEL) if _EMPAQUES_DEL_PANEL else EMPAQUES


def piezas_del_pedido(items) -> int:
    """Cuántas PIEZAS lleva el pedido en total. Es lo que decide el empaque.

    Se cuentan TODAS las piezas, no sólo los viales: un frasco de agua de 30 ml y una
    jeringa ocupan lugar en la bolsa igual que un vial, y quien decide si cabe es el
    bulto, no la etiqueta del producto. Contar de más se equivoca hacia el lado bueno —
    manda el pedido a revisión humana— y contar de menos se equivoca hacia el recobro.
    """
    total = 0
    for it in items or []:
        get = (lambda k: getattr(it, k, None)) if not isinstance(it, dict) else it.get
        try:
            total += max(0, int(get('quantity') or 0))
        except (TypeError, ValueError):
            continue
    return total


def empaque_para(piezas: int) -> dict | None:
    """En qué empaque cabe este pedido. **None cuando no cabe en ninguno.**

    ⛔ EL `None` ES EL FRENO, no un error. Significa "esto no entra en nada de lo que
    hay en la bodega": el servidor NO compra la guía sola y le pregunta a Christián qué
    empaque va a usar. Es exactamente lo que él pidió para los pedidos de 5 piezas o
    más, que hoy no tienen empaque que los reciba.
    """
    try:
        n = int(piezas or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for e in empaques():
        if n <= int(e.get('hasta_piezas') or 0):
            return dict(e)
    return None


def paquete_de_empaque(empaque: dict) -> dict:
    """Traduce un empaque a lo que la paquetería necesita saber del bulto.

    Mismas llaves que `paquete_del_pedido` para que los dos caminos —el automático y
    el que despacha el admin a mano— hablen el mismo idioma río abajo.
    """
    empaque = empaque or {}
    paquete = {
        'peso_kg': max(PESO_MINIMO_KG, float(empaque.get('peso_facturable_kg') or 0)),
        'largo_cm': empaque.get('largo_cm'),
        'ancho_cm': empaque.get('ancho_cm'),
        'alto_cm': empaque.get('alto_cm'),
        'caja': empaque.get('nombre', ''),
        'peso_volumetrico_kg': peso_volumetrico(empaque),
    }
    paquete['peso_facturable_kg'] = peso_facturable(paquete)
    return paquete


# ------------------------------------------------- quién paga el envío
# ⛔ LA POLÍTICA DE ENVÍO, DICTADA POR CHRISTIÁN EL 2026-07-31, EN SUS PALABRAS:
#
#   «La política de envío será gratis siempre y cuando el ticket supere los $2,500
#    de compra mínima y/o no sea mayor a 5% del total de la compra. Primero se debe
#    cumplir la compra mínima. De otra manera, se cobra un flat fee.»
#
# Son DOS candados y van EN ESTE ORDEN, que es lo que él subrayó:
#
#   1º LA COMPRA MÍNIMA ($2,500). Sin ella no hay beneficio de ningún tipo: el
#      pedido paga su tarifa plana y ya. Un pedido de $879 nunca lleva envío gratis
#      por barata que salga la guía.
#   2º EL 5%. Cumplida la mínima, la casa absorbe la guía SOLO hasta el 5% de lo que
#      el cliente pagó de mercancía. Si la guía cuesta más, el excedente lo paga él.
#
# ⚠️ LO QUE ESTO SIGNIFICA EN LA CAJA, para que nadie se sorprenda: el tope bajó de
# 10% a 5% este mismo día. Con una guía de $250, el 5% no alcanza a taparla hasta los
# $5,000 de compra. O sea que entre $2,500 y $5,000 el cliente ya NO paga $0 sino la
# diferencia (en $3,000 son $100; en $4,000 son $50). Arriba de $5,000, gratis de
# verdad. Es exactamente la regla que pidió el dueño y es la que protege el margen.
TOPE_ENVIO_SOBRE_COMPRA = 0.05

# LA COMPRA MÍNIMA, EN PESOS Y ESCRITA A MANO A PROPÓSITO.
#
# Hasta hoy el umbral se DERIVABA del costo de la guía (`SHIPPING_FLAT / TOPE` en
# server.py, o sea 250 / 10% = 2,500). Esa cuenta era elegante mientras el tope fue
# del 10%, pero con el 5% habría movido la mínima sola de $2,500 a $5,000 sin que
# nadie lo pidiera — y Christián dictó la mínima en pesos, no como una consecuencia
# del tope. Por eso ahora son dos números independientes: cambiar uno no mueve al
# otro en silencio.
COMPRA_MINIMA_ENVIO_GRATIS = 2500

# ✅ DECIDIDO POR CHRISTIAN el 2026-07-28 y RATIFICADO el 2026-07-31, en sus palabras:
# "En un pedido de más de 2.5k pesos donde el envío pasa del [tope], el cliente paga
#  la diferencia y la casa absorbe hasta el [tope] del costo del envío máximo."
#
# O sea: en un pedido de $3,000 con envío de $600, la casa pone $150 (su 5%) y el
# cliente paga los otros $450. La casa NUNCA absorbe más del tope de la compra, y el
# cliente nunca paga el envío completo si ya pasó de la compra mínima.
#
# Para cambiarlo: esta línea, nada más. No hay otro lugar donde se decida.
CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE = False

# ✅ LA ESTRATEGIA DEL 2026-08-02, dictada por Christián con todas sus letras:
#
#   · El cliente YA NO ESCOGE paquetería: la casa la elige. Él ve dos cosas:
#     ESTÁNDAR (3-5 días hábiles) y EXPRESS (1-2 días hábiles).
#   · Estándar: $250 parejo abajo de la compra mínima; INCLUIDO desde $2,500.
#   · EL PISO DE ABSORCIÓN: desde la mínima, la casa absorbe la guía hasta
#     $250 **o el 5% de la compra, LO QUE SEA MAYOR**. Con guías reales de
#     $139-$165 esto es «gratis parejo desde $2,500»; el excedente sólo existe
#     para la guía monstruosa que un día salga en $800 o $4,000 (su ejemplo:
#     compra de $40,000 → la casa absorbe hasta $2,000).
#   · Express: EXTRA_EXPRESS_MXN encima de lo anterior, y SE COBRA SIEMPRE,
#     incluido el pedido con envío gratis. ⛔ Nunca se promete «1 día»: en
#     México no existe (sus palabras); la realidad son 36-48 horas hábiles y
#     por eso la promesa en pantalla es «1-2 días hábiles».
PISO_ABSORCION_MXN = 250.0
EXTRA_EXPRESS_MXN = 150.0

# El plazo que define un servicio EXPRESS al elegir la guía (días prometidos por
# la paquetería). El estándar sigue siendo DIAS_MAXIMOS_ENTREGA (5) de skydropx.py.
DIAS_MAXIMOS_EXPRESS = 2


def cobro_de_envio_al_cliente(costo_envio: float, mercancia_pagada: float,
                              envio_gratis_desde: float,
                              tarifa_plana: float | None = None) -> float:
    """Cuánto de la guía se le cobra al cliente. Devuelve pesos, nunca negativos.

    Tres casos, EN ESTE ORDEN — el orden es la regla, no un detalle:

      1. PRIMERO LA COMPRA MÍNIMA. Abajo del umbral el pedido paga su tarifa plana
         (o, si no se le pasa ninguna, la guía completa). Absorber $250 en un pedido
         de $879 se come el 28% del ingreso. Aquí el 5% ni se mira: Christián fue
         explícito en que la mínima se cumple primero.
      2. Compra grande y envío barato (≤ 5% de lo que pagó): GRATIS, la casa lo
         absorbe. Es la promesa que se le hace al cliente.
      3. Compra grande y envío caro (> 5%): la casa absorbe su 5% y el cliente paga
         la diferencia. (La regla contraria —que pague todo— se prende con la
         constante de arriba.)

    `tarifa_plana` es lo que se COBRA abajo de la mínima cuando eso es un precio de
    la casa y no el costo de la guía. Se dejó opcional a propósito: el camino de
    Skydropx no la pasa porque ahí, abajo de la mínima, el cliente paga la guía real
    que se cotizó, que es justo lo que cuesta.

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
        if tarifa_plana is None:
            return round(costo)
        try:
            plana = float(tarifa_plana)
        except (TypeError, ValueError):
            plana = -1.0
        # Una tarifa en negativo o con basura NO puede dejar el envío regalado: se
        # cae al costo de la guía, que es el número que protege a la casa.
        return round(plana) if plana >= 0 else round(costo)
    # EL PISO DE ABSORCIÓN (Christián, 2026-08-02): la casa absorbe hasta $250 o
    # el 5% de la compra, lo que sea MAYOR. Sin el piso, entre $2,500 y $5,000
    # había una franja donde el cliente pagaba un pico ($40-$125) y el «gratis
    # desde $2,500» era letra chica. Con el piso, la guía normal ($139-$250) sale
    # gratis parejo desde la mínima, y el 5% sólo entra a proteger a la casa
    # cuando la guía es monstruosa en un pedido grande.
    tope = max(PISO_ABSORCION_MXN, mercancia * TOPE_ENVIO_SOBRE_COMPRA)
    if costo <= tope:
        return 0.0
    if CLIENTE_PAGA_EL_ENVIO_COMPLETO_AL_PASAR_EL_TOPE:
        return round(costo)
    return round(costo - tope)


def tope_que_absorbe_la_casa(mercancia_pagada: float) -> float:
    """El MÁXIMO de envío que la casa está dispuesta a comerse en un pedido.

    Desde el 2026-08-02: $250 o el 5% de lo que pagó de mercancía, LO QUE SEA
    MAYOR (el mismo piso de `cobro_de_envio_al_cliente` — si este espejo no lo
    llevara, los reportes gritarían «fuera de tope» por guías que la regla nueva
    sí absorbe). Palabras de Christian sobre el 5%: «si una compra por 2,500
    genera un costo de envío de $500 ni en pedo lo pago».
    """
    try:
        mercancia = max(0.0, float(mercancia_pagada or 0))
    except (TypeError, ValueError):
        mercancia = 0.0
    return round(max(PISO_ABSORCION_MXN, mercancia * TOPE_ENVIO_SOBRE_COMPRA), 2)


def envio_que_absorbe_la_casa(costo_envio: float, cobrado_al_cliente: float) -> float:
    """Lo que la guía le cuesta a la casa DESPUÉS de lo que pagó el cliente.

    El espejo de `cobro_de_envio_al_cliente`. Existe porque el número que duele no
    es el que se cobra sino el que NO se cobra. Nació cuando el checkout no cobraba
    envío: la casa absorbía el 100% de cada guía y en el pedido eso se guardaba como
    $0 — un pedido de $179 se llevaba $250 de envío, el 140%, y no aparecía en ningún
    reporte. Hoy el cobro está prendido, pero cada pedido con envío gratis sigue
    dejando aquí lo que la casa se comió, que es lo que hay que poder sumar.
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
    """Cuánto se pasó la casa del tope del 5% en ESTE pedido. 0 si respetó la regla.

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
