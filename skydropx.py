"""Skydropx PRO (API v2): cotizar un envío y comprar la guía, por REST directa.

⛔ ESTA ES LA API **PRO**, NO LA VIEJA. Las credenciales del dueño son de Skydropx
PRO y esa API no se parece a la anterior:

  · la vieja se autenticaba con `Authorization: Token token=<api key>`;
    la PRO usa **OAuth2**: se cambian client_id + client_secret por un token que
    dura 2 horas y viaja como `Authorization: Bearer <token>`;
  · la vieja devolvía las tarifas en la misma respuesta;
    la PRO **cotiza en diferido**: contesta al instante con las tarifas VACÍAS y
    hay que volver a preguntar hasta que `is_completed` sea true (comprobado en
    vivo el 2026-07-28: entre 2 y 5 segundos).

Endpoints, tal como se comprobaron contra el servicio real:

  POST /oauth/token        client_id + client_secret     → access_token (2 h)
  POST /quotations         CP origen/destino + bulto     → id de cotización (vacía)
  GET  /quotations/{id}    ídem                          → las tarifas, ya con precio
  POST /shipments          rate_id + direcciones + bulto → el envío y su guía

Se enciende con DOS credenciales, no con una:

  SKYDROPX_CLIENT_ID
  SKYDROPX_CLIENT_SECRET   del entorno o pegadas desde Admin → Cobros (secretos.py)

⛔ SIN CREDENCIALES NO SE ROMPE NADA. `enabled()` devuelve False, el checkout no
ofrece cotización y la compra sigue su curso exactamente como hoy. Lo único que
pasa es que queda dicho en la bitácora. Un checkout que se cae porque falta una
llave de paquetería es un checkout que deja de vender.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# La cuenta PRO del dueño vive aquí. Se deja configurable para poder apuntar a un
# ambiente de pruebas sin tocar código.
API = os.environ.get('SKYDROPX_API_URL', 'https://pro.skydropx.com/api/v1').rstrip('/')
TIMEOUT = 20                       # por cada petición HTTP suelta


# ⛔ SOLO SE LE ENSEÑAN ESTAS PAQUETERÍAS AL CLIENTE (Christian, 2026-07-28: empezó
# siendo solo Estafeta y ese mismo día sumó Paquetexpress y FedEx). La API devuelve
# más de veinte; el cliente ve tres. Ampliar es agregar un renglón aquí.
# Van EN MINÚSCULAS y sin acentos, que es como las manda la API PRO en
# `provider_name` y como se comparan aquí.
PAQUETERIAS_PERMITIDAS = ('estafeta', 'paquetexpress', 'fedex')

# ⛔ EL PLAZO TAMBIÉN ES UN FILTRO, NO SOLO EL PRECIO (Christian, 2026-07-28).
# Comprobado en vivo: Paquetexpress "Nacional" sale a $51.25 pero tarda 7 días, y
# el sitio le promete al cliente 2-5. Ordenar solo por precio pondría ESA hasta
# arriba y rompería la promesa por ahorrarse cien pesos. Las tarifas que se pasan
# de este número no se le enseñan al cliente. Cambiarlo es cambiar esta línea.
DIAS_MAXIMOS_ENTREGA = 5
# Se PAGA por llegar antes (Christian, 2026-07-28: "pagamos un poco mas por envio
# express"). Como al cliente se le cobran $250 parejo, lo barato deja de ser el
# criterio: entre dos opciones que cumplen el plazo, gana la que llega ANTES, y solo
# se desempata por precio. Medido desde Playa del Carmen, lo de $51 tarda 7-8 dias y
# rompe la promesa de "2-5 dias" del sitio; lo que la cumple anda en $139-$165, que
# cabe de sobra en los $250.
PREFERIR_MAS_RAPIDO = True

# La cotización es asíncrona y el checkout NO se puede colgar esperándola. Si a
# los 12 segundos la paquetería no terminó, se devuelve sin opciones y el checkout
# sigue vendiendo como hoy. Un carrito que se congela cuesta más que un envío.
ESPERA_MAX_COTIZACION_S = 12
ESPERA_ENTRE_CONSULTAS_S = 0.7

# Comprar la guía sí puede tardar más: pasa después de que el cliente ya pagó, en
# segundo plano, y ahí nadie está mirando una rueda girar.
ESPERA_MAX_GUIA_S = 30


# --------------------------------------------------------------- credenciales
def _credenciales() -> tuple:
    """Las credenciales efectivas: el entorno manda, y si no, las del Admin."""
    import secretos
    return (secretos.valor('SKYDROPX_CLIENT_ID'),
            secretos.valor('SKYDROPX_CLIENT_SECRET'))


def enabled() -> bool:
    """Con una sola credencial no se puede hablar con la API: se piden las dos."""
    return all(_credenciales())


# ------------------------------------------------------------------- el token
# El token dura 2 horas (`expires_in: 7200`, comprobado). Pedir uno nuevo en cada
# cotización sería duplicar cada llamada, así que se guarda en memoria y se renueva
# solo: por reloj, con margen, o cuando la API contesta 401.
_TOKEN = {'valor': '', 'vence': 0.0}
MARGEN_TOKEN_S = 300               # se renueva 5 min antes de vencer, no al filo


def olvidar_token() -> None:
    """Tira el token guardado. La siguiente llamada pedirá uno nuevo."""
    _TOKEN['valor'], _TOKEN['vence'] = '', 0.0


def _pedir_token() -> str:
    cid, secreto = _credenciales()
    if not (cid and secreto):
        raise RuntimeError('Faltan SKYDROPX_CLIENT_ID / SKYDROPX_CLIENT_SECRET')
    resp = requests.post(f'{API}/oauth/token',
                         headers={'Content-Type': 'application/json'},
                         json={'client_id': cid, 'client_secret': secreto,
                               'grant_type': 'client_credentials'},
                         timeout=TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f'Skydropx /oauth/token {resp.status_code}: {resp.text[:300]}')
    data = resp.json() or {}
    token = str(data.get('access_token') or '')
    if not token:
        raise RuntimeError('Skydropx: el oauth no devolvio access_token')
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
    """Una petición con token. Si contesta 401, pide token nuevo y reintenta UNA vez.

    Un token puede morir antes de tiempo (lo revocan desde el panel, cambian las
    credenciales). Reintentar una vez evita que eso tumbe una cotización; reintentar
    en bucle convertiría un 401 legítimo en una tormenta de peticiones.
    """
    resp = fn(f'{API}{ruta}', headers=_headers(_token()), timeout=TIMEOUT, **kw)
    if getattr(resp, 'status_code', 0) == 401:
        olvidar_token()
        resp = fn(f'{API}{ruta}', headers=_headers(_token(refrescar=True)),
                  timeout=TIMEOUT, **kw)
    if resp.status_code >= 300:
        raise RuntimeError(f'Skydropx {ruta} {resp.status_code}: {resp.text[:300]}')
    return resp.json()


def _post(ruta: str, cuerpo: dict):
    return _llamar(requests.post, ruta, json=cuerpo)


def _get(ruta: str):
    return _llamar(requests.get, ruta)


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

# Los campos del remitente, en el orden en que se piden en el panel. La clave es la
# interna de esta casa y el valor la variable de entorno equivalente. Existe como
# tabla y no como veinte renglones sueltos para que agregar un campo sea agregar
# una línea, aquí y en ningún otro lado.
CAMPOS_REMITENTE = (
    ('name', 'SKYDROPX_FROM_NAME'),
    ('company', 'SKYDROPX_FROM_COMPANY'),
    ('address1', 'SKYDROPX_FROM_ADDRESS1'),
    ('address2', 'SKYDROPX_FROM_ADDRESS2'),
    # La colonia: la API PRO la exige (`area_level3`) y no se puede deducir del CP.
    ('colonia', 'SKYDROPX_FROM_COLONIA'),
    ('city', 'SKYDROPX_FROM_CITY'),
    ('province', 'SKYDROPX_FROM_PROVINCE'),
    ('zip', 'SKYDROPX_FROM_ZIP'),
    ('country', 'SKYDROPX_FROM_COUNTRY'),
    ('phone', 'SKYDROPX_FROM_PHONE'),
    ('email', 'SKYDROPX_FROM_EMAIL'),
    ('reference', 'SKYDROPX_FROM_REFERENCE'),
)

# Lo único que se rellena solo cuando nadie lo escribió. El resto va vacío a
# propósito: un remitente a medias tiene que VERSE a medias, no parecer completo.
REMITENTE_POR_OMISION = {'company': 'Exygen Labs', 'country': 'MX',
                         'reference': 'Recepcion'}

# ⛔ SE CONFIGURA DESDE EL PANEL, NO EN EL CÓDIGO. Christián trabaja desde el
# teléfono y la dirección es la de un trabajador que puede cambiar: hornearla en el
# repositorio sería publicar el domicilio de una persona en GitHub. Aquí solo vive
# un hueco; server.py lo rellena desde la base al arrancar y cada vez que se guarda.
#
# EL ENTORNO SIEMPRE MANDA sobre el panel, igual que con las llaves de cobro: así un
# despliegue nunca queda a merced de lo que haya en la base.
_DEL_PANEL: dict = {}


def cargar_remitente_del_panel(datos: dict) -> int:
    """Guarda en memoria el remitente que capturó el admin. Devuelve cuántos campos."""
    _DEL_PANEL.clear()
    for clave, _env in CAMPOS_REMITENTE:
        valor = str((datos or {}).get(clave) or '').strip()
        if valor:
            _DEL_PANEL[clave] = valor
    return len(_DEL_PANEL)


def remitente() -> dict:
    """La dirección de quien envía, en el formato interno de esta casa.

    Se traduce al de Skydropx en `_direccion_*`. Así el resto del sitio (server.py
    arma el destino con estas mismas claves) no tiene que saber cómo se llama cada
    campo en la API de hoy.

    Orden de mando: entorno → panel de Admin → por omisión.
    """
    r = {}
    for clave, env in CAMPOS_REMITENTE:
        r[clave] = (os.environ.get(env) or _DEL_PANEL.get(clave)
                    or REMITENTE_POR_OMISION.get(clave, ''))
    return r


def origen_del_remitente(datos: dict | None = None) -> str:
    """De dónde salió cada dato: 'servidor', 'panel' o nada. Para que se vea en el panel."""
    for _clave, env in CAMPOS_REMITENTE:
        if os.environ.get(env):
            return 'servidor'
    return 'panel' if (_DEL_PANEL or datos) else ''


def remitente_configurado() -> bool:
    """¿Ya tenemos una dirección de origen de verdad?

    Se exige lo mínimo con lo que una paquetería puede recoger, y lo mínimo que la
    API PRO acepta sin devolver 422: nombre, calle, ciudad, estado, CP, teléfono y
    correo — y que ninguno siga marcado como PENDIENTE.
    """
    r = remitente()
    obligatorios = ('name', 'address1', 'city', 'province', 'zip', 'phone', 'email')
    return all((r.get(k) or '').strip() and REMITENTE_PENDIENTE not in (r.get(k) or '')
               for k in obligatorios)


def cp_origen() -> str:
    return (remitente().get('zip') or '').strip()


# --------------------------------------------------------------- direcciones
# La API PRO exige `area_level1/2/3` (estado, municipio, colonia) y NO acepta que
# vayan vacíos. Pero — comprobado en vivo el 2026-07-28 — el precio lo decide el
# CÓDIGO POSTAL: la misma cotización con los estados de verdad y con basura
# ("XX/YY/ZZ") devolvió exactamente las mismas 12 tarifas y los mismos importes.
#
# Por eso al COTIZAR, donde lo único que el checkout sabe del cliente es su CP, se
# rellena con lo que haya y si no con este relleno. Al COMPRAR sí se manda lo real
# del pedido: eso es lo que se imprime en la guía.
AREA_DESCONOCIDA = 'N/D'


def _direccion_cotizar(d: dict, cp: str = '') -> dict:
    """Lo que /quotations necesita: país, CP y los tres niveles de área."""
    d = d or {}
    return {
        'country_code': (d.get('country') or 'MX').strip().lower() or 'mx',
        'postal_code': (cp or d.get('zip') or '').strip(),
        'area_level1': (d.get('province') or '').strip() or AREA_DESCONOCIDA,
        'area_level2': (d.get('city') or '').strip() or AREA_DESCONOCIDA,
        'area_level3': (d.get('colonia') or d.get('city') or '').strip() or AREA_DESCONOCIDA,
    }


def _direccion_envio(d: dict) -> dict:
    """Lo que /shipments necesita de más: a quién y en qué calle.

    Los datos de zona (país, CP, área) NO se mandan aquí: la API los toma de la
    cotización a la que pertenece el `rate_id` (comprobado en vivo — mandarlos
    aparte no los usa). Estos cinco campos sí son obligatorios y sí se imprimen.
    """
    d = d or {}
    calle = ' '.join(x for x in ((d.get('address1') or '').strip(),
                                 (d.get('address2') or '').strip()) if x)
    return {
        'name': (d.get('name') or '').strip(),
        'company': (d.get('company') or '').strip(),
        'street1': calle,
        'phone': (d.get('phone') or '').strip(),
        'email': (d.get('email') or '').strip(),
        # La API la exige y no la deja vacía. Sin referencia del cliente se manda
        # la colonia o la ciudad: algo verdadero antes que una cadena inventada.
        'reference': ((d.get('reference') or '').strip()
                      or (d.get('colonia') or '').strip()
                      or (d.get('city') or '').strip() or 'Sin referencia'),
    }


# --------------------------------------------------------------- cotizar
def _normaliza(nombre: str) -> str:
    return (nombre or '').strip().lower().translate(str.maketrans('áéíóúü', 'aeiouu'))


def permitida(proveedor: str) -> bool:
    n = _normaliza(proveedor)
    return any(p in n for p in PAQUETERIAS_PERMITIDAS)


def dentro_del_plazo(dias) -> bool:
    """¿Esta tarifa cumple la promesa de entrega del sitio?

    Un 0 quiere decir "la paquetería no dijo cuántos días", no "llega hoy": no se
    castiga por falta de dato, se castiga por exceso de días.
    """
    try:
        d = int(dias or 0)
    except (TypeError, ValueError):
        d = 0
    return d <= DIAS_MAXIMOS_ENTREGA


def solo_permitidas(opciones: list) -> list:
    """El filtro de la regla: al cliente solo se le enseña lo que está en la lista.

    Dos cedazos, no uno: la paquetería tiene que estar permitida Y cumplir el plazo.
    """
    buenas = [o for o in (opciones or [])
              if permitida(o.get('paqueteria_id') or o.get('paqueteria', ''))
              and dentro_del_plazo(o.get('dias'))]
    if not PREFERIR_MAS_RAPIDO:
        return buenas
    # Primero los DIAS, luego el precio. Un 0 en dias significa "no dijo", no "hoy":
    # se manda al final para que no le gane a una opcion que si promete una fecha.
    def orden(o):
        d = o.get('dias') or 0
        try: d = int(d)
        except (TypeError, ValueError): d = 0
        return (d if d > 0 else 99, float(o.get('precio') or o.get('costo') or 0))
    return sorted(buenas, key=orden)


def _opcion(cruda: dict) -> dict | None:
    """Una tarifa de la API PRO traducida a lo que este sitio entiende.

    Una tarifa que no trae `success: true` viene con `amount: null` y `total: null`
    (sin cobertura, sin convenio, sin precio). En la prueba real salieron 12 buenas
    de 27. Esas no son opciones: son ruido.
    """
    if not isinstance(cruda, dict) or not cruda.get('success'):
        return None
    try:
        precio = float(cruda.get('total') or 0)
    except (TypeError, ValueError):
        precio = 0.0
    if precio <= 0:
        return None                     # una tarifa sin precio no es una opción
    try:
        dias = int(cruda.get('days') or 0)
    except (TypeError, ValueError):
        dias = 0
    return {
        'rate_id': str(cruda.get('id') or ''),
        # `paqueteria` es lo que se le enseña al cliente y lo que se guarda en el
        # pedido ("Estafeta"); `paqueteria_id` es como la nombra la API ("estafeta")
        # y es contra lo que se compara la lista de permitidas.
        'paqueteria': str(cruda.get('provider_display_name')
                          or cruda.get('provider_name') or ''),
        'paqueteria_id': _normaliza(cruda.get('provider_name')),
        'servicio': str(cruda.get('provider_service_name') or ''),
        'servicio_codigo': str(cruda.get('provider_service_code') or ''),
        'dias': dias,
        'precio': round(precio, 2),
        'moneda': str(cruda.get('currency_code') or 'MXN'),
    }


def _paquete_api(paquete: dict) -> dict:
    return {
        'length': paquete.get('largo_cm'),
        'width': paquete.get('ancho_cm'),
        'height': paquete.get('alto_cm'),
        'weight': paquete.get('peso_kg'),
    }


def _esperar_cotizacion(data: dict, espera_max: float) -> dict:
    """Vuelve a preguntar hasta que la cotización esté completa. O se rinde.

    ⛔ ES EL CAMBIO GRANDE DE LA API PRO. `POST /quotations` contesta en menos de un
    segundo con las tarifas en `status: pending` y `amount: null`; los precios
    aparecen después. Medido en vivo: entre 2 y 5 segundos.

    Si se acaba el tiempo devuelve lo último que vio SIN completar, y quien llama
    decide. Aquí `cotizar` decide no enseñar nada: media cotización enseñaría la
    tarifa que alcanzó a llegar, no la mejor.
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
    """Una cotización completa: su id, su bulto y sus opciones ya filtradas.

    Devuelve también `id` y `packages` porque comprar la guía los necesita: el
    envío se crea contra el `rate_id` de ESTA cotización y con el mismo
    `package_number` (si no coinciden, la API lo rechaza).

    ⚠️ `requires_origin_verification` — la API lo devuelve en `true` para esta
    cuenta. Averiguado el 2026-07-28: es la verificación de la dirección de ORIGEN
    que Skydropx PRO pide para poder recoger en ella (se aprueba desde su panel,
    quedando la dirección guardada como plantilla). NO estorbó para cotizar: las
    tarifas salieron completas y con precio de todas formas. Puede estorbar para
    COMPRAR o para que pasen a recoger, y eso no se pudo comprobar sin comprar una
    guía de verdad. Se devuelve aquí para que quede a la vista el día que una
    compra falle por ese motivo. Cada tarifa trae además el suyo, siempre `false`.
    """
    # El tope se lee AQUÍ y no en la firma a propósito: escrito como valor por
    # omisión quedaría congelado al importar el módulo y no habría forma de bajarlo.
    if espera_max is None:
        espera_max = ESPERA_MAX_COTIZACION_S
    cuerpo = {'quotation': {
        'address_from': _direccion_cotizar(origen if origen is not None else remitente()),
        'address_to': _direccion_cotizar(destino),
        'parcel': _paquete_api(paquete or {}),
    }}
    data = _esperar_cotizacion(_post('/quotations', cuerpo), espera_max)
    completa = bool(data.get('is_completed'))
    if not completa:
        logger.warning('Skydropx: la cotizacion %s no termino en %ss; el checkout '
                       'sigue sin opciones de envio', data.get('id'), espera_max)
    crudas = data.get('rates') if isinstance(data.get('rates'), list) else []
    opciones = [o for o in (_opcion(t) for t in crudas) if o] if completa else []
    # ⛔ `filtrar` es la diferencia entre lo que ve el CLIENTE y lo que ve LA CASA.
    # Al cliente solo se le enseñan las tres paqueterías permitidas y solo las que
    # cumplen el plazo prometido. Al admin, cuando despacha, se le enseña TODO lo
    # que la paquetería cotizó: es su dinero y es él quien decide si le conviene
    # una que tarda siete días. Ocultarle opciones a quien paga la guía es
    # exactamente lo que hace que un envío cueste $600.
    utiles = solo_permitidas(opciones) if filtrar else sorted(
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
    """Precios reales por peso y código postal. Devuelve SOLO las permitidas.

    `paquete` es lo que arma `envios.paquete_del_pedido`: peso en kg y medidas en cm.
    Ordena de más barato a más caro DENTRO de las que cumplen el plazo — que es el
    orden en que un cliente decide cuando todas le sirven.

    Si la paquetería no termina de cotizar a tiempo devuelve la lista vacía: el
    checkout se comporta como hoy en vez de quedarse colgado.
    """
    a_donde = dict(destino or {})
    a_donde['zip'] = (cp_destino or a_donde.get('zip') or '').strip()
    origen = remitente()
    if (cp_desde or '').strip():
        origen = dict(origen, zip=cp_desde.strip())
    return cotizacion(a_donde, paquete, origen)['opciones']


# --------------------------------------------------------------- comprar guía
# Códigos del SAT que la guía tiene que declarar. La clase es la del producto
# (31181701 = insumos de laboratorio) y el empaque el tipo de bulto.
def _clase_sat() -> str:
    return os.environ.get('SKYDROPX_CLASE_SAT', '31181701')


def _empaque_sat() -> str:
    return os.environ.get('SKYDROPX_EMPAQUE_SAT', '4G')


def _guia_del_json(data) -> dict:
    """Saca número de guía, PDF y rastreo de la respuesta de /shipments.

    ⚠️ NO VERIFICADO CONTRA UNA COMPRA REAL — comprar una guía cuesta dinero de
    verdad y no se hizo. La FORMA de la petición sí se comprobó en vivo (la API
    validó todo el cuerpo y solo se quejó del dato que se mandó mal a propósito);
    la de la RESPUESTA se deduce del formato JSON:API que devuelve `GET /shipments`
    (`{data, included, meta}`). Por eso se busca en los dos lados y con varios
    nombres: el día que se compre la primera guía, esto se confirma o se corrige.
    """
    if not isinstance(data, dict):
        return {}
    cuerpo = data.get('data') if isinstance(data.get('data'), dict) else data
    candidatos = [cuerpo]
    if isinstance(cuerpo.get('attributes'), dict):
        candidatos.append(cuerpo['attributes'])
    for inc in (data.get('included') or []):
        if isinstance(inc, dict):
            candidatos.append(inc)
            if isinstance(inc.get('attributes'), dict):
                candidatos.append(inc['attributes'])

    def primero(*nombres) -> str:
        for c in candidatos:
            for n in nombres:
                if isinstance(c, dict) and c.get(n):
                    return str(c[n])
        return ''

    return {
        'shipment_id': str(cuerpo.get('id') or ''),
        'tracking_number': primero('tracking_number', 'master_tracking_number'),
        'label_url': primero('label_url', 'label_file_url', 'pdf_url'),
        'tracking_url': primero('tracking_url_provider', 'tracking_url'),
    }


def comprar_guia(rate_id: str, destino: dict, paquete: dict,
                 package_number: int = 1) -> dict:
    """Compra la guía de una tarifa. Devuelve número, PDF y URL de rastreo.

    En la API PRO cotizar y comprar son dos pasos: `/quotations` da precios y
    `/shipments` convierte UNA de esas tarifas en un envío con guía. Las direcciones
    de zona salen de la cotización; aquí van los datos de la persona.

    ⚠️ Esta llamada CUESTA DINERO. Nunca se ejecuta en pruebas.
    """
    cuerpo = {'shipment': {
        'rate_id': str(rate_id or ''),
        'address_from': _direccion_envio(remitente()),
        'address_to': _direccion_envio(destino),
        'packages': [dict(_paquete_api(paquete or {}),
                          package_number=package_number,
                          package_type=_empaque_sat(),
                          consignment_note=_clase_sat())],
    }}
    data = _post('/shipments', cuerpo)
    guia = _guia_del_json(data)
    # La guía puede tardar en generarse del lado de la paquetería. Si todavía no
    # trae número, se vuelve a preguntar un rato — este camino corre en segundo
    # plano, con el pedido ya pagado, así que esperar no le cuesta a nadie.
    limite = time.time() + ESPERA_MAX_GUIA_S
    while not guia.get('tracking_number') and guia.get('shipment_id') and time.time() < limite:
        time.sleep(ESPERA_ENTRE_CONSULTAS_S)
        guia = _guia_del_json(_get(f"/shipments/{guia['shipment_id']}"))
    return guia


def guia_para(destino: dict, paquete: dict, servicio_codigo: str = '') -> dict:
    """De cero a guía en un solo llamado: cotiza, elige tarifa y compra.

    Si el pedido guardó qué servicio eligió el cliente (`servicio_codigo`), se
    respeta ESE; si ya no está disponible, cae a la más barata de las permitidas —
    nunca a una paquetería que el cliente no pidió ni a un plazo que no se le
    prometió.
    """
    if not remitente_configurado():
        # A propósito revienta en vez de comprar con un remitente inventado.
        raise RuntimeError('Falta configurar la direccion del remitente (SKYDROPX_FROM_*)')
    cot = cotizacion(destino, paquete, espera_max=ESPERA_MAX_GUIA_S)
    tarifas = cot['opciones']
    if not tarifas:
        raise RuntimeError('Skydropx no devolvio ninguna tarifa de las paqueterias permitidas')
    elegida = next((t for t in tarifas if servicio_codigo and t['servicio_codigo'] == servicio_codigo),
                   tarifas[0])
    paquetes = cot.get('packages') or []
    numero = 1
    if paquetes and isinstance(paquetes[0], dict):
        try:
            numero = int(paquetes[0].get('package_number') or 1)
        except (TypeError, ValueError):
            numero = 1
    guia = comprar_guia(elegida['rate_id'], destino, paquete, numero)
    guia['carrier'] = elegida['paqueteria']
    guia['servicio'] = elegida['servicio']
    guia['costo'] = elegida['precio']
    guia['shipment_id'] = guia.get('shipment_id') or ''
    return guia
