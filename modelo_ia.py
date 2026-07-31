"""EL MOTOR DEL CHAT — cambiar de modelo tiene que ser pegar una llave.

Por qué existe
-------------
Christián preguntó (2026-07-31) si conviene pasar de Gemini a GPT. La respuesta
honesta a ESE día era "el problema de hoy no es el modelo, es lo que le mandamos"
—y así resultó: los rechazos salían con `finish_reason=STOP` y sin un solo
`safety_rating` disparado, o sea nuestros, del prompt—. Pero la pregunta de fondo
seguía siendo cara de contestar, porque el proveedor estaba cableado dentro de
`ai_assistant.py`: probar otro modelo era reescribir código y desplegar.

Esta capa lo vuelve una decisión de configuración. El contrato es UNO —recibir el
system prompt y el mensaje, devolver trozos de texto— y debajo caben tres motores:

  · `gemini`  (Google)      — el de hoy, el que está encendido.
  · `openai`  (GPT)         — implementado, APAGADO por falta de llave.
  · `claude`  (Anthropic)   — implementado, APAGADO por falta de llave.

⛔ NACE SIN CAMBIAR NADA. Sin `AI_PROVIDER` el motor es Gemini y el sitio se
comporta exactamente igual que antes de este archivo. Encenderlo es:

  1. pegar la llave (`OPENAI_API_KEY` o `ANTHROPIC_API_KEY`) en Admin → Cobros,
     o en el `.env` del servidor — el entorno manda, como en `secretos.py`;
  2. poner `AI_PROVIDER=openai` (o `claude`).

Si se pide un proveedor sin llave, NO se cae en silencio ni se cambia solo a otro:
truena con un mensaje que dice qué falta. Un cambio de motor silencioso es peor
que un error — cambiaría el precio por consulta y la voz del asistente sin que
nadie se entere.

⚠️ HONESTIDAD SOBRE LO NO PROBADO: los conectores de OpenAI y Anthropic siguen la
forma documentada de sus APIs de streaming (SSE), pero NO se han corrido contra la
API de verdad porque no hay cuenta ni llave. Mismo estado que
`enviosinternacionales.py`: la forma está, la llamada real falta. El día que haya
llave, la primera consulta es la prueba.
"""

import json
import os

import httpx

import secretos

# El motor. Vacío o desconocido = Gemini, que es el que está pagado y probado.
PROVEEDOR = (os.environ.get('AI_PROVIDER') or 'gemini').strip().lower()

# Qué modelo usa cada motor si nadie dice otra cosa. Se pisa con AI_MODEL_NAME
# sin tocar código.
#
# ⚠️ EL DE OPENAI VA VACÍO A PROPÓSITO. Los nombres de modelo de OpenAI cambian
# seguido y no se verificaron contra su catálogo; inventar uno haría que la
# primera consulta muriera con un 404 confuso en vez de decir qué falta. Cuando
# Christián abra la cuenta, el nombre exacto se pega en AI_MODEL_NAME.
MODELO_POR_OMISION = {
    'gemini': 'gemini-flash-latest',
    'openai': '',
    # El bueno de Anthropic ($5 / $25 por millón de tokens). Si el gasto asusta,
    # `AI_MODEL_NAME=claude-haiku-4-5` es el barato ($1 / $5) — pero esa es
    # decisión de Christián, no un recorte que se hace solo por omisión.
    'claude': 'claude-opus-5',
}

# Cuánto esperamos a que el modelo empiece a hablar. Es un chat en vivo: más de
# esto y el distribuidor ya cerró la pestaña.
TIMEOUT = 60


def proveedor() -> str:
    return PROVEEDOR if PROVEEDOR in MODELO_POR_OMISION else 'gemini'


def modelo() -> str:
    return os.environ.get('AI_MODEL_NAME') or MODELO_POR_OMISION[proveedor()]


