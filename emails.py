import os
import html
import asyncio
import logging
from pathlib import Path

import boto3
import requests

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / 'templates'
SUPPORTED_LANGUAGES = {'es', 'en', 'pt'}
DEFAULT_LANGUAGE = 'es'

WELCOME_SUBJECTS = {
    'es': 'Tu cuenta en Exygen Labs está lista',
    'en': 'Your Exygen Labs account is ready',
    'pt': 'Sua conta na Exygen Labs está pronta',
}

# ⛔ QUIEN ATIENDE AL CLIENTE ES LA CASA, NUNCA EL DISTRIBUIDOR
# (orden de Christián, 2026-07-31). El cliente final no puede enterarse de que el
# código que usó es de María, de Alanís o de quien sea: eso es su lista de
# clientes, y con un nombre y un correo se la puede llevar cualquiera.
#
# Por eso todo correo que ve un cliente firma con ESTA persona —la atención de la
# casa— y la respuesta cae en ESTE buzón. El distribuidor se entera por dentro
# (la campanita del panel), no exponiéndose en el correo.
ATENCION_NOMBRE = 'Mónica Flores'
ATENCION_CORREO = os.environ.get('ATENCION_EMAIL', 'hola@exygenlabs.com')


def normalize_language(language):
    lang = (language or DEFAULT_LANGUAGE).lower().strip()[:2]
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def email_enabled() -> bool:
    """Si el envio esta apagado no podemos exigir confirmacion de correo:
    dejaria fuera a todo el que se registre. El servidor lo consulta antes
    de bloquear un login."""
    return os.environ.get('EMAIL_ENABLED', 'false').lower() == 'true'


def _sender():
    return os.environ.get('EMAIL_FROM', 'Exygen Labs <hola@exygenlabs.com>')


def _send_via_ses(to_address, subject, html_body, reply_to=None):
    region = os.environ.get('SES_REGION', 'us-east-1')
    ses = boto3.client('sesv2', region_name=region)
    extra = {'ReplyToAddresses': [reply_to]} if reply_to else {}
    ses.send_email(
        FromEmailAddress=_sender(),
        Destination={'ToAddresses': [to_address]},
        Content={'Simple': {
            'Subject': {'Data': subject, 'Charset': 'UTF-8'},
            'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'}},
        }},
        **extra,
    )


def _send_via_resend(to_address, subject, html_body, reply_to=None):
    """Resend por HTTP. No necesita SDK y no tiene sandbox que pedir."""
    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        raise RuntimeError('RESEND_API_KEY is not configured.')
    cuerpo = {'from': _sender(), 'to': [to_address], 'subject': subject, 'html': html_body}
    if reply_to:
        cuerpo['reply_to'] = reply_to
    resp = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json=cuerpo,
        timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f'Resend {resp.status_code}: {resp.text[:300]}')


PROVIDERS = {'ses': _send_via_ses, 'resend': _send_via_resend}


def _send_email_sync(to_address, subject, html_body, reply_to=None):
    """Despacha al proveedor configurado. `EMAIL_PROVIDER` = ses | resend.

    ⛔ EL REMITENTE NUNCA CAMBIA: sale de `EMAIL_FROM` (el dominio de Exygen, que
    es el que está autenticado con SPF/DKIM). `reply_to` sólo cambia a dónde va la
    RESPUESTA — así el cliente que contesta una cotización le contesta a su
    distribuidor, sin que el correo salga suplantando su dominio y caiga en spam.
    """
    name = os.environ.get('EMAIL_PROVIDER', 'ses').strip().lower()
    send = PROVIDERS.get(name)
    if not send:
        raise RuntimeError(f'EMAIL_PROVIDER desconocido: {name}')
    send(to_address, subject, html_body, reply_to=reply_to)


RESET_SUBJECTS = {
    'es': 'Restablece tu contrasena de Exygen Labs',
    'en': 'Reset your Exygen Labs password',
    'pt': 'Redefina sua senha da Exygen Labs',
}

RESET_BODIES = {
    'es': ('Hola, {name}:', 'Recibimos una solicitud para restablecer tu contrasena. '
           'Tu usuario es <strong>{email}</strong>. Haz clic en el boton (valido por 1 hora):',
           'Restablecer contrasena', 'Si no fuiste tu, ignora este correo; tu cuenta sigue segura.'),
    'en': ('Hi {name},', 'We received a request to reset your password. '
           'Your username is <strong>{email}</strong>. Click the button (valid for 1 hour):',
           'Reset password', "If this wasn't you, ignore this email; your account remains safe."),
    'pt': ('Ola, {name}:', 'Recebemos uma solicitacao para redefinir sua senha. '
           'Seu usuario e <strong>{email}</strong>. Clique no botao (valido por 1 hora):',
           'Redefinir senha', 'Se nao foi voce, ignore este e-mail; sua conta continua segura.'),
}


VERIFY_SUBJECTS = {
    'es': 'Confirma tu correo para activar tu cuenta',
    'en': 'Confirm your email to activate your account',
    'pt': 'Confirme seu e-mail para ativar sua conta',
}

VERIFY_BODIES = {
    'es': ('Hola, {name}:', 'Ya casi. Confirma que <strong>{email}</strong> es tuyo para dejar tu cuenta '
           'lista. El enlace vence en 24 horas.',
           'Confirmar mi correo', 'Si no creaste esta cuenta, ignora este correo y no pasara nada.'),
    'en': ('Hi {name},', 'Almost there. Confirm that <strong>{email}</strong> is yours to finish setting up '
           'your account. The link expires in 24 hours.',
           'Confirm my email', "If you didn't create this account, just ignore this email."),
    'pt': ('Ola, {name}:', 'Quase la. Confirme que <strong>{email}</strong> e seu para deixar sua conta '
           'pronta. O link expira em 24 horas.',
           'Confirmar meu e-mail', 'Se voce nao criou esta conta, ignore este e-mail.'),
}

INVITE_SUBJECTS = {
    'es': 'Te invitamos a Exygen Labs: activa tu cuenta',
    'en': "You're invited to Exygen Labs: activate your account",
    'pt': 'Convite para a Exygen Labs: ative sua conta',
}

# Nunca mandamos contrasenas por correo: el enlace lleva a que la elija el mismo,
# y al hacerlo queda confirmado el correo de un solo golpe.
INVITE_BODIES = {
    'es': ('Hola, {name}:', 'Te creamos una cuenta en Exygen Labs con el correo <strong>{email}</strong>. '
           'Elige tu contrasena para activarla; con eso queda confirmado tu correo. El enlace vence en 7 dias.',
           'Activar mi cuenta', 'Si crees que esta invitacion no era para ti, escribenos a hola@exygenlabs.com.'),
    'en': ('Hi {name},', 'We created an Exygen Labs account for <strong>{email}</strong>. '
           'Choose your password to activate it; that also confirms your email. The link expires in 7 days.',
           'Activate my account', "If you think this invitation wasn't for you, write to hola@exygenlabs.com."),
    'pt': ('Ola, {name}:', 'Criamos uma conta na Exygen Labs com o e-mail <strong>{email}</strong>. '
           'Escolha sua senha para ativa-la; isso tambem confirma seu e-mail. O link expira em 7 dias.',
           'Ativar minha conta', 'Se acha que este convite nao era para voce, escreva para hola@exygenlabs.com.'),
}


def _action_email_html(greet, body, cta, footer, name, email, link):
    """Plantilla comun de los correos con un boton de accion. Documento
    completo para que el bloque de modo oscuro (DARK_EMAIL_STYLE) aplique."""
    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0;padding:0;background-color:#FFFFFF;">
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div class="em-ink" style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div class="em-muted" style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <p class="em-body" style="font-size:15px;color:#3D4657;margin-top:28px;">{greet.format(name=html.escape(name))}</p>
      <p class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;">{body.format(email=html.escape(email))}</p>
      <p style="text-align:center;margin:28px 0;">
        <a href="{link}" class="em-btn" style="display:inline-block;background-color:#132763;color:#FFFFFF;font-size:15px;font-weight:bold;text-decoration:none;padding:14px 36px;border-radius:999px;">{cta}</a>
      </p>
      <p class="em-muted" style="font-size:13px;color:#8A93A8;line-height:1.6;word-break:break-all;">
        Si el boton no funciona, copia y pega este enlace:<br>{html.escape(link)}
      </p>
      <p class="em-muted" style="font-size:13px;color:#8A93A8;line-height:1.6;">{footer}</p>
    </div>
</body>
</html>"""


async def _send_action_email(name, email, link, language, subjects, bodies, kind):
    """Envia un correo con boton. Nunca lanza: el alta no debe fallar por el correo."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        logger.info('EMAIL_ENABLED != true, skipping %s email to %s', kind, email)
        return
    lang = normalize_language(language)
    body_html = _action_email_html(*bodies[lang], name=name, email=email, link=link)
    try:
        await asyncio.to_thread(_send_email_sync, email, subjects[lang], body_html)
        logger.info('%s email sent to %s (lang=%s)', kind, email, lang)
    except Exception:
        logger.exception('Failed to send %s email to %s', kind, email)


def admin_notify_address():
    """A dónde van los avisos internos. Configurable, con el correo que Christián lee.

    Estaba clavado en `hola@exygenlabs.com`, que es el buzón de la tienda: los avisos que
    tienen que hacer que ALGUIEN SE MUEVA se perdían entre los correos de clientes.
    Christián los quiere en su cuenta (2026-07-30)."""
    return (os.environ.get('ADMIN_NOTIFY_EMAIL') or 'exygenlabs@gmail.com').strip()


