"""¿ESTE PEDIDO HUELE A BROMA? — el filtro de los «chismosos»

⛔ ENCARGO DE CHRISTIÁN (2026-08-05): «está llegando mucha basura/spam del carrito…
quiero evitar esos "chismosos" que nada más le están picando a lo estúpido», y en el
mismo mensaje dijo qué hacer con ellos: «que un pedido así no me notifique ni dispare
un correo de carrito abandonado y que caduque solo».

Lo que se vio en la base ese día (11 pedidos en total, 2 de broma):

    nombre 'Hola'      correo hola@gmail.com          tel +52 (12) 3456-7890
    nombre 'Hola hola' correo fjwoijewijfeow@gmail.com  misma dirección, 10 min después

⛔ NO VENÍAN VACÍOS. Traían nombre, correo, teléfono y dirección — inventados. Por eso
esto NO es «validar que los campos estén llenos»: es oler datos que un humano serio no
teclea. Y por eso tampoco sirve comprobar el dominio del correo: `gmail.com` es real.

⛔⛔ ESTO NO BLOQUEA NINGUNA VENTA, Y NO ES NEGOCIABLE. La regla madre de la casa es
VENDER SIEMPRE (`exygen-vender-siempre-envio-partido`). Un detector de basura que se
equivoca y rechaza a un cliente de verdad cuesta MUCHÍSIMO más que diez pedidos de
broma: el de broma nunca iba a pagar, el legítimo sí. Aquí sólo se CLASIFICA, y lo que
cambia es el ruido — a quién se le avisa, a quién se le persigue con la oferta de
carrito abandonado, y qué se puede caducar solo.

Por eso el umbral pide **DOS señales**, no una: cualquiera de ellas sola tiene un caso
legítimo (hay gente que se llama Ana, hay direcciones sin número, hay quien escribe su
CP en el renglón de la ciudad por prisa). Dos a la vez ya no es prisa.
"""
import re
import unicodedata

# Cuántas señales hacen falta para llamarle basura. DOS. Ver el porqué arriba.
MINIMO_SENALES = 2

# Palabras que nadie pone de nombre ni de correo cuando de verdad quiere su paquete.
PALABRAS_DE_JUEGO = {
    'hola', 'hello', 'test', 'prueba', 'pruebas', 'asdf', 'asd', 'qwerty', 'aaa',
    'xxx', 'na', 'nada', 'ninguno', 'sin nombre', 'nombre', 'ejemplo', 'example',
    'fulano', 'mengano', 'perengano', 'juan perez', 'john doe', 'xd', 'jaja',
}

# ---------------------------------------------------------------------------
#  EL CÓDIGO POSTAL — lo que pidió Christián el mismo día
# ---------------------------------------------------------------------------
# «Que valide además el C.P. de la dirección de envío no?»
#
# En México el CP son 5 dígitos y los DOS PRIMEROS dicen el estado. Esta tabla es
# pública y estable (SEPOMEX). Hay prefijos compartidos por dos estados —el 63 lo
# usan Jalisco y Nayarit, el 98 Yucatán y Zacatecas—, así que cada prefijo apunta a
# un CONJUNTO y basta con que el estado elegido esté dentro.
#
# ⚠️ QUE NO CUADRE EL ESTADO ES UNA SEÑAL, NO UN RECHAZO. Un cliente que escribe
# «Edomex» en vez de «México», o que se equivoca de renglón, sigue queriendo comprar.
# Lo único que sí se puede afirmar sin miedo es que un CP que no son 5 dígitos está
# mal escrito — y ni eso bloquea aquí: sólo suma señal.
PREFIJO_CP = {}


def _rango(desde, hasta, estados):
    for p in range(desde, hasta + 1):
        PREFIJO_CP.setdefault(f'{p:02d}', set()).update(estados)


