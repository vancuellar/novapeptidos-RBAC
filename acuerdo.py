"""ACUERDO DE DISTRIBUIDOR — aceptación electrónica con evidencia.

⛔ NACE APAGADO. Mientras `ACUERDO_DISTRIBUIDOR_ACTIVO` no valga `true`, este
módulo NO cambia absolutamente nada para nadie: no se pide firmar, no se
bloquea nada, no se deja de pagar una sola comisión. Está así a propósito —
el texto v2 todavía trae [CORCHETES] sin resolver (razón social, RFC,
domicilio, plazos y fuero) y quien los llena es Christián, que es el abogado.
Encenderlo antes sería recabar firmas sobre un contrato incompleto.

Cómo se enciende (UNA variable, en el .env del servidor):

    ACUERDO_DISTRIBUIDOR_ACTIVO=true

y se reinicia el backend con ./deploy.sh. Para apagarlo: se quita la variable
(o se pone en `false`) y se vuelve a desplegar. Apagar NO borra las
aceptaciones ya recabadas: siguen en `acuerdos_aceptados` como prueba.

QUÉ EXIGE LA LEY (Código de Comercio arts. 93, 93 Bis y 1298-A). Un contrato
por medios electrónicos es válido si se puede acreditar QUIÉN manifestó su
voluntad, SOBRE QUÉ TEXTO, y que ese texto quedó ÍNTEGRO y accesible para
consulta posterior. De ahí sale, uno por uno, lo que guarda `registrar()`:

  · user_id + email + nombre  → quién (usuario autenticado)
  · version + hash del texto  → sobre qué exactamente (integridad, art. 93 Bis)
  · accepted_at (ISO, UTC)    → cuándo
  · ip + user_agent           → desde dónde (elemento de atribución, 1298-A)
  · casilla NO premarcada     → el consentimiento es un acto, no un descuido

VERSIONES. `VERSION` es la firma del documento. Cuando el texto cambie en algo
esencial (comisión, mínimos, responsabilidad, disputas — cl. 4-e del propio
acuerdo) se sube `VERSION` y TODOS vuelven a firmar automáticamente: la
comparación es `aceptada != vigente`. Cambios de mera redacción: se sube
`VERSION` igual si se quiere volver a pedir, o se deja si no.

Módulo casi puro: el render y las reglas se prueban sin base de datos; las
cuatro funciones que sí la tocan reciben `db` por parámetro.
"""

import hashlib
import html as _html
import os
import re

# --------------------------------------------------------------------- versión
# ⚠️ SUBIR ESTO cuando cambie el texto = todos los distribuidores vuelven a
# firmar. Formato: vN-estado-AAAA-MM-DD.
VERSION = 'v2-borrador-2026-07-30'
FECHA = '2026-07-30'
TITULO = 'Acuerdo de Distribuidor — Exygen Labs'

# El texto todavía es BORRADOR: trae [corchetes] sin resolver. La bandera viaja
# al navegador para que, si alguien enciende el interruptor por error, la
# pantalla lo grite en vez de recabar firmas en silencio.
ES_BORRADOR = True

# La colección donde vive la prueba. Nombre pedido por Christián.
COLECCION = 'acuerdos_aceptados'

# El interruptor. Se lee en cada llamada (no se congela al importar) para que
# una prueba pueda encenderlo y apagarlo sin recargar el módulo.
ENV_INTERRUPTOR = 'ACUERDO_DISTRIBUIDOR_ACTIVO'


def activo() -> bool:
    """¿Está encendida la aceptación electrónica? Por omisión NO.

    Sólo `true`, `1`, `si`, `sí` o `yes` encienden. Cualquier otra cosa —vacío,
    ausente, `False`, una errata— deja el sistema apagado. El valor por defecto
    es el seguro: si la variable no llega al contenedor, nada cambia."""
    return (os.environ.get(ENV_INTERRUPTOR) or '').strip().lower() in (
        'true', '1', 'si', 'sí', 'yes')