def llave(nombre: str) -> str:
    """La llave, del entorno o del panel. El entorno manda (ver `secretos.py`)."""
    try:
        return secretos.valor(nombre)
    except Exception:                       # pragma: no cover - defensivo
        return os.environ.get(nombre, '')


def encendido(cual: str = None) -> bool:
    """¿Hay con qué hablarle a este motor? Es el `enabled()` de siempre."""
    cual = cual or proveedor()
    if cual == 'gemini':
        return bool(llave('GEMINI_API_KEY') or llave('GOOGLE_API_KEY'))
    if cual == 'openai':
        return bool(llave('OPENAI_API_KEY'))
    if cual == 'claude':
        return bool(llave('ANTHROPIC_API_KEY'))
    return False


# ---------------------------------------------------------------------------
#  Los conectores
# ---------------------------------------------------------------------------

async def _sse(url, headers, cuerpo, sacar_texto):
    """El esqueleto común de OpenAI y Anthropic: los dos hablan SSE.

    `sacar_texto` recibe el JSON de un evento y devuelve el trozo de texto, o
    None si ese evento no trae texto (arranques, usos de tokens, cierres).
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
        async with cliente.stream('POST', url, headers=headers, json=cuerpo) as r:
            if r.status_code >= 400:
                detalle = (await r.aread()).decode('utf-8', 'replace')[:400]
                raise RuntimeError(f'{r.status_code} {detalle}')
            async for linea in r.aiter_lines():
                if not linea.startswith('data:'):
                    continue
                crudo = linea[5:].strip()
                if not crudo or crudo == '[DONE]':
                    continue
                try:
                    trozo = sacar_texto(json.loads(crudo))
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if trozo:
                    yield trozo


async def _openai(system: str, mensaje: str):
    """GPT. `stream: true` sobre /v1/chat/completions."""
    api = llave('OPENAI_API_KEY')
    if not api:
        raise RuntimeError('OPENAI_API_KEY no está configurada.')
    if not modelo():
        raise RuntimeError('Falta AI_MODEL_NAME: OpenAI no trae modelo por omisión.')

    def texto(ev):
        return ((ev.get('choices') or [{}])[0].get('delta') or {}).get('content')

    async for t in _sse(
        'https://api.openai.com/v1/chat/completions',
        {'Authorization': f'Bearer {api}', 'Content-Type': 'application/json'},
        {'model': modelo(), 'stream': True,
         'messages': [{'role': 'system', 'content': system},
                      {'role': 'user', 'content': mensaje}]},
        texto,
    ):
        yield t


async def _claude(system: str, mensaje: str):
    """Anthropic. El system va en su propio campo, no como un mensaje más."""
    api = llave('ANTHROPIC_API_KEY')
    if not api:
        raise RuntimeError('ANTHROPIC_API_KEY no está configurada.')

    def texto(ev):
        if ev.get('type') == 'content_block_delta':
            return (ev.get('delta') or {}).get('text')
        return None

    async for t in _sse(
        'https://api.anthropic.com/v1/messages',
        {'x-api-key': api, 'anthropic-version': '2023-06-01',
         'Content-Type': 'application/json'},
        {'model': modelo(), 'stream': True, 'max_tokens': 2048, 'system': system,
         'messages': [{'role': 'user', 'content': mensaje}]},
        texto,
    ):
        yield t


async def responder(system: str, mensaje: str):
    """El contrato: system + mensaje -> trozos de texto. Da igual qué motor sea.

    Gemini no pasa por aquí: lo sirve `ai_assistant.stream_reply`, que ya trae su
    manejo de filtros y su rechazo de respaldo. Duplicarlo aquí sería tener dos
    sitios donde arreglar el mismo bicho.
    """
    cual = proveedor()
    if cual == 'openai':
        async for t in _openai(system, mensaje):
            yield t
    elif cual == 'claude':
        async for t in _claude(system, mensaje):
            yield t
    else:                                   # pragma: no cover - lo cubre ai_assistant
        raise RuntimeError(f'El motor "{cual}" no se sirve desde aquí.')
