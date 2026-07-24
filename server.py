from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from starlette.middleware.cors import CORSMiddleware
import os
import base64
import logging
import uuid
import random
import string
import re
import json
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel
from database import db, client
from models import (
    RegisterInput, LoginInput, ForgotPasswordInput, ResetPasswordInput,
    ProfileUpdate, ChangePasswordInput,
    ProductCreate, ProductUpdate, Product, Category,
    OrderCreate, Order, OrderItem, CustomerInfo, OrderStatusUpdate, OrderShippingUpdate,
    ProtocolInput, ProtocolUpdate, LabReportInput,
    TokenInput, ActivateInput, ResendVerificationInput,
    ChatInput, DistributorCreate, DiscountCodeCreate, AnnouncementCreate, GoogleAuthInput, now_iso,
)
from auth import (
    hash_password, verify_password, create_token, create_view_as_token, deny_view_as,
    get_current_user, get_optional_user, get_current_admin, get_current_distributor,
)
from ai_assistant import build_chat, stream_reply, extract_lab_report, interpret_lab_report
import coa_store
from google_auth import verify_google_token, google_enabled, GOOGLE_CLIENT_ID
import loyalty
import pyramid
import auth_factors
import btcpay
import nowpayments
from fastapi import Request


def crypto_enabled() -> bool:
    """Hay vía cripto si CUALQUIER proveedor está encendido."""
    return nowpayments.enabled() or btcpay.enabled()
from urllib.parse import urlparse
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
            return dist, max(0.0, min(pyramid.tier_rate(dist.get('tier')), doc.get('discount_rate', 0)))
    dist = await db.users.find_one({'distributor_code': c, 'role': 'distributor'},
                                   {'_id': 0, 'password_hash': 0})
    if dist:
        return dist, max(0.0, min(pyramid.tier_rate(dist.get('tier')), dist.get('customer_discount_rate', 0)))
    return None, 0.0


