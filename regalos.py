"""OBSEQUIOS del distribuidor y CARRITO COMPARTIBLE — la aritmética, sin base de datos.

Encargo de Christián (2026-08-01), en sus palabras:

    «Necesito que Mónica pueda agregar regalos por ejemplo agua bac o envío, etc.
     Quizás un código especial de Distribuidor que sí acepte stacking con los
     códigos de descuento PERO que no se muestre nunca al cliente en sus
     cotizaciones o cuando se comparta un carrito.»

Son TRES reglas y las tres viven aquí, no en el navegador:

  1. EL OBSEQUIO SE APILA. A diferencia de un cupón —que SUSTITUYE al código del
     distribuidor en el checkout (`create_order`: si hay cupón, `referrer` se
     anula)— el obsequio va ENCIMA del descuento del código. Por eso no es un
     cupón: es un atributo del carrito compartido, y el código `MONICAF-*` sigue
     cobrando y atribuyendo la venta igual que siempre.

  2. EL CÓDIGO DEL OBSEQUIO NO SE ENSEÑA JAMÁS. Vive en `gift_code` dentro del
     documento del carrito y NUNCA sale al cliente. El candado no es "acuérdate de
     borrarlo": es `vista_publica`, que arma el diccionario de respuesta DESDE CERO
     con una lista blanca de llaves. Un campo nuevo en el documento no se filtra
     por descuido — hay que escribirlo a mano en la lista para que salga.

  3. ⛔ EL REGALO NO ROMPE EL ROI. Regalar es descontar con otro nombre: un vial de
     cortesía es exactamente el mismo dinero que un descuento por su precio de
     lista. Por eso el obsequio SUMA al descuento y el total tiene que caber bajo
     el mismo tope que ya protege a la casa (`commission_cap` por producto) y bajo
     el techo del 40%. Si no cabe, el obsequio NO se aplica.

Módulo PURO a propósito, igual que `descuentos.py` y `envios.py`: no toca la base
ni la petición, así que se puede probar de verdad en vez de leer el texto de
`create_order`.
"""
import secrets

# ------------------------------------------------------------------ los tipos
# Un obsequio es UNA de dos cosas, y nada más:
#   · 'producto' — una pieza del catálogo de cortesía (el agua bacteriostática es
#     el caso que lo motivó: se vende casi al costo y por eso NUNCA lleva descuento,
#     pero regalarla sí se puede porque el costo se mide contra el pedido completo).
#   · 'envio'    — la casa absorbe la guía completa de ese pedido.
TIPO_PRODUCTO = 'producto'
TIPO_ENVIO = 'envio'
TIPOS = (TIPO_PRODUCTO, TIPO_ENVIO)

# Cuántos obsequios caben en un carrito. No es una regla de negocio: es el freno
# que impide que alguien mande diez mil renglones de cortesía en una petición.
MAX_OBSEQUIOS = 6

# Piezas máximas de UN obsequio de producto. Un regalo es un detalle, no un pedido.
MAX_PIEZAS_OBSEQUIO = 5

# El prefijo del código interno. Se elige distinto de los que ya existen
# (`VUELVE-`, `WA-`, `GIFT-`, `MONICAF-`) para que en la bitácora y en el panel se
# vea de un vistazo de dónde salió, y para que las pruebas puedan buscar esta
# palabra exacta en un payload público y truene si aparece.
PREFIJO_OBSEQUIO = 'DGIFT'


def nuevo_codigo_de_obsequio() -> str:
    """El código INTERNO del obsequio. `DGIFT-XXXXXXXXXXXX`.

    ⛔ Este texto NO se le enseña al cliente nunca — ni en el PDF, ni en el carrito
    compartido, ni en el WhatsApp. Existe para que el obsequio sea auditable por
    dentro (quién lo dio, en qué pedido se cobró) sin que el cliente aprenda que
    hay un código que podría reutilizar o pasarle a alguien más.

    `token_hex` y no `token_urlsafe`: sale en mayúsculas y sin guiones bajos, como
    el resto de los códigos de la casa.
    """
    return f'{PREFIJO_OBSEQUIO}-{secrets.token_hex(6).upper()}'


