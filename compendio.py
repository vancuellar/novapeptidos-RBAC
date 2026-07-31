"""EL COMPENDIO — lo que Exygen ya publica sobre cada compuesto, para el chat interno.

Por qué existe
--------------
El "Asesor de Negocio" del panel no contestaba nada técnico y el diagnóstico fácil
era culpar al prompt. Sólo la mitad: el backend **no tenía el contenido**. La
colección `products` de Mongo trae precio, existencia y presentación, y punto —
comprobado contra la API en vivo: 191 productos, CERO con `start_dose`. Las
monografías, las dosis de referencia y las guías de /aprende viven en el repo del
sitio, en JavaScript, y nunca cruzaron al servidor.

Así que el asesor tenía dos frenos: uno de permiso (el prompt) y uno de datos
(esto). Quitar sólo el primero lo habría dejado inventando, que es peor.

`compendio.json` lo genera `exportar_compendio.mjs` desde el repo del sitio. Es el
MISMO contenido que lee cualquier visitante en exygenlabs.com: monografía por
compuesto, ficha del producto, las dosis de referencia que ya salen en la
calculadora, y las guías de /aprende. Un asistente interno no puede ser más
restrictivo que la página que abre cualquiera.

Cómo se usa
-----------
No se adjuntan las 95 fichas: se adjunta LO QUE LA PREGUNTA PIDE. Un contexto de
400 KB no cabe en la ventana y, peor, diluye lo que sí importa. `buscar()` empareja
la pregunta contra los nombres del catálogo y devuelve un puñado de entradas.
"""

import json
import re
import unicodedata
from pathlib import Path

RUTA = Path(__file__).with_name('compendio.json')

# Cuántas fichas caben en un contexto. Con 4 monografías largas van ~12 KB, que es
# lo que aguanta bien junto al catálogo entero sin comerse la ventana.
MAX_FICHAS = 4

# Recorte por guía de /aprende. Las guías van de 8 a 24 KB; la respuesta no
# necesita la guía entera, necesita la parte de arriba, que es donde está el
# procedimiento.
MAX_GUIA = 9000

_datos = None


def datos() -> dict:
    """Carga perezosa. Si el archivo no está (despliegue viejo), el chat sigue
    funcionando con catálogo y reglas: se degrada, no se cae."""
    global _datos
    if _datos is None:
        try:
            _datos = json.loads(RUTA.read_text(encoding='utf-8'))
        except Exception:                    # pragma: no cover - defensivo
            _datos = {'compuestos': {}, 'productos': {}, 'guias': {}}
    return _datos


def _plano(t: str) -> str:
    """Minúsculas, sin acentos y con los signos vueltos espacio.

    `BPC-157` y `bpc 157` tienen que empatar, igual que `NAD+` y `nad`.
    """
    t = unicodedata.normalize('NFD', (t or '').lower())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return ' ' + re.sub(r'[^a-z0-9]+', ' ', t).strip() + ' '


# Palabras del nombre que no distinguen a nadie: buscar "acetate" o "mg" no
# encuentra un compuesto, encuentra treinta.
_RUIDO = {'mg', 'mcg', 'ml', 'iu', 'ui', 'acetate', 'acetato', 'blend', 'kit',
          'vial', 'de', 'la', 'el', 'y', 'con', 'sin', 'plus'}


def _terminos(entrada: dict):
    """Con qué palabras se le llama a este compuesto en una pregunta real."""
    salida = set()
    for bruto in (entrada.get('nombre'), entrada.get('slug')):
        p = _plano(bruto).strip()
        if not p:
            continue
        salida.add(p)
        # El nombre sin la presentación: "Retatrutida 20 mg" -> "retatrutida".
        corto = re.sub(r'\b\d+(\s*\d+)*\s*(mg|mcg|ml|iu|ui)\b', '', p).strip()
        if len(corto) >= 3:
            salida.add(corto)
        # La primera palabra, si por sí sola identifica: "tesamorelina", "bpc".
        # Tres letras basta y sobra: media familia se llama así (bpc, nad, ghk,
        # mgf) y con cuatro, "BPC + TB-500 juntos" sólo encontraba el TB-500.
        cabeza = p.split()[0]
        if len(cabeza) >= 3 and cabeza not in _RUIDO:
            salida.add(cabeza)
    return {t for t in salida if len(t) >= 3}


# De cómo pregunta un distribuidor a la categoría del catálogo. Sin esto, "un
# cliente que busca recuperación muscular" no traía UNA ficha: el nombre del
# compuesto no aparece por ningún lado en la pregunta, que es justo el caso en el
# que más falta hace que el asesor proponga algo.
CATEGORIA_POR_TEMA = {
    'perdida-peso': ('bajar de peso', 'perder peso', 'perdida de peso', 'adelgaz',
                     'grasa', 'obesidad', 'apetito', 'metabol', 'glp'),
    'hormona-crecimiento': ('hormona de crecimiento', 'gh ', 'masa muscular',
                            'musculo', 'crecimiento'),
    'recuperacion': ('recupera', 'lesion', 'tendon', 'articulacion', 'rodilla',
                     'cicatriz', 'reparacion', 'inflamacion', 'intestin'),
    # El sueño no tiene categoría propia: DSIP y las orexinas viven en nootrópicos.
    'nootropicos': ('memoria', 'concentracion mental', 'cognitiv', 'cerebro',
                    'nootropic', 'enfoque', 'dormir', 'sueno', 'insomni',
                    'descansar'),
    'estetica': ('piel', 'arruga', 'colageno', 'cabello', 'pelo', 'estetic',
                 'bronce'),
    'sexual-hormonal': ('libido', 'sexual', 'erecc', 'testosterona', 'hormonal'),
    'longevidad': ('longevidad', 'envejec', 'antiedad', 'anti edad', 'energia',
                   'mitocondri'),
    'bioreguladores': ('biorregulador', 'khavinson'),
}


