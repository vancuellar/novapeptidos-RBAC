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
    """Plantilla comun de los correos con un boton de accion."""
    return f"""
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <p style="font-size:15px;color:#3D4657;margin-top:28px;">{greet.format(name=html.escape(name))}</p>
      <p style="font-size:15px;color:#3D4657;line-height:1.6;">{body.format(email=html.escape(email))}</p>
      <p style="text-align:center;margin:28px 0;">
        <a href="{link}" style="display:inline-block;background-color:#132763;color:#FFFFFF;font-size:15px;font-weight:bold;text-decoration:none;padding:14px 36px;border-radius:999px;">{cta}</a>
      </p>
      <p style="font-size:13px;color:#8A93A8;line-height:1.6;word-break:break-all;">
        Si el boton no funciona, copia y pega este enlace:<br>{html.escape(link)}
      </p>
      <p style="font-size:13px;color:#8A93A8;line-height:1.6;">{footer}</p>
    </div>
    """


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
