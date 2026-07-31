"""enviosinternacionales.com: el SEGUNDO cotizador de guías. Espejo de `skydropx.py`.

Por qué existe
-------------
Christián decidió (2026-07-31) cotizar cada envío en DOS lados y contratar el más
barato. enviosinternacionales.com es un REVENDEDOR de Skydropx: vende la misma red de
paqueterías con su propio contrato, y un revendedor con volumen puede tener tarifas
mejores que las nuestras en unas rutas y peores en otras. Cuál gana no se adivina: se
cotiza y se compara, pedido por pedido.

⛔ NACE APAGADO Y NO ROMPE NADA. `enabled()` es False mientras no haya credenciales, y
mientras sea False el sitio se comporta EXACTAMENTE como hoy: se cotiza sólo con
Skydropx y se compra igual que siempre. Christián todavía no abre la cuenta; el día que
la abra pega las llaves y esto se enciende solo, sin desplegar nada.

Dónde se pegan las llaves (cualquiera de las dos formas, el entorno manda):

  · en el servidor:  ~/.config/exygen/enviosinternacionales.env
        ENVIOSINT_CLIENT_ID=...
        ENVIOSINT_CLIENT_SECRET=...
  · o desde el teléfono: Admin → Cobros (se guardan cifradas, ver `secretos.py`)

✅ LA FORMA DE LA API SÍ ESTÁ VERIFICADA — contra su OpenAPI 3.0.1 público (2026-07-31),
descargado de https://app.enviosinternacionales.com/es-MX/api-docs.json (45 rutas). Lo que
NO se ha probado es una llamada real, porque todavía no hay cuenta ni llaves.

Lo que dice esa especificación, y que es lo que implementa este módulo:

  · es **white-label de Skydropx** (sus propios assets y hostname aparecen en el portal),
    y en efecto los esquemas son los MISMOS: `quotation.address_from/address_to/parcel`
    con `country_code` + `postal_code` + `area_level1/2/3`, y `shipment.rate_id` con
    direcciones y `packages`. Por eso aquí no se reescribe ni un traductor: se reutilizan
    los de `skydropx.py`. Si son la misma API, tienen que ser la misma traducción — y un
    solo lugar donde arreglarla el día que cambie;
  · **OAuth2 `client_credentials`**, en `POST /api/v1/oauth/token` y con cuerpo
    **JSON** (la especificación declara `application/json`, igual que Skydropx PRO — NO
    es el header viejo `Authorization: Token token=`). El token dura 2 horas;
  · **cotización en DIFERIDO, igual que Skydropx PRO**: `POST /api/v1/quotations` y
    luego `GET /api/v1/quotations/{id}` hasta que `is_completed` sea true;
  · la compra es `POST /api/v1/shipments/` **con diagonal final** — así está en la
    especificación, y sin ella la ruta es otra (`/shipments` a secas sólo acepta GET).
    Ese detalle vale una guía: por eso está escrito aparte y con nombre.

Hosts: producción `app.enviosinternacionales.com`, pruebas `sb-app.enviosinternacionales.com`
(se cambia con `ENVIOSINT_API_URL`, sin tocar código). Su límite es 2 peticiones por
segundo — el mismo de Skydropx, y por eso se conserva el mismo ritmo de consulta.
"""
import logging
import os
import time

import requests

import ritmo
import skydropx

logger = logging.getLogger(__name__)

# ⛔ SU PROPIO TOPE, SU PROPIA CUENTA. También son 2 peticiones por segundo, pero se
# cuentan aparte de las de Skydropx: el límite es por cuenta, no del mundo. Compartir un
# solo freno entre los dos proveedores frenaría a la mitad sin motivo — que es justo lo
# que haría más lento el doble cotizador y lo volvería un estorbo.
RITMO = ritmo.Ritmo(float(os.environ.get('ENVIOSINT_REQ_POR_SEG', 2)),
                    'enviosinternacionales')

# El nombre con el que este proveedor aparece en el panel y en la bitácora.
NOMBRE = 'Envios Internacionales'
CLAVE = 'enviosinternacionales'

# El host de producción, tal como lo declara su OpenAPI (`servers`). Para pruebas se
# apunta a `https://sb-app.enviosinternacionales.com/api/v1` con esta misma variable, sin
# tocar código ni desplegar.
API = os.environ.get('ENVIOSINT_API_URL',
                     'https://app.enviosinternacionales.com/api/v1').rstrip('/')

# ⛔ LA DIAGONAL FINAL NO SOBRA. En su especificación, `POST` de envíos es
# `/api/v1/shipments/` **con** diagonal; `/api/v1/shipments` sin ella sólo acepta `GET`.
# Escrito aparte para que nadie lo "limpie" pensando que es un descuido: quitarla es
# cambiar de ruta y quedarse sin guía después de haber cotizado.
RUTA_COMPRAR = '/shipments/'

