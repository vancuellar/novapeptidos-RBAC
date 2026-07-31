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
#  y delante de ellos LA PUERTA: un nginx diminuto (servicio "puerta") que
#  escucha en el 8010 (y en el 8000 de siempre) y manda todo a UNO de los dos.
#  El despliegue:
#
#      1. levanta el color APAGADO con la imagen nueva
#      2. espera a que conteste 200 y a que docker lo declare "healthy"
#      3. reescribe una linea de la puerta y le manda HUP  <- el cambio
#      4. pasado un periodo de gracia, apaga el color viejo
#
#  POR QUE UN NGINX Y NO RECARGAR CADDY. Se probo primero con Caddy: reescribir
#  su upstream y "systemctl reload caddy". Medido con un bucle de peticiones,
#  ESA RECARGA PIERDE conexiones: 15 recargas seguidas tumbaron 14 peticiones
#  de 358 (y 3 de 438 en una tanda mas espaciada). Recargar nginx con HUP no
#  pierde ninguna: 600 peticiones y 20 recargas, cero fallos. Por eso Caddy ya
#  no se toca nunca en un despliegue: apunta al 8010 y se olvida.
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

# La puerta: el nginx que decide a que color va todo.
SERVICIO_PUERTA="puerta"
PUERTA_DIR="/opt/exygen/puerta"
PUERTA_CONF="${PUERTA_DIR}/default.conf"
PUERTO_PUERTA="8010"
PUERTO_LOCAL="8000"

# El contenedor del esquema viejo (un solo "api" en el 8000). Solo existe hasta
# el primer despliegue con azul/verde; a partir de ahi esta funcion sobra.
CONTENEDOR_LEGADO="app-api-1"

PUERTO_HUMO="8099"
CONTENEDOR_HUMO="exygen-prueba-de-humo"
URL_PUERTA="http://127.0.0.1:${PUERTO_PUERTA}/api/"
URL_LOCAL="http://127.0.0.1:${PUERTO_LOCAL}/api/"
URL_PUBLICA="https://api.exygenlabs.com/api/"

CADDYFILE="/etc/caddy/Caddyfile"
CADDY_COLOR="/etc/caddy/exygen-color.caddy"   # del esquema anterior; ya no se usa

ESTADO="${APP_DIR}/.despliegue-anterior"
BITACORA="/var/log/exygen-deploy.log"
CERROJO="/var/lock/exygen-deploy.lock"

# Segundos que el color viejo sigue encendido despues del cambio de trafico.
# Sirve para que las peticiones lentas que ya estaban dentro terminen en paz.
GRACIA="${GRACIA:-15}"

# Los colores y la puerta llevan "profiles" en el compose para que un
# "docker compose up -d" a secas no los recree de golpe. Aqui si los queremos.
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
#  cruzados se pisarian el color y dejarian la puerta apuntando a un contenedor
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
# tiene la puerta, porque la puerta es quien reparte el trafico de verdad.
color_activo() {
  local p=""
  # El "|| true" del final no es adorno: el script corre con "pipefail" y estos
  # archivos no existen el primer dia. Sin el, leerlos aborta el despliegue.
  if [ -f "$PUERTA_CONF" ]; then
    p="$(grep -o 'server 127\.0\.0\.1:[0-9][0-9]*' "$PUERTA_CONF" 2>/dev/null | head -1 | cut -d: -f2 || true)"
  fi
  if [ -z "$p" ] && [ -f "$CADDY_COLOR" ]; then
    # Rastro del esquema intermedio (Caddy apuntando directo al color).
    p="$(grep -o '127\.0\.0\.1:[0-9][0-9]*' "$CADDY_COLOR" 2>/dev/null | head -1 | cut -d: -f2 || true)"
  fi
  case "$p" in
    "$PUERTO_AZUL")  echo azul  ;;
    "$PUERTO_VERDE") echo verde ;;
    *)               echo ""    ;;   # todavia en el esquema viejo
  esac
}

contenedor_de() { docker compose ps -a -q "$(servicio_de "$1")" 2>/dev/null | head -1 || true; }
contenedor_puerta() { docker compose ps -a -q "$SERVICIO_PUERTA" 2>/dev/null | head -1 || true; }

corriendo_id() {
  [ -n "${1:-}" ] && [ "$(docker inspect "$1" --format '{{.State.Running}}' 2>/dev/null || echo false)" = "true" ]
}