_rango(1, 16, {'ciudad de mexico', 'cdmx', 'distrito federal', 'df'})
_rango(20, 20, {'aguascalientes'})
_rango(21, 22, {'baja california'})
_rango(23, 23, {'baja california sur'})
_rango(24, 24, {'campeche'})
_rango(25, 27, {'coahuila', 'coahuila de zaragoza'})
_rango(28, 28, {'colima'})
_rango(29, 30, {'chiapas'})
_rango(31, 33, {'chihuahua'})
_rango(34, 35, {'durango'})
_rango(36, 38, {'guanajuato'})
_rango(39, 41, {'guerrero'})
_rango(42, 43, {'hidalgo'})
_rango(44, 49, {'jalisco'})
_rango(50, 57, {'mexico', 'estado de mexico', 'edomex'})
_rango(58, 61, {'michoacan', 'michoacan de ocampo'})
_rango(62, 62, {'morelos'})
_rango(63, 63, {'nayarit', 'jalisco'})          # prefijo compartido
_rango(64, 67, {'nuevo leon'})
_rango(68, 71, {'oaxaca'})
_rango(72, 75, {'puebla'})
_rango(76, 76, {'queretaro'})
_rango(77, 77, {'quintana roo'})
_rango(78, 79, {'san luis potosi'})
_rango(80, 82, {'sinaloa'})
_rango(83, 85, {'sonora'})
_rango(86, 86, {'tabasco'})
_rango(87, 89, {'tamaulipas'})
_rango(90, 90, {'tlaxcala'})
_rango(91, 96, {'veracruz', 'veracruz de ignacio de la llave'})
_rango(97, 97, {'yucatan'})
_rango(98, 98, {'yucatan', 'zacatecas'})        # prefijo compartido
_rango(99, 99, {'zacatecas'})


def _plano(texto) -> str:
    """Minúsculas, sin acentos y sin espacios de sobra. Para comparar nombres de
    estado que cada quien escribe como puede («Nuevo León», «nuevo leon», «N.L.»)."""
    t = unicodedata.normalize('NFKD', str(texto or ''))
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', t).strip().lower()


def cp_valido(cp) -> bool:
    """5 dígitos y un prefijo que existe en México. Nada más."""
    c = re.sub(r'\D', '', str(cp or ''))
    return len(c) == 5 and c[:2] in PREFIJO_CP


def cp_cuadra_con_estado(cp, estado) -> bool:
    """¿El CP corresponde al estado que eligió? Si falta alguno de los dos, se da por
    bueno: no se castiga por un dato que no se pidió."""
    c = re.sub(r'\D', '', str(cp or ''))
    est = _plano(estado)
    if not est or len(c) != 5 or c[:2] not in PREFIJO_CP:
        return True
    return any(est == e or est in e or e in est for e in PREFIJO_CP[c[:2]])


def _telefono_de_juguete(tel) -> bool:
    """`1234567890`, `0000000000`, `1111111111`: nadie los teclea en serio.

    ⛔ VACÍO NO ES FALSO. Un teléfono en blanco es un campo que no se llenó, no una
    burla — y las VENTAS DIRECTAS de la casa entran así, sin teléfono ni ciudad
    (Alanis y Paz, en la base). Contarlo como señal las dejaba a una sola de
    caducarse solas. Se juzga lo que SÍ escribió, no lo que faltó.
    """
    d = re.sub(r'\D', '', str(tel or ''))
    if not d:
        return False
    if len(d) < 10:
        return True
    d = d[-10:]                              # sin lada de país
    if len(set(d)) <= 2:                     # 0000000000, 1212121212
        return True
    seguidos = ''.join(str(i % 10) for i in range(int(d[0]), int(d[0]) + 10))
    return d == seguidos or d == seguidos[::-1]


# Pares de letras que NO EXISTEN en español ni en inglés. Salen solos cuando alguien
# machaca el teclado (`fjwoijewijfeow` trae `fj`, `jw` y `jf`).
#
# ⚠️ La lista se quedó corta a propósito y se revisó contra apellidos mexicanos: NO
# lleva `zq` porque Vázquez y Velázquez lo tienen, ni `dz` (Fernández escrito sin
# acento), ni `xi`/`xc` (México, Ixchel). Ante la duda, fuera de la lista: el costo de
# marcar a un cliente real es la venta entera.
BIGRAMAS_IMPOSIBLES = {
    'jb', 'jc', 'jd', 'jf', 'jg', 'jk', 'jl', 'jm', 'jn', 'jp', 'jq', 'js',
    'jt', 'jv', 'jw', 'jx', 'jz',   # ⚠️ 'jr' NO: «Jr» es comunísimo
    'qb', 'qc', 'qd', 'qf', 'qg', 'qh', 'qj', 'qk', 'ql', 'qm', 'qn', 'qp', 'qr',
    'qs', 'qt', 'qv', 'qw', 'qx', 'qz',
    'wj', 'wq', 'wx', 'wz',
    'xj', 'xq', 'xw', 'xz',
    'zj', 'zw', 'zx',
    'vj', 'vq', 'vw', 'vx', 'vz',
    'kj', 'kq', 'kv', 'kw', 'kx', 'kz',
    'fj', 'fq', 'fv', 'fx', 'fz',
    'bq', 'bx', 'cq', 'cx', 'dq', 'gq', 'gx', 'hq', 'hx', 'pq', 'px', 'sx', 'tq',
}


