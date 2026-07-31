"""DEVUELVE SUS CÓDIGOS A QUIEN NUNCA DEBIÓ PERDERLOS (Christián, 2026-07-31).

Qué pasó
--------
La orden de privacidad de esa mañana era sobre MARÍA: «los clientes no pueden
ver que el código de descuento es de María». La rotación que se hizo la tomó
como una regla global y le puso el prefijo de la casa (`MONICAF`) a TODOS los
distribuidores. Alanís y Javier quedaron con códigos que no decían su nombre sin
que nadie lo hubiera pedido.

La corrección vive en el generador (`server.prefijo_de`): el prefijo de la casa
lo usa QUIEN LO TRAIGA MARCADO EN SU FICHA (`users.code_prefix`), no todo el
mundo. Este script pone la base de datos de acuerdo con esa regla:

  · A María se le MARCA la ficha (`code_prefix = 'MONICAF'`) para que su rotación
    se sostenga sola la próxima vez que se emitan códigos.
  · A los demás se les DEVUELVEN sus códigos con su nombre.

⛔ AQUÍ NO SE MATA NI UN CÓDIGO. Ese fue el punto que costó trabajo lograr el
   mismo día y sería absurdo tirarlo ahora para arreglar otra cosa. Lo que se
   hace es intercambiar cuál se REPARTE y cuál está JUBILADO:

     - los `MONICAF-*` que se les crearon quedan JUBILADOS: siguen cobrando, con
       su mismo descuento y atribuyendo al mismo distribuidor, hasta su
       caducidad (ver `_codigos_jubilados` y el periodo de gracia en
       `_ensure_distributor_codes`). Sólo dejan de ser los que se reparten.
     - sus códigos de siempre (`ALANIS-*`, `JAVIER-*`) vuelven a ser los
       VIGENTES, con su caducidad original intacta.
     - el código ÚNICO legacy (`users.distributor_code`) vuelve a ser el suyo
       (`ALAN-2292`, `JAVI-7116`) y el `MONICAF-NNNN` que lo había sustituido se
       guarda en `discount_codes` como jubilado — misma mudanza que hace
       `_rotar_codigo_unico`, en sentido contrario, para que tampoco ése muera.

   Si alguno de los `MONICAF-*` YA SE USÓ, el script lo dice y NO lo toca.

Uso (en el servidor, donde MONGO_URL ya está en el entorno):

    ./.venv/bin/python arreglar_codigos_monicaf.py --ver        # sólo mirar
    ./.venv/bin/python arreglar_codigos_monicaf.py --aplicar

Es IDEMPOTENTE: correrlo dos veces no cambia nada la segunda vez.
"""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

from database import db
from server import CODE_TTL_DAYS, PREFIJO_CODIGO

# La única ficha que lleva el prefijo de la casa. Por correo, que es lo que no
# cambia: el id es distinto en cada base y el nombre se escribe de tres maneras.
MARCADOS = {'marianeunfeld0@gmail.com': PREFIJO_CODIGO}

ahora = lambda: datetime.now(timezone.utc).isoformat()
es_de_la_casa = lambda code: str(code or '').upper().startswith(PREFIJO_CODIGO + '-')


async def _ya_se_uso(doc: dict, dist_id: str) -> list:
    """¿Ese código alcanzó a moverle algo a alguien? Si contesta algo, no se toca.

    ⚠️ El texto del código NO se guarda en el pedido: la orden sólo apunta al
    distribuidor (`referred_by`). Así que no hay forma de preguntar «¿alguien
    tecleó MONICAF-15-UTNG?» y se pregunta lo único que sí se puede saber con
    certeza: si a ese distribuidor le entró ALGO —una venta o un cliente nuevo—
    desde que el código nació. Si no le entró nada, el código no pudo usarse.
    Prefiere equivocarse de más: cualquier movimiento posterior lo deja quieto."""
    desde = doc.get('created_at') or ''
    huellas = []
    n = await db.orders.count_documents({'referred_by': dist_id, 'created_at': {'$gte': desde}})
    if n:
        huellas.append(f'{n} pedido(s) de ese distribuidor desde que nació')
    n = await db.users.count_documents({'referred_by': dist_id, 'created_at': {'$gte': desde}})
    if n:
        huellas.append(f'{n} cliente(s) registrados desde que nació')
    return huellas


