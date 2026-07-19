import os
import html
import asyncio
import logging
from pathlib import Path

import boto3

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


def _send_email_sync(to_address, subject, html_body):
    region = os.environ.get('SES_REGION', 'us-east-1')
    sender = os.environ.get('EMAIL_FROM', 'Exygen Labs <hola@exygenlabs.com>')
    ses = boto3.client('sesv2', region_name=region)
    ses.send_email(
        FromEmailAddress=sender,
        Destination={'ToAddresses': [to_address]},
        Content={'Simple': {
            'Subject': {'Data': subject, 'Charset': 'UTF-8'},
            'Body': {'Html': {'Data': html_body, 'Charset': 'UTF-8'}},
        }},
    )


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


async def send_reset_email(name, email, link, language=None):
    """Correo de restablecimiento de contrasena. Nunca lanza excepcion."""
    if os.environ.get('EMAIL_ENABLED', 'false').lower() != 'true':
        logger.info('EMAIL_ENABLED != true, skipping reset email to %s', email)
        return
    lang = normalize_language(language)
    greet, body, cta, footer = RESET_BODIES[lang]
    html_body = f"""
    <div style="max-width:560px;margin:0 auto;font-family:Helvetica,Arial,sans-serif;padding:32px 24px;">
      <div style="text-align:center;font-size:20px;letter-spacing:3px;color:#132763;font-weight:bold;">EXYGEN&nbsp;LABS</div>
      <div style="text-align:center;font-size:11px;letter-spacing:2px;color:#8A93A8;padding-top:4px;">RESEARCH PEPTIDES</div>
      <p style="font-size:15px;color:#3D4657;margin-top:28px;">{greet.format(name=html.escape(name))}</p>
      <p style="font-size:15px;color:#3D4657;line-height:1.6;">{body.format(email=html.escape(email))}</p>
      <p style="text-align:center;margin:28px 0;">
        <a href="{link}" style="display:inline-block;background-color:#132763;color:#FFFFFF;font-size:15px;font-weight:bold;text-decoration:none;padding:14px 36px;border-radius:999px;">{cta}</a>
      </p>
      <p style="font-size:13px;color:#8A93A8;line-height:1.6;">{footer}</p>
    </div>
    """
    try:
        await asyncio.to_thread(_send_email_sync, email, RESET_SUBJECTS[lang], html_body)
        logger.info('Reset email sent to %s (lang=%s)', email, lang)
    except Exception:
        logger.exception('Failed to send reset email to %s', email)


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
