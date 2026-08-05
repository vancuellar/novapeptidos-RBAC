"""LA GUÍA, LISTA PARA IMPRIMIR DESDE EL PANEL — sin salir a la página de nadie.

⛔ ORDEN DE CHRISTIÁN (2026-07-31): «¿Puedes hacer que recibamos la guía para imprimir
en nuestro panel de distribuidor o admin panel? Recuerda que quiero manejar TODO desde
nuestra app». Hasta hoy el PDF de la etiqueta era un enlace crudo a la paquetería que
sólo veía el admin, y sólo si el `label_url` ya estaba escrito en el pedido.

Tres cosas que este archivo resuelve y que conviene no perder de vista:

  1. ⛔ EL PDF LO SIRVE LA CASA, NO LA PAQUETERÍA. La pantalla nunca recibe la URL del
     proveedor: pide `/…/etiqueta` a nuestro servidor y le llegan los bytes del PDF.
     Así el candado de rol se aplica en el servidor (un distribuidor sólo saca las
     etiquetas de SUS pedidos) y no hay forma de reenviarle a nadie una liga que da
     acceso a la cuenta de envíos.

  2. ⛔ UNA LIGA FIRMADA QUE CADUCÓ NO PUEDE VERSE COMO UN BOTÓN ROTO. El `label_url`
     que guarda el pedido es una URL firmada del proveedor: caduca. Aquí se INTENTA
     bajar y, si el proveedor ya no la sirve (o devuelve una página de error en vez de
     un PDF), se vuelve a pedir la etiqueta por número de rastreo, se guarda la nueva y
     se reintenta. Al que imprime esto no le importa y no tiene por qué enterarse.

  3. ⛔ EL PDF LLEGA SEGUNDOS DESPUÉS DE COMPRAR LA GUÍA. En la primera compra real la
     paquetería contestó al instante con el rastreo y el `label_url` VACÍO. Si todavía
     no está, esto NO inventa un error feo: contesta 409 con `estado: generando`, que
     es lo que la pantalla convierte en «Generando…» y reintenta sola.

  4. ⛔ NO DEPENDE DE LA PLATAFORMA. Hay dos proveedores (Skydropx y Envíos
     Internacionales) y cada pedido guarda con cuál se compró (`label_provider`).
     Todo pasa por `paqueterias.modulo(...)`, igual que el rescate: agregar un tercero
     no toca este archivo.

Lo que este archivo NO hace, a propósito: no compra nada, no cobra, no cambia el
pedido más que para refrescar la dirección del papel de una guía YA PAGADA.
"""
import asyncio
import logging

import requests
from fastapi import APIRouter, Depends, HTTPException, Response

import paqueterias
from auth import get_current_admin, get_current_distributor
from database import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api')

# Bajar un PDF de la paquetería. Corto a propósito: quien está parado frente a la
# impresora con el paquete en la mano prefiere un «Generando…» y reintentar, a una
# pantalla congelada medio minuto.
TIMEOUT_PDF = 20


class EtiquetaNoLista(Exception):
    """Todavía no hay PDF que imprimir. NO es una falla: es un «espérate tantito».

    Se distingue de un error de verdad porque lo que hay que hacer es distinto: aquí
    no se avisa a nadie ni se reintenta la compra, sólo se vuelve a preguntar en unos
    segundos. Por eso es su propia clase y no un `RuntimeError` pelón.

    `estado` separa dos cosas que se veían iguales y NO se arreglan igual:
      · `generando` — el papel viene en camino: hay que esperar y reintentar.
      · `manual`    — no hay papel nuestro y no lo va a haber (la guía se tecleó a
                      mano y la paquetería no nos la reconoce). Reintentar es perder
                      el tiempo; lo que toca es ir por el PDF a donde se compró.
    """

    def __init__(self, mensaje: str, estado: str = 'generando'):
        super().__init__(mensaje)
        self.estado = estado


def _parece_pdf(contenido: bytes) -> bool:
    """¿Esto es de verdad un PDF? Una liga firmada que caducó no da 404: muchas veces
    devuelve 200 con una página de error en HTML. Imprimir eso saca una hoja con la
    palabra «Forbidden» y el paquete se va sin etiqueta."""
    return bool(contenido) and contenido[:5].startswith(b'%PDF')


def _bajar(url: str) -> bytes:
    """Los bytes del PDF, o vacío si esa liga ya no sirve. Nunca revienta."""
    if not url:
        return b''
    try:
        r = requests.get(url, timeout=TIMEOUT_PDF, allow_redirects=True)
    except Exception as e:
        logger.warning('Etiqueta: no se pudo bajar el PDF: %s', e)
        return b''
    if r.status_code != 200 or not _parece_pdf(r.content):
        logger.info('Etiqueta: la liga guardada ya no sirve (HTTP %s, %s bytes)',
                    r.status_code, len(r.content or b''))
        return b''
    return r.content


def _rescatar(proveedor: str, tracking_number: str) -> str:
    """Vuelve a preguntarle a la paquetería dónde está el PDF de una guía YA COMPRADA.

    Es el mismo mecanismo del rescate del admin (`/admin/orders/{id}/rescatar-etiqueta`),
    aquí puesto donde sirve solo: entre que alguien pica «Imprimir Guía» y que sale el
    papel. No compra ni cobra nada.
    """
    mod = paqueterias.modulo(proveedor or 'skydropx')
    if mod is None or not mod.enabled():
        return ''
    try:
        guia = mod.etiqueta_por_rastreo(tracking_number)
    except Exception as e:
        logger.warning('Etiqueta: %s no pudo devolver la guía %s: %s',
                       proveedor, tracking_number, e)
        return ''
    return (guia or {}).get('label_url') or ''


