"""LA RESPUESTA SALE LIMPIA — el Markdown que se le escapa al modelo.

⛔ Christián, 2026-07-31: «las respuestas de la AI no están limpias, dejan
código, ejemplo `**NAD+ 500**`». Estas pruebas usan TEXTO DE VERDAD, del que
contesta el asistente, y miran dos cosas:

  · que lo que sale ya no traiga marcadores sueltos, y
  · que el chorrito (streaming) no parta un `**` a la mitad y deje basura en
    pantalla — el caso que se ve feo aunque la limpieza del texto completo esté
    perfecta.

Lo que NO se prueba aquí es que la pantalla pinte bonito: eso es de la UI
(`RespuestaIA.js`). Aquí sólo se prueba que el texto que viaja está presentable.
"""

import pytest

import texto_ia


# Una respuesta real del asesor, con todo lo que el modelo suele meter.
RESPUESTA = """## Comparación rápida

Para alguien que empieza te recomiendo **NAD+ 500**, no el de 1000.

- **NAD+ 500 mg**: $1,259 MXN · el más pedido
- **NAD+ 1000 mg**: $2,099 MXN · para quien ya lleva camino

Usa `agua bacteriostática` para reconstituirlo.
"""


def _todo(texto, tamano=None):
    """Pasa el texto por la limpieza. Con `tamano`, en trozos de ese tamaño —
    que es como llega de verdad."""
    limpieza = texto_ia.LimpiezaEnVivo()
    if tamano is None:
        return texto_ia.limpiar(texto)
    salida = [limpieza.alimentar(texto[i:i + tamano])
              for i in range(0, len(texto), tamano)]
    salida.append(limpieza.cerrar())
    return ''.join(salida)


# ------------------------------------------------------- el texto, de una pieza
def test_el_caso_que_reporto_christian():
    """`**NAD+ 500**` bien cerrado se queda (la pantalla lo pinta en negrita);
    lo que jamás puede quedar es un asterisco huérfano."""
    salida = texto_ia.limpiar('Te recomiendo **NAD+ 500 y no el de 1000.')
    assert '*' not in salida
    assert 'NAD+ 500 y no el de 1000.' in salida


def test_los_titulos_pierden_las_almohadillas():
    assert texto_ia.limpiar('## Comparación rápida').strip() == 'Comparación rápida'


def test_los_acentos_graves_se_van_pero_el_texto_se_queda():
    salida = texto_ia.limpiar('Usa `agua bacteriostática` para reconstituir.')
    assert '`' not in salida
    assert 'agua bacteriostática' in salida


def test_el_bloque_de_codigo_pierde_la_cerca_no_el_contenido():
    salida = texto_ia.limpiar('Cuenta:\n```\n2 x $1,259 = $2,518\n```\nEso es todo.')
    assert '```' not in salida
    assert '2 x $1,259 = $2,518' in salida


def test_la_vineta_con_asterisco_se_vuelve_guion():
    salida = texto_ia.limpiar('* NAD+ 500 mg: $1,259 MXN')
    assert salida.startswith('- NAD+ 500 mg')


def test_no_destruye_el_contenido():
    """La orden fue limpiar, no borrar: los números y los nombres se quedan."""
    salida = texto_ia.limpiar(RESPUESTA)
    for dato in ('NAD+ 500 mg', '$1,259 MXN', '$2,099 MXN', 'Comparación rápida',
                 'agua bacteriostática'):
        assert dato in salida, f'se perdió "{dato}"'
    assert '`' not in salida and '#' not in salida


def test_la_negrita_bien_cerrada_sobrevive():
    """Se queda para que la pantalla la pinte en negrita de verdad. Si algún día
    se decide borrarla también, que sea una decisión, no un accidente."""
    assert '**NAD+ 500**' in texto_ia.limpiar('Te recomiendo **NAD+ 500** hoy.')


# --------------------------------------------------------------- el chorrito
@pytest.mark.parametrize('tamano', [1, 2, 3, 5, 7, 13, 40, 200])
def test_el_chorrito_da_lo_mismo_que_el_texto_de_una(tamano):
    """Parta por donde parta —incluso letra por letra, que es donde un `**`
    queda a la mitad— el resultado tiene que ser el mismo."""
    assert _todo(RESPUESTA, tamano) == _todo(RESPUESTA)


@pytest.mark.parametrize('tamano', [1, 2, 3, 5, 7, 13])
def test_nunca_asoma_medio_marcador_en_pantalla(tamano):
    """Lo que de verdad se ve feo: el usuario mirando `**` mientras el modelo
    todavía escribe la palabra. Se revisa la pantalla en CADA paso."""
    limpieza = texto_ia.LimpiezaEnVivo()
    pantalla = ''
    for i in range(0, len(RESPUESTA), tamano):
        pantalla += limpieza.alimentar(RESPUESTA[i:i + tamano])
        # Una negrita a medio cerrar puede estar abierta; lo prohibido es un
        # marcador que ya no va a cerrar nunca porque la línea terminó.
        for linea in pantalla.split('\n')[:-1]:
            assert linea.count('*') % 2 == 0, f'renglón con asterisco suelto: {linea!r}'
            assert '`' not in linea and not linea.startswith('#')
    pantalla += limpieza.cerrar()
    assert '`' not in pantalla


def test_el_marcador_partido_entre_dos_trozos():
    """El caso exacto: un trozo termina en `*` y el siguiente empieza con `*`."""
    limpieza = texto_ia.LimpiezaEnVivo()
    visto = limpieza.alimentar('Te recomiendo *')
    assert '*' not in visto
    visto += limpieza.alimentar('*NAD+ 500** hoy.')
    visto += limpieza.cerrar()
    assert visto == 'Te recomiendo **NAD+ 500** hoy.'


def test_la_tabla_de_tuberias_no_se_rompe():
    """No la borramos: la pantalla la pinta como tabla de verdad. Lo que se
    prueba es que la limpieza no la deje a medias."""
    tabla = '| Producto | Precio |\n|---|---|\n| NAD+ 500 | $1,259 |\n'
    assert texto_ia.limpiar(tabla) == tabla


def test_texto_vacio_no_truena():
    assert texto_ia.limpiar('') == ''
    assert texto_ia.limpiar(None) == ''
