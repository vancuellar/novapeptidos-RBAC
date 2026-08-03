# Reglas de este repo (backend de Exygen)

## ⛔ Quien prueba comprando en producción, limpia lo que ensució — en la misma sesión

Orden de Christián del 2026-08-01: *«Asegúrate de borrar los pedidos de prueba cuando
termines de hacer las pruebas. De otra manera queda mucha basura en el sitio.»*

Probar comprando de verdad **sí está permitido y a veces es la única prueba que vale**
(un test unitario no toca el checkout). Lo que no está permitido es **irse dejando el
pedido ahí**. La limpieza es parte de la prueba, no un pendiente para después: si la
sesión termina, termina limpia.

### Cómo se hace

1. **Al terminar la compra de prueba, márcala.** En el Panel → Pedidos: se selecciona y
   se aprieta **Marcar Como Prueba**. Por API:

   ```
   PUT /api/admin/orders/{order_id}/prueba   { "es_prueba": true }
   ```

   Es sólo una etiqueta: no borra nada, no esconde nada y se quita igual de fácil. El
   pedido queda con el sello «Prueba» a la vista en la lista, para que nadie lo confunda
   con una venta.

2. **Antes de cerrar la sesión, barre.** Panel → Pedidos → **Barrer Pruebas**. Enseña
   primero un **simulacro** (qué se llevaría y qué no, con el motivo de cada uno) y sólo
   entonces aparece el botón que borra. Por API:

   ```
   POST /api/admin/orders/barrer-pruebas   { "simulacro": true }    # sólo mira
   POST /api/admin/orders/barrer-pruebas   { "simulacro": false }   # borra
   ```

3. **Si el barrido no se lo lleva, NO lo fuerces.** Que un pedido quede protegido quiere
   decir que enseña una señal de venta real. Se investiga; no se fuerza.

### La línea que no se cruza

**El barrido no puede tocar una venta de verdad.** Entre los pedidos de prueba vive la
única venta real de esos días —así se perdió el sueño el 2026-07-29, cuando la lista
estrenó «seleccionar todo»— y por eso hay tres candados encadenados:

1. Sólo se miran los pedidos **etiquetados**. Lo que nadie marcó no existe para el
   barrido: no hay «borrar todos los pendientes».
2. De ésos se aparta cualquiera con una **señal de venta real**
   (`pruebas.senales_de_venta_real`): pagado, surtido, con comprobante o con guía. Ojo
   con `surtido`: un pedido ENTREGADO y FIADO (`paid: False`) no está pagado y aun así es
   una venta de las de doler, porque la mercancía ya salió. Ante la duda, no se borra.
3. El borrado **no se escribe en el barrido**: se le pasa a `/admin/orders/lote` con
   `forzar=False`, que es donde vive el candado de los pedidos pagados y donde se
   devuelven puntos e inventario. Un camino de borrado propio sería un candado menos.

El porqué completo está en `pruebas.py`. Lo fija `test_pedidos_de_prueba.py`: si alguien
le escribe al barrido su propio `delete_one`, o le quita el filtro de la etiqueta, la
suite se pone roja.

⛔ **Ningún agente borra pedidos de producción por su cuenta.** El barrido es una
herramienta para Christián en el Panel; el trabajo del agente es marcar lo que ensució y
avisarle, no limpiar la base a sus espaldas.

## Las pruebas

```bash
.venv/bin/python -m pytest -q
```

`pytest.ini` fija `testpaths` a mano y **hay que sumar ahí cada archivo de pruebas
nuevo**, o no se corre nunca. No es capricho: sin esa lista pytest sale a recorrer el
`.venv` (que cuelga de iCloud) y se queda colgado — el 2026-07-28 una recolección tardó
3 min 32 s y hubo que matar varias corridas a mano.

## Aquí hay varias sesiones trabajando a la vez

En este árbol suele haber más de una sesión de Claude abierta. El índice de git es
**compartido**: lo que una sesión deja a medias, otra se lo puede llevar en su commit.

**PROHIBIDO `git add -A`, `git add .`, `git commit -a` y `git stash` sin ruta.**

⛔ **Y EL `git commit` TAMBIÉN LLEVA RUTAS.** `git add` explícito NO basta:
`git commit -m "…"` a secas publica **todo lo que esté en el índice**, incluido lo
que otra sesión dejó ahí con su propio `git add`. Siempre:

```bash
git status                          # si aparece algo que no reconoces, no es tuyo
git commit -m "…" -- modelo_ia.py test_modelo_ia.py
```

Con `-- <rutas>` git commitea ESOS archivos e **ignora el resto del índice**. Es lo
único que aísla de verdad con dos sesiones abiertas.

**Qué pasó el 2026-08-03.** Dos agentes en paralelo, uno en `modelo_ia.py` y otro en
`server.py`/`chat_negocio.py`/`models.py`/`test_chat_negocio.py`. El primero hizo su
`git add` explícito y correcto —sólo sus dos archivos— pero commiteó sin rutas y se
llevó los cuatro archivos a medio terminar del otro dentro del commit `7a42032`. Esa
vez no hubo daño (el trabajo estaba completo y la suite en verde), pero el mismo
descuido pudo haber desplegado backend a medias.
