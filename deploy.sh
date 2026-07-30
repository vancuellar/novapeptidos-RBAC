#!/usr/bin/env bash
#
# ============================================================================
#  DESPLIEGUE DEL BACKEND DE EXYGEN  —  /opt/exygen/app  en el EC2
# ============================================================================
#
#  POR QUE EXISTE ESTE ARCHIVO
#  ---------------------------
#  El 30 de julio de 2026 la tienda entera se cayo. La causa: un despliegue
#  subio un "import meta_capi" sin el archivo meta_capi.py (un git add que
#  fallo). El contenedor arrancaba, reventaba al importar, y "restart: always"
#  lo volvia a arrancar. En "docker ps" se leia "Up". Nadie se entero hasta que
#  un cliente aviso. Con la API muerta, la pagina se quedo sin login, sin
#  catalogo y sin ventas.
#
#  La leccion no es "hacer mejor el git add". Es que APAGAMOS lo que funcionaba
#  ANTES de comprobar que lo nuevo funciona. Este script invierte ese orden:
#
#      construir  ->  PROBAR LA IMAGEN NUEVA  ->  y solo entonces cambiarla
#
#  Si la prueba falla, el contenedor viejo NUNCA se toca: la tienda sigue
#  vendiendo con la version anterior y el script grita.
#
#  USO
#  ---
#    sudo ./deploy.sh                 desplegar lo ultimo de origin/main
#    sudo ./deploy.sh --sin-pull      desplegar el codigo que ya esta en disco
#    sudo ./deploy.sh --rollback      volver a la version anterior (un comando)
#    sudo ./deploy.sh --estado        que hay corriendo ahora mismo
#
# ============================================================================

set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGEN="app-api:latest"
IMAGEN_ANTERIOR="app-api:anterior"
SERVICIO="api"
PUERTO_LOCAL="8000"
PUERTO_HUMO="8099"
CONTENEDOR_HUMO="exygen-prueba-de-humo"
URL_LOCAL="http://127.0.0.1:${PUERTO_LOCAL}/api/"
URL_PUBLICA="https://api.exygenlabs.com/api/"
ESTADO="${APP_DIR}/.despliegue-anterior"
BITACORA="/var/log/exygen-deploy.log"

# ---------------------------------------------------------------- presentacion
rojo()  { printf '\033[1;31m%s\033[0m\n' "$*"; }
verde() { printf '\033[1;32m%s\033[0m\n' "$*"; }
gris()  { printf '\033[0;90m%s\033[0m\n' "$*"; }

paso() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

apuntar() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$BITACORA" 2>/dev/null || true; }

morir() {
  echo ""
  rojo "############################################################"
  rojo "#  DESPLIEGUE ABORTADO"
  rojo "#  $*"
  rojo "############################################################"
  apuntar "ABORTADO: $*"
  exit 1
}

# Si algo revienta en un punto no previsto, que tampoco pase inadvertido.
trap 'rojo "Fallo inesperado en la linea $LINENO."; limpiar_humo' ERR

limpiar_humo() {
  docker rm -f "$CONTENEDOR_HUMO" >/dev/null 2>&1 || true
}
trap 'limpiar_humo' EXIT

# ------------------------------------------------------------------ permisos
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -E "$0" "$@"
fi

cd "$APP_DIR"
[ -f docker-compose.yml ] || morir "No encuentro docker-compose.yml en $APP_DIR."
[ -f .env ] || morir "No encuentro .env en $APP_DIR. Sin el, la API no arranca."

# ============================================================================
#  PIEZAS
# ============================================================================

# El sha256 de la imagen que esta sirviendo ahora mismo.
imagen_en_uso() {
  docker inspect "app-${SERVICIO}-1" --format '{{.Image}}' 2>/dev/null || echo ""
}

# Guarda la version actual (imagen + commit) para poder volver en un comando.
guardar_marcha_atras() {
  local img commit
  img="$(imagen_en_uso)"
  commit="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo '')"
  if [ -n "$img" ]; then
    docker tag "$img" "$IMAGEN_ANTERIOR"
    gris "Version anterior guardada como $IMAGEN_ANTERIOR (commit ${commit:0:7})."
  else
    gris "No habia contenedor corriendo: no hay version anterior que guardar."
  fi
  printf 'imagen=%s\ncommit=%s\nfecha=%s\n' "$img" "$commit" "$(date -Iseconds)" > "$ESTADO"
}

