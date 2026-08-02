"""EL DOBLE COTIZADOR: se pregunta en los dos lados y se contrata el más barato.

Orden de Christián (2026-07-31): cada envío se cotiza con Skydropx Y con
enviosinternacionales.com, se comparan, y la casa contrata la guía más barata.

Tres cosas que este archivo cuida y que conviene no perder de vista:

  1. ⛔ ES INTERNO. Esta comparación es lo que le cuesta A LA CASA. El cliente no la ve
     nunca: al checkout sigue saliendo lo de siempre por `skydropx.cotizar`, y lo que
     paga sigue mandándolo la política de cobro (`envios.py`). Enseñarle al cliente que
     un proveedor nos cobra $139 y el otro $210 es enseñarle nuestro margen.

  2. ⛔ UN PROVEEDOR CAÍDO NO PUEDE TUMBAR UN DESPACHO. Si uno de los dos no contesta,
     truena o todavía no tiene llaves, se sigue con el otro y se anota el porqué. La
     alternativa —que no se pueda mandar un paquete porque una API tuvo un mal día— es
     mucho más cara que perderse un ahorro de cien pesos ese día.

  3. ⛔ LA GUÍA SE COMPRA CON EL MISMO PROVEEDOR QUE LA COTIZÓ. Un `rate_id` sólo vale
     en la casa que lo emitió: comprar en Skydropx una tarifa que cotizó el revendedor
     es, en el mejor caso, un error 404, y en el peor una guía equivocada ya pagada. Por
     eso cada opción carga su `proveedor` y `comprar_guia` obedece esa etiqueta.
"""
import logging

import enviosinternacionales
import skydropx

logger = logging.getLogger(__name__)

# Los proveedores que compiten, en orden. Skydropx va primero porque es el que está
# probado en vivo: si los dos cotizan exactamente lo mismo, gana el conocido.
#
# Agregar un tercero es agregar un renglón aquí y un módulo con las mismas cuatro
# funciones (`enabled`, `cotizacion`, `comprar_guia`, y el nombre). Nada más.
PROVEEDORES = (
    ('skydropx', 'Skydropx', skydropx),
    (enviosinternacionales.CLAVE, enviosinternacionales.NOMBRE, enviosinternacionales),
)


def modulo(clave: str):
    """El módulo que atiende a ese proveedor. None si no existe o está apagado."""
    for c, _nombre, mod in PROVEEDORES:
        if c == clave:
            return mod
    return None


def encendidos() -> list:
    """Qué proveedores tienen llaves hoy. Para que el panel lo pueda decir."""
    return [{'clave': c, 'nombre': n, 'activo': bool(mod.enabled())}
            for c, n, mod in PROVEEDORES]


def cuantos_activos() -> int:
    return sum(1 for p in encendidos() if p['activo'])


def cotizar_en_todos(destino: dict, paquete: dict, espera_max: float | None = None,
                     filtrar: bool = False) -> dict:
    """Cotiza el MISMO bulto en todos los proveedores encendidos y junta las tarifas.

    Devuelve las opciones de todos revueltas y ordenadas por precio —que es el orden en
    que la casa decide— más una ficha por proveedor con qué pasó en cada uno: cuántas
    tarifas dio, cuál fue su mejor precio, o por qué no dio ninguna.

    `filtrar` en False a propósito: quien despacha es la casa y tiene derecho a ver
    TODAS las tarifas, aunque tarden más de lo que se le prometió al cliente. Ocultarle
    opciones a quien paga la guía es exactamente lo que hace que un envío cueste $600.
    """
    todas, por_proveedor, cotizaciones = [], [], {}
    for clave, nombre, mod in PROVEEDORES:
        if not mod.enabled():
            por_proveedor.append({'clave': clave, 'nombre': nombre, 'activo': False,
                                  'tarifas': 0, 'mejor': None,
                                  'detalle': 'sin credenciales'})
            continue
        try:
            kw = {'filtrar': filtrar}
            if espera_max is not None:
                kw['espera_max'] = espera_max
            cot = mod.cotizacion(destino, paquete, **kw)
        except Exception as e:
            # Un proveedor que truena NO tumba el despacho: se anota y se sigue.
            logger.exception('%s: no se pudo cotizar', nombre)
            por_proveedor.append({'clave': clave, 'nombre': nombre, 'activo': True,
                                  'tarifas': 0, 'mejor': None,
                                  'detalle': f'no respondio: {e}'[:200]})
            continue
        opciones = []
        for o in cot.get('opciones') or []:
            # La etiqueta del dueño de la tarifa es lo que después decide con quién se
            # compra. Se pone aquí y no en cada módulo para que ninguno pueda olvidarla.
            opciones.append(dict(o, proveedor=clave, proveedor_nombre=nombre))
        todas.extend(opciones)
        cotizaciones[clave] = {'id': cot.get('id', ''),
                               'packages': cot.get('packages') or [],
                               'requiere_verificar_origen':
                                   cot.get('requiere_verificar_origen', False)}
        por_proveedor.append({
            'clave': clave, 'nombre': nombre, 'activo': True,
            'tarifas': len(opciones),
            'mejor': min((o['precio'] for o in opciones), default=None),
            'detalle': '' if opciones else 'sin tarifas para ese codigo postal',
        })
    todas.sort(key=lambda o: o['precio'])
    return {'opciones': todas, 'proveedores': por_proveedor, 'cotizaciones': cotizaciones}


