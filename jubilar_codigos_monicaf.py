"""JUBILA LOS OCHO `MONICAF-*` DE ALANÍS Y JAVIER (Christián, 2026-07-31).

Sus palabras: **«Jubila los códigos MONICAF de Javier y Alanís.»**

Qué se jubila y por qué
-----------------------
El 31-jul una rotación tomó una orden de privacidad que era SOBRE MARÍA («los
clientes no pueden ver que el código de descuento es de María») y se la aplicó a
todo el mundo: a Alanís y a Javier se les crearon códigos con el prefijo de la
casa que nadie había pedido. `arreglar_codigos_monicaf.py` ya les devolvió sus
códigos con su nombre ese mismo día, pero dejó los `MONICAF-*` **cobrando**, por
la regla de la casa de no matar un código que alguien pueda traer en la mano.

Hoy Christián cierra el asunto: esos ocho **dejan de servir**. Se puede porque
NUNCA TUVIERON MOVIMIENTO — cero clientes, cero pedidos, cero subdistribuidores
para los dos— así que apagarlos no le quita el descuento a ningún cliente. El
script lo COMPRUEBA antes de tocar nada y se niega a apagar el que sí se haya
usado.

⛔ LO QUE NO SE TOCA, y es la mitad del encargo:

  · los códigos con SU nombre (`ALANIS-*`, `JAVIER-*`, `ALAN-2292`, `JAVI-7116`):
    son los que se reparten desde el 31-jul;
  · los `MONICAF-*` de **María**, que son los suyos de verdad — ella SÍ pidió el
    prefijo de la casa;
  · sus `MARIAN-*` y `MARI-3537`, en periodo de gracia hasta el 29-oct.

Por eso la lista de abajo es EXPLÍCITA, código por código, y además se verifica
el dueño: un `startswith('MONICAF-')` habría apagado los cuatro de María.

Cómo se apaga
-------------
`active: False` **y** `superseded_at`. Los dos, y no es de más:

  · `active: False` es lo que lo mata de verdad: `_resolve_code` sólo mira los
    activos, así que el checkout deja de reconocerlo (y no cae al código único
    legacy, porque ninguna ficha lo trae ya);
  · `superseded_at` es el candado contra la RESURRECCIÓN. Sin él,
    `_ensure_distributor_codes` ve un documento «muerto» de un nivel que sí
    aplica (15/20/25%) y lo REESCRIBE EN SU SITIO con texto nuevo y
    `active: True` — el mismo renglón volvería a la vida en la primera lectura
    de `/distributor/codes`. Con `superseded_at` puesto, ese barrido lo salta.

Y `active: False` los deja fuera de `_codigos_jubilados()`, que lista los que ya
no se reparten pero TODAVÍA COBRAN: éstos ya no cobran, así que tampoco tienen
por qué aparecerle al distribuidor como si sirvieran.

Uso (en el servidor, donde MONGO_URL ya está en el entorno):

    ./.venv/bin/python jubilar_codigos_monicaf.py --ver
    ./.venv/bin/python jubilar_codigos_monicaf.py --aplicar

Es IDEMPOTENTE: correrlo dos veces no cambia nada la segunda vez.
"""
import asyncio
import sys
from datetime import datetime, timezone

from database import db

MOTIVO = 'orden de Christián 2026-07-31: «Jubila los códigos MONICAF de Javier y Alanís.»'

# ⛔ LA LISTA ES EL PERMISO. Autoriza ESOS ocho textos y ninguno más, igual que
# `subidas_autorizadas.json` en el motor de precios: un permiso abierto («los
# MONICAF de los que no son María») apagaría mañana, solo, un código que alguien
# creara por otra razón. El correo es parte del permiso porque el `id` cambia de
# una base a otra y el nombre se escribe de tres maneras.
A_JUBILAR = {
    'alexfermc@hotmail.com': ['MONICAF-15-UTNG', 'MONICAF-20-UEQZ',
                              'MONICAF-25-7Y4S', 'MONICAF-5313'],
    'javier.rojomor@gmail.com': ['MONICAF-15-CDSB', 'MONICAF-20-1XYE',
                                 'MONICAF-25-PDV9', 'MONICAF-5659'],
}

ahora = lambda: datetime.now(timezone.utc).isoformat()


async def _movimiento(dist_id, desde):
    """¿Le entró ALGO a ese distribuidor desde que nació el código?

    ⚠️ El texto del código NO se guarda en el pedido: la orden sólo apunta al
    distribuidor (`referred_by`). Así que no hay forma de preguntar «¿alguien
    tecleó MONICAF-15-UTNG?» y se pregunta lo único que sí se sabe con certeza.
    Prefiere equivocarse de más: cualquier rastro deja el código encendido."""
    huellas = []
    n = await db.orders.count_documents({'referred_by': dist_id,
                                         'created_at': {'$gte': desde or ''}})
    if n:
        huellas.append(f'{n} pedido(s)')
    n = await db.users.count_documents({'referred_by': dist_id,
                                        'created_at': {'$gte': desde or ''}})
    if n:
        huellas.append(f'{n} cliente(s)')
    return huellas


async def main():
    aplicar = '--aplicar' in sys.argv
    if not aplicar and '--ver' not in sys.argv:
        print(__doc__)
        sys.exit(1)

    print('APLICANDO' if aplicar else 'SÓLO MIRANDO (usa --aplicar para escribir)')
    apagados = vivos = ya_estaban = 0
    for correo, codigos in A_JUBILAR.items():
        dist = await db.users.find_one({'email': correo, 'role': 'distributor'},
                                       {'_id': 0, 'password_hash': 0})
        print(f'\n{correo}')
        if not dist:
            print('   ⚠ no existe esa ficha de distribuidor: no toco nada')
            continue
        # El código único de la ficha NO puede ser uno de los que se apagan, o el
        # distribuidor se quedaría sin ninguno. Hoy es ALAN-2292 / JAVI-7116.
        unico = (dist.get('distributor_code') or '').strip().upper()
        if unico in codigos:
            print(f'   ⛔ {unico} sigue siendo su código ÚNICO: no lo apago. '
                  'Corre antes arreglar_codigos_monicaf.py --aplicar')
            continue
        print(f"   su código único es {unico} y NO se toca")
        for code in codigos:
            doc = await db.discount_codes.find_one({'code': code})
            if not doc:
                print(f'   ⚠ {code:<18} no existe en la base: nada que hacer')
                continue
            if doc.get('distributor_id') != dist['id']:
                print(f'   ⛔ {code:<18} NO es de esta ficha: no lo toco')
                continue
            if not doc.get('active', True):
                print(f'   · {code:<18} ya estaba jubilado')
                ya_estaban += 1
                continue
            huellas = await _movimiento(dist['id'], doc.get('created_at'))
            if huellas:
                print(f"   ⚠ {code:<18} YA SE USÓ ({', '.join(huellas)}): "
                      'lo dejo VIGENTE')
                vivos += 1
                continue
            print(f"   {code:<18} → jubilado (deja de dar su "
                  f"{int(round(doc.get('discount_rate', 0) * 100))}%)")
            apagados += 1
            if aplicar:
                await db.discount_codes.update_one(
                    {'id': doc['id']},
                    {'$set': {'active': False, 'superseded_at': ahora(),
                              'retired_at': ahora(), 'retired_reason': MOTIVO}})

    print(f'\n{apagados} por jubilar · {ya_estaban} ya lo estaban · '
          f'{vivos} se quedan vigentes porque tuvieron movimiento')
    print('Listo.' if aplicar else 'Nada se escribió.')


if __name__ == '__main__':
    asyncio.run(main())