# Espera a que la API conteste 200 en una URL. Devuelve 1 si se agota el plazo.
esperar_200() {
  local url="$1" plazo="${2:-60}" i=0 codigo
  while [ "$i" -lt "$plazo" ]; do
    codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || echo 000)"
    if [ "$codigo" = "200" ]; then return 0; fi
    i=$((i + 2)); sleep 2
  done
  return 1
}

# ============================================================================
#  LAS DOS PRUEBAS DE HUMO — se corren SOBRE LA IMAGEN NUEVA, con el
#  contenedor viejo todavia vivo y atendiendo clientes.
# ============================================================================

# PRUEBA 1 — "import server".
# Esta es exactamente la que habria atajado la caida del 30 de julio: un modulo
# que se importa pero no existe revienta aqui, en tres segundos, sin que nadie
# se quede sin tienda.
prueba_import() {
  paso "Prueba 1/2 — importar la aplicacion dentro de la imagen nueva"
  local salida
  if salida="$(docker run --rm --entrypoint sh "$IMAGEN" \
        -c 'python -c "import server"' 2>&1)"; then
    verde "  OK — 'import server' pasa limpio."
  else
    echo "$salida" | tail -25
    morir "La imagen nueva NI SIQUIERA IMPORTA. El contenedor viejo sigue intacto y la tienda sigue viva. Arregla el codigo y vuelve a intentar."
  fi
}

# PRUEBA 2 — arrancar un uvicorn de verdad, efimero, y pegarle a /api/.
# Va en la misma red que el mongo y con el mismo .env, o sea que tambien atrapa
# fallos de arranque y de conexion a la base que un simple import no ve.
# Escucha en el 8099, NO en el 8000: no le quita el puesto a nadie.
prueba_arranque() {
  paso "Prueba 2/2 — arrancar la imagen nueva de verdad y pedirle /api/"
  limpiar_humo
  docker run -d --name "$CONTENEDOR_HUMO" \
    --network app_default \
    --env-file "${APP_DIR}/.env" \
    -e SEED_DEMO_USERS=false \
    -p "127.0.0.1:${PUERTO_HUMO}:8000" \
    "$IMAGEN" >/dev/null

  if esperar_200 "http://127.0.0.1:${PUERTO_HUMO}/api/" 60; then
    verde "  OK — la imagen nueva contesta 200 en /api/."
    gris  "  $(curl -s --max-time 5 "http://127.0.0.1:${PUERTO_HUMO}/api/")"
    limpiar_humo
  else
    rojo "  Los ultimos renglones de la imagen nueva:"
    docker logs "$CONTENEDOR_HUMO" 2>&1 | tail -30
    limpiar_humo
    morir "La imagen nueva arranca pero NO contesta. El contenedor viejo sigue intacto y la tienda sigue viva."
  fi
}

# ============================================================================
#  MARCHA ATRAS
# ============================================================================
rollback() {
  paso "MARCHA ATRAS — volviendo a la version anterior"
  docker image inspect "$IMAGEN_ANTERIOR" >/dev/null 2>&1 \
    || morir "No hay imagen '$IMAGEN_ANTERIOR' guardada. No puedo volver atras solo."

  local commit=""
  [ -f "$ESTADO" ] && commit="$(grep '^commit=' "$ESTADO" | cut -d= -f2 || true)"
  if [ -n "$commit" ]; then
    gris "Regresando el codigo al commit ${commit:0:7}."
    git -C "$APP_DIR" reset --hard "$commit" >/dev/null 2>&1 || rojo "No pude regresar el codigo; sigo con la imagen."
  fi

  docker tag "$IMAGEN_ANTERIOR" "$IMAGEN"
  docker compose up -d --no-build --no-deps "$SERVICIO"

  if esperar_200 "$URL_LOCAL" 90; then
    verde "Marcha atras lista: la API vuelve a contestar 200."
    apuntar "ROLLBACK OK a ${commit:0:7}"
  else
    rojo "La marcha atras tampoco contesta. Revisa: sudo docker compose logs --tail 50 $SERVICIO"
    apuntar "ROLLBACK FALLIDO"
    exit 1
  fi
}

estado() {
  paso "Estado actual"
  docker compose ps
  echo ""
  gris "Commit desplegado: $(git -C "$APP_DIR" log --oneline -1)"
  echo ""
  local codigo_local codigo_publico
  codigo_local="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$URL_LOCAL" || echo 000)"
  codigo_publico="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL_PUBLICA" || echo 000)"
  echo "  $URL_LOCAL   -> $codigo_local"
  echo "  $URL_PUBLICA -> $codigo_publico"
  if [ -f "$ESTADO" ]; then
    echo ""
    gris "Version guardada para marcha atras:"
    sed 's/^/  /' "$ESTADO"
  fi
  return 0
}

