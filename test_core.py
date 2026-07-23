"""Pruebas de la logica de negocio del API de Exygen Labs.

No tocan la base de datos ni la red: solo ejercitan las funciones puras de
`server.py`, que es donde vive la aritmetica que le duele al negocio si se
rompe (descuentos, comision, viales restantes, rastreo de envio).

Correr:  pytest test_core.py -q
"""
import json
import os

# database.py exige MONGO_URL al importar. El cliente de motor es perezoso:
# no abre conexion hasta la primera consulta, asi que basta con declararla.
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

from datetime import datetime, timedelta, timezone

import coa_store
from ai_assistant import build_chat, language_instruction
from server import (
    ORDER_NUMBER_RE, SHIPPING_INTENT_RE, STATUS_LABEL,
    build_tracking_url, _order_summary_line, _protocol_projection,
    gen_order_number, gen_distributor_code, _distributor_rollup,
    REPURCHASE_WARN_DAYS, _decorate_report,
)
from lab_reference import (
    MARKERS, MARKERS_BY_KEY, evaluate, range_for, families_for_products, relevant_markers,
)
from emails import _distributor_email_html, DIST_COPY


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---------- Correo de bienvenida de distribuidor ----------
def test_distributor_email_shows_code_and_activation_cta():
    html = _distributor_email_html(DIST_COPY['es'], name='María', code='MARI-4821',
                                   link='https://exygenlabs.com/activar?token=x', needs_activation=True)
    assert 'MARI-4821' in html                 # su código de referido, visible
    assert 'Activar mi cuenta' in html         # cuenta nueva → botón de activar
    assert 'Entrar a mi panel' not in html
    assert 'programa de distribuidores' in html.lower() or 'distribuidores' in html.lower()


def test_distributor_email_for_converted_client_links_to_panel():
    html = _distributor_email_html(DIST_COPY['es'], name='María', code='MARI-4821',
                                   link='https://exygenlabs.com/distribuidor', needs_activation=False)
    assert 'MARI-4821' in html
    assert 'Entrar a mi panel' in html         # ya tiene contraseña → botón al panel
    assert 'Activar mi cuenta' not in html


def test_distributor_email_escapes_the_name():
    html = _distributor_email_html(DIST_COPY['es'], name='<b>x</b>', code='C-1',
                                   link='https://exygenlabs.com/x', needs_activation=True)
    assert '<b>x</b>' not in html and '&lt;b&gt;' in html


# ---------- Numero de pedido ----------
def test_order_number_matches_canonical_format():
    m = ORDER_NUMBER_RE.search('quiero saber de EX-20260719-1234 porfa')
    assert m and m.group(1) == '20260719' and m.group(2) == '1234'


def test_order_number_tolerates_spaces_and_lowercase():
    assert ORDER_NUMBER_RE.search('ex 20260719 1234')
    assert ORDER_NUMBER_RE.search('EX202607191234')


def test_order_number_ignores_wrong_shapes():
    assert ORDER_NUMBER_RE.search('EX-2026-1234') is None       # fecha corta
    assert ORDER_NUMBER_RE.search('EX-20260719-12') is None     # secuencia corta


def test_generated_order_number_is_parseable():
    assert ORDER_NUMBER_RE.fullmatch(gen_order_number())


# ---------- Intencion de envio ----------
def test_shipping_intent_detects_the_usual_phrasings():
    for phrase in ['donde va mi pedido', '¿ya llegó mi envío?', 'dame la guia',
                   'quiero rastrear', 'cual es el estatus', 'cuando entregan']:
        assert SHIPPING_INTENT_RE.search(phrase), phrase


def test_shipping_intent_ignores_unrelated_questions():
    for phrase in ['que es un peptido', 'cuanto cuesta el BPC-157']:
        assert SHIPPING_INTENT_RE.search(phrase) is None, phrase


# ---------- URL de rastreo ----------
def test_tracking_url_is_built_for_known_carriers():
    assert '794123' in build_tracking_url('FedEx', '794123')
    assert '999' in build_tracking_url('Paquetexpress', '999')


def test_tracking_url_tolerates_how_the_admin_types_the_carrier():
    # El nombre lo escribe una persona: espacios, mayusculas y acentos varian.
    for typed in ['  fedex ', 'FEDEX', 'Fed Ex']:
        assert '794123' in build_tracking_url(typed, '794123'), typed
    for typed in ['Paquete Express', 'paquetexpress']:
        assert '999' in build_tracking_url(typed, '999'), typed
    assert '555' in build_tracking_url('Correos de México', '555')


def test_tracking_url_is_empty_when_we_cannot_build_one():
    assert build_tracking_url('MensajeriaLocal', '123') == ''
    assert build_tracking_url('FedEx', '') == ''


# ---------- Resumen de orden para la IA ----------
def test_order_summary_includes_status_and_tracking():
    line = _order_summary_line({
        'order_number': 'EX-20260719-1234', 'status': 'enviado', 'created_at': '2026-07-19T10:00:00+00:00',
        'total': 4109, 'items': [{'quantity': 2, 'name': 'Tirzepatida 20 mg'}],
        'carrier': 'FedEx', 'tracking_number': '794123', 'tracking_url': 'https://x/794123',
        'shipped_at': '2026-07-19T18:00:00+00:00',
    })
    assert STATUS_LABEL['enviado'] in line
    assert '794123' in line and 'FedEx' in line
    assert '2x Tirzepatida 20 mg' in line


