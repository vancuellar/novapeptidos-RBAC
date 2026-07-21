"""Segundo factor (TOTP) para administradores: helpers puros y testeables.

El TOTP es el codigo de 6 digitos de una app autenticadora (Google
Authenticator, 1Password, etc.). Solo lo exigimos a cuentas admin: para
clientes la friccion no paga; su via segura es Google o una llave de acceso.
"""
import base64
import io

import pyotp
import qrcode

ISSUER = 'Exygen Labs'


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    """URI otpauth:// que entienden todas las apps autenticadoras."""
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify_totp(secret: str, code) -> bool:
    """Valida el codigo con 1 intervalo de tolerancia (relojes desfasados)."""
    if not secret or not code:
        return False
    cleaned = str(code).strip().replace(' ', '')
    try:
        return pyotp.TOTP(secret).verify(cleaned, valid_window=1)
    except Exception:
        return False


def qr_data_uri(text: str) -> str:
    """El QR como data URI: no se guarda en disco ni pasa por terceros."""
    img = qrcode.make(text, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