async def send_admin_notification(subject, html_body):
    """Aviso interno para Christian. Nunca lanza y calla si el correo saliente está
    apagado — el flujo que avisa no debe fallar por esto."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        return
    try:
        await asyncio.to_thread(_send_email_sync, admin_notify_address(), subject, html_body)
    except Exception:
        logger.exception('Failed to send admin notification: %s', subject)


async def send_reset_email(name, email, link, language=None):
    """Correo de restablecimiento de contrasena."""
    await _send_action_email(name, email, link, language, RESET_SUBJECTS, RESET_BODIES, 'reset')


async def send_verification_email(name, email, link, language=None):
    """Confirmacion de correo tras registrarse. Sin esto no se puede entrar."""
    await _send_action_email(name, email, link, language, VERIFY_SUBJECTS, VERIFY_BODIES, 'verification')


async def send_invitation_email(name, email, link, language=None):
    """Invitacion a un cliente o distribuidor creado desde el admin."""
    await _send_action_email(name, email, link, language, INVITE_SUBJECTS, INVITE_BODIES, 'invitation')


# ---------- Bienvenida al programa de distribuidores ----------
DIST_SUBJECTS = {
    'es': 'Bienvenido al programa de distribuidores de Exygen Labs',
    'en': 'Welcome to the Exygen Labs distributor program',
    'pt': 'Bem-vindo ao programa de distribuidores da Exygen Labs',
}

# (saludo, intro, como_funciona[3 puntos], etiqueta_codigo, cta_activar, cta_panel,
#  nota_activar, nota_panel, cierre). Nunca ponemos el % de comision aqui: se ve
# en el panel y Christian la ajusta a mano.
DIST_COPY = {
    'es': {
        'greet': 'Hola, {name}:',
        'intro': 'Ya eres parte del programa de distribuidores de <strong>Exygen Labs</strong>. '
                 'Este es tu código de referido: compártelo con tus clientes.',
        'how': ['Tus clientes usan tu código al comprar y reciben su descuento.',
                'Cada venta hecha con tu código te genera comisión.',
                'En tu panel ves tu comisión, tus ventas, tus clientes y tus materiales.'],
        'code_label': 'TU CÓDIGO DE REFERIDO',
        'cta_activate': 'Activar mi cuenta',
        'cta_panel': 'Entrar a mi panel',
        'note_activate': 'Primero elige tu contraseña con el botón de arriba; con eso activas tu cuenta. El enlace vence en 7 días.',
        'note_panel': 'Entra a tu panel de distribuidor con el botón de arriba.',
        'close': 'Cualquier duda, escríbenos a hola@exygenlabs.com.',
    },
    'en': {
        'greet': 'Hi {name},',
        'intro': "You're now part of the <strong>Exygen Labs</strong> distributor program. "
                 'This is your referral code — share it with your clients.',
        'how': ['Your clients use your code at checkout and get their discount.',
                'Every sale made with your code earns you commission.',
                'Your dashboard shows your commission, sales, clients and materials.'],
        'code_label': 'YOUR REFERRAL CODE',
        'cta_activate': 'Activate my account',
        'cta_panel': 'Go to my dashboard',
        'note_activate': 'First choose your password with the button above; that activates your account. The link expires in 7 days.',
        'note_panel': 'Open your distributor dashboard with the button above.',
        'close': 'Any questions, write to hola@exygenlabs.com.',
    },
    'pt': {
        'greet': 'Olá, {name}:',
        'intro': 'Agora você faz parte do programa de distribuidores da <strong>Exygen Labs</strong>. '
                 'Este é o seu código de indicação — compartilhe com seus clientes.',
        'how': ['Seus clientes usam seu código na compra e recebem o desconto.',
                'Cada venda feita com seu código gera comissão para você.',
                'No seu painel você vê sua comissão, vendas, clientes e materiais.'],
        'code_label': 'SEU CÓDIGO DE INDICAÇÃO',
        'cta_activate': 'Ativar minha conta',
        'cta_panel': 'Entrar no meu painel',
        'note_activate': 'Primeiro escolha sua senha no botão acima; isso ativa sua conta. O link expira em 7 dias.',
        'note_panel': 'Acesse seu painel de distribuidor no botão acima.',
        'close': 'Qualquer dúvida, escreva para hola@exygenlabs.com.',
    },
}


def _distributor_email_html(copy, name, code, link, needs_activation):
    """Correo propio del distribuidor: bienvenida + su código de referido en una
    caja destacada + botón (activar cuenta nueva, o entrar al panel si ya existe)."""
    how_items = ''.join(
        f'<li style="margin-bottom:8px;">{h}</li>' for h in copy['how']
    )
    cta = copy['cta_activate'] if needs_activation else copy['cta_panel']
    note = copy['note_activate'] if needs_activation else copy['note_panel']
    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0;padding:0;background-color:#FFFFFF;">
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div class="em-ink" style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div class="em-muted" style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <p class="em-body" style="font-size:15px;color:#3D4657;margin-top:28px;">{copy['greet'].format(name=html.escape(name))}</p>
      <p class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;">{copy['intro']}</p>
      <div class="em-card" style="margin:24px 0;padding:20px;border:2px solid #132763;border-radius:12px;text-align:center;background-color:#F5F7FC;">
        <div class="em-muted" style="font-size:11px;letter-spacing:2px;color:#8A93A8;">{copy['code_label']}</div>
        <div class="em-ink" style="font-size:28px;font-weight:bold;letter-spacing:2px;color:#132763;padding-top:6px;">{html.escape(code)}</div>
      </div>
      <ul class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;padding-left:20px;">{how_items}</ul>
      <p style="text-align:center;margin:28px 0;">
        <a href="{link}" class="em-btn" style="display:inline-block;background-color:#132763;color:#FFFFFF;font-size:15px;font-weight:bold;text-decoration:none;padding:14px 36px;border-radius:999px;">{cta}</a>
      </p>
      <p class="em-muted" style="font-size:13px;color:#8A93A8;line-height:1.6;word-break:break-all;">
        {note}<br>{html.escape(link)}
      </p>
      <p class="em-muted" style="font-size:13px;color:#8A93A8;line-height:1.6;">{copy['close']}</p>
    </div>
</body>
</html>"""


NEWS_SUBJECT = {'es': 'Novedad de Exygen Labs', 'en': 'News from Exygen Labs', 'pt': 'Novidade da Exygen Labs'}


def _news_email_html(name, title, body):
    safe_body = html.escape(body).replace('\n', '<br>')
    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0;padding:0;background-color:#FFFFFF;">
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div class="em-ink" style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div class="em-muted" style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <h1 class="em-ink" style="font-size:20px;color:#132763;margin-top:28px;">{html.escape(title)}</h1>
      <p class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;">{safe_body}</p>
      <p class="em-muted" style="font-size:12px;color:#8A93A8;line-height:1.6;margin-top:24px;">exygenlabs.com</p>
    </div>
</body>
</html>"""


def _recovery_email_html(name, items, oferta, code, site='https://exygenlabs.com'):
    """Correo de 'se te quedo el carrito'. Con cupon arriba de $2,500; abajo, solo
    un recordatorio. El cupon dice CLARO que exige comprar el mismo monto o mas."""
    saludo = f'Hola {html.escape(name)},' if name else 'Hola,'
    lista = ''.join(
        f'<tr><td class="em-body" style="font-size:14px;color:#3D4657;padding:4px 0;">'
        f'{html.escape(str(i.get("name", "")))}</td>'
        f'<td class="em-body" align="right" style="font-size:14px;color:#3D4657;padding:4px 0;">'
        f'x{int(i.get("quantity", 1))}</td></tr>'
        for i in (items or [])[:12])
    if code:
        minimo = _money(oferta.get('min_order', 0))
        bloque = f"""
      <div style="border:1px solid #132763;border-radius:12px;padding:20px;margin-top:24px;text-align:center;">
        <div class="em-muted" style="font-size:12px;letter-spacing:2px;color:#8A93A8;">TU CUPON</div>
        <div class="em-ink" style="font-size:26px;font-weight:bold;color:#132763;padding:8px 0;letter-spacing:2px;">{html.escape(code)}</div>
        <div class="em-body" style="font-size:15px;color:#3D4657;">
          {int(round(oferta['rate'] * 100))}% de descuento + {html.escape(oferta.get('perk_text', ''))}
        </div>
        <div class="em-muted" style="font-size:12px;color:#8A93A8;padding-top:10px;line-height:1.5;">
          Valido en compras de {minimo} o mas, por 7 dias.<br>
          Algunos productos admiten menos descuento por su margen.
        </div>
      </div>"""
    else:
        bloque = ('<p class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;">'
                  'Si tienes alguna duda sobre estos compuestos, respondenos este correo '
                  'y con gusto te orientamos.</p>')
    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0;padding:0;background-color:#FFFFFF;">
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div class="em-ink" style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div class="em-muted" style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <h1 class="em-ink" style="font-size:20px;color:#132763;margin-top:28px;">Se te quedo el carrito</h1>
      <p class="em-body" style="font-size:15px;color:#3D4657;line-height:1.6;">{saludo} guardamos lo que tenias listo:</p>
      <table width="100%" cellpadding="0" cellspacing="0">{lista}</table>
      {bloque}
      <p style="text-align:center;margin-top:28px;">
        <a href="{site}/carrito" style="background:#132763;color:#FFFFFF;text-decoration:none;padding:13px 28px;border-radius:8px;font-size:15px;display:inline-block;">Retomar mi compra</a>
      </p>
      <p class="em-muted" style="font-size:12px;color:#8A93A8;line-height:1.6;margin-top:24px;">
        Productos para uso en investigacion (RUO). exygenlabs.com
      </p>
    </div>
</body>
</html>"""


async def send_cart_recovery_email(name, email, items, oferta, code=None):
    """UNA sola oferta por carrito abandonado. Best-effort; nunca lanza."""
    if not email:
        return
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        logger.info('EMAIL_ENABLED != true, no mando recuperacion a %s', email)
        return
    asunto = 'Se te quedo el carrito en Exygen Labs'
    if code:
        asunto = f'Tu cupon {code} — se te quedo el carrito'
    try:
        await asyncio.to_thread(_send_email_sync, email, asunto,
                                _recovery_email_html(name or '', items, oferta, code))
    except Exception:
        logger.exception('Failed to send cart recovery email to %s', email)


async def send_news_email(name, email, title, body, language=None):
    """Aviso del centro de noticias por correo. Best-effort; nunca lanza."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        return
    lang = normalize_language(language)
    try:
        await asyncio.to_thread(_send_email_sync, email, NEWS_SUBJECT[lang], _news_email_html(name or '', title, body))
    except Exception:
        logger.exception('Failed to send news email to %s', email)


async def send_distributor_welcome_email(name, email, code, link, language=None, needs_activation=True):
    """Bienvenida propia del distribuidor con su código de referido. `needs_activation`
    True = cuenta nueva (el botón activa y elige contraseña); False = cliente convertido
    (el botón lleva a su panel, ya tiene contraseña). Nunca lanza."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        logger.info('EMAIL_ENABLED != true, skipping distributor email to %s', email)
        return
    lang = normalize_language(language)
    body_html = _distributor_email_html(DIST_COPY[lang], name=name, code=code, link=link, needs_activation=needs_activation)
    try:
        await asyncio.to_thread(_send_email_sync, email, DIST_SUBJECTS[lang], body_html)
        logger.info('distributor welcome email sent to %s (lang=%s)', email, lang)
    except Exception:
        logger.exception('Failed to send distributor welcome email to %s', email)


PAID_SUBJECTS = {
    'es': 'Confirmamos tu pago — pedido {number}',
    'en': 'Payment confirmed — order {number}',
    'pt': 'Pagamento confirmado — pedido {number}',
}
# (greet, body, cta, footer) para _action_email_html.
PAID_BODIES = {
    'es': ('Hola, {name}:', 'Recibimos y confirmamos tu pago del pedido <strong>{number}</strong>. '
           'Ya lo estamos preparando; en cuanto salga te mandamos el numero de guia.',
           'Ver mi pedido', 'Gracias por tu compra. Cualquier duda, responde a este correo.'),
    'en': ('Hi {name},', 'We received and confirmed your payment for order <strong>{number}</strong>. '
           'We are preparing it now; we will email you the tracking number as soon as it ships.',
           'View my order', 'Thank you for your purchase. Any questions, just reply to this email.'),
    'pt': ('Ola, {name}:', 'Recebemos e confirmamos seu pagamento do pedido <strong>{number}</strong>. '
           'Ja estamos preparando; enviaremos o codigo de rastreio assim que for despachado.',
           'Ver meu pedido', 'Obrigado pela sua compra. Qualquer duvida, responda a este e-mail.'),
}