# ============================================================================
#  DESPLIEGUE
# ============================================================================
desplegar() {
  local con_pull="$1"

  paso "Exygen — despliegue del backend"
  gris "Carpeta: $APP_DIR"
  apuntar "INICIO despliegue (pull=$con_pull)"

  # -- 0. Guardar a donde volver, ANTES de tocar nada.
  paso "Guardando la version actual por si hay que volver"
  guardar_marcha_atras

  # -- 1. Traer el codigo.
  if [ "$con_pull" = "si" ]; then
    paso "Trayendo el codigo de origin/main"

    # El docker-compose.yml se generaba a mano en el servidor y por eso quedo
    # como archivo "sin seguimiento". Ahora vive en el repo. Si el de disco
    # estorba el pull, se aparta una sola vez con copia de seguridad.
    if [ -f docker-compose.yml ] && ! git ls-files --error-unmatch docker-compose.yml >/dev/null 2>&1; then
      if git cat-file -e origin/main:docker-compose.yml 2>/dev/null; then
        local resguardo="docker-compose.yml.previo-al-repo-$(date +%Y%m%d%H%M%S)"
        mv docker-compose.yml "$resguardo"
        gris "El docker-compose.yml local se aparto como $resguardo (ahora lo manda el repo)."
      fi
    fi

    git fetch origin main
    local antes despues
    antes="$(git rev-parse HEAD)"
    git pull --ff-only origin main
    despues="$(git rev-parse HEAD)"
    if [ "$antes" = "$despues" ]; then
      gris "Sin novedades: ya estabamos en $(git log --oneline -1)."
    else
      gris "Entra:"
      git log --oneline "$antes".."$despues" | sed 's/^/    /'
    fi
  else
    paso "Sin pull (--sin-pull): se despliega el codigo que ya esta en disco"
    gris "$(git log --oneline -1)"
  fi

  # -- 2. Construir. Esto pisa app-api:latest, pero la anterior ya quedo a
  #       salvo bajo el nombre app-api:anterior en el paso 0.
  paso "Construyendo la imagen nueva"
  docker compose build "$SERVICIO"

  # -- 3. LAS PRUEBAS. Aqui todavia no se ha tocado nada de lo que esta vivo.
  prueba_import
  prueba_arranque

  # -- 4. Recien ahora se cambia el contenedor.
  #       "--no-deps" a proposito: un despliegue de la API NO debe reiniciar la
  #       base de datos. Sin esta bandera, cualquier cambio en el compose del
  #       mongo lo recrea de rebote y la tienda se queda sin base unos segundos.
  #       Si algun dia hay que aplicar un cambio al mongo, se hace aparte y a
  #       proposito:  sudo docker compose up -d mongo
  paso "Las dos pruebas pasaron — cambiando el contenedor"
  docker compose up -d --no-build --no-deps "$SERVICIO"

  # -- 5. Verificacion final. Si esto falla, marcha atras sola.
  paso "Verificando la API ya en produccion"
  if ! esperar_200 "$URL_LOCAL" 90; then
    rojo "La API NO contesta despues del cambio. Los ultimos renglones:"
    docker compose logs --tail 30 "$SERVICIO" || true
    rollback
    morir "Se cambio el contenedor, no contesto, y se volvio sola a la version anterior."
  fi
  verde "  $URL_LOCAL -> 200"

  local codigo_publico
  codigo_publico="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL_PUBLICA" || echo 000)"
  if [ "$codigo_publico" = "200" ]; then
    verde "  $URL_PUBLICA -> 200"
  else
    rojo "  $URL_PUBLICA -> $codigo_publico  (la API local si contesta: revisa Caddy con 'sudo systemctl status caddy')"
  fi

  # -- 6. Que el estado de salud tambien quede confirmado.
  local salud
  salud="$(docker inspect "app-${SERVICIO}-1" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-healthcheck{{end}}' 2>/dev/null || echo desconocido)"
  gris "Estado de salud del contenedor: $salud"

  echo ""
  verde "############################################################"
  verde "#  DESPLIEGUE LISTO"
  verde "#  $(git log --oneline -1)"
  verde "#  Marcha atras si algo sale mal:  sudo ./deploy.sh --rollback"
  verde "############################################################"
  apuntar "OK $(git rev-parse --short HEAD)"
}

# ============================================================================
case "${1:-}" in
  --rollback)  rollback ;;
  --estado)    estado ;;
  --sin-pull)  desplegar "no" ;;
  --help|-h)
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  "")          desplegar "si" ;;
  *)           morir "Opcion desconocida: $1  (usa --help)" ;;
esac
