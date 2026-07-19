"""Pruebas de la logica de negocio del API de Exygen Labs.

No tocan la base de datos ni la red: solo ejercitan las funciones puras de
`server.py`, que es donde vive la aritmetica que le duele al negocio si se
rompe (descuentos, comision, viales restantes, rastreo de envio).

Correr:  pytest test_core.py -q
"""
import os

# database.py exige MONGO_URL al importar. El cliente de motor es perezoso:
# no abre conexion hasta la primera consulta, asi que basta con declararla.
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

from datetime import datetime, timedelta, timezone

from server import (
    ORDER_NUMBER_RE, SHIPPING_INTENT_RE, STATUS_LABEL,
    build_tracking_url, _order_summary_line, _protocol_projection,
    gen_order_number, gen_distributor_code, _distributor_rollup,
    REPURCHASE_WARN_DAYS,
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
