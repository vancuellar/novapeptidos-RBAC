"""EL 5% POR PAGAR EN CRIPTO — Christián, 2026-08-03.

⛔ DE DÓNDE SALE EL DINERO, QUE ES LO QUE LO HACE VIABLE. No sale del margen del
producto: sale de la comisión que la casa NO paga. Mercado Pago se queda 3.49% +
$4.00 más 16% de IVA sobre la comisión —o sea 4.05% + $4.64— de cada cobro con
tarjeta. Un cobro en cripto no pasa por ahí. Así que este 5% cuesta de verdad menos
del 1%: la pasarela financia casi todo.

Por eso este descuento **no cuenta contra el techo del 40%** de descuento comercial.
Son dos cosas distintas: aquél sale del margen del producto y por eso está topado;
éste sale de un gasto que desaparece. Mezclarlos haría que un cliente con 40% ya no
pudiera pagar en cripto, que es exactamente al revés de lo que se quiere — el que
más compra es al que más conviene sacar de la tarjeta.

⛔ SE APLICA SÓLO A LA MERCANCÍA, NUNCA AL ENVÍO. La guía se le paga a la paquetería
completa, en pesos, cobre lo que cobre la pasarela. Descontar sobre el envío sería
regalar dinero que ya salió de la casa.

⛔ Y SÓLO SI EL PAGO ES DE VERDAD EN CRIPTO. El descuento se calcula del método
elegido al crear el pedido, así que un pedido que dice «cripto» y luego se paga por
transferencia se llevaría el descuento sin el ahorro. Ver `revisar_al_cobrar`.

Módulo puro a propósito: la aritmética del dinero se prueba sin base de datos.
"""

METODO = 'cripto'
TASA = 0.05


def aplica(metodo_de_pago) -> bool:
    """¿Este pedido va a pagarse en cripto?"""
    return str(metodo_de_pago or '').strip().lower() == METODO


def descuento(mercancia_con_descuento: float, metodo_de_pago) -> int:
    """Los pesos que se le bajan por pagar en cripto, en entero.

    Entra la mercancía YA con su descuento comercial aplicado, no el subtotal: si
    entrara el subtotal, el 5% se calcularía sobre un precio que nadie va a pagar y
    la casa regalaría de más en cada pedido con descuento.
    """
    if not aplica(metodo_de_pago):
        return 0
    base = max(0.0, float(mercancia_con_descuento or 0))
    return int(round(base * TASA))


def texto_del_ahorro(mercancia_con_descuento: float) -> int:
    """Lo que se le puede PROMETER a alguien que todavía no elige método de pago.

    Es el mismo número, pero con nombre distinto a propósito: `descuento` cobra y
    esto anuncia. El día que la promesa y el cobro se separen, se separan aquí y no
    en una plantilla de correo.
    """
    return descuento(mercancia_con_descuento, METODO)


def revisar_al_cobrar(metodo_del_pedido, metodo_real, descuento_dado: int) -> dict:
    """¿El pedido se pagó por donde dijo que se iba a pagar?

    Devuelve el veredicto, NO lo corrige: un pedido ya cobrado no se re-cobra a
    espaldas del cliente. Sirve para que la casa lo vea y decida — y para que, si
    esto pasa seguido, se sepa que hay una fuga y no una casualidad.
    """
    prometido = aplica(metodo_del_pedido)
    cumplido = aplica(metodo_real)
    return {
        'coincide': prometido == cumplido,
        'fuga_mxn': int(descuento_dado or 0) if (prometido and not cumplido) else 0,
        'metodo_prometido': str(metodo_del_pedido or ''),
        'metodo_real': str(metodo_real or ''),
    }
