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


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


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
    dist = {'id': 'd1', 'name': 'Ana', 'email': 'a@x.com', 'commission_rate': 0.25}
    users = [{'id': 'u1', 'referred_by': 'd1'}, {'id': 'u2', 'referred_by': 'otro'}]
    orders = [
        {'user_id': 'u1', 'status': 'entregado', 'total': 1000, 'commission': 250},
        {'user_id': 'u1', 'status': 'cancelado', 'total': 5000, 'commission': 1250},
        {'user_id': 'u2', 'status': 'entregado', 'total': 9000, 'commission': 2250},
    ]
    r = _distributor_rollup(dist, users, orders)
    assert r['clients_count'] == 1
    assert r['sales_count'] == 1
    assert r['sales_total'] == 1000        # la cancelada no cuenta
    assert r['earnings'] == 250            # la del cliente ajeno tampoco


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
    # 5% de lo pagado en mercancia, entero hacia abajo.
    assert loyalty.earn(1070, True) == 53
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