def test_order_summary_never_leaks_the_customer_address():
    line = _order_summary_line({
        'order_number': 'EX-20260719-1234', 'status': 'pendiente', 'created_at': '2026-07-19T10:00:00+00:00',
        'total': 1000, 'items': [],
        'customer': {'full_name': 'Juan Perez', 'address': 'Calle Secreta 123', 'phone': '5512345678'},
    })
    assert 'Calle Secreta' not in line
    assert 'Juan Perez' not in line
    assert '5512345678' not in line


# ---------- Proyeccion de consumo ----------
def test_protocol_projection_counts_doses_and_days():
    # 10 mg de vial, 250 mcg por dosis = 40 dosis; a 7 por semana son 40 dias.
    p = _protocol_projection({
        'vial_mg': 10, 'vials': 1, 'dose': 250, 'dose_unit': 'mcg',
        'doses_per_week': 7, 'started_at': days_ago(0), 'active': True,
    })
    assert p['total_doses'] == 40
    assert p['doses_used'] == 0
    assert p['days_left'] == 40
    assert p['pct_left'] == 100
    assert p['needs_repurchase'] is False


def test_protocol_projection_consumes_over_time():
    p = _protocol_projection({
        'vial_mg': 10, 'vials': 1, 'dose': 250, 'dose_unit': 'mcg',
        'doses_per_week': 7, 'started_at': days_ago(30), 'active': True,
    })
    assert p['doses_used'] == 30
    assert p['doses_left'] == 10
    assert p['days_left'] == 10
    assert p['needs_repurchase'] is True          # 10 <= REPURCHASE_WARN_DAYS


def test_protocol_projection_handles_mg_dosing():
    # 60 mg de vial, 2.5 mg por dosis semanal = 24 dosis = 168 dias.
    p = _protocol_projection({
        'vial_mg': 60, 'vials': 1, 'dose': 2.5, 'dose_unit': 'mg',
        'doses_per_week': 1, 'started_at': days_ago(0), 'active': True,
    })
    assert p['total_doses'] == 24
    assert p['days_left'] == 168
    assert p['needs_repurchase'] is False


def test_protocol_projection_never_goes_negative():
    p = _protocol_projection({
        'vial_mg': 10, 'vials': 1, 'dose': 250, 'dose_unit': 'mcg',
        'doses_per_week': 7, 'started_at': days_ago(999), 'active': True,
    })
    assert p['doses_left'] == 0
    assert p['days_left'] == 0
    assert p['pct_left'] == 0


def test_protocol_projection_declines_to_guess_with_missing_data():
    for bad in ({'vial_mg': 0, 'dose': 250, 'doses_per_week': 7},
                {'vial_mg': 10, 'dose': 0, 'doses_per_week': 7},
                {'vial_mg': 10, 'dose': 250, 'doses_per_week': 0}):
        p = _protocol_projection({'vials': 1, 'dose_unit': 'mcg', 'started_at': days_ago(1), **bad})
        assert p['days_left'] is None
        assert p['runs_out_at'] is None
        assert p['needs_repurchase'] is False


def test_protocol_projection_survives_a_corrupt_start_date():
    p = _protocol_projection({
        'vial_mg': 10, 'vials': 1, 'dose': 250, 'dose_unit': 'mcg',
        'doses_per_week': 7, 'started_at': 'no-es-una-fecha', 'active': True,
    })
    assert p['days_left'] == 40      # cae a "empieza hoy" en vez de reventar


def test_repurchase_threshold_is_the_documented_one():
    assert REPURCHASE_WARN_DAYS == 14


# ---------- Distribuidores ----------
def test_distributor_code_is_alphanumeric_and_prefixed():
    code = gen_distributor_code('Farmacia Ñandú 3000')
    prefix, _, digits = code.partition('-')
    assert prefix.isalnum() and len(prefix) <= 4
    assert digits.isdigit() and len(digits) == 4


def test_distributor_code_falls_back_when_the_name_has_no_letters():
    assert gen_distributor_code('!!! ???').startswith('DIST-')


def test_distributor_rollup_excludes_cancelled_orders():
    # Regla Christian 2026-07-22: una venta cuenta solo si trae el codigo del
    # distribuidor (order.referred_by == su id). Un pedido de su cliente SIN
    # codigo NO cuenta, aunque el cliente este ligado a el.
    dist = {'id': 'd1', 'name': 'Ana', 'email': 'a@x.com', 'commission_rate': 0.30}
    users = [{'id': 'u1', 'referred_by': 'd1'}, {'id': 'u2', 'referred_by': 'otro'}]
    orders = [
        {'referred_by': 'd1', 'user_id': 'u1', 'status': 'entregado', 'total': 1000, 'commission': 300},
        {'referred_by': 'd1', 'user_id': 'u1', 'status': 'cancelado', 'total': 5000, 'commission': 1500},
        {'user_id': 'u1', 'status': 'entregado', 'total': 8000, 'commission': 0},   # SIN codigo → no cuenta
        {'referred_by': 'otro', 'user_id': 'u2', 'status': 'entregado', 'total': 9000, 'commission': 2700},
    ]
    r = _distributor_rollup(dist, users, orders)
    assert r['clients_count'] == 1
    assert r['sales_count'] == 1
    assert r['sales_total'] == 1000        # la cancelada no cuenta
    assert r['earnings'] == 300            # la sin codigo y la ajena tampoco


