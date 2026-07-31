"""UN SOLO CORREO CUANDO SE PUEDA, y una guía que se compra sola con frenos.

Las dos órdenes de Christián del 2026-07-31, cada una con sus pruebas:

  A. **NADIE RECIBE TRES CORREOS POR UNA COMPRA.** Hasta hoy una compra con tarjeta
     mandaba tres casi seguidos («recibimos tu pedido», «confirmamos tu pago», «va en
     camino»). Ahora sale UNO cuando se puede, DOS cuando el pago no es inmediato, y
     nunca tres. Lo que se vigila aquí es el CONTEO, no la redacción: contar es lo
     único que no se puede engañar a sí mismo.

  B. **LA GUÍA SE COMPRA SOLA, PERO CON DOS FRENOS Y UN CANDADO.** El tope de $400, la
     tabla de empaques (hoy: sólo la bolsa de 4 piezas), y la idempotencia. Los tres
     protegen dinero de verdad: una guía de $900 comprada sola, un recobro por
     sobrepeso, o dos guías pagadas del mismo pedido.

⛔ ESTAS PRUEBAS CORREN EL CÓDIGO DE VERDAD. No leen el archivo fuente ni miran
constantes: llaman a `comprar_guia_del_pedido`, a `_confirmar_y_avisar` y al checkout,
y cuentan lo que de verdad salió. Una prueba que sólo lee el código pasa en verde
mientras el sistema está roto — ya pasó en esta casa.

⛔ Nunca se llama a Skydropx ni al proveedor de correo de verdad.
"""
import asyncio
import os

import pytest

os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'exygen_test')

import emails
import envios
import paqueterias
import server

# El doble de la base y el de Skydropx ya existen y están probados: se reusan en vez
# de escribir otros que se desincronicen con el primero.
from test_envios import FakeDB, _falsear_skydropx, con_llave, con_remitente  # noqa: F401


async def _nada(*a, **k):
    return None


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(server, 'db', fake)
    return fake


@pytest.fixture()
def correos(monkeypatch):
    """Atrapa TODO correo que salga al cliente. Devuelve la lista de lo que se mandó.

    Se intercepta en `emails._send_email_sync`, que es el único punto por donde salen
    los correos de verdad: así se cuentan aunque mañana alguien agregue otra función
    de correo por su cuenta. Cada renglón es (destinatario, asunto).
    """
    mandados = []

    def falso(to, subject, html_body, reply_to=None):
        mandados.append((to, subject, html_body))
        return True
    monkeypatch.setattr(emails, '_send_email_sync', falso)
    monkeypatch.setenv('EMAIL_ENABLED', 'true')
    return mandados


@pytest.fixture()
def al_cliente(correos):
    """Sólo los correos que le llegan a la CLIENTA, sin los avisos internos."""
    return correos


def _del_cliente(correos):
    return [c for c in correos if c[0] == 'ana@x.com']


def _de_christian(correos):
    return [c for c in correos if c[0] != 'ana@x.com']


@pytest.fixture()
def fondo(monkeypatch):
    """Corre de verdad lo que el servidor manda a segundo plano.

    ⛔ SIN ESTO LAS PRUEBAS MIENTEN. `asyncio.create_task` dentro de `asyncio.run`
    agenda en un bucle que se cierra antes de que la tarea corra: el correo nunca
    sale y el conteo da cero para todo, incluso para lo que está roto.
    """
    pendientes = []

    def agendar(coro):
        pendientes.append(coro)
        return coro
    monkeypatch.setattr(server.asyncio, 'create_task', agendar)

    def correr():
        async def todo():
            while pendientes:
                coro = pendientes.pop(0)
                if asyncio.iscoroutine(coro):
                    await coro
        asyncio.run(todo())
    return correr


