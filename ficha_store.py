"""Almacén privado de fichas técnicas.

Cómo funciona
-------------
Los PDF viven en disco, fuera de git, en la carpeta que indique FICHA_DIR
(por defecto `/opt/exygen/fichas` en el servidor). No hay índice público ni
carpeta navegable: nada de esto lo alcanza Google ni se puede listar desde
fuera. Una ficha se entrega solo por dos vías:

  1. **Quien compró.** Igual que los COA: se resuelve por `product_slug`
     contra los pedidos pagados del usuario.
  2. **Quien la pide por el chat.** El asistente puede emitir un enlace
     firmado y con caducidad. No hace falta cuenta, pero el enlace muere
     solo y queda registrado quién lo pidió.

El nombre del archivo es la convención: `FICHA-TECNICA-<SLUG>.pdf`, en
mayúsculas. Agregar una ficha nueva = copiar el PDF a FICHA_DIR. No hay
registro que mantener ni código que tocar.

El generador de los PDF vive en `fichas-tecnicas/build_fichas.py` del repo
del sitio.
"""

import hashlib
import hmac
import os
import re
import time
from pathlib import Path

FICHA_DIR = Path(os.environ.get('FICHA_DIR', '/opt/exygen/fichas'))

# Mismos estados de pedido que los COA: el cliente ya pagó.
PAID_STATUSES = ('confirmado', 'enviado', 'entregado')

# Un slug es minúsculas, dígitos y guiones. Sirve para que un nombre mal
# formado no pueda salirse de FICHA_DIR con "../".
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')

# Cuánto vive un enlace emitido por el chat.
ENLACE_HORAS = int(os.environ.get('FICHA_LINK_HORAS', '48'))

_SECRETO = os.environ.get('JWT_SECRET', 'nova-peptides-secret-key-change-me')


def _ruta(slug: str) -> Path | None:
    """Ruta del PDF de un slug. None si no existe o el slug es inválido.

    Se construye el nombre desde el slug validado; nunca se acepta una ruta
    de fuera, así que no hay forma de leer otra carpeta.
    """
    if not slug or not SLUG_RE.match(slug):
        return None
    path = FICHA_DIR / f'FICHA-TECNICA-{slug.upper()}.pdf'
    return path if path.is_file() else None


def existe(slug: str) -> bool:
    return _ruta(slug) is not None


def ruta_de(slug: str) -> Path | None:
    return _ruta(slug)


def nombre_descarga(slug: str) -> str:
    return f'Ficha-Tecnica-{slug}.pdf'


def slugs_disponibles() -> list:
    """Los slugs que hoy tienen ficha en disco. Uso interno, nunca se expone
    completo al navegador: sirve para que el chat sepa qué puede ofrecer."""
    if not FICHA_DIR.is_dir():
        return []
    out = []
    for p in FICHA_DIR.glob('FICHA-TECNICA-*.pdf'):
        slug = p.stem.replace('FICHA-TECNICA-', '', 1).lower()
        if SLUG_RE.match(slug):
            out.append(slug)
    return sorted(out)


# En la base cada PRESENTACIÓN es su propio producto ("retatrutida-40-mg"),
# pero la ficha es del COMPUESTO ("retatrutida"). Estos son los sufijos de
# presentación que hay que recortar para emparejarlos.
_UNIDAD = r'(?:mg|mcg|g|ml|iu|ui)'
_SUFIJOS = (
    re.compile(rf'-\d+-{_UNIDAD}$'),        # retatrutida-40-mg
    re.compile(rf'-\d+-\d+-{_UNIDAD}$'),    # retatrutida-2-5-mg (el decimal)
    re.compile(rf'-\d+{_UNIDAD}$'),         # ...-60mg, con la unidad pegada
)


def compuesto_de(slug: str, disponibles=None) -> str | None:
    """La ficha que le toca a un slug del catálogo. None si no hay ninguna.

    Sin este recorte no emparejaba NI UNO: los 193 productos del catálogo
    daban cero fichas, porque ninguno se llama igual que su compuesto. Se
    descubrió el 2026-07-29 probando en vivo con el cliente que sí compró.

    Se prueba del recorte más corto al más largo y gana el primero que TENGA
    ficha. El orden importa porque hay compuestos cuyo nombre lleva número:
    `thymosin-alpha-1` y `snap-8` no se pueden recortar hasta `thymosin-alpha`
    ni `snap`. Como se exige que el resultado exista en disco, un recorte de
    más nunca puede entregar la ficha de otro compuesto.
    """
    if not slug or not isinstance(slug, str):
        return None
    disp = set(slugs_disponibles() if disponibles is None else disponibles)
    if slug in disp:
        return slug
    for rx in _SUFIJOS:
        base = rx.sub('', slug)
        if base != slug and base in disp:
            return base
    return None


def para_slugs(slugs) -> list:
    """Fichas que le corresponden a una lista de slugs comprados."""
    disponibles = set(slugs_disponibles())
    encontradas = {compuesto_de(s, disponibles) for s in slugs if s}
    encontradas.discard(None)
    return [{'product_slug': s, 'nombre_archivo': nombre_descarga(s)}
            for s in sorted(encontradas)]


# ----------------------------------------------------------- enlaces firmados

def emitir_enlace(slug: str, horas: int = None) -> str | None:
    """Token firmado para descargar una ficha sin tener cuenta.

    Formato: `<slug>.<expira>.<firma>`. La firma es HMAC-SHA256 sobre
    "slug.expira" con el secreto de la app, así que el token no se puede
    fabricar ni alargar desde fuera.
    """
    if not existe(slug):
        return None
    # OJO: `horas or ENLACE_HORAS` convertiria un 0 legitimo en 48.
    ventana = ENLACE_HORAS if horas is None else horas
    expira = int(time.time()) + int(ventana * 3600)
    base = f'{slug}.{expira}'
    firma = hmac.new(_SECRETO.encode(), base.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{base}.{firma}'


def validar_enlace(token: str) -> str | None:
    """Devuelve el slug si el token es válido y no caducó; None si no."""
    if not token or token.count('.') != 2:
        return None
    slug, expira, firma = token.split('.')
    if not SLUG_RE.match(slug):
        return None
    try:
        if int(expira) <= int(time.time()):
            return None
    except ValueError:
        return None
    esperada = hmac.new(_SECRETO.encode(), f'{slug}.{expira}'.encode(),
                        hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(firma, esperada):
        return None
    return slug if existe(slug) else None