def nuevo_token_de_carrito() -> str:
    """El texto que viaja en el ENLACE que Mónica manda por WhatsApp.

    Es un identificador opaco y nada más: no dice quién lo hizo, qué trae dentro ni
    cuánto cuesta. Todo eso lo pone el servidor al abrirlo. 32 caracteres
    hexadecimales son 128 bits — no se adivina probando.
    """
    return secrets.token_hex(16)


def nueva_clave_de_prellenado() -> str:
    """LA SEGUNDA LLAVE del carrito compartido: la que abre los datos del cliente.

    Encargo de Christián (2026-08-01): «Cuando el cliente abre el link de la
    cotización, su nombre, email, teléfono, dirección, NADA se guardó. Necesito que
    corrijas esto si el distribuidor ya lo llenó por él.»

    ⛔ POR QUÉ SON DOS SECRETOS Y NO UNO. El token del carrito viaja en la RUTA
    (`/carrito/<token>`) y por eso queda escrito en los registros del servidor, del
    proxy y de cualquier intermediario. Esta clave viaja en el FRAGMENTO del enlace
    (`#d=<clave>`), la única parte de una dirección que el navegador NO manda a
    ningún servidor: no aparece en registros, ni en la cabecera `Referer` que se le
    filtra a terceros. Así, quien tenga el registro del servidor tiene el token —con
    el que ya podía ver productos y precios— pero JAMÁS lo que hace falta para leer
    un nombre, un teléfono o un domicilio.

    Y quien pruebe tokens al azar no saca nada: los datos personales no salen por
    `GET /carrito/{token}` (ver `vista_publica`, lista blanca), sólo por la ruta que
    exige esta clave. Adivinar las dos es adivinar 128 + 192 bits.
    """
    return secrets.token_urlsafe(24)


# ------------------------------------------------------------ qué vale un regalo
def valor_de_obsequios(obsequios, precio_de, costo_envio=0.0) -> float:
    """Cuánto DINERO regala este carrito, a precio de lista. En pesos.

    Es la cuenta que convierte «una cortesía» en un número comparable con el
    descuento. Sin ella el obsequio sería el único descuento del sistema que nadie
    mide, y por ahí se va el margen sin que aparezca en ningún reporte.

      · `precio_de(product_id)` devuelve el precio público de un producto, o 0 si
        no existe. Lo resuelve quien llama, contra el catálogo real.
      · `costo_envio` es lo que la guía le CUESTA a la casa, no lo que se le cobra
        al cliente: regalar el envío no vale «$0 porque ya era gratis», vale lo que
        la casa va a pagar de guía.
    """
    total = 0.0
    for o in (obsequios or []):
        if not isinstance(o, dict):
            continue
        tipo = str(o.get('tipo') or o.get('kind') or '').strip().lower()
        if tipo == TIPO_ENVIO:
            try:
                total += max(0.0, float(costo_envio or 0))
            except (TypeError, ValueError):
                pass
            continue
        if tipo != TIPO_PRODUCTO:
            continue
        try:
            piezas = int(o.get('cantidad') or o.get('quantity') or 1)
        except (TypeError, ValueError):
            piezas = 1
        piezas = max(0, min(MAX_PIEZAS_OBSEQUIO, piezas))
        try:
            precio = max(0.0, float(precio_de(o.get('product_id')) or 0))
        except (TypeError, ValueError):
            precio = 0.0
        total += precio * piezas
    return round(total, 2)