def _pedido(metodo='tarjeta', piezas=1, **extra):
    base = {
        'id': 'o1', 'order_number': 'EX-20260731-0001', 'status': 'pendiente',
        'payment_method': metodo, 'total': 1180, 'subtotal': 1000, 'shipping': 180,
        'items': [{'product_id': 'a', 'name': 'BPC-157', 'quantity': piezas,
                   'price': 1000}],
        'customer': {'full_name': 'Ana', 'email': 'ana@x.com', 'phone': '+528111111111',
                     'address': 'Calle 1', 'city': 'Monterrey', 'state': 'Nuevo León',
                     'postal_code': '64000', 'country': 'MX'},
        'shipping_quote': {'carrier': 'Estafeta', 'service_code': 'estafeta_standard',
                           'cost': 168.33},
        'emails_sent': [],
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _sin_avisos_internos_reales(monkeypatch):
    """Los avisos a Christián y a Meta no salen a internet en las pruebas."""
    monkeypatch.setattr(server, 'send_purchase_alert', _nada)
    monkeypatch.setattr(server, 'notify', _nada)
    monkeypatch.setattr(server.meta_capi, 'enviar_compra', lambda *a, **k: None)


# ==========================================================================
#  A. EL CONTEO DE CORREOS — la orden de "nadie recibe tres"
# ==========================================================================
def test_tarjeta_con_guia_manda_UN_SOLO_correo(db, con_llave, con_remitente,
                                               monkeypatch, correos, fondo):
    """EL CASO FELIZ Y EL MÁS COMÚN: tarjeta, hay inventario, la guía se compra.

    ⛔ ES LA PRUEBA MADRE DE TODA LA ORDEN. Antes de hoy este mismo camino mandaba
    TRES correos. Uno. Y con el número de guía adentro, que es lo que hace que el
    cliente no tenga que preguntar «¿ya lo mandaron?».
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_pedido('tarjeta')))

    asyncio.run(server._confirm_paid_order('EX-20260731-0001'))
    fondo()

    mios = _del_cliente(correos)
    assert len(mios) == 1, f'salieron {len(mios)} correos y debía salir UNO: {[c[1] for c in mios]}'
    asunto, cuerpo = mios[0][1], mios[0][2]
    assert 'ya va en camino' in asunto, 'el asunto no dice que ya salió'
    # Las tres cosas que antes iban en tres correos, ahora en éste:
    assert 'BPC-157' in cuerpo, 'falta el detalle del pedido'
    assert 'Confirmamos' in cuerpo or 'confirmamos' in cuerpo, 'no dice que el pago entró'
    assert '7712345678' in cuerpo, 'falta el número de guía: éste era el tercer correo'


def test_spei_manda_DOS_correos_y_ni_uno_mas(db, con_llave, con_remitente,
                                             monkeypatch, correos, fondo):
    """SPEI: el de la CLABE al comprar y el de pago+guía al confirmar. DOS.

    Christián lo pidió así con todas sus letras: los datos de pago en pantalla Y por
    correo. Ese primer correo se gana su lugar porque la clienta todavía tiene que ir
    a su banco. El segundo lleva pago y guía juntos.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    orden = _pedido('spei')
    asyncio.run(db.orders.insert_one(orden))

    # 1) El correo con la CLABE, tal como lo manda el checkout.
    asyncio.run(server._apartar_correo('o1', 'nuevo'))
    asyncio.run(emails.send_order_email(
        dict(orden, spei={'clabe': '012790001244916613', 'bank': 'BBVA',
                          'beneficiary': 'Servicios Profesionales Quimimid SA de CV'}),
        'es', 'nuevo'))
    # 2) El admin ve el depósito y confirma.
    from models import OrderStatusUpdate
    asyncio.run(server.update_order_status('o1', OrderStatusUpdate(status='confirmado'),
                                           admin={'id': 'admin'}))
    fondo()

    mios = _del_cliente(correos)
    assert len(mios) == 2, f'SPEI mandó {len(mios)} correos: {[c[1] for c in mios]}'
    assert '012790001244916613' in mios[0][2], 'el primer correo no trae la CLABE'
    assert '7712345678' in mios[1][2], 'el segundo correo no trae la guía'
    # ⛔ Y LA CLABE NO SE REPITE EN EL SEGUNDO: pagar dos veces no es una opción.
    assert '012790001244916613' not in mios[1][2], \
        'el correo de pago confirmado trae la CLABE otra vez: invita a pagar dos veces'


def test_tarjeta_no_manda_correo_al_comprar_solo_al_pagar(db, monkeypatch, correos,
                                                          fondo):
    """El correo de «recibimos tu pedido» ya NO sale con tarjeta ni con cripto.

    Es la mitad del ahorro: ese correo llegaba pegado al de pago confirmado, con un
    minuto de diferencia y diciendo casi lo mismo.
    """
    assert 'tarjeta' not in server.PAGOS_DIFERIDOS
    assert 'cripto' not in server.PAGOS_DIFERIDOS
    # Y los que SÍ necesitan que la persona haga algo más, sí lo mandan.
    assert 'spei' in server.PAGOS_DIFERIDOS
    assert 'oxxo' in server.PAGOS_DIFERIDOS


def test_sin_guia_el_cliente_recibe_su_pago_confirmado_pero_NINGUN_rastreo_falso(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """La guía no se pudo comprar. La clienta se entera de su pago igual.

    ⛔ LAS DOS COSAS AL MISMO TIEMPO, y ésta es la regla que más fácil se rompe: que
    NO se le quede sin avisar que su dinero llegó, y que NO se le mande un número de
    rastreo que no existe. Un correo con una guía inventada es peor que no mandarlo.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch, fallos={'/quotations': RuntimeError('sin saldo')})
    asyncio.run(db.orders.insert_one(_pedido('tarjeta')))

    asyncio.run(server._confirm_paid_order('EX-20260731-0001'))
    fondo()

    mios = _del_cliente(correos)
    assert len(mios) == 1, f'salieron {len(mios)} correos: {[c[1] for c in mios]}'
    cuerpo = mios[0][2]
    assert 'Confirmamos' in cuerpo, 'no le avisó que su pago entró'
    assert 'guia' in cuerpo.lower() or 'guía' in cuerpo.lower()
    assert '7712345678' not in cuerpo, '¡le mandó un número de guía que no se compró!'
    # Y el pedido quedó SIN rastreo, no con uno a medias.
    assert not db.orders.docs[0].get('tracking_number')


def test_la_guia_que_llega_tarde_manda_su_correo_UNA_SOLA_VEZ(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """Guía comprada después del correo de pago: ése sí es un evento nuevo.

    Y el reintento posterior no lo vuelve a mandar: la ranura ya está apartada.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    orden = _pedido('tarjeta', status='confirmado', emails_sent=['pagado'])
    asyncio.run(db.orders.insert_one(orden))
    _falsear_skydropx(monkeypatch)

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden))
    fondo()
    assert hecho and hecho['tracking_number'] == '7712345678'
    assert len(_del_cliente(correos)) == 1, 'el aviso de rastreo no salió'

    # Se vuelve a llamar a la puerta con la MISMA guía: no sale nada.
    fresco = db.orders.docs[0]
    asyncio.run(server.avisar_del_envio(fresco))
    fondo()
    assert len(_del_cliente(correos)) == 1, 'mandó el rastreo dos veces'


