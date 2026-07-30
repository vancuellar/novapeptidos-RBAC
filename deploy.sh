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
#  AZUL Y VERDE — el parpadeo de segundos que ya no ocurre
#  -------------------------------------------------------
#  Aun pasando las pruebas quedaba un hueco: "docker compose up -d api" apagaba
#  el contenedor viejo y arrancaba el nuevo EN EL MISMO PUERTO. Entre lo uno y
#  lo otro la tienda contestaba error unos segundos. Con varios agentes
#  desplegando al dia, el dueno lo sufria varias veces al dia.
#
#  Ahora hay DOS contenedores gemelos, en dos puertos distintos:
#
#      api-azul  -> 127.0.0.1:8001        api-verde -> 127.0.0.1:8002
#
#  y Caddy (la puerta de entrada, api.exygenlabs.com) apunta a UNO de los dos
#  con una sola linea en /etc/caddy/exygen-color.caddy. El despliegue:
#
#      1. levanta el color APAGADO con la imagen nueva
#      2. espera a que conteste 200 y a que docker lo declare "healthy"
#      3. reescribe esa linea y recarga Caddy  <- el cambio de trafico
#      4. pasado un periodo de gracia, apaga el color viejo
#
#  La recarga de Caddy es en caliente: las peticiones en vuelo terminan en el
#  color viejo y las nuevas entran al color nuevo. No se pierde ninguna.
#
#  USO
#  ---
#    sudo ./deploy.sh                 desplegar lo ultimo de origin/main
#    sudo ./deploy.sh --sin-pull      desplegar el codigo que ya esta en disco
#    sudo ./deploy.sh --rollback      volver a la version anterior (un comando)
#    sudo ./deploy.sh --estado        que hay corriendo ahora mismo
#
#  Variable opcional:  GRACIA=30 sudo ./deploy.sh   (segundos que el color
#  viejo sigue vivo despues del cambio; por omision 15).
#
# ============================================================================

set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGEN="app-api:latest"
IMAGEN_ANTERIOR="app-api:anterior"

# Los dos colores y sus puertos. NO se tocan sin tocar tambien el compose.
PUERTO_AZUL="8001"
PUERTO_VERDE="8002"

# El contenedor del esquema viejo (un solo "api" en el 8000). Solo existe hasta
# el primer despliegue con azul/verde; a partir de ahi esta funcion sobra.
CONTENEDOR_LEGADO="app-api-1"

PUERTO_HUMO="8099"
CONTENEDOR_HUMO="exygen-prueba-de-humo"
PUERTO_LOCAL="8000"
URL_LOCAL="http://127.0.0.1:${PUERTO_LOCAL}/api/"
URL_PUBLICA="https://api.exygenlabs.com/api/"

CADDYFILE="/etc/caddy/Caddyfile"
CADDY_COLOR="/etc/caddy/exygen-color.caddy"

ESTADO="${APP_DIR}/.despliegue-anterior"
BITACORA="/var/log/exygen-deploy.log"
CERROJO="/var/lock/exygen-deploy.lock"

# Segundos que el color viejo sigue encendido despues del cambio de trafico.
# Sirve para que las peticiones lentas que ya estaban dentro terminen en paz.
GRACIA="${GRACIA:-15}"

# Los dos colores llevan "profiles" en el compose para que un "docker compose
# up -d" a secas no los levante a los dos. Aqui si los queremos ver siempre.
export COMPOSE_PROFILES="colores"

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
#  UN DESPLIEGUE A LA VEZ
#  Varios agentes despliegan al dia y a veces a la misma hora. Dos despliegues
#  cruzados se pisarian el color y dejarian a Caddy apuntando a un contenedor
#  que el otro acaba de apagar. Con este cerrojo, el segundo espera su turno.
# ============================================================================
tomar_cerrojo() {
  exec 9>"$CERROJO"
  if ! flock -w 900 9; then
    morir "Hay otro despliegue en curso desde hace mas de 15 minutos. Revisa con: sudo ./deploy.sh --estado"
  fi
}

