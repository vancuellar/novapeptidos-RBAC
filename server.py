from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
import os
import logging
import uuid
import random
from datetime import datetime, timezone
from typing import List, Optional

from database import db, client
from models import (
    RegisterInput, LoginInput, ProductCreate, ProductUpdate, Product, Category,
    OrderCreate, Order, OrderStatusUpdate, ChatInput, now_iso,
)
from auth import (
    hash_password, verify_password, create_token,
    get_current_user, get_optional_user, get_current_admin,
)
from ai_assistant import build_chat, stream_reply
from emails import send_welcome_email, normalize_language
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
    return 'NP-' + datetime.now().strftime('%Y%m%d') + '-' + str(random.randint(1000, 9999))


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
    user = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        'password_hash': hash_password(payload.password),
        'role': 'user',
        'language': normalize_language(payload.language),
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
    subtotal = sum(item.price * item.quantity for item in payload.items)
    shipping = payload.shipping if payload.shipping else (0 if subtotal >= 2500 else 199)
    total = subtotal + shipping
    order = Order(
        order_number=gen_order_number(),
        user_id=user['id'] if user else None,
        items=payload.items,
        customer=payload.customer,
        payment_method=payload.payment_method,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
    )
    await db.orders.insert_one(order.model_dump())
    for item in payload.items:
        await db.products.update_one({'id': item.product_id}, {'$inc': {'stock': -item.quantity}})
    return clean(order.model_dump())


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
    result = await db.orders.update_one({'id': order_id}, {'$set': {'status': payload.status}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    return order


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


# ----------------- AI Chat (streaming) -----------------
@api_router.post('/ai/chat')
async def ai_chat(payload: ChatInput):
    chat = build_chat(payload.session_id, payload.product_context)
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