async def _arreglar(dist, aplicar):
    """Devuelve a UN distribuidor sus códigos con su nombre. Devuelve el reporte."""
    lineas = []
    hoy = ahora()
    codes = await db.discount_codes.find({'distributor_id': dist['id']}).to_list(500)

    # 1. Los MONICAF-* que se le crearon: a JUBILADOS (siguen cobrando).
    for c in codes:
        if not es_de_la_casa(c.get('code')) or c.get('superseded_at') or not c.get('active', True):
            continue
        usos = await _ya_se_uso(c, dist['id'])
        if usos:
            lineas.append(f"   ⚠ {c['code']} YA SE USÓ ({', '.join(usos)}): lo dejo VIGENTE")
            continue
        lineas.append(f"   {c['code']} → jubilado (sigue cobrando hasta {(c.get('expires_at') or '')[:10]})")
        if aplicar:
            await db.discount_codes.update_one({'id': c['id']}, {'$set': {'superseded_at': hoy}})

    # 2. Los suyos de siempre, que la rotación había jubilado: vuelven a ser los
    #    VIGENTES. El legacy no entra aquí: ése se trata abajo, en el paso 3.
    for c in codes:
        if es_de_la_casa(c.get('code')) or not c.get('superseded_at') or c.get('legacy'):
            continue
        if not c.get('active', True):
            continue
        lineas.append(f"   {c['code']} → vigente otra vez ({int(round(c.get('discount_rate', 0) * 100))}%)")
        if aplicar:
            await db.discount_codes.update_one({'id': c['id']}, {'$set': {'superseded_at': None}})

    # 3. El código ÚNICO legacy: la mudanza al revés.
    unico = (dist.get('distributor_code') or '').strip().upper()
    if es_de_la_casa(unico):
        viejo = next((c for c in codes if c.get('legacy') and not es_de_la_casa(c.get('code'))), None)
        if not viejo:
            lineas.append(f'   ⚠ {unico}: no encuentro su código único anterior; lo dejo como está')
        elif await _ya_se_uso({'created_at': viejo.get('created_at')}, dist['id']):
            lineas.append(f'   ⚠ {unico} YA SE USÓ: lo dejo como código único')
        else:
            lineas.append(f"   {unico} → jubilado · {viejo['code']} → código único otra vez")
            if aplicar:
                # El MONICAF-NNNN sólo existe en la ficha (un campo, un texto), así
                # que antes de soltarlo se copia a `discount_codes` como jubilado.
                if not await db.discount_codes.find_one({'code': unico}):
                    await db.discount_codes.insert_one({
                        'id': str(uuid.uuid4()), 'distributor_id': dist['id'], 'code': unico,
                        'discount_rate': float(dist.get('customer_discount_rate') or 0),
                        'active': True, 'created_at': hoy, 'superseded_at': hoy, 'legacy': True,
                        'expires_at': (datetime.now(timezone.utc)
                                       + timedelta(days=CODE_TTL_DAYS)).isoformat()})
                await db.users.update_one({'id': dist['id']},
                                          {'$set': {'distributor_code': viejo['code']}})
                # Y el suyo se va de `discount_codes`: vuelve a vivir en la ficha,
                # que es su casa. Dejarlo en las dos lo pintaría de "jubilado" en el
                # panel del distribuidor cuando en realidad es el que se reparte.
                await db.discount_codes.delete_one({'id': viejo['id']})
    return lineas


async def main():
    aplicar = '--aplicar' in sys.argv
    if not aplicar and '--ver' not in sys.argv:
        print(__doc__)
        sys.exit(1)

    print('APLICANDO' if aplicar else 'SÓLO MIRANDO (usa --aplicar para escribir)')
    dists = await db.users.find({'role': 'distributor'}, {'_id': 0, 'password_hash': 0}).to_list(500)
    for dist in sorted(dists, key=lambda d: d.get('name') or ''):
        correo = (dist.get('email') or '').lower()
        marca = MARCADOS.get(correo)
        print(f"\n{dist.get('name')} <{correo}>")
        if marca:
            if dist.get('code_prefix') == marca:
                print(f'   ya marcada con {marca}: no toco nada de lo suyo')
            else:
                print(f'   marca en su ficha: code_prefix = {marca} (sus códigos NO se tocan)')
                if aplicar:
                    await db.users.update_one({'id': dist['id']}, {'$set': {'code_prefix': marca}})
            continue
        if dist.get('code_prefix'):
            if aplicar:
                await db.users.update_one({'id': dist['id']}, {'$unset': {'code_prefix': ''}})
            print('   le quito la marca de la ficha')
        lineas = await _arreglar(dist, aplicar)
        print('\n'.join(lineas) if lineas else '   nada que hacer')

    print('\nListo.' if aplicar else '\nNada se escribió.')


if __name__ == '__main__':
    asyncio.run(main())
