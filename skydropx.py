"""Skydropx: cotizar un envío y comprar la guía, por API REST directa.

Sin WordPress y sin WooCommerce: aquí no hay tienda de terceros que traduzca nada,
le hablamos a Skydropx nosotros. Documentación: https://docs.skydropx.com/

  POST /quotations   zip_from + zip_to + bulto      → lista de tarifas (para el checkout)
  POST /shipments    direcciones completas + bulto  → el envío, con sus tarifas y sus ids
  POST /labels       rate_id                        → la guía: número, PDF y rastreo

Se enciende con una llave, igual que las pasarelas de cobro:

  SKYDROPX_API_KEY   se lee del entorno o se pega desde Admin → Cobros (ver secretos.py)

⛔ SIN LLAVE NO SE ROMPE NADA. `enabled()` devuelve False, el checkout no ofrece
cotización y la compra sigue su curso exactamente como hoy. Lo único que pasa es
que queda dicho en la bitácora. Un checkout que se cae porque falta una llave de
paquetería es un checkout que deja de vender.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

# La demo de Skydropx vive en otra URL. Se deja configurable para poder probar
# contra su ambiente de pruebas sin tocar código.
API = os.environ.get('SKYDROPX_API_URL', 'https://api.skydropx.com/v1').rstrip('/')
TIMEOUT = 20


# ⛔ SOLO SE LE ENSEÑA ESTAFETA AL CLIENTE (Christian, 2026-07-28). La API devuelve
# varias paqueterías; el cliente ve una. Ampliar es agregar un renglón aquí — en
# minúsculas y sin acentos, que es como se compara.
PAQUETERIAS_PERMITIDAS = ('estafeta',)


def _llave() -> str:
    """La llave efectiva: el entorno manda, y si no, la que se pegó en el Admin."""
    import secretos
    return secretos.valor('SKYDROPX_API_KEY')


def enabled() -> bool:
    return bool(_llave())


def _headers() -> dict:
    return {'Authorization': f'Token token={_llave()}',
            'Content-Type': 'application/json'}


def _post(ruta: str, cuerpo: dict) -> dict | list:
    resp = requests.post(f'{API}{ruta}', headers=_headers(), json=cuerpo, timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f'Skydropx {ruta} {resp.status_code}: {resp.text[:300]}')
    return resp.json()


# --------------------------------------------------------------- el remitente
# ⚠️ PENDIENTE DE CHRISTIAN: la dirección de quien envía.
#
# NO es la casa del dueño — va a ser la de un trabajador, y todavía no la tenemos.
# Por eso vive en variables de entorno y no en el código: cuando exista, se pega en
# el servidor (o en Admin → Cobros) y ya. Los valores de ejemplo de abajo llevan la
# palabra PENDIENTE a propósito, para que se vea a leguas que no son reales.
#
# ⛔ Y EL SISTEMA SE NIEGA A COMPRAR GUÍA SI NO ESTÁ CONFIGURADA. Comprar una guía
# con un remitente inventado significa que la paquetería recoge en una dirección que
# no existe, o que una devolución se va a la nada — con el paquete ya pagado.
REMITENTE_PENDIENTE = 'PENDIENTE-CONFIGURAR'


def remitente() -> dict:
    """La dirección de quien envía, tal como la pide Skydropx."""
    return {
        'name': os.environ.get('SKYDROPX_FROM_NAME', REMITENTE_PENDIENTE),
        'company': os.environ.get('SKYDROPX_FROM_COMPANY', 'Exygen Labs'),
        'address1': os.environ.get('SKYDROPX_FROM_ADDRESS1', REMITENTE_PENDIENTE),
        'address2': os.environ.get('SKYDROPX_FROM_ADDRESS2', ''),
        'city': os.environ.get('SKYDROPX_FROM_CITY', REMITENTE_PENDIENTE),
        'province': os.environ.get('SKYDROPX_FROM_PROVINCE', REMITENTE_PENDIENTE),
        'zip': os.environ.get('SKYDROPX_FROM_ZIP', ''),
        'country': os.environ.get('SKYDROPX_FROM_COUNTRY', 'MX'),
        'phone': os.environ.get('SKYDROPX_FROM_PHONE', ''),
        'email': os.environ.get('SKYDROPX_FROM_EMAIL', ''),
    }


def remitente_configurado() -> bool:
    """¿Ya tenemos una dirección de origen de verdad?

    Se exige lo mínimo con lo que una paquetería puede recoger: nombre, calle,
    ciudad, estado y CP — y que ninguno siga marcado como PENDIENTE.
    """
    r = remitente()
    obligatorios = ('name', 'address1', 'city', 'province', 'zip')
    return all((r.get(k) or '').strip() and REMITENTE_PENDIENTE not in (r.get(k) or '')
               for k in obligatorios)


def cp_origen() -> str:
    return (remitente().get('zip') or '').strip()


# --------------------------------------------------------------- cotizar
def _normaliza(nombre: str) -> str:
    return (nombre or '').strip().lower().translate(str.maketrans('áéíóúü', 'aeiouu'))


def permitida(proveedor: str) -> bool:
    n = _normaliza(proveedor)
    return any(p in n for p in PAQUETERIAS_PERMITIDAS)


def solo_permitidas(opciones: list) -> list:
    """El filtro de la regla: al cliente solo se le enseña lo que está en la lista."""
    return [o for o in (opciones or []) if permitida(o.get('paqueteria', ''))]


def _tarifas_del_json(data) -> list:
    """Saca las tarifas venga como venga: arreglo pelón, {data:[...]} o JSON:API."""
    if isinstance(data, dict):
        data = data.get('data') if isinstance(data.get('data'), list) else data.get('data', data)
    if isinstance(data, dict):
        rel = ((data.get('relationships') or {}).get('rates') or {}).get('data')
        data = rel if isinstance(rel, list) else [data]
    return data if isinstance(data, list) else []


def _opcion(cruda: dict) -> dict | None:
    """Una tarifa de Skydropx traducida a lo que este sitio entiende."""
    attr = cruda.get('attributes') if isinstance(cruda.get('attributes'), dict) else cruda
    try:
        precio = float(attr.get('total_pricing') or attr.get('amount_local') or 0)
    except (TypeError, ValueError):
        precio = 0.0
    if precio <= 0:
        return None                     # una tarifa sin precio no es una opción
    try:
        dias = int(attr.get('days') or 0)
    except (TypeError, ValueError):
        dias = 0
    return {
        'rate_id': str(cruda.get('id') or attr.get('id') or ''),
        'paqueteria': str(attr.get('provider') or ''),
        'servicio': str(attr.get('service_level_name') or ''),
        'servicio_codigo': str(attr.get('service_level_code') or ''),
        'dias': dias,
        'precio': round(precio, 2),
        'moneda': str(attr.get('currency_local') or 'MXN'),
    }


def cotizar(cp_destino: str, paquete: dict, cp_desde: str = '') -> list:
    """Precios reales por peso y código postal. Devuelve SOLO las permitidas.

    `paquete` es lo que arma `envios.paquete_del_pedido`: peso en kg y medidas en cm.
    Ordena de más barato a más caro — que es el orden en que un cliente decide.
    """
    cuerpo = {
        'zip_from': (cp_desde or cp_origen() or '').strip(),
        'zip_to': (cp_destino or '').strip(),
        'parcel': {
            'weight': paquete.get('peso_kg'),
            'height': paquete.get('alto_cm'),
            'width': paquete.get('ancho_cm'),
            'length': paquete.get('largo_cm'),
        },
        # Se le pide a la API solo lo que se va a enseñar. Igual se vuelve a filtrar
        # al recibir: la lista de permitidas es NUESTRA regla, no un favor de ellos.
        'carriers': [{'name': p} for p in PAQUETERIAS_PERMITIDAS],
    }
    opciones = [o for o in (_opcion(t) for t in _tarifas_del_json(_post('/quotations', cuerpo))) if o]
    return sorted(solo_permitidas(opciones), key=lambda o: o['precio'])


# --------------------------------------------------------------- comprar guía
def crear_envio(destino: dict, paquete: dict, contenido: str = 'Insumos de laboratorio') -> dict:
    """Da de alta el envío con las direcciones completas. Devuelve sus tarifas con id.

    Cotizar y comprar son dos pasos distintos en Skydropx: `/quotations` da precios
    (sin direcciones, para enseñar) y `/shipments` da tarifas COMPRABLES (con id).
    """
    cuerpo = {
        'address_from': remitente(),
        'address_to': destino,
        'parcels': [{
            'weight': paquete.get('peso_kg'),
            'height': paquete.get('alto_cm'),
            'width': paquete.get('ancho_cm'),
            'length': paquete.get('largo_cm'),
            'mass_unit': 'KG',
            'distance_unit': 'CM',
        }],
        'carriers': [{'name': p} for p in PAQUETERIAS_PERMITIDAS],
        'consignment_note_class_code': os.environ.get('SKYDROPX_CLASE_SAT', '31181701'),
        'consignment_note_packaging_code': os.environ.get('SKYDROPX_EMPAQUE_SAT', '4G'),
    }
    data = _post('/shipments', cuerpo)
    cuerpo_data = data.get('data') if isinstance(data, dict) else {}
    tarifas = [o for o in (_opcion(t) for t in _tarifas_del_json(data)) if o]
    return {'shipment_id': str((cuerpo_data or {}).get('id') or ''),
            'tarifas': sorted(solo_permitidas(tarifas), key=lambda o: o['precio'])}


def comprar_guia(rate_id: str) -> dict:
    """Compra la guía de una tarifa. Devuelve número, PDF y URL de rastreo."""
    data = _post('/labels', {'rate_id': rate_id, 'label_format': 'pdf'})
    attr = ((data or {}).get('data') or {}).get('attributes') or {}
    return {
        'tracking_number': str(attr.get('tracking_number') or ''),
        'label_url': str(attr.get('label_url') or ''),
        'tracking_url': str(attr.get('tracking_url_provider') or ''),
    }


def guia_para(destino: dict, paquete: dict, servicio_codigo: str = '') -> dict:
    """De cero a guía en un solo llamado: alta del envío, elige tarifa y compra.

    Si el pedido guardó qué servicio eligió el cliente (`servicio_codigo`), se
    respeta ESE; si ya no está disponible, cae a la más barata de las permitidas —
    nunca a una paquetería que el cliente no pidió.
    """
    if not remitente_configurado():
        # A propósito revienta en vez de comprar con un remitente inventado.
        raise RuntimeError('Falta configurar la direccion del remitente (SKYDROPX_FROM_*)')
    envio = crear_envio(destino, paquete)
    tarifas = envio['tarifas']
    if not tarifas:
        raise RuntimeError('Skydropx no devolvio ninguna tarifa de las paqueterias permitidas')
    elegida = next((t for t in tarifas if servicio_codigo and t['servicio_codigo'] == servicio_codigo),
                   tarifas[0])
    guia = comprar_guia(elegida['rate_id'])
    guia['carrier'] = elegida['paqueteria']
    guia['servicio'] = elegida['servicio']
    guia['costo'] = elegida['precio']
    guia['shipment_id'] = envio['shipment_id']
    return guia
