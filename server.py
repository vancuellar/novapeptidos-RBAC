from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response, PlainTextResponse
from starlette.middleware.cors import CORSMiddleware
import os
import base64
import logging
import uuid
import random
import string
import re
import json
import io
# `csv` a secas chocaría con el parámetro `csv=1` de /admin/envios/costo-real, que es
# como se pide el export en ese formato. Se renombra el módulo, no el parámetro.
import csv as csv_mod
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel
from database import db, client
from models import (
    RegisterInput, LoginInput, ForgotPasswordInput, ResetPasswordInput,
    ProfileUpdate, ChangePasswordInput,
    ProductCreate, ProductUpdate, Product, Category,
    OrderCreate, Order, OrderItem, CustomerInfo, OrderStatusUpdate, OrderShippingUpdate,
    DistributorShippingUpdate,
    ShippingQuoteRequest, TrackEvent, RemitenteUpdate, CajasUpdate, ComprarGuiaRequest,
    ProtocolInput, ProtocolUpdate, PerfilSalud, LabReportInput,
    TokenInput, ActivateInput, ResendVerificationInput, AceptarAcuerdoInput,
    ChatInput, DistributorCreate, DiscountCodeCreate, AnnouncementCreate, GoogleAuthInput, now_iso,
    QuoteEmailRequest,
)
from auth import (
    hash_password, verify_password, create_token, create_view_as_token, deny_view_as,
    get_current_user, get_optional_user, get_current_admin, get_current_distributor,
    get_current_marketing,
)
from ai_assistant import build_chat, stream_reply, extract_lab_report, interpret_lab_report
# ⛔ CHAT IA DE NEGOCIO (admin + distribuidores). El candado por rol vive ahí: el
# contexto que recibe el modelo se arma según quién pregunta. Ver chat_negocio.py.
import chat_negocio
import coa_store
import ficha_store
import secretos
import meta_ads
import meta_capi
import marketing
import director
import recovery
from google_auth import verify_google_token, google_enabled, GOOGLE_CLIENT_ID
from microsoft_auth import verify_microsoft_token, microsoft_enabled, MICROSOFT_CLIENT_ID
import loyalty
import pyramid
# LA REGLA DE 5 (consumo propio de distribuidores) y el cierre de la puerta
# anónima. Módulo puro para poder probarlo de verdad; ver descuentos.py.
import descuentos
# Los TEXTOS de la campanita cuando entra una venta (en los tres idiomas).
import avisos_de_venta
# ⛔ ACUERDO DE DISTRIBUIDOR — aceptación electrónica. NACE APAGADO: mientras
# ACUERDO_DISTRIBUIDOR_ACTIVO no valga 'true', ninguna de estas llamadas cambia
# nada para nadie. Ver acuerdo.py.
import acuerdo
# ⛔ QUÉ CUENTA COMO INGRESO. Una sola regla para todo el backend: ver cobrado.py.
# Los nombres se re-exportan aquí porque medio server.py (y los tests) ya los usaban
# cuando la regla vivía dentro de este archivo.
from cobrado import (ESTADOS_PAGADOS, esta_pagado, esta_vivo, cobrado_de,
                     por_cobrar_de, solo_cobrados)
import auth_factors
import btcpay
import mercadopago
import nowpayments
import envios
import paqueterias
import skydropx
from fastapi import Request


def crypto_enabled() -> bool:
    """Hay vía cripto si CUALQUIER proveedor está encendido."""
    return nowpayments.enabled() or btcpay.enabled()
from urllib.parse import urlparse, quote as urlquote
from webauthn import (
    generate_registration_options, verify_registration_response,
    generate_authentication_options, verify_authentication_response, options_to_json,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor, AuthenticatorAttachment,
)
from lab_reference import (
    MARKERS_BY_KEY, range_for, evaluate, families_for_products, relevant_markers,
)
from emails import (
    send_welcome_email, send_reset_email, send_verification_email,
    send_invitation_email, send_order_email, send_payment_confirmed_email, normalize_language, email_enabled,
    send_admin_notification, send_distributor_welcome_email, send_news_email,
    send_purchase_alert, send_shipped_email, send_quote_email,
    ATENCION_CORREO, ATENCION_NOMBRE,
)
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


# ----------------- Códigos de descuento (auto-generados por nivel) -----------------
CODE_TTL_DAYS = 90   # rotación automática: los códigos se renuevan cada 90 días


def _texto_ordenable(texto: str) -> str:
    """Llave de orden para 'sort=name_*'/'category_*' del catalogo: minusculas
    y sin acentos, para que "Nombre" y "nombre" (o "Péptido" y "Peptido")
    queden juntos en vez de separados por mayusculas/acentos."""
    import unicodedata
    t = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in t if not unicodedata.combining(c)).lower()


def gen_sku(name: str, presentation: str = '') -> str:
    """SKU legible y estable a partir del nombre: 'BPC-157 5 mg' -> 'BPC157-5MG'.

    Se arma del COMPUESTO + PRESENTACION, en mayusculas y sin simbolos. Es la
    llave que el carrito manda al hacer el pedido.
    """
    import unicodedata
    def clean(x):
        x = unicodedata.normalize('NFKD', str(x or '')).encode('ascii', 'ignore').decode()
        return re.sub(r'[^A-Za-z0-9]', '', x).upper()

    base = str(name or '')
    # separar la presentacion del final del nombre si viene pegada
    m = re.search(r'^(.*?)[\s]+([\d.,]+\s*(?:mg|iu|ml|u|g))\s*$', base, re.I)
    if m:
        compuesto, pres = m.group(1), m.group(2)
    else:
        compuesto, pres = base, presentation
    comp = clean(compuesto)[:14] or 'PROD'
    pr = clean(pres)
    return f'{comp}-{pr}' if pr else comp


def gen_discount_code(name, pct):
    """Código OPAQUE, no adivinable: PREFIJO-PCT-XXXX (parte al azar). El % en el
    texto es informativo; el descuento real SIEMPRE sale del valor guardado."""
    allowed = string.ascii_uppercase + string.digits
    base = ''.join(c for c in (name or '').upper() if c in allowed)[:6] or 'DIST'
    rand = ''.join(random.choices(allowed, k=4))
    return f'{base}-{int(round((pct or 0) * 100))}-{rand}'


async def _resolve_code(code):
    """Resuelve un código a (distribuidor, descuento). Busca primero en los códigos
    múltiples (activos y no caducados); si no, cae al código único legacy del
    distribuidor. El descuento se ACOTA a la comisión del nivel. Devuelve (None, 0)
    si no aplica. Nunca calcula el descuento del texto del código."""
    if not code:
        return None, 0.0
    c = code.strip().upper()
    doc = await db.discount_codes.find_one({'code': c, 'active': True, 'kind': {'$ne': 'coupon'}})
    if doc:
        if doc.get('expires_at') and doc['expires_at'] < now_iso():
            return None, 0.0   # caducado
        dist = await db.users.find_one({'id': doc['distributor_id'], 'role': 'distributor'},
                                       {'_id': 0, 'password_hash': 0})
        if dist:
            return dist, max(0.0, min(pyramid.effective_rate(dist), doc.get('discount_rate', 0)))
    dist = await db.users.find_one({'distributor_code': c, 'role': 'distributor'},
                                   {'_id': 0, 'password_hash': 0})
    if dist:
        return dist, max(0.0, min(pyramid.effective_rate(dist), dist.get('customer_discount_rate', 0)))
    return None, 0.0


# ----------------- Centro de noticias / notificaciones -----------------
async def notify(user_id, ntype, title, body='', link=None, dedup=None, meta=None):
    """Crea una notificación PERSONAL para un usuario. `dedup`: si se pasa, no
    duplica una del mismo tipo+dedup en los últimos 30 días (para 'por terminarse').

    `meta` cuelga datos duros del aviso (`order_number`, `client_id`) para que TOCAR LA
    CAMPANITA ABRA LA COSA, no una pantalla donde hay que volver a buscarla. Sin esto el
    aviso decía "entró el pedido EX-…" y dejaba al lector a media calle."""
    if not user_id:
        return
    if dedup:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        exists = await db.notifications.find_one(
            {'user_id': user_id, 'type': ntype, 'dedup': dedup, 'created_at': {'$gte': since}})
        if exists:
            return
    await db.notifications.insert_one({
        'id': str(uuid.uuid4()), 'kind': 'personal', 'user_id': user_id, 'type': ntype,
        'title': title, 'body': body, 'link': link, 'dedup': dedup, 'created_at': now_iso(),
        **{k: v for k, v in (meta or {}).items() if v}})


async def avisar_de_la_venta(order, commissions=None):
    """LA CAMPANITA CUANDO ENTRA UNA VENTA. Al admin y a quien ganó comisión.

    ⛔ POR QUÉ EXISTE. El sistema avisaba por correo y repartía comisiones, pero la
    campanita —lo único que se ve al entrar— no decía una palabra de las ventas. El
    2026-07-30 entraron dos pedidos con el código de María y ni Christián ni ella se
    enteraron dentro de la app.

    IDEMPOTENTE: `dedup` es el número de pedido, así que volver a llamarla (o el
    barrido retroactivo) no llena la campanita de repetidos.

    Cada quien en SU idioma: María abre la cuenta en pt-BR.
    """
    commissions = commissions if commissions is not None else (order.get('commissions') or [])
    numero = order.get('order_number') or ''
    # Para que el aviso ABRA el pedido y la ficha de quien compró, no sólo los nombre.
    donde = {'order_number': numero, 'client_id': _id_de_cliente(order)}
    vendedor = None
    if order.get('referred_by'):
        vendedor = await db.users.find_one({'id': order['referred_by']},
                                           {'_id': 0, 'id': 1, 'name': 1, 'preferred_language': 1})
    # 1) El admin. TODOS los admin, no un correo hardcodeado: si mañana hay dos, los dos
    # se enteran. El aviso vive en su cuenta, no en un buzón.
    admins = await db.users.find({'role': 'admin'},
                                 {'_id': 0, 'id': 1, 'preferred_language': 1}).to_list(20)
    for a in admins:
        titulo, cuerpo = avisos_de_venta.aviso_para_el_admin(
            order, (vendedor or {}).get('name', ''), a.get('preferred_language'))
        await notify(a['id'], 'venta_admin', titulo, cuerpo,
                     link=f'/admin?tab=orders', dedup=f'venta:{numero}', meta=donde)
    # 2) Quien ganó comisión: el vendedor y los uplines, cada uno con SU tajada.
    for row in (commissions or []):
        if row.get('amount', 0) <= 0:
            continue
        quien = await db.users.find_one({'id': row['distributor_id']},
                                        {'_id': 0, 'id': 1, 'preferred_language': 1})
        if not quien:
            continue
        titulo, cuerpo = avisos_de_venta.aviso_para_el_vendedor(
            order, row['amount'], row.get('role') != 'seller', quien.get('preferred_language'))
        await notify(quien['id'], 'new_sale', titulo, cuerpo,
                     link='/distribuidor', dedup=f'venta:{numero}', meta=donde)


async def broadcast_notification(ntype, title, body='', audience='all', link=None):
    """Aviso del admin para una audiencia (all | clients | distributors)."""
    doc = {'id': str(uuid.uuid4()), 'kind': 'broadcast', 'audience': audience, 'type': ntype,
           'title': title, 'body': body, 'link': link, 'created_at': now_iso()}
    await db.notifications.insert_one(doc)
    return doc


def _audience_for_role(role):
    """Qué broadcasts ve cada rol."""
    if role == 'distributor':
        return ['all', 'distributors']
    if role == 'admin':
        return ['all', 'clients', 'distributors']
    return ['all', 'clients']


async def _upline_chain(dist, levels=len(pyramid.TIER_ORDER)):
    """Sube por el árbol de la pirámide desde `dist`: devuelve sus uplines
    (distribuidores) del más cercano al más lejano. El override diferencial sube
    toda la cadena (el total nunca pasa de la tasa más alta). Corta ciclos."""
    chain = []
    seen = {dist['id']}
    current = dist
    for _ in range(levels):
        up_id = current.get('upline_id')
        if not up_id or up_id in seen:
            break
        up = await db.users.find_one({'id': up_id, 'role': 'distributor'}, {'_id': 0, 'password_hash': 0})
        if not up:
            break
        chain.append(up)
        seen.add(up_id)
        current = up
    return chain


async def _downline_stats(dist_id):
    """Estadísticas de la RED (downline) de un distribuidor, para la barra de nivel:
    - active_recruits: distribuidores en su red con ≥1 venta propia COBRADA.
    - team_sales: ventas propias del distribuidor + de toda su red, ya cobradas.
    Recorre el árbol por upline_id (BFS), corta ciclos.

    ⛔ UNA VENTA FIADA NO ASCIENDE A NADIE. La barra de nivel contaba todo lo no
    cancelado, así que entregar sin cobrar subía de nivel (y con el nivel, la tasa de
    comisión de todas las ventas siguientes). El nivel se gana con dinero cobrado, igual
    que la comisión."""
    dists = await db.users.find({'role': 'distributor'}, {'_id': 0, 'id': 1, 'upline_id': 1}).to_list(5000)
    children = {}
    for d in dists:
        children.setdefault(d.get('upline_id'), []).append(d['id'])
    # BFS: todos los descendientes
    network, queue, seen = [], list(children.get(dist_id, [])), set()
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid)
        network.append(nid)
        queue.extend(children.get(nid, []))
    # Ventas propias (no canceladas) por distribuidor, en un solo paso
    ids = network + [dist_id]
    rows = await db.orders.find(
        {'referred_by': {'$in': ids}, 'status': {'$ne': 'cancelado'}},
        {'_id': 0, 'referred_by': 1, 'total': 1, 'status': 1, 'paid': 1},
    ).to_list(20000)
    sales_by = {}
    for o in rows:
        sales_by[o['referred_by']] = sales_by.get(o['referred_by'], 0) + cobrado_de(o)
    active_recruits = sum(1 for nid in network if sales_by.get(nid, 0) > 0)
    team_sales = sum(sales_by.values())
    return {'active_recruits': active_recruits, 'team_sales': team_sales,
            'personal_sales': sales_by.get(dist_id, 0), 'network_size': len(network)}


# ----------------- Health -----------------
@api_router.get('/')
async def root():
    return {'message': 'Exygen Labs API', 'status': 'ok'}


# ----------------- Auth -----------------

def _session_user(user):
    """Lo que el frontend guarda de la sesión al entrar.

    `extra_roles` SUMA papeles (María: distribuidora + difusión) y las
    preferencias hacen que su cuenta abra en su idioma y tema, sin importar
    el navegador. Cambia aquí y cambian TODAS las formas de iniciar sesión."""
    return {
        'id': user['id'], 'name': user['name'], 'email': user['email'],
        'role': user.get('role', 'user'),
        'extra_roles': user.get('extra_roles') or [],
        'preferred_language': user.get('preferred_language'),
        'preferred_theme': user.get('preferred_theme'),
    }

async def _usuario_por_correo(email: str):
    """Busca la cuenta por su correo principal O por un correo alterno.

    Una misma persona puede tener dos direcciones (p. ej. la cuenta admin y su
    Gmail): `alt_emails` las liga a UNA sola cuenta, y todas las puertas de
    entrada (contraseña, Google, Outlook, recuperación) deben mirar ambas."""
    e = (email or '').lower()
    return await db.users.find_one({'$or': [{'email': e}, {'alt_emails': e}]})


@api_router.post('/auth/register')
async def register(payload: RegisterInput):
    existing = await _usuario_por_correo(payload.email)
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    if not payload.age_confirmed:
        raise HTTPException(status_code=400, detail='Debes confirmar que tienes 18 anos o mas y aceptar los Terminos y Condiciones')
    if not payload.privacy_accepted:
        raise HTTPException(status_code=400, detail='Debes aceptar la Politica de privacidad')
    referrer = await resolve_distributor(payload.distributor_code)
    consented_at = now_iso()
    user = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        'password_hash': hash_password(payload.password),
        'role': 'user',
        'language': normalize_language(payload.language),
        'referred_by': referrer['id'] if referrer else None,
        # Registro de consentimiento: guardamos QUE aceptó y CUÁNDO, porque es
        # lo que hay que poder demostrar si alguien lo pregunta.
        'consents': {
            'age_confirmed': True,
            'privacy_accepted': True,
            'marketing_email': bool(payload.marketing_email),
            'promos': bool(payload.promos),
            'accepted_at': consented_at,
        },
        'created_at': consented_at,
    }
    # Solo exigimos confirmacion si el correo saliente esta encendido. Si no,
    # la cuenta nace confirmada: nadie puede quedar encerrado fuera por una
    # configuracion del servidor.
    require_confirmation = email_enabled()
    user['email_verified'] = not require_confirmation
    await db.users.insert_one(user)
    if require_confirmation:
        await _send_verification(user)
        return {
            'pending_verification': True,
            'email': user['email'],
            'message': 'Te mandamos un correo para confirmar tu cuenta. Revisa tambien la carpeta de spam.',
        }
    asyncio.create_task(send_welcome_email(user['name'], user['email'], user['language']))
    # Solo se llega aqui cuando el correo saliente esta APAGADO y la cuenta nace ya
    # confirmada; con el encendido, este registro devuelve 'pending_verification' y
    # no adopta nada hasta que abra el enlace.
    adoptados = await _adoptar_pedidos_de_invitado(user['id'])
    return {
        'pending_verification': False,
        'token': create_token(user['id']),
        'user': _session_user(user),
        'adopted_orders': adoptados,
    }


@api_router.post('/auth/login')
async def login(payload: LoginInput):
    user = await _usuario_por_correo(payload.email)
    if not user or not verify_password(payload.password, user.get('password_hash', '')):
        raise HTTPException(status_code=401, detail='Correo o contrasena incorrectos')
    if user.get('blocked'):
        raise HTTPException(status_code=403, detail='Esta cuenta esta deshabilitada')
    # Las cuentas viejas no tienen el campo: se dan por confirmadas para no dejar
    # a nadie fuera. Solo las nuevas nacen sin confirmar.
    if user.get('email_verified') is False and email_enabled():
        raise HTTPException(
            status_code=403,
            detail='Confirma tu correo antes de entrar. Te mandamos el enlace cuando creaste la cuenta.',
        )
    # Segundo factor: si la cuenta lo tiene encendido (solo admins), la
    # contrasena sola no basta. Se entrega un pase corto y se pide el codigo.
    if user.get('totp_enabled'):
        pre = await _issue_token(user['id'], 'totp',
                                 (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        return {'needs_totp': True, 'pre_token': pre}
    token = create_token(user['id'])
    return {
        'token': token,
        'user': _session_user(user),
    }


@api_router.post('/auth/totp')
async def totp_login(payload: dict):
    """Segundo paso del login con 2FA: pase corto + codigo de la app.

    El pase NO se consume si el codigo es incorrecto: equivocarse tecleando
    no debe obligar a re-escribir la contrasena (el pase vive 5 minutos)."""
    rec = await db.account_tokens.find_one({'token': payload.get('pre_token', ''), 'purpose': 'totp', 'used': False})
    if not rec or rec.get('expires_at', '') < now_iso():
        raise HTTPException(status_code=401, detail='La sesion expiro. Vuelve a entrar con tu contrasena.')
    user = await db.users.find_one({'id': rec['user_id']})
    if not user or not auth_factors.verify_totp(user.get('totp_secret', ''), payload.get('code', '')):
        raise HTTPException(status_code=401, detail='Codigo incorrecto. Revisa tu app autenticadora.')
    await db.account_tokens.update_one({'token': rec['token']}, {'$set': {'used': True}})
    return {
        'token': create_token(user['id']),
        'user': _session_user(user),
    }


@api_router.get('/auth/google/config')
async def google_config():
    """El sitio pregunta si Google Sign-In esta encendido y con que client id.
    Si no hay client id configurado, el boton no se muestra."""
    return {'enabled': google_enabled(), 'client_id': GOOGLE_CLIENT_ID if google_enabled() else ''}


async def _social_login(info: dict, payload: GoogleAuthInput, sub_field: str, source: str):
    """Entra o crea la cuenta con una identidad ya verificada (Google/Microsoft).

    El proveedor ya verifico el correo, asi que la cuenta nace confirmada: no
    tiene sentido mandar un correo de confirmacion a una direccion recien
    validada. Si el correo ya existe con contrasena, se vincula y entra: es la
    misma persona.
    """
    user = await _usuario_por_correo(info['email'])
    if user and user.get('blocked'):
        raise HTTPException(status_code=403, detail='Esta cuenta esta deshabilitada')
    if user:
        # Cuenta existente: se vincula con el proveedor y se da por confirmada.
        # No se piden consentimientos: ya los dio al registrarse.
        await db.users.update_one(
            {'id': user['id']},
            {'$set': {sub_field: info[sub_field], 'email_verified': True}},
        )
    else:
        # Cuenta NUEVA: el proveedor avala el correo, pero 18+/Terminos y
        # Privacidad los tiene que aceptar la persona. Sin eso, el sitio pide
        # las casillas y reintenta con la misma credencial.
        if not (payload.age_confirmed and payload.privacy_accepted):
            return {'needs_consent': True, 'name': info['name'], 'email': info['email']}
        referrer = await resolve_distributor(payload.distributor_code)
        consented_at = now_iso()
        user = {
            'id': str(uuid.uuid4()),
            'name': info['name'],
            'email': info['email'],
            # Sin contrasena: solo entra con el proveedor hasta que use
            # "recuperar contrasena" para ponerse una.
            'password_hash': '',
            'role': 'user',
            'language': normalize_language(payload.language),
            'referred_by': referrer['id'] if referrer else None,
            sub_field: info[sub_field],
            'email_verified': True,
            'consents': {
                'age_confirmed': True,
                'privacy_accepted': True,
                'marketing_email': bool(payload.marketing_email),
                'promos': bool(payload.promos),
                'accepted_at': consented_at,
                'source': source,
            },
            'created_at': consented_at,
        }
        await db.users.insert_one(user)
        asyncio.create_task(send_welcome_email(user['name'], user['email'], user['language']))

    # El proveedor ya avalo el correo, asi que la cuenta entra confirmada: es un
    # momento de confirmacion igual de bueno que abrir el enlace, y sirve para
    # las dos ramas (la cuenta que ya existia y la que se acaba de crear).
    adoptados = await _adoptar_pedidos_de_invitado(user['id'])
    return {
        'token': create_token(user['id']),
        'user': _session_user(user),
        'adopted_orders': adoptados,
    }


@api_router.post('/auth/google')
async def google_login(payload: GoogleAuthInput):
    try:
        info = await verify_google_token(payload.credential)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return await _social_login(info, payload, 'google_sub', 'google')


@api_router.get('/auth/microsoft/config')
async def microsoft_config():
    """El sitio pregunta si el login con Outlook esta encendido y con que
    client id. Si no hay client id configurado, el boton no se muestra."""
    return {'enabled': microsoft_enabled(), 'client_id': MICROSOFT_CLIENT_ID if microsoft_enabled() else ''}


@api_router.post('/auth/microsoft')
async def microsoft_login(payload: GoogleAuthInput):
    # El payload es identico al de Google (credencial + consentimientos).
    try:
        info = await verify_microsoft_token(payload.credential)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return await _social_login(info, payload, 'microsoft_sub', 'microsoft')


SITE_URL = os.environ.get('SITE_URL', 'https://exygenlabs.com')
# URL pública del API, para los webhooks que le mandan los proveedores de pago.
API_BASE_URL = os.environ.get('API_BASE_URL', 'https://api.exygenlabs.com')

VERIFY_TTL_HOURS = 24
INVITE_TTL_DAYS = 7


async def _issue_token(user_id: str, purpose: str, expires_at: str) -> str:
    token = uuid.uuid4().hex
    await db.account_tokens.insert_one({
        'token': token, 'user_id': user_id, 'purpose': purpose,
        'expires_at': expires_at, 'used': False, 'created_at': now_iso(),
    })
    return token


async def _consume_token(token: str, purpose: str):
    """Devuelve el usuario de un token válido y lo marca usado. Un token sirve una vez."""
    rec = await db.account_tokens.find_one({'token': token, 'purpose': purpose, 'used': False}, {'_id': 0})
    if not rec or rec.get('expires_at', '') < now_iso():
        raise HTTPException(status_code=400, detail='El enlace no es valido o ya expiro. Solicita uno nuevo.')
    user = await db.users.find_one({'id': rec['user_id']})
    if not user:
        raise HTTPException(status_code=400, detail='El enlace no es valido o ya expiro. Solicita uno nuevo.')
    await db.account_tokens.update_one({'token': token}, {'$set': {'used': True}})
    return user


# ----------------- Llaves de acceso (passkeys) y 2FA -----------------
# El RP ID es el dominio del sitio: las llaves creadas en exygenlabs.com solo
# sirven en exygenlabs.com. Configurable por env para pruebas locales.
PASSKEY_RP_ID = os.environ.get('PASSKEY_RP_ID') or (urlparse(SITE_URL).hostname or 'localhost')
PASSKEY_ORIGIN = os.environ.get('PASSKEY_ORIGIN', SITE_URL)
CHALLENGE_TTL_MINUTES = 5


async def _store_challenge(challenge: bytes, purpose: str, user_id=None) -> str:
    cid = uuid.uuid4().hex
    await db.webauthn_challenges.insert_one({
        'id': cid, 'challenge': bytes_to_base64url(challenge), 'purpose': purpose,
        'user_id': user_id,
        'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_TTL_MINUTES)).isoformat(),
    })
    return cid


async def _take_challenge(cid: str, purpose: str):
    """Un reto se usa UNA vez: se borra al leerlo (evita repeticion)."""
    rec = await db.webauthn_challenges.find_one_and_delete({'id': cid or '', 'purpose': purpose})
    if not rec or rec.get('expires_at', '') < now_iso():
        raise HTTPException(status_code=400, detail='La solicitud expiro. Intenta de nuevo.')
    return rec


def _passkey_public(row: dict) -> dict:
    return {'id': row['id'], 'name': row.get('name', ''), 'created_at': row.get('created_at', '')}


@api_router.post('/me/passkeys/options')
async def passkey_register_options(user=Depends(get_current_user)):
    deny_view_as(user)
    existing = await db.passkeys.find({'user_id': user['id']}, {'_id': 0}).to_list(50)
    options = generate_registration_options(
        rp_id=PASSKEY_RP_ID,
        rp_name='Exygen Labs',
        user_id=user['id'].encode(),
        user_name=user['email'],
        user_display_name=user.get('name') or user['email'],
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c['credential_id'])) for c in existing
        ],
        # PLATFORM = el sensor del propio equipo (Touch ID / Face ID / Windows
        # Hello), no una llave USB externa. user_verification REQUIRED obliga a la
        # biometría o PIN. resident_key REQUIRED = entra sin escribir el correo.
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    cid = await _store_challenge(options.challenge, 'register', user['id'])
    return {'challenge_id': cid, 'options': json.loads(options_to_json(options))}


@api_router.post('/me/passkeys/verify')
async def passkey_register_verify(payload: dict, user=Depends(get_current_user)):
    deny_view_as(user)
    rec = await _take_challenge(payload.get('challenge_id'), 'register')
    if rec.get('user_id') != user['id']:
        raise HTTPException(status_code=400, detail='La solicitud expiro. Intenta de nuevo.')
    try:
        verified = verify_registration_response(
            credential=payload.get('credential'),
            expected_challenge=base64url_to_bytes(rec['challenge']),
            expected_rp_id=PASSKEY_RP_ID,
            expected_origin=PASSKEY_ORIGIN,
        )
    except Exception:
        raise HTTPException(status_code=400, detail='No se pudo registrar la llave de acceso.')
    await db.passkeys.insert_one({
        'id': str(uuid.uuid4()), 'user_id': user['id'],
        'credential_id': bytes_to_base64url(verified.credential_id),
        'public_key': bytes_to_base64url(verified.credential_public_key),
        'sign_count': verified.sign_count,
        'name': str(payload.get('name') or 'Llave de acceso')[:60],
        'created_at': now_iso(),
    })
    rows = await db.passkeys.find({'user_id': user['id']}, {'_id': 0}).to_list(50)
    return [_passkey_public(r) for r in rows]


@api_router.get('/me/passkeys')
async def passkey_list(user=Depends(get_current_user)):
    rows = await db.passkeys.find({'user_id': user['id']}, {'_id': 0}).to_list(50)
    return [_passkey_public(r) for r in rows]


@api_router.delete('/me/passkeys/{passkey_id}')
async def passkey_delete(passkey_id: str, user=Depends(get_current_user)):
    deny_view_as(user)
    result = await db.passkeys.delete_one({'id': passkey_id, 'user_id': user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail='Llave no encontrada')
    rows = await db.passkeys.find({'user_id': user['id']}, {'_id': 0}).to_list(50)
    return [_passkey_public(r) for r in rows]


@api_router.post('/auth/passkey/options')
async def passkey_login_options():
    """Publico y sin usuario: la llave descubrible dice quien es."""
    options = generate_authentication_options(
        rp_id=PASSKEY_RP_ID,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    cid = await _store_challenge(options.challenge, 'login')
    return {'challenge_id': cid, 'options': json.loads(options_to_json(options))}


@api_router.post('/auth/passkey/verify')
async def passkey_login_verify(payload: dict):
    rec = await _take_challenge(payload.get('challenge_id'), 'login')
    credential = payload.get('credential') or {}
    row = await db.passkeys.find_one({'credential_id': credential.get('id', '')}, {'_id': 0})
    if not row:
        raise HTTPException(status_code=401, detail='Llave de acceso no reconocida.')
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(rec['challenge']),
            expected_rp_id=PASSKEY_RP_ID,
            expected_origin=PASSKEY_ORIGIN,
            credential_public_key=base64url_to_bytes(row['public_key']),
            credential_current_sign_count=int(row.get('sign_count', 0) or 0),
        )
    except Exception:
        raise HTTPException(status_code=401, detail='No se pudo verificar la llave de acceso.')
    await db.passkeys.update_one({'id': row['id']}, {'$set': {'sign_count': verified.new_sign_count}})
    user = await db.users.find_one({'id': row['user_id']}, {'_id': 0, 'password_hash': 0})
    if not user:
        raise HTTPException(status_code=401, detail='La cuenta ya no existe.')
    # La llave de acceso ya es un factor fuerte y resistente a phishing:
    # no se pide TOTP encima.
    return {
        'token': create_token(user['id']),
        'user': _session_user(user),
    }


@api_router.post('/me/totp/setup')
async def totp_setup(user=Depends(get_current_user)):
    """Genera el secreto y el QR. Solo admins: para clientes la via segura y
    sencilla es Google o una llave de acceso (decision de Christian)."""
    deny_view_as(user)
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='El codigo 2FA es solo para administradores.')
    secret = auth_factors.new_totp_secret()
    await db.users.update_one({'id': user['id']}, {'$set': {'totp_secret_pending': secret}})
    uri = auth_factors.totp_uri(secret, user['email'])
    return {'otpauth': uri, 'qr': auth_factors.qr_data_uri(uri), 'secret': secret}


@api_router.post('/me/totp/enable')
async def totp_enable(payload: dict, user=Depends(get_current_user)):
    """Enciende el 2FA solo despues de comprobar un codigo real: si el QR no
    se escaneo bien, encenderlo dejaria al admin fuera de su propia cuenta."""
    deny_view_as(user)
    fresh = await db.users.find_one({'id': user['id']}, {'_id': 0, 'totp_secret_pending': 1})
    secret = (fresh or {}).get('totp_secret_pending', '')
    if not auth_factors.verify_totp(secret, payload.get('code', '')):
        raise HTTPException(status_code=400, detail='Codigo incorrecto. Escanea el QR y prueba de nuevo.')
    await db.users.update_one(
        {'id': user['id']},
        {'$set': {'totp_secret': secret, 'totp_enabled': True}, '$unset': {'totp_secret_pending': ''}},
    )
    return {'totp_enabled': True}


@api_router.post('/me/totp/disable')
async def totp_disable(payload: dict, user=Depends(get_current_user)):
    deny_view_as(user)
    fresh = await db.users.find_one({'id': user['id']}, {'_id': 0, 'totp_secret': 1})
    if not auth_factors.verify_totp((fresh or {}).get('totp_secret', ''), payload.get('code', '')):
        raise HTTPException(status_code=400, detail='Codigo incorrecto.')
    await db.users.update_one(
        {'id': user['id']},
        {'$set': {'totp_enabled': False}, '$unset': {'totp_secret': ''}},
    )
    return {'totp_enabled': False}


async def _send_verification(user: dict):
    expires = (datetime.now(timezone.utc) + timedelta(hours=VERIFY_TTL_HOURS)).isoformat()
    token = await _issue_token(user['id'], 'verify', expires)
    link = f'{SITE_URL}/confirmar?token={token}'
    asyncio.create_task(send_verification_email(user['name'], user['email'], link, user.get('language')))


async def _send_invitation(user: dict) -> str:
    """Manda la invitacion y devuelve el enlace. Si el correo saliente esta
    apagado se lo entregamos al admin para que lo comparta el mismo."""
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)).isoformat()
    token = await _issue_token(user['id'], 'invite', expires)
    link = f'{SITE_URL}/activar?token={token}'
    asyncio.create_task(send_invitation_email(user['name'], user['email'], link, user.get('language')))
    return link


async def _send_distributor_invitation(dist: dict) -> str:
    """Como _send_invitation, pero manda el correo PROPIO del distribuidor (con
    su código de referido y la bienvenida al programa). Devuelve el enlace de
    activación (o para que el admin lo comparta si el correo está apagado)."""
    expires = (datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)).isoformat()
    token = await _issue_token(dist['id'], 'invite', expires)
    link = f'{SITE_URL}/activar?token={token}'
    asyncio.create_task(send_distributor_welcome_email(
        dist['name'], dist['email'], dist.get('distributor_code', ''), link,
        dist.get('language'), needs_activation=True))
    return link


@api_router.post('/auth/verify-email')
async def verify_email(payload: TokenInput):
    """Confirma el correo y deja la sesion iniciada: sin fricción extra."""
    user = await _consume_token(payload.token, 'verify')
    if not user.get('email_verified'):
        await db.users.update_one({'id': user['id']}, {'$set': {'email_verified': True, 'verified_at': now_iso()}})
        asyncio.create_task(send_welcome_email(user['name'], user['email'], user.get('language')))
    # ESTE es el momento en que el correo queda probado, y por eso es el unico
    # momento en que se pueden adoptar las compras que hizo como invitado.
    adoptados = await _adoptar_pedidos_de_invitado(user['id'])
    return {
        'token': create_token(user['id']),
        'user': _session_user(user),
        'adopted_orders': adoptados,
    }


@api_router.post('/auth/resend-verification')
async def resend_verification(payload: ResendVerificationInput):
    """Siempre responde ok: no revelamos si el correo existe."""
    user = await _usuario_por_correo(payload.email)
    if user and user.get('email_verified') is False:
        user['language'] = payload.language or user.get('language')
        await _send_verification(user)
    return {'ok': True}


@api_router.get('/auth/invitation/{token}')
async def read_invitation(token: str):
    """Datos mínimos para pintar la pantalla de activación. No consume el token."""
    rec = await db.account_tokens.find_one({'token': token, 'purpose': 'invite', 'used': False}, {'_id': 0})
    if not rec or rec.get('expires_at', '') < now_iso():
        raise HTTPException(status_code=400, detail='Esta invitacion ya no es valida. Pide una nueva.')
    user = await db.users.find_one({'id': rec['user_id']}, {'_id': 0, 'password_hash': 0})
    if not user:
        raise HTTPException(status_code=400, detail='Esta invitacion ya no es valida. Pide una nueva.')
    return {'name': user['name'], 'email': user['email'], 'role': user.get('role', 'user')}


@api_router.post('/auth/activate')
async def activate_account(payload: ActivateInput, request: Request):
    """El invitado elige su contraseña; eso mismo confirma su correo.

    ⛔ ACUERDO DE DISTRIBUIDOR (sólo con el interruptor encendido). Si quien
    activa es distribuidor, la pantalla le enseñó el acuerdo completo con una
    casilla NO premarcada; aquí se exige que venga marcada y se levanta el acta
    (versión, hash, fecha, IP, user-agent). ES EL MOMENTO CORRECTO para firmar:
    es cuando de verdad entra al canal, y así ningún distribuidor nuevo empieza
    a operar sin contrato.

    Con el interruptor APAGADO —como está hoy— `acuerdo.activo()` es False y
    todo este bloque se salta entero: la activación es exactamente la de
    siempre, marque o no marque la casilla (que ni siquiera se le pinta)."""
    user = await _consume_token(payload.token, 'invite')
    if acuerdo.activo() and acuerdo.es_distribuidor(user) and not payload.acepta_acuerdo:
        raise HTTPException(status_code=400,
                            detail='Para activar tu cuenta de distribuidor tienes que '
                                   'leer y aceptar el Acuerdo de Distribuidor.')
    await db.users.update_one({'id': user['id']}, {'$set': {
        'password_hash': hash_password(payload.password),
        'email_verified': True,
        'verified_at': now_iso(),
    }})
    if acuerdo.activo() and acuerdo.es_distribuidor(user) and payload.acepta_acuerdo:
        await acuerdo.registrar(db, user, ip=acuerdo.ip_de(request),
                                user_agent=acuerdo.user_agent_de(request), origen='activacion')
    asyncio.create_task(send_welcome_email(user['name'], user['email'], user.get('language')))
    # Activar la invitacion confirma el correo (solo llega al buzon real), asi que
    # aqui tambien se recogen las compras que haya hecho antes como invitado.
    adoptados = await _adoptar_pedidos_de_invitado(user['id'])
    return {
        'token': create_token(user['id']),
        'user': _session_user(user),
        'adopted_orders': adoptados,
    }


@api_router.post('/auth/forgot-password')
async def forgot_password(payload: ForgotPasswordInput):
    """Siempre responde ok (no revela si el correo existe)."""
    user = await _usuario_por_correo(payload.email)
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
    deny_view_as(user)
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
        if await _usuario_por_correo(payload.email):
            raise HTTPException(status_code=400, detail='Ese correo ya esta registrado')
        update['email'] = payload.email.lower()
    if update:
        await db.users.update_one({'id': user['id']}, {'$set': update})
    return await db.users.find_one({'id': user['id']}, {'_id': 0, 'password_hash': 0})


@api_router.post('/auth/change-password')
async def change_password(payload: ChangePasswordInput, user=Depends(get_current_user)):
    deny_view_as(user)
    full = await db.users.find_one({'id': user['id']})
    if not verify_password(payload.current_password, full['password_hash']):
        raise HTTPException(status_code=400, detail='La contrasena actual no es correcta')
    await db.users.update_one({'id': user['id']}, {'$set': {'password_hash': hash_password(payload.new_password)}})
    return {'ok': True}


@api_router.get('/auth/me')
async def me(user=Depends(get_current_user)):
    # Un distribuidor compra para sí mismo con SU comisión máxima como descuento
    # (Christian, 2026-07-25). El carrito lo necesita para mostrarlo en vivo.
    return {**user, 'self_discount_rate': buyer_own_rate(user)}


IDIOMAS_VALIDOS = {'es-MX', 'en-US', 'pt-BR'}
TEMAS_VALIDOS = {'light', 'dark', 'system'}


class PrefsDeCuenta(BaseModel):
    language: Optional[str] = None
    theme: Optional[str] = None


@api_router.put('/auth/me/prefs')
async def guardar_prefs(payload: PrefsDeCuenta, user=Depends(get_current_user)):
    """Idioma y tema viajan con la CUENTA, no con el navegador.

    La cuenta abre con lo que el admin dejó puesto (María: portugués y oscuro)
    hasta que el propio usuario cambia algo — y entonces manda SU elección."""
    deny_view_as(user)
    cambio = {}
    if payload.language is not None:
        if payload.language not in IDIOMAS_VALIDOS:
            raise HTTPException(status_code=400, detail='Idioma desconocido')
        cambio['preferred_language'] = payload.language
    if payload.theme is not None:
        if payload.theme not in TEMAS_VALIDOS:
            raise HTTPException(status_code=400, detail='Tema desconocido')
        cambio['preferred_theme'] = payload.theme
    if cambio:
        await db.users.update_one({'id': user['id']}, {'$set': cambio})
    return {'ok': True, **cambio}


ROLES_EXTRA_VALIDOS = {'marketing'}


class RolesExtra(BaseModel):
    roles: list[str]
    language: Optional[str] = None
    theme: Optional[str] = None


@api_router.put('/admin/customers/{user_id}/extra-roles')
async def set_extra_roles(user_id: str, payload: RolesExtra, admin=Depends(get_current_admin)):
    """Papeles que SUMAN sin quitar el rol principal (María: distribuidora que
    además lleva la difusión). De paso el admin puede dejar puestas las
    preferencias de arranque de la cuenta (idioma/tema)."""
    if not set(payload.roles) <= ROLES_EXTRA_VALIDOS:
        raise HTTPException(status_code=400, detail='Rol extra desconocido')
    target = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'email': 1})
    if not target:
        raise HTTPException(status_code=404, detail='Usuario no encontrado')
    cambio = {'extra_roles': sorted(set(payload.roles))}
    if payload.language is not None:
        if payload.language not in IDIOMAS_VALIDOS:
            raise HTTPException(status_code=400, detail='Idioma desconocido')
        cambio['preferred_language'] = payload.language
    if payload.theme is not None:
        if payload.theme not in TEMAS_VALIDOS:
            raise HTTPException(status_code=400, detail='Tema desconocido')
        cambio['preferred_theme'] = payload.theme
    await db.users.update_one({'id': user_id}, {'$set': cambio})
    logger.info('Admin %s dejó extra_roles=%s a %s', admin.get('email'),
                cambio['extra_roles'], target.get('email'))
    return {'id': user_id, **cambio}


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
        # Un combo puede estar en su categoría funcional Y en 'stacks': se busca
        # en la principal o en las adicionales.
        query['$or'] = [{'category': category}, {'extra_categories': category}]
    if featured is not None:
        query['featured'] = featured
    if search:
        search_or = [
            {'name': {'$regex': search, '$options': 'i'}},
            {'short_description': {'$regex': search, '$options': 'i'}},
            {'description': {'$regex': search, '$options': 'i'}},
        ]
        if '$or' in query:            # ya hay filtro de categoría: exige ambos
            query = {'$and': [{'$or': query.pop('$or')}, {'$or': search_or}]}
        else:
            query['$or'] = search_or
    if in_stock:
        query['stock'] = {'$gt': 0}
    price_q = {}
    if min_price is not None:
        price_q['$gte'] = min_price
    if max_price is not None:
        price_q['$lte'] = max_price
    if price_q:
        query['price'] = price_q

    # Los ocultos no salen NUNCA del catálogo público (ver `hidden` en models.py).
    query['hidden'] = {'$ne': True}
    cursor = db.products.find(query, {'_id': 0})
    products = await cursor.to_list(500)

    if sort == 'price_asc':
        products.sort(key=lambda p: p.get('price', 0))
    elif sort == 'price_desc':
        products.sort(key=lambda p: p.get('price', 0), reverse=True)
    elif sort == 'newest':
        products.sort(key=lambda p: p.get('created_at', ''), reverse=True)
    elif sort == 'name_asc':
        products.sort(key=lambda p: _texto_ordenable(p.get('name', '')))
    elif sort == 'name_desc':
        products.sort(key=lambda p: _texto_ordenable(p.get('name', '')), reverse=True)
    elif sort in ('category_asc', 'category_desc'):
        # Dos pasadas con sort estable: primero nombre (A-Z, siempre), luego
        # categoría (en la dirección pedida). Así el resultado dentro de cada
        # categoría queda predecible sin importar si la categoría va A-Z o Z-A.
        products.sort(key=lambda p: _texto_ordenable(p.get('name', '')))
        products.sort(key=lambda p: _texto_ordenable(p.get('category', '')),
                      reverse=(sort == 'category_desc'))
    # ⛔ Sin sesión no salen los campos de margen. Ver `vista_publica_de_producto`.
    return [vista_publica_de_producto(p) for p in products]


@api_router.get('/products/{slug}')
async def get_product(slug: str):
    product = await db.products.find_one({'slug': slug}, {'_id': 0})
    if not product or product.get('hidden'):
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    return vista_publica_de_producto(product)


# ----------------- Admin: Products -----------------
@api_router.get('/admin/products')
async def admin_list_products(admin=Depends(get_current_admin)):
    # Catálogo COMPLETO, ocultos incluidos. La lista pública filtra `hidden`,
    # así que sin esta ruta un producto oculto no se puede volver a encontrar
    # por SKU para re-mostrarlo (ocultar_productos.js --mostrar).
    return await db.products.find({}, {'_id': 0}).to_list(1000)


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
    doc = await db.products.find_one({'id': product_id},
                                     {'_id': 0, 'id': 1, 'sku': 1, 'slug': 1,
                                      'presentation': 1})
    result = await db.products.delete_one({'id': product_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    # Y su renglón de inventario vivo se va con él. Si se queda, `db.stock` acumula
    # llaves huérfanas —existencias de productos que ya no existen— que el Panel sigue
    # mostrando y nadie puede reconciliar contra nada.
    await db.stock.delete_many(
        {'key': {'$in': llaves_de_inventario_vivo(product_id, doc)}})
    return {'ok': True}


# ----------------- Orders -----------------
# Tope duro de comision de distribuidores (regla de Christian, 2026-07-21).
COMMISSION_CAP = 0.50


# ⛔⛔ EL TECHO ÚNICO DEL DESCUENTO — 40%. VIVE AQUÍ Y EN NINGÚN OTRO LADO.
#
# Christián, 2026-07-31, textual:
#   «Baja también Paz Cambray a 40%, nadie por encima de ese 40% a menos que seamos
#    María y yo.»
#
# POR QUÉ UNA SOLA FUNCIÓN Y NO UN `min(0.40, …)` EN CADA PUERTA: porque ya se intentó
# lo otro y falló tres veces. El techo del 40% se fue descubriendo de una en una —
# la venta directa del admin estaba en 60% (cerrada el 29-jul), el cupón GIFT en 50%
# (el 31-jul) y el trato especial por cuenta en 50% (el 31-jul también) — y cada vez
# el auditor tuvo que encontrar la puerta olvidada. Cada `min` suelto es una puerta
# más que alguien puede subir sin que nada truene. Aquí sólo hay un número.
#
# LA EXCEPCIÓN son las cuentas de Christián y María, y es SOLO PARA LO QUE ELLOS
# COMPRAN. No es permiso para REGALAR más del 40% a un tercero: si María pudiera
# otorgar 50%, habría un cliente por encima del techo, que es justo lo prohibido.
#
# ⚠️ ENCIMA DE ESTO SIGUEN MANDANDO SIEMPRE, sin excepción para nadie:
#   · el tope de CADA producto (`commission_cap`) — el ROI de la casa manda;
#   · los insumos (`NO_DISCOUNT_CATEGORIES`) nunca llevan descuento;
#   · con el 40% NO se acumulan puntos (`loyalty.earns_points`).
TECHO_DESCUENTO = loyalty.MAX_DISCOUNT          # 0.40 — un solo número, un solo lugar


def sin_tope_de_descuento(user) -> bool:
    """¿Esta cuenta puede comprar por encima del 40%? Sólo Christián y María.

    Se decide por DATOS de la cuenta, nunca por un correo escrito en el código: un
    correo hardcodeado es una puerta trasera que nadie encuentra al auditar, y el día
    que Christián cambie de correo deja de funcionar sin avisar.

      · `role == 'admin'`  → Christián.
      · `descuento_sin_tope`  → la marca que Christián le pone a María desde el Panel.

    Nace APAGADA para todo el mundo: si nadie la trae, el techo es 40% para todos, que
    es exactamente el estado que pidió Christián."""
    if not user:
        return False
    return user.get('role') == 'admin' or bool(user.get('descuento_sin_tope'))


def techo_de_descuento(user=None) -> float:
    """El descuento MÁXIMO que puede llevar una compra de `user`. 40% salvo Christián
    y María. Toda puerta de descuento del sistema pasa por aquí."""
    return COMMISSION_CAP if sin_tope_de_descuento(user) else TECHO_DESCUENTO

# Insumos: NUNCA entran a ningun descuento ni pagan comision (regla de Christian,
# 2026-07-25). El agua bacteriostatica es el caso que lo motivo: se vende casi al
# costo, un descuento encima la deja en perdida. El descuento se aplica a los demas
# productos y estos quedan a precio de lista.
NO_DISCOUNT_CATEGORIES = {'suministros', 'accesorios'}


def tasa_de_cupon(doc):
    """El descuento que vale un cupón, TOPADO AL MÁXIMO DE LA CASA (40%).

    ⛔ «El regalo debe estar topado en 40%» (Christián, 2026-07-31). El cupón GIFT del
    admin se creaba con `min(0.50, …)`, así que era la única puerta por la que salía un
    descuento arriba del techo de la casa — más alto incluso que el de la venta directa,
    que sí se capó el 29-jul. Un regalo del 50% es medio producto regalado.

    El tope se aplica AL USARLO, no sólo al crearlo, por dos razones: los cupones que ya
    andan sueltos con 50% tienen que cobrar 40% (no se cancelan — el cliente ya tiene su
    regalo, sólo vale menos), y el candado no depende de por dónde se creó el documento.

    Encima de esto sigue mandando el tope de CADA producto (`_disc_of` → `commission_cap`)
    y los insumos siguen fuera: el 40% es el techo, no un piso garantizado.

    El número sale de `techo_de_descuento`, que es donde vive el único 40% del sistema.
    Un cupón es un REGALO a un tercero, así que aquí no hay excepción de cuenta: la de
    Christián y María es para lo que ELLOS compran, no para lo que regalan."""
    return max(0.0, min(TECHO_DESCUENTO, float((doc or {}).get('discount_rate') or 0)))


def es_hgh_neto(product_id, name):
    """Familia HGH (no el Fragment): precio NETO siempre — su margen no aguanta
    ningún descuento (Christian, 2026-07-22).

    Miramos id Y nombre porque en producción el product_id es un UUID (no dice
    "hgh"); el nombre sí. Vive a nivel de módulo porque ahora lo usan DOS caminos
    —el checkout y el cotizador del distribuidor— y con la regla copiada en dos
    lados una de las dos se queda atrás y el cotizador promete lo que la caja no
    respeta."""
    key = f'{product_id or ""} {name or ""}'.lower()
    return 'hgh' in key and 'fragment' not in key


def tope_de_descuento(doc):
    """Cuánto descuento aguanta UN producto del catálogo. Cero = no participa.

    Es la MISMA regla que aplica el checkout renglón por renglón (ver `_cap_of` /
    `_eligible` en `create_order`): insumos fuera, productos que no dejan 5x neto
    fuera, familia HGH a precio neto, y el resto acotado por su `commission_cap`.

    ⛔ Devuelve un número y nada más: aquí NO se asoma el costo, el proveedor ni el
    ROI. Justo por eso se puede publicar a un distribuidor."""
    doc = doc or {}
    if (doc.get('category') or '') in NO_DISCOUNT_CATEGORIES:
        return 0.0
    if not bool(doc.get('distributor_eligible', True)):
        return 0.0
    if es_hgh_neto(doc.get('id') or doc.get('sku'), doc.get('name')):
        return 0.0
    try:
        cap = float(doc.get('commission_cap', COMMISSION_CAP) or COMMISSION_CAP)
    except (TypeError, ValueError):
        cap = COMMISSION_CAP
    return max(0.0, min(COMMISSION_CAP, cap))


# ⛔ LO QUE NUNCA SALE DEL CATÁLOGO PÚBLICO (Christián, 2026-07-30).
#
# `commission_cap` y `distributor_eligible` viajaban en /api/products SIN sesión y
# en el catálogo de respaldo que baja cualquier visitante. No son el costo, pero
# dicen cuánto margen aguanta cada producto y cuáles no dejan 5x — información de
# la casa, publicada a cualquiera que abriera la consola del navegador.
#
# Se quedan en la base y en /admin/products. El cotizador del distribuidor los
# recibe recortados por su ruta autenticada; el checkout los calcula en el
# servidor y sigue siendo la verdad final.
CAMPOS_PRIVADOS_DE_PRODUCTO = {'commission_cap', 'distributor_eligible'}

# Techo del descuento que puede recibir un CLIENTE por la promo de la casa (15% por
# volumen). Lo que el catálogo público publica se recorta aquí: si un producto
# aguanta 25% o 40%, afuera se ve "15%" y nadie se entera de la diferencia.
TECHO_DESCUENTO_CLIENTE = 0.15


def vista_publica_de_producto(doc):
    """El producto tal como lo ve alguien SIN sesión.

    Dos cosas a la vez:
      · quita los campos de margen (`CAMPOS_PRIVADOS_DE_PRODUCTO`);
      · pone en su lugar lo ÚNICO que el carrito anónimo necesita para no mentirle
        al cliente sobre su total:
          - `descuentable: False` en lo que no lleva descuento (insumos, HGH neto,
            lo que no participa del canal). Eso el cliente lo ve igual en su
            carrito, así que no revela nada que no supiera;
          - `max_descuento_cliente` SÓLO cuando el tope real es menor que el techo
            de cliente. Va recortado a ese techo, así que jamás delata cuánto
            aguanta de verdad un producto — sólo que ahí el descuento llega hasta
            cierto punto.

    Con el catálogo de hoy ningún producto descuentable cae debajo del 15%, así
    que en la práctica sólo sale la bandera. El campo existe para el día que uno sí
    caiga: sin él, el carrito prometería un descuento que la caja no da."""
    fuera = {k: v for k, v in (doc or {}).items() if k not in CAMPOS_PRIVADOS_DE_PRODUCTO}
    tope = tope_de_descuento(doc)
    fuera['descuentable'] = tope > 0
    if 0 < tope < TECHO_DESCUENTO_CLIENTE:
        fuera['max_descuento_cliente'] = round(tope, 4)
    return fuera


# ENVIO (Christian, 2026-07-26). Mandar un paquete dentro de Mexico cuesta ~$250.
# Absorberlo en un pedido de \$879 se comia el 28% del ingreso. En vez de dejar de
# vender las presentaciones chicas — que son la puerta de entrada del cliente
# nuevo — el envio se regala solo a partir de cierto monto. Abajo de eso se cobra.
# El umbral es el mismo que el de las ofertas de carrito abandonado, a proposito.
def _pesos_de_entorno(nombre: str, por_omision: int) -> int:
    """Un número de envío que se puede mover sin desplegar código.

    Vive en el `.env` del servidor. Si viene vacío o con basura, manda el valor de
    aquí: un envío mal capturado en una variable de entorno no puede dejar el sitio
    cobrando $0 ni $99,000."""
    crudo = (os.environ.get(nombre) or '').strip()
    if not crudo:
        return por_omision
    try:
        valor = int(float(crudo))
    except (TypeError, ValueError):
        logger.warning('%s trae basura (%r): se usa el valor de fabrica %s',
                       nombre, crudo, por_omision)
        return por_omision
    if valor < 0:
        logger.warning('%s viene en negativo (%s): se usa el valor de fabrica %s',
                       nombre, valor, por_omision)
        return por_omision
    return valor


# LO QUE SE LE COBRA al pedido que NO llega a la compra minima. Es un PRECIO, no un
# costo. Configurable desde el .env del servidor (SHIPPING_FLAT) para que Christian
# pueda moverlo sin desplegar: el 2026-07-31 dijo «creo que Certified cobra $250 flat;
# si es cierto, nosotros debemos cobrar menos, quizas $200 o $219». Eso fue un QUIZAS,
# no una orden: hasta que el decida, se queda en $250.
SHIPPING_FLAT = _pesos_de_entorno('SHIPPING_FLAT', 250)

# LO QUE LA GUIA LE CUESTA A LA CASA cuando no hay cotizacion real de Skydropx. Es un
# COSTO, no un precio, y por eso se separo de `SHIPPING_FLAT` el 2026-07-31: contra
# este numero se mide el 5%, y mezclarlo con la tarifa que se cobra hacia que bajar el
# precio al cliente moviera solo —y en silencio— el punto donde el envio sale gratis.
COSTO_GUIA_ESTIMADO = _pesos_de_entorno('COSTO_GUIA_ESTIMADO', 250)

# El tope vive en UN solo lugar (`envios.py`), donde también vive la regla que lo
# usa contra el costo real de Skydropx. Escrito dos veces se desalinea en silencio,
# que es exactamente lo que pasó el 2026-07-27.
TOPE_ENVIO_SOBRE_COMPRA = envios.TOPE_ENVIO_SOBRE_COMPRA

# LA COMPRA MINIMA, EN PESOS. Antes se derivaba (`SHIPPING_FLAT / TOPE` = 250 / 10% =
# 2,500). Ya no: el 2026-07-31 el tope bajo al 5% y esa cuenta habria movido la minima
# sola de $2,500 a $5,000 sin que nadie lo pidiera. Christian la dicto en pesos —
# «el ticket supere los $2,500 de compra minima»— y en pesos se queda. Sigue amarrada
# al cupon de carrito abandonado (`recovery.MIN_FOR_OFFER`), a proposito.
FREE_SHIPPING_FROM = _pesos_de_entorno('FREE_SHIPPING_FROM', envios.COMPRA_MINIMA_ENVIO_GRATIS)


# ✅ SE COBRA ENVIO OTRA VEZ: $250 PAREJO (Christian, 2026-07-28, en sus palabras:
# "vamos a dejarlo con $250 parejo y pagamos un poco mas por envio express").
#
# El numero sale de la cotizacion REAL de Skydropx desde Playa del Carmen, que es de
# donde salen los paquetes. Las opciones de $51 tardan 7 u 8 dias y rompen la promesa
# de "2-5 dias" del sitio; las que si la cumplen andan en $139-$165. Cobrando $250
# alcanza para pagar la express y todavia queda margen.
#
# Y arriba de $2,500 se activa el beneficio, con el tope que vive en envios.py: la casa
# absorbe la guia hasta el 5% de la compra (era 10% hasta el 2026-07-31) y el cliente
# paga la diferencia. Con una guia de $250 eso quiere decir gratis-gratis desde $5,000,
# y entre $2,500 y $5,000 un cobro parcial que baja solo conforme sube el ticket.
#
# ⚠️ CONTRA LA COMPETENCIA, CON LO QUE DE VERDAD ESTA COMPROBADO (2026-07-31):
#   · Exoma: $200 fijos, gratis desde $2,000. VERIFICADO en exomapeptides.mx/envios.
#   · Certified: NO PUBLICA su costo de envio. Su shipping-policy y su FAQ no traen
#     ni un importe; solo aparece al llegar al checkout con producto en el carrito.
# Aqui decia "Certified cobra $250 SIEMPRE, sin excepcion". Esa linea NO tenia fuente
# y coincidia al peso con nuestro propio SHIPPING_FLAT — o sea que muy probablemente
# alguien escribio nuestro numero como si fuera de ellos. No se usa para decidir nada
# hasta que haya evidencia. Y bajarle a Exoma NO aplica: rige el trinquete (solo
# bajamos si baja Certified).
COBRAR_ENVIO = True


def shipping_for(merchandise_paid, costo_real=None):
    """Cuanto se le cobra de envio a un pedido, CUANDO se cobra.

    Se mide sobre lo que el cliente PAGA de mercancia (ya con descuento), no sobre
    el precio de lista: si no, un codigo grande dejaria el envio gratis cobrando
    mucho menos. Primero el ROI.

    ⛔ LA REGLA VIVE EN UN SOLO SITIO: `envios.cobro_de_envio_al_cliente`. Esta
    funcion solo le pone los tres numeros de la casa: lo que la guia CUESTA, la compra
    minima y lo que se COBRA de tarifa plana abajo de esa minima. Antes tenia su propia
    cuenta —"gratis arriba de $2,500"— que NUNCA miraba lo que la guia costaba de
    verdad: con un envio real de $500, un pedido de $2,600 salia gratis y la casa
    absorbia el 19%. Palabras de Christian: «si una compra por 2,500 genera un costo de
    envio de $500 ni en pedo lo pago».

    `costo_real` es lo que cuesta ESA guia (la cotizacion de Skydropx). Si no se pasa,
    se asume el costo estimado de la casa — que NO es lo mismo que la tarifa que se
    cobra, aunque hoy los dos valgan $250."""
    costo = COSTO_GUIA_ESTIMADO if costo_real is None else costo_real
    return envios.cobro_de_envio_al_cliente(costo, merchandise_paid, FREE_SHIPPING_FROM,
                                            tarifa_plana=SHIPPING_FLAT)


# ==========================================================================
#  ENVÍO POR SKYDROPX — cotizar en el checkout y comprar la guía al pagarse
# ==========================================================================
# ⛔ TODO ESTO NACE APAGADO (`envios.COTIZAR_EN_CHECKOUT` y
# `envios.COMPRAR_GUIA_AL_PAGAR`, los dos en False). Con ellos apagados el sitio se
# comporta EXACTAMENTE como antes: el checkout no cotiza, no cobra envío y nadie
# compra guías. Christian los prende cuando decida.
COLECCION_COTIZACIONES = 'shipping_quotes'


def envio_se_cotiza() -> bool:
    """¿Hoy el checkout cotiza y cobra envío real?

    Dos candados, y los dos tienen que ceder: el interruptor de Christian Y que la
    llave de Skydropx exista. Sin llave no se cotiza y no se rompe nada — el
    checkout sigue vendiendo igual (ver skydropx.py).
    """
    return bool(envios.COTIZAR_EN_CHECKOUT and skydropx.enabled())


def _mismo_cp(a: str, b: str) -> bool:
    return (a or '').strip()[:5] == (b or '').strip()[:5]


async def _guardar_cotizacion(cp: str, paquete: dict, opciones: list) -> dict:
    """Guarda la cotización que se le enseñó al cliente y le pone fecha de muerte.

    ⛔ ES LA PIEZA QUE HACE QUE EL PRECIO LO PONGA EL SERVIDOR. Al navegador solo
    se le devuelve un ID por opción; el PRECIO se queda aquí. Cuando el pedido
    llegue diciendo "elegí esta", el servidor va por el monto a este documento —
    nunca al cuerpo de la petición. Ya costó dinero creerle al navegador un precio
    (2026-07-27, se podía comprar un vial de $9,359 mandando precio 0).
    """
    ahora = datetime.now(timezone.utc)
    doc = {
        'id': str(uuid.uuid4()),
        'postal_code': (cp or '').strip(),
        'peso_kg': paquete.get('peso_kg'),
        'paquete': paquete,
        'created_at': ahora.isoformat(),
        'expires_at': (ahora + timedelta(minutes=envios.VIGENCIA_COTIZACION_MIN)).isoformat(),
        'opciones': [dict(o, opcion_id=str(uuid.uuid4())) for o in opciones],
    }
    await db[COLECCION_COTIZACIONES].insert_one(dict(doc))
    doc.pop('_id', None)
    return doc


async def _cotizacion_valida(opcion_id: str, cp: str, peso_kg: float):
    """La opción guardada que corresponde a ese ID, si TODAVÍA vale. Si no, None.

    Cuatro preguntas, y basta que una falle para tirarla:
      1. ¿Existe esa cotización? (un ID inventado no compra nada)
      2. ¿Sigue vigente? (30 min — una tarifa de hace un mes no es la de hoy)
      3. ¿Es para ESTE código postal? (cotizar a la esquina y mandar a Tijuana)
      4. ¿Es para ESTE peso? (cotizar un vial y despachar cuarenta)
    """
    if not opcion_id:
        return None
    doc = await db[COLECCION_COTIZACIONES].find_one(
        {'opciones.opcion_id': opcion_id}, {'_id': 0})
    if not doc:
        return None
    if (doc.get('expires_at') or '') < now_iso():
        return None
    if not _mismo_cp(doc.get('postal_code'), cp):
        return None
    if abs(float(doc.get('peso_kg') or 0) - float(peso_kg or 0)) > 0.01:
        return None
    opcion = next((o for o in doc.get('opciones', []) if o.get('opcion_id') == opcion_id), None)
    if not opcion or not skydropx.permitida(opcion.get('paqueteria', '')):
        return None            # una paquetería fuera de la lista no se cobra ni se compra
    return dict(opcion, peso_kg=doc.get('peso_kg'), paquete=doc.get('paquete') or {})


@api_router.post('/shipping/quote')
async def shipping_quote(payload: ShippingQuoteRequest):
    """El checkout pregunta cuánto cuesta mandar ESTE carrito a ESE código postal.

    Devuelve precios reales de Estafeta, por peso y CP. Y se degrada con elegancia:
    si el envío está apagado, si falta la llave o si Skydropx no contesta, responde
    `enabled: false` y el checkout se comporta como hoy. Nunca revienta la compra.
    """
    if not envio_se_cotiza():
        if envios.COTIZAR_EN_CHECKOUT and not skydropx.enabled():
            logger.info('Envio: no se cotiza porque faltan SKYDROPX_CLIENT_ID / '
                        'SKYDROPX_CLIENT_SECRET (se pegan en Admin → Cobros o en '
                        'el entorno).')
        return {'enabled': False, 'options': []}
    cp = (payload.postal_code or '').strip()
    if len(cp) < 5:
        return {'enabled': True, 'options': [], 'detail': 'Falta el código postal'}
    # El peso lo calcula el SERVIDOR contra el catálogo real. Lo que diga el
    # navegador del peso no se pregunta siquiera: no viaja en la petición.
    pflags = await _catalogo_de(payload.items)
    paquete = envios.paquete_del_pedido(payload.items, pflags)
    if not paquete['peso_kg']:
        return {'enabled': True, 'options': [], 'detail': 'El carrito está vacío'}
    try:
        # El estado y la ciudad viajan solo para llenar los campos de zona que la
        # API PRO exige. El precio lo decide el CP (comprobado en vivo), así que si
        # el checkout no los manda no cambia nada.
        opciones = skydropx.cotizar(cp, paquete, destino={
            'province': payload.state, 'city': payload.city, 'country': payload.country})
    except Exception:
        logger.exception('Skydropx: no se pudo cotizar a %s', cp)
        return {'enabled': False, 'options': [], 'detail': 'La paquetería no respondió'}
    if not opciones:
        return {'enabled': True, 'options': [], 'peso_kg': paquete['peso_kg'],
                'detail': 'Sin cobertura para ese código postal'}
    doc = await _guardar_cotizacion(cp, paquete, opciones)
    return {
        'enabled': True,
        'peso_kg': paquete['peso_kg'],
        'expires_at': doc['expires_at'],
        'free_shipping_from': FREE_SHIPPING_FROM,
        # Al navegador se le da el precio para ENSEÑARLO. El que se cobra sale del
        # documento guardado, no de esta respuesta cuando vuelva.
        'options': [{'id': o['opcion_id'], 'carrier': o['paqueteria'],
                     'service': o['servicio'], 'days': o['dias'],
                     'price': o['precio']} for o in doc['opciones']],
    }


async def _envio_del_pedido(payload, paid_merchandise, pflags):
    """Cuánto se le cobra de envío a este pedido, y con qué cotización.

    ⛔ EL PRECIO LO PONE EL SERVIDOR. El monto de envío que venga en la petición se
    ignora por completo, igual que se ignoran los precios de los productos: sale de
    la cotización que el propio servidor guardó, y solo si sigue siendo válida para
    este CP y este peso. Si no lo es, se vuelve a cotizar aquí mismo.

    Devuelve (lo que paga el cliente, lo que se guarda en el pedido).
    """
    if not envio_se_cotiza():
        # El camino de siempre: la tarifa plana dormida detrás de COBRAR_ENVIO. La
        # línea se escribe TAL CUAL porque hay pruebas que la buscan literal — es el
        # candado que impide que alguien vuelva a dejar el envío sin interruptor.
        shipping = shipping_for(paid_merchandise) if COBRAR_ENVIO else 0
        return shipping, {}
    cp = (payload.customer.postal_code or '').strip()
    paquete = envios.paquete_del_pedido(payload.items, pflags)
    opcion = await _cotizacion_valida(payload.shipping_quote_id, cp, paquete['peso_kg'])
    if not opcion:
        # Cotización vencida, ausente, de otro CP o de otro peso: se cotiza de nuevo
        # AQUÍ, con el carrito de verdad. Se toma la más barata de las permitidas.
        try:
            frescas = skydropx.cotizar(cp, paquete, destino={
                'province': getattr(payload.customer, 'state', '') or '',
                'city': getattr(payload.customer, 'city', '') or '',
                'country': getattr(payload.customer, 'country', 'MX') or 'MX'})
        except Exception:
            logger.exception('Skydropx: no se pudo recotizar el pedido a %s', cp)
            frescas = []
        if not frescas:
            return 0, {}                # sin cotización no se inventa un cargo
        doc = await _guardar_cotizacion(cp, paquete, frescas)
        opcion = dict(doc['opciones'][0], peso_kg=paquete['peso_kg'], paquete=paquete)
    costo = float(opcion.get('precio') or 0)
    cobrado = envios.cobro_de_envio_al_cliente(costo, paid_merchandise, FREE_SHIPPING_FROM)
    guardado = {
        'carrier': opcion.get('paqueteria', ''),
        'service': opcion.get('servicio', ''),
        'service_code': opcion.get('servicio_codigo', ''),
        'days': opcion.get('dias', 0),
        'cost': round(costo, 2),
        'charged': cobrado,
        'peso_kg': opcion.get('peso_kg'),
        'paquete': opcion.get('paquete') or paquete,
        'postal_code': cp,
        'quoted_at': now_iso(),
    }
    return cobrado, guardado


# ==========================================================================
#  AJUSTES DE ENVÍO QUE SE CAPTURAN EN EL PANEL (remitente y cajas)
# ==========================================================================
# ⛔ POR QUÉ NO ESTÁN EN EL CÓDIGO. El remitente es el domicilio de un TRABAJADOR:
# escribirlo en el repositorio sería publicar los datos personales de alguien en
# GitHub. Y las cajas cambian cuando cambia el proveedor de empaque, sin que eso
# tenga por qué costar un despliegue.
#
# EL ENTORNO SIGUE MANDANDO. Si la variable existe en el `.env` del servidor, gana
# sobre lo que haya aquí — misma regla que las llaves de cobro (ver secretos.py).
COLECCION_AJUSTES_ENVIO = 'shipping_settings'


async def _cargar_ajustes_envio() -> dict:
    """Mete en memoria el remitente y las cajas que capturó el admin.

    Se llama al arrancar y cada vez que se guardan. `skydropx` y `envios` son
    módulos síncronos y Mongo es asíncrono: por eso el valor se les EMPUJA en vez de
    que ellos vayan a buscarlo.
    """
    doc = await db[COLECCION_AJUSTES_ENVIO].find_one({'id': 'envios'}, {'_id': 0}) or {}
    skydropx.cargar_remitente_del_panel(doc.get('remitente') or {})
    envios.cargar_cajas_del_panel(doc.get('cajas') or [])
    return doc


@api_router.get('/admin/envios/config')
async def admin_envios_config(admin=Depends(get_current_admin)):
    """Lo que el panel necesita para la pestaña de Envíos.

    A diferencia de las llaves de cobro, el remitente SÍ se devuelve completo: no es
    un secreto, es una dirección que el admin tiene que poder revisar antes de que
    salga impresa en una guía. Las credenciales de Skydropx siguen sin devolverse
    nunca — de ellas solo se dice si están puestas.
    """
    doc = await db[COLECCION_AJUSTES_ENVIO].find_one({'id': 'envios'}, {'_id': 0}) or {}
    return {
        'credenciales_puestas': skydropx.enabled(),
        # Los DOS cotizadores y cuál está encendido. Las llaves nunca se devuelven:
        # de ellas sólo se dice si están puestas (ver secretos.py).
        'proveedores': paqueterias.encendidos(),
        'remitente': skydropx.remitente(),
        'remitente_completo': skydropx.remitente_configurado(),
        'remitente_origen': skydropx.origen_del_remitente(doc.get('remitente')),
        'campos_remitente': [c for c, _ in skydropx.CAMPOS_REMITENTE],
        'cajas': [dict(c, peso_volumetrico_kg=envios.peso_volumetrico(c))
                  for c in envios.cajas()],
        'cajas_de_fabrica': not bool(doc.get('cajas')),
        'cotiza_en_checkout': envios.COTIZAR_EN_CHECKOUT,
        'compra_guia_al_pagar': envios.COMPRAR_GUIA_AL_PAGAR,
    }


@api_router.post('/admin/orders/{order_id}/rescatar-etiqueta')
async def admin_rescatar_etiqueta(order_id: str, admin=Depends(get_current_admin)):
    """Vuelve a pedir el PDF de una guía YA COMPRADA. No compra nada ni cobra de nuevo.

    ⛔ POR QUÉ HACE FALTA (primera compra real, 2026-07-31): la paquetería contestó al
    instante con el número de rastreo y el `label_url` VACÍO — el PDF se genera unos
    segundos más tarde. El pedido quedó con guía pagada y sin papel que pegarle al
    paquete. Esto lo rescata buscando por número de rastreo.
    """
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    numero = (order.get('tracking_number') or '').strip()
    if not numero:
        raise HTTPException(status_code=400, detail='Ese pedido todavía no tiene guía')
    if order.get('label_url'):
        return {'label_url': order['label_url'], 'ya_estaba': True}
    mod = paqueterias.modulo(order.get('label_provider') or 'skydropx')
    if mod is None or not mod.enabled():
        raise HTTPException(status_code=400, detail='Ese proveedor no tiene credenciales')
    guia = mod.etiqueta_por_rastreo(numero)
    url = (guia or {}).get('label_url') or ''
    if not url:
        raise HTTPException(status_code=404,
                            detail='La paquetería todavía no publica el PDF de esa guía')
    await db.orders.update_one({'id': order_id}, {'$set': {'label_url': url}})
    return {'label_url': url, 'ya_estaba': False}


@api_router.get('/admin/envios/saldo')
async def admin_saldo_paqueterias(admin=Depends(get_current_admin)):
    """Cuánto dinero queda en cada cuenta de paquetería para comprar guías.

    ⛔ EXISTE POR UN SUSTO REAL (2026-07-31). La primera compra de verdad rebotó con «No
    tienes los créditos suficientes» — con el pedido YA PAGADO y la clienta esperando.
    Cotizar es gratis y siempre funcionó, así que nada avisaba de que la cuenta estaba en
    ceros hasta el segundo exacto de comprar. Esto se mira ANTES de despachar.
    """
    return {'proveedores': [
        {'clave': c, 'nombre': n, **mod.saldo()}
        for c, n, mod in paqueterias.PROVEEDORES]}


@api_router.get('/admin/envios/costo-real')
async def admin_costo_real_envio(csv: int = 0, admin=Depends(get_current_admin)):
    """Lo que de VERDAD han costado las guías. Es lo que alimenta el piso de 5× del ROI.

    ⛔ POR QUÉ EXISTE. El motor de precios medía el piso de 5× restando $250 de envío,
    que NO es lo que cuesta una guía: son la tarifa plana de la política de cobro al
    cliente. Una guía real anda en $139–$165 (medido en vivo el 2026-07-30). Con esta
    ruta el motor deja de suponer: se exporta a `pricing-system/datos/envios_reales.csv`
    y `actualizar_costo_envio.py` reescribe la regla con el dato de verdad.

    ⛔ SÓLO ADMIN, y con razón: estos números son costos de la casa. Un costo en una ruta
    pública es el margen del negocio publicado. Del cliente aquí no sale ni el nombre —
    sólo pedido, ruta, peso y lo que costó la guía.
    """
    # Las guías REALMENTE compradas: un pedido con número de guía y un costo escrito.
    pedidos = await db.orders.find(
        {'tracking_number': {'$ne': ''}, 'shipping_cost': {'$gt': 0}},
        {'_id': 0, 'order_number': 1, 'carrier': 1, 'shipping_service': 1,
         'shipping_cost': 1, 'label_provider': 1, 'shipped_at': 1,
         'shipping_quote': 1, 'customer': 1}).to_list(5000)
    filas = []
    for p in pedidos:
        q = p.get('shipping_quote') or {}
        filas.append({
            'fecha': (p.get('shipped_at') or '')[:10],
            'pedido': p.get('order_number') or '',
            'proveedor': p.get('label_provider') or 'skydropx',
            'paqueteria': p.get('carrier') or '',
            'servicio': p.get('shipping_service') or q.get('service') or '',
            'cp_origen': skydropx.cp_origen(),
            'cp_destino': ((p.get('customer') or {}).get('postal_code') or ''),
            'peso_kg': q.get('peso_kg') or (q.get('paquete') or {}).get('peso_kg') or '',
            'costo_mxn': round(float(p.get('shipping_cost') or 0), 2),
            'fuente': 'guia comprada',
        })
    costos = sorted(f['costo_mxn'] for f in filas)
    resumen = {
        'guias': len(costos),
        'min_mxn': costos[0] if costos else None,
        'max_mxn': costos[-1] if costos else None,
        'promedio_mxn': round(sum(costos) / len(costos), 2) if costos else None,
    }
    if not csv:
        return {'resumen': resumen, 'guias': filas}
    # El CSV con las columnas EXACTAS que espera `pricing-system/datos/envios_reales.csv`.
    columnas = ['fecha', 'pedido', 'proveedor', 'paqueteria', 'servicio', 'cp_origen',
                'cp_destino', 'peso_kg', 'costo_mxn', 'fuente']
    buf = io.StringIO()
    w = csv_mod.DictWriter(buf, fieldnames=columnas)
    w.writeheader()
    w.writerows(filas)
    return PlainTextResponse(buf.getvalue(), media_type='text/csv')


@api_router.put('/admin/envios/remitente')
async def admin_guardar_remitente(payload: RemitenteUpdate, admin=Depends(get_current_admin)):
    """Captura la dirección de quien despacha. Es lo que se imprime en la guía."""
    datos = {c: (getattr(payload, c, '') or '').strip()
             for c, _env in skydropx.CAMPOS_REMITENTE}
    await db[COLECCION_AJUSTES_ENVIO].update_one(
        {'id': 'envios'},
        {'$set': {'id': 'envios', 'remitente': datos, 'updated_at': now_iso()}},
        upsert=True)
    await _cargar_ajustes_envio()
    return {'remitente': skydropx.remitente(),
            'remitente_completo': skydropx.remitente_configurado()}


@api_router.put('/admin/envios/cajas')
async def admin_guardar_cajas(payload: CajasUpdate, admin=Depends(get_current_admin)):
    """Las cajas con las que se cotiza. Menos volumen = envío más barato."""
    cajas = [c.model_dump() for c in (payload.cajas or [])]
    if not envios.cargar_cajas_del_panel(cajas):
        # Una lista de cajas inválida dejaría el sitio cotizando contra nada.
        raise HTTPException(status_code=400, detail='Ninguna caja tiene medidas válidas')
    await db[COLECCION_AJUSTES_ENVIO].update_one(
        {'id': 'envios'},
        {'$set': {'id': 'envios', 'cajas': cajas, 'updated_at': now_iso()}},
        upsert=True)
    await _cargar_ajustes_envio()
    return {'cajas': [dict(c, peso_volumetrico_kg=envios.peso_volumetrico(c))
                      for c in envios.cajas()]}


# ==========================================================================
#  DESPACHAR UN PEDIDO: cotizar de verdad y comprar la guía con un clic
# ==========================================================================
# Esto es lo que faltaba. Hasta hoy la única forma de mandar un paquete era ir al
# mostrador de la paquetería y pagar lo que dijeran — el 2026-07-30 eso costó casi
# $600 por dos viales a Nuevo León. Cotizar aquí enseña lo que cuesta de verdad por
# peso y código postal, con TODAS las paqueterías que Skydropx alcance.
#
# ⛔ ESTO NO CAMBIA UN PESO DE LO QUE PAGA EL CLIENTE. El cliente sigue con sus $250
# parejos y su envío gratis arriba de $2,500 con tope del 5% (`COBRAR_ENVIO` /
# `envios.cobro_de_envio_al_cliente`). Aquí solo se decide qué le cuesta A LA CASA.
def _destino_del_pedido(order: dict) -> dict:
    c = (order or {}).get('customer') or {}
    return {
        'name': c.get('full_name', ''), 'company': '',
        'address1': c.get('address', ''), 'address2': c.get('address_2', ''),
        'city': c.get('city', ''), 'province': c.get('state', ''),
        'colonia': c.get('colonia', '') or c.get('neighborhood', ''),
        'zip': c.get('postal_code', ''), 'country': c.get('country', 'MX') or 'MX',
        'phone': c.get('phone', ''), 'email': c.get('email', ''),
        'reference': c.get('notes', ''), 'contents': 'Insumos de laboratorio',
    }


async def _paquete_real_del_pedido(order: dict) -> dict:
    """El bulto de ESTE pedido, pesado contra el catálogo de verdad.

    Se resuelve el catálogo en vez de confiar en lo que quedó guardado: un pedido
    creado antes de que existieran los pesos traería medidas viejas, y despachar con
    medidas viejas es cotizar barato y pagar caro en el mostrador.
    """
    items = order.get('items') or []
    pflags = await _catalogo_de(items)
    return envios.paquete_del_pedido(items, pflags)


@api_router.post('/admin/orders/{order_id}/cotizar-envio')
async def admin_cotizar_envio(order_id: str, admin=Depends(get_current_admin)):
    """Cuánto cuesta mandar ESTE pedido, de verdad, en CADA proveedor y por paquetería.

    ⛔ DOBLE COTIZADOR (Christián, 2026-07-31). Se pregunta en Skydropx Y en
    enviosinternacionales.com, se juntan todas las tarifas y se ordenan por precio: la
    casa contrata la más barata, venga de quien venga. Si uno de los dos está apagado o
    no contesta, se sigue con el otro exactamente como hasta hoy.

    Devuelve TODAS las tarifas con precio y días —no solo las que ve el cliente— porque
    quien paga la guía es la casa y tiene derecho a ver la más barata aunque tarde más.
    Guarda la cotización para que comprar sea un clic.
    """
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if not paqueterias.cuantos_activos():
        return {'enabled': False, 'options': [],
                'proveedores': paqueterias.encendidos(),
                'detail': 'Faltan SKYDROPX_CLIENT_ID y SKYDROPX_CLIENT_SECRET'}
    destino = _destino_del_pedido(order)
    cp = (destino.get('zip') or '').strip()
    if len(cp) < 5:
        return {'enabled': True, 'options': [], 'detail': 'El pedido no trae código postal'}
    paquete = await _paquete_real_del_pedido(order)
    if not paquete['peso_kg']:
        return {'enabled': True, 'options': [], 'detail': 'El pedido no trae artículos'}
    comp = paqueterias.cotizar_en_todos(destino, paquete,
                                        espera_max=skydropx.ESPERA_MAX_GUIA_S)
    if not comp['opciones']:
        # Nadie dio tarifa: se dice de cada proveedor por qué, en vez de un "no se pudo".
        motivos = '; '.join(f"{p['nombre']}: {p['detalle']}"
                            for p in comp['proveedores'] if p.get('detalle'))
        return {'enabled': True, 'options': [], 'paquete': paquete,
                'proveedores': comp['proveedores'],
                'detail': f'Sin tarifas para ese código postal. {motivos}'[:300]}
    doc = await _guardar_cotizacion(cp, paquete, comp['opciones'])
    # Los ids de cotización de CADA proveedor se guardan con la nuestra: la guía se
    # compra contra el `rate_id` de ESA cotización y con ESE proveedor, no con el otro.
    cots = comp.get('cotizaciones') or {}
    await db[COLECCION_COTIZACIONES].update_one(
        {'id': doc['id']},
        {'$set': {'order_id': order_id,
                  'skydropx_id': (cots.get('skydropx') or {}).get('id', ''),
                  'cotizaciones': cots,
                  'packages': (cots.get('skydropx') or {}).get('packages') or []}})
    recomendada = min(comp['opciones'], key=lambda o: o['precio'])
    ahorro = paqueterias.ahorro(comp)
    if ahorro['comparados'] > 1:
        logger.info('Envio: doble cotizacion de %s — gana %s, se ahorran $%s',
                    order.get('order_number'), ahorro['gana'], ahorro['ahorro_mxn'])
    return {
        'enabled': True,
        'quote_id': doc['id'],
        'paquete': paquete,
        'expires_at': doc['expires_at'],
        'remitente_completo': skydropx.remitente_configurado(),
        'requiere_verificar_origen': any(
            (c or {}).get('requiere_verificar_origen') for c in cots.values()),
        'recomendada': recomendada['rate_id'],
        # La comparación entre proveedores: cuántas tarifas dio cada uno, su mejor
        # precio, y cuánto se ahorra por haber preguntado en dos lados.
        'proveedores': comp['proveedores'],
        'ahorro': ahorro,
        'options': [{'id': o['opcion_id'], 'carrier': o['paqueteria'],
                     'service': o['servicio'], 'days': o['dias'], 'price': o['precio'],
                     'provider': o.get('proveedor', 'skydropx'),
                     'provider_name': o.get('proveedor_nombre', 'Skydropx'),
                     'para_el_cliente': skydropx.permitida(o['paqueteria_id'])
                                        and skydropx.dentro_del_plazo(o['dias'])}
                    for o in doc['opciones']],
    }


@api_router.post('/admin/orders/{order_id}/guia')
async def admin_comprar_guia(order_id: str, payload: ComprarGuiaRequest,
                             admin=Depends(get_current_admin)):
    """Compra la guía de la opción que eligió el admin y la deja en el pedido.

    ⚠️ CUESTA DINERO DE VERDAD. Por eso pide explícitamente qué opción, no adivina.

    ⛔ El precio y la tarifa salen de la cotización que guardó el SERVIDOR; lo que
    mande el navegador no se usa para nada más que decir cuál de ellas.
    """
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if order.get('tracking_number'):
        raise HTTPException(status_code=409, detail='Este pedido ya tiene guía')
    if not paqueterias.cuantos_activos():
        raise HTTPException(status_code=400, detail='Faltan las credenciales de Skydropx')
    if not skydropx.remitente_configurado():
        raise HTTPException(status_code=400,
                            detail='Falta capturar el remitente en Admin → Envíos')
    doc = await db[COLECCION_COTIZACIONES].find_one(
        {'opciones.opcion_id': payload.option_id}, {'_id': 0})
    if not doc or doc.get('order_id') != order_id:
        raise HTTPException(status_code=400, detail='Esa cotización no es de este pedido')
    if (doc.get('expires_at') or '') < now_iso():
        raise HTTPException(status_code=400, detail='La cotización venció; vuelve a cotizar')
    opcion = next((o for o in doc.get('opciones', []) if o.get('opcion_id') == payload.option_id), None)
    if not opcion:
        raise HTTPException(status_code=400, detail='Esa opción no existe')
    numero_paquete = 1
    paquetes = doc.get('packages') or []
    if paquetes and isinstance(paquetes[0], dict):
        try:
            numero_paquete = int(paquetes[0].get('package_number') or 1)
        except (TypeError, ValueError):
            numero_paquete = 1
    # ⛔ CON EL PROVEEDOR QUE LA COTIZÓ. Un `rate_id` sólo vale en la casa que lo emitió:
    # mandarlo al otro proveedor es un 404 en el mejor caso y una guía mal comprada en el
    # peor. La etiqueta viaja pegada a la opción desde que se cotizó (ver paqueterias.py).
    try:
        guia = paqueterias.comprar_guia(opcion, _destino_del_pedido(order),
                                        doc.get('paquete') or {}, numero_paquete)
    except Exception as e:
        logger.exception('Envio: no se pudo comprar la guia de %s', order.get('order_number'))
        await db.orders.update_one({'id': order_id}, {'$set': {'label_error': str(e)[:300]}})
        raise HTTPException(status_code=502, detail=f'La paquetería no dio la guía: {e}'[:200])
    numero = guia.get('tracking_number') or ''
    carrier = opcion.get('paqueteria') or ''
    update = {
        'carrier': carrier,
        'tracking_number': numero,
        'tracking_url': guia.get('tracking_url') or build_tracking_url(carrier, numero),
        'label_url': guia.get('label_url') or '',
        # Con quién se compró: es lo que después permite reclamarle a la casa correcta.
        'label_provider': guia.get('proveedor') or opcion.get('proveedor') or 'skydropx',
        'label_error': '',
        'shipping_cost': opcion.get('precio') or 0,
        'shipping_service': opcion.get('servicio') or '',
        'shipped_at': order.get('shipped_at') or now_iso(),
        'status': 'enviado',
    }
    await db.orders.update_one({'id': order_id}, {'$set': update})
    logger.info('Envio: guia comprada a mano para %s — %s %s por $%s (via %s)',
                order.get('order_number'), carrier, numero, update['shipping_cost'],
                update['label_provider'])
    fresco = await db.orders.find_one({'id': order_id}, {'_id': 0})
    await avisar_del_envio(fresco)
    return fresco


async def avisar_del_envio(order: dict) -> bool:
    """Le manda al cliente su número de guía. Nunca revienta hacia arriba.

    Hasta hoy el rastreo se guardaba en el pedido y ahí se quedaba: el cliente tenía
    que entrar a su cuenta a buscarlo. El correo de confirmación le PROMETE que se
    lo vamos a mandar ("en cuanto salga te mandamos el número de guía"), así que no
    mandarlo era incumplir por escrito.
    """
    if not order or not order.get('tracking_number'):
        return False
    lang = None
    if order.get('user_id'):
        u = await db.users.find_one({'id': order['user_id']}, {'_id': 0, 'language': 1})
        lang = (u or {}).get('language')
    num = order.get('order_number')
    await notify(order.get('user_id'), 'order_shipped', 'Tu pedido va en camino',
                 f'El pedido {num} ya salió. Guía {order.get("tracking_number")}.',
                 link=f'/pedido/{num}', dedup=f'shipped:{num}')
    asyncio.create_task(send_shipped_email(order, lang))
    return True


async def comprar_guia_del_pedido(order: dict) -> dict | None:
    """Compra la guía de un pedido YA PAGADO y la deja en el pedido. Idempotente.

    La llaman los cuatro caminos del dinero: tarjeta y OXXO (Mercado Pago), cripto
    (NOWPayments/BTCPay) y SPEI (cuando el admin confirma el depósito). Todos pasan
    por aquí porque todos terminan en el mismo lugar: el pedido en 'confirmado'.

    Nunca revienta hacia arriba: un pedido pagado no se puede quedar a medias
    porque la paquetería tenga un mal día. Si falla, lo deja escrito en el pedido
    (`label_error`) y en la bitácora, y el admin compra la guía a mano como hoy.
    """
    if not envios.COMPRAR_GUIA_AL_PAGAR:
        return None
    if not order or order.get('tracking_number'):
        return None                     # ya tiene guía: no se compra dos veces
    if not paqueterias.cuantos_activos():
        logger.info('Envio: no se compra guia de %s porque ningun proveedor de '
                    'paqueteria tiene credenciales (SKYDROPX_CLIENT_ID / '
                    'SKYDROPX_CLIENT_SECRET, o los de enviosinternacionales)',
                    order.get('order_number'))
        return None
    if not skydropx.remitente_configurado():
        # ⛔ A PROPÓSITO. Comprar con un remitente inventado es pagar una recolección
        # en una dirección que no existe. Ver skydropx.py.
        logger.error('Envio: NO se compra guia de %s — falta la direccion del '
                     'remitente (SKYDROPX_FROM_*). ⚠️ PENDIENTE de Christian.',
                     order.get('order_number'))
        await db.orders.update_one({'id': order['id']},
                                   {'$set': {'label_error': 'Falta configurar el remitente'}})
        return None
    c = order.get('customer') or {}
    destino = {
        'name': c.get('full_name', ''), 'company': '',
        'address1': c.get('address', ''), 'address2': c.get('address_2', ''),
        'city': c.get('city', ''), 'province': c.get('state', ''),
        'zip': c.get('postal_code', ''), 'country': c.get('country', 'MX') or 'MX',
        'phone': c.get('phone', ''), 'email': c.get('email', ''),
        'reference': c.get('notes', ''), 'contents': 'Insumos de laboratorio',
    }
    quote = order.get('shipping_quote') or {}
    paquete = quote.get('paquete') or envios.paquete_del_pedido(order.get('items') or [], {})
    try:
        # Doble cotizador: pregunta en Skydropx y en enviosinternacionales.com y compra
        # la más barata de las permitidas. Con uno solo encendido se comporta como antes.
        guia = paqueterias.guia_para(destino, paquete, quote.get('service_code', ''))
    except Exception as e:
        logger.exception('Envio: no se pudo comprar la guia de %s', order.get('order_number'))
        await db.orders.update_one({'id': order['id']}, {'$set': {'label_error': str(e)[:300]}})
        return None
    numero = guia.get('tracking_number') or ''
    update = {
        'carrier': guia.get('carrier') or 'Estafeta',
        'tracking_number': numero,
        'tracking_url': guia.get('tracking_url') or build_tracking_url(guia.get('carrier', ''), numero),
        'label_url': guia.get('label_url') or '',
        'label_provider': guia.get('proveedor') or 'skydropx',
        'label_error': '',
        'shipping_cost': guia.get('costo') or quote.get('cost') or 0,
        'shipped_at': order.get('shipped_at') or now_iso(),
        'status': 'enviado',
    }
    await db.orders.update_one({'id': order['id']}, {'$set': update})
    logger.info('Envio: guia comprada para %s — %s %s (via %s)',
                order.get('order_number'), update['carrier'], numero,
                update['label_provider'])
    # El cliente se entera por correo, no entrando a buscar. Se le manda el pedido ya
    # actualizado: con el de antes iría sin número de guía, que es justo lo que avisa.
    await avisar_del_envio(dict(order, **update))
    return update


def buyer_own_rate(user):
    """Descuento PROPIO de quien compra, sin necesidad de código (Christian, 2026-07-25).

    - Distribuidor comprando para sí mismo: su comisión máxima (Alanís 40% → paga 60%).
      Ese descuento ES su comisión, cobrada por adelantado: no gana comisión encima.
    - Cliente con trato especial: el `personal_discount_rate` que le puso el admin
      (el caso de Paz Cambray, 40% aunque sea solo cliente).

    Siempre se recorta al tope de CADA producto (el ROI manda) y los insumos quedan
    fuera. Es un piso, no un techo: si trae un código mejor, gana el mayor.

    ⛔ TOPADO AL 40% (`techo_de_descuento`), salvo Christián y María. El trato especial
    admitía hasta 50%: era la tercera puerta arriba del techo que aparecía en dos días.
    Se topa AQUÍ, al cobrar, y no sólo al guardarlo, para que los tratos que ya están
    puestos por encima valgan 40% sin tener que tocarle la cuenta a nadie."""
    if not user:
        return 0.0
    if user.get('role') == 'distributor':
        return pyramid.effective_rate(user)
    try:
        return max(0.0, min(techo_de_descuento(user),
                            float(user.get('personal_discount_rate') or 0)))
    except (TypeError, ValueError):
        return 0.0

# ----------------- Lealtad (puntos) -----------------
async def _points_entry(user_id, order, kind, points):
    await db.points.insert_one({
        'id': str(uuid.uuid4()), 'user_id': user_id, 'order_id': order['id'],
        'order_number': order.get('order_number', ''), 'type': kind,
        'points': int(points), 'created_at': now_iso(),
    })


async def award_order_points(order):
    """Deposita los puntos de una orden pagada. Idempotente: el flag
    points_awarded se toma con una sola actualizacion condicional.

    ⛔ SIN COBRAR NO HAY PUNTOS. Los que llaman aquí miraban sólo el `status`, así que
    un pedido ENTREGADO Y FIADO regalaba puntos —dinero de la casa— por una venta que
    todavía no se ha cobrado. El candado va aquí dentro, en el único lugar por donde
    pasan todos los caminos (checkout, pasarela, venta directa, cambio de estado).
    Cuando el pago se marque después, este mismo camino los deposita."""
    if not order.get('user_id') or int(order.get('points_earned', 0) or 0) <= 0:
        return
    if not esta_pagado(order):
        return
    res = await db.orders.update_one(
        {'id': order['id'], 'points_awarded': {'$ne': True}},
        {'$set': {'points_awarded': True}},
    )
    if res.modified_count == 0:
        return
    await db.users.update_one({'id': order['user_id']}, {'$inc': {'points_balance': int(order['points_earned'])}})
    await _points_entry(order['user_id'], order, 'earn', order['points_earned'])


async def _adoptar_pedidos_de_invitado(user_id: str) -> int:
    """Pasa a esta cuenta los pedidos que la persona hizo COMO INVITADO.

    Sin esto, quien compra sin cuenta y se registra después no ve nada de lo que ya
    compró: ni en su historial, ni en sus puntos, ni para desbloquear herramientas.

    ⛔ EL CANDADO: solo se adopta si la cuenta YA CONFIRMÓ ese correo. Adoptar por
    correo a secas sería regalarle el historial de compras de cualquiera —su nombre,
    su teléfono, su dirección y qué péptidos compró— al primero que se registre
    tecleando el correo de otro. La confirmación es la única prueba que tenemos de
    que ese buzón es suyo, así que esta función se llama SIEMPRE en el momento en
    que el correo queda confirmado, nunca en el de registrarse.

    Un pedido que YA tiene dueño no cambia de dueño jamás: la búsqueda exige
    `user_id` nulo, y la toma se hace con esa misma condición dentro del update
    para que dos confirmaciones a la vez no se lo peleen.

    Idempotente por construcción: al adoptarlo el pedido deja de ser huérfano, así
    que la segunda corrida no encuentra nada. Los puntos los deposita
    `award_order_points`, que ya tiene su propio candado (`points_awarded`).
    """
    user = await db.users.find_one({'id': user_id}, {'_id': 0, 'password_hash': 0})
    email = ((user or {}).get('email') or '').strip()
    # `is not True` a propósito: las cuentas viejas sin el campo NO adoptan nada.
    # Aquí, a diferencia del login, la duda se resuelve del lado seguro.
    if not email or (user or {}).get('email_verified') is not True:
        return 0
    # Sin distinguir mayúsculas: el pedido guarda el correo tal como lo tecleó el
    # invitado, y "Ana@X.com" es la misma persona que "ana@x.com".
    huerfanos = await db.orders.find(
        {'customer.email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'},
         'user_id': None},
        {'_id': 0}).to_list(500)
    adoptados = 0
    for o in huerfanos:
        tomado = await db.orders.update_one(
            {'id': o['id'], 'user_id': None},
            {'$set': {'user_id': user_id, 'adopted_at': now_iso(),
                      'adopted_from_guest': True}})
        if tomado.modified_count == 0:
            continue                     # se lo llevó otra corrida: no es nuestro
        adoptados += 1
        # Los puntos se calculan AHORA, con las reglas de siempre: cuando se creó el
        # pedido no había cuenta a la que abonar, así que nació en cero. Solo los
        # pedidos PAGADOS los generan, igual que en una compra normal.
        if o.get('status') in loyalty.PAID_STATUSES and not o.get('points_awarded'):
            mercancia = float(o.get('total', 0) or 0) - float(o.get('shipping', 0) or 0)
            ganados = loyalty.earn(mercancia, loyalty.eligible(user), o.get('discount_rate', 0))
            if ganados > 0:
                await db.orders.update_one({'id': o['id']}, {'$set': {'points_earned': ganados}})
                fresco = await db.orders.find_one({'id': o['id']}, {'_id': 0})
                await award_order_points(fresco)
    await _heredar_referido_de_pedidos(user, huerfanos)
    return adoptados


async def _heredar_referido_de_pedidos(user, pedidos):
    """Si compró con el código de un distribuidor SIN cuenta, al registrarse queda ligado.

    ⛔ QUIEN USA EL CÓDIGO ES SU CLIENTE (Christián, 2026-07-30). Los pedidos ya traían el
    `referred_by` y la comisión ya estaba pagada, pero la cuenta nueva nacía huérfana: la
    relación se perdía justo cuando la persona por fin se registraba, y el distribuidor la
    veía desaparecer de su lista. Se toma el del pedido MÁS RECIENTE si hubiera varios.

    NO pisa un referido propio: si la cuenta ya viene ligada a alguien —porque se registró
    con el código de otro— esa decisión es del cliente y manda sobre el historial."""
    if not user or user.get('referred_by'):
        return
    con_codigo = sorted((o for o in pedidos if o.get('referred_by')),
                        key=lambda o: o.get('created_at', ''))
    if not con_codigo:
        return
    dist_id = con_codigo[-1]['referred_by']
    # Condicionado dentro del update: si otra corrida ya lo ligó, no se toca.
    await db.users.update_one(
        {'id': user['id'], '$or': [{'referred_by': None}, {'referred_by': {'$exists': False}}]},
        {'$set': {'referred_by': dist_id, 'referred_from_guest_at': now_iso()}})


async def _recordar_datos_de_compra(user, customer):
    """Guarda en el perfil los datos con los que este cliente acaba de comprar, para
    no volver a pedírselos la próxima vez (Christian, 2026-07-28).

    Solo dirección y teléfono. El NOMBRE de la cuenta NO se toca: en el checkout se
    escribe el de quien RECIBE el paquete —un regalo, la oficina, un familiar— y con
    eso no se le puede cambiar el nombre a la cuenta de nadie.
    """
    if not user:
        return                      # invitado: no hay dónde guardarlo, y así se queda
    update = {'shipping_address': {
        'address': customer.address, 'address_2': customer.address_2,
        'city': customer.city, 'state': customer.state,
        'postal_code': customer.postal_code, 'country': customer.country,
    }}
    if (customer.phone or '').strip():
        update['phone'] = customer.phone.strip()
    await db.users.update_one({'id': user['id']}, {'$set': update})


@api_router.get('/me/checkout')
async def my_checkout_data(user=Depends(get_current_user)):
    """Los datos con los que el checkout se pinta ya lleno para quien tiene sesión.

    Va por sesión y SOLO por sesión: no recibe correo, ni id, ni SKU. Una ruta que
    devuelva la dirección de alguien a partir de un dato que se puede teclear es una
    lista de clientes servida en bandeja.
    """
    fresh = await db.users.find_one(
        {'id': user['id']},
        {'_id': 0, 'name': 1, 'email': 1, 'phone': 1, 'shipping_address': 1}) or {}
    envio = fresh.get('shipping_address') or {}
    return {
        'full_name': fresh.get('name') or '',
        'email': fresh.get('email') or '',
        'phone': fresh.get('phone') or '',
        'shipping_address': {
            'address': envio.get('address') or '',
            'address_2': envio.get('address_2') or '',
            'city': envio.get('city') or '',
            'state': envio.get('state') or '',
            'postal_code': envio.get('postal_code') or '',
            'country': envio.get('country') or 'MX',
        },
        # Para que el sitio sepa si debe avisar "usamos los datos de tu última
        # compra": sin dirección guardada no hay nada que avisar.
        'prefilled': bool(envio.get('address')),
    }


def _familia_del_slug(slug, presentacion):
    """El slug del producto PADRE: el del renglón sin su presentación pegada.

        'bronchogen-10-mg'  + '10 mg' -> 'bronchogen'
        'hgh-24-iu'         + '24 IU' -> 'hgh'
        'lemon-bottle-10-ml'+ '10 mL' -> 'lemon-bottle'
    """
    slug = (slug or '').strip().lower()
    cola = re.sub(r'[^a-z0-9]+', '-', (presentacion or '').strip().lower()).strip('-')
    if cola and slug.endswith('-' + cola):
        return slug[:-(len(cola) + 1)]
    return slug


def llaves_de_inventario_vivo(product_id, doc):
    """Todas las formas en que `db.stock` puede tener guardada ESTA presentación.

    ⛔ EL AGUJERO QUE QUEDABA. En `db.products` cada presentación es su propio documento
    (`bronchogen-10-mg`, con su UUID y su SKU), pero el Panel guarda el inventario vivo
    con la llave del producto AGRUPADO del sitio: `fallback-bronchogen::10 mg`. Ninguna
    de las tres llaves que se probaban —product_id, sku, id— puede ser jamás esa cadena,
    así que en TODO producto con presentaciones (o sea, casi todo el catálogo) el
    descuento no encontraba nada y `db.stock` no bajaba NUNCA. Y `db.stock` es justo lo
    que la ficha del sitio usa para pintar "EN MANO / entrega inmediata": se seguía
    anunciando existencia física de piezas que ya se vendieron, indefinidamente.
    """
    llaves = [product_id]
    doc = doc or {}
    for k in ('sku', 'id'):
        if doc.get(k) and doc[k] not in llaves:
            llaves.append(doc[k])
    pres = (doc.get('presentation') or '').strip()
    if pres:
        familia = _familia_del_slug(doc.get('slug'), pres)
        for base in (f'fallback-{familia}' if familia else '', familia,
                     (doc.get('slug') or '').strip()):
            llave = f'{base}::{pres}'
            if base and llave not in llaves:
                llaves.append(llave)
    return [k for k in llaves if k]


async def _descontar_inventario_vivo(product_id, doc, delta):
    """Mueve el inventario VIVO (`db.stock`) de UNA presentación. Devuelve si acertó.

    La llave de `db.stock` la escribe el Panel como `<producto agrupado>::<presentación>`,
    pero el carrito manda a veces el id, a veces un UUID y a veces el SKU. Cuando no
    coincidía, `update_one` no encontraba el documento y devolvía "0 modificados" sin
    quejarse: el pedido salía, el inventario vivo se quedaba igual, y el sitio seguía
    ofreciendo como "en mano" algo que ya no existe. Se prueban TODAS las llaves posibles
    (ver `llaves_de_inventario_vivo`) y, si ninguna existe, se avisa — porque el silencio
    aquí se ve exactamente igual que el éxito.

    Y nunca por debajo de cero: un inventario vivo en negativo no es un dato, es una
    mentira con signo. Si el pedido se lleva más de lo que había en mano, queda en 0 y
    se GRITA, que es lo que de verdad hay que revisar."""
    candidatas = llaves_de_inventario_vivo(product_id, doc)
    for llave in candidatas:
        if delta < 0:
            r = await db.stock.update_one({'key': llave, 'qty': {'$gte': -delta}},
                                          {'$inc': {'qty': delta}})
            if r.matched_count:
                return True
            # el renglón existe pero no alcanza: se vendió más de lo que había en mano
            r = await db.stock.update_one({'key': llave}, {'$set': {'qty': 0}})
            if r.matched_count:
                logger.warning('INVENTARIO VIVO EN CORTO: %s no tenía las %s piezas de '
                               'este pedido. Queda en 0 — revisa qué se está vendiendo '
                               'sin existencia física.', llave, -delta)
                return True
            continue
        r = await db.stock.update_one({'key': llave}, {'$inc': {'qty': delta}})
        if r.matched_count:
            return True
    logger.warning('INVENTARIO VIVO SIN DESCONTAR: no hay renglón en db.stock para %s '
                   '(probé %s). El pedido salió y las piezas no bajaron.',
                   product_id, candidatas)
    return False


async def _existencia_viva(product_id, doc):
    """Las piezas que DE VERDAD hay de esta presentación, o None si no lleva renglón.

    `db.stock` es el inventario real: lo que el Panel captura pieza por pieza y lo que la
    ficha del sitio pinta como "en mano". `db.products.stock` es otra cosa —un contador
    SEMBRADO al dar de alta el producto, casi siempre 40— y no representa nada físico.
    Devolver None (y no 0) cuando no hay renglón es a propósito: "no sé" y "no hay" no
    son lo mismo, y bloquear la venta de un producto solo porque nadie le capturó
    inventario sería cerrar la tienda por un dato faltante."""
    for llave in llaves_de_inventario_vivo(product_id, doc):
        row = await db.stock.find_one({'key': llave}, {'_id': 0, 'qty': 1})
        if row is not None:
            return max(0, int(row.get('qty') or 0))
    return None


async def _disponible_de(clave, doc):
    """Lo que HAY EN MANO de un producto: lo MENOR entre el contador del catálogo y el
    inventario vivo.

    ⚠️ ESTO NO ES UN LÍMITE DE VENTA. Es lo que sale HOY. Lo que se pida de más se vende
    igual y se manda pedir al proveedor (ver `_reservar_inventario`). Sirve para partir el
    pedido en dos entregas y para avisarle al cliente ANTES de pagar, no para rechazarlo.

    ⛔ EL CONTADOR SEMBRADO NO MANDA sobre lo que hay. `db.products.stock` es un número de
    fábrica (casi todo el catálogo nació en 40) y no cuenta piezas físicas; `db.stock` sí.
    El 30-jul había 191 de 193 productos anunciando 40 con 20 piezas reales, así que sin
    esto el pedido se partía contra un número inventado."""
    sembrado = int((doc or {}).get('stock') or 0)
    vivo = await _existencia_viva(clave, doc)
    return sembrado if vivo is None else min(sembrado, vivo)


async def _catalogo_de(items):
    """El catálogo REAL de unos renglones del carrito, indexado por id Y por SKU.

    Aceptamos las dos llaves porque el carrito manda cualquiera de las dos: el
    carrito viejo inventaba ids tipo "slug::5 mg" que no existían, y eso hacía que
    el producto se saltara su tope de comisión (Christian, 2026-07-25).

    Vive en su propia función porque ahora lo usan DOS rutas —crear el pedido y
    cotizar el envío— y las dos tienen que mirar exactamente el mismo catálogo. Con
    la consulta copiada en dos lados, una de las dos se queda sin un campo (el peso,
    por ejemplo) y el error no se ve hasta que la paquetería cobra de más.
    """
    keys = [getattr(it, 'product_id', None) or (it.get('product_id') if isinstance(it, dict) else '')
            for it in (items or [])]
    _pdocs = await db.products.find(
        {'$or': [{'id': {'$in': keys}}, {'sku': {'$in': keys}}]},
        # `slug` y `presentation` NO son adorno: con ellos se arma la llave del
        # inventario vivo (`fallback-<familia>::<presentación>`). Sin ellos el descuento
        # de `db.stock` no encuentra nada y no baja nunca.
        # `weight_kg` tampoco: es lo que cotiza el envío por peso real.
        {'_id': 0, 'id': 1, 'sku': 1, 'name': 1, 'commission_cap': 1,
         'distributor_eligible': 1, 'category': 1, 'stock': 1, 'price': 1,
         'hidden': 1, 'slug': 1, 'presentation': 1, 'weight_kg': 1}).to_list(500)
    pflags = {}
    for d in _pdocs:
        pflags[d['id']] = d
        if d.get('sku'):
            pflags[d['sku']] = d
    return pflags


def _agrupar_por_producto(items, pflags):
    """Junta los renglones del carrito POR PRODUCTO REAL, no por lo que mandó el navegador.

    Dos trampas, las dos ya pagadas:
      · el mismo producto repetido en varios renglones (40 y 40 son 80, no 40 ≤ 40 dos
        veces);
      · el mismo producto escrito de dos formas — su UUID en un renglón y su SKU en otro.
        `pflags` acepta las dos llaves justo porque el carrito manda cualquiera, así que
        agrupar por el texto los cuenta como productos distintos y cada uno pasa la prueba
        contra el MISMO inventario. El descuento, en cambio, sí los junta (busca por id O
        sku): ochenta piezas de las cuarenta que hay.
    """
    agrupado = {}
    for it in items:
        d = pflags.get(it.product_id)
        clave = d['id'] if d else it.product_id
        agrupado.setdefault(clave, {'total': 0, 'nombre': it.name, 'doc': d})
        agrupado[clave]['total'] += int(it.quantity)
    return agrupado


# Cuántas veces se reintenta apartar cuando otro pedido se movió en medio. No es un
# número mágico: cada vuelta solo ocurre si OTRO checkout ganó la carrera en ese instante,
# y con cinco ya se agotó cualquier escenario real de dos o tres clientes a la vez.
REINTENTOS_RESERVA = 5


async def _apartar_del_catalogo(clave, n):
    """Aparta HASTA `n` piezas del contador del catálogo. Devuelve cuántas apartó.

    ⛔ MIRAR Y RESTAR TIENEN QUE SER EL MISMO PASO, y nunca por debajo de cero. Antes se
    leía el stock arriba y se restaba mucho después, y entre las dos cosas cabía otro
    pedido entero: dos clientes veían la última pieza, los dos pasaban, y el inventario
    terminaba en −1. Basta con dos personas comprando a la vez, que es justo lo que pasa
    cuando un anuncio pega.

    Aquí la condición viaja DENTRO del update (`stock >= lo que voy a tomar`). Si otro
    pedido se llevó piezas mientras tanto, Mongo no encuentra el documento, no resta nada
    y se vuelve a leer para tomar lo que quedó — no se rechaza al cliente, se toma menos y
    el resto se manda pedir."""
    for _ in range(REINTENTOS_RESERVA):
        d = await db.products.find_one({'$or': [{'id': clave}, {'sku': clave}]},
                                       {'_id': 0, 'stock': 1})
        if d is None:
            return 0
        tomar = min(int(n), max(0, int(d.get('stock') or 0)))
        if tomar <= 0:
            return 0
        r = await db.products.update_one(
            {'$or': [{'id': clave}, {'sku': clave}], 'stock': {'$gte': tomar}},
            {'$inc': {'stock': -tomar}})
        if r.matched_count:
            return tomar
    return 0


async def _devolver_reserva(reservado):
    """Deshace lo apartado por un pedido que no llegó a existir."""
    for clave, n in reservado:
        await db.products.update_one({'$or': [{'id': clave}, {'sku': clave}]},
                                     {'$inc': {'stock': n}})


async def _apartar_del_inventario_real(clave, doc, n):
    """Aparta HASTA `n` piezas del inventario REAL. Devuelve (apartadas, llave).

    `db.stock` es lo que hay de verdad: lo que el Panel captura y lo que la ficha pinta
    como "en mano". Si la presentación no lleva renglón devuelve `(None, None)` — "no sé"
    no es "no hay", y ahí manda el contador del catálogo."""
    for llave in llaves_de_inventario_vivo(clave, doc):
        row = await db.stock.find_one({'key': llave}, {'_id': 0, 'qty': 1})
        if row is None:
            continue
        for _ in range(REINTENTOS_RESERVA):
            tomar = min(int(n), max(0, int((row or {}).get('qty') or 0)))
            if tomar <= 0:
                return 0, llave
            r = await db.stock.update_one({'key': llave, 'qty': {'$gte': tomar}},
                                          {'$inc': {'qty': -tomar}})
            if r.matched_count:
                return tomar, llave
            row = await db.stock.find_one({'key': llave}, {'_id': 0, 'qty': 1})
        return 0, llave
    return None, None


async def _devolver_reserva_viva(reservado):
    """Regresa al inventario real lo apartado por un pedido que no llegó a existir."""
    for llave, n in reservado:
        await db.stock.update_one({'key': llave}, {'$inc': {'qty': n}})


async def _reservar_inventario(pedido_por_producto):
    """Aparta LO QUE HAYA y deja el resto POR SURTIR. Nunca rechaza.

    ⛔ LA REGLA MADRE (Christián, 2026-07-30): NINGUNA venta se bloquea por inventario,
    jamás. «Si piden 40 y solo tengo 20 de un producto, se mandan los 20 y se mandan pedir
    los otros 20.» El pedido se parte en dos entregas: lo que hay sale ya por el flujo
    normal (2 a 5 días) y el excedente llega alrededor de una semana después. Lo único que
    sigue sin venderse son los productos OCULTOS y los vetados, que es otra cosa: eso no es
    falta de inventario, es que no están a la venta.

    Aquí estuvo el péndulo entero. Primero el checkout no miraba nada y se cobraba lo que
    no existía sin decírselo a nadie. Después se puso un bloqueo duro que rechazaba el
    pedido — y eso tiraba ventas, que era peor. Lo que hacía falta no era decidir entre
    vender a ciegas o no vender: era CONTAR bien y AVISAR.

    Devuelve `(del_catálogo, del_inventario_real, por_surtir)`, donde `por_surtir` trae,
    renglón por renglón, cuántas piezas salen ya y cuántas hay que mandar pedir. Ese
    desglose es lo que ve el cliente antes de pagar y lo que ve el equipo en el Panel.

    Los dos inventarios se mueven SIEMPRE por el mismo número: manda el real, y si el
    contador del catálogo tenía menos, se le devuelve la diferencia al real. Cuando cada
    lado bajaba por su cuenta, cada ciclo de pedido y cancelación los desbalanceaba —
    Orexin A quedó en 43 cuando tenía 40 (2026-07-27)."""
    reservado, vivo_reservado, por_surtir = [], [], []
    for clave, acum in pedido_por_producto.items():
        doc = acum.get('doc')
        if not doc:
            continue          # producto que no resolvimos: no se toca su inventario
        n = int(acum['total'])
        vivas, llave = await _apartar_del_inventario_real(clave, doc, n)
        if vivas is None:
            logger.warning('INVENTARIO REAL SIN CAPTURAR: no hay renglón en db.stock '
                           'para %s. El pedido se parte contra el contador sembrado del '
                           'catálogo, que no cuenta piezas físicas.', clave)
        del_catalogo = await _apartar_del_catalogo(clave, n if vivas is None else vivas)
        if vivas is not None and del_catalogo < vivas:
            # El contador tenía menos que la bodega: los dos bajan por el número chico o
            # se desbalancean para siempre.
            await db.stock.update_one({'key': llave}, {'$inc': {'qty': vivas - del_catalogo}})
            vivas = del_catalogo
        if del_catalogo:
            reservado.append((clave, del_catalogo))
        if vivas:
            vivo_reservado.append((llave, vivas))
        if del_catalogo < n:
            por_surtir.append({
                'product_id': clave,
                'name': acum['nombre'],
                'pedidas': n,
                'en_mano': int(del_catalogo),
                'por_surtir': int(n - del_catalogo),
            })
    return reservado, vivo_reservado, por_surtir


async def _apartar_puntos(user_id, puntos) -> bool:
    """Aparta puntos del saldo COMPARANDO Y RESTANDO EN EL MISMO PASO.

    ⛔ ES EL MISMO CANDADO QUE `_reservar_inventario`, y por la misma razón. El saldo se
    leía en una consulta (`find_one` → `clamp_redeem`) y se restaba MUCHO después, ya
    grabado el pedido. Entre las dos cosas cabía otro checkout del mismo cliente: los
    dos leían 1,000 puntos, los dos los canjeaban enteros y el saldo terminaba en −1,000.
    El cliente pagaba dos pedidos con los mismos puntos. Es dinero que sale, no un
    desajuste cosmético: 1 punto = 1 peso de mercancía.

    Aquí la condición viaja DENTRO del update (`points_balance >= lo que pides`): si el
    otro pedido ya se los llevó, Mongo no encuentra el documento, no resta nada y se
    devuelve False. Nunca deja el saldo en negativo.
    """
    puntos = int(puntos or 0)
    if puntos <= 0:
        return True
    r = await db.users.update_one(
        {'id': user_id, 'points_balance': {'$gte': puntos}},
        {'$inc': {'points_balance': -puntos}})
    return bool(r.matched_count)


async def _devolver_puntos(user_id, puntos):
    """Regresa los puntos apartados por un pedido que no llegó a existir."""
    puntos = int(puntos or 0)
    if puntos > 0:
        await db.users.update_one({'id': user_id}, {'$inc': {'points_balance': puntos}})


async def _apartar_cupon(coupon, order_number: str) -> bool:
    """Quema un cupón de un solo uso MIRANDO Y MARCANDO EN EL MISMO PASO.

    ⛔ EL MISMO CANDADO QUE `_apartar_puntos` Y `_reservar_inventario`, y por la misma
    razón — este era el que faltaba. El cupón se miraba al principio del checkout
    (`not _c.get('used')`) y se marcaba usado hasta el final, ya grabado el pedido.
    Entre esas dos líneas hay una docena de `await` (inventario, puntos, insert del
    pedido, correos), y cada uno suelta el hilo: dos checkouts simultáneos con el MISMO
    cupón leían los dos `used: False`, los dos se llevaban el descuento y el cupón de un
    solo uso pagaba dos veces. Es dinero que sale, no un desajuste cosmético.

    Aquí la condición viaja DENTRO del update (`used` distinto de True): si el pedido de
    al lado ya lo quemó, Mongo no encuentra el documento, no marca nada y se devuelve
    False. El que llega tarde se queda sin descuento, que es lo correcto.
    """
    if not coupon or not coupon.get('single_use', True):
        return True                      # los reutilizables no se apartan
    r = await db.discount_codes.update_one(
        {'id': coupon['id'], 'used': {'$ne': True}},
        {'$set': {'used': True, 'active': False, 'used_order': order_number}})
    return bool(r.matched_count)


async def _devolver_cupon(coupon):
    """Revive un cupón que se quemó para un pedido que no llegó a existir."""
    if coupon and coupon.get('single_use', True):
        await db.discount_codes.update_one(
            {'id': coupon['id']},
            {'$set': {'used': False, 'active': True}, '$unset': {'used_order': ''}})


def _piezas_a_devolver(order):
    """Cuántas piezas devolverle al inventario por producto, al cancelar o borrar.

    ⛔ SE DEVUELVE LO QUE SE APARTÓ, NO LO QUE SE PIDIÓ. Con el envío partido las dos
    cosas dejaron de ser lo mismo: un pedido de 40 con 20 en bodega solo se llevó 20, y
    devolver 40 le regala 20 piezas al inventario en cada cancelación. Es exactamente la
    asimetría que dejó Orexin A en 43 cuando tenía 40 (2026-07-27), servida en bandeja.

    Los pedidos anteriores al envío partido no traen `stock_taken`: ésos se llevaron
    justo lo que pedían, así que se devuelven por cantidad — que es lo que hacían."""
    tomado = order.get('stock_taken') or {}
    if tomado:
        return {pid: int(n) for pid, n in tomado.items() if pid and int(n or 0) > 0}
    piezas = {}
    for item in order.get('items', []):
        pid, qty = item.get('product_id'), int(item.get('quantity') or 0)
        if pid and qty > 0:
            piezas[pid] = piezas.get(pid, 0) + qty
    return piezas


async def restore_order_stock(order):
    """Devuelve al inventario lo que la orden se habia llevado. Se llama al
    CANCELAR y al BORRAR: antes el stock se descontaba y nunca regresaba, asi que
    cada cancelacion perdia piezas para siempre (auditoria del 2026-07-25)."""
    if order.get('stock_restored'):
        return                      # ya se devolvio: no lo hagamos dos veces
    for pid, qty in _piezas_a_devolver(order).items():
        await db.products.update_one({'$or': [{'id': pid}, {'sku': pid}]},
                                     {'$inc': {'stock': qty}})
        # La devolución usa el MISMO resolvedor de llaves que el descuento. Si cada lado
        # busca de una forma distinta, el inventario se desbalancea con cada cancelación
        # — ya pasó una vez y dejó Orexin A en 43 cuando tenía 40.
        doc = await db.products.find_one(
            {'$or': [{'id': pid}, {'sku': pid}]},
            {'_id': 0, 'id': 1, 'sku': 1, 'slug': 1, 'presentation': 1})
        await _descontar_inventario_vivo(pid, doc, qty)
    await db.orders.update_one({'id': order['id']}, {'$set': {'stock_restored': True}})


async def revoke_order_points(order):
    """Al cancelar: quita lo depositado y devuelve lo canjeado. Idempotente."""
    if not order.get('user_id'):
        return
    if order.get('points_awarded'):
        res = await db.orders.update_one(
            {'id': order['id'], 'points_awarded': True},
            {'$set': {'points_awarded': False}},
        )
        if res.modified_count:
            await db.users.update_one({'id': order['user_id']}, {'$inc': {'points_balance': -int(order.get('points_earned', 0))}})
            await _points_entry(order['user_id'], order, 'revoke', -int(order.get('points_earned', 0)))
    if int(order.get('points_used', 0) or 0) > 0 and not order.get('points_refunded'):
        res = await db.orders.update_one(
            {'id': order['id'], 'points_refunded': {'$ne': True}},
            {'$set': {'points_refunded': True}},
        )
        if res.modified_count:
            await db.users.update_one({'id': order['user_id']}, {'$inc': {'points_balance': int(order['points_used'])}})
            await _points_entry(order['user_id'], order, 'refund', order['points_used'])


async def retirar_puntos_ganados(order):
    """Quita SÓLO los puntos que este pedido depositó. Idempotente.

    ⛔ NO ES `revoke_order_points`. Esa además DEVUELVE los puntos canjeados, que es lo
    correcto al CANCELAR (el pedido desaparece) pero un regalo si el pedido sigue en pie:
    desmarcar el pago de un pedido vivo con esa función le devolvería al cliente los
    puntos que ya gastó en él, y podría gastarlos otra vez. Aquí sólo se deshace el
    depósito.
    """
    if not order.get('user_id') or not order.get('points_awarded'):
        return
    res = await db.orders.update_one(
        {'id': order['id'], 'points_awarded': True},
        {'$set': {'points_awarded': False}},
    )
    if res.modified_count:
        ganados = int(order.get('points_earned', 0) or 0)
        await db.users.update_one({'id': order['user_id']}, {'$inc': {'points_balance': -ganados}})
        await _points_entry(order['user_id'], order, 'revoke', -ganados)


async def recobrar_puntos_canjeados(order):
    """Un pedido que sale de 'cancelado' vuelve a pagar sus puntos. Idempotente.

    ⛔ EL OTRO DOBLE GASTO. Cancelar devolvía los puntos canjeados y ponía la marca
    `points_refunded`; reactivar el pedido no hacía nada con ella. Así el cliente se
    quedaba con el pedido Y con los puntos: los mismos puntos pagaron dos veces, esta
    vez sin necesidad de dos pedidos simultáneos — bastaba cancelar y volver a
    confirmar. Se descuentan comparando y restando en el mismo paso, y si el saldo ya
    no alcanza (se los gastó mientras el pedido estaba cancelado) NO se deja el saldo
    en negativo: se avisa en la bitácora para que un humano lo resuelva.
    """
    if not order.get('user_id') or not order.get('points_refunded'):
        return
    usados = int(order.get('points_used', 0) or 0)
    if usados <= 0:
        return
    res = await db.orders.update_one({'id': order['id'], 'points_refunded': True},
                                     {'$set': {'points_refunded': False}})
    if not res.modified_count:
        return
    if await _apartar_puntos(order['user_id'], usados):
        await _points_entry(order['user_id'], order, 'redeem', -usados)
    else:
        await db.orders.update_one({'id': order['id']},
                                   {'$set': {'points_refunded': True}})
        logger.warning('PUNTOS SIN RECOBRAR: el pedido %s se reactivó pero el cliente %s '
                       'ya no tiene los %s puntos que había canjeado.',
                       order.get('order_number'), order['user_id'], usados)


@api_router.get('/me/points')
async def my_points(user=Depends(get_current_user)):
    """Saldo y movimientos de puntos. Los distribuidores no participan."""
    if not loyalty.eligible(user):
        return {'eligible': False, 'balance': 0, 'ledger': []}
    fresh = await db.users.find_one({'id': user['id']}, {'_id': 0, 'points_balance': 1})
    ledger = await db.points.find({'user_id': user['id']}, {'_id': 0}).to_list(200)
    ledger.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return {'eligible': True, 'balance': int((fresh or {}).get('points_balance', 0) or 0),
            'earn_rate': loyalty.EARN_RATE, 'ledger': ledger[:100]}


# Pedidos que NO cuentan como venta en ningun reporte de marketing.
NO_CUENTAN = ('cancelado',)


async def _es_primera_compra(email: str) -> bool:
    """¿Es la PRIMERA compra de este correo?

    Es la pieza que hace honesto el costo por cliente. Si un cliente que ya
    compraba vuelve a comprar, esa venta no es un cliente que el anuncio haya
    conseguido: contarla como tal abarata el costo artificialmente y termina
    justificando gasto en campañas que en realidad no traen gente nueva.
    """
    if not email:
        return False
    # Sin distinguir mayusculas: el correo se guarda tal como lo escribio el
    # cliente, asi que "Ana@X.com" y "ana@x.com" son la MISMA persona. Buscarlo
    # exacto contaria a un cliente viejo como nuevo cada vez que teclea distinto.
    previo = await db.orders.find_one(
        {'customer.email': {'$regex': f'^{re.escape(email.strip())}$', '$options': 'i'},
         'status': {'$nin': list(NO_CUENTAN)}},
        {'_id': 0, 'id': 1})
    return previo is None


@api_router.post('/orders')
async def create_order(payload: OrderCreate, user=Depends(get_optional_user)):
    deny_view_as(user)          # en modo "ver como" no se compra nada
    if not payload.items:
        raise HTTPException(status_code=400, detail='El carrito esta vacio')
    # 'tarjeta' SOLO si Mercado Pago esta configurado. Hasta el 2026-07-26 se
    # aceptaba siempre y no cobraba nada: el cliente veia "Pedido recibido" sin
    # que nadie le cobrara. Si no hay pasarela, no se ofrece la vía.
    # OXXO viaja por Mercado Pago igual que tarjeta (mismo webhook, misma
    # verificación), así que se enciende y se apaga con la misma llave.
    allowed_methods = ['spei'] + (['tarjeta', 'oxxo'] if mercadopago.enabled() else []) \
        + (['cripto'] if crypto_enabled() else [])
    if payload.payment_method not in allowed_methods:
        raise HTTPException(status_code=400, detail='Metodo de pago no disponible')
    # Familia HGH (no el Fragment): precio neto SIEMPRE (Christian, 2026-07-22).
    # La regla vive en `es_hgh_neto`, a nivel de módulo, porque el cotizador del
    # distribuidor tiene que aplicar EXACTAMENTE la misma.
    def _is_hgh_net(item):
        return es_hgh_neto(item.product_id, item.name)
    # Tope de comisión y elegibilidad POR PRODUCTO (regla Christian 2026-07-23):
    # si un producto no deja 5x neto, no participa del canal de distribuidores
    # (ni descuento de código, ni promo, ni comisión) — solo venta directa.
    # Resolvemos cada renglon contra el catalogo real. Aceptamos id o SKU: el
    # carrito viejo mandaba ids inventados ("slug::5 mg") que no existian, y eso
    # hacia que el producto se saltara su tope de comision (Christian 2026-07-25).
    _pflags = await _catalogo_de(payload.items)

    # ⛔ EL PRECIO LO PONE EL SERVIDOR, NUNCA EL NAVEGADOR.
    # Hasta el 2026-07-27 el subtotal se calculaba con `item.price` tal como venía en la
    # petición. Cualquiera podía mandar precio 0 y llevarse un vial de $9,359 pagando los
    # $250 del envío. Ahora cada renglón se retasa contra el catálogo real y el precio del
    # navegador se ignora por completo. Lo cazó una auditoría externa (Codex).
    _huerfanos = [it.name for it in payload.items if it.product_id not in _pflags]
    if _huerfanos:
        # Antes solo se anotaba en la bitácora y el pedido seguía con el precio del
        # navegador. Un producto que no se resuelve no se puede tasar: no se vende.
        raise HTTPException(
            status_code=400,
            detail=f'No reconocemos estos productos: {", ".join(_huerfanos)}. '
                   'Vacía el carrito y vuelve a agregarlos.')
    _ocultos = [it.name for it in payload.items if _pflags[it.product_id].get('hidden')]
    if _ocultos:
        raise HTTPException(status_code=400,
                            detail=f'Ya no está a la venta: {", ".join(_ocultos)}')
    for it in payload.items:
        real = _pflags[it.product_id].get('price')
        if real is None:
            raise HTTPException(status_code=400, detail=f'{it.name} no tiene precio')
        if abs(float(it.price or 0) - float(real)) > 0.01:
            logger.warning('Precio del navegador distinto al del catálogo en %s: '
                           'mandó %s, vale %s', it.product_id, it.price, real)
        it.price = float(real)

    def _cap_of(item):
        d = _pflags.get(item.product_id, {})
        return max(0.0, min(0.50, float(d.get('commission_cap', 0.50) or 0.50)))

    def _eligible(item):
        d = _pflags.get(item.product_id, {})
        if (d.get('category') or '') in NO_DISCOUNT_CATEGORIES:
            return False   # insumos (agua bacteriostática, viales, jeringas): nunca
        return bool(d.get('distributor_eligible', True)) and not _is_hgh_net(item)

    def _disc_of(item, rate):
        """Descuento REAL de un renglón: el menor entre el que se pidió y el tope
        del producto. Regla de Christian (2026-07-25): primero el ROI de la casa.
        Si el tope no aguanta, se RECORTA (antes se daba cero)."""
        if not _eligible(item):
            return 0.0
        return min(float(rate or 0), _cap_of(item))

    # ⛔ EL INVENTARIO NO BLOQUEA NINGUNA VENTA. Regla de Christián (2026-07-30): «si
    # piden 40 y solo tengo 20, se mandan los 20 y se mandan pedir los otros 20». El
    # pedido se PARTE: lo que hay sale ya (2 a 5 días) y el resto llega alrededor de una
    # semana después. Lo único que sigue sin venderse son los OCULTOS y los vetados —
    # eso se revisa arriba y no es falta de inventario, es que no están a la venta.
    #
    # Aquí llegó a haber un rechazo duro (409 "no tenemos suficiente"). Estaba mal: tiraba
    # ventas. Y antes de eso no había NADA y se cobraba lo que no existe sin avisar. Lo
    # que hacía falta no era elegir entre vender a ciegas o no vender: era CONTAR bien y
    # AVISAR. El conteo vive en `_reservar_inventario` (aparta lo que hay y devuelve el
    # desglose) y el aviso viaja en la respuesta para que el sitio lo pinte.
    #
    # ⚠️ SE SUMAN LOS RENGLONES DEL MISMO PRODUCTO. El carrito puede mandar el MISMO
    # producto dos veces y, sin agrupar, cada renglón se parte por su cuenta: el desglose
    # de "cuántas salen ya" sale mal y el Panel manda pedir de más.
    #
    # ⚠️ Y SE AGRUPA POR EL PRODUCTO RESUELTO, NO POR EL TEXTO QUE MANDÓ EL CARRITO. El
    # MISMO producto viaja a veces con su UUID y a veces con su SKU (`_pflags` acepta los
    # dos justo porque el carrito manda cualquiera). Agrupando por el texto son dos
    # productos distintos, y el inventario sí los junta: el reparto entre "en mano" y "por
    # surtir" queda mal por partida doble.
    for it in payload.items:
        if it.quantity is None or it.quantity < 1:
            raise HTTPException(status_code=400, detail=f'Cantidad invalida en {it.name}')
    pedido_por_producto = _agrupar_por_producto(payload.items, _pflags)

    # Ya con los precios del catálogo (ver arriba): el navegador no decide nada.
    subtotal = sum(item.price * item.quantity for item in payload.items)
    discountable = sum(
        item.price * item.quantity for item in payload.items if _eligible(item)
    )
    # Atribucion a distribuidor: SOLO si esta venta usa un codigo de distribuidor.
    # Regla de Christian (2026-07-22): si el cliente NO pone un codigo, la venta NO
    # cuenta para ningun distribuidor — aunque ese cliente haya comprado antes con
    # el codigo de alguien. El vinculo 'referred_by' del usuario NO genera comision
    # por si solo; cada orden se atribuye por el codigo usado en ESA compra.
    # El código puede ser uno de los VARIOS del distribuidor (con su propio
    # descuento) o su código legacy. El descuento sale del código, acotado a su
    # comisión de nivel, y de SU tajada. Sin código = promo automática (la casa).
    # Cupón personal (regalo del admin): descuento directo, sin comisión ni atribución.
    coupon = None
    if payload.distributor_code:
        _c = await db.discount_codes.find_one({'code': payload.distributor_code.strip().upper(),
                                               'active': True, 'kind': 'coupon'})
        # EL CANDADO DEL MONTO: un cupón de recuperación de carrito solo sirve si la
        # compra es del mismo monto o mayor al carrito que lo generó (Christian,
        # 2026-07-25). Si no, el cliente quita productos, usa el cupón y salimos
        # perdiendo. Los cupones normales (GIFT) no traen min_order y pasan igual.
        if _c and not _c.get('used') and (not _c.get('expires_at') or _c['expires_at'] >= now_iso()) \
           and (not _c.get('user_id') or (user and user['id'] == _c['user_id'])) \
           and recovery.coupon_is_valid_for(_c, discountable):
            coupon = _c
    referrer, code_discount = ((None, 0.0) if coupon else await _resolve_code(payload.distributor_code))
    if coupon:
        # ⛔ TOPADO AL 40% AL COBRAR, no sólo al crearlo: los GIFT que ya andan sueltos
        # con 50% valen 40% (Christián, 2026-07-31). Ver `tasa_de_cupon`.
        discount_rate = tasa_de_cupon(coupon)
    elif referrer:
        discount_rate = code_discount
    else:
        discount_rate = 0.15 if discountable >= 35000 else 0.10
    # COMPRA PROPIA de un distribuidor (regla de Christian, 2026-07-25): compra para
    # sí mismo con SU comisión máxima como descuento. Ese descuento ES su comisión,
    # cobrada por adelantado: NO gana comisión encima, y la orden no se atribuye a
    # nadie. Sigue acotado al tope de cada producto (el ROI manda) y los insumos
    # siguen fuera.
    #
    # `tasa_base` es el precio de CLIENTE de esta compra: cupón, código, o la promo
    # automática. Se guarda aparte porque la REGLA DE 5 lo necesita: los renglones que
    # no llegan a cinco piezas pagan eso, no el precio de distribuidor.
    tasa_base = discount_rate
    own_rate = buyer_own_rate(user)
    # ⛔ LA PUERTA ANÓNIMA (Christián, 2026-07-30). Hasta hoy, un distribuidor que
    # cerraba su sesión y compraba con SU PROPIO código se llevaba las tres cosas:
    # el descuento del código, la comisión encima, y el crédito de venta de nivel —
    # `buyer_own_rate` veía a un invitado (0) y `referrer` no se anulaba. Salía más
    # barato comprar deslogueado que entrando a su cuenta. Si el correo del comprador
    # es el del dueño del código, es compra propia y se trata como tal.
    # Sólo empareja por correo EXACTO: adivinar de más le quitaría su comisión a
    # alguien que sí vendió (ver descuentos.motivo_de_compra_propia).
    motivo_propio = descuentos.motivo_de_compra_propia(user, referrer, payload.customer.email)
    compra_propia = bool(motivo_propio)
    if motivo_propio == 'correo':
        own_rate = max(own_rate, pyramid.effective_rate(referrer))
        logger.info('Compra propia detectada por correo (%s): sin comisión ni crédito de nivel.',
                    referrer.get('email'))
    if compra_propia:
        referrer = None   # su propia compra no se le atribuye ni le paga comision
    elif own_rate > 0:
        # Cliente con trato especial (`personal_discount_rate`): sigue siendo un
        # descuento PAREJO para todo el carrito. La regla de 5 es del canal de
        # distribuidores, no de un trato negociado con un cliente.
        tasa_base = max(tasa_base, own_rate)
    # LA REGLA DE 5, por PRODUCTO ya resuelto contra el catálogo (no por el texto que
    # mandó el carrito): el precio de distribuidor sólo baja en los renglones que
    # juntan 5 o más piezas del mismo producto; los de 1 a 4 pagan precio de cliente.
    tasas_pedidas = descuentos.tasas_por_producto(
        {clave: g['total'] for clave, g in pedido_por_producto.items()},
        tasa_base, own_rate, compra_propia)
    regla_de_5 = (descuentos.faltantes_para_precio_distribuidor(pedido_por_producto)
                  if compra_propia and own_rate > tasa_base + 1e-9 else [])

    def _clave_de(item):
        d = _pflags.get(item.product_id)
        return d['id'] if d else item.product_id

    def _pedida_de(item):
        return tasas_pedidas.get(_clave_de(item), tasa_base)

    # Descuento RENGLÓN POR RENGLÓN: cada producto recibe el menor entre el descuento
    # pedido y su propio tope. Los que reciben menos se listan para poder avisarle al
    # cliente ("producto no participante, se aplicó un descuento alterno").
    #
    # `discount_rate` de la orden = la MAYOR tasa pedida del carrito. Lo leen los
    # puntos (el 40% no genera), los reportes y las fichas de pedido, y con un carrito
    # parejo —que es todo lo que existía hasta hoy— vale exactamente lo mismo que
    # antes. El promedio efectivo no sirve aquí: pintaría "34.7%" en los reportes y
    # aflojaría el candado del 40% sin que nadie lo decidiera. El desglose fino, el
    # que sí explica qué pagó cada renglón, va en `discount_lines`.
    # ⛔⛔ EL TECHO DEL 40%, EN EL EMBUDO POR DONDE PASAN TODAS LAS PUERTAS.
    # Christián, 2026-07-31: «nadie por encima de ese 40% a menos que seamos María y yo».
    #
    # Aquí ya se juntaron TODAS las formas de descontar del checkout: el cupón, el código
    # del distribuidor, la promo automática, la compra propia y la regla de 5. Topar en
    # este punto —y no en cada una— es justamente lo que evita la puerta olvidada: en dos
    # días aparecieron TRES (venta directa 60%, cupón GIFT 50%, trato especial 50%), cada
    # una encontrada por separado. Lo que entre aquí arriba de 40 sale en 40, venga de
    # donde venga.
    #
    # ⚠️ Esto TAMBIÉN acota el código de un distribuidor de nivel alto (diamante 43%,
    # manual hasta 50%): un cliente suyo ya no puede recibir 43%. Su COMISIÓN no se toca
    # —eso es otra regla, tope 50%— pero su compra propia sí queda en 40%.
    techo = techo_de_descuento(user)
    tasa_base = min(tasa_base, techo)
    tasas_pedidas = {k: min(v, techo) for k, v in tasas_pedidas.items()}

    discount, discount_rate, discount_capped, discount_lines = descuentos.repartir(
        payload.items, _clave_de, lambda it: _cap_of(it) if _eligible(it) else 0.0,
        tasas_pedidas, tasa_base)
    after_discount = subtotal - discount
    # Lealtad: el canje se limita al saldo real y a la mercancia (el envio va en dinero).
    points_used = 0
    if payload.points_to_use and user and loyalty.eligible(user):
        fresh = await db.users.find_one({'id': user['id']}, {'_id': 0, 'points_balance': 1})
        balance = int((fresh or {}).get('points_balance', 0) or 0)
        points_used = loyalty.clamp_redeem(payload.points_to_use, balance, after_discount)
    paid_merchandise = after_discount - points_used
    # El envio lo decide el SERVIDOR, no lo que mande el navegador: el campo
    # `shipping` de la peticion se ignora por completo — igual que se ignoran los
    # precios de los productos. Con Skydropx encendido el monto sale de la cotizacion
    # GUARDADA (revalidada contra este CP y este peso, y recotizada si ya no vale);
    # apagado, del camino de siempre.
    shipping, shipping_quote = await _envio_del_pedido(payload, paid_merchandise, _pflags)
    total = paid_merchandise + shipping
    # Lo que la guia cuesta DE VERDAD. Sin cotizacion de Skydropx no es cero: es la
    # tarifa plana, que es lo que la paqueteria cobra igual. Se calculaba con
    # `shipping_quote.get('cost') or 0` y con el cobro apagado la cotizacion viene
    # vacia, asi que TODO pedido guardaba costo $0 y absorbido $0 — un pedido de $179
    # se llevaba $250 de envio (el 140%) y no aparecia en ningun reporte. (2026-07-28)
    costo_guia = float(shipping_quote.get('cost') or COSTO_GUIA_ESTIMADO)
    envio_absorbido = envios.envio_que_absorbe_la_casa(costo_guia, shipping)
    fuera_de_tope = envios.absorcion_fuera_de_tope(costo_guia, paid_merchandise, shipping)
    if fuera_de_tope > 0:
        # No bloquea la venta —el dueño manda— pero deja constancia: la regla de la
        # casa es absorber como máximo el 5% de la compra (era 10% hasta 2026-07-31).
        logger.warning(
            'ENVIO FUERA DE TOPE: la casa absorbe $%.0f de guia en una compra de $%.0f '
            '(tope %.0f%% = $%.0f, se pasa por $%.0f).',
            envio_absorbido, paid_merchandise, TOPE_ENVIO_SOBRE_COMPRA * 100,
            envios.tope_que_absorbe_la_casa(paid_merchandise), fuera_de_tope)
    # `discount_rate` es el descuento CONCEDIDO: con el máximo (40%) el pedido no
    # genera puntos. Ver la regla en loyalty.py.
    points_earned = loyalty.earn(paid_merchandise, user is not None and loyalty.eligible(user),
                                 discount_rate)
    # Pirámide: el vendedor gana (su tasa − el descuento que dio) y cada upline su
    # DIFERENCIAL, sobre la mercancía con descuento (`discountable`). Se bloquea en
    # pesos al crear la orden; los reportes suman lo guardado.
    commissions = []
    commission = 0
    # ⛔ SI LOS PUNTOS PAGARON TODA LA MERCANCÍA, NO HAY COMISIÓN NI PUNTOS NUEVOS.
    # Regla de Christian (2026-07-28). El canje al 100% sí se permite —los puntos ya se
    # ganaron y son suyos—, pero de un pedido donde no entró un peso por la mercancía no
    # se puede pagar además una comisión sobre el precio completo: eso convierte cada
    # canje en dinero que SALE. El barrido adversarial lo puso en números: 80 viales,
    # $0 de ingreso, $187,180 de comisión y $74,896 de costo — $262,076 de pérdida en un
    # solo pedido. Y tampoco se depositan puntos nuevos: si no pagaste, no acumulas.
    pagado_todo_con_puntos = points_used > 0 and paid_merchandise <= 0
    if pagado_todo_con_puntos:
        points_earned = 0
    if referrer and not pagado_todo_con_puntos:
        upline = await _upline_chain(referrer)
        # El reparto se calcula POR TOPE de producto: descuento + comisiones
        # nunca rebasan el tope (así la casa conserva su 5x).
        groups = {}
        for it in payload.items:
            if not _eligible(it):
                continue
            cap = _cap_of(it)
            # El descuento que de verdad se dio EN ESE RENGLÓN. Con la regla de 5 dos
            # renglones del mismo pedido pueden llevar tasas distintas, y el tope
            # (descuento + comisión) se calcula contra la de cada uno.
            disc = _disc_of(it, _pedida_de(it))
            amt = it.price * it.quantity
            # Lo que queda del tope después del descuento es lo máximo que puede
            # repartirse en comisiones. Si el descuento se comió el tope, es 0.
            key = (round(max(0.0, cap - disc), 4), round(disc, 4))
            groups[key] = groups.get(key, 0) + amt
        merged = {}
        for (allowed, disc), amount in groups.items():
            rows = pyramid.compute_commission_breakdown(amount, referrer, upline, discount_rate=disc)
            rows = pyramid.cap_breakdown(rows, amount, allowed)
            for r in rows:
                k = (r['distributor_id'], r.get('role'))
                if k in merged:
                    merged[k]['amount'] += r['amount']
                else:
                    merged[k] = dict(r)
        commissions = list(merged.values())
        # ⛔ La misma regla del canje al 100% pero sin el escalón: de mercancía
        # cobrada en puntos no se paga comisión (ver pyramid.prorratear_por_dinero).
        if points_used > 0:
            commissions = pyramid.prorratear_por_dinero(
                commissions, paid_merchandise, after_discount)
        # ⛔ SIN ACUERDO FIRMADO NO SE DEVENGAN COMISIONES NUEVAS. Se aplica
        # renglón por renglón: si el vendedor no firmó pero su upline sí, el
        # upline cobra su diferencial igual — no se castiga a quien cumplió. El
        # cliente conserva su descuento pase lo que pase: el precio ya se le
        # prometió y esto es un asunto entre la Empresa y el canal.
        #
        # Con el interruptor APAGADO esta llamada devuelve la lista TAL CUAL sin
        # una sola consulta a la base: hoy no cambia ni un peso de comisión.
        commissions = await acuerdo.filtrar_comisiones_sin_acuerdo(db, commissions)
        commission = pyramid.seller_amount(commissions)
    order = Order(
        order_number=gen_order_number(),
        user_id=user['id'] if user else None,
        items=payload.items,
        customer=payload.customer,
        payment_method=payload.payment_method,
        subtotal=subtotal,
        discount=discount,
        discount_rate=discount_rate,
        discount_capped=discount_capped,
        discount_lines=discount_lines,
        regla_de_5=regla_de_5,
        shipping=shipping,
        shipping_quote=shipping_quote,
        shipping_cost=costo_guia,
        # Lo que la casa se comió del envío. Sin este número nadie sabe cuánto
        # cuesta de verdad la promesa de "envío gratis".
        shipping_absorbed=envio_absorbido,
        # Lo que se pasó del tope del 5% en ESTE pedido. Cero cuando se respeta.
        shipping_over_cap=fuera_de_tope,
        total=total,
        referred_by=referrer['id'] if referrer else None,
        commission=commission,
        commissions=commissions,
        points_used=points_used,
        points_earned=points_earned,
        attribution=(payload.attribution.model_dump() if payload.attribution else {}),
        first_order=await _es_primera_compra(payload.customer.email),
        # Se guarda lo que dice el navegador y NO se inventa una fecha cuando viene
        # vacía: una constancia fabricada por el servidor no prueba nada.
        terms_accepted_at=(payload.terms_accepted_at or '').strip()[:40],
    )
    # ⛔ LAS PIEZAS SE APARTAN JUSTO ANTES DE GRABAR EL PEDIDO, mirando y restando en el
    # MISMO paso (ver `_reservar_inventario`). Aquí y no antes: entre esta línea y el
    # `insert_one` no queda nada que pueda fallar y dejar piezas apartadas de un pedido
    # que nunca existió.
    #
    # Y NO RECHAZA NUNCA: aparta lo que hay y devuelve el desglose de lo que falta. Los
    # dos inventarios —el contador del catálogo y las piezas reales— bajan por el mismo
    # número, siempre.
    reservado, reservado_vivo, por_surtir = await _reservar_inventario(pedido_por_producto)
    order.backorder = bool(por_surtir)
    order.backorder_items = por_surtir
    order.stock_taken = {clave: n for clave, n in reservado}
    # ⛔ Y LOS PUNTOS SE APARTAN AQUÍ MISMO, igual que las piezas y por lo mismo: el saldo
    # se leyó arriba y se restaba hasta después de grabar, así que dos pedidos a la vez
    # gastaban los MISMOS puntos (ver `_apartar_puntos`).
    if points_used and not await _apartar_puntos(user['id'], points_used):
        await _devolver_reserva(reservado)
        await _devolver_reserva_viva(reservado_vivo)
        raise HTTPException(
            status_code=409,
            detail='Tus puntos cambiaron mientras comprabas. Vuelve a intentarlo.')
    # ⛔ Y EL CUPÓN SE QUEMA AQUÍ MISMO, por lo mismo que las piezas y los puntos: se
    # miraba al principio del checkout y se marcaba usado hasta el final, así que dos
    # pedidos a la vez usaban el MISMO cupón de un solo uso (ver `_apartar_cupon`).
    if not await _apartar_cupon(coupon, order.order_number):
        await _devolver_reserva(reservado)
        await _devolver_reserva_viva(reservado_vivo)
        await _devolver_puntos(user['id'] if user else None, points_used)
        raise HTTPException(
            status_code=409,
            detail='Ese cupón acaba de usarse en otra compra. Vuelve a intentarlo.')
    try:
        await db.orders.insert_one(order.model_dump())
    except Exception:
        await _devolver_reserva(reservado)      # sin pedido no hay nada que apartar
        await _devolver_reserva_viva(reservado_vivo)
        await _devolver_puntos(user['id'] if user else None, points_used)
        await _devolver_cupon(coupon)
        raise
    # Ese carrito SI se cerro: su intento deja de estar pendiente y la IA ya no le escribe.
    asyncio.create_task(_cerrar_intentos(payload.customer.email))
    # Con sesion iniciada, estos datos quedan como los suyos para la proxima compra.
    await _recordar_datos_de_compra(user, payload.customer)
    # El cupón YA se quemó arriba, apartado en un solo paso condicionado
    # (`_apartar_cupon`). Aquí se marcaba con un `$set` a secas y SIN condición: ése era
    # el canje que dos pedidos simultáneos podían hacer dos veces con el mismo cupón.
    # LA CAMPANITA: el admin y quien ganó comisión se enteran DENTRO de la app, no
    # sólo por correo (Christián, 2026-07-30).
    await avisar_de_la_venta(order.model_dump(), commissions)
    if points_used:
        # El saldo YA se restó arriba, apartado en un solo paso condicionado
        # (`_apartar_puntos`). Aquí solo queda el asiento en la bitácora. Se restaba
        # también aquí, con un `$inc` a secas y sin condición: ése era el canje que dos
        # pedidos simultáneos podían hacer dos veces con los mismos puntos.
        await _points_entry(user['id'], order.model_dump(), 'redeem', -points_used)
    # LOS DOS INVENTARIOS YA BAJARON, arriba y antes de grabar, por el MISMO número y
    # agrupados por producto (`_reservar_inventario`). Aquí no se vuelve a restar nada:
    #   · el catálogo se restaba renglón por renglón buscando por `id` O `sku`, que junta
    #     lo que la revisión separaba — las mismas piezas descontadas dos veces;
    #   · el inventario vivo se restaba AQUÍ, ya grabado el pedido, y si no alcanzaba lo
    #     dejaba en 0 con una advertencia: se cobraba lo que no existe y nadie se enteraba.
    # La devolución (`restore_order_stock`) mueve los dos, busca igual y devuelve lo que
    # de verdad se apartó (`stock_taken`): cuando cada lado hacía lo suyo, cada ciclo de
    # pedido y cancelación INFLABA el inventario (Orexin A en 43 con 40, el 2026-07-27).
    # Confirmacion por correo, en segundo plano: la compra no debe quedarse
    # esperando al proveedor de correo ni fallar si esta caido.
    email_order = order.model_dump()
    if payload.payment_method == 'spei':
        email_order['spei'] = spei_details()   # la CLABE también va en el correo
    asyncio.create_task(send_order_email(email_order, user.get('language') if user else None))
    # Y el aviso interno: Christián necesita saber QUÉ PREPARAR, sobre todo si el pedido
    # trae piezas que hay que mandar pedir. En segundo plano como el del cliente: el
    # checkout no se cae porque el correo no salga. Y va con A QUIÉN COMPRARLE pegado:
    # el nombre y el teléfono del proveedor más barato de cada renglón sobre pedido.
    asyncio.create_task(_avisar_de_la_compra(email_order, 'nuevo'))
    # La respuesta del checkout va al navegador del CLIENTE: sale sin quién lo
    # refirió ni el reparto de comisiones (ver `pedido_para_el_cliente`).
    result = pedido_para_el_cliente(clean(order.model_dump()))
    # Cripto: creamos la factura del proveedor encendido y devolvemos su enlace.
    # El pedido queda 'pendiente' hasta que su webhook confirme que llegó el
    # dinero. NOWPayments primero (más simple); BTCPay como respaldo.
    # Tarjeta: se manda al cliente a la pagina de Mercado Pago. Los datos de la
    # tarjeta NUNCA pasan por nuestro servidor. El pedido queda 'pendiente' hasta
    # que su webhook confirme que el dinero entro.
    if payload.payment_method in ('tarjeta', 'oxxo'):
        order_url = f"{SITE_URL}/pedido/{order.order_number}"
        try:
            pref = mercadopago.create_preference(
                order.order_number,
                [it.model_dump() for it in payload.items],
                total,
                payer_email=payload.customer.email or '',
                success_url=order_url,
                failure_url=f"{SITE_URL}/carrito",
                webhook_url=f"{API_BASE_URL}/api/payments/mercadopago/webhook",
                metodo=payload.payment_method,
            )
            await db.orders.update_one(
                {'id': order.id},
                {'$set': {'card_preference_id': pref['preference_id'], 'card_provider': 'mercadopago'}})
            result['card_checkout_url'] = pref['checkout_url']
        except Exception:
            logger.exception('MercadoPago preference failed for %s', order.order_number)
    if payload.payment_method == 'cripto':
        order_url = f"{SITE_URL}/pedido/{order.order_number}"
        try:
            if nowpayments.enabled():
                inv = nowpayments.create_invoice(
                    order.order_number, total,
                    success_url=order_url, cancel_url=f"{SITE_URL}/carrito",
                    ipn_url=f"{API_BASE_URL}/api/payments/nowpayments/webhook",
                )
                await db.orders.update_one({'id': order.id}, {'$set': {'crypto_invoice_id': inv['invoice_id'], 'crypto_provider': 'nowpayments'}})
                result['crypto_checkout_url'] = inv['checkout_url']
            elif btcpay.enabled():
                inv = btcpay.create_invoice(
                    order.order_number, total,
                    redirect_url=order_url, buyer_email=payload.customer.email or '',
                )
                await db.orders.update_one({'id': order.id}, {'$set': {'crypto_invoice_id': inv['invoice_id'], 'crypto_provider': 'btcpay'}})
                result['crypto_checkout_url'] = inv['checkout_url']
        except Exception:
            logger.exception('Crypto invoice failed for %s', order.order_number)
    return result


async def _confirm_paid_order(order_number: str):
    """Marca pagado un pedido y deposita puntos. Idempotente.

    La usan cripto (NOWPayments, BTCPay) y tarjeta (Mercado Pago): el pedido solo
    pasa a 'confirmado' cuando el proveedor AVISA que el dinero llego, nunca
    cuando el cliente vuelve al sitio — la URL de regreso se puede teclear a mano.
    """
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if order and order.get('status') == 'pendiente':
        # ⛔ `paid: True` VA AQUÍ, no sólo `paid_at`. Los pedidos nacen con `paid: False`
        # (es el default del modelo), así que sin esta línea una tarjeta o un pago en
        # cripto REALMENTE COBRADO nunca contaría como ingreso: el tablero se iría a
        # cero al revés. Es el espejo del bug de Alanís.
        await db.orders.update_one(
            {'id': order['id']},
            {'$set': {'status': 'confirmado', 'paid': True, 'paid_at': now_iso()}})
        fresh = await db.orders.find_one({'id': order['id']}, {'_id': 0})
        await award_order_points(fresh)
        asyncio.create_task(send_payment_confirmed_email(fresh))
        # Segundo aviso a Christián: el primero dice qué se va a necesitar, éste dice que
        # ya se puede mandar. Con uno solo, o se prepara mercancía que nadie pagó o se
        # entera tarde de que ya puede salir.
        asyncio.create_task(_avisar_de_la_compra(fresh, 'pagado'))
        # La guía se compra sola en cuanto entra el dinero (tarjeta, OXXO, cripto).
        # En segundo plano: el webhook de la pasarela no debe quedarse esperando a
        # la paquetería — si tarda o falla, el pago ya quedó confirmado igual.
        asyncio.create_task(comprar_guia_del_pedido(fresh))
        # Y se le avisa a Meta que ENTRÓ EL DINERO (Conversions API). Sin esto,
        # las compras que llegan sin cookie —las de WhatsApp— Meta no las ve, y
        # una campaña que no ve compras no puede optimizar a Compras. En segundo
        # plano y a prueba de fallos: medir nunca debe tumbar un webhook de pago.
        asyncio.create_task(meta_capi.enviar_compra(fresh))


@api_router.get('/payments/config')
async def payments_config():
    """El checkout pregunta qué métodos están encendidos hoy, y si el pedido cobra
    envío (para que el carrito enseñe el mismo número que se cobra).

    `shipping_charged` es la llave que manda: con ella apagada el sitio no pinta
    ningún cargo de envío. Los otros números siguen viajando porque describen
    la regla dormida, no lo que se cobra hoy.

    Desde el 2026-07-31 viajan también el tope (5%) y el costo estimado de la guía:
    sin ellos el carrito no puede pintar el cobro PARCIAL que hay entre la compra
    mínima y el punto donde el envío sale gratis de verdad — y un carrito que enseña
    $0 donde la caja cobra $100 ya costó dinero antes. El número final lo sigue
    poniendo el servidor al crear el pedido; esto es sólo para que la pantalla no
    mienta mientras tanto."""
    return {'crypto_enabled': crypto_enabled(),
            'card_enabled': mercadopago.enabled(),
            'oxxo_enabled': mercadopago.enabled(),   # viaja por la misma pasarela
            'shipping_charged': COBRAR_ENVIO,
            'shipping_flat': SHIPPING_FLAT,
            'free_shipping_from': FREE_SHIPPING_FROM,
            'shipping_cap_rate': TOPE_ENVIO_SOBRE_COMPRA,
            'shipping_cost_estimate': COSTO_GUIA_ESTIMADO,
            # Cotización real por CP y peso (Skydropx). Apagada: el checkout ni
            # pregunta y la pantalla se ve EXACTAMENTE como hoy.
            'shipping_quote_enabled': envio_se_cotiza()}


@api_router.post('/payments/nowpayments/webhook')
async def nowpayments_webhook(request: Request):
    """NOWPayments avisa aquí (IPN). Verificamos la firma HMAC-SHA512 y, si el
    pago quedó 'finished', confirmamos el pedido. Nunca confía sin firma válida."""
    raw = await request.body()
    if not nowpayments.verify_ipn(raw, request.headers.get('x-nowpayments-sig', '')):
        raise HTTPException(status_code=401, detail='firma invalida')
    event = json.loads(raw.decode() or '{}')
    if event.get('payment_status') in nowpayments.SETTLED_STATUSES:
        await _confirm_paid_order(event.get('order_id') or '')
    return {'ok': True}


@api_router.post('/payments/mercadopago/webhook')
async def mercadopago_webhook(request: Request):
    """Mercado Pago avisa aqui cuando algo pasa con un pago.

    Tres candados, en este orden:
      1. Solo avisos de tipo 'payment' (tambien manda de merchant_order y demas).
      2. La FIRMA `x-signature` tiene que cuadrar. Sin secreto configurado no pasa
         nada — igual que en las otras pasarelas.
      3. El estado NO se cree del cuerpo: se le pregunta a la API de Mercado Pago
         con el id del pago. El cuerpo del aviso se puede falsificar; la respuesta
         de su API, no.

    Y el monto se compara contra el total del pedido: un pago aprobado por menos
    de lo que costaba NO confirma nada.
    """
    raw = await request.body()
    try:
        body = json.loads(raw.decode() or '{}')
    except ValueError:
        body = {}
    query = dict(request.query_params)
    if not mercadopago.is_payment_event(query, body):
        return {'ok': True, 'ignorado': 'no es un aviso de pago'}

    payment_id = mercadopago.extract_payment_id(query, body)
    if not payment_id:
        return {'ok': True, 'ignorado': 'sin id de pago'}

    if not mercadopago.verify_webhook(request.headers.get('x-signature', ''),
                                      request.headers.get('x-request-id', ''),
                                      payment_id):
        raise HTTPException(status_code=401, detail='firma invalida')

    try:
        pago = mercadopago.get_payment(payment_id)
    except Exception:
        logger.exception('MercadoPago: no se pudo consultar el pago %s', payment_id)
        raise HTTPException(status_code=502, detail='no se pudo verificar el pago')

    if pago.get('status') not in mercadopago.SETTLED_STATUSES:
        return {'ok': True, 'estado': pago.get('status')}

    numero = (pago.get('external_reference')
              or (pago.get('metadata') or {}).get('order_number') or '')
    if not numero:
        return {'ok': True, 'ignorado': 'el pago no trae numero de pedido'}

    # Que lo pagado alcance lo que costaba. Un centavo menos y no se confirma.
    order = await db.orders.find_one({'order_number': numero}, {'_id': 0, 'total': 1})
    pagado = float(pago.get('transaction_amount') or 0)
    if order and pagado + 0.01 < float(order.get('total') or 0):
        logger.warning('MercadoPago: %s pago %s de %s — no se confirma',
                       numero, pagado, order.get('total'))
        return {'ok': True, 'ignorado': 'pago incompleto'}

    await db.orders.update_one({'order_number': numero},
                               {'$set': {'card_payment_id': str(payment_id)}})
    await _confirm_paid_order(numero)
    return {'ok': True}


@api_router.post('/payments/btcpay/webhook')
async def btcpay_webhook(request: Request):
    """BTCPay avisa aquí cuando una factura se paga. Verificamos la firma HMAC
    y, si la factura quedó liquidada, confirmamos el pedido (lo que deposita los
    puntos de lealtad). Nunca confía en el cuerpo sin firma válida."""
    raw = await request.body()
    if not btcpay.verify_webhook(raw, request.headers.get('BTCPay-Sig', '')):
        raise HTTPException(status_code=401, detail='firma invalida')
    event = json.loads(raw.decode() or '{}')
    if event.get('type') not in btcpay.SETTLED_EVENTS:
        return {'ok': True}
    await _confirm_paid_order((event.get('metadata') or {}).get('orderId') or '')
    return {'ok': True}


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
        # Nunca en negativo. La UI ya lo acota, pero un botón no es una compuerta: la
        # ruta se puede llamar a mano, y un inventario vivo negativo se muestra en la
        # ficha del producto como si fuera un dato.
        update['qty'] = max(0, int(payload['qty']))
    if 'in_hand' in payload:
        update['in_hand'] = bool(payload['in_hand'])
    await db.stock.update_one({'key': key}, {'$set': update}, upsert=True)
    row = await db.stock.find_one({'key': key}, {'_id': 0})
    return row


# ⛔ EL PEDIDO QUE VE EL CLIENTE VA SIN RASTRO DEL DISTRIBUIDOR
# (Christián, 2026-07-31). El pedido guarda quién lo refirió y cómo se repartió la
# comisión —`referred_by`, `commission`, `commissions`— y esas tres rutas devolvían
# el documento COMPLETO, tal cual sale de la base. Con eso, cualquiera que abriera
# la consola del navegador después de comprar veía el id del distribuidor y cuánto
# ganó; y `/orders/{numero}` ni siquiera pide sesión.
#
# El candado va aquí, en el servidor, y no escondiéndolo con CSS: lo que no viaja
# no se puede leer. El distribuidor y el admin siguen viendo TODO lo suyo por sus
# propias rutas (`/distributor/orders`, `/admin/orders`), que no pasan por aquí.
CAMPOS_DEL_DISTRIBUIDOR = ('referred_by', 'commission', 'commissions')


def pedido_para_el_cliente(order):
    """El pedido tal como puede verlo quien compró: sin quién lo refirió ni cuánto
    ganó nadie. Devuelve una copia; el documento original no se toca."""
    if not order:
        return order
    limpio = {k: v for k, v in order.items() if k not in CAMPOS_DEL_DISTRIBUIDOR}
    limpio.pop('_id', None)
    return limpio


@api_router.get('/orders/me')
async def my_orders(user=Depends(get_current_user)):
    orders = await db.orders.find({'user_id': user['id']}, {'_id': 0}).to_list(200)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return [pedido_para_el_cliente(o) for o in orders]


def spei_details():
    """Datos de la cuenta SPEI donde el cliente deposita. Config por env; NUNCA
    en el repo. Se muestran solo en un pedido SPEI ya hecho, no en páginas públicas."""
    clabe = os.environ.get('SPEI_CLABE', '')
    if not clabe:
        return None
    return {
        'beneficiary': os.environ.get('SPEI_BENEFICIARY', 'Exygen Labs'),
        'bank': os.environ.get('SPEI_BANK', ''),
        'clabe': clabe,
    }


@api_router.get('/orders/{order_number}')
async def get_order(order_number: str):
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    # Esta ruta NO PIDE SESIÓN (el que compró como invitado no tiene cuenta), así
    # que es la más expuesta de las tres: sale limpia de rastro del distribuidor.
    order = pedido_para_el_cliente(order)
    # Solo un pedido SPEI (y solo ese) lleva la CLABE; la referencia es el número de pedido.
    if (order.get('payment_method') or '') == 'spei':
        order['spei'] = spei_details()
    return order


# Comprobante de transferencia SPEI que sube el cliente (para que el admin lo
# muestre a quien administra la cuenta). Se guarda en Mongo (persiste), no en disco.
RECEIPT_MIME = {'application/pdf', 'image/jpeg', 'image/png', 'image/webp'}
RECEIPT_MAX_BYTES = 8 * 1024 * 1024


@api_router.post('/orders/{order_number}/spei-receipt')
async def upload_spei_receipt(order_number: str, file: UploadFile = File(...)):
    """El cliente sube su comprobante. Permitido por número de pedido (el que
    compró como invitado no tiene sesión). Se valida tipo y tamaño."""
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if (order.get('payment_method') or '') != 'spei':
        raise HTTPException(status_code=400, detail='Este pedido no es por transferencia SPEI')
    if file.content_type not in RECEIPT_MIME:
        raise HTTPException(status_code=400, detail='Solo aceptamos PDF, JPG, PNG o WEBP')
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail='El archivo esta vacio')
    if len(data) > RECEIPT_MAX_BYTES:
        raise HTTPException(status_code=400, detail='El archivo pesa mas de 8 MB')
    await db.spei_receipts.update_one(
        {'order_id': order['id']},
        {'$set': {
            'order_id': order['id'], 'order_number': order_number,
            'filename': (file.filename or 'comprobante')[:120],
            'content_type': file.content_type,
            'data': base64.b64encode(data).decode(),
            'uploaded_at': now_iso(),
        }},
        upsert=True,
    )
    # Marcamos que el cliente ya reportó su pago (el admin aún debe verificarlo).
    await db.orders.update_one({'id': order['id']}, {'$set': {'spei_receipt_at': now_iso()}})
    return {'ok': True}


@api_router.get('/admin/orders/{order_id}/spei-receipt')
async def download_spei_receipt(order_id: str, admin=Depends(get_current_admin)):
    """Solo el admin descarga el comprobante (para mostrarlo a la cuenta receptora)."""
    rec = await db.spei_receipts.find_one({'order_id': order_id}, {'_id': 0})
    if not rec:
        raise HTTPException(status_code=404, detail='Sin comprobante')
    from fastapi.responses import Response
    return Response(
        content=base64.b64decode(rec['data']),
        media_type=rec.get('content_type', 'application/octet-stream'),
        headers={'Content-Disposition': f'inline; filename="{rec.get("filename", "comprobante")}"'},
    )


# ----------------- Admin: Orders -----------------
@api_router.get('/admin/orders')
async def admin_orders(archivados: bool = False, admin=Depends(get_current_admin)):
    """Los pedidos. Por omisión SIN los archivados; con `?archivados=true`, sólo esos.

    Archivar no borra: sólo los quita de la vista de todos los días. Los pedidos viejos
    y las pruebas ensucian la lista, pero borrarlos es irreversible y a veces hay que
    volver a mirarlos."""
    filtro = {'archived': True} if archivados else {'archived': {'$ne': True}}
    orders = await db.orders.find(filtro, {'_id': 0}).to_list(500)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    # A QUIÉN COMPRARLE, resuelto por el servidor y no por el Panel: el mapa de
    # proveedores es privado y no puede viajar entero al navegador. Se pide UNA vez y
    # se reparte entre los pedidos que traen piezas sobre pedido.
    mapa = await _mapa_de_proveedores()
    # `pagado` YA RESUELTO por el servidor. El campo `paid` no existe en los pedidos
    # viejos, y si el panel tuviera que adivinarlo tendríamos dos reglas de qué es un
    # ingreso —una aquí y otra en JavaScript— que tarde o temprano se separan. La
    # inferencia se hace en un solo lado (cobrado.py) y viaja resuelta.
    return [{**o, 'pagado': esta_pagado(o),
             **({'backorder_items': _con_proveedor(o['backorder_items'], mapa)}
                if o.get('backorder_items') else {})}
            for o in orders]


# `ESTADOS_PAGADOS` y `esta_pagado` viven en cobrado.py y se importan arriba: los
# reportes de marketing y de pirámide también los necesitan y no pueden importar
# server.py. Un pedido en esos estados no se borra en masa por accidente: es una venta
# real, con dinero que entró y contabilidad detrás.


class MarcaDePago(BaseModel):
    pagado: bool


@api_router.put('/admin/orders/{order_id}/pago')
async def admin_marcar_pago(order_id: str, payload: MarcaDePago,
                            admin=Depends(get_current_admin)):
    """Marca un pedido como PAGADO o como no pagado, sin tocar el estado de entrega."""
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    cambio = {'paid': payload.pagado,
              'paid_at': datetime.now(timezone.utc).isoformat() if payload.pagado else None}
    await db.orders.update_one({'id': order_id}, {'$set': cambio})
    # Los PUNTOS siguen al dinero, no a la mercancía. Un pedido fiado no los genera
    # (ver `award_order_points`), así que se depositan justo aquí, cuando por fin se
    # cobra; y si el admin desmarca el pago, se retiran. Ambos caminos son idempotentes.
    fresco = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if payload.pagado:
        await award_order_points(fresco)
    else:
        await retirar_puntos_ganados(fresco)
    logger.info('Admin %s marcó el pedido %s como %s', admin.get('email'),
                order.get('order_number'), 'PAGADO' if payload.pagado else 'NO pagado')
    return {'order_number': order.get('order_number'), **cambio}


class LoteDePedidos(BaseModel):
    ids: list[str]
    accion: str                      # 'borrar' | 'archivar' | 'desarchivar'
    forzar: bool = False             # sólo para borrar algo ya pagado


@api_router.post('/admin/orders/lote')
async def admin_orders_lote(payload: LoteDePedidos, admin=Depends(get_current_admin)):
    """Archiva, desarchiva o BORRA varios pedidos de un golpe.

    ⛔ EL CANDADO QUE IMPORTA: `borrar` se NIEGA a tocar un pedido ya pagado
    (confirmado/enviado/entregado) a menos que venga `forzar`. La lista del Panel tiene
    'seleccionar todo', y entre doce pedidos de prueba vive UNA venta real —
    la de Paz Cambray. Un clic distraído en 'seleccionar todo' + 'borrar' se la llevaba
    junto con la basura, sin deshacer. (Christián, 2026-07-29)

    Borrar usa exactamente el mismo camino que el borrado de uno solo: devuelve los
    puntos que la orden depositó o canjeó, regresa las piezas al inventario y limpia su
    bitácora de puntos. No es un `delete_many` a secas — eso dejaría puntos regalados y
    el inventario corto.
    """
    if payload.accion not in ('borrar', 'archivar', 'desarchivar'):
        raise HTTPException(status_code=400, detail='Acción desconocida')
    if not payload.ids:
        raise HTTPException(status_code=400, detail='No mandaste ningún pedido')

    hechos, protegidos, faltantes = [], [], []
    for oid in payload.ids:
        order = await db.orders.find_one({'id': oid}, {'_id': 0})
        if not order:
            faltantes.append(oid)
            continue
        if payload.accion == 'borrar':
            if order.get('status') in ESTADOS_PAGADOS and not payload.forzar:
                protegidos.append({'order_number': order.get('order_number'),
                                   'status': order.get('status'),
                                   'cliente': (order.get('customer') or {}).get('full_name', ''),
                                   'total': order.get('total', 0)})
                continue
            if order.get('status') != 'cancelado':
                await revoke_order_points(order)
            await restore_order_stock(order)
            await db.orders.delete_one({'id': oid})
            await db.points.delete_many({'order_id': oid})
            logger.warning('Admin %s BORRÓ EN LOTE el pedido %s (%s, $%s)',
                           admin.get('email'), order.get('order_number'),
                           (order.get('customer') or {}).get('full_name'), order.get('total'))
        else:
            await db.orders.update_one(
                {'id': oid}, {'$set': {'archived': payload.accion == 'archivar'}})
        hechos.append(order.get('order_number'))

    return {'accion': payload.accion, 'hechos': len(hechos), 'numeros': hechos,
            'protegidos': protegidos, 'no_encontrados': faltantes}


@api_router.put('/admin/orders/{order_id}/status')
async def update_order_status(order_id: str, payload: OrderStatusUpdate, admin=Depends(get_current_admin)):
    prev = await db.orders.find_one({'id': order_id}, {'_id': 0, 'status': 1, 'paid': 1,
                                                       'paid_at': 1})
    if not prev:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    update = {'status': payload.status}
    if payload.status == 'enviado':
        update['shipped_at'] = now_iso()
    elif payload.status == 'entregado':
        update['delivered_at'] = now_iso()
    # ⛔ 'confirmado' ES EL PASO DEL DINERO; 'enviado' y 'entregado' NO.
    # Marcar confirmado es literalmente lo que hace Christián cuando ve el depósito
    # SPEI en el banco (y el cliente recibe el correo "Pago confirmado"), así que ese
    # paso —y sólo ese— marca el pedido como cobrado. Mover la mercancía no cobra
    # nada: por eso un pedido puede quedar ENTREGADO y seguir debiendo.
    if payload.status == 'confirmado':
        update['paid'] = True
        if not prev.get('paid_at'):
            update['paid_at'] = now_iso()
    await db.orders.update_one({'id': order_id}, {'$set': update})
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    # Lealtad: pago verificado deposita puntos; cancelacion los revierte.
    if payload.status in loyalty.PAID_STATUSES:
        # Si venía de 'cancelado', lo canjeado se vuelve a cobrar ANTES de depositar
        # nada nuevo: si no, cancelar y reconfirmar regala los puntos canjeados.
        await recobrar_puntos_canjeados(order)
        await award_order_points(order)
    elif payload.status == 'cancelado':
        await revoke_order_points(order)
        await restore_order_stock(order)     # lo cancelado regresa al inventario
    # Aviso de pago confirmado al cliente, solo al ENTRAR a 'confirmado'.
    num = order.get('order_number')
    if payload.status == 'confirmado' and (prev.get('status') or '') != 'confirmado':
        lang = None
        if order.get('user_id'):
            u = await db.users.find_one({'id': order['user_id']}, {'_id': 0, 'language': 1})
            lang = (u or {}).get('language')
        asyncio.create_task(send_payment_confirmed_email(order, lang))
        await notify(order.get('user_id'), 'payment_confirmed', 'Pago confirmado',
                     f'Confirmamos el pago de tu pedido {num}. ¡Gracias!', link=f'/pedido/{num}')
        # SPEI llega por aquí: el admin verifica el depósito y marca 'confirmado'.
        # Es el cuarto método de pago, y compra su guía igual que los otros tres.
        asyncio.create_task(comprar_guia_del_pedido(order))
    # Notificación de entrega, solo al ENTRAR a 'entregado'.
    if payload.status == 'entregado' and (prev.get('status') or '') != 'entregado':
        await notify(order.get('user_id'), 'order_delivered', 'Pedido entregado',
                     f'Tu pedido {num} fue entregado. ¡Disfrútalo!', link=f'/pedido/{num}')
    return await db.orders.find_one({'id': order_id}, {'_id': 0})


CARRIER_TRACKING_URLS = {
    'fedex': 'https://www.fedex.com/fedextrack/?trknbr={n}',
    'dhl': 'https://www.dhl.com/mx-es/home/rastreo.html?tracking-id={n}',
    'estafeta': 'https://www.estafeta.com/Herramientas/Rastreo?wayBill={n}',
    'ups': 'https://www.ups.com/track?tracknum={n}',
    'paquetexpress': 'https://www.paquetexpress.com.mx/rastreo?guia={n}',
    'paqueteexpress': 'https://www.paquetexpress.com.mx/rastreo?guia={n}',
    'redpack': 'https://www.redpack.com.mx/es/rastreo/?guias={n}',
    'correosdemexico': 'https://www.correosdemexico.gob.mx/SSLServicios/SeguimientoEnvio/Seguimiento.aspx?guia={n}',
}


def build_tracking_url(carrier: str, number: str) -> str:
    """URL de rastreo del transportista. Vacío si no lo conocemos.

    Normalizamos espacios y acentos porque el admin escribe el nombre a mano.
    """
    key = (carrier or '').strip().lower().replace(' ', '')
    key = key.translate(str.maketrans('áéíóúü', 'aeiouu'))
    tpl = CARRIER_TRACKING_URLS.get(key)
    return tpl.format(n=number.strip()) if tpl and number else ''


async def _guardar_envio(order: dict, payload: OrderShippingUpdate, *,
                         permitir_status: bool = True) -> dict:
    """Escribe guía/paquetería en un pedido y avisa al cliente si la guía es nueva.

    ⛔ ES EL ÚNICO LUGAR QUE ESCRIBE ENVÍO. Lo comparten el admin y el distribuidor
    a propósito: el candado de QUIÉN puede tocar CUÁL pedido vive en cada endpoint,
    pero lo que se guarda y el correo que sale son exactamente lo mismo. Si un día
    se arregla algo aquí, queda arreglado para los dos.

    `permitir_status=False` (el distribuidor) ignora el `status` que venga en el
    cuerpo: él captura la guía que ya tiene, no mueve el pedido a mano. El paso
    automático a 'enviado' sí ocurre, porque capturar una guía ES que ya salió.
    """
    order_id = order['id']
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
    status_pedido = payload.status if permitir_status else None
    if status_pedido:
        update['status'] = status_pedido
        if status_pedido == 'enviado' and not order.get('shipped_at'):
            update['shipped_at'] = now_iso()
        elif status_pedido == 'entregado' and not order.get('delivered_at'):
            update['delivered_at'] = now_iso()
    # Capturar una guía implica que ya salió: si seguía pendiente, pasa a enviado.
    if number and not status_pedido and order.get('status') in ('pendiente', 'confirmado'):
        update['status'] = 'enviado'
        update.setdefault('shipped_at', now_iso())
    if update:
        await db.orders.update_one({'id': order_id}, {'$set': update})
    result = await db.orders.find_one({'id': order_id}, {'_id': 0})
    # Si en esta captura APARECIÓ una guía que antes no existía, el cliente se entera.
    # `dedup` en la notificación impide que reeditar el número mande dos correos.
    if number and not (order.get('tracking_number') or ''):
        await avisar_del_envio(result)
    if result.get('status') in loyalty.PAID_STATUSES:
        # Los puntos tienen su propio candado de cobro dentro (`award_order_points`):
        # mover la mercancía no los libera si el pedido sigue sin pagarse.
        await award_order_points(result)
        result = await db.orders.find_one({'id': order_id}, {'_id': 0})
    return result


@api_router.put('/admin/orders/{order_id}/shipping')
async def update_order_shipping(order_id: str, payload: OrderShippingUpdate, admin=Depends(get_current_admin)):
    """Captura guía y transportista. Si no dan URL, la armamos con la del transportista."""
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    return await _guardar_envio(order, payload)


@api_router.get('/admin/stats')
async def admin_stats(admin=Depends(get_current_admin)):
    total_products = await db.products.count_documents({})
    total_orders = await db.orders.count_documents({})
    total_users = await db.users.count_documents({'role': 'user'})
    orders = await db.orders.find(
        {}, {'_id': 0, 'total': 1, 'status': 1, 'paid': 1,
             'backorder_items': 1, 'archived': 1}).to_list(1000)
    # El ingreso son los pedidos COBRADOS, no los entregados: ver `esta_pagado`.
    revenue = sum(o.get('total', 0) for o in orders if esta_pagado(o))
    por_cobrar = sum(o.get('total', 0) for o in orders
                     if o.get('status') != 'cancelado' and not esta_pagado(o))
    pending = sum(1 for o in orders if o.get('status') == 'pendiente')
    # ⛔ LO QUE HAY QUE MANDAR PEDIR, EN LA PORTADA DEL PANEL. Vivía sólo en el correo
    # y en la ficha de cada pedido: para enterarse había que ir abriendo pedidos uno por
    # uno. Se cuentan los VIVOS (ni cancelados ni archivados): un pedido cancelado no
    # necesita que se le compre nada.
    vivos_con_backorder = [o for o in orders
                           if o.get('backorder_items') and not o.get('archived')
                           and o.get('status') != 'cancelado']
    piezas_por_pedir = sum(int(b.get('por_surtir', 0) or 0)
                           for o in vivos_con_backorder
                           for b in (o.get('backorder_items') or []))
    return {
        # Cuántos pedidos esperan mercancía y cuántas piezas hay que comprar.
        'pedidos_por_surtir': len(vivos_con_backorder),
        'piezas_por_pedir': piezas_por_pedir,
        'total_products': total_products,
        'total_orders': total_orders,
        'total_users': total_users,
        'revenue': revenue,
        # Entregado o en camino pero SIN cobrar. Sin este número, un pedido fiado
        # desaparece del tablero: ni suma en ingresos ni aparece en ningún lado.
        'por_cobrar': por_cobrar,
        'pending_orders': pending,
    }


# ----------------- Admin: Motor de Precios -----------------
# La foto resumida del motor, para verla en el Panel sin abrir una terminal.
#
# ⛔ ESTO NUNCA PUEDE SER PÚBLICO. Lleva el COSTO de cada producto, el nombre de cada
# proveedor, el ROI y el margen. Nació como un archivo en `public/` del sitio y así
# habría quedado servido en abierto en exygenlabs.com — cualquiera con el enlace veía
# a cuánto compramos y a quién. Por eso vive aquí, detrás de sesión de admin, y por eso
# el sitio ya NO lo lleva como archivo suelto.
#
# La foto la calcula la Mac de Christian (`pricing-system/publicar_dashboard_precios.py`)
# porque la base del motor vive ahí, no en el servidor. El backend sólo la guarda y la
# entrega; no la interpreta ni la recalcula.
# Cuántas horas vale una foto del motor antes de considerarse vieja. Es un día
# hábil: los precios se mueven a diario (la competencia se refresca a diario y la
# maestra con ella), así que una foto de anteayer describe un catálogo que ya no
# existe. No borra nada — solo deja de presentarse como si fuera de hoy.
MOTOR_PRECIOS_VIGENCIA_HORAS = 24


def _frescura_de_la_foto(subido: str) -> dict:
    """Qué tan vieja es la foto del motor, calculado por el SERVIDOR.

    ⛔ La antigüedad NO se cree de `generado`: ese campo lo escribe la Mac de
    Christian con su reloj local y sin zona horaria, así que un reloj atrasado (o un
    script que se corre sin volver a construir la base) hace pasar por nueva una foto
    vieja. Lo único que el servidor sabe de verdad es CUÁNDO LA RECIBIÓ, y de ahí
    sale esto. El Panel enseñaba datos que ya no correspondían a la base y nada lo
    decía (2026-07-28: la foto era de las 16:46 y la base de las 18:24).
    """
    try:
        cuando = datetime.fromisoformat(subido)
    except (TypeError, ValueError):
        return {'horas': None, 'vencida': True, 'subido': subido or ''}
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    horas = (datetime.now(timezone.utc) - cuando).total_seconds() / 3600
    return {'horas': round(max(0.0, horas), 1),
            'vencida': horas > MOTOR_PRECIOS_VIGENCIA_HORAS,
            'vigencia_horas': MOTOR_PRECIOS_VIGENCIA_HORAS,
            'subido': subido}


@api_router.get('/admin/motor-precios')
async def admin_motor_precios(admin=Depends(get_current_admin)):
    doc = await db.app_data.find_one({'clave': 'motor_precios'}, {'_id': 0})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail='Todavía no se ha subido ninguna foto del motor de precios.')
    foto = dict(doc.get('valor') or {})
    # La frescura viaja SIEMPRE con la foto: un tablero que parece vivo y está viejo
    # es peor que no tener tablero.
    foto['frescura'] = _frescura_de_la_foto(doc.get('subido') or '')
    return foto


@api_router.put('/admin/motor-precios')
async def admin_motor_precios_guardar(payload: dict, admin=Depends(get_current_admin)):
    """Recibe la foto tal cual y la guarda. No la valida a fondo a propósito: quien la
    genera es el motor, y ponerle aquí una segunda opinión sobre su forma crea dos
    verdades sobre el mismo dato — el error que la base del motor vino a matar."""
    if not isinstance(payload, dict) or not payload.get('generado'):
        raise HTTPException(status_code=400,
                            detail='La foto viene sin fecha de generación.')
    await db.app_data.update_one(
        {'clave': 'motor_precios'},
        {'$set': {'clave': 'motor_precios', 'valor': payload,
                  'subido': datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return {'ok': True, 'generado': payload.get('generado')}


# ---------- A QUIÉN LE COMPRO: el proveedor más barato de cada producto ----------
#
# ⛔ ESTO NO SE PUBLICA NUNCA. Nombres de proveedores, teléfonos y costos: los dos
# extremos van detrás de sesión de admin, igual que la foto del motor de precios. La
# carpeta `public/` del sitio se sirve entera en exygenlabs.com y ahí no entra nada de
# esto. Lo genera `publicar_proveedores.py --subir` desde la Mac (la base del motor vive
# ahí, no en el servidor), por el mismo canal privado que ya usa el tablero de precios.
@api_router.get('/admin/proveedores')
async def admin_proveedores(admin=Depends(get_current_admin)):
    doc = await db.app_data.find_one({'clave': 'proveedores_por_producto'}, {'_id': 0})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail='Todavía no se ha subido la lista de proveedores. '
                   'Corre: python3 publicar_proveedores.py --subir')
    foto = dict(doc.get('valor') or {})
    foto['frescura'] = _frescura_de_la_foto(doc.get('subido') or '')
    return foto


@api_router.put('/admin/proveedores')
async def admin_proveedores_guardar(payload: dict, admin=Depends(get_current_admin)):
    """Recibe el mapa producto → proveedor más barato y lo guarda tal cual."""
    if not isinstance(payload, dict) or not payload.get('generado'):
        raise HTTPException(status_code=400,
                            detail='La lista viene sin fecha de generación.')
    await db.app_data.update_one(
        {'clave': 'proveedores_por_producto'},
        {'$set': {'clave': 'proveedores_por_producto', 'valor': payload,
                  'subido': datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return {'ok': True, 'generado': payload.get('generado'),
            'productos': len(payload.get('por_producto') or {})}


async def _mapa_de_proveedores():
    """{clave de producto: a quién comprarle}. Vacío si nunca se ha subido la lista."""
    doc = await db.app_data.find_one({'clave': 'proveedores_por_producto'}, {'_id': 0})
    return ((doc or {}).get('valor') or {}).get('por_producto') or {}


def _con_proveedor(backorder_items, mapa):
    """Le pega a cada renglón SOBRE PEDIDO el proveedor más barato y su teléfono.

    ⛔ EL HUECO SE ANUNCIA, NO SE CALLA. Un producto sin proveedor con precio en la base
    sale marcado (`sin_proveedor`) para que el aviso diga «sin proveedor registrado —
    revisar motor de precios». Callarlo deja a alguien buscando en el peor momento: con
    el pedido ya vendido y el cliente esperando.
    """
    out = []
    for b in (backorder_items or []):
        b = dict(b)
        # `product_id` aquí ya es el producto RESUELTO contra el catálogo, y el mapa
        # trae cada producto por su id Y por su SKU: cualquiera de los dos entra.
        info = mapa.get(str(b.get('product_id') or '')) or mapa.get(str(b.get('sku') or ''))
        if info and info.get('proveedor'):
            b['proveedor'] = info.get('proveedor')
            b['telefono'] = info.get('telefono') or ''
            b['costo_vial_usd'] = info.get('costo_vial_usd')
            b['whatsapp'] = info.get('whatsapp') or ''
            b['proveedor_verificado'] = bool(info.get('verificado'))
            b['sin_proveedor'] = False
        else:
            b['proveedor'] = None
            b['telefono'] = ''
            b['sin_proveedor'] = True
        out.append(b)
    return out


async def _pedido_con_proveedores(order):
    """El pedido con sus renglones sobre pedido ya resueltos a un proveedor."""
    if not order.get('backorder_items'):
        return order
    return dict(order, backorder_items=_con_proveedor(order['backorder_items'],
                                                      await _mapa_de_proveedores()))


async def _avisar_de_la_compra(order, momento):
    """El aviso interno, con A QUIÉN COMPRARLE ya pegado a cada renglón sobre pedido.

    Nunca revienta: se llama en segundo plano y una venta no se puede caer porque la
    lista de proveedores no esté subida. Sin lista, el correo sale como salía antes (y
    diciendo que no hay proveedor registrado), no deja de salir."""
    try:
        order = await _pedido_con_proveedores(order)
    except Exception:
        logger.exception('No pude resolver los proveedores del pedido %s',
                         order.get('order_number'))
    await send_purchase_alert(order, momento)


# Lo que Christian decide sobre los productos que le ofrecen y no vende.
#
# El botón NO publica nada. Guarda la decisión, y la Mac la aplica después pasando el
# producto por el motor de precios como cualquier otro. Es a propósito: el precio lo pone
# la fórmula (costo, competencia, piso de 5×), y un alta desde una pantalla se saltaría
# todo eso — que es exactamente como se acaba vendiendo algo por debajo del costo.
@api_router.get('/admin/motor-precios/decisiones')
async def admin_motor_decisiones(admin=Depends(get_current_admin)):
    doc = await db.app_data.find_one({'clave': 'motor_decisiones'}, {'_id': 0})
    return (doc or {}).get('valor') or {}


@api_router.put('/admin/motor-precios/decisiones/{llave}')
async def admin_motor_decidir(llave: str, payload: dict,
                              admin=Depends(get_current_admin)):
    """Aprueba o descarta UN producto de la lista de oportunidades.

    El veto se comprueba AQUÍ además de en la pantalla. La lista que ve el Panel ya viene
    filtrada, pero un botón no es una compuerta: cualquiera con sesión de admin puede
    llamar a esta ruta a mano, y lo que está en juego es dar de alta un esteroide
    anabólico o un medicamento regulado. Un control que sólo vive en el navegador no es
    un control."""
    decision = str((payload or {}).get('decision') or '').strip().lower()
    if decision not in ('aprobado', 'descartado', 'pendiente'):
        raise HTTPException(status_code=400,
                            detail='La decisión sólo puede ser aprobado, descartado o pendiente.')
    if decision == 'aprobado':
        foto = await db.app_data.find_one({'clave': 'motor_precios'}, {'_id': 0})
        vetados = {str(x).lower() for x in
                   (((foto or {}).get('valor') or {}).get('oportunidades') or {}).get('vetados', [])}
        if llave.lower() in vetados:
            raise HTTPException(
                status_code=409,
                detail='Ese producto está vetado (sustancia controlada o insumo): '
                       'no se puede dar de alta desde aquí.')
    doc = await db.app_data.find_one({'clave': 'motor_decisiones'}, {'_id': 0})
    valor = (doc or {}).get('valor') or {}
    if decision == 'pendiente':
        valor.pop(llave, None)
    else:
        valor[llave] = {
            'decision': decision,
            'nota': str((payload or {}).get('nota') or '')[:400],
            'quien': admin.get('email') or admin.get('id'),
            'cuando': datetime.now(timezone.utc).isoformat(),
            'aplicado': False,
        }
    await db.app_data.update_one({'clave': 'motor_decisiones'},
                                 {'$set': {'clave': 'motor_decisiones', 'valor': valor}},
                                 upsert=True)
    return {'ok': True, 'llave': llave, 'decision': decision}


# ----------------- Admin: Customers -----------------
@api_router.get('/admin/customers')
async def admin_customers(admin=Depends(get_current_admin)):
    """Todos los clientes con su historial de compra. Nunca expone password_hash."""
    users = await db.users.find({'role': 'user'}, {'_id': 0, 'password_hash': 0}).to_list(2000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(5000)
    by_user = {}
    for o in orders:
        if o.get('user_id'):
            by_user.setdefault(o['user_id'], []).append(o)
    out = []
    for u in users:
        uo = sorted(by_user.get(u['id'], []), key=lambda o: o.get('created_at', ''), reverse=True)
        valid = [o for o in uo if esta_vivo(o)]
        addresses, phones = [], []
        for o in uo:
            c = o.get('customer') or {}
            # El país solo se muestra cuando NO es México (el caso normal no estorba).
            country = c.get('country') if c.get('country') not in (None, '', 'MX') else None
            addr = ', '.join(x for x in [c.get('address'), c.get('city'), c.get('state'), c.get('postal_code'), country] if x)
            if addr and addr not in addresses:
                addresses.append(addr)
            if c.get('phone') and c['phone'] not in phones:
                phones.append(c['phone'])
        out.append({
            **u,
            'orders_count': len(uo),
            # Lo que este cliente REALMENTE PAGÓ, y aparte lo que debe. "Gastado" era
            # todo lo no cancelado, así que un cliente fiado se veía como el que mejor
            # paga — justo al revés de lo que hay que saber de él.
            'total_spent': sum(cobrado_de(o) for o in valid),
            'por_cobrar': sum(por_cobrar_de(o) for o in valid),
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
    """Ventas agregadas: por mes, por producto, por metodo de pago y por estado.

    ⛔ AQUÍ SEGUÍA VIVO EL BUG DE ALANÍS. `/admin/stats` ya separaba lo cobrado de lo
    fiado, pero esta pantalla —la gráfica de ventas por mes, el ticket promedio, el
    ranking de productos— filtraba sólo por "no cancelado", así que la venta ENTREGADA
    Y SIN PAGAR seguía pintada como ingreso ($7,204 en vez de $3,347). Todo el dinero
    de aquí pasa por `cobrado_de`; lo que se debe viaja aparte en `por_cobrar`.
    """
    orders = await db.orders.find({}, {'_id': 0}).to_list(10000)
    vivos = [o for o in orders if esta_vivo(o)]
    cobrados = solo_cobrados(vivos)
    by_month, by_pay, by_status, prod = {}, {}, {}, {}
    for o in orders:
        s = o.get('status', 'pendiente')
        by_status[s] = by_status.get(s, 0) + 1
    for o in vivos:
        month = (o.get('created_at') or '')[:7]
        e = by_month.setdefault(month, {'month': month, 'revenue': 0, 'por_cobrar': 0, 'orders': 0})
        # `orders` sigue contando TODO lo vivo: un pedido fiado sí es una venta hecha.
        # Lo que no es, es dinero en la cuenta — y eso es lo único que cambia de cajón.
        e['revenue'] += cobrado_de(o)
        e['por_cobrar'] += por_cobrar_de(o)
        e['orders'] += 1
    for o in cobrados:
        pm = o.get('payment_method', 'otro')
        by_pay[pm] = by_pay.get(pm, 0) + o.get('total', 0)
        for it in o.get('items', []):
            p = prod.setdefault(it.get('name', '?'), {'name': it.get('name', '?'), 'units': 0, 'revenue': 0})
            p['units'] += it.get('quantity', 1)
            p['revenue'] += it.get('price', 0) * it.get('quantity', 1)
    revenue_total = sum(cobrado_de(o) for o in vivos)
    por_cobrar_total = sum(por_cobrar_de(o) for o in vivos)
    return {
        'monthly': sorted(by_month.values(), key=lambda e: e['month']),
        'top_products': sorted(prod.values(), key=lambda p: -p['revenue'])[:10],
        'by_payment': [{'method': k, 'revenue': v} for k, v in sorted(by_pay.items(), key=lambda x: -x[1])],
        'by_status': by_status,
        # El ticket se saca de los pedidos COBRADOS: dividir ingreso cobrado entre
        # todos los pedidos daría un ticket inventado, más bajo que cualquier venta real.
        'avg_ticket': round(revenue_total / len(cobrados)) if cobrados else 0,
        'revenue_total': revenue_total,
        'por_cobrar': por_cobrar_total,
    }


# ----------------- Public: validar codigo de distribuidor -----------------
@api_router.get('/discount-code/{code}')
async def check_discount_code(code: str):
    """Publico: valida un codigo y devuelve SOLO el % de descuento (nada personal).

    ⛔ AQUÍ NO SE AGREGA NI UN CAMPO MÁS DEL DISTRIBUIDOR (Christián, 2026-07-31).
    Esta ruta NO PIDE SESIÓN —el carrito la consulta antes de que nadie entre— así
    que lo que conteste lo puede leer cualquiera con la dirección. Devolver aquí el
    nombre o el correo de quien dio el código sería publicar de quién es cada
    código: la lista de clientes del canal, servida en bandeja. Sólo el porcentaje
    y las condiciones. Lo vigila `test_privacidad_distribuidor.py` y, en vivo,
    `npm run auditoria`."""
    c = (code or '').strip().upper()
    cdoc = await db.discount_codes.find_one({'code': c, 'active': True, 'kind': 'coupon'})
    if cdoc and not cdoc.get('used') and (not cdoc.get('expires_at') or cdoc['expires_at'] >= now_iso()):
        # `min_order`: los cupones de recuperación de carrito exigen un monto mínimo.
        # Se devuelve para que el carrito lo diga ANTES, y no le cobre al cliente algo
        # distinto de lo que vio en pantalla.
        # La MISMA cuenta que cobra el checkout (topada al 40%): si aquí se anunciara el
        # 50% guardado, el carrito pintaría un descuento que la caja no va a dar.
        return {'code': c, 'discount_rate': tasa_de_cupon(cdoc),
                'min_order': float(cdoc.get('min_order') or 0)}
    dist, discount = await _resolve_code(code)
    if not dist:
        raise HTTPException(status_code=404, detail='Codigo no valido')
    return {'code': c, 'discount_rate': discount}


# ----------------- Distribuidor: sus códigos de descuento (auto) -----------------
def _code_projection(doc):
    return {
        'id': doc['id'], 'code': doc['code'], 'discount_rate': doc.get('discount_rate', 0),
        'created_at': doc.get('created_at'), 'expires_at': doc.get('expires_at'),
    }


async def _new_code_string(name, rate):
    code = gen_discount_code(name, rate)
    while await db.discount_codes.find_one({'code': code}):
        code = gen_discount_code(name, rate)
    return code


async def _ensure_distributor_codes(dist, force_rotate=False):
    """Mantiene el set de códigos AUTO del distribuidor: uno por cada nivel de
    descuento de su comisión (15%, 20%… hasta 5% debajo de su comisión). Crea los
    que falten, ROTA los caducados (nuevo texto, el viejo muere), y desactiva los
    que ya no correspondan a su nivel. Devuelve los códigos vigentes ordenados."""
    rate_basis = pyramid.effective_rate(dist)
    tiers = pyramid.discount_tiers_de(dist)
    tierset = {round(r, 4) for r in tiers}
    existing = await db.discount_codes.find({'distributor_id': dist['id']}).to_list(300)
    by_rate = {}
    for c in existing:
        by_rate.setdefault(round(c.get('discount_rate', 0), 4), c)
    now = now_iso()
    new_exp = (datetime.now(timezone.utc) + timedelta(days=CODE_TTL_DAYS)).isoformat()
    out = []
    for rate in tiers:
        c = by_rate.get(round(rate, 4))
        expired = bool(c and c.get('expires_at') and c['expires_at'] < now)
        if not c:
            doc = {'id': str(uuid.uuid4()), 'distributor_id': dist['id'],
                   'code': await _new_code_string(dist.get('name'), rate),
                   'discount_rate': rate, 'active': True, 'created_at': now, 'expires_at': new_exp}
            await db.discount_codes.insert_one(doc)
            out.append(doc)
        elif force_rotate or expired or not c.get('active', True):
            new_code = await _new_code_string(dist.get('name'), rate)
            await db.discount_codes.update_one({'id': c['id']}, {'$set': {
                'code': new_code, 'active': True, 'created_at': now, 'expires_at': new_exp}})
            c.update({'code': new_code, 'active': True, 'created_at': now, 'expires_at': new_exp})
            out.append(c)
        else:
            out.append(c)
    # Desactiva códigos de niveles que ya no aplican (p.ej. tras cambiar de nivel).
    for c in existing:
        if round(c.get('discount_rate', 0), 4) not in tierset and c.get('active', True):
            await db.discount_codes.update_one({'id': c['id']}, {'$set': {'active': False}})
    out.sort(key=lambda c: c.get('discount_rate', 0))
    return out


# ------------------- ACUERDO DE DISTRIBUIDOR: el candado suave -------------------
# ⛔ APAGADO POR OMISIÓN. `acuerdo.bloquea()` contesta False sin tocar la base
# mientras ACUERDO_DISTRIBUIDOR_ACTIVO no valga 'true', así que hoy estas tres
# líneas no hacen absolutamente nada. Cuando Christián encienda el interruptor,
# frenan SÓLO lo que crea obligaciones nuevas: generar códigos, cotizar y
# devengar comisión. Ver el panel, los pedidos y los clientes sigue abierto.
async def _exigir_acuerdo(dist):
    """403 con un código que el navegador reconoce para abrir la pantalla solo."""
    if await acuerdo.bloquea(db, dist):
        raise HTTPException(status_code=403, detail=acuerdo.AVISO,
                            headers={'X-Acuerdo': acuerdo.CODIGO})


@api_router.get('/distributor/codes')
async def list_discount_codes(dist=Depends(get_current_distributor)):
    """Los códigos AUTO del distribuidor (uno por nivel de descuento). Se generan
    y rotan solos cada 30 días; el distribuidor solo elige cuál da a cada cliente.

    Esta ruta CREA códigos (`_ensure_distributor_codes`), no sólo los lee: por eso
    el candado del acuerdo va aquí y no únicamente en `/rotate`. Sin firmar, no se
    emite ni un código nuevo."""
    await _exigir_acuerdo(dist)
    codes = await _ensure_distributor_codes(dist)
    return {'max_discount': pyramid.effective_rate(dist),
            'rotate_days': CODE_TTL_DAYS,
            'codes': [_code_projection(c) for c in codes]}


@api_router.get('/distributor/quote-caps')
async def distributor_quote_caps(dist=Depends(get_current_distributor)):
    """Lo ÚNICO que el COTIZADOR del distribuidor necesita del servidor: hasta
    cuánto descuento aguanta cada producto y hasta cuánto puede dar ÉL.

    ⛔ REGLA DE ORO (Christián, 2026-07-30). Ni el distribuidor ni el cliente ven
    JAMÁS el costo real, el proveedor ni el ROI: eso es territorio EXCLUSIVO del
    admin. Por eso esta ruta NO devuelve productos, devuelve DOS NÚMEROS por
    llave —`product_id` y `discount_cap`— y nada más. El precio público sale del
    catálogo que el sitio ya trae en el navegador; de aquí no sale ni un peso de
    costo. Hay una prueba que lee el payload entero y truena si aparece.

    El tope se calcula con `tope_de_descuento`, LA MISMA función que usa el
    checkout. Si aquí saliera más alto, el distribuidor cotizaría un descuento que
    la caja no le va a respetar y el cliente vería otro total al pagar.

    Se emite una fila por CADA llave con la que el carrito puede nombrar al
    producto (su id y su SKU), porque el navegador manda cualquiera de las dos.
    """
    await _exigir_acuerdo(dist)
    productos = await db.products.find(
        {},
        {'_id': 0, 'id': 1, 'sku': 1, 'name': 1, 'category': 1,
         'commission_cap': 1, 'distributor_eligible': 1, 'hidden': 1},
    ).to_list(2000)
    caps = []
    for p in productos:
        if p.get('hidden'):
            continue          # lo que no está a la venta no se cotiza
        tope = round(tope_de_descuento(p), 4)
        for llave in (p.get('id'), p.get('sku')):
            if llave:
                caps.append({'product_id': llave, 'discount_cap': tope})
    return {
        # Su descuento máximo: el mayor de los niveles que le tocan por su comisión
        # (con la base de 30% del canal son 25%). Es el MISMO número que ya ve en
        # "Mis Códigos"; no es información nueva, y menos aún un costo.
        'max_discount': max(pyramid.discount_tiers_de(dist) or [0]),
        'caps': caps,
    }


# ----------------- La cotización por CORREO -----------------
#
# El distribuidor arma la cotización en su panel y se la manda al correo de su
# cliente. Tres candados, todos en el servidor:
#
#   1. `get_current_distributor` + `deny_view_as` — un cliente no entra (403), y el
#      "ver como" del admin es SOLO LECTURA: espiar un panel no puede convertirse
#      en mandar correos desde la cuenta de otro.
#   2. EL PRECIO LO PONE EL SERVIDOR. Del navegador sólo viajan qué producto y
#      cuántos; el precio, el tope de cada renglón y el total se calculan aquí con
#      el catálogo real. Si no fuera así, cualquiera con la consola abierta podría
#      mandar un correo firmado por Exygen prometiendo Retatrutida a $1.
#   3. TOPE DE ENVÍOS. El botón manda correo a una dirección escrita a mano: sin
#      freno es un cañón de spam con el dominio de Exygen de munición, y el dominio
#      se quema para TODOS los correos (pedidos incluidos).

COTIZACIONES_POR_HORA = 20        # a mano nadie cotiza más rápido que esto
COTIZACIONES_POR_DIA = 60
_COTIZACIONES_MANDADAS = {}       # distribuidor -> marcas de tiempo del último día


def _puede_mandar_cotizacion(dist_id, ahora=None):
    """Freno por distribuidor: 20 por hora y 60 por día. En memoria a propósito —
    si el proceso se reinicia el contador se va, y eso está bien: el freno protege
    del abuso sostenido, no es una cuota contable."""
    import time as _t
    ahora = ahora if ahora is not None else _t.time()
    marcas = [m for m in _COTIZACIONES_MANDADAS.get(dist_id, []) if ahora - m < 86400]
    if sum(1 for m in marcas if ahora - m < 3600) >= COTIZACIONES_POR_HORA \
            or len(marcas) >= COTIZACIONES_POR_DIA:
        _COTIZACIONES_MANDADAS[dist_id] = marcas
        return False
    marcas.append(ahora)
    _COTIZACIONES_MANDADAS[dist_id] = marcas
    return True


def _nombre_cotizable(doc):
    """El nombre que ve el cliente: producto + presentación, sin repetirla."""
    nombre = (doc.get('name') or '').strip()
    pres = (doc.get('presentation') or '').strip()
    if pres and pres.lower() not in nombre.lower():
        return f'{nombre} {pres}'.strip()
    return nombre


def _armar_cotizacion(items, pedido_pct, tope_dist, catalogo):
    """Los renglones de la cotización CON LOS PRECIOS DEL SERVIDOR.

    El descuento de cada renglón es el menor de tres: el que pidió el distribuidor,
    lo que aguanta ese producto (`tope_de_descuento`, la misma función del checkout)
    y su propio máximo. Es la misma aritmética que la pantalla, para que el correo y
    la hoja impresa digan el mismo número."""
    lineas, lista_total, total = [], 0.0, 0.0
    for it in items:
        doc = catalogo.get(it.product_id)
        if not doc or doc.get('hidden'):
            continue
        precio = float(doc.get('price') or 0)
        if precio <= 0:
            continue
        pct = min(pedido_pct, tope_de_descuento(doc), tope_dist)
        unit = round(precio * (1 - pct))
        qty = int(it.quantity)
        # `product_id` viaja en el renglón para armar el enlace al checkout con
        # el carrito ya puesto. Al HTML del correo no llega: ahí sólo se pintan
        # nombre, cantidad y precio.
        lineas.append({'product_id': it.product_id, 'name': _nombre_cotizable(doc),
                       'quantity': qty, 'unit_price': unit,
                       'list_price': round(precio), 'amount': unit * qty})
        lista_total += round(precio) * qty
        total += unit * qty
    return lineas, lista_total, total


@api_router.post('/distributor/quote/email')
async def distributor_quote_email(payload: QuoteEmailRequest,
                                  dist=Depends(get_current_distributor)):
    """Manda la cotización al correo del cliente. Nada de costos viaja adentro."""
    deny_view_as(dist)
    if not payload.items:
        raise HTTPException(status_code=400, detail='La cotización no tiene renglones')
    if len(payload.items) > 40:
        raise HTTPException(status_code=400, detail='Demasiados renglones en la cotización')
    if not _puede_mandar_cotizacion(dist['id']):
        raise HTTPException(status_code=429,
                            detail='Demasiadas cotizaciones seguidas. Espera un momento.')

    catalogo = await _catalogo_de(payload.items)
    tope_dist = max(pyramid.discount_tiers_de(dist) or [0])
    lineas, lista_total, total = _armar_cotizacion(
        payload.items, max(0.0, float(payload.discount or 0)), tope_dist, catalogo)
    if not lineas:
        raise HTTPException(status_code=400, detail='Ninguno de esos productos se puede cotizar')

    codigo = (dist.get('distributor_code') or '').strip()
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    # EL ENLACE VA AL CHECKOUT CON EL CARRITO YA ARMADO (Christián, 2026-07-30):
    # `?pedido=id:cantidad,...` — sólo QUÉ y CUÁNTOS, los mismos renglones que
    # sobrevivieron la validación de arriba. El sitio los hidrata contra el
    # catálogo real (CartContext) y el precio lo vuelve a poner el servidor al
    # cobrar; un enlace manipulado no puede cambiar ni un peso.
    pedido = ','.join(f"{ln['product_id']}:{ln['quantity']}" for ln in lineas)
    enlace = f'{site}/checkout?pedido={urlquote(pedido)}'
    if codigo:
        enlace += f'&ref={urlquote(codigo)}'
    cotizacion = {
        'folio': (payload.folio or '').strip()[:32],
        'client_name': (payload.client_name or '').strip()[:80],
        'client_email': (payload.client_email or '').strip()[:120],
        'client_phone': (payload.client_phone or '').strip()[:40],
        'client_address': (payload.client_address or '').strip()[:200],
        # ⛔ AQUÍ NO VA EL NOMBRE DEL DISTRIBUIDOR (Christián, 2026-07-31). Iba, y
        # el cliente leía "María preparó esta cotización para ti": su identidad
        # regalada en el primer correo. Quien firma es la atención de la casa, y
        # ese nombre lo pone `emails.py`, no esta ruta.
        'code': codigo,
        'link': enlace,
        'lines': lineas,
        'list_total': lista_total,
        'savings': max(0.0, lista_total - total),
        'total': total,
    }
    # ⛔ LA RESPUESTA CAE EN EL BUZÓN DE LA CASA, NO EN EL DEL DISTRIBUIDOR.
    # Antes el `reply_to` era su correo personal: bastaba con que el cliente
    # picara "Responder" para ver de quién era el código. Ahora contesta la
    # atención de la casa, y al distribuidor se le avisa por dentro (abajo).
    salio = await send_quote_email(
        payload.email, cotizacion,
        language=payload.language or dist.get('language'),
        reply_to=ATENCION_CORREO)
    if not salio:
        raise HTTPException(status_code=502, detail='No se pudo enviar la cotización')
    # El aviso POR DENTRO: el distribuidor sabe que su cotización salió y a quién,
    # sin que su nombre ni su correo hayan viajado al cliente. Si la campanita
    # falla, la cotización ya salió y eso no se deshace.
    try:
        await notify(dist['id'], 'quote_sent', 'Cotización enviada',
                     f'Se envió tu cotización a {payload.email} por ${total:,.0f}. '
                     f'El cliente ve a {ATENCION_NOMBRE} como quien lo atiende, y si '
                     f'responde el correo la respuesta llega a {ATENCION_CORREO}.',
                     link='/distribuidor')
    except Exception:
        logger.exception('No se pudo avisar la cotización a %s', dist['id'])
    return {'sent': True, 'total': total, 'lines': len(lineas)}


@api_router.post('/distributor/codes/rotate')
async def rotate_discount_codes(dist=Depends(get_current_distributor)):
    """Renueva YA todos los códigos (nuevos textos). Los viejos dejan de servir."""
    await _exigir_acuerdo(dist)
    codes = await _ensure_distributor_codes(dist, force_rotate=True)
    return {'rotated': True, 'codes': [_code_projection(c) for c in codes]}


# ----------------- Acuerdo de Distribuidor: texto, firma y copia -----------------
# ⛔ NADA DE ESTO SE ACTIVA SOLO. Con el interruptor apagado —que es como está
# hoy— estas rutas siguen existiendo pero contestan `requiere_aceptacion: false`
# y el panel del distribuidor no enseña ni una pantalla nueva. Ver acuerdo.py
# para el porqué legal (Código de Comercio arts. 93, 93 Bis y 1298-A).


@api_router.get('/acuerdo/distribuidor')
async def leer_acuerdo(user=Depends(get_optional_user)):
    """El acuerdo vigente + si ESTE usuario ya lo firmó.

    `get_optional_user` a propósito: la pantalla de ACTIVACIÓN tiene que poder
    enseñar el texto antes de que exista la sesión —nadie firma a ciegas—, y el
    texto no es secreto: es lo que se le pide firmar a cualquiera que entre al
    canal. Los datos de la aceptación (IP, fecha) sólo salen si hay sesión, y
    sólo los suyos."""
    return await acuerdo.estado_para(db, user)


@api_router.post('/acuerdo/distribuidor/aceptar')
async def aceptar_acuerdo(payload: AceptarAcuerdoInput, request: Request,
                          user=Depends(get_current_user)):
    """Registra la aceptación. Aquí nace la prueba.

    ⛔ TRES CANDADOS:
      1. Sesión real (`get_current_user`) — una firma anónima no prueba nada.
      2. `deny_view_as` — el "ver como" del admin es SOLO LECTURA: espiar el
         panel de alguien JAMÁS puede convertirse en firmar un contrato en su
         nombre. Éste es el caso donde más caro saldría.
      3. La versión que manda el navegador tiene que ser la vigente. Si el
         acuerdo cambió mientras la pantalla estaba abierta, se rechaza y se
         recarga: nadie firma un texto distinto del que leyó.

    Y la casilla: `acepto` tiene que llegar en `true` explícito. El modelo la
    declara `False` por omisión, así que un cuerpo vacío NO firma nada."""
    deny_view_as(user)
    if not acuerdo.activo():
        raise HTTPException(status_code=409,
                            detail='La aceptación del acuerdo todavía no está habilitada.')
    if not payload.acepto:
        raise HTTPException(status_code=400,
                            detail='Debes marcar la casilla de aceptación.')
    if payload.version and payload.version != acuerdo.VERSION:
        raise HTTPException(status_code=409,
                            detail='El acuerdo cambió mientras lo leías. Vuelve a cargarlo.')
    await acuerdo.registrar(db, user, ip=acuerdo.ip_de(request),
                            user_agent=acuerdo.user_agent_de(request), origen='panel')
    return await acuerdo.estado_para(db, user)


@api_router.get('/acuerdo/distribuidor/copia')
async def descargar_acuerdo(user=Depends(get_current_user)):
    """La COPIA DESCARGABLE del art. 93 Bis: el texto íntegro más su acta de
    aceptación (quién, cuándo, desde qué IP, sobre qué huella SHA-256).

    Un HTML autocontenido —sin estilos externos ni JavaScript— que se abre igual
    dentro de diez años y se imprime a PDF con Ctrl+P. Se entrega aunque todavía
    no haya firmado: leer lo que se le pide firmar no puede depender de firmarlo."""
    ace = await acuerdo.aceptacion_de(db, user['id'])
    return Response(content=acuerdo.copia_imprimible(ace), media_type='text/html; charset=utf-8',
                    headers={'Content-Disposition':
                             f'attachment; filename="{acuerdo.nombre_de_archivo()}"'})


@api_router.get('/admin/acuerdo/aceptaciones')
async def listar_aceptaciones(admin=Depends(get_current_admin)):
    """El expediente completo, para Christián: quién firmó, qué versión y cuándo.

    Es la lista que se enseña si algún día hay que probar el consentimiento de
    todo el canal. Sólo admin: son datos personales de terceros (IP incluida)."""
    docs = await db[acuerdo.COLECCION].find({}, {'_id': 0}).to_list(2000)
    docs.sort(key=lambda d: d.get('accepted_at', ''), reverse=True)
    return {
        'activo': acuerdo.activo(),
        'version_vigente': acuerdo.VERSION,
        'hash_vigente': acuerdo.hash_documento(),
        'total': len(docs),
        'al_dia': sum(1 for d in docs if d.get('version') == acuerdo.VERSION),
        'aceptaciones': docs,
    }


# ----------------- Centro de noticias: feed del usuario -----------------
async def _generate_running_low(user):
    """Notificación 'por terminarse' a partir de los protocolos del cliente
    (misma proyección que Mi cuenta). No duplica (dedup por protocolo)."""
    try:
        protos = await db.protocols.find({'user_id': user['id'], 'active': True}, {'_id': 0}).to_list(100)
    except Exception:
        return
    for p in protos:
        proj = _protocol_projection(p)
        if proj.get('needs_repurchase'):
            name = p.get('product_name') or 'tu péptido'
            days = proj.get('days_left')
            await notify(user['id'], 'running_low', 'Se te está por acabar un producto',
                         f'Según tu dosis, {name} te alcanza para unos {days} días. Considera recomprar.',
                         link='/cuenta?tab=tools', dedup=p.get('id'))


@api_router.get('/me/notifications')
async def my_notifications(user=Depends(get_current_user)):
    """Feed del usuario: sus notificaciones personales + los avisos de su
    audiencia, más el conteo de no leídas."""
    if user.get('role') == 'user':
        await _generate_running_low(user)
    aud = _audience_for_role(user.get('role'))
    docs = await db.notifications.find({'$or': [
        {'kind': 'personal', 'user_id': user['id']},
        {'kind': 'broadcast', 'audience': {'$in': aud}},
    ]}, {'_id': 0}).to_list(500)
    # Las que el usuario borró con la X no vuelven a aparecer.
    dismissed = set(user.get('notifications_dismissed') or [])
    docs = [d for d in docs if d.get('id') not in dismissed]
    docs.sort(key=lambda d: d.get('created_at', ''), reverse=True)
    seen_at = user.get('notifications_seen_at') or ''
    unread = sum(1 for d in docs if d.get('created_at', '') > seen_at)
    return {'unread': unread, 'notifications': docs[:100]}


@api_router.post('/me/notifications/seen')
async def mark_notifications_seen(user=Depends(get_current_user)):
    """Marca todo como leído (guarda la fecha)."""
    deny_view_as(user)
    await db.users.update_one({'id': user['id']}, {'$set': {'notifications_seen_at': now_iso()}})
    return {'ok': True}


@api_router.delete('/me/notifications/{notif_id}')
async def dismiss_notification(notif_id: str, user=Depends(get_current_user)):
    """La X de una notificación. Las personales se borran; los avisos del admin
    (broadcast) solo se ocultan para ESE usuario."""
    deny_view_as(user)
    doc = await db.notifications.find_one({'id': notif_id}, {'_id': 0, 'kind': 1, 'user_id': 1})
    if not doc:
        raise HTTPException(status_code=404, detail='No encontrada')
    if doc.get('kind') == 'personal':
        if doc.get('user_id') != user['id']:
            raise HTTPException(status_code=403, detail='No es tuya')
        await db.notifications.delete_one({'id': notif_id})
    else:
        await db.users.update_one({'id': user['id']},
                                  {'$addToSet': {'notifications_dismissed': notif_id}})
    return {'ok': True}


@api_router.delete('/me/notifications')
async def dismiss_all_notifications(user=Depends(get_current_user)):
    """Limpia el centro de novedades del usuario de un jalón."""
    deny_view_as(user)
    aud = _audience_for_role(user.get('role'))
    await db.notifications.delete_many({'kind': 'personal', 'user_id': user['id']})
    bcast = await db.notifications.find({'kind': 'broadcast', 'audience': {'$in': aud}},
                                        {'_id': 0, 'id': 1}).to_list(500)
    if bcast:
        await db.users.update_one({'id': user['id']},
                                  {'$addToSet': {'notifications_dismissed': {'$each': [b['id'] for b in bcast]}}})
    await db.users.update_one({'id': user['id']}, {'$set': {'notifications_seen_at': now_iso()}})
    return {'ok': True}


# ----------------- Centro de noticias: admin publica avisos -----------------
@api_router.get('/admin/announcements')
async def list_announcements(admin=Depends(get_current_admin)):
    docs = await db.notifications.find({'kind': 'broadcast'}, {'_id': 0}).to_list(500)
    docs.sort(key=lambda d: d.get('created_at', ''), reverse=True)
    return docs


@api_router.post('/admin/announcements')
async def create_announcement(payload: AnnouncementCreate, admin=Depends(get_current_admin)):
    aud = payload.audience if payload.audience in ('all', 'clients', 'distributors') else 'all'
    doc = await broadcast_notification('announcement', payload.title.strip()[:140],
                                       (payload.body or '').strip()[:4000], aud, payload.link)
    if payload.email:
        asyncio.create_task(_email_announcement(aud, doc['title'], doc['body']))
    return {'id': doc['id'], 'audience': aud, 'emailed': bool(payload.email)}


@api_router.delete('/admin/announcements/{ann_id}')
async def delete_announcement(ann_id: str, admin=Depends(get_current_admin)):
    res = await db.notifications.delete_one({'id': ann_id, 'kind': 'broadcast'})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail='Aviso no encontrado')
    return {'id': ann_id, 'deleted': True}


async def _email_announcement(audience, title, body):
    """Manda el aviso por correo a la audiencia. Best-effort; nunca lanza."""
    q = {'role': 'user'} if audience == 'clients' else {'role': 'distributor'} if audience == 'distributors' else {}
    try:
        users = await db.users.find({**q, 'email_verified': True, 'blocked': {'$ne': True}},
                                    {'_id': 0, 'email': 1, 'name': 1, 'language': 1}).to_list(20000)
        for u in users:
            if u.get('email'):
                await send_news_email(u['name'], u['email'], title, body, u.get('language'))
    except Exception:
        logger.exception('Failed to email announcement')


# ----------------- Admin: Invite customers -----------------
@api_router.post('/admin/customers/invite')
async def invite_customer(payload: DistributorCreate, admin=Depends(get_current_admin)):
    """Invita a un cliente: crea la cuenta y le manda un enlace para que elija su contrasena."""
    existing = await _usuario_por_correo(payload.email)
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    user = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        # Contrasena imposible de adivinar y que nadie conoce: la cuenta solo se
        # abre por el enlace de invitacion.
        'password_hash': hash_password(uuid.uuid4().hex + uuid.uuid4().hex),
        'role': 'user',
        'language': 'es',
        'email_verified': False,
        'invited_at': now_iso(),
        'created_at': now_iso(),
    }
    await db.users.insert_one(user)
    link = await _send_invitation(user)
    sent = email_enabled()
    return {'id': user['id'], 'name': user['name'], 'email': user['email'],
            'invitation_sent': sent, 'invitation_link': None if sent else link}


# ----------------- Admin: Distributors -----------------
def _distributor_rollup(dist, users, orders):
    """Arma el resumen de un distribuidor: sus clientes y sus ventas atribuidas.

    Regla de Christian (2026-07-22): una VENTA cuenta solo si se hizo con el codigo
    del distribuidor (order.referred_by == su id). Los pedidos sin codigo de un
    cliente NO cuentan, aunque el cliente este ligado a el. 'clients' sigue siendo
    la relacion (quien uso su codigo/registro), solo para listarlos."""
    clients = [u for u in users if u.get('referred_by') == dist['id']]
    # VENTAS propias = pedidos hechos con SU código, vivos. El DINERO de esas ventas
    # sólo cuenta si se cobró: `sales_total` es lo cobrado y `por_cobrar` lo fiado.
    valid = [o for o in orders if o.get('referred_by') == dist['id'] and esta_vivo(o)]
    # Red (downline) desde los usuarios ya cargados, para ventas de equipo,
    # reclutas activos y la señal secreta de Diamond (solo la ve el admin).
    children = {}
    for u in users:
        if u.get('role') == 'distributor':
            children.setdefault(u.get('upline_id'), []).append(u['id'])
    network, queue, seen = [], list(children.get(dist['id'], [])), set()
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid); network.append(nid); queue.extend(children.get(nid, []))
    net_ids = set(network) | {dist['id']}
    sales_by = {}
    for o in orders:
        rb = o.get('referred_by')
        if rb in net_ids and esta_vivo(o):
            sales_by[rb] = sales_by.get(rb, 0) + cobrado_de(o)
    team_sales = sum(sales_by.values())
    active_recruits = sum(1 for nid in network if sales_by.get(nid, 0) > 0)
    return {
        'id': dist['id'],
        'name': dist['name'],
        'email': dist['email'],
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': dist.get('commission_rate', 0.25),
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        # El interruptor de privacidad, para que el admin pueda VERLO y prenderlo desde
        # el panel. Sin devolverlo, encenderle los datos a otro distribuidor sería
        # adivinar a ciegas si ya está encendido (Christián, 2026-07-31).
        CAMPO_VE_CLIENTE: bool(dist.get(CAMPO_VE_CLIENTE)),
        # Pirámide: nivel y de quién cuelga.
        'tier': pyramid.normalize_tier(dist.get('tier')),
        # Lo que de verdad gana y el descuento máximo que puede dar (nivel o mano).
        'effective_rate': pyramid.effective_rate(dist),
        'max_discount': max(pyramid.discount_tiers_de(dist) or [0]),
        'upline_id': dist.get('upline_id'),
        'created_at': dist.get('created_at'),
        'email_verified': dist.get('email_verified', False),
        'invited_at': dist.get('invited_at'),
        'admin_notes': dist.get('admin_notes', ''),
        'clients_count': len(clients),
        'sales_count': len(valid),
        'sales_total': sum(cobrado_de(o) for o in valid),
        # Lo que sus clientes recibieron y todavía no pagaron.
        'por_cobrar': sum(por_cobrar_de(o) for o in valid),
        # GANANCIAS = su tajada como vendedor + sobrecomisiones de su downline. Sólo de
        # ventas COBRADAS: no se le paga comisión de dinero que la casa no tiene.
        'earnings': pyramid.earnings_for(dist['id'], orders),
        'team_sales': team_sales,
        'active_recruits': active_recruits,
        # Señal secreta: este Elite ya desbloqueó el Diamond (43%). Solo el admin la ve.
        'diamond_eligible': dist.get('tier') == 'elite' and pyramid.diamond_qualifies(team_sales, active_recruits),
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
    existing = await _usuario_por_correo(payload.email)
    if existing:
        raise HTTPException(status_code=400, detail='Este correo ya esta registrado')
    code = gen_distributor_code(payload.name)
    while await db.users.find_one({'distributor_code': code}):
        code = gen_distributor_code(payload.name)
    dist = {
        'id': str(uuid.uuid4()),
        'name': payload.name,
        'email': payload.email.lower(),
        # Igual que con los clientes: nadie conoce esta contrasena. El distribuidor
        # elige la suya con el enlace de invitacion, y eso confirma su correo.
        'password_hash': hash_password(uuid.uuid4().hex + uuid.uuid4().hex),
        'role': 'distributor',
        'distributor_code': code,
        # Tope duro de Christian (2026-07-21): ningun distribuidor comisiona
        # arriba del 50%. El servidor lo exige; el navegador no basta.
        'commission_rate': max(0.0, min(COMMISSION_CAP, payload.commission_rate)),
        'customer_discount_rate': max(0.05, min(TECHO_DESCUENTO, payload.customer_discount_rate)),
        # Pirámide (§4ter): todo distribuidor nuevo entra como JUNIOR salvo que el
        # admin diga otra cosa; upline = quién lo trajo (para las sobrecomisiones).
        'tier': payload.tier if payload.tier in pyramid.TIER_RATES else 'junior',
        'upline_id': payload.upline_id,
        'language': 'es',
        'email_verified': False,
        'invited_at': now_iso(),
        'created_at': now_iso(),
    }
    await db.users.insert_one(dist)
    await _ensure_distributor_codes(dist)   # códigos AUTO por su nivel
    link = await _send_distributor_invitation(dist)
    sent = email_enabled()
    return {'id': dist['id'], 'name': dist['name'], 'email': dist['email'],
            'distributor_code': code, 'commission_rate': dist['commission_rate'],
            'customer_discount_rate': dist['customer_discount_rate'],
            'invitation_sent': sent, 'invitation_link': None if sent else link}


@api_router.post('/distributor-applications')
async def create_distributor_application(payload: dict):
    """Solicitud pública 'Quiero ser distribuidor' (sección Mayoreo del home).
    Nada se aprueba solo: queda pendiente hasta que Christian decida en el Admin."""
    name = str(payload.get('name') or '').strip()[:120]
    email = str(payload.get('email') or '').strip().lower()[:200]
    phone = str(payload.get('phone') or '').strip()[:40]
    kind = str(payload.get('kind') or '').strip()[:40]
    message = str(payload.get('message') or '').strip()[:2000]
    if not name or '@' not in email or '.' not in email.split('@')[-1]:
        raise HTTPException(status_code=400, detail='Nombre y correo válido son obligatorios')
    # Una solicitud pendiente por correo: reintentos no duplican.
    if await db.distributor_applications.find_one({'email': email, 'status': 'pendiente'}):
        return {'ok': True}
    await db.distributor_applications.insert_one({
        'id': str(uuid.uuid4()), 'name': name, 'email': email, 'phone': phone,
        'kind': kind, 'message': message, 'status': 'pendiente', 'created_at': now_iso(),
    })
    asyncio.create_task(send_admin_notification(
        f'Nueva solicitud de distribuidor: {name}',
        f'<p><strong>{name}</strong> · {email} · {phone or "sin teléfono"} · {kind or "sin tipo"}</p>'
        f'<p>{message or "(sin mensaje)"}</p><p>Apruébala o recházala en el Admin &gt; Distribuidores.</p>',
    ))
    return {'ok': True}


@api_router.get('/admin/distributor-applications')
async def list_distributor_applications(admin=Depends(get_current_admin)):
    apps = await db.distributor_applications.find({}, {'_id': 0}).to_list(1000)
    apps.sort(key=lambda a: (a.get('status') != 'pendiente', a.get('created_at', '')), reverse=False)
    return apps


@api_router.put('/admin/distributor-applications/{app_id}')
async def resolve_distributor_application(app_id: str, payload: dict, admin=Depends(get_current_admin)):
    """Aprobar convierte la cuenta existente o crea la invitación (mismas rutas
    de siempre); rechazar solo marca la solicitud. Christian decide, nunca el sitio."""
    app_doc = await db.distributor_applications.find_one({'id': app_id})
    if not app_doc:
        raise HTTPException(status_code=404, detail='Solicitud no encontrada')
    action = payload.get('action')
    if action == 'rechazar':
        await db.distributor_applications.update_one({'id': app_id}, {'$set': {'status': 'rechazada', 'resolved_at': now_iso()}})
        return {'id': app_id, 'status': 'rechazada'}
    if action != 'aprobar':
        raise HTTPException(status_code=400, detail='Acción inválida')
    commission = max(0.0, min(COMMISSION_CAP, float(payload.get('commission_rate', 0.25) or 0)))
    discount = max(0.05, min(TECHO_DESCUENTO, float(payload.get('customer_discount_rate', 0.10) or 0.10)))
    existing = await _usuario_por_correo(app_doc['email'])
    if existing and existing.get('role') == 'distributor':
        result = {'already': True}
    elif existing:
        # Cliente existente: misma conversión que el botón "Hacer distribuidor".
        code = gen_distributor_code(existing.get('name') or existing['email'])
        while await db.users.find_one({'distributor_code': code}):
            code = gen_distributor_code(existing.get('name') or existing['email'])
        await db.users.update_one({'id': existing['id']}, {'$set': {
            'role': 'distributor', 'distributor_code': code, 'commission_rate': commission,
            'customer_discount_rate': discount, 'converted_from_customer_at': now_iso(),
        }})
        result = {'converted': True, 'distributor_code': code}
    else:
        # Correo nuevo: invitación como la de "Nuevo distribuidor".
        code = gen_distributor_code(app_doc['name'])
        while await db.users.find_one({'distributor_code': code}):
            code = gen_distributor_code(app_doc['name'])
        dist = {
            'id': str(uuid.uuid4()), 'name': app_doc['name'], 'email': app_doc['email'],
            'password_hash': hash_password(uuid.uuid4().hex + uuid.uuid4().hex),
            'role': 'distributor', 'distributor_code': code,
            'commission_rate': commission, 'customer_discount_rate': discount,
            'language': 'es', 'email_verified': False, 'invited_at': now_iso(), 'created_at': now_iso(),
        }
        await db.users.insert_one(dist)
        link = await _send_distributor_invitation(dist)
        result = {'invited': True, 'distributor_code': code,
                  'invitation_sent': email_enabled(), 'invitation_link': None if email_enabled() else link}
    await db.distributor_applications.update_one({'id': app_id}, {'$set': {'status': 'aprobada', 'resolved_at': now_iso()}})
    return {'id': app_id, 'status': 'aprobada', **result}


@api_router.put('/admin/customers/{user_id}/blocked')
async def set_customer_blocked(user_id: str, payload: dict, admin=Depends(get_current_admin)):
    """Bloquea o desbloquea una cuenta (Christian, 2026-07-22: cuentas curiosas
    creadas 'solo para ver'). Bloqueada: no entra ni con contraseña, ni con
    Google, ni con un token vigente. Sus datos y pedidos no se tocan."""
    user = await db.users.find_one({'id': user_id})
    if not user:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    if user.get('role') == 'admin':
        raise HTTPException(status_code=400, detail='Una cuenta admin no se puede bloquear')
    blocked = bool(payload.get('blocked'))
    await db.users.update_one({'id': user_id}, {'$set': {
        'blocked': blocked,
        'blocked_at': now_iso() if blocked else None,
    }})
    return {'id': user_id, 'blocked': blocked}


@api_router.post('/admin/customers/{user_id}/make-distributor')
async def convert_customer_to_distributor(user_id: str, payload: dict, admin=Depends(get_current_admin)):
    """Convierte una cuenta de cliente existente en distribuidor, conservando
    su historial de compras y su misma contraseña/acceso.

    Reglas de Christian (2026-07-21): al convertirse deja de participar en el
    programa de lealtad (los distribuidores ni ganan ni canjean; su saldo queda
    congelado) y aplican los topes de siempre: comisión <= 50% y descuento a
    clientes entre 5% y 50%. Si el cliente venía referido por otro distribuidor,
    ese vínculo se conserva.
    """
    user = await db.users.find_one({'id': user_id})
    if not user:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    if user.get('role') == 'admin':
        raise HTTPException(status_code=400, detail='Una cuenta admin no puede ser distribuidor')
    if user.get('role') == 'distributor':
        raise HTTPException(status_code=400, detail='Esta cuenta ya es distribuidor')
    code = gen_distributor_code(user.get('name') or user['email'])
    while await db.users.find_one({'distributor_code': code}):
        code = gen_distributor_code(user.get('name') or user['email'])
    try:
        commission = float(payload.get('commission_rate', 0.25) or 0)
        discount = float(payload.get('customer_discount_rate', 0.10) or 0.10)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='Tasas inválidas')
    commission = max(0.0, min(COMMISSION_CAP, commission))
    discount = max(0.05, min(TECHO_DESCUENTO, discount))
    await db.users.update_one({'id': user_id}, {'$set': {
        'role': 'distributor',
        'distributor_code': code,
        'commission_rate': commission,
        'customer_discount_rate': discount,
        'converted_from_customer_at': now_iso(),
    }})
    # Ya tiene contraseña: la bienvenida lleva a su panel, no a activar.
    asyncio.create_task(send_distributor_welcome_email(
        user.get('name') or user['email'], user['email'], code,
        f'{SITE_URL}/distribuidor', user.get('language'), needs_activation=False))
    return {'id': user_id, 'name': user.get('name'), 'email': user['email'],
            'distributor_code': code, 'commission_rate': commission,
            'customer_discount_rate': discount}


@api_router.put('/admin/distributors/{dist_id}/rates')
async def update_distributor_rates(dist_id: str, payload: dict, admin=Depends(get_current_admin)):
    """Ajusta la comision y/o el descuento de UN distribuidor, hacia adelante.

    Las ventas ya hechas NO se tocan: cada orden guardo su comision en pesos
    al momento de crearse, y los reportes suman lo guardado. Cambiar la tasa
    hoy solo afecta ordenes futuras (regla de Christian, 2026-07-21)."""
    dist = await db.users.find_one({'id': dist_id, 'role': 'distributor'}, {'_id': 0})
    if not dist:
        raise HTTPException(status_code=404, detail='Distribuidor no encontrado')
    update = {}
    if payload.get('commission_rate') is not None:
        update['commission_rate'] = max(0.0, min(COMMISSION_CAP, float(payload['commission_rate'])))
    if payload.get('customer_discount_rate') is not None:
        update['customer_discount_rate'] = max(0.05, min(TECHO_DESCUENTO, float(payload['customer_discount_rate'])))
    if not update:
        raise HTTPException(status_code=400, detail='Nada que actualizar')
    await db.users.update_one({'id': dist_id}, {'$set': update})
    fresh = await db.users.find_one({'id': dist_id}, {'_id': 0, 'password_hash': 0})
    # Al cambiar la comisión, sus códigos AUTO se rehacen EN EL ACTO: se crean los
    # nuevos niveles de descuento (hasta 5% debajo de su comisión) y se desactivan
    # los que ya no le corresponden. Antes había que esperar a que abriera el panel.
    if 'commission_rate' in update:
        before = pyramid.effective_rate(dist)
        after = pyramid.effective_rate(fresh)
        await _ensure_distributor_codes(fresh)
        if after > before:
            tope = round(max(pyramid.discount_tiers_for(after) or [0]) * 100)
            await notify(dist_id, 'commission_up', 'Subió tu comisión',
                         f'Tu comisión ahora es {round(after * 100)}%. Ya tienes códigos nuevos '
                         f'para dar hasta {tope}% de descuento a tus clientes.',
                         link='/distribuidor?tab=codes')
    return {'id': fresh['id'], 'name': fresh['name'],
            'commission_rate': fresh['commission_rate'],
            'customer_discount_rate': fresh['customer_discount_rate'],
            'effective_rate': pyramid.effective_rate(fresh)}


@api_router.put('/admin/distributors/{dist_id}/pyramid')
async def update_distributor_pyramid(dist_id: str, payload: dict, admin=Depends(get_current_admin)):
    """Asigna el NIVEL (junior/senior/master) y/o el UPLINE de un distribuidor.

    Es como se arma y se asciende en la pirámide (§4ter). Los ascensos van hacia
    adelante: las ventas ya hechas guardaron su reparto en pesos y no se tocan.
    Reglas de seguridad: el upline debe existir y ser distribuidor, no puede ser
    él mismo, y no se permite un ciclo (que su upline termine colgando de él)."""
    dist = await db.users.find_one({'id': dist_id, 'role': 'distributor'}, {'_id': 0})
    if not dist:
        raise HTTPException(status_code=404, detail='Distribuidor no encontrado')
    update = {}
    if 'tier' in payload:
        tier = payload.get('tier')
        if tier not in pyramid.TIER_RATES:
            raise HTTPException(status_code=400, detail='Nivel inválido (junior/senior/master)')
        update['tier'] = tier
    if 'upline_id' in payload:
        up_id = payload.get('upline_id') or None
        if up_id:
            if up_id == dist_id:
                raise HTTPException(status_code=400, detail='Un distribuidor no puede ser su propio upline')
            up = await db.users.find_one({'id': up_id, 'role': 'distributor'}, {'_id': 0})
            if not up:
                raise HTTPException(status_code=400, detail='El upline debe ser un distribuidor existente')
            # Evitar ciclos: subir desde el upline propuesto; si topamos con dist_id, es ciclo.
            cursor, hops = up, 0
            while cursor and hops < 50:
                if cursor.get('id') == dist_id:
                    raise HTTPException(status_code=400, detail='Ese upline crearía un ciclo en la pirámide')
                nxt = cursor.get('upline_id')
                cursor = await db.users.find_one({'id': nxt}, {'_id': 0}) if nxt else None
                hops += 1
        update['upline_id'] = up_id
    if not update:
        raise HTTPException(status_code=400, detail='Nada que actualizar')
    await db.users.update_one({'id': dist_id}, {'$set': update})
    fresh = await db.users.find_one({'id': dist_id}, {'_id': 0, 'password_hash': 0})
    # Notificar el ASCENSO (subió de nivel) — un logro para el distribuidor.
    if 'tier' in update:
        # Su nivel nuevo puede darle más descuentos: rehacer los códigos AUTO ya.
        await _ensure_distributor_codes(fresh)
        order = pyramid.TIER_ORDER
        old_i = order.index(pyramid.normalize_tier(dist.get('tier')))
        new_i = order.index(update['tier']) if update['tier'] in order else -1
        if new_i > old_i:
            names = {'junior0': 'Junior 0', 'junior1': 'Junior 1', 'senior': 'Senior',
                     'master': 'Master', 'elite': 'Elite', 'diamond': 'Diamond'}
            nice = names.get(update['tier'], update['tier'])
            rate = round(pyramid.tier_rate(update['tier']) * 100)
            await notify(dist_id, 'level_up', f'¡Subiste a {nice}!',
                         f'Alcanzaste el nivel {nice}: ahora tu comisión es {rate}%. ¡Felicidades!',
                         link='/distribuidor')
    return {'id': fresh['id'], 'name': fresh['name'],
            'tier': fresh.get('tier', 'junior'), 'upline_id': fresh.get('upline_id'),
            'commission_rate': fresh.get('commission_rate')}


# ----------------- Distributor portal -----------------
def _my_amount(order, dist_id):
    """Lo que ESTE distribuidor gana en una orden: su tajada en el reparto de la
    pirámide (vendedor o upline). Cae al campo viejo `commission` si la orden es
    anterior a la pirámide y fue su venta directa.

    ⛔ CERO SI LA VENTA NO SE COBRÓ. El candado va aquí, en el único lugar por donde
    pasan todos los totales de ganancias del portal, para que ninguna pantalla enseñe
    como ganado un dinero que la casa todavía no recibió. Ver `por_cobrar` en el mismo
    resumen: la venta no se esconde, sólo deja de contarse como ganancia."""
    if not esta_pagado(order):
        return 0
    rows = order.get('commissions')
    if rows:
        return sum(r.get('amount', 0) for r in rows if r.get('distributor_id') == dist_id)
    return order.get('commission', 0) if order.get('referred_by') == dist_id else 0


@api_router.get('/distributor/summary')
async def distributor_summary(dist=Depends(get_current_distributor)):
    # Clientes = relación (referred_by). VENTAS propias = pedidos con SU código.
    # GANANCIAS = su tajada como vendedor + sobrecomisiones de su downline, así que
    # jalamos también los pedidos donde aparece en el reparto (commissions).
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    orders = await db.orders.find(
        {'$or': [{'referred_by': dist['id']}, {'commissions.distributor_id': dist['id']}]}, {'_id': 0}
    ).to_list(10000)
    valid = [o for o in orders if esta_vivo(o)]
    own_sales = [o for o in valid if o.get('referred_by') == dist['id']]
    by_month = {}
    for o in valid:
        m = (o.get('created_at') or '')[:7]
        e = by_month.setdefault(m, {'month': m, 'earnings': 0, 'sales': 0, 'por_cobrar': 0})
        e['earnings'] += _my_amount(o, dist['id'])
        if o.get('referred_by') == dist['id']:
            e['sales'] += cobrado_de(o)
            e['por_cobrar'] += por_cobrar_de(o)
    earnings_total = sum(_my_amount(o, dist['id']) for o in valid)
    own_earnings = sum(_my_amount(o, dist['id']) for o in own_sales)
    # Red: reclutas activos y ventas de equipo, para la barra de nivel (ventas + reclutas).
    net = await _downline_stats(dist['id'])
    tier = pyramid.normalize_tier(dist.get('tier'))
    rate = pyramid.effective_rate(dist)   # su nivel O la tasa que el admin le puso a mano
    return {
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': rate,
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        'tier': tier,
        'max_discount': rate,
        'clients_count': len(users),
        'sales_count': len(own_sales),
        # VENTAS = lo cobrado. Lo entregado y no pagado va aparte, como deuda: ni suma
        # en ventas ni genera comisión, pero tampoco desaparece de su tablero.
        'sales_total': sum(cobrado_de(o) for o in own_sales),
        'por_cobrar': sum(por_cobrar_de(o) for o in own_sales),
        'earnings_total': earnings_total,
        # Desglose: cuánto es de ventas propias y cuánto de sobrecomisión del equipo.
        'own_earnings': own_earnings,
        'override_earnings': earnings_total - own_earnings,
        # Red y barra de nivel: avance en VENTAS y en RECLUTAS ACTIVOS.
        'active_recruits': net['active_recruits'],
        'network_size': net['network_size'],
        'team_sales': net['team_sales'],
        'level': pyramid.level_progress(tier, net['personal_sales'], net['team_sales'], net['active_recruits']),
        'monthly': sorted(by_month.values(), key=lambda e: e['month']),
    }


@api_router.get('/distributor/best-sellers')
async def distributor_best_sellers(dist=Depends(get_current_distributor)):
    """Ranking AGREGADO de los productos que más vende su red (para que sepa
    qué empujar). Nunca dice QUIÉN compró qué — solo totales por producto."""
    orders = await db.orders.find({'referred_by': dist['id'], 'status': {'$ne': 'cancelado'}},
                                  {'_id': 0, 'items': 1}).to_list(10000)
    agg = {}
    for o in orders:
        for it in o.get('items', []):
            name = it.get('name') or '—'
            row = agg.setdefault(name, {'name': name, 'units': 0, 'orders': 0})
            row['units'] += int(it.get('quantity', 0) or 0)
            row['orders'] += 1
    ranking = sorted(agg.values(), key=lambda r: -r['units'])[:10]
    return {'ranking': ranking, 'total_products': len(agg)}


def _compradores_invitados(orders, correos_con_cuenta):
    """Los que compraron con el código del distribuidor SIN tener cuenta.

    ⛔ QUIEN USA EL CÓDIGO ES SU CLIENTE, tenga cuenta o no (Christián, 2026-07-30). Las
    listas de clientes se armaban únicamente con `users.referred_by`, así que un comprador
    INVITADO quedaba invisible para su distribuidor aunque su pedido trajera el
    `referred_by` correcto y la comisión ya estuviera pagada. Le pasó a María con Aidee
    (EX-20260730-2906, $2,830 cobrados y $780 de comisión): el dinero se contó, la persona
    no. Un distribuidor que no ve a quién le vendió no puede volver a venderle.

    Se agrupa por correo en minúsculas y se descarta el que YA tiene cuenta, o la misma
    persona sale dos veces — una como cliente y otra como invitada."""
    por_correo = {}
    for o in orders:
        if o.get('user_id') or not esta_vivo(o):
            continue                     # con cuenta: ya sale por la otra vía
        c = o.get('customer') or {}
        correo = (c.get('email') or '').strip().lower()
        if not correo or correo in correos_con_cuenta:
            continue
        g = por_correo.setdefault(correo, {
            'id': f'invitado:{correo}', 'guest': True, 'name': c.get('full_name') or correo,
            'email': correo, 'phone': c.get('phone') or '', 'orders': [],
        })
        g['orders'].append(o)
        # El nombre y el teléfono más recientes: la gente corrige sus datos al recomprar.
        if o.get('created_at', '') >= max((x.get('created_at', '') for x in g['orders']), default=''):
            g['name'] = c.get('full_name') or g['name']
            g['phone'] = c.get('phone') or g['phone']
    return list(por_correo.values())


@api_router.get('/distributor/clients')
async def distributor_clients(dist=Depends(get_current_distributor)):
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    # Solo pedidos hechos con SU código cuentan (no todo lo que compró el cliente).
    orders = await db.orders.find({'referred_by': dist['id']}, {'_id': 0}).to_list(10000)
    by_user = {}
    for o in orders:
        if o.get('user_id'):
            by_user.setdefault(o['user_id'], []).append(o)
    out = []
    for u in users:
        uo = [o for o in by_user.get(u['id'], []) if esta_vivo(o)]
        # Privacidad (Christian 2026-07-23): el distribuidor ve un RESUMEN, no la
        # ficha del cliente. Nada de correo, teléfono ni domicilio.
        out.append({
            'id': u['id'], 'name': u['name'], 'created_at': u.get('created_at'),
            'guest': False,
            'orders_count': len(uo),
            # Pagado y por cobrar, separados: es la información que el distribuidor
            # necesita para saber a quién tiene que cobrarle.
            'total_spent': sum(cobrado_de(o) for o in uo),
            'por_cobrar': sum(por_cobrar_de(o) for o in uo),
            'my_earnings': sum(_my_amount(o, dist['id']) for o in uo),
            'last_order_at': max([o.get('created_at', '') for o in uo], default=None),
        })
    # Y los que compraron con su código SIN cuenta: también son suyos.
    con_cuenta = {(u.get('email') or '').strip().lower() for u in users}
    for g in _compradores_invitados(orders, con_cuenta):
        uo = g['orders']
        out.append({
            'id': g['id'], 'name': g['name'], 'created_at': min(
                (o.get('created_at', '') for o in uo), default=None),
            'guest': True,
            'orders_count': len(uo),
            'total_spent': sum(cobrado_de(o) for o in uo),
            'por_cobrar': sum(por_cobrar_de(o) for o in uo),
            'my_earnings': sum(_my_amount(o, dist['id']) for o in uo),
            'last_order_at': max([o.get('created_at', '') for o in uo], default=None),
        })
    out.sort(key=lambda u: -u['total_spent'])
    return out


@api_router.get('/cotizador/clientes')
async def cotizador_clientes(quien=Depends(get_current_distributor)):
    """Los clientes que puede autollenar quien está cotizando. Admin o distribuidor.

    ⛔ EL CANDADO DE PRIVACIDAD VIVE AQUÍ, EN EL SERVIDOR, NO EN LA PANTALLA. Esconder
    los campos en el navegador no esconde nada: la respuesta se lee en la consola con la
    sesión abierta. Así que lo que no se puede ver, NO VIAJA.

      · admin              → todos los clientes, con su contacto completo;
      · distribuidor CON el interruptor (hoy sólo María) → SUS clientes, con contacto;
      · distribuidor SIN el interruptor → SUS clientes, y sólo el NOMBRE.

    En el último caso el autollenado rellena el nombre y deja lo demás en blanco, que es
    exactamente lo que ese distribuidor puede saber de su cliente (regla de Christián del
    2026-07-23, todavía vigente para todos menos los encendidos).

    El «sólo SUS clientes» no depende del interruptor: se filtra por `referred_by` antes
    de mirar ninguna otra cosa.
    """
    es_admin = quien.get('role') == 'admin'
    abierto = es_admin or ve_datos_del_cliente(quien)

    filtro = {} if es_admin else {'referred_by': quien['id']}
    users = await db.users.find(filtro, {'_id': 0, 'password_hash': 0}).to_list(5000)
    fuera = {'admin'} if es_admin else set()
    gente = []
    for u in users:
        if u.get('role') in fuera:
            continue
        ficha = {'id': u['id'], 'name': u.get('name') or '', 'guest': False}
        if abierto:
            # `address` en los usuarios puede venir como texto suelto o como diccionario
            # (según cómo se registró). Se aplana aquí para que la pantalla no adivine.
            dom = u.get('address')
            if isinstance(dom, dict):
                dom = ', '.join(x for x in (dom.get('address'), dom.get('address_2'),
                                            dom.get('city'), dom.get('state'),
                                            dom.get('postal_code')) if x)
            ficha.update({'email': u.get('email') or '', 'phone': u.get('phone') or '',
                          'address': dom or ''})
        gente.append(ficha)

    # Los que compraron SIN cuenta también son clientes: sus datos viven en el pedido.
    pedidos = await db.orders.find(
        {} if es_admin else {'referred_by': quien['id']}, {'_id': 0}).to_list(10000)
    con_cuenta = {(u.get('email') or '').strip().lower() for u in users}
    vistos = set()
    for o in pedidos:
        c = o.get('customer') or {}
        correo = (c.get('email') or '').strip().lower()
        if not correo or correo in con_cuenta or correo in vistos or o.get('user_id'):
            continue
        vistos.add(correo)
        ficha = {'id': f'invitado:{correo}', 'name': c.get('full_name') or '',
                 'guest': True}
        if abierto:
            ficha.update({
                'email': c.get('email') or '', 'phone': c.get('phone') or '',
                'address': ', '.join(x for x in (
                    c.get('address'), c.get('address_2'), c.get('city'),
                    c.get('state'), c.get('postal_code')) if x),
            })
        gente.append(ficha)

    gente.sort(key=lambda g: (g['name'] or '').lower())
    return {'puede_ver_contacto': abierto, 'clientes': gente}


def _id_de_cliente(order):
    """A QUÉ FICHA APUNTA EL NOMBRE DE ESTE PEDIDO.

    Con cuenta, su id de usuario; sin cuenta, `invitado:<correo>` — la misma llave que
    usan las listas de clientes. Va en TODAS las listas donde sale un nombre para que el
    clic abra la ficha desde cualquier lado y no sólo desde la lista de Clientes."""
    if order.get('user_id'):
        return order['user_id']
    correo = ((order.get('customer') or {}).get('email') or '').strip().lower()
    return f'invitado:{correo}' if correo else None


async def _distributor_orders(dist):
    """Órdenes atribuidas al distribuidor: SOLO las hechas con su código
    (regla Christian 2026-07-22). Un pedido sin código no le pertenece."""
    orders = await db.orders.find({'referred_by': dist['id']}, {'_id': 0}).to_list(10000)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return orders


# ⛔ QUIÉN VE LOS DATOS DE CONTACTO DEL CLIENTE — INTERRUPTOR POR PERSONA
#
# El 2026-07-23 Christián ordenó que un distribuidor NO viera «correo, teléfono,
# domicilio, ni qué compuestos compró su cliente». El 2026-07-31 lo cambió, pero SÓLO
# para María: ella atiende a sus clientes de verdad y necesita poder llamarles.
#
# Por eso esto es un INTERRUPTOR POR DISTRIBUIDOR y no una regla nueva para todos: los
# demás siguen exactamente como el 23 de julio, y encender a otro es un clic del admin,
# no un despliegue. Mismo patrón que `personal_discount_rate`.
#
# ⛔ LO QUE ESTE INTERRUPTOR NO AFLOJA, NUNCA:
#   · el candado de «sólo SUS clientes» (`referred_by == dist['id']`), que vive en el
#     servidor y decide a qué pedidos puede asomarse siquiera;
#   · el margen de la casa: costo, ROI y lo que ganan los demás en la pirámide no
#     viajan aunque el interruptor esté encendido;
#   · el «ver como» del admin, que sigue siendo de sólo lectura.
CAMPO_VE_CLIENTE = 've_datos_del_cliente'


def ve_datos_del_cliente(dist) -> bool:
    """¿A este distribuidor se le abrieron los datos de contacto de SUS clientes?"""
    return bool((dist or {}).get(CAMPO_VE_CLIENTE))


def _contacto_del_cliente(o, dist=None, es_admin=False) -> dict:
    """Los datos de contacto del cliente, si quien pregunta puede verlos.

    El admin siempre; el distribuidor sólo si tiene el interruptor encendido. Cuando no
    puede, se devuelve el diccionario VACÍO en vez de las claves en blanco: así la
    pantalla no puede «enseñar un campo vacío» y dar a entender que el dato no existe.
    """
    if not (es_admin or ve_datos_del_cliente(dist)):
        return {}
    c = o.get('customer') or {}
    return {
        'customer_full_name': c.get('full_name') or '',
        'customer_email': c.get('email') or '',
        'customer_phone': c.get('phone') or '',
        'customer_address': c.get('address') or '',
        'customer_address_2': c.get('address_2') or '',
        'customer_city': c.get('city') or '',
        'customer_state': c.get('state') or '',
        'customer_postal_code': c.get('postal_code') or '',
        'customer_country': c.get('country') or '',
        'customer_notes': c.get('notes') or '',
    }


@api_router.get('/distributor/sales')
async def distributor_sales(dist=Depends(get_current_distributor)):
    orders = await _distributor_orders(dist)
    # Solo lo que el distribuidor necesita: no exponemos datos internos de margen del negocio.
    return [{
        'order_number': o.get('order_number'),
        'created_at': o.get('created_at'),
        'status': o.get('status'),
        # `pagado` es lo que dice si esa comisión ya es COBRABLE. La entrega no la
        # libera: hasta que entre el dinero, la fila se ve pero no se paga.
        'pagado': esta_pagado(o),
        'customer_name': ((o.get('customer') or {}).get('full_name') or '').split(' ')[0],
        'client_id': _id_de_cliente(o),
        # El envío viaja también aquí: desde Ventas se pone la guía sin cambiarse de
        # pestaña, y el botón necesita saber si dice "poner" o "cambiar".
        'carrier': o.get('carrier', ''),
        'tracking_number': o.get('tracking_number', ''),
        'tracking_url': o.get('tracking_url', ''),
        'total': o.get('total', 0),
        'commission': _my_amount(o, dist['id']),
        'items_count': sum(int(it.get('quantity', 0) or 0) for it in o.get('items', [])),
    } for o in orders]


@api_router.get('/distributor/orders')
async def distributor_orders(dist=Depends(get_current_distributor)):
    """Pedidos de SUS clientes con estatus y seguimiento de envío.

    Incluye datos de contacto y entrega del cliente (el distribuidor los atiende),
    pero nunca el margen interno del negocio.
    """
    orders = await _distributor_orders(dist)
    abierto = ve_datos_del_cliente(dist)
    out = []
    for o in orders:
        c = o.get('customer') or {}
        # Privacidad (Christian 2026-07-23): NADA de correo, teléfono, domicilio,
        # ni qué compuestos compró su cliente. Solo lo necesario para dar
        # seguimiento: quién, cuánto, cómo pagó, en qué va el envío.
        #
        # ⛔ SALVO QUE TENGA EL INTERRUPTOR (Christián, 2026-07-31 — hoy sólo María).
        # Ahí sí van los datos de contacto de SUS clientes, porque ella los atiende.
        # El candado de «sólo sus pedidos» no se toca: lo aplica `_distributor_orders`.
        out.append({
            'order_number': o.get('order_number'),
            'created_at': o.get('created_at'),
            'status': o.get('status', 'pendiente'),
            'customer_name': (c.get('full_name') or '').split(' ')[0],
            'client_id': _id_de_cliente(o),
            'payment_method': o.get('payment_method'),
            'items_count': sum(int(it.get('quantity', 0) or 0) for it in o.get('items', [])),
            'total': o.get('total', 0),
            'discount_rate': o.get('discount_rate', 0),
            'commission': o.get('commission', 0),
            'carrier': o.get('carrier', ''),
            # ⛔ LA GUÍA VIAJA EN LA LISTA. Iba sólo la paquetería, así que la columna
            # "Envío" del panel leía un `tracking_number` que nunca llegaba: TODOS los
            # pedidos se veían "Sin guía todavía", aun los que ya iban en camino con su
            # número guardado. Y sin este dato el botón no sabe si toca "Poner Guía" o
            # "Cambiar Guía" (Christián, 2026-07-30).
            'tracking_number': o.get('tracking_number', ''),
            'tracking_url': o.get('tracking_url', ''),
            'shipped_at': o.get('shipped_at'),
            'delivered_at': o.get('delivered_at'),
            'eta': o.get('eta', ''),
            **(_contacto_del_cliente(o, dist) if abierto else {}),
        })
    return out


def _detalle_de_pedido(o, dist_id=None, dist=None, es_admin=False):
    """El detalle de UN pedido para verlo en una ficha. `dist_id` = quién pregunta.

    Lleva lo que hace falta para responder "¿qué compró y qué pasó con su dinero?": los
    renglones con precio unitario, el descuento y de dónde salió, cuánto se pagó y cuánto
    falta, el envío (y si la casa lo absorbió), los puntos y el estado del paquete.

    ⛔ NUNCA VIAJA UN DATO DE PAGO. No los guardamos —la tarjeta se teclea en Mercado
    Pago, nunca en nuestro servidor— así que aquí no hay nada que filtrar; se dice por
    escrito para que a nadie se le ocurra añadirlo.

    Y el DINERO INTERNO tampoco: al distribuidor se le dice SU comisión de este pedido,
    no el margen de la casa ni lo que ganaron los demás en la pirámide."""
    c = o.get('customer') or {}
    envio = float(o.get('shipping', 0) or 0)
    return {
        # El id va porque la hoja de "poner guía" del ADMIN guarda por id
        # (/admin/orders/{id}/shipping) y esa hoja se abre desde dentro de esta ficha.
        # Sin el id habría que ir a buscar el pedido a la lista para poder capturarla,
        # que es justo lo que se quitó. Al distribuidor no le sirve —él guarda por número
        # de pedido— pero tampoco le dice nada que no sepa: es la llave de un pedido suyo.
        'id': o.get('id'),
        'order_number': o.get('order_number'),
        'created_at': o.get('created_at'),
        'status': o.get('status', 'pendiente'),
        'customer_name': c.get('full_name') or '',
        'items': [{
            'name': it.get('name'), 'presentation': it.get('presentation', ''),
            'quantity': int(it.get('quantity', 0) or 0),
            'unit_price': float(it.get('price', 0) or 0),
            'line_total': float(it.get('price', 0) or 0) * int(it.get('quantity', 0) or 0),
        } for it in o.get('items', [])],
        'subtotal': o.get('subtotal', 0),
        'discount': o.get('discount', 0),
        'discount_rate': o.get('discount_rate', 0),
        'discount_code': o.get('distributor_code') or '',
        'points_used': int(o.get('points_used', 0) or 0),
        'points_earned': int(o.get('points_earned', 0) or 0),
        # El envío, con la verdad completa: gratis NO quiere decir que no costó.
        'shipping': envio,
        'shipping_free': envio <= 0,
        'shipping_absorbed': float(o.get('shipping_absorbed', 0) or 0),
        'total': o.get('total', 0),
        # Pagado ≠ entregado (Christián, 2026-07-29): son dos preguntas distintas.
        'paid': esta_pagado(o),
        'paid_at': o.get('paid_at'),
        'por_cobrar': por_cobrar_de(o),
        'payment_method': o.get('payment_method'),
        # Envío partido: qué salió ya y qué quedó por surtir.
        'backorder': bool(o.get('backorder')),
        'backorder_items': o.get('backorder_items') or [],
        'carrier': o.get('carrier', ''),
        'tracking_number': o.get('tracking_number', ''),
        'tracking_url': o.get('tracking_url', ''),
        'shipped_at': o.get('shipped_at'),
        'delivered_at': o.get('delivered_at'),
        'eta': o.get('eta', ''),
        'my_commission': _my_amount(o, dist_id) if dist_id else None,
        # El PDF de la guía, SÓLO para el admin: comprar guías es dinero de la casa y
        # vive en el admin (igual que cotizar). Sin esto, una guía comprada quedaba sin
        # forma de imprimirse desde la ficha — que es justo lo que hace falta para poder
        # pegarla en el paquete y llevarlo al mostrador.
        **({'label_url': o.get('label_url') or ''} if es_admin else {}),
        # Los datos de contacto sólo si quien pregunta puede verlos: el admin siempre,
        # el distribuidor sólo con su interruptor encendido (hoy, sólo María).
        **_contacto_del_cliente(o, dist, es_admin),
    }


@api_router.get('/distributor/orders/{order_number}')
async def distributor_order_detail(order_number: str, dist=Depends(get_current_distributor)):
    """El detalle de un pedido SUYO. De otro, 403.

    ⛔ EL CANDADO VIVE AQUÍ, NO EN LA PANTALLA. Una ficha que solo se esconde en el
    navegador se abre tecleando el número de pedido de otro en la barra de direcciones —
    y ahí va el nombre del cliente ajeno, qué compró y cuánto pagó. El servidor exige que
    el pedido traiga SU `referred_by`; si no, no existe para él."""
    o = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if o.get('referred_by') != dist['id']:
        raise HTTPException(status_code=403, detail='Ese pedido no es tuyo')
    return _detalle_de_pedido(o, dist['id'], dist=dist)


@api_router.put('/distributor/orders/{order_number}/shipping')
async def distributor_order_shipping(order_number: str, payload: DistributorShippingUpdate,
                                     dist=Depends(get_current_distributor)):
    """El distribuidor captura la guía de un pedido SUYO. De otro, 403.

    Hasta hoy sólo el admin podía teclear el número de guía, así que cada paquete que
    despachaba un distribuidor tenía que pasar por Christián para que el cliente se
    enterara. María atiende a sus clientes: que capture ella la guía que ya tiene en
    la mano (Christián, 2026-07-30).

    ⛔ TRES CANDADOS, TODOS EN EL SERVIDOR:
      1. `get_current_distributor` — un cliente normal no entra (403).
      2. `referred_by == dist['id']` — el pedido de otro distribuidor NO existe para
         él. Esconder el formulario en la pantalla no sirve de nada: el número de
         pedido ajeno se teclea en la barra de direcciones.
      3. `deny_view_as` — el "ver como" del admin es SOLO LECTURA; espiar el panel de
         alguien no puede convertirse en escribirle en sus pedidos.

    Y sólo envío: el modelo `DistributorShippingUpdate` no tiene `status`, ni precios,
    ni pagos, ni forma de borrar nada. La cotización y COMPRA de guías por Skydropx
    —dinero de la casa— se queda en el admin: aquí sólo se captura la guía que ya existe.
    """
    deny_view_as(dist)
    o = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if o.get('referred_by') != dist['id']:
        raise HTTPException(status_code=403, detail='Ese pedido no es tuyo')
    envio = OrderShippingUpdate(carrier=payload.carrier,
                                tracking_number=payload.tracking_number,
                                tracking_url=payload.tracking_url)
    # El mismo camino del admin (mismo correo de rastreo al cliente), pero sin `status`.
    resultado = await _guardar_envio(o, envio, permitir_status=False)
    return _detalle_de_pedido(resultado, dist['id'], dist=dist)


@api_router.get('/admin/orders/{order_number}/detalle')
async def admin_order_detail(order_number: str, admin=Depends(get_current_admin)):
    """El mismo detalle para el admin, que sí ve todos — y con A QUIÉN COMPRARLE.

    El proveedor SÓLO va por aquí, nunca por la ficha del distribuidor: nombres de
    proveedores, teléfonos y costos son de la casa."""
    o = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    # `es_admin=True`: el admin ve los datos de contacto del cliente SIEMPRE, sin
    # depender de ningún interruptor. El que se restringe es el distribuidor.
    return _detalle_de_pedido(await _pedido_con_proveedores(o), o.get('referred_by'),
                              es_admin=True)


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


async def _user_product_slugs(user_id: str) -> set:
    """Slugs de los productos que este cliente compró en pedidos ya pagados.

    Es lo que decide a qué COA tiene acceso: se entrega el certificado del
    producto que compró, no el catálogo completo.
    """
    orders = await db.orders.find(
        {'user_id': user_id, 'status': {'$in': list(coa_store.PAID_STATUSES)}},
        {'_id': 0, 'items': 1},
    ).to_list(500)
    product_ids = {it.get('product_id') for o in orders for it in o.get('items', []) if it.get('product_id')}
    if not product_ids:
        return set()
    rows = await db.products.find({'id': {'$in': list(product_ids)}}, {'_id': 0, 'slug': 1}).to_list(500)
    return {r.get('slug') for r in rows if r.get('slug')}


@api_router.get('/coa/public')
async def coa_public():
    """El único COA de muestra visible sin haber comprado. {} si no hay."""
    return coa_store.public_entry() or {}


@api_router.get('/me/coas')
async def my_coas(user=Depends(get_current_user)):
    """COAs de los lotes de los productos que el usuario compró."""
    slugs = await _user_product_slugs(user['id'])
    return coa_store.entries_for_slugs(slugs)


@api_router.get('/me/coa/{lot}')
async def download_coa(lot: str, user=Depends(get_current_user)):
    """Descarga el PDF de un lote, si el usuario tiene derecho a verlo."""
    entry = coa_store.entry_for_lot(lot)
    if not entry:
        raise HTTPException(status_code=404, detail='COA no encontrado')

    if not entry.get('public'):
        slugs = await _user_product_slugs(user['id'])
        if entry.get('product_slug') not in slugs:
            # 404 y no 403: no confirmamos qué lotes existen a quien no compró.
            raise HTTPException(status_code=404, detail='COA no encontrado')

    path = coa_store.file_path_for(entry)
    if not path:
        raise HTTPException(status_code=404, detail='El archivo del COA no está disponible')
    return FileResponse(path, media_type='application/pdf', filename=f'COA-{entry["lot"]}.pdf')


# ------------------------------------------------------ fichas técnicas (RUO)
# Las fichas NO se publican: no hay índice ni carpeta navegable. Se entregan
# por dos vías, y solo por esas dos.
#
#   1. A quien compró ese producto (igual que los COA).
#   2. A quien la pida por el chat, con un enlace firmado que caduca.
#
# El contenido lo genera `fichas-tecnicas/build_fichas.py` y por regla no
# lleva dosis, pautas de administración, farmacocinética humana ni sellos de
# agencias. Ver ficha_store.py.


def _ve_el_catalogo_completo(user) -> bool:
    """¿Esta persona ve TODAS las fichas, no solo las de lo que compró?

    Un distribuidor vende el catálogo entero: necesita la ficha de cualquier
    compuesto para contestarle a su cliente antes de que exista el pedido.
    Atarlo a lo que él compró lo dejaba sin material de venta. Decisión de
    Christian, 2026-07-29. Los COA siguen igual: esos acreditan un lote que se
    entregó, y solo los ve quien lo recibió.
    """
    return user.get('role') in ('distributor', 'admin')


@api_router.get('/me/fichas')
async def mis_fichas(user=Depends(get_current_user)):
    """Fichas técnicas: el catálogo completo si es distribuidor, y si no, las
    de los productos que el usuario compró."""
    if _ve_el_catalogo_completo(user):
        return ficha_store.para_slugs(ficha_store.slugs_disponibles())
    slugs = await _user_product_slugs(user['id'])
    return ficha_store.para_slugs(slugs)


@api_router.get('/me/ficha/{slug}')
async def descargar_mi_ficha(slug: str, user=Depends(get_current_user)):
    """Descarga la ficha de un producto que el usuario compró (o cualquiera,
    si es distribuidor)."""
    if _ve_el_catalogo_completo(user):
        permitidas = set(ficha_store.slugs_disponibles())
    else:
        # Lo que compró son PRESENTACIONES ("retatrutida-40-mg"); `para_slugs`
        # las traduce al compuesto, que es como se llama la ficha.
        comprados = await _user_product_slugs(user['id'])
        permitidas = {f['product_slug'] for f in ficha_store.para_slugs(comprados)}
    if slug not in permitidas or not ficha_store.existe(slug):
        # 404 y no 403: a quien no compró no se le confirma qué fichas hay.
        raise HTTPException(status_code=404, detail='Ficha no encontrada')
    path = ficha_store.ruta_de(slug)
    if not path:
        raise HTTPException(status_code=404, detail='Ficha no encontrada')
    return FileResponse(path, media_type='application/pdf',
                        filename=ficha_store.nombre_descarga(slug))


@api_router.post('/ficha/solicitar')
async def solicitar_ficha(payload: dict, request: Request):
    """Emite un enlace con caducidad para una ficha. La usa el chat de IA.

    No exige cuenta a propósito: el objetivo es que quien pregunta por datos
    técnicos los reciba. Queda registrado quién lo pidió, y el enlace muere
    solo, así que no equivale a publicar el PDF.
    """
    slug = (payload or {}).get('slug') or ''
    if not ficha_store.existe(slug):
        raise HTTPException(status_code=404, detail='Ficha no encontrada')

    token = ficha_store.emitir_enlace(slug)
    if not token:
        raise HTTPException(status_code=404, detail='Ficha no encontrada')

    await db.ficha_requests.insert_one({
        'slug': slug,
        'email': ((payload or {}).get('email') or '').strip().lower() or None,
        'session_id': (payload or {}).get('session_id'),
        'ip': (request.client.host if request.client else None),
        'created_at': datetime.now(timezone.utc),
    })
    return {'url': f'/api/ficha/descargar?t={token}',
            'expira_en_horas': ficha_store.ENLACE_HORAS}


# --------------------------------------------- credenciales de pasarelas de pago
# Christian trabaja desde el teléfono y no puede entrar por SSH cada vez que rota
# una llave. Estas rutas le dejan pegarlas desde el Admin.
#
# El `.env` del servidor SIEMPRE manda: si la variable está en el entorno, la de
# la base se ignora. El valor nunca se devuelve al navegador, solo si está
# configurado y sus últimos 4 caracteres. Ver secretos.py.


@api_router.get('/admin/credenciales')
async def credenciales_estado(admin=Depends(get_current_admin)):
    """Qué pasarelas están configuradas. Nunca devuelve los valores."""
    return await secretos.estado(db)


@api_router.put('/admin/credenciales')
async def credenciales_guardar(payload: dict, admin=Depends(get_current_admin)):
    """Guarda o borra una credencial. Mandar '' borra la que hubiera."""
    nombre = (payload or {}).get('nombre') or ''
    if nombre not in secretos.PERMITIDAS:
        raise HTTPException(status_code=400, detail='Credencial no reconocida')
    if os.environ.get(nombre):
        raise HTTPException(
            status_code=409,
            detail='Esa llave viene del servidor y manda sobre el panel. Para '
                   'editarla desde aquí, primero hay que quitarla del .env.')
    await secretos.guardar(db, nombre, (payload or {}).get('valor') or '')
    # Sin recargar, la pasarela seguiría usando la llave vieja hasta el próximo
    # reinicio: el cache es lo que leen mercadopago.py y compañía.
    await secretos.recargar(db)
    return {'ok': True, 'estado': await secretos.estado(db)}


@api_router.get('/ficha/descargar')
async def descargar_ficha_con_enlace(t: str = ''):
    """Descarga por enlace firmado. Sin token válido no hay archivo."""
    slug = ficha_store.validar_enlace(t)
    if not slug:
        raise HTTPException(status_code=404, detail='Enlace no válido o vencido')
    path = ficha_store.ruta_de(slug)
    if not path:
        raise HTTPException(status_code=404, detail='Ficha no encontrada')
    return FileResponse(path, media_type='application/pdf',
                        filename=ficha_store.nombre_descarga(slug))


# ------------------------------------------------ perfil de salud del cliente
# El seguimiento personalizado que pidió Christian: peso, % de grasa y lo que le
# indicó su médico. Con eso el calendario deja de ser genérico.
#
# ⚠️ El candado (`consulto_medico` + `tiene_analisis`) es parte del flujo, no
# letra chica al pie. El sitio NO decide dosis: acompaña la que el cliente y su
# médico ya decidieron. `puede_seguir` es lo que el frontend debe consultar
# antes de dejar configurar nada.

@api_router.get('/me/perfil-salud')
async def get_perfil_salud(user=Depends(get_current_user)):
    p = await db.perfiles_salud.find_one({'user_id': user['id']}, {'_id': 0, 'user_id': 0}) or {}
    return {
        'perfil': p,
        'puede_seguir': bool(p.get('consulto_medico') and p.get('tiene_analisis')),
        # Lo que falta para poder usar el seguimiento, dicho en claro.
        'falta': [m for m, ok in (
            ('Confirmar que ya consultaste a un médico', p.get('consulto_medico')),
            ('Confirmar que tienes análisis previos', p.get('tiene_analisis')),
        ) if not ok],
    }


@api_router.put('/me/perfil-salud')
async def set_perfil_salud(payload: PerfilSalud, user=Depends(get_current_user)):
    deny_view_as(user)          # en modo "ver como" el admin no escribe nada
    doc = payload.model_dump()
    doc['actualizado'] = now_iso()
    await db.perfiles_salud.update_one({'user_id': user['id']},
                                       {'$set': {**doc, 'user_id': user['id']}}, upsert=True)
    return await get_perfil_salud(user)


@api_router.get('/me/protocols')
async def list_protocols(user=Depends(get_current_user)):
    rows = await db.protocols.find({'user_id': user['id']}, {'_id': 0}).to_list(200)
    rows.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    return [_protocol_projection(r) for r in rows]


@api_router.post('/me/protocols')
async def create_protocol(payload: ProtocolInput, user=Depends(get_current_user)):
    deny_view_as(user)
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
    deny_view_as(user)
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
    deny_view_as(user)
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


# ----------------- Estudios de laboratorio -----------------
LAB_MAX_BYTES = 8 * 1024 * 1024
LAB_MIME_TYPES = {'application/pdf', 'image/jpeg', 'image/png', 'image/webp', 'image/heic'}

LAB_DISCLAIMER = (
    'Esto NO es un diagnóstico médico ni una indicación de tratamiento. Es una explicación '
    'educativa de lo que miden tus marcadores, generada automáticamente. Los rangos de referencia '
    'varían entre laboratorios: manda siempre el rango impreso en tu hoja. Solo un profesional de '
    'la salud puede interpretar tus resultados en el contexto de tu historia clínica.'
)


async def _user_compound_names(user_id: str) -> list:
    """Compuestos que este cliente compró (pedidos pagados) o registró en su seguimiento.

    Es lo que acota la herramienta: sin compuestos no hay marcadores que mostrar.
    """
    names = []
    orders = await db.orders.find(
        {'user_id': user_id, 'status': {'$in': ['confirmado', 'enviado', 'entregado']}}, {'_id': 0, 'items': 1}
    ).to_list(200)
    for o in orders:
        names += [it.get('name', '') for it in o.get('items', [])]
    protocols = await db.protocols.find({'user_id': user_id}, {'_id': 0, 'product_name': 1}).to_list(200)
    names += [p.get('product_name', '') for p in protocols]
    return [n for n in names if n]


def _decorate_report(report: dict, sex: str, allowed_keys: set) -> dict:
    """Añade rango, clasificación y explicación a cada marcador, y filtra los que
    no tienen que ver con los compuestos del cliente."""
    out_markers = []
    for m in report.get('markers', []):
        key = m.get('key') or ''
        catalog = MARKERS_BY_KEY.get(key)
        if catalog and key not in allowed_keys:
            continue          # marcador conocido pero ajeno a sus compuestos
        low, high = range_for(catalog, sex) if catalog else (None, None)
        out_markers.append({
            **m,
            'group': catalog['group'] if catalog else 'Otros',
            'plain': catalog['plain'] if catalog else '',
            'ref_low': low,
            'ref_high': high,
            'status': evaluate(key, m.get('value'), sex) if catalog else None,
        })
    return {**report, 'markers': out_markers}


@api_router.get('/me/labs')
async def list_lab_reports(user=Depends(get_current_user)):
    """Estudios del cliente, ya evaluados contra los rangos de referencia."""
    names = await _user_compound_names(user['id'])
    families = families_for_products(names)
    allowed = {m['key'] for m in relevant_markers(families)}
    rows = await db.lab_reports.find({'user_id': user['id']}, {'_id': 0}).to_list(100)
    rows.sort(key=lambda r: (r.get('taken_at') or r.get('created_at') or ''), reverse=True)
    reports = [_decorate_report(r, r.get('sex') or '', allowed) for r in rows]

    # Serie por marcador para poder graficar la evolución.
    series = {}
    for r in sorted(reports, key=lambda x: (x.get('taken_at') or x.get('created_at') or '')):
        stamp = (r.get('taken_at') or r.get('created_at') or '')[:10]
        for m in r['markers']:
            if m.get('key') and m.get('value') is not None:
                series.setdefault(m['key'], []).append({'date': stamp, 'value': m['value']})

    return {
        'reports': reports,
        'series': series,
        'relevant_markers': [
            {'key': m['key'], 'label': m['label'], 'unit': m['unit'], 'group': m['group'], 'plain': m['plain']}
            for m in relevant_markers(families)
        ],
        'families': sorted(families),
        'disclaimer': LAB_DISCLAIMER,
    }


@api_router.post('/me/labs/extract')
async def extract_lab_file(file: UploadFile = File(...), user=Depends(get_current_user)):
    """Convierte un PDF o foto del laboratorio a texto UNA sola vez.

    No guardamos el archivo: devolvemos los valores para que el cliente los
    revise y confirme antes de guardarlos.
    """
    deny_view_as(user)
    if file.content_type not in LAB_MIME_TYPES:
        raise HTTPException(status_code=400, detail='Solo aceptamos PDF, JPG, PNG, WEBP o HEIC')
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail='El archivo esta vacio')
    if len(data) > LAB_MAX_BYTES:
        raise HTTPException(status_code=400, detail='El archivo pesa mas de 8 MB')
    try:
        raw = await extract_lab_report(data, file.content_type)
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail='No pudimos leer el estudio. Intenta con una foto mas nitida o captura los valores a mano.')
    except Exception as e:
        logger.error(f'Lab extraction error: {e}')
        raise HTTPException(status_code=502, detail='No pudimos procesar el archivo. Intenta de nuevo en un momento.')

    markers = []
    for m in parsed.get('markers') or []:
        try:
            markers.append({
                'key': (m.get('key') or '').strip(),
                'label': (m.get('label') or '').strip(),
                'value': float(m.get('value')),
                'unit': (m.get('unit') or '').strip(),
                'reference': (m.get('reference') or '').strip(),
            })
        except (TypeError, ValueError):
            continue          # renglon no numerico: se queda solo en el markdown
    return {
        'lab_name': (parsed.get('lab_name') or '').strip(),
        'taken_at': (parsed.get('taken_at') or '').strip(),
        'markdown': parsed.get('markdown') or '',
        'markers': markers,
        'disclaimer': LAB_DISCLAIMER,
    }


@api_router.post('/me/labs')
async def create_lab_report(payload: LabReportInput, user=Depends(get_current_user)):
    deny_view_as(user)
    doc = {
        'id': str(uuid.uuid4()),
        'user_id': user['id'],
        'taken_at': payload.taken_at or now_iso()[:10],
        'lab_name': payload.lab_name,
        'markdown': payload.markdown,
        'sex': payload.sex if payload.sex in ('male', 'female') else '',
        'markers': [m.model_dump() for m in payload.markers],
        'created_at': now_iso(),
    }
    await db.lab_reports.insert_one(doc)
    return {k: v for k, v in doc.items() if k != '_id'}


@api_router.delete('/me/labs/{report_id}')
async def delete_lab_report(report_id: str, user=Depends(get_current_user)):
    deny_view_as(user)
    result = await db.lab_reports.delete_one({'id': report_id, 'user_id': user['id']})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail='Estudio no encontrado')
    return {'ok': True}


@api_router.post('/me/labs/{report_id}/interpret')
async def interpret_lab(report_id: str, user=Depends(get_current_user)):
    """Explicación educativa, acotada a los compuestos del propio cliente."""
    deny_view_as(user)
    report = await db.lab_reports.find_one({'id': report_id, 'user_id': user['id']}, {'_id': 0})
    if not report:
        raise HTTPException(status_code=404, detail='Estudio no encontrado')

    names = await _user_compound_names(user['id'])
    families = families_for_products(names)
    if not families:
        raise HTTPException(
            status_code=400,
            detail='Todavia no tenemos compuestos tuyos. La explicacion se acota a los peptidos que compraste o registraste.',
        )
    allowed = {m['key'] for m in relevant_markers(families)}
    decorated = _decorate_report(report, report.get('sex') or '', allowed)
    if not decorated['markers']:
        raise HTTPException(status_code=400, detail='Este estudio no trae marcadores relacionados con tus compuestos.')

    lines = []
    for m in decorated['markers']:
        rng = f"{m['ref_low']}-{m['ref_high']}" if m['ref_low'] is not None else (m.get('reference') or 'sin rango')
        lines.append(f"- {m['label']}: {m['value']} {m.get('unit', '')} | referencia {rng} | clasificacion: {m.get('status') or 'sin clasificar'}")

    context = (
        f"Compuestos de investigacion del usuario: {', '.join(sorted(set(names))[:20])}.\n"
        f"Vias implicadas: {', '.join(sorted(families))}.\n"
        f"Fecha del estudio: {report.get('taken_at', 'sin fecha')}.\n\n"
        "Marcadores (solo estos; no menciones ningun otro):\n" + '\n'.join(lines)
    )
    try:
        text = await interpret_lab_report(context)
    except Exception as e:
        logger.error(f'Lab interpretation error: {e}')
        raise HTTPException(status_code=502, detail='No pudimos generar la explicacion. Intenta de nuevo en un momento.')

    await db.lab_reports.update_one(
        {'id': report_id}, {'$set': {'interpretation': text, 'interpreted_at': now_iso()}}
    )
    return {'interpretation': text, 'disclaimer': LAB_DISCLAIMER}


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
    # El asistente es nuestro vendedor 24/7: necesita saber QUE vendemos y a que
    # precio, o termina mandando al cliente al correo por cosas que si tenemos.
    catalog = await db.products.find({}, {'_id': 0, 'name': 1, 'price': 1,
                                          'category': 1, 'stock': 1}).to_list(500)
    chat = build_chat(payload.session_id, payload.product_context, payload.language,
                      products=catalog)
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
            # 429 = cuota de Gemini agotada (plan gratis: 20/dia). Mensaje honesto
            # en vez de un error tecnico: el usuario sabe que es demanda, no su culpa.
            if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
                err = ('Nuestro asistente esta recibiendo mucha demanda en este momento. '
                       'Intenta de nuevo en unos minutos o escribenos a hola@exygenlabs.com '
                       'y con gusto te ayudamos.')
            else:
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


# ===========================================================================
#  CHAT IA DE NEGOCIO — detrás de la sesión, candado por rol en el SERVIDOR
# ===========================================================================
#
# Es OTRO chat, no el del sitio. Vive en el panel y responde de negocio:
# cotizaciones, cuánto gana con tal descuento, qué ofrecerle a un cliente.
#
# ⛔ REGLA DE ORO (Christián, 2026-07-30). Costos, proveedores, márgenes y ROI
# son EXCLUSIVOS del admin. El candado no es una frase en el prompt: el contexto
# se ARMA aquí según el rol (ver `chat_negocio.armar_contexto`), así que a un
# distribuidor el costo no le llega porque nunca entró al sobre. Un modelo se
# convence; un `if` en el servidor no.
#
# Tres puertas, todas del lado del servidor:
#   1. `get_current_distributor` — sin sesión 401, cliente 403.
#   2. `deny_view_as` — el "ver como" del admin es SOLO LECTURA: espiar un panel
#      no puede gastar la cuota ni escribir en la conversación de otro.
#   3. La conversación se guarda y se lee por `user_id`: nadie ve la de nadie.

# La conversación previa que se le recuerda al modelo. Corta a propósito: cada
# vuelta re-manda el catálogo entero, y con la cuota gratis de Gemini (20/día)
# un historial largo no compra nada.
NEGOCIO_HISTORIAL = 8


def _negocio_sin_cuota(e) -> bool:
    """¿El error es la cuota de Gemini agotada? (plan gratis: 20 al día)."""
    return '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e)


@api_router.post('/business/chat')
async def business_chat(payload: ChatInput, user=Depends(get_current_distributor)):
    deny_view_as(user)
    # El mismo catálogo que ve el sitio, más los campos con los que se calcula el
    # tope. `tope_de_descuento` recorta a un número: por ahí no se asoma un costo.
    catalog = await db.products.find(
        {}, {'_id': 0, 'name': 1, 'price': 1, 'category': 1, 'stock': 1, 'presentation': 1,
             'id': 1, 'sku': 1, 'commission_cap': 1, 'distributor_eligible': 1, 'hidden': 1},
    ).to_list(1000)
    chat = await chat_negocio.armar_contexto(
        db, user, catalog, tope_de=tope_de_descuento, language=payload.language)

    prior = await db.business_chat_messages.find(
        {'session_id': payload.session_id, 'user_id': user['id']}, {'_id': 0},
    ).sort('created_at', 1).to_list(50)

    await db.business_chat_messages.insert_one({
        'id': str(uuid.uuid4()), 'session_id': payload.session_id, 'user_id': user['id'],
        'role': 'user', 'content': payload.message, 'created_at': now_iso(),
    })

    historia = ''
    if prior:
        lineas = [f"{'Usuario' if m['role'] == 'user' else 'Asesor'}: {m['content']}"
                  for m in prior[-NEGOCIO_HISTORIAL:]]
        historia = ('Conversacion previa:\n' + '\n'.join(lineas)
                    + '\n\nNuevo mensaje del usuario:\n')

    async def event_generator():
        collected = ''
        try:
            async for chunk in stream_reply(chat, historia + payload.message):
                collected += chunk
                yield chunk
        except Exception as e:
            logger.error(f'Chat de negocio: {e}')
            # Sin llave o sin cuota NO se truena: se degrada con un mensaje claro.
            # El asesor es una ayuda, no la caja — que se caiga en silencio con un
            # error técnico en pantalla es peor que decir qué pasó.
            if _negocio_sin_cuota(e):
                err = ('Se acabó la cuota del asistente por hoy (el plan gratuito de '
                       'Google da 20 consultas al día). Vuelve a intentar mañana, o '
                       'avísale a Christián para activar el plan de pago.')
            elif 'GEMINI_API_KEY' in str(e):
                err = ('El asistente todavía no tiene su llave configurada en el '
                       'servidor. Avísale a Christián.')
            else:
                err = 'No pude responder en este momento. Intenta de nuevo en un minuto.'
            collected = err
            yield err
        finally:
            await db.business_chat_messages.insert_one({
                'id': str(uuid.uuid4()), 'session_id': payload.session_id,
                'user_id': user['id'], 'role': 'assistant', 'content': collected,
                'created_at': now_iso(),
            })

    return StreamingResponse(
        event_generator(),
        media_type='text/plain; charset=utf-8',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@api_router.get('/business/history/{session_id}')
async def business_chat_history(session_id: str, user=Depends(get_current_distributor)):
    """SU conversación. El filtro por `user_id` no es cosmético: sin él, adivinar
    el id de sesión de otro distribuidor abriría su chat entero."""
    return await db.business_chat_messages.find(
        {'session_id': session_id, 'user_id': user['id']}, {'_id': 0},
    ).sort('created_at', 1).to_list(100)


# ----------------- Startup: seed -----------------
@app.on_event('startup')
async def arrancar_recuperacion():
    """Cada 15 minutos revisa los carritos abandonados y manda LA oferta a quien
    ya lleva una hora sin cerrar. Una vez por carrito, nunca dos."""
    async def bucle():
        while True:
            await asyncio.sleep(900)
            try:
                await _barrer_intentos()
            except Exception:
                logger.exception('Fallo el barrido de carritos abandonados')
    asyncio.create_task(bucle())


@app.on_event('startup')
async def reanclar_comisiones_en_la_base():
    """TODOS LOS DISTRIBUIDORES ARRANCAN EN 30% — Christián, 2026-07-30.

    «Todos los distribuidores van a empezar a partir de ahora a recibir un 30% de
    comisión (menos el % que hayan otorgado de descuento) y de ahí irán subiendo».

    El piso lo pone `pyramid.BASE_RATE` y cubre a los niveles bajos. Pero hay tres
    tasas puestas A MANO por encima (María 40%, Alanís 40%, Javier 35%) y esas viven
    en la base, no en el código: sin esto seguirían cobrando 40 y 35 mientras el resto
    entra en 30. Aquí se bajan a la base.

    ⚠️ SE GUARDA EL ANTES. Cada tasa anterior queda en el propio usuario
    (`commission_rate_previo`) y la foto completa en `db.migraciones`, para poder
    revertir de un jalón si Christián cambia de opinión.

    Corre sola al arrancar y UNA sola vez: la marca se toma con un upsert atómico,
    así que si dos procesos arrancan a la vez sólo uno la aplica.
    """
    MARCA = 'reancla-comision-30-2026-07-30'
    try:
        tomada = await db.migraciones.update_one(
            {'id': MARCA}, {'$setOnInsert': {'id': MARCA, 'empezada_en': now_iso()}}, upsert=True)
        if tomada.upserted_id is None:
            return   # ya se aplicó (o la está aplicando otro proceso)
        antes = await db.users.find(
            {'role': 'distributor'},
            {'_id': 0, 'id': 1, 'name': 1, 'email': 1, 'tier': 1,
             'commission_rate': 1, 'customer_discount_rate': 1}).to_list(1000)
        cambiados = []
        for u in antes:
            previo = float(u.get('commission_rate') or 0)
            if previo <= pyramid.BASE_RATE + 1e-9:
                continue
            await db.users.update_one({'id': u['id']}, {'$set': {
                'commission_rate': pyramid.BASE_RATE,
                'commission_rate_previo': previo,
                'commission_reanclada_en': now_iso(),
            }})
            fresco = await db.users.find_one({'id': u['id']}, {'_id': 0, 'password_hash': 0})
            # Sus códigos AUTO se rehacen con la tasa nueva: los de 30% y 35% que ya no
            # le corresponden se desactivan solos (ver `_ensure_distributor_codes`).
            await _ensure_distributor_codes(fresco)
            cambiados.append({'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                              'antes': previo, 'despues': pyramid.BASE_RATE})
            logger.warning('COMISIÓN REANCLADA: %s (%s) %.0f%% → %.0f%%',
                           u.get('name'), u.get('email'), previo * 100,
                           pyramid.BASE_RATE * 100)
        await db.migraciones.update_one({'id': MARCA}, {'$set': {
            'aplicada_en': now_iso(), 'base': pyramid.BASE_RATE,
            'antes': antes, 'cambiados': cambiados,
        }})
        logger.info('Reancla de comisiones al %.0f%%: %d distribuidores revisados, %d cambiados.',
                    pyramid.BASE_RATE * 100, len(antes), len(cambiados))
    except Exception:
        # Que no tumbe el arranque: el piso de 30% ya lo da el código aunque esto falle.
        logger.exception('No se pudo reanclar las comisiones en la base del canal')


@app.on_event('startup')
async def avisos_de_ventas_atrasados():
    """LAS VENTAS QUE NUNCA SONARON LA CAMPANITA — Christián, 2026-07-30.

    Las ventas de estos días se repartieron y se cobraron bien, pero la campanita no
    existía: ni Christián ni el distribuidor se enteraron DENTRO de la app. Este barrido
    las genera hacia atrás, una sola vez, sobre los pedidos VIVOS de la última semana.

    No hace falta que sea perfecto ni que corra siempre: `avisar_de_la_venta` deduplica
    por número de pedido, así que aunque se repitiera no ensucia nada.
    """
    MARCA = 'avisos-de-venta-atrasados-2026-07-30'
    try:
        tomada = await db.migraciones.update_one(
            {'id': MARCA}, {'$setOnInsert': {'id': MARCA, 'empezada_en': now_iso()}}, upsert=True)
        if tomada.upserted_id is None:
            return
        desde = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        pedidos = await db.orders.find(
            {'created_at': {'$gte': desde}, 'status': {'$ne': 'cancelado'},
             'archived': {'$ne': True}}, {'_id': 0}).to_list(200)
        for o in pedidos:
            await avisar_de_la_venta(o)
        await db.migraciones.update_one({'id': MARCA}, {'$set': {
            'aplicada_en': now_iso(),
            'pedidos': [o.get('order_number') for o in pedidos]}})
        logger.info('Campanita al día: %d ventas avisadas hacia atrás.', len(pedidos))
    except Exception:
        logger.exception('No pude generar los avisos de venta atrasados')


@app.on_event('startup')
async def avisos_de_venta_apuntan_al_cliente():
    """LOS AVISOS QUE YA ESTABAN GUARDADOS TAMBIÉN ABREN LA FICHA — Christián, 2026-07-30.

    Desde hoy cada aviso de venta nace con `order_number` y `client_id`, así que tocarlo
    abre el pedido y a quien lo hizo. Los que ya estaban guardados (los de las ventas de
    María de esta misma semana) nacieron sin esos campos: el número de pedido se puede
    leer del texto, pero el cliente no está en ninguna parte. Sin este barrido, los
    únicos avisos de venta que existen hoy serían justo los que no llevan a la persona.

    Una sola vez, con marca en `migraciones`, y sin tocar nada más del aviso."""
    MARCA = 'avisos-de-venta-con-cliente-2026-07-30'
    try:
        tomada = await db.migraciones.update_one(
            {'id': MARCA}, {'$setOnInsert': {'id': MARCA, 'empezada_en': now_iso()}}, upsert=True)
        if tomada.upserted_id is None:
            return
        avisos = await db.notifications.find(
            {'type': {'$in': ['venta_admin', 'new_sale']}}, {'_id': 0}).to_list(1000)
        arreglados = 0
        for n in avisos:
            if n.get('client_id') and n.get('order_number'):
                continue
            m = re.search(r'EX-\d{6,}-\d+', f"{n.get('title') or ''} {n.get('body') or ''}")
            if not m:
                continue
            o = await db.orders.find_one({'order_number': m.group(0)}, {'_id': 0})
            if not o:
                continue
            await db.notifications.update_one({'id': n['id']}, {'$set': {
                k: v for k, v in {'order_number': m.group(0),
                                  'client_id': _id_de_cliente(o)}.items() if v}})
            arreglados += 1
        await db.migraciones.update_one({'id': MARCA},
                                        {'$set': {'aplicada_en': now_iso(), 'avisos': arreglados}})
        logger.info('Avisos de venta que ya apuntan al cliente: %d.', arreglados)
    except Exception:
        logger.exception('No pude apuntar los avisos de venta al cliente')


@app.on_event('startup')
async def seed_db():
    try:
        # Llaves de pasarelas que Christian pegó desde el Admin. El .env manda,
        # así que esto solo llena lo que no venga del entorno. Ver secretos.py.
        cargadas = await secretos.recargar(db)
        if cargadas:
            logger.info('Credenciales de pasarela cargadas del panel: %s', cargadas)

        # Remitente y cajas capturados en Admin → Envíos. Misma regla: el .env manda.
        await _cargar_ajustes_envio()
        if not skydropx.remitente_configurado():
            logger.warning('Envios: el REMITENTE no esta configurado. No se podran '
                           'comprar guias hasta capturarlo en Admin → Envios.')

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


# ---------- Videos de tutoriales (solo miembros con sesion) ----------
from pathlib import Path as _Path

TUTORIAL_DIR = _Path(__file__).parent / 'tutorial_videos'
# Los videos del panel de distribuidor no se sirven a clientes.
TUTORIAL_DIST_ONLY = {
    'tutorial-1-panel-distribuidor.mp4',
    'tutorial-2-mis-codigos.mp4',
    'tutorial-3-mis-clientes.mp4',
    'tutorial-4-pedidos-y-ventas.mp4',
    'tutorial-5-novedades.mp4',
}
# El video de métricas de difusión enseña ventas, gastos de anuncios e ingresos
# internos: SOLO admin, ni clientes ni distribuidores.
TUTORIAL_ADMIN_ONLY = {
    'tutorial-12-metricas-difusion.mp4',
    'tutorial-12-metricas-difusao-pt.mp4',   # el mismo video, narrado en portugués para María
}


def tutorial_allowed(filename: str, user: dict) -> bool:
    """Un cliente solo ve videos de cliente; distribuidor/admin ven todo.
    Los videos internos del negocio (difusión) son para admin o para quien
    lleva la difusión — rol 'marketing' propio o como rol extra (María)."""
    role = user.get('role', 'client')
    difusion = role in ('admin', 'marketing') or 'marketing' in (user.get('extra_roles') or [])
    if filename in TUTORIAL_ADMIN_ONLY:
        return difusion
    if filename in TUTORIAL_DIST_ONLY:
        return role in ('distributor', 'admin')
    return True


def parse_range_header(header, file_size: int):
    """Devuelve (inicio, fin) inclusivos para un header Range, o None si no aplica.

    Safari exige respuestas 206 para <video>; starlette 0.37 no trae soporte
    de rangos en FileResponse, asi que lo resolvemos aqui.
    """
    if not header or not header.startswith('bytes=') or file_size <= 0:
        return None
    spec = header[6:].split(',')[0].strip()
    if '-' not in spec:
        return None
    start_s, _, end_s = spec.partition('-')
    try:
        if start_s == '':
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, file_size - n), file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None
    if start > end or start >= file_size:
        return None
    return start, min(end, file_size - 1)


@api_router.get('/tutorials/{filename}')
async def tutorial_video(filename: str, request: Request, token: str = Query(...)):
    # El token viaja como query porque la etiqueta <video> no manda headers.
    import jwt as _jwt
    from auth import JWT_SECRET, JWT_ALGORITHM
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get('sub')
    except Exception:
        raise HTTPException(status_code=401, detail='No autenticado')
    user = await db.users.find_one({'id': user_id}, {'_id': 0, 'role': 1, 'blocked': 1, 'extra_roles': 1})
    if not user or user.get('blocked'):
        raise HTTPException(status_code=401, detail='No autenticado')
    if '/' in filename or '..' in filename or not filename.endswith('.mp4'):
        raise HTTPException(status_code=404, detail='No encontrado')
    if not tutorial_allowed(filename, user):
        raise HTTPException(status_code=403, detail='Solo para distribuidores')
    path = TUTORIAL_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail='No encontrado')
    size = path.stat().st_size
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'private, max-age=3600'}
    rng = parse_range_header(request.headers.get('range'), size)
    if rng is None:
        return FileResponse(path, media_type='video/mp4', headers=headers)
    start, end = rng
    with open(path, 'rb') as f:
        f.seek(start)
        chunk = f.read(end - start + 1)
    headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    from starlette.responses import Response as _Response
    return _Response(content=chunk, status_code=206, media_type='video/mp4', headers=headers)


@api_router.post('/admin/backfill-order-skus')
async def backfill_order_skus(admin=Depends(get_current_admin)):
    """Migra pedidos viejos: cambia el product_id inventado por el SKU real.

    Los pedidos hechos antes del 2026-07-25 guardaron ids tipo "slug::5 mg". Se
    reemplazan casando por NOMBRE exacto del renglon con el catalogo.
    """
    prods = await db.products.find({}, {'_id': 0, 'id': 1, 'sku': 1, 'name': 1}).to_list(1000)
    por_nombre = {(p.get('name') or '').strip().lower(): p for p in prods}
    validos = {p['id'] for p in prods} | {p.get('sku') for p in prods if p.get('sku')}

    orders = await db.orders.find({}, {'_id': 0, 'id': 1, 'items': 1}).to_list(5000)
    tocados = renglones = sin_match = 0
    for o in orders:
        items = o.get('items') or []
        cambio = False
        for it in items:
            pid = it.get('product_id')
            if pid in validos:
                continue                      # ya apunta a algo real
            m = por_nombre.get((it.get('name') or '').strip().lower())
            if m and m.get('sku'):
                it['product_id'] = m['sku']
                cambio = True
                renglones += 1
            else:
                sin_match += 1
        if cambio:
            await db.orders.update_one({'id': o['id']}, {'$set': {'items': items}})
            tocados += 1
    return {'pedidos_migrados': tocados, 'renglones_corregidos': renglones,
            'renglones_sin_match': sin_match, 'pedidos_totales': len(orders)}


@api_router.post('/admin/backfill-skus')
async def backfill_skus(admin=Depends(get_current_admin)):
    """Asigna SKU a todo producto que no lo tenga. Idempotente y sin colisiones."""
    prods = await db.products.find({}, {'_id': 0, 'id': 1, 'name': 1, 'presentation': 1,
                                        'sku': 1}).to_list(1000)
    usados = {p['sku'] for p in prods if p.get('sku')}
    hechos, colisiones = 0, 0
    for p in prods:
        if p.get('sku'):
            continue
        base = gen_sku(p.get('name', ''), p.get('presentation', ''))
        sku = base
        n = 2
        while sku in usados:          # dos presentaciones no pueden compartir SKU
            sku = f'{base}-{n}'
            n += 1
            colisiones += 1
        usados.add(sku)
        await db.products.update_one({'id': p['id']}, {'$set': {'sku': sku}})
        hechos += 1
    return {'asignados': hechos, 'colisiones_resueltas': colisiones, 'total': len(prods)}


# ----------------- Admin: "ver como" (solo lectura) -----------------
@api_router.post('/admin/view-as/{user_id}')
async def admin_view_as(user_id: str, admin=Depends(get_current_admin)):
    """Devuelve un token TEMPORAL (30 min) para ver el panel de ese usuario tal
    como lo ve él. Solo lectura: cualquier escritura se rechaza."""
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'name': 1, 'role': 1, 'blocked': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Usuario no encontrado')
    if u.get('role') == 'admin':
        raise HTTPException(status_code=400, detail='No se puede ver como otro admin')
    token = create_view_as_token(admin['id'], user_id)
    return {'token': token, 'name': u.get('name'), 'role': u.get('role'), 'minutes': 30}


# ----------------- Embudo de venta / efectividad de publicidad -----------------
EVENT_TYPES = ('visit', 'product_view', 'add_to_cart', 'checkout_start', 'purchase')


@api_router.post('/events')
async def track_event(payload: TrackEvent):
    """Registra un paso del embudo. Publico y anonimo: sirve para saber si la
    gente que llega (sobre todo de publicidad) esta comprando o donde se cae."""
    if payload.type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail='Tipo de evento no valido')
    doc = payload.model_dump()
    doc['id'] = str(uuid.uuid4())
    doc['created_at'] = now_iso()
    await db.events.insert_one(doc)
    return {'ok': True}


# ----------------- Intentos de compra (carritos abandonados) -----------------
class IntentoCompra(BaseModel):
    email: str = ''
    name: str = ''
    phone: str = ''
    items: List[dict] = []
    subtotal: float = 0
    total: float = 0
    session_id: str = ''


@api_router.post('/checkout/intento')
async def registrar_intento(payload: IntentoCompra):
    """Guarda que ALGUIEN estuvo a punto de comprar y no cerró.

    Se llama desde el checkout mientras el cliente llena sus datos. No es un
    pedido: vive aparte, con estatus 'pendiente', y NO cuenta en ingresos ni en
    el contador de pedidos (Christian, 2026-07-25). Sirve para que la IA le dé
    seguimiento y trate de cerrar la venta.

    Se actualiza el mismo registro (por correo, o por sesión si aún no lo puso),
    no se crea uno por cada tecla."""
    email = (payload.email or '').strip().lower()
    if not email and not payload.session_id:
        return {'ok': False}
    if not payload.items:
        return {'ok': False}
    clave = {'email': email} if email else {'session_id': payload.session_id}
    ahora = now_iso()
    doc = {
        **clave,
        'name': (payload.name or '').strip(),
        'phone': (payload.phone or '').strip(),
        'items': payload.items[:50],
        'subtotal': float(payload.subtotal or 0),
        'total': float(payload.total or 0),
        'session_id': payload.session_id or '',
        'status': 'pendiente',
        'updated_at': ahora,
    }
    existing = await db.checkout_intentos.find_one(clave, {'_id': 0, 'id': 1, 'contacted': 1})
    if existing:
        await db.checkout_intentos.update_one({'id': existing['id']}, {'$set': doc})
        return {'ok': True, 'id': existing['id']}
    doc.update({'id': str(uuid.uuid4()), 'created_at': ahora, 'contacted': False,
                'contacted_at': None, 'offer_code': None, 'offer_rate': 0})
    if email:
        doc['email'] = email
    await db.checkout_intentos.insert_one(doc)
    return {'ok': True, 'id': doc['id']}


async def _cerrar_intentos(email, session_id=None):
    """Ese carrito sí se cerró: el intento deja de estar pendiente."""
    o = [{'email': (email or '').strip().lower()}] if email else []
    if session_id:
        o.append({'session_id': session_id})
    if not o:
        return
    await db.checkout_intentos.update_many(
        {'$or': o, 'status': 'pendiente'},
        {'$set': {'status': 'convertido', 'converted_at': now_iso()}})


async def _mandar_oferta(intento):
    """Manda LA oferta (una sola) de un carrito abandonado.

    Abajo de $2,500 no lleva cupón: solo un recordatorio. Arriba, el cupón exige
    comprar el mismo monto o más — si no, quitarían productos para usarlo."""
    oferta = recovery.offer_for(intento.get('total'))
    if oferta['kind'] == 'nada':
        return None
    nombre = intento.get('name') or ''
    marca = {'contacted': True, 'contacted_at': now_iso(), 'offer_kind': oferta['kind']}
    code = None
    if oferta['kind'] == 'cupon':
        code = 'VUELVE-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        while await db.discount_codes.find_one({'code': code}):
            code = 'VUELVE-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        expira = (datetime.now(timezone.utc) + timedelta(days=recovery.COUPON_DAYS)).isoformat()
        await db.discount_codes.insert_one({
            'id': str(uuid.uuid4()), 'code': code, 'kind': 'coupon',
            'user_id': None,                       # por correo, no por cuenta
            'email': intento.get('email'),
            'discount_rate': oferta['rate'],
            'min_order': oferta['min_order'],      # EL CANDADO del monto
            'active': True, 'used': False, 'single_use': True,
            'note': f'Recuperación de carrito · {oferta["perk_text"]}',
            'created_by': 'ia_recuperacion', 'created_at': now_iso(), 'expires_at': expira,
        })
        marca.update({'offer_code': code, 'offer_rate': oferta['rate'],
                      'offer_min_order': oferta['min_order'], 'offer_perks': oferta['perks'],
                      'offer_gifts': oferta['gifts'],
                      'offer_perk_text': oferta['perk_text']})
    await db.checkout_intentos.update_one({'id': intento['id']}, {'$set': marca})
    try:
        await send_cart_recovery_email(nombre, intento.get('email'), intento.get('items', []),
                                       oferta, code)
    except Exception as e:
        logger.warning('No se pudo mandar la oferta de recuperación a %s: %s',
                       intento.get('email'), e)
    return code


@api_router.get('/admin/intentos')
async def admin_intentos(admin=Depends(get_current_admin)):
    """Carritos que no se cerraron, el más reciente primero."""
    rows = await db.checkout_intentos.find({}, {'_id': 0}).to_list(1000)
    rows.sort(key=lambda r: r.get('updated_at', ''), reverse=True)
    pend = [r for r in rows if r.get('status') == 'pendiente']
    return {
        'intentos': rows[:300],
        'pendientes': len(pend),
        'valor_pendiente': sum(r.get('total', 0) for r in pend),
        'recuperados': sum(1 for r in rows if r.get('status') == 'convertido' and r.get('contacted')),
        'minimo_para_cupon': recovery.MIN_FOR_OFFER,
    }


@api_router.post('/admin/intentos/{intento_id}/oferta')
async def admin_forzar_oferta(intento_id: str, admin=Depends(get_current_admin)):
    """Manda la oferta YA, sin esperar la hora (para probar o para empujar)."""
    it = await db.checkout_intentos.find_one({'id': intento_id}, {'_id': 0})
    if not it:
        raise HTTPException(status_code=404, detail='Intento no encontrado')
    if it.get('contacted'):
        raise HTTPException(status_code=400, detail='A este cliente ya se le escribió una vez')
    code = await _mandar_oferta(it)
    return {'enviado': True, 'codigo': code, 'oferta': recovery.offer_for(it.get('total'))}


@api_router.delete('/admin/intentos/{intento_id}')
async def admin_borrar_intento(intento_id: str, admin=Depends(get_current_admin)):
    r = await db.checkout_intentos.delete_one({'id': intento_id})
    if not r.deleted_count:
        raise HTTPException(status_code=404, detail='Intento no encontrado')
    return {'deleted': True}


async def _barrer_intentos():
    """Cada rato: a quién ya toca escribirle. UNA vez por carrito, nunca dos."""
    try:
        pend = await db.checkout_intentos.find(
            {'status': 'pendiente', 'contacted': False}, {'_id': 0}).to_list(500)
    except Exception as e:
        logger.warning('No pude leer los intentos: %s', e)
        return
    ahora = datetime.now(timezone.utc)
    for it in pend:
        try:
            visto = datetime.fromisoformat(it.get('updated_at') or it.get('created_at'))
            minutos = (ahora - visto).total_seconds() / 60
        except (TypeError, ValueError):
            continue
        if recovery.should_contact(it, minutos):
            await _mandar_oferta(it)


@api_router.delete('/admin/orders/{order_id}')
async def admin_delete_order(order_id: str, admin=Depends(get_current_admin)):
    """BORRA un pedido para siempre. Solo el admin, y no hay deshacer.

    Antes de borrarlo devuelve lo que la orden se llevó: los puntos de lealtad que
    depositó (o los que el cliente canjeó). Las comisiones viven DENTRO de la orden,
    así que se van con ella y los reportes dejan de contarla — que es justo lo que
    se busca al borrar un pedido que nunca existió.

    Para un pedido de verdad casi siempre conviene 'cancelado' en vez de borrar:
    deja rastro. Borrar es para lo que nunca debió estar ahí."""
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if order.get('status') != 'cancelado':
        await revoke_order_points(order)     # devuelve puntos ganados y canjeados
    await restore_order_stock(order)         # y devuelve las piezas al inventario
    await db.orders.delete_one({'id': order_id})
    await db.points.delete_many({'order_id': order_id})
    logger.warning('Admin %s borró el pedido %s (%s, $%s)', admin.get('email'),
                   order.get('order_number'), (order.get('customer') or {}).get('full_name'),
                   order.get('total'))
    return {'deleted': True, 'order_number': order.get('order_number')}


# ----------------- Panel de anuncios de Meta -----------------
class MetaCsv(BaseModel):
    csv: str


@api_router.post('/admin/meta/import')
async def admin_meta_import(payload: MetaCsv, admin=Depends(get_current_marketing)):
    """Sube el CSV del Administrador de Anuncios. Reemplaza la foto anterior:
    el CSV de Meta ya trae el acumulado, no hay que ir sumando."""
    deny_view_as(admin)   # 'ver como' es de SOLO lectura: aquí se escribe
    rows = meta_ads.parse_csv(payload.csv or '')
    if not rows:
        raise HTTPException(status_code=400,
                            detail='Ese archivo no parece el CSV de campañas de Meta '
                                   '(no encuentro la columna "Nombre de la campaña").')
    await db.meta_ads.delete_many({})
    stamp = now_iso()
    await db.meta_ads.insert_many([{**r, 'imported_at': stamp} for r in rows])
    return {'imported': len(rows), 'summary': meta_ads.summarize(rows)}


# --------------------------------------------------------------- Meta en vivo
# Christian: "la info de Meta debe estar live siempre, quiero ver siempre la
# más actual disponible". Dos cosas hacen falta para cumplirlo de verdad:
#
#  1. Pedirle a Meta un rango que INCLUYA HOY (lo resuelve meta_ads.rango()).
#  2. NO servir jamás el CSV viejo disfrazado de dato fresco. Antes, si la API
#     fallaba, el panel caía al último CSV y lo mostraba sin decir nada: se veían
#     números de hace semanas con cara de actuales. Eso es peor que no mostrar
#     nada, porque se decide dinero con ellos.
#
# La caché de 60 s no es por lentitud: es para no chocar con el límite de
# llamadas de Meta cuando el panel se refresca o se abren varias pestañas.
_META_CACHE = {'filas': [], 'at': 0.0, 'days': None}
META_CACHE_SEG = 60


async def _meta_filas(days: int = 30, forzar: bool = False):
    """(filas, estado). `estado` dice SIEMPRE de dónde salió el dato y qué tan viejo es."""
    import time as _time
    ahora_ts = _time.time()
    if meta_ads.live_configured():
        fresca = (not forzar and _META_CACHE['days'] == days
                  and ahora_ts - _META_CACHE['at'] < META_CACHE_SEG and _META_CACHE['filas'])
        if fresca:
            return _META_CACHE['filas'], {
                'fuente': 'meta_en_vivo', 'actualizado': _iso_de(_META_CACHE['at']),
                'edad_segundos': int(ahora_ts - _META_CACHE['at']), 'aviso': ''}
        try:
            filas = await meta_ads.fetch_live(days)
            _META_CACHE.update({'filas': filas, 'at': ahora_ts, 'days': days})
            return filas, {'fuente': 'meta_en_vivo', 'actualizado': now_iso(),
                           'edad_segundos': 0, 'aviso': ''}
        except Exception as e:
            logger.warning('Meta en vivo falló: %s', e)
            filas = await db.meta_ads.find({}, {'_id': 0}).to_list(500)
            viejo = filas[0].get('imported_at', '') if filas else ''
            return filas, {
                'fuente': 'csv_viejo' if filas else 'sin_datos',
                'actualizado': viejo,
                'edad_segundos': None,
                # A la vista y en español: el panel debe pintar esto en rojo.
                'aviso': (f'No se pudo hablar con Meta ({e}). Lo que ves es el último '
                          f'archivo subido{" el " + viejo[:10] if viejo else ""}, NO el dato de hoy.'),
            }
    filas = await db.meta_ads.find({}, {'_id': 0}).to_list(500)
    viejo = filas[0].get('imported_at', '') if filas else ''
    return filas, {
        'fuente': 'csv' if filas else 'sin_datos',
        'actualizado': viejo, 'edad_segundos': None,
        'aviso': ('Sin token de Meta: esto es el último archivo subido, no el dato en vivo.'
                  if filas else 'No hay datos de Meta todavía.'),
    }


def _iso_de(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@api_router.get('/admin/meta/dashboard')
async def admin_meta_dashboard(days: int = 30, admin=Depends(get_current_marketing)):
    """Lo que Meta gastó, cruzado con lo que el sitio de verdad vendió.

    Fuente EN VIVO si hay token de Meta; si no, el último CSV subido. La salida es
    idéntica en los dos casos, así que el panel no cambia cuando llegue el token.

    `estado` dice siempre de dónde salió el dato: el panel NUNCA debe pintar un
    CSV de hace semanas como si fuera de hoy."""
    rows, estado = await _meta_filas(days)
    summary = meta_ads.summarize(rows)
    # Cruce con la realidad: qué vendió el sitio en el mismo periodo.
    desde = summary.get('date_start') or (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    orders = await db.orders.find({'created_at': {'$gte': desde}}, {'_id': 0}).to_list(5000)
    # La variable se llamaba `pagadas` pero eran "las no canceladas": el ROAS de Meta
    # se calculaba con dinero que todavía no había entrado. Ahora los pedidos vivos y
    # los cobrados son dos cosas distintas, y el ingreso sale sólo de los segundos.
    vivas = [o for o in orders if esta_vivo(o)]
    evs = await db.events.find({'created_at': {'$gte': desde}, 'type': 'visit'},
                               {'_id': 0, 'session_id': 1}).to_list(50000)
    visitas = len({e.get('session_id') for e in evs if e.get('session_id')})
    ingreso = sum(cobrado_de(o) for o in vivas)
    return {
        **estado,
        'resumen': summary,
        'campanas': sorted(rows, key=lambda r: -r.get('spend', 0)),
        'sitio': {'visitas': visitas, 'pedidos': len(vivas), 'ingreso': ingreso,
                  'por_cobrar': sum(por_cobrar_de(o) for o in vivas)},
        'recomendaciones': meta_ads.advise(summary, site_visits=visitas,
                                           site_orders=len(vivas), site_revenue=ingreso),
        'apagar': meta_ads.dead_weight(rows),
    }


# ------------------------------------------------------- área de marketing
# El panel de anuncios sabía cuánto se gastó y cuánto se vendió, pero nunca lo
# unía. Aquí se une para poder contestar la pregunta que importa: cuánto costó
# cada cliente que DE VERDAD compró. Ver marketing.py para las reglas que
# impiden que ese número se abarate solo.

# ⚠️ TC FIJO 17.5 — el mismo de la maestra de precios. Christian: "usemos todo a
# 17.50 a menos que yo te diga que hay que recalibrar precios". NO se actualiza
# solo: el día que cambie hay que recalibrar los precios junto con esto.
def _fx():
    return marketing.TC_MAESTRA




async def _pedidos_y_sesiones(days: int):
    """Lo que pasó del lado del SITIO en la ventana: pedidos válidos y sesiones
    únicas por campaña (para poder sacar conversión por campaña)."""
    desde = (datetime.now(timezone.utc) - timedelta(days=max(1, days) - 1)).isoformat()[:10]
    pedidos = await db.orders.find({'created_at': {'$gte': desde}}, {'_id': 0}).to_list(20000)
    pedidos = [o for o in pedidos if o.get('status') not in NO_CUENTAN]

    evs = await db.events.find(
        {'created_at': {'$gte': desde}, 'type': 'visit'},
        {'_id': 0, 'session_id': 1, 'utm_campaign': 1, 'utm_source': 1,
         'utm_content': 1, 'fbclid': 1, 'referrer': 1}
    ).to_list(100000)
    sesiones = {}
    for e in evs:
        c = marketing.campana_del_pedido(e)
        if c:
            sesiones.setdefault(c, set()).add(e.get('session_id'))
    return desde, pedidos, {c: len(s) for c, s in sesiones.items()}


# Qué trajo cada cupón. Christian, 2026-07-26: "cada anuncio de WhatsApp y cada
# correo de seguimiento debe llevar su propio cupón, para poder medir de dónde
# viene la venta".
#
# Es la única forma de medir lo que NO pasa por un enlace: WhatsApp no tiene URL
# donde pegar un utm, y un correo puede reenviarse. El cupón sí viaja con la
# persona hasta el checkout.
#
# El prefijo dice el origen:
#   VUELVE-*  → correo de carrito abandonado (ya existía, `created_by`)
#   WA-*      → anuncio que manda a WhatsApp
#   GIFT-*    → regalo del admin
ORIGEN_POR_PREFIJO = {
    'VUELVE': 'correo de carrito abandonado',
    'WA': 'anuncio de WhatsApp',
    'GIFT': 'regalo del admin',
}


async def _ventas_por_cupon(desde: str):
    """Cuánto trajo cada origen de cupón, y cuántos se mandaron sin usarse."""
    cupones = await db.discount_codes.find({'created_at': {'$gte': desde}}, {'_id': 0}).to_list(5000)
    usados = {c.get('used_order') for c in cupones if c.get('used') and c.get('used_order')}
    pedidos = await db.orders.find(
        {'order_number': {'$in': list(usados)}, 'status': {'$nin': list(NO_CUENTAN)}},
        {'_id': 0, 'order_number': 1, 'total': 1, 'first_order': 1, 'status': 1,
         'paid': 1}).to_list(5000) if usados else []
    por_pedido = {o['order_number']: o for o in pedidos}

    grupos = {}
    for c in cupones:
        origen = ORIGEN_POR_PREFIJO.get(str(c.get('code', '')).split('-')[0].upper(), 'otro')
        g = grupos.setdefault(origen, {'origen': origen, 'mandados': 0, 'usados': 0,
                                       'clientes_nuevos': 0, 'ingreso_mxn': 0.0,
                                       'por_cobrar_mxn': 0.0})
        g['mandados'] += 1
        o = por_pedido.get(c.get('used_order'))
        if o:
            # El cupón SÍ se usó (por eso cuenta en `usados`), pero el dinero sólo
            # entra en el ingreso si de verdad se cobró.
            g['usados'] += 1
            g['ingreso_mxn'] += cobrado_de(o)
            g['por_cobrar_mxn'] += por_cobrar_de(o)
            if o.get('first_order'):
                g['clientes_nuevos'] += 1
    filas = []
    for g in grupos.values():
        filas.append({**g, 'ingreso_mxn': round(g['ingreso_mxn']),
                      'por_cobrar_mxn': round(g['por_cobrar_mxn']),
                      'ingreso': round(marketing.a_usd(g['ingreso_mxn'], _fx()), 2),
                      # De cada 100 cupones mandados, cuántos se usaron. Es la
                      # medida de si la oferta sirve o solo estamos regalando.
                      'uso_pct': round(g['usados'] / g['mandados'] * 100, 1) if g['mandados'] else 0})
    filas.sort(key=lambda f: -f['ingreso'])
    return filas


@api_router.get('/admin/marketing/resumen')
async def admin_marketing_resumen(days: int = 30, admin=Depends(get_current_marketing)):
    """Costo por cliente CON COMPRA HECHA, campaña por campaña.

    Es lo que Christian pidió: no "cuánto costó un clic" sino cuánto costó cada
    persona que terminó pagando. Cada campaña trae además su veredicto en una
    palabra, y lo que no se pudo atribuir se muestra aparte en vez de repartirse.
    """
    filas, estado = await _meta_filas(days)
    desde, pedidos, sesiones = await _pedidos_y_sesiones(days)

    reporte = marketing.cruzar(filas, pedidos, sesiones, fx=_fx())
    return {
        **estado,
        'periodo': {'desde': desde, 'hasta': datetime.now(timezone.utc).isoformat()[:10], 'dias': days},
        'fx': _fx(),
        **reporte,
        # No solo Meta: de dónde llegó CADA venta (WhatsApp, Google, directo…) y
        # cuánto costaron las de distribuidor, cuyo costo real es su comisión.
        'canales': marketing.canales(pedidos, reporte['total']['gasto'], _fx()),
        'cupones': await _ventas_por_cupon(desde),
        # Lo que hay que pegar en cada anuncio para que la campaña se pueda medir.
        'enlaces': [{'campana': f['campana'], 'slug': f['slug'],
                     'url': marketing.enlace(SITE_URL, f['campana'])}
                    for f in reporte['campanas']],
    }


@api_router.get('/admin/marketing/campana/{campaign_id}')
async def admin_marketing_campana(campaign_id: str, days: int = 30,
                                  admin=Depends(get_current_marketing)):
    """La radiografía de UNA campaña: día a día, anuncio por anuncio, y a quién
    se le mostró. Todo del mismo token; no hace falta ningún permiso nuevo."""
    filas, estado = await _meta_filas(days)
    fila = next((r for r in filas if str(r.get('campaign_id')) == str(campaign_id)), None)
    if not fila:
        raise HTTPException(status_code=404, detail='No encuentro esa campaña en el periodo.')

    _, pedidos, sesiones = await _pedidos_y_sesiones(days)
    cruce = marketing.cruzar([fila], pedidos, sesiones, fx=_fx())
    resumen = cruce['campanas'][0]

    # Los cuatro cortes van en paralelo: son cuatro viajes a Meta y en serie se
    # siente lento justo cuando Christian está decidiendo si apaga algo.
    dia, anuncios, edad, donde = await asyncio.gather(
        meta_ads.fetch_dia_a_dia(campaign_id, days),
        meta_ads.fetch_anuncios(campaign_id, days),
        meta_ads.fetch_corte(campaign_id, 'age,gender', days),
        meta_ads.fetch_corte(campaign_id, 'publisher_platform', days),
        return_exceptions=True,
    )
    def _ok(x):
        # Si un corte truena, se devuelve vacío: la radiografía sirve igual con
        # tres de cuatro, y tirar todo por un permiso faltante sería peor.
        return [] if isinstance(x, Exception) else x

    slug = resumen['slug']
    pedidos_campana = [
        {'order_number': o.get('order_number'), 'total': o.get('total'),
         'status': o.get('status'), 'created_at': o.get('created_at'),
         'nuevo': bool(o.get('first_order')),
         'anuncio': (o.get('attribution') or {}).get('utm_content', '')}
        for o in pedidos if marketing.campana_del_pedido(o.get('attribution')) == slug
    ]
    pedidos_campana.sort(key=lambda o: o.get('created_at') or '', reverse=True)

    return {
        **estado,
        'campana': resumen,
        'dia_a_dia': _ok(dia),
        'anuncios': _ok(anuncios),
        'por_edad_sexo': _ok(edad),
        'por_plataforma': _ok(donde),
        # Los pedidos REALES que salieron de esta campaña, con nombre y apellido
        # de número de pedido. Es lo que convierte el reporte en algo verificable.
        'pedidos': pedidos_campana,
        'enlace_sugerido': marketing.enlace(SITE_URL, resumen['campana']),
    }


class DirectorPedido(BaseModel):
    objetivo: str = 'conseguir clientes nuevos'
    presupuesto_mxn: float = 0
    days: int = 90          # el director mira MÁS atrás que el panel: para
                            # aprender conviene todo el historial que haya


@api_router.post('/admin/marketing/director')
async def admin_marketing_director(payload: DirectorPedido, admin=Depends(get_current_marketing)):
    """Modo "director de marketing": arma una campaña nueva desde cero.

    La IA NO inventa los datos: `director.briefing()` arma los hechos desde la
    base (qué se vendió, qué campaña ganó, cuánto costó cada cliente) y eso es lo
    único que ve el modelo. Si no hay historial suficiente, el briefing lo dice y
    la propuesta sale marcada como tal en vez de fingir seguridad.

    Devuelve una PROPUESTA para que Christian apruebe. No publica nada en Meta:
    eso necesita permisos de escritura, revisión de la app y gasta dinero real.
    """
    deny_view_as(admin)   # 'ver como' es de SOLO lectura: aquí se escribe
    days = max(7, min(int(payload.days or 90), 365))
    filas, estado = await _meta_filas(min(days, 90))
    _, pedidos, sesiones = await _pedidos_y_sesiones(days)
    cruce = marketing.cruzar(filas, pedidos, sesiones, fx=_fx())
    productos = await db.products.find({}, {'_id': 0}).to_list(1000)

    brief = director.briefing(cruce['campanas'], pedidos, productos, fx=_fx())
    try:
        propuesta = await director.proponer(brief, payload.objetivo, payload.presupuesto_mxn)
    except Exception as e:
        logger.warning('Director de marketing falló: %s', e)
        raise HTTPException(status_code=503, detail=f'La IA no respondió: {e}')

    nombre = propuesta.get('nombre') or 'campana-nueva'
    return {
        **estado,
        'propuesta': propuesta,
        # El briefing se devuelve completo a propósito: Christian tiene que poder
        # ver en qué se basó la IA, no solo lo que le dijo.
        'briefing': brief,
        'enlace': marketing.enlace(SITE_URL, nombre),
    }


@api_router.get('/admin/series')
async def admin_series(bucket: str = 'day', days: int = 30, admin=Depends(get_current_admin)):
    """Tráfico y ventas a lo largo del tiempo, por DÍA, SEMANA o MES.

    Christian lo pidió tres veces: el panel tenía totales pero no series, así que
    no se podía ver si algo sube o baja. Devuelve un renglón por periodo con
    visitas, sesiones únicas, pedidos e ingreso — todo del MISMO periodo, para que
    se puedan encimar en la misma gráfica.

    Los periodos VACÍOS también salen. Si no, una semana sin ventas desaparece de
    la gráfica y la línea salta como si nunca hubiera existido: se ve un negocio
    que crece cuando en realidad estuvo parado.
    """
    bucket = bucket if bucket in ('day', 'week', 'month') else 'day'
    days = max(1, min(int(days or 30), 730))
    ahora = datetime.now(timezone.utc)
    desde_dt = ahora - timedelta(days=days)
    desde = desde_dt.isoformat()

    def etiqueta(iso: str) -> str:
        """A qué periodo cae una fecha ISO. La semana se nombra por su LUNES."""
        try:
            d = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return ''
        if bucket == 'month':
            return d.strftime('%Y-%m')
        if bucket == 'week':
            return (d - timedelta(days=d.weekday())).strftime('%Y-%m-%d')
        return d.strftime('%Y-%m-%d')

    # Todos los periodos del rango, aunque no haya nada que contar.
    periodos = []
    cursor = desde_dt
    if bucket == 'week':
        cursor -= timedelta(days=cursor.weekday())
    while cursor <= ahora:
        et = etiqueta(cursor.isoformat())
        if et and (not periodos or periodos[-1] != et):
            periodos.append(et)
        cursor += timedelta(days=1 if bucket != 'month' else 28)
    et_hoy = etiqueta(ahora.isoformat())
    if et_hoy and et_hoy not in periodos:
        periodos.append(et_hoy)

    filas = {p: {'periodo': p, 'visitas': 0, 'sesiones': 0, 'pedidos': 0, 'ingreso': 0.0,
                 'por_cobrar': 0.0, 'pedidos_cobrados': 0}
             for p in periodos}
    sesiones = {p: set() for p in periodos}
    # Únicas de TODO el rango, aparte de las de cada cajón. Una sesión que cruza
    # la medianoche es única en el rango pero cae en dos días: si el total fuera
    # la suma de los cajones, el mismo mes daría una conversión distinta al verlo
    # por día que por semana. Y no puede.
    sesiones_rango = set()
    visitantes_rango = set()

    evs = await db.events.find(
        {'created_at': {'$gte': desde}, 'type': 'visit'},
        {'_id': 0, 'created_at': 1, 'session_id': 1, 'visitor_id': 1}).to_list(100000)
    for e in evs:
        p = etiqueta(e.get('created_at'))
        if p in filas:
            filas[p]['visitas'] += 1
            if e.get('session_id'):
                sesiones[p].add(e['session_id'])
        if e.get('session_id'):
            sesiones_rango.add(e['session_id'])
        if e.get('visitor_id'):
            visitantes_rango.add(e['visitor_id'])

    # ⛔ `paid` VIAJA EN LA CONSULTA. Sin ese campo la gráfica no puede distinguir lo
    # cobrado de lo fiado y la línea de ingreso pintaba la venta de Alanís como dinero
    # que entró ($3,857 el 29 de julio). La deuda no desaparece: sale en `por_cobrar`,
    # su propia línea.
    orders = await db.orders.find(
        {'created_at': {'$gte': desde}},
        {'_id': 0, 'created_at': 1, 'total': 1, 'status': 1, 'paid': 1}).to_list(20000)
    for o in orders:
        if not esta_vivo(o):                    # una venta cancelada no es una venta
            continue
        p = etiqueta(o.get('created_at'))
        if p in filas:
            filas[p]['pedidos'] += 1
            filas[p]['ingreso'] += cobrado_de(o)
            filas[p]['por_cobrar'] += por_cobrar_de(o)
            if esta_pagado(o):
                filas[p]['pedidos_cobrados'] += 1

    for p in filas:
        filas[p]['sesiones'] = len(sesiones[p])
        filas[p]['ingreso'] = round(filas[p]['ingreso'], 2)
        filas[p]['por_cobrar'] = round(filas[p]['por_cobrar'], 2)

    serie = [filas[p] for p in periodos]
    total_ing = sum(f['ingreso'] for f in serie)
    total_deuda = sum(f['por_cobrar'] for f in serie)
    total_ped = sum(f['pedidos'] for f in serie)
    total_ped_cobrados = sum(f['pedidos_cobrados'] for f in serie)
    total_ses = len(sesiones_rango)
    return {
        'bucket': bucket,
        'dias': days,
        'serie': serie,
        'totales': {
            'visitas': sum(f['visitas'] for f in serie),
            'sesiones': total_ses,
            # Personas distintas. Una que vuelve tres veces son 3 sesiones pero
            # UN visitante. Los eventos viejos no lo traen, por eso puede salir 0.
            'visitantes': len(visitantes_rango),
            'pedidos': total_ped,
            'ingreso': round(total_ing, 2),
            # Entregado o en camino y todavía sin cobrar. Va al lado del ingreso, no
            # dentro: son las dos mitades de la misma venta y no se pueden sumar.
            'por_cobrar': round(total_deuda, 2),
            # Cuántas de las sesiones acabaron comprando. Es EL número que dice si
            # la publicidad sirve: 683 clics y 1 pedido no es lo mismo que 10 y 1.
            # Un pedido fiado SÍ cuenta como conversión: la publicidad hizo su trabajo.
            'conversion': round(total_ped / total_ses * 100, 2) if total_ses else 0,
            # El ticket se saca de lo cobrado entre los pedidos cobrados. Mezclar
            # ingreso cobrado con TODOS los pedidos daría un ticket que nunca existió.
            'ticket': round(total_ing / total_ped_cobrados) if total_ped_cobrados else 0,
        },
    }


@api_router.get('/admin/funnel')
async def admin_funnel(days: int = 30, admin=Depends(get_current_marketing)):
    """Embudo + origen del trafico. Responde: llega gente? compra? de donde viene?"""
    desde = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    evs = await db.events.find({'created_at': {'$gte': desde}}, {'_id': 0}).to_list(50000)

    # Embudo por SESIONES unicas (no por clics): una persona cuenta una vez por paso.
    pasos = {t: set() for t in EVENT_TYPES}
    for e in evs:
        t = e.get('type')
        if t in pasos:
            pasos[t].add(e.get('session_id'))
    embudo = [{'paso': t, 'sesiones': len(pasos[t])} for t in EVENT_TYPES]

    visitas = len(pasos['visit']) or 1
    compras = len(pasos['purchase'])

    # ⛔ EL INGRESO DEL EMBUDO SE VERIFICA CONTRA LOS PEDIDOS, NO CONTRA EL EVENTO.
    # El evento `purchase` lo escribe el navegador al terminar el checkout y trae el
    # monto pegado, así que sumarlo a secas cuenta como ingreso una venta que después
    # no se cobró (o que se canceló). Con el número de pedido que ya viaja en el
    # evento se le pregunta a la base quién pagó de verdad; lo que no, se va a la
    # cubeta de deuda. Un evento sin número de pedido (los viejos) se sigue creyendo:
    # es lo único que hay de esa época, y borrarlo sería perder historia.
    nums = {e.get('order_number') for e in evs
            if e.get('type') == 'purchase' and e.get('order_number')}
    pagado_por_num = {}
    if nums:
        docs = await db.orders.find({'order_number': {'$in': list(nums)}},
                                    {'_id': 0, 'order_number': 1, 'status': 1,
                                     'paid': 1}).to_list(20000)
        pagado_por_num = {d['order_number']: esta_pagado(d) for d in docs}

    def _estado_del_evento(e) -> str:
        """'cobrado' | 'por_cobrar' | 'fantasma', según el pedido de este evento.

        ⛔ EL FANTASMA IMPORTA. En la base viva hay CINCO eventos de compra ($11,027)
        cuyos pedidos ya no existen: eran los pedidos de prueba que se borraron. El
        embudo los sumaba como ingreso, así que enseñaba $11,027 cobrados con $3,347 en
        la cuenta. Un pedido borrado no es ingreso NI cuenta por cobrar —no hay a quién
        cobrarle— así que no entra en ninguna de las dos cifras; se cuenta aparte.

        Sin número de pedido no hay nada contra qué verificar (eventos de antes de que
        el número viajara): se cree, porque es lo único que hay de esa época.
        """
        num = e.get('order_number')
        if not num:
            return 'cobrado'
        if num not in pagado_por_num:
            return 'fantasma'
        return 'cobrado' if pagado_por_num[num] else 'por_cobrar'

    ingreso = 0.0
    ingreso_por_cobrar = 0.0
    ingreso_fantasma = 0.0
    for e in evs:
        if e.get('type') != 'purchase':
            continue
        monto = float(e.get('value', 0) or 0)
        estado = _estado_del_evento(e)
        if estado == 'cobrado':
            ingreso += monto
        elif estado == 'por_cobrar':
            ingreso_por_cobrar += monto
        else:
            ingreso_fantasma += monto

    # Por origen: de donde vino y cuanto convirtio (esto mide la publicidad).
    origen = {}
    sesion_origen = {}
    for e in sorted(evs, key=lambda x: x.get('created_at', '')):
        sid = e.get('session_id')
        if sid not in sesion_origen:
            src = e.get('utm_source') or ''
            if not src:
                ref = (e.get('referrer') or '').lower()
                if 'facebook' in ref or 'fb.' in ref: src = 'facebook (sin utm)'
                elif 'instagram' in ref: src = 'instagram (sin utm)'
                elif 'google' in ref: src = 'google (sin utm)'
                elif ref: src = 'otro sitio'
                else: src = 'directo'
            sesion_origen[sid] = src
    for e in evs:
        src = sesion_origen.get(e.get('session_id'), 'directo')
        o = origen.setdefault(src, {'origen': src, 'visitas': set(), 'compras': set(),
                                    'ingreso': 0, 'por_cobrar': 0})
        if e.get('type') == 'visit':
            o['visitas'].add(e.get('session_id'))
        if e.get('type') == 'purchase':
            o['compras'].add(e.get('session_id'))
            # Mismo candado que arriba: por origen tampoco se cuenta como ingreso
            # una venta que no se cobró. Si no, el ROAS de un canal se infla con fiado.
            estado_e = _estado_del_evento(e)
            if estado_e == 'cobrado':
                o['ingreso'] += float(e.get('value', 0) or 0)
            elif estado_e == 'por_cobrar':
                o['por_cobrar'] += float(e.get('value', 0) or 0)
    por_origen = sorted(
        [{'origen': v['origen'], 'visitas': len(v['visitas']), 'compras': len(v['compras']),
          'ingreso': round(v['ingreso']), 'por_cobrar': round(v['por_cobrar']),
          'conversion': round(len(v['compras']) / len(v['visitas']) * 100, 1) if v['visitas'] else 0}
         for v in origen.values()],
        key=lambda x: -x['visitas'])

    # Productos mas vistos que NO se venden: donde se pierde el interes.
    vistos = {}
    for e in evs:
        if e.get('type') == 'product_view' and e.get('product'):
            vistos[e['product']] = vistos.get(e['product'], 0) + 1
    top_vistos = sorted([{'producto': k, 'vistas': v} for k, v in vistos.items()],
                        key=lambda x: -x['vistas'])[:10]

    return {
        'dias': days,
        'embudo': embudo,
        'conversion_total': round(compras / visitas * 100, 2),
        'ingreso': round(ingreso),
        'por_cobrar': round(ingreso_por_cobrar),
        # Compras cuyo pedido ya no existe (se borró). No son ingreso ni deuda, pero se
        # dicen: si el embudo enseña compras y cero pesos, la explicación tiene que estar
        # a la vista y no parecer un error del reporte.
        'ingreso_sin_pedido': round(ingreso_fantasma),
        'por_origen': por_origen,
        'top_vistos': top_vistos,
        'sin_datos': len(evs) == 0,
    }


# ----------------- Admin: venta directa (2026-07-23) -----------------
class ManualOrderCreate(BaseModel):
    user_id: str
    items: List[OrderItem]
    discount_rate: float = 0.0        # p. ej. 0.40 en venta directa con Christian
    status: str = 'confirmado'
    note: str = ''
    # ⛔ ¿YA TE PAGARON? Nace en True porque la venta directa normal es de mano en mano:
    # Christián entrega y cobra en el mismo momento, y así los cientos de ventas
    # directas siguen contando como ingreso igual que siempre. Se apaga para el caso de
    # Alanís: entregado y a deber. Sin este interruptor la separación pagado/entregado
    # no serviría de nada, porque el pedido nace con `paid: False` (default del modelo)
    # y TODA venta directa habría dejado de contar como ingreso.
    pagado: bool = True


@api_router.post('/admin/orders')
async def admin_create_order(payload: ManualOrderCreate, admin=Depends(get_current_admin)):
    """Registra una VENTA DIRECTA (hecha en persona con Christian) en la cuenta
    del cliente, para que la vea en su historial. Sin comisión de nadie."""
    u = await db.users.find_one({'id': payload.user_id}, {'_id': 0, 'password_hash': 0})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    if payload.status not in ('pendiente', 'confirmado', 'enviado', 'entregado'):
        raise HTTPException(status_code=400, detail='Estado no válido')
    # ⛔ EL TECHO ES 40%, NO 60%. Este `min` estaba en 0.60 y dejaba que una venta
    # directa regalara hasta 60% cuando el máximo de la casa es 40%
    # (`loyalty.MAX_DISCOUNT`), que es el que sí respeta el checkout público. En un
    # pedido de $374,360 la diferencia son **$74,872** que salen de más.
    # Lo cazó el auditor de Codex el 2026-07-29.
    # El techo del comprador: 40%, salvo que la venta directa sea para Christián o María.
    rate = max(0.0, min(techo_de_descuento(u), payload.discount_rate))
    # ⛔ EL PRECIO LO PONE EL SERVIDOR, TAMBIÉN AQUÍ.
    # El checkout público ya se blindó el 2026-07-27 (`create_order` retasa cada renglón
    # contra el catálogo), pero la VENTA DIRECTA se quedó fuera: sumaba `i.price` tal cual
    # venía en la petición. Con eso se podía registrar un pedido de $0 —o negativo— y de
    # paso disparar los puntos de lealtad y el descuento de inventario. El descuento se
    # pide con `discount_rate`, que sí está acotado; el PRECIO no se negocia.
    if not payload.items:
        raise HTTPException(status_code=400, detail='Un pedido sin renglones no es un pedido')
    _claves = [i.product_id for i in payload.items]
    _docs = await db.products.find(
        {'$or': [{'id': {'$in': _claves}}, {'sku': {'$in': _claves}}]},
        {'_id': 0, 'id': 1, 'sku': 1, 'price': 1, 'slug': 1, 'presentation': 1,
         'commission_cap': 1, 'category': 1}
    ).to_list(500)
    _catalogo = {}
    for d in _docs:
        _catalogo[d['id']] = d
        if d.get('sku'):
            _catalogo[d['sku']] = d
    _huerfanos = [i.name for i in payload.items if i.product_id not in _catalogo]
    if _huerfanos:
        raise HTTPException(
            status_code=400,
            detail=f'No reconocemos estos productos: {", ".join(_huerfanos)}. '
                   'Sin catálogo no hay precio que cobrar.')
    for i in payload.items:
        if i.quantity is None or int(i.quantity) < 1:
            raise HTTPException(status_code=400, detail=f'Cantidad inválida en {i.name}')
        real = _catalogo[i.product_id].get('price')
        if not real or float(real) <= 0:
            raise HTTPException(status_code=400, detail=f'{i.name} no tiene precio')
        if abs(float(i.price or 0) - float(real)) > 0.01:
            logger.warning('Venta directa con precio distinto al del catálogo en %s: '
                           'mandaron %s, vale %s', i.product_id, i.price, real)
        i.price = float(real)
    subtotal = sum(i.price * i.quantity for i in payload.items)
    # ⛔ Y EL TOPE POR PRODUCTO TAMBIÉN APLICA AQUÍ. El descuento se calculaba sobre el
    # subtotal completo, plano, ignorando que cada producto tiene su propio techo
    # (`commission_cap`) — el que protege el 5× de la casa. El checkout público sí lo
    # respeta renglón por renglón (`_disc_of`); la venta directa no, así que por aquí se
    # podía regalar más de lo que el ROI aguanta en los productos de margen apretado.
    # Los insumos (agua bacteriostática, jeringas) nunca llevan descuento.
    discount = 0
    for i in payload.items:
        d = _catalogo.get(i.product_id) or {}
        if (d.get('category') or '') in NO_DISCOUNT_CATEGORIES:
            continue
        tope = max(0.0, min(0.50, float(d.get('commission_cap', 0.50) or 0.50)))
        discount += round(i.price * i.quantity * min(rate, tope))
    total = subtotal - discount
    # La venta directa es justo donde se da el 40%: con ese descuento no hay puntos.
    points_earned = loyalty.earn(total, loyalty.eligible(u), rate)
    order = Order(
        order_number=gen_order_number(),
        user_id=u['id'],
        items=payload.items,
        customer=CustomerInfo(full_name=u.get('name', ''), email=u.get('email'),
                              phone='', address='Venta directa', notes=payload.note),
        payment_method='directa',
        subtotal=subtotal, discount=discount, discount_rate=rate,
        shipping=0, total=total, status=payload.status,
        paid=payload.pagado,
        paid_at=now_iso() if payload.pagado else None,
        referred_by=None, commission=0, commissions=[],
        points_used=0, points_earned=points_earned,
    )
    await db.orders.insert_one(order.model_dump())
    # ⛔ LA VENTA DIRECTA TAMBIÉN SE LLEVA PIEZAS. No las descontaba, y `restore_order_stock`
    # sí las DEVUELVE al cancelar o al borrar: cada venta directa cancelada le regalaba al
    # inventario piezas que nunca salieron de él. Es exactamente la asimetría que dejó
    # Orexin A en 43 cuando tenía 40, viva todavía en este otro camino. Y mientras tanto el
    # sitio seguía ofreciendo piezas que Christian ya vendió en persona.
    #
    # A diferencia del checkout, aquí NO se condiciona a que haya: esto registra una venta
    # que YA ocurrió, y no tiene sentido negarle al admin apuntar la realidad. Si el
    # inventario queda corto, el checkout público lo verá y dejará de venderlo — que es
    # justo lo que debe pasar.
    for item in payload.items:
        if int(item.quantity or 0) <= 0:
            continue
        await db.products.update_one({'$or': [{'id': item.product_id}, {'sku': item.product_id}]},
                                     {'$inc': {'stock': -int(item.quantity)}})
        await _descontar_inventario_vivo(item.product_id, _catalogo.get(item.product_id),
                                         -int(item.quantity))
    # Los puntos los pone `award_order_points`, que ya se niega si el pedido no está
    # cobrado: una venta directa fiada no regala puntos hasta que se pague.
    if payload.status in loyalty.PAID_STATUSES:
        fresh = await db.orders.find_one({'id': order.id}, {'_id': 0})
        await award_order_points(fresh)
    await notify(u['id'], 'direct_sale', 'Registramos tu compra',
                 f'Tu compra directa quedó registrada (pedido {order.order_number}, total ${total:,.0f}'
                 + (f', {round(rate*100)}% de descuento' if rate else '') + '). '
                 + (payload.note or ''), link='/cuenta')
    return {'order_number': order.order_number, 'total': total, 'discount': discount,
            'points_earned': points_earned}


# ----------------- Admin: fichas por persona (2026-07-23) -----------------
class AdminNotes(BaseModel):
    notes: str


@api_router.put('/admin/distributors/{dist_id}/notes')
async def admin_distributor_notes(dist_id: str, payload: AdminNotes, admin=Depends(get_current_admin)):
    """Notas internas del admin sobre un distribuidor (deudas, acuerdos, etc.).
    Solo las ve el admin — nunca el distribuidor."""
    res = await db.users.update_one({'id': dist_id, 'role': 'distributor'},
                                    {'$set': {'admin_notes': payload.notes[:2000]}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail='Distribuidor no encontrado')
    return {'ok': True}


@api_router.get('/admin/distributors/{dist_id}/detail')
async def admin_distributor_detail(dist_id: str, admin=Depends(get_current_admin)):
    """Ficha completa de UN distribuidor: perfil, códigos, clientes, red y ventas.
    Sin datos confidenciales de clientes (ni teléfonos, ni direcciones, ni salud)."""
    dist = await db.users.find_one({'id': dist_id, 'role': 'distributor'},
                                   {'_id': 0, 'password_hash': 0, 'totp_secret': 0})
    if not dist:
        raise HTTPException(status_code=404, detail='Distribuidor no encontrado')
    users = await db.users.find({}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(10000)
    roll = _distributor_rollup(dist, users, orders)

    codes = await db.discount_codes.find({'distributor_id': dist_id}, {'_id': 0}).to_list(300)
    codes = [_code_projection(c) | {'active': c.get('active', False)} for c in codes]

    # Clientes: SOLO nombre, correo y números de negocio (nada personal).
    my_orders = [o for o in orders if o.get('referred_by') == dist_id and esta_vivo(o)]
    by_user = {}
    for o in my_orders:
        if o.get('user_id'):
            b = by_user.setdefault(o['user_id'], {'orders': 0, 'total': 0.0, 'por_cobrar': 0.0,
                                                  'commission': 0.0, 'last': ''})
            b['orders'] += 1
            # `total` es lo COBRADO a ese cliente; lo fiado va en `por_cobrar`. Y la
            # comisión sale de `_my_amount`, que ya devuelve cero si no se cobró.
            b['total'] += cobrado_de(o)
            b['por_cobrar'] += por_cobrar_de(o)
            b['commission'] += _my_amount(o, dist_id)
            b['last'] = max(b['last'], o.get('created_at', ''))
    clients = []
    for u in users:
        if u.get('referred_by') == dist_id or u['id'] in by_user:
            b = by_user.get(u['id'], {'orders': 0, 'total': 0, 'por_cobrar': 0,
                                      'commission': 0, 'last': ''})
            clients.append({'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                            'guest': False,
                            'orders': b['orders'], 'total': b['total'],
                            'por_cobrar': b['por_cobrar'],
                            'commission': b['commission'], 'last_order': b['last'] or None})
    # Los invitados que usaron su código: la misma regla que en el panel del distribuidor,
    # o el admin y el distribuidor ven listas distintas de la misma realidad.
    con_cuenta = {(u.get('email') or '').strip().lower() for u in users}
    for g in _compradores_invitados([o for o in orders if o.get('referred_by') == dist_id],
                                    con_cuenta):
        uo = g['orders']
        clients.append({'id': g['id'], 'name': g['name'], 'email': g['email'],
                        'guest': True,
                        'orders': len(uo), 'total': sum(cobrado_de(o) for o in uo),
                        'por_cobrar': sum(por_cobrar_de(o) for o in uo),
                        'commission': sum(_my_amount(o, dist_id) for o in uo),
                        'last_order': max((o.get('created_at', '') for o in uo), default=None)})
    clients.sort(key=lambda c: -c['total'])

    # Red: sub-distribuidores directos con sus números.
    subs = []
    for u in users:
        if u.get('role') == 'distributor' and u.get('upline_id') == dist_id:
            sroll = _distributor_rollup(u, users, orders)
            subs.append({'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                         'tier': u.get('tier'), 'distributor_code': u.get('distributor_code'),
                         'sales_total': sroll.get('sales_total', 0), 'clients_count': sroll.get('clients_count', 0),
                         'earnings': sroll.get('earnings', 0)})
    subs.sort(key=lambda x: -x['sales_total'])

    sales = [{'order_number': o.get('order_number'), 'created_at': o.get('created_at'),
              'status': o.get('status'), 'total': o.get('total', 0),
              'pagado': esta_pagado(o),
              'commission': _my_amount(o, dist_id)}
             for o in sorted(my_orders, key=lambda o: o.get('created_at', ''), reverse=True)[:50]]

    return {'distributor': roll, 'codes': codes, 'clients': clients, 'subdistributors': subs, 'sales': sales}


@api_router.get('/admin/customers/{user_id}/detail')
async def admin_customer_detail(user_id: str, admin=Depends(get_current_admin)):
    """Ficha de UN cliente: pedidos, pagos, puntos y cupones que le hemos dado."""
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'password_hash': 0, 'totp_secret': 0})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    orders = await db.orders.find({'user_id': user_id}, {'_id': 0}).to_list(1000)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    # "Pagados" son los COBRADOS, no los entregados: mirar sólo el estado hacía que la
    # ficha de Alanís dijera que ya pagó $3,857.
    paid = solo_cobrados(orders)
    coupons = await db.discount_codes.find({'kind': 'coupon', 'user_id': user_id}, {'_id': 0}).to_list(100)
    ledger = await db.points.find({'user_id': user_id}, {'_id': 0}).to_list(200)
    ledger.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return {
        'customer': {'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                     'created_at': u.get('created_at'), 'blocked': u.get('blocked', False),
                     'referred_by': u.get('referred_by'),
                     'personal_discount_rate': float(u.get('personal_discount_rate') or 0),
                     'points_balance': int(u.get('points_balance', 0) or 0)},
        'orders': [{'id': o['id'], 'order_number': o.get('order_number'), 'created_at': o.get('created_at'),
                    'status': o.get('status'), 'total': o.get('total', 0),
                    'pagado': esta_pagado(o),
                    'payment_method': o.get('payment_method'), 'discount': o.get('discount', 0),
                    'points_used': o.get('points_used', 0)} for o in orders[:100]],
        'paid_total': sum(o.get('total', 0) for o in paid),
        'paid_count': len(paid),
        # Lo que este cliente debe: entregado o en camino, sin cobrar.
        'por_cobrar': sum(por_cobrar_de(o) for o in orders),
        # El descuento que de verdad va a cobrar (topado al 40%), no el guardado: la ficha
        # no puede prometerle al admin un 50% que la caja ya no da.
        'coupons': [{'code': c['code'], 'discount_rate': tasa_de_cupon(c),
                     'expires_at': c.get('expires_at'), 'used': c.get('used', False),
                     'active': c.get('active', False), 'note': c.get('note', '')} for c in coupons],
        'points_ledger': ledger[:50],
    }


class CouponCreate(BaseModel):
    discount_rate: float           # 0.05 .. 0.40 (el techo de la casa)
    expires_days: int = 30
    note: str = ''


@api_router.post('/admin/customers/{user_id}/coupon')
async def admin_send_coupon(user_id: str, payload: CouponCreate, admin=Depends(get_current_admin)):
    """Cupón PERSONAL de un solo uso para un cliente. Sin comisión de nadie."""
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'name': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    # ⛔ EL REGALO TAMBIÉN SE TOPA EN 40% (Christián, 2026-07-31). Este `min` estaba en
    # 0.50 y era la ÚNICA puerta que quedaba arriba del techo de la casa: la venta directa
    # se capó el 29-jul y el checkout público nunca pasó de ahí. Se topa también al
    # cobrarlo (`tasa_de_cupon`), para los que ya andan sueltos con 50%.
    rate = max(0.05, min(TECHO_DESCUENTO, payload.discount_rate))
    code = 'GIFT-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    while await db.discount_codes.find_one({'code': code}):
        code = 'GIFT-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    from datetime import timedelta
    expires = (datetime.now(timezone.utc) + timedelta(days=max(1, payload.expires_days))).isoformat()
    await db.discount_codes.insert_one({
        'id': str(uuid.uuid4()), 'code': code, 'kind': 'coupon', 'user_id': user_id,
        'discount_rate': rate, 'active': True, 'used': False, 'single_use': True,
        'note': payload.note, 'created_by': 'admin', 'created_at': now_iso(), 'expires_at': expires,
    })
    await notify(user_id, 'coupon', 'Tienes un regalo de Exygen',
                 f'Te mandamos el cupón {code} con {round(rate * 100)}% de descuento en tu próxima compra. '
                 + (payload.note or ''), link='/catalogo')
    return {'code': code, 'discount_rate': rate, 'expires_at': expires}


class PersonalRate(BaseModel):
    rate: float   # 0 = quitar el trato especial; hasta 0.50


@api_router.put('/admin/customers/{user_id}/personal-discount')
async def admin_set_personal_discount(user_id: str, payload: PersonalRate, admin=Depends(get_current_admin)):
    """Trato especial PERMANENTE para un cliente: compra siempre con ese % sin
    necesidad de código (el caso de Paz Cambray al 40%). Se recorta al tope de cada
    producto y los insumos siguen fuera. 0 lo quita."""
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'name': 1, 'role': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    if u.get('role') == 'distributor':
        raise HTTPException(status_code=400, detail='Un distribuidor ya compra con su comisión máxima')
    # ⛔ TOPADO AL 40% (Christián, 2026-07-31: «nadie por encima de ese 40%»). Estaba en
    # 0.50. Se topa también AL COBRAR (`buyer_own_rate`), así que los tratos ya puestos
    # arriba del techo valen 40% aunque nadie vuelva a tocar esta pantalla.
    rate = max(0.0, min(techo_de_descuento(u), float(payload.rate or 0)))
    await db.users.update_one({'id': user_id}, {'$set': {'personal_discount_rate': rate}})
    if rate > 0:
        await notify(user_id, 'personal_discount', 'Tienes un descuento permanente',
                     f'A partir de ahora tus compras llevan {round(rate * 100)}% de descuento, '
                     'sin necesidad de código.', link='/catalogo')
    return {'id': user_id, 'name': u.get('name'), 'personal_discount_rate': rate}


class VeDatosCliente(BaseModel):
    activo: bool


@api_router.put('/admin/distributors/{user_id}/ve-datos-cliente')
async def admin_set_ve_datos_cliente(user_id: str, payload: VeDatosCliente,
                                     admin=Depends(get_current_admin)):
    """Abre (o cierra) los datos de contacto de SUS clientes a UN distribuidor.

    ⛔ POR QUÉ ES UN INTERRUPTOR Y NO UNA REGLA PARA TODOS. El 2026-07-23 Christián
    ordenó que ningún distribuidor viera correo, teléfono ni domicilio de sus clientes.
    El 2026-07-31 lo abrió para María —ella los atiende y necesita poder llamarles—
    pero sólo para ella. Encender a otro es este clic, no un despliegue; y apagarlo
    también, que es lo que hace que se pueda revertir sin tocar código.

    Lo que este interruptor NO afloja: el candado de «sólo SUS clientes» (vive en el
    servidor, no en la pantalla), el margen de la casa, y que el «ver como» del admin
    siga siendo de sólo lectura.
    """
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'name': 1, 'role': 1,
                                                  'extra_roles': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Usuario no encontrado')
    roles = {u.get('role')} | set(u.get('extra_roles') or [])
    if 'distributor' not in roles:
        raise HTTPException(status_code=400,
                            detail='Sólo aplica a distribuidores: el admin ya ve todo')
    activo = bool(payload.activo)
    await db.users.update_one({'id': user_id}, {'$set': {CAMPO_VE_CLIENTE: activo}})
    logger.info('Privacidad: %s %s los datos de contacto de sus clientes para %s',
                admin.get('email'), 'ABRIÓ' if activo else 'CERRÓ', u.get('name'))
    return {'id': user_id, 'name': u.get('name'), CAMPO_VE_CLIENTE: activo}


class GiftPoints(BaseModel):
    points: int
    note: str = ''


@api_router.post('/admin/customers/{user_id}/gift-points')
async def admin_gift_points(user_id: str, payload: GiftPoints, admin=Depends(get_current_admin)):
    """Regala puntos de lealtad a un cliente (cortesía de la casa)."""
    if payload.points <= 0 or payload.points > 100000:
        raise HTTPException(status_code=400, detail='Cantidad de puntos no válida')
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    await db.users.update_one({'id': user_id}, {'$inc': {'points_balance': int(payload.points)}})
    await db.points.insert_one({
        'id': str(uuid.uuid4()), 'user_id': user_id, 'order_id': None, 'order_number': '',
        'type': 'gift', 'points': int(payload.points), 'note': payload.note, 'created_at': now_iso(),
    })
    await notify(user_id, 'gift_points', 'Te regalamos puntos',
                 f'Exygen te regaló {payload.points:,} puntos de lealtad. {payload.note or ""}'.strip(),
                 link='/cuenta')
    fresh = await db.users.find_one({'id': user_id}, {'_id': 0, 'points_balance': 1})
    return {'points_balance': int((fresh or {}).get('points_balance', 0) or 0)}


# ----------------- LA FICHA DE UN CLIENTE (una sola verdad) -----------------
#
# ⛔ UNA SOLA FICHA PARA TODA LA PLATAFORMA (Christián, 2026-07-30). El nombre de un
# cliente sale en seis listas distintas —Clientes y Pedidos del admin, la ficha del
# distribuidor, Mis Clientes, Ventas, Envíos— y cada una enseñaba una cosa distinta;
# desde varias ni se podía abrir nada. Ahora todas piden ESTA ruta.
#
# ⛔ EL CANDADO DECIDE QUÉ SE ENTREGA, NO QUÉ PANTALLA PREGUNTA. Si cada lista
# recortara lo suyo en JavaScript, la que se olvide de recortar enseña de más — y un
# distribuidor tecleando el id de un cliente ajeno en la barra de direcciones se lleva
# el domicilio y el historial de compras de alguien que no es suyo. Aquí el rol de
# quien pregunta arma la respuesta: admin ve todo; distribuidor ve SÓLO a los suyos y
# SÓLO sus pedidos con él; cliente ajeno = 403; sin sesión = 401.

def _es_invitado(client_id):
    """`invitado:aidee@correo.com` — el que compró con un código pero no abrió cuenta."""
    return str(client_id or '').startswith('invitado:')


def _correo_de_invitado(client_id):
    return str(client_id or '')[len('invitado:'):].strip().lower()


def _contacto_de_pedidos(orders):
    """Teléfonos y domicilios que dejó en sus pedidos, del más nuevo al más viejo.

    Es la única fuente de contacto de un INVITADO (no tiene cuenta), y para el que sí
    tiene cuenta es lo que completa su ficha: la gente corrige su dirección al recomprar.
    """
    telefonos, domicilios = [], []
    for o in sorted(orders, key=lambda x: x.get('created_at', ''), reverse=True):
        c = o.get('customer') or {}
        pais = c.get('country') if c.get('country') not in (None, '', 'MX') else None
        dom = ', '.join(x for x in [c.get('address'), c.get('city'), c.get('state'),
                                    c.get('postal_code'), pais] if x)
        if dom and dom not in domicilios:
            domicilios.append(dom)
        tel = (c.get('phone') or '').strip()
        if tel and tel not in telefonos:
            telefonos.append(tel)
    return telefonos, domicilios


def _fila_de_pedido_de_ficha(o, dist_id=None):
    """Un renglón de la lista de pedidos de la ficha.

    Lo mismo para los dos roles salvo la comisión: el distribuidor ve LO SUYO de ese
    pedido; el admin no ve una comisión "propia" porque no la tiene. Ni aquí ni en
    ningún lado viaja el costo, el proveedor ni el margen."""
    fila = {
        'id': o.get('id'),
        'order_number': o.get('order_number'),
        'created_at': o.get('created_at'),
        'status': o.get('status', 'pendiente'),
        'total': o.get('total', 0),
        'pagado': esta_pagado(o),
        'payment_method': o.get('payment_method'),
        'items_count': sum(int(it.get('quantity', 0) or 0) for it in (o.get('items') or [])),
        # El envío viaja en el renglón para que la hoja de "poner guía" se abra YA LLENA
        # con lo que hubiera, y para que el botón sepa si dice "poner" o "cambiar".
        'carrier': o.get('carrier', ''),
        'tracking_number': o.get('tracking_number', ''),
        'tracking_url': o.get('tracking_url', ''),
    }
    if dist_id:
        fila['my_commission'] = _my_amount(o, dist_id)
    return fila


async def _nota_de_cliente(client_id, es_invitado):
    """La nota privada del admin sobre esta persona. Del que tiene cuenta vive en su
    usuario (`admin_notes`, el mismo campo que la ficha del distribuidor, para que la
    nota sobreviva si mañana lo convertimos en distribuidor); la del invitado no tiene
    usuario dónde vivir y va en su propia colección."""
    if es_invitado:
        doc = await db.client_notes.find_one({'client_id': client_id}, {'_id': 0, 'note': 1})
        return (doc or {}).get('note', '')
    u = await db.users.find_one({'id': client_id}, {'_id': 0, 'admin_notes': 1})
    return (u or {}).get('admin_notes', '')


@api_router.get('/clientes/{client_id}/ficha')
async def ficha_de_cliente(client_id: str, user=Depends(get_current_user)):
    """LA ficha de un cliente. La misma desde donde sea que se le haga clic.

    `client_id` es el id del usuario, o `invitado:<correo>` para quien compró sin cuenta.
    """
    rol = user.get('role')
    if rol not in ('admin', 'distributor'):
        raise HTTPException(status_code=403, detail='No tienes acceso a esta ficha')
    es_admin = rol == 'admin'
    dist_id = None if es_admin else user['id']

    orders_all = await db.orders.find({}, {'_id': 0}).to_list(20000)

    invitado = _es_invitado(client_id)
    cuenta = None
    if invitado:
        correo = _correo_de_invitado(client_id)
        if not correo:
            raise HTTPException(status_code=404, detail='Cliente no encontrado')
        # Si con el tiempo abrió cuenta con ese mismo correo, deja de ser invitado:
        # una persona, una ficha. Si no, existe únicamente dentro de sus pedidos.
        cuenta = await db.users.find_one({'email': correo},
                                         {'_id': 0, 'password_hash': 0, 'totp_secret': 0})
        if cuenta:
            invitado = False
            client_id = cuenta['id']
    if not invitado and cuenta is None:
        cuenta = await db.users.find_one({'id': client_id},
                                         {'_id': 0, 'password_hash': 0, 'totp_secret': 0})
        if not cuenta:
            raise HTTPException(status_code=404, detail='Cliente no encontrado')

    if invitado:
        correo = _correo_de_invitado(client_id)
        suyos = [o for o in orders_all
                 if not o.get('user_id')
                 and ((o.get('customer') or {}).get('email') or '').strip().lower() == correo]
        if not suyos:
            raise HTTPException(status_code=404, detail='Cliente no encontrado')
        reciente = max(suyos, key=lambda o: o.get('created_at', ''))
        c = reciente.get('customer') or {}
        persona = {'id': f'invitado:{correo}', 'guest': True,
                   'name': c.get('full_name') or correo, 'email': correo,
                   'created_at': min((o.get('created_at', '') for o in suyos), default=None)}
    else:
        suyos = [o for o in orders_all if o.get('user_id') == cuenta['id']]
        persona = {'id': cuenta['id'], 'guest': False, 'name': cuenta.get('name'),
                   'email': cuenta.get('email'), 'created_at': cuenta.get('created_at')}

    if dist_id:
        # ⛔ SÓLO LO SUYO. Un pedido que el cliente hizo por su cuenta (o con el código
        # de otro distribuidor) no es asunto de éste, aunque la persona sea su cliente.
        conmigo = [o for o in suyos if o.get('referred_by') == dist_id]
        de_su_red = (not invitado) and cuenta.get('referred_by') == dist_id
        if not conmigo and not de_su_red:
            raise HTTPException(status_code=403, detail='Ese cliente no es tuyo')
        if not invitado and cuenta.get('role') not in ('user', None, ''):
            # Ni otro distribuidor ni el admin se abren como "cliente".
            raise HTTPException(status_code=403, detail='Ese cliente no es tuyo')
        suyos = conmigo

    vivos = [o for o in suyos if esta_vivo(o)]
    vivos.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    telefonos, domicilios = _contacto_de_pedidos(suyos)
    if not invitado and cuenta.get('phone') and cuenta['phone'] not in telefonos:
        telefonos.insert(0, cuenta['phone'])

    persona['phones'] = telefonos
    persona['addresses'] = domicilios

    ficha = {
        'scope': 'admin' if es_admin else 'distributor',
        'client': persona,
        'orders': [_fila_de_pedido_de_ficha(o, dist_id) for o in vivos[:100]],
        'totals': {
            'orders_count': len(vivos),
            'paid_total': sum(cobrado_de(o) for o in vivos),
            'paid_count': len(solo_cobrados(vivos)),
            'por_cobrar': sum(por_cobrar_de(o) for o in vivos),
            'last_order_at': max((o.get('created_at', '') for o in vivos), default=None),
        },
    }
    if dist_id:
        ficha['totals']['my_earnings'] = sum(_my_amount(o, dist_id) for o in vivos)
        return ficha

    # De aquí para abajo, SÓLO EL ADMIN. Puntos, cupones, quién lo refirió y la nota
    # privada no son cosa del distribuidor ni siquiera sobre sus propios clientes.
    persona['blocked'] = bool((cuenta or {}).get('blocked', False))
    persona['points_balance'] = int((cuenta or {}).get('points_balance', 0) or 0)
    persona['personal_discount_rate'] = float((cuenta or {}).get('personal_discount_rate') or 0)
    persona['email_verified'] = bool((cuenta or {}).get('email_verified', False)) if cuenta else None
    ref_id = (cuenta or {}).get('referred_by') or next(
        (o.get('referred_by') for o in vivos if o.get('referred_by')), None)
    if ref_id:
        ref = await db.users.find_one({'id': ref_id}, {'_id': 0, 'id': 1, 'name': 1,
                                                      'distributor_code': 1})
        if ref:
            persona['referred_by'] = {'id': ref['id'], 'name': ref.get('name'),
                                      'code': ref.get('distributor_code')}
    ficha['coupons'] = []
    ficha['points_ledger'] = []
    if cuenta:
        cupones = await db.discount_codes.find(
            {'kind': 'coupon', 'user_id': cuenta['id']}, {'_id': 0}).to_list(100)
        ficha['coupons'] = [{'code': c['code'], 'discount_rate': c.get('discount_rate', 0),
                             'expires_at': c.get('expires_at'), 'used': c.get('used', False),
                             'active': c.get('active', False), 'note': c.get('note', '')}
                            for c in cupones]
        ledger = await db.points.find({'user_id': cuenta['id']}, {'_id': 0}).to_list(200)
        ledger.sort(key=lambda e: e.get('created_at', ''), reverse=True)
        ficha['points_ledger'] = ledger[:50]
    ficha['note'] = await _nota_de_cliente(persona['id'], invitado)
    return ficha


class NotaDeCliente(BaseModel):
    note: str = ''


@api_router.put('/admin/clientes/{client_id}/nota')
async def guardar_nota_de_cliente(client_id: str, payload: NotaDeCliente,
                                  admin=Depends(get_current_admin)):
    """La nota privada del admin sobre un cliente. Nunca la ve nadie más."""
    deny_view_as(admin)
    texto = (payload.note or '')[:2000]
    if _es_invitado(client_id):
        await db.client_notes.update_one({'client_id': client_id},
                                         {'$set': {'note': texto, 'updated_at': now_iso()}},
                                         upsert=True)
        return {'ok': True, 'note': texto}
    res = await db.users.update_one({'id': client_id}, {'$set': {'admin_notes': texto}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    return {'ok': True, 'note': texto}


app.include_router(api_router)


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=['*'],
    allow_headers=['*'],
)