def _por_categoria(texto: str, limite: int) -> list:
    """Fichas de la categoría que describe la pregunta, cuando no se nombró ningún
    compuesto. Van primero las que tienen dosis publicada: son las que dejan
    contestar completo."""
    for categoria, claves in CATEGORIA_POR_TEMA.items():
        if not any(k in texto for k in claves):
            continue
        de_ahi = [e for e in datos().get('productos', {}).values()
                  if e.get('categoria') == categoria]
        de_ahi.sort(key=lambda e: (0 if e.get('dosis') else 1, e.get('nombre') or ''))
        return de_ahi[:limite]
    return []


def buscar(pregunta: str, limite: int = MAX_FICHAS) -> list:
    """Las fichas que la pregunta está pidiendo, de la más específica a la menos.

    Empareja por palabra completa: sin eso, "gh" saldría dentro de "ghk" y la
    respuesta hablaría del compuesto equivocado.
    """
    texto = _plano(pregunta)
    if not texto.strip():
        return []
    hallados = []
    for slug, entrada in datos().get('productos', {}).items():
        mejor = 0
        for termino in _terminos(entrada):
            if re.search(rf'(?<![a-z0-9]){re.escape(termino)}(?![a-z0-9])', texto):
                mejor = max(mejor, len(termino))
        if mejor:
            hallados.append((mejor, slug, entrada))
    hallados.sort(key=lambda x: -x[0])
    if hallados:
        return [e for _, _, e in hallados[:limite]]
    return _por_categoria(texto, limite)


def _por_prefijo(slug: str):
    """La monografía de un producto. El catálogo vive por presentación
    (`bpc-157-10-mg`) y la monografía por compuesto (`bpc-157`), así que se busca
    el prefijo más largo que exista."""
    comp = datos().get('compuestos', {})
    if slug in comp:
        return comp[slug]
    mejor = ''
    for clave in comp:
        if slug.startswith(clave + '-') and len(clave) > len(mejor):
            mejor = clave
    return comp.get(mejor) if mejor else None


# Cada cuándo, en palabras. Es el MISMO diccionario que pinta la calculadora del
# sitio (`FREQ_PHRASES` en ReconstitutionCalculator.js): si aquí se dijera otra
# cosa, el asesor contradiría la pantalla que el cliente tiene enfrente.
FRECUENCIAS = {
    'weekly': '1 vez por semana',
    'daily': '1 vez al dia',
    'daily_2x': '2 veces al dia (manana y noche)',
    '2x_week': '2 veces por semana',
    '3x_week': '3 veces por semana',
    'eod': 'un dia si y un dia no',
    'as_needed': 'solo cuando se necesita',
    'daily_cycle': '1 vez al dia, en ciclos de 10 a 20 dias',
    'mt': '1 vez al dia para empezar; al lograr el tono, 1-2 por semana',
}

FASES = {'inicio': 'inicio', 'mantenimiento': 'mantenimiento', 'carga': 'carga'}


def _renglon_dosis(d: dict) -> list:
    """Los tres niveles de referencia tal como el sitio los enseña.

    ⚠️ Sólo existen si el producto trae `fuente` anotada. Es el mismo interruptor
    de la calculadora: 63 productos se quedaron sin dosis a propósito porque nadie
    los investigó (Christián, 2026-07-26). Si aquí apareciera una cifra que la
    pantalla no muestra, sería inventada.
    """
    unidad = d.get('unidad') or ''
    freq = d.get('freq') or {}
    fase = d.get('fase') or {}
    lineas = ['  Dosis de referencia que publica el sitio (orientativas, RUO):']
    for nivel, etiqueta in (('inicial', 'inicial'), ('tipica', 'tipica'),
                            ('avanzada', 'avanzada')):
        valor = d.get(nivel)
        if valor is None:
            continue
        cola = FRECUENCIAS.get(freq.get(nivel) or d.get('freq_producto') or '', '')
        f = FASES.get(fase.get(nivel) or '', '')
        extra = ' · '.join(x for x in (cola, f'fase de {f}' if f else '') if x)
        lineas.append(f'    - {etiqueta}: {valor} {unidad}'.rstrip()
                      + (f' · {extra}' if extra else ''))
    agua = d.get('agua_ml') or {}
    if agua:
        lineas.append('    - agua sugerida por vial: '
                      + ', '.join(f'{mg} mg -> {ml} mL' for mg, ml in agua.items()))
    lineas.append(f'    - de donde sale: {d.get("fuente")}')
    return lineas


