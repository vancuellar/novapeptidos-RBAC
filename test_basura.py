"""EL FILTRO DE LOS «CHISMOSOS» — y sobre todo, que no se coma a un cliente real.

Christián, 2026-08-05: «quiero evitar esos chismosos que nada más le están picando a lo
estúpido», y «que un pedido así no me notifique ni dispare un correo de carrito
abandonado y que caduque solo».

⛔ LA MITAD IMPORTANTE DE ESTE ARCHIVO SON LOS FALSOS POSITIVOS. Un pedido de broma que
se cuela cuesta un renglón feo en el panel. Un cliente de verdad marcado como broma
cuesta LA VENTA: no le suena la campanita a nadie, no lo persigue la oferta y a las 24
horas se cancela solo. Por eso los seis clientes REALES de la base están aquí abajo, con
sus datos tal cual, y ninguno puede salir marcado nunca.
"""
import basura


# =============================================================================
#  LOS DOS DE BROMA QUE DE VERDAD LLEGARON (5-ago-2026)
# =============================================================================
BROMA_1 = {'full_name': 'Hola hola', 'email': 'fjwoijewijfeow@gmail.com',
           'phone': '+52 (81) 2622-1548', 'address': 'La jolla 6 ',
           'city': '66218', 'state': '', 'postal_code': ''}
BROMA_2 = {'full_name': 'Hola', 'email': 'hola@gmail.com',
           'phone': '+52 (12) 3456-7890', 'address': 'La Jolla 6 ',
           'city': 'San Nicolas', 'state': '', 'postal_code': ''}

# =============================================================================
#  LOS CLIENTES REALES — copiados de producción. NINGUNO puede caer.
# =============================================================================
REALES = [
    {'full_name': 'Fabiola Hernández Rodríguez', 'email': 'fiby.rodriguez@gmail.com',
     'phone': '+52 (55) 8007-6163', 'address': 'Acapulco s/n, Manzana 1, Lote 1',
     'city': 'Ecatepec de Morelos', 'state': 'México', 'postal_code': '55000'},
    {'full_name': 'Brenda Iliana Oseguera Gonzalez', 'email': 'breniog73@yahoo.com.mx',
     'phone': '+52 (44) 2521-7088', 'address': 'Prolongación el Roble 73',
     'city': 'San Juan del Río', 'state': 'Querétaro', 'postal_code': '76800'},
    {'full_name': 'aidee liliana garcia hernandez', 'email': 'lilygarciahdz@hotmail.com',
     'phone': '+52 (81) 3630-9271', 'address': 'Cozumel 1001, Col. Valles de San Roque',
     'city': 'Guadalupe', 'state': 'Nuevo León', 'postal_code': '67140'},
    # Éste escribió la calle también en el renglón de la ciudad: UNA señal, y por eso
    # NO cae. Es exactamente el caso que justifica pedir dos.
    {'full_name': 'Ivan Mimila', 'email': 'ivanamimila@gmail.com',
     'phone': '+52 (55) 3986-6223', 'address': 'Querubines 1',
     'city': 'Querubines 1', 'state': 'Querétaro', 'postal_code': '76000'},
    {'full_name': 'Agustín dela rosa', 'email': 'delarosasolin@gmail.com',
     'phone': '+52 (89) 9426-8496', 'address': 'Indianapolis 123',
     'city': 'Reynosa', 'state': 'Tamaulipas', 'postal_code': '88500'},
]


def test_los_dos_pedidos_de_broma_reales_SI_se_marcan():
    assert basura.es_basura(BROMA_1), basura.senales(BROMA_1)
    assert basura.es_basura(BROMA_2), basura.senales(BROMA_2)


def test_NINGUN_cliente_real_se_marca_jamas():
    """⛔ EL CANDADO QUE MÁS IMPORTA. Si esto se pone rojo, se está tirando una venta."""
    for c in REALES:
        assert not basura.es_basura(c), (
            f"{c['full_name']} quedaría sin campanita y se cancelaría solo: "
            f"{basura.senales(c)}")


def test_una_sola_senal_NO_basta():
    """La ciudad igual que la calle es prisa, no burla. Con una señal se deja pasar."""
    solo_una = dict(REALES[3])                      # Ivan Mimila, ciudad = calle
    assert len(basura.senales(solo_una)) == 1
    assert not basura.es_basura(solo_una)


# =============================================================================
#  EL CÓDIGO POSTAL — «que valide además el C.P. de la dirección de envío»
# =============================================================================
def test_el_CP_debe_ser_de_cinco_digitos_y_existir():
    assert basura.cp_valido('64000')                # Monterrey
    assert basura.cp_valido('06600')                # CDMX, con cero al frente
    assert not basura.cp_valido('1234')             # corto
    assert not basura.cp_valido('123456')           # largo
    assert not basura.cp_valido('abcde')
    assert not basura.cp_valido('17000')            # prefijo que no existe en México


