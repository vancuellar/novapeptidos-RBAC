#!/usr/bin/env python3
"""
EL VIGILANTE — ¿la tienda esta viva AHORA MISMO?
=================================================

POR QUE EXISTE
--------------
El 30 y el 31 de julio de 2026 la tienda se cayo varias veces y nadie se
entero hasta que alguien la abrio a mano. Christian pidio un compromiso de
99.99% de tiempo en pie. Ese numero no se puede prometer si no se MIDE: sin
una bitacora, "casi siempre estuvo bien" es una opinion.

Esto corre en el EC2 desde cron cada 3 minutos. Hace tres cosas:

  1. Revisa la tienda y la API.
  2. Apunta el resultado en una bitacora (una linea por revision).
  3. Si algo se cae —o si vuelve— le manda un correo a Christian. Una vez, al
     cambiar de estado; no cada 3 minutos.

QUE REVISA (y por que cada cosa)
--------------------------------
  * API viva            GET /api/ -> {"status": "ok"}. Es la sonda que ya usan
                        docker, nginx y deploy.sh.
  * Catalogo con fondo   GET /api/products con decenas de productos. Un catalogo
                        vacio contesta 200 y no vende nada.
  * Portada servida     La portada trae <div id="root">, y el CSS y el JS a los
                        que apunta existen y pesan lo que deben. Asi se caza el
                        caso de "desplegamos un index.html que apunta a un
                        bundle que no subio".

QUE **NO** REVISA, Y ES A PROPOSITO
-----------------------------------
No abre un navegador: en un servidor de 2 GB, Chromium por cron es un lujo que
no se paga solo. La prueba de PINTADO (que React monte y que los botones se
puedan picar) vive en el otro lado, donde de verdad hace falta: en
`novapeptidos-UI/scripts/verificar-en-vivo.js`, que corre en cada despliegue y
da marcha atras sola si el sitio quedo roto.

LO QUE ESTE VIGILANTE NO PUEDE VER
----------------------------------
Corre DENTRO del mismo EC2 que vigila. Si el EC2 entero se apaga, tambien se
apaga el, y en la bitacora eso se ve como un HUECO, no como una caida. El
resumen cuenta esos huecos aparte y los declara "sin dato" — nunca los da por
buenos. Para vigilar de verdad la caida del servidor hace falta un segundo
ojo FUERA de AWS (ahi si serviria el servidor de JADA, o un Worker gratis de
Cloudflare).

USO
---
    python3 vigilante.py                # una revision (esto es lo que corre cron)
    python3 vigilante.py --resumen      # % de disponibilidad real medida
    python3 vigilante.py --resumen 7    # de los ultimos 7 dias
    python3 vigilante.py --probar-correo

INSTALACION EN EL SERVIDOR
--------------------------
    sudo mkdir -p /opt/exygen/vigilante
    sudo cp vigilante.py /opt/exygen/vigilante/
    # y en /etc/cron.d/exygen-vigilante:
    */3 * * * * root /usr/bin/python3 /opt/exygen/vigilante/vigilante.py >>/var/log/exygen-vigilante.log 2>&1

Vive FUERA de /opt/exygen/app a proposito: `deploy.sh --rollback` hace
`git reset --hard` en esa carpeta, y el vigilante no se puede quedar mudo justo
en el momento en que mas se necesita.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SITIO = os.environ.get('EXYGEN_SITIO', 'https://exygenlabs.com')
API = os.environ.get('EXYGEN_API', 'https://api.exygenlabs.com/api')
CARPETA = os.environ.get('EXYGEN_VIGILANTE_DIR', '/opt/exygen/vigilante')
BITACORA = os.path.join(CARPETA, 'bitacora.jsonl')
ESTADO = os.path.join(CARPETA, 'estado.json')
ENV_APP = os.environ.get('EXYGEN_ENV', '/opt/exygen/app/.env')

TIEMPO = 20               # segundos de paciencia por peticion
FALLAS_PARA_GRITAR = 2    # dos revisiones malas seguidas antes de dar la alarma
# ⛔ EL PISO MIDE EL CATALOGO ENTERO, NO LO QUE ESTA A LA VENTA (2026-08-05).
# Era 40 a secas, y el 5-ago mando una alarma FALSA a Christian diciendo que la
# tienda estaba caida: ese dia el dejo a la venta SOLO las 13 presentaciones que
# tiene en bodega y escondio las otras 192, a proposito. La tienda estaba perfecta.
#
# Un piso escrito a mano convierte cada decision de negocio en una alarma falsa, y
# una alarma que grita cuando la casa hizo lo que QUISO se aprende a ignorar — que es
# la peor averia posible en un vigia. Es la misma leccion que ya se aprendio en
# `auditoria-e2e.js` el 1-ago con las presentaciones ocultadas por ventana de sentido.
#
# Lo que este vigia tiene que cazar de verdad son DOS cosas, y las dos siguen vivas:
#   1. que no haya NADA que vender (cero productos), y
#   2. que la respuesta venga TRUNCADA — se mide a la venta + escondidos, porque un
#      truncamiento se lleva los dos por delante y llega en decenas de menos.
PRODUCTOS_MINIMOS = 40    # a la venta + escondidos; menos que esto es truncamiento


# --------------------------------------------------------------------------
# Credenciales: el .env del servidor manda. Se lee a mano para no depender de
# nada instalado (este script corre con el python del sistema, sin venv).
# --------------------------------------------------------------------------
def _del_env(clave, poromision=''):
    if os.environ.get(clave):
        return os.environ[clave]
    try:
        with open(ENV_APP, 'r', encoding='utf-8') as f:
            for linea in f:
                linea = linea.strip()
                if linea.startswith(clave + '='):
                    return linea.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return poromision


def _traer(url, quiero_texto=True):
    """Devuelve (codigo, cuerpo, milisegundos). Nunca lanza."""
    t0 = time.time()
    pet = urllib.request.Request(url, headers={
        'User-Agent': 'ExygenVigilante/1.0',
        'Cache-Control': 'no-cache',
    })
    try:
        with urllib.request.urlopen(pet, timeout=TIEMPO) as r:
            crudo = r.read()
            ms = int((time.time() - t0) * 1000)
            return r.status, (crudo.decode('utf-8', 'replace') if quiero_texto else crudo), ms
    except urllib.error.HTTPError as e:
        return e.code, '', int((time.time() - t0) * 1000)
    except Exception as e:                                    # red, DNS, TLS…
        return 0, f'{type(e).__name__}: {e}'[:150], int((time.time() - t0) * 1000)


# --------------------------------------------------------------------------
# Las revisiones
# --------------------------------------------------------------------------
def revisar_api(fallas, tiempos):
    codigo, cuerpo, ms = _traer(f'{API}/')
    tiempos['api'] = ms
    if codigo != 200:
        fallas.append(f'la API no contesta ({codigo or "sin conexion"}: {cuerpo[:70]})')
        return
    try:
        if json.loads(cuerpo).get('status') != 'ok':
            fallas.append('la API contesta pero no dice "ok"')
    except ValueError:
        fallas.append('la API contesta algo que no es JSON')


def revisar_catalogo(fallas, tiempos):
    codigo, cuerpo, ms = _traer(f'{API}/products')
    tiempos['catalogo'] = ms
    if codigo != 200:
        fallas.append(f'el catalogo no contesta ({codigo or "sin conexion"})')
        return
    try:
        datos = json.loads(cuerpo)
        cuantos = len(datos if isinstance(datos, list) else datos.get('products', []))
    except ValueError:
        fallas.append('el catalogo contesta algo que no es JSON')
        return
    # Cero es cero: no hay nada que vender, y eso si es la tienda caida.
    if cuantos == 0:
        fallas.append('el catalogo no trae NI UN producto: no se puede vender')
        return
    # Y el truncamiento se mide contra el catalogo COMPLETO. Que Christian esconda
    # 192 productos a proposito no es una averia; que se pierdan 192 sin que nadie
    # los escondiera, si — y eso lo caza igual, porque entonces tampoco aparecen
    # en la lista de escondidos.
    escondidos = 0
    codigo_o, cuerpo_o, _ = _traer(f'{API}/catalogo/ocultos')
    if codigo_o == 200:
        try:
            escondidos = len(json.loads(cuerpo_o).get('skus') or [])
        except ValueError:
            escondidos = 0
    if cuantos + escondidos < PRODUCTOS_MINIMOS:
        fallas.append(f'el catalogo trae {cuantos} a la venta + {escondidos} escondidos '
                      f'(deberian ser {PRODUCTOS_MINIMOS}+ en total): viene truncado')


def revisar_sitio(fallas, tiempos):
    codigo, html, ms = _traer(f'{SITIO}/?vigilante={int(time.time())}')
    tiempos['sitio'] = ms
    if codigo != 200:
        fallas.append(f'la portada no contesta ({codigo or "sin conexion"}: {html[:70]})')
        return
    if 'id="root"' not in html:
        fallas.append('la portada contesta pero no trae la caja de la aplicacion (id="root")')
        return

    # Los recursos a los que apunta el HTML tienen que existir. Aqui se caza el
    # caso feo: index.html nuevo apuntando a un bundle que no se subio.
    for etiqueta, patron, minimo in (
        ('el JavaScript', r'/static/js/main\.[a-z0-9]+\.js', 50_000),
        ('el CSS', r'/static/css/main\.[a-z0-9]+\.css', 5_000),
    ):
        m = re.search(patron, html)
        if not m:
            fallas.append(f'la portada no referencia {etiqueta} principal')
            continue
        # Se reintenta una vez. Cloudflare no estrena version en TODOS sus
        # bordes a la vez: durante unos segundos, justo tras un despliegue, un
        # borde puede traer el index.html nuevo y todavia no su bundle. Eso dura
        # segundos y no es una caida; sin este reintento cada despliegue dejaba
        # una mancha falsa en la bitacora y ensuciaba el % de disponibilidad.
        for intento in (1, 2):
            c, cuerpo, _ = _traer(SITIO + m.group(0), quiero_texto=False)
            if c == 200 and len(cuerpo) >= minimo:
                break
            if intento == 1:
                time.sleep(5)
                continue
            if c != 200:
                fallas.append(f'{etiqueta} ({m.group(0)}) contesta {c or "nada"} — el sitio sale en blanco')
            else:
                fallas.append(f'{etiqueta} pesa {len(cuerpo)} bytes: viene cortado')


def revisar():
    fallas, tiempos = [], {}
    revisar_api(fallas, tiempos)
    revisar_catalogo(fallas, tiempos)
    revisar_sitio(fallas, tiempos)
    return fallas, tiempos


# --------------------------------------------------------------------------
# Avisar
# --------------------------------------------------------------------------
def avisar(asunto, cuerpo_html):
    """Correo por Resend. Devuelve True/False; nunca lanza."""
    llave = _del_env('RESEND_API_KEY')
    if not llave:
        print('sin RESEND_API_KEY: no puedo avisar', file=sys.stderr)
        return False
    para = _del_env('ADMIN_NOTIFY_EMAIL', 'exygenlabs@gmail.com')
    remitente = _del_env('EMAIL_FROM', 'Exygen Labs <hola@exygenlabs.com>')
    datos = json.dumps({
        'from': remitente, 'to': [para], 'subject': asunto, 'html': cuerpo_html,
    }).encode()
    # El User-Agent NO es adorno: Resend esta detras de Cloudflare y bloquea el
    # que urllib pone por omision ("Python-urllib/3.10") con un 403 y el codigo
    # 1010. Con esto pasa. Costo de averiguarlo: un rato el 2026-07-31.
    pet = urllib.request.Request(
        'https://api.resend.com/emails', data=datos, method='POST',
        headers={'Authorization': f'Bearer {llave}', 'Content-Type': 'application/json',
                 'User-Agent': 'ExygenVigilante/1.0'})
    try:
        with urllib.request.urlopen(pet, timeout=25) as r:
            return r.status in (200, 201)
    except Exception as e:
        print(f'no pude mandar el correo: {e}', file=sys.stderr)
        return False


def _lista(fallas):
    return ''.join(f'<li>{f}</li>' for f in fallas)


# --------------------------------------------------------------------------
# Estado con histeresis: un tropiezo de red no es una caida.
# --------------------------------------------------------------------------
def leer_estado():
    try:
        with open(ESTADO, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'caido': False, 'malas_seguidas': 0, 'desde': None}


def guardar(ruta, datos, agregar=False):
    os.makedirs(os.path.dirname(ruta) or '.', exist_ok=True)
    with open(ruta, 'a' if agregar else 'w', encoding='utf-8') as f:
        f.write(datos)


def una_revision():
    ahora = datetime.now(timezone.utc)
    fallas, tiempos = revisar()
    bien = not fallas

    guardar(BITACORA, json.dumps({
        'cuando': ahora.isoformat(timespec='seconds'),
        'bien': bien, 'fallas': fallas, 'ms': tiempos,
    }, ensure_ascii=False) + '\n', agregar=True)

    st = leer_estado()
    st['malas_seguidas'] = 0 if bien else st.get('malas_seguidas', 0) + 1

    if not bien and not st.get('caido') and st['malas_seguidas'] >= FALLAS_PARA_GRITAR:
        st['caido'] = True
        st['desde'] = ahora.isoformat(timespec='seconds')
        avisar('⛔ Exygen: la tienda esta CAIDA', (
            '<h2>La tienda no esta funcionando</h2>'
            f'<p>Detectado el {ahora:%d/%m/%Y a las %H:%M} UTC, tras '
            f'{st["malas_seguidas"]} revisiones malas seguidas.</p>'
            f'<ul>{_lista(fallas)}</ul>'
            f'<p><a href="{SITIO}">Abrir la tienda</a></p>'
            '<p style="color:#888;font-size:12px">Te aviso una sola vez. '
            'Cuando vuelva a estar bien, te mando el aviso de que ya paso.</p>'))
        print('CAIDA avisada:', '; '.join(fallas))

    elif bien and st.get('caido'):
        desde = st.get('desde')
        cuanto = ''
        if desde:
            try:
                minutos = int((ahora - datetime.fromisoformat(desde)).total_seconds() // 60)
                cuanto = f' Estuvo caida unos {minutos} minutos.'
            except ValueError:
                pass
        st['caido'] = False
        st['desde'] = None
        avisar('✅ Exygen: la tienda ya volvio', (
            f'<h2>Ya funciona</h2><p>Restablecida el {ahora:%d/%m/%Y a las %H:%M} UTC.'
            f'{cuanto}</p><p><a href="{SITIO}">Abrir la tienda</a></p>'))
        print('RECUPERADA')

    guardar(ESTADO, json.dumps(st))

    if bien:
        print(f'ok {ahora:%H:%M} ' + ' '.join(f'{k}={v}ms' for k, v in tiempos.items()))
        return 0
    print(f'MAL {ahora:%H:%M} (' + '; '.join(fallas) + ')')
    return 1


# --------------------------------------------------------------------------
# El numero que Christian pidio
# --------------------------------------------------------------------------
def resumen(dias=1):
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    revisiones = []
    try:
        with open(BITACORA, 'r', encoding='utf-8') as f:
            for linea in f:
                try:
                    r = json.loads(linea)
                    if datetime.fromisoformat(r['cuando']) >= desde:
                        revisiones.append(r)
                except (ValueError, KeyError):
                    continue
    except OSError:
        print(f'Todavia no hay bitacora en {BITACORA}.')
        return 1

    if not revisiones:
        print(f'Sin revisiones en los ultimos {dias} dia(s).')
        return 1

    buenas = sum(1 for r in revisiones if r['bien'])
    total = len(revisiones)
    pct = 100.0 * buenas / total

    # Huecos: si entre dos revisiones pasaron mas de 10 minutos, el vigilante
    # no estaba mirando. Eso NO se cuenta como "estuvo bien".
    tiempos = sorted(datetime.fromisoformat(r['cuando']) for r in revisiones)
    huecos = sum(1 for a, b in zip(tiempos, tiempos[1:]) if (b - a).total_seconds() > 600)
    minutos_sin_dato = sum(int((b - a).total_seconds() // 60)
                           for a, b in zip(tiempos, tiempos[1:]) if (b - a).total_seconds() > 600)

    print(f'Ultimos {dias} dia(s) — {total} revisiones')
    print(f'  En pie:   {pct:.3f} %   ({buenas} bien, {total - buenas} mal)')
    print(f'  Objetivo: 99.990 %   -> {"SE CUMPLE" if pct >= 99.99 else "NO se cumple"}')
    if huecos:
        print(f'  ⚠️  {huecos} hueco(s) sin vigilancia, ~{minutos_sin_dato} min SIN DATO '
              '(el vigilante no estaba mirando: no cuentan como buenos).')
    malas = [r for r in revisiones if not r['bien']][-5:]
    if malas:
        print('  Ultimos problemas:')
        for r in malas:
            print(f"    {r['cuando']}  {'; '.join(r['fallas'])[:110]}")
    return 0


if __name__ == '__main__':
    if '--resumen' in sys.argv:
        i = sys.argv.index('--resumen')
        d = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 1
        sys.exit(resumen(d))
    if '--probar-correo' in sys.argv:
        ok = avisar('Exygen: prueba del vigilante', (
            '<p>Esto es una prueba. El vigilante ya esta puesto y te va a avisar '
            'si la tienda se cae.</p>'))
        print('correo enviado' if ok else 'NO se pudo enviar')
        sys.exit(0 if ok else 1)
    sys.exit(una_revision())