# --------------------------------------------------------------------- el texto
# El acuerdo vive AQUÍ, dentro del repo, y no en un archivo suelto del escritorio:
# así viaja con cada despliegue, entra en el diff de cada commit y no puede
# cambiar sin que quede rastro en git. La copia de trabajo de Christián está en
# ~/Documents/Exygen Peptides/ACUERDO-DISTRIBUIDOR-BORRADOR.md; cuando él la
# apruebe, se pega aquí y se sube VERSION.
TEXTO = """# ACUERDO DE DISTRIBUIDOR — EXYGEN LABS
**BORRADOR v2 (2026-07-30) — redactado por F5, revisado por Codex (adversarial), pendiente de la pluma final de Christián. Los [CORCHETES] son decisiones suyas. Cambio mayor vs v1: el precio mínimo obligatorio se sustituyó por precio sugerido (riesgo LFCE/COFECE, art. 56).**

## 1. Partes y objeto
Acuerdo entre **[razón social — CONFIRMAR: ¿Servicios Profesionales Quimimid, S.A. de C.V.?, con RFC y domicilio]** ("la Empresa") y la persona que acepta electrónicamente al activar su cuenta ("el Distribuidor"). Objeto: compra y reventa **no exclusiva** de productos de investigación. No crea relación laboral, sociedad, franquicia ni agencia. **⛔ No activar aceptaciones hasta llenar los corchetes.**

## 2. Naturaleza de los productos (RUO)
Productos **exclusivamente para uso en investigación** (Research Use Only). Prohibido venderlos, promocionarlos o presentarlos como aptos para consumo humano o veterinario, medicamento, suplemento o cosmético. El Distribuidor **trasladará el aviso RUO a cada comprador final y conservará evidencia** de ello (leyenda visible en su punto de venta y confirmación del comprador).

## 3. Publicidad y afirmaciones
Prohibido publicitar con: dosis o protocolos de uso en humanos, testimonios de consumo, fotografías de "transformación corporal", claims terapéuticos o de seguridad. El material de marca solo se usa conforme a los lineamientos vigentes de la Empresa.

## 4. Precios de compra, mínimos y comisiones
a) **Precio de distribuidor**: únicamente en renglones de **cinco (5) o más piezas del mismo producto y presentación, dentro de un mismo pedido confirmado y de una misma cuenta**. No se acumulan pedidos, cuentas ni compras retroactivas para alcanzar el mínimo.
b) Renglones de 1 a 4 piezas: **precio de cliente** con el descuento general vigente.
c) Compra propia (directa o mediante su propio código, con o sin sesión): **sin comisión, sin puntos y sin crédito de nivel**. Se consideran compra propia las realizadas por cuenta compartida, familiares directos o sociedades relacionadas cuando la evidencia razonable así lo indique.
d) **Comisión inicial: 30%**, de la cual se resta el porcentaje de descuento otorgado al cliente; incrementos conforme al programa de niveles vigente. Descuento y comisión comparten el tope por producto; los insumos no llevan descuento.
e) La Empresa puede actualizar precios, topes y mínimos con efectos hacia adelante. Los **cambios esenciales** (comisión, mínimos, responsabilidad, disputas) requieren **nueva aceptación expresa por clic**; los demás, aviso con [15] días.

## 5. Precio de reventa **sugerido**
La Empresa publica un **precio de reventa sugerido, no vinculante**. El Distribuidor fija libremente sus precios. La Empresa no impone precio mínimo ni sanciona su inobservancia. *(Nota para Christián: Codex advirtió que un MAP obligatorio es conducta investigable bajo el art. 56 LFCE; si quieres MAP duro, pide antes opinión especializada en competencia económica.)*

## 6. Canales autorizados
La reventa en marketplaces (Mercado Libre, Amazon, etc.) y tiendas en línea propias requiere **autorización previa por escrito** de la Empresa, basada en criterios de cumplimiento RUO y presentación del producto — nunca en el precio. Prohibido mezclar inventario de la Empresa con producto de terceros bajo un mismo listado.

## 7. Prohibición de reetiquetado — alcance completo
Prohibido alterar, retirar, cubrir o sustituir **etiquetas, empaques externos, insertos, códigos, lotes o cualquier identificación**, física o digital (incluidas fotografías que oculten la marca); revender bajo otra marca o presentación; fraccionar o reenvasar. Violación = **terminación inmediata** y responsabilidad por daños.

## 8. Cuenta personal; prohibida la triangulación
La cuenta es personal e intransferible. Prohibido: prestar o compartir la cuenta, comprar para revendedores terceros, nombrar subdistribuidores sin autorización escrita, y usar cuentas múltiples o interpósitas personas para simular ventas o esquivar mínimos. La Empresa puede consolidar cuentas relacionadas para efectos de esta cláusula.

## 9. Trazabilidad, quejas y retiro
El Distribuidor llevará **registro por lote** de sus reventas (lote, fecha, comprador) y lo exhibirá a la Empresa dentro de [5] días hábiles cuando se le solicite por queja de calidad o retiro. Ante aviso de retiro, inmovilizará de inmediato el producto afectado. Almacenamiento conforme a las condiciones indicadas en la ficha del producto.

## 10. Exportación
Prohibido exportar los productos fuera de México sin autorización escrita de la Empresa y sin cumplir la regulación aplicable del país destino.

## 11. Marca y propiedad intelectual
Uso de marca solo para revender producto legítimo conforme a lineamientos. Prohibido registrar marcas, dominios o cuentas confusamente similares. La licencia termina con el Acuerdo.

## 12. Datos personales
Cada parte es responsable del tratamiento de los datos personales que recabe de sus propios clientes conforme a la LFPDPPP. El Distribuidor no usará datos de clientes de la Empresa recibidos a través de la plataforma para fines distintos del pedido correspondiente.

## 13. No captación
Durante la vigencia y [12] meses después, el Distribuidor no captará activamente, para un competidor directo, a clientes de la Empresa cuyos datos conoció por la plataforma. (Limitado a esos clientes; no es una prohibición general de competir.)

## 14. Confidencialidad
Listas de precios de distribuidor, condiciones comerciales, datos y secretos comerciales: confidenciales; sobrevive **[5] años** a la terminación.

## 15. Cumplimiento e indemnización
El Distribuidor responde del cumplimiento legal de sus reventas (fiscal, sanitario, aduanero, publicidad). Indemnizará a la Empresa por reclamaciones de terceros **causadas por actos u omisiones del Distribuidor**, previa notificación oportuna, con derecho del Distribuidor a participar en la defensa; se excluyen los daños causados por actos propios de la Empresa.

## 16. Vigencia, terminación e inventario remanente
Indefinida; terminación por cualquiera con aviso de [15] días. Terminación inmediata por: reetiquetado (cl. 7), presentación para consumo humano (cl. 2-3), triangulación (cl. 8), fraude en descuentos/comisiones o uso indebido de marca. Al terminar: cesan descuentos, comisiones y uso de marca; el inventario remanente puede (i) recomprarlo la Empresa al precio pagado, o (ii) agotarse en [60] días cumpliendo este Acuerdo; las comisiones devengadas y cobradas se liquidan conforme al programa.

## 17. Responsabilidad
Productos entregados para investigación con su **certificado de análisis correspondiente al lote enviado**, disponible en la plataforma. Responsabilidad total de la Empresa limitada al precio pagado del pedido correspondiente, **salvo dolo, mala fe o aquello que la ley no permita limitar** (art. 2106 CCF).

## 18. Aceptación electrónica (evidencia reforzada)
Aceptación mediante **casilla NO premarcada** + clic de conformidad al activar la cuenta, registrándose: **versión del documento, fecha y hora, usuario autenticado y dirección IP**, con **copia descargable** del texto aceptado (Código de Comercio arts. 93, 93 Bis, 1298-A). Cambios esenciales: nueva aceptación (cl. 4-e).

## 19. Sanciones graduadas
Publicidad o listado en infracción: aviso con **24 horas** para retirar; reincidencia dentro de **12 meses**: suspensión del canal; tercera vez o falta grave (cl. 16): terminación.

## 20. Disputas, ley y fuero
Escalonado: negociación directa ([15] días) → mediación ([30] días) → tribunales de **[Mérida, Yucatán — CONFIRMAR]**, leyes de los Estados Unidos Mexicanos, con renuncia a cualquier otro fuero.
"""


