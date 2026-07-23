import os
import jwt
import bcrypt
from datetime import datetime, timezone, timedelta
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import db

JWT_SECRET = os.environ.get('JWT_SECRET', 'nova-peptides-secret-key-change-me')
JWT_ALGORITHM = 'HS256'
JWT_EXPIRE_DAYS = 30

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def create_view_as_token(admin_id: str, target_id: str, minutes: int = 30) -> str:
    """Token de 'ver como' (solo lectura). Lleva marca `view_as` para que
    cualquier endpoint que ESCRIBE lo rechace. Vida corta a propósito."""
    payload = {
        'sub': target_id,
        'view_as': True,
        'admin_id': admin_id,
        'exp': datetime.now(timezone.utc) + timedelta(minutes=minutes),
        'iat': datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_token(user_id: str) -> str:
    payload = {
        'sub': user_id,
        'exp': datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
        'iat': datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='No autenticado')
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get('sub')
        view_as = bool(payload.get('view_as'))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Sesion expirada')
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Token invalido')

    user = await db.users.find_one({'id': user_id}, {'_id': 0, 'password_hash': 0, 'totp_secret': 0, 'totp_secret_pending': 0})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Usuario no encontrado')
    # Cuentas bloqueadas por el admin: fuera, aunque el token siga vigente.
    if user.get('blocked'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Esta cuenta esta deshabilitada')
    if view_as:
        user['view_as'] = True
        user['view_as_admin'] = payload.get('admin_id')
    return user


def deny_view_as(user):
    """Corta cualquier ESCRITURA hecha con un token de 'ver como'."""
    if user and user.get('view_as'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail='Modo "ver como": solo lectura')


async def get_optional_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials is None:
        return None
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user = await db.users.find_one({'id': payload.get('sub')}, {'_id': 0, 'password_hash': 0, 'totp_secret': 0, 'totp_secret_pending': 0})
        return None if (user and user.get('blocked')) else user
    except Exception:
        return None


async def get_current_admin(user=Depends(get_current_user)):
    if user.get('role') != 'admin':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Acceso solo para administradores')
    return user


async def get_current_distributor(user=Depends(get_current_user)):
    if user.get('role') not in ('distributor', 'admin'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Acceso solo para distribuidores')
    return user
