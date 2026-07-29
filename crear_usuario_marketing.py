"""Crea (o promueve) una cuenta con el rol 'marketing': SOLO difusión.

El rol 'marketing' entra únicamente a Embudo, Marketing y Anuncios (Meta) del
Admin. Todo lo demás (pedidos, clientes, stock, cobros, precios, distribuidores)
le devuelve 403 desde el backend — la seguridad no depende del frontend.

Uso (en el servidor, donde MONGO_URL ya está en el entorno):

    ./.venv/bin/python crear_usuario_marketing.py "María Neunfeld" marianeunfeld0@gmail.com 'ContraseñaSegura'

- Si el correo NO existe: crea la cuenta ya confirmada, con esa contraseña.
- Si el correo YA existe: le pone el rol 'marketing' y le cambia la contraseña.
  Se niega a tocar una cuenta 'admin' (bajarle el rol al admin sería un desastre).
"""
import asyncio
import sys
import uuid
from datetime import datetime, timezone

from auth import hash_password
from database import db


async def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    name, email, password = sys.argv[1], sys.argv[2].strip().lower(), sys.argv[3]
    if len(password) < 8:
        print('La contraseña debe tener al menos 8 caracteres.')
        sys.exit(1)

    now = datetime.now(timezone.utc).isoformat()
    existing = await db.users.find_one({'email': email})
    if existing:
        if existing.get('role') == 'admin':
            print(f'{email} es ADMIN; no lo voy a degradar a marketing. Nada cambió.')
            sys.exit(1)
        await db.users.update_one({'email': email}, {'$set': {
            'role': 'marketing',
            'name': name,
            'password_hash': hash_password(password),
            'email_verified': True,
            'blocked': False,
        }})
        print(f'Listo: {email} ahora tiene rol marketing (cuenta existente actualizada).')
        return

    await db.users.insert_one({
        'id': str(uuid.uuid4()),
        'name': name,
        'email': email,
        'password_hash': hash_password(password),
        'role': 'marketing',
        'language': 'es',
        'email_verified': True,
        'created_at': now,
    })
    print(f'Listo: cuenta marketing creada para {email}. Entra en /login con esa contraseña.')


if __name__ == '__main__':
    asyncio.run(main())