def test_distributor_rollup_ignores_codeless_orders_from_linked_client():
    # Un cliente ligado que compra SIN el codigo: cero para el distribuidor.
    dist = {'id': 'd1', 'name': 'Ana', 'email': 'a@x.com', 'commission_rate': 0.30}
    users = [{'id': 'u1', 'referred_by': 'd1'}]
    orders = [{'user_id': 'u1', 'status': 'entregado', 'total': 4000, 'commission': 0}]
    r = _distributor_rollup(dist, users, orders)
    assert r['clients_count'] == 1     # sigue siendo su cliente (relacion)
    assert r['sales_count'] == 0       # pero la venta sin codigo no cuenta
    assert r['earnings'] == 0


def test_distributor_rollup_counts_orders_attributed_by_code():
    dist = {'id': 'd1', 'name': 'Ana', 'email': 'a@x.com'}
    orders = [{'referred_by': 'd1', 'user_id': None, 'status': 'confirmado', 'total': 2000, 'commission': 500}]
    r = _distributor_rollup(dist, [], orders)
    assert r['sales_count'] == 1 and r['earnings'] == 500


# ---------- Laboratorio: rangos de referencia ----------
def test_every_marker_declares_a_usable_range():
    for m in MARKERS:
        low, high = range_for(m, 'male')
        assert low is not None and high is not None, m['key']
        assert low < high, m['key']
        assert m['plain'] and m['peptides'], m['key']


def test_marker_keys_are_unique():
    keys = [m['key'] for m in MARKERS]
    assert len(keys) == len(set(keys))


def test_evaluate_classifies_against_the_range():
    assert evaluate('glucosa', 85) == 'normal'
    assert evaluate('glucosa', 55) == 'bajo'
    assert evaluate('glucosa', 130) == 'alto'


def test_evaluate_uses_the_range_for_the_declared_sex():
    # Hemoglobina 12.5 es normal en mujer y baja en hombre.
    assert evaluate('hemoglobina', 12.5, 'female') == 'normal'
    assert evaluate('hemoglobina', 12.5, 'male') == 'bajo'


def test_evaluate_is_silent_on_what_it_does_not_know():
    assert evaluate('marcador_inventado', 10) is None
    assert evaluate('glucosa', None) is None
    assert evaluate('glucosa', 'no numerico') is None


# ---------- Laboratorio: acotar a los compuestos del cliente ----------
def test_families_are_detected_from_product_names():
    assert 'incretinas' in families_for_products(['Tirzepatida 20 mg'])
    assert 'gh' in families_for_products(['CJC-1295 (sin DAC)', 'Ipamorelin'])
    assert 'reparacion' in families_for_products(['BPC-157 10 mg'])
    assert families_for_products([]) == set()


def test_incretin_user_gets_pancreas_markers_and_not_the_gonadal_axis():
    keys = {m['key'] for m in relevant_markers(families_for_products(['Semaglutida 2 mg']))}
    assert {'lipasa', 'hba1c', 'glucosa'} <= keys
    assert 'testosterona_total' not in keys
    assert 'igf1' not in keys


def test_gh_user_gets_igf1():
    keys = {m['key'] for m in relevant_markers(families_for_products(['Ipamorelin 5 mg']))}
    assert 'igf1' in keys


def test_a_client_with_no_compounds_gets_no_markers():
    assert relevant_markers(families_for_products(['Agua bacteriostatica'])) == []


def test_report_hides_known_markers_outside_the_clients_scope():
    report = {'markers': [
        {'key': 'lipasa', 'label': 'Lipasa', 'value': 40, 'unit': 'U/L'},
        {'key': 'testosterona_total', 'label': 'Testosterona', 'value': 500, 'unit': 'ng/dL'},
    ]}
    allowed = {m['key'] for m in relevant_markers({'incretinas'})}
    out = _decorate_report(report, 'male', allowed)
    labels = [m['label'] for m in out['markers']]
    assert 'Lipasa' in labels
    assert 'Testosterona' not in labels      # no tiene que ver con sus compuestos


def test_report_keeps_unrecognised_markers_and_flags_out_of_range():
    report = {'markers': [
        {'key': 'glucosa', 'label': 'Glucosa', 'value': 130, 'unit': 'mg/dL'},
        {'key': '', 'label': 'Marcador raro del laboratorio', 'value': 3, 'unit': 'x'},
    ]}
    allowed = {m['key'] for m in relevant_markers({'incretinas'})}
    out = _decorate_report(report, 'male', allowed)
    glucosa = next(m for m in out['markers'] if m['key'] == 'glucosa')
    assert glucosa['status'] == 'alto'
    assert glucosa['ref_low'] == 70 and glucosa['ref_high'] == 99
    assert glucosa['plain']
    # Lo que no reconocemos se conserva, sin clasificar y sin inventarle rango.
    raro = next(m for m in out['markers'] if m['key'] == '')
    assert raro['status'] is None and raro['ref_low'] is None


# ---------------- Idioma del asistente ----------------

def test_language_instruction_follows_site_selection():
    """El asistente responde en el idioma que el usuario eligio en el sitio."""
    assert 'English' in language_instruction('en-US')
    assert 'portugues' in language_instruction('pt-BR')
    assert 'francais' in language_instruction('fr-CA')
    assert 'espanol' in language_instruction('es-MX')