def piso_de_rentabilidad(items, tope_de, techo=0.40) -> float:
    """El MÁXIMO que este pedido puede regalar sin romper el ROI, en pesos.

    Dos candados a la vez, y manda el MÁS ESTRICTO:

      1. EL TOPE POR PRODUCTO. Cada renglón aguanta hasta `tope_de(item)` de su
         importe — es el mismo `commission_cap` que ya limita descuento + comisión
         en el checkout, el número que le conserva a la casa su 5x. Los insumos y
         el HGH neto dan CERO, así que no aportan ni un peso de margen que repartir.
      2. EL TECHO DEL 40%. El mismo `techo_de_descuento` por el que ya pasan todas
         las puertas de descuento del sistema. Sin él, un carrito de puros productos
         con tope de 50% permitiría regalar la mitad del pedido.

    `items` son los renglones REALES del pedido (los que se cobran), no los
    obsequios: el regalo se paga con el margen de lo que sí se vendió.
    """
    por_producto = 0.0
    lista = 0.0
    for it in items or []:
        get = (lambda k: getattr(it, k, None)) if not isinstance(it, dict) else it.get
        try:
            precio = max(0.0, float(get('price') or 0))
            piezas = max(0, int(get('quantity') or 0))
        except (TypeError, ValueError):
            continue
        importe = precio * piezas
        lista += importe
        try:
            tope = max(0.0, float(tope_de(it) or 0))
        except (TypeError, ValueError):
            tope = 0.0
        por_producto += importe * tope
    try:
        techo = max(0.0, float(techo or 0))
    except (TypeError, ValueError):
        techo = 0.0
    return round(min(por_producto, lista * techo), 2)


def cabe_el_obsequio(descuento_pesos, valor_obsequio, permitido_pesos) -> dict:
    """¿Cabe este regalo encima del descuento que ya se dio? Y por cuánto no cabe.

    ⛔ ÉSTA ES LA REGLA QUE IMPIDE QUE UN REGALO SE COMA EL PEDIDO. Regalar es
    descontar: el vial de cortesía y el 30% de descuento salen del MISMO margen. Se
    suman y se miden contra el mismo tope.

    Devuelve un diccionario, no un booleano, porque el «no» tiene que poder
    explicarse en pantalla: cuánto se regaló, cuánto se podía y por cuánto se pasó.
    """
    def _num(v):
        try:
            return max(0.0, float(v or 0))
        except (TypeError, ValueError):
            return 0.0

    descuento = _num(descuento_pesos)
    obsequio = _num(valor_obsequio)
    permitido = _num(permitido_pesos)
    entregado = round(descuento + obsequio, 2)
    # Un peso de tolerancia: los importes se redondean por renglón y no vale la pena
    # tumbar un regalo por un centavo de acumulación.
    cabe = entregado <= permitido + 1.0
    return {
        'cabe': cabe,
        'descuento': round(descuento, 2),
        'obsequio': round(obsequio, 2),
        'entregado': entregado,
        'permitido': round(permitido, 2),
        'exceso': 0.0 if cabe else round(entregado - permitido, 2),
    }


def limpiar_obsequios(crudos, existe_producto) -> list:
    """Normaliza lo que mandó el navegador. Lo que no se entiende se TIRA en silencio.

    Lo que sale de aquí ya es de fiar: tipo válido, producto que existe de verdad y
    cantidad dentro de rango. Se valida en el servidor porque el navegador de un
    distribuidor es tan público como el de un cliente — lo que llegue de allá es una
    petición, no un hecho.
    """
    limpios = []
    for o in (crudos or [])[:MAX_OBSEQUIOS * 4]:
        if not isinstance(o, dict):
            continue
        tipo = str(o.get('tipo') or o.get('kind') or '').strip().lower()
        if tipo == TIPO_ENVIO:
            if not any(x['tipo'] == TIPO_ENVIO for x in limpios):
                limpios.append({'tipo': TIPO_ENVIO, 'product_id': '', 'cantidad': 1})
            continue
        if tipo != TIPO_PRODUCTO:
            continue
        pid = str(o.get('product_id') or '').strip()
        if not pid or not existe_producto(pid):
            continue
        try:
            piezas = int(o.get('cantidad') or o.get('quantity') or 1)
        except (TypeError, ValueError):
            piezas = 1
        piezas = max(1, min(MAX_PIEZAS_OBSEQUIO, piezas))
        ya = next((x for x in limpios if x['product_id'] == pid), None)
        if ya:
            ya['cantidad'] = min(MAX_PIEZAS_OBSEQUIO, ya['cantidad'] + piezas)
        else:
            limpios.append({'tipo': TIPO_PRODUCTO, 'product_id': pid, 'cantidad': piezas})
        if len(limpios) >= MAX_OBSEQUIOS:
            break
    return limpios


