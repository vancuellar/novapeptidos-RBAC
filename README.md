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

### Que hace `deploy.sh`, en orden

1. **Guarda a donde volver.** Etiqueta la imagen que esta sirviendo como
   `app-api:anterior` y apunta el commit en `.despliegue-anterior`.
2. `git pull --ff-only origin main`.
3. `docker compose build api`.
4. **Prueba de humo 1 — `import server` dentro de la imagen nueva.**
   Es la que habria evitado la caida del 30 de julio de 2026 (un
   `import meta_capi` sin `meta_capi.py`).
5. **Prueba de humo 2 — arranca la imagen nueva de verdad** en un contenedor
   efimero, en el puerto 8099 y en la red del mongo, y le pide `/api/`
   hasta que conteste 200.
6. **Solo si las dos pasan**, cambia el contenedor (`docker compose up -d`).
7. Verifica `http://127.0.0.1:8000/api/` y `https://api.exygenlabs.com/api/`.
   Si la API no contesta despues del cambio, **vuelve sola** a la version anterior.

**Lo importante:** los pasos 4 y 5 corren con el contenedor viejo todavia vivo y
vendiendo. Si la imagen nueva esta rota, el script se planta y **no toca nada**.

### Los otros comandos

| Comando | Para que |
|---|---|
| `sudo ./deploy.sh` | Desplegar lo ultimo de `origin/main` |
| `sudo ./deploy.sh --rollback` | Volver a la version anterior (imagen **y** commit) |
| `sudo ./deploy.sh --estado` | Que hay corriendo, y si `/api/` contesta |
| `sudo ./deploy.sh --sin-pull` | Desplegar el codigo que ya esta en disco |

Bitacora de cada despliegue: `/var/log/exygen-deploy.log`.

### Lo que NO hay que usar

- `docker compose up -d --build` a pelo. Ese era el metodo viejo: apaga lo que
  funciona antes de saber si lo nuevo sirve.
- `deploy-exygen-backend.sh` (carpeta padre) **crea una instancia nueva desde
  cero**. No sirve para actualizar y no hay que tocarlo.

### El healthcheck

`docker-compose.yml` le pone un healthcheck a la API (y al mongo). Antes, un
contenedor que reventaba al arrancar y se reiniciaba en bucle se leia como
`Up` en `docker ps`. Ahora se lee `unhealthy`, y `deploy.sh` lo reporta.
La imagen es `python:3.11-slim` y **no trae `curl` ni `wget`**: el healthcheck
va con `urllib` de Python.

El `restart` es `unless-stopped` y los logs estan topados a 10 MB x 3 archivos,
para que un ciclo de reinicios no llene el disco de 20 GB.
