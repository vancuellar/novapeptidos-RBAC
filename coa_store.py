"""Almacén de certificados de análisis (COA).

Cómo funciona
-------------
Los PDF viven en disco, fuera de git, en la carpeta que indique COA_DIR
(por defecto `/opt/exygen/coa` en el servidor, `./coa_files` en local).
Un archivo `registry.json` en esa misma carpeta mapea cada lote con el
producto al que pertenece:

    {
      "lots": [
        {
          "lot": "EX-BPC5-2601",
          "product_slug": "bpc-157",
          "product_name": "BPC-157",
          "presentation": "5 mg",
          "purity": "99.4%",
          "analyzed_at": "2026-01-15",
          "file": "EX-BPC5-2601.pdf",
          "public": false
        }
      ]
    }

Reglas de acceso:
  - `public: true` marca el ÚNICO COA de muestra que se enseña sin comprar.
    Christian elige cuál. Si hay varios marcados, se usa el primero.
  - El resto SOLO lo ve quien compró ese producto: el acceso se resuelve por
    `product_slug` contra los pedidos pagados del usuario.

Agregar un COA nuevo = copiar el PDF a COA_DIR y añadir su entrada al
registry.json. No hay que tocar código ni volver a desplegar.
"""

import json
import os
import re
from pathlib import Path

COA_DIR = Path(os.environ.get('COA_DIR', '/opt/exygen/coa'))
REGISTRY_NAME = 'registry.json'

# Estados de pedido en los que se considera que el cliente ya pagó.
PAID_STATUSES = ('confirmado', 'enviado', 'entregado')

# Un lote es alfanumérico con guiones. Sirve para no dejar pasar rutas ("../").
LOT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


def _registry_path() -> Path:
    return COA_DIR / REGISTRY_NAME


def load_registry() -> list:
    """Lee el registro. Devuelve [] si aún no existe (no truena el sitio)."""
    path = _registry_path()
    if not path.is_file():
        return []
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    lots = data.get('lots') if isinstance(data, dict) else data
    return [x for x in (lots or []) if isinstance(x, dict) and x.get('lot')]


def public_entry() -> dict | None:
    """El COA de muestra que se enseña sin haber comprado. None si no hay."""
    for entry in load_registry():
        if entry.get('public'):
            return _clean(entry)
    return None


def entry_for_lot(lot: str) -> dict | None:
    if not lot or not LOT_RE.match(lot):
        return None
    for entry in load_registry():
        if entry.get('lot') == lot:
            return entry
    return None


def entries_for_slugs(slugs) -> list:
    """COAs de los productos que trae la lista de slugs."""
    wanted = {s for s in slugs if s}
    return [_clean(e) for e in load_registry() if e.get('product_slug') in wanted]


def file_path_for(entry: dict) -> Path | None:
    """Ruta del PDF en disco. None si el registro apunta a un archivo ausente.

    Se ignora cualquier ruta en `file`: solo se usa el nombre, y siempre
    dentro de COA_DIR. Así una entrada mal escrita no puede leer otra carpeta.
    """
    name = os.path.basename((entry or {}).get('file') or '')
    if not name:
        return None
    path = COA_DIR / name
    return path if path.is_file() else None


def _clean(entry: dict) -> dict:
    """Lo que se le manda al navegador: nunca la ruta del archivo."""
    return {
        'lot': entry.get('lot'),
        'product_slug': entry.get('product_slug'),
        'product_name': entry.get('product_name') or entry.get('product_slug'),
        'presentation': entry.get('presentation') or '',
        'purity': entry.get('purity') or '',
        'analyzed_at': entry.get('analyzed_at') or '',
        'method': entry.get('method') or 'HPLC + MS',
        'public': bool(entry.get('public')),
    }
