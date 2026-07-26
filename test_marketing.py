"""Pruebas del cruce gasto-de-Meta contra ventas-del-sitio.

Todo en DOLARES con el TC fijo de la maestra (17.5): el gasto de Meta y los
costos de proveedor ya nacen en dolares; lo unico que se convierte son las VENTAS.

Lo que se protege aquí es que el costo por cliente NO se abarate solo. Es la
cifra con la que Christian va a decidir dónde poner el dinero, así que cada
regla que la hace honesta tiene su prueba.
"""
import marketing as m


# ---------------------------------------------------------------- nombres
def test_slug_empata_el_nombre_de_meta_con_el_utm():
    # En Meta la campaña se llama con acentos y mayúsculas; en el enlace va en
    # minúsculas y con guiones. Si no empatan, todo cae en "sin etiquetar".
    assert m.slug('Retatrutida — Julio 2026') == m.slug('retatrutida-julio-2026')
    assert m.slug('Péptidos  MÉXICO!!') == 'peptidos-mexico'
    assert m.slug(None) == ''


# ------------------------------------------------------------ de dónde vino
def test_fbclid_basta_para_saber_que_vino_de_meta():
    # El caso real: publicación impulsada sin etiquetar. Meta siempre pega fbclid.
    assert m.es_de_meta({'fbclid': 'IwAR123'}) is True
    assert m.campana_del_pedido({'fbclid': 'IwAR123'}) == m.SIN_ETIQUETAR


def test_utm_manda_sobre_todo_lo_demas():
    attr = {'fbclid': 'x', 'utm_source': 'facebook', 'utm_campaign': 'Verano 2026'}
    assert m.campana_del_pedido(attr) == 'verano-2026'


def test_lo_que_no_es_de_meta_no_entra():
    assert m.campana_del_pedido({'utm_source': 'google'}) == ''
    assert m.campana_del_pedido({}) == ''
    assert m.campana_del_pedido({'referrer': 'https://www.google.com/'}) == ''


def test_referrer_de_facebook_cuenta_aunque_no_haya_utm_ni_fbclid():
    assert m.es_de_meta({'referrer': 'https://l.facebook.com/'}) is True


# ------------------------------------------------------- el costo por cliente
def _pedido(campana, total, nuevo=True, fbclid=''):
    return {'attribution': {'utm_source': 'facebook', 'utm_campaign': campana, 'fbclid': fbclid},
            'first_order': nuevo, 'total': total}


def test_cac_es_gasto_entre_clientes_nuevos():
    filas = [{'campaign': 'Verano', 'spend': 100.0, 'currency': 'USD', 'link_clicks': 200}]
    pedidos = [_pedido('Verano', 3000), _pedido('Verano', 2000)]
    r = m.cruzar(filas, pedidos)
    fila = r['campanas'][0]
    assert fila['gasto'] == 100               # el gasto de Meta NO se convierte
    assert fila['clientes_nuevos'] == 2
    assert fila['cac'] == 50.0                # 100 USD / 2 clientes nuevos
    assert fila['ingreso_mxn'] == 5000        # lo que se cobro, sin tocar
    assert fila['ingreso'] == round(5000 / 17.5, 2)   # solo la VENTA se convierte
    assert fila['roas'] == round((5000 / 17.5) / 100, 2)


def test_un_cliente_que_repite_NO_abarata_el_costo():
    # La regla que más protege el número: solo cuenta quien compra por primera vez.
    filas = [{'campaign': 'Verano', 'spend': 100.0, 'currency': 'USD', 'link_clicks': 200}]
    solo_nuevo = m.cruzar(filas, [_pedido('Verano', 3000)])['campanas'][0]
    con_repetidor = m.cruzar(filas, [_pedido('Verano', 3000),
                                     _pedido('Verano', 9000, nuevo=False)])['campanas'][0]
    assert solo_nuevo['cac'] == con_repetidor['cac'] == 100.0
    assert con_repetidor['pedidos'] == 2            # los dos pedidos sí se ven
    assert con_repetidor['ingreso_mxn'] == 12000    # y su ingreso también


def test_sin_clientes_el_cac_es_nulo_no_cero():
    # Un CAC de 0 se lee como "gratis"; nulo se lee como "todavía no trae nadie".
    filas = [{'campaign': 'Fria', 'spend': 100.0, 'currency': 'USD', 'link_clicks': 200}]
    fila = m.cruzar(filas, [])['campanas'][0]
    assert fila['cac'] is None
    assert fila['veredicto'] == 'no trae clientes'


def test_lo_sin_etiquetar_va_aparte_y_no_se_reparte():
    # Si se repartiera, TODAS las campañas se verían mejor de lo que son.
    filas = [{'campaign': 'Verano', 'spend': 100.0, 'currency': 'USD', 'link_clicks': 200}]
    pedidos = [_pedido('', 5000, fbclid='IwAR1')]      # vino de Meta, sin utm
    r = m.cruzar(filas, pedidos)
    assert r['campanas'][0]['clientes_nuevos'] == 0
    assert r['campanas'][0]['ingreso'] == 0
    assert r['sin_etiquetar']['clientes_nuevos'] == 1
    assert r['sin_etiquetar']['ingreso_mxn'] == 5000
    assert r['total']['clientes_nuevos'] == 1          # en el total sí suma