async def send_payment_confirmed_email(order, language=None):
    """⚠️ JUBILADA EL 2026-07-31. NO LA VUELVAS A CONECTAR AL FLUJO DE COMPRA.

    Éste era el correo de en medio de los TRES que recibía quien compraba con
    tarjeta: decía «confirmamos tu pago» y nada más, y llegaba entre el de
    «recibimos tu pedido» y el de «va en camino». Christián mandó consolidarlos.

    Lo que hace su trabajo hoy es `send_order_email(order, lang, etapa='pagado')`,
    que dice lo mismo Y lleva el detalle del pedido Y el número de guía adentro —
    los tres correos en uno. Y se manda por una sola puerta (`avisar_al_cliente` en
    server.py) que lleva el candado de «nunca dos veces lo mismo».

    Se conserva porque sigue siendo un correo correcto y hay una prueba que lo cuida,
    pero **conectarla otra vez a la confirmación de pago devuelve el tercer correo**.
    """
    if not email_enabled():
        return
    customer = order.get('customer', {}) or {}
    to = customer.get('email')
    if not to:
        return
    lang = normalize_language(language)
    number = str(order.get('order_number', ''))
    greet, body, cta, footer = PAID_BODIES[lang]
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    html_body = _action_email_html(
        greet, body.replace('{number}', html.escape(number)), cta, footer,
        name=customer.get('full_name', ''), email='', link=f'{site}/pedido/{number}')
    try:
        await asyncio.to_thread(_send_email_sync, to, PAID_SUBJECTS[lang].format(number=number), html_body)
        logger.info('Payment-confirmed email sent to %s (order=%s)', to, number)
    except Exception:
        logger.exception('Failed to send payment-confirmed email for %s', number)


# ---------- Ya salió: el número de guía ----------
# El correo de pago confirmado le PROMETE al cliente, por escrito y en tres idiomas,
# que "en cuanto salga te mandamos el número de guía". Hasta el 2026-07-30 ese correo
# no existía: el rastreo se guardaba en el pedido y el cliente tenía que entrar a
# buscarlo. Prometer por correo y no cumplir por correo es lo que genera el mensaje
# de "¿ya lo mandaron?" que después hay que contestar a mano.
SHIPPED_SUBJECTS = {
    'es': 'Tu pedido {number} va en camino',
    'en': 'Your order {number} is on its way',
    'pt': 'Seu pedido {number} esta a caminho',
}
SHIPPED_BODIES = {
    'es': ('Hola, {name}:',
           'Tu pedido <strong>{number}</strong> ya salio con <strong>{carrier}</strong>. '
           'Numero de guia: <strong>{tracking}</strong>.',
           'Rastrear mi pedido',
           'Puede tardar unas horas en aparecer en el sitio de la paqueteria. '
           'Cualquier duda, responde a este correo.'),
    'en': ('Hi {name},',
           'Your order <strong>{number}</strong> shipped with <strong>{carrier}</strong>. '
           'Tracking number: <strong>{tracking}</strong>.',
           'Track my order',
           'It can take a few hours to show up on the carrier site. '
           'Any questions, just reply to this email.'),
    'pt': ('Ola, {name}:',
           'Seu pedido <strong>{number}</strong> ja foi despachado pela <strong>{carrier}</strong>. '
           'Codigo de rastreio: <strong>{tracking}</strong>.',
           'Rastrear meu pedido',
           'Pode levar algumas horas para aparecer no site da transportadora. '
           'Qualquer duvida, responda a este e-mail.'),
}


async def send_shipped_email(order, language=None):
    """Le manda al cliente su numero de guia. Nunca lanza.

    ⛔ EL BOTON LLEVA A NUESTRA PAGINA, NO A LA DE FEDEX (Christian, 2026-07-31):
    «quiero que vivan en nuestra pagina el mayor tiempo posible». Antes apuntaba al
    rastreo de la paqueteria y se perdia al cliente en el primer clic. Ya no hace
    falta: `/pedido/{numero}` trae el rastreo ADENTRO —los mismos eventos que enseña
    la paqueteria, pedidos a su API y pintados con nuestra marca (ver rastreo.py y
    RastreoEnvio.js)—, y ahi mismo tiene la liga al sitio del carrier si la quiere.
    """
    if not email_enabled():
        return
    customer = (order or {}).get('customer', {}) or {}
    to = customer.get('email')
    numero = str((order or {}).get('tracking_number') or '')
    if not to or not numero:
        return                       # sin guia no hay nada que avisar
    lang = normalize_language(language)
    number = str(order.get('order_number', ''))
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    greet, body, cta, footer = SHIPPED_BODIES[lang]
    cuerpo = (body.replace('{number}', html.escape(number))
                  .replace('{carrier}', html.escape(str(order.get('carrier') or '')))
                  .replace('{tracking}', html.escape(numero)))
    html_body = _action_email_html(
        greet, cuerpo, cta, footer, name=customer.get('full_name', ''), email='',
        link=f'{site}/pedido/{number}')
    try:
        await asyncio.to_thread(_send_email_sync, to,
                                SHIPPED_SUBJECTS[lang].format(number=number), html_body)
        logger.info('Shipped email sent to %s (order=%s, guia=%s)', to, number, numero)
    except Exception:
        logger.exception('Failed to send shipped email for %s', number)