def hash_documento(texto: str = None) -> str:
    """Huella SHA-256 del texto. Va en cada aceptación.

    Es lo que responde «¿este PDF que enseñas es el que firmé?» sin tener que
    creerle a nadie: si el texto guardado no da este hash, no es el mismo texto
    (art. 93 Bis: integridad del mensaje de datos)."""
    return hashlib.sha256((TEXTO if texto is None else texto).encode('utf-8')).hexdigest()


# ------------------------------------------------------------ markdown -> HTML
# Un convertidor DIMINUTO y a propósito: sólo entiende lo que este documento usa
# (títulos, párrafos, negritas, cursivas y la regla horizontal). No se mete una
# dependencia nueva para 20 párrafos, y sobre todo: se escapa TODO antes de
# convertir, así que nada de lo que diga el texto puede inyectar HTML.
_NEGRITA = re.compile(r'\*\*(.+?)\*\*', re.S)
_CURSIVA = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', re.S)


def _inline(linea: str) -> str:
    seguro = _html.escape(linea, quote=False)
    seguro = _NEGRITA.sub(r'<strong>\1</strong>', seguro)
    seguro = _CURSIVA.sub(r'<em>\1</em>', seguro)
    return seguro


def md_a_html(texto: str = None) -> str:
    """El markdown del acuerdo, en HTML legible.

    Sin `<html>` ni `<body>`: son los bloques del documento y nada más, para que
    el sitio los pinte con SU estilo (claro/oscuro incluidos). La copia
    descargable sí los envuelve, en `copia_imprimible()`."""
    fuente = TEXTO if texto is None else texto
    partes = []
    for bruto in fuente.split('\n'):
        linea = bruto.strip()
        if not linea:
            continue
        if linea.startswith('---'):
            partes.append('<hr />')
        elif linea.startswith('## '):
            partes.append(f'<h2>{_inline(linea[3:])}</h2>')
        elif linea.startswith('# '):
            partes.append(f'<h1>{_inline(linea[2:])}</h1>')
        else:
            partes.append(f'<p>{_inline(linea)}</p>')
    return '\n'.join(partes)


