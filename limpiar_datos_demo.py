"""Borra los datos DEMO (sembrados) para empezar a medir trafico real.

Se corre UNA vez, dentro del servidor:
    sudo docker compose exec api python limpiar_datos_demo.py --confirmar

Sin --confirmar solo ENSENA lo que borraria (modo simulacro). Nunca toca:
  - clientes ni distribuidores reales (ids UUID)
  - pedidos de clientes reales
  - el catalogo de productos ni los precios
"""
import argparse
import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient

# Todo lo sembrado por seed_data lleva id 'seed-*'. Los usuarios reales usan UUID.
PREFIJO_DEMO = 'seed-'

# Pedidos de prueba que dejaron las compras E2E (invitados, sin cliente detras).
# Son mios, no de un cliente: se van con nombre y apellido para no borrar de mas.
PEDIDOS_PRUEBA = [
    'EX-20260725-3835',
    'EX-20260725-7454',
    'EX-20260725-7444',
    'EX-20260725-4963',
    'EX-20260725-1912',
]


async def main(confirmar: bool) -> None:
    cliente = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = cliente[os.environ.get('DB_NAME', 'exygen')]

    filtro_demo = {'id': {'$regex': f'^{PREFIJO_DEMO}'}}
    filtro_pedidos = {'$or': [
        {'user_id': {'$regex': f'^{PREFIJO_DEMO}'}},
        {'order_number': {'$in': PEDIDOS_PRUEBA}},
    ]}

    usuarios = await db.users.count_documents(filtro_demo)
    pedidos = await db.orders.count_documents(filtro_pedidos)
    eventos = await db.events.count_documents({})

    ingreso_falso = 0
    async for o in db.orders.find(filtro_pedidos, {'_id': 0, 'total': 1}):
        ingreso_falso += o.get('total') or 0

    print(f'usuarios demo .... {usuarios}')
    print(f'pedidos demo ..... {pedidos}  (${ingreso_falso:,.0f} de ingreso falso)')
    print(f'eventos embudo ... {eventos}  (contaminados con las pruebas)')

    # Lo que sobrevive, para poder verificarlo de un vistazo.
    reales = await db.users.count_documents({'id': {'$not': {'$regex': f'^{PREFIJO_DEMO}'}}})
    pedidos_reales = await db.orders.count_documents({'$nor': [filtro_pedidos]})
    print(f'\nse conservan ..... {reales} personas reales y {pedidos_reales} pedidos reales')

    if not confirmar:
        print('\nSIMULACRO. Nada se borro. Corre con --confirmar para ejecutar.')
        return

    r1 = await db.users.delete_many(filtro_demo)
    r2 = await db.orders.delete_many(filtro_pedidos)
    r3 = await db.events.delete_many({})
    print(f'\nborrados: {r1.deleted_count} usuarios, {r2.deleted_count} pedidos, '
          f'{r3.deleted_count} eventos')

    # Rastros que colgaban de las personas falsas: comisiones, puntos, avisos.
    de_usuario_demo = {'user_id': {'$regex': f'^{PREFIJO_DEMO}'}}
    for nombre in ('commissions', 'loyalty_ledger', 'points', 'notifications'):
        r = await db[nombre].delete_many(de_usuario_demo)
        if r.deleted_count:
            print(f'  + {r.deleted_count} en {nombre}')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--confirmar', action='store_true', help='ejecuta el borrado de verdad')
    asyncio.run(main(p.parse_args().confirmar))
