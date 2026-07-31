"""¿DE QUÉ PAQUETERÍA ES ESTE NÚMERO DE GUÍA?

⛔ ES EL GEMELO DE `novapeptidos-UI/src/lib/paqueteria.js`. Las MISMAS reglas, en el
mismo orden y con los mismos nombres. No es copiar por gusto: la detección tiene que
pasar en los dos lados y son dos lenguajes distintos.

  · en la pantalla (JS) para SUGERIRLE la paquetería a quien captura la guía, en vivo
    mientras teclea, y dejarlo corregirla;
  · aquí (Python) como RED DE SEGURIDAD, porque la ruta que guarda el envío se puede
    llamar sin pasar por esa pantalla —el distribuidor, un script, la app de mañana— y
    un pedido con guía pero sin paquetería es un pedido que no se puede rastrear.

⛔ SI SE CAMBIA UNA REGLA, SE CAMBIA EN LOS DOS. `test_guias.py` compara este archivo
contra el JS de verdad cuando lo tiene a la mano, y truena si se separaron.

Los nombres salen EXACTAMENTE como los espera `server.CARRIER_TRACKING_URLS`, que es
quien arma la liga de rastreo. Escribirlos distinto aquí deja al cliente sin a dónde ir.
"""
import re

# Los mismos nombres, y en el mismo orden, que enseña el selector de la pantalla.
PAQUETERIAS = ('FedEx', 'DHL', 'Estafeta', 'UPS', 'Paquete Express', 'Redpack',
               'Correos de México')


def limpiar_guia(valor) -> str:
    """En MAYÚSCULAS y sin espacios ni guiones: la gente pega el número como se lo
    mandaron por WhatsApp, con espacios cada cuatro dígitos."""
    return re.sub(r'[\s-]', '', str(valor or '')).upper()


# (expresión, quién, seguro). El ORDEN importa: gana la primera que case.
REGLAS = (
    # 1Z + 16 caracteres es UPS y sólo UPS. No hay forma de confundirlo.
    (r'^1Z[0-9A-Z]{16}$', 'UPS', True),
    # Formato postal universal (UPU): RR123456789MX. Correos de México.
    (r'^[A-Z]{2}\d{9}MX$', 'Correos de México', True),
    (r'^[A-Z]{2}\d{9}[A-Z]{2}$', 'Correos de México', False),
    # DHL eCommerce: JVGL… / JJD…
    (r'^(JVGL|JJD)[0-9A-Z]+$', 'DHL', True),
    # Paquete Express marca sus guías con letras al frente.
    (r'^(PQ|PE|PX)\d{6,}$', 'Paquete Express', True),
    (r'^(RP|RED)\d{6,}$', 'Redpack', True),
    # FedEx: 12, 15, 20 o 22 dígitos. Ninguna otra de las que usamos llega a esos largos.
    (r'^\d{12}$', 'FedEx', True),
    (r'^\d{15}$', 'FedEx', True),
    (r'^\d{20}$', 'FedEx', True),
    (r'^\d{22}$', 'FedEx', True),
    # Estafeta alfanumérica de 22.
    (r'^(?=.*[A-Z])[0-9A-Z]{22}$', 'Estafeta', True),
    # 11 dígitos: DHL (su waybill clásico).
    (r'^\d{11}$', 'DHL', True),
    # ⚠️ 10 dígitos los usan Estafeta Y DHL. Se sugiere Estafeta —es la que más sale de
    # aquí— pero marcada como NO segura para que la pantalla invite a confirmarla.
    (r'^\d{10}$', 'Estafeta', False),
    # Redpack numérico de 9.
    (r'^\d{9}$', 'Redpack', False),
)


def detectar(numero) -> dict:
    """`{'quien': 'FedEx', 'seguro': True}` o `{}` si el número no dice nada.

    `seguro=False` significa «es lo más probable, pero el formato lo comparten
    varias»: sirve para sugerir, no para decidir a ciegas.
    """
    g = limpiar_guia(numero)
    if len(g) < 8:            # demasiado corto: o está mal, o siguen tecleando
        return {}
    for expresion, quien, seguro in REGLAS:
        if re.match(expresion, g):
            return {'quien': quien, 'seguro': seguro}
    return {}


def paqueteria_de(numero) -> str:
    """El nombre de la paquetería, o '' si no se puede saber. Atajo para quien sólo
    quiere el nombre y no le importa qué tan seguro es."""
    return detectar(numero).get('quien', '')