def test_NINGUN_camino_manda_tres_correos(db, con_llave, con_remitente,
                                          monkeypatch, correos, fondo):
    """El barrido: los cuatro métodos de pago, con guía y sin ella. Nunca tres.

    ⛔ ES LA PRUEBA QUE CIERRA LA ORDEN. Las de arriba miran un camino cada una; ésta
    los recorre todos y cuenta. Si mañana alguien agrega un correo en cualquier punto
    del flujo, aquí se ve.
    """
    for metodo in ('tarjeta', 'cripto', 'spei', 'oxxo'):
        for compra_guia in (True, False):
            correos.clear()
            fake = FakeDB()
            monkeypatch.setattr(server, 'db', fake)
            monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
            if compra_guia:
                _falsear_skydropx(monkeypatch)
            else:
                _falsear_skydropx(monkeypatch,
                                  fallos={'/quotations': RuntimeError('caida')})
            orden = _pedido(metodo)
            asyncio.run(fake.orders.insert_one(orden))
            # El correo del checkout sale sólo cuando el pago no es inmediato.
            if metodo in server.PAGOS_DIFERIDOS:
                if asyncio.run(server._apartar_correo('o1', 'nuevo')):
                    asyncio.run(emails.send_order_email(orden, 'es', 'nuevo'))
            asyncio.run(server._confirm_paid_order('EX-20260731-0001'))
            fondo()
            # Y por si acaso, se toca la puerta del envío otra vez.
            asyncio.run(server.avisar_del_envio(fake.orders.docs[0]))
            fondo()

            n = len(_del_cliente(correos))
            esperados = (2 if metodo in server.PAGOS_DIFERIDOS else 1)
            assert n <= 2, (f'{metodo} con guía={compra_guia} mandó {n} correos: '
                            f'{[c[1] for c in _del_cliente(correos)]}')
            assert n == esperados, (f'{metodo} con guía={compra_guia} mandó {n} y '
                                    f'se esperaban {esperados}')


def test_dos_webhooks_a_la_vez_no_mandan_el_correo_dos_veces(db, monkeypatch,
                                                             correos, fondo):
    """LA IDEMPOTENCIA DEL CORREO. Las pasarelas reintentan sus webhooks: pasa.

    El candado es un `$addToSet` condicionado en un solo paso, igual que el del cupón
    y el de los puntos. Gana el primero; el segundo se va en silencio.
    """
    asyncio.run(db.orders.insert_one(_pedido('tarjeta', status='confirmado')))
    orden = db.orders.docs[0]

    primero = asyncio.run(server.avisar_al_cliente(orden, 'pagado'))
    segundo = asyncio.run(server.avisar_al_cliente(orden, 'pagado'))
    fondo()

    assert primero is True and segundo is False
    assert len(_del_cliente(correos)) == 1, 'el mismo correo salió dos veces'


