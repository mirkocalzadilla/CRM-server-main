#!/usr/bin/env bash
# Deploy pull-based: el VPS chequea origin/main y se actualiza solo (sin SSH entrante).
# Reemplaza el job SSH de deploy.yml, que el firewall del VPS bloquea para los runners
# de GitHub. Diseno: docs/SPEC_M-Deploy-pullbased.md. Lo dispara mks-deploy.timer (~2 min).
#
# Flujo: fetch -> resolver la imagen del commit REAL de HEAD (saltando commits [skip ci] de
# bitacora) -> si ya corre esa imagen, no-op; si su build aun no esta en GHCR, esperar -> pull
# + migrate + up -d -> health-check -> rollback si queda unhealthy. Registra la imagen REAL
# desplegada. Idempotente y seguro de re-ejecutar: si no hay nada nuevo, sale 0 sin tocar nada.
set -euo pipefail

REPO_DIR="/home/deploy/marketing-services"
STATE_DIR="/home/deploy/.mks-deploy"
COMPOSE="docker compose -f docker-compose.prod.yml"
IMAGE="ghcr.io/funnelops-marketing-services/server"
APP_CONTAINER="marketing-app"
WORKER_CONTAINER="marketing-worker"
HEALTH_RETRIES=30      # x HEALTH_INTERVAL = ventana total de health-check
HEALTH_INTERVAL=5      # segundos

DEPLOYED_FILE="${STATE_DIR}/deployed_sha"   # ultimo SHA desplegado OK
FAILED_FILE="${STATE_DIR}/failed_sha"       # SHA que fallo: no reintentar en automatico

mkdir -p "${STATE_DIR}"
cd "${REPO_DIR}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Avisos de deploy a Sentry (best-effort: una falla aca JAMAS frena el deploy). Se envia el
# evento por curl directo al endpoint envelope con el SENTRY_DSN del .env (sin SDK ni deps
# nuevas en el VPS). Fingerprint fijo por tipo -> un solo Issue por tipo en Sentry cuya lista
# de eventos es el timeline de deploys; el sha viaja en tags.deploy_sha y en release. Level
# info para inicio/OK (igual crean Issue visible) y error solo para el rollback (#243).
SENTRY_DSN="$(grep -m1 '^SENTRY_DSN=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)"