def test_campana_apagada_que_sigue_vendiendo_no_desaparece():
    # Meta ya no la reporta, pero su ingreso es real y debe verse.
    r = m.cruzar([], [_pedido('Vieja', 4000)])
    fila = next(f for f in r['campanas'] if f['slug'] == 'vieja')
    assert fila['gasto'] == 0 and fila['ingreso_mxn'] == 4000


def test_si_meta_facturara_en_pesos_ese_gasto_si_se_pasa_a_dolares():
    filas = [{'campaign': 'MX', 'spend': 1750.0, 'currency': 'MXN', 'link_clicks': 200}]
    assert m.cruzar(filas, [])['campanas'][0]['gasto'] == 100.0


def test_con_poco_gasto_dice_sin_datos_en_vez_de_juzgar():
    filas = [{'campaign': 'Nueva', 'spend': 1.0, 'currency': 'USD', 'link_clicks': 3}]
    assert m.cruzar(filas, [])['campanas'][0]['veredicto'] == 'sin datos'


def test_veredictos():
    assert m.veredicto(5000, 500, 3, 3.0) == 'gana'
    assert m.veredicto(5000, 500, 3, 1.4) == 'apenas'
    assert m.veredicto(5000, 500, 3, 0.4) == 'pierde'


# ------------------------------------------------------------------ enlaces
def test_el_enlace_trae_las_etiquetas_que_luego_se_cruzan():
    url = m.enlace('https://exygenlabs.com', 'Retatrutida Julio', contenido='Video A')
    assert 'utm_campaign=retatrutida-julio' in url
    assert 'utm_content=video-a' in url
    # y lo que se pega en el enlace debe cruzar con el nombre en Meta:
    assert m.slug('Retatrutida Julio') == 'retatrutida-julio'


def test_el_enlace_respeta_una_ruta_con_query():
    url = m.enlace('https://exygenlabs.com/producto/nad-plus?x=1', 'Verano')
    assert url.count('?') == 1 and 'utm_campaign=verano' in url


# ------------------------------------------------- todos los canales, no solo Meta
def _p(total, origen=None, nuevo=True, dist=None, comision=0):
    o = {'total': total, 'first_order': nuevo, 'attribution': origen or {}}
    if dist:
        o['referred_by'] = dist
        o['commissions'] = [{'distributor_id': dist, 'role': 'seller', 'amount': comision}]
    return o


def test_clasifica_cada_canal_de_origen():
    assert m.canal_de_origen({'fbclid': 'x'}) == 'meta'
    assert m.canal_de_origen({'utm_source': 'whatsapp'}) == 'whatsapp'
    assert m.canal_de_origen({'referrer': 'https://wa.me/521555'}) == 'whatsapp'
    assert m.canal_de_origen({'utm_source': 'google'}) == 'google'
    assert m.canal_de_origen({'referrer': 'https://otro.com/'}) == 'otro sitio'
    assert m.canal_de_origen({}) == 'directo'


def test_el_canal_directo_no_finge_tener_costo():
    # Directo no es "gratis": es que su costo no está en ningún lado. Poner 0
    # haría que pareciera el canal más rentable del mundo.
    r = m.canales([_p(3000)], gasto_meta=100)
    directo = next(f for f in r['por_origen'] if f['canal'] == 'directo')
    assert directo['costo'] is None and directo['cac'] is None


def test_meta_si_lleva_su_costo_y_su_cac():
    r = m.canales([_p(3000, {'fbclid': 'x'}), _p(5000, {'fbclid': 'y'})], gasto_meta=100)
    meta = next(f for f in r['por_origen'] if f['canal'] == 'meta')
    assert meta['clientes_nuevos'] == 2 and meta['costo'] == 100
    assert meta['cac'] == 50.0
    assert meta['ingreso_mxn'] == 8000


def test_el_costo_del_distribuidor_es_su_comision():
    pedidos = [_p(10000, dist='d1', comision=2000),
               _p(6000, dist='d1', comision=1200, nuevo=False)]
    d = m.canales(pedidos)['distribuidores']
    assert d['pedidos'] == 2 and d['clientes_nuevos'] == 1
    assert d['comisiones_mxn'] == 3200
    assert d['cac'] == round(3200 / 17.5, 2)   # la comision, en dolares, / 1 nuevo
    assert d['detalle'][0]['distributor_id'] == 'd1'


def test_el_traslape_meta_distribuidor_se_dice_en_voz_alta():
    # Llego por un anuncio Y se cerro con codigo de distribuidor: sale en las dos
    # tablas a proposito, por eso hay que avisar que no se suman.
    pedidos = [_p(4000, {'fbclid': 'x'}, dist='d1', comision=800)]
    r = m.canales(pedidos, gasto_meta=100)
    assert r['traslape_meta_distribuidor'] == 1
    assert next(f for f in r['por_origen'] if f['canal'] == 'meta')['pedidos'] == 1
    assert r['distribuidores']['pedidos'] == 1