# ============================================================================
#  PIEZAS — colores
# ============================================================================

puerto_de() { [ "$1" = "azul" ] && echo "$PUERTO_AZUL" || echo "$PUERTO_VERDE"; }
servicio_de() { echo "api-$1"; }
url_de() { echo "http://127.0.0.1:$(puerto_de "$1")/api/"; }
el_otro() { [ "${1:-}" = "azul" ] && echo "verde" || echo "azul"; }

# QUIEN ESTA SIRVIENDO. La verdad no la tiene un archivo de estado nuestro: la
# tiene Caddy, porque Caddy es quien reparte el trafico de verdad. Se lee de
# ahi para que no puedan discrepar.
color_activo() {
  local p=""
  # El "|| true" del final no es adorno: el script corre con "pipefail" y este
  # archivo no existe todavia el primer dia. Sin el, leerlo aborta el despliegue.
  p="$(grep -o '127\.0\.0\.1:[0-9][0-9]*' "$CADDY_COLOR" 2>/dev/null | head -1 | cut -d: -f2 || true)"
  case "$p" in
    "$PUERTO_AZUL")  echo azul  ;;
    "$PUERTO_VERDE") echo verde ;;
    *)               echo ""    ;;   # todavia en el esquema viejo
  esac
}

contenedor_de() { docker compose ps -a -q "$(servicio_de "$1")" 2>/dev/null | head -1 || true; }

esta_corriendo() {
  local id; id="$(contenedor_de "$1")"
  [ -n "$id" ] && [ "$(docker inspect "$id" --format '{{.State.Running}}' 2>/dev/null)" = "true" ]
}

salud_de() {
  local id; id="$(contenedor_de "$1")"
  [ -n "$id" ] || { echo "no-existe"; return 0; }
  docker inspect "$id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-healthcheck{{end}}' 2>/dev/null || echo desconocido
}

legado_vivo() {
  [ "$(docker inspect "$CONTENEDOR_LEGADO" --format '{{.State.Running}}' 2>/dev/null || echo false)" = "true" ]
}

# El sha256 de la imagen que esta sirviendo ahora mismo.
imagen_en_uso() {
  local color id img=""
  color="$(color_activo)"
  if [ -n "$color" ]; then
    id="$(contenedor_de "$color")"
    if [ -n "$id" ]; then
      img="$(docker inspect "$id" --format '{{.Image}}' 2>/dev/null || echo '')"
    fi
  fi
  if [ -z "$img" ]; then
    img="$(docker inspect "$CONTENEDOR_LEGADO" --format '{{.Image}}' 2>/dev/null || echo '')"
  fi
  echo "$img"
}

# Guarda la version actual (imagen + commit + color) para poder volver.
guardar_marcha_atras() {
  local img commit color
  img="$(imagen_en_uso)"
  commit="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo '')"
  color="$(color_activo)"
  if [ -n "$img" ]; then
    docker tag "$img" "$IMAGEN_ANTERIOR"
    gris "Version anterior guardada como $IMAGEN_ANTERIOR (commit ${commit:0:7}, color ${color:-legado})."
  else
    gris "No habia contenedor corriendo: no hay version anterior que guardar."
  fi
  printf 'imagen=%s\ncommit=%s\ncolor=%s\nfecha=%s\n' \
    "$img" "$commit" "${color:-legado}" "$(date -Iseconds)" > "$ESTADO"
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

# ESPERAR A QUE UN COLOR ESTE SANO DE VERDAD. Dos condiciones, no una:
#   1. contesta 200 en su propio puerto
#   2. docker lo declara "healthy" (o sea, el healthcheck del compose paso)
# Si docker lo declara "unhealthy" no se espera el plazo completo: se corta ya.
esperar_sano() {
  local color="$1" plazo="${2:-150}" i=0 codigo salud
  local url; url="$(url_de "$color")"
  while [ "$i" -lt "$plazo" ]; do
    salud="$(salud_de "$color")"
    if [ "$salud" = "unhealthy" ]; then
      rojo "  El color $color se declaro 'unhealthy'."
      return 1
    fi
    codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || echo 000)"
    if [ "$codigo" = "200" ] && { [ "$salud" = "healthy" ] || [ "$salud" = "sin-healthcheck" ]; }; then
      return 0
    fi
    i=$((i + 2)); sleep 2
  done
  rojo "  Se agoto el plazo esperando al color $color (ultimo codigo: ${codigo:-?}, salud: ${salud:-?})."
  return 1
}

