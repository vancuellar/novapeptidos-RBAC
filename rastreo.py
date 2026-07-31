"""EL RASTREO VIVE EN NUESTRA PÁGINA — no en la de FedEx.

⛔ ORDEN DE CHRISTIÁN (2026-07-31): «quiero que el cliente rastree su pedido DENTRO de
exygenlabs.com, sin mandarlo a la página de FedEx; quiero que vivan en nuestra página el
mayor tiempo posible».

Por qué NO fue un iframe (que era la idea original)
---------------------------------------------------
Meter la página de la paquetería en un marco dentro de la nuestra NO SE PUEDE, y no es
cuestión de programarlo mejor: las paqueterías lo prohíben desde SU servidor, y el
navegador obedece. Comprobado con `curl -I` el 2026-07-31:

    https://www.fedex.com/wtrk/track/?trknbr=875164874865
        x-frame-options: SAMEORIGIN
        content-security-policy: frame-ancestors 'self'

    https://rastreo3.estafeta.com/RastreoWebInternet/consultaEnvio.do
        x-frame-options: SAMEORIGIN

`frame-ancestors 'self'` significa literalmente «sólo fedex.com puede enmarcarme». Dentro
de exygenlabs.com ese marco sale EN BLANCO — no con un error entendible: en blanco. Es
peor que mandar al cliente a FedEx, porque parece que nuestra página está rota.

Lo que sí se puede, y es lo que hace este archivo: pedirle a la API de la paquetería los
eventos del envío y PINTARLOS NOSOTROS, con la marca de la casa. El cliente no sale del
sitio y nosotros mandamos en cómo se ve.

Tres cosas que este archivo cuida
---------------------------------
  1. ⛔ NO REEXPONE NADA DE LO QUE YA SE TAPÓ. La ficha `/pedido/{numero}` es PÚBLICA
     (el que compró como invitado no tiene cuenta), y por eso `server.pedido_para_el_cliente`
     le quita al pedido el distribuidor que lo refirió, las comisiones y lo que la guía
     le costó a la casa. Esta ruta es igual de pública: devuelve SÓLO estatus de envío y
     lo que el cliente ya sabe de su pedido. `label_provider` se usa aquí adentro para
     saber a quién preguntarle, y NO SALE en la respuesta — es dato de la casa.

  2. ⛔ UN CLIENTE RECARGANDO NO PUEDE TUMBAR LA CUOTA. El tope de las dos paqueterías es
     de 2 peticiones por segundo POR CUENTA, y ese tope lo comparten las cotizaciones y
     las compras de guía: si alguien deja la pestaña recargando, se queda sin cupo el
     despacho, que es lo que sí cuesta dinero. Por eso todo pasa por una caché de unos
     minutos (`TTL_S`): mil recargas del mismo pedido son UNA sola llamada. El freno de
     `ritmo.py` sigue debajo como última red.

  3. ⛔ SIN EVENTOS NO ES UN ERROR. Las primeras horas después de comprar la guía el
     carrier todavía no reporta nada, y eso es lo NORMAL. La página no puede enseñar
     «falla»: enseña la línea de tiempo con el primer paso prendido y los demás en gris.
     Por eso esta ruta contesta 200 con la lista vacía y nunca 5xx por culpa de la
     paquetería.

Lo que este archivo NO hace, a propósito: no compra guías, no cobra, no cambia el pedido.
Sólo lee.
"""
import asyncio
import logging
import threading
import time

from fastapi import APIRouter, HTTPException

import paqueterias
from database import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api')

# Cuánto vale una respuesta guardada. Cinco minutos es el punto medio honesto: un paquete
# no cambia de ciudad en cinco minutos, y a cambio protege la cuota del despacho de
# cualquiera que deje la pestaña abierta recargando.
TTL_S = float(300)

# ---------------------------------------------------------------- los cuatro pasos
# La línea de tiempo que ve el cliente. Cuatro y no doce: quien abre esto quiere saber
# «¿ya viene?», no leer la bitácora interna de una paquetería.
PASOS = ('recibido', 'transito', 'reparto', 'entregado')

