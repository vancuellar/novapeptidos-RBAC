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


def _send_via_ses(to_address, subject, html_body):
    region = os.environ.get('SES_REGION', 'us-east-1')
    ses = boto3.client('sesv2', region_name=region)
    ses.send_email(
        FromEmailAddress=_sender(),
        Destination={'ToAddresses': [to_address]},
        Content={'Simple': {
            'Subject': {'Data': subject, 'Charset': 'UTF-8'},
            'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'}},
        }},
    )


def _send_via_resend(to_address, subject, html_body):
    """Resend por HTTP. No necesita SDK y no tiene sandbox que pedir."""
    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        raise RuntimeError('RESEND_API_KEY is not configured.')
    resp = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'from': _sender(), 'to': [to_address], 'subject': subject, 'html': html_body},
        timeout=20,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f'Resend {resp.status_code}: {resp.text[:300]}')


PROVIDERS = {'ses': _send_via_ses, 'resend': _send_via_resend}


def _send_email_sync(to_address, subject, html_body):
    """Despacha al proveedor configurado. `EMAIL_PROVIDER` = ses | resend."""
    name = os.environ.get('EMAIL_PROVIDER', 'ses').strip().lower()
    send = PROVIDERS.get(name)
    if not send:
        raise RuntimeError(f'EMAIL_PROVIDER desconocido: {name}')
    send(to_address, subject, html_body)


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


async def send_reset_email(name, email, link, language=None):
    """Correo de restablecimiento de contrasena."""
    await _send_action_email(name, email, link, language, RESET_SUBJECTS, RESET_BODIES, 'reset')


async def send_verification_email(name, email, link, language=None):
    """Confirmacion de correo tras registrarse. Sin esto no se puede entrar."""
    await _send_action_email(name, email, link, language, VERIFY_SUBJECTS, VERIFY_BODIES, 'verification')


async def send_invitation_email(name, email, link, language=None):
    """Invitacion a un cliente o distribuidor creado desde el admin."""
    await _send_action_email(name, email, link, language, INVITE_SUBJECTS, INVITE_BODIES, 'invitation')


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
    """Aviso al cliente de que su pago quedo confirmado (SPEI verificado por el
    admin, o cripto liquidada). Nunca lanza."""
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
        'ruo': 'Uso exclusivo en investigacion (RUO). No es un medicamento ni un suplemento; no esta destinado a consumo humano ni animal.',
        'help': 'Cualquier duda, responde a este correo o escribenos a',
        'savings': 'AHORRASTE {amount}',
        'points': 'GANAS {points} PUNTOS CON ESTA COMPRA',
        'pointsUsed': 'Puntos canjeados',
        'thanks': 'GRACIAS POR TU COMPRA',
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
        'ruo': 'Research use only (RUO). Not a medicine or a supplement; not intended for human or animal consumption.',
        'help': 'Any questions, reply to this email or write to',
        'savings': 'YOU SAVED {amount}',
        'points': 'YOU EARN {points} POINTS WITH THIS ORDER',
        'pointsUsed': 'Points redeemed',
        'thanks': 'THANK YOU FOR YOUR ORDER',
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
        'ruo': 'Uso exclusivo em pesquisa (RUO). Nao e medicamento nem suplemento; nao se destina ao consumo humano ou animal.',
        'help': 'Qualquer duvida, responda a este e-mail ou escreva para',
        'savings': 'VOCE ECONOMIZOU {amount}',
        'points': 'VOCE GANHA {points} PONTOS COM ESTA COMPRA',
        'pointsUsed': 'Pontos resgatados',
        'thanks': 'OBRIGADO PELA SUA COMPRA',
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


def _order_email_html(order, copy, link):
    """Mismo lenguaje visual que el correo de bienvenida: tarjeta blanca sobre
    fondo gris, tablas anidadas y estilos en linea, que es lo unico que rinde
    parejo en Gmail, Outlook y Apple Mail. Con version oscura via clases em-*
    (ver DARK_EMAIL_STYLE)."""
    esc = html.escape
    INK, BODY, MUTED, LINE, BG = '#132763', '#3D4657', '#8A93A8', '#E4E8F0', '#FBFCFE'
    FONT = 'Helvetica,Arial,sans-serif'

    rows = []
    for item in order.get('items', []):
        qty = int(item.get('quantity', 1) or 1)
        line_total = float(item.get('price', 0) or 0) * qty
        rows.append(
            f'<tr>'
            f'<td class="em-body em-line" style="padding:10px 0;border-bottom:1px solid {LINE};font-family:{FONT};'
            f'font-size:14px;line-height:1.5;color:{BODY};">{esc(str(item.get("name", "")))}'
            f'<span class="em-muted" style="color:{MUTED};">&nbsp;&times;{qty}</span></td>'
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
                                        customer.get('state', ''), customer.get('postal_code', '')] if b))
    is_spei = (order.get('payment_method') or '') == 'spei'
    next_text = copy['nextSpei'] if is_spei else copy['nextCard']
    spei = order.get('spei') if is_spei else None
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
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">{copy['preheader'].format(number=number)}</div>
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
              <p class="em-body" style="margin:16px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{copy['greet'].format(name=esc(str(customer.get('full_name', '')).upper()))}</p>
              <p class="em-body" style="margin:12px 0 0 0; font-size:15px; line-height:1.6; color:{BODY};">{copy['intro']}</p>
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
              <a href="{link}" class="em-btn" style="display:inline-block; background-color:{INK}; color:#FFFFFF; font-family:{FONT}; font-size:15px; font-weight:bold; text-decoration:none; padding:14px 36px; border-radius:999px;">{copy['track']}</a>
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


async def send_order_email(order, language=None):
    """Confirmacion de pedido. Nunca lanza: una compra no puede fallar porque
    el proveedor de correo este caido."""
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
    link = f"{site}/pedido/{order.get('order_number', '')}"
    subject = ORDER_SUBJECTS[lang].format(number=order.get('order_number', ''))
    try:
        await asyncio.to_thread(_send_email_sync, to_address, subject, _order_email_html(order, copy, link))
        logger.info('Order email sent to %s (order=%s, lang=%s)', to_address, order.get('order_number'), lang)
    except Exception:
        logger.exception('Failed to send order email for %s', order.get('order_number'))