def test_language_instruction_defaults_to_spanish():
    """Sin idioma, o con uno que no manejamos, se queda en espanol."""
    for value in (None, '', 'de-DE', 'zz'):
        assert 'espanol' in language_instruction(value)


def test_build_chat_appends_language_instruction():
    chat = build_chat('sess-1', None, 'en-US')
    assert 'IDIOMA DE RESPUESTA' in chat['system_message']
    assert 'English' in chat['system_message']


# ---------------- Almacen de COAs ----------------

def test_coa_lot_regex_blocks_path_traversal():
    """Un lote con barras o puntos-punto no debe poder salir de la carpeta."""
    for bad in ('../secreto', 'a/b', '..', '/etc/passwd', ''):
        assert coa_store.entry_for_lot(bad) is None


def test_coa_file_path_ignores_directories_in_registry(tmp_path, monkeypatch):
    """Aunque el registro traiga una ruta, solo se usa el nombre del archivo."""
    monkeypatch.setattr(coa_store, 'COA_DIR', tmp_path)
    (tmp_path / 'EX-TEST-1.pdf').write_bytes(b'%PDF-1.4')
    path = coa_store.file_path_for({'file': '../../EX-TEST-1.pdf'})
    assert path == tmp_path / 'EX-TEST-1.pdf'
    # Un archivo que no existe no devuelve ruta, en vez de tronar al servirlo.
    assert coa_store.file_path_for({'file': 'no-existe.pdf'}) is None


def test_coa_registry_missing_is_not_fatal(tmp_path, monkeypatch):
    """Sin registry.json el sitio sigue de pie: simplemente no hay COAs."""
    monkeypatch.setattr(coa_store, 'COA_DIR', tmp_path / 'vacia')
    assert coa_store.load_registry() == []
    assert coa_store.public_entry() is None


def test_coa_access_is_scoped_to_purchased_products(tmp_path, monkeypatch):
    """Solo se listan los COAs de los productos que el cliente compro."""
    monkeypatch.setattr(coa_store, 'COA_DIR', tmp_path)
    (tmp_path / 'registry.json').write_text(json.dumps({'lots': [
        {'lot': 'EX-BPC5-2601', 'product_slug': 'bpc-157', 'file': 'a.pdf'},
        {'lot': 'EX-TB5-2601', 'product_slug': 'tb-500', 'file': 'b.pdf'},
        {'lot': 'EX-MUESTRA-1', 'product_slug': 'demo', 'file': 'c.pdf', 'public': True},
    ]}), encoding='utf-8')

    solo_bpc = coa_store.entries_for_slugs({'bpc-157'})
    assert [e['lot'] for e in solo_bpc] == ['EX-BPC5-2601']
    assert coa_store.entries_for_slugs(set()) == []
    assert coa_store.public_entry()['lot'] == 'EX-MUESTRA-1'
    # Lo que se manda al navegador nunca incluye la ruta del archivo.
    assert 'file' not in solo_bpc[0]


# ---------- Lealtad (puntos) ----------
import loyalty
from emails import _order_email_html, ORDER_COPY


def test_loyalty_eligibility():
    assert loyalty.eligible({'role': 'user'})
    assert loyalty.eligible({'role': 'admin'})
    assert not loyalty.eligible({'role': 'distributor'})   # regla de Christian
    assert not loyalty.eligible(None)                      # invitado sin cuenta


def test_loyalty_clamp_redeem():
    # Nunca mas que el saldo ni mas que la mercancia; nunca negativo.
    assert loyalty.clamp_redeem(500, 300, 10000) == 300
    assert loyalty.clamp_redeem(500, 800, 400) == 400
    assert loyalty.clamp_redeem(200, 800, 10000) == 200
    assert loyalty.clamp_redeem(-50, 800, 10000) == 0
    assert loyalty.clamp_redeem(None, 800, 10000) == 0
    assert loyalty.clamp_redeem('abc', 800, 10000) == 0


def test_loyalty_earn():
    # 3% de lo pagado en mercancia, entero hacia abajo.
    assert loyalty.earn(1070, True) == 32
    assert loyalty.earn(19.9, True) == 0
    assert loyalty.earn(0, True) == 0
    assert loyalty.earn(1070, False) == 0
    assert loyalty.earn(None, True) == 0


def test_order_email_ticket_elements():
    order = {
        'order_number': 'EX-20260720-0001',
        'items': [{'name': 'Retatrutida 5 mg', 'quantity': 1, 'price': 1189}],
        'subtotal': 1189, 'discount': 119, 'shipping': 0, 'total': 1070,
        'points_used': 100, 'points_earned': 53, 'payment_method': 'spei',
        'customer': {'full_name': 'Christian Cuellar', 'email': 'x@y.z', 'address': 'Calle 1'},
    }
    html_out = _order_email_html(order, ORDER_COPY['es'], 'https://exygenlabs.com/pedido/x')
    assert 'Apreciable CHRISTIAN CUELLAR:' in html_out       # saludo estilo ticket
    assert 'AHORRASTE $119 MXN' in html_out                  # caja de ahorro
    assert 'GANAS 53 PUNTOS' in html_out                     # puntos de la compra
    assert 'Puntos canjeados' in html_out                    # renglon del canje
    assert 'GRACIAS POR TU COMPRA' in html_out               # despedida
    # Sin descuento, sin puntos: las cajas del ticket no aparecen.
    plain = dict(order, discount=0, points_used=0, points_earned=0)
    html_plain = _order_email_html(plain, ORDER_COPY['es'], 'https://exygenlabs.com/pedido/x')
    assert 'AHORRASTE' not in html_plain
    assert 'GANAS' not in html_plain
    assert 'Puntos canjeados' not in html_plain


