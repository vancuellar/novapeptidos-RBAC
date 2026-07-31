"""El freno de mano de las paqueterías: nunca más de N peticiones por segundo.

Por qué existe
--------------
Skydropx y enviosinternacionales.com topan en **2 peticiones por segundo**. Hasta el
2026-07-31 lo único que marcaba el paso era el `sleep` de 0.7 s ENTRE consultas de UNA
cotización — que alcanza mientras se cotice un pedido a la vez.

⛔ Y ESE ERA EL AGUJERO. En cuanto se cotizan dos pedidos al mismo tiempo (dos pestañas
del panel, el checkout mientras el admin despacha, o el doble cotizador preguntándole a
los dos proveedores) cada cotización lleva su propio reloj y ninguna sabe de la otra.
Tres despachos simultáneos son seis peticiones en el mismo segundo: el triple del tope.
Lo que devuelve la paquetería entonces es un 429, y un 429 en mitad de una compra de guía
es un pedido que no sale.

Cómo funciona
-------------
Ventana deslizante, no un `sleep` fijo: se recuerdan las marcas de tiempo del último
segundo y sólo se espera si ya se gastaron los permisos. Así se permite la ráfaga
legítima (2 seguidas) sin pasarse nunca del tope, en vez de castigar a todos con medio
segundo de espera cada vez.

El candado es un `Lock` de verdad porque FastAPI corre estas rutas —que son síncronas—
en su pool de hilos: dos peticiones del panel son dos hilos, no dos corrutinas, y sin
candado los dos leerían el mismo hueco libre y saldrían juntos.

⛔ CADA PROVEEDOR LLEVA SU PROPIA CUENTA. El tope es por cuenta, no del mundo: las
peticiones a Skydropx no gastan los permisos de enviosinternacionales. Por eso cada
módulo crea el suyo y no se comparte uno global.
"""
import threading
import time


class Ritmo:
    """Un tope de `por_segundo` peticiones, compartido por todos los hilos."""

    def __init__(self, por_segundo: float = 2.0, nombre: str = ''):
        self.por_segundo = max(0.0, float(por_segundo or 0))
        self.nombre = nombre
        self._marcas: list = []
        self._candado = threading.Lock()

    def esperar(self) -> float:
        """Se queda esperando hasta que haya permiso. Devuelve cuánto esperó, en segundos.

        Devolver la espera no es adorno: es lo que permite que una prueba compruebe que
        el freno de verdad frenó, en vez de confiar en que sí.
        """
        if self.por_segundo <= 0:
            return 0.0
        esperado = 0.0
        while True:
            with self._candado:
                ahora = time.monotonic()
                # Sólo importa lo que pasó en el último segundo.
                self._marcas = [m for m in self._marcas if ahora - m < 1.0]
                if len(self._marcas) < self.por_segundo:
                    self._marcas.append(ahora)
                    return esperado
                # Hay que esperar a que el más viejo cumpla su segundo.
                falta = 1.0 - (ahora - self._marcas[0])
            # ⛔ SE DUERME FUERA DEL CANDADO. Dormir con el candado puesto congelaría a
            # todos los demás hilos exactamente el tiempo que este se está aguantando,
            # y el freno se convertiría en un embudo de un solo carril.
            if falta > 0:
                time.sleep(falta)
                esperado += falta

    def olvidar(self) -> None:
        """Borra la cuenta. Sólo para las pruebas: en producción el tiempo pasa solo."""
        with self._candado:
            self._marcas.clear()