def documento() -> dict:
    """El acuerdo vigente, listo para pintar. No toca la base de datos."""
    return {
        'version': VERSION,
        'fecha': FECHA,
        'titulo': TITULO,
        'borrador': ES_BORRADOR,
        'hash': hash_documento(),
        'html': md_a_html(),
        'texto': TEXTO,
    }


# ------------------------------------------------------------------ el registro
def _fila(doc: dict) -> dict:
    """Proyección de una aceptación para enseñarla. Se devuelve COMPLETA a
    propósito —incluida la IP— porque es la prueba, y quien la pide es su dueño
    (el propio distribuidor) o el admin. A un tercero nunca se le sirve."""
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != '_id'}


async def aceptacion_de(db, user_id: str) -> dict:
    """La ÚLTIMA aceptación de un usuario (la de la versión más reciente que
    firmó). Se guarda una fila por versión: firmar la v3 no borra la prueba de
    que en su día firmó la v2."""
    if db is None or not user_id:
        return None
    docs = await db[COLECCION].find({'user_id': user_id}).to_list(50)
    if not docs:
        return None
    docs.sort(key=lambda d: d.get('accepted_at', ''))
    return _fila(docs[-1])


async def historial_de(db, user_id: str) -> list:
    """Todas las versiones que ha firmado, de la más nueva a la más vieja."""
    if db is None or not user_id:
        return []
    docs = await db[COLECCION].find({'user_id': user_id}).to_list(50)
    docs.sort(key=lambda d: d.get('accepted_at', ''), reverse=True)
    return [_fila(d) for d in docs]


def es_distribuidor(user) -> bool:
    """A quién aplica el acuerdo. SÓLO al canal de distribución.

    El admin queda fuera: es la Empresa, no firma consigo mismo — y si quedara
    dentro, un interruptor mal puesto dejaría a Christián sin panel."""
    return bool(user) and (user or {}).get('role') == 'distributor'


async def firmo_la_vigente(db, user) -> bool:
    """¿Este usuario ya aceptó la versión que está vigente HOY?"""
    ace = await aceptacion_de(db, (user or {}).get('id'))
    return bool(ace) and ace.get('version') == VERSION


async def estado_para(db, user) -> dict:
    """Todo lo que la pantalla necesita saber, en una sola respuesta.

    `requiere_aceptacion` es la única llave que el navegador tiene que mirar, y
    con el interruptor apagado vale SIEMPRE False."""
    doc = documento()
    ace = await aceptacion_de(db, (user or {}).get('id')) if user else None
    aplica = activo() and es_distribuidor(user)
    return {
        **doc,
        'activo': activo(),
        'aplica': aplica,
        'aceptado': bool(ace) and ace.get('version') == VERSION,
        'aceptacion': ace,
        'requiere_aceptacion': aplica and not (bool(ace) and ace.get('version') == VERSION),
        # Firmó, pero una versión anterior: la pantalla lo dice con otras
        # palabras («el acuerdo cambió») en vez de tratarlo como si nunca firmara.
        'version_anterior': (ace or {}).get('version') if ace and ace.get('version') != VERSION else None,
    }