# ---------- Segundo factor (TOTP) ----------
import pyotp
import auth_factors


def test_totp_roundtrip():
    secret = auth_factors.new_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert auth_factors.verify_totp(secret, code)
    assert auth_factors.verify_totp(secret, f'  {code[:3]} {code[3:]} ')   # con espacios
    assert not auth_factors.verify_totp(secret, '000000') or code == '000000'
    assert not auth_factors.verify_totp(secret, '')
    assert not auth_factors.verify_totp('', code)
    assert not auth_factors.verify_totp(secret, 'abcdef')


def test_totp_uri_and_qr():
    secret = auth_factors.new_totp_secret()
    uri = auth_factors.totp_uri(secret, 'admin@exygenlabs.com')
    assert uri.startswith('otpauth://totp/')
    assert 'Exygen%20Labs' in uri
    qr = auth_factors.qr_data_uri(uri)
    assert qr.startswith('data:image/png;base64,')
    assert len(qr) > 500


# ---------- Comisiones: tope y candado historico ----------
from server import COMMISSION_CAP


def test_commission_cap_is_50_percent():
    assert COMMISSION_CAP == 0.50


def test_rollup_uses_stored_commission_not_current_rate():
    """La comision de una venta hecha queda CONGELADA en la orden (en pesos).
    Cambiar la tasa del distribuidor o el precio del producto despues NO debe
    mover lo ya ganado: el rollup suma order['commission'], nunca recalcula."""
    dist = {'id': 'd1', 'name': 'Dist', 'email': 'd@x.y', 'distributor_code': 'EX-D1',
            'commission_rate': 0.10,   # tasa ACTUAL, recien bajada
            'customer_discount_rate': 0.10, 'created_at': '2026-07-01T00:00:00+00:00'}
    orders = [
        # venta vieja, cuando comisionaba 40% de $1,000
        {'id': 'o1', 'user_id': 'u1', 'referred_by': 'd1', 'status': 'entregado',
         'total': 1000, 'commission': 400, 'created_at': '2026-07-10T00:00:00+00:00'},
        # venta nueva con la tasa nueva
        {'id': 'o2', 'user_id': 'u1', 'referred_by': 'd1', 'status': 'confirmado',
         'total': 1000, 'commission': 100, 'created_at': '2026-07-20T00:00:00+00:00'},
        # cancelada: no cuenta
        {'id': 'o3', 'user_id': 'u1', 'referred_by': 'd1', 'status': 'cancelado',
         'total': 1000, 'commission': 400, 'created_at': '2026-07-11T00:00:00+00:00'},
    ]
    users = [{'id': 'u1', 'referred_by': 'd1', 'name': 'C', 'email': 'c@x.y'}]
    roll = _distributor_rollup(dist, users, orders)
    assert roll['earnings'] == 500   # 400 congelados + 100 nuevos; la cancelada fuera


# ---------- Cripto (BTCPay) ----------
import btcpay


