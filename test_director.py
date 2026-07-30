"""Pruebas del director de marketing.

Lo que se protege: que la IA reciba HECHOS y no adivinanzas, y que cuando no
haya datos suficientes lo diga en vez de fingir seguridad. La parte creativa no
se puede probar; la parte que puede estar mal en silencio, sí.
"""
import json
import director as D


def _pedido(total, sku='BPC157-5MG', nombre='BPC-157', qty=1, precio=None, nuevo=True,
            pagado=True):
    # El director sólo aprende de ventas COBRADAS (cobrado.py, 2026-07-29).
    return {'total': total, 'first_order': nuevo, 'status': 'entregado', 'paid': pagado,
            'items': [{'product_id': sku, 'name': nombre, 'quantity': qty,
                       'price': precio if precio is not None else total}]}


def _campana(nombre, veredicto, cac=None, roas=None, nuevos=0, gasto=0):
    return {'campana': nombre, 'veredicto': veredicto, 'cac': cac, 'roas': roas,
            'clientes_nuevos': nuevos, 'gasto_mxn': gasto}


# ------------------------------------------------------------- honestidad
def test_sin_historial_avisa_que_va_a_ciegas():
    b = D.briefing()
    assert b['confianza']['suficiente'] is False
    assert any('pedidos' in r for r in b['confianza']['por_que'])


def test_con_historial_suficiente_dice_que_si():
    pedidos = [_pedido(3000) for _ in range(6)]
    campanas = [_campana('A', 'gana', cac=500, roas=3, nuevos=4),
                _campana('B', 'pierde', gasto=2000)]
    b = D.briefing(campanas, pedidos)
    assert b['confianza']['suficiente'] is True


def test_avisa_cuando_ninguna_campana_trajo_clientes():
    pedidos = [_pedido(3000) for _ in range(6)]
    campanas = [_campana('A', 'no trae clientes'), _campana('B', 'no trae clientes')]
    b = D.briefing(campanas, pedidos)
    assert b['confianza']['suficiente'] is False
    assert any('cliente nuevo' in r for r in b['confianza']['por_que'])


# ------------------------------------------------------------------ hechos
def test_los_mas_vendidos_salen_por_ingreso_no_por_piezas():
    pedidos = [
        _pedido(500, 'BARATO', 'Barato', qty=10, precio=50),    # 10 piezas, $500
        _pedido(4000, 'CARO', 'Caro', qty=1, precio=4000),      # 1 pieza, $4000
    ]
    top = D.briefing([], pedidos)['ventas']['mas_vendidos']
    assert top[0]['sku'] == 'CARO'


def test_ticket_promedio_y_clientes_nuevos():
    pedidos = [_pedido(1000), _pedido(3000), _pedido(2000, nuevo=False)]
    v = D.briefing([], pedidos)['ventas']
    assert v['ticket_promedio'] == 2000
    assert v['clientes_nuevos'] == 2
    assert v['pedidos'] == 3


def test_separa_ganadoras_de_perdedoras_y_saca_el_cac_promedio():
    campanas = [_campana('Gana', 'gana', cac=400, roas=3, nuevos=5),
                _campana('Pierde', 'pierde', cac=1600, roas=0.4, nuevos=1, gasto=1600)]
    c = D.briefing(campanas, [])['campanas']
    assert [x['campana'] for x in c['ganadoras']] == ['Gana']
    assert [x['campana'] for x in c['perdedoras']] == ['Pierde']
    assert c['cac_promedio'] == 1000          # (400 + 1600) / 2


def test_los_angulos_ya_usados_van_para_no_repetirlos():
    campanas = [_campana('Retatrutida Julio', 'gana', cac=1, roas=1, nuevos=1)]
    assert 'Retatrutida Julio' in D.briefing(campanas, [])['campanas']['angulos_ya_usados']


def test_el_catalogo_solo_lleva_lo_que_hay_en_existencia():
    productos = [{'sku': 'HAY', 'name': 'Hay', 'price': 100, 'stock': 5, 'category': 'x'},
                 {'sku': 'NO-HAY', 'name': 'No hay', 'price': 100, 'stock': 0, 'category': 'x'}]
    skus = [p['sku'] for p in D.briefing([], [], productos)['catalogo']]
    assert skus == ['HAY']   # anunciar lo agotado es tirar el dinero


# ------------------------------------------------------------------ prompt
def test_el_prompt_lleva_los_datos_reales_y_el_objetivo():
    b = D.briefing([], [_pedido(3000)])
    p = D.prompt(b, objetivo='vender NAD+', presupuesto_mxn=5000)
    assert 'vender NAD+' in p and '5,000' in p
    assert 'ticket_promedio' in p          # el briefing va completo, no resumido


def test_las_reglas_duras_estan_en_el_sistema():
    # Si alguien las borra sin querer, esta prueba lo caza: son las que evitan
    # que salga un anuncio con promesas de salud (y que Meta lo rechace o peor).
    s = D.SISTEMA.lower()
    for regla in ('investigación', 'nunca', 'cofepris', 'no inventes'):
        assert regla in s


# ----------------------------------------------------------------- parseo
def test_parsea_json_envuelto_en_bloque_de_codigo():
    crudo = '```json\n{"nombre": "Prueba", "angulo": "x"}\n```'
    assert D.parsear(crudo)['nombre'] == 'Prueba'


def test_si_no_es_json_lo_dice_en_vez_de_reventar():
    r = D.parsear('Claro, aquí va tu campaña:')
    assert 'error' in r and 'crudo' in r


def test_parsea_json_limpio():
    assert D.parsear(json.dumps({'nombre': 'A'}))['nombre'] == 'A'


def test_el_sistema_pide_las_dos_versiones_de_cada_anuncio():
    # Christian, 2026-07-26: cada anuncio en dos copias, una al sitio y otra a
    # WhatsApp. Si alguien borra esto sin querer, esta prueba lo caza.
    s = D.SISTEMA.lower()
    assert 'version_web' in s and 'version_whatsapp' in s


def test_la_version_de_whatsapp_exige_cupon():
    # Es lo unico que hace medible a WhatsApp: sin URL no hay utm, asi que la
    # venta solo se puede atribuir por el codigo que use el cliente al comprar.
    s = D.SISTEMA.lower()
    assert 'cupon' in s or 'cupón' in s
    assert 'wa-' in s


def test_marca_los_productos_desaprovechados():
    # Christian, 2026-07-26 (a raiz de GHK-Cu): el director proponia por lo que YA
    # se habia vendido, asi que un producto con buen margen que nadie empujo nunca
    # se proponia solo. Quedaba fuera para siempre por no haber estado dentro.
    productos = [
        {'sku': 'GANADOR', 'name': 'Ganador', 'price': 100, 'stock': 10, 'commission_cap': 0.5},
        {'sku': 'OLVIDADO', 'name': 'Olvidado', 'price': 100, 'stock': 10, 'commission_cap': 0.5},
        {'sku': 'FLACO', 'name': 'Sin margen', 'price': 100, 'stock': 10, 'commission_cap': 0.1},
    ]
    pedidos = [_pedido(5000, sku='GANADOR', nombre='Ganador')]
    d = D.briefing([], pedidos, productos)['desaprovechados']
    skus = [x['sku'] for x in d]
    assert 'OLVIDADO' in skus          # margen bueno y cero ventas
    assert 'GANADOR' not in skus       # ese ya vende
    assert 'FLACO' not in skus         # sin margen no vale la pena empujarlo


def test_el_sistema_le_dice_que_no_repita_siempre_al_ganador():
    assert 'desaprovechados' in D.SISTEMA.lower()