def ficha_texto(entrada: dict, con_monografia: bool = True) -> str:
    """Una ficha completa en texto: qué es, presentación, manejo y dosis."""
    lineas = [f'### {entrada.get("nombre")} ({entrada.get("slug")})']
    if entrada.get('resumen'):
        lineas.append(f'  {entrada["resumen"]}')
    if entrada.get('descripcion'):
        lineas.append(f'  {entrada["descripcion"]}')
    presentaciones = ', '.join(entrada.get('presentaciones') or [])
    ficha = ' · '.join(x for x in (
        f'presentaciones: {presentaciones}' if presentaciones else '',
        f'forma: {entrada.get("forma")}' if entrada.get('forma') else '',
        f'pureza: {entrada.get("pureza")}' if entrada.get('pureza') else '',
    ) if x)
    if ficha:
        lineas.append(f'  {ficha}')
    if entrada.get('conservacion'):
        lineas.append(f'  Conservacion: {entrada["conservacion"]}')
    if entrada.get('dosis'):
        lineas.extend(_renglon_dosis(entrada['dosis']))
    else:
        lineas.append('  Dosis de referencia: NO la publicamos para este producto '
                      '(nadie la investigo con fuente). Dilo asi; no la estimes.')
    if con_monografia:
        mono = _por_prefijo(entrada.get('slug') or '')
        if mono:
            if mono.get('tagline'):
                lineas.append(f'  {mono["tagline"]}')
            for sec in mono.get('secciones') or []:
                lineas.append(f'  [{sec.get("titulo")}]')
                for par in sec.get('parrafos') or []:
                    lineas.append(f'  {par}')
    return '\n'.join(lineas)


# ---------------------------------------------------------------------------
#  Las guías de /aprende
# ---------------------------------------------------------------------------
#
# Qué guía pide cada pregunta. Se empareja por palabra suelta porque la gente
# pregunta como habla: "cuánta agua le pongo", "se me echó a perder".
GUIAS_POR_TEMA = (
    ('reconstitucion-paso-a-paso',
     ('reconstitu', 'agua bacterio', 'cuanta agua', 'diluir', 'dilucion', 'rayita',
      'jeringa', 'unidades', 'mezclar', 'preparar el vial', 'ml', 'mililitro',
      'concentracion', 'calculadora')),
    ('conservacion',
     ('conserv', 'almacen', 'guardar', 'refriger', 'congel', 'temperatura', 'caduc',
      'vida util', 'degrad', 'se echo a perder', 'viaje', 'calor', 'frio')),
    ('protocolos',
     ('protocolo', 'ciclo', 'cuanto tiempo', 'frecuencia', 'combin', 'apilar',
      'stack', 'descanso', 'semanas', 'juntos', 'al mismo tiempo')),
    ('que-significa-99-por-ciento',
     ('pureza', 'hplc', '99', 'purez')),
    ('como-verificamos-cada-lote',
     ('coa', 'certificado', 'lote', 'analisis', 'verific')),
    ('legalidad',
     ('legal', 'aduana', 'import', 'cofepris', 'permiso', 'receta')),
    ('peptidos-explicados',
     ('que es un peptido', 'que son los peptidos', 'liofiliz')),
    ('mitos', ('mito', 'es cierto que', 'verdad que')),
    ('preguntas-frecuentes', ('pregunta frecuente', 'dudas comunes')),
)


def guias_para(pregunta: str, limite: int = 1) -> list:
    """Las guías de /aprende que la pregunta pide (a lo más `limite`)."""
    texto = _plano(pregunta)
    todas = datos().get('guias', {})
    elegidas = []
    for slug, claves in GUIAS_POR_TEMA:
        if slug in todas and any(k in texto for k in claves):
            elegidas.append(todas[slug])
            if len(elegidas) >= limite:
                break
    return elegidas


# La aritmética de la calculadora del sitio, escrita. Es la única parte que no se
# copia de un archivo: son tres fórmulas, y tenerlas explícitas evita que el
# modelo las improvise mal, que es exactamente lo que hace un modelo con
# aritmética de unidades.
CALCULADORA = """COMO SE RECONSTITUYE (la aritmetica EXACTA de la calculadora del sitio):
- Concentracion = mg del vial / mL de agua bacteriostatica. Ejemplo: vial de 10 mg
  con 2 mL de agua = 5 mg/mL.
- Rayitas en jeringa de insulina U-100 (1 mL = 100 rayitas):
  rayitas = (dosis en mg / concentracion en mg/mL) x 100.
  Ejemplo: 0.5 mg de un vial a 5 mg/mL = 0.1 mL = 10 rayitas.
- En mcg: rayitas = (dosis en mcg / 1000 / concentracion) x 100.
- Menos de 2 rayitas no se mide bien: si sale menos, ponle MENOS agua al vial.
- Jeringas comunes en Mexico: U-100 de 1 mL, de 0.5 mL y de 0.3 mL.
Haz la cuenta y ensenala. Es conversion de unidades, no una recomendacion."""