TIMEOUT = 20

# Los mismos topes que Skydropx, y por la misma razón: un checkout que se congela
# esperando a una paquetería cuesta más que el envío. Aquí pesa DOBLE, porque este
# proveedor cotiza DESPUÉS del otro: si los dos se tardan lo suyo, se suman.
# El ritmo de consulta respeta su límite de 2 peticiones por segundo.
ESPERA_MAX_COTIZACION_S = 12
ESPERA_ENTRE_CONSULTAS_S = 0.7
ESPERA_MAX_GUIA_S = 30


# --------------------------------------------------------------- credenciales
def _credenciales() -> tuple:
    """Las credenciales efectivas: el entorno manda, y si no, las del Admin."""
    import secretos
    return (secretos.valor('ENVIOSINT_CLIENT_ID'),
            secretos.valor('ENVIOSINT_CLIENT_SECRET'))


def enabled() -> bool:
    """¿Hay con qué hablarle a este proveedor?

    Se piden las DOS credenciales porque su OAuth2 exige `client_id` Y `client_secret`
    (así lo marca su especificación). Sin las dos devuelve False, y el doble cotizador
    sigue con un solo proveedor, sin quejarse y sin romperse.
    """
    return all(_credenciales())


# ------------------------------------------------------------------- el token
# Igual que en Skydropx: el token se guarda en memoria y se renueva por reloj o cuando
# la API contesta 401. Pedir uno por cotización duplicaría cada llamada.
_TOKEN = {'valor': '', 'vence': 0.0}
MARGEN_TOKEN_S = 300


def olvidar_token() -> None:
    """Tira el token guardado. La siguiente llamada pedirá uno nuevo."""
    _TOKEN['valor'], _TOKEN['vence'] = '', 0.0


def _pedir_token() -> str:
    cid, secreto = _credenciales()
    if not enabled():
        raise RuntimeError('Faltan ENVIOSINT_CLIENT_ID / ENVIOSINT_CLIENT_SECRET')
    RITMO.esperar()          # el trámite del token también gasta cupo de la cuenta
    resp = requests.post(f'{API}/oauth/token',
                         headers={'Content-Type': 'application/json'},
                         json={'client_id': cid, 'client_secret': secreto,
                               'grant_type': 'client_credentials'},
                         timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f'{NOMBRE} /oauth/token {resp.status_code}: {resp.text[:300]}')
    data = resp.json() or {}
    token = str(data.get('access_token') or '')
    if not token:
        raise RuntimeError(f'{NOMBRE}: el oauth no devolvio access_token')
    try:
        dura = float(data.get('expires_in') or 0)
    except (TypeError, ValueError):
        dura = 0.0
    _TOKEN['valor'] = token
    _TOKEN['vence'] = time.time() + max(60.0, dura - MARGEN_TOKEN_S)
    return token


def _token(refrescar: bool = False) -> str:
    if refrescar or not _TOKEN['valor'] or time.time() >= _TOKEN['vence']:
        return _pedir_token()
    return _TOKEN['valor']


def _headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _llamar(fn, ruta: str, **kw):
    """Una petición con token. Si contesta 401, pide token nuevo y reintenta UNA vez."""
    RITMO.esperar()
    resp = fn(f'{API}{ruta}', headers=_headers(_token()), timeout=TIMEOUT, **kw)
    if getattr(resp, 'status_code', 0) == 401:
        olvidar_token()
        RITMO.esperar()
        resp = fn(f'{API}{ruta}', headers=_headers(_token(refrescar=True)),
                  timeout=TIMEOUT, **kw)
    if resp.status_code >= 300:
        raise RuntimeError(f'{NOMBRE} {ruta} {resp.status_code}: {resp.text[:300]}')
    return resp.json()


def _post(ruta: str, cuerpo: dict):
    return _llamar(requests.post, ruta, json=cuerpo)


def _get(ruta: str):
    return _llamar(requests.get, ruta)


# --------------------------------------------------------------- cotizar
# ⛔ EL REMITENTE ES EL MISMO. La casa despacha desde una sola dirección: tener dos
# remitentes distintos según la paquetería sería la forma perfecta de imprimir una guía
# con la dirección de nadie. Se lee de `skydropx.remitente()`, que es donde el Admin lo
# captura, y por eso este módulo no tiene pantalla propia de remitente.
def remitente() -> dict:
    return skydropx.remitente()


def _esperar_cotizacion(data: dict, espera_max: float) -> dict:
    """Vuelve a preguntar hasta que la cotización esté completa. O se rinde.

    Igual que en Skydropx PRO, que cotiza en diferido. Si este proveedor resultara
    contestar con los precios de una vez, `is_completed` no vendrá y el primer intento
    ya traerá las tarifas: `_opcion` las lee igual y el bucle no da ni una vuelta.
    """
    if not isinstance(data, dict):
        return {}
    qid = str(data.get('id') or '')
    limite = time.time() + max(0.0, espera_max)
    while not data.get('is_completed') and qid and time.time() < limite:
        time.sleep(ESPERA_ENTRE_CONSULTAS_S)
        nueva = _get(f'/quotations/{qid}')
        if isinstance(nueva, dict):
            data = nueva
    return data


