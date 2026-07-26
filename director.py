"""Director de marketing: arma una campaña nueva desde cero.

La idea de Christian: un botón que se pone en modo "director de marketing" y
construye una campaña completa con lo que ya se aprendió de las campañas
anteriores, de los productos y de los clientes reales.

La regla que hace que esto sirva y no sea un generador de texto bonito:
**la IA no inventa los datos, solo los interpreta.** `briefing()` es una función
pura que arma los hechos a partir de la base —qué se vendió, a qué precio, qué
campaña ganó y cuál perdió, cuánto costó cada cliente— y eso es lo único que ve
el modelo. Si no hay datos, el briefing lo dice y la propuesta sale marcada como
"a ciegas" en vez de fingir seguridad.

Por eso `briefing()` está separada y probada: es la parte que puede estar mal de
forma silenciosa. Lo que devuelve el modelo es una propuesta para que Christian
apruebe, no algo que se publique solo.
"""
import json
import os

from marketing import slug, campana_del_pedido

# Sin al menos esto, cualquier lectura del pasado es ruido y hay que decirlo.
MIN_PEDIDOS_PARA_APRENDER = 5
MIN_CAMPANAS_PARA_APRENDER = 2


def _top(d, n, clave):
    return sorted(d.values(), key=lambda x: -x[clave])[:n]


def briefing(campanas=None, pedidos=None, productos=None, fx=18.0):
    """Los HECHOS con los que se va a armar la campaña. Función pura.

    Devuelve un dict listo para meter en el prompt, más `confianza`, que es la
    honestidad del asunto: dice cuánto sabemos de verdad.
    """
    campanas = campanas or []
    pedidos = pedidos or []
    productos = productos or []

    # ---- qué se vende de verdad ----
    por_sku = {}
    for o in pedidos:
        for it in (o.get('items') or []):
            k = it.get('product_id') or it.get('name') or '?'
            d = por_sku.setdefault(k, {'sku': k, 'nombre': it.get('name', k),
                                       'piezas': 0, 'ingreso': 0.0})
            d['piezas'] += int(it.get('quantity') or 0)
            d['ingreso'] += float(it.get('price') or 0) * int(it.get('quantity') or 0)

    totales = [float(o.get('total') or 0) for o in pedidos]
    ticket = round(sum(totales) / len(totales)) if totales else 0
    nuevos = sum(1 for o in pedidos if o.get('first_order'))

    # ---- qué campaña funcionó y cuál no ----
    ganadoras = [c for c in campanas if c.get('veredicto') == 'gana']
    perdedoras = [c for c in campanas if c.get('veredicto') in ('pierde', 'no trae clientes')]
    con_cac = [c['cac'] for c in campanas if c.get('cac') is not None]
    cac_medio = round(sum(con_cac) / len(con_cac)) if con_cac else None

    # ---- margen: no hay costo por producto, pero el tope de comisión es el
    # proxy que ya usa la casa (escalera ROI de la maestra). Se dice tal cual
    # para que nadie lea "margen" donde dice "holgura".
    holgura = {p.get('sku') or p.get('slug'): p.get('commission_cap', 0)
               for p in productos if p.get('sku') or p.get('slug')}

    catalogo = [{'sku': p.get('sku') or p.get('slug'), 'nombre': p.get('name'),
                 'precio': p.get('price'), 'categoria': p.get('category'),
                 'existencia': p.get('stock', 0),
                 'holgura_comision': p.get('commission_cap', 0)}
                for p in productos if p.get('stock', 0) > 0][:60]

    razones = []
    if len(pedidos) < MIN_PEDIDOS_PARA_APRENDER:
        razones.append(f'solo hay {len(pedidos)} pedidos en el periodo')
    if len(campanas) < MIN_CAMPANAS_PARA_APRENDER:
        razones.append(f'solo hay {len(campanas)} campañas para comparar')
    if not con_cac:
        razones.append('ninguna campaña ha traído todavía un cliente nuevo atribuido')

    return {
        'confianza': {
            'suficiente': not razones,
            'por_que': razones or ['hay historial suficiente para aprender de él'],
        },
        'ventas': {
            'pedidos': len(pedidos),
            'clientes_nuevos': nuevos,
            'ticket_promedio': ticket,
            'ingreso': round(sum(totales)),
            'mas_vendidos': _top(por_sku, 8, 'ingreso'),
        },
        'campanas': {
            'cac_promedio': cac_medio,
            'ganadoras': [{'campana': c['campana'], 'cac': c['cac'], 'roas': c['roas'],
                           'clientes_nuevos': c['clientes_nuevos']} for c in ganadoras][:6],
            'perdedoras': [{'campana': c['campana'], 'gasto': c['gasto_mxn'],
                            'clientes_nuevos': c['clientes_nuevos']} for c in perdedoras][:6],
            'angulos_ya_usados': [c['campana'] for c in campanas][:15],
        },
        'catalogo': catalogo,
        'holgura': holgura,
        'fx': fx,
    }