# ----------------- Centro de noticias / notificaciones -----------------
async def notify(user_id, ntype, title, body='', link=None, dedup=None):
    """Crea una notificación PERSONAL para un usuario. `dedup`: si se pasa, no
    duplica una del mismo tipo+dedup en los últimos 30 días (para 'por terminarse')."""
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
        'title': title, 'body': body, 'link': link, 'dedup': dedup, 'created_at': now_iso()})


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
    - active_recruits: distribuidores en su red con ≥1 venta propia (no cancelada).
    - team_sales: ventas propias del distribuidor + de toda su red.
    Recorre el árbol por upline_id (BFS), corta ciclos."""
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
        {'_id': 0, 'referred_by': 1, 'total': 1},
    ).to_list(20000)
    sales_by = {}
    for o in rows:
        sales_by[o['referred_by']] = sales_by.get(o['referred_by'], 0) + o.get('total', 0)
    active_recruits = sum(1 for nid in network if sales_by.get(nid, 0) > 0)
    team_sales = sum(sales_by.values())
    return {'active_recruits': active_recruits, 'team_sales': team_sales,
            'personal_sales': sales_by.get(dist_id, 0), 'network_size': len(network)}


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
    return {
        'pending_verification': False,
        'token': create_token(user['id']),
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
    }


@api_router.post('/auth/login')
async def login(payload: LoginInput):
    user = await db.users.find_one({'email': payload.email.lower()})
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
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
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
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
    }


@api_router.get('/auth/google/config')
async def google_config():
    """El sitio pregunta si Google Sign-In esta encendido y con que client id.
    Si no hay client id configurado, el boton no se muestra."""
    return {'enabled': google_enabled(), 'client_id': GOOGLE_CLIENT_ID if google_enabled() else ''}


@api_router.post('/auth/google')
async def google_login(payload: GoogleAuthInput):
    """Entra o crea la cuenta con una credencial de Google.

    Google ya verifico el correo, asi que la cuenta nace confirmada: no tiene
    sentido mandar un correo de confirmacion a una direccion que Google acaba
    de validar. Si el correo ya existe con contrasena, se vincula y entra: es
    la misma persona.
    """
    try:
        info = await verify_google_token(payload.credential)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    user = await db.users.find_one({'email': info['email']})
    if user and user.get('blocked'):
        raise HTTPException(status_code=403, detail='Esta cuenta esta deshabilitada')
    if user:
        # Cuenta existente: se vincula con Google y se da por confirmada.
        # No se piden consentimientos: ya los dio al registrarse.
        await db.users.update_one(
            {'id': user['id']},
            {'$set': {'google_sub': info['google_sub'], 'email_verified': True}},
        )
    else:
        # Cuenta NUEVA: Google avala el correo, pero 18+/Terminos y Privacidad
        # los tiene que aceptar la persona. Sin eso, el sitio pide las casillas
        # y reintenta con la misma credencial.
        if not (payload.age_confirmed and payload.privacy_accepted):
            return {'needs_consent': True, 'name': info['name'], 'email': info['email']}
        referrer = await resolve_distributor(payload.distributor_code)
        consented_at = now_iso()
        user = {
            'id': str(uuid.uuid4()),
            'name': info['name'],
            'email': info['email'],
            # Sin contrasena: solo entra por Google hasta que use "recuperar
            # contrasena" para ponerse una.
            'password_hash': '',
            'role': 'user',
            'language': normalize_language(payload.language),
            'referred_by': referrer['id'] if referrer else None,
            'google_sub': info['google_sub'],
            'email_verified': True,
            'consents': {
                'age_confirmed': True,
                'privacy_accepted': True,
                'marketing_email': bool(payload.marketing_email),
                'promos': bool(payload.promos),
                'accepted_at': consented_at,
                'source': 'google',
            },
            'created_at': consented_at,
        }
        await db.users.insert_one(user)
        asyncio.create_task(send_welcome_email(user['name'], user['email'], user['language']))

    return {
        'token': create_token(user['id']),
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user.get('role', 'user')},
    }


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
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user.get('role', 'user')},
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
    return {
        'token': create_token(user['id']),
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user['role']},
    }


@api_router.post('/auth/resend-verification')
async def resend_verification(payload: ResendVerificationInput):
    """Siempre responde ok: no revelamos si el correo existe."""
    user = await db.users.find_one({'email': payload.email.lower()})
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
async def activate_account(payload: ActivateInput):
    """El invitado elige su contraseña; eso mismo confirma su correo."""
    user = await _consume_token(payload.token, 'invite')
    await db.users.update_one({'id': user['id']}, {'$set': {
        'password_hash': hash_password(payload.password),
        'email_verified': True,
        'verified_at': now_iso(),
    }})
    asyncio.create_task(send_welcome_email(user['name'], user['email'], user.get('language')))
    return {
        'token': create_token(user['id']),
        'user': {'id': user['id'], 'name': user['name'], 'email': user['email'], 'role': user.get('role', 'user')},
    }


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
        if await db.users.find_one({'email': payload.email.lower()}):
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
# Tope duro de comision de distribuidores (regla de Christian, 2026-07-21).
COMMISSION_CAP = 0.50

# ----------------- Lealtad (puntos) -----------------
async def _points_entry(user_id, order, kind, points):
    await db.points.insert_one({
        'id': str(uuid.uuid4()), 'user_id': user_id, 'order_id': order['id'],
        'order_number': order.get('order_number', ''), 'type': kind,
        'points': int(points), 'created_at': now_iso(),
    })


async def award_order_points(order):
    """Deposita los puntos de una orden pagada. Idempotente: el flag
    points_awarded se toma con una sola actualizacion condicional."""
    if not order.get('user_id') or int(order.get('points_earned', 0) or 0) <= 0:
        return
    res = await db.orders.update_one(
        {'id': order['id'], 'points_awarded': {'$ne': True}},
        {'$set': {'points_awarded': True}},
    )
    if res.modified_count == 0:
        return
    await db.users.update_one({'id': order['user_id']}, {'$inc': {'points_balance': int(order['points_earned'])}})
    await _points_entry(order['user_id'], order, 'earn', order['points_earned'])


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


@api_router.post('/orders')
async def create_order(payload: OrderCreate, user=Depends(get_optional_user)):
    deny_view_as(user)          # en modo "ver como" no se compra nada
    if not payload.items:
        raise HTTPException(status_code=400, detail='El carrito esta vacio')
    allowed_methods = ['tarjeta', 'spei'] + (['cripto'] if crypto_enabled() else [])
    if payload.payment_method not in allowed_methods:
        raise HTTPException(status_code=400, detail='Metodo de pago no disponible')
    subtotal = sum(item.price * item.quantity for item in payload.items)
    # Familia HGH (no el Fragment): precio neto SIEMPRE — su margen no aguanta
    # ningún descuento (Christian, 2026-07-22). Miramos id Y nombre porque en
    # producción el product_id es un UUID (no dice "hgh"); el nombre sí.
    def _is_hgh_net(item):
        key = f"{item.product_id} {item.name}".lower()
        return 'hgh' in key and 'fragment' not in key
    # Tope de comisión y elegibilidad POR PRODUCTO (regla Christian 2026-07-23):
    # si un producto no deja 5x neto, no participa del canal de distribuidores
    # (ni descuento de código, ni promo, ni comisión) — solo venta directa.
    _ids = [it.product_id for it in payload.items]
    _pdocs = await db.products.find({'id': {'$in': _ids}},
                                    {'_id': 0, 'id': 1, 'commission_cap': 1, 'distributor_eligible': 1}).to_list(500)
    _pflags = {d['id']: d for d in _pdocs}

    def _cap_of(item):
        d = _pflags.get(item.product_id, {})
        return max(0.0, min(0.50, float(d.get('commission_cap', 0.50) or 0.50)))

    def _eligible(item):
        d = _pflags.get(item.product_id, {})
        return bool(d.get('distributor_eligible', True)) and not _is_hgh_net(item)

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
        if _c and not _c.get('used') and (not _c.get('expires_at') or _c['expires_at'] >= now_iso()) \
           and (not _c.get('user_id') or (user and user['id'] == _c['user_id'])):
            coupon = _c
    referrer, code_discount = ((None, 0.0) if coupon else await _resolve_code(payload.distributor_code))
    if coupon:
        discount_rate = coupon['discount_rate']
    elif referrer:
        discount_rate = code_discount
    else:
        discount_rate = 0.15 if discountable >= 35000 else 0.10
    if referrer:
        # Un producto solo acepta el descuento del código si su tope lo aguanta.
        _take = [it for it in payload.items if _eligible(it) and _cap_of(it) >= discount_rate]
        _base_desc = sum(it.price * it.quantity for it in _take)
        discount = round(_base_desc * discount_rate)
    else:
        discount = round(discountable * discount_rate)
    after_discount = subtotal - discount
    # Lealtad: el canje se limita al saldo real y a la mercancia (el envio va en dinero).
    points_used = 0
    if payload.points_to_use and user and loyalty.eligible(user):
        fresh = await db.users.find_one({'id': user['id']}, {'_id': 0, 'points_balance': 1})
        balance = int((fresh or {}).get('points_balance', 0) or 0)
        points_used = loyalty.clamp_redeem(payload.points_to_use, balance, after_discount)
    paid_merchandise = after_discount - points_used
    shipping = payload.shipping if payload.shipping else 0   # el envio se cotiza por separado
    total = paid_merchandise + shipping
    points_earned = loyalty.earn(paid_merchandise, user is not None and loyalty.eligible(user))
    # Pirámide: el vendedor gana (su tasa − el descuento que dio) y cada upline su
    # DIFERENCIAL, sobre la mercancía con descuento (`discountable`). Se bloquea en
    # pesos al crear la orden; los reportes suman lo guardado.
    commissions = []
    commission = 0
    if referrer:
        upline = await _upline_chain(referrer)
        # El reparto se calcula POR TOPE de producto: descuento + comisiones
        # nunca rebasan el tope (así la casa conserva su 5x).
        groups = {}
        for it in payload.items:
            if not _eligible(it):
                continue
            cap = _cap_of(it)
            amt = it.price * it.quantity
            if cap >= discount_rate:
                key = (round(max(0.0, cap - discount_rate), 4), discount_rate)
            else:
                key = (round(cap, 4), 0.0)   # sin descuento: el tope entero es comisión
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
        shipping=shipping,
        total=total,
        referred_by=referrer['id'] if referrer else None,
        commission=commission,
        commissions=commissions,
        points_used=points_used,
        points_earned=points_earned,
    )
    await db.orders.insert_one(order.model_dump())
    if coupon and coupon.get('single_use', True):
        await db.discount_codes.update_one({'id': coupon['id']},
                                           {'$set': {'used': True, 'active': False, 'used_order': order.order_number}})
    # Notificar a quienes ganan comisión en esta venta (vendedor + uplines).
    for row in commissions:
        if row.get('amount', 0) > 0:
            role = 'tu venta' if row['role'] == 'seller' else 'una venta de tu equipo'
            await notify(row['distributor_id'], 'new_sale', 'Nueva venta',
                         f"Ganaste ${row['amount']:,.0f} por {role} (pedido {order.order_number}).",
                         link='/distribuidor')
    if points_used:
        # El canje se descuenta de inmediato: si no, dos pedidos seguidos
        # podrian gastar el mismo saldo.
        await db.users.update_one({'id': user['id']}, {'$inc': {'points_balance': -points_used}})
        await _points_entry(user['id'], order.model_dump(), 'redeem', -points_used)
    for item in payload.items:
        await db.products.update_one({'id': item.product_id}, {'$inc': {'stock': -item.quantity}})
        # Inventario vivo por presentacion (key = product_id del carrito, ya incluye ::presentacion)
        await db.stock.update_one({'key': item.product_id}, {'$inc': {'qty': -item.quantity}})
    # Confirmacion por correo, en segundo plano: la compra no debe quedarse
    # esperando al proveedor de correo ni fallar si esta caido.
    email_order = order.model_dump()
    if payload.payment_method == 'spei':
        email_order['spei'] = spei_details()   # la CLABE también va en el correo
    asyncio.create_task(send_order_email(email_order, user.get('language') if user else None))
    result = clean(order.model_dump())
    # Cripto: creamos la factura del proveedor encendido y devolvemos su enlace.
    # El pedido queda 'pendiente' hasta que su webhook confirme que llegó el
    # dinero. NOWPayments primero (más simple); BTCPay como respaldo.
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


async def _confirm_crypto_order(order_number: str):
    """Marca pagado un pedido de cripto y deposita puntos. Idempotente."""
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if order and order.get('status') == 'pendiente':
        await db.orders.update_one({'id': order['id']}, {'$set': {'status': 'confirmado', 'paid_at': now_iso()}})
        fresh = await db.orders.find_one({'id': order['id']}, {'_id': 0})
        await award_order_points(fresh)
        asyncio.create_task(send_payment_confirmed_email(fresh))


@api_router.get('/payments/config')
async def payments_config():
    """El checkout pregunta qué métodos están encendidos hoy."""
    return {'crypto_enabled': crypto_enabled()}


@api_router.post('/payments/nowpayments/webhook')
async def nowpayments_webhook(request: Request):
    """NOWPayments avisa aquí (IPN). Verificamos la firma HMAC-SHA512 y, si el
    pago quedó 'finished', confirmamos el pedido. Nunca confía sin firma válida."""
    raw = await request.body()
    if not nowpayments.verify_ipn(raw, request.headers.get('x-nowpayments-sig', '')):
        raise HTTPException(status_code=401, detail='firma invalida')
    event = json.loads(raw.decode() or '{}')
    if event.get('payment_status') in nowpayments.SETTLED_STATUSES:
        await _confirm_crypto_order(event.get('order_id') or '')
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
    await _confirm_crypto_order((event.get('metadata') or {}).get('orderId') or '')
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
async def admin_orders(admin=Depends(get_current_admin)):
    orders = await db.orders.find({}, {'_id': 0}).to_list(500)
    orders.sort(key=lambda o: o.get('created_at', ''), reverse=True)
    return orders


@api_router.put('/admin/orders/{order_id}/status')
async def update_order_status(order_id: str, payload: OrderStatusUpdate, admin=Depends(get_current_admin)):
    prev = await db.orders.find_one({'id': order_id}, {'_id': 0, 'status': 1})
    if not prev:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    update = {'status': payload.status}
    if payload.status == 'enviado':
        update['shipped_at'] = now_iso()
    elif payload.status == 'entregado':
        update['delivered_at'] = now_iso()
    await db.orders.update_one({'id': order_id}, {'$set': update})
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    # Lealtad: pago verificado deposita puntos; cancelacion los revierte.
    if payload.status in loyalty.PAID_STATUSES:
        await award_order_points(order)
    elif payload.status == 'cancelado':
        await revoke_order_points(order)
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
    result = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if result.get('status') in loyalty.PAID_STATUSES:
        await award_order_points(result)
        result = await db.orders.find_one({'id': order_id}, {'_id': 0})
    return result


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
    users = await db.users.find({'role': 'user'}, {'_id': 0, 'password_hash': 0}).to_list(2000)
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
    c = (code or '').strip().upper()
    cdoc = await db.discount_codes.find_one({'code': c, 'active': True, 'kind': 'coupon'})
    if cdoc and not cdoc.get('used') and (not cdoc.get('expires_at') or cdoc['expires_at'] >= now_iso()):
        return {'code': c, 'discount_rate': cdoc.get('discount_rate', 0)}
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
    rate_basis = dist.get('commission_rate', pyramid.tier_rate(dist.get('tier')))
    tiers = pyramid.discount_tiers_for(rate_basis)
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


@api_router.get('/distributor/codes')
async def list_discount_codes(dist=Depends(get_current_distributor)):
    """Los códigos AUTO del distribuidor (uno por nivel de descuento). Se generan
    y rotan solos cada 30 días; el distribuidor solo elige cuál da a cada cliente."""
    codes = await _ensure_distributor_codes(dist)
    return {'max_discount': pyramid.tier_rate(dist.get('tier')),
            'rotate_days': CODE_TTL_DAYS,
            'codes': [_code_projection(c) for c in codes]}


@api_router.post('/distributor/codes/rotate')
async def rotate_discount_codes(dist=Depends(get_current_distributor)):
    """Renueva YA todos los códigos (nuevos textos). Los viejos dejan de servir."""
    codes = await _ensure_distributor_codes(dist, force_rotate=True)
    return {'rotated': True, 'codes': [_code_projection(c) for c in codes]}


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
    existing = await db.users.find_one({'email': payload.email.lower()})
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
    # VENTAS propias = pedidos hechos con SU código (no canceladas).
    valid = [o for o in orders if o.get('referred_by') == dist['id'] and o.get('status') != 'cancelado']
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
        if rb in net_ids and o.get('status') != 'cancelado':
            sales_by[rb] = sales_by.get(rb, 0) + o.get('total', 0)
    team_sales = sum(sales_by.values())
    active_recruits = sum(1 for nid in network if sales_by.get(nid, 0) > 0)
    return {
        'id': dist['id'],
        'name': dist['name'],
        'email': dist['email'],
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': dist.get('commission_rate', 0.25),
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        # Pirámide: nivel y de quién cuelga.
        'tier': dist.get('tier', pyramid.DEFAULT_TIER),
        'upline_id': dist.get('upline_id'),
        'created_at': dist.get('created_at'),
        'email_verified': dist.get('email_verified', False),
        'invited_at': dist.get('invited_at'),
        'admin_notes': dist.get('admin_notes', ''),
        'clients_count': len(clients),
        'sales_count': len(valid),
        'sales_total': sum(o.get('total', 0) for o in valid),
        # GANANCIAS = su tajada como vendedor + sobrecomisiones de su downline.
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
    existing = await db.users.find_one({'email': payload.email.lower()})
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
        'customer_discount_rate': max(0.05, min(0.50, payload.customer_discount_rate)),
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
    discount = max(0.05, min(0.50, float(payload.get('customer_discount_rate', 0.10) or 0.10)))
    existing = await db.users.find_one({'email': app_doc['email']})
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
    discount = max(0.05, min(0.50, discount))
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
        update['customer_discount_rate'] = max(0.05, min(0.50, float(payload['customer_discount_rate'])))
    if not update:
        raise HTTPException(status_code=400, detail='Nada que actualizar')
    await db.users.update_one({'id': dist_id}, {'$set': update})
    fresh = await db.users.find_one({'id': dist_id}, {'_id': 0, 'password_hash': 0})
    return {'id': fresh['id'], 'name': fresh['name'],
            'commission_rate': fresh['commission_rate'],
            'customer_discount_rate': fresh['customer_discount_rate']}


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
        order = pyramid.TIER_ORDER
        old_i = order.index(dist.get('tier')) if dist.get('tier') in order else -1
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
    anterior a la pirámide y fue su venta directa."""
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
    valid = [o for o in orders if o.get('status') != 'cancelado']
    own_sales = [o for o in valid if o.get('referred_by') == dist['id']]
    by_month = {}
    for o in valid:
        m = (o.get('created_at') or '')[:7]
        e = by_month.setdefault(m, {'month': m, 'earnings': 0, 'sales': 0})
        e['earnings'] += _my_amount(o, dist['id'])
        if o.get('referred_by') == dist['id']:
            e['sales'] += o.get('total', 0)
    earnings_total = sum(_my_amount(o, dist['id']) for o in valid)
    own_earnings = sum(_my_amount(o, dist['id']) for o in own_sales)
    # Red: reclutas activos y ventas de equipo, para la barra de nivel (ventas + reclutas).
    net = await _downline_stats(dist['id'])
    tier = dist.get('tier', pyramid.DEFAULT_TIER)
    return {
        'distributor_code': dist.get('distributor_code'),
        'commission_rate': dist.get('commission_rate', pyramid.tier_rate(tier)),
        'customer_discount_rate': dist.get('customer_discount_rate', 0),
        'tier': tier,
        'max_discount': pyramid.max_discount(tier),
        'clients_count': len(users),
        'sales_count': len(own_sales),
        'sales_total': sum(o.get('total', 0) for o in own_sales),
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
        uo = [o for o in by_user.get(u['id'], []) if o.get('status') != 'cancelado']
        # Privacidad (Christian 2026-07-23): el distribuidor ve un RESUMEN, no la
        # ficha del cliente. Nada de correo, teléfono ni domicilio.
        out.append({
            'id': u['id'], 'name': u['name'], 'created_at': u.get('created_at'),
            'orders_count': len(uo),
            'total_spent': sum(o.get('total', 0) for o in uo),
            'my_earnings': sum(o.get('commission', 0) for o in uo),
            'last_order_at': max([o.get('created_at', '') for o in uo], default=None),
        })
    out.sort(key=lambda u: -u['total_spent'])
    return out


async def _distributor_orders(dist):
    """Órdenes atribuidas al distribuidor: SOLO las hechas con su código
    (regla Christian 2026-07-22). Un pedido sin código no le pertenece."""
    orders = await db.orders.find({'referred_by': dist['id']}, {'_id': 0}).to_list(10000)
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
        'customer_name': ((o.get('customer') or {}).get('full_name') or '').split(' ')[0],
        'total': o.get('total', 0),
        'commission': o.get('commission', 0),
        'items_count': sum(int(it.get('quantity', 0) or 0) for it in o.get('items', [])),
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
        # Privacidad (Christian 2026-07-23): NADA de correo, teléfono, domicilio,
        # ni qué compuestos compró su cliente. Solo lo necesario para dar
        # seguimiento: quién, cuánto, cómo pagó, en qué va el envío.
        out.append({
            'order_number': o.get('order_number'),
            'created_at': o.get('created_at'),
            'status': o.get('status', 'pendiente'),
            'customer_name': (c.get('full_name') or '').split(' ')[0],
            'payment_method': o.get('payment_method'),
            'items_count': sum(int(it.get('quantity', 0) or 0) for it in o.get('items', [])),
            'total': o.get('total', 0),
            'discount_rate': o.get('discount_rate', 0),
            'commission': o.get('commission', 0),
            'carrier': o.get('carrier', ''),
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
    chat = build_chat(payload.session_id, payload.product_context, payload.language)
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


def tutorial_allowed(filename: str, role: str) -> bool:
    """Un cliente solo ve videos de cliente; distribuidor/admin ven todo."""
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
    user = await db.users.find_one({'id': user_id}, {'_id': 0, 'role': 1, 'blocked': 1})
    if not user or user.get('blocked'):
        raise HTTPException(status_code=401, detail='No autenticado')
    if '/' in filename or '..' in filename or not filename.endswith('.mp4'):
        raise HTTPException(status_code=404, detail='No encontrado')
    if not tutorial_allowed(filename, user.get('role', 'client')):
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


# ----------------- Admin: venta directa (2026-07-23) -----------------
class ManualOrderCreate(BaseModel):
    user_id: str
    items: List[OrderItem]
    discount_rate: float = 0.0        # p. ej. 0.40 en venta directa con Christian
    status: str = 'confirmado'
    note: str = ''


@api_router.post('/admin/orders')
async def admin_create_order(payload: ManualOrderCreate, admin=Depends(get_current_admin)):
    """Registra una VENTA DIRECTA (hecha en persona con Christian) en la cuenta
    del cliente, para que la vea en su historial. Sin comisión de nadie."""
    u = await db.users.find_one({'id': payload.user_id}, {'_id': 0, 'password_hash': 0})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    if payload.status not in ('pendiente', 'confirmado', 'enviado', 'entregado'):
        raise HTTPException(status_code=400, detail='Estado no válido')
    rate = max(0.0, min(0.60, payload.discount_rate))
    subtotal = sum(i.price * i.quantity for i in payload.items)
    discount = round(subtotal * rate)
    total = subtotal - discount
    points_earned = loyalty.earn(total, loyalty.eligible(u))
    order = Order(
        order_number=gen_order_number(),
        user_id=u['id'],
        items=payload.items,
        customer=CustomerInfo(full_name=u.get('name', ''), email=u.get('email'),
                              phone='', address='Venta directa', notes=payload.note),
        payment_method='directa',
        subtotal=subtotal, discount=discount, discount_rate=rate,
        shipping=0, total=total, status=payload.status,
        referred_by=None, commission=0, commissions=[],
        points_used=0, points_earned=points_earned,
    )
    await db.orders.insert_one(order.model_dump())
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
    my_orders = [o for o in orders if o.get('referred_by') == dist_id and o.get('status') != 'cancelado']
    by_user = {}
    for o in my_orders:
        if o.get('user_id'):
            b = by_user.setdefault(o['user_id'], {'orders': 0, 'total': 0.0, 'commission': 0.0, 'last': ''})
            b['orders'] += 1
            b['total'] += o.get('total', 0)
            b['commission'] += next((r.get('amount', 0) for r in o.get('commissions', [])
                                     if r.get('distributor_id') == dist_id), o.get('commission', 0) if o.get('referred_by') == dist_id else 0)
            b['last'] = max(b['last'], o.get('created_at', ''))
    clients = []
    for u in users:
        if u.get('referred_by') == dist_id or u['id'] in by_user:
            b = by_user.get(u['id'], {'orders': 0, 'total': 0, 'commission': 0, 'last': ''})
            clients.append({'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                            'orders': b['orders'], 'total': b['total'],
                            'commission': b['commission'], 'last_order': b['last'] or None})
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
              'commission': next((r.get('amount', 0) for r in o.get('commissions', [])
                                  if r.get('distributor_id') == dist_id), o.get('commission', 0))}
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
    paid = [o for o in orders if o.get('status') in loyalty.PAID_STATUSES]
    coupons = await db.discount_codes.find({'kind': 'coupon', 'user_id': user_id}, {'_id': 0}).to_list(100)
    ledger = await db.points.find({'user_id': user_id}, {'_id': 0}).to_list(200)
    ledger.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return {
        'customer': {'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                     'created_at': u.get('created_at'), 'blocked': u.get('blocked', False),
                     'referred_by': u.get('referred_by'),
                     'points_balance': int(u.get('points_balance', 0) or 0)},
        'orders': [{'id': o['id'], 'order_number': o.get('order_number'), 'created_at': o.get('created_at'),
                    'status': o.get('status'), 'total': o.get('total', 0),
                    'payment_method': o.get('payment_method'), 'discount': o.get('discount', 0),
                    'points_used': o.get('points_used', 0)} for o in orders[:100]],
        'paid_total': sum(o.get('total', 0) for o in paid),
        'paid_count': len(paid),
        'coupons': [{'code': c['code'], 'discount_rate': c.get('discount_rate', 0),
                     'expires_at': c.get('expires_at'), 'used': c.get('used', False),
                     'active': c.get('active', False), 'note': c.get('note', '')} for c in coupons],
        'points_ledger': ledger[:50],
    }


class CouponCreate(BaseModel):
    discount_rate: float           # 0.05 .. 0.50
    expires_days: int = 30
    note: str = ''


@api_router.post('/admin/customers/{user_id}/coupon')
async def admin_send_coupon(user_id: str, payload: CouponCreate, admin=Depends(get_current_admin)):
    """Cupón PERSONAL de un solo uso para un cliente. Sin comisión de nadie."""
    u = await db.users.find_one({'id': user_id}, {'_id': 0, 'id': 1, 'name': 1})
    if not u:
        raise HTTPException(status_code=404, detail='Cliente no encontrado')
    rate = max(0.05, min(0.50, payload.discount_rate))
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


app.include_router(api_router)


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=['*'],
    allow_headers=['*'],
)