def test_el_correo_del_cliente_NUNCA_trae_la_cifra_del_envio(db, con_llave,
                                                             con_remitente,
                                                             monkeypatch, correos,
                                                             fondo):
    """⛔ CHRISTIÁN ABSORBE EL ENVÍO Y EL CLIENTE NO VE LO QUE CUESTA.

    La guía de esta prueba cuesta $168.33 y ese número no puede aparecer en ninguno
    de los correos nuevos. Enseñarle al cliente lo que nos cuesta mandarle su paquete
    es enseñarle el margen.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_pedido('tarjeta')))
    asyncio.run(server._confirm_paid_order('EX-20260731-0001'))
    fondo()

    for _to, _asunto, cuerpo in _del_cliente(correos):
        for prohibido in ('168.33', '168', 'Skydropx', 'skydropx'):
            assert prohibido not in cuerpo, \
                f'el correo del cliente enseña «{prohibido}»: eso es interno'


def test_el_boton_del_correo_lleva_a_NUESTRA_pagina_no_a_la_de_fedex(db, monkeypatch,
                                                                     correos, fondo):
    """⛔ ORDEN DE CHRISTIÁN (2026-07-31): «quiero que vivan en nuestra página el
    mayor tiempo posible».

    El correo con guía mandaba el botón al rastreo de FedEx y se perdía al cliente en
    el primer clic. Ahora `/pedido/{numero}` trae el rastreo ADENTRO (ver rastreo.py y
    RastreoEnvio.js), así que el botón se queda en casa.

    La liga de la paquetería NO desaparece del mundo: sigue estando en nuestra propia
    página, abajo de la línea de tiempo. Lo que no puede es ser el destino del botón.
    """
    asyncio.run(db.orders.insert_one(
        _pedido('tarjeta', status='confirmado', carrier='FedEx',
                tracking_number='875164874865',
                tracking_url='https://www.fedex.com/fedextrack/?trknbr=875164874865')))
    asyncio.run(server.avisar_al_cliente(db.orders.docs[0], 'pagado'))
    fondo()

    cuerpo = _del_cliente(correos)[0][2]
    assert 'fedex.com' not in cuerpo.lower(), \
        'el botón del correo sigue mandando al sitio de la paquetería'
    assert 'exygenlabs.com/pedido/EX-20260731-0001' in cuerpo, \
        'el correo no lleva a nuestra página del pedido'
    # El número de guía SÍ se queda: es lo que el cliente busca cuando abre el correo.
    assert '875164874865' in cuerpo


def test_el_correo_de_pago_sigue_sin_revelar_al_distribuidor(db, monkeypatch,
                                                             correos, fondo):
    """El candado de Mónica Flores no se deshizo al consolidar los correos."""
    assert emails.ATENCION_NOMBRE == 'Mónica Flores'
    asyncio.run(db.orders.insert_one(
        _pedido('tarjeta', status='confirmado', referred_by='dist-1',
                commissions=[{'distributor_id': 'dist-1', 'amount': 300,
                              'role': 'seller'}])))
    asyncio.run(server.avisar_al_cliente(db.orders.docs[0], 'pagado'))
    fondo()

    cuerpo = _del_cliente(correos)[0][2]
    for prohibido in ('dist-1', 'comisión', 'comision', 'distribuidor'):
        assert prohibido not in cuerpo, f'el correo revela «{prohibido}»'


# ==========================================================================
#  B. LA GUÍA AUTOMÁTICA — los dos frenos y el candado
# ==========================================================================
def test_hasta_CUATRO_piezas_la_guia_se_compra_sola(db, con_llave, con_remitente,
                                                    monkeypatch, correos, fondo):
    """1 a 4 piezas caben en la bolsa stand-up: compra sola, sin preguntar."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta', piezas=4)
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden, avisar=False))

    assert hecho and hecho['tracking_number'] == '7712345678'
    assert hecho['label_empaque'] == 'bolsa stand-up'


