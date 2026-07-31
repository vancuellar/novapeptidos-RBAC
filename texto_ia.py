"""LIMPIEZA DEL TEXTO QUE ESCRIBE EL MODELO — la red de abajo.

⛔ EL PROBLEMA (Christián, 2026-07-31): «las respuestas de la AI no están
limpias, dejan código, ejemplo `**NAD+ 500**`». El modelo contesta en Markdown y
la pantalla lo pintaba crudo, así que el cliente veía los asteriscos.

El arreglo de verdad es de tres capas y ésta es la tercera:
  1. El PROMPT le pide prosa limpia y viñetas simples (sin `##`, sin tablas de
     tubería, sin bloques de código). Ver `ai_assistant.SYSTEM_PROMPT` y
     `chat_negocio.PROMPT_BASE`.
  2. La PANTALLA pinta el Markdown de verdad (`RespuestaIA.js` en la UI): las
     negritas salen negritas y las viñetas salen viñetas.
  3. ESTO: lo que se le escapa al modelo y la pantalla no necesita, se va antes
     de salir del servidor. Sirve además para lo que ya está guardado en la base
     y para cualquier otro consumidor que no pinte Markdown.

QUÉ SE VA Y QUÉ SE QUEDA (a propósito):
  · SE VA: las almohadillas de título, los acentos graves, los bloques de
    código, los asteriscos HUÉRFANOS (los que no tienen pareja) y las viñetas
    con asterisco, que se vuelven guion.
  · SE QUEDA: las negritas bien cerradas (`**así**`), las listas y las tablas.
    La pantalla ya las pinta bonito, y borrarlas sería tirar el contenido para
    esconder un símbolo. La orden fue limpiar, no destruir.

⚠️ EL CHORRITO (streaming). La respuesta llega en pedazos y un `**` puede venir
partido entre dos: si se limpiara pedazo por pedazo, medio marcador se colaría a
la pantalla como basura. Por eso está `LimpiezaEnVivo`, que suelta sólo hasta el
último punto SEGURO y se guarda el resto hasta que llegue lo que falta. Es el
mismo cuidado que hay que tener al partir un carácter UTF-8 en dos.
"""

import re

# Marcadores que, pegados al final de lo que llegó, todavía pueden crecer:
# `*` puede volverse `**`, y un acento grave puede estar abriendo un bloque.
_MARCAS = '*`_~'

# Un renglón que EMPIEZA con esto define el tipo de bloque entero (título,
# tabla, cita, bloque de código): se espera al salto de línea antes de soltarlo.
_ARRANQUES = re.compile(r'^\s{0,3}[#`|>]')

_CERCA = re.compile(r'^\s{0,3}```')


def _sin_huerfanos(texto: str) -> str:
    """Quita el asterisco que se quedó sin pareja, y sólo ése.

    `**NAD+ 500**` se queda (la pantalla lo pinta en negrita). `**NAD+ 500` a
    secas pierde los asteriscos: no abre nada que se cierre.
    """
    while True:
        rachas = list(re.finditer(r'\*+', texto))
        if len(rachas) % 2 == 0:
            return texto
        ultima = rachas[-1]
        texto = texto[:ultima.start()] + texto[ultima.end():]


def limpiar_linea(linea: str, inicio: bool = True) -> str:
    """Un renglón. `inicio=False` cuando ya se soltó un pedazo de ese renglón:
    las reglas de principio de línea (título, viñeta) ya se aplicaron."""
    if inicio:
        linea = re.sub(r'^(\s{0,3})#{1,6}\s*', r'\1', linea)      # titulos: fuera almohadillas
        linea = re.sub(r'^(\s*)\*\s+', r'\1- ', linea)            # vineta con asterisco
        linea = re.sub(r'^(\s*)[•·]\s*', r'\1- ', linea)
    linea = linea.replace('`', '')                                # nada de codigo en el chat
    linea = re.sub(r'\*{3,}', '**', linea)                        # ***asi*** -> **asi**
    linea = re.sub(r'\*\*\s*\*\*', '', linea)                     # negrita vacia
    return _sin_huerfanos(linea)


def limpiar(texto: str) -> str:
    """El texto completo, de una. Para lo que no viene en chorrito."""
    limpio = LimpiezaEnVivo()
    return limpio.alimentar(texto or '') + limpio.cerrar()


def _corte_seguro(parcial: str) -> int:
    """Hasta dónde se puede soltar un renglón a medio llegar sin partir una marca."""
    if _ARRANQUES.match(parcial):
        return 0                       # el arranque manda: se espera el renglon entero
    corte = len(parcial)
    while corte > 0 and parcial[corte - 1] in _MARCAS:
        corte -= 1                     # una marca pegada al final todavia puede crecer
    rachas = [m.start() for m in re.finditer(r'\*+', parcial[:corte])]
    if len(rachas) % 2 == 1:
        corte = rachas[-1]             # hay una negrita abierta: se espera a que cierre
    return max(corte, 0)


class LimpiezaEnVivo:
    """Limpia la respuesta según va llegando, sin partir un marcador a la mitad.

    Se alimenta con cada trozo y devuelve lo que YA es seguro mandar. Al final,
    `cerrar()` suelta lo que quedó guardado.
    """

    def __init__(self):
        self._cola = ''            # el renglon a medio llegar
        self._soltado = False      # ¿ya se solto un pedazo de este renglon?
        self._en_bloque = False    # dentro de un bloque de codigo (```)

    def _renglon(self, linea: str) -> str:
        """Un renglón completo, ya con su salto. Devuelve lo que hay que mandar."""
        if _CERCA.match(linea):
            self._en_bloque = not self._en_bloque
            return ''                          # la cerca se va; el contenido se queda
        if self._en_bloque:
            return linea + '\n'                # dentro del bloque, tal cual (sin la cerca)
        return limpiar_linea(linea, inicio=not self._soltado) + '\n'

    def alimentar(self, trozo: str) -> str:
        self._cola += trozo or ''
        salida = []
        while '\n' in self._cola:
            linea, self._cola = self._cola.split('\n', 1)
            salida.append(self._renglon(linea))
            self._soltado = False
        if self._cola and not self._en_bloque:
            corte = _corte_seguro(self._cola)
            if corte:
                trocito = limpiar_linea(self._cola[:corte], inicio=not self._soltado)
                self._cola = self._cola[corte:]
                if trocito:
                    self._soltado = True
                    salida.append(trocito)
        return ''.join(salida)

    def cerrar(self) -> str:
        """Lo que quedó pendiente, ya sin esperar nada más."""
        resto, self._cola = self._cola, ''
        if not resto:
            return ''
        if self._en_bloque:
            return resto
        limpio = limpiar_linea(resto, inicio=not self._soltado)
        self._soltado = True
        return limpio
