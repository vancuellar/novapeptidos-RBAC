from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional
from datetime import datetime, timezone
import uuid


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- Auth ----------
class RegisterInput(BaseModel):
    name: str
    email: EmailStr
    password: str = Field(min_length=6)
    language: str = 'es'   # es | en | pt — UI language at signup, drives email language
    distributor_code: Optional[str] = None   # si el cliente viene referido por un distribuidor
    # Consentimientos. Los dos primeros son obligatorios y el servidor los exige:
    # no basta con validarlos en el navegador porque el API es público.
    age_confirmed: bool = False      # 18+ y acepta Términos y Condiciones
    privacy_accepted: bool = False   # acepta la Política de privacidad
    marketing_email: bool = False
    promos: bool = False             # bonos y campañas


class LoginInput(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordInput(BaseModel):
    email: EmailStr
    language: str = 'es'


class AddressInput(BaseModel):
    address: str = ''
    # Segunda línea: interior, departamento, entre calles, referencia. Va aparte
    # porque metida en `address` la paquetería la imprime pegada al número y el
    # repartidor no la lee. Opcional siempre.
    address_2: str = ''
    city: str = ''
    state: str = ''
    postal_code: str = ''
    country: str = 'MX'  # ISO-3166 alfa-2; México por default


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    shipping_address: Optional[AddressInput] = None
    billing_address: Optional[AddressInput] = None
    preferred_payment: Optional[str] = None   # mercado_pago | tarjeta | oxxo | spei | contra_entrega
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None    # requerido solo si cambia el correo


class ChangePasswordInput(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


class ResetPasswordInput(BaseModel):
    token: str
    password: str = Field(min_length=6)


class TokenInput(BaseModel):
    token: str


class ActivateInput(BaseModel):
    """Activación desde una invitación: el usuario elige su propia contraseña.
    Nunca mandamos una contraseña por correo."""
    token: str
    password: str = Field(min_length=6)
    # ⛔ ACUERDO DE DISTRIBUIDOR — la casilla de la pantalla de activación.
    # `False` por omisión a propósito: es la casilla NO PREMARCADA que exige el
    # art. 93 Bis del Código de Comercio. Un cuerpo que no la mande NO firma.
    # Mientras el interruptor esté apagado el servidor la ignora por completo.
    acepta_acuerdo: bool = False
    acuerdo_version: Optional[str] = None


class AceptarAcuerdoInput(BaseModel):
    """La firma desde el panel: casilla + versión que el usuario tenía leída.

    `acepto` NO tiene valor por omisión distinto de False, y `version` viaja para
    que el servidor rechace la firma si el texto cambió mientras la pantalla
    estaba abierta. Nadie firma un documento distinto del que leyó."""
    acepto: bool = False
    version: Optional[str] = None


class ResendVerificationInput(BaseModel):
    email: EmailStr
    language: str = 'es'


# ---------- Products ----------
class PriceTier(BaseModel):
    min_qty: int
    price: float


class ProductBase(BaseModel):
    name: str
    slug: str
    category: str
    short_description: str = ''
    description: str = ''
    presentation: str = ''      # e.g. '10 mg / vial'
    form: str = 'Liofilizado'
    purity: str = '99%'
    price: float
    tiers: List[PriceTier] = []
    stock: int = 0
    image_url: str = ''
    coa_url: str = ''
    batch_number: str = ''
    storage: str = 'Conservar a -20 C, protegido de la luz.'
    featured: bool = False
    is_new: bool = False
    # Tope de comisión por producto (escalera ROI de la maestra) y si el producto
    # puede venderse por distribuidores. Si no deja 5x neto: SOLO venta directa.
    # SKU: codigo unico y estable de cada presentacion (BPC157-5MG). Es la llave
    # que usa el carrito; sin el, el front inventaba ids tipo "slug::5 mg" que no
    # existian en la base (bug de checkout, Christian 2026-07-25).
    sku: str = ''
    commission_cap: float = 0.50
    distributor_eligible: bool = True
    # Peso de UNA pieza, en kilos, para cotizar el envío por peso real.
    # ⚠️ PENDIENTE DE CHRISTIAN: capturar los reales. Mientras venga en 0, el
    # envío usa el peso por omisión del tipo de presentación (ver envios.py).
    weight_kg: float = 0
    # Un producto vive en su `category` principal y, opcionalmente, aparece
    # también en estas otras (p. ej. un combo en su categoría funcional Y en
    # "stacks"). Christian 2026-07-23.
    extra_categories: List[str] = []
    # Fuera del catálogo público sin borrarlo: no se lista ni se abre por slug, pero
    # sigue en la base con su SKU y su historial de pedidos. Se agregó para Dysport
    # (toxina botulínica: venta con receta en México y no es un péptido de
    # investigación) — Christian, 2026-07-27.
    hidden: bool = False


class ProductCreate(ProductBase):
    pass


class Product(ProductBase):
    model_config = ConfigDict(extra='ignore')
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(default_factory=now_iso)


class ProductUpdate(BaseModel):
    # `hidden` saca el producto del catálogo público SIN borrarlo: sigue en la base,
    # con su historial de pedidos y su SKU, pero no se lista ni se puede abrir por
    # slug. Se agregó para Dysport (toxina botulínica: en México es venta con receta
    # y no es un péptido de investigación) — Christian, 2026-07-27.
    hidden: Optional[bool] = None
    sku: Optional[str] = None
    commission_cap: Optional[float] = None
    distributor_eligible: Optional[bool] = None
    extra_categories: Optional[List[str]] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    category: Optional[str] = None
    short_description: Optional[str] = None
    description: Optional[str] = None
    presentation: Optional[str] = None
    form: Optional[str] = None
    purity: Optional[str] = None
    price: Optional[float] = None
    tiers: Optional[List[PriceTier]] = None
    stock: Optional[int] = None
    image_url: Optional[str] = None
    coa_url: Optional[str] = None
    batch_number: Optional[str] = None
    storage: Optional[str] = None
    featured: Optional[bool] = None
    is_new: Optional[bool] = None
    weight_kg: Optional[float] = None    # kilos por pieza; 0 = usar el peso por omisión


# ---------- Categories ----------
class Category(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    slug: str
    description: str = ''
    icon: str = 'FlaskConical'


# ---------- Orders ----------
class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    quantity: int
    presentation: str = ''
    image_url: str = ''


class CustomerInfo(BaseModel):
    full_name: str
    email: EmailStr
    phone: str
    address: str
    address_2: str = ''   # interior / depto / referencia — opcional
    city: str = ''
    state: str = ''
    postal_code: str = ''
    country: str = 'MX'  # ISO-3166 alfa-2; México por default
    notes: str = ''


class Attribution(BaseModel):
    """De dónde salió el cliente. PRIMER TOQUE: el anuncio que lo TRAJO, no la
    última pestaña que tenía abierta.

    Sin esto guardado en el pedido, el gasto de Meta y las ventas viven en dos
    mundos que no se tocan: se puede saber cuánto se gastó y cuánto se vendió,
    pero NO cuánto costó cada cliente que de verdad compró. Christian, 2026-07-26.

    `utm_content` es el ANUNCIO concreto dentro de la campaña, y `fbclid` es la
    red de seguridad: Meta se lo pega solo a los enlaces de sus anuncios aunque
    nadie los haya etiquetado.
    """
    model_config = ConfigDict(extra='ignore')
    utm_source: str = ''
    utm_medium: str = ''
    utm_campaign: str = ''
    utm_content: str = ''
    utm_term: str = ''
    fbclid: str = ''
    referrer: str = ''
    landing_path: str = ''
    first_seen: str = ''
    visitor_id: str = ''
    session_id: str = ''


class OrderCreate(BaseModel):
    items: List[OrderItem]
    customer: CustomerInfo
    payment_method: str   # tarjeta | spei
    shipping: float = 0
    discount: float = 0                      # informativo; el servidor recalcula con su propia regla
    distributor_code: Optional[str] = None   # referido por un distribuidor (atribuye la venta)
    points_to_use: int = 0                   # puntos de lealtad a canjear; el servidor valida saldo
    # Cotización de envío que eligió el cliente. Viaja el ID, NUNCA el precio: el
    # servidor va por el precio a la cotización que ÉL guardó. Ver `shipping_quotes`
    # en server.py. Lo que el navegador mande en `shipping` se sigue ignorando.
    shipping_quote_id: Optional[str] = None
    # ⛔ EL CLIENTE YA NO ESCOGE PAQUETERÍA (Christián, 2026-08-02): escoge el TIPO.
    # ESTÁNDAR (3-5 días hábiles, $250 o incluido según el importe) o EXPRESS
    # (1-2 días hábiles, +$150 SIEMPRE). Del navegador viaja sólo esta bandera;
    # el monto lo pone el servidor con la regla de la casa (`_envio_del_pedido`).
    shipping_express: bool = False
    # EL CARRITO COMPARTIDO que le mandó su distribuidora por WhatsApp. Viaja el
    # TOKEN, nunca el regalo ni su valor: el servidor abre el documento, revalida el
    # obsequio contra el ROI de ESTE pedido y sólo entonces lo aplica. Un token
    # inventado no encuentra documento y la compra sigue como una compra normal.
    shared_cart_token: Optional[str] = None
    attribution: Optional[Attribution] = None
    # Cuándo aceptó el comprador el aviso 18+/RUO en la puerta del sitio (ISO).
    # Antes se le volvía a pedir lo MISMO con una casilla en el checkout, y sin
    # marcarla el botón de pagar parecía muerto; la casilla se quitó y quedó esto,
    # que es lo único que aportaba: la constancia. Christian, 2026-07-28.
    terms_accepted_at: str = ''
    # ⛔ ENVÍO PARTIDO: LO ELIGE EL CLIENTE, NO NOSOTROS (Christián, 2026-07-31).
    #
    # Cuando el pedido no se puede surtir completo hay dos formas de atenderlo y las
    # dos son razonables: mandar lo que hay YA (2-5 días) y el resto después, o
    # esperar a tenerlo todo junto (~1 semana). Hasta hoy la casa decidía sola y
    # siempre partía; a quien tenía prisa le servía y a quien no quería dos entregas
    # le molestaba, y nadie le preguntó.
    #
    #   'partido' → manda lo disponible ya; el resto en un segundo envío (con su
    #               propia guía y su propio aviso). ES EL VALOR POR OMISIÓN, porque es
    #               lo que se hacía hasta hoy y porque un pedido completo nunca se
    #               parte: sin faltantes esta decisión no cambia nada.
    #   'completo' → no sale nada hasta que esté todo. Una sola guía.
    #
    # Lo que el navegador manda aquí es una PREFERENCIA, no dinero: no mueve un peso
    # del total. Por eso se acepta tal cual, a diferencia del precio o del envío.
    shipping_preference: str = 'partido'


class Order(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_number: str
    user_id: Optional[str] = None
    items: List[OrderItem]
    customer: CustomerInfo
    payment_method: str
    subtotal: float
    discount: float = 0         # descuento automatico por volumen (10/15/20%)
    # El 5% por pagar en cripto, APARTE del descuento comercial (Christián,
    # 2026-08-03). Son dos dineros con origen distinto: aquél sale del margen del
    # producto y por eso está topado al 40%; éste sale de la comisión de Mercado Pago
    # que el pedido no paga. Juntarlos en un solo campo haría imposible saber cuánto
    # costó de verdad la promoción. Ver descuento_cripto.py.
    crypto_discount: float = 0
    # La MAYOR tasa concedida a algún renglón de este pedido. Con un carrito parejo
    # —todo lo que existía antes de la REGLA DE 5— es la tasa de siempre. Lo leen los
    # puntos (el 40% no genera), las comisiones viejas y los reportes.
    discount_rate: float = 0
    # Renglones que NO recibieron el descuento completo porque su tope de producto
    # no lo aguanta (o porque son insumos, que nunca entran). Solo para explicarle
    # al cliente por que su codigo dio menos en esos productos.
    discount_capped: List[dict] = []
    # DESGLOSE FINO: qué tasa pidió y qué tasa recibió CADA renglón. Existe desde la
    # REGLA DE 5 (Christián, 2026-07-30), donde dos renglones del mismo pedido pueden
    # llevar descuentos distintos y `discount_rate` sola ya no cuenta la historia
    # completa. [{product_id, name, quantity, asked_rate, applied_rate}]
    discount_lines: List[dict] = []
    # Los productos de una COMPRA PROPIA de distribuidor que se quedaron en 1-4 piezas
    # y por eso pagaron precio de cliente. Es el empujón del carrito: «llevas 3 de 5».
    # [{product_id, name, quantity, faltan, minimo}]
    regla_de_5: List[dict] = []
    # ⛔ LAS CORTESÍAS DEL DISTRIBUIDOR (Christián, 2026-08-01). Lo que se obsequió en
    # este pedido, en pesos y renglón por renglón. Es dinero regalado y por eso se
    # guarda: sin este número, «Mónica regala agua» no aparece en ningún reporte.
    #
    # ⛔ AQUÍ NO SE GUARDA EL CÓDIGO DEL OBSEQUIO, A PROPÓSITO. El pedido se le enseña
    # al cliente (ficha de pedido, correo de confirmación, panel de su cuenta), así
    # que cualquier cosa escrita aquí es cosa que él puede leer. El rastro para
    # auditar va por `shared_cart_token`, que sí es suyo y ya conoce: de ahí sale el
    # `gift_code` en `shared_carts`, que nunca cruza la puerta.
    gift_discount: float = 0
    gift_lines: List[dict] = []         # [{product_id, name, quantity, list_price}]
    gift_shipping: bool = False         # la guía fue de cortesía
    shared_cart_token: str = ''
    shipping: float
    # El TIPO de envío que eligió el cliente (2026-08-02): express = 1-2 días
    # hábiles con su extra ya cobrado en `shipping`. Lo lee la compra automática
    # de la guía (elige servicio rápido y su tope de gasto es el de express).
    shipping_express: bool = False
    total: float
    status: str = 'pendiente'   # pendiente | confirmado | enviado | entregado | cancelado
    # ⛔ PAGADO ES OTRA COSA QUE ENTREGADO (Christián, 2026-07-29).
    # El estado de arriba cuenta el viaje de la MERCANCÍA; éste cuenta si entró el
    # DINERO. Se separaron porque Christián entrega en persona y a veces cobra
    # después: la venta de Alanís salió entregada y sin pagar, y el tablero la
    # contaba como ingreso. Un reporte que dice que cobraste lo que no cobraste es
    # peor que no tener reporte.
    paid: bool = False
    paid_at: Optional[str] = None
    referred_by: Optional[str] = None   # id del distribuidor cuyo código se usó (si aplica)
    # ⛔ EL TEXTO DEL CUPÓN, ESCRITO EN EL PEDIDO (2026-07-31).
    #
    # Hasta hoy el vínculo cupón→venta vivía SÓLO al revés, en el cupón
    # (`used_order`), y eso sólo funciona con cupones de UN SOLO USO: se queman al
    # cobrarse y guardan el pedido que se los llevó. Un cupón de CAMPAÑA —el mismo
    # texto repartido en cien conversaciones de WhatsApp— no se quema nunca, así
    # que no tenía dónde apuntar sus ventas: `_ventas_por_cupon` lo contaba como
    # «mandado y jamás usado» aunque hubiera vendido.
    #
    # Con el texto aquí, la pregunta que Christián no podía contestar —«¿las 110
    # conversaciones de WhatsApp se volvieron ventas?»— se contesta con un conteo,
    # y sirve igual para los de un solo uso. Es un CÓDIGO, no una persona: no dice
    # de quién es (por eso el prefijo público es de la casa, no del distribuidor).
    coupon_code: str = ''
    commission: float = 0               # tajada del VENDEDOR en esta orden (MXN) — compat
    # Pirámide: reparto completo bloqueado al crear la orden. Cada fila es
    # {distributor_id, role: 'seller'|'override', rate, amount(MXN)}. Los reportes
    # suman lo guardado, así que cambiar tasas/niveles nunca toca ventas pasadas.
    commissions: List[dict] = Field(default_factory=list)
    # Lealtad: canje descontado al crear; los ganados se depositan al confirmarse el pago
    points_used: int = 0
    points_earned: int = 0
    points_awarded: bool = False
    points_refunded: bool = False
    # Cripto: factura del proveedor (NOWPayments/BTCPay) y momento de pago
    crypto_provider: str = ''
    crypto_invoice_id: str = ''
    paid_at: Optional[str] = None
    # Envío / rastreo
    carrier: str = ''                   # FedEx, Estafeta, DHL...
    tracking_number: str = ''
    tracking_url: str = ''
    shipped_at: Optional[str] = None
    delivered_at: Optional[str] = None
    eta: str = ''                       # texto libre: '3-5 días hábiles'
    # Skydropx: qué se cotizó, qué se cobró y qué guía se compró sola al pagarse.
    # `shipping_quote` guarda la cotización COMPLETA tal como la validó el servidor
    # (paquetería, servicio, precio real, peso) — no lo que dijo el navegador.
    shipping_quote: dict = Field(default_factory=dict)
    shipping_cost: float = 0            # lo que cuesta la guía de verdad
    shipping_absorbed: float = 0        # lo que la casa absorbió del envío
    # Cuánto se pasó esa absorción del tope del 10% de la compra (regla de Christian).
    # Cero cuando se respeta. Sin este número, un envío que se traga el pedido no
    # existe en ningún reporte: un pedido de $179 con guía de $250 se veía en $0.
    shipping_over_cap: float = 0
    label_url: str = ''                 # PDF de la guía
    label_provider: str = ''            # 'skydropx'
    label_error: str = ''               # por qué no se pudo comprar sola (si pasó)
    # ⛔ POR QUÉ LA GUÍA NO SE COMPRÓ SOLA, cuando fue A PROPÓSITO y no un fallo.
    # Son cosas distintas y se atienden distinto: un `label_error` se reintenta solo,
    # un `label_hold` espera una decisión de Christián y no tiene caso reintentarlo.
    #   'sin_empaque'            → lleva más piezas de las que caben en lo que hay
    #   'sobre_tope'             → la guía se pasa del tope de gasto automático
    #   'espera_pedido_completo' → el cliente pidió que todo llegue junto
    label_hold: str = ''
    label_lock: bool = False            # candado atómico: nunca dos guías del mismo pedido
    label_intentos: int = 0             # cuántas veces falló la compra automática
    label_ultimo_intento: str = ''
    label_piezas: int = 0               # cuántas piezas se contaron para elegir empaque
    label_empaque: str = ''             # en qué se mandó
    label_precio_cotizado: float = 0    # lo que costaba cuando se pasó del tope
    # Qué correos ya recibió quien compró. ⛔ ES EL CANDADO DE «NADIE RECIBE TRES
    # CORREOS»: cada evento aparta su ranura en un solo paso condicionado (igual que el
    # cupón y los puntos), así que dos webhooks simultáneos no mandan lo mismo dos
    # veces. Ranuras: 'nuevo', 'pagado', 'enviado:<número de guía>'.
    emails_sent: List[str] = Field(default_factory=list)
    # La liga de pago de Mercado Pago. Con OXXO ESTA URL ES LA FICHA: si no se guarda,
    # el cliente que cierra la pestaña no puede volver a verla nunca.
    card_checkout_url: str = ''
    # Marketing: de dónde vino este cliente y si era su PRIMERA compra.
    # `first_order` es la pieza que hace honesto el costo por cliente: si un
    # cliente que ya compraba vuelve a comprar, esa venta NO es un cliente que
    # el anuncio haya conseguido, y contarla abarata el costo artificialmente.
    attribution: dict = Field(default_factory=dict)
    first_order: bool = False
    # Constancia de la aceptación 18+/RUO (ver OrderCreate). Los pedidos anteriores
    # a esto no la traen y se leen igual de bien: por eso tiene default.
    terms_accepted_at: str = ''
    # ⛔ ENVÍO PARTIDO (Christián, 2026-07-30): «si piden 40 y solo tengo 20, se mandan
    # los 20 y se mandan pedir los otros 20». NINGUNA venta se bloquea por inventario.
    # `backorder` prende cuando algo de este pedido no salía de la bodega ese día, y
    # `backorder_items` dice exactamente qué: cuántas se pidieron, cuántas van en la
    # primera entrega y cuántas hay que mandar pedir al proveedor. Es lo que el cliente
    # ve ANTES de pagar y lo que el equipo ve en el Panel para salir a comprar.
    backorder: bool = False
    backorder_items: List[dict] = Field(default_factory=list)
    # Qué eligió el cliente al pagar: 'partido' (lo de hoy sale ya) o 'completo'
    # (espera a estar entero). Se respeta en el despacho: con 'completo' la guía
    # automática NO se compra mientras falte mercancía. Ver OrderCreate.
    shipping_preference: str = 'partido'
    # Cuántas piezas se APARTARON de verdad, por producto ({clave: piezas}). No es lo
    # mismo que lo pedido: en un pedido partido se aparta menos. Cancelar tiene que
    # devolver EXACTAMENTE esto y no la cantidad pedida, o cada cancelación le regala
    # al inventario piezas que nunca salieron — así quedó Orexin A en 43 cuando tenía
    # 40 (2026-07-27). Los pedidos viejos no lo traen y se devuelven por cantidad,
    # que es lo que hacían.
    stock_taken: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class OrderStatusUpdate(BaseModel):
    status: str


class ShippingQuoteRequest(BaseModel):
    """Lo que el checkout manda para cotizar: a dónde va y qué lleva.

    NO trae precio de envío ni peso: los dos los calcula el servidor. Un peso que
    manda el navegador es un envío barato que alguien se inventó.
    """
    postal_code: str
    items: List[OrderItem] = []
    state: str = ''
    city: str = ''
    country: str = 'MX'


class CotizadorEnvioRequest(BaseModel):
    """Lo que el COTIZADOR manda: a dónde va y qué se manda, sin pedido de por medio.

    ⛔ NO ES EL CHECKOUT Y NO COBRA NADA. Aquí sí se acepta un peso capturado a mano,
    y sólo por eso: lo que sale de este camino es una respuesta en pantalla, jamás un
    cargo. La cotización se guarda en su PROPIA colección (`shipping_cotizador`) para
    que ningún id de aquí pueda colarse en un pedido — el checkout sigue leyendo
    únicamente las suyas, calculadas por el servidor contra el catálogo real.

    `mode`:
      · 'items'  → se eligen productos y cantidades; el peso Y el importe de mercancía
        los saca el SERVIDOR del catálogo, exactamente como en el checkout.
      · 'manual' → un bulto cualquiera: peso y medidas a mano. El importe de mercancía
        se teclea porque no hay productos de donde sacarlo; es un "qué pasaría si".
    """
    postal_code: str = ''
    state: str = ''
    city: str = ''
    country: str = 'MX'
    mode: str = 'items'
    items: List[OrderItem] = []
    peso_kg: float = 0.0
    largo_cm: float = 0.0
    ancho_cm: float = 0.0
    alto_cm: float = 0.0
    # Sólo se mira en 'manual'. En 'items' el servidor lo calcula y este campo se ignora.
    merchandise_mxn: float = 0.0


class RemitenteUpdate(BaseModel):
    """La dirección de quien despacha, capturada desde Admin → Envíos.

    ⛔ Ningún campo trae valor de ejemplo. Es el domicilio de un trabajador: si el
    código lo rellenara, alguien acabaría comprando una guía con una dirección
    inventada y la paquetería iría a recoger a una casa que no existe.
    """
    name: str = ''
    company: str = ''
    address1: str = ''
    address2: str = ''
    colonia: str = ''
    city: str = ''
    province: str = ''
    zip: str = ''
    country: str = 'MX'
    phone: str = ''
    email: str = ''
    reference: str = ''


class CajaEnvio(BaseModel):
    """Una caja del catálogo de empaque. Las medidas van en cm y los pesos en kg."""
    nombre: str = 'caja'
    largo_cm: float
    ancho_cm: float
    alto_cm: float
    peso_max_kg: float = 999.0     # hasta cuánta mercancía le cabe
    peso_caja_kg: float = 0.0      # lo que pesa vacía, con relleno


class EmpaqueEnvio(BaseModel):
    """Un empaque REAL de la bodega, medido por CUÁNTAS PIEZAS le caben.

    ⛔ ES OTRA COSA QUE `CajaEnvio` y por eso no se reusa. La caja se elige por peso
    calculado —que sirve para cotizar— y ésta se elige por cuántas piezas caben, que es
    lo único que se puede saber de cierto cuando el catálogo no trae pesos reales. Es la
    tabla que decide si el servidor compra la guía solo o le pregunta a Christián.

    Hoy sólo existe un renglón: la bolsa stand-up de 12×15×1 cm con 4 piezas. El día que
    haya cajas se capturan aquí, desde el Panel, sin desplegar nada.
    """
    nombre: str = 'empaque'
    hasta_piezas: int              # cuántas piezas le caben. Sin esto no dice nada.
    largo_cm: float
    ancho_cm: float
    alto_cm: float
    peso_facturable_kg: float = 1.0    # con qué peso se cotiza (mínimo 1 kg)


class EmpaquesUpdate(BaseModel):
    empaques: List[EmpaqueEnvio] = []


class CajasUpdate(BaseModel):
    cajas: List[CajaEnvio] = []


class ComprarGuiaRequest(BaseModel):
    """Qué opción de la cotización quiere comprar el admin. ⚠️ Cuesta dinero real."""
    option_id: str


class OrderShippingUpdate(BaseModel):
    """Datos de envío que captura el admin cuando despacha un pedido."""
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None
    eta: Optional[str] = None
    status: Optional[str] = None


class DistributorShippingUpdate(BaseModel):
    """Lo ÚNICO que un distribuidor puede capturar de un pedido suyo: la guía.

    ⛔ NO HEREDA de OrderShippingUpdate a propósito. Aquí no existen `status` ni
    ningún campo de dinero, así que aunque alguien mande `status` o `total` en el
    cuerpo, el modelo ni siquiera los tiene dónde recibir: se caen solos.
    """
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None


class QuoteLine(BaseModel):
    """Un renglón de la cotización que el distribuidor manda por correo.

    ⛔ SOLO QUÉ Y CUÁNTOS. Ni precio ni descuento por renglón: el precio lo pone
    el SERVIDOR con el catálogo real (misma regla que el checkout). Si el modelo
    aceptara un precio, cualquiera podría mandarle a un cliente una cotización
    firmada por Exygen con el número que se le antojara."""
    product_id: str
    quantity: int = Field(1, ge=1, le=999)


class QuoteEmailRequest(BaseModel):
    """La cotización que sale por correo. `discount` es una PETICIÓN, no una
    orden: el servidor la recorta al tope de cada producto y al máximo de este
    distribuidor antes de escribir un solo peso."""
    email: EmailStr
    client_name: Optional[str] = ''
    # Datos de contacto del cliente, TODOS opcionales (Christián, 2026-07-30):
    # si vienen se pintan en el documento; si no, la cotización sale igual.
    # Texto libre a propósito — un teléfono puede traer extensión y una dirección
    # no tiene formato; el servidor los recorta y escapa antes de pintarlos.
    client_email: Optional[str] = ''
    client_phone: Optional[str] = ''
    client_address: Optional[str] = ''
    discount: float = Field(0, ge=0, le=1)
    language: Optional[str] = None
    folio: Optional[str] = ''
    items: List[QuoteLine] = Field(default_factory=list)


class GiftLine(BaseModel):
    """UN obsequio del distribuidor. `tipo` es 'producto' o 'envio'.

    ⛔ AQUÍ NO HAY PRECIO NI CÓDIGO, y es a propósito. El valor del regalo lo pone
    el SERVIDOR contra el catálogo real —igual que el precio de un renglón— y el
    código interno lo GENERA el servidor: si el navegador pudiera mandarlo, el
    código dejaría de ser secreto en el momento en que alguien abriera la consola.
    """
    tipo: str = 'producto'          # ver regalos.TIPOS — 'producto' | 'envio'
    product_id: Optional[str] = ''
    cantidad: int = Field(1, ge=1, le=5)


class ShareCartRequest(BaseModel):
    """EL CARRITO COMPARTIBLE que el distribuidor manda por WhatsApp.

    Mismo principio que la cotización por correo: del navegador sólo viajan QUÉ
    productos, CUÁNTOS, cuánto descuento se PIDE y qué se quiere obsequiar. Los
    precios, el descuento real de cada renglón, el envío y el valor del regalo los
    calcula el servidor y los vuelve a calcular al cobrar.
    """
    client_name: Optional[str] = ''
    # LOS DATOS DEL CLIENTE que el distribuidor ya capturó en el cotizador
    # (Christián, 2026-08-01): «Cuando el cliente abre el link de la cotización, su
    # nombre, email, teléfono, dirección, NADA se guardó.» Se guardan para
    # PRELLENARLE el checkout y ahorrarle teclearlo todo otra vez.
    #
    # ⛔ NO SALEN por `GET /carrito/{token}`, que es público. Salen sólo por
    # `POST /carrito/{token}/datos`, que exige la segunda llave del enlace.
    # Todos opcionales: si ella no los llenó, el checkout se comporta como siempre.
    client_email: Optional[str] = ''
    client_phone: Optional[str] = ''
    client_address: Optional[str] = ''
    # El domicilio POR CAMPOS (Christián, 2026-08-02): ciudad, estado y CP como
    # en el checkout. Con el CP el cotizador ya cotiza el envío con las reglas
    # de la casa, y el prellenado del cliente llena la dirección completa.
    client_city: Optional[str] = ''
    client_state: Optional[str] = ''
    client_zip: Optional[str] = ''
    discount: float = Field(0, ge=0, le=1)
    language: Optional[str] = None
    folio: Optional[str] = ''
    items: List[QuoteLine] = Field(default_factory=list)
    gifts: List[GiftLine] = Field(default_factory=list)


class PrellenadoRequest(BaseModel):
    """La segunda llave del enlace, para leer los datos de contacto de ESE carrito.

    Va en el CUERPO de un POST y no en la dirección a propósito: así no se escribe
    en los registros del servidor ni en el historial del navegador. Ver
    `regalos.nueva_clave_de_prellenado` para el porqué completo.
    """
    clave: str = Field('', max_length=200)


# ---------- Distributors ----------
class DistributorCreate(BaseModel):
    name: str
    email: EmailStr
    commission_rate: float = 0.30          # 0..1 — proporción de cada venta que gana el distribuidor (default 30%, Christian 2026-07-22)
    customer_discount_rate: float = 0.10   # 0.05..0.50 — descuento que su código da a SUS clientes
    tier: str = 'junior'                   # junior | senior | master — pirámide (§4ter)
    upline_id: Optional[str] = None        # distribuidor que lo trajo (para las sobrecomisiones)


class SolicitudPagoComision(BaseModel):
    """El distribuidor pide su pago. Sin monto = todo su saldo por pagar.

    El monto es una PETICIÓN: el servidor la valida contra el saldo real
    (`comisiones.puede_solicitar`) antes de escribir nada."""
    amount: Optional[float] = Field(None, ge=0)


class RegistroPagoComision(BaseModel):
    """El admin registra que YA pagó una comisión. `reference` es el rastro del
    dinero (folio SPEI, «efectivo», lo que sea) — opcional pero muy recomendable,
    porque el documento que esto crea es el recibo."""
    distributor_id: str
    amount: float = Field(..., gt=0)
    reference: Optional[str] = Field('', max_length=200)


class RechazoPagoComision(BaseModel):
    """El admin niega una solicitud, con motivo. No mueve un peso de saldo."""
    payout_id: str
    motivo: Optional[str] = Field('', max_length=300)


class DiscountCodeCreate(BaseModel):
    # El distribuidor crea VARIOS códigos y elige cuál da a cada cliente. El
    # descuento va de 0 hasta su comisión de nivel (el servidor lo acota).
    label: str = ''
    discount_rate: float = 0.0             # 0..su comisión de nivel


class AnnouncementCreate(BaseModel):
    # Aviso que publica el admin (centro de noticias). Audiencia: todos / clientes
    # / distribuidores. `email` = mandarlo también por correo (para lo importante).
    title: str
    body: str = ''
    audience: str = 'all'                  # all | clients | distributors
    link: Optional[str] = None
    email: bool = False


# ---------- Protocolos (seguimiento de consumo / recompra) ----------
class ProtocolInput(BaseModel):
    """Lo que el cliente registra para que calculemos cuándo se le acaba el vial.

    Todo es información de investigación (RUO): no es una pauta de uso.
    """
    product_name: str
    product_slug: str = ''
    vial_mg: float                       # mg por vial
    vials: int = 1                       # cuántos viales tiene en mano
    dose: float                          # dosis por aplicación
    dose_unit: str = 'mcg'               # mcg | mg
    doses_per_week: float = 7            # frecuencia
    water_ml: float = 0                  # opcional, solo informativo
    # Nivel de referencia con el que se calculó: inicial | tipica | avanzada.
    # La reconstitución cambia con él, así que se guarda para poder repetirla.
    level: str = ''
    started_at: Optional[str] = None     # ISO; default = hoy
    notes: str = ''
    remind: bool = True                  # avisar cuando se acerque el final


class PerfilSalud(BaseModel):
    """Los datos del cliente con los que se personaliza su seguimiento.

    Christian, 2026-07-26: el sitio debe llevar un seguimiento calendarizado con
    el peso, el porcentaje de grasa y lo que su médico le haya indicado.

    ⚠️ EL CANDADO: `consulto_medico` y `tiene_analisis` NO son letra chica, son
    requisito. El sitio no decide dosis — acompaña la que el cliente y su médico
    ya decidieron. Por eso el seguimiento no se puede configurar sin confirmar
    primero que hubo consulta y análisis previos. Si el disclaimer vive escondido
    al pie de página no sirve de nada; aquí es parte del flujo.

    Nada de esto es obligatorio para comprar: solo para usar el seguimiento.
    """
    model_config = ConfigDict(extra='ignore')
    peso_kg: Optional[float] = None
    estatura_cm: Optional[float] = None
    grasa_pct: Optional[float] = None
    sexo: str = ''                       # 'm' | 'f' | '' (para rangos de laboratorio)
    edad: Optional[int] = None
    objetivo: str = ''                   # texto libre del cliente
    # El candado
    consulto_medico: bool = False
    tiene_analisis: bool = False
    # Lo que le indicó su médico, tal cual. Es la fuente que manda sobre
    # cualquier cosa que sugiera el sitio.
    indicacion_medica: str = ''
    medico_nombre: str = ''
    medico_especialidad: str = ''
    actualizado: str = ''


class ProtocolUpdate(BaseModel):
    vial_mg: Optional[float] = None
    vials: Optional[int] = None
    dose: Optional[float] = None
    dose_unit: Optional[str] = None
    doses_per_week: Optional[float] = None
    water_ml: Optional[float] = None
    started_at: Optional[str] = None
    notes: Optional[str] = None
    remind: Optional[bool] = None
    active: Optional[bool] = None


# ---------- Estudios de laboratorio ----------
class LabMarkerInput(BaseModel):
    key: str = ''            # clave del catálogo (lab_reference) si la reconocimos
    label: str               # nombre tal como venía en la hoja
    value: float
    unit: str = ''
    reference: str = ''      # rango impreso por el laboratorio


class LabReportInput(BaseModel):
    """Un estudio. Nunca guardamos el archivo original ni datos de identidad:
    solo los marcadores y la tabla en texto que sale de la extracción."""
    taken_at: str = ''       # AAAA-MM-DD
    lab_name: str = ''
    markdown: str = ''
    markers: List[LabMarkerInput] = []
    sex: str = ''            # male | female | '' — solo para elegir el rango de referencia


# ---------- AI Chat ----------
class ChatInput(BaseModel):
    session_id: str
    message: str
    product_context: Optional[str] = None
    # Idioma elegido por el usuario en el sitio (es-MX, en-US, pt-BR, fr-CA).
    # El asistente responde en ese idioma, no siempre en espanol.
    language: Optional[str] = None


class GoogleAuthInput(BaseModel):
    """Credencial de Google Identity Services (el ID token del boton).

    Los consentimientos solo aplican cuando la cuenta es NUEVA: Google avala
    el correo, pero aceptar 18+/Terminos y Privacidad es decision del usuario
    y nadie la puede marcar por el."""
    credential: str
    language: Optional[str] = None
    distributor_code: Optional[str] = None
    age_confirmed: bool = False
    privacy_accepted: bool = False
    marketing_email: bool = False
    promos: bool = False


class TrackEvent(BaseModel):
    """Evento del embudo de venta. Sin datos personales: solo un id de sesion
    anonimo y de donde vino la visita (para medir publicidad)."""
    type: str                     # visit | product_view | add_to_cart | checkout_start | purchase
    session_id: str               # caduca a los 30 min sin actividad: es UNA visita
    visitor_id: str = ''          # permanente: sirve para saber si alguien vuelve
    path: str = ''
    product: str = ''             # SKU o slug, cuando aplica
    value: float = 0              # monto, en compra
    order_number: str = ''
    utm_source: str = ''          # facebook, instagram, google...
    utm_medium: str = ''
    utm_campaign: str = ''
    # El ANUNCIO concreto dentro de la campaña, y el clic de Meta. `fbclid` es la
    # red de seguridad de toda la medición: Meta se lo pega a los enlaces de sus
    # anuncios aunque nadie los haya etiquetado, así que sin él una publicación
    # impulsada es indistinguible del tráfico directo.
    utm_content: str = ''
    utm_term: str = ''
    fbclid: str = ''
    referrer: str = ''
    landing_path: str = ''
    # ---- Los tres datos que Christián autorizó el 2026-07-31 ----
    #
    # ⛔ POR QUÉ HACÍAN FALTA. Hasta hoy el sitio no guardaba NADA de esto, así que
    # dos preguntas de dinero no se podían contestar con datos propios:
    #   1. «¿la mayoría entra por teléfono?» — sólo se deducía de dónde se compran
    #      los anuncios, o sea que era adivinar;
    #   2. «¿sirvió adelgazar la portada móvil?» — sin el corte por dispositivo, el
    #      8.7% de visita→ficha es un promedio que esconde justo lo que se cambió.
    #
    # 🔒 PRIVACIDAD (misma orden de Christián). Sólo lo AGREGADO: la categoría del
    # aparato y el ancho. NO se guarda IP, NO se guarda el User-Agent, NO se calcula
    # huella digital del visitante. `device` viaja YA CLASIFICADO desde el navegador
    # precisamente para no tener que guardar aquí el texto del User-Agent —que sí es
    # huella— y clasificarlo después.
    device: str = ''       # telefono | tableta | computadora | '' (eventos de antes de hoy)
    screen_w: int = 0      # ancho del navegador en px: 375 en un teléfono, 1400 en un monitor
    # El `?ref=` del distribuidor. Ya existía en el carrito (`np_dist_code`) pero
    # nunca llegaba a la medición: una visita traída por María era indistinguible de
    # una visita directa hasta que alguien COMPRABA. Es un CÓDIGO, no una persona:
    # no revela de quién es (regla de los códigos sin nombre, 2026-07-31).
    ref_code: str = ''