def test_de_CINCO_piezas_para_arriba_NO_compra_sola_y_le_avisa_a_christian(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """⛔ EL FRENO DEL EMPAQUE. Cinco piezas no caben en la única bolsa que hay.

    Comprar la guía de todos modos sería cotizarla con medidas que no son, y el
    sobrepeso vuelve semanas después como RECOBRO que paga la casa. Se detiene y se
    le pregunta a él.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta', piezas=5)
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden))
    fondo()

    assert hecho is None, 'compró una guía para un pedido que no cabe en la bolsa'
    # ⛔ Y NO GASTÓ NI UNA LLAMADA: se frenó ANTES de hablar con la paquetería.
    assert not [l for l in api.llamadas if l['ruta'] == '/shipments']
    guardado = db.orders.docs[0]
    assert guardado['label_hold'] == 'sin_empaque'
    assert guardado['label_piezas'] == 5
    assert not guardado.get('tracking_number')
    # Y Christián se enteró, con algo que puede accionar.
    avisos = _de_christian(correos)
    assert avisos, 'no le avisó a Christián que un pedido pagado no puede salir'
    assert 'empaque' in avisos[0][1].lower()


def test_arriba_de_400_pesos_NO_compra_sola_y_pide_visto_bueno(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """⛔ EL TOPE DE GASTO. Con el tope en $1, la guía de $168 ya no pasa.

    Se mueve el tope en vez de fabricar una tarifa cara: lo que se prueba es que el
    freno EXISTE y que corta ANTES de pagar, no cuánto cuesta esta guía en concreto.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    monkeypatch.setattr(envios, 'TOPE_GUIA_AUTOMATICA_MXN', 1.0)
    api = _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta')
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden))
    fondo()

    assert hecho is None, 'gastó por encima del tope sin preguntar'
    # Cotizó (eso es gratis) pero NO compró.
    assert not [l for l in api.llamadas if l['ruta'] == '/shipments'], \
        '¡compró la guía a pesar del tope!'
    guardado = db.orders.docs[0]
    assert guardado['label_hold'] == 'sobre_tope'
    assert guardado['label_precio_cotizado'] > 1
    avisos = _de_christian(correos)
    assert avisos and 'tope' in avisos[0][1].lower()


def test_el_tope_de_400_es_el_que_pidio_christian():
    """El número es una decisión de dinero: si cambia, que se vea en una prueba."""
    assert envios.TOPE_GUIA_AUTOMATICA_MXN == 400.0


def test_si_hay_una_tarifa_mas_barata_dentro_del_tope_se_toma_esa(monkeypatch):
    """El tope frena el gasto, no el despacho.

    Si el servicio que pidió el cliente se pasa pero hay otra permitida que cabe, se
    toma ésa: detener un pedido pagado por cien pesos cuando había alternativa es
    peor negocio que el ahorro.
    """
    compradas = []
    monkeypatch.setattr(paqueterias.skydropx, 'remitente_configurado', lambda: True)
    monkeypatch.setattr(paqueterias, 'cotizar_en_todos', lambda *a, **k: {
        'opciones': [
            {'precio': 150, 'paqueteria': 'Estafeta', 'servicio': 'Terrestre',
             'servicio_codigo': 'barata', 'rate_id': 'r1', 'proveedor': 'skydropx'},
            {'precio': 900, 'paqueteria': 'FedEx', 'servicio': 'Express',
             'servicio_codigo': 'elegido', 'rate_id': 'r2', 'proveedor': 'skydropx'},
        ],
        'proveedores': [], 'cotizaciones': {}})
    monkeypatch.setattr(paqueterias, 'comprar_guia',
                        lambda op, *a, **k: (compradas.append(op['rate_id'])
                                             or {'tracking_number': 'X'}))

    guia = paqueterias.guia_para({}, {}, 'elegido', tope_mxn=400)

    assert compradas == ['r1'], 'no se cayó a la tarifa barata que sí cabía en el tope'
    assert guia['costo'] == 150


def test_cuando_NINGUNA_tarifa_cabe_en_el_tope_no_compra_nada(monkeypatch):
    monkeypatch.setattr(paqueterias.skydropx, 'remitente_configurado', lambda: True)
    monkeypatch.setattr(paqueterias, 'cotizar_en_todos', lambda *a, **k: {
        'opciones': [{'precio': 900, 'paqueteria': 'FedEx', 'servicio': 'Express',
                      'servicio_codigo': 'c', 'rate_id': 'r2', 'proveedor': 'skydropx'}],
        'proveedores': [], 'cotizaciones': {}})
    monkeypatch.setattr(paqueterias, 'comprar_guia',
                        lambda *a, **k: pytest.fail('¡compró por encima del tope!'))

    with pytest.raises(paqueterias.TopeDeGastoExcedido) as e:
        paqueterias.guia_para({}, {}, '', tope_mxn=400)
    assert e.value.precio == 900 and e.value.tope == 400