# ------------------------------------------------- lo que SÍ ve el cliente
# ⛔ LISTA BLANCA, NO LISTA NEGRA. Ésta es la única puerta por la que un carrito
# compartido sale hacia el navegador de un cliente, y arma el diccionario DESDE
# CERO: lo que no esté escrito aquí abajo no sale, aunque mañana alguien guarde un
# campo nuevo en el documento. Al revés —borrando `gift_code` de una copia— el día
# que se agregue `gift_code_anterior` o `gift_note` se filtra sin que nadie lo note.
#
# ⛔ Y AQUÍ NO ENTRAN NI EL CORREO, NI EL TELÉFONO, NI EL DOMICILIO DEL CLIENTE
# (2026-08-01). Se guardan en el documento —para prellenarle el checkout— pero esta
# ruta es PÚBLICA y sin sesión: si salieran por aquí, probar tokens al azar sería
# una cosecha de domicilios. Salen por `datos_de_contacto`, que exige la segunda
# llave. `client_name` sí queda: es lo que ya se pintaba («Cotización para Ana») y
# sin él la pantalla no puede saludar a nadie.
LLAVES_PUBLICAS = (
    'token', 'folio', 'client_name', 'currency',
    'lines', 'gifts', 'list_total', 'discount', 'discount_rate',
    'shipping', 'shipping_free', 'shipping_pending', 'total', 'ref', 'expires_at',
)

# Los CUATRO datos que el distribuidor capturó por su cliente, y nada más. Misma
# técnica que arriba: se arma desde cero, así que un campo nuevo en el documento
# —el `gift_code`, por ejemplo— no se cuela por esta puerta tampoco.
LLAVES_DE_CONTACTO = ('client_name', 'client_email', 'client_phone', 'client_address')


def datos_de_contacto(doc) -> dict:
    """Los datos del cliente con los que se PRELLENA el checkout.

    ⛔ Quien llama es responsable de haber comprobado la segunda llave ANTES. Esta
    función no autoriza nada: sólo recorta. Devuelve los nombres que usa el
    formulario del checkout, no los del documento, para que la pantalla no tenga que
    traducir nada (y para que un campo nuevo del documento no se cuele por parecido).
    """
    doc = doc or {}
    return {
        'full_name': str(doc.get('client_name') or '')[:80],
        'email': str(doc.get('client_email') or '')[:120],
        'phone': str(doc.get('client_phone') or '')[:40],
        'address': str(doc.get('client_address') or '')[:200],
    }


def vista_publica(doc) -> dict:
    """El carrito compartido tal como lo ve el CLIENTE. Sin código de obsequio.

    ⛔ Aquí NO puede asomarse: el `gift_code`, el costo, el proveedor, el ROI, el
    tope de ningún producto, ni el nombre o el correo del distribuidor. El cliente
    ve productos, precios, su descuento, su envío y sus cortesías — nada más.

    Hay una prueba que lee el JSON entero de esta función como texto y truena si
    aparece `DGIFT`, el código guardado, o las palabras costo/proveedor/margen.
    """
    doc = doc or {}
    fuera = {}
    for llave in LLAVES_PUBLICAS:
        if llave in doc:
            fuera[llave] = doc[llave]
    # Los renglones y las cortesías se vuelven a armar campo por campo por la misma
    # razón: un renglón guardado puede traer el tope del producto y eso es del
    # negocio, no del cliente.
    fuera['lines'] = [{
        'product_id': str((ln or {}).get('product_id') or ''),
        'name': str((ln or {}).get('name') or ''),
        'quantity': int((ln or {}).get('quantity') or 0),
        'unit_price': (ln or {}).get('unit_price', 0),
        'list_price': (ln or {}).get('list_price', 0),
        'amount': (ln or {}).get('amount', 0),
    } for ln in (doc.get('lines') or [])]
    # La cortesía viaja con su NOMBRE y su etiqueta, jamás con su código.
    fuera['gifts'] = [{
        'tipo': str((g or {}).get('tipo') or ''),
        'name': str((g or {}).get('name') or ''),
        'quantity': int((g or {}).get('quantity') or 1),
    } for g in (doc.get('gifts') or [])]
    return fuera
