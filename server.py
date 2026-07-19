from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import uuid
import random
import re
from datetime import datetime, timezone
from typing import List, Optional

from database import db, client
from models import (
    RegisterInput, LoginInput, ForgotPasswordInput, ResetPasswordInput,
    ProfileUpdate, ChangePasswordInput,
    ProductCreate, ProductUpdate, Product, Category,
    OrderCreate, Order, OrderStatusUpdate, OrderShippingUpdate,
    ProtocolInput, ProtocolUpdate,
    ChatInput, DistributorCreate, now_iso,
)
from auth import (
    hash_password, verify_password, create_token,
    get_current_user, get_optional_user, get_current_admin, get_current_distributor,
)
from ai_assistant import build_chat, stream_reply
from emails import send_welcome_email, send_reset_email, normalize_language
from datetime import timedelta
import asyncio
from seed_data import CATEGORIES, PRODUCTS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title='Exygen Labs API')
api_router = APIRouter(prefix='/api')


# ----------------- Helpers -----------------
def clean(doc):
    if doc and '_id' in doc:
        doc.pop('_id', None)
    return doc


def gen_order_number():
    return 'EX-' + datetime.now().strftime('%Y%m%d') + '-' + str(random.randint(1000, 9999))


def gen_distributor_code(name: str) -> str:
    base = ''.join(c for c in name.upper() if c.isalnum())[:4] or 'DIST'
    return base + '-' + str(random.randint(1000, 9999))


async def resolve_distributor(code):
    """Devuelve el distribuidor (dict) para un codigo dado, o None."""
    if not code:
        return None
    return await db.users.find_one({'distributor_code': code, 'role': 'distributor'}, {'_id': 0, 'password_hash': 0})


# ----------------- Health -----------------
@api_router.get('/')
async def root():
    return {'message': 'Exygen Labs API', 'status': 'ok'}