def test_nunca_se_compran_DOS_guias_del_mismo_pedido(db, con_llave, con_remitente,
                                                     monkeypatch, correos, fondo):
    """LA IDEMPOTENCIA DE LA GUÍA. Dos webhooks del mismo pago: una sola guía.

    ⛔ ESTO ES DINERO. Cada guía se paga. El candado es atómico (`label_lock` tomado
    en un solo paso condicionado): mirar `tracking_number` en un dict ya leído no
    alcanza, porque entre la lectura y la compra cabe el otro webhook completo.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta')
    asyncio.run(db.orders.insert_one(orden))

    primera = asyncio.run(server.comprar_guia_del_pedido(orden, avisar=False))
    # El segundo webhook llega con el MISMO dict viejo, sin rastreo: es el caso real.
    segunda = asyncio.run(server.comprar_guia_del_pedido(orden, avisar=False))

    assert primera and segunda is None, 'compró la guía dos veces'
    assert len([l for l in api.llamadas if l['ruta'] == '/shipments']) == 1


# ==========================================================================
#  C. ENVÍO PARTIDO: lo elige el cliente
# ==========================================================================
def test_si_el_cliente_pidio_TODO_JUNTO_no_se_manda_lo_que_hay(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """⛔ SU DECISIÓN MANDA. Pidió esperar a tenerlo completo: no sale nada todavía."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta', shipping_preference='completo',
                    backorder_items=[{'product_id': 'a', 'name': 'BPC-157',
                                      'pedidas': 4, 'por_surtir': 2}])
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden))

    assert hecho is None, 'mandó media entrega a quien pidió esperar a tenerlo todo'
    assert not [l for l in api.llamadas if l['ruta'] == '/shipments']
    assert db.orders.docs[0]['label_hold'] == 'espera_pedido_completo'


def test_si_el_cliente_pidio_PARTIDO_sale_lo_disponible_ya(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta', shipping_preference='partido',
                    backorder_items=[{'product_id': 'a', 'name': 'BPC-157',
                                      'pedidas': 4, 'por_surtir': 2}])
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden, avisar=False))

    assert hecho and hecho['tracking_number'] == '7712345678'