# ============================================================================
#  EL CONMUTADOR — Caddy
#  Cambiar de color es reescribir UNA linea y recargar. La recarga de Caddy es
#  en caliente: no cierra el puerto, no corta conexiones y las peticiones que
#  ya estaban dentro terminan contra el color viejo.
# ============================================================================

# El 8000 es el puerto de toda la vida. Mientras siga ahi el contenedor del
# esquema viejo no se lo podemos dar a Caddy; en cuanto se apague, si.
puerto_8000_ocupado_por_otro() {
  local linea
  linea="$(ss -ltnp 2>/dev/null | grep -E '127\.0\.0\.1:8000' || true)"
  [ -n "$linea" ] && ! echo "$linea" | grep -q 'caddy'
}

caddyfile_deseado() {
  cat <<'ARRIBA'
# ============================================================================
#  Caddy — la puerta de entrada de Exygen.   LO ESCRIBE deploy.sh, no se edita
#  a mano (el siguiente despliegue lo volveria a escribir).
#
#  Aqui NO dice a que contenedor va el trafico. Eso vive en una sola linea, en
#  /etc/caddy/exygen-color.caddy, y dice "azul" (8001) o "verde" (8002).
#  Cambiar de color = reescribir esa linea + "systemctl reload caddy".
# ============================================================================

api.exygenlabs.com, chat.exygenlabs.com {
	import /etc/caddy/exygen-color.caddy
}
ARRIBA
  if ! puerto_8000_ocupado_por_otro; then
    cat <<'ABAJO'

# El atajo de siempre. Los contenedores ya no escuchan en el 8000 (ahora son el
# 8001 y el 8002), pero quien pregunte por 127.0.0.1:8000 —scripts, revisiones,
# el propio deploy.sh— tiene que seguir hablando con la API. Caddy se lo pasa
# al color que este sirviendo, igual que al trafico publico.
http://127.0.0.1:8000 {
	import /etc/caddy/exygen-color.caddy
}
ABAJO
  fi
}

# Deja el Caddyfile como lo queremos. Solo escribe si cambio algo, y valida
# ANTES de recargar: una configuracion mala rechazada es un susto; aplicada,
# es la tienda caida.
asegurar_caddyfile() {
  local nuevo; nuevo="$(caddyfile_deseado)"
  if [ -f "$CADDYFILE" ] && [ "$(cat "$CADDYFILE")" = "$nuevo" ]; then
    return 0
  fi
  if [ -f "$CADDYFILE" ] && [ ! -f "${CADDYFILE}.antes-de-los-colores" ]; then
    cp "$CADDYFILE" "${CADDYFILE}.antes-de-los-colores"
    gris "Copia del Caddyfile original en ${CADDYFILE}.antes-de-los-colores"
  fi
  printf '%s\n' "$nuevo" > "$CADDYFILE"
  return 0
}

apuntar_caddy_a() {
  local color="$1" puerto; puerto="$(puerto_de "$color")"
  cat > "$CADDY_COLOR" <<FIN
# Color que esta sirviendo AHORA MISMO. Lo escribe deploy.sh. Una sola linea.
#   azul = 8001      verde = 8002
reverse_proxy 127.0.0.1:${puerto}
FIN
  asegurar_caddyfile
  if ! caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
    rojo "Caddy rechaza la configuracion. NO la aplico. Detalle:"
    caddy validate --config "$CADDYFILE" 2>&1 | tail -15
    return 1
  fi
  systemctl reload caddy
  return 0
}