def test_el_CP_tiene_que_cuadrar_con_el_estado():
    assert basura.cp_cuadra_con_estado('64000', 'Nuevo León')
    assert basura.cp_cuadra_con_estado('64000', 'nuevo leon')       # sin acento
    assert not basura.cp_cuadra_con_estado('64000', 'Yucatán')      # Monterrey no es Mérida
    assert basura.cp_cuadra_con_estado('06600', 'Ciudad de México')
    assert basura.cp_cuadra_con_estado('06600', 'CDMX')


def test_los_prefijos_compartidos_no_castigan_a_nadie():
    """El 63 lo usan Jalisco y Nayarit; el 98, Yucatán y Zacatecas. Los dos valen."""
    assert basura.cp_cuadra_con_estado('63000', 'Nayarit')
    assert basura.cp_cuadra_con_estado('63000', 'Jalisco')
    assert basura.cp_cuadra_con_estado('98000', 'Zacatecas')
    assert basura.cp_cuadra_con_estado('98000', 'Yucatán')


def test_sin_CP_o_sin_estado_se_da_por_bueno():
    """No se castiga por un dato que el formulario no pidió."""
    assert basura.cp_cuadra_con_estado('', 'Jalisco')
    assert basura.cp_cuadra_con_estado('44100', '')


def test_el_CP_que_no_cuadra_es_UNA_senal_no_un_rechazo():
    c = dict(REALES[0], postal_code='64000')        # CP de NL con estado México
    assert 'el codigo postal no cuadra con el estado' in basura.senales(c)
    assert not basura.es_basura(c), 'un CP mal tecleado no puede costar la venta'


# =============================================================================
#  LAS SEÑALES, UNA POR UNA
# =============================================================================
def test_telefonos_de_juguete():
    assert basura._telefono_de_juguete('1234567890')
    assert basura._telefono_de_juguete('0000000000')
    assert basura._telefono_de_juguete('55 1234')            # incompleto
    assert not basura._telefono_de_juguete('+52 (55) 8007-6163')


def test_teclado_machacado():
    assert basura._machacado('fjwoijewijfeow')
    assert basura._machacado('asdkjhgqwlkjh')
    assert not basura._machacado('rodriguez')
    assert not basura._machacado('hernandez')
    assert not basura._machacado('breniog73')                # correo real de la base


def test_nombres_que_no_son_nombres():
    assert basura._nombre_de_juego('Hola')
    assert basura._nombre_de_juego('Hola hola')
    assert basura._nombre_de_juego('test')
    assert basura._nombre_de_juego('a')
    assert not basura._nombre_de_juego('Ana Li')             # corto pero real
    assert not basura._nombre_de_juego('Agustín dela rosa')


def test_el_apellido_corto_NO_es_machacado():
    """`_machacado` exige 8 letras: 'Cruz' o 'Diaz' no pueden caer por cortos."""
    for apellido in ('Cruz', 'Diaz', 'Ruiz', 'Sanchez'):
        assert not basura._machacado(apellido)


def test_los_motivos_se_devuelven_legibles():
    """Van al pedido y al panel: Christián tiene que poder discutir el veredicto."""
    motivos = basura.senales(BROMA_2)
    assert all(isinstance(m, str) and ' ' in m for m in motivos)
    assert len(motivos) >= 2


# =============================================================================
#  FALSOS POSITIVOS QUE YA SE COMIERON — cada uno costó un cliente en la prueba
# =============================================================================
def test_el_punto_SEPARA_no_pega():
    """⛔ `vazquez.jr` se volvía `vazquezjr` (zj + jr) y `xochitl.hdz` se volvía
    `xochitlhdz` (tlhdz, cinco consonantes). Dos clientes reales, marcados por
    quitarles el punto. El punto separa palabras: hay que respetarlo."""
    assert not basura._correo_de_juego('vazquez.jr@gmail.com')
    assert not basura._correo_de_juego('xochitl.hdz@hotmail.com')
    assert not basura._correo_de_juego('ma.guadalupe.glz@gmail.com')


def test_apellidos_mexicanos_con_letras_raras_pasan_limpio():
    """Vázquez trae `zq`, Bojórquez trae `jq`, Xóchitl trae `xc`. Todos reales."""
    for nombre in ('Vázquez', 'Velázquez', 'Bojórquez', 'Quiñones', 'Xóchitl',
                   'Ixchel', 'Cuauhtémoc', 'Nezahualcóyotl', 'Zúñiga', 'Joaquín',
                   'Wenceslao', 'Nguyen', 'Schwarz'):
        assert not basura._nombre_de_juego(nombre), nombre
        assert not basura._machacado(nombre), nombre


def test_los_nombres_de_dos_letras_son_reales():
    """«Li», «Ma» (de Ma. Guadalupe). Con el mínimo en 3 caían los dos."""
    assert not basura._nombre_de_juego('Li')
    assert not basura._nombre_de_juego('Ma')
    assert basura._nombre_de_juego('a')          # una sola letra, eso sí


def test_sigue_cazando_el_correo_machacado_de_verdad():
    """El que llegó el 5-ago. Intercala vocales, así que la regla de cinco
    consonantes NO lo veía: hace falta la de pares imposibles."""
    assert basura._correo_de_juego('fjwoijewijfeow@gmail.com')
    assert basura._machacado('asdkjhgqwlkjh')
    assert basura._machacado('zxcvbnmasd')
