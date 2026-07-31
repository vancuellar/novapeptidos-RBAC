"""Archivo histórico de los reportes semanales de publicidad.

Qué es
------
Cada semana el pipeline de video (`Media/Videos/pipeline/record-reporte-ads.js`)
produce un MP4 de ~15 MB con el reporte de anuncios. Antes quedaba suelto en la
carpeta del proyecto y a la semana siguiente nadie volvía a encontrarlo. Aquí se
archiva: video + texto + LAS CIFRAS de esa semana, para poder compararlas.

⛔ POR QUÉ NO VA EN GIT. 15 MB × 52 semanas = 780 MB al año, y git no olvida: una
vez dentro, el repo carga ese peso para siempre en cada clon. Los videos viven en
DISCO del servidor, fuera del contenedor, igual que los COA y las fichas técnicas:

    REPORTES_ADS_DIR (por omisión /opt/exygen/reportes-ads)
      retencion.json                  <- lo que Christián decida conservar
      2026/
        2026-W31/
          video.mp4
          resumen.md                  <- el reporte escrito que acompaña al video
          datos.json                  <- semana, duración, tamaño y CIFRAS

El montaje está en docker-compose.yml (`/opt/exygen/reportes-ads:/data/reportes-ads`),
así que un despliegue azul/verde no se lleva nada por delante.

Las cifras importan MÁS que el video
------------------------------------
Nadie va a ver 52 videos de cinco minutos. Pero `datos.json` guarda gasto, clics,
conversaciones de WhatsApp, compras y costo por conversación de cada semana, y con
eso el panel pinta la evolución semana a semana aunque nunca se abra un video. Ese
es el valor real del archivo.

Retención
---------
NO se borra nada solo. `retencion.json` dice cuántas semanas conservar (52 por
omisión); lo que se pasa de esa raya se REPORTA como "por vencer" para que
Christián lo vea en el panel y decida. Borrar es siempre una acción suya.
"""

import json
import os
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

# Misma idea que COA_DIR y FICHA_DIR: la carpeta vive fuera del contenedor.
def dir_base() -> Path:
    """Se lee en cada llamada (no al importar) para que las pruebas puedan
    apuntar a una carpeta temporal con monkeypatch del entorno."""
    return Path(os.environ.get('REPORTES_ADS_DIR', '/opt/exygen/reportes-ads'))


# Una semana es "2026-W31" y nada más. Con esto un nombre inventado no puede
# salirse de la carpeta con "../" ni leer otro disco.
SEMANA_RE = re.compile(r'^(\d{4})-W(\d{2})$')

SEMANAS_POR_OMISION = 52

# Las cifras que se comparan semana con semana. Se fija la lista para que dos
# semanas distintas no terminen guardando llaves distintas y la tabla salga rota.
CIFRAS = (
    'gasto_usd',                 # lo que Meta cobró
    'impresiones',
    'clics',
    'conversaciones_wa',         # conversaciones de WhatsApp iniciadas
    'costo_conversacion_usd',
    'compras_atribuidas',        # las que Meta le cuelga a los anuncios
    'visitas',                   # embudo del propio sitio
    'fichas',                    # vistas de ficha de producto
    'compras_sitio',             # compras reales del sitio, vengan de donde vengan
)


def semana_de(dia) -> str:
    """La semana ISO a la que pertenece un día: date(2026,7,31) -> '2026-W31'."""
    if isinstance(dia, str):
        dia = date.fromisoformat(dia[:10])
    y, w, _ = dia.isocalendar()
    return f'{y}-W{w:02d}'


def _valida(semana: str):
    return bool(semana and isinstance(semana, str) and SEMANA_RE.match(semana))


def carpeta(semana: str) -> Path | None:
    """Carpeta de una semana. None si el nombre no tiene la forma exacta."""
    if not _valida(semana):
        return None
    anio = semana.split('-')[0]
    return dir_base() / anio / semana


def ruta_video(semana: str) -> Path | None:
    c = carpeta(semana)
    if c is None:
        return None
    p = c / 'video.mp4'
    return p if p.is_file() else None


def ruta_texto(semana: str) -> Path | None:
    c = carpeta(semana)
    if c is None:
        return None
    p = c / 'resumen.md'
    return p if p.is_file() else None


def nombre_descarga(semana: str) -> str:
    return f'Reporte-Publicidad-{semana}.mp4'


# --------------------------------------------------------------------- leer

