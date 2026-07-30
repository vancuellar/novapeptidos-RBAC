# Nova Peptidos RBAC

Backend for the Nova Peptidos storefront, including users, admin access, products, orders, and API routes.

## Run locally

```bash
pip install -r requirements.txt
uvicorn server:app --reload
```

## Hosting

The backend is ready for Docker hosting. It needs:

- `MONGO_URL`
- `DB_NAME`
- `JWT_SECRET`
- `CORS_ORIGINS`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `OPENAI_API_KEY` only if the AI chat should answer with OpenAI

`render.yaml` is included for Render Blueprint deployment.

After the backend is live, set the UI build variable:

```bash
REACT_APP_BACKEND_URL=https://your-backend-url
```

---

## Despliegue a produccion (api.exygenlabs.com)

**Un solo comando, y es este:**

```bash
sudo /opt/exygen/app/deploy.sh
```

Se corre **dentro del EC2** (`i-09fe943689eaebe0d`, `44.204.127.242`, usuario `ubuntu`).
Desde la Mac hay un atajo que entra y lo corre por ti:

```bash
"/Users/christian/Documents/Exygen Peptides/actualizar-exygen-backend.sh"
```

### Azul y verde: por que ya no parpadea

Antes habia **un solo** contenedor de la API, en el 8000. Desplegar era apagarlo
y encenderlo: entre lo uno y lo otro la tienda contestaba error unos segundos.
Con varios agentes desplegando al dia, eso pasaba varias veces al dia.

Ahora hay **dos contenedores gemelos** y solo uno recibe trafico:

| Color | Servicio en el compose | Puerto en el servidor |
|---|---|---|
| azul | `api-azul` | `127.0.0.1:8001` |
| verde | `api-verde` | `127.0.0.1:8002` |

Quien reparte es **Caddy**, y a que color reparte lo dice **una sola linea** en
`/etc/caddy/exygen-color.caddy` (`reverse_proxy 127.0.0.1:8001`). Cambiar de
color = reescribir esa linea + `systemctl reload caddy`. La recarga de Caddy es
**en caliente**: no cierra el puerto, las peticiones que ya estaban dentro
terminan contra el color viejo y las nuevas entran al color nuevo. **No se
pierde ninguna.**

`/etc/caddy/Caddyfile` lo escribe `deploy.sh` — no se edita a mano. Ademas del
sitio publico publica el atajo de siempre, `http://127.0.0.1:8000`, que Caddy
manda al color que este sirviendo: todo lo que apuntaba al 8000 sigue funcionando.

### Que hace `deploy.sh`, en orden

1. **Un despliegue a la vez.** Toma un cerrojo (`/var/lock/exygen-deploy.lock`).
   Si otro agente esta desplegando, este espera su turno en vez de pisarlo.
2. **Guarda a donde volver.** Etiqueta la imagen que esta sirviendo como
   `app-api:anterior` y apunta commit y color en `.despliegue-anterior`.
3. `git pull --ff-only origin main`.
4. `docker compose build` del **color que esta apagado**.
5. **Prueba de humo 1 — `import server` dentro de la imagen nueva.**
   Es la que habria evitado la caida del 30 de julio de 2026 (un
   `import meta_capi` sin `meta_capi.py`).
6. **Prueba de humo 2 — arranca la imagen nueva de verdad** en un contenedor
   efimero, en el puerto 8099 y en la red del mongo, y le pide `/api/`
   hasta que conteste 200.
7. **Levanta el color apagado** con la version nueva. El color que esta
   vendiendo no se toca.
8. **Espera a que el color nuevo este sano de verdad**: 200 en su puerto **y**
   `healthy` en docker. Si no llega, se planta ahi y no mueve nada.
9. **EL CAMBIO:** reescribe la linea del color y recarga Caddy. Aqui es donde
   antes se perdian peticiones y ahora no se pierde ninguna.
10. Verifica `https://api.exygenlabs.com/api/`. Si no contesta, **devuelve el
    trafico solo** al color anterior (que sigue encendido: es instantaneo).
11. **Periodo de gracia** (15 s por omision, `GRACIA=` lo cambia) y recien
    entonces apaga el color viejo. Su contenedor **no se borra**: queda
    guardado, apagado, listo para la marcha atras.

**Lo importante:** los pasos 5 a 8 corren con el color viejo vendiendo. Si la
version nueva esta rota, el script se planta y **no toca el trafico**.

### La marcha atras

`sudo ./deploy.sh --rollback` devuelve el trafico al **otro color**, que todavia
tiene su contenedor con la imagen de antes. Si ese contenedor sigue encendido
(estas dentro del periodo de gracia) el cambio es inmediato; si ya se apago, se
enciende, se espera a que este sano y **entonces** se mueve el trafico — o sea,
tampoco se pierde ninguna peticion. Tambien regresa el codigo al commit guardado.

### Los otros comandos

| Comando | Para que |
|---|---|
| `sudo ./deploy.sh` | Desplegar lo ultimo de `origin/main` |
| `sudo ./deploy.sh --rollback` | Volver a la version anterior (color, imagen **y** commit) |
| `sudo ./deploy.sh --estado` | Que color sirve, que hay en reserva, y si `/api/` contesta |
| `sudo ./deploy.sh --sin-pull` | Desplegar el codigo que ya esta en disco |

Bitacora de cada despliegue: `/var/log/exygen-deploy.log`.

### Lo que NO hay que usar

- `docker compose up -d --build` a pelo. Ese era el metodo viejo: apaga lo que
  funciona antes de saber si lo nuevo sirve. Ademas los dos colores llevan
  `profiles: ["colores"]` justamente para que un `docker compose up -d` a secas
  **no** los levante a los dos de golpe.
- Editar `/etc/caddy/Caddyfile` a mano: lo reescribe `deploy.sh`. Si hay que
  cambiar algo de la puerta de entrada, se cambia en `caddyfile_deseado()`
  dentro de `deploy.sh` y se commitea.
- `deploy-exygen-backend.sh` (carpeta padre) **crea una instancia nueva desde
  cero**. No sirve para actualizar y no hay que tocarlo.

### El healthcheck

`docker-compose.yml` le pone un healthcheck a la API (y al mongo). Antes, un
contenedor que reventaba al arrancar y se reiniciaba en bucle se leia como
`Up` en `docker ps`. Ahora se lee `unhealthy`, y `deploy.sh` lo reporta.
La imagen es `python:3.11-slim` y **no trae `curl` ni `wget`**: el healthcheck
va con `urllib` de Python.

Lleva `start_interval: 2s`: durante el arranque docker pregunta cada 2 segundos
en vez de cada 30, asi el color nuevo se declara `healthy` a los pocos segundos
de estar listo y el despliegue no espera de mas. Ese `healthy` es una de las dos
condiciones que `deploy.sh` exige **antes** de mover el trafico.

El `restart` es `unless-stopped` y los logs estan topados a 10 MB x 3 archivos,
para que un ciclo de reinicios no llene el disco de 20 GB.