# ----------------- Auth -----------------
@api_router.post('/auth/register')
async def register(payload: RegisterInput):
    existing = await db.users.find_one({'email': payload.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    referrer = await resolve_distributor(payload.distributor_code)
    user = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        'password_hash': hash_password(payload.password),
        'role': 'user',
        'language': normalize_language(payload.language),
        'referred_by': referrer['id'] if referrer else None,
        'created_at': now_iso(),
    }
    await db.users.insert_one(user)
    asyncio.create_task(send_welcome_email(user['name'], user['email'], user['language']))
    token = create_token(user['id'])
    return {
        'token': token,
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
    }


@api_router.post('/auth/login')
async def login(payload: LoginInput):
    user = await db.users.find_one({'email': payload.email.lower()})
    if not user or not verify_password(payload.password, user.get('password_hash', '')):
        raise HTTPException(status_code=401, detail='Correo o contrasena incorrectos')
    token = create_token(user['id'])
    return {
        'token': token,
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
    }


SITE_URL = os.environ.get('SITE_URL', 'https://exygenlabs.com')


@api_router.post('/auth/forgot-password')
async def forgot_password(payload: ForgotPasswordInput):
    """Siempre responde ok (no revela si el correo existe)."""
    user = await db.users.find_one({'email': payload.email.lower()}, {'_id': 0, 'password_hash': 0})
    if user:
        token = uuid.uuid4().hex
        await db.password_resets.insert_one({
            'token': token,
            'user_id': user['id'],
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'used': False,
            'created_at': now_iso(),
        })
        link = f'{SITE_URL}/restablecer?token={token}'
        asyncio.create_task(send_reset_email(user['name'], user['email'], link,
                                             payload.language or user.get('language')))
    return {'ok': True}


@api_router.post('/auth/reset-password')
async def reset_password(payload: ResetPasswordInput):
    rec = await db.password_resets.find_one({'token': payload.token, 'used': False}, {'_id': 0})
    if not rec or rec.get('expires_at', '') < datetime.now(timezone.utc).isoformat():
        raise HTTPException(status_code=400, detail='El enlace no es valido o ya expiro. Solicita uno nuevo.')
    await db.users.update_one({'id': rec['user_id']}, {'$set': {'password_hash': hash_password(payload.password)}})
    await db.password_resets.update_one({'token': payload.token}, {'$set': {'used': True}})
    return {'ok': True}


@api_router.put('/auth/profile')
async def update_profile(payload: ProfileUpdate, user=Depends(get_current_user)):
    """Perfil del usuario. NUNCA guardamos numeros de tarjeta — solo la preferencia
    de metodo de pago; los datos de tarjeta viven con el procesador de pagos."""
    update = {}
    if payload.name is not None and payload.name.strip():
        update['name'] = payload.name.strip()
    if payload.phone is not None:
        update['phone'] = payload.phone.strip()
    if payload.preferred_payment is not None:
        if payload.preferred_payment not in ('', 'tarjeta', 'spei'):
            raise HTTPException(status_code=400, detail='Metodo de pago no valido')
        update['preferred_payment'] = payload.preferred_payment
    if payload.shipping_address is not None:
        update['shipping_address'] = payload.shipping_address.model_dump()
    if payload.billing_address is not None:
        update['billing_address'] = payload.billing_address.model_dump()
    if payload.email and payload.email.lower() != user['email']:
        full = await db.users.find_one({'id': user['id']})
        if not payload.current_password or not verify_password(payload.current_password, full['password_hash']):
            raise HTTPException(status_code=400, detail='Para cambiar el correo, confirma tu contrasena actual')
        if await db.users.find_one({'email': payload.email.lower()}):
            raise HTTPException(status_code=400, detail='Ese correo ya esta registrado')
        update['email'] = payload.email.lower()
    if update:
        await db.users.update_one({'id': user['id']}, {'$set': update})
    return await db.users.find_one({'id': user['id']}, {'_id': 0, 'password_hash': 0})


@api_router.post('/auth/change-password')
async def change_password(payload: ChangePasswordInput, user=Depends(get_current_user)):
    full = await db.users.find_one({'id': user['id']})
    if not verify_password(payload.current_password, full['password_hash']):
        raise HTTPException(status_code=400, detail='La contrasena actual no es correcta')
    await db.users.update_one({'id': user['id']}, {'$set': {'password_hash': hash_password(payload.new_password)}})
    return {'ok': True}


@api_router.get('/auth/me')
async def me(user=Depends(get_current_user)):
    return user


# ----------------- Categories -----------------
@api_router.get('/categories')
async def list_categories():
    cats = await db.categories.find({}, {'_id': 0}).to_list(100)
    return cats


# ----------------- Products -----------------
@api_router.get('/products')
async def list_products(
    category: Optional[str] = None,
    search: Optional[str] = None,
    featured: Optional[bool] = None,
    in_stock: Optional[bool] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: Optional[str] = 'relevance',
):
    query = {}
    if category:
        query['category'] = category
    if featured is not None:
        query['featured'] = featured
    if search:
        query['$or'] = [
            {'name': {'$regex': search, '$options': 'i'}},
            {'short_description': {'$regex': search, '$options': 'i'}},
            {'description': {'$regex': search, '$options': 'i'}},
        ]
    if in_stock:
        query['stock'] = {'$gt': 0}
    price_q = {}
    if min_price is not None:
        price_q['$gte'] = min_price
    if max_price is not None:
        price_q['$lte'] = max_price
    if price_q:
        query['price'] = price_q

    cursor = db.products.find(query, {'_id': 0})
    products = await cursor.to_list(500)

    if sort == 'price_asc':
        products.sort(key=lambda p: p.get('price', 0))
    elif sort == 'price_desc':
        products.sort(key=lambda p: p.get('price', 0), reverse=True)
    elif sort == 'newest':
        products.sort(key=lambda p: p.get('created_at', ''), reverse=True)
    return products


@api_router.get('/products/{slug}')
async def get_product(slug: str):
    product = await db.products.find_one({'slug': slug}, {'_id': 0})
    if not product:
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    return product


# ----------------- Admin: Products -----------------
@api_router.post('/admin/products')
async def create_product(payload: ProductCreate, admin=Depends(get_current_admin)):
    existing = await db.products.find_one({'slug': payload.slug})
    if existing:
        raise HTTPException(status_code=400, detail='Ya existe un producto con ese slug')
    product = Product(**payload.model_dump())
    await db.products.insert_one(product.model_dump())
    return clean(product.model_dump())


@api_router.put('/admin/products/{product_id}')
async def update_product(product_id: str, payload: ProductUpdate, admin=Depends(get_current_admin)):
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail='Sin cambios')
    result = await db.products.update_one({'id': product_id}, {'$set': update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    product = await db.products.find_one({'id': product_id}, {'_id': 0})
    return product


@api_router.delete('/admin/products/{product_id}')
async def delete_product(product_id: str, admin=Depends(get_current_admin)):
    result = await db.products.delete_one({'id': product_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    return {'ok': True}


# ----------------- Orders -----------------
@api_router.post('/orders')
async def create_order(payload: OrderCreate, user=Depends(get_optional_user)):
    if not payload.items:
        raise HTTPException(status_code=400, detail='El carrito esta vacio')
    if payload.payment_method not in ('tarjeta', 'spei'):
        raise HTTPException(status_code=400, detail='Metodo de pago no disponible')
    subtotal = sum(item.price * item.quantity for item in payload.items)
    # Atribucion a distribuidor: por codigo explicito o por el que refirio al usuario.
    referrer = await resolve_distributor(payload.distributor_code)
    if not referrer and user and user.get('referred_by'):
        referrer = await db.users.find_one({'id': user['referred_by'], 'role': 'distributor'}, {'_id': 0, 'password_hash': 0})
    # Descuento: automatico por volumen (10/15/20%) O el del codigo del distribuidor —
    # NUNCA se acumulan; aplica el MAYOR de los dos. Manda el servidor.
    auto_rate = 0.20 if subtotal >= 40000 else 0.15 if subtotal >= 20000 else 0.10
    code_rate = referrer.get('customer_discount_rate', 0) if referrer else 0
    discount_rate = max(auto_rate, code_rate)
    discount = round(subtotal * discount_rate)
    after_discount = subtotal - discount
    shipping = payload.shipping if payload.shipping else 0   # el envio se cotiza por separado
    total = after_discount + shipping
    commission = round(after_discount * referrer.get('commission_rate', 0.25)) if referrer else 0
    order = Order(
        order_number=gen_order_number(),
        user_id=user['id'] if user else None,
        items=payload.items,
        customer=payload.customer,
        payment_method=payload.payment_method,
        subtotal=subtotal,
        discount=discount,
        discount_rate=discount_rate,
        shipping=shipping,
        total=total,
        referred_by=referrer['id'] if referrer else None,
        commission=commission,
    )
    await db.orders.insert_one(order.model_dump())
    for item in payload.items:
        await db.products.update_one({'id': item.product_id}, {'$inc': {'stock': -item.quantity}})
        # Inventario vivo por presentacion (key = product_id del carrito, ya incluye ::presentacion)
        await db.stock.update_one({'key': item.product_id}, {'$inc': {'qty': -item.quantity}})
    return clean(order.model_dump())


# ----------------- Stock (inventario vivo por presentacion) -----------------
@api_router.get('/stock')
async def get_stock():
    """Publico: {key: {qty, in_hand}} para todas las presentaciones."""
    rows = await db.stock.find({}, {'_id': 0}).to_list(2000)
    return {r['key']: {'qty': r.get('qty', 0), 'in_hand': bool(r.get('in_hand'))} for r in rows}


@api_router.put('/admin/stock')
async def set_stock(payload: dict, admin=Depends(get_current_admin)):
    key = payload.get('key')
    if not key:
        raise HTTPException(status_code=400, detail='Falta key')
    update = {}
    if 'qty' in payload:
        update['qty'] = int(payload['qty'])
    if 'in_hand' in payload:
        update['in_hand'] = bool(payload['in_hand'])
    await db.stock.update_one({'key': key}, {'$set': update}, upsert=True)
    row = await db.stock.find_one({'key': key}, {'_id': 0})
    return row


@api_router.get('/orders/me')
async def my_orders(user=Depends(get_current_user)):
    orders = await db.orders.find({'user_id': user['id']}, {'_id': 0}).to_list(200)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return orders


@api_router.get('/orders/{order_number}')
async def get_order(order_number: str):
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    return order


# ----------------- Admin: Orders -----------------
@api_router.get('/admin/orders')
async def admin_orders(admin=Depends(get_current_admin)):
    orders = await db.orders.find({}, {'_id': 0}).to_list(500)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return orders


@api_router.put('/admin/orders/{order_id}/status')
async def update_order_status(order_id: str, payload: OrderStatusUpdate, admin=Depends(get_current_admin)):
    update = {'status': payload.status}
    if payload.status == 'enviado':
        update['shipped_at'] = now_iso()
    elif payload.status == 'entregado':
        update['delivered_at'] = now_iso()
    result = await db.orders.update_one({'id': order_id}, {'$set': update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    return order


CARRIER_TRACKING_URLS = {
    'fedex': 'https://www.fedex.com/fedextrack/?trknbr={n}',
    'dhl': 'https://www.dhl.com/mx-es/home/rastreo.html?tracking-id={n}',
    'estafeta': 'https://www.estafeta.com/Herramientas/Rastreo?wayBill={n}',
    'ups': 'https://www.ups.com/track?tracknum={n}',
    'paquetexpress': 'https://www.paquetexpress.com.mx/rastreo?guia={n}',
}


def build_tracking_url(carrier: str, number: str) -> str:
    """URL de rastreo del transportista. Vacío si no lo conocemos."""
    key = (carrier or '').strip().lower().replace(' ', '')
    tpl = CARRIER_TRACKING_URLS.get(key)
    return tpl.format(n=number.strip()) if tpl and number else ''


@api_router.put('/admin/orders/{order_id}/shipping')
async def update_order_shipping(order_id: str, payload: OrderShippingUpdate, admin=Depends(get_current_admin)):
    """Captura guía y transportista. Si no dan URL, la armamos con la del transportista."""
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    update = {}
    for field in ('carrier', 'tracking_number', 'tracking_url', 'eta'):
        value = getattr(payload, field)
        if value is not None:
            update[field] = value.strip()
    carrier = update.get('carrier', order.get('carrier', ''))
    number = update.get('tracking_number', order.get('tracking_number', ''))
    if not update.get('tracking_url') and number:
        auto = build_tracking_url(carrier, number)
        if auto:
            update['tracking_url'] = auto
    if payload.status:
        update['status'] = payload.status
        if payload.status == 'enviado' and not order.get('shipped_at'):
            update['shipped_at'] = now_iso()
        elif payload.status == 'entregado' and not order.get('delivered_at'):
            update['delivered_at'] = now_iso()
    # Capturar una guía implica que ya salió: si seguía pendiente, pasa a enviado.
    if number and not payload.status and order.get('status') in ('pendiente', 'confirmado'):
        update['status'] = 'enviado'
        update.setdefault('shipped_at', now_iso())
    if update:
        await db.orders.update_one({'id': order_id}, {'$set': update})
    return await db.orders.find_one({'id': order_id}, {'_id': 0})


@api_router.get('/admin/stats')
async def admin_stats(admin=Depends(get_current_admin)):
    total_products = await db.products.count_documents({})
    total_orders = await db.orders.count_documents({})
    total_users = await db.users.count_documents({'role': 'user'})
    orders = await db.orders.find({}, {'_id': 0, 'total': 1, 'status': 1}).to_list(1000)
    revenue = sum(o.get('total', 0) for o in orders if o.get('status') != 'cancelado')
    pending = sum(1 for o in orders if o.get('status') == 'pendiente')
    return {
        'total_products': total_products,
        'total_orders': total_orders,
        'total_users': total_users,
        'revenue': revenue,
        'pending_orders': pending,
    }


# ----------------- Admin: Customers -----------------
@api_router.get('/admin/customers')
async def admin_customers(admin=Depends(get_current_admin)):
    """Todos los clientes con su historial de compra. Nunca expone password_hash."""
    users = await db.users.find({'role': {'$ne': 'admin'}}, {'_id': 0, 'password_hash': 0}).to_list(2000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(5000)
    by_user = {}
    for o in orders:
        if o.get('user_id'):
            by_user.setdefault(o['user_id'], []).append(o)
    out = []
    for u in users:
        uo = sorted(by_user.get(u['id'], []), key=lambda o: o.get('created_at', ''), reverse=True)
        valid = [o for o in uo if o.get('status') != 'cancelado']
        addresses, phones = [], []
        for o in uo:
            c = o.get('customer') or {}
            addr = ', '.join(x for x in [c.get('address'), c.get('city'), c.get('state'), c.get('postal_code')] if x)
            if addr and addr not in addresses:
                addresses.append(addr)
            if c.get('phone') and c['phone'] not in phones:
                phones.append(c['phone'])
        out.append({
            **u,
            'orders_count': len(uo),
            'total_spent': sum(o.get('total', 0) for o in valid),
            'last_order_at': uo[0].get('created_at') if uo else None,
            'addresses': addresses,
            'phones': phones,
            'orders': uo,
        })
    out.sort(key=lambda u: (-u['total_spent'], u.get('created_at', '')))
    return out


# ----------------- Admin: Analytics -----------------
@api_router.get('/admin/analytics')
async def admin_analytics(admin=Depends(get_current_admin)):
    """Ventas agregadas: por mes, por producto, por metodo de pago y por estado."""
    orders = await db.orders.find({}, {'_id': 0}).to_list(10000)
    valid = [o for o in orders if o.get('status') != 'cancelado']
    by_month, by_pay, by_status, prod = {}, {}, {}, {}
    for o in orders:
        s = o.get('status', 'pendiente')
        by_status[s] = by_status.get(s, 0) + 1
    for o in valid:
        month = (o.get('created_at') or '')[:7]
        e = by_month.setdefault(month, {'month': month, 'revenue': 0, 'orders': 0})
        e['revenue'] += o.get('total', 0)
        e['orders'] += 1
        pm = o.get('payment_method', 'otro')
        by_pay[pm] = by_pay.get(pm, 0) + o.get('total', 0)
        for it in o.get('items', []):
            p = prod.setdefault(it.get('name', '?'), {'name': it.get('name', '?'), 'units': 0, 'revenue': 0})
            p['units'] += it.get('quantity', 1)
            p['revenue'] += it.get('price', 0) * it.get('quantity', 1)
    revenue_total = sum(o.get('total', 0) for o in valid)
    return {
        'monthly': sorted(by_month.values(), key=lambda e: e['month']),
        'top_products': sorted(prod.values(), key=lambda p: -p['revenue'])[:10],
        'by_payment': [{'method': k, 'revenue': v} for k, v in sorted(by_pay.items(), key=lambda x: -x[1])],
        'by_status': by_status,
        'avg_ticket': round(revenue_total / len(valid)) if valid else 0,
        'revenue_total': revenue_total,
    }


# ----------------- Public: validar codigo de distribuidor -----------------
@api_router.get('/discount-code/{code}')
async def check_discount_code(code: str):
    """Publico: valida un codigo y devuelve SOLO el % de descuento (nada personal)."""
    dist = await resolve_distributor(code.strip().upper())
    if not dist:
        raise HTTPException(status_code=404, detail='Codigo no valido')
    return {'code': dist['distributor_code'], 'discount_rate': dist.get('customer_discount_rate', 0)}


# ----------------- Admin: Invite customers -----------------
@api_router.post('/admin/customers/invite')
async def invite_customer(payload: DistributorCreate, admin=Depends(get_current_admin)):
    """Invita a un cliente: crea la cuenta con contrasena temporal y manda bienvenida."""
    existing = await db.users.find_one({'email': payload.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    temp_password = uuid.uuid4().hex[:12]
    user = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        'password_hash': hash_password(temp_password),
        'role': 'user',
        'language': 'es',
        'created_at': now_iso(),
    }
    await db.users.insert_one(user)
    asyncio.create_task(send_welcome_email(user['name'], user['email'], 'es'))
    return {'id': user['id'], 'name': user['name'], 'email': user['email'], 'temp_password': temp_password}


# ----------------- Admin: Distributors -----------------
def _distributor_rollup(dist, users, orders):
    """Arma el resumen de un distribuidor: sus clientes y sus ventas atribuidas."""
    clients = [u for u in users if u.get('referred_by') == dist['id']]
    client_ids = {u['id'] for u in clients}
    sales = [o for o in orders if o.get('referred_by') == dist['id'] or o.get('user_id') in client_ids]
    valid = [o for o in sales if o.get('status') != 'cancelado']
    return {
        'id': dist['id'],
        'name': dist['name'],
        'email': dist['email'],
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': dist.get('commission_rate', 0.25),
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        'created_at': dist.get('created_at'),
        'clients_count': len(clients),
        'sales_count': len(valid),
        'sales_total': sum(o.get('total', 0) for o in valid),
        'earnings': sum(o.get('commission', 0) for o in valid),
    }


@api_router.get('/admin/distributors')
async def admin_distributors(admin=Depends(get_current_admin)):
    dists = await db.users.find({'role': 'distributor'}, {'_id': 0, 'password_hash': 0}).to_list(1000)
    users = await db.users.find({}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(10000)
    out = [_distributor_rollup(d, users, orders) for d in dists]
    out.sort(key=lambda d: -d['earnings'])
    return out


@api_router.post('/admin/distributors')
async def create_distributor(payload: DistributorCreate, admin=Depends(get_current_admin)):
    existing = await db.users.find_one({'email': payload.email.lower()})
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    code = gen_distributor_code(payload.name)
    while await db.users.find_one({'distributor_code': code}):
        code = gen_distributor_code(payload.name)
    temp_password = uuid.uuid4().hex[:12]
    dist = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        'password_hash': hash_password(temp_password),
        'role': 'distributor',
        'distributor_code': code,
        'commission_rate': max(0.0, min(1.0, payload.commission_rate)),
        'customer_discount_rate': max(0.05, min(0.50, payload.customer_discount_rate)),
        'language': 'es',
        'created_at': now_iso(),
    }
    await db.users.insert_one(dist)
    # temp_password se entrega al admin para compartir; el distribuidor la cambia al entrar.
    return {'id': dist['id'], 'name': dist['name'], 'email': dist['email'],
            'distributor_code': code, 'commission_rate': dist['commission_rate'],
            'customer_discount_rate': dist['customer_discount_rate'],
            'temp_password': temp_password}


# ----------------- Distributor portal -----------------
@api_router.get('/distributor/summary')
async def distributor_summary(dist=Depends(get_current_distributor)):
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    client_ids = {u['id'] for u in users}
    orders = await db.orders.find(
        {'$or': [{'referred_by': dist['id']}, {'user_id': {'$in': list(client_ids)}}]}, {'_id': 0}
    ).to_list(10000)
    valid = [o for o in orders if o.get('status') != 'cancelado']
    by_month = {}
    for o in valid:
        m = (o.get('created_at') or '')[:7]
        e = by_month.setdefault(m, {'month': m, 'earnings': 0, 'sales': 0})
        e['earnings'] += o.get('commission', 0)
        e['sales'] += o.get('total', 0)
    return {
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': dist.get('commission_rate', 0.25),
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        'clients_count': len(users),
        'sales_count': len(valid),
        'sales_total': sum(o.get('total', 0) for o in valid),
        'earnings_total': sum(o.get('commission', 0) for o in valid),
        'monthly': sorted(by_month.values(), key=lambda e: e['month']),
    }


@api_router.get('/distributor/clients')
async def distributor_clients(dist=Depends(get_current_distributor)):
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(10000)
    by_user = {}
    for o in orders:
        if o.get('user_id'):
            by_user.setdefault(o['user_id'], []).append(o)
    out = []
    for u in users:
        uo = [o for o in by_user.get(u['id'], []) if o.get('status') != 'cancelado']
        out.append({
            'id': u['id'], 'name': u['name'], 'email': u['email'], 'created_at': u.get('created_at'),
            'orders_count': len(uo),
            'total_spent': sum(o.get('total', 0) for o in uo),
            'my_earnings': sum(o.get('commission', 0) for o in uo),
        })
    out.sort(key=lambda u: -u['total_spent'])
    return out


async def _distributor_orders(dist):
    """Órdenes atribuidas al distribuidor: por código o por cliente referido."""
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0}).to_list(5000)
    client_ids = {u['id'] for u in users}
    orders = await db.orders.find(
        {'$or': [{'referred_by': dist['id']}, {'user_id': {'$in': list(client_ids)}}]}, {'_id': 0}
    ).to_list(10000)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return orders


@api_router.get('/distributor/sales')
async def distributor_sales(dist=Depends(get_current_distributor)):
    orders = await _distributor_orders(dist)
    # Solo lo que el distribuidor necesita: no exponemos datos internos de margen del negocio.
    return [{
        'order_number': o.get('order_number'),
        'created_at': o.get('created_at'),
        'status': o.get('status'),
        'customer_name': (o.get('customer') or {}).get('full_name'),
        'total': o.get('total', 0),
        'commission': o.get('commission', 0),
        'items': [{'name': it.get('name'), 'quantity': it.get('quantity')} for it in o.get('items', [])],
    } for o in orders]


@api_router.get('/distributor/orders')
async def distributor_orders(dist=Depends(get_current_distributor)):
    """Pedidos de SUS clientes con estatus y seguimiento de envío.

    Incluye datos de contacto y entrega del cliente (el distribuidor los atiende),
    pero nunca el margen interno del negocio.
    """
    orders = await _distributor_orders(dist)
    out = []
    for o in orders:
        c = o.get('customer') or {}
        out.append({
            'order_number': o.get('order_number'),
            'created_at': o.get('created_at'),
            'status': o.get('status', 'pendiente'),
            'customer_name': c.get('full_name'),
            'customer_email': c.get('email'),
            'customer_phone': c.get('phone'),
            'destination': ', '.join(x for x in [c.get('city'), c.get('state')] if x),
            'payment_method': o.get('payment_method'),
            'items': [{'name': it.get('name'), 'quantity': it.get('quantity'),
                       'presentation': it.get('presentation', '')} for it in o.get('items', [])],
            'total': o.get('total', 0),
            'discount_rate': o.get('discount_rate', 0),
            'commission': o.get('commission', 0),
            'carrier': o.get('carrier', ''),
            'tracking_number': o.get('tracking_number', ''),
            'tracking_url': o.get('tracking_url', ''),
            'shipped_at': o.get('shipped_at'),
            'delivered_at': o.get('delivered_at'),
            'eta': o.get('eta', ''),
        })
    return out


# ----------------- Protocolos: consumo y recompra -----------------
REPURCHASE_WARN_DAYS = 14   # a partir de aquí sugerimos recomprar


def _protocol_projection(p: dict) -> dict:
    """Calcula cuánto material queda y para cuándo alcanza.

    Todo en mcg internamente. Si faltan datos o la frecuencia es 0, devolvemos
    los campos calculados en None en vez de inventar una fecha.
    """
    dose_mcg = float(p.get('dose', 0)) * (1000 if p.get('dose_unit') == 'mg' else 1)
    total_mcg = float(p.get('vial_mg', 0)) * 1000 * max(1, int(p.get('vials', 1)))
    per_week = float(p.get('doses_per_week', 0))
    out = {**p, 'total_doses': None, 'doses_used': None, 'doses_left': None,
           'days_left': None, 'runs_out_at': None, 'pct_left': None, 'needs_repurchase': False}
    if dose_mcg <= 0 or total_mcg <= 0 or per_week <= 0:
        return out
    total_doses = int(total_mcg // dose_mcg)
    try:
        started = datetime.fromisoformat((p.get('started_at') or '').replace('Z', '+00:00'))
    except ValueError:
        started = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    days_elapsed = max(0, (datetime.now(timezone.utc) - started).days)
    per_day = per_week / 7
    doses_used = min(total_doses, int(days_elapsed * per_day))
    doses_left = max(0, total_doses - doses_used)
    days_left = int(doses_left / per_day) if per_day else None
    runs_out = (datetime.now(timezone.utc) + timedelta(days=days_left)).isoformat() if days_left is not None else None
    out.update({
        'total_doses': total_doses,
        'doses_used': doses_used,
        'doses_left': doses_left,
        'days_left': days_left,
        'runs_out_at': runs_out,
        'pct_left': round(100 * doses_left / total_doses) if total_doses else 0,
        'needs_repurchase': bool(p.get('active', True)) and days_left is not None and days_left <= REPURCHASE_WARN_DAYS,
    })
    return out


@api_router.get('/me/protocols')
async def list_protocols(user=Depends(get_current_user)):
    rows = await db.protocols.find({'user_id': user['id']}, {'_id': 0}).to_list(200)
    rows.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    return [_protocol_projection(r) for r in rows]


@api_router.post('/me/protocols')
async def create_protocol(payload: ProtocolInput, user=Depends(get_current_user)):
    doc = {
        'id': str(uuid.uuid4()),
        'user_id': user['id'],
        **payload.model_dump(),
        'active': True,
        'created_at': now_iso(),
    }
    doc['started_at'] = doc.get('started_at') or now_iso()
    await db.protocols.insert_one(doc)
    return _protocol_projection({k: v for k, v in doc.items() if k != '_id'})


@api_router.put('/me/protocols/{protocol_id}')
async def edit_protocol(protocol_id: str, payload: ProtocolUpdate, user=Depends(get_current_user)):
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail='Sin cambios')
    result = await db.protocols.update_one({'id': protocol_id, 'user_id': user['id']}, {'$set': update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail='Seguimiento no encontrado')
    row = await db.protocols.find_one({'id': protocol_id}, {'_id': 0})
    return _protocol_projection(row)


@api_router.delete('/me/protocols/{protocol_id}')
async def delete_protocol(protocol_id: str, user=Depends(get_current_user)):
    result = await db.protocols.delete_one({'id': protocol_id, 'user_id': user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail='Seguimiento no encontrado')
    return {'ok': True}


@api_router.get('/admin/repurchase')
async def admin_repurchase(admin=Depends(get_current_admin)):
    """Clientes cuyo material está por acabarse — oportunidad de recompra."""
    rows = await db.protocols.find({'active': True}, {'_id': 0}).to_list(5000)
    users = await db.users.find({}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    by_id = {u['id']: u for u in users}
    out = []
    for r in rows:
        proj = _protocol_projection(r)
        if proj['days_left'] is None:
            continue
        u = by_id.get(r.get('user_id')) or {}
        out.append({
            'user_id': r.get('user_id'),
            'customer_name': u.get('name', '?'),
            'customer_email': u.get('email', ''),
            'product_name': r.get('product_name'),
            'product_slug': r.get('product_slug', ''),
            'days_left': proj['days_left'],
            'doses_left': proj['doses_left'],
            'runs_out_at': proj['runs_out_at'],
            'needs_repurchase': proj['needs_repurchase'],
        })
    out.sort(key=lambda r: r['days_left'])
    return out


# ----------------- AI Chat (streaming) -----------------
STATUS_LABEL = {
    'pendiente': 'pendiente de confirmar pago',
    'confirmado': 'confirmado, en preparacion',
    'enviado': 'enviado',
    'entregado': 'entregado',
    'cancelado': 'cancelado',
}

ORDER_NUMBER_RE = re.compile(r'\bEX[-\s]?(\d{8})[-\s]?(\d{4})\b', re.IGNORECASE)
SHIPPING_INTENT_RE = re.compile(
    r'\b(pedido|orden|envio|env[ií]o|guia|gu[ií]a|rastre|paqueter|entrega|lleg|track|estatus|status)\w*',
    re.IGNORECASE,
)


def _order_summary_line(o: dict) -> str:
    """Resumen de una orden para el prompt. Sin direccion ni datos personales."""
    parts = [
        f"Pedido {o.get('order_number')}",
        f"estado: {STATUS_LABEL.get(o.get('status', ''), o.get('status', 'desconocido'))}",
        f"fecha: {(o.get('created_at') or '')[:10]}",
        f"total: ${round(o.get('total', 0)):,} MXN",
    ]
    items = ', '.join(f"{it.get('quantity', 1)}x {it.get('name', '?')}" for it in o.get('items', [])[:6])
    if items:
        parts.append(f'articulos: {items}')
    if o.get('carrier') or o.get('tracking_number'):
        parts.append(f"paqueteria: {o.get('carrier') or 'por confirmar'}")
        if o.get('tracking_number'):
            parts.append(f"guia: {o['tracking_number']}")
        if o.get('tracking_url'):
            parts.append(f"rastreo: {o['tracking_url']}")
    if o.get('shipped_at'):
        parts.append(f"enviado el: {o['shipped_at'][:10]}")
    if o.get('delivered_at'):
        parts.append(f"entregado el: {o['delivered_at'][:10]}")
    if o.get('eta'):
        parts.append(f"tiempo estimado: {o['eta']}")
    return ' | '.join(parts)


async def build_order_context(message: str, user) -> str:
    """Si el usuario pregunta por su pedido, adjunta los datos reales al prompt.

    Dos vias: (a) numero de pedido escrito en el mensaje; (b) sesion autenticada,
    de donde tomamos sus ultimos pedidos. Nunca exponemos ordenes ajenas a un
    usuario anonimo mas alla del estatus y la guia del numero que el mismo dio.
    """
    if not SHIPPING_INTENT_RE.search(message or ''):
        return ''
    found = []
    match = ORDER_NUMBER_RE.search(message or '')
    if match:
        number = f'EX-{match.group(1)}-{match.group(2)}'
        order = await db.orders.find_one({'order_number': number}, {'_id': 0})
        if order:
            # Si hay sesion, solo su propia orden; si es anonimo, basta el numero exacto.
            if not user or not order.get('user_id') or order['user_id'] == user['id']:
                found.append(order)
        else:
            return (f'\n\nDATOS DEL SISTEMA: no existe ningun pedido con el numero {number}. '
                    'Pide al usuario que verifique el numero o que escriba a hola@exygenlabs.com.')
    if not found and user:
        recent = await db.orders.find({'user_id': user['id']}, {'_id': 0}).to_list(50)
        recent.sort(key=lambda o: o.get('created_at', ''), reverse=True)
        found = recent[:3]
    if not found:
        return ('\n\nDATOS DEL SISTEMA: el usuario no ha iniciado sesion y no dio un numero de pedido. '
                'Pidele su numero de pedido (formato EX-AAAAMMDD-1234) o que inicie sesion para consultarlo.')
    lines = '\n'.join('- ' + _order_summary_line(o) for o in found)
    return ('\n\nDATOS DEL SISTEMA (pedidos reales del usuario; usalos para responder sobre estatus '
            'y envio, no inventes nada mas):\n' + lines)


@api_router.post('/ai/chat')
async def ai_chat(payload: ChatInput, user=Depends(get_optional_user)):
    chat = build_chat(payload.session_id, payload.product_context)
    order_context = await build_order_context(payload.message, user)
    if order_context:
        chat['system_message'] += order_context
    prior = await db.chat_messages.find(
        {'session_id': payload.session_id}, {'_id': 0}
    ).sort('created_at', 1).to_list(50)

    await db.chat_messages.insert_one({
        'id': str(uuid.uuid4()), 'session_id': payload.session_id,
        'role': 'user', 'content': payload.message, 'created_at': now_iso(),
    })

    history_text = ''
    if prior:
        recent = prior[-8:]
        lines = []
        for m in recent:
            who = 'Usuario' if m['role'] == 'user' else 'Exygen'
            lines.append(f"{who}: {m['content']}")
        history_text = 'Conversacion previa:\n' + '\n'.join(lines) + '\n\nNuevo mensaje del usuario:\n'

    full_message = history_text + payload.message

    async def event_generator():
        collected = ''
        try:
            async for chunk in stream_reply(chat, full_message):
                collected += chunk
                yield chunk
        except Exception as e:
            logger.error(f'AI chat error: {e}')
            err = 'Lo siento, ocurrio un error al procesar tu mensaje. Intenta de nuevo.'
            collected = err
            yield err
        finally:
            await db.chat_messages.insert_one({
                'id': str(uuid.uuid4()), 'session_id': payload.session_id,
                'role': 'assistant', 'content': collected, 'created_at': now_iso(),
            })

    return StreamingResponse(
        event_generator(),
        media_type='text/plain; charset=utf-8',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@api_router.get('/ai/history/{session_id}')
async def chat_history(session_id: str):
    msgs = await db.chat_messages.find(
        {'session_id': session_id}, {'_id': 0}
    ).sort('created_at', 1).to_list(100)
    return msgs


# ----------------- Startup: seed -----------------
@app.on_event('startup')
async def seed_db():
    try:
        admin_email = os.environ.get('ADMIN_EMAIL')
        admin_password = os.environ.get('ADMIN_PASSWORD')
        if admin_email and admin_password and not await db.users.find_one({'email': admin_email.lower()}):
            await db.users.insert_one({
                'id': str(uuid.uuid4()), 'name': os.environ.get('ADMIN_NAME', 'Administrador'),
                'email': admin_email.lower(), 'password_hash': hash_password(admin_password),
                'role': 'admin', 'created_at': now_iso(),
            })
            logger.info('Seeded admin user')

        if os.environ.get('SEED_DEMO_USERS') == 'true' and not await db.users.find_one({'email': 'cliente@exygenlabs.com'}):
            await db.users.insert_one({
                'id': str(uuid.uuid4()), 'name': 'Cliente Demo',
                'email': 'cliente@exygenlabs.com', 'password_hash': hash_password('Cliente123!'),
                'role': 'user', 'created_at': now_iso(),
            })
            logger.info('Seeded test customer')

        if await db.categories.count_documents({}) == 0:
            for c in CATEGORIES:
                await db.categories.insert_one(Category(**c).model_dump())
            logger.info('Seeded categories')

        if await db.products.count_documents({}) == 0:
            for p in PRODUCTS:
                await db.products.insert_one(Product(**p).model_dump())
            logger.info(f'Seeded {len(PRODUCTS)} products')
    except Exception as e:
        logger.error(f'Seed error: {e}')


@app.on_event('shutdown')
async def shutdown_db_client():
    client.close()


app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=['*'],
    allow_headers=['*'],
)
