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
    notes: str = ''


class OrderCreate(BaseModel):
    items: List[OrderItem]
    customer: CustomerInfo
    payment_method: str   # tarjeta | spei
    shipping: float = 0
    discount: float = 0                      # informativo; el servidor recalcula con su propia regla
    distributor_code: Optional[str] = None   # referido por un distribuidor (atribuye la venta)


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
    created_at: str = Field(default_factory=now_iso)


class OrderStatusUpdate(BaseModel):
    status: str


# ---------- Distributors ----------
class DistributorCreate(BaseModel):
    name: str
    email: EmailStr
    commission_rate: float = 0.25   # 0..1 — proporción de cada venta que gana el distribuidor


# ---------- AI Chat ----------
class ChatInput(BaseModel):
    session_id: str
    message: str
    product_context: Optional[str] = None
