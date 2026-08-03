from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
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
import secrets                  # comparar la llave del prellenado en tiempo constante
import unicodedata            # para comparar nombres sin acentos al buscar duplicados
import html as html_lib          # para escapar lo que va en los avisos internos
# `csv` a secas chocaría con el parámetro `csv=1` de /admin/envios/costo-real, que es
# como se pide el export en ese formato. Se renombra el módulo, no el parámetro.
import csv as csv_mod
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, EmailStr
from database import db, client
from models import (
    RegisterInput, LoginInput, ForgotPasswordInput, ResetPasswordInput,
    ProfileUpdate, ChangePasswordInput,
    ProductCreate, ProductUpdate, Product, Category,
    OrderCreate, Order, OrderItem, CustomerInfo, OrderStatusUpdate, OrderShippingUpdate,
    DistributorShippingUpdate,
    ShippingQuoteRequest, TrackEvent, RemitenteUpdate, CajasUpdate, EmpaquesUpdate,
    ComprarGuiaRequest, CotizadorEnvioRequest,
    ProtocolInput, ProtocolUpdate, PerfilSalud, LabReportInput,
    TokenInput, ActivateInput, ResendVerificationInput, AceptarAcuerdoInput,
    ChatInput, DistributorCreate, DiscountCodeCreate, AnnouncementCreate, GoogleAuthInput, now_iso,
    QuoteEmailRequest, ShareCartRequest, PrellenadoRequest,
    SolicitudPagoComision, RegistroPagoComision, RechazoPagoComision,
    AprobarSolicitudGuia, RechazoSolicitudGuia,
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
# El motor del chat es intercambiable por variable de entorno, y de aquí salen
# también los avisos de "no pude responder" en los tres idiomas. Ver modelo_ia.py.
import modelo_ia
# La red de abajo del formato: lo que el modelo escriba en Markdown y la pantalla
# no necesite, se limpia antes de salir — sin partir un marcador a la mitad
# cuando la respuesta llega en chorrito. Ver texto_ia.py.
import texto_ia
import coa_store
import ficha_store
import secretos
import meta_ads
import meta_capi
import marketing
# El archivo histórico de los reportes semanales de publicidad: los MP4 viven en
# disco (fuera de git y fuera del contenedor) y lo que se compara son las cifras
# que quedan guardadas junto a cada video. Ver reportes_ads.py.
import reportes_ads
import director
import recovery
from google_auth import verify_google_token, google_enabled, GOOGLE_CLIENT_ID
from microsoft_auth import verify_microsoft_token, microsoft_enabled, MICROSOFT_CLIENT_ID
import loyalty
import pyramid
# LA REGLA DE 5 (consumo propio de distribuidores) y el cierre de la puerta
# anónima. Módulo puro para poder probarlo de verdad; ver descuentos.py.
import descuentos
# El 5% por pagar en cripto. Lo financia la comisión de pasarela que NO se paga.
import descuento_cripto
import comisiones
import guia_solicitudes
# ⛔ OBSEQUIOS DEL DISTRIBUIDOR Y CARRITO COMPARTIBLE (Christián, 2026-08-01). El
# regalo se APILA con el código de descuento, su código interno NUNCA se le enseña
# al cliente, y no puede romper el ROI. Módulo puro; ver regalos.py.
import regalos
# Los TEXTOS de la campanita cuando entra una venta (en los tres idiomas).
import avisos_de_venta
# ⛔ ACUERDO DE DISTRIBUIDOR — aceptación electrónica. NACE APAGADO: mientras
# ACUERDO_DISTRIBUIDOR_ACTIVO no valga 'true', ninguna de estas llamadas cambia
# nada para nadie. Ver acuerdo.py.
import acuerdo
# La constancia del aviso de entrada (RUO): quién aceptó, cuándo y desde dónde.
import ruo_constancia
# ⛔ QUÉ CUENTA COMO INGRESO. Una sola regla para todo el backend: ver cobrado.py.
# Los nombres se re-exportan aquí porque medio server.py (y los tests) ya los usaban
# cuando la regla vivía dentro de este archivo.
from cobrado import (ESTADOS_PAGADOS, esta_pagado, esta_vivo, cobrado_de,
                     por_cobrar_de, solo_cobrados)
# Marcar y barrer los pedidos que dejan las pruebas, sin tocar una venta real.
import pruebas
import auth_factors
import btcpay
import mercadopago
import nowpayments
import envios
import paqueterias
import skydropx
# EL PDF DE LA GUÍA, servido por la casa y listo para imprimir desde el panel
# (admin y distribuidor). Vive aparte porque trae sus propias rutas y su propio
# candado de rol; ver etiquetas.py.
import etiquetas
# De que paqueteria es un numero de guia. Gemelo de la deteccion de la pantalla
# (src/lib/paqueteria.js); ver guias.py.
import guias
# EL RASTREO DENTRO DE NUESTRA PÁGINA. La paquetería no se deja enmarcar
# (`x-frame-options: SAMEORIGIN`), así que le pedimos los eventos a su API y los
# pintamos nosotros. Vive aparte y sólo lee; ver rastreo.py.
import rastreo
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


# El prefijo de la casa: quien atiende de cara al cliente. Es
# `emails.ATENCION_NOMBRE` ('Mónica Flores') escrito como se escribe un código.
#
# ⛔ NO ES DE TODOS: ES DE QUIEN LO TRAIGA MARCADO EN SU FICHA (Christián,
# 2026-07-31, la corrección del mismo día). La primera versión se lo puso a TODOS
# los distribuidores y estuvo mal: la orden de privacidad hablaba de MARÍA y de
# nadie más. Alanís y Javier vuelven a emitir con su propio nombre.
#
# El interruptor es `users.code_prefix`, una marca POR PERSONA. Si la ficha lo
# trae, los códigos de ese distribuidor salen con él; si no, salen de su nombre,
# como toda la vida. Una regla global es exactamente lo que hubo que deshacer.
PREFIJO_CODIGO = 'MONICAF'

# Lo único que cabe en un código. Fuera de aquí, nada.
_CODIGO_OK = string.ascii_uppercase + string.digits


def prefijo_de(quien, largo: int = 6) -> str:
    """El prefijo de los códigos de ESTE distribuidor.

    `quien` es la ficha del distribuidor (dict) o —cuando todavía no existe,
    porque apenas se está dando de alta— su nombre suelto.

    La marca de la ficha (`code_prefix`) MANDA sobre el nombre. Así la orden del
    31-jul («los clientes no pueden ver que el código de descuento es de María»)
    tapa a María sin arrastrar a nadie: sólo su ficha trae `code_prefix`.

    Lo pregunta el generador de las DOS familias de códigos —el AUTO por nivel y
    el ÚNICO legacy—, que es la lección de la vez pasada: cambiar sólo uno deja
    la mitad de la regla en pie y nadie lo nota."""
    if isinstance(quien, dict):
        marca = ''.join(c for c in str(quien.get('code_prefix') or '').upper()
                        if c in _CODIGO_OK)[:12]
        if marca:
            return marca
        quien = quien.get('name') or quien.get('email') or ''
    return ''.join(c for c in str(quien or '').upper() if c in _CODIGO_OK)[:largo] or 'DIST'


def gen_distributor_code(quien) -> str:
    """El código ÚNICO (legacy) del distribuidor: PREFIJO-NNNN.

    Es el HERMANO OLVIDADO de `gen_discount_code` —vive en `users.distributor_code`,
    no en `discount_codes`, y `_resolve_code` cae a él cuando el texto no está en
    la colección—. `quien` puede ser la ficha o el nombre: ver `prefijo_de`."""
    return prefijo_de(quien, 4) + '-' + str(random.randint(1000, 9999))


async def resolve_distributor(code):
    """Devuelve el distribuidor (dict) para un codigo dado, o None.

    ⛔ MIRA LAS DOS COLECCIONES (2026-07-31). Sólo consultaba
    `users.distributor_code`, o sea el código ÚNICO legacy. Un registro con `?ref=`
    de uno de los códigos AUTO —o de un legacy jubilado por la rotación a
    `MONICAF`— no vinculaba al cliente con nadie: la venta entraba huérfana y sin
    comisión, callada. `_resolve_code` busca primero en `discount_codes` y cae a
    `users`, que es EL MISMO orden que usa el checkout: así el enlace de referido y
    la caja no pueden volver a contestar cosas distintas al mismo texto."""
    if not code:
        return None
    dist, _ = await _resolve_code(code)
    return dist


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


def gen_discount_code(quien, pct):
    """Código OPAQUE, no adivinable: PREFIJO-PCT-XXXX (parte al azar). El % en el
    texto es informativo; el descuento real SIEMPRE sale del valor guardado.

    El PREFIJO sale de `prefijo_de(quien)`: la marca de la ficha si la trae
    (María: `MONICAF-15-R4YV`), y si no, el nombre de siempre (`ALANIS-20-FRUK`,
    `JAVIER-25-RHV4`). `quien` es la ficha completa, no el nombre suelto —eso es
    lo que le permite ver la marca—; se acepta un texto sólo para el alta, cuando
    la ficha todavía no existe.

    Lo vigila `test_privacidad_distribuidor.py`: el código de quien está marcado
    no puede delatarlo."""
    rand = ''.join(random.choices(_CODIGO_OK, k=4))
    return f'{prefijo_de(quien, 6)}-{int(round((pct or 0) * 100))}-{rand}'


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
        # 21, no 18 (Christián, 2026-08-02). Se le pasó al barrido de ese día porque
        # este texto vive en el backend y el barrido miró el frontend y los i18n.
        raise HTTPException(status_code=400, detail='Debes confirmar que tienes 21 anos o mas y aceptar los Terminos y Condiciones')
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


def _aviso_de_peso(doc: dict) -> dict | None:
    """El GRITO de un alta sin peso. `None` cuando el producto sí trae báscula.

    ⛔ UN DATO AUSENTE GRITA, NO SE RELLENA CON UN CERO. `weight_kg` nació vacío en
    todo el catálogo y el envío se cotizaba con un número redondo que nadie midió;
    el hueco no se veía porque el sistema lo tapaba solo. Dar de alta un producto
    sin peso SIGUE ESTANDO PERMITIDO —no se bloquea una venta por un dato de
    logística— pero el alta contesta diciendo que va con un estimado, y el producto
    queda listado en /admin/envios/pesos hasta que alguien lo ponga en la báscula.
    """
    if envios.origen_del_peso(doc) == 'declarado':
        return None
    estimado = envios.peso_estimado_de_pieza(doc)
    logger.warning(
        'PESO SIN CAPTURAR: "%s" (%s) se dio de alta sin weight_kg. El envío lo va a '
        'cotizar con un ESTIMADO de %s kg. Capturar el real en Admin → Envíos → Pesos.',
        doc.get('name') or doc.get('slug') or doc.get('id') or '?',
        doc.get('sku') or 's/SKU', estimado)
    return {
        'campo': 'weight_kg',
        'origen': 'estimado',
        'peso_estimado_kg': estimado,
        'detalle': ('Este producto NO tiene peso capturado. El envío se va a cotizar '
                    f'con un estimado de {estimado} kg calculado de su presentación, '
                    'no con una báscula. Captúralo en Admin → Envíos → Pesos.'),
    }


@api_router.post('/admin/products')
async def create_product(payload: ProductCreate, admin=Depends(get_current_admin)):
    existing = await db.products.find_one({'slug': payload.slug})
    if existing:
        raise HTTPException(status_code=400, detail='Ya existe un producto con ese slug')
    product = Product(**payload.model_dump())
    await db.products.insert_one(product.model_dump())
    creado = clean(product.model_dump())
    aviso = _aviso_de_peso(product.model_dump())
    if aviso:
        creado = dict(creado, aviso_peso=aviso)
    return creado


@api_router.put('/admin/products/{product_id}')
async def update_product(product_id: str, payload: ProductUpdate, admin=Depends(get_current_admin)):
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if not update:
        raise HTTPException(status_code=400, detail='Sin cambios')
    result = await db.products.update_one({'id': product_id}, {'$set': update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail='Producto no encontrado')
    product = await db.products.find_one({'id': product_id}, {'_id': 0})
    aviso = _aviso_de_peso(product or {})
    if aviso:
        product = dict(product or {}, aviso_peso=aviso)
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

# LA PROMO AUTOMÁTICA DE LA CASA: lo que recibe cualquier cliente que llega solo,
# sin código y sin distribuidor. 10% parejo, 15% desde $35,000 de mercancía
# descuentable (Christian, 2026-07-21: el escalón del 20% se quitó y el 15% subió
# a $35,000 para no competir con los descuentos de los distribuidores).
UMBRAL_PROMO_15 = 35000


def promo_automatica(mercancia_descuentable: float) -> float:
    """El descuento que la casa da SOLA, sin que nadie lo pida.

    ⛔ ES EL PISO DE CUALQUIER VENTA (Christián, 2026-08-01): un cliente que llega
    por un distribuidor que NO puso descuento propio recibe esta promo igual — no
    puede quedar PEOR por haber llegado recomendado. La pantalla del carrito ya
    prometía «el mayor gana» desde antes; esta función es la misma regla puesta
    donde cobra."""
    return 0.15 if (mercancia_descuentable or 0) >= UMBRAL_PROMO_15 else 0.10


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
# ⛔ QUÉ PAGOS NO SON INMEDIATOS. Es lo que decide si al cliente le sale un correo al
# comprar o si se espera al de «pago confirmado» (ver el checkout más abajo).
#
# SPEI: tiene que ir a su banco a transferir y necesita la CLABE por escrito.
# OXXO:  tiene que ir a la tienda con su ficha.
# Tarjeta y cripto se confirman en segundos: mandarles un correo al comprar y otro al
# confirmar es mandar dos correos con un minuto de diferencia.
PAGOS_DIFERIDOS = ('spei', 'oxxo')

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
# ✅ LOS DOS INTERRUPTORES ESTÁN PRENDIDOS desde el 2026-08-01
# (`envios.COTIZAR_EN_CHECKOUT` y `envios.COMPRAR_GUIA_AL_PAGAR`, los dos en True).
# El checkout cotiza con Skydropx, le cobra el envío al cliente, y cuando entra el
# pago el servidor compra la guía solo, con el tope de `TOPE_GUIA_AUTOMATICA_MXN`.
#
# ⛔ `COTIZAR_EN_CHECKOUT` NO SE APAGA. Christián lo ordenó así: «Yo jamás lo apagué.
# Préndelo y SIEMPRE debe estar prendido.» Este comentario decía justo lo contrario
# —que ambos nacían apagados— y era mentira desde ese día; se corrigió el 2026-08-01.
#
# Y lo que costó la mentira, para que no se repita: `COTIZAR_EN_CHECKOUT` nació
# apagado el 28-jul como precaución mientras se estrenaba Skydropx y ahí se quedó,
# pero `COMPRAR_GUIA_AL_PAGAR` sí quedó prendido. O sea que durante esos días la casa
# **compró la guía de cada pedido y no se la cobró a nadie**: entre $165 y $250
# regalados por venta, invisibles porque el checkout se veía normal.
#
# Apagar el interruptor NO es la forma de reaccionar a una caída de la paquetería.
# `envio_se_cotiza()` exige DOS cosas —el interruptor y que exista la llave de
# Skydropx—, así que sin llave, o si la paquetería no contesta, el sitio se degrada
# solo: cae a la tarifa plana de $250 y sigue vendiendo. Apagarlo es otra cosa, es
# decidir NO COBRAR, y eso sólo lo decide Christián.
#
# Lo fija `test_core.test_el_cobro_del_pedido_respeta_el_interruptor`, que exige los
# dos en True: si alguien los apaga, la suite se pone roja.
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


# ---------------- el código postal sugiere la ciudad y el estado ----------------
# Encargo de Christián (2026-08-02): «If he enters the zip code, that should
# assist User with suggesting the rest of the address.» La fuente es Nominatim
# (OpenStreetMap), que para un CP mexicano devuelve municipio y estado; si no
# contesta, se cae a Zippopotam, que al menos trae el estado. Con caché en
# memoria: un CP no cambia de ciudad entre visitas, y así a los proveedores
# gratuitos se les pega UNA vez por CP, no una por tecla.
_CP_CACHE = {}
_CP_NADA = {'found': False, 'city': '', 'state': ''}


def _ciudad_estado_de_nominatim(cuerpo) -> dict:
    """Saca (ciudad, estado) del JSON de Nominatim. Pura, para poderla probar."""
    try:
        addr = (cuerpo or [])[0].get('address') or {}
    except (IndexError, AttributeError, TypeError):
        return dict(_CP_NADA)
    ciudad = addr.get('city') or addr.get('town') or addr.get('county') or ''
    # «Municipio de Tijuana» → «Tijuana»: el campo del checkout es la ciudad.
    for sobra in ('Municipio de ', 'Municipality of '):
        if ciudad.startswith(sobra):
            ciudad = ciudad[len(sobra):]
    estado = addr.get('state') or ''
    if not (ciudad or estado):
        return dict(_CP_NADA)
    return {'found': True, 'city': ciudad.strip(), 'state': estado.strip()}


@api_router.get('/cp/{cp}')
async def sugerir_por_cp(cp: str):
    """Ciudad y estado desde el código postal. Público y de sólo lectura.

    Nunca revienta hacia el checkout: sin respuesta de las fuentes se contesta
    `found: False` y el cliente simplemente teclea su ciudad como siempre."""
    cp = (cp or '').strip()
    if not (cp.isdigit() and len(cp) == 5):
        raise HTTPException(status_code=400, detail='El código postal son 5 dígitos')
    if cp in _CP_CACHE:
        return _CP_CACHE[cp]
    import requests as _rq
    out = dict(_CP_NADA)
    try:
        r = _rq.get('https://nominatim.openstreetmap.org/search',
                    params={'postalcode': cp, 'country': 'mx', 'format': 'json',
                            'addressdetails': 1, 'limit': 1},
                    headers={'User-Agent': 'ExygenLabs/1.0 (soporte@exygenlabs.com)'},
                    timeout=5)
        if r.ok:
            out = _ciudad_estado_de_nominatim(r.json())
    except Exception:
        logger.info('CP %s: Nominatim no contestó; probando Zippopotam', cp)
    if not out['found']:
        try:
            r = _rq.get(f'https://api.zippopotam.us/MX/{cp}', timeout=5)
            if r.ok:
                lugares = (r.json() or {}).get('places') or []
                estado = (lugares[0].get('state') or '').strip() if lugares else ''
                if estado:
                    out = {'found': True, 'city': '', 'state': estado}
        except Exception:
            logger.info('CP %s: Zippopotam tampoco contestó', cp)
    # Sólo se guarda lo ENCONTRADO: un «no» de hoy puede ser un timeout, no un CP
    # inexistente, y cachearlo dejaría ese CP mudo hasta el próximo despliegue.
    if out['found']:
        _CP_CACHE[cp] = out
    return out


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


def _COSTO_ESTIMADO_EXPRESS() -> float:
    """Lo que cuesta una guía EXPRESS cuando la paquetería no contestó: el estimado
    de la casa más los ~$150 que el servicio rápido cuesta de más en la vida real
    (el mismo número del extra, y no es casualidad: de ahí salió)."""
    return float(COSTO_GUIA_ESTIMADO) + float(envios.EXTRA_EXPRESS_MXN)


def _cobro_express(paid_merchandise: float, costo_real: float | None) -> int:
    """Cuánto paga el cliente por un envío EXPRESS. La regla v2 (Christián,
    2026-08-02, con estas palabras: «si el costo total... no pasa de 5% en total,
    la casa absorbe el costo total, ¿va?»):

      · Abajo de la mínima: tarifa plana + extra ($250 + $150 = $400).
      · Desde $2,500: el costo REAL de la guía express al CP del cliente se mide
        contra el presupuesto de absorción — max($250, 5% de la compra). Si cabe,
        GRATIS TOTAL (ni los $150 se cobran); si se pasa, paga SOLO el excedente.
        Su ejemplo: compra de $30,000, guía express de ~$700 → cliente paga $0.

    `costo_real=None` = la paquetería no contestó: se usa el estimado express de
    la casa, que es lo que impide regalar un express por una falla ajena."""
    costo = float(costo_real) if costo_real else _COSTO_ESTIMADO_EXPRESS()
    base = envios.cobro_de_envio_al_cliente(costo, paid_merchandise, FREE_SHIPPING_FROM,
                                            tarifa_plana=SHIPPING_FLAT)
    if float(paid_merchandise or 0) < float(FREE_SHIPPING_FROM):
        return round(base + envios.EXTRA_EXPRESS_MXN)
    return round(base)


def _opcion_express(opciones):
    """La opción EXPRESS de una lista ya filtrada y ordenada (días, luego precio):
    la primera que promete 1-2 días. Si ninguna lo promete, la más rápida que haya
    — al cliente se le cumple con lo mejor que exista ese día."""
    for o in (opciones or []):
        try:
            d = int(o.get('dias') or 0)
        except (TypeError, ValueError):
            d = 0
        if 0 < d <= envios.DIAS_MAXIMOS_EXPRESS:
            return o
    return (opciones or [None])[0]


async def _envio_del_pedido(payload, paid_merchandise, pflags):
    """Cuánto se le cobra de envío a este pedido, y con qué cotización.

    ⛔ EL PRECIO LO PONE EL SERVIDOR. El monto de envío que venga en la petición se
    ignora por completo, igual que se ignoran los precios de los productos.

    ✅ LA ESTRATEGIA DEL 2026-08-02 (Christián): el cliente ya no escoge paquetería
    — escoge el TIPO. El cobro es POLÍTICA, no cotización:

        estándar  = $250 abajo de la mínima; desde $2,500 INCLUIDO, con el piso
                    de absorción (la casa come hasta $250 o el 5%, lo mayor;
                    el excedente de una guía monstruosa lo paga el cliente);
        express   = lo anterior + $150, SIEMPRE.

    Skydropx se sigue consultando, pero POR DENTRO: para saber lo que la guía
    cuesta de verdad (el candado del excedente y los reportes de absorción) y
    para dejar apuntado qué servicio comprar al pagarse (rápido si es express).
    Si la paquetería no contesta, la política cobra igual con el costo estimado
    de la casa — el cobro ya no depende de que un tercero conteste a tiempo.

    Devuelve (lo que paga el cliente, lo que se guarda en el pedido).
    """
    es_express = bool(getattr(payload, 'shipping_express', False))
    if not COBRAR_ENVIO:
        return 0, {}
    if not envio_se_cotiza():
        # El camino de siempre: la tarifa plana dormida detrás de COBRAR_ENVIO. La
        # línea se escribe TAL CUAL porque hay pruebas que la buscan literal — es el
        # candado que impide que alguien vuelva a dejar el envío sin interruptor.
        shipping = shipping_for(paid_merchandise) if COBRAR_ENVIO else 0
        if es_express:
            shipping = _cobro_express(paid_merchandise, None)
        return round(shipping), ({'express': True} if es_express else {})
    cp = (payload.customer.postal_code or '').strip()
    paquete = envios.paquete_del_pedido(payload.items, pflags)
    try:
        frescas = skydropx.cotizar(cp, paquete, destino={
            'province': getattr(payload.customer, 'state', '') or '',
            'city': getattr(payload.customer, 'city', '') or '',
            'country': getattr(payload.customer, 'country', 'MX') or 'MX'})
    except Exception:
        logger.exception('Skydropx: no se pudo cotizar el pedido a %s', cp)
        frescas = []
    # Estándar: la MÁS BARATA de las permitidas (todas cumplen el plazo de 5 días;
    # el cliente paga política, así que el ahorro de la guía es de la casa).
    # Express: la primera que promete 1-2 días.
    if es_express:
        opcion = _opcion_express(frescas) or {}
        costo = float(opcion.get('precio') or 0) or None
        cobrado = _cobro_express(paid_merchandise, costo)
        costo = costo if costo is not None else _COSTO_ESTIMADO_EXPRESS()
    else:
        opcion = (min(frescas, key=lambda o: float(o.get('precio') or 0))
                  if frescas else {})
        # Sin tarifas no se inventa un costo de guía: se usa el estimado de la casa.
        costo = float(opcion.get('precio') or 0) or float(COSTO_GUIA_ESTIMADO)
        cobrado = round(envios.cobro_de_envio_al_cliente(
            costo, paid_merchandise, FREE_SHIPPING_FROM, tarifa_plana=SHIPPING_FLAT))
    guardado = {
        'carrier': opcion.get('paqueteria', ''),
        'service': opcion.get('servicio', ''),
        'service_code': opcion.get('servicio_codigo', ''),
        'days': opcion.get('dias', 0),
        'express': es_express,
        'cost': round(costo, 2),
        'charged': cobrado,
        'peso_kg': paquete.get('peso_kg'),
        'paquete': paquete,
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
    # Los empaques REALES (los que deciden si la guía se compra sola). Vacío = manda
    # la tabla de fábrica, que hoy es la única bolsa que Christián tiene.
    envios.cargar_empaques_del_panel(doc.get('empaques') or [])
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
        # Los empaques de verdad: qué hay en la bodega y hasta cuántas piezas le caben.
        # Es lo que decide si el servidor compra la guía solo o le pregunta a Christián.
        'empaques': [dict(e, peso_volumetrico_kg=envios.peso_volumetrico(e))
                     for e in envios.empaques()],
        'empaques_de_fabrica': not bool(doc.get('empaques')),
        'piezas_que_compran_solas': max(
            (int(e.get('hasta_piezas') or 0) for e in envios.empaques()), default=0),
        'tope_guia_automatica': envios.TOPE_GUIA_AUTOMATICA_MXN,
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


@api_router.get('/admin/envios/pesos')
async def admin_pesos_de_productos(admin=Depends(get_current_admin)):
    """Qué productos tienen peso DE BÁSCULA y cuáles van con una cuenta.

    ⛔ EL CANDADO DEL PENDIENTE 14. `weight_kg` nació vacío en todo el catálogo y el
    envío se cotizaba con un peso por omisión —0.05 kg por vial— que nadie había
    medido ni calculado: era un número redondo. Un dato ausente que se rellena solo
    y en silencio deja de verse, y a los meses ya nadie sabe cuál es cuál.

    Aquí se ven separados a propósito:
      · `declarado` = alguien lo puso en la báscula y lo capturó. Sustenta dinero.
      · `estimado`  = lo calculó `envios.peso_estimado_de_pieza` del formato de
        frasco que le toca a su presentación (vidrio ISO 8362-1 + cierre + etiqueta
        + burbuja). Sirve para cotizar y para despachar; NO para decidir.

    `diferencia_kg` sólo aparece en los declarados: es de cuánto se equivocaba la
    cuenta contra la báscula, que es la forma de saber si el estimado sirve.
    """
    docs = await db.products.find(
        {}, {'_id': 0, 'id': 1, 'sku': 1, 'name': 1, 'slug': 1, 'category': 1,
             'presentation': 1, 'hidden': 1, 'weight_kg': 1}).to_list(1000)
    filas = []
    for d in docs:
        estimado = envios.peso_estimado_de_pieza(d)
        origen = envios.origen_del_peso(d)
        try:
            declarado = round(float(d.get('weight_kg') or 0), 4)
        except (TypeError, ValueError):
            declarado = 0.0
        mg, ml = envios.contenido_declarado(str(d.get('presentation') or ''))
        filas.append({
            'id': d.get('id') or '', 'sku': d.get('sku') or '',
            'nombre': d.get('name') or '', 'presentacion': d.get('presentation') or '',
            'oculto': bool(d.get('hidden')),
            'origen': origen,
            'peso_declarado_kg': declarado or None,
            'peso_estimado_kg': estimado,
            'formato_vial': envios.formato_de_vial(mg=mg, ml=ml),
            'diferencia_kg': (round(declarado - estimado, 4)
                              if origen == 'declarado' else None),
        })
    faltan = [f for f in filas if f['origen'] == 'estimado']
    filas.sort(key=lambda f: (f['origen'] != 'estimado', f['nombre'].lower()))
    return {
        'total': len(filas),
        'declarados': len(filas) - len(faltan),
        'estimados': len(faltan),
        'fuente_del_estimado': envios.FUENTE_DEL_PESO,
        'productos': filas,
    }


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


@api_router.put('/admin/envios/empaques')
async def admin_guardar_empaques(payload: EmpaquesUpdate, admin=Depends(get_current_admin)):
    """Los empaques REALES de la bodega y hasta cuántas piezas le caben a cada uno.

    ⛔ ES LA PANTALLA QUE DESTRABA LA COMPRA AUTOMÁTICA. Hoy Christián tiene un solo
    empaque —la bolsa stand-up de 12×15×1 cm, ~4 piezas— y por eso todo pedido de 5
    piezas o más se detiene y le pregunta a él. El día que compre cajas, captura aquí
    sus medidas y hasta cuántas piezas les caben, y ese tamaño de pedido empieza a
    comprar guía solo. **Sin tocar código y sin desplegar.**

    Guardar una lista vacía devuelve el control a la tabla de fábrica (la bolsa), que es
    lo correcto: quedarse sin empaques sería quedarse sin poder despachar nada.
    """
    empaques = [e.model_dump() for e in (payload.empaques or [])]
    if empaques and not envios.cargar_empaques_del_panel(empaques):
        # Medidas en cero o sin tope de piezas: no se guarda nada. Un empaque inválido
        # haría que el servidor comprara guías cotizadas contra basura, que es
        # exactamente el recobro por sobrepeso que esto viene a evitar.
        raise HTTPException(
            status_code=400,
            detail='Cada empaque necesita medidas mayores a cero y cuántas piezas le caben')
    await db[COLECCION_AJUSTES_ENVIO].update_one(
        {'id': 'envios'},
        {'$set': {'id': 'envios', 'empaques': empaques, 'updated_at': now_iso()}},
        upsert=True)
    await _cargar_ajustes_envio()
    return {'empaques': [dict(e, peso_volumetrico_kg=envios.peso_volumetrico(e))
                         for e in envios.empaques()],
            'piezas_que_compran_solas': max(
                (int(e.get('hasta_piezas') or 0) for e in envios.empaques()), default=0)}


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


# ==========================================================================
#  COTIZADOR DE ENVÍOS — preguntar sin tener que armar un pedido
# ==========================================================================
# ⛔ POR QUÉ EXISTE (Christián, 2026-08-01). Hasta hoy el envío sólo se podía
# cotizar de dos maneras, y las dos exigían un pedido: el checkout (con un carrito)
# o la ficha de un pedido ya hecho. Pero la pregunta que se hace todos los días —
# «¿cuánto cuesta mandar esto a tal código postal?»— llega ANTES de que exista el
# pedido, cuando un cliente pregunta por WhatsApp. Sin esta pantalla la respuesta
# era inventar un carrito de mentira o adivinar.
#
# ⛔ DOS PANTALLAS, UNA SOLA CUENTA. El admin y el distribuidor preguntan lo mismo y
# el servidor cotiza igual; lo que cambia es lo que se DEVUELVE:
#   · el admin ve además lo que la casa absorbe, si se pasa del tope y qué proveedor
#     ganó — él sí ve costos;
#   · el distribuidor ve la tarifa, el plazo y lo que se le cobraría al cliente. Y
#     NADA MÁS. Su respuesta se arma con una lista blanca de llaves (`_solo_lo_del_`
#     `distribuidor`), no quitando campos: quitar se olvida el día que se agregue
#     uno nuevo, la lista blanca no. Hay pruebas que intentan sacarle un costo.
#
# ⛔ COLECCIÓN APARTE, A PROPÓSITO. Las cotizaciones de aquí NO viven en
# `shipping_quotes` sino en `shipping_cotizador`. En modo manual el peso lo teclea
# una persona, y si compartieran colección un id de este cotizador podría acabar
# pagando un envío en el checkout. Separadas, eso es imposible: `_cotizacion_valida`
# sólo lee la otra. El precio del checkout lo sigue poniendo el servidor.
COLECCION_COTIZADOR = 'shipping_cotizador'

# Cuántas consultas recientes se guardan y se enseñan. Es un historial CORTO a
# propósito: sirve para no repetir la consulta de hace un rato, no para auditar.
HISTORIAL_COTIZADOR = 10

# Skydropx (y el segundo proveedor) sólo cubren México. No es una limitación
# nuestra: es el alcance de las paqueterías contratadas. Decirlo de frente es mejor
# que dejar una rueda girando hasta que la API contesta que no.
PAISES_CON_COBERTURA = ('', 'MX', 'MEX', 'MEXICO', 'MÉXICO')


def _hay_cobertura(pais: str) -> bool:
    return (pais or 'MX').strip().upper() in PAISES_CON_COBERTURA


async def _bulto_del_cotizador(payload) -> tuple:
    """El bulto a cotizar y el importe de mercancía.

    Devuelve (paquete, mercancía, piezas, renglones sin precio).

    En 'items' los dos números salen del CATÁLOGO —el mismo que usa el checkout— y
    lo que venga del navegador sobre peso o precio no se mira. En 'manual' el peso es
    el que se capturó, y por eso ese camino no puede cobrar nada (ver `models`).

    ⛔ EL CUARTO NÚMERO NO ES ADORNO. Si un renglón no aparece en el catálogo del
    servidor, su precio NO se toma del navegador —eso es justo lo que no se hace
    aquí— así que suma cero y el importe de compra queda corto. Corto quiere decir
    que la pantalla diría «el cliente paga los $250 de tarifa plana» cuando el pedido
    de verdad quizá pasaba de la mínima. Callarlo es enseñar un número equivocado con
    cara de bueno; por eso se cuenta y se dice.
    """
    if (payload.mode or 'items').strip().lower() == 'manual':
        paquete = envios.paquete_manual(payload.peso_kg, payload.largo_cm,
                                        payload.ancho_cm, payload.alto_cm)
        try:
            mercancia = max(0.0, float(payload.merchandise_mxn or 0))
        except (TypeError, ValueError):
            mercancia = 0.0
        return paquete, round(mercancia, 2), 0, 0
    pflags = await _catalogo_de(payload.items)
    paquete = envios.paquete_del_pedido(payload.items, pflags)
    mercancia, sin_precio = 0.0, 0
    for it in payload.items or []:
        doc = pflags.get(it.product_id) or {}
        try:
            precio = float(doc.get('price') or 0)
            mercancia += precio * max(0, int(it.quantity or 0))
        except (TypeError, ValueError):
            precio = 0.0
        if precio <= 0:
            sin_precio += 1
    return (paquete, round(mercancia, 2), envios.piezas_del_pedido(payload.items),
            sin_precio)


def _solo_lo_del_distribuidor(resp: dict) -> dict:
    """La MISMA respuesta, recortada a lo que el distribuidor puede ver.

    ⛔ LISTA BLANCA, NO LISTA NEGRA. Se copian las llaves permitidas a un diccionario
    nuevo; todo lo demás se queda fuera por no estar nombrado. Si mañana alguien le
    agrega `costo_de_compra` a la respuesta del admin, aquí no aparece solo — que es
    exactamente lo contrario de lo que pasaría con un `pop()`.

    Lo que SÍ ve: la tarifa del envío, el plazo, la paquetería y lo que se le cobraría
    al cliente con la política de la casa (que es pública: está en el checkout).
    Lo que NO: lo que la casa absorbe, si se pasó de su tope, con qué proveedor se
    contrata, cuánto se ahorra por comparar y el detalle del empaque.
    """
    opciones = [{'carrier': o.get('carrier', ''), 'service': o.get('service', ''),
                 'days': o.get('days', 0), 'price': o.get('price', 0),
                 'recomendada': bool(o.get('recomendada'))}
                for o in (resp.get('opciones') or [])]
    cobro = resp.get('cobro') or {}
    return {
        'enabled': resp.get('enabled', False),
        'cobertura': resp.get('cobertura', True),
        'pais': resp.get('pais', 'MX'),
        'detail': resp.get('detail', ''),
        'peso_kg': resp.get('peso_kg', 0),
        'quoted_at': resp.get('quoted_at', ''),
        'opciones': opciones,
        'cobro': {
            'mercancia': cobro.get('mercancia', 0),
            'cliente_paga': cobro.get('cliente_paga', 0),
            'gratis': cobro.get('gratis', False),
            'envio_gratis_desde': cobro.get('envio_gratis_desde', 0),
            'tarifa_plana': cobro.get('tarifa_plana', 0),
            'falta_para_gratis': cobro.get('falta_para_gratis', 0),
            'productos_sin_precio': cobro.get('productos_sin_precio', 0),
        },
    }


async def _cotizar_para_el_cotizador(payload, quien: dict, es_admin: bool) -> dict:
    """El cotizador completo, con TODO lo que ve el admin. El recorte va aparte.

    Se degrada con elegancia igual que el checkout: sin credenciales, fuera de México
    o sin tarifas, responde una frase que se puede leer en pantalla en vez de dejar
    la rueda girando.
    """
    pais = (payload.country or 'MX').strip().upper() or 'MX'
    if not _hay_cobertura(pais):
        # ⛔ Se contesta ANTES de llamar a nadie: las paqueterías contratadas no salen
        # de México y preguntarles por Madrid es esperar un "no" caro.
        return {'enabled': True, 'cobertura': False, 'pais': pais, 'opciones': [],
                'detail': 'Fuera de México no hay cobertura: las paqueterías '
                          'contratadas (Skydropx) sólo cubren la República Mexicana. '
                          'Un envío internacional se cotiza aparte, a mano.'}
    cp = (payload.postal_code or '').strip()
    if len(cp) < 5:
        return {'enabled': True, 'cobertura': True, 'pais': pais, 'opciones': [],
                'detail': 'Falta el código postal de destino (5 dígitos).'}
    paquete, mercancia, piezas, sin_precio = await _bulto_del_cotizador(payload)
    if not paquete.get('peso_kg'):
        return {'enabled': True, 'cobertura': True, 'pais': pais, 'opciones': [],
                'detail': 'Falta decir qué se manda: elige productos o captura el peso.'}
    if not paqueterias.cuantos_activos():
        return {'enabled': False, 'cobertura': True, 'pais': pais, 'opciones': [],
                'proveedores': paqueterias.encendidos(),
                'detail': 'No hay paquetería conectada: faltan SKYDROPX_CLIENT_ID y '
                          'SKYDROPX_CLIENT_SECRET (se pegan en Admin → Cobros).'}
    destino = {'zip': cp, 'province': payload.state or '', 'city': payload.city or '',
               'country': 'MX'}
    try:
        # `filtrar` en False sólo para el admin: quien paga la guía tiene derecho a ver
        # TODAS las tarifas aunque tarden más de lo prometido. Al distribuidor se le
        # enseñan nada más las que de verdad se le podrían ofrecer a un cliente.
        comp = paqueterias.cotizar_en_todos(destino, paquete,
                                            espera_max=skydropx.ESPERA_MAX_GUIA_S,
                                            filtrar=not es_admin)
    except Exception:
        logger.exception('Cotizador: no se pudo cotizar a %s', cp)
        return {'enabled': False, 'cobertura': True, 'pais': pais, 'opciones': [],
                'detail': 'La paquetería no respondió. Vuelve a intentar en un minuto.'}
    if not comp['opciones']:
        motivos = '; '.join(f"{p['nombre']}: {p['detalle']}"
                            for p in comp['proveedores'] if p.get('detalle'))
        return {'enabled': True, 'cobertura': True, 'pais': pais, 'opciones': [],
                'peso_kg': paquete['peso_kg'], 'proveedores': comp['proveedores'],
                'detail': f'Sin tarifas para el CP {cp}. {motivos}'.strip()[:300]}
    # De más barata a más cara, y la primera gana. Se ordena AQUÍ aunque
    # `cotizar_en_todos` ya lo haga: el orden es lo que decide qué se recomienda y qué
    # cuesta, y no puede depender de que otro módulo se acuerde de ordenar.
    ordenadas = sorted(comp['opciones'], key=lambda o: float(o.get('precio') or 0))
    mejor = ordenadas[0]
    costo = float(mejor.get('precio') or 0)
    # ⛔ LA POLÍTICA NO SE TOCA AQUÍ, SE CONSULTA. Es la MISMA función que cobra en el
    # checkout (`envios.cobro_de_envio_al_cliente`), con los mismos tres números de la
    # casa. Si algún día cambia la regla, esta pantalla cambia sola.
    cliente_paga = envios.cobro_de_envio_al_cliente(costo, mercancia, FREE_SHIPPING_FROM,
                                                    tarifa_plana=SHIPPING_FLAT)
    absorbe = envios.envio_que_absorbe_la_casa(costo, cliente_paga)
    resp = {
        'enabled': True,
        'cobertura': True,
        'pais': pais,
        'detail': '',
        'peso_kg': paquete['peso_kg'],
        'piezas': piezas,
        'paquete': paquete,
        'quoted_at': now_iso(),
        'opciones': [{'carrier': o.get('paqueteria', ''), 'service': o.get('servicio', ''),
                      'days': o.get('dias', 0), 'price': o.get('precio', 0),
                      'provider': o.get('proveedor', 'skydropx'),
                      'provider_name': o.get('proveedor_nombre', 'Skydropx'),
                      'recomendada': o is mejor,
                      'para_el_cliente': skydropx.permitida(o.get('paqueteria_id', ''))
                                         and skydropx.dentro_del_plazo(o.get('dias'))}
                     for o in ordenadas],
        'cobro': {
            'mercancia': mercancia,
            'cliente_paga': cliente_paga,
            'gratis': cliente_paga <= 0 and costo > 0,
            'envio_gratis_desde': FREE_SHIPPING_FROM,
            'tarifa_plana': SHIPPING_FLAT,
            'falta_para_gratis': round(max(0.0, FREE_SHIPPING_FROM - mercancia), 2),
            'se_cobra_envio': COBRAR_ENVIO,
            # Renglones que el catálogo del servidor no reconoció. Con uno solo, el
            # importe de compra va corto y lo que dice «paga el cliente» no es de fiar.
            'productos_sin_precio': sin_precio,
        },
        # ⛔ DE AQUÍ PARA ABAJO, SÓLO EL ADMIN. Es lo que le cuesta a la casa.
        'casa': {
            'costo_guia': round(costo, 2),
            'absorbe': absorbe,
            'tope_absorcion': envios.tope_que_absorbe_la_casa(mercancia),
            'fuera_de_tope': envios.absorcion_fuera_de_tope(costo, mercancia, cliente_paga),
            'tope_guia_automatica': envios.TOPE_GUIA_AUTOMATICA_MXN,
            'se_compra_sola': costo <= envios.TOPE_GUIA_AUTOMATICA_MXN,
        },
        'proveedores': comp['proveedores'],
        'ahorro': paqueterias.ahorro(comp),
    }
    await _guardar_en_el_historial(payload, quien, resp, cp, pais)
    return resp


async def _guardar_en_el_historial(payload, quien: dict, resp: dict, cp: str, pais: str):
    """Apunta la consulta para que no haya que repetirla. Falla en silencio a propósito.

    Si la base no acepta el apunte, la cotización YA está hecha y en pantalla: tumbar
    la respuesta por no poder guardar el historial sería cambiar una molestia por un
    error.
    """
    mejor = (resp.get('opciones') or [{}])[0]
    cobro = resp.get('cobro') or {}
    try:
        await db[COLECCION_COTIZADOR].insert_one({
            'id': str(uuid.uuid4()),
            'user_id': quien.get('id', ''),
            'user_role': quien.get('role', ''),
            'postal_code': cp,
            'state': payload.state or '',
            'city': payload.city or '',
            'country': pais,
            'mode': (payload.mode or 'items').strip().lower(),
            'peso_kg': resp.get('peso_kg', 0),
            'piezas': resp.get('piezas', 0),
            'mercancia': cobro.get('mercancia', 0),
            'carrier': mejor.get('carrier', ''),
            'service': mejor.get('service', ''),
            'days': mejor.get('days', 0),
            'price': mejor.get('price', 0),
            'cliente_paga': cobro.get('cliente_paga', 0),
            'opciones_n': len(resp.get('opciones') or []),
            'created_at': now_iso(),
        })
    except Exception:
        logger.exception('Cotizador: no se pudo guardar la consulta de %s', cp)


async def _historial_del_cotizador(quien: dict, todo: bool) -> dict:
    """Las últimas consultas. El admin ve todas; el distribuidor SÓLO las suyas.

    El filtro por `user_id` no es cortesía: el historial de otro distribuidor diría a
    dónde y cuánto vende, que no es asunto suyo.
    """
    filtro = {} if todo else {'user_id': quien.get('id', '')}
    docs = await db[COLECCION_COTIZADOR].find(filtro, {'_id': 0}) \
        .sort('created_at', -1).to_list(HISTORIAL_COTIZADOR)
    return {'historial': docs}


@api_router.post('/admin/shipping/cotizador')
async def cotizador_envios_admin(payload: CotizadorEnvioRequest,
                                 admin=Depends(get_current_admin)):
    """«¿Cuánto cuesta mandar esto a tal CP?», con los números de la casa a la vista."""
    return await _cotizar_para_el_cotizador(payload, admin, es_admin=True)


@api_router.get('/admin/shipping/cotizador/historial')
async def cotizador_envios_admin_historial(admin=Depends(get_current_admin)):
    return await _historial_del_cotizador(admin, todo=True)


@api_router.post('/distributor/shipping/cotizador')
async def cotizador_envios_distribuidor(payload: CotizadorEnvioRequest,
                                        dist=Depends(get_current_distributor)):
    """El mismo cotizador para el distribuidor, recortado a lo que puede ver.

    ⛔ EL RECORTE LO HACE EL SERVIDOR, no la pantalla. Ocultar un dato con CSS es
    dejarlo servido en la consola del navegador: el costo que la casa absorbe nunca
    sale de esta ruta (ver `_solo_lo_del_distribuidor` y test_cotizador_envios.py).
    """
    completa = await _cotizar_para_el_cotizador(payload, dist, es_admin=False)
    return _solo_lo_del_distribuidor(completa)


@api_router.get('/distributor/shipping/cotizador/historial')
async def cotizador_envios_distribuidor_historial(dist=Depends(get_current_distributor)):
    return await _historial_del_cotizador(dist, todo=False)


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


# ==========================================================================
#  UN SOLO CORREO CUANDO SE PUEDA — el candado vive aquí
# ==========================================================================
# ⛔ EL PROBLEMA. Una compra con tarjeta mandaba TRES correos casi seguidos:
# «recibimos tu pedido», «confirmamos tu pago» y «va en camino». Orden de Christián
# (2026-07-31): «nadie debe recibir tres correos por una compra. Consolida.»
#
# LA REGLA, en una línea: **un correo por EVENTO REAL, y jamás dos por el mismo
# evento.** Los eventos son tres y no más:
#
#   1. hay que pagar    → sólo existe cuando el pago NO es inmediato (SPEI, OXXO)
#   2. entró el dinero  → siempre
#   3. salió un paquete → sólo si la guía apareció DESPUÉS del correo del punto 2
#
# Cuando dos eventos caen juntos en el tiempo, caen juntos en el mismo correo. Por eso
# el pago se confirma DESPUÉS de intentar comprar la guía y no antes: así el correo de
# «pago confirmado» ya puede llevar el número de guía adentro y el tercero desaparece.
#
# Lo que le llega al cliente, en la práctica:
#   tarjeta/cripto + guía comprada   → 1 correo
#   SPEI/OXXO + guía comprada        → 2 correos
#   pago inmediato sin guía todavía  → 2 correos
#
# ⛔ EL CANDADO ES ATÓMICO, como el del cupón y el de los puntos. Un `$addToSet`
# condicionado en un solo paso: gana el primero que llegue y el segundo se va en
# silencio. Sin esto, dos webhooks de la pasarela llegando juntos —que pasa— mandan el
# mismo correo dos veces, que es exactamente el ruido que esto viene a quitar.
async def _apartar_correo(order_id: str, ranura: str) -> bool:
    """Aparta la ranura de correo. True si le tocó mandarlo, False si ya se mandó.

    `ranura` es el evento, no el correo: 'nuevo', 'pagado', y 'enviado:<guía>' para el
    rastreo. La guía va en el nombre a propósito — en un envío partido hay DOS guías y
    cada una es un aviso legítimo; con una ranura pelada, el segundo paquete saldría
    mudo.
    """
    if not order_id or not ranura:
        return False
    res = await db.orders.update_one(
        {'id': order_id, 'emails_sent': {'$ne': ranura}},
        {'$addToSet': {'emails_sent': ranura}})
    return res.modified_count > 0


async def _idioma_del_pedido(order: dict) -> str | None:
    """En qué idioma le escribimos a quien compró. None = español, el de la casa."""
    if not (order or {}).get('user_id'):
        return None
    u = await db.users.find_one({'id': order['user_id']}, {'_id': 0, 'language': 1})
    return (u or {}).get('language')


async def avisar_al_cliente(order: dict, evento: str) -> bool:
    """LA ÚNICA PUERTA por la que le sale un correo de compra a un cliente.

    Existe para que la regla de «nunca tres correos» se pueda cumplir en UN lugar en
    vez de en los cinco sitios que mandaban correos por su cuenta. Devuelve True si de
    verdad salió uno.

    ⛔ 'pagado' APARTA TAMBIÉN LA RANURA DEL RASTREO que va dentro. Sin eso, comprar la
    guía y confirmar el pago casi al mismo tiempo mandaría el correo de pago CON la
    guía y, un segundo después, el de «va en camino» con la MISMA guía. Apartar las dos
    ranuras de un tirón es lo que hace imposible ese duplicado.
    """
    if not order or not order.get('id'):
        return False
    numero = order.get('order_number', '')
    rastreo = str(order.get('tracking_number') or '').strip()
    if evento == 'enviado' and not rastreo:
        return False                        # sin guía no hay nada que avisar
    ranura = f'enviado:{rastreo}' if evento == 'enviado' else evento
    if not await _apartar_correo(order['id'], ranura):
        logger.info('Correo %s del pedido %s: ya se había mandado, no se repite',
                    evento, numero)
        return False
    lang = await _idioma_del_pedido(order)
    if evento == 'pagado':
        # El rastreo viaja DENTRO de este correo: se aparta su ranura para que el
        # camino del envío no lo vuelva a mandar por separado.
        if rastreo:
            await _apartar_correo(order['id'], f'enviado:{rastreo}')
        asyncio.create_task(send_order_email(order, lang, 'pagado'))
    elif evento == 'enviado':
        asyncio.create_task(send_shipped_email(order, lang))
    else:
        asyncio.create_task(send_order_email(order, lang, 'nuevo'))
    return True


async def avisar_del_envio(order: dict) -> bool:
    """Le manda al cliente su número de guía. Nunca revienta hacia arriba.

    Hasta hoy el rastreo se guardaba en el pedido y ahí se quedaba: el cliente tenía
    que entrar a su cuenta a buscarlo. El correo de confirmación le PROMETE que se
    lo vamos a mandar ("en cuanto salga te mandamos el número de guía"), así que no
    mandarlo era incumplir por escrito.
    """
    if not order or not order.get('tracking_number'):
        return False
    num = order.get('order_number')
    # La campanita lleva la guía en el `dedup` por lo mismo que la ranura del correo:
    # un envío partido tiene DOS guías y las dos merecen su aviso.
    await notify(order.get('user_id'), 'order_shipped', 'Tu pedido va en camino',
                 f'El pedido {num} ya salió. Guía {order.get("tracking_number")}.',
                 link=f'/pedido/{num}', dedup=f'shipped:{num}:{order["tracking_number"]}')
    # ⛔ POR LA PUERTA ÚNICA. Si este rastreo ya viajó dentro del correo de «pago
    # confirmado» —que es el caso normal desde hoy— aquí no sale nada. Ése es
    # exactamente el tercer correo que Christián mandó quitar.
    await avisar_al_cliente(order, 'enviado')
    return True


# ==========================================================================
#  LA GUÍA SE COMPRA SOLA — con dos frenos y un candado
# ==========================================================================
# ⛔ ESTO GASTA DINERO DE VERDAD SIN QUE NADIE APRIETE UN BOTÓN. Por eso lleva:
#
#   FRENO 1 — EL EMPAQUE (`envios.empaque_para`). Christián tiene UN empaque: la bolsa
#     stand-up de 12×15×1 cm, donde caben ~4 piezas. Antes TODO se cotizaba como si
#     cupiera en la caja de 1 kg, y lo que no cabía volvía como RECOBRO por sobrepeso
#     semanas después. Ahora: 1-4 piezas compra sola; 5 o más se detiene y se le
#     pregunta a él qué empaque va a usar. El día que compre cajas las captura en el
#     Panel y ese rango empieza a comprar solo, sin desplegar nada.
#
#   FRENO 2 — EL TOPE DE GASTO ($400, `envios.TOPE_GUIA_AUTOMATICA_MXN`). Arriba de
#     eso no compra: pide el visto bueno. Se revisa ENTRE cotizar y comprar, que es el
#     único momento en que un tope sirve para algo.
#
#   CANDADO — atómico, como el del cupón. `label_lock` se toma en un solo paso
#     condicionado: dos webhooks de la pasarela llegando juntos —que pasa— comprarían
#     DOS guías del mismo pedido y las dos se pagan. Mirar `tracking_number` en un dict
#     ya leído no alcanza: entre la lectura y la compra cabe el otro webhook completo.
#
# NINGUNO DE LOS TRES FRENOS DEJA AL CLIENTE SIN NOTICIAS: los tres devuelven None y
# quien llama manda igual el correo de pago confirmado diciendo que el rastreo va en
# camino. Un freno nunca puede convertirse en silencio.
async def _liberar_candado_guia(order_id: str, extra: dict | None = None):
    """Suelta el candado de compra y, de paso, escribe por qué no se compró."""
    await db.orders.update_one({'id': order_id},
                               {'$set': {'label_lock': False, **(extra or {})}})


async def _avisar_a_christian(asunto: str, cuerpo: str, *, titulo: str,
                              orden: dict, urgente: bool = False):
    """Correo + campanita a TODOS los admin. Nunca revienta hacia arriba.

    Van las dos cosas porque las dos fallan distinto: el correo se pierde entre otros
    correos y la campanita sólo se ve si entra al Panel. Un pedido pagado que no puede
    salir tiene que encontrarlo a él, no esperar a que él lo encuentre.
    """
    numero = (orden or {}).get('order_number', '')
    marca = '🚨 URGENTE — ' if urgente else '⚠️ '
    try:
        await send_admin_notification(
            f'{marca}{asunto} (pedido {numero})',
            f'<p style="font-family:Helvetica,Arial,sans-serif;font-size:15px;'
            f'line-height:1.6;">{cuerpo}</p>'
            f'<p style="font-family:Helvetica,Arial,sans-serif;font-size:15px;">'
            f'<a href="{SITE_URL}/admin?tab=orders">Abrir el pedido en el Panel</a></p>')
    except Exception:
        logger.exception('No se pudo avisar por correo de %s', numero)
    try:
        admins = await db.users.find({'role': 'admin'}, {'_id': 0, 'id': 1}).to_list(20)
        for a in admins:
            await notify(a['id'], 'guia_pendiente', titulo,
                         f'Pedido {numero}: {asunto}.', link='/admin?tab=orders',
                         dedup=f'guia:{numero}:{asunto[:40]}',
                         meta={'order_number': numero})
    except Exception:
        logger.exception('No se pudo poner la campanita de %s', numero)


async def comprar_guia_del_pedido(order: dict, avisar: bool = True) -> dict | None:
    """Compra la guía de un pedido YA PAGADO y la deja en el pedido. Idempotente.

    La llaman los cuatro caminos del dinero: tarjeta y OXXO (Mercado Pago), cripto
    (NOWPayments/BTCPay) y SPEI (cuando el admin confirma el depósito). Todos pasan
    por aquí porque todos terminan en el mismo lugar: el pedido en 'confirmado'.

    `avisar=False` lo usa el camino del pago: ahí la guía va DENTRO del correo de «pago
    confirmado», así que avisar por separado sería el tercer correo que se quitó.

    Nunca revienta hacia arriba: un pedido pagado no se puede quedar a medias
    porque la paquetería tenga un mal día. Si falla, lo deja escrito en el pedido
    (`label_error`), le avisa a Christián y el admin compra la guía a mano como hoy.
    """
    if not envios.COMPRAR_GUIA_AL_PAGAR:
        return None
    if not order or not order.get('id') or order.get('tracking_number'):
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

    # ⛔ ENVÍO PARTIDO: LO QUE ELIGIÓ EL CLIENTE MANDA. Si pidió que todo llegue junto y
    # todavía falta mercancía, aquí no se compra nada: comprar ahora sería mandarle la
    # mitad justo después de que él dijo que prefería esperar.
    if (order.get('shipping_preference') == 'completo'
            and order.get('backorder_items')):
        logger.info('Envio: %s espera a estar completo por decision del cliente',
                    order.get('order_number'))
        await db.orders.update_one(
            {'id': order['id']},
            {'$set': {'label_hold': 'espera_pedido_completo', 'label_error': ''}})
        return None

    # ⛔ FRENO 1: ¿en qué lo meto? Sin empaque que lo reciba no se compra: la guía
    # saldría cotizada contra una caja que no existe y el sobrepeso vuelve como recobro.
    piezas = envios.piezas_del_pedido(order.get('items') or [])
    empaque = envios.empaque_para(piezas)
    if empaque is None:
        cabe = max((int(e.get('hasta_piezas') or 0) for e in envios.empaques()), default=0)
        logger.warning('Envio: %s lleva %s piezas y el empaque mas grande aguanta %s. '
                       'NO se compra sola; se le pregunta a Christian.',
                       order.get('order_number'), piezas, cabe)
        await db.orders.update_one(
            {'id': order['id']},
            {'$set': {'label_hold': 'sin_empaque', 'label_piezas': piezas,
                      'label_error': ''}})
        if avisar:
            await _avisar_a_christian(
                'este pedido no cabe en la bolsa: dime qué empaque uso',
                f'El pedido lleva <strong>{piezas} piezas</strong> y hoy el único '
                f'empaque registrado (la bolsa stand-up de 12×15×1 cm) aguanta '
                f'{cabe}. <strong>No compré la guía</strong> para no cotizarla con '
                f'medidas que no son y que la paquetería nos cobre el sobrepeso '
                f'después.<br><br>Dos formas de resolverlo: cotiza y compra la guía '
                f'a mano desde el Panel eligiendo el empaque, o captura las medidas '
                f'de la caja en Admin → Envíos y a partir de ahí este tamaño de '
                f'pedido también compra solo.<br><br>El cliente ya recibió su '
                f'confirmación de pago; sabe que el rastreo le llega en cuanto salga.',
                titulo='Falta empaque para un pedido pagado', orden=order)
        return None

    # ⛔ EL CANDADO, justo antes de gastar. Gana el primero que llegue; el segundo se va
    # sin comprar nada. Se pide que el pedido siga SIN guía en la misma condición: si
    # otro camino ya la compró, esto no se ejecuta aunque el candado estuviera libre.
    tomado = await db.orders.update_one(
        {'id': order['id'], 'label_lock': {'$ne': True},
         'tracking_number': {'$in': [None, '']}},
        {'$set': {'label_lock': True}})
    if tomado.modified_count == 0:
        logger.info('Envio: la guia de %s ya la esta comprando otro camino',
                    order.get('order_number'))
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
    # ⛔ EL BULTO SALE DEL EMPAQUE DE VERDAD (lo que Christián tiene en la bodega), no
    # del peso del catálogo. Y sigue así aunque el peso por pieza ya sea defendible:
    # el peso dice cuánto pesa la mercancía, no en qué la va a meter. Es la diferencia
    # entre cotizar lo que se manda y cotizar una suposición — y la suposición es la
    # que produce el recobro.
    paquete = envios.paquete_de_empaque(empaque)
    try:
        # Doble cotizador: pregunta en Skydropx y en enviosinternacionales.com y compra
        # la más barata de las permitidas. Con uno solo encendido se comporta como antes.
        # ⛔ FRENO 2 va aquí dentro, entre cotizar y pagar.
        # El tope de gasto depende del TIPO de envío: express paga guías más caras
        # ($600) porque el cliente ya pagó su extra; estándar se queda en $400. Y un
        # pedido EXPRESS sólo compra servicios de 1-2 días — si ninguno cabe en el
        # tope, no se degrada a uno lento en silencio: le pregunta a Christián.
        guia = paqueterias.guia_para(
            destino, paquete, quote.get('service_code', ''),
            tope_mxn=envios.tope_guia_automatica(order.get('shipping_express')),
            dias_max=envios.DIAS_MAXIMOS_EXPRESS if order.get('shipping_express') else None)
    except paqueterias.TopeDeGastoExcedido as tope:
        logger.warning('Envio: la guia de %s cuesta $%s y el tope automatico es $%s. '
                       'NO se compra; se le pregunta a Christian.',
                       order.get('order_number'), tope.precio, tope.tope)
        await _liberar_candado_guia(order['id'], {
            'label_hold': 'sobre_tope', 'label_error': '',
            'label_precio_cotizado': tope.precio})
        if avisar:
            await _avisar_a_christian(
                f'la guía cuesta ${tope.precio:,.0f} y el tope automático es '
                f'${tope.tope:,.0f}',
                f'La más barata que encontré es <strong>{tope.paqueteria} '
                f'{tope.servicio}</strong> a <strong>${tope.precio:,.2f}</strong>, '
                f'arriba del tope de ${tope.tope:,.2f} que puede gastar el servidor '
                f'solo. <strong>No compré nada.</strong><br><br>Si te parece bien el '
                f'precio, cotiza y compra desde el Panel con un clic. Si no, ahí mismo '
                f'puedes ver todas las tarifas y elegir otra.<br><br>El cliente ya '
                f'recibió su confirmación de pago; sabe que el rastreo le llega en '
                f'cuanto salga.',
                titulo='Una guía necesita tu visto bueno', orden=order)
        return None
    except Exception as e:
        # Sin saldo, API caída, dirección rechazada: es un FALLO, no un freno. Aquí sí
        # se reintenta solo (ver `_reintentar_guias_pendientes`) y el aviso es urgente.
        logger.exception('Envio: no se pudo comprar la guia de %s', order.get('order_number'))
        intentos = int(order.get('label_intentos') or 0) + 1
        await _liberar_candado_guia(order['id'], {
            'label_error': str(e)[:300], 'label_hold': '',
            'label_intentos': intentos, 'label_ultimo_intento': now_iso()})
        if avisar:
            await _avisar_a_christian(
                'no se pudo comprar la guía',
                f'El pedido está <strong>pagado</strong> y la compra de la guía falló: '
                f'<code>{html_lib.escape(str(e)[:300])}</code><br><br>Las causas de '
                f'siempre son tres: <strong>no hay saldo</strong> en la cuenta de la '
                f'paquetería (Admin → Envíos → Saldo), la API está caída, o la '
                f'dirección del cliente no la acepta la paquetería.<br><br>'
                f'<strong>Entra al Panel y cómprala a mano.</strong> Va por el intento '
                f'#{intentos}; el servidor sigue reintentando solo cada 10 minutos.'
                f'<br><br>El cliente ya recibió su confirmación de pago; sabe que el '
                f'rastreo le llega en cuanto salga. No se le prometió ningún número '
                f'que no exista.',
                titulo='No se pudo comprar una guía', orden=order, urgente=True)
        return None
    numero = guia.get('tracking_number') or ''
    update = {
        'carrier': guia.get('carrier') or 'Estafeta',
        'tracking_number': numero,
        'tracking_url': guia.get('tracking_url') or build_tracking_url(guia.get('carrier', ''), numero),
        'label_url': guia.get('label_url') or '',
        'label_provider': guia.get('proveedor') or 'skydropx',
        'label_error': '',
        'label_hold': '',
        'label_lock': False,
        'label_empaque': empaque.get('nombre', ''),
        'shipping_cost': guia.get('costo') or quote.get('cost') or 0,
        'shipped_at': order.get('shipped_at') or now_iso(),
        'status': 'enviado',
    }
    await db.orders.update_one({'id': order['id']}, {'$set': update})
    logger.info('Envio: guia comprada para %s — %s %s en %s por $%s (via %s)',
                order.get('order_number'), update['carrier'], numero,
                update['label_empaque'], update['shipping_cost'],
                update['label_provider'])
    # El cliente se entera por correo, no entrando a buscar. Se le manda el pedido ya
    # actualizado: con el de antes iría sin número de guía, que es justo lo que avisa.
    # ⛔ Salvo cuando quien llama va a mandar el correo de «pago confirmado» con la guía
    # adentro: ahí avisar aquí sería el tercer correo.
    if avisar:
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


async def _obsequios_del_pedido(payload):
    """Las CORTESÍAS que trae el carrito compartido de este pedido, si trae alguna.

    Devuelve `(documento, renglones_de_cortesía, envío_de_cortesía)`.

    ⛔ De la petición sólo se lee el TOKEN. Qué se regala, cuánto vale y si cabe se
    resuelve aquí contra el catálogo real y, más abajo, contra el ROI de ESTE pedido
    (no el del carrito que se compartió: el cliente pudo haberle quitado renglones
    entre que lo abrió y que pagó, y ahí el mismo regalo ya no cabe).

    Y de paso pega la ATRIBUCIÓN: si el cliente no escribió ningún código, la venta
    se le acredita a quien le mandó el enlace. Si escribió uno, manda el suyo — un
    enlace no le quita a nadie el código que el cliente tecleó a propósito.
    """
    token = (getattr(payload, 'shared_cart_token', '') or '').strip()
    if not token:
        return None, [], False
    doc = await db[COLECCION_CARRITOS].find_one({'token': token}, {'_id': 0})
    if not doc or doc.get('deleted_at'):
        logger.info('Carrito compartido no encontrado o borrado (%s): el pedido sigue sin cortesías.', token[:8])
        return None, [], False
    if doc.get('expires_at') and doc['expires_at'] < now_iso():
        logger.info('Carrito compartido vencido (%s): el pedido sigue sin cortesías.', token[:8])
        return None, [], False
    if not (payload.distributor_code or '').strip() and doc.get('ref'):
        payload.distributor_code = doc['ref']

    obsequios = doc.get('gifts') or []
    envio_gratis = any(g.get('tipo') == regalos.TIPO_ENVIO for g in obsequios)
    productos = [g for g in obsequios if g.get('tipo') == regalos.TIPO_PRODUCTO and g.get('product_id')]
    if not productos:
        return doc, [], envio_gratis
    catalogo = await _catalogo_de([_Renglon(g['product_id']) for g in productos])
    renglones = []
    for g in productos:
        d = catalogo.get(g['product_id'])
        if not d or d.get('hidden') or not d.get('price'):
            continue
        renglones.append(OrderItem(
            product_id=g['product_id'], name=_nombre_cotizable(d),
            price=0.0, quantity=int(g.get('cantidad') or 1),
            presentation=d.get('presentation') or ''))
    return doc, renglones, envio_gratis


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
    # ⛔ EL CARRITO COMPARTIDO (Christián, 2026-08-01). Si el cliente llegó por el
    # enlace que le mandó su distribuidora, el token trae consigo DOS cosas que el
    # navegador no puede fabricar: la atribución de la venta y las cortesías.
    #
    # Del token no se cree ni un peso: se abre el documento guardado y se vuelven a
    # resolver los obsequios contra el catálogo de HOY. Un token inventado no
    # encuentra nada y la compra sigue como cualquier otra.
    _carrito_doc, _regalo_items, _envio_de_cortesia = await _obsequios_del_pedido(payload)
    if _regalo_items:
        payload.items.extend(_regalo_items)

    _pflags = await _catalogo_de(payload.items)
    # Los renglones de CORTESÍA nacen sin precio: se los pone el catálogo AQUÍ, antes
    # de la comparación de abajo, para no anotar en la bitácora una discrepancia que
    # no existe (esos renglones el navegador jamás los mandó).
    for _r in _regalo_items:
        _r.price = float((_pflags.get(_r.product_id) or {}).get('price') or 0)

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

    # ⛔ LOS RENGLONES DE CORTESÍA NO ENTRAN A LA ARITMÉTICA DEL DESCUENTO.
    # Se identifican por OBJETO, no por `product_id`: si el cliente compró agua y
    # además se le obsequió agua, son dos renglones del mismo producto y sólo uno es
    # el regalo — comparando ids se le quitaría el descuento al que sí se está pagando.
    _cortesias = {id(r) for r in _regalo_items}

    def _es_cortesia(item):
        return id(item) in _cortesias

    def _eligible(item):
        if _es_cortesia(item):
            # Un regalo ya está al 100% de descuento: no se le descuenta encima ni
            # paga comisión. Su costo se mide aparte, contra el piso de rentabilidad.
            return False
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
    # La REGLA DE 5 cuenta piezas COMPRADAS. Una cortesía no acerca a nadie al precio
    # de distribuidor: si contara, regalarse dos viales a uno mismo sería la forma de
    # bajarle el precio a los otros tres.
    pedido_por_producto = _agrupar_por_producto(
        [it for it in payload.items if not _es_cortesia(it)], _pflags)

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
        # EL CARRITO COMPARTIDO COBRA LO QUE SE COTIZÓ (Christián, 2026-08-01). El
        # descuento que la distribuidora pidió al armar el enlace vive en el
        # documento del carrito, no en el código: hasta hoy la caja cobraba el
        # descuento del código `ref` y un carrito cotizado al 20% podía cobrarse
        # al 10% sin que nadie lo viera. Acotado a su comisión, como todo código.
        # Sólo aplica si la atribución sigue siendo la del enlace: un código que
        # el cliente tecleó a propósito manda él, no el carrito.
        _es_del_carrito = bool(
            _carrito_doc and (payload.distributor_code or '').strip() == (_carrito_doc.get('ref') or ''))
        if _es_del_carrito:
            _pedida = max(0.0, float(_carrito_doc.get('discount_asked') or 0))
            # ⛔ SIN DESCUENTO PROPIO, MANDA LA PROMO DE LA CASA (Christián,
            # 2026-08-01): un carrito compartido al 0% le enseña al cliente la
            # promo automática (ver `_armar_cotizacion`), y la caja cobra ESO —
            # ni el 0% que dejaría al cliente peor que un anónimo, ni el
            # descuento del código, que aquí nadie ofreció.
            discount_rate = (min(pyramid.effective_rate(referrer), _pedida)
                             if _pedida > 0 else promo_automatica(discountable))
        else:
            # Código suelto (tecleado o de un enlace `?ref=`): su descuento, y la
            # promo automática como PISO — el mayor gana, que es lo que el
            # carrito de la página promete desde siempre. Un código sin
            # descuento ya no puede dejar al cliente sin los automáticos.
            discount_rate = max(code_discount, promo_automatica(discountable))
    else:
        discount_rate = promo_automatica(discountable)
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

    # ⛔ LAS CORTESÍAS, MEDIDAS CONTRA EL ROI DE **ESTE** PEDIDO (Christián, 2026-08-01).
    #
    # Regalar es descontar: el vial de cortesía sale del MISMO margen que el descuento,
    # así que se suman los dos y se miden contra el mismo tope que ya protege a la casa
    # (`commission_cap` por producto, y el techo del 40% encima).
    #
    # Se revisa AQUÍ y no sólo al compartir el enlace porque el cliente pudo quitarle
    # renglones al carrito entre que lo abrió y que pagó: el mismo regalo que cabía en
    # un pedido de $12,000 no cabe en uno de $900. Si no cabe, se cae EL REGALO — nunca
    # la venta (regla de la casa: vender siempre) — y queda constancia en la bitácora.
    gift_discount = round(sum(r.price * r.quantity for r in _regalo_items))
    _valor_cortesias = gift_discount + (COSTO_GUIA_ESTIMADO if _envio_de_cortesia else 0)
    if _valor_cortesias > 0:
        _permitido = regalos.piso_de_rentabilidad(
            [it for it in payload.items if not _es_cortesia(it)],
            lambda it: _cap_of(it) if _eligible(it) else 0.0,
            techo=techo_de_descuento(user))
        _veredicto = regalos.cabe_el_obsequio(discount, _valor_cortesias, _permitido)
        if not _veredicto['cabe']:
            logger.warning(
                'CORTESÍA RECHAZADA AL COBRAR (carrito %s): descuento $%.0f + regalo $%.0f '
                'sobre un permitido de $%.0f; se pasa por $%.0f. La venta sigue, el regalo no.',
                (getattr(payload, 'shared_cart_token', '') or '')[:8], discount,
                _valor_cortesias, _veredicto['permitido'], _veredicto['exceso'])
            payload.items = [it for it in payload.items if not _es_cortesia(it)]
            _regalo_items, _envio_de_cortesia, gift_discount = [], False, 0
            _cortesias = set()
            # El subtotal se rehace sin los renglones de cortesía. El descuento NO hace
            # falta recalcularlo: las cortesías nunca recibieron un peso de descuento
            # (`_eligible` las excluye), así que `discount` ya vale lo mismo sin ellas.
            subtotal = sum(it.price * it.quantity for it in payload.items)

    # El regalo se descuenta ENTERO: el cliente no paga un peso por lo que se le
    # obsequió, pero el renglón sí existe en el pedido para que salga en la caja y
    # baje del inventario como cualquier otra pieza que se manda.
    after_discount = subtotal - discount - gift_discount
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
    # ENVÍO DE CORTESÍA: la distribuidora lo obsequió en el carrito que compartió. El
    # cliente no paga guía; la casa sí la sigue pagando, y eso se guarda igual en
    # `shipping_absorbed` para que el regalo se pueda sumar después. Ya pasó el piso
    # de rentabilidad de arriba: aquí sólo se aplica lo que allá se autorizó.
    if _envio_de_cortesia:
        # La cortesía regala el envío ESTÁNDAR; el regalo no se agranda solo. Si el
        # cliente además quiere express: abajo de la mínima paga el extra ($150);
        # desde la mínima aplica la regla v2 igual que a cualquiera — el cobro ya
        # calculado ES sólo el excedente sobre el presupuesto, así que se queda.
        if not getattr(payload, 'shipping_express', False):
            shipping = 0
        elif paid_merchandise < FREE_SHIPPING_FROM:
            shipping = round(envios.EXTRA_EXPRESS_MXN)
    # EL 5% POR PAGAR EN CRIPTO (Christián, 2026-08-03). Sobre la mercancía YA
    # descontada y NUNCA sobre el envío: la guía se le paga completa a la paquetería.
    # No cuenta contra el techo del 40% porque no sale del margen del producto, sino
    # de la comisión de Mercado Pago que este pedido no va a pagar. Ver
    # descuento_cripto.py.
    crypto_discount = descuento_cripto.descuento(paid_merchandise, payload.payment_method)
    paid_merchandise = max(0, paid_merchandise - crypto_discount)
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
        # Se guarda APARTE del descuento comercial a propósito: son dos dineros con
        # origen distinto (aquél sale del margen, éste del ahorro de pasarela) y
        # mezclarlos haría imposible saber cuánto costó de verdad la promoción.
        crypto_discount=crypto_discount,
        discount_rate=discount_rate,
        discount_capped=discount_capped,
        discount_lines=discount_lines,
        regla_de_5=regla_de_5,
        # Las cortesías: lo que se regaló y cuánto valía. El código del obsequio NO
        # entra aquí — el pedido lo lee el cliente. Ver el comentario en `models.py`.
        gift_discount=gift_discount,
        gift_lines=[{'product_id': r.product_id, 'name': r.name,
                     'quantity': int(r.quantity), 'list_price': float(r.price)}
                    for r in _regalo_items],
        gift_shipping=bool(_envio_de_cortesia),
        shared_cart_token=(getattr(payload, 'shared_cart_token', '') or '')[:64],
        shipping=shipping,
        shipping_express=bool(getattr(payload, 'shipping_express', False)),
        shipping_quote=shipping_quote,
        shipping_cost=costo_guia,
        # Lo que la casa se comió del envío. Sin este número nadie sabe cuánto
        # cuesta de verdad la promesa de "envío gratis".
        shipping_absorbed=envio_absorbido,
        # Lo que se pasó del tope del 5% en ESTE pedido. Cero cuando se respeta.
        shipping_over_cap=fuera_de_tope,
        total=total,
        referred_by=referrer['id'] if referrer else None,
        # ⛔ EL TEXTO DEL CUPÓN SE ESCRIBE EN EL PEDIDO (2026-07-31). Antes el vínculo
        # sólo existía al revés (`discount_codes.used_order`), que se llena al QUEMAR
        # el cupón — y un cupón de campaña multiuso no se quema nunca. Sin esta línea,
        # un código de WhatsApp repartido en cien conversaciones podía vender y aun así
        # aparecer en el panel como «mandado y jamás usado».
        coupon_code=(coupon.get('code') or '') if coupon else '',
        commission=commission,
        commissions=commissions,
        points_used=points_used,
        points_earned=points_earned,
        attribution=(payload.attribution.model_dump() if payload.attribution else {}),
        first_order=await _es_primera_compra(payload.customer.email),
        # Se guarda lo que dice el navegador y NO se inventa una fecha cuando viene
        # vacía: una constancia fabricada por el servidor no prueba nada.
        terms_accepted_at=(payload.terms_accepted_at or '').strip()[:40],
        # ⛔ SE NORMALIZA EN EL SERVIDOR. Lo que llegue que no sea exactamente
        # 'completo' vale 'partido', que es lo que se hacía hasta hoy y lo que nunca
        # deja mercancía pagada detenida. Un valor inventado en el navegador no puede
        # convertirse en un pedido que se queda esperando para siempre.
        shipping_preference=('completo'
                             if (payload.shipping_preference or '').strip() == 'completo'
                             else 'partido'),
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
    # ⛔ EL CORREO DE «RECIBIMOS TU PEDIDO» SÓLO SALE CUANDO EL PAGO NO ES INMEDIATO.
    #
    # Con SPEI y OXXO el cliente TIENE que hacer algo todavía —transferir, o ir a la
    # tienda— y necesita los datos por escrito: ése correo se gana su lugar. Christián
    # lo pidió explícitamente para SPEI: «las dos cosas», en pantalla Y por correo.
    #
    # Con tarjeta y cripto no: el pago se confirma en segundos y este correo llegaría
    # pegado al de «pago confirmado». Ahí está el tercer correo que sobraba. Se calla
    # aquí y todo el detalle del pedido viaja dentro del de pago confirmado.
    #
    # ⚠️ La única forma de que un pedido con tarjeta se quede sin ningún correo es que
    # el cliente nunca pague — y para eso está la recuperación de carritos, no un
    # correo de confirmación de algo que no se cobró.
    if payload.payment_method in PAGOS_DIFERIDOS:
        if await _apartar_correo(order.id, 'nuevo'):
            asyncio.create_task(send_order_email(
                email_order, user.get('language') if user else None, 'nuevo'))
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
            # ⛔ LA URL DE PAGO SE GUARDA, NO SÓLO SE DEVUELVE. Con OXXO esa URL ES la
            # ficha con el código de barras: hasta hoy viajaba una sola vez en la
            # respuesta del checkout y si el cliente cerraba la pestaña, se perdía y no
            # había forma de recuperarla desde el sitio. Christián pidió justo lo
            # contrario: «que pueda volver a verlos las veces que haga falta».
            await db.orders.update_one(
                {'id': order.id},
                {'$set': {'card_preference_id': pref['preference_id'],
                          'card_provider': 'mercadopago',
                          'card_checkout_url': pref['checkout_url']}})
            result['card_checkout_url'] = pref['checkout_url']
        except Exception:
            logger.exception('MercadoPago preference failed for %s', order.order_number)
            # Sin liga de pago el cliente se queda sin poder pagar Y sin correo: por eso
            # aquí sí sale el de «recibimos tu pedido», para que tenga algo por escrito
            # y a quién contestarle.
            if await _apartar_correo(order.id, 'nuevo'):
                asyncio.create_task(send_order_email(
                    email_order, user.get('language') if user else None, 'nuevo'))
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
            # Igual que con la tarjeta: sin factura no puede pagar, así que al menos
            # que le quede el pedido por escrito.
            if await _apartar_correo(order.id, 'nuevo'):
                asyncio.create_task(send_order_email(
                    email_order, user.get('language') if user else None, 'nuevo'))
    return result


async def _confirmar_y_avisar(order: dict):
    """Compra la guía y DESPUÉS manda UN correo con el pago y el rastreo juntos.

    ⛔ ES EL CORAZÓN DE «UN SOLO CORREO». El orden importa y no es negociable:

      1. se intenta comprar la guía (sin avisar por su cuenta),
      2. se relee el pedido —ya con guía o sin ella—,
      3. sale UN correo: «pago confirmado» y, si la guía existe, con su número dentro.

    Si la guía no se pudo comprar (freno de empaque, tope de $400, o un fallo de la
    paquetería) el correo sale igual diciendo que el rastreo llega en cuanto salga. ⛔
    NUNCA se le manda al cliente un número de guía que no existe, y nunca se le deja
    sin saber que su dinero llegó: las dos cosas al mismo tiempo.

    Nunca revienta: se llama en segundo plano y el pago ya quedó confirmado antes.
    """
    try:
        await comprar_guia_del_pedido(order, avisar=False)
    except Exception:
        logger.exception('Envio: fallo la compra automatica de la guia de %s',
                         order.get('order_number'))
    fresco = await db.orders.find_one({'id': order['id']}, {'_id': 0}) or order
    await avisar_al_cliente(fresco, 'pagado')
    if fresco.get('tracking_number'):
        # La campanita del envío sí va aparte: es otra pantalla, no otro correo.
        num = fresco.get('order_number')
        await notify(fresco.get('user_id'), 'order_shipped', 'Tu pedido va en camino',
                     f'El pedido {num} ya salió. Guía {fresco["tracking_number"]}.',
                     link=f'/pedido/{num}',
                     dedup=f'shipped:{num}:{fresco["tracking_number"]}')


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
        # Segundo aviso a Christián: el primero dice qué se va a necesitar, éste dice que
        # ya se puede mandar. Con uno solo, o se prepara mercancía que nadie pagó o se
        # entera tarde de que ya puede salir.
        asyncio.create_task(_avisar_de_la_compra(fresh, 'pagado'))
        # ⛔ PRIMERO LA GUÍA, DESPUÉS EL CORREO. Ése es todo el truco de «un solo
        # correo»: si la guía se compra antes, el correo de pago confirmado ya la
        # lleva adentro y el tercer correo no existe. El webhook de la pasarela no se
        # queda esperando —esto corre en segundo plano— pero el correo sí espera a la
        # guía, que es lo que hay que esperar.
        asyncio.create_task(_confirmar_y_avisar(fresh))
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
            # LA ESTRATEGIA DEL 2026-08-02: el piso de absorción (la casa come
            # hasta $250 o el 5%, lo mayor) y el extra del express. La pantalla
            # REPITE la cuenta con estos números; el cobro lo hace el servidor.
            'shipping_absorb_floor': envios.PISO_ABSORCION_MXN,
            'shipping_express_extra': envios.EXTRA_EXPRESS_MXN,
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

# ⛔ NI LO QUE LE CUESTA LA GUÍA A LA CASA. La regla de Christián es que el cliente
# NUNCA vea una cifra de envío: la casa lo absorbe y enseñarle lo que cuesta es
# enseñarle el margen. Esa regla estaba cuidada en los correos pero NO en la API, que
# devolvía el documento entero — `shipping_cost` (lo que se pagó de guía),
# `shipping_absorbed` (lo que se comió la casa) y con quién se compró, todo a la vista
# de cualquiera que abriera la consola del navegador. Y `/orders/{numero}` ni siquiera
# pide sesión.
#
# Los `label_*` se suman por lo mismo: `label_precio_cotizado` es una cotización
# interna y `label_error` / `label_hold` son problemas de la casa, no del cliente.
# Lo que el cliente sí necesita —`tracking_number`, `tracking_url`, `carrier`— se
# queda: eso es justo lo que se le prometió por correo.
# ⛔ Y `label_url` SOBRE TODO. Se coló en la primera pasada porque parece «la liga de
# la guía», algo que el cliente ya conoce. NO LO ES: es la liga FIRMADA al PDF de la
# ETIQUETA, y ese papel trae impreso el NOMBRE Y EL DOMICILIO COMPLETO de quien recibe,
# más la dirección del remitente. Comprobado en vivo el 2026-07-31 con el pedido de
# Brenda: bastaba pedir `/api/orders/EX-...` sin sesión y bajar el PDF para tener su
# casa. Y los números de pedido son enumerables (`EX-AAAAMMDD-` + cuatro dígitos).
# Quien tenga que imprimir la etiqueta la pide por `/…/etiqueta` (ver etiquetas.py),
# que sí exige rol.
CAMPOS_INTERNOS_DE_ENVIO = (
    'shipping_cost', 'shipping_absorbed', 'shipping_over_cap', 'shipping_quote',
    'label_provider', 'label_url', 'label_error', 'label_hold', 'label_lock',
    'label_intentos', 'label_ultimo_intento', 'label_precio_cotizado', 'label_piezas',
    'label_empaque',
    'emails_sent', 'card_checkout_url', 'card_preference_id', 'stock_taken',
)


def pedido_para_el_cliente(order):
    """El pedido tal como puede verlo quien compró: sin quién lo refirió, sin cuánto
    ganó nadie y sin lo que la guía le costó a la casa. Devuelve una copia; el
    documento original no se toca."""
    if not order:
        return order
    fuera = CAMPOS_DEL_DISTRIBUIDOR + CAMPOS_INTERNOS_DE_ENVIO
    limpio = {k: v for k, v in order.items() if k not in fuera}
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
    # ⛔ Y LA FICHA DE OXXO, PARA QUE SE PUEDA VOLVER A ELLA. Es la URL de Mercado Pago
    # con el código de barras: viajaba UNA sola vez en la respuesta del checkout y
    # quien cerraba esa pestaña se quedaba sin forma de pagar. Se devuelve sólo si el
    # pedido es de OXXO y SIGUE PENDIENTE: una liga de pago de algo ya pagado no le
    # sirve a nadie y sólo invita a pagar dos veces.
    if ((order.get('payment_method') or '') == 'oxxo'
            and order.get('status') == 'pendiente'):
        completo = await db.orders.find_one({'order_number': order_number},
                                            {'_id': 0, 'card_checkout_url': 1})
        if (completo or {}).get('card_checkout_url'):
            order['card_checkout_url'] = completo['card_checkout_url']
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


# ----------------- Los pedidos que dejan las pruebas -----------------
# ⛔ QUIEN PRUEBA COMPRANDO EN PRODUCCIÓN, LIMPIA LO QUE ENSUCIÓ (Christián, 2026-08-01).
# La regla completa está en CLAUDE.md; la mecánica y el porqué, en `pruebas.py`.
class MarcaDePrueba(BaseModel):
    es_prueba: bool


@api_router.put('/admin/orders/{order_id}/prueba')
async def admin_marcar_prueba(order_id: str, payload: MarcaDePrueba,
                              admin=Depends(get_current_admin)):
    """Pone (o quita) la etiqueta de PEDIDO DE PRUEBA.

    Es SÓLO una etiqueta: no borra nada, no esconde nada y se quita igual de fácil.
    Existe para que el barrido sepa qué es basura de una prueba y qué no: lo que nadie
    marcó, el barrido ni lo mira.
    """
    deny_view_as(admin)                      # 'ver como' es de sólo lectura
    order = await db.orders.find_one({'id': order_id}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    await db.orders.update_one({'id': order_id},
                               {'$set': {'es_prueba': bool(payload.es_prueba)}})
    logger.info('Admin %s marcó el pedido %s como %s', admin.get('email'),
                order.get('order_number'),
                'DE PRUEBA' if payload.es_prueba else 'NO de prueba')
    # Se contesta de una vez si el barrido podrá llevárselo. Marcar una venta real no
    # está prohibido —a veces uno se equivoca de renglón— pero el Panel tiene que poder
    # decirlo en el momento y no cuando ya se apretó "barrer".
    motivos = pruebas.senales_de_venta_real(order)
    return {'order_number': order.get('order_number'), 'es_prueba': bool(payload.es_prueba),
            'se_puede_barrer': bool(payload.es_prueba) and not motivos, 'motivos': motivos}


class BarridoDePruebas(BaseModel):
    simulacro: bool = True


@api_router.post('/admin/orders/barrer-pruebas')
async def admin_barrer_pruebas(payload: BarridoDePruebas, admin=Depends(get_current_admin)):
    """BARRE los pedidos marcados como prueba. No puede llevarse una venta de verdad.

    Tres candados, en este orden:

      1. Sólo mira los pedidos con la etiqueta `es_prueba`. Lo que nadie marcó no
         existe para el barrido — no hay "borrar todos los pendientes".
      2. De ésos aparta cualquiera con una señal de venta real
         (`pruebas.senales_de_venta_real`): pagado, surtido, con comprobante o con guía.
         Ante la duda, no se borra: se devuelve en `protegidos` con el motivo.
      3. El borrado NO se escribe aquí. Se le pasa al lote de siempre con
         `forzar=False`, que es donde vive el candado de los pedidos pagados y donde se
         devuelven puntos e inventario. Un camino de borrado propio sería un candado
         menos y otra copia de la aritmética.

    Por omisión es SIMULACRO: contesta qué haría y no toca nada. Borra de verdad sólo
    con `simulacro: false`.
    """
    deny_view_as(admin)
    marcados = await db.orders.find({'es_prueba': True}, {'_id': 0}).to_list(500)
    barrer, protegidos = [], []
    for o in marcados:
        motivos = pruebas.senales_de_venta_real(o)
        if motivos:
            protegidos.append({'order_number': o.get('order_number'),
                               'status': o.get('status'),
                               'cliente': (o.get('customer') or {}).get('full_name', ''),
                               'total': o.get('total', 0),
                               'motivos': motivos})
        else:
            barrer.append(o)

    if payload.simulacro or not barrer:
        return {'simulacro': bool(payload.simulacro), 'marcados': len(marcados),
                'borrados': 0, 'numeros': [o.get('order_number') for o in barrer],
                'protegidos': protegidos}

    lote = await admin_orders_lote(
        LoteDePedidos(ids=[o['id'] for o in barrer], accion='borrar', forzar=False), admin)
    # Si el lote frenó alguno (un pedido que cambió de estado entre el simulacro y el
    # botón), su motivo es siempre el mismo candado: ya estaba pagado.
    return {'simulacro': False, 'marcados': len(marcados),
            'borrados': lote['hechos'], 'numeros': lote['numeros'],
            'protegidos': protegidos + [{**p, 'motivos': ['pagado']}
                                        for p in lote['protegidos']]}


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
        await notify(order.get('user_id'), 'payment_confirmed', 'Pago confirmado',
                     f'Confirmamos el pago de tu pedido {num}. ¡Gracias!', link=f'/pedido/{num}')
        # SPEI llega por aquí: el admin verifica el depósito y marca 'confirmado'.
        # Es el cuarto método de pago, y compra su guía igual que los otros tres —y
        # como los otros tres, el correo de pago sale DESPUÉS de la guía para que la
        # lleve adentro. Con SPEI son dos correos en total: el de la CLABE y éste.
        asyncio.create_task(_confirmar_y_avisar(order))
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
    # ⛔ UNA GUÍA SIN PAQUETERÍA NO SE PUEDE RASTREAR (Christián, 2026-07-31). La
    # pantalla que captura guías ya la adivina mientras se teclea, pero esta ruta se
    # puede llamar sin pasar por ahí —el distribuidor, un script, la app de mañana— y
    # entonces el pedido queda con número y sin transportista: ni liga de rastreo, ni
    # eventos, ni forma de saber a quién preguntarle. Se deduce del propio número
    # (`guias.py`, el gemelo de la detección de la pantalla). Lo que SÍ venga capturado
    # manda siempre: esto sólo rellena el hueco.
    if number and not carrier:
        carrier = guias.paqueteria_de(number)
        if carrier:
            update['carrier'] = carrier
            logger.info('Envio: la guia %s no traia paqueteria; se dedujo %s',
                        number, carrier)
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
    # ⛔ EL MOSAICO TIENE QUE DAR EL MISMO NÚMERO QUE LA LISTA (Christián, 2026-07-31).
    # Desde que los invitados son clientes, contar sólo las cuentas dejaba el tablero
    # diciendo «4 clientes» y la pestaña de al lado enseñando 6 — y a un tablero que se
    # contradice con la lista que abre no se le vuelve a creer. Se cuenta EXACTAMENTE lo
    # mismo que arma `/admin/customers`: un renglón por correo de invitado. Si alguno de
    # esos correos ya tiene cuenta, ahí siguen los dos renglones a propósito, marcados
    # como posible duplicado, hasta que Christián decida fusionarlos.
    todos = await db.orders.find({}, {'_id': 0, 'user_id': 1, 'customer': 1}).to_list(20000)
    total_users += len(_agrupar_invitados(todos))
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
#
# ⛔ TODO EL QUE COMPRA ES CLIENTE, TENGA CUENTA O NO (Christián, 2026-07-31).
# El checkout permite comprar como invitado, y esta lista se armaba únicamente con
# `users.role == 'user'`: quien compró sin abrir cuenta NO EXISTÍA para la casa. Le
# pasó a Brenda ($4,827) y a Aidee ($2,830) el 2026-07-30 — dinero cobrado, guía puesta,
# comisión pagada, y ni una ficha a la que volver para venderles otra vez.

def _correo_llave(x) -> str:
    """La llave con la que se identifica a una persona SIN cuenta: su correo, en
    minúsculas y sin espacios. «Juan@X.mx» y «juan@x.mx» son la misma persona."""
    return (x or '').strip().lower()


def _correos_de_la_cuenta(u) -> list:
    """El correo principal MÁS los alternos. `alt_emails` puede venir como texto suelto
    (así lo guardó la fusión de cuentas de la casa) o como lista: se aceptan los dos."""
    alt = u.get('alt_emails') or []
    if isinstance(alt, str):
        alt = [alt]
    return [c for c in (_correo_llave(x) for x in [u.get('email'), *alt]) if c]


async def _mapa_de_correos_con_cuenta() -> dict:
    """correo → cuenta, mirando también los alternos.

    ⛔ MISMA REGLA QUE EN LA PUERTA DE ENTRADA. `_usuario_por_correo` ya resuelve el
    login con `{'$or': [{'email': e}, {'alt_emails': e}]}` desde que se fusionaron las
    dos direcciones de la casa. Si las listas de clientes miraran sólo `email`, la misma
    persona saldría dos veces: una por su cuenta y otra como «invitada» de su correo
    alterno. Una persona, una ficha — también aquí."""
    users = await db.users.find({}, {'_id': 0, 'id': 1, 'name': 1, 'email': 1,
                                     'alt_emails': 1, 'role': 1,
                                     'email_verified': 1}).to_list(5000)
    mapa = {}
    for u in users:
        for c in _correos_de_la_cuenta(u):
            mapa.setdefault(c, u)
    return mapa


def _agrupar_invitados(orders) -> dict:
    """Los pedidos SIN cuenta, agrupados por correo. correo → lista de pedidos.

    Un pedido sin correo no se puede atribuir a nadie y se queda fuera: no hay llave."""
    por_correo = {}
    for o in orders:
        if o.get('user_id'):
            continue                     # con cuenta: sale por la otra vía
        correo = _correo_llave((o.get('customer') or {}).get('email'))
        if not correo:
            continue
        por_correo.setdefault(correo, []).append(o)
    return por_correo


def _contacto_mas_reciente(pedidos) -> dict:
    """Nombre, teléfono y domicilios de un invitado: los de su pedido MÁS NUEVO manda,
    porque la gente corrige sus datos al recomprar."""
    ordenados = sorted(pedidos, key=lambda o: o.get('created_at', ''), reverse=True)
    nombre, telefonos, domicilios = '', [], []
    for o in ordenados:
        c = o.get('customer') or {}
        nombre = nombre or (c.get('full_name') or '')
        tel = (c.get('phone') or '').strip()
        if tel and tel not in telefonos:
            telefonos.append(tel)
        pais = c.get('country') if c.get('country') not in (None, '', 'MX') else None
        dom = ', '.join(x for x in [c.get('address'), c.get('city'), c.get('state'),
                                    c.get('postal_code'), pais] if x)
        if dom and dom not in domicilios:
            domicilios.append(dom)
    return {'name': nombre, 'phones': telefonos, 'addresses': domicilios}


def _lo_que_suele_llevar(orders, tope=5) -> list:
    """Qué productos compra esta persona, del que más piezas se lleva al que menos.

    Es la pregunta que se hace quien va a volver a venderle: «¿qué le ofrezco?». Sin
    esto había que abrir sus pedidos uno por uno y sumar de memoria."""
    agg = {}
    for o in orders:
        for it in (o.get('items') or []):
            nombre = it.get('name') or '—'
            fila = agg.setdefault(nombre, {'name': nombre, 'units': 0, 'orders': 0})
            fila['units'] += int(it.get('quantity', 0) or 0)
            fila['orders'] += 1
    return sorted(agg.values(), key=lambda f: (-f['units'], f['name']))[:tope]


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
            'guest': False,
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

    # ⛔ Y LOS QUE COMPRARON SIN CUENTA. Mismo renglón, misma ficha, con el distintivo
    # de «invitado» para que se sepa de un vistazo que no hay perfil detrás.
    cuentas = await _mapa_de_correos_con_cuenta()
    for correo, pedidos in _agrupar_invitados(orders).items():
        vivos = [o for o in pedidos if esta_vivo(o)]
        pedidos.sort(key=lambda o: o.get('created_at', ''), reverse=True)
        contacto = _contacto_mas_reciente(pedidos)
        fila = {
            'id': f'invitado:{correo}',
            'guest': True,
            'name': contacto['name'] or correo,
            'email': correo,
            'role': 'user',
            # Para un invitado, «desde cuándo es cliente» es su PRIMERA compra: no hay
            # fecha de registro porque no hay registro.
            'created_at': min((o.get('created_at', '') for o in pedidos), default=None),
            'orders_count': len(pedidos),
            'total_spent': sum(cobrado_de(o) for o in vivos),
            'por_cobrar': sum(por_cobrar_de(o) for o in vivos),
            'last_order_at': pedidos[0].get('created_at') if pedidos else None,
            'addresses': contacto['addresses'],
            'phones': contacto['phones'],
            'orders': pedidos,
        }
        # Si ese correo YA tiene cuenta y aun así el pedido quedó huérfano, es que la
        # cuenta nunca confirmó el correo (`_adoptar_pedidos_de_invitado` no adopta sin
        # confirmar, y con razón). NO se fusiona a ciegas: se marca para que el admin
        # decida. Ver /admin/clientes/duplicados.
        gemela = cuentas.get(correo)
        if gemela:
            fila['posible_duplicado_de'] = {'id': gemela.get('id'), 'name': gemela.get('name'),
                                            'email': gemela.get('email')}
        out.append(fila)

    out.sort(key=lambda u: (-u['total_spent'], u.get('created_at') or ''))
    return out


@api_router.get('/admin/clientes/duplicados')
async def clientes_duplicados(admin=Depends(get_current_admin)):
    """LA MISMA PERSONA, DOS VECES. Un reporte, no una fusión.

    ⛔ NO SE FUSIONA A CIEGAS (Christián, 2026-07-31). Juntar dos fichas es regalarle a
    una cuenta el historial de compras de otra —nombre, teléfono, domicilio y qué
    compró— y si el emparejamiento se equivoca no hay vuelta atrás. Así que aquí sólo se
    señala; la decisión es de Christián.

    Tres formas de estar duplicado:
      · `invitado_con_cuenta` — compró como invitado con un correo que YA tiene cuenta.
        Pasa cuando la cuenta nunca confirmó su correo: la adopción automática exige la
        confirmación (es la única prueba de que el buzón es suyo).
      · `correo_repetido` — dos cuentas con el mismo correo normalizado (o el principal
        de una es el alterno de la otra).
      · `telefono_repetido` — dos personas distintas con el mismo teléfono. Puede ser
        legítimo (una pareja, una oficina); por eso se reporta y no se toca.
      · `nombre_repetido` — el mismo nombre con dos correos distintos. Es el caso más
        flojo de los tres (hay muchos Juan Pérez) pero es el que caza a la persona que
        se registró dos veces con dos buzones, que no deja ninguna otra huella. Como
        todo lo de aquí, es un aviso para mirarlo, no una fusión.
    """
    users = await db.users.find({}, {'_id': 0, 'password_hash': 0,
                                     'totp_secret': 0}).to_list(5000)
    orders = await db.orders.find({}, {'_id': 0}).to_list(20000)
    cuentas = await _mapa_de_correos_con_cuenta()
    hallazgos = []

    for correo, pedidos in _agrupar_invitados(orders).items():
        cuenta = cuentas.get(correo)
        if not cuenta:
            continue
        vivos = [o for o in pedidos if esta_vivo(o)]
        hallazgos.append({
            'tipo': 'invitado_con_cuenta',
            'llave': correo,
            'invitado': {'id': f'invitado:{correo}', 'name': _contacto_mas_reciente(pedidos)['name'],
                         'email': correo, 'orders_count': len(pedidos),
                         'total_spent': sum(cobrado_de(o) for o in vivos)},
            'cuenta': {'id': cuenta.get('id'), 'name': cuenta.get('name'),
                       'email': cuenta.get('email'),
                       'email_verified': bool(cuenta.get('email_verified'))},
            # Qué hacer: si la cuenta confirma su correo, la adopción es automática y el
            # duplicado desaparece solo. Por eso el motivo va explícito.
            'motivo': 'correo_sin_confirmar' if not cuenta.get('email_verified') else 'pedido_huerfano',
        })

    por_correo, por_telefono, por_nombre = {}, {}, {}
    for u in users:
        for c in _correos_de_la_cuenta(u):
            por_correo.setdefault(c, []).append(u)
        # Los últimos 10 dígitos: el mismo número con y sin lada (+52 55…) es UNO.
        tel = re.sub(r'\D', '', str(u.get('phone') or ''))[-10:]
        if len(tel) == 10:
            por_telefono.setdefault(tel, []).append(u)
        # Sin acentos, sin dobles espacios y en minúsculas: «María  Neunfeld» y
        # «maria neunfeld» se teclearon distinto pero son la misma persona.
        nom = unicodedata.normalize('NFD', str(u.get('name') or ''))
        nom = ' '.join(''.join(x for x in nom if not unicodedata.combining(x)).lower().split())
        if len(nom) >= 5:
            por_nombre.setdefault(nom, []).append(u)

    def _fichas(lista):
        return [{'id': x.get('id'), 'name': x.get('name'), 'email': x.get('email'),
                 'role': x.get('role')} for x in lista]

    for correo, lista in por_correo.items():
        if len({x.get('id') for x in lista}) > 1:
            hallazgos.append({'tipo': 'correo_repetido', 'llave': correo,
                              'cuentas': _fichas(lista)})
    for tel, lista in por_telefono.items():
        if len({x.get('id') for x in lista}) > 1:
            hallazgos.append({'tipo': 'telefono_repetido', 'llave': tel,
                              'cuentas': _fichas(lista)})
    for nom, lista in por_nombre.items():
        if len({x.get('id') for x in lista}) > 1:
            hallazgos.append({'tipo': 'nombre_repetido', 'llave': nom,
                              'cuentas': _fichas(lista)})

    hallazgos.sort(key=lambda h: (h['tipo'], h['llave']))
    return {'total': len(hallazgos), 'duplicados': hallazgos}


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


async def _new_code_string(dist, rate):
    """Un texto de código libre para ese distribuidor. Recibe la FICHA COMPLETA,
    no el nombre: es lo que deja a `prefijo_de` ver la marca `code_prefix`."""
    code = gen_discount_code(dist, rate)
    while await db.discount_codes.find_one({'code': code}):
        code = gen_discount_code(dist, rate)
    return code


async def _codigos_jubilados(dist_id):
    """Los códigos que YA NO SE REPARTEN pero todavía cobran: rotados hace poco y
    aún dentro de su caducidad. Ordenados por cuál se muere primero."""
    ahora = now_iso()
    docs = await db.discount_codes.find({'distributor_id': dist_id, 'active': True,
                                         'superseded_at': {'$ne': None}}).to_list(300)
    vivos = [d for d in docs if d.get('superseded_at')
             and (not d.get('expires_at') or d['expires_at'] >= ahora)]
    vivos.sort(key=lambda d: d.get('expires_at') or '')
    return vivos


async def _ensure_distributor_codes(dist, force_rotate=False):
    """Mantiene el set de códigos AUTO del distribuidor: uno por cada nivel de
    descuento de su comisión (15%, 20%… hasta 5% debajo de su comisión). Crea los
    que falten, ROTA los caducados, y desactiva los que ya no correspondan a su
    nivel. Devuelve los códigos VIGENTES —los que hay que repartir— ordenados.

    ⛔ ROTAR YA NO MATA AL VIEJO (Christián, 2026-07-31). Antes esto reescribía el
    texto DENTRO del mismo documento: en cuanto alguien pulsaba «rotar», el código
    que los clientes ya traían en la mano dejaba de existir y se quedaban sin su
    descuento sin que nadie les avisara. Christián lo preguntó justo antes de la
    rotación a `MONICAF`: «¿los códigos que María ya repartió siguen funcionando?».
    Ahora el viejo se JUBILA —`superseded_at`, sigue `active` y conserva SU
    caducidad original— y el nuevo nace a su lado. Los dos resuelven al MISMO
    distribuidor y con el MISMO porcentaje (`_resolve_code` busca por texto exacto y
    la atribución sale de `distributor_id`), así que durante la gracia no se pierde
    ni una comisión. El periodo de gracia no se inventa: es lo que le quedaba de
    vida al código viejo, hasta 90 días (`CODE_TTL_DAYS`).

    Un código MUERTO —caducado o desactivado— sí se reescribe en su sitio: ahí no
    hay gracia que preservar, y guardar basura sólo llena la colección."""
    rate_basis = pyramid.effective_rate(dist)
    tiers = pyramid.discount_tiers_de(dist)
    tierset = {round(r, 4) for r in tiers}
    existing = await db.discount_codes.find({'distributor_id': dist['id']}).to_list(300)
    now = now_iso()
    by_rate = {}
    for c in existing:
        if c.get('superseded_at'):
            continue          # jubilado: sigue cobrando, pero ya no es EL código del nivel
        by_rate.setdefault(round(c.get('discount_rate', 0), 4), c)
    new_exp = (datetime.now(timezone.utc) + timedelta(days=CODE_TTL_DAYS)).isoformat()
    out = []
    for rate in tiers:
        c = by_rate.get(round(rate, 4))
        expired = bool(c and c.get('expires_at') and c['expires_at'] < now)
        if c is not None and force_rotate and not expired and c.get('active', True):
            # Está VIVO y lo mandan rotar: se jubila (conserva su caducidad) y abajo
            # nace el nuevo. Es todo el periodo de gracia, en dos líneas.
            await db.discount_codes.update_one({'id': c['id']},
                                               {'$set': {'superseded_at': now}})
            c = None
        if not c:
            doc = {'id': str(uuid.uuid4()), 'distributor_id': dist['id'],
                   'code': await _new_code_string(dist, rate),
                   'discount_rate': rate, 'active': True, 'created_at': now,
                   'expires_at': new_exp, 'superseded_at': None}
            await db.discount_codes.insert_one(doc)
            out.append(doc)
        elif expired or not c.get('active', True):
            # Muerto: no hay nada que preservar, se reescribe en su sitio.
            new_code = await _new_code_string(dist, rate)
            await db.discount_codes.update_one({'id': c['id']}, {'$set': {
                'code': new_code, 'active': True, 'created_at': now,
                'expires_at': new_exp, 'superseded_at': None}})
            c.update({'code': new_code, 'active': True, 'created_at': now,
                      'expires_at': new_exp, 'superseded_at': None})
            out.append(c)
        else:
            out.append(c)
    # Desactiva códigos de niveles que ya no aplican (p.ej. tras cambiar de nivel).
    # ⛔ SALVO LOS JUBILADOS: su razón de existir es sobrevivir hasta su caducidad, y
    # su nivel casi nunca sigue en `tierset` (el legacy del 10% no está en ningún
    # escalón). Sin esta salvedad, la primera lectura de `/distributor/codes`
    # apagaría el periodo de gracia recién concedido.
    for c in existing:
        if (round(c.get('discount_rate', 0), 4) not in tierset
                and c.get('active', True) and not c.get('superseded_at')):
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
    emite ni un código nuevo.

    `previos` son los que ya NO se reparten pero SIGUEN COBRANDO hasta su caducidad
    (ver el periodo de gracia en `_ensure_distributor_codes`). Van aparte y no
    revueltos con los vigentes: el distribuidor tiene que saber cuáles seguir dando
    y cuáles nada más va a ver llegar."""
    await _exigir_acuerdo(dist)
    codes = await _ensure_distributor_codes(dist)
    previos = await _codigos_jubilados(dist['id'])
    return {'max_discount': pyramid.effective_rate(dist),
            'rotate_days': CODE_TTL_DAYS,
            'codes': [_code_projection(c) for c in codes],
            'previos': [_code_projection(c) for c in previos]}


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
    la hoja impresa digan el mismo número.

    ⛔ SIN DESCUENTO PROPIO, MANDA LA PROMO DE LA CASA (Christián, 2026-08-01):
    si el distribuidor cotizó al 0%, el cliente recibe los automáticos — 10%, o
    15% desde $35,000 de mercancía descuentable — igual que si llegara solo al
    sitio. La caja cobra esta misma cuenta (ver `create_order`)."""
    if (pedido_pct or 0) <= 0:
        base = sum(round(float(d.get('price') or 0)) * int(it.quantity)
                   for it in items
                   for d in [catalogo.get(it.product_id) or {}]
                   if d and not d.get('hidden') and float(d.get('price') or 0) > 0
                   and tope_de_descuento(d) > 0)
        pedido_pct = promo_automatica(base)
        # La promo es de la casa, no del nivel del distribuidor: no se recorta
        # con su máximo (el tope POR PRODUCTO sí sigue mandando, abajo).
        tope_dist = max(tope_dist, pedido_pct)
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
    salio, motivo = await send_quote_email(
        payload.email, cotizacion,
        language=payload.language or dist.get('language'),
        reply_to=ATENCION_CORREO)
    if not salio:
        # ⛔ EL MOTIVO VIAJA A LA PANTALLA (Christián, 2026-08-01). «No se pudo,
        # intenta de nuevo» era falso cuando el correo está APAGADO en el servidor:
        # ahí no hay nada que reintentar. La pantalla lo dice con esas palabras y
        # ofrece WhatsApp y el enlace del carrito, que sí funcionan.
        if motivo == 'apagado':
            raise HTTPException(status_code=503, detail={
                'error': 'correo_apagado',
                'mensaje': ('El envío de correo está apagado en el servidor '
                            '(EMAIL_ENABLED). Comparte por WhatsApp o con el enlace '
                            'del carrito mientras tanto.')})
        raise HTTPException(status_code=502, detail={
            'error': 'correo_rechazado',
            'mensaje': 'El proveedor de correo rechazó el envío. No salió nada.'})
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


# ==========================================================================
#  EL CARRITO COMPARTIBLE  —  el enlace que Mónica manda por WhatsApp
# ==========================================================================
# Encargo de Christián (2026-08-01): «Necesito que Mónica pueda compartir un carrito
# con sus clientes.» Un enlace, el cliente lo abre en su teléfono sin cuenta, ve su
# carrito ya armado con SUS precios y SUS cortesías, y la venta se le atribuye a ella.
#
# ⛔ EL ENLACE NO LLEVA DINERO ADENTRO. Sólo un token opaco. Todo —precios, descuento,
# envío y el valor de las cortesías— lo calcula el SERVIDOR al abrirlo y lo vuelve a
# calcular al cobrar. Es la misma lección que costó dinero el 2026-07-27, cuando se
# podía comprar mandando precio $0: si el número viaja en la URL, el número se edita.
#
# ⛔ Y EL CÓDIGO DEL OBSEQUIO NO VIAJA. Vive en `gift_code` dentro del documento y no
# sale por ninguna de estas rutas: la respuesta se arma con la lista blanca de
# `regalos.vista_publica`. Ver la prueba que lee el payload entero y truena si aparece.
COLECCION_CARRITOS = 'shared_carts'

# Cuánto vive un carrito compartido. Un mes es lo que tarda una conversación de
# WhatsApp en cerrarse; más allá de eso los precios ya no son los de esa charla y es
# mejor que el cliente vuelva a pedirle uno nuevo a su distribuidora.
VIGENCIA_CARRITO_DIAS = 30

# Cuántos carritos puede armar un distribuidor al día. El mismo freno que las
# cotizaciones por correo, y por lo mismo: es una ruta que ESCRIBE.
CARRITOS_POR_DIA = 120
_CARRITOS_ARMADOS = {}


def _puede_armar_carrito(dist_id, ahora=None):
    import time as _t
    ahora = ahora if ahora is not None else _t.time()
    marcas = [m for m in _CARRITOS_ARMADOS.get(dist_id, []) if ahora - m < 86400]
    if len(marcas) >= CARRITOS_POR_DIA:
        _CARRITOS_ARMADOS[dist_id] = marcas
        return False
    marcas.append(ahora)
    _CARRITOS_ARMADOS[dist_id] = marcas
    return True


class _Renglon:
    """Un renglón suelto con la forma que esperan `_catalogo_de`, `_armar_cotizacion`
    y `regalos.piso_de_rentabilidad` (que leen atributos, no llaves). Existe para no
    tener que fabricar un `OrderItem` completo —con nombre e imagen— sólo para
    preguntarle su precio al catálogo."""

    def __init__(self, product_id, quantity=1, price=0.0, name=''):
        self.product_id = product_id
        self.quantity = int(quantity or 1)
        self.price = float(price or 0)
        self.name = name


async def _resolver_carrito(doc):
    """Recalcula un carrito compartido CONTRA EL CATÁLOGO DE HOY. Devuelve (privado, público).

    Se recalcula al ABRIRLO y otra vez al COBRAR, nunca se lee un total guardado: un
    carrito de hace tres semanas tiene que cobrar los precios de hoy, y un token
    manipulado no puede inventar ni un peso porque de él sólo se toman qué productos,
    cuántos y qué se obsequió.

    El diccionario PRIVADO trae lo que necesita el checkout (incluido el veredicto de
    ROI); el PÚBLICO es el que ve el cliente, armado con lista blanca.
    """
    guardados = doc.get('items') or []
    lineas_pedidas = [_Renglon(i.get('product_id'), i.get('quantity') or 1)
                      for i in guardados]
    catalogo = await _catalogo_de(lineas_pedidas + [
        _Renglon(g.get('product_id')) for g in (doc.get('gifts') or []) if g.get('product_id')])

    dist = await db.users.find_one({'id': doc.get('distributor_id')}, {'_id': 0}) or {}
    tope_dist = max(pyramid.discount_tiers_de(dist) or [0]) if dist else 0.0
    pedido_pct = max(0.0, float(doc.get('discount_asked') or 0))
    lineas, lista_total, total_mercancia = _armar_cotizacion(
        lineas_pedidas, pedido_pct, tope_dist, catalogo)

    # ---- las cortesías, valuadas por el servidor contra el catálogo real ----
    def _precio_de(pid):
        d = catalogo.get(pid) or {}
        return float(d.get('price') or 0)

    costo_guia = float(COSTO_GUIA_ESTIMADO)
    obsequios = doc.get('gifts') or []
    valor_obsequio = regalos.valor_de_obsequios(obsequios, _precio_de, costo_guia)

    # ---- ⛔ EL PISO DE RENTABILIDAD, EN EL SERVIDOR Y OTRA VEZ ----
    # Regalar es descontar: el vial de cortesía sale del mismo margen que el
    # descuento. Se suman y se miden contra el tope de cada producto y el techo del
    # 40%. Si no cabe, el obsequio NO se aplica — la venta sigue en pie (nunca se
    # bloquea una venta), pero el regalo se cae y queda constancia.
    items_para_tope = [_Renglon(ln['product_id'], ln['quantity'],
                                float(ln['list_price']), ln['name'])
                       for ln in lineas]
    permitido = regalos.piso_de_rentabilidad(
        items_para_tope,
        lambda it: tope_de_descuento(catalogo.get(it.product_id) or {}),
        techo=techo_de_descuento(None))
    veredicto = regalos.cabe_el_obsequio(lista_total - total_mercancia, valor_obsequio, permitido)

    aplicados = obsequios if veredicto['cabe'] else []
    if obsequios and not veredicto['cabe']:
        logger.warning('OBSEQUIO RECHAZADO por ROI en el carrito %s: se regalaban $%.0f '
                       'sobre un permitido de $%.0f (se pasa por $%.0f).',
                       doc.get('token'), veredicto['entregado'], veredicto['permitido'],
                       veredicto['exceso'])

    # Cómo se le NOMBRA cada cortesía al cliente: su nombre y la palabra "cortesía"
    # la pone la pantalla. Aquí sólo viaja el nombre real del producto.
    vistos = []
    envio_de_cortesia = False
    for g in aplicados:
        if g.get('tipo') == regalos.TIPO_ENVIO:
            envio_de_cortesia = True
            vistos.append({'tipo': regalos.TIPO_ENVIO, 'name': '', 'quantity': 1})
        else:
            d = catalogo.get(g.get('product_id')) or {}
            if not d or d.get('hidden'):
                continue
            vistos.append({'tipo': regalos.TIPO_PRODUCTO, 'name': _nombre_cotizable(d),
                           'quantity': int(g.get('cantidad') or 1)})

    # ---- el envío, con la política de la casa y no una inventada aquí ----
    # ⛔ SIN DIRECCIÓN NO SE COTIZA ENVÍO (Christián, 2026-08-01, con estas
    # palabras: «que en las cotizaciones no se muestre el costo de envío si no se
    # proporcionó una dirección»). El carrito enseña «se calcula al pagar»
    # (`shipping_pending`) y el checkout lo cobra como siempre, ya con el
    # domicilio enfrente. El envío de CORTESÍA es gratis con o sin dirección.
    # Con el CP basta para cotizar (Christián, 2026-08-02): «necesitamos por lo
    # menos saber el Zip Code». Sin CP y sin calle, se cotiza por separado.
    sin_direccion = not (str(doc.get('client_address') or '').strip()
                         or str(doc.get('client_zip') or '').strip())
    envio = 0
    envio_pendiente = False
    if COBRAR_ENVIO and lineas and not envio_de_cortesia:
        if sin_direccion:
            envio_pendiente = True
        else:
            envio = shipping_for(total_mercancia)

    publico = regalos.vista_publica({
        'token': doc.get('token'),
        'folio': doc.get('folio') or '',
        'client_name': doc.get('client_name') or '',
        'currency': 'MXN',
        'lines': lineas,
        'gifts': vistos,
        'list_total': round(lista_total),
        'discount': round(lista_total - total_mercancia),
        # El PORCENTAJE, que es lo que Christián pidió que se lea en vez de "ahorro".
        'discount_rate': round((lista_total - total_mercancia) / lista_total, 4) if lista_total else 0,
        'shipping': round(envio),
        'shipping_free': bool(COBRAR_ENVIO and lineas and envio <= 0 and not envio_pendiente),
        'shipping_pending': envio_pendiente,
        'total': round(total_mercancia + envio),
        # El código del DISTRIBUIDOR sí viaja: es lo que atribuye la venta, y el
        # cliente ya lo veía en los enlaces `?ref=` de siempre. El del OBSEQUIO no.
        'ref': doc.get('ref') or '',
        'expires_at': doc.get('expires_at'),
    })
    privado = {
        'gifts_aplicados': aplicados,
        'gifts_vistos': vistos,
        'envio_de_cortesia': envio_de_cortesia,
        'veredicto': veredicto,
        'catalogo': catalogo,
        'ref': doc.get('ref') or '',
    }
    return privado, publico


@api_router.post('/distributor/cart/share')
async def distributor_cart_share(payload: ShareCartRequest,
                                 dist=Depends(get_current_distributor)):
    """Arma el carrito compartible y devuelve SU ENLACE. Nunca el código del obsequio.

    Tres candados, los tres en el servidor:
      1. `get_current_distributor` + `deny_view_as` — un cliente no entra, y el
         "ver como" del admin es sólo lectura.
      2. EL PRECIO LO PONE EL SERVIDOR. Del navegador sólo viajan qué, cuántos y
         cuánto descuento se PIDE.
      3. EL REGALO NO ROMPE EL ROI. Si no cabe bajo el tope, se responde 409 con la
         cuenta hecha para que el distribuidor lo vea y lo baje. Aquí sí se frena
         —es la pantalla de quien arma— y al cobrar se vuelve a medir por si el
         catálogo cambió entre que se compartió y que el cliente pagó.
    """
    deny_view_as(dist)
    await _exigir_acuerdo(dist)
    if not payload.items:
        raise HTTPException(status_code=400, detail='El carrito no tiene renglones')
    if len(payload.items) > 40:
        raise HTTPException(status_code=400, detail='Demasiados renglones en el carrito')
    if not _puede_armar_carrito(dist['id']):
        raise HTTPException(status_code=429, detail='Demasiados carritos seguidos. Espera un momento.')

    catalogo = await _catalogo_de(
        list(payload.items) + [_Renglon(g.product_id) for g in payload.gifts if g.product_id])
    limpios = regalos.limpiar_obsequios(
        [g.model_dump() for g in payload.gifts],
        lambda pid: bool(catalogo.get(pid)) and not (catalogo.get(pid) or {}).get('hidden'))

    ahora = now_iso()
    vence = (datetime.now(timezone.utc) + timedelta(days=VIGENCIA_CARRITO_DIAS)).isoformat()
    doc = {
        'token': regalos.nuevo_token_de_carrito(),
        # ⛔ EL CÓDIGO INTERNO DEL OBSEQUIO. Se guarda para poder auditar de dónde
        # salió cada cortesía, y NO SALE de este archivo hacia ningún cliente: ni en
        # la respuesta de aquí, ni en la de `/carrito/{token}`, ni en el PDF.
        'gift_code': regalos.nuevo_codigo_de_obsequio(),
        'distributor_id': dist['id'],
        'ref': (dist.get('distributor_code') or '').strip(),
        # ⛔ LA SEGUNDA LLAVE — la que abre los datos del cliente y NADA MÁS.
        # Viaja en el FRAGMENTO del enlace (`#d=`), que el navegador no manda a
        # ningún servidor: no queda en los registros ni en el `Referer`. Se guarda
        # tal cual y sólo vuelve a salir por dos puertas: la respuesta de ESTE
        # endpoint (a la distribuidora que lo acaba de crear) y su lista de
        # cotizaciones (para poder reenviar el mismo enlace sin rearmarlo).
        # Ver `regalos.nueva_clave_de_prellenado`.
        'prefill_key': regalos.nueva_clave_de_prellenado(),
        'folio': (payload.folio or '').strip()[:32],
        'client_name': (payload.client_name or '').strip()[:80],
        # Los otros tres datos del cliente. ⛔ NO salen por `/carrito/{token}`:
        # esa ruta es pública y se arma con la lista blanca de `vista_publica`.
        'client_email': (payload.client_email or '').strip()[:120],
        'client_phone': (payload.client_phone or '').strip()[:40],
        'client_address': (payload.client_address or '').strip()[:200],
        # El domicilio POR CAMPOS (Christián, 2026-08-02): con el CP el envío se
        # cotiza de verdad y el checkout del cliente llega con todo puesto.
        'client_city': (payload.client_city or '').strip()[:80],
        'client_state': (payload.client_state or '').strip()[:60],
        'client_zip': (payload.client_zip or '').strip()[:10],
        'language': (payload.language or dist.get('language') or 'es')[:5],
        'discount_asked': max(0.0, float(payload.discount or 0)),
        'items': [{'product_id': i.product_id, 'quantity': int(i.quantity)}
                  for i in payload.items],
        'gifts': limpios,
        'created_at': ahora,
        'expires_at': vence,
    }
    privado, publico = await _resolver_carrito(doc)
    if not publico['lines']:
        raise HTTPException(status_code=400, detail='Ninguno de esos productos se puede cotizar')
    if limpios and not privado['veredicto']['cabe']:
        # El único "no" de todo esto, y es del lado del distribuidor (no del cliente):
        # el regalo se pasa del margen que este pedido aguanta.
        raise HTTPException(status_code=409, detail={
            'error': 'regalo_sin_margen',
            'entregado': privado['veredicto']['entregado'],
            'permitido': privado['veredicto']['permitido'],
            'exceso': privado['veredicto']['exceso'],
        })
    # LA FOTO DEL MOMENTO, para la LISTA de cotizaciones del panel (Christián,
    # 2026-08-01: «necesito que las cotizaciones generadas se guarden en el panel del
    # distribuidor»). Es sólo para pintar la tabla sin volver a tasar 200 carritos
    # contra el catálogo en cada carga. ⛔ NO es lo que se cobra: al abrir el enlace y
    # al pasar por la caja, el precio se recalcula siempre (ver `_resolver_carrito`).
    doc['snapshot'] = _foto_del_carrito(publico)
    await db[COLECCION_CARRITOS].insert_one(dict(doc))
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    # EL ENLACE LLEVA LA SEGUNDA LLAVE EN EL FRAGMENTO. `#d=` y no `?d=`: el
    # fragmento es la única parte de una dirección que el navegador NO manda al
    # servidor, así que la llave no queda escrita en ningún registro de acceso ni
    # se le filtra a terceros por la cabecera `Referer`.
    clave = doc['prefill_key'] if _hay_datos_privados_del_cliente(doc) else ''
    return {
        'token': doc['token'],
        # `prefill_key` viaja SÓLO a la distribuidora autenticada que acaba de armar
        # el carrito, para que su pantalla pueda pegar el fragmento en los dos
        # enlaces (ver y pagar). Al cliente nunca se le manda por ninguna ruta.
        'prefill_key': clave,
        'url': f"{site}/carrito/{doc['token']}" + (f'#d={clave}' if clave else ''),
        **publico,
    }


def _hay_datos_privados_del_cliente(doc) -> bool:
    """¿Hace falta repartir la segunda llave con este carrito?

    Sólo si hay algo PRIVADO que abrir: correo, teléfono o domicilio. El NOMBRE no
    cuenta — ya viaja en la vista pública desde el primer día («Cotización para
    Ana»), así que pedir un secreto para leerlo sería teatro. Y si no hay nada que
    prellenar, el enlace sale limpio: un secreto que no abre nada es un secreto de
    más rodando por WhatsApp.
    """
    doc = doc or {}
    return any((str(doc.get(k) or '')).strip()
               for k in ('client_email', 'client_phone', 'client_address',
                         'client_city', 'client_state', 'client_zip'))


# Cuántas veces se puede preguntar por los datos de UN carrito. No es una regla de
# negocio: es el freno que impide que alguien con el token en la mano se ponga a
# probar claves. Con 192 bits de clave no le alcanzaría la vida, pero un freno que
# se ve en la bitácora es lo que convierte un intento en un aviso.
PRELLENADOS_POR_HORA = 30
_PRELLENADOS_PEDIDOS = {}


def _puede_pedir_prellenado(token, ahora=None):
    import time as _t
    ahora = ahora if ahora is not None else _t.time()
    marcas = [m for m in _PRELLENADOS_PEDIDOS.get(token, []) if ahora - m < 3600]
    if len(marcas) >= PRELLENADOS_POR_HORA:
        _PRELLENADOS_PEDIDOS[token] = marcas
        return False
    marcas.append(ahora)
    _PRELLENADOS_PEDIDOS[token] = marcas
    return True


@api_router.post('/carrito/{token}/datos')
async def datos_del_carrito_compartido(token: str, payload: PrellenadoRequest,
                                       respuesta: Response):
    """LOS DATOS DEL CLIENTE PARA PRELLENAR SU CHECKOUT. Exige la segunda llave.

    Encargo de Christián (2026-08-01): «Cuando el cliente abre el link de la
    cotización, su nombre, email, teléfono, dirección, NADA se guardó. Necesito que
    corrijas esto si el distribuidor ya lo llenó por él.»

    ⛔ POR QUÉ ESTO NO ES UNA FUGA, Y POR QUÉ NO VA EN `GET /carrito/{token}`:

      1. `GET /carrito/{token}` no cambió ni una coma. Quien pruebe tokens al azar
         saca exactamente lo que sacaba ayer: productos y precios. CERO datos
         personales. Es el antecedente de la casa —el domicilio que salía con sólo
         el número de pedido— y no se repite.
      2. Aquí hacen falta DOS secretos independientes: el token (128 bits, en la
         ruta) y la clave (192 bits, en el fragmento). El fragmento no viaja al
         servidor, así que ni siquiera quien lea los registros de acceso completos
         tiene con qué abrir esto.
      3. Se compara en tiempo constante (`compare_digest`), con freno por token y
         con la respuesta marcada `no-store` para que no quede en ninguna caché.
      4. Sale lo JUSTO: cuatro campos armados desde cero con lista blanca
         (`regalos.datos_de_contacto`). Ni el código del obsequio, ni quién es la
         distribuidora, ni un peso.
      5. Muere con el carrito: vencido (30 días) contesta 410 como el resto.

    Y contesta 404 —no 403— cuando la clave no cuadra: a quien anda probando no se
    le confirma que ese token exista.
    """
    respuesta.headers['Cache-Control'] = 'no-store'
    tok = (token or '').strip()
    clave = (payload.clave or '').strip()
    if not tok or not clave:
        raise HTTPException(status_code=404, detail='No hay datos para ese carrito')
    if not _puede_pedir_prellenado(tok):
        logger.warning('PRELLENADO frenado por ritmo en el carrito %s', tok[:8])
        raise HTTPException(status_code=429, detail='Demasiados intentos. Espera un momento.')
    doc = await db[COLECCION_CARRITOS].find_one({'token': tok}, {'_id': 0})
    if not doc or doc.get('deleted_at'):
        raise HTTPException(status_code=404, detail='No hay datos para ese carrito')
    if doc.get('expires_at') and doc['expires_at'] < now_iso():
        raise HTTPException(status_code=410, detail='Ese carrito ya venció.')
    guardada = str(doc.get('prefill_key') or '')
    if not guardada or not secrets.compare_digest(guardada, clave):
        logger.warning('PRELLENADO con clave que no cuadra en el carrito %s', tok[:8])
        raise HTTPException(status_code=404, detail='No hay datos para ese carrito')
    return regalos.datos_de_contacto(doc)


@api_router.get('/carrito/{token}')
async def abrir_carrito_compartido(token: str):
    """EL CARRITO QUE ABRE EL CLIENTE. Sin sesión, desde su teléfono.

    ⛔ Ruta PÚBLICA a propósito: el cliente de Mónica no tiene cuenta y no se le va a
    pedir una para ver lo que ella le mandó. Lo que sale de aquí es lo mismo que ya
    puede ver cualquiera en el catálogo —productos y precios— más el descuento que su
    distribuidora le dio. Ni un costo, ni un proveedor, ni un margen, ni el código del
    obsequio.
    """
    doc = await db[COLECCION_CARRITOS].find_one({'token': (token or '').strip()}, {'_id': 0})
    # El borrado por la distribuidora mata el enlace: para el cliente es como si
    # nunca hubiera existido (2026-08-01).
    if not doc or doc.get('deleted_at'):
        raise HTTPException(status_code=404, detail='Ese carrito ya no existe')
    if doc.get('expires_at') and doc['expires_at'] < now_iso():
        raise HTTPException(status_code=410, detail='Ese carrito ya venció. Pídele uno nuevo a quien te lo mandó.')
    _privado, publico = await _resolver_carrito(doc)
    return publico


# ==========================================================================
#  MIS COTIZACIONES  —  la lista que se guarda en el panel del distribuidor
# ==========================================================================
# Encargo de Christián (2026-08-01), textual: «necesito que las cotizaciones
# generadas se guarden en el panel del distribuidor por si necesita reenviarlas, que
# no las tenga que volver a generar de cero. Y, una vez pagadas dejan de ser
# cotizaciones y se transforman en ventas.»
#
# ⛔ EL ESTADO NO SE GUARDA: SE DEDUCE. Se mira si existe un pedido con ESTE token
# (`orders.shared_cart_token`) y si ese pedido está cobrado (`cobrado.esta_pagado`,
# la misma respuesta que usan todos los reportes). Guardar un campo `estado` en el
# carrito obligaría a acordarse de moverlo en los CINCO caminos por los que un
# pedido se paga (webhook de tarjeta, de cripto, el admin marcando el SPEI, el
# cambio de estado, el marcado a mano); el día que uno se olvide, el panel enseña
# una cotización que ya se vendió. Deducirlo no se puede desincronizar.
#
# ⛔ Y NO SE DUPLICA: una cotización pagada deja de contar como cotización y sale de
# la lista como VENTA, con su número de pedido — un solo renglón, no dos.
ESTADO_COTIZACION = 'cotizacion'   # todavía es un papel
ESTADO_PEDIDO = 'pedido'           # el cliente ya compró, falta que entre el dinero
ESTADO_VENTA = 'venta'             # cobrado: ya no es cotización

# Cuántas cotizaciones se enseñan. Un distribuidor puede armar 120 al día, así que
# sin tope esto crecería sin fin; con esto se ven las últimas semanas de trabajo.
MAX_COTIZACIONES_EN_LISTA = 200


def _foto_del_carrito(publico) -> dict:
    """La FOTO del carrito para la lista: sólo números, ni un producto ni un código."""
    return {
        'list_total': publico.get('list_total') or 0,
        'discount': publico.get('discount') or 0,
        'discount_rate': publico.get('discount_rate') or 0,
        'shipping': publico.get('shipping') or 0,
        'total': publico.get('total') or 0,
        'lines': len(publico.get('lines') or []),
        'gifts': len(publico.get('gifts') or []),
    }


def _renglon_de_cotizacion(doc, pedido, site):
    """UN renglón de la lista, armado DESDE CERO con lista blanca.

    ⛔ Misma técnica que `regalos.vista_publica`, y por lo mismo: el documento del
    carrito guarda el código interno del obsequio, y un `dict(doc)` con dos llaves
    borradas se filtra el día que alguien guarde un campo nuevo. Aquí lo que no está
    escrito no sale — ni siquiera para la propia distribuidora.
    """
    foto = doc.get('snapshot') or {}
    token = doc.get('token') or ''
    clave = doc.get('prefill_key') or ''
    # El enlace se REARMA aquí, con su fragmento, para que reenviar sea copiar y
    # pegar en vez de volver a generar la cotización desde cero.
    fragmento = f'#d={clave}' if (clave and _hay_datos_privados_del_cliente(doc)) else ''
    pedido = pedido or {}
    hay_pedido = bool(pedido.get('order_number'))
    pagado = esta_pagado(pedido) if hay_pedido else False
    return {
        'token': token,
        'folio': doc.get('folio') or '',
        # Los datos del cliente: SUYOS, los tecleó ella. No se recortan aquí porque
        # esta ruta ya exige su sesión y sólo devuelve SUS carritos.
        **regalos.datos_de_contacto(doc),
        'created_at': doc.get('created_at') or '',
        'expires_at': doc.get('expires_at') or '',
        'vencida': bool(doc.get('expires_at') and doc['expires_at'] < now_iso()),
        'total': foto.get('total') or 0,
        'list_total': foto.get('list_total') or 0,
        'discount': foto.get('discount') or 0,
        'discount_rate': foto.get('discount_rate') or 0,
        'shipping': foto.get('shipping') or 0,
        'lines': foto.get('lines') or 0,
        'gifts': foto.get('gifts') or 0,
        'estado': (ESTADO_VENTA if pagado else ESTADO_PEDIDO) if hay_pedido else ESTADO_COTIZACION,
        'order_number': pedido.get('order_number') or '',
        'order_total': pedido.get('total') or 0,
        'order_status': pedido.get('status') or '',
        'paid_at': pedido.get('paid_at') or '',
        'archivada': bool(doc.get('archived_at')),
        'url': f'{site}/carrito/{token}{fragmento}',
    }


@api_router.get('/distributor/quotes')
async def distributor_quotes(archivadas: int = 0, dist=Depends(get_current_distributor)):
    """MIS COTIZACIONES. Sólo las suyas — nunca las de otro.

    El filtro es `distributor_id` contra el id del token, no un parámetro de la
    dirección: no hay forma de pedir las de otra persona porque no hay dónde
    escribirlo. El "ver como" del admin entra (es de lectura) y ve las del
    distribuidor que está mirando, que es justo lo que se espera de esa herramienta.

    Las BORRADAS no salen jamás; las ARCHIVADAS sólo con `?archivadas=1` — el
    archivo no es un bote de basura, es un cajón (Christián, 2026-08-01).
    """
    docs = await db[COLECCION_CARRITOS].find(
        {'distributor_id': dist['id']}, {'_id': 0}).sort(
        'created_at', -1).to_list(MAX_COTIZACIONES_EN_LISTA)
    # El recorte va aquí y no en la consulta a propósito: son 200 documentos a lo
    # más, y así la regla es una sola línea que se lee (y se prueba) completa.
    docs = [d for d in docs if not d.get('deleted_at')
            and bool(d.get('archived_at')) == bool(archivadas)]
    # LOS CARRITOS DE ANTES DE HOY no traen foto: se les saca una ahora y se guarda,
    # para que esto pase una sola vez por carrito y no en cada carga del panel.
    for doc in docs:
        if not doc.get('snapshot'):
            try:
                _privado, publico = await _resolver_carrito(doc)
            except Exception:
                logger.exception('No se pudo tasar el carrito %s para la lista',
                                 (doc.get('token') or '')[:8])
                continue
            doc['snapshot'] = _foto_del_carrito(publico)
            await db[COLECCION_CARRITOS].update_one(
                {'token': doc.get('token')}, {'$set': {'snapshot': doc['snapshot']}})
    tokens = [d.get('token') for d in docs if d.get('token')]
    pedidos = await db.orders.find(
        {'shared_cart_token': {'$in': tokens}},
        {'_id': 0, 'shared_cart_token': 1, 'order_number': 1, 'total': 1,
         'status': 1, 'paid': 1, 'paid_at': 1}).to_list(len(tokens) + 50) if tokens else []
    # Si un token trajo dos pedidos (el cliente pagó dos veces), manda el COBRADO: la
    # cotización se convirtió en venta y eso es lo que ella tiene que ver.
    por_token = {}
    for p in pedidos:
        tk = p.get('shared_cart_token')
        if tk not in por_token or (esta_pagado(p) and not esta_pagado(por_token[tk])):
            por_token[tk] = p
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    filas = [_renglon_de_cotizacion(d, por_token.get(d.get('token')), site) for d in docs]
    return {
        'quotes': filas,
        'cotizaciones': sum(1 for f in filas if f['estado'] == ESTADO_COTIZACION),
        'pedidos': sum(1 for f in filas if f['estado'] == ESTADO_PEDIDO),
        'ventas': sum(1 for f in filas if f['estado'] == ESTADO_VENTA),
        'vendido': sum(f['order_total'] for f in filas if f['estado'] == ESTADO_VENTA),
    }


class LoteDeCotizaciones(BaseModel):
    """Archivar o borrar UNA O VARIAS cotizaciones de un jalón (Christián,
    2026-08-01: «un checkbox para seleccionar, un archivar y un borrar»)."""
    tokens: List[str]
    accion: str          # 'archivar' | 'borrar' | 'desarchivar'


@api_router.post('/distributor/quotes/lote')
async def lote_de_cotizaciones(payload: LoteDeCotizaciones,
                               dist=Depends(get_current_distributor)):
    """Archiva o borra cotizaciones PROPIAS, seleccionadas en el panel.

    Tres reglas, las tres del lado del servidor:
      · SÓLO LAS SUYAS: cada token se resuelve contra su `distributor_id`; un
        token ajeno simplemente no se encuentra.
      · ⛔ UNA VENTA NO SE BORRA (regla de la casa: lo pagado no se toca). Si el
        token ya tiene un pedido, «borrar» lo ARCHIVA y se le dice cuántas se
        protegieron así — el registro del dinero no se destruye desde un panel.
      · El borrado es SUAVE (`deleted_at`): el enlace muere para el cliente
        (404 al abrirlo) pero el documento queda para auditar el obsequio.
    """
    deny_view_as(dist)
    if payload.accion not in ('archivar', 'borrar', 'desarchivar'):
        raise HTTPException(status_code=400, detail='Acción desconocida')
    tokens = [t.strip() for t in (payload.tokens or []) if t and t.strip()][:MAX_COTIZACIONES_EN_LISTA]
    if not tokens:
        raise HTTPException(status_code=400, detail='No se seleccionó ninguna cotización')
    ahora = now_iso()
    archivadas = borradas = protegidas = desarchivadas = 0
    for tk in tokens:
        doc = await db[COLECCION_CARRITOS].find_one(
            {'token': tk, 'distributor_id': dist['id']}, {'_id': 0, 'token': 1, 'deleted_at': 1})
        if not doc or doc.get('deleted_at'):
            continue
        if payload.accion == 'desarchivar':
            await db[COLECCION_CARRITOS].update_one({'token': tk}, {'$set': {'archived_at': None}})
            desarchivadas += 1
            continue
        con_pedido = bool(await db.orders.find_one({'shared_cart_token': tk}, {'_id': 0, 'id': 1}))
        if payload.accion == 'borrar' and not con_pedido:
            await db[COLECCION_CARRITOS].update_one({'token': tk}, {'$set': {'deleted_at': ahora}})
            borradas += 1
        else:
            await db[COLECCION_CARRITOS].update_one({'token': tk}, {'$set': {'archived_at': ahora}})
            if payload.accion == 'borrar':
                protegidas += 1     # quiso borrar una venta: se archivó en su lugar
            else:
                archivadas += 1
    return {'archivadas': archivadas, 'borradas': borradas,
            'protegidas': protegidas, 'desarchivadas': desarchivadas}


class ReenvioDeCotizacion(BaseModel):
    email: EmailStr
    language: Optional[str] = None


@api_router.post('/distributor/quotes/{token}/email')
async def reenviar_cotizacion(token: str, payload: ReenvioDeCotizacion,
                              dist=Depends(get_current_distributor)):
    """REENVIAR por correo una cotización YA GUARDADA, sin rearmarla.

    Del cuerpo sólo viaja a quién. Qué productos, cuántos y cuánto descuento salen
    del documento que ella guardó, y el PRECIO lo vuelve a poner el servidor contra
    el catálogo de hoy — igual que al armarla. Una cotización de hace tres semanas se
    reenvía con los precios de hoy, que son los que la caja va a cobrar.
    """
    deny_view_as(dist)          # espiar un panel no puede mandar correos desde él
    doc = await db[COLECCION_CARRITOS].find_one(
        {'token': (token or '').strip(), 'distributor_id': dist['id']}, {'_id': 0})
    if not doc:
        raise HTTPException(status_code=404, detail='Esa cotización no existe')
    if not _puede_mandar_cotizacion(dist['id']):
        raise HTTPException(status_code=429,
                            detail='Demasiadas cotizaciones seguidas. Espera un momento.')
    items = [_Renglon(i.get('product_id'), i.get('quantity') or 1)
             for i in (doc.get('items') or [])]
    catalogo = await _catalogo_de(items)
    tope_dist = max(pyramid.discount_tiers_de(dist) or [0])
    lineas, lista_total, total = _armar_cotizacion(
        items, max(0.0, float(doc.get('discount_asked') or 0)), tope_dist, catalogo)
    if not lineas:
        raise HTTPException(status_code=400, detail='Ninguno de esos productos se puede cotizar')
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    clave = doc.get('prefill_key') or ''
    fragmento = f'#d={clave}' if (clave and _hay_datos_privados_del_cliente(doc)) else ''
    # ⛔ El enlace del correo es EL DEL CARRITO GUARDADO, no un `?pedido=` nuevo: así
    # el cliente abre exactamente la misma cotización —con sus cortesías y su
    # prellenado— en vez de un carrito parecido sin nada de eso.
    cotizacion = {
        'folio': doc.get('folio') or '',
        **{k: (doc.get(k) or '') for k in regalos.LLAVES_DE_CONTACTO},
        'code': (dist.get('distributor_code') or '').strip(),
        'link': f'{site}/carrito/{doc.get("token")}{fragmento}',
        'lines': lineas,
        'list_total': lista_total,
        'savings': max(0.0, lista_total - total),
        'total': total,
    }
    salio, motivo = await send_quote_email(
        payload.email, cotizacion,
        language=payload.language or doc.get('language') or dist.get('language'),
        reply_to=ATENCION_CORREO)
    if not salio:
        if motivo == 'apagado':
            raise HTTPException(status_code=503, detail={
                'error': 'correo_apagado',
                'mensaje': ('El envío de correo está apagado en el servidor '
                            '(EMAIL_ENABLED). Comparte por WhatsApp o con el enlace '
                            'del carrito mientras tanto.')})
        raise HTTPException(status_code=502, detail={
            'error': 'correo_rechazado',
            'mensaje': 'El proveedor de correo rechazó el envío. No salió nada.'})
    return {'sent': True, 'total': total, 'lines': len(lineas)}


@api_router.post('/distributor/codes/rotate')
async def rotate_discount_codes(dist=Depends(get_current_distributor)):
    """Renueva YA todos los códigos (nuevos textos).

    ⛔ LOS VIEJOS NO MUEREN (Christián, 2026-07-31). Siguen cobrando —con su mismo
    descuento y atribuyendo al mismo distribuidor— hasta su caducidad natural, y
    salen en `previos` con la fecha en que se apagan solos. Antes esta ruta los
    mataba en el acto y dejaba sin descuento a quien ya traía el código en la
    mano."""
    await _exigir_acuerdo(dist)
    codes = await _ensure_distributor_codes(dist, force_rotate=True)
    previos = await _codigos_jubilados(dist['id'])
    return {'rotated': True,
            'codes': [_code_projection(c) for c in codes],
            'previos': [_code_projection(c) for c in previos]}


async def _rotar_codigo_unico(dist):
    """Cambia el código ÚNICO (legacy) del distribuidor SIN matar el viejo.

    El legacy vive en UN SOLO campo del usuario (`users.distributor_code`), así que
    por construcción no admite dos textos a la vez: sobrescribirlo mata en el acto el
    que los clientes traen en la mano. La salida es MUDARLO DE CASA — se copia a
    `discount_codes` como jubilado, con su mismo descuento y su caducidad de 90
    días— y sólo entonces se escribe el nuevo en el usuario. `_resolve_code` busca
    primero en `discount_codes`, así que el texto viejo sigue cobrando igual y
    atribuyendo al mismo distribuidor; `resolve_distributor` cae por el mismo camino,
    de modo que el enlace `?ref=` viejo tampoco se rompe.

    Devuelve (viejo, nuevo), o None si no había código único que rotar."""
    viejo = (dist.get('distributor_code') or '').strip().upper()
    if not viejo:
        return None
    ahora = now_iso()
    if not await db.discount_codes.find_one({'code': viejo}):
        await db.discount_codes.insert_one({
            'id': str(uuid.uuid4()), 'distributor_id': dist['id'], 'code': viejo,
            # El legacy da el `customer_discount_rate` de la ficha: el mismo que daba
            # ayer. `_resolve_code` lo vuelve a acotar a la comisión del nivel.
            'discount_rate': float(dist.get('customer_discount_rate') or 0),
            'active': True, 'created_at': ahora, 'superseded_at': ahora, 'legacy': True,
            'expires_at': (datetime.now(timezone.utc)
                           + timedelta(days=CODE_TTL_DAYS)).isoformat()})
    nuevo = gen_distributor_code(dist)
    while (await db.users.find_one({'distributor_code': nuevo})
           or await db.discount_codes.find_one({'code': nuevo})):
        nuevo = gen_distributor_code(dist)
    await db.users.update_one({'id': dist['id']}, {'$set': {'distributor_code': nuevo}})
    return viejo, nuevo


@api_router.post('/admin/distributors/{dist_id}/rotate-codes')
async def admin_rotate_distributor_codes(dist_id: str, admin=Depends(get_current_admin)):
    """Rota TODOS los códigos de un distribuidor al prefijo de la casa, con gracia.

    Existe porque la rotación del 2026-07-31 —quitarle el nombre del distribuidor al
    texto del código— tenía que alcanzar las DOS familias, y el distribuidor sólo
    manda sobre una: `/distributor/codes/rotate` renueva los AUTO, pero el código
    ÚNICO legacy (`MARI-3537`, `ALAN-2292`, `JAVI-7116`) vive en su ficha y no había
    ninguna ruta que lo tocara. Sin esto, la mitad de la fuga seguía en pie.

    No mata nada: los viejos de las dos familias quedan vivos hasta su caducidad (ver
    `_ensure_distributor_codes` y `_rotar_codigo_unico`). El acuerdo NO se exige aquí
    —el candado del acuerdo frena lo que crea obligaciones nuevas al distribuidor, y
    esto es una orden de la casa sobre su propia privacidad."""
    dist = await db.users.find_one({'id': dist_id, 'role': 'distributor'},
                                   {'_id': 0, 'password_hash': 0})
    if not dist:
        raise HTTPException(status_code=404, detail='Distribuidor no encontrado')
    unico = await _rotar_codigo_unico(dist)
    fresh = await db.users.find_one({'id': dist_id}, {'_id': 0, 'password_hash': 0})
    codes = await _ensure_distributor_codes(fresh, force_rotate=True)
    previos = await _codigos_jubilados(dist_id)
    return {'rotated': True, 'name': fresh.get('name'),
            'codigo_unico': {'antes': unico[0], 'ahora': unico[1]} if unico else None,
            'codes': [_code_projection(c) for c in codes],
            'previos': [_code_projection(c) for c in previos]}


# ----------------- Acuerdo de Distribuidor: texto, firma y copia -----------------
# ⛔ NADA DE ESTO SE ACTIVA SOLO. Con el interruptor apagado —que es como está
# hoy— estas rutas siguen existiendo pero contestan `requiere_aceptacion: false`
# y el panel del distribuidor no enseña ni una pantalla nueva. Ver acuerdo.py
# para el porqué legal (Código de Comercio arts. 93, 93 Bis y 1298-A).


# ---------------------------------------------------------------------------
# LA CONSTANCIA DEL AVISO DE ENTRADA (RUO). Ver ruo_constancia.py para el porqué:
# hasta hoy el "acepto" vivía SÓLO en el navegador del cliente, o sea que la casa
# no tenía con qué sostener que alguien lo aceptó.
class RuoAceptarInput(BaseModel):
    edad: bool = False
    investigacion: bool = False
    recordar: bool = True
    idioma: str = ''
    # Lo que el navegador dice haber PINTADO en pantalla. Se guarda tal cual para
    # poder reconstruir qué aceptó exactamente: la versión la escribe el backend,
    # pero el texto vive en el i18n del frontend y podía cambiar sin que nadie
    # subiera la versión (hallazgo de la revisión de Codex, 2026-08-03).
    textos: dict = {}


@api_router.post('/ruo/aceptar')
async def ruo_aceptar(payload: RuoAceptarInput, request: Request,
                      user=Depends(get_optional_user)):
    """Deja constancia en el SERVIDOR de que alguien aceptó el aviso de entrada.

    `get_optional_user` a propósito, y es el punto entero: casi todo el mundo
    acepta ANTES de tener cuenta, y ése es justo el caso que hay que poder probar.

    ⛔ NUNCA DEVUELVE ERROR NI BLOQUEA. Si la base falla, contesta 200 con
    `guardada: false` y el visitante entra igual. Un aviso legal que deja a la
    gente afuera cuando se cae Mongo es peor que no tener constancia — y el
    navegador ya guarda su propio rastro, que es lo que le abre la puerta.

    Las dos casillas se exigen aquí también, no sólo en el botón del navegador:
    quien llame a mano puede mandar lo que quiera, y una constancia a medias dice
    que aceptó cuando no aceptó."""
    quien = (user or {}).get('id') if isinstance(user, dict) else getattr(user, 'id', None)
    res = await ruo_constancia.registrar(
        db, request, payload.edad, payload.investigacion, payload.recordar,
        user_id=quien, idioma=payload.idioma, textos=payload.textos)
    # Sólo se le devuelve lo que le importa: cuándo y qué versión. La IP y el
    # user-agent son la prueba de la CASA, no información que el visitante pidió.
    return {'guardada': bool(res.get('guardada')),
            'accepted_at': res.get('accepted_at'),
            'version': ruo_constancia.VERSION,
            'edad_minima': ruo_constancia.EDAD_MINIMA}


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
        code = gen_distributor_code(existing)
        while await db.users.find_one({'distributor_code': code}):
            code = gen_distributor_code(existing)
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
    code = gen_distributor_code(user)
    while await db.users.find_one({'distributor_code': code}):
        code = gen_distributor_code(user)
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


def _corte_de_periodo(periodo: str) -> str:
    """Desde cuándo cuenta un pedido para el filtro de fechas del panel.

    Devuelve un ISO-8601 UTC, o '' cuando se pide TODO. `semana` y `30dias` son
    ventanas móviles (los últimos 7 y 30 días); `mes` y `ano` son de calendario
    (lo que va de ESTE mes y de ESTE año) — si los cuatro fueran móviles, «mes»
    y «30 días» serían el mismo botón dos veces."""
    ahora = datetime.now(timezone.utc)
    p = (periodo or '').strip().lower()
    if p == 'semana':
        return (ahora - timedelta(days=7)).isoformat()
    if p in ('30dias', '30'):
        return (ahora - timedelta(days=30)).isoformat()
    if p == 'mes':
        return ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    if p in ('ano', 'anio', 'año'):
        return ahora.replace(month=1, day=1, hour=0, minute=0,
                             second=0, microsecond=0).isoformat()
    return ''


@api_router.get('/distributor/clients')
async def distributor_clients(periodo: str = 'todo', dist=Depends(get_current_distributor)):
    """Sus clientes con totales POR PERIODO (Christián, 2026-08-01: «totales de
    comisión por cliente, con filtro de fecha: semana, 30 días, mes, año, todo»).

    El filtro recorta los PEDIDOS que se suman, no la lista: un cliente sin
    compras esta semana sigue saliendo, con ceros — desaparecerlo parecería que
    se borró."""
    corte = _corte_de_periodo(periodo)
    users = await db.users.find({'referred_by': dist['id']}, {'_id': 0, 'password_hash': 0}).to_list(5000)
    # Solo pedidos hechos con SU código cuentan (no todo lo que compró el cliente).
    orders = await db.orders.find({'referred_by': dist['id']}, {'_id': 0}).to_list(10000)
    by_user = {}
    for o in orders:
        if o.get('user_id'):
            by_user.setdefault(o['user_id'], []).append(o)
    out = []
    for u in users:
        uo = [o for o in by_user.get(u['id'], []) if esta_vivo(o)
              and (not corte or (o.get('created_at') or '') >= corte)]
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
    # Y los que compraron con su código SIN cuenta: también son suyos. El corte
    # del periodo se aplica a sus pedidos IGUAL que a los de los clientes con
    # cuenta — el invitado se detecta con todos sus pedidos (para no perder su
    # nombre más reciente), pero se le suma sólo lo del periodo.
    con_cuenta = {(u.get('email') or '').strip().lower() for u in users}
    for g in _compradores_invitados(orders, con_cuenta):
        uo = [o for o in g['orders']
              if not corte or (o.get('created_at') or '') >= corte]
        out.append({
            # «Cliente desde» sale de TODOS sus pedidos, no de los del periodo:
            # la fecha en que alguien se volvió cliente no cambia con el filtro.
            'id': g['id'], 'name': g['name'], 'created_at': min(
                (o.get('created_at', '') for o in g['orders']), default=None),
            'guest': True,
            'orders_count': len(uo),
            'total_spent': sum(cobrado_de(o) for o in uo),
            'por_cobrar': sum(por_cobrar_de(o) for o in uo),
            'my_earnings': sum(_my_amount(o, dist['id']) for o in uo),
            'last_order_at': max([o.get('created_at', '') for o in uo], default=None),
        })
    out.sort(key=lambda u: -u['total_spent'])
    return out


# ==========================================================================
#  EL PAGO DE LAS COMISIONES  —  solicitar, deber y pagar (Christián, 2026-08-01)
# ==========================================================================
# «Hoy no hay dónde ver qué se le debe a cada quien ni qué ya se pagó.» Ahora sí:
# el distribuidor ve ganado / pagado / por pagar y SOLICITA su pago; el admin ve
# la deuda de toda la casa y REGISTRA cada pago con su referencia. La aritmética
# y los candados viven en `comisiones.py`, probados sin red.
COLECCION_PAGOS_COMISION = 'commission_payouts'


async def _ganado_de(dist_id: str) -> float:
    """Lo GANADO por un distribuidor, con la MISMA suma que ven sus paneles
    (`pyramid.earnings_for`: vendedor + sobrecomisiones, sólo ventas cobradas)."""
    orders = await db.orders.find(
        {'$or': [{'referred_by': dist_id}, {'commissions.distributor_id': dist_id}]},
        {'_id': 0, 'status': 1, 'paid': 1, 'referred_by': 1,
         'commissions': 1, 'commission': 1}).to_list(10000)
    return pyramid.earnings_for(dist_id, orders)


async def _pagos_de(dist_id: str) -> list:
    docs = await db[COLECCION_PAGOS_COMISION].find(
        {'distributor_id': dist_id}, {'_id': 0}).to_list(500)
    return sorted(docs, key=lambda p: p.get('requested_at') or '', reverse=True)


@api_router.get('/distributor/comisiones')
async def distributor_comisiones(dist=Depends(get_current_distributor)):
    """Su bolsa: ganado, pagado, por pagar, lo solicitado y su historial."""
    ganado = await _ganado_de(dist['id'])
    pagos = await _pagos_de(dist['id'])
    r = comisiones.resumen(ganado, pagos)
    r['historial'] = pagos
    return r


@api_router.post('/distributor/comisiones/solicitar')
async def solicitar_pago_de_comision(payload: SolicitudPagoComision,
                                     dist=Depends(get_current_distributor)):
    """El distribuidor pide su pago. Sin monto = todo su saldo.

    El candado (`comisiones.puede_solicitar`) rebota lo que rebase el saldo y la
    segunda solicitud mientras hay una en camino. Al admin le llega la campanita."""
    deny_view_as(dist)
    ganado = await _ganado_de(dist['id'])
    pagos = await _pagos_de(dist['id'])
    monto = float(payload.amount) if payload.amount else comisiones.por_pagar(ganado, pagos)
    ok, motivo = comisiones.puede_solicitar(monto, ganado, pagos)
    if not ok:
        raise HTTPException(status_code=400, detail=motivo)
    doc = {
        'id': str(uuid.uuid4()),
        'distributor_id': dist['id'],
        'distributor_name': dist.get('name') or '',
        'amount': round(monto),
        'status': comisiones.ESTADO_SOLICITADO,
        'requested_at': now_iso(),
        'requested_by': 'distribuidor',
    }
    await db[COLECCION_PAGOS_COMISION].insert_one(dict(doc))
    try:
        admins = await db.users.find({'role': 'admin'}, {'_id': 0, 'id': 1}).to_list(20)
        for a in admins:
            await notify(a['id'], 'comision_solicitada', 'Solicitud De Pago De Comisión',
                         f'{dist.get("name") or "Un distribuidor"} solicita el pago de '
                         f'${monto:,.0f} de comisiones.', link='/admin?tab=distributors')
    except Exception:
        logger.exception('No se pudo avisar la solicitud de comisión de %s', dist['id'])
    return {'solicitado': True, **doc}


@api_router.get('/admin/comisiones')
async def admin_comisiones(admin=Depends(get_current_admin)):
    """La deuda de toda la casa, distribuidor por distribuidor.

    Una sola pasada por pedidos y pagos — no una consulta por persona — porque
    esta pantalla se abre a diario y a la base no se le pega dos mil veces."""
    dists = await db.users.find({'role': 'distributor'},
                                {'_id': 0, 'id': 1, 'name': 1, 'email': 1,
                                 'distributor_code': 1}).to_list(1000)
    orders = await db.orders.find({}, {'_id': 0, 'status': 1, 'paid': 1,
                                       'referred_by': 1, 'commissions': 1,
                                       'commission': 1}).to_list(20000)
    pagos = await db[COLECCION_PAGOS_COMISION].find({}, {'_id': 0}).to_list(5000)
    pagos_por_dist = {}
    for p in pagos:
        pagos_por_dist.setdefault(p.get('distributor_id'), []).append(p)
    out = []
    for d in dists:
        suyos = pagos_por_dist.get(d['id'], [])
        r = comisiones.resumen(pyramid.earnings_for(d['id'], orders), suyos)
        out.append({'id': d['id'], 'name': d.get('name') or '',
                    'email': d.get('email') or '',
                    'distributor_code': d.get('distributor_code') or '', **r})
    # Primero los que tienen solicitud esperando; luego por deuda, de mayor a menor.
    out.sort(key=lambda x: (0 if x['solicitud_pendiente'] else 1, -x['por_pagar']))
    return {'distribuidores': out,
            'por_pagar_total': round(sum(x['por_pagar'] for x in out)),
            'solicitudes': sum(1 for x in out if x['solicitud_pendiente'])}


@api_router.post('/admin/comisiones/pagar')
async def admin_pagar_comision(payload: RegistroPagoComision,
                               admin=Depends(get_current_admin)):
    """Registra un pago YA HECHO. No mueve dinero: deja el recibo.

    Si hay una solicitud en camino, ese documento se convierte en el recibo (el
    monto pagado manda; el solicitado queda guardado aparte). Sin solicitud, el
    recibo nace directo — Christián puede pagar sin que nadie se lo pida."""
    deny_view_as(admin)
    dist = await db.users.find_one({'id': payload.distributor_id, 'role': 'distributor'},
                                   {'_id': 0, 'id': 1, 'name': 1})
    if not dist:
        raise HTTPException(status_code=404, detail='Ese distribuidor no existe')
    ganado = await _ganado_de(dist['id'])
    pagos = await _pagos_de(dist['id'])
    ok, motivo = comisiones.puede_pagar(payload.amount, ganado, pagos)
    if not ok:
        raise HTTPException(status_code=400, detail=motivo)
    ahora = now_iso()
    pendiente = comisiones.solicitud_pendiente(pagos)
    if pendiente:
        await db[COLECCION_PAGOS_COMISION].update_one(
            {'id': pendiente['id']},
            {'$set': {'status': comisiones.ESTADO_PAGADO,
                      'amount': round(float(payload.amount)),
                      'requested_amount': pendiente.get('amount'),
                      'reference': (payload.reference or '').strip()[:200],
                      'resolved_at': ahora, 'paid_by': admin['id']}})
        recibo_id = pendiente['id']
    else:
        recibo_id = str(uuid.uuid4())
        await db[COLECCION_PAGOS_COMISION].insert_one({
            'id': recibo_id,
            'distributor_id': dist['id'],
            'distributor_name': dist.get('name') or '',
            'amount': round(float(payload.amount)),
            'status': comisiones.ESTADO_PAGADO,
            'requested_at': ahora, 'requested_by': 'admin',
            'reference': (payload.reference or '').strip()[:200],
            'resolved_at': ahora, 'paid_by': admin['id']})
    try:
        await notify(dist['id'], 'comision_pagada', 'Comisión Pagada',
                     f'Se registró el pago de ${float(payload.amount):,.0f} de tus '
                     'comisiones. Revísalo en tu panel.', link='/distribuidor')
    except Exception:
        logger.exception('No se pudo avisar el pago de comisión a %s', dist['id'])
    return {'pagado': True, 'id': recibo_id}


@api_router.post('/admin/comisiones/rechazar')
async def admin_rechazar_solicitud(payload: RechazoPagoComision,
                                   admin=Depends(get_current_admin)):
    """Niega una solicitud, con motivo. El saldo no se toca y el distribuidor
    puede volver a solicitar cuando quiera."""
    deny_view_as(admin)
    doc = await db[COLECCION_PAGOS_COMISION].find_one(
        {'id': payload.payout_id, 'status': comisiones.ESTADO_SOLICITADO}, {'_id': 0})
    if not doc:
        raise HTTPException(status_code=404, detail='Esa solicitud no existe o ya se resolvió')
    await db[COLECCION_PAGOS_COMISION].update_one(
        {'id': doc['id']},
        {'$set': {'status': comisiones.ESTADO_RECHAZADO,
                  'motivo': (payload.motivo or '').strip()[:300],
                  'resolved_at': now_iso(), 'paid_by': admin['id']}})
    try:
        await notify(doc['distributor_id'], 'comision_rechazada', 'Solicitud De Pago Rechazada',
                     (f'Tu solicitud de ${float(doc.get("amount") or 0):,.0f} no procedió.'
                      + (f' Motivo: {payload.motivo.strip()}' if (payload.motivo or '').strip() else '')),
                     link='/distribuidor')
    except Exception:
        logger.exception('No se pudo avisar el rechazo a %s', doc['distributor_id'])
    return {'rechazado': True}


# ==========================================================================
#  LA SOLICITUD DE GUÍA  —  el distribuidor pide, Christián aprueba (2026-08-03)
# ==========================================================================
# «Un botón "solicitar guía" junto al cliente al que le falte número de guía,
# siempre y cuando ya haya pagado.» El distribuidor NUNCA dispara la compra —
# una guía cuesta dinero de verdad —; la aprobación de Christián es la compuerta,
# y la compra es `comprar_guia_del_pedido`, el MISMO camino del pago automático,
# para que el correo al cliente y los frenos de gasto sean los mismos.
# Los candados viven en `guia_solicitudes.py`, probados sin red.
COLECCION_SOLICITUDES_GUIA = 'label_requests'


@api_router.post('/distributor/orders/{order_number}/solicitar-guia')
async def distribuidor_solicitar_guia(order_number: str,
                                      dist=Depends(get_current_distributor)):
    """El distribuidor pide la guía de un pedido SUYO, pagado y sin guía.

    No se compra nada aquí: se deja la solicitud y la campanita al admin. El
    candado de «es SU pedido» es el mismo del detalle: `referred_by` o 403."""
    deny_view_as(dist)
    o = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    if o.get('referred_by') != dist['id']:
        raise HTTPException(status_code=403, detail='Ese pedido no es tuyo')
    previas = await db[COLECCION_SOLICITUDES_GUIA].find(
        {'order_id': o['id']}, {'_id': 0}).to_list(50)
    ok, motivo = guia_solicitudes.puede_solicitar(o, previas)
    if not ok:
        raise HTTPException(status_code=400, detail=motivo)
    doc = {
        'id': str(uuid.uuid4()),
        'order_id': o['id'],
        'order_number': o.get('order_number', ''),
        'distributor_id': dist['id'],
        'distributor_name': dist.get('name') or '',
        'status': guia_solicitudes.ESTADO_SOLICITADA,
        'requested_at': now_iso(),
    }
    await db[COLECCION_SOLICITUDES_GUIA].insert_one(dict(doc))
    try:
        admins = await db.users.find({'role': 'admin'}, {'_id': 0, 'id': 1}).to_list(20)
        for a in admins:
            await notify(a['id'], 'guia_solicitada', 'Solicitud De Guía De Envío',
                         f'{dist.get("name") or "Un distribuidor"} solicita la guía '
                         f'del pedido {o.get("order_number")}.',
                         link='/admin?tab=orders',
                         dedup=f'guia-solicitud:{o.get("order_number")}')
    except Exception:
        logger.exception('No se pudo avisar la solicitud de guía de %s', dist['id'])
    return {'solicitado': True, **doc}


@api_router.get('/admin/guia-solicitudes')
async def admin_guia_solicitudes(admin=Depends(get_current_admin)):
    """Las solicitudes de guía, las pendientes primero. Cada una con el estado
    ACTUAL de su pedido — pagado y con o sin guía — porque entre solicitar y
    aprobar el pedido puede cambiar, y aprobar es gastar."""
    docs = await db[COLECCION_SOLICITUDES_GUIA].find({}, {'_id': 0}).to_list(1000)
    ids = list({d.get('order_id') for d in docs})
    pedidos = {o['id']: o for o in await db.orders.find(
        {'id': {'$in': ids}}, {'_id': 0, 'id': 1, 'status': 1, 'paid': 1,
                               'total': 1, 'tracking_number': 1}).to_list(1000)} if ids else {}
    out = []
    for d in docs:
        o = pedidos.get(d.get('order_id')) or {}
        out.append({**d, 'order_status': o.get('status', ''),
                    'order_paid': esta_pagado(o) if o else False,
                    'order_total': o.get('total', 0),
                    'order_tracking': o.get('tracking_number', '')})
    # Las más nuevas arriba y, encima de todo, las que esperan respuesta.
    out.sort(key=lambda x: x.get('requested_at') or '', reverse=True)
    out.sort(key=lambda x: 0 if x['status'] == guia_solicitudes.ESTADO_SOLICITADA else 1)
    return {'solicitudes': out,
            'pendientes': sum(1 for x in out
                              if x['status'] == guia_solicitudes.ESTADO_SOLICITADA)}


@api_router.post('/admin/guia-solicitudes/aprobar')
async def admin_aprobar_solicitud_guia(payload: AprobarSolicitudGuia,
                                       admin=Depends(get_current_admin)):
    """Aprueba una solicitud: COMPRA la guía por el mismo camino del pago
    automático (`comprar_guia_del_pedido`, con su correo al cliente, su candado
    de doble compra y sus frenos de gasto) y asigna el número al pedido.

    ⚠️ CUESTA DINERO DE VERDAD. Si un freno detiene la compra (tope de gasto,
    sin empaque, paquetería caída), la solicitud SE QUEDA PENDIENTE y el error
    se devuelve tal cual: aprobar sin comprar sería mentirle al distribuidor."""
    deny_view_as(admin)
    s = await db[COLECCION_SOLICITUDES_GUIA].find_one(
        {'id': payload.solicitud_id, 'status': guia_solicitudes.ESTADO_SOLICITADA},
        {'_id': 0})
    if not s:
        raise HTTPException(status_code=404,
                            detail='Esa solicitud no existe o ya se resolvió')
    o = await db.orders.find_one({'id': s['order_id']}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='El pedido de la solicitud ya no existe')
    ahora = now_iso()
    if (o.get('tracking_number') or '').strip():
        # Alguien la compró por otro camino entre solicitar y aprobar: no se
        # compra dos veces; la solicitud se resuelve con la guía que ya hay.
        await db[COLECCION_SOLICITUDES_GUIA].update_one(
            {'id': s['id']},
            {'$set': {'status': guia_solicitudes.ESTADO_APROBADA,
                      'tracking_number': o['tracking_number'],
                      'nota': 'El pedido ya tenía guía al aprobar',
                      'resolved_at': ahora, 'resolved_by': admin['id']}})
        await _avisar_guia_al_distribuidor(s, o['tracking_number'])
        return {'aprobada': True, 'tracking_number': o['tracking_number'],
                'ya_tenia_guia': True}
    if not esta_pagado(o):
        raise HTTPException(status_code=409, detail='Ese pedido ya no figura como pagado')
    if not envios.COMPRAR_GUIA_AL_PAGAR:
        raise HTTPException(status_code=400,
                            detail='La compra automática de guías está apagada; '
                                   'cómprala a mano desde el pedido')
    update = await comprar_guia_del_pedido(o, avisar=True)
    if not update or not update.get('tracking_number'):
        # El porqué quedó escrito en el pedido por `comprar_guia_del_pedido`
        # (label_hold/label_error) y Christián ya tiene su aviso detallado.
        fresco = await db.orders.find_one({'id': o['id']},
                                          {'_id': 0, 'label_hold': 1, 'label_error': 1})
        motivo = ((fresco or {}).get('label_error')
                  or (fresco or {}).get('label_hold') or 'la compra no procedió')
        raise HTTPException(status_code=502,
                            detail=f'La guía no se pudo comprar: {motivo}. '
                                   'La solicitud sigue pendiente.'[:300])
    await db[COLECCION_SOLICITUDES_GUIA].update_one(
        {'id': s['id']},
        {'$set': {'status': guia_solicitudes.ESTADO_APROBADA,
                  'tracking_number': update['tracking_number'],
                  'resolved_at': ahora, 'resolved_by': admin['id']}})
    await _avisar_guia_al_distribuidor(s, update['tracking_number'])
    return {'aprobada': True, 'tracking_number': update['tracking_number']}


async def _avisar_guia_al_distribuidor(solicitud: dict, numero: str):
    """La campanita de vuelta: al que solicitó le llega el número. El CLIENTE ya
    recibió su correo por `comprar_guia_del_pedido`; esto es el otro sentido."""
    try:
        await notify(solicitud['distributor_id'], 'guia_aprobada', 'Guía Generada',
                     f'La guía del pedido {solicitud.get("order_number")} ya está: '
                     f'{numero}. Tu cliente recibió el aviso con el rastreo.',
                     link='/distribuidor')
    except Exception:
        logger.exception('No se pudo avisar la guía al distribuidor %s',
                         solicitud.get('distributor_id'))


@api_router.post('/admin/guia-solicitudes/rechazar')
async def admin_rechazar_solicitud_guia(payload: RechazoSolicitudGuia,
                                        admin=Depends(get_current_admin)):
    """Niega una solicitud, con motivo. No se compra nada y el distribuidor
    puede volver a solicitar cuando quiera."""
    deny_view_as(admin)
    s = await db[COLECCION_SOLICITUDES_GUIA].find_one(
        {'id': payload.solicitud_id, 'status': guia_solicitudes.ESTADO_SOLICITADA},
        {'_id': 0})
    if not s:
        raise HTTPException(status_code=404,
                            detail='Esa solicitud no existe o ya se resolvió')
    await db[COLECCION_SOLICITUDES_GUIA].update_one(
        {'id': s['id']},
        {'$set': {'status': guia_solicitudes.ESTADO_RECHAZADA,
                  'motivo': (payload.motivo or '').strip()[:300],
                  'resolved_at': now_iso(), 'resolved_by': admin['id']}})
    try:
        await notify(s['distributor_id'], 'guia_rechazada', 'Solicitud De Guía Rechazada',
                     (f'La solicitud de guía del pedido {s.get("order_number")} no procedió.'
                      + (f' Motivo: {payload.motivo.strip()}'
                         if (payload.motivo or '').strip() else '')),
                     link='/distribuidor')
    except Exception:
        logger.exception('No se pudo avisar el rechazo de guía a %s', s.get('distributor_id'))
    return {'rechazado': True}


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
    # Sus solicitudes de guía pendientes, de un jalón: el botón «Solicitar guía»
    # necesita saber si ya hay una en camino para no ofrecerse dos veces.
    solicitadas = {s.get('order_id') for s in await db[COLECCION_SOLICITUDES_GUIA].find(
        {'distributor_id': dist['id'],
         'status': guia_solicitudes.ESTADO_SOLICITADA},
        {'_id': 0, 'order_id': 1}).to_list(500)}
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
            # Para el botón «Solicitar guía»: sólo pagado y sin guía se puede
            # pedir, y si ya se pidió el botón dice «Solicitada» y se apaga.
            'paid': esta_pagado(o),
            'guia_solicitada': o.get('id') in solicitadas,
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
        # ⛔ IMPRIMIR LA GUÍA SÍ ES DE LOS DOS (Christián, 2026-07-31: «quiero manejar
        # TODO desde nuestra app»). Lo que NO viaja al distribuidor es la URL cruda del
        # proveedor —eso es la cuenta de envíos de la casa—: él pide el PDF por
        # `/distributor/orders/{numero}/etiqueta` y el servidor se lo sirve.
        #
        # Es un SÍ/NO, no la liga, y dice «hay guía comprada por nosotros», no «hay
        # PDF»: el papel puede tardar unos segundos en publicarse y aun así el botón
        # tiene que estar (lo rescata solo). En cambio una guía TECLEADA a mano no
        # tiene PDF que traer, y por eso el botón no aparece: prometer un papel que
        # no existe es peor que no ofrecerlo.
        'tiene_etiqueta': bool(o.get('label_url')
                               or (o.get('label_provider') and o.get('tracking_number'))),
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
        # El mismo aseo que el chat: esta explicación también la escribe el
        # modelo y también se lee en pantalla (ver texto_ia.py).
        text = texto_ia.limpiar(await interpret_lab_report(context))
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
        # El texto sale LIMPIO de Markdown suelto (Christián, 2026-07-31: un
        # cliente no puede ver `**NAD+ 500**` en la tienda). Y se guarda limpio,
        # para que el historial de ayer tampoco se relea sucio.
        limpieza = texto_ia.LimpiezaEnVivo()
        try:
            async for chunk in stream_reply(chat, full_message):
                pedazo = limpieza.alimentar(chunk)
                if pedazo:
                    collected += pedazo
                    yield pedazo
            cola = limpieza.cerrar()
            if cola:
                collected += cola
                yield cola
        except Exception as e:
            logger.error(f'AI chat error: {e}')
            # Mensaje honesto en vez de un error tecnico: el usuario sabe que es
            # demanda, no su culpa. El texto vive en `modelo_ia.AVISOS`, en los
            # tres idiomas y SIN nombrar al proveedor — ver el porque alli.
            clase = modelo_ia.clase_de_error(e)
            # Al cliente le da igual si falta una llave: para el es lo mismo que
            # una caida. Lo de la llave se le dice a Christian en el panel.
            if clase == 'sin_llave':
                clase = 'generico'
            err = modelo_ia.aviso('tienda', clase, payload.language)
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
# vuelta re-manda el catálogo entero, y una vuelta larga se paga por token en
# cualquier proveedor.
NEGOCIO_HISTORIAL = 8


@api_router.post('/business/chat')
async def business_chat(payload: ChatInput, user=Depends(get_current_distributor)):
    deny_view_as(user)
    # El mismo catálogo que ve el sitio, más los campos con los que se calcula el
    # tope. `tope_de_descuento` recorta a un número: por ahí no se asoma un costo.
    catalog = await db.products.find(
        {}, {'_id': 0, 'name': 1, 'price': 1, 'category': 1, 'stock': 1, 'presentation': 1,
             'id': 1, 'sku': 1, 'commission_cap': 1, 'distributor_eligible': 1, 'hidden': 1},
    ).to_list(1000)
    # La pregunta viaja al armador: con ella elige QUÉ fichas de compuesto adjunta
    # (las 95 no caben en la ventana). Ver `chat_negocio.bloque_compuestos`.
    chat = await chat_negocio.armar_contexto(
        db, user, catalog, tope_de=tope_de_descuento, language=payload.language,
        pregunta=payload.message)

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
        limpieza = texto_ia.LimpiezaEnVivo()      # mismo aseo que el chat del sitio
        try:
            async for chunk in stream_reply(chat, historia + payload.message):
                pedazo = limpieza.alimentar(chunk)
                if pedazo:
                    collected += pedazo
                    yield pedazo
            cola = limpieza.cerrar()
            if cola:
                collected += cola
                yield cola
        except Exception as e:
            logger.error(f'Chat de negocio: {e}')
            # Sin llave o sin cuota NO se truena: se degrada con un mensaje claro.
            # El asesor es una ayuda, no la caja — que se caiga en silencio con un
            # error técnico en pantalla es peor que decir qué pasó. Aquí SÍ se
            # distingue lo de la llave: quien lee esto es de la casa y puede
            # arreglarlo. Los textos, en los tres idiomas, en `modelo_ia.AVISOS`.
            err = modelo_ia.aviso('panel', modelo_ia.clase_de_error(e), payload.language)
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
# Handle del barrido de carritos, para poder cancelarlo al apagar.
_TAREA_RECUPERACION = None


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
    # ⛔ SE GUARDA LA TAREA PARA PODER MATARLA. Sin esto el bucle queda vivo y
    # dormido 15 minutos, y al apagar la app el grupo de tareas de anyio lo
    # ESPERA: en producción no se nota (el proceso muere entero), pero en las
    # pruebas cada `with TestClient(app)` colgaba la corrida hasta un cuarto de
    # hora. Se veía como un pytest "trabado" al 36%, sin error y sin pista, y
    # aparecía sólo a veces — la peor clase de falla, porque se le echa la culpa
    # al cambio de quien pasaba por ahí. Ver `apagar_recuperacion`.
    global _TAREA_RECUPERACION
    _TAREA_RECUPERACION = asyncio.create_task(bucle())


# ⛔ EL REINTENTO. Cuando la compra de la guía falla por algo pasajero —la cuenta se
# quedó sin saldo y Christián la recargó, la API de la paquetería tuvo un mal rato— no
# tiene sentido que un pedido pagado espere a que alguien se acuerde de entrar al Panel.
# Cada 10 minutos se vuelve a intentar lo que falló.
#
# ⛔ SÓLO REINTENTA LOS FALLOS, NUNCA LOS FRENOS. Un pedido detenido por `label_hold`
# —sin empaque, sobre el tope, o esperando a estar completo— está esperando una
# DECISIÓN, no otra oportunidad: reintentarlo sería llenarle el correo a Christián con
# el mismo aviso cada diez minutos sin que nada cambie.
#
# Y se rinde a los 6 intentos (una hora). Después de eso ya no es un problema pasajero
# y el aviso urgente que él recibió es lo que corresponde, no un bucle eterno gastando
# llamadas a la paquetería.
MAX_INTENTOS_GUIA = 6


async def _reintentar_guias_pendientes() -> int:
    """Vuelve a intentar la compra de las guías que FALLARON. Devuelve cuántas salieron."""
    if not envios.COMPRAR_GUIA_AL_PAGAR:
        return 0
    pendientes = await db.orders.find({
        'label_error': {'$nin': ['', None]},
        'tracking_number': {'$in': [None, '']},
        'label_lock': {'$ne': True},
        'status': {'$in': list(ESTADOS_PAGADOS)},
        'label_intentos': {'$lt': MAX_INTENTOS_GUIA},
    }, {'_id': 0}).to_list(50)
    salieron = 0
    for pedido in pendientes:
        # `avisar=False`: el cliente ya recibió su correo de pago confirmado cuando esto
        # falló la primera vez, así que si ahora sí sale, el rastreo es un evento nuevo
        # y lo manda `avisar_del_envio` — una sola vez, por la ranura del correo.
        try:
            update = await comprar_guia_del_pedido(pedido, avisar=False)
        except Exception:
            logger.exception('Reintento: fallo la guia de %s', pedido.get('order_number'))
            continue
        if update:
            salieron += 1
            logger.info('Reintento: por fin salio la guia de %s', pedido.get('order_number'))
            await avisar_del_envio(dict(pedido, **update))
    return salieron


@app.on_event('startup')
async def arrancar_reintento_de_guias():
    """Cada 10 minutos reintenta las guías que no se pudieron comprar."""
    async def bucle():
        while True:
            await asyncio.sleep(600)
            try:
                await _reintentar_guias_pendientes()
            except Exception:
                logger.exception('Fallo el reintento de guias pendientes')
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
async def apagar_recuperacion():
    """El barrido de carritos se cancela ANTES de cerrar nada más.

    Es el par de `arrancar_recuperacion`: su bucle duerme 900 segundos entre
    vuelta y vuelta, y una tarea dormida que nadie cancela deja el apagado
    esperandola."""
    tarea = _TAREA_RECUPERACION
    if tarea is not None and not tarea.done():
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('El barrido de carritos murio raro al apagar')


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

# Los tres aparatos, en palabras que Christián lee sin traducir. Nada más entra:
# lo que llegue raro cae en '' (desconocido) y se cuenta aparte, nunca se inventa.
DEVICES = ('telefono', 'tableta', 'computadora')

# Ancho máximo que se cree. Arriba de esto es un monitor gigante o un navegador
# mintiendo; se recorta en vez de rechazar el evento, porque medir NUNCA debe
# tirar un dato bueno por culpa de uno raro.
SCREEN_MAX = 10000


@api_router.post('/events')
async def track_event(payload: TrackEvent):
    """Registra un paso del embudo. Publico y anonimo: sirve para saber si la
    gente que llega (sobre todo de publicidad) esta comprando o donde se cae.

    🔒 LO QUE ESTE ENDPOINT NO GUARDA, A PROPÓSITO. No toca `Request`: no ve ni
    guarda la IP, ni el User-Agent, ni ninguna cabecera. Lo único nuevo desde el
    2026-07-31 es AGREGADO —categoría de aparato y ancho de pantalla— que es lo
    que Christián autorizó para poder decidir con datos. Sin huella digital.
    """
    if payload.type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail='Tipo de evento no valido')
    doc = payload.model_dump()
    # El navegador ya manda el aparato clasificado; aquí sólo se comprueba que sea
    # uno de los tres. Un valor inventado en el navegador no puede crear una
    # categoría nueva en el panel y ensuciar el corte que se va a comparar.
    doc['device'] = doc.get('device') if doc.get('device') in DEVICES else ''
    try:
        doc['screen_w'] = max(0, min(SCREEN_MAX, int(doc.get('screen_w') or 0)))
    except (TypeError, ValueError):
        doc['screen_w'] = 0
    doc['ref_code'] = str(doc.get('ref_code') or '').strip().upper()[:40]
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


# ================= ¿LAS CONVERSACIONES DE WHATSAPP VENDEN? =================
#
# ⛔ EL AGUJERO QUE ESTO TAPA (Christián, 2026-07-31). La semana del 25 al 31 de
# julio: $237 USD gastados, 110 conversaciones de WhatsApp a $39 MXN cada una, y
# CERO compras atribuidas por Meta. Las 3 ventas reales llegaron por otro lado.
# Nadie sabía si esas 110 conversaciones se volvieron ventas — y sin saberlo,
# subir o bajar el presupuesto es adivinar.
#
# POR QUÉ UN CÓDIGO Y NO UN ENLACE. Una conversación de WhatsApp no tiene URL
# donde pegar un `utm`, no deja `fbclid` y no comparte cookie con el sitio: para
# Meta y para el píxel, esa venta nace de la nada. El cupón es lo ÚNICO que viaja
# con la persona desde el chat hasta el carrito.
#
# POR QUÉ REUTILIZABLE Y NO UNO POR CONVERSACIÓN. Se consideró un código distinto
# por cada chat (atribución perfecta: conversación ↔ venta). Se descartó porque a
# 110 conversaciones por semana obliga a Mónica a administrar un inventario de
# códigos dentro de WhatsApp Web, y el día que se equivoque de renglón la venta se
# atribuye al chat de otra persona — o sea, un dato peor que ninguno. Un código
# por ANUNCIO, siempre el mismo, no se puede teclear mal y contesta la pregunta
# que importa: ¿de qué anuncio salió la venta? La granularidad por conversación
# queda disponible (`cantidad > 0`) para cuando haya volumen que la justifique.
#
# 🔒 EL PREFIJO ES DE LA CASA. `WA-` + el anuncio. Nunca el nombre de quien lo
# reparte: los códigos dejaron de delatar al distribuidor el 2026-07-31, y éstos
# nacen ya cumpliendo esa regla.
WA_PREFIJO = 'WA'


def _texto_wa(campana: str, mes: str = '') -> str:
    """`WA-RETA-JUL`: prefijo de la casa, anuncio, mes.

    Corto a propósito: lo teclea un cliente en el carrito después de leerlo en un
    chat, y cada letra de más es un cupón que «no funciona» y una venta que se cae
    sin que nadie se entere. Ocho letras del anuncio es el techo — si el anuncio se
    llama largo, conviene nombrarlo corto al crearlo ('Reta' mejor que
    'Retatrutida julio 2026'). Es la misma forma que `director.py` ya le dicta a la
    IA para los anuncios de WhatsApp (`WA-RETA-JUL`).
    """
    base = marketing.slug(campana).upper().replace('-', '')[:8] or 'GRAL'
    return '-'.join(x for x in (WA_PREFIJO, base, mes.upper()[:4]) if x)


class WhatsAppCode(BaseModel):
    campana: str                    # el anuncio: 'Retatrutida', 'Asesoria'
    discount_rate: float = 0.10
    expires_days: int = 30
    # 0 = UN código de campaña, reutilizable (lo normal: Mónica pega el mismo en
    # todos los chats de ese anuncio). N = N códigos de un solo uso, uno por
    # conversación, para cuando se quiera el detalle fino.
    cantidad: int = 0
    min_order: float = 0            # compra mínima, si se quiere poner piso
    mes: str = ''                   # 'JUL' — para no reciclar el código del mes pasado
    note: str = ''


@api_router.post('/admin/marketing/whatsapp/codigos')
async def admin_crear_codigos_whatsapp(payload: WhatsAppCode,
                                       admin=Depends(get_current_marketing)):
    """Crea el (o los) código(s) que Mónica reparte en el chat de un anuncio."""
    if not (payload.campana or '').strip():
        raise HTTPException(status_code=400, detail='Dime de qué anuncio es el código.')
    # Mismo techo que todo lo demás. Un cupón de WhatsApp no es una puerta trasera
    # al 50%: `tasa_de_cupon` lo volvería a topar al cobrar, y un panel que promete
    # un descuento que el checkout no da es peor que no tener panel.
    rate = max(0.05, min(TECHO_DESCUENTO, payload.discount_rate))
    expira = (datetime.now(timezone.utc)
              + timedelta(days=max(1, payload.expires_days))).isoformat()
    base = _texto_wa(payload.campana, payload.mes)
    cuantos = max(0, min(500, int(payload.cantidad or 0)))
    ahora = now_iso()

    def _doc(code, single):
        return {
            'id': str(uuid.uuid4()), 'code': code, 'kind': 'coupon',
            'user_id': None,                 # se reparte en un chat, no por cuenta
            'discount_rate': rate,
            'min_order': float(payload.min_order or 0),
            'active': True, 'used': False,
            # ⛔ `single_use: False` en el código de campaña. `_apartar_cupon` no lo
            # quema, así que sirve las veces que haga falta — y por eso mismo NO
            # puede dejar rastro en `used_order`. Su atribución vive en el pedido
            # (`orders.coupon_code`), que es lo que se añadió hoy.
            'single_use': bool(single),
            'campana_wa': marketing.slug(payload.campana),
            'note': (payload.note or f'Conversaciones de WhatsApp · {payload.campana}')[:200],
            'created_by': 'whatsapp', 'created_at': ahora, 'expires_at': expira,
        }

    codigos = []
    if cuantos:
        for _ in range(cuantos):
            code = f'{base}-{"".join(random.choices(string.ascii_uppercase + string.digits, k=4))}'
            while await db.discount_codes.find_one({'code': code}):
                code = f'{base}-{"".join(random.choices(string.ascii_uppercase + string.digits, k=4))}'
            doc = _doc(code, True)
            await db.discount_codes.insert_one(doc)
            codigos.append(code)
    else:
        # Ya existe el de este anuncio: se devuelve el mismo en vez de crear un gemelo.
        # Si no, cada clic del botón partiría las ventas del mismo anuncio en dos filas.
        ya = await db.discount_codes.find_one({'code': base, 'active': True}, {'_id': 0, 'code': 1})
        if not ya:
            await db.discount_codes.insert_one(_doc(base, False))
        codigos.append(base)

    return {'codigos': codigos, 'discount_rate': rate, 'expires_at': expira,
            'reutilizable': not cuantos,
            # El renglón listo para copiar y pegar en el chat.
            'mensaje': (f'Te dejo {codigos[0]} para que apliques {round(rate * 100)}% '
                        f'de descuento en exygenlabs.com')}


def _enlaces_de_retatrutida():
    """Los enlaces que Christián pega en Meta para mandar a la FICHA, no a la portada.

    ⛔ POR QUÉ LA FICHA. Retatrutida se lleva 68 de las 118 vistas de producto de la
    semana (58%). Mandar ese clic a la portada le cobra al visitante dos toques más
    para llegar a lo que venía buscando, y ahí es donde se cae el embudo: de visita a
    ficha sólo pasa el 8.7%.

    ⛔ Y POR QUÉ ETIQUETADOS. Un enlace sin `utm` cae en «sin etiquetar» para siempre
    y esa campaña nunca podrá tener un costo por cliente. `{{site_source_name}}` es la
    macro oficial de Meta: se rellena sola con `fb` / `ig` / `msg`, que es lo que
    `marketing.es_de_meta` ya reconoce. Christián los pega; aquí no se toca Meta.
    """
    base = f'{SITE_URL.rstrip("/")}/producto/retatrutida/'
    comun = 'utm_source={{site_source_name}}&utm_medium=paid'
    return [
        {'para': 'Anuncio de tráfico a la ficha de Retatrutida',
         'url': f'{base}?{comun}&utm_campaign=retatrutida-ficha&utm_content={{{{ad_id}}}}'},
        {'para': 'Anuncio de Reels (el más barato esta semana)',
         'url': f'{base}?{comun}&utm_campaign=retatrutida-reels&utm_content={{{{ad_id}}}}'},
        {'para': 'Campo «Parámetros de URL del sitio web» (sólo los parámetros)',
         'url': f'{comun}&utm_campaign=retatrutida-ficha&utm_content={{{{ad_id}}}}'},
    ]


@api_router.get('/admin/marketing/whatsapp')
async def admin_whatsapp(days: int = 7, admin=Depends(get_current_marketing)):
    """¿Las conversaciones de WhatsApp se vuelven ventas? Cierra el círculo.

    Conversaciones (Meta) → códigos entregados → códigos usados → pesos cobrados.
    """
    desde = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    filas, estado = await _meta_filas(days)
    conversaciones = sum(f.get('conversaciones', 0) for f in filas)
    gasto = sum(float(f.get('spend', 0) or 0) for f in filas)

    cupones = await db.discount_codes.find(
        {'created_by': 'whatsapp'}, {'_id': 0}).to_list(5000)
    # Las ventas se buscan por el TEXTO escrito en el pedido. Es lo que hace que un
    # código de campaña reutilizable pueda demostrar que vendió: antes del
    # 2026-07-31 el único rastro era `used_order`, que sólo llenan los de un uso.
    textos = [c['code'] for c in cupones if c.get('code')]
    pedidos = await db.orders.find(
        {'coupon_code': {'$in': textos}, 'created_at': {'$gte': desde},
         'status': {'$nin': list(NO_CUENTAN)}},
        {'_id': 0, 'order_number': 1, 'coupon_code': 1, 'total': 1, 'status': 1,
         'paid': 1, 'first_order': 1}).to_list(5000) if textos else []

    grupos = {}
    for c in cupones:
        campana = c.get('campana_wa') or 'sin anuncio'
        g = grupos.setdefault(campana, {
            'campana': campana, 'codigos': [], 'entregados': 0, 'usados': 0,
            'pedidos': 0, 'clientes_nuevos': 0, 'cobrado_mxn': 0.0, 'por_cobrar_mxn': 0.0,
            'reutilizable': False, 'vigente_hasta': ''})
        g['entregados'] += 1
        if not c.get('single_use', True):
            g['reutilizable'] = True
        if c.get('code') not in g['codigos']:
            g['codigos'].append(c.get('code'))
        g['vigente_hasta'] = max(g['vigente_hasta'], c.get('expires_at') or '')

    por_codigo = {c['code']: (c.get('campana_wa') or 'sin anuncio') for c in cupones if c.get('code')}
    usados_por_campana = {}
    for o in pedidos:
        campana = por_codigo.get(o.get('coupon_code'))
        g = grupos.get(campana)
        if not g:
            continue
        g['pedidos'] += 1
        g['cobrado_mxn'] += cobrado_de(o)
        g['por_cobrar_mxn'] += por_cobrar_de(o)
        if o.get('first_order'):
            g['clientes_nuevos'] += 1
        usados_por_campana.setdefault(campana, set()).add(o.get('coupon_code'))
    for campana, g in grupos.items():
        g['usados'] = len(usados_por_campana.get(campana, ()))
        g['cobrado_mxn'] = round(g['cobrado_mxn'])
        g['por_cobrar_mxn'] = round(g['por_cobrar_mxn'])

    filas_wa = sorted(grupos.values(), key=lambda g: -g['cobrado_mxn'])
    ventas = sum(g['pedidos'] for g in filas_wa)
    cobrado = sum(g['cobrado_mxn'] for g in filas_wa)
    return {
        **estado,
        'dias': days,
        'conversaciones': conversaciones,
        'gasto_usd': round(gasto, 2),
        'costo_conversacion_usd': round(gasto / conversaciones, 2) if conversaciones else 0,
        'campanas': filas_wa,
        'ventas': ventas,
        'cobrado_mxn': cobrado,
        # ⛔ DE CADA 100 CONVERSACIONES, CUÁNTAS COMPRARON. Es LA cifra que decide si
        # se sube o se baja el presupuesto de WhatsApp. Cero con códigos repartidos
        # significa que no venden; cero SIN códigos repartidos no significa nada —por
        # eso `medible` viaja al lado y el panel no deja confundir las dos cosas.
        'conversion': round(ventas / conversaciones * 100, 2) if conversaciones else 0,
        'medible': bool(filas_wa),
        'enlaces_retatrutida': _enlaces_de_retatrutida(),
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


# =====================================================================
#  ARCHIVO DE REPORTES SEMANALES DE PUBLICIDAD
# =====================================================================
#  Orden de Christián (2026-07-31): "este video de publicidad debe estar en mi
#  página de Marketing e irse archivando por fecha, semana con semana".
#
#  El video NO va en git (15 MB × 52 semanas = 780 MB al año, y git no olvida).
#  Vive en disco del servidor y sólo sale por estas rutas, que exigen sesión de
#  quien lleva la difusión. No hay carpeta pública ni URL adivinable: son datos
#  del negocio (gasto, ventas, embudo). Ver reportes_ads.py.
# =====================================================================

@api_router.get('/admin/marketing/reportes')
async def admin_reportes_ads(admin=Depends(get_current_marketing)):
    """El archivo completo: cada semana con sus cifras, de la más nueva a la más
    vieja. Las cifras viajan aunque el video no exista, porque comparar semanas
    es para lo que sirve de verdad este archivo."""
    reportes = reportes_ads.listar()
    return {
        'reportes': reportes,
        'almacen': reportes_ads.almacen(reportes),
        'retencion': reportes_ads.retencion(),
        'cifras': list(reportes_ads.CIFRAS),
    }


@api_router.get('/admin/marketing/reportes/{semana}/texto')
async def admin_reporte_ads_texto(semana: str, admin=Depends(get_current_marketing)):
    """El reporte escrito que acompaña al video, en Markdown."""
    d = reportes_ads.uno(semana)
    if not d:
        raise HTTPException(status_code=404, detail='No hay reporte de esa semana.')
    return {'semana': semana, 'markdown': reportes_ads.texto_de(semana)}


async def _usuario_de_token_de_video(token: str):
    """Quién es el dueño de un token que viaja por la URL.

    La etiqueta <video> del navegador no manda headers, así que el token de
    sesión va como query — igual que en /api/tutorials. Que el token viaje por
    la URL no afloja el candado: se valida la firma, se relee al usuario de la
    base (por si lo bloquearon) y se le exige el rol.
    """
    import jwt as _jwt
    from auth import JWT_SECRET, JWT_ALGORITHM
    try:
        payload = _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get('sub')
    except Exception:
        raise HTTPException(status_code=401, detail='No autenticado')
    user = await db.users.find_one({'id': user_id},
                                   {'_id': 0, 'role': 1, 'blocked': 1, 'extra_roles': 1})
    if not user or user.get('blocked'):
        raise HTTPException(status_code=401, detail='No autenticado')
    if user.get('role') not in ('admin', 'marketing') \
            and 'marketing' not in (user.get('extra_roles') or []):
        raise HTTPException(status_code=403, detail='Sólo para administradores o marketing')
    return user


@api_router.get('/admin/marketing/reportes/{semana}/video')
async def admin_reporte_ads_video(semana: str, request: Request, token: str = Query(...),
                                  descargar: int = 0):
    """El MP4 de una semana. Con soporte de rangos (Safari exige 206 para <video>)."""
    await _usuario_de_token_de_video(token)
    path = reportes_ads.ruta_video(semana)
    if path is None:
        raise HTTPException(status_code=404, detail='No hay video de esa semana.')
    size = path.stat().st_size
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'private, max-age=3600'}
    if descargar:
        headers['Content-Disposition'] = f'attachment; filename="{reportes_ads.nombre_descarga(semana)}"'
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


@api_router.post('/admin/marketing/reportes')
async def admin_publicar_reporte_ads(
    semana: str = Form(...),
    datos: str = Form(...),
    video: UploadFile = File(None),
    texto: str = Form(None),
    admin=Depends(get_current_admin),
):
    """PUBLICAR EL REPORTE DE LA SEMANA. Esto es lo que llama el pipeline.

    Multipart: `semana` (2026-W31), `datos` (JSON con fechas, duración, resumen y
    cifras), `video` (el MP4, opcional) y `texto` (el reporte escrito, opcional).
    Publicar dos veces la misma semana la REEMPLAZA, para que una corrida repetida
    no llene el archivo de duplicados.
    """
    deny_view_as(admin)
    try:
        d = json.loads(datos)
        if not isinstance(d, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail='`datos` tiene que ser un JSON.')
    contenido = await video.read() if video is not None else None
    if contenido is not None and not contenido:
        contenido = None
    try:
        guardado = reportes_ads.publicar(semana, d, video=contenido, texto=texto)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info('Reporte de publicidad archivado: %s', semana)
    return {'ok': True, 'reporte': guardado, 'almacen': reportes_ads.almacen()}


class RetencionReportes(BaseModel):
    semanas: int


@api_router.put('/admin/marketing/reportes/retencion')
async def admin_retencion_reportes(payload: RetencionReportes, admin=Depends(get_current_admin)):
    """Cuántas semanas conservar. Cambiarlo NO borra nada: lo que se pase de la
    raya sale marcado como "por vencer" en el panel y se borra a mano."""
    deny_view_as(admin)
    try:
        return reportes_ads.guardar_retencion(int(payload.semanas))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_router.delete('/admin/marketing/reportes/{semana}')
async def admin_borrar_reporte_ads(semana: str, admin=Depends(get_current_admin)):
    """Borrado MANUAL de una semana. Ningún temporizador llama aquí."""
    deny_view_as(admin)
    if not reportes_ads.borrar(semana):
        raise HTTPException(status_code=404, detail='No hay reporte de esa semana.')
    return {'ok': True, 'almacen': reportes_ads.almacen()}


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


# Cajones de ancho de pantalla. No son arbitrarios: 375 es el iPhone de siempre y
# 390-430 los actuales, así que el corte de 480 separa «teléfono» de todo lo demás
# sin partir a los teléfonos en dos. 1024 es donde la portada deja de apilarse.
ANCHOS = (
    ('hasta 480 px (teléfono)', 0, 480),
    ('481-768 px (teléfono ancho / tableta)', 481, 768),
    ('769-1024 px (tableta / laptop chica)', 769, 1024),
    ('más de 1024 px (monitor)', 1025, 10 ** 9),
)


def _embudo_por_dispositivo(evs):
    """El embudo partido en teléfono / tableta / computadora, y los anchos.

    ⛔ EL DISPOSITIVO ES DE LA SESIÓN, NO DEL EVENTO. Una misma sesión manda cinco
    eventos y todos traen el mismo aparato, así que contar eventos multiplicaría por
    cinco a quien navega mucho. Se resuelve UNA vez por sesión —con el primer evento
    que sí lo diga— y ese aparato manda para todos sus pasos. Es la misma regla que
    ya usa `sesion_origen` para el utm, y por lo mismo: si no, el teléfono que sólo
    mira la portada y el escritorio que recorre seis fichas pesarían igual.

    Devuelve (filas por dispositivo, filas por ancho, sesiones sin dispositivo).
    """
    ses_device, ses_ancho = {}, {}
    for e in sorted(evs, key=lambda x: x.get('created_at', '')):
        sid = e.get('session_id')
        if not sid:
            continue
        d = e.get('device')
        if d in DEVICES and sid not in ses_device:
            ses_device[sid] = d
        w = e.get('screen_w') or 0
        if w and sid not in ses_ancho:
            ses_ancho[sid] = int(w)

    filas = []
    for d in DEVICES:
        pasos = {t: set() for t in EVENT_TYPES}
        for e in evs:
            sid = e.get('session_id')
            if ses_device.get(sid) != d:
                continue
            if e.get('type') in pasos:
                pasos[e['type']].add(sid)
        vis = len(pasos['visit'])
        filas.append({
            'dispositivo': d,
            'visitas': vis,
            'embudo': [{'paso': t, 'sesiones': len(pasos[t])} for t in EVENT_TYPES],
            # EL número que Christián va a comparar: hoy, sumando todo, es 8.7%.
            'visita_a_ficha': round(len(pasos['product_view']) / vis * 100, 1) if vis else 0,
            'conversion': round(len(pasos['purchase']) / vis * 100, 2) if vis else 0,
        })
    filas.sort(key=lambda f: -f['visitas'])

    anchos = []
    for etiqueta, lo, hi in ANCHOS:
        n = sum(1 for w in ses_ancho.values() if lo <= w <= hi)
        anchos.append({'rango': etiqueta, 'sesiones': n})

    # Sesiones que existen pero no dicen aparato: las de antes de que esto se midiera.
    todas = {e.get('session_id') for e in evs if e.get('session_id')}
    return filas, anchos, len(todas - set(ses_device))


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

    por_dispositivo, anchos, sin_dispositivo = _embudo_por_dispositivo(evs)

    return {
        'dias': days,
        'embudo': embudo,
        'conversion_total': round(compras / visitas * 100, 2),
        # ⛔ EL CORTE QUE FALTABA (Christián, 2026-07-31). El 8.7% de visita→ficha es un
        # PROMEDIO de teléfonos y computadoras juntos, y lo que se cambió esta semana
        # —adelgazar la portada móvil— sólo se ve en la mitad de teléfono. Sin este
        # desglose no hay forma honesta de decir si sirvió.
        'por_dispositivo': por_dispositivo,
        # Cómo de ancha es la pantalla de quien entra: ¿la portada se está viendo a
        # 375 px o a 1,400? Responde la pregunta de para cuál diseñar primero.
        'anchos': anchos,
        # ⛔ LA LÍNEA HONESTA. Las sesiones de ANTES de que se midiera el aparato
        # (todo lo anterior al 2026-07-31) no traen dispositivo, y meterlas en
        # 'computadora' sería inventar. Se cuentan aparte y a la vista: mientras
        # este número sea grande, el corte por dispositivo todavía no se puede
        # comparar contra el 8.7% de la semana pasada.
        'sin_dispositivo': sin_dispositivo,
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
        #
        # ⛔ TAMBIÉN POR EL CORREO ALTERNO. Se buscaba sólo por `email`, así que quien
        # compró como invitado con la dirección que después quedó de `alt_emails` seguía
        # abriendo una ficha aparte — la misma persona, dos fichas. La puerta de entrada
        # (`_usuario_por_correo`) mira las dos desde la fusión de cuentas de la casa;
        # aquí también.
        cuenta = await _usuario_por_correo(correo)
        if cuenta:
            cuenta = {k: v for k, v in cuenta.items()
                      if k not in ('_id', 'password_hash', 'totp_secret')}
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

    # ⛔ EL INTERRUPTOR DE CONTACTO TAMBIÉN MANDA AQUÍ (Christián, 2026-07-31). La ficha
    # del pedido (`_detalle_de_pedido`) y el autollenado (`/cotizador/clientes`) ya
    # recortaban correo, teléfono y domicilio al distribuidor SIN el interruptor; esta
    # ruta —que se abre desde ocho lugares— los mandaba enteros. Un distribuidor que
    # abría a su propio cliente se llevaba su contacto completo, justo lo que la regla
    # del 2026-07-23 prohíbe. Lo que no se puede ver, NO VIAJA: se recorta aquí, en el
    # servidor, no en la pantalla.
    #
    # Lo que NUNCA se recorta: el NOMBRE (nunca fue secreto) y sus pedidos CON ÉL, que
    # son de lo que vive el distribuidor.
    ve_contacto = es_admin or ve_datos_del_cliente(user)
    if ve_contacto:
        persona['phones'] = telefonos
        persona['addresses'] = domicilios
    else:
        persona.pop('email', None)
        persona['phones'] = []
        persona['addresses'] = []

    ficha = {
        'scope': 'admin' if es_admin else 'distributor',
        'client': persona,
        'orders': [_fila_de_pedido_de_ficha(o, dist_id) for o in vivos[:100]],
        'totals': {
            'orders_count': len(vivos),
            'paid_total': sum(cobrado_de(o) for o in vivos),
            'paid_count': len(solo_cobrados(vivos)),
            'por_cobrar': sum(por_cobrar_de(o) for o in vivos),
            # PRIMERA y ÚLTIMA compra. La última sola no dice nada: «desde cuándo es
            # cliente» y «hace cuánto no vuelve» son dos preguntas distintas, y las dos
            # se contestan mirando este par.
            'first_order_at': min((o.get('created_at', '') for o in vivos), default=None),
            'last_order_at': max((o.get('created_at', '') for o in vivos), default=None),
        },
        # Lo que suele llevar: la respuesta a «¿qué le ofrezco?» sin abrir pedido por
        # pedido. Al distribuidor sólo le cuenta lo que le compró A ÉL — y sólo si tiene
        # el interruptor: «qué compuestos compró su cliente» estaba en la misma lista de
        # lo prohibido del 2026-07-23 que el correo y el teléfono.
        'top_products': _lo_que_suele_llevar(vivos) if ve_contacto else [],
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
# Las rutas del PDF de la guía traen su propio prefijo `/api` y sus propios candados.
app.include_router(etiquetas.router)
# El rastreo del pedido, público por número de pedido igual que la ficha.
app.include_router(rastreo.router)


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=['*'],
    allow_headers=['*'],
)