def _pdf_de_la_guia(order: dict) -> tuple[bytes, str]:
    """Los bytes del PDF y la liga con la que se consiguió. Bloquea: va a la red.

    El orden importa y es el barato primero: se intenta la liga guardada; sólo si no
    hay o ya no sirve se molesta a la paquetería.
    """
    numero = (order.get('tracking_number') or '').strip()
    if not numero:
        raise EtiquetaNoLista('Ese pedido todavía no tiene guía')
    guardada = (order.get('label_url') or '').strip()
    pdf = _bajar(guardada)
    if pdf:
        return pdf, guardada
    fresca = _rescatar(order.get('label_provider') or '', numero)
    if not fresca:
        # ⛔ DECIR CUÁL DE LAS DOS ES. Si el pedido nunca pasó por una paquetería
        # nuestra, esto NO es un «espérate»: no hay papel que esperar, y quien está
        # frente a la impresora tiene que enterarse ya para ir por él a otro lado en
        # vez de picarle diez veces. Con guía comprada por nosotros sí es esperar.
        comprada = bool(guardada or order.get('label_provider'))
        if not comprada:
            raise EtiquetaNoLista(
                'Esa guía se capturó a mano: no hay PDF nuestro que imprimir. '
                'Búscalo donde se compró la guía.', estado='manual')
        raise EtiquetaNoLista('La paquetería todavía no publica el PDF de esa guía')
    pdf = _bajar(fresca)
    if not pdf:
        raise EtiquetaNoLista('La paquetería todavía no publica el PDF de esa guía')
    return pdf, fresca


async def etiqueta_para_imprimir(order: dict) -> Response:
    """La respuesta HTTP con el PDF listo para mandar a la impresora.

    ⛔ EN OTRO HILO (`to_thread`). Bajar un PDF y preguntarle a la paquetería son
    llamadas de red BLOQUEANTES de hasta 20 segundos cada una. Hacerlas en el hilo del
    servidor deja a la TIENDA ENTERA congelada mientras alguien imprime una etiqueta:
    ni checkout, ni catálogo, ni pagos. No vale la pena ahorrarse una línea.
    """
    try:
        pdf, liga = await asyncio.to_thread(_pdf_de_la_guia, order)
    except EtiquetaNoLista as e:
        # 409, no 404: el pedido y la guía existen; lo que falta es el papel. Con
        # `generando` la pantalla pinta «Generando…» y vuelve a preguntar sola; con
        # `manual` deja de reintentar y explica que esa guía no tiene PDF nuestro.
        raise HTTPException(status_code=409,
                            detail={'estado': getattr(e, 'estado', 'generando'),
                                    'mensaje': str(e)})
    # Si la liga cambió (la firmada de antes caducó) se deja escrita la nueva: la
    # próxima impresión ya no tiene que salir a preguntar.
    if liga and liga != (order.get('label_url') or ''):
        await db.orders.update_one({'id': order.get('id')}, {'$set': {'label_url': liga}})
    numero = order.get('order_number') or order.get('id') or 'guia'
    return Response(
        content=pdf, media_type='application/pdf',
        headers={
            # `inline`: se abre en el visor y se imprime de un toque. Guardar sigue
            # siendo un clic más, pero lo normal es imprimir y pegar.
            'Content-Disposition': f'inline; filename="guia-{numero}.pdf"',
            'Cache-Control': 'no-store',
        })


async def _pedido_por_numero(order_number: str) -> dict:
    """El pedido, por número de pedido o por id. Los dos porque la ficha unificada se
    abre desde ocho lugares y no todos traen la misma llave en la mano."""
    o = await db.orders.find_one(
        {'$or': [{'order_number': order_number}, {'id': order_number}]}, {'_id': 0})
    if not o:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    return o


@router.get('/admin/orders/{order_number}/etiqueta')
async def admin_etiqueta(order_number: str, admin=Depends(get_current_admin)):
    """El PDF de la guía de cualquier pedido. Sólo admin."""
    return await etiqueta_para_imprimir(await _pedido_por_numero(order_number))


@router.get('/distributor/orders/{order_number}/etiqueta')
async def distributor_etiqueta(order_number: str, dist=Depends(get_current_distributor)):
    """El PDF de la guía de un pedido SUYO. De otro, 403.

    ⛔ EL CANDADO VIVE AQUÍ, NO EN LA PANTALLA — mismo patrón que el resto de sus
    rutas. Esconder el botón no sirve de nada: el número de pedido ajeno se teclea en
    la barra de direcciones, y una etiqueta trae el nombre y el domicilio COMPLETO del
    cliente de otro. El servidor exige que el pedido traiga SU `referred_by`.

    ⛔ POR QUÉ NO LLEVA `deny_view_as`. El «ver como» del admin es de sólo lectura y
    esto no escribe nada del negocio: no compra, no cobra, no cambia el pedido. Lo
    único que puede tocar es refrescar la dirección del papel de una guía ya pagada,
    que es caché, no un dato de nadie. Un admin espiando un panel puede LEER lo que
    ese distribuidor lee, y esto es leer.
    """
    o = await _pedido_por_numero(order_number)
    if o.get('referred_by') != dist['id']:
        raise HTTPException(status_code=403, detail='Ese pedido no es tuyo')
    return await etiqueta_para_imprimir(o)