notify_sentry() {
  local level="$1" fingerprint="$2" message="$3" sha="$4"
  [[ -n "${SENTRY_DSN}" ]] || return 0
  local key="${SENTRY_DSN#*://}" host="${SENTRY_DSN#*@}" project_id="${SENTRY_DSN##*/}"
  key="${key%%@*}"
  host="${host%%/*}"
  local event_id ts
  event_id="$(tr -d '-' < /proc/sys/kernel/random/uuid)"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  curl -s -o /dev/null --max-time 5 \
    -H "X-Sentry-Auth: Sentry sentry_version=7, sentry_key=${key}, sentry_client=vps-pull-deploy/1.0" \
    --data-binary "{\"event_id\":\"${event_id}\"}
{\"type\":\"event\"}
{\"event_id\":\"${event_id}\",\"timestamp\":\"${ts}\",\"platform\":\"other\",\"level\":\"${level}\",\"logger\":\"vps-pull-deploy\",\"message\":\"${message}\",\"fingerprint\":[\"${fingerprint}\"],\"release\":\"${sha}\",\"environment\":\"production\",\"tags\":{\"deploy_sha\":\"${sha}\"}}" \
    "https://${host}/api/${project_id}/envelope/" || true
}

git fetch --quiet origin main
target="$(git rev-parse origin/main)"
current="$(cat "${DEPLOYED_FILE}" 2>/dev/null || echo none)"

image_exists() { docker manifest inspect "${IMAGE}:$1" >/dev/null 2>&1; }

# La imagen a desplegar es la del commit REAL que representa el codigo de HEAD. `main` HEAD
# suele ser el commit [skip ci] del bot de bitacora (solo toca docs; el codigo es identico al
# merge de abajo) y no tiene imagen :<sha> propia. Se salta hacia atras SOLO sobre commits
# [skip ci] hasta el primer commit real y se despliega/registra ESA imagen. Antes se registraba
# HEAD y, si el build del commit real aun no habia terminado, se caia a una imagen mas vieja
# marcando HEAD como desplegado -> el loop quedaba trabado corriendo codigo viejo (#218).
resolve_deploy_sha() {
  local sha="${1}" i
  for i in $(seq 1 20); do
    if ! git log -1 --format=%B "${sha}" | grep -q '\[skip ci\]'; then
      echo "${sha}"
      return 0
    fi
    sha="$(git rev-parse --verify --quiet "${sha}^")" || return 1
  done
  return 1
}

deploy_sha="$(resolve_deploy_sha "${target}")" || {
  log "No se encontro un commit real bajo ${target:0:12}; se reintenta en el proximo tick."
  exit 0
}

# Ya corre la imagen correcta -> nada que hacer (caso normal en cada tick). Se compara contra
# deploy_sha (la imagen real desplegada), NO contra HEAD: HEAD suele ser el commit [skip ci] de
# bitacora, que nunca es == a la imagen desplegada y trababa el loop (#218).
# Ademas de deployed_sha se verifica la imagen CORRIENDO en app y worker: un `up` manual sin
# IMAGE_TAG recreaba los containers con :latest (viejo) sin tocar deployed_sha, y el loop
# quedaba ciego al drift creyendo que no habia nada que hacer (#233, incidente 2026-07-22).
# Con drift detectado se sigue de largo y el deploy normal lo corrige en este mismo tick.
running_matches() {
  local c
  for c in "${APP_CONTAINER}" "${WORKER_CONTAINER}"; do
    [[ "$(docker inspect -f '{{.Config.Image}}' "${c}" 2>/dev/null || echo none)" == "${IMAGE}:${deploy_sha}" ]] || return 1
  done
  return 0
}

if [[ "${deploy_sha}" == "${current}" ]]; then
  if running_matches; then
    exit 0
  fi
  log "Drift detectado: deployed_sha=${current:0:12} pero app/worker no corren esa imagen; se redeploya."
fi

# No reintentar en bucle una imagen que ya fallo (evita crash-loop cada 2 min).
if [[ "${deploy_sha}" == "$(cat "${FAILED_FILE}" 2>/dev/null || echo none)" ]]; then
  log "Imagen ${deploy_sha:0:12} marcada como fallida; se omite (limpiar ${FAILED_FILE} para reintentar)."
  exit 0
fi

# La imagen del commit real puede no estar aun en GHCR (build en curso): esperar, sin caer a
# una mas vieja.
if ! image_exists "${deploy_sha}"; then
  log "Imagen ${IMAGE}:${deploy_sha:0:12} aun no esta en GHCR; se reintenta en el proximo tick."
  exit 0
fi

log "Nuevo deploy ${deploy_sha:0:12} (actual ${current:0:12}, HEAD ${target:0:12})."
notify_sentry info deploy-start "Deploy iniciado" "${deploy_sha}" || true

# Avanzar el checkout (trae docker-compose.prod.yml/Caddyfile/migraciones; el codigo es identico
# a deploy_sha, HEAD solo suma commits [skip ci] de docs).
git merge --ff-only origin/main

# Pull de la imagen resuelta (deberia existir; si falla por una carrera, reintentar luego).
if ! IMAGE_TAG="${deploy_sha}" ${COMPOSE} pull app worker migrate; then
  log "No se pudo pullear ${IMAGE}:${deploy_sha:0:12}; se reintenta en el proximo tick."
  exit 0
fi

# Migracion (forward-only). -T: sin TTY/stdin (corre bajo systemd).
log "Corriendo migraciones (alembic upgrade head)."
IMAGE_TAG="${deploy_sha}" ${COMPOSE} --profile migrate run --rm -T migrate

log "Recreando servicios con la imagen nueva."
IMAGE_TAG="${deploy_sha}" ${COMPOSE} up -d --remove-orphans

# Health-check: esperar a que el contenedor app quede 'healthy' (reusa el healthcheck del compose).
healthy=false
for _ in $(seq 1 "${HEALTH_RETRIES}"); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "${APP_CONTAINER}" 2>/dev/null || echo starting)"
  if [[ "${status}" == "healthy" ]]; then
    healthy=true
    break
  fi
  sleep "${HEALTH_INTERVAL}"
done

# Health profundo (#246): con el contenedor healthy (liveness), validar dependencias reales
# (DB/Redis/worker) antes de declarar OK — un release con env rota (p. ej. DATABASE_URL mala)
# pasa el healthcheck liviano. Curl desde dentro del contenedor (no depende de Caddy/DNS);
# urlopen lanza excepcion con el 503 de "degraded" -> exit != 0. Reintentos: el worker tarda
# unos segundos en publicar su primer heartbeat tras el up. Trade-off asumido: una caida real
# de DB/Redis concurrente con el deploy tambien dispara rollback (conservador, y avisa Sentry).
if [[ "${healthy}" == "true" ]]; then
  deep_ok=false
  for _ in $(seq 1 6); do
    if docker exec "${APP_CONTAINER}" python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/deep', timeout=8)" >/dev/null 2>&1; then
      deep_ok=true
      break
    fi
    sleep "${HEALTH_INTERVAL}"
  done
  if [[ "${deep_ok}" != "true" ]]; then
    log "ALERTA: /health/deep degradado tras el deploy de ${deploy_sha:0:12} (DB/Redis/worker caidos o env rota)."
    healthy=false
  fi
fi

if [[ "${healthy}" != "true" ]]; then
  log "ALERTA: ${APP_CONTAINER} no quedo healthy tras el deploy de ${deploy_sha:0:12}. Rollback a ${current:0:12}."
  notify_sentry error deploy-rollback "Deploy fallido: rollback" "${deploy_sha}" || true
  echo "${deploy_sha}" > "${FAILED_FILE}"
  if [[ "${current}" != "none" ]]; then
    IMAGE_TAG="${current}" ${COMPOSE} up -d --remove-orphans app worker || true
    log "Rollback de imagen aplicado (app/worker en ${current:0:12}). NOTA: la migracion no se revierte."
  else
    log "Sin SHA previo registrado; no hay rollback automatico posible."
  fi
  exit 1
fi

# Exito: registrar la IMAGEN desplegada (deploy_sha, no HEAD) para que el loop converja y se
# auto-cure (#218); limpiar marca de fallo + imagenes viejas.
echo "${deploy_sha}" > "${DEPLOYED_FILE}"
rm -f "${FAILED_FILE}"
docker image prune -f >/dev/null 2>&1 || true
log "Deploy OK: ${deploy_sha:0:12} healthy y en produccion."
notify_sentry info deploy-ok "Deploy OK" "${deploy_sha}" || true