def ahorro(comparacion: dict) -> dict:
    """Cuánto se ahorra por cotizar en dos lados en vez de uno. 0 si sólo hubo uno.

    No decide nada: MIDE. Existe para que el día que Christián se pregunte si valió la
    pena abrir la segunda cuenta, el número esté escrito en el panel y en la bitácora en
    vez de en la intuición de alguien.
    """
    mejores = {p['clave']: p['mejor'] for p in comparacion.get('proveedores') or []
               if p.get('mejor') is not None}
    if len(mejores) < 2:
        return {'comparados': len(mejores), 'ahorro_mxn': 0.0, 'gana': '',
                'mejores': mejores}
    gana = min(mejores, key=lambda k: mejores[k])
    peor = max(mejores.values())
    return {'comparados': len(mejores),
            'ahorro_mxn': round(peor - mejores[gana], 2),
            'gana': gana, 'mejores': mejores}


class TopeDeGastoExcedido(RuntimeError):
    """La guía más barata cuesta más de lo que el servidor puede gastar solo.

    ⛔ NO ES UN ERROR: es el freno funcionando. Se distingue de un fallo de verdad
    (API caída, sin saldo, dirección rechazada) porque lo que hay que hacer es
    distinto: aquí no se reintenta nada, se le pregunta a Christián si autoriza el
    gasto. Por eso es su propia clase y no un `RuntimeError` pelón — quien la atrapa
    río arriba tiene que poder decir «esto necesita tu visto bueno» en vez de
    «la paquetería falló».
    """

    def __init__(self, precio: float, tope: float, paqueteria: str = '',
                 servicio: str = ''):
        self.precio = round(float(precio or 0), 2)
        self.tope = round(float(tope or 0), 2)
        self.paqueteria = paqueteria or ''
        self.servicio = servicio or ''
        super().__init__(
            f'La guía más barata cuesta ${self.precio:,.2f} y el tope automático '
            f'es ${self.tope:,.2f}')