def _leer_datos(dir_semana: Path) -> dict | None:
    try:
        with open(dir_semana / 'datos.json', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or not _valida(d.get('semana')):
        return None
    video = dir_semana / 'video.mp4'
    d['tiene_video'] = video.is_file()
    d['tiene_texto'] = (dir_semana / 'resumen.md').is_file()
    d['tamano_bytes'] = video.stat().st_size if d['tiene_video'] else 0
    # Las cifras que falten salen en None, NO en cero: cero se lee como "no gastó
    # nada" y nulo como "esa semana no se midió". Confundirlos cuesta dinero.
    cifras = d.get('cifras') or {}
    d['cifras'] = {k: cifras.get(k) for k in CIFRAS}
    return d


def listar() -> list:
    """Todas las semanas archivadas, de la más nueva a la más vieja."""
    base = dir_base()
    if not base.is_dir():
        return []
    out = []
    for anio in base.iterdir():
        if not anio.is_dir() or not re.fullmatch(r'\d{4}', anio.name):
            continue
        for sem in anio.iterdir():
            if not sem.is_dir() or not _valida(sem.name):
                continue
            d = _leer_datos(sem)
            if d:
                out.append(d)
    out.sort(key=lambda d: d['semana'], reverse=True)
    return out


def uno(semana: str) -> dict | None:
    c = carpeta(semana)
    if c is None or not c.is_dir():
        return None
    return _leer_datos(c)


def texto_de(semana: str) -> str:
    p = ruta_texto(semana)
    if p is None:
        return ''
    try:
        return p.read_text(encoding='utf-8')
    except Exception:
        return ''


# ----------------------------------------------------------------- retención

def retencion() -> dict:
    """Cuántas semanas conservar. Es una preferencia de Christián, no una regla
    del sistema: nadie borra por su cuenta, sólo se avisa."""
    semanas = SEMANAS_POR_OMISION
    try:
        with open(dir_base() / 'retencion.json', encoding='utf-8') as f:
            v = json.load(f).get('semanas')
        if isinstance(v, int) and 1 <= v <= 520:
            semanas = v
    except Exception:
        pass
    return {'semanas': semanas, 'borrado_automatico': False}


def guardar_retencion(semanas: int) -> dict:
    if not isinstance(semanas, int) or not (1 <= semanas <= 520):
        raise ValueError('La retención va de 1 a 520 semanas.')
    base = dir_base()
    base.mkdir(parents=True, exist_ok=True)
    with open(base / 'retencion.json', 'w', encoding='utf-8') as f:
        json.dump({'semanas': semanas, 'borrado_automatico': False}, f)
    return retencion()


def almacen(reportes=None) -> dict:
    """Cuánto ocupa el archivo y qué se pasó de la retención.

    `por_vencer` son las semanas más viejas que el límite. NO se tocan: se
    enseñan en el panel para que Christián decida. Si algún día se borra, se
    borra porque él le picó a un botón.
    """
    reportes = listar() if reportes is None else reportes
    limite = retencion()['semanas']
    por_vencer = [r['semana'] for r in reportes[limite:]]
    return {
        'semanas': len(reportes),
        'bytes': sum(r.get('tamano_bytes') or 0 for r in reportes),
        'por_vencer': por_vencer,
        # Con lo que pesa hoy en promedio, cuánto va a ocupar un año completo.
        'proyeccion_anual_bytes': round(
            (sum(r.get('tamano_bytes') or 0 for r in reportes) / len(reportes)) * 52
        ) if reportes else 0,
    }


# ---------------------------------------------------------------- publicar

def publicar(semana: str, datos: dict, video: bytes | None = None,
             texto: str | None = None, video_desde: str | None = None) -> dict:
    """Deposita el reporte de una semana en el archivo. Idempotente: volver a
    publicar la misma semana la reemplaza (así una corrida repetida no duplica).

    `video` son los bytes; `video_desde` es una ruta local, para el uso desde la
    Mac sin subir 15 MB por HTTP. Los dos son opcionales: un reporte sin video
    sigue sirviendo, porque lo que se compara son las cifras.
    """
    if not _valida(semana):
        raise ValueError('La semana va en formato 2026-W31.')
    c = carpeta(semana)
    c.mkdir(parents=True, exist_ok=True)

    if video is not None:
        (c / 'video.mp4').write_bytes(video)
    elif video_desde:
        origen = Path(video_desde)
        if not origen.is_file():
            raise ValueError(f'No encuentro el video: {video_desde}')
        shutil.copyfile(origen, c / 'video.mp4')

    if texto is not None:
        (c / 'resumen.md').write_text(texto, encoding='utf-8')

    cifras = {k: (datos.get('cifras') or {}).get(k) for k in CIFRAS}
    completo = {
        'semana': semana,
        'desde': (datos.get('desde') or '')[:10],
        'hasta': (datos.get('hasta') or '')[:10],
        'titulo': (datos.get('titulo') or '')[:200],
        'resumen': (datos.get('resumen') or '')[:400],
        'duracion_seg': round(float(datos.get('duracion_seg') or 0), 1),
        'creado': datos.get('creado') or datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'cifras': cifras,
    }
    with open(c / 'datos.json', 'w', encoding='utf-8') as f:
        json.dump(completo, f, ensure_ascii=False, indent=2)
    return uno(semana)


def borrar(semana: str) -> bool:
    """Borrado MANUAL de una semana. Nunca lo llama un temporizador: sólo el
    admin desde el panel, y sólo después de ver el aviso."""
    c = carpeta(semana)
    if c is None or not c.is_dir():
        return False
    shutil.rmtree(c)
    return True