def cotizacion(destino: dict, paquete: dict, origen: dict | None = None,
               espera_max: float | None = None, filtrar: bool = True) -> dict:
    """Una cotización completa de ESTE proveedor, en el mismo formato que la de Skydropx.

    Que devuelva la MISMA forma no es casualidad: es lo que permite que el comparador
    (`paqueterias.py`) trate a los dos proveedores como iguales y no tenga que saber de
    quién es cada tarifa más que para decirlo en pantalla.
    """
    if espera_max is None:
        espera_max = ESPERA_MAX_COTIZACION_S
    cuerpo = {'quotation': {
        'address_from': skydropx._direccion_cotizar(
            origen if origen is not None else remitente()),
        'address_to': skydropx._direccion_cotizar(destino),
        'parcel': skydropx._paquete_api(paquete or {}),
    }}
    data = _esperar_cotizacion(_post('/quotations', cuerpo), espera_max)
    crudas = data.get('rates') if isinstance(data.get('rates'), list) else []
    # Si la API contesta con los precios de una vez, no habrá `is_completed`: se da por
    # completa en cuanto hay tarifas con precio, en vez de tirar una cotización buena.
    completa = bool(data.get('is_completed')) or bool(
        [t for t in crudas if isinstance(t, dict) and t.get('success')])
    if not completa:
        logger.warning('%s: la cotizacion %s no termino en %ss; se sigue con el otro '
                       'proveedor', NOMBRE, data.get('id'), espera_max)
    opciones = [o for o in (skydropx._opcion(t) for t in crudas) if o] if completa else []
    for o in opciones:
        o['proveedor'] = CLAVE            # de quién es esta tarifa, para poder comprarla
    utiles = skydropx.solo_permitidas(opciones) if filtrar else sorted(
        opciones, key=lambda o: o['precio'])
    return {
        'id': str(data.get('id') or ''),
        'completa': completa,
        'requiere_verificar_origen': bool(data.get('requires_origin_verification')),
        'packages': data.get('packages') or [],
        'opciones': sorted(utiles, key=lambda o: o['precio']),
    }


def cotizar(cp_destino: str, paquete: dict, cp_desde: str = '',
            destino: dict | None = None) -> list:
    """Precios reales por peso y código postal. Misma firma que `skydropx.cotizar`."""
    a_donde = dict(destino or {})
    a_donde['zip'] = (cp_destino or a_donde.get('zip') or '').strip()
    origen = remitente()
    if (cp_desde or '').strip():
        origen = dict(origen, zip=cp_desde.strip())
    return cotizacion(a_donde, paquete, origen)['opciones']


# --------------------------------------------------------------- comprar guía
def comprar_guia(rate_id: str, destino: dict, paquete: dict,
                 package_number: int = 1) -> dict:
    """Compra la guía de una tarifa de ESTE proveedor.

    ⚠️ CUESTA DINERO. Nunca se ejecuta en pruebas: se cobra de la cuenta de
    enviosinternacionales.com, que es distinta de la de Skydropx.

    ⛔ `unique_shipment: true` — SEGURO CONTRA GUÍAS DUPLICADAS, y lo pide esta casa
    a propósito. Su API cachea la respuesta de un `rate_id` por 96 horas: si esta llamada
    se reintenta (se cortó la red, el proceso se reinició, alguien picó dos veces),
    devuelve la MISMA guía en vez de comprar otra. Sin esto, un reintento es una segunda
    guía pagada que nadie va a usar. Skydropx no ofrece el equivalente; aquí sí existe y
    sería tonto no usarlo.
    """
    cuerpo = {'shipment': {
        'rate_id': str(rate_id or ''),
        'unique_shipment': True,
        'address_from': skydropx._direccion_envio(remitente()),
        'address_to': skydropx._direccion_envio(destino),
        'packages': [dict(skydropx._paquete_api(paquete or {}),
                          package_number=package_number,
                          package_type=skydropx._empaque_sat(),
                          consignment_note=skydropx._clase_sat())],
    }}
    data = _post(RUTA_COMPRAR, cuerpo)
    guia = skydropx._guia_del_json(data)
    limite = time.time() + ESPERA_MAX_GUIA_S
    while not guia.get('tracking_number') and guia.get('shipment_id') and time.time() < limite:
        time.sleep(ESPERA_ENTRE_CONSULTAS_S)
        guia = skydropx._guia_del_json(_get(f"/shipments/{guia['shipment_id']}"))
    return guia