async def registrar(db, user, ip: str = '', user_agent: str = '', origen: str = 'panel') -> dict:
    """Guarda la aceptación. Es LA prueba; se escribe una vez por versión.

    Idempotente: volver a aceptar la misma versión no duplica la fila ni mueve
    la fecha original — la primera manifestación de voluntad es la que vale."""
    ya = await db[COLECCION].find_one({'user_id': user['id'], 'version': VERSION})
    if ya:
        return _fila(ya)
    fila = {
        'user_id': user['id'],
        'email': user.get('email', ''),
        'name': user.get('name', ''),
        'role': user.get('role', ''),
        'distributor_code': user.get('distributor_code', ''),
        'version': VERSION,
        'documento_fecha': FECHA,
        'documento_hash': hash_documento(),
        'documento_titulo': TITULO,
        'accepted_at': _ahora(),
        'ip': (ip or '')[:64],
        'user_agent': (user_agent or '')[:400],
        # De dónde salió el clic: 'activacion' (al activar la cuenta) o 'panel'
        # (distribuidor que ya existía y firma al entrar).
        'origen': origen,
        # La casilla no venía marcada. Se deja escrito para que la prueba lo diga
        # por sí sola y no dependa de que alguien recuerde cómo era la pantalla.
        'casilla_no_premarcada': True,
    }
    await db[COLECCION].insert_one(dict(fila))
    return _fila(fila)


def _ahora() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def ip_de(request) -> str:
    """La IP REAL de quien firma, no la del proxy.

    El backend vive detrás de dos saltos (Caddy público → la puerta nginx →
    el contenedor), así que `request.client.host` es siempre 127.0.0.1 y no
    sirve como prueba de nada. La buena es la PRIMERA de `X-Forwarded-For`:
    la que puso Caddy al recibir la conexión de internet. Las siguientes son
    los proxies de en medio.

    ⚠️ La cabecera la puede falsificar quien pega directo al backend — pero al
    backend sólo se llega por el 8010 de la puerta, y el único que habla con
    ella es Caddy, que la reescribe. Se guarda como el elemento de atribución
    que es (art. 1298-A), no como una identificación infalsificable."""
    cabeceras = getattr(request, 'headers', None) or {}
    reenviada = (cabeceras.get('x-forwarded-for') or '').split(',')[0].strip()
    if reenviada:
        return reenviada[:64]
    cliente = getattr(request, 'client', None)
    return (getattr(cliente, 'host', '') or '')[:64]


def user_agent_de(request) -> str:
    cabeceras = getattr(request, 'headers', None) or {}
    return (cabeceras.get('user-agent') or '')[:400]


# -------------------------------------------------------------------- bloqueos
# Mensaje único: si el aviso cambia, cambia en un solo sitio y los tres bloqueos
# dicen exactamente lo mismo.
AVISO = ('Para seguir operando como distribuidor necesitas leer y aceptar el '
         'Acuerdo de Distribuidor vigente. Entra a tu panel: te aparecerá en pantalla.')
CODIGO = 'acuerdo_pendiente'   # lo lee el navegador para abrir la pantalla solo


async def bloquea(db, user) -> bool:
    """¿Hay que frenar a este usuario? Con el interruptor apagado, NUNCA.

    Es un bloqueo SUAVE por diseño: sólo cuelga de las tres acciones que crean
    obligaciones nuevas (generar códigos, cotizar, devengar comisión). Ver su
    panel, sus pedidos, sus clientes y su historial sigue abierto — lo que ya
    ganó es suyo, haya firmado o no."""
    if not activo() or not es_distribuidor(user):
        return False
    return not await firmo_la_vigente(db, user)