async def send_welcome_email(name, email, language=None):
    """Send the account-confirmation email. Never raises: registration must
    succeed even if the email provider is down or unconfigured."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        logger.info('EMAIL_ENABLED != true, skipping welcome email to %s', email)
        return
    lang = normalize_language(language)
    try:
        template = (TEMPLATES_DIR / f'welcome_email.{lang}.html').read_text(encoding='utf-8')
        body = template.replace('{{name}}', html.escape(name)).replace('{{email}}', html.escape(email))
        await asyncio.to_thread(_send_email_sync, email, WELCOME_SUBJECTS[lang], body)
        logger.info('Welcome email sent to %s (lang=%s)', email, lang)
    except Exception:
        logger.exception('Failed to send welcome email to %s', email)


# ---------- Confirmacion de pedido ----------
ORDER_SUBJECTS = {
    'es': 'Recibimos tu pedido {number} — Exygen Labs',
    'en': 'We received your order {number} — Exygen Labs',
    'pt': 'Recebemos seu pedido {number} — Exygen Labs',
}

# ==========================================================================
#  UN SOLO CORREO CUANDO SE PUEDA (Christián, 2026-07-31)
# ==========================================================================
# ⛔ EL PROBLEMA QUE ESTO RESUELVE. Una compra con tarjeta mandaba TRES correos casi
# seguidos: «recibimos tu pedido», «confirmamos tu pago» y «va en camino». Tres
# correos por una sola compra es ruido: el cliente deja de abrirlos y el que de
# verdad importa —el del número de guía— se pierde entre los otros dos.
#
# LA REGLA, en sus palabras: «un solo correo cuando se pueda; nadie debe recibir
# tres correos por una compra».
#
# CÓMO SE CUMPLE. El mismo correo rico de siempre (el del detalle del pedido) se
# rinde en TRES ETAPAS y cada compra usa las MENOS posibles:
#
#   · 'nuevo'   → «recibimos tu pedido» + los datos para pagar (CLABE / ficha).
#                 SÓLO sale cuando el pago no es inmediato (SPEI, OXXO): con tarjeta
#                 o cripto no se manda nada todavía, se espera al pago.
#   · 'pagado'  → «confirmamos tu pago» + el detalle completo + EL NÚMERO DE GUÍA si
#                 ya se compró. Es el correo que hace el trabajo de tres.
#   · 'enviado' → sólo el rastreo, y sólo cuando la guía apareció DESPUÉS del correo
#                 de pago (guía comprada a mano, reintento, o la segunda caja de un
#                 envío partido). Ése sí es un evento nuevo de verdad.
#
# Lo que queda en la práctica:
#   tarjeta/cripto + guía comprada  → 1 correo
#   SPEI/OXXO + guía comprada       → 2 correos
#   pago inmediato sin guía todavía → 2 correos (pago, y luego el rastreo)
#
# ⛔ EL CANDADO DE «NUNCA DOS VECES LO MISMO» NO VIVE AQUÍ, vive en el pedido
# (`emails_sent` en server.py, apartado en un solo paso condicionado igual que el
# cupón y los puntos). Aquí sólo se decide qué DICE cada etapa.
ORDER_SUBJECTS_PAGADO = {
    'es': 'Confirmamos tu pago del pedido {number} — Exygen Labs',
    'en': 'Payment confirmed for order {number} — Exygen Labs',
    'pt': 'Pagamento confirmado do pedido {number} — Exygen Labs',
}
# Cuando el pago Y la guía caen en el mismo correo, el asunto lo dice: es lo que hace
# que el cliente lo abra. «Confirmamos tu pago» se puede dejar para después; «ya va en
# camino, aquí está tu guía» no.
ORDER_SUBJECTS_PAGADO_CON_GUIA = {
    'es': 'Pago confirmado y tu pedido {number} ya va en camino — Exygen Labs',
    'en': 'Payment confirmed — order {number} is on its way — Exygen Labs',
    'pt': 'Pagamento confirmado — seu pedido {number} já está a caminho — Exygen Labs',
}

ORDER_COPY = {
    'es': {
        'heading': 'Recibimos tu pedido',
        'preheader': 'Tu pedido {number} quedo registrado. Aqui esta el detalle.',
        'trustShipping': 'Envio nacional',
        'greet': 'Apreciable {name}:',
        'intro': 'Recibimos tu pedido y ya quedo registrado. Aqui esta el detalle para que lo tengas por escrito.',
        'orderLabel': 'Numero de pedido',
        'items': 'Lo que pediste',
        'subtotal': 'Subtotal',
        'discount': 'Descuento',
        'shipping': 'Envio',
        'total': 'Total',
        'nextTitle': 'Que sigue',
        'speiTitle': 'Datos para tu transferencia SPEI',
        'speiBeneficiary': 'Beneficiario',
        'speiBank': 'Banco',
        'speiReference': 'Referencia / concepto',
        'nextCard': 'Verificamos el pago y preparamos tu pedido. En cuanto salga te mandamos el numero de guia por correo.',
        'nextSpei': 'Tu pedido queda apartado en cuanto se refleje la transferencia. En horario bancario suele tardar minutos; de noche o en fin de semana puede pasar al siguiente dia habil.',
        'track': 'Ver mi pedido',
        'shipTo': 'Enviar a',
        'ruo': 'Uso exclusivo en investigacion (RUO), en laboratorio y ensayos in vitro. No es un medicamento ni un suplemento.',
        'help': 'Cualquier duda, responde a este correo o escribenos a',
        'savings': 'AHORRASTE {amount}',
        'points': 'GANAS {points} PUNTOS CON ESTA COMPRA',
        'pointsUsed': 'Puntos canjeados',
        # ENVIO PARTIDO: lo que hay sale ya y lo demas se manda pedir. Christian,
        # 2026-07-30. Ya NO se anuncia como "dos entregas" ni con plazos duros: una
        # nota corta pegada a CADA producto sobre pedido, igual que en el sitio.
        'backorderItemNote': 'Se surte desde EUA: tarda un poco mas en llegar. Te mantendremos al tanto.',
        'thanks': 'GRACIAS POR TU COMPRA',
        # --- Etapa 'pagado': el correo que hace el trabajo de tres ---
        'headingPaid': 'Confirmamos tu pago',
        'headingPaidShipped': 'Pago confirmado: tu pedido va en camino',
        'preheaderPaid': 'Recibimos tu pago del pedido {number}. Aqui esta todo el detalle.',
        'introPaid': 'Recibimos y confirmamos tu pago. Aqui esta todo el detalle de tu pedido, para que lo tengas por escrito.',
        # Pagado Y con guia: el rastreo va en este mismo correo, no en otro.
        'nextPaidShipped': 'Tu pedido ya salio. Abajo esta tu numero de guia; puede tardar unas horas en aparecer en el sitio de la paqueteria.',
        # Pagado y todavia sin guia (empaque por confirmar, o algo fallo).
        'nextPaidWaiting': 'Ya lo estamos preparando. En cuanto salga te mandamos el numero de guia por correo; es el unico correo que falta.',
        # Pagado y NADA salia de bodega ese dia: se dice la verdad, sin plazo inventado.
        'nextPaidBackorder': 'Ya lo estamos trabajando: las piezas de tu pedido se surten desde EUA. En cuanto salga te mandamos el numero de guia por correo.',
        # Caja del rastreo dentro del correo.
        'trackingTitle': 'Tu numero de guia',
        'trackingCarrier': 'Paqueteria',
        'trackingNumber': 'Numero de guia',
        'trackShipment': 'Rastrear mi pedido',
        # ENVIO PARTIDO: cuando el cliente pidio que lo disponible salga ya, este
        # paquete NO lleva todo. Decirlo aqui evita el mensaje de "me falto algo".
        'partialShipment': 'Este envio lleva lo que ya teniamos en existencia. Lo demas sale en un segundo envio y te mandamos su propio numero de guia.',
    },
    'en': {
        'heading': 'We received your order',
        'preheader': 'Your order {number} is registered. Here is the detail.',
        'trustShipping': 'Nationwide shipping',
        'greet': 'Dear {name},',
        'intro': 'We received your order and it is now registered. Here is the detail for your records.',
        'orderLabel': 'Order number',
        'items': 'What you ordered',
        'subtotal': 'Subtotal',
        'discount': 'Discount',
        'shipping': 'Shipping',
        'total': 'Total',
        'nextTitle': "What's next",
        'speiTitle': 'Details for your SPEI transfer',
        'speiBeneficiary': 'Beneficiary',
        'speiBank': 'Bank',
        'speiReference': 'Reference / memo',
        'nextCard': 'We verify the payment and prepare your order. As soon as it ships we will email you the tracking number.',
        'nextSpei': 'Your order is reserved as soon as the transfer clears. During banking hours that usually takes minutes; at night or on weekends it may roll to the next business day.',
        'track': 'View my order',
        'shipTo': 'Ship to',
        'ruo': 'Research use only (RUO), for laboratory and in vitro work. Not a medicine or a supplement.',
        'help': 'Any questions, reply to this email or write to',
        'savings': 'YOU SAVED {amount}',
        'points': 'YOU EARN {points} POINTS WITH THIS ORDER',
        'pointsUsed': 'Points redeemed',
        'backorderItemNote': "Ships from the USA - takes a little longer. We'll keep you posted.",
        'thanks': 'THANK YOU FOR YOUR ORDER',
        'headingPaid': 'Payment confirmed',
        'headingPaidShipped': 'Payment confirmed - your order is on its way',
        'preheaderPaid': 'We received your payment for order {number}. Here is the full detail.',
        'introPaid': 'We received and confirmed your payment. Here is the full detail of your order, for your records.',
        'nextPaidShipped': 'Your order has shipped. Your tracking number is below; it can take a few hours to show up on the carrier site.',
        'nextPaidWaiting': 'We are preparing it now. As soon as it ships we will email you the tracking number - that is the only email left.',
        'nextPaidBackorder': 'We are working on it: the items in your order ship from the USA. As soon as it leaves we will email you the tracking number.',
        'trackingTitle': 'Your tracking number',
        'trackingCarrier': 'Carrier',
        'trackingNumber': 'Tracking number',
        'trackShipment': 'Track my order',
        'partialShipment': 'This shipment carries what we already had in stock. The rest goes out in a second shipment with its own tracking number.',
    },
    'pt': {
        'heading': 'Recebemos seu pedido',
        'preheader': 'Seu pedido {number} foi registrado. Aqui esta o detalhe.',
        'trustShipping': 'Envio nacional',
        'greet': 'Prezado(a) {name}:',
        'intro': 'Recebemos seu pedido e ele ja esta registrado. Aqui esta o detalhe para o seu controle.',
        'orderLabel': 'Numero do pedido',
        'items': 'O que voce pediu',
        'subtotal': 'Subtotal',
        'discount': 'Desconto',
        'shipping': 'Frete',
        'total': 'Total',
        'nextTitle': 'Proximos passos',
        'speiTitle': 'Dados para sua transferencia SPEI',
        'speiBeneficiary': 'Beneficiario',
        'speiBank': 'Banco',
        'speiReference': 'Referencia',
        'nextCard': 'Verificamos o pagamento e preparamos seu pedido. Assim que for enviado, mandamos o codigo de rastreio por e-mail.',
        'nextSpei': 'Seu pedido fica reservado assim que a transferencia for compensada. Em horario bancario costuma levar minutos; a noite ou no fim de semana pode passar para o proximo dia util.',
        'track': 'Ver meu pedido',
        'shipTo': 'Enviar para',
        'ruo': 'Uso exclusivo em pesquisa (RUO), em laboratorio e ensaios in vitro. Nao e medicamento nem suplemento.',
        'help': 'Qualquer duvida, responda a este e-mail ou escreva para',
        'savings': 'VOCE ECONOMIZOU {amount}',
        'points': 'VOCE GANHA {points} PONTOS COM ESTA COMPRA',
        'pointsUsed': 'Pontos resgatados',
        'backorderItemNote': 'Vem dos EUA: demora um pouco mais para chegar. Vamos te manter informado.',
        'thanks': 'OBRIGADO PELA SUA COMPRA',
        'headingPaid': 'Confirmamos seu pagamento',
        'headingPaidShipped': 'Pagamento confirmado: seu pedido esta a caminho',
        'preheaderPaid': 'Recebemos seu pagamento do pedido {number}. Aqui esta todo o detalhe.',
        'introPaid': 'Recebemos e confirmamos seu pagamento. Aqui esta todo o detalhe do seu pedido, para o seu controle.',
        'nextPaidShipped': 'Seu pedido ja foi despachado. Abaixo esta o codigo de rastreio; pode levar algumas horas para aparecer no site da transportadora.',
        'nextPaidWaiting': 'Ja estamos preparando. Assim que for despachado mandamos o codigo de rastreio por e-mail; e o unico e-mail que falta.',
        'nextPaidBackorder': 'Ja estamos trabalhando nele: as pecas do seu pedido vem dos EUA. Assim que sair mandamos o codigo de rastreio por e-mail.',
        'trackingTitle': 'Seu codigo de rastreio',
        'trackingCarrier': 'Transportadora',
        'trackingNumber': 'Codigo de rastreio',
        'trackShipment': 'Rastrear meu pedido',
        'partialShipment': 'Este envio leva o que ja tinhamos em estoque. O restante sai em um segundo envio, com seu proprio codigo de rastreio.',
    },
}


def _money(value):
    """Formato de moneda mexicana, igual que en el sitio."""
    try:
        return '$' + f'{float(value):,.0f}' + ' MXN'
    except (TypeError, ValueError):
        return '$0 MXN'


# Modo oscuro en correo: los estilos van en linea (obligatorio para Outlook),
# asi que cada color que cambia se duplica como clase con !important dentro de
# @media (prefers-color-scheme: dark). El claro sigue siendo el diseno base,
# porque Gmail app y Outlook no respetan el modo oscuro de forma confiable.
# Paleta oscura = la del sitio: lienzo negro, grises neutros, azul aclarado.
DARK_EMAIL_STYLE = """
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <style>
    :root { color-scheme: light dark; supported-color-schemes: light dark; }
    @media (prefers-color-scheme: dark) {
      body, .em-bg { background-color: #0A0A0A !important; }
      .em-card { background-color: #141414 !important; border-color: #262626 !important; }
      .em-box { background-color: #0A0A0A !important; border-color: #262626 !important; }
      .em-line { border-color: #262626 !important; }
      .em-ink { color: #F5F5F5 !important; }
      .em-body { color: #D6D6D6 !important; }
      .em-muted { color: #A3A3A3 !important; }
      .em-footer { color: #8C8C8C !important; }
      .em-btn { background-color: #4E73E8 !important; color: #FFFFFF !important; }
      .em-link { color: #93AAF0 !important; }
      .em-save { background-color: #0A0A0A !important; border-color: #93AAF0 !important; }
    }
  </style>
"""


def _algo_sale_ya(order) -> bool:
    """¿Hay AL MENOS UNA pieza de este pedido que salga de la bodega hoy?

    Se usa para no prometer de más. Si NADA sale hoy, el correo de pago confirmado
    dice «lo estamos trabajando, se surte desde EUA» en vez de «ya lo estamos
    preparando»; y si algo sí sale, el envío que se manda es parcial y hay que
    decirlo. La cuenta se hace comparando lo PEDIDO contra lo que quedó por surtir,
    que es justo lo que guarda `backorder_items` (`pedidas` vs `por_surtir`).
    """
    lineas = (order or {}).get('backorder_items') or []
    if not lineas:
        return True                     # sin renglones por surtir, todo sale ya
    for b in lineas:
        try:
            pedidas = int(b.get('pedidas') or b.get('quantity') or 0)
            faltan = int(b.get('por_surtir') or b.get('faltan') or 0)
        except (TypeError, ValueError):
            continue
        if pedidas > faltan:
            return True                 # de este renglón sí sale una parte
    # Y si algún renglón del pedido NO aparece en la lista de por surtir, ése sale ya.
    ids = {str(b.get('product_id')) for b in lineas if b.get('product_id')}
    nombres = {str(b.get('name', '')).strip().lower() for b in lineas if b.get('name')}
    for it in (order or {}).get('items', []) or []:
        get = (lambda k: getattr(it, k, None)) if not isinstance(it, dict) else it.get
        if (str(get('product_id') or '') not in ids
                and str(get('name') or '').strip().lower() not in nombres):
            return True
    return False


def _order_email_html(order, copy, link, etapa='nuevo'):
    """Mismo lenguaje visual que el correo de bienvenida: tarjeta blanca sobre
    fondo gris, tablas anidadas y estilos en linea, que es lo unico que rinde
    parejo en Gmail, Outlook y Apple Mail. Con version oscura via clases em-*
    (ver DARK_EMAIL_STYLE).

    `etapa` decide QUE dice este mismo correo (ver ORDER_SUBJECTS_PAGADO arriba):

      · 'nuevo'  → recibimos tu pedido + como pagar (CLABE / ficha).
      · 'pagado' → confirmamos tu pago + el detalle + el numero de guia si ya lo hay.

    ⛔ LOS DATOS DE PAGO SOLO VAN EN 'nuevo'. Mandarle la CLABE a alguien que ya pago
    es invitarlo a pagar dos veces."""
    esc = html.escape
    INK, BODY, MUTED, LINE, BG = '#132763', '#3D4657', '#8A93A8', '#E4E8F0', '#FBFCFE'
    FONT = 'Helvetica,Arial,sans-serif'

    # ⛔ ENVIO PARTIDO, VERSION CORTA (Christian, 2026-07-30). Ya no es un bloque grande
    # con "dos entregas" y el desglose de piezas: es una nota chiquita pegada a CADA
    # producto que va sobre pedido, igual que en el carrito y el checkout del sitio. Se
    # empareja por product_id (lo normal) y por nombre como respaldo, porque no todo
    # renglon viejo trae el id.
    lineas_bo = order.get('backorder_items') or []
    ids_bo = {str(b.get('product_id')) for b in lineas_bo if b.get('product_id')}
    nombres_bo = {str(b.get('name', '')).strip().lower() for b in lineas_bo if b.get('name')}

    rows = []
    for item in order.get('items', []):
        qty = int(item.get('quantity', 1) or 1)
        line_total = float(item.get('price', 0) or 0) * qty
        es_sobre_pedido = (str(item.get('product_id', '')) in ids_bo
                           or str(item.get('name', '')).strip().lower() in nombres_bo)
        nota = (f'<br><span class="em-muted" style="color:{MUTED};font-size:11px;">'
                f'{copy["backorderItemNote"]}</span>' if es_sobre_pedido else '')
        rows.append(
            f'<tr>'
            f'<td class="em-body em-line" style="padding:10px 0;border-bottom:1px solid {LINE};font-family:{FONT};'
            f'font-size:14px;line-height:1.5;color:{BODY};">{esc(str(item.get("name", "")))}'
            f'<span class="em-muted" style="color:{MUTED};">&nbsp;&times;{qty}</span>{nota}</td>'
            f'<td align="right" class="em-body em-line" style="padding:10px 0;border-bottom:1px solid {LINE};font-family:{FONT};'
            f'font-size:14px;color:{BODY};white-space:nowrap;">{_money(line_total)}</td>'
            f'</tr>'
        )

    def total_row(label, value, strong=False):
        color = INK if strong else MUTED
        cls = 'em-ink' if strong else 'em-muted'
        size = '16px' if strong else '14px'
        weight = 'bold' if strong else 'normal'
        pad = '12px 0 0 0' if strong else '6px 0 0 0'
        return (f'<tr>'
                f'<td class="{cls}" style="padding:{pad};font-family:{FONT};font-size:{size};color:{color};font-weight:{weight};">{label}</td>'
                f'<td align="right" class="{cls}" style="padding:{pad};font-family:{FONT};font-size:{size};color:{color};'
                f'font-weight:{weight};white-space:nowrap;">{value}</td>'
                f'</tr>')

    totals = [total_row(copy['subtotal'], _money(order.get('subtotal', 0)))]
    if float(order.get('discount', 0) or 0) > 0:
        totals.append(total_row(copy['discount'], '-' + _money(order.get('discount', 0))))
    if int(order.get('points_used', 0) or 0) > 0:
        totals.append(total_row(copy['pointsUsed'], '-' + _money(order.get('points_used', 0))))
    totals.append(total_row(copy['shipping'], _money(order.get('shipping', 0))))
    totals.append(total_row(copy['total'], _money(order.get('total', 0)), strong=True))

    customer = order.get('customer', {}) or {}
    address = esc(', '.join(b for b in [customer.get('address', ''), customer.get('city', ''),
                                        customer.get('state', ''), customer.get('postal_code', ''),
                                        # El país solo cuando no es México (el caso normal no estorba).
                                        customer.get('country', '') if customer.get('country') not in ('', 'MX') else ''] if b))
    es_pagado = etapa == 'pagado'
    is_spei = (order.get('payment_method') or '') == 'spei'
    rastreo = str(order.get('tracking_number') or '').strip()
    # ⛔ QUE SIGUE, por etapa. En 'pagado' hay tres verdades distintas y cada una tiene
    # su texto: ya salio (y aqui va la guia), sigue en preparacion, o todo se surte
    # desde EUA. Ninguno promete una fecha que no se pueda cumplir.
    if es_pagado:
        if rastreo:
            next_text = copy['nextPaidShipped']
        elif order.get('backorder') and not _algo_sale_ya(order):
            next_text = copy['nextPaidBackorder']
        else:
            next_text = copy['nextPaidWaiting']
    else:
        next_text = copy['nextSpei'] if is_spei else copy['nextCard']
    # Los datos bancarios NUNCA viajan en el correo de pago confirmado.
    spei = order.get('spei') if (is_spei and not es_pagado) else None
    spei_html = ''
    if spei and spei.get('clabe'):
        line = ('<tr><td style="padding:3px 0;font-family:{f};font-size:13px;color:{m};">{k}</td>'
                '<td align="right" style="padding:3px 0;font-family:{f};font-size:13px;color:{b};font-weight:bold;">{v}</td></tr>')
        rows_spei = ''.join(line.format(f=FONT, m=MUTED, b=BODY, k=k, v=esc(str(v))) for k, v in [
            (copy.get('speiBeneficiary', 'Beneficiario'), spei.get('beneficiary', '')),
            (copy.get('speiBank', 'Banco'), spei.get('bank', '')),
            ('CLABE', spei.get('clabe', '')),
            (copy.get('speiReference', 'Referencia'), order.get('order_number', '')),
        ] if v)
        spei_html = (
            f'<tr><td style="padding:18px 40px 0 40px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" '
            f'style="background-color:{BG};border:1px solid {LINE};border-radius:10px;"><tr><td style="padding:14px 18px;">'
            f'<div class="em-ink" style="font-family:{FONT};font-size:13px;font-weight:bold;color:{INK};padding-bottom:6px;">{copy.get("speiTitle", "Datos para tu transferencia SPEI")}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_spei}</table>'
            f'</td></tr></table></td></tr>'
        )
    number = esc(str(order.get('order_number', '')))

    # ⛔ LA CAJA DEL RASTREO. Es la razon de ser de todo esto: el numero de guia viaja
    # DENTRO del correo de pago confirmado en vez de en un tercer correo aparte.
    rastreo_html = ''
    if es_pagado and rastreo:
        fila = ('<tr><td style="padding:3px 0;font-family:{f};font-size:13px;color:{m};">{k}</td>'
                '<td align="right" style="padding:3px 0;font-family:{f};font-size:13px;color:{b};font-weight:bold;">{v}</td></tr>')
        filas = ''.join(fila.format(f=FONT, m=MUTED, b=BODY, k=k, v=esc(str(v))) for k, v in [
            (copy.get('trackingCarrier', 'Paqueteria'), order.get('carrier', '')),
            (copy.get('trackingNumber', 'Numero de guia'), rastreo),
        ] if v)
        # Envio partido: este paquete NO lleva todo y hay que decirlo aqui mismo.
        parcial = ''
        if order.get('backorder_items') and _algo_sale_ya(order):
            parcial = (f'<div class="em-muted" style="font-family:{FONT};font-size:11px;'
                       f'line-height:1.5;color:{MUTED};padding-top:8px;">'
                       f'{copy.get("partialShipment", "")}</div>')
        rastreo_html = (
            f'<tr><td style="padding:18px 40px 0 40px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" '
            f'style="background-color:{BG};border:1px solid {LINE};border-radius:10px;"><tr><td style="padding:14px 18px;">'
            f'<div class="em-ink" style="font-family:{FONT};font-size:13px;font-weight:bold;color:{INK};padding-bottom:6px;">{copy.get("trackingTitle", "Tu numero de guia")}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{filas}</table>'
            f'{parcial}'
            f'</td></tr></table></td></tr>'
        )

    # Encabezado, preheader, intro y boton, segun la etapa.
    if es_pagado:
        heading = copy['headingPaidShipped'] if rastreo else copy['headingPaid']
        preheader = copy['preheaderPaid'].format(number=number)
        intro = copy['introPaid']
        boton = copy.get('trackShipment', copy['track']) if rastreo else copy['track']
        # ⛔ EL BOTON SE QUEDA EN CASA (Christian, 2026-07-31): «quiero que vivan en
        # nuestra pagina el mayor tiempo posible». Antes, con guia, este renglon
        # mandaba el boton al rastreo de FedEx y se perdia al cliente en cuanto
        # picaba. Ya no hace falta: `/pedido/{numero}` trae el rastreo ADENTRO
        # (ver rastreo.py y RastreoEnvio.js), con los mismos eventos que enseña la
        # paqueteria. Quien de todos modos quiera ver el sitio de FedEx tiene la liga
        # abajo de la linea de tiempo. Por eso aqui ya no se pisa `link`.
    else:
        heading = copy['heading']
        preheader = copy['preheader'].format(number=number)
        intro = copy['intro']
        boton = copy['track']

    # Estilo ticket de super: ahorro y puntos en cajas punteadas, solo si aplican.
    ticket_lines = []
    if float(order.get('discount', 0) or 0) > 0:
        ticket_lines.append(copy['savings'].format(amount=_money(order.get('discount', 0))))
    if int(order.get('points_earned', 0) or 0) > 0:
        ticket_lines.append(copy['points'].format(points=int(order['points_earned'])))
    ticket_html = ''
    if ticket_lines:
        rows_html = ''.join(
            f'<tr><td align="center" class="em-ink" style="padding:4px 20px; font-family:{FONT}; '
            f'font-size:15px; font-weight:bold; letter-spacing:1.5px; color:{INK};">{line}</td></tr>'
            for line in ticket_lines
        )
        ticket_html = (f'<tr><td style="padding:18px 40px 0 40px;">'
                       f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-save" '
                       f'style="background-color:{BG}; border:2px dashed {INK}; border-radius:10px;">'
                       f'<tr><td style="padding:8px 0;"><table role="presentation" width="100%" cellpadding="0" '
                       f'cellspacing="0">{rows_html}</table></td></tr></table></td></tr>')

    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0; padding:0; background-color:{BG};">
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">{preheader}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-bg" style="background-color:{BG};">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" class="em-card" style="max-width:560px; width:100%; background-color:#FFFFFF; border:1px solid {LINE}; border-radius:14px;">

          <tr>
            <td align="center" style="padding:36px 40px 8px 40px;">
              <div class="em-ink" style="font-family:{FONT}; font-size:20px; letter-spacing:3px; color:{INK}; font-weight:bold;">EXYGEN&nbsp;LABS</div>
              <div class="em-muted" style="font-family:{FONT}; font-size:11px; letter-spacing:2px; color:{MUTED}; padding-top:4px;">RESEARCH PEPTIDES</div>
            </td>
          </tr>

          <tr>
            <td style="padding:28px 40px 0 40px; font-family:{FONT};">
              <h1 class="em-ink" style="margin:0; font-size:26px; line-height:1.25; color:{INK}; font-weight:bold;">{heading}</h1>
              <p class="em-body" style="margin:16px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{copy['greet'].format(name=esc(str(customer.get('full_name', '')).upper()))}</p>
              <p class="em-body" style="margin:12px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{intro}</p>
            </td>
          </tr>

          <tr>
            <td style="padding:22px 40px 0 40px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" style="background-color:{BG}; border:1px solid {LINE}; border-radius:10px;">
                <tr><td align="center" style="padding:14px 20px; font-family:{FONT};">
                  <div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; text-transform:uppercase;">{copy['orderLabel']}</div>
                  <div class="em-ink" style="font-size:20px; color:{INK}; font-weight:bold; letter-spacing:1px; padding-top:5px;">{number}</div>
                </td></tr>
              </table>
            </td>
          </tr>

          <tr>
            <td style="padding:26px 40px 0 40px; font-family:{FONT};">
              <div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; text-transform:uppercase; padding-bottom:4px;">{copy['items']}</div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(totals)}</table>
            </td>
          </tr>
          {ticket_html}
          {spei_html}
          {rastreo_html}
          <tr>
            <td style="padding:24px 40px 0 40px; font-family:{FONT};">
              <div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; text-transform:uppercase;">{copy['nextTitle']}</div>
              <p class="em-body" style="margin:8px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{next_text}</p>
              <p class="em-muted" style="margin:14px 0 0 0; font-size:14px; line-height:1.6; color:{MUTED};">
                <strong class="em-body" style="color:{BODY};">{copy['shipTo']}:</strong> {address}
              </p>
            </td>
          </tr>

          <tr>
            <td align="center" style="padding:28px 40px 8px 40px;">
              <a href="{link}" class="em-btn" style="display:inline-block; background-color:{INK}; color:#FFFFFF; font-family:{FONT}; font-size:15px; font-weight:bold; text-decoration:none; padding:14px 36px; border-radius:999px;">{boton}</a>
            </td>
          </tr>

          <tr>
            <td align="center" class="em-ink" style="padding:20px 40px 4px 40px; font-family:{FONT}; font-size:14px; font-weight:bold; letter-spacing:2px; color:{INK};">
              {copy['thanks']}
            </td>
          </tr>

          <tr>
            <td align="center" class="em-muted" style="padding:10px 40px 28px 40px; font-family:{FONT}; font-size:12px; color:{MUTED}; letter-spacing:0.5px;">
              Pureza HPLC &ge;99% &nbsp;&middot;&nbsp; {copy['trustShipping']}
            </td>
          </tr>

          <tr><td style="padding:0 40px;"><div class="em-line" style="border-top:1px solid {LINE};"></div></td></tr>

          <tr>
            <td class="em-muted" style="padding:20px 40px 8px 40px; font-family:{FONT}; font-size:13px; line-height:1.6; color:{MUTED};">
              {copy['help']} <a href="mailto:hola@exygenlabs.com" class="em-link" style="color:{INK};">hola@exygenlabs.com</a>
            </td>
          </tr>

          <tr>
            <td class="em-footer" style="padding:12px 40px 28px 40px; font-family:{FONT}; font-size:11px; line-height:1.6; color:#A6ADBE;">
              {copy['ruo']}<br><br>
              &copy; 2026 Exygen Labs &middot; <a href="https://exygenlabs.com" class="em-footer" style="color:{MUTED};">exygenlabs.com</a>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


async def send_order_email(order, language=None, etapa='nuevo'):
    """EL correo del cliente. Nunca lanza: una compra no puede fallar porque
    el proveedor de correo este caido.

    `etapa='nuevo'`  → «recibimos tu pedido» (+ CLABE si es SPEI).
    `etapa='pagado'` → «confirmamos tu pago» + el detalle + LA GUIA si ya se compro.
                       Es el correo que sustituye a los tres de antes.

    ⛔ QUIEN LLAMA NO DECIDE SI SE MANDA. El candado de una-sola-vez vive en el pedido
    (`_apartar_correo` en server.py): aqui se manda lo que se pida, siempre.
    """
    if not email_enabled():
        logger.info('EMAIL_ENABLED != true, skipping order email for %s', order.get('order_number'))
        return
    to_address = (order.get('customer', {}) or {}).get('email')
    if not to_address:
        logger.warning('Order %s has no customer email', order.get('order_number'))
        return
    lang = normalize_language(language)
    copy = ORDER_COPY[lang]
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    numero = order.get('order_number', '')
    link = f"{site}/pedido/{numero}"
    if etapa == 'pagado':
        asuntos = (ORDER_SUBJECTS_PAGADO_CON_GUIA if order.get('tracking_number')
                   else ORDER_SUBJECTS_PAGADO)
    else:
        asuntos = ORDER_SUBJECTS
    subject = asuntos[lang].format(number=numero)
    try:
        await asyncio.to_thread(_send_email_sync, to_address, subject,
                                _order_email_html(order, copy, link, etapa))
        logger.info('Order email sent to %s (order=%s, lang=%s, etapa=%s)',
                    to_address, numero, lang, etapa)
    except Exception:
        logger.exception('Failed to send order email for %s', numero)


# ---------- Aviso interno de compra: qué hay que preparar y mandar ----------
#
# Christián (2026-07-30): quiere un correo por cada pedido para saber qué preparar. No es
# el correo del cliente con otro membrete: es una ORDEN DE TRABAJO. Va en español llano,
# con lo que hace falta para actuar y nada más — qué empacar, qué HAY QUE MANDAR PEDIR,
# a dónde va, si ya pagó, y el enlace para abrir la ficha en el Panel.
#
# Sale por el mismo camino que los demás correos (Resend, dominio exygenlabs.com) y en
# segundo plano: si el proveedor de correo está caído, la compra sale igual. Un aviso que
# puede tumbar un checkout no es un aviso, es un riesgo.

def _aviso_compra_html(order, link_admin):
    esc = html.escape
    INK, BODY, MUTED, LINE, BG = '#132763', '#3D4657', '#8A93A8', '#E4E8F0', '#FBFCFE'
    FONT = 'Helvetica,Arial,sans-serif'
    c = order.get('customer', {}) or {}

    def fila(k, v):
        return (f'<tr><td style="padding:3px 0;font-family:{FONT};font-size:13px;color:{MUTED};'
                f'white-space:nowrap;padding-right:12px;">{k}</td>'
                f'<td style="padding:3px 0;font-family:{FONT};font-size:13px;color:{BODY};">{v}</td></tr>')

    articulos = ''.join(
        f'<tr><td style="padding:6px 0;border-bottom:1px solid {LINE};font-family:{FONT};'
        f'font-size:14px;color:{BODY};">{esc(str(it.get("name", "")))}'
        f'<span style="color:{MUTED};"> {esc(str(it.get("presentation") or ""))}</span></td>'
        f'<td align="right" style="padding:6px 0;border-bottom:1px solid {LINE};font-family:{FONT};'
        f'font-size:15px;font-weight:bold;color:{INK};white-space:nowrap;">×{int(it.get("quantity", 1) or 1)}</td></tr>'
        for it in order.get('items', []))

    # ⛔ LO PRIMERO DEL CORREO SI EXISTE: lo que hay que comprarle al proveedor. Si va
    # hasta abajo, se manda el paquete incompleto y nadie sale a comprar lo que falta.
    pedir = ''
    if order.get('backorder_items'):
        def _a_quien(b):
            """A QUIÉN COMPRARLE. Sin esto el aviso decía QUÉ mandar pedir y dejaba a
            Christián abriendo una terminal con el pedido ya vendido. El dato viaja
            pegado al renglón (ver `_con_proveedor` en server.py); si el producto no
            tiene proveedor con precio en la base, se DICE — un hueco callado es peor."""
            if b.get('sin_proveedor') or not b.get('proveedor'):
                return (f'<div style="font-family:{FONT};font-size:12px;color:#B54708;'
                        f'padding-top:2px;">⚠️ Sin proveedor registrado — revisar el '
                        f'motor de precios</div>')
            tel = esc(str(b.get('telefono') or ''))
            partes = [f'<b>COMPRAR A: {esc(str(b["proveedor"]))}</b>']
            partes.append(f'tel {tel}' if tel else
                          '<span style="color:#B54708;">⚠️ sin teléfono</span>')
            costo = b.get('costo_vial_usd')
            if costo:
                partes.append(f'${float(costo):,.2f} USD/vial')
            fila = ' · '.join(partes)
            if b.get('whatsapp'):
                fila += (f' · <a href="{esc(str(b["whatsapp"]))}" '
                         f'style="color:#7A5A00;">escribirle por WhatsApp</a>')
            return (f'<div style="font-family:{FONT};font-size:12px;color:#7A5A00;'
                    f'padding-top:2px;">{fila}</div>')

        renglones = ''.join(
            f'<tr><td style="padding:5px 0;font-family:{FONT};font-size:13px;color:{BODY};">'
            f'{esc(str(b.get("name", "")))}{_a_quien(b)}</td>'
            f'<td align="right" valign="top" style="padding:5px 0;font-family:{FONT};font-size:13px;color:{BODY};'
            f'white-space:nowrap;">salen ya: <b>{int(b.get("en_mano", 0) or 0)}</b> · '
            f'mandar pedir: <b>{int(b.get("por_surtir", 0) or 0)}</b></td></tr>'
            for b in order['backorder_items'])
        pedir = (
            f'<div style="background:#FFF6E5;border:2px solid #E0A800;border-radius:10px;'
            f'padding:14px 16px;margin-bottom:16px;">'
            f'<div style="font-family:{FONT};font-size:14px;font-weight:bold;color:#7A5A00;'
            f'padding-bottom:6px;">HAY QUE MANDAR PEDIR</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0">{renglones}</table>'
            f'<div style="font-family:{FONT};font-size:12px;color:#7A5A00;padding-top:8px;">'
            f'Este pedido no sale completo de la bodega. Al cliente ya se le avisó que llega '
            f'en dos entregas: lo que hay en 2 a 5 días y el resto alrededor de una semana '
            f'después.</div></div>')

    pagado = ('<span style="color:#1B7F4B;font-weight:bold;">SÍ — ya entró el dinero</span>'
              if order.get('paid') else
              '<span style="color:#B54708;font-weight:bold;">TODAVÍA NO — está por cobrarse</span>')
    envio = order.get('shipping', 0) or 0

    # ⛔ LO QUE CUESTA LA GUÍA, A LA VISTA. El número que duele no es lo que se cobra
    # de envío sino lo que la casa NO cobra: un pedido de $179 con guía de $250 se
    # come el 140% y hasta hoy eso no aparecía en ningún correo.
    try:
        costo_guia = float(order.get('shipping_cost') or 0)
    except (TypeError, ValueError):
        costo_guia = 0.0
    guia_fila = ''
    if costo_guia > 0:
        absorbe = max(0.0, costo_guia - float(envio or 0))
        detalle = _money(costo_guia)
        if order.get('carrier'):
            detalle += f' · {esc(str(order["carrier"]))}'
        if absorbe > 0:
            detalle += (f' <span style="color:#B54708;">(la casa pone '
                        f'{_money(absorbe)})</span>')
        guia_fila = fila('Costo de la guía', detalle)
    direccion = '<br>'.join(esc(x) for x in [
        c.get('address', ''), c.get('address_2', ''),
        ', '.join(b for b in [c.get('city', ''), c.get('state', ''), c.get('postal_code', '')] if b),
        c.get('country', ''),
    ] if x)

    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body style="margin:0;padding:0;background:{BG};">
  <div style="max-width:600px;margin:0 auto;padding:24px 20px;font-family:{FONT};">
    <div style="font-size:12px;letter-spacing:2px;color:{MUTED};">EXYGEN LABS · AVISO INTERNO</div>
    <h1 style="margin:6px 0 2px 0;font-size:22px;color:{INK};">Entró un pedido</h1>
    <div style="font-family:{FONT};font-size:18px;font-weight:bold;color:{INK};letter-spacing:1px;
                padding-bottom:16px;">{esc(str(order.get('order_number', '')))}</div>
    {pedir}
    <div style="font-size:13px;font-weight:bold;color:{INK};padding-bottom:4px;">QUÉ VA EN LA CAJA</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">{articulos}</table>

    <div style="font-size:13px;font-weight:bold;color:{INK};padding-bottom:4px;">EL DINERO</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      {fila('Total', f'<b style="font-size:15px;color:{INK};">{_money(order.get("total", 0))}</b>')}
      {fila('¿Ya pagó?', pagado)}
      {fila('Cómo paga', esc(str(order.get('payment_method', ''))))}
      {fila('Estado', esc(str(order.get('status', ''))))}
      {fila('Envío', 'Gratis (lo absorbe la casa)' if not envio else _money(envio))}
      {guia_fila}
    </table>

    <div style="font-size:13px;font-weight:bold;color:{INK};padding-bottom:4px;">A QUIÉN Y A DÓNDE</div>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
      {fila('Cliente', esc(str(c.get('full_name', ''))))}
      {fila('Teléfono', esc(str(c.get('phone', ''))))}
      {fila('Correo', esc(str(c.get('email', ''))))}
      {fila('Dirección', direccion)}
      {fila('Notas', esc(str(c.get('notes', '') or '—')))}
    </table>

    <a href="{link_admin}" style="display:inline-block;background:{INK};color:#FFFFFF;font-size:14px;
       font-weight:bold;text-decoration:none;padding:12px 28px;border-radius:999px;">Abrir en el Panel</a>
  </div>
</body>
</html>"""


# El correo con el que corre la suite E2E y el marcador que deja en los pedidos.
CORREO_E2E = 'e2e-no-responder@example.com'


def es_pedido_de_prueba(order):
    """¿Este pedido lo hizo la suite E2E y no una persona?

    ⛔ LOS PEDIDOS DE PRUEBA NO AVISAN. El aviso interno salió el mismo día que la suite
    E2E y Christián recibió el correo de un pedido que nadie compró — se puso a prepararlo.
    Un aviso que se equivoca es peor que no tenerlo: la próxima vez que llegue uno de
    verdad, ya no se le va a creer.

    Solo se calla el AVISO: el pedido de prueba sigue su flujo completo, porque de eso se
    trata la prueba."""
    c = (order or {}).get('customer') or {}
    correo = (c.get('email') or '').strip().lower()
    if correo == CORREO_E2E:
        return True
    marca = f"{c.get('full_name') or ''} {c.get('notes') or ''}".upper()
    return 'E2E' in marca


async def send_purchase_alert(order, momento='nuevo'):
    """Le avisa a Christián que entró un pedido (o que ya se pagó).

    `momento`: 'nuevo' al crearse, 'pagado' cuando el webhook confirma el dinero. Son dos
    avisos a propósito: uno dice qué se va a necesitar y el otro dice que ya se puede
    mandar. Con uno solo, o se prepara mercancía que nadie pagó o se entera tarde.

    Nunca lanza: se llama en segundo plano y una compra no puede fallar porque el correo
    no salga."""
    if not email_enabled():
        logger.info('EMAIL_ENABLED != true, skipping purchase alert for %s',
                    order.get('order_number'))
        return
    if es_pedido_de_prueba(order):
        logger.info('Pedido de prueba (E2E): no se manda aviso interno de %s',
                    order.get('order_number'))
        return
    numero = order.get('order_number', '')
    site = os.environ.get('SITE_URL', 'https://exygenlabs.com')
    marca = ' · CON PIEZAS SOBRE PEDIDO' if order.get('backorder_items') else ''
    asunto = (f'PAGADO: pedido {numero} — {_money(order.get("total", 0))}{marca}'
              if momento == 'pagado' else
              f'Nuevo pedido {numero} — {_money(order.get("total", 0))}{marca}')
    try:
        await send_admin_notification(asunto, _aviso_compra_html(order, f'{site}/admin'))
        logger.info('Purchase alert (%s) sent for %s to %s', momento, numero,
                    admin_notify_address())
    except Exception:
        logger.exception('Failed to send purchase alert for %s', numero)


# ============================================================================
#  LA COTIZACIÓN POR CORREO  (Christián, 2026-07-30)
# ============================================================================
#
# El distribuidor arma la cotización en su panel y la manda al correo de su cliente.
# Es el MISMO armazón visual de los demás correos de Exygen —tarjeta blanca sobre
# fondo gris, tablas anidadas, estilos en línea, versión oscura con las clases
# `em-*`— para que el cliente reconozca de dónde viene antes de leer una palabra.
#
# ⛔ AQUÍ NO SE ASOMA NI UN COSTO. Lo ve un cliente final: sólo hay precio público,
# el descuento que se le dio y los totales. Ni proveedor, ni margen, ni ROI, ni la
# comisión del distribuidor. Hay una prueba que lee el HTML entero y truena si
# aparece cualquiera de esas palabras.
#
# El remitente es SIEMPRE el de Exygen (dominio autenticado); lo único que cambia
# es el `reply-to`, que apunta al distribuidor para que el cliente le conteste a él.

QUOTE_SUBJECTS = {
    'es': 'Tu cotización de Exygen Labs{folio}',
    'en': 'Your Exygen Labs quote{folio}',
    'pt': 'Seu orçamento da Exygen Labs{folio}',
}

QUOTE_COPY = {
    'es': {
        'preheader': 'Tu cotización de Exygen Labs por {total}.',
        'heading': 'Tu cotización',
        'greet': 'HOLA {name}',
        'greetPlain': 'HOLA',
        'intro': '{advisor} preparó esta cotización para ti. Los precios ya traen tu descuento.',
        'folioLabel': 'Cotización',
        'items': 'Lo que cotizaste',
        'money': 'El dinero',
        'listPrice': 'Precio de lista',
        'savings': 'Ahorro',
        'total': 'Total',
        'savingsBadge': 'TE AHORRAS {amount}',
        'cta': 'Pagar En Línea',
        'ctaNote': 'El botón abre la tienda con tu carrito ya armado: llegas a un paso de pagar.',
        'toLabel': 'Cotización para',
        'codeTitle': 'Tu código de descuento',
        'codeNote': 'Entra con el botón de arriba y tu descuento se aplica solo. También puedes teclear el código al pagar.',
        'validity': 'Cotización informativa, vigencia de 7 días. Precios en pesos mexicanos (MXN) e incluyen IVA. El envío se calcula al pagar.',
        'help': '¿Dudas? Contesta este correo y te atiende Mónica, o escríbenos a',
        'ruo': 'Productos para uso exclusivo en investigación (RUO), en laboratorio y ensayos in vitro. No son medicamentos ni suplementos.',
        'thanks': 'GRACIAS POR TU INTERÉS',
        'each': 'c/u',
    },
    'en': {
        'preheader': 'Your Exygen Labs quote for {total}.',
        'heading': 'Your quote',
        'greet': 'HELLO {name}',
        'greetPlain': 'HELLO',
        'intro': '{advisor} put together this quote for you. Prices already include your discount.',
        'folioLabel': 'Quote',
        'items': 'What you asked for',
        'money': 'The money',
        'listPrice': 'List price',
        'savings': 'Savings',
        'total': 'Total',
        'savingsBadge': 'YOU SAVE {amount}',
        'cta': 'Pay Online',
        'ctaNote': 'The button opens the store with your cart already set up — one step from paying.',
        'toLabel': 'Quote for',
        'codeTitle': 'Your discount code',
        'codeNote': 'Use the button above and your discount applies by itself. You can also type the code at checkout.',
        'validity': 'Informational quote, valid for 7 days. Prices in Mexican pesos (MXN), tax included. Shipping is calculated at checkout.',
        'help': 'Questions? Reply to this email and Mónica will take care of you, or write to',
        'ruo': 'Research use only (RUO) products, for laboratory and in vitro work. Not medicines or supplements.',
        'thanks': 'THANK YOU FOR YOUR INTEREST',
        'each': 'each',
    },
    'pt': {
        'preheader': 'Seu orçamento da Exygen Labs de {total}.',
        'heading': 'Seu orçamento',
        'greet': 'OLA {name}',
        'greetPlain': 'OLA',
        'intro': '{advisor} preparou este orçamento para você. Os preços já incluem o seu desconto.',
        'folioLabel': 'Orçamento',
        'items': 'O que você pediu',
        'money': 'O dinheiro',
        'listPrice': 'Preço de tabela',
        'savings': 'Economia',
        'total': 'Total',
        'savingsBadge': 'VOCE ECONOMIZA {amount}',
        'cta': 'Pagar On-line',
        'ctaNote': 'O botão abre a loja com o seu carrinho já montado — a um passo de pagar.',
        'toLabel': 'Orçamento para',
        'codeTitle': 'Seu código de desconto',
        'codeNote': 'Entre pelo botão acima e o seu desconto é aplicado sozinho. Você também pode digitar o código no pagamento.',
        'validity': 'Orçamento informativo, validade de 7 dias. Preços em pesos mexicanos (MXN), impostos incluídos. O frete é calculado no pagamento.',
        'help': 'Dúvidas? Responda este e-mail e a Mónica te atende, ou escreva para',
        'ruo': 'Produtos para uso exclusivo em pesquisa (RUO), em laboratório e ensaios in vitro. Não são medicamentos nem suplementos.',
        'thanks': 'OBRIGADO PELO SEU INTERESSE',
        'each': 'cada',
    },
}


def _quote_email_html(copy, quote):
    """La cotización con el mismo lenguaje visual del correo de pedido.

    `quote`: {folio, client_name, code, link, lines, list_total, savings,
              total}. `lines`: [{name, quantity, unit_price, amount,
              list_price}] — nombre, cuánto y a cómo. Nada más.

    ⛔ QUIEN FIRMA ES LA CASA. El saludo NO lee ningún `advisor` que venga en
    `quote`: siempre pinta `ATENCION_NOMBRE`. Este correo lo abre un CLIENTE
    FINAL, y aquí llevaba el nombre del distribuidor — o sea, su identidad
    regalada en el primer correo. El candado vive aquí, en el que arma el HTML,
    y no en quien lo llama: así, aunque mañana alguien vuelva a meter el nombre
    del distribuidor en el diccionario, no hay por dónde salga.
    """
    esc = html.escape
    INK, BODY, MUTED, LINE, BG = '#132763', '#3D4657', '#8A93A8', '#E4E8F0', '#FBFCFE'
    GREEN = '#0F7B5A'
    FONT = 'Helvetica,Arial,sans-serif'

    rows = []
    for ln in quote.get('lines', []):
        qty = int(ln.get('quantity', 1) or 1)
        unit = float(ln.get('unit_price', 0) or 0)
        lista = float(ln.get('list_price', 0) or 0)
        # El espacio va FUERA del tachado: dentro, la raya se come la separación y
        # el precio nuevo y el viejo se leen pegados ("$2,549 MXN c/u$2,999 MXN").
        antes = (f'&nbsp;&nbsp;<span class="em-muted" style="color:{MUTED};font-size:11px;'
                 f'text-decoration:line-through;">{_money(lista)}</span>'
                 if lista > unit else '')
        rows.append(
            f'<tr>'
            f'<td class="em-body em-line" style="padding:10px 0;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};font-size:14px;line-height:1.5;color:{BODY};">'
            f'{esc(str(ln.get("name", "")))}'
            f'<span class="em-muted" style="color:{MUTED};">&nbsp;&times;{qty}</span>'
            f'<br><span class="em-muted" style="color:{MUTED};font-size:11px;">'
            f'{_money(unit)} {copy["each"]}{antes}</span></td>'
            f'<td align="right" class="em-body em-line" style="padding:10px 0;border-bottom:1px solid {LINE};'
            f'font-family:{FONT};font-size:14px;color:{BODY};white-space:nowrap;">'
            f'{_money(ln.get("amount", 0))}</td>'
            f'</tr>'
        )

    def total_row(label, value, strong=False, color=None):
        c = color or (INK if strong else MUTED)
        cls = 'em-ink' if strong else 'em-muted'
        size = '17px' if strong else '14px'
        weight = 'bold' if strong else 'normal'
        pad = '12px 0 0 0' if strong else '6px 0 0 0'
        return (f'<tr>'
                f'<td class="{cls}" style="padding:{pad};font-family:{FONT};font-size:{size};'
                f'color:{c};font-weight:{weight};">{label}</td>'
                f'<td align="right" class="{cls}" style="padding:{pad};font-family:{FONT};'
                f'font-size:{size};color:{c};font-weight:{weight};white-space:nowrap;">{value}</td>'
                f'</tr>')

    ahorro = float(quote.get('savings', 0) or 0)
    totals = [total_row(copy['listPrice'], _money(quote.get('list_total', 0)))]
    if ahorro > 0:
        totals.append(total_row(copy['savings'], '-' + _money(ahorro), color=GREEN))
    totals.append(total_row(copy['total'], _money(quote.get('total', 0)), strong=True))

    badge = ''
    if ahorro > 0:
        badge = (f'<tr><td style="padding:18px 40px 0 40px;">'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-save" '
                 f'style="background-color:{BG}; border:2px dashed {INK}; border-radius:10px;">'
                 f'<tr><td align="center" class="em-ink" style="padding:11px 20px;font-family:{FONT};'
                 f'font-size:15px;font-weight:bold;letter-spacing:1.5px;color:{INK};">'
                 f'{copy["savingsBadge"].format(amount=_money(ahorro))}</td></tr></table></td></tr>')

    codigo = esc(str(quote.get('code', '') or ''))
    code_html = ''
    if codigo:
        code_html = (
            f'<tr><td style="padding:18px 40px 0 40px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" '
            f'style="background-color:{BG};border:1px solid {LINE};border-radius:10px;">'
            f'<tr><td style="padding:14px 18px;font-family:{FONT};">'
            f'<div class="em-muted" style="font-size:11px;letter-spacing:1.5px;color:{MUTED};'
            f'text-transform:uppercase;">{copy["codeTitle"]}</div>'
            f'<div class="em-ink" style="font-size:20px;color:{INK};font-weight:bold;'
            f'letter-spacing:1px;padding-top:5px;">{codigo}</div>'
            f'<div class="em-muted" style="font-size:12px;line-height:1.6;color:{MUTED};padding-top:6px;">'
            f'{copy["codeNote"]}</div>'
            f'</td></tr></table></td></tr>'
        )

    nombre = str(quote.get('client_name', '') or '').strip()
    saludo = copy['greet'].format(name=esc(nombre.upper())) if nombre else copy['greetPlain']
    intro = copy['intro'].format(advisor=esc(ATENCION_NOMBRE))
    folio = esc(str(quote.get('folio', '') or ''))
    link = quote.get('link') or 'https://exygenlabs.com/catalogo'

    # Los datos del cliente, si el distribuidor los puso. NINGUNO es obligatorio:
    # con solo el nombre el bloque no aparece (el saludo ya lo trae); con correo,
    # teléfono o dirección se pinta la tarjetita "Cotización para", como en la hoja.
    contacto = [str(quote.get(k, '') or '').strip()
                for k in ('client_email', 'client_phone', 'client_address')]
    contacto_html = ''
    if any(contacto):
        datos = [nombre] if nombre else []
        datos += [d for d in contacto if d]
        filas_contacto = ''.join(
            f'<div class="em-body" style="font-size:13px;line-height:1.7;color:{BODY};">{esc(d)}</div>'
            for d in datos)
        contacto_html = (
            f'<tr><td style="padding:18px 40px 0 40px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" '
            f'style="background-color:{BG};border:1px solid {LINE};border-radius:10px;">'
            f'<tr><td style="padding:14px 18px;font-family:{FONT};">'
            f'<div class="em-muted" style="font-size:11px;letter-spacing:1.5px;color:{MUTED};'
            f'text-transform:uppercase;padding-bottom:4px;">{copy["toLabel"]}</div>'
            f'{filas_contacto}'
            f'</td></tr></table></td></tr>'
        )

    folio_html = ''
    if folio:
        folio_html = (
            f'<tr><td style="padding:22px 40px 0 40px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-box" '
            f'style="background-color:{BG}; border:1px solid {LINE}; border-radius:10px;">'
            f'<tr><td align="center" style="padding:14px 20px; font-family:{FONT};">'
            f'<div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; '
            f'text-transform:uppercase;">{copy["folioLabel"]}</div>'
            f'<div class="em-ink" style="font-size:20px; color:{INK}; font-weight:bold; '
            f'letter-spacing:1px; padding-top:5px;">{folio}</div>'
            f'</td></tr></table></td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="es-MX">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{DARK_EMAIL_STYLE}</head>
<body class="em-bg" style="margin:0; padding:0; background-color:{BG};">
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">{copy['preheader'].format(total=_money(quote.get('total', 0)))}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="em-bg" style="background-color:{BG};">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" class="em-card" style="max-width:560px; width:100%; background-color:#FFFFFF; border:1px solid {LINE}; border-radius:14px;">

          <tr>
            <td align="center" style="padding:36px 40px 8px 40px;">
              <div class="em-ink" style="font-family:{FONT}; font-size:20px; letter-spacing:3px; color:{INK}; font-weight:bold;">EXYGEN&nbsp;LABS</div>
              <div class="em-muted" style="font-family:{FONT}; font-size:11px; letter-spacing:2px; color:{MUTED}; padding-top:4px;">RESEARCH PEPTIDES</div>
            </td>
          </tr>

          <tr>
            <td style="padding:28px 40px 0 40px; font-family:{FONT};">
              <h1 class="em-ink" style="margin:0; font-size:26px; line-height:1.25; color:{INK}; font-weight:bold;">{copy['heading']}</h1>
              <p class="em-body" style="margin:16px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{saludo}</p>
              <p class="em-body" style="margin:12px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{intro}</p>
            </td>
          </tr>
          {folio_html}
          {contacto_html}
          <tr>
            <td style="padding:26px 40px 0 40px; font-family:{FONT};">
              <div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; text-transform:uppercase; padding-bottom:4px;">{copy['items']}</div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table>
              <div class="em-muted" style="font-size:11px; letter-spacing:1.5px; color:{MUTED}; text-transform:uppercase; padding:18px 0 0 0;">{copy['money']}</div>
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(totals)}</table>
            </td>
          </tr>
          {badge}
          {code_html}
          <tr>
            <td align="center" style="padding:28px 40px 8px 40px;">
              <a href="{link}" class="em-btn" style="display:inline-block; background-color:{INK}; color:#FFFFFF; font-family:{FONT}; font-size:15px; font-weight:bold; text-decoration:none; padding:14px 36px; border-radius:999px;">{copy['cta']}</a>
              <div class="em-muted" style="padding-top:10px; font-family:{FONT}; font-size:12px; line-height:1.6; color:{MUTED};">{copy['ctaNote']}</div>
            </td>
          </tr>

          <tr>
            <td align="center" class="em-ink" style="padding:20px 40px 4px 40px; font-family:{FONT}; font-size:14px; font-weight:bold; letter-spacing:2px; color:{INK};">
              {copy['thanks']}
            </td>
          </tr>

          <tr>
            <td align="center" class="em-muted" style="padding:10px 40px 22px 40px; font-family:{FONT}; font-size:12px; color:{MUTED}; letter-spacing:0.5px;">
              Pureza HPLC &ge;99% &nbsp;&middot;&nbsp; {copy['validity']}
            </td>
          </tr>

          <tr><td style="padding:0 40px;"><div class="em-line" style="border-top:1px solid {LINE};"></div></td></tr>

          <tr>
            <td class="em-muted" style="padding:20px 40px 8px 40px; font-family:{FONT}; font-size:13px; line-height:1.6; color:{MUTED};">
              {copy['help']} <a href="mailto:{ATENCION_CORREO}" class="em-link" style="color:{INK};">{ATENCION_CORREO}</a>
            </td>
          </tr>

          <tr>
            <td class="em-footer" style="padding:12px 40px 28px 40px; font-family:{FONT}; font-size:11px; line-height:1.6; color:#A6ADBE;">
              {copy['ruo']}<br><br>
              &copy; 2026 Exygen Labs &middot; <a href="https://exygenlabs.com" class="em-footer" style="color:{MUTED};">exygenlabs.com</a>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


async def send_quote_email(to_address, quote, language=None, reply_to=None):
    """Manda la cotización al cliente del distribuidor. Devuelve True si salió.

    A diferencia de los demás correos, este SÍ informa el fracaso: no va colgado
    de una compra en segundo plano, sino de un botón que alguien acaba de pulsar,
    y decirle 'enviada' cuando no salió es mentirle en la cara."""
    if not email_enabled():
        logger.info('EMAIL_ENABLED != true, skipping quote email to %s', to_address)
        return False
    lang = normalize_language(language)
    copy = QUOTE_COPY[lang]
    folio = str(quote.get('folio', '') or '').strip()
    subject = QUOTE_SUBJECTS[lang].format(folio=f' {folio}' if folio else '')
    try:
        await asyncio.to_thread(_send_email_sync, to_address, subject,
                                _quote_email_html(copy, quote), reply_to)
        logger.info('Quote email sent to %s (folio=%s, lang=%s)', to_address, folio, lang)
        return True
    except Exception:
        logger.exception('Failed to send quote email to %s', to_address)
        return False