# Los códigos que declara la API de la paquetería (su OpenAPI los lista como enum),
# traducidos a nuestros cuatro pasos.
#
# ⛔ `delivered_to_branch` NO ES `delivered`. Significa que el paquete llegó a una
# sucursal a esperar a que lo recojan: para el cliente es «ya casi», no «ya lo tienes».
# Pintarlo como entregado haría que alguien deje de buscar un paquete que sí tiene que ir
# a recoger.
POR_CODIGO = {
    'created': 'recibido',
    'picked_up': 'transito',
    'in_transit': 'transito',
    'last_mile': 'reparto',
    'delivery_attempt': 'reparto',
    'delivered_to_branch': 'reparto',
    'delivered': 'entregado',
}

# Los códigos que NO son avance sino problema. No mueven la línea de tiempo hacia
# adelante —un paquete devuelto no está «más entregado»— pero sí hay que decirlo.
INCIDENCIAS = {'exception', 'in_return', 'canceled', 'destroyed', 'retained'}


def paso_de(eventos: list, estado_pedido: str = '') -> str:
    """En cuál de los cuatro pasos está el envío, según lo que reportó el carrier.

    Se recorre TODO el historial y se toma el paso más avanzado, no el del último
    evento: los carriers mandan eventos administrativos tardíos (una corrección de
    dirección, un aviso de facturación) que llegan después de la entrega y harían
    RETROCEDER la barra si sólo se mirara el último renglón. Una barra que retrocede
    hace que el cliente escriba a preguntar qué pasó.
    """
    avance = -1
    for e in eventos or []:
        paso = POR_CODIGO.get((e.get('estado') or '').lower())
        if paso:
            avance = max(avance, PASOS.index(paso))
    if avance >= 0:
        return PASOS[avance]
    # Sin eventos que entienda, manda lo que sabe la casa: si ya se marcó entregado o
    # enviado, se dice. El cliente ve el primer paso prendido, no una pantalla vacía.
    if estado_pedido == 'entregado':
        return 'entregado'
    return 'transito' if estado_pedido == 'enviado' else 'recibido'


def hay_incidencia(eventos: list) -> bool:
    """¿El carrier reportó algún problema? Se mira sólo el ÚLTIMO evento con código
    conocido: una incidencia vieja que ya se resolvió (un intento de entrega fallido el
    martes, entregado el miércoles) no es un problema de hoy."""
    for e in reversed(eventos or []):
        codigo = (e.get('estado') or '').lower()
        if codigo in INCIDENCIAS:
            return True
        if codigo in POR_CODIGO:
            return False
    return False


# ---------------------------------------------------------------- la caché
# Un candado de verdad y no un `dict` a secas: FastAPI corre las rutas síncronas en un
# pool de HILOS, así que dos clientes recargando son dos hilos tocando esto a la vez.
_CACHE: dict = {}
_CANDADO = threading.Lock()


def limpiar_cache() -> None:
    """Tira lo guardado. Existe para las pruebas y para el día que haya que forzar."""
    with _CANDADO:
        _CACHE.clear()


def _guardado(clave: str):
    with _CANDADO:
        fila = _CACHE.get(clave)
    if not fila:
        return None
    cuando, valor = fila
    if time.monotonic() - cuando > TTL_S:
        return None
    return valor


def _guardar(clave: str, valor) -> None:
    with _CANDADO:
        _CACHE[clave] = (time.monotonic(), valor)
        # Poda barata para que esto no crezca sin fin en un proceso de meses.
        if len(_CACHE) > 500:
            viejo = time.monotonic() - TTL_S
            for k, (c, _v) in list(_CACHE.items()):
                if c < viejo:
                    _CACHE.pop(k, None)