SISTEMA = """Eres el director de marketing de Exygen Labs, una tienda mexicana de
péptidos de investigación (RUO, Research Use Only). Escribes en español de México.

REGLAS QUE NO SE ROMPEN:
- Los productos son SOLO para investigación. NUNCA prometas efectos en personas,
  resultados de salud, pérdida de peso ni nada terapéutico. Nada de "baja de peso",
  "cura", "rejuvenece", "dosis recomendada".
- No inventes cifras. Usa SOLO los números del briefing. Si el briefing dice que no
  hay datos suficientes, dilo en `advertencia` y propón algo conservador y barato
  de probar.
- En México la venta de estos compuestos toca terreno delicado (COFEPRIS). NUNCA
  afirmes que algo es legal, aprobado, o que no requiere receta ni licencia.
- Meta rechaza anuncios con promesas de salud, imágenes de "antes y después" y
  lenguaje médico. Escribe copy que pase revisión: enfoque en investigación,
  pureza, certificado por lote y envío.

Devuelve SOLO un JSON válido, sin texto alrededor y sin ```. Con esta forma exacta:
{
 "nombre": "nombre corto de la campaña",
 "advertencia": "qué tan confiable es esta propuesta y por qué (una frase)",
 "por_que": "en qué dato del briefing te basas (cita las cifras)",
 "producto": {"sku": "", "nombre": "", "por_que": ""},
 "publico": {"quien": "", "edad": "", "intereses": [], "ubicacion": ""},
 "angulo": "la idea central, distinta a los ángulos ya usados",
 "anuncios": [
   {"formato": "video|imagen", "gancho": "primeros 3 segundos",
    "titulo": "", "texto": "", "llamado": "", "idea_visual": ""}
 ],
 "presupuesto": {"diario_mxn": 0, "dias": 0, "total_mxn": 0,
                 "clientes_esperados": 0, "en_que_me_baso": ""},
 "que_medir": ["", ""],
 "cuando_apagar": "la regla concreta para matarla si no jala",
 "siguiente_prueba": "qué probar después si esta funciona"
}
Dame 3 anuncios distintos entre sí (distinto gancho, no la misma idea reescrita)."""


def prompt(brief, objetivo='conseguir clientes nuevos', presupuesto_mxn=0):
    """El texto que ve el modelo. Separado para poder revisarlo sin gastar llamadas."""
    partes = [
        f'OBJETIVO: {objetivo}',
        f'PRESUPUESTO DISPONIBLE: ${presupuesto_mxn:,.0f} MXN' if presupuesto_mxn
        else 'PRESUPUESTO: no definido, propón uno prudente para probar.',
        '',
        'BRIEFING (datos reales del negocio, no inventes otros):',
        json.dumps(brief, ensure_ascii=False, indent=1),
    ]
    return '\n'.join(partes)


def _limpiar(texto):
    """El modelo a veces envuelve el JSON en ```json … ```."""
    t = (texto or '').strip()
    if t.startswith('```'):
        t = t.split('\n', 1)[-1]
        t = t.rsplit('```', 1)[0]
    return t.strip()


def parsear(texto):
    """Convierte la respuesta en dict. Si no es JSON, lo dice en vez de reventar."""
    try:
        d = json.loads(_limpiar(texto))
        return d if isinstance(d, dict) else {'error': 'La IA no devolvió una propuesta.'}
    except Exception:
        return {'error': 'La IA no devolvió un JSON válido.', 'crudo': (texto or '')[:2000]}


async def proponer(brief, objetivo='conseguir clientes nuevos', presupuesto_mxn=0):
    """Le pide la campaña al modelo. Lo único que sale a internet."""
    from google import genai
    from google.genai import types
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        raise RuntimeError('Falta GEMINI_API_KEY: el director necesita la IA configurada.')
    client = genai.Client(api_key=key)
    r = await client.aio.models.generate_content(
        model=os.environ.get('AI_MODEL_NAME', 'gemini-3.5-flash'),
        contents=prompt(brief, objetivo, presupuesto_mxn),
        config=types.GenerateContentConfig(system_instruction=SISTEMA, temperature=0.9),
    )
    return parsear(getattr(r, 'text', '') or '')
