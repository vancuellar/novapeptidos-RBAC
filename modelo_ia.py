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
system prompt y el mensaje, devolver trozos de texto— y debajo caben cuatro
motores:

  · `gemini`  (Google)      — el de hoy. Lo sirve `ai_assistant._gemini`.
  · `openai`  (GPT)         — implementado, se enciende con llave.
  · `kimi`    (Moonshot)    — implementado, se enciende con llave.
  · `claude`  (Anthropic)   — implementado, se enciende con llave.

⛔ NACE SIN CAMBIAR NADA. Sin `AI_PROVIDER` el motor es Gemini y el sitio se
comporta exactamente igual que antes de este archivo. Encenderlo es:

  1. pegar la llave (`OPENAI_API_KEY`, `MOONSHOT_API_KEY` o `ANTHROPIC_API_KEY`)
     en Admin → Cobros, o en el `.env` del servidor — el entorno manda, como en
     `secretos.py`;
  2. poner `AI_PROVIDER=openai` (o `kimi`, o `claude`).

DOS FALLAS QUE NO SE PARECEN EN NADA
------------------------------------
Y por eso se tratan distinto, que es la decisión de diseño de este archivo:

  · **Falta la llave o el nombre del modelo** = alguien se equivocó al desplegar.
    Truena (`FaltaConfiguracion`) y dice qué pegar. NO se cambia solo a otro motor:
    un cambio silencioso movería el precio por consulta y la voz del asistente sin
    que nadie se entere, y el error nunca se arreglaría porque nadie lo vería.

  · **El proveedor falló** (cuota agotada, 500, se cayó la red, tardó de más) = no
    es culpa de nadie y va a volver a pasar. Ahí SÍ se cae de vuelta al motor de
    respaldo (`AI_PROVIDER_FALLBACK`, por omisión `gemini`) antes que dejar al
    cliente hablándole a una pared. Ver `cadena()` y `ai_assistant.stream_reply`.