esta_corriendo() { corriendo_id "$(contenedor_de "$1")"; }

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
#  LA PUERTA — el conmutador
#
#  Es un nginx de 50 MB con una sola responsabilidad: decir a que color va el
#  trafico. Cambiar de color es reescribir una linea y mandarle HUP. nginx
#  aplica la configuracion nueva SIN cerrar el puerto: las conexiones que ya
#  estaban dentro terminan contra el color viejo y las nuevas entran al nuevo.
#  Medido: 600 peticiones con 20 recargas de por medio, cero fallos.
# ============================================================================

# El 8000 es el puerto de toda la vida. Mientras lo tenga otro (Caddy, o el
# contenedor del esquema viejo) la puerta no puede escucharlo; en cuanto quede
# libre, se lo queda y todo lo que apuntaba al 8000 sigue funcionando.
puerto_8000_ajeno() {
  local linea
  # Se mira la COLUMNA de la direccion local, no el renglon entero: quien tenga
  # el 8000 puede tenerlo como "127.0.0.1:8000" o como "*:8000" (Caddy lo abria
  # asi), y equivocarse aqui significa que nginx no arranca por choque de puerto.
  linea="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:8000$/' || true)"
  [ -n "$linea" ] && ! echo "$linea" | grep -q 'nginx'
}