async def filtrar_comisiones_sin_acuerdo(db, filas: list) -> list:
    """Quita del reparto a quien no haya firmado la versión vigente.

    Se aplica renglón por renglón, no al pedido entero: si el vendedor no firmó
    pero su upline sí, el upline cobra su diferencial igual — no se castiga a
    quien sí cumplió. Y el cliente conserva su descuento pase lo que pase: el
    precio ya se le prometió y esto es un asunto entre la Empresa y el canal.

    ⛔ Con el interruptor APAGADO devuelve la lista TAL CUAL, sin una sola
    consulta a la base. Ese es el camino de hoy: cero cambios, cero costo."""
    if not activo() or not filas:
        return filas
    ids = {f.get('distributor_id') for f in filas if f.get('distributor_id')}
    if not ids:
        return filas
    docs = await db[COLECCION].find(
        {'user_id': {'$in': list(ids)}, 'version': VERSION}, {'_id': 0, 'user_id': 1}).to_list(200)
    firmaron = {d['user_id'] for d in docs}
    return [f for f in filas if f.get('distributor_id') in firmaron]


# ---------------------------------------------------------- copia descargable
# El art. 93 Bis pide que el texto quede accesible para consulta posterior. Esta
# es esa copia: un HTML autocontenido (sin hojas de estilo externas, sin
# JavaScript) que se abre igual dentro de diez años y que se imprime a PDF con
# Ctrl+P. Lleva pegado el acta de aceptación: quién, cuándo, desde dónde y sobre
# qué hash.
_ESTILO = """
  body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.65;
         max-width: 46rem; margin: 0 auto; padding: 3rem 1.5rem; color: #16211c;
         background: #fff; }
  h1 { font-size: 1.5rem; line-height: 1.3; margin: 0 0 1.5rem; }
  h2 { font-size: 1.05rem; margin: 2rem 0 .5rem; }
  p { margin: 0 0 .9rem; text-align: justify; }
  hr { border: 0; border-top: 1px solid #d8ded9; margin: 2.5rem 0; }
  .acta { border: 1px solid #d8ded9; border-radius: 8px; padding: 1.25rem 1.5rem;
          margin-top: 2.5rem; font-family: -apple-system, system-ui, sans-serif;
          font-size: .85rem; background: #f7f9f8; }
  .acta h2 { margin-top: 0; font-family: inherit; font-size: .95rem; }
  .acta dt { font-weight: 600; float: left; clear: left; width: 11rem; }
  .acta dd { margin: 0 0 .35rem 11rem; word-break: break-word; }
  .sello { font-family: ui-monospace, Menlo, monospace; font-size: .72rem; }
  @media print { body { padding: 0; } .acta { break-inside: avoid; } }
"""


def _dato(etiqueta: str, valor) -> str:
    return f'<dt>{_html.escape(str(etiqueta))}</dt><dd>{_html.escape(str(valor or "—"))}</dd>'


def copia_imprimible(aceptacion: dict = None) -> str:
    """El documento completo + su acta de aceptación, en un solo archivo.

    Sin aceptación (el interruptor está apagado, o todavía no firma) se entrega
    igual el texto: es su derecho leerlo antes y después."""
    acta = ''
    if aceptacion:
        acta = (
            '<div class="acta"><h2>Acta de aceptación electrónica</h2><dl>'
            + _dato('Aceptado por', aceptacion.get('name'))
            + _dato('Correo', aceptacion.get('email'))
            + _dato('Código de distribuidor', aceptacion.get('distributor_code'))
            + _dato('Versión del documento', aceptacion.get('version'))
            + _dato('Fecha y hora (UTC)', aceptacion.get('accepted_at'))
            + _dato('Dirección IP', aceptacion.get('ip'))
            + _dato('Navegador (user-agent)', aceptacion.get('user_agent'))
            + _dato('Casilla premarcada', 'No (marcada por el usuario)')
            + f'<dt>Huella del texto (SHA-256)</dt><dd class="sello">'
              f'{_html.escape(aceptacion.get("documento_hash") or "")}</dd>'
            + '</dl><p style="margin-top:1rem">Constancia generada conforme a los '
              'artículos 93, 93 Bis y 1298-A del Código de Comercio.</p></div>'
        )
    return (
        '<!doctype html><html lang="es"><head><meta charset="utf-8" />'
        f'<title>{_html.escape(TITULO)} — {_html.escape(VERSION)}</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f'<style>{_ESTILO}</style></head><body>'
        + md_a_html() + acta +
        '</body></html>'
    )


def nombre_de_archivo() -> str:
    return f'acuerdo-distribuidor-exygen-{VERSION}.html'