# Solo se usa en el estreno de los colores: si el cambio sale mal cuando todavia
# existe el contenedor del esquema viejo, se devuelve el Caddyfile original (el
# que apunta directo al 8000) y la tienda vuelve a estar como estaba.
volver_caddy_al_legado() {
  [ -f "${CADDYFILE}.antes-de-los-colores" ] || return 1
  cp "${CADDYFILE}.antes-de-los-colores" "$CADDYFILE"
  caddy validate --config "$CADDYFILE" >/dev/null 2>&1 || return 1
  systemctl reload caddy
  return 0
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
# Escucha en el 8099, NO en el puerto de ningun color: no le quita el puesto a
# nadie.
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
#  APAGAR EL COLOR VIEJO — despues, nunca antes.
# ============================================================================
apagar_el_viejo() {
  local viejo="$1"

  if [ "$GRACIA" -gt 0 ] 2>/dev/null; then
    gris "Periodo de gracia: ${GRACIA}s para que terminen las peticiones que ya estaban dentro."
    sleep "$GRACIA"
  fi

  if [ -z "$viejo" ]; then
    # Primer despliegue con colores: lo que hay que apagar es el contenedor del
    # esquema viejo, el que ocupa el 8000.
    if legado_vivo; then
      gris "Apagando el contenedor del esquema viejo ($CONTENEDOR_LEGADO)."
      docker rm -f "$CONTENEDOR_LEGADO" >/dev/null 2>&1 || true
      # Ya libero el 8000: ahora Caddy si puede quedarse con el atajo local.
      apuntar_caddy_a "$(color_activo)" || rojo "No pude publicar el atajo 127.0.0.1:8000 (la tienda no se entera; revisa Caddy)."
    fi
    return 0
  fi

  # El color viejo se APAGA, no se borra: su contenedor guarda la version
  # anterior lista para volver a encenderla en un comando.
  gris "Apagando el color $viejo (queda guardado para la marcha atras)."
  docker compose stop -t 20 "$(servicio_de "$viejo")" >/dev/null 2>&1 || true
}

# ============================================================================
#  MARCHA ATRAS
# ============================================================================
rollback() {
  tomar_cerrojo
  paso "MARCHA ATRAS — devolviendo el trafico a la version anterior"

  local activo anterior
  activo="$(color_activo)"
  if [ -z "$activo" ]; then
    morir "Todavia no hay colores: este servidor no ha hecho ningun despliegue azul/verde. Vuelve atras con un despliegue normal del commit bueno."
  fi
  anterior="$(el_otro "$activo")"

  gris "Sirviendo ahora: $activo. Vuelvo a: $anterior."

  local commit=""
  [ -f "$ESTADO" ] && commit="$(grep '^commit=' "$ESTADO" | cut -d= -f2 || true)"
  if [ -n "$commit" ]; then
    gris "Regresando el codigo al commit ${commit:0:7}."
    git -C "$APP_DIR" reset --hard "$commit" >/dev/null 2>&1 || rojo "No pude regresar el codigo; sigo con el contenedor."
  fi

  if [ -n "$(contenedor_de "$anterior")" ]; then
    if esta_corriendo "$anterior"; then
      gris "El color $anterior sigue encendido: el cambio es inmediato."
    else
      gris "Encendiendo otra vez el color $anterior (con SU imagen, la de antes)."
      docker compose start "$(servicio_de "$anterior")" >/dev/null
    fi
  else
    # No hay contenedor del otro color (p.ej. justo despues de estrenar los
    # colores). Se reconstruye desde la imagen guardada.
    paso "No hay contenedor del color $anterior: lo levanto con $IMAGEN_ANTERIOR"
    docker image inspect "$IMAGEN_ANTERIOR" >/dev/null 2>&1 \
      || morir "No hay imagen '$IMAGEN_ANTERIOR' guardada. No puedo volver atras solo."
    docker tag "$IMAGEN_ANTERIOR" "$IMAGEN"
    docker compose up -d --no-build --no-deps --force-recreate "$(servicio_de "$anterior")" >/dev/null
  fi

  if ! esperar_sano "$anterior" 150; then
    docker compose logs --tail 30 "$(servicio_de "$anterior")" || true
    morir "El color $anterior no levanta. NO muevo el trafico: sigue sirviendo $activo, que al menos esta en pie."
  fi
  verde "  El color $anterior esta sano."

  paso "Cambiando el trafico a $anterior"
  apuntar_caddy_a "$anterior" || morir "No pude recargar Caddy. El trafico sigue en $activo."

  if esperar_200 "$URL_PUBLICA" 30; then
    verde "  $URL_PUBLICA -> 200 (sirviendo $anterior)"
  else
    rojo "  Ojo: $URL_PUBLICA no contesta 200. Revisa: sudo systemctl status caddy"
  fi

  apagar_el_viejo "$activo"

  verde "Marcha atras lista: ahora sirve el color $anterior (commit ${commit:0:7})."
  apuntar "ROLLBACK OK a ${commit:0:7} (color $anterior)"
}

# ============================================================================
estado() {
  paso "Estado actual"
  docker compose ps
  echo ""
  local activo; activo="$(color_activo)"
  if [ -n "$activo" ]; then
    verde "Sirviendo: color $activo (127.0.0.1:$(puerto_de "$activo"))"
    gris  "En reserva: color $(el_otro "$activo") — $(salud_de "$(el_otro "$activo")")"
  else
    rojo "Sirviendo: esquema VIEJO (un solo contenedor en el 8000). El proximo despliegue estrena azul/verde."
  fi
  echo ""
  gris "Commit desplegado: $(git -C "$APP_DIR" log --oneline -1 || true)"
  echo ""
  local codigo_local codigo_publico
  codigo_local="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$URL_LOCAL" || echo 000)"
  codigo_publico="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL_PUBLICA" || echo 000)"
  echo "  $URL_LOCAL   -> $codigo_local"
  echo "  $URL_PUBLICA -> $codigo_publico"
  for c in azul verde; do
    if [ -n "$(contenedor_de "$c")" ]; then
      local cod; cod="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$(url_de "$c")" || echo 000)"
      echo "  color $c ($(url_de "$c")) -> $cod   [$(salud_de "$c")]"
    fi
  done
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

  tomar_cerrojo

  paso "Exygen — despliegue del backend"
  gris "Carpeta: $APP_DIR"
  apuntar "INICIO despliegue (pull=$con_pull)"

  local viejo nuevo
  viejo="$(color_activo)"
  nuevo="$(el_otro "$viejo")"     # sin color previo -> estrena con azul
  if [ -n "$viejo" ]; then
    gris "Sirviendo ahora: color $viejo. La version nueva entra por el color $nuevo."
  else
    gris "Primer despliegue azul/verde: la version nueva entra por el color $nuevo."
  fi

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
  #       salvo bajo el nombre app-api:anterior en el paso 0, y el contenedor
  #       del color viejo sigue corriendo con SU imagen: mover la etiqueta no
  #       le afecta.
  paso "Construyendo la imagen nueva"
  docker compose build "$(servicio_de "$nuevo")"

  # -- 3. LAS PRUEBAS. Aqui todavia no se ha tocado nada de lo que esta vivo.
  prueba_import
  prueba_arranque

  # -- 4. Levantar el color NUEVO. El viejo sigue vendiendo, sin enterarse.
  #       "--no-deps" a proposito: un despliegue de la API NO debe reiniciar la
  #       base de datos. Sin esta bandera, cualquier cambio en el compose del
  #       mongo lo recrea de rebote y la tienda se queda sin base unos segundos.
  #       Si algun dia hay que aplicar un cambio al mongo, se hace aparte y a
  #       proposito:  sudo docker compose up -d mongo
  paso "Las dos pruebas pasaron — levantando el color $nuevo (sin tocar al que sirve)"
  docker compose up -d --no-build --no-deps --force-recreate "$(servicio_de "$nuevo")"

  paso "Esperando a que el color $nuevo este sano de verdad"
  if ! esperar_sano "$nuevo" 180; then
    rojo "Los ultimos renglones del color $nuevo:"
    docker compose logs --tail 30 "$(servicio_de "$nuevo")" || true
    docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
    morir "El color $nuevo no llego a estar sano. NO se movio el trafico: la tienda sigue vendiendo con la version anterior."
  fi
  verde "  El color $nuevo contesta 200 y docker lo declara sano."

  # -- 5. EL CAMBIO. Una linea y una recarga en caliente de Caddy. Aqui es
  #       donde antes se perdian peticiones y ahora no se pierde ninguna.
  paso "Cambiando el trafico al color $nuevo"
  if ! apuntar_caddy_a "$nuevo"; then
    docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
    morir "No pude recargar Caddy. El trafico NUNCA se movio: la tienda sigue con la version anterior."
  fi
  verde "  Caddy ya reparte al color $nuevo."

  # -- 6. Verificacion. Si esto falla, marcha atras sola (y es instantanea:
  #       el color viejo todavia esta encendido).
  paso "Verificando por la puerta de entrada"
  if ! esperar_200 "$URL_PUBLICA" 40; then
    rojo "La API publica NO contesta despues del cambio. Devuelvo el trafico a donde estaba."
    if [ -n "$viejo" ]; then
      if apuntar_caddy_a "$viejo"; then
        docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
        morir "Se cambio el trafico, la puerta publica no contesto, y el trafico volvio solo al color $viejo."
      fi
    else
      # Estreno de los colores: el contenedor del esquema viejo sigue vivo en el
      # 8000, asi que basta con devolver el Caddyfile de antes.
      if volver_caddy_al_legado; then
        docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
        morir "Se cambio el trafico, la puerta publica no contesto, y Caddy volvio al contenedor de siempre."
      fi
    fi
    morir "Se cambio el trafico y la puerta publica no contesta. Revisa Caddy: sudo systemctl status caddy"
  fi
  verde "  $URL_PUBLICA -> 200"

  local codigo_local
  codigo_local="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL_LOCAL" || echo 000)"
  [ "$codigo_local" = "200" ] && verde "  $URL_LOCAL -> 200" \
                              || gris  "  $URL_LOCAL -> $codigo_local (el atajo local se publica al apagar el esquema viejo)"

  # -- 7. Y AHORA si, apagar el color viejo.
  paso "Apagando el color anterior"
  apagar_el_viejo "$viejo"

  codigo_local="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL_LOCAL" || echo 000)"
  gris "  $URL_LOCAL -> $codigo_local"
  gris "  Salud del color $nuevo: $(salud_de "$nuevo")"

  echo ""
  verde "############################################################"
  verde "#  DESPLIEGUE LISTO — sirviendo el color $nuevo"
  verde "#  $(git log --oneline -1)"
  verde "#  Marcha atras si algo sale mal:  sudo ./deploy.sh --rollback"
  verde "############################################################"
  apuntar "OK $(git rev-parse --short HEAD) (color $nuevo)"
}

# ============================================================================
case "${1:-}" in
  --rollback)  rollback ;;
  --estado)    estado ;;
  --sin-pull)  desplegar "no" ;;
  --help|-h)
    sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  "")          desplegar "si" ;;
  *)           morir "Opcion desconocida: $1  (usa --help)" ;;
esac

# Este "exit" no sobra: deploy.sh se actualiza a si mismo con el git pull de
# arriba. Terminando aqui de forma explicita, bash nunca intenta seguir leyendo
# el archivo despues de haber cambiado de tamano.
exit 0