def eventos_de(proveedor: str, tracking_number: str, carrier: str = '') -> list:
    """Los eventos del carrier, de la caché o de la API. BLOQUEA: sale a la red.

    ⛔ NUNCA TRUENA. Si la paquetería no contesta se devuelve lista vacía y la página
    enseña la línea de tiempo con lo que la casa sí sabe. El cliente ya pagó: tiene
    derecho a ver su pedido aunque FedEx tenga un mal día.
    """
    tn = (tracking_number or '').strip()
    if not tn:
        return []
    clave = f'{proveedor or "skydropx"}|{tn}'
    guardado = _guardado(clave)
    if guardado is not None:
        return guardado
    mod = paqueterias.modulo(proveedor or 'skydropx')
    if mod is None or not mod.enabled():
        return []
    try:
        eventos = mod.rastrear(tn, carrier) or []
    except Exception as e:            # cinturón: `rastrear` ya no debería dejar pasar nada
        logger.warning('Rastreo: %s no pudo con la guia %s: %s', proveedor, tn, e)
        eventos = []
    # Se guarda incluso la lista vacía, y a propósito: el caso de «todavía no hay
    # eventos» es justo el que más se recarga, y es el que más cuota gastaría.
    _guardar(clave, eventos)
    return eventos


def ficha_publica(order: dict, eventos: list) -> dict:
    """Lo que se le manda a la pantalla del cliente. Nada más que esto.

    ⛔ LISTA BLANCA, NO LISTA NEGRA. Se construye un diccionario NUEVO campo por campo
    en vez de copiar el pedido y borrarle cosas. Así, el día que alguien agregue un dato
    interno al pedido, este cajón NO se lo lleva de contrabando: para que salga hay que
    escribirlo aquí a mano. `label_provider`, `shipping_cost`, `referred_by` y compañía
    no están, y no pueden colarse solos.
    """
    return {
        'numero': order.get('order_number') or '',
        'paqueteria': order.get('carrier') or '',
        'rastreo': order.get('tracking_number') or '',
        # La liga al sitio de la paquetería va discreta, abajo de todo. No se esconde
        # —quien la quiera la tiene— pero deja de ser la protagonista.
        'url_paqueteria': order.get('tracking_url') or '',
        'paso': paso_de(eventos, order.get('status') or ''),
        'incidencia': hay_incidencia(eventos),
        'entrega_estimada': order.get('eta') or '',
        'enviado_en': order.get('shipped_at') or '',
        'entregado_en': order.get('delivered_at') or '',
        'eventos': eventos,
    }


@router.get('/orders/{order_number}/rastreo')
async def rastreo_del_pedido(order_number: str):
    """Dónde va el paquete. PÚBLICA por número de pedido, igual que `/orders/{numero}`.

    ⛔ MISMO CANDADO QUE LA FICHA, NI MÁS NI MENOS. Esta ruta no pide sesión por la misma
    razón que la ficha del pedido: quien compró como invitado no tiene cuenta y aun así
    tiene que poder ver a dónde va lo que pagó. Y por eso mismo devuelve MENOS que la
    ficha: sólo estatus de envío, que es lo que ya venía en el correo.

    ⛔ EN OTRO HILO (`to_thread`). Preguntarle a la paquetería es una llamada de red
    bloqueante de hasta 20 segundos. Hacerla en el hilo del servidor deja congelada a la
    TIENDA ENTERA —checkout incluido— mientras un cliente mira su rastreo.
    """
    order = await db.orders.find_one({'order_number': order_number}, {'_id': 0})
    if not order:
        raise HTTPException(status_code=404, detail='Pedido no encontrado')
    numero = (order.get('tracking_number') or '').strip()
    if not numero:
        # Todavía no hay guía: el pedido existe y va en camino de salir. No es 404 —el
        # pedido SÍ está— y la pantalla lo pinta como «preparando tu pedido».
        return ficha_publica(order, [])
    eventos = await asyncio.to_thread(
        eventos_de, order.get('label_provider') or '', numero, order.get('carrier') or '')
    return ficha_publica(order, eventos)