def _machacado(texto) -> bool:
    """¿Esto lo tecleó alguien de verdad, o le dio de manotazos al teclado?

    Dos formas de cazarlo, y hace falta UNA:
      · **cinco consonantes seguidas** — no las tiene ninguna palabra en español ni
        en inglés (`asdkjhgqwlkjh`);
      · **dos o más pares de letras imposibles** (`fjwoijewijfeow` = fj, jw, jf).

    El segundo existe porque el primero NO alcanzaba: el correo de broma que de
    verdad llegó el 5-ago intercala vocales (`fj-o-ij-e-w-ij-f-eow`) y nunca junta
    cinco consonantes. Se pedían cinco y pasaba limpio.

    Largo mínimo 8 para no rozar apellidos cortos (Cruz, Díaz, Ruiz), y se piden DOS
    pares imposibles y no uno: uno solo puede ser un nombre extranjero.
    """
    # ⛔ TROZO POR TROZO, NUNCA LA CADENA PEGADA. Al quitar puntos y guiones se
    # inventan pares que nadie escribió: `vazquez.jr` se volvía `vazquezjr` (zj + jr)
    # y `xochitl.hdz` se volvía `xochitlhdz` (tlhdz, cinco consonantes). Los dos son
    # clientes reales y los dos salían marcados. El punto SEPARA palabras: hay que
    # respetarlo.
    for trozo in re.split(r'[^a-z]+', _plano(texto)):
        if len(trozo) < 8:
            continue
        if re.search(r'[bcdfghjklmnpqrstvwxyz]{5}', trozo):
            return True
        raros = sum(1 for i in range(len(trozo) - 1)
                    if trozo[i:i + 2] in BIGRAMAS_IMPOSIBLES)
        if raros >= 2:
            return True
    return False


def _nombre_de_juego(nombre) -> bool:
    n = _plano(nombre)
    # «Li», «Ma» (de Ma. Guadalupe) son nombres REALES y salían marcados con un
    # mínimo de 3. Se baja a 2: sólo cae quien escribió una sola letra.
    if not n or len(n.replace(' ', '')) < 2:
        return True
    if n in PALABRAS_DE_JUEGO:
        return True
    palabras = n.split()
    # «Hola hola», «Juan juan»: la misma palabra repetida no es un nombre.
    if len(palabras) >= 2 and len(set(palabras)) == 1:
        return True
    return any(p in PALABRAS_DE_JUEGO for p in palabras) or _machacado(n)


def _correo_de_juego(correo) -> bool:
    local = _plano(correo).split('@')[0]
    if not local:
        return True
    return local in PALABRAS_DE_JUEGO or _machacado(local)


def senales(customer: dict) -> list:
    """Todo lo que huele mal en estos datos, en texto legible.

    Se devuelven los MOTIVOS y no un booleano a secas porque acaban en el pedido y en
    el panel: cuando Christián vea un pedido apagado tiene que poder saber POR QUÉ, y
    discutirlo si el filtro se equivocó.
    """
    c = customer or {}
    fuera = []
    if _nombre_de_juego(c.get('full_name')):
        fuera.append('el nombre no parece un nombre')
    if _correo_de_juego(c.get('email')):
        fuera.append('el correo parece tecleado al azar')
    if _telefono_de_juguete(c.get('phone')):
        fuera.append('el telefono es de juguete')
    cp = c.get('postal_code')
    if cp and not cp_valido(cp):
        fuera.append('el codigo postal no existe en Mexico')
    elif not cp_cuadra_con_estado(cp, c.get('state')):
        fuera.append('el codigo postal no cuadra con el estado')
    # El CP tecleado en el renglón de la ciudad: pasó tal cual el 5-ago
    # (ciudad = '66218'). Solo, es una prisa; acompañado, es que no le importó.
    ciudad = _plano(c.get('city'))
    if ciudad and ciudad.isdigit():
        fuera.append('escribio el codigo postal donde va la ciudad')
    direccion = _plano(c.get('address'))
    if direccion and ciudad and direccion == ciudad:
        fuera.append('la ciudad es igual que la calle')
    return fuera


def es_basura(customer: dict) -> bool:
    """¿Se le apaga el ruido a este pedido? DOS señales o más.

    ⛔ Nunca decide si la venta procede. Sólo si molestamos a alguien por ella.
    """
    return len(senales(customer)) >= MINIMO_SENALES