⚠️ HONESTIDAD SOBRE LO NO PROBADO: los conectores de OpenAI, Moonshot y Anthropic
siguen la forma documentada de sus APIs de streaming (SSE) —los tres son
compatibles con el formato de OpenAI salvo Anthropic, que trae el suyo— pero NO se
han corrido contra la API de verdad porque no hay cuenta ni llave. Mismo estado que
`enviosinternacionales.py`: la forma está, la llamada real falta. El día que haya
llave, la primera consulta es la prueba.
"""

import json
import os

import httpx

import secretos

# El motor, si alguien lo eligió a mano. VACÍO = lo elige `proveedor()` solo, con
# las llaves que haya (ver ORDEN_PROVEEDOR_AUTO). Antes decía 'gemini' aquí, y eso
# amarraba el chat al plan gratis aunque hubiera llaves de pago pegadas en el panel.
PROVEEDOR = (os.environ.get('AI_PROVIDER') or '').strip().lower()

# A dónde se cae si el motor de arriba falla EN CALIENTE (no si le falta la llave).
# `ninguno` lo apaga. Por omisión Gemini, que es el que ya está pagado y probado.
RESPALDO = (os.environ.get('AI_PROVIDER_FALLBACK') or 'gemini').strip().lower()

# Qué modelo usa cada motor si nadie dice otra cosa. Se pisa SIN TOCAR CÓDIGO con
# `AI_MODEL_NAME_<MOTOR>` (p.ej. `AI_MODEL_NAME_OPENAI`), o con `AI_MODEL_NAME` a
# secas, que sólo aplica al motor elegido.
#
# ⛔ POR QUÉ HAY UNO POR MOTOR Y NO UNA SOLA VARIABLE. Con `AI_MODEL_NAME` a secas
# y el respaldo encendido, caerse de GPT a Gemini le mandaría a Google el nombre de
# un modelo de OpenAI: el respaldo moriría con un 404 justo el día que hace falta.
#
# ⚠️ EL DE OPENAI VA VACÍO A PROPÓSITO. Los nombres de modelo de OpenAI cambian
# seguido; inventar uno haría que la primera consulta muriera con un 404 confuso
# en vez de decir qué falta. Cuando Christián abra la cuenta, el nombre exacto se
# pega en AI_MODEL_NAME_OPENAI.
MODELO_POR_OMISION = {
    # El que corre hoy en vivo. Es el mismo valor que tenía cableado
    # `ai_assistant.AI_MODEL_NAME`: si aquí se pusiera otro, cambiaría el motor
    # de producción sin que nadie lo pidiera.
    'gemini': 'gemini-3.5-flash',
    'openai': '',
    # Moonshot. `kimi-latest` es el alias que la propia Moonshot mantiene apuntando
    # a su modelo vigente, así que no caduca con el nombre de la generación.
    'kimi': 'kimi-latest',
    # El bueno de Anthropic ($5 / $25 por millón de tokens). Si el gasto asusta,
    # `AI_MODEL_NAME_CLAUDE=claude-haiku-4-5` es el barato ($1 / $5) — pero esa es
    # decisión de Christián, no un recorte que se hace solo por omisión.
    'claude': 'claude-opus-5',
}

# Qué llave necesita cada motor. Una sola tabla: la usan `encendido()`, los
# conectores y el aviso de qué falta.
LLAVE_DE = {
    'gemini': 'GEMINI_API_KEY',
    'openai': 'OPENAI_API_KEY',
    'kimi': 'MOONSHOT_API_KEY',
    'claude': 'ANTHROPIC_API_KEY',
}

# Los que hablan el formato de OpenAI (`/chat/completions` con SSE). Moonshot lo
# implementa a propósito para que cambiar de uno a otro sea cambiar la URL.
# La URL se puede pisar con `<MOTOR>_BASE_URL` por si mueven el dominio o hay que
# apuntar al endpoint de China (api.moonshot.cn) en vez del global.
BASE_URL = {
    'openai': 'https://api.openai.com/v1',
    'kimi': 'https://api.moonshot.ai/v1',
}

# Cuánto esperamos a que el modelo empiece a hablar. Es un chat en vivo: más de
# esto y el distribuidor ya cerró la pestaña.
TIMEOUT = 60


class FaltaConfiguracion(RuntimeError):
    """Falta la llave o el nombre del modelo: es un error de despliegue.

    ⛔ Se distingue de un fallo del proveedor A PROPÓSITO, y por eso es una clase
    y no un `RuntimeError` cualquiera: `ai_assistant.stream_reply` NO se cae al
    respaldo con esto. Si lo hiciera, un `.env` mal pegado se taparía solo y el
    chat correría meses en el motor equivocado sin que nadie se entere.
    """


# El orden para elegir motor cuando NADIE puso `AI_PROVIDER`. Gemini va AL FINAL
# (Christián, 2026-08-03: «quita lo de Gemini»): su plan gratis son 20 consultas
# al día, así que el chat amanecía bien y para la tarde decía «se acabó la cuota».
# Un motor de pago por uso cuesta centavos y no se apaga a media jornada.
ORDEN_PROVEEDOR_AUTO = ('kimi', 'openai', 'claude', 'gemini')


def proveedor() -> str:
    """El motor de casa. `AI_PROVIDER` manda; si nadie lo puso, se elige solo.

    ⛔ POR QUÉ SE ELIGE SOLO. Antes esto devolvía `gemini` a secas, y `AI_PROVIDER`
    únicamente se puede poner en el `.env` del servidor — o sea que pegar una llave
    en el panel NO cambiaba de motor y el chat seguía amarrado al plan gratis que
    se agota. Ahora basta con pegar la llave: se toma el primer motor de la lista
    que tenga llave Y nombre de modelo.
    """
    if PROVEEDOR in MODELO_POR_OMISION:
        return PROVEEDOR
    for cual in ORDEN_PROVEEDOR_AUTO:
        if _tiene_modelo(cual) and encendido(cual):
            return cual
    return 'gemini'


def _tiene_modelo(cual: str) -> bool:
    """¿Este motor sabe a qué modelo pegarle? Mira su variable propia y su
    nombre por omisión — nunca `modelo()`, que preguntaría por el proveedor y
    haría recursión infinita con la función de arriba."""
    return bool(os.environ.get(f'AI_MODEL_NAME_{cual.upper()}')
                or MODELO_POR_OMISION.get(cual))


def modelo(cual: str = None) -> str:
    """El modelo de ESTE motor. Ver el comentario de `MODELO_POR_OMISION`."""
    cual = cual or proveedor()
    propio = os.environ.get(f'AI_MODEL_NAME_{cual.upper()}')
    if propio:
        return propio
    # `AI_MODEL_NAME` a secas es la variable vieja: sólo vale para el motor
    # elegido, nunca para el respaldo.
    if cual == proveedor() and os.environ.get('AI_MODEL_NAME'):
        return os.environ['AI_MODEL_NAME']
    return MODELO_POR_OMISION.get(cual, '')


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
    nombre = LLAVE_DE.get(cual)
    return bool(nombre and llave(nombre))


# El orden en que se busca un respaldo AUTOMÁTICO cuando nadie lo eligió a mano.
#
# ⛔ DE MÁS BARATO A MÁS CARO (Christián, 2026-08-03: «que el más barato responda
# primero»). El respaldo entra justo cuando el motor de casa se agotó, o sea el
# día de más tráfico: si ahí salta al más caro, el pico de demanda es también el
# pico de la factura. Kimi cuesta centavos por consulta, GPT bastante más y
# Claude es el más caro de los tres — así que se prueban en ese orden.
#
# Un motor sin nombre de modelo se salta solo (ver el `modelo(cual)` de abajo):
# entrar sin nombre muere con «falta AI_MODEL_NAME» en la primera línea, que es
# peor que no tener respaldo.
ORDEN_RESPALDO_AUTO = ('kimi', 'openai', 'claude', 'gemini')


def respaldo() -> str:
    """El motor de respaldo EFECTIVO, o cadena vacía si no hay.

    Un respaldo sin llave no es un respaldo: se descarta aquí para que la cadena
    no cargue con un motor que va a morir en la primera línea.

    ⛔ POR QUÉ HAY RESPALDO AUTOMÁTICO (Christián, 2026-08-03). El chat seguía
    diciendo «se acabó la cuota» con las llaves de GPT y Claude YA pegadas en el
    panel: `AI_PROVIDER_FALLBACK` venía en `gemini` por omisión y el proveedor
    también era `gemini`, o sea «si falla Gemini, usa Gemini» — un respaldo que
    no existe. Pegar una llave tenía que bastar; que además hubiera que editar
    una variable en el servidor era una trampa silenciosa.

    Ahora: si nadie eligió respaldo a mano, se toma el primer motor ENCENDIDO
    (con llave, del entorno o del panel) distinto del proveedor. Apagarlo sigue
    siendo `AI_PROVIDER_FALLBACK=ninguno`, y elegirlo a mano sigue mandando.
    """
    if RESPALDO in ('ninguno', 'none', 'off', 'no', '0'):
        return ''
    # Elegido a mano: se respeta tal cual (y se descarta si no sirve).
    if RESPALDO and RESPALDO != 'gemini':
        if RESPALDO not in MODELO_POR_OMISION or RESPALDO == proveedor():
            return ''
        return RESPALDO if encendido(RESPALDO) else ''
    # Nadie lo eligió (o quedó el `gemini` por omisión): se busca solo.
    if RESPALDO == 'gemini' and RESPALDO != proveedor() and encendido('gemini'):
        return 'gemini'
    for cual in ORDEN_RESPALDO_AUTO:
        # `modelo(cual)` mira también `AI_MODEL_NAME_<MOTOR>`: un motor sin nombre
        # por omisión SÍ entra si Christián ya le puso el suyo.
        if cual != proveedor() and _tiene_modelo(cual) and encendido(cual):
            return cual
    return ''


def cadena() -> list:
    """Los motores a intentar, en orden: el elegido y —si lo hay— su respaldo.

    Sin configurar nada esto devuelve `['gemini']`, exactamente el chat de hoy.
    """
    puestos = [proveedor()]
    de_atras = respaldo()
    if de_atras:
        puestos.append(de_atras)
    return puestos


# ---------------------------------------------------------------------------
#  Avisos al usuario cuando el motor no contesta
# ---------------------------------------------------------------------------
#
# ⛔ VAN EN LOS TRES IDIOMAS y SIN NOMBRAR AL PROVEEDOR. Antes decían "el plan
# gratuito de Google da 20 consultas al día", en español y nada más: el día que el
# motor deje de ser Google el mensaje pasa a ser mentira, y a un cliente en Brasil
# le sale en un idioma que no eligió. El nombre del proveedor es un detalle nuestro
# que al cliente no le sirve para nada.

AVISOS = {
    ('tienda', 'saturado'): {
        'es': ('Nuestro asistente está recibiendo mucha demanda en este momento. '
               'Intenta de nuevo en unos minutos o escríbenos a hola@exygenlabs.com '
               'y con gusto te ayudamos.'),
        'en': ('Our assistant is handling a lot of demand right now. Please try '
               'again in a few minutes, or write to us at hola@exygenlabs.com and '
               'we will gladly help you.'),
        'pt': ('Nosso assistente está recebendo muita demanda neste momento. Tente '
               'novamente em alguns minutos ou escreva para hola@exygenlabs.com que '
               'teremos prazer em ajudar.'),
    },
    ('tienda', 'generico'): {
        'es': 'Lo siento, ocurrió un error al procesar tu mensaje. Intenta de nuevo.',
        'en': 'Sorry, something went wrong with your message. Please try again.',
        'pt': 'Desculpe, ocorreu um erro ao processar sua mensagem. Tente novamente.',
    },
    ('panel', 'saturado'): {
        'es': ('Se acabó la cuota del asistente por hoy. Vuelve a intentar más '
               'tarde, o avísale a Christián para ampliar el plan.'),
        'en': ('The assistant has run out of quota for today. Try again later, or '
               'let Christián know so he can extend the plan.'),
        'pt': ('A cota do assistente acabou por hoje. Tente novamente mais tarde ou '
               'avise o Christián para ampliar o plano.'),
    },
    ('panel', 'sin_llave'): {
        'es': ('Al asistente todavía le falta su llave en el servidor. Avísale a '
               'Christián.'),
        'en': ('The assistant is still missing its key on the server. Let Christián '
               'know.'),
        'pt': ('O assistente ainda está sem a chave no servidor. Avise o Christián.'),
    },
    ('panel', 'generico'): {
        'es': 'No pude responder en este momento. Intenta de nuevo en un minuto.',
        'en': 'I could not answer right now. Please try again in a minute.',
        'pt': 'Não consegui responder agora. Tente novamente em um minuto.',
    },
}


def clase_de_error(e) -> str:
    """En qué cajón cae este error: `sin_llave`, `saturado` o `generico`."""
    texto = str(e)
    if isinstance(e, FaltaConfiguracion) or 'API_KEY' in texto or 'AI_MODEL_NAME' in texto:
        return 'sin_llave'
    # 429 = cuota agotada. Lo dicen así los cuatro proveedores; Google además
    # manda su propio nombre para lo mismo.
    if '429' in texto or 'RESOURCE_EXHAUSTED' in texto or 'rate_limit' in texto.lower():
        return 'saturado'
    return 'generico'


def aviso(ambito: str, clase: str, language: str = None) -> str:
    """El mensaje que ve el usuario, en su idioma. `ambito` = tienda | panel."""
    codigo = (language or 'es').split('-')[0].lower()
    textos = AVISOS.get((ambito, clase)) or AVISOS[(ambito, 'generico')]
    return textos.get(codigo) or textos['es']


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


async def _compatible_openai(cual: str, system: str, mensaje: str):
    """GPT y Kimi. Los dos hablan `/chat/completions` con `stream: true`.

    Moonshot implementó el formato de OpenAI a propósito para que cambiar de uno a
    otro sea cambiar la URL y la llave, así que aquí es UN solo conector y no dos
    copias que hay que arreglar por separado.
    """
    nombre_llave = LLAVE_DE[cual]
    api = llave(nombre_llave)
    if not api:
        raise FaltaConfiguracion(f'{nombre_llave} no está configurada.')
    if not modelo(cual):
        raise FaltaConfiguracion(
            f'Falta AI_MODEL_NAME_{cual.upper()}: "{cual}" no trae modelo por omisión.')

    base = (os.environ.get(f'{cual.upper()}_BASE_URL') or BASE_URL[cual]).rstrip('/')

    def texto(ev):
        return ((ev.get('choices') or [{}])[0].get('delta') or {}).get('content')

    async for t in _sse(
        f'{base}/chat/completions',
        {'Authorization': f'Bearer {api}', 'Content-Type': 'application/json'},
        {'model': modelo(cual), 'stream': True,
         'messages': [{'role': 'system', 'content': system},
                      {'role': 'user', 'content': mensaje}]},
        texto,
    ):
        yield t


async def _claude(system: str, mensaje: str):
    """Anthropic. El system va en su propio campo, no como un mensaje más."""
    api = llave('ANTHROPIC_API_KEY')
    if not api:
        raise FaltaConfiguracion('ANTHROPIC_API_KEY no está configurada.')

    def texto(ev):
        if ev.get('type') == 'content_block_delta':
            return (ev.get('delta') or {}).get('text')
        return None

    async for t in _sse(
        'https://api.anthropic.com/v1/messages',
        {'x-api-key': api, 'anthropic-version': '2023-06-01',
         'Content-Type': 'application/json'},
        {'model': modelo('claude'), 'stream': True, 'max_tokens': 2048,
         'system': system,
         'messages': [{'role': 'user', 'content': mensaje}]},
        texto,
    ):
        yield t


async def responder(system: str, mensaje: str, cual: str = None):
    """El contrato: system + mensaje -> trozos de texto. Da igual qué motor sea.

    `cual` va explícito para que el respaldo pueda pedir un motor DISTINTO al
    configurado; sin él, el de configuración.

    Gemini no pasa por aquí: lo sirve `ai_assistant._gemini`, que ya trae su
    manejo de filtros de Google. Duplicarlo aquí sería tener dos sitios donde
    arreglar el mismo bicho.
    """
    cual = cual or proveedor()
    if cual in BASE_URL:                    # openai, kimi
        async for t in _compatible_openai(cual, system, mensaje):
            yield t
    elif cual == 'claude':
        async for t in _claude(system, mensaje):
            yield t
    else:                                   # pragma: no cover - lo cubre ai_assistant
        raise RuntimeError(f'El motor "{cual}" no se sirve desde aquí.')
