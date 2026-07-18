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