puerta_conf_deseada() {
  local color="$1" puerto; puerto="$(puerto_de "$color")"
  cat <<CABEZA
# ============================================================================
#  LA PUERTA DE EXYGEN  —  LO ESCRIBE deploy.sh, no se edita a mano.
#
#  La linea que importa es la de aqui abajo: dice que color esta sirviendo.
#  Cambiarla y mandarle HUP a nginx es TODO el cambio de version, y no se
#  pierde ni una peticion. Los colores viven en el 8001 (azul) y el 8002
#  (verde); esta puerta escucha en el 8010 (y en el 8000 de siempre).
# ============================================================================

upstream exygen_color {
    server 127.0.0.1:${puerto};   # ${color}
    keepalive 32;
}

server {
    listen 127.0.0.1:${PUERTO_PUERTA};
CABEZA
  if ! puerto_8000_ajeno; then
    echo "    listen 127.0.0.1:${PUERTO_LOCAL};"
  fi
  cat <<'CUERPO'
    server_name _;

    # Que las respuestas no anden pregonando "nginx/1.27.5": es informacion
    # gratis para quien busque una version con agujeros conocidos.
    server_tokens off;

    # Caddy no le pone tope al tamano de subida y nginx si (1 MB). Sin esto,
    # subir un COA o una ficha en PDF por el panel empezaria a fallar.
    client_max_body_size 100m;

    location / {
        proxy_pass http://exygen_color;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # Las cabeceras ya vienen puestas por Caddy: se pasan TAL CUAL. Si aqui
        # se pusiera "$scheme" la API creeria que todo llega por http.
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $http_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
        proxy_set_header X-Forwarded-Host $http_x_forwarded_host;

        # Los videos de tutoriales se sirven por trozos (Range) y los PDF pueden
        # pesar: sin buffering nginx los pasa de largo en vez de acumularlos.
        proxy_buffering off;

        # Caddy no corta por tiempo; nginx corta a los 60 s por omision.
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
CUERPO
}

# Deja la puerta apuntando al color que se le diga. Si la configuracion no
# cambia y nginx ya esta corriendo, no hace nada (ni siquiera recarga).
apuntar_puerta_a() {
  local color="$1" previo="" nuevo id
  mkdir -p "$PUERTA_DIR"
  nuevo="$(puerta_conf_deseada "$color")"
  [ -f "$PUERTA_CONF" ] && previo="$(cat "$PUERTA_CONF")"

  id="$(contenedor_puerta)"

  if [ "$previo" = "$nuevo" ] && corriendo_id "$id"; then
    return 0
  fi

  printf '%s\n' "$nuevo" > "$PUERTA_CONF"

  if ! corriendo_id "$id"; then
    gris "Levantando la puerta (nginx)."
    docker compose up -d --no-deps "$SERVICIO_PUERTA" >/dev/null
    esperar_200 "$URL_PUERTA" 40 || { rojo "La puerta no contesta en $URL_PUERTA."; return 1; }
    return 0
  fi

  if ! docker exec "$id" nginx -t >/dev/null 2>&1; then
    rojo "nginx rechaza la configuracion de la puerta. La dejo como estaba:"
    docker exec "$id" nginx -t 2>&1 | tail -8
    [ -n "$previo" ] && printf '%s' "$previo" > "$PUERTA_CONF"
    return 1
  fi

  # HUP: nginx arranca procesos nuevos con la configuracion nueva y jubila a los
  # viejos cuando terminan lo que tenian entre manos. El puerto no se cierra.
  docker kill -s HUP "$id" >/dev/null
  esperar_200 "$URL_PUERTA" 20 || { rojo "La puerta dejo de contestar tras el HUP."; return 1; }
  return 0
}

# ============================================================================
#  CADDY — se toca UNA vez en la vida y nunca mas.
#  Apunta a la puerta y se olvida. Recargar Caddy pierde conexiones (medido),
#  asi que un despliegue no lo toca jamas: esta funcion no hace nada si el
#  archivo ya dice lo que tiene que decir.
# ============================================================================
caddyfile_deseado() {
  cat <<FIN
# ============================================================================
#  Caddy — la entrada publica de Exygen.
#
#  NO dice a que contenedor va el trafico, y por eso NO se recarga nunca en un
#  despliegue: manda todo a la puerta (nginx, 127.0.0.1:${PUERTO_PUERTA}) y es la
#  puerta la que elige color. Recargar Caddy pierde conexiones sueltas; nginx
#  recargado con HUP no pierde ninguna. Ver deploy.sh.
# ============================================================================

api.exygenlabs.com, chat.exygenlabs.com {
	reverse_proxy 127.0.0.1:${PUERTO_PUERTA}
}
FIN
}

asegurar_caddy() {
  local nuevo; nuevo="$(caddyfile_deseado)"
  if [ -f "$CADDYFILE" ] && [ "$(cat "$CADDYFILE")" = "$nuevo" ]; then
    return 0    # lo normal: no se toca nada
  fi

  paso "Caddy todavia no apunta a la puerta: se cambia UNA vez"
  if [ -f "$CADDYFILE" ] && [ ! -f "${CADDYFILE}.antes-de-la-puerta" ]; then
    cp "$CADDYFILE" "${CADDYFILE}.antes-de-la-puerta"
    gris "Copia del Caddyfile anterior en ${CADDYFILE}.antes-de-la-puerta"
  fi
  local respaldo; respaldo="$(cat "$CADDYFILE" 2>/dev/null || echo '')"
  printf '%s\n' "$nuevo" > "$CADDYFILE"
  if ! caddy validate --config "$CADDYFILE" >/dev/null 2>&1; then
    rojo "Caddy rechaza la configuracion nueva. La dejo como estaba:"
    caddy validate --config "$CADDYFILE" 2>&1 | tail -10
    [ -n "$respaldo" ] && printf '%s' "$respaldo" > "$CADDYFILE"
    return 1
  fi
  systemctl reload caddy
  gris "Caddy ya manda todo a la puerta. No se volvera a tocar en los despliegues."
  # El archivo del esquema intermedio ya no lo importa nadie.
  rm -f "$CADDY_COLOR"
  return 0
}

# La entrada completa, en el orden que no deja huecos:
#   1. la puerta (nginx) existe y sirve el color que toca — escucha el 8010
#   2. Caddy pasa a apuntar al 8010 (una sola vez en la historia) y suelta el 8000
#   3. la puerta se queda tambien con el 8000, que acaba de quedar libre
asegurar_la_entrada() {
  local color="$1"
  apuntar_puerta_a "$color" || return 1
  asegurar_caddy || return 1
  apuntar_puerta_a "$color" || return 1
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

  if [ -n "$viejo" ]; then
    # El color viejo se APAGA, no se borra: su contenedor guarda la version
    # anterior lista para volver a encenderla en un comando.
    gris "Apagando el color $viejo (queda guardado para la marcha atras)."
    docker compose stop -t 20 "$(servicio_de "$viejo")" >/dev/null 2>&1 || true
  fi

  # El contenedor del esquema viejo (un solo "api" en el 8000) sobra en cuanto
  # hay colores. Se comprueba SIEMPRE, no solo el primer dia: si por lo que sea
  # sigue vivo, se queda con el 8000 y la puerta no puede publicarlo.
  if legado_vivo; then
    gris "Quitando el contenedor del esquema viejo ($CONTENEDOR_LEGADO): ya no sirve trafico."
    docker rm -f "$CONTENEDOR_LEGADO" >/dev/null 2>&1 || true
    apuntar_puerta_a "$(color_activo)" \
      || rojo "No pude publicar el atajo 127.0.0.1:8000 (la tienda no se entera; revisa la puerta)."
  fi
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
  apuntar_puerta_a "$anterior" || morir "No pude cambiar la puerta. El trafico sigue en $activo."

  if esperar_200 "$URL_PUBLICA" 30; then
    verde "  $URL_PUBLICA -> 200 (sirviendo $anterior)"
  else
    rojo "  Ojo: $URL_PUBLICA no contesta 200. Revisa: sudo systemctl status caddy"
  fi

  apagar_el_viejo "$activo"

  if ! esperar_200 "$URL_PUBLICA" 20; then
    morir "Despues de apagar el color $activo la tienda dejo de contestar. Enciendelo otra vez: sudo docker compose start $(servicio_de "$activo")"
  fi

  verde "Marcha atras lista: ahora sirve el color $anterior (commit ${commit:0:7})."
  apuntar "ROLLBACK OK a ${commit:0:7} (color $anterior)"
}

# ============================================================================
estado() {
  paso "Estado actual"
  docker compose ps || true
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
  local codigo
  for u in "$URL_LOCAL" "$URL_PUERTA" "$URL_PUBLICA"; do
    codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$u" || echo 000)"
    printf '  %-38s -> %s\n' "$u" "$codigo"
  done
  for c in azul verde; do
    if [ -n "$(contenedor_de "$c")" ]; then
      codigo="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$(url_de "$c")" || echo 000)"
      printf '  color %-5s %-26s -> %s   [%s]\n' "$c" "$(url_de "$c")" "$codigo" "$(salud_de "$c")"
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

  # -- 0b. La puerta. Si ya estaba puesta (lo normal) esto no hace nada.
  if [ -n "$viejo" ]; then
    asegurar_la_entrada "$viejo" || morir "No pude dejar lista la puerta de entrada. No se toco nada mas."
  fi

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

  # -- 5. EL CAMBIO. Una linea en la puerta y un HUP a nginx. Aqui es donde
  #       antes se perdian peticiones y ahora no se pierde ninguna.
  paso "Cambiando el trafico al color $nuevo"
  if [ -z "$viejo" ]; then
    # Estreno: la puerta todavia no existia y Caddy apuntaba a otro lado.
    asegurar_la_entrada "$nuevo" || {
      docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
      morir "No pude dejar lista la puerta. El trafico NUNCA se movio."
    }
  elif ! apuntar_puerta_a "$nuevo"; then
    docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
    morir "No pude cambiar la puerta. El trafico NUNCA se movio: la tienda sigue con la version anterior."
  fi
  verde "  La puerta ya manda todo al color $nuevo."

  # -- 6. Verificacion. Si esto falla, marcha atras sola (y es instantanea:
  #       el color viejo todavia esta encendido).
  paso "Verificando por la entrada publica"
  if ! esperar_200 "$URL_PUBLICA" 40; then
    rojo "La API publica NO contesta despues del cambio. Devuelvo el trafico al color $viejo."
    if [ -n "$viejo" ] && apuntar_puerta_a "$viejo"; then
      docker compose stop -t 5 "$(servicio_de "$nuevo")" >/dev/null 2>&1 || true
      morir "Se cambio el trafico, la entrada publica no contesto, y el trafico volvio solo al color $viejo."
    fi
    morir "Se cambio el trafico y la entrada publica no contesta. Revisa: sudo systemctl status caddy"
  fi
  verde "  $URL_PUBLICA -> 200"

  # -- 7. Y AHORA si, apagar el color viejo.
  paso "Apagando el color anterior"
  apagar_el_viejo "$viejo"

  # -- 8. Y comprobar que apagarlo no rompio nada (o sea: que el cambio de la
  #       puerta se aplico de verdad y no seguiamos comiendo del color viejo).
  if ! esperar_200 "$URL_PUBLICA" 20; then
    rojo "Despues de apagar el color $viejo la tienda dejo de contestar. Marcha atras."
    rollback || true
    morir "Se apago el color viejo y la tienda dejo de contestar. Se intento la marcha atras."
  fi
  local codigo_local
  codigo_local="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$URL_LOCAL" || echo 000)"
  verde "  $URL_PUBLICA -> 200   |   $URL_LOCAL -> $codigo_local"
  gris  "  Salud del color $nuevo: $(salud_de "$nuevo")"

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
    sed -n '2,62p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  "")          desplegar "si" ;;
  *)           morir "Opcion desconocida: $1  (usa --help)" ;;
esac

# Este "exit" no sobra: deploy.sh se actualiza a si mismo con el git pull de
# arriba. Terminando aqui de forma explicita, bash nunca intenta seguir leyendo
# el archivo despues de haber cambiado de tamano.
exit 0
