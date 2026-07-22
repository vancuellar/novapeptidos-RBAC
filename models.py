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


class ProductCreate(ProductBase):
    pass


class Product(ProductBase):
    model_config = ConfigDict(extra='ignore')
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = Field(default_factory=now_iso)


class ProductUpdate(BaseModel):
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
    city: str = ''
    state: str = ''
    postal_code: str = ''
    country: str = 'MX'  # ISO-3166 alfa-2; México por default
    notes: str = ''


class OrderCreate(BaseModel):
    items: List[OrderItem]
    customer: CustomerInfo
    payment_method: str   # tarjeta | spei
    shipping: float = 0
    discount: float = 0                      # informativo; el servidor recalcula con su propia regla
    distributor_code: Optional[str] = None   # referido por un distribuidor (atribuye la venta)
    points_to_use: int = 0                   # puntos de lealtad a canjear; el servidor valida saldo


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
    discount_rate: float = 0
    shipping: float
    total: float
    status: str = 'pendiente'   # pendiente | confirmado | enviado | entregado | cancelado
    referred_by: Optional[str] = None   # id del distribuidor que refirió (si aplica)
    commission: float = 0               # ganancia del distribuidor en esta orden (MXN)
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
    created_at: str = Field(default_factory=now_iso)


class OrderStatusUpdate(BaseModel):
    status: str


class OrderShippingUpdate(BaseModel):
    """Datos de envío que captura el admin cuando despacha un pedido."""
    carrier: Optional[str] = None
    tracking_number: Optional[str] = None
    tracking_url: Optional[str] = None
    eta: Optional[str] = None
    status: Optional[str] = None


# ---------- Distributors ----------
class DistributorCreate(BaseModel):
    name: str
    email: EmailStr
    commission_rate: float = 0.30          # 0..1 — proporción de cada venta que gana el distribuidor (default 30%, Christian 2026-07-22)
    customer_discount_rate: float = 0.10   # 0.05..0.50 — descuento que su código da a SUS clientes


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
