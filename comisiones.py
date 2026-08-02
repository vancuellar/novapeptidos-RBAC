"""El PAGO de las comisiones: cuánto se le debe a cada quien y qué ya se pagó.

Encargo de Christián (2026-08-01): «Solicitar y registrar el pago de comisiones —
hoy no hay dónde ver qué se le debe a cada quien ni qué ya se pagó.»

Aquí vive la ARITMÉTICA, sin red y sin base de datos, para poderla probar de
verdad (mismo trato que `envios.py` y `descuentos.py`). Las rutas viven en
`server.py`; los documentos, en la colección `commission_payouts`.

Las tres reglas de la casa:

  · LO GANADO YA ESTÁ CALCULADO Y NO SE RECALCULA AQUÍ. Sale de
    `pyramid.earnings_for`, la misma suma que ve el distribuidor en su panel y
    el admin en el suyo — sólo ventas COBRADAS. Si esta bolsa hiciera su propia
    cuenta, habría dos verdades y un día dirían números distintos.

  · NO SE PAGA MÁS DE LO QUE SE DEBE. Ni el distribuidor puede solicitar por
    encima de su saldo, ni el admin puede registrar un pago que lo rebase. El
    saldo es `ganado − pagado`; lo SOLICITADO pendiente no descuenta (todavía
    no salió un peso) pero sí impide solicitar dos veces.

  · UN PAGO REGISTRADO NO SE EDITA NI SE BORRA por estas rutas: es el recibo.
    Si se registró mal, se registra el ajuste como constancia nueva — la
    historia del dinero no se reescribe.
"""

ESTADO_SOLICITADO = 'solicitado'   # el distribuidor pidió su pago; nadie ha pagado
ESTADO_PAGADO = 'pagado'           # el dinero ya salió; el documento es el recibo
ESTADO_RECHAZADO = 'rechazado'     # el admin lo negó, con motivo; no mueve saldo

ESTADOS = (ESTADO_SOLICITADO, ESTADO_PAGADO, ESTADO_RECHAZADO)

# Tolerancia de centavos: las comisiones se guardan redondeadas en pesos, pero
# comparar flotantes al peso exacto rebota solicitudes legítimas por 0.001.
_CENTAVOS = 0.01


def pagado_de(payouts) -> float:
    """Cuánto se le ha PAGADO ya: la suma de sus recibos."""
    return float(sum((p.get('amount') or 0) for p in (payouts or [])
                     if p.get('status') == ESTADO_PAGADO))


def solicitud_pendiente(payouts):
    """Su solicitud SIN resolver, si la hay. A lo más existe una a la vez."""
    for p in (payouts or []):
        if p.get('status') == ESTADO_SOLICITADO:
            return p
    return None


def por_pagar(ganado: float, payouts) -> float:
    """El saldo: lo ganado menos lo ya pagado. Nunca negativo — si un día lo
    pagado rebasara lo ganado (una comisión que se canceló después de pagarse),
    el saldo se queda en cero y la diferencia es un asunto para el admin, no un
    número rojo en el panel del distribuidor."""
    return max(0.0, float(ganado or 0) - pagado_de(payouts))


def puede_solicitar(monto: float, ganado: float, payouts):
    """¿Puede el distribuidor solicitar ESTE monto? → (True, '') o (False, motivo).

    El motivo va en palabras del panel, porque es lo que la pantalla enseña."""
    saldo = por_pagar(ganado, payouts)
    if solicitud_pendiente(payouts):
        return False, 'Ya tienes una solicitud en camino. Espera a que se resuelva.'
    monto = float(monto or 0)
    if monto <= 0:
        return False, 'No hay saldo por pagar.'
    if monto > saldo + _CENTAVOS:
        return False, f'Tu saldo por pagar es ${saldo:,.0f}; no se puede solicitar más.'
    return True, ''


def puede_pagar(monto: float, ganado: float, payouts):
    """¿Puede el admin registrar ESTE pago? → (True, '') o (False, motivo).

    El candado es el mismo del lado del que paga: registrar de más convertiría
    la bolsa en una fuente de dinero que nadie autorizó."""
    saldo = por_pagar(ganado, payouts)
    monto = float(monto or 0)
    if monto <= 0:
        return False, 'El monto del pago tiene que ser mayor que cero.'
    if monto > saldo + _CENTAVOS:
        return False, (f'El saldo por pagar de este distribuidor es ${saldo:,.0f}; '
                       'un pago mayor quedaría sin respaldo.')
    return True, ''


def resumen(ganado: float, payouts) -> dict:
    """Los cuatro números del tablero, juntos para que ninguna pantalla haga su
    propia suma: ganado, pagado, por pagar y lo solicitado en camino."""
    pendiente = solicitud_pendiente(payouts)
    return {
        'ganado': round(float(ganado or 0)),
        'pagado': round(pagado_de(payouts)),
        'por_pagar': round(por_pagar(ganado, payouts)),
        'solicitado': round(float((pendiente or {}).get('amount') or 0)),
        'solicitud_pendiente': pendiente,
    }