def guia_para(destino: dict, paquete: dict, servicio_codigo: str = '',
              tope_mxn: float | None = None,
              dias_max: int | None = None) -> dict:
    """De cero a guía en un solo llamado: cotiza en los DOS, elige y compra la mejor.

    Es el camino automático, el que corre solo cuando un pago se confirma. Aplica las
    mismas dos reglas de siempre —sólo paqueterías permitidas y sólo dentro del plazo
    prometido— y encima de eso elige el proveedor más barato.

    Si el pedido guardó qué servicio eligió el cliente, se respeta ÉSE; si ya no está
    disponible, cae a la más barata de las permitidas — nunca a una paquetería que el
    cliente no pidió ni a un plazo que no se le prometió.

    ⛔ `tope_mxn` ES EL FRENO DE GASTO y se revisa ENTRE cotizar y comprar, que es el
    único momento en que sirve: después de comprar el dinero ya salió. Si la elegida
    se pasa, revienta con `TopeDeGastoExcedido` SIN comprar nada. Se pasa como
    parámetro y no se lee de una constante aquí adentro para que el camino del admin
    —que compra a mano lo que él eligió y ya vio el precio— no herede un tope pensado
    para lo que hace el servidor solo.
    """
    if not skydropx.remitente_configurado():
        # A propósito revienta en vez de comprar con un remitente inventado.
        raise RuntimeError('Falta configurar la direccion del remitente (SKYDROPX_FROM_*)')
    # `filtrar=True`: este camino es el que le cumple al CLIENTE lo prometido, así que
    # aquí sí mandan la lista de permitidas y el plazo máximo.
    comp = cotizar_en_todos(destino, paquete, espera_max=skydropx.ESPERA_MAX_GUIA_S,
                            filtrar=True)
    tarifas = comp['opciones']
    # ⛔ UN PEDIDO EXPRESS NO SE DEGRADA SOLO (Christián, 2026-08-02). `dias_max`
    # recorta las tarifas al plazo que el cliente PAGÓ (1-2 días para express):
    # sin este filtro, una guía express arriba del tope caía a «la más barata que
    # quepa» — que puede ser una de 4-5 días — y el cliente pagó su extra para
    # recibir rápido. Si ninguna rápida cabe, se detiene y se le pregunta al
    # dueño, que es lo que él pidió. Un 0 en días es «no dijo», no «hoy»: fuera.
    if dias_max:
        def _dias(t):
            try:
                return int(t.get('dias') or 0)
            except (TypeError, ValueError):
                return 0
        tarifas = [t for t in tarifas if 0 < _dias(t) <= int(dias_max)]
    if not tarifas:
        raise RuntimeError('Ningun proveedor devolvio tarifas de las paqueterias permitidas'
                           + (f' con entrega en {dias_max} dias o menos' if dias_max else ''))
    elegida = next((t for t in tarifas
                    if servicio_codigo and t['servicio_codigo'] == servicio_codigo),
                   tarifas[0])
    # ⛔ EL FRENO, JUSTO ANTES DE GASTAR. Si el servicio que pidió el cliente se pasa
    # del tope pero hay una permitida más barata que sí cabe, se toma ésa: el tope es
    # sobre el dinero de la casa, no una razón para no despachar. Sólo cuando NINGUNA
    # cabe se detiene todo y se le pregunta a Christián.
    if tope_mxn is not None:
        tope = float(tope_mxn)
        if float(elegida.get('precio') or 0) > tope:
            cabe = next((t for t in tarifas if float(t.get('precio') or 0) <= tope), None)
            if cabe is None:
                barata = tarifas[0]
                raise TopeDeGastoExcedido(barata.get('precio') or 0, tope,
                                          barata.get('paqueteria', ''),
                                          barata.get('servicio', ''))
            logger.info('Envio: el servicio pedido costaba $%s (tope $%s); se toma '
                        '%s a $%s', elegida.get('precio'), tope,
                        cabe.get('paqueteria'), cabe.get('precio'))
            elegida = cabe
    numero = 1
    paquetes = (comp.get('cotizaciones') or {}).get(
        elegida.get('proveedor') or 'skydropx', {}).get('packages') or []
    if paquetes and isinstance(paquetes[0], dict):
        try:
            numero = int(paquetes[0].get('package_number') or 1)
        except (TypeError, ValueError):
            numero = 1
    guia = comprar_guia(elegida, destino, paquete, numero)
    guia['carrier'] = elegida['paqueteria']
    guia['servicio'] = elegida['servicio']
    guia['costo'] = elegida['precio']
    guia['shipment_id'] = guia.get('shipment_id') or ''
    guia['ahorro'] = ahorro(comp)
    return guia


def comprar_guia(opcion: dict, destino: dict, paquete: dict,
                 package_number: int = 1) -> dict:
    """Compra la guía CON EL PROVEEDOR QUE LA COTIZÓ. Nunca con el otro.

    ⚠️ CUESTA DINERO. El `rate_id` sólo existe en la casa que lo emitió: mandarlo al
    proveedor equivocado es un 404 en el mejor caso y una guía mal comprada en el peor.
    """
    clave = (opcion or {}).get('proveedor') or 'skydropx'
    mod = modulo(clave)
    if mod is None:
        raise RuntimeError(f'Proveedor de paqueteria desconocido: {clave}')
    if not mod.enabled():
        raise RuntimeError(f'El proveedor {clave} no tiene credenciales')
    guia = mod.comprar_guia(opcion.get('rate_id') or '', destino, paquete, package_number)
    # Se devuelve DICHO de quién es la guía: en el pedido queda escrito con quién se
    # compró, que es lo que después permite reclamarle a la casa correcta.
    guia['proveedor'] = clave
    return guia