def test_btcpay_disabled_without_env(monkeypatch):
    for k in ('BTCPAY_URL', 'BTCPAY_STORE_ID', 'BTCPAY_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    assert not btcpay.enabled()


def test_btcpay_enabled_with_env(monkeypatch):
    monkeypatch.setenv('BTCPAY_URL', 'https://pay.exygenlabs.com')
    monkeypatch.setenv('BTCPAY_STORE_ID', 'store123')
    monkeypatch.setenv('BTCPAY_API_KEY', 'key123')
    assert btcpay.enabled()


def test_btcpay_webhook_signature(monkeypatch):
    import hashlib, hmac
    monkeypatch.setenv('BTCPAY_WEBHOOK_SECRET', 'topsecret')
    body = b'{"type":"InvoiceSettled","metadata":{"orderId":"EX-1"}}'
    good = 'sha256=' + hmac.new(b'topsecret', body, hashlib.sha256).hexdigest()
    assert btcpay.verify_webhook(body, good)
    assert not btcpay.verify_webhook(body, 'sha256=deadbeef')   # firma falsa
    assert not btcpay.verify_webhook(body, '')                  # sin firma


def test_btcpay_webhook_failclosed_without_secret(monkeypatch):
    monkeypatch.delenv('BTCPAY_WEBHOOK_SECRET', raising=False)
    # Sin secreto configurado, NINGÚN webhook se acepta.
    assert not btcpay.verify_webhook(b'{}', 'sha256=whatever')


# ---------- Cripto (NOWPayments) ----------
import nowpayments


def test_nowpayments_disabled_without_env(monkeypatch):
    monkeypatch.delenv('NOWPAYMENTS_API_KEY', raising=False)
    assert not nowpayments.enabled()


def test_nowpayments_enabled_with_env(monkeypatch):
    monkeypatch.setenv('NOWPAYMENTS_API_KEY', 'key_abc')
    assert nowpayments.enabled()


def test_nowpayments_ipn_signature(monkeypatch):
    import hashlib, hmac, json
    monkeypatch.setenv('NOWPAYMENTS_IPN_SECRET', 'ipnsecret')
    body = b'{"payment_status":"finished","order_id":"EX-9","price_amount":100}'
    ordered = json.dumps(json.loads(body), sort_keys=True, separators=(',', ':'))
    good = hmac.new(b'ipnsecret', ordered.encode(), hashlib.sha512).hexdigest()
    assert nowpayments.verify_ipn(body, good)
    assert not nowpayments.verify_ipn(body, 'deadbeef')   # firma falsa
    assert not nowpayments.verify_ipn(body, '')           # sin firma


def test_nowpayments_ipn_failclosed_without_secret(monkeypatch):
    monkeypatch.delenv('NOWPAYMENTS_IPN_SECRET', raising=False)
    assert not nowpayments.verify_ipn(b'{}', 'whatever')


# ---------- SPEI: datos de la cuenta en el correo ----------
def test_order_email_shows_spei_clabe():
    order = {
        'order_number': 'EX-20260721-0007',
        'items': [{'name': 'Sema 10 mg', 'quantity': 1, 'price': 2049}],
        'subtotal': 2049, 'discount': 205, 'shipping': 0, 'total': 1844,
        'payment_method': 'spei',
        'spei': {'beneficiary': 'Servicios Profesionales Quimimid SA de CV', 'bank': 'BBVA', 'clabe': '012790001244916613'},
        'customer': {'full_name': 'Cliente Prueba', 'email': 'x@y.z', 'address': 'Calle 1'},
    }
    html_out = _order_email_html(order, ORDER_COPY['es'], 'https://exygenlabs.com/pedido/x')
    assert '012790001244916613' in html_out
    assert 'Quimimid' in html_out
    # Un pedido con tarjeta NO lleva CLABE
    card = dict(order, payment_method='tarjeta'); card.pop('spei')
    assert '012790001244916613' not in _order_email_html(card, ORDER_COPY['es'], 'https://x/y')


# ---------- Ruteo: que los decoradores no se peguen a la funcion equivocada ----------
def test_orders_route_maps_to_get_order():
    """Regresion: un helper metido entre el decorador y la funcion robaba la ruta,
    haciendo que GET /orders/{n} devolviera la CLABE en vez del pedido."""
    import server
    hits = [r.endpoint.__name__ for r in server.app.routes
            if getattr(r, 'path', '') == '/api/orders/{order_number}' and 'GET' in getattr(r, 'methods', set())]
    assert hits and all(name == 'get_order' for name in hits), hits


# ---------- Correo de pago confirmado ----------
import asyncio as _asyncio
def test_payment_confirmed_email_renders(monkeypatch):
    import emails
    monkeypatch.setenv('EMAIL_ENABLED', 'true')
    sent = {}
    def fake_send(to, subject, html_body):
        sent['to'] = to; sent['subject'] = subject; sent['html'] = html_body
    monkeypatch.setattr(emails, '_send_email_sync', fake_send)
    order = {'order_number': 'EX-20260721-0009', 'customer': {'full_name': 'Ana', 'email': 'ana@x.y'}}
    _asyncio.run(emails.send_payment_confirmed_email(order, 'es'))
    assert sent['to'] == 'ana@x.y'
    assert 'EX-20260721-0009' in sent['subject']
    assert 'EX-20260721-0009' in sent['html'] and 'Ana' in sent['html']


# ---------- Pirámide de distribuidores (motor de comisiones) ----------
import pyramid


def test_pyramid_tier_rates_are_the_six_levels():
    assert pyramid.tier_rate('junior0') == 0.20
    assert pyramid.tier_rate('junior1') == 0.25
    assert pyramid.tier_rate('senior') == 0.30
    assert pyramid.tier_rate('master') == 0.35
    assert pyramid.tier_rate('elite') == 0.40
    assert pyramid.tier_rate('diamond') == 0.43   # secreto


def test_pyramid_differential_total_equals_top_tier():
    # Vende junior0 (20). Senior arriba (30) y Elite (40): diferenciales 10 y 10.
    j = {'id': 'j', 'tier': 'junior0'}
    s = {'id': 's', 'tier': 'senior'}
    e = {'id': 'e', 'tier': 'elite'}
    b = pyramid.compute_commission_breakdown(10000, j, [s, e])
    amounts = {r['distributor_id']: r['amount'] for r in b}
    assert amounts == {'j': 2000, 's': 1000, 'e': 1000}   # 20 + 10 + 10
    assert pyramid.total_amount(b) == 4000                 # 40% total = tasa del más alto


def test_pyramid_adjacent_levels_split_by_the_gap():
    j1 = {'id': 'j1', 'tier': 'junior1'}      # 25
    se = {'id': 'se', 'tier': 'senior'}        # 30
    ma = {'id': 'ma', 'tier': 'master'}        # 35
    b = pyramid.compute_commission_breakdown(10000, j1, [se, ma])
    amounts = {r['distributor_id']: r['amount'] for r in b}
    assert amounts == {'j1': 2500, 'se': 500, 'ma': 500}   # 25 + 5 + 5 = 35


def test_pyramid_skipped_level_is_absorbed_by_the_one_above():
    # junior0 (20) con un Master (35) directo arriba, sin nadie en medio:
    # el Master absorbe todo el hueco → 15%. Total 35.
    j = {'id': 'j', 'tier': 'junior0'}
    ma = {'id': 'ma', 'tier': 'master'}
    b = pyramid.compute_commission_breakdown(10000, j, [ma])
    amounts = {r['distributor_id']: r['amount'] for r in b}
    assert amounts == {'j': 2000, 'ma': 1500}              # 20 + 15 = 35
    assert pyramid.total_amount(b) == 3500


def test_pyramid_lower_upline_earns_nothing():
    # Un upline de MENOR nivel que alguien debajo no cobra (diferencial negativo).
    ma = {'id': 'ma', 'tier': 'master'}        # vende Master 35
    j = {'id': 'j', 'tier': 'junior0'}          # arriba un junior0 20 (raro pero posible)
    b = pyramid.compute_commission_breakdown(10000, ma, [j])
    assert [r['distributor_id'] for r in b] == ['ma']       # el junior0 no cobra
    assert pyramid.total_amount(b) == 3500


def test_pyramid_discount_comes_out_of_seller_slice_only():
    # Senior (30) da 15% de descuento: se queda 15; el Master arriba cobra su 5 intacto.
    se = {'id': 'se', 'tier': 'senior'}
    ma = {'id': 'ma', 'tier': 'master'}
    b = pyramid.compute_commission_breakdown(10000, se, [ma], discount_rate=0.15)
    amounts = {r['distributor_id']: r['amount'] for r in b}
    assert amounts == {'se': 1500, 'ma': 500}              # (30-15)=15 y 5 intacto
    # El cliente recibió 15% (1500); total que da el negocio sigue siendo 35%.
    assert 1500 + pyramid.total_amount(b) == 3500


def test_pyramid_discount_capped_at_sellers_rate():
    j = {'id': 'j', 'tier': 'junior0'}         # 20
    b = pyramid.compute_commission_breakdown(10000, j, [], discount_rate=0.50)
    assert b[0]['discount'] == 0.20 and b[0]['amount'] == 0   # no puede dar más de su 20


def test_pyramid_max_discount_equals_commission():
    assert pyramid.max_discount('elite') == 0.40
    assert pyramid.max_discount('junior0') == 0.20


def test_pyramid_never_pays_the_same_distributor_twice():
    j = {'id': 'j', 'tier': 'junior0'}
    b = pyramid.compute_commission_breakdown(10000, j, [j, {'id': 's', 'tier': 'senior'}])
    assert [r['distributor_id'] for r in b] == ['j', 's']


def test_pyramid_earnings_for_sums_seller_and_override_roles():
    orders = [
        {'status': 'entregado', 'commissions': [
            {'distributor_id': 'j', 'role': 'seller', 'amount': 2000},
            {'distributor_id': 's', 'role': 'override', 'amount': 1000}]},
        {'status': 'entregado', 'commissions': [
            {'distributor_id': 's', 'role': 'seller', 'amount': 3000}]},
        {'status': 'cancelado', 'commissions': [
            {'distributor_id': 's', 'role': 'seller', 'amount': 9999}]},
    ]
    assert pyramid.earnings_for('s', orders) == 1000 + 3000
    assert pyramid.earnings_for('j', orders) == 2000


def test_pyramid_earnings_for_reads_legacy_commission_field():
    orders = [{'status': 'entregado', 'referred_by': 'd1', 'commission': 500}]
    assert pyramid.earnings_for('d1', orders) == 500
    assert pyramid.earnings_for('otro', orders) == 0


def test_pyramid_zero_merchandise_pays_nothing():
    assert pyramid.compute_commission_breakdown(0, {'id': 'x', 'tier': 'elite'}, []) == []


def test_distributor_rollup_counts_override_earnings():
    ma = {'id': 'ma', 'name': 'M', 'email': 'm@x', 'tier': 'master'}
    orders = [
        {'referred_by': 'ma', 'status': 'entregado', 'total': 5000, 'commissions': [
            {'distributor_id': 'ma', 'role': 'seller', 'amount': 1500}]},
        {'referred_by': 'j', 'status': 'entregado', 'total': 10000, 'commissions': [
            {'distributor_id': 'j', 'role': 'seller', 'amount': 2000},
            {'distributor_id': 'ma', 'role': 'override', 'amount': 500}]},
    ]
    r = _distributor_rollup(ma, [], orders)
    assert r['sales_count'] == 1 and r['sales_total'] == 5000
    assert r['earnings'] == 2000          # 1500 propia + 500 sobrecomisión
    assert r['tier'] == 'master'


# ---------- Pirámide: barra de nivel (ventas + reclutas) ----------
def test_level_progress_two_bars_sales_and_recruits():
    lp = pyramid.level_progress('junior0', personal_sales=250000, team_sales=250000, active_recruits=1)
    assert lp['next'] == 'junior1' and lp['kind'] == 'promotion'
    assert lp['sales']['target'] == 500000 and abs(lp['sales']['progress'] - 0.5) < 1e-9
    assert lp['recruits']['target'] == 2 and lp['recruits']['value'] == 1
    assert lp['qualifies'] is False        # ventas a medias y falta 1 recluta


def test_level_progress_qualifies_when_both_met():
    lp = pyramid.level_progress('junior1', personal_sales=3000000, team_sales=3000000, active_recruits=4)
    assert lp['next'] == 'senior' and lp['qualifies'] is True


def test_level_progress_senior_uses_team_sales():
    lp = pyramid.level_progress('senior', personal_sales=0, team_sales=10000000, active_recruits=8)
    assert lp['sales']['basis'] == 'team' and lp['sales']['done'] is True
    assert lp['recruits']['target'] == 8 and lp['qualifies'] is True


def test_level_progress_elite_is_the_visible_top():
    # Diamond es SECRETO: para el distribuidor, Elite es el tope de la escalera.
    lp = pyramid.level_progress('elite', 0, 999999999, 999)
    assert lp['kind'] == 'top' and lp['next'] is None and lp['rate'] == 0.40


def test_level_progress_diamond_is_top():
    lp = pyramid.level_progress('diamond', 0, 0, 0)
    assert lp['kind'] == 'top' and lp['next'] is None and lp['rate'] == 0.43


def test_diamond_qualifies_needs_50m_and_more_than_32():
    assert pyramid.diamond_qualifies(50000000, 33) is True
    assert pyramid.diamond_qualifies(50000000, 32) is False    # debe ser MÁS de 32
    assert pyramid.diamond_qualifies(49999999, 40) is False    # y $50M de equipo
    assert pyramid.diamond_qualifies(60000000, 40) is True


# ---------- Códigos de descuento (opacos, no adivinables) ----------
from server import gen_discount_code


def test_discount_code_is_opaque_with_random_suffix():
    import re
    c = gen_discount_code('Maria Lopez', 0.25)
    assert re.fullmatch(r'MARIAL-25-[A-Z0-9]{4}', c), c   # PREFIJO-PCT-XXXX opaco
    parts = c.split('-')
    assert parts[0] == 'MARIAL' and parts[1] == '25' and len(parts[2]) == 4


def test_discount_codes_differ_each_time():
    a = gen_discount_code('Ana', 0.15)
    b = gen_discount_code('Ana', 0.15)
    assert a != b                            # el sufijo al azar los hace únicos


def test_discount_code_falls_back_prefix_when_no_letters():
    assert gen_discount_code('!!!', 0.20).startswith('DIST-20-')


# ---------- Niveles de descuento auto por comisión ----------
def test_discount_tiers_start_at_15_and_step_5_below_commission():
    assert pyramid.discount_tiers_for(0.20) == [0.15]                          # Junior 0
    assert pyramid.discount_tiers_for(0.25) == [0.15, 0.20]                    # Junior 1
    assert pyramid.discount_tiers_for(0.30) == [0.15, 0.20, 0.25]             # Senior
    assert pyramid.discount_tiers_for(0.35) == [0.15, 0.20, 0.25, 0.30]      # Master
    assert pyramid.discount_tiers_for(0.40) == [0.15, 0.20, 0.25, 0.30, 0.35]  # Elite


def test_discount_tiers_diamond_ends_at_38():
    assert pyramid.discount_tiers_for(0.43) == [0.15, 0.20, 0.25, 0.30, 0.35, 0.38]


# ---------- Centro de noticias: audiencia por rol ----------
from server import _audience_for_role


def test_notification_audience_by_role():
    assert _audience_for_role('user') == ['all', 'clients']
    assert _audience_for_role('distributor') == ['all', 'distributors']
    assert set(_audience_for_role('admin')) == {'all', 'clients', 'distributors'}


# ---------- Videos de tutoriales protegidos ----------
from server import tutorial_allowed, parse_range_header


def test_tutorial_videos_role_gating():
    dist_video = 'tutorial-2-mis-codigos.mp4'
    client_video = 'tutorial-9-calculadora.mp4'
    assert not tutorial_allowed(dist_video, 'client')      # cliente NO ve lo de distribuidor
    assert tutorial_allowed(dist_video, 'distributor')
    assert tutorial_allowed(dist_video, 'admin')
    for role in ('client', 'distributor', 'admin'):
        assert tutorial_allowed(client_video, role)        # lo de cliente lo ven todos


def test_parse_range_header_variants():
    assert parse_range_header('bytes=0-99', 1000) == (0, 99)
    assert parse_range_header('bytes=200-', 1000) == (200, 999)     # abierto al final
    assert parse_range_header('bytes=-100', 1000) == (900, 999)     # sufijo (ultimos N)
    assert parse_range_header('bytes=0-5000', 1000) == (0, 999)     # se recorta al tamano
    assert parse_range_header(None, 1000) is None                   # sin header → archivo completo
    assert parse_range_header('bytes=900-100', 1000) is None        # rango invertido
    assert parse_range_header('bytes=1000-', 1000) is None          # fuera de rango
    assert parse_range_header('chars=0-99', 1000) is None           # unidad desconocida


# ---------- Tope de comisión por producto (regla 2026-07-23) ----------
from pyramid import cap_breakdown


def test_cap_breakdown_scales_when_over_cap():
    rows = [
        {'distributor_id': 'a', 'role': 'seller', 'amount': 300},
        {'distributor_id': 'b', 'role': 'upline', 'amount': 100},
    ]
    # tope 20% de $1,000 = $200; se pedían $400 → todos a la mitad
    out = cap_breakdown(rows, 1000, 0.20)
    assert sum(r['amount'] for r in out) == 200
    assert out[0]['amount'] == 150 and out[1]['amount'] == 50   # prorrata
    assert all(r.get('capped') for r in out)


def test_cap_breakdown_leaves_room_untouched():
    rows = [{'distributor_id': 'a', 'role': 'seller', 'amount': 100}]
    out = cap_breakdown(rows, 1000, 0.20)                        # tope $200, pedían $100
    assert out[0]['amount'] == 100 and 'capped' not in out[0]    # no se toca


def test_cap_breakdown_zero_cap_pays_nothing():
    rows = [{'distributor_id': 'a', 'role': 'seller', 'amount': 100}]
    out = cap_breakdown(rows, 1000, 0.0)
    assert sum(r['amount'] for r in out) == 0