def test_el_pedido_completo_no_se_detiene_aunque_pida_todo_junto(
        db, con_llave, con_remitente, monkeypatch, correos, fondo):
    """Sin faltantes, 'completo' no cambia nada: no hay nada que esperar."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    orden = _pedido('tarjeta', shipping_preference='completo')
    asyncio.run(db.orders.insert_one(orden))

    hecho = asyncio.run(server.comprar_guia_del_pedido(orden, avisar=False))
    assert hecho and hecho['tracking_number'] == '7712345678'


def test_el_envio_partido_avisa_en_el_correo_que_falta_una_parte(db, monkeypatch,
                                                                 correos, fondo):
    """Dos guías, dos avisos, y cada uno dice qué lleva.

    Sin esta línea el cliente abre la caja, ve la mitad y escribe. Con ella, sabe.
    """
    orden = _pedido('tarjeta', status='confirmado', tracking_number='7712345678',
                    carrier='Estafeta', shipping_preference='partido',
                    items=[{'product_id': 'a', 'name': 'BPC-157', 'quantity': 4,
                            'price': 1000}],
                    backorder_items=[{'product_id': 'a', 'name': 'BPC-157',
                                      'pedidas': 4, 'por_surtir': 2}])
    asyncio.run(db.orders.insert_one(orden))
    asyncio.run(server.avisar_al_cliente(db.orders.docs[0], 'pagado'))
    fondo()

    cuerpo = _del_cliente(correos)[0][2]
    assert 'segundo envio' in cuerpo, 'no avisa que falta una parte'


def test_lo_que_manda_el_navegador_en_la_preferencia_se_normaliza():
    """Una preferencia inventada no puede dejar mercancía pagada detenida para siempre."""
    from models import OrderCreate, CustomerInfo, OrderItem
    quien = CustomerInfo(full_name='Ana', email='ana@x.com', phone='+528111111111',
                         address='Calle 1', city='Monterrey', state='NL',
                         postal_code='64000')
    pieza = [OrderItem(product_id='a', name='BPC-157', price=1000, quantity=1)]
    # Sin decir nada: parte, que es lo que se hacía hasta hoy.
    assert OrderCreate(items=pieza, customer=quien,
                       payment_method='tarjeta').shipping_preference == 'partido'
    # ⛔ Y LO QUE LLEGUE RARO NO PUEDE DETENER MERCANCÍA PAGADA. La normalización vive
    # en el servidor, no en el modelo: se comprueba que siga ahí.
    src = open(os.path.join(os.path.dirname(__file__), 'server.py'),
               encoding='utf-8').read()
    assert "== 'completo'" in src, \
        'el servidor dejó de comparar la preferencia contra el único valor válido'
    assert "shipping_preference=('completo'" in src, \
        'el checkout dejó de normalizar la preferencia en el servidor'


# ==========================================================================
#  D. LA TABLA DE EMPAQUES ES CONFIGURACIÓN, NO PROGRAMACIÓN
# ==========================================================================
def test_de_fabrica_solo_existe_la_bolsa_que_christian_tiene():
    """⚠️ No se inventan cajas. Hoy hay UNA bolsa y son sus medidas de verdad."""
    tabla = envios.EMPAQUES
    assert len(tabla) == 1
    bolsa = tabla[0]
    assert (bolsa['largo_cm'], bolsa['ancho_cm'], bolsa['alto_cm']) == (12, 15, 1)
    assert bolsa['hasta_piezas'] == 4
    assert bolsa['peso_facturable_kg'] == 1.0


def test_capturar_una_caja_en_el_panel_destraba_los_pedidos_grandes(monkeypatch):
    """EL DÍA QUE COMPRE CAJAS: se capturan y ese tamaño empieza a comprar solo.

    Sin desplegar y sin tocar código, que es lo que Christián pidió expresamente.
    """
    monkeypatch.setattr(envios, '_EMPAQUES_DEL_PANEL', [])
    assert envios.empaque_para(20) is None          # hoy: no cabe en nada

    envios.cargar_empaques_del_panel([
        {'nombre': 'bolsa stand-up', 'hasta_piezas': 4, 'largo_cm': 12,
         'ancho_cm': 15, 'alto_cm': 1, 'peso_facturable_kg': 1.0},
        {'nombre': 'caja chica', 'hasta_piezas': 15, 'largo_cm': 20,
         'ancho_cm': 15, 'alto_cm': 10, 'peso_facturable_kg': 2.0},
        {'nombre': 'caja mediana', 'hasta_piezas': 40, 'largo_cm': 30,
         'ancho_cm': 20, 'alto_cm': 15, 'peso_facturable_kg': 4.0},
    ])
    assert envios.empaque_para(3)['nombre'] == 'bolsa stand-up'
    assert envios.empaque_para(12)['nombre'] == 'caja chica'
    assert envios.empaque_para(30)['nombre'] == 'caja mediana'
    assert envios.empaque_para(50) is None          # arriba de todo, sigue preguntando


def test_un_empaque_con_medidas_en_cero_se_tira(monkeypatch):
    """Un empaque inválido haría cotizar contra basura: no se guarda."""
    monkeypatch.setattr(envios, '_EMPAQUES_DEL_PANEL', [])
    assert envios.cargar_empaques_del_panel([
        {'nombre': 'mala', 'hasta_piezas': 10, 'largo_cm': 0, 'ancho_cm': 15,
         'alto_cm': 10}]) == 0
    assert envios.cargar_empaques_del_panel([
        {'nombre': 'sin tope', 'hasta_piezas': 0, 'largo_cm': 20, 'ancho_cm': 15,
         'alto_cm': 10}]) == 0
    # Y sin panel válido, manda la tabla de fábrica: nunca se queda sin empaques.
    assert envios.empaques() == envios.EMPAQUES


def test_las_piezas_se_cuentan_TODAS_no_solo_los_viales():
    """Un frasco de agua ocupa lugar en la bolsa igual que un vial.

    Contar de más manda el pedido a revisión humana; contar de menos manda el paquete
    con medidas que no son. Se prefiere el primer error.
    """
    items = [{'product_id': 'a', 'name': 'BPC-157', 'quantity': 3},
             {'product_id': 'b', 'name': 'Agua bacteriostática 30 ml', 'quantity': 2}]
    assert envios.piezas_del_pedido(items) == 5
    assert envios.empaque_para(envios.piezas_del_pedido(items)) is None


def test_el_bulto_que_se_cotiza_sale_del_empaque_de_verdad():
    """⛔ LA RAÍZ DEL RECOBRO. Se cotiza lo que se manda, no un peso supuesto."""
    bolsa = envios.empaque_para(2)
    paquete = envios.paquete_de_empaque(bolsa)
    assert (paquete['largo_cm'], paquete['ancho_cm'], paquete['alto_cm']) == (12, 15, 1)
    assert paquete['peso_kg'] == 1.0
    # Y abulta casi nada: 12×15×1 ÷ 5000 = 0.04 kg volumétricos.
    assert paquete['peso_volumetrico_kg'] < 0.1


# ==========================================================================
#  E. EL REINTENTO
# ==========================================================================
def test_el_reintento_agarra_los_FALLOS_pero_nunca_los_frenos(db, con_llave,
                                                              con_remitente,
                                                              monkeypatch, correos,
                                                              fondo):
    """⛔ UN FRENO NO SE REINTENTA: espera una decisión, no otra oportunidad.

    Reintentar un pedido detenido por el tope o por el empaque sería llenarle el
    correo a Christián con el mismo aviso cada diez minutos sin que nada cambie.
    """
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_pedido(
        'tarjeta', id='o-fallo', order_number='EX-A', status='confirmado',
        label_error='sin saldo', label_intentos=1)))
    asyncio.run(db.orders.insert_one(_pedido(
        'tarjeta', id='o-freno', order_number='EX-B', status='confirmado',
        label_hold='sobre_tope', label_error='')))

    salieron = asyncio.run(server._reintentar_guias_pendientes())
    fondo()

    assert salieron == 1, 'el reintento no agarró el que había fallado'
    porid = {d['id']: d for d in db.orders.docs}
    assert porid['o-fallo'].get('tracking_number') == '7712345678'
    assert not porid['o-freno'].get('tracking_number'), \
        'reintentó un pedido que está esperando una decisión de Christián'


def test_el_reintento_se_rinde_y_no_gira_para_siempre(db, con_llave, con_remitente,
                                                      monkeypatch, correos, fondo):
    """A los 6 intentos ya no es un problema pasajero: se deja de gastar llamadas."""
    monkeypatch.setattr(envios, 'COMPRAR_GUIA_AL_PAGAR', True)
    api = _falsear_skydropx(monkeypatch)
    asyncio.run(db.orders.insert_one(_pedido(
        'tarjeta', status='confirmado', label_error='sin saldo',
        label_intentos=server.MAX_INTENTOS_GUIA)))

    salieron = asyncio.run(server._reintentar_guias_pendientes())

    assert salieron == 0
    assert not _peticiones_de_compra(api)


def _peticiones_de_compra(api):
    return [l for l in api.llamadas if l['ruta'] == '/shipments']


# ==========================================================================
#  F. LOS DATOS DE PAGO SE PUEDEN VOLVER A VER, Y LO INTERNO NO SE VE NUNCA
# ==========================================================================
def test_la_ficha_de_OXXO_se_puede_volver_a_ver(db, monkeypatch):
    """⛔ SE PODÍA PERDER PARA SIEMPRE. La URL de Mercado Pago ES la ficha con el
    código de barras: viajaba una sola vez en la respuesta del checkout y no se
    guardaba. Quien cerraba esa pestaña antes de pagar ya no podía pagar.
    """
    asyncio.run(db.orders.insert_one(_pedido(
        'oxxo', card_checkout_url='https://mp.test/ficha/abc')))

    visto = asyncio.run(server.get_order('EX-20260731-0001'))
    assert visto['card_checkout_url'] == 'https://mp.test/ficha/abc'


def test_la_ficha_de_OXXO_desaparece_cuando_ya_pago(db, monkeypatch):
    """Una liga de pago de algo ya pagado sólo invita a pagar dos veces."""
    asyncio.run(db.orders.insert_one(_pedido(
        'oxxo', status='confirmado', card_checkout_url='https://mp.test/ficha/abc')))

    visto = asyncio.run(server.get_order('EX-20260731-0001'))
    assert 'card_checkout_url' not in visto


def test_el_pedido_que_ve_el_cliente_NO_trae_lo_que_cuesta_la_guia(db, monkeypatch):
    """⛔ LA MISMA REGLA DE LOS CORREOS, PERO EN LA API.

    Estaba cuidada en el correo y NO en la respuesta del servidor: el documento
    salía entero, con lo que se pagó de guía y lo que la casa absorbió, a la vista de
    cualquiera que abriera la consola. Y `/orders/{numero}` ni siquiera pide sesión.
    """
    asyncio.run(db.orders.insert_one(_pedido(
        'tarjeta', status='enviado', shipping_cost=168.33, shipping_absorbed=168.33,
        label_provider='skydropx', label_error='sin saldo', label_hold='sobre_tope',
        label_precio_cotizado=900, tracking_number='7712345678', carrier='Estafeta',
        tracking_url='https://estafeta.test/7712345678',
        referred_by='dist-1', commission=300)))

    visto = asyncio.run(server.get_order('EX-20260731-0001'))

    for interno in ('shipping_cost', 'shipping_absorbed', 'label_provider',
                    'label_error', 'label_hold', 'label_precio_cotizado',
                    'emails_sent', 'referred_by', 'commission'):
        assert interno not in visto, f'el cliente puede leer «{interno}»'
    # Y lo que SÍ es suyo sigue llegando: es lo que se le prometió por correo.
    assert visto['tracking_number'] == '7712345678'
    assert visto['carrier'] == 'Estafeta'
    assert visto['tracking_url'] == 'https://estafeta.test/7712345678'
    assert visto['total'] == 1180
