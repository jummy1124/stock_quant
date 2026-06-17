#!/usr/bin/env bash
# =============================================================================
# 在 GCP VM 上執行的部署腳本 (由 GitHub Actions deploy.yml 透過 IAP SSH 呼叫)。
# 職責：拉取指定版本 image -> 用 docker compose 重啟 -> 健康檢查 -> 失敗自動回滾。
#
# 認證：使用 VM「附掛的服務帳戶」(attached service account) 經 metadata server 取得
# access token 後 docker login，因此 VM 上不需安裝 gcloud。該服務帳戶需有
# Artifact Registry Reader 權限 (見 deploy/setup_gcp.sh)。
#
# 必要環境變數 (由 deploy.yml 帶入):
#   IMAGE        要部署的完整 image ref，含 tag/sha
#   REGION_HOST  Artifact Registry host，例如 asia-east1-docker.pkg.dev
# 選用:
#   APP_DIR      應用目錄 (預設為目前目錄)；需含 .env、data/、docker-compose.deploy.yml
#   HEALTH_URL   健康檢查 URL (預設 http://localhost:8000/health)
#   HEALTH_RETRIES / HEALTH_INTERVAL  健康檢查重試次數與間隔秒
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
COMPOSE_FILE="docker-compose.deploy.yml"
HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-30}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-3}"
SERVICE="stock-quant"
METADATA_TOKEN_URL="http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

cd "${APP_DIR}"

: "${IMAGE:?需要 IMAGE 環境變數}"
: "${REGION_HOST:?需要 REGION_HOST 環境變數}"

log() { echo "[$(date '+%F %T')] $*"; }

# 是否需要 sudo 才能跑 docker (OS Login 服務帳戶通常需要；在 docker 群組則不需)
if docker info >/dev/null 2>&1; then SUDO=""; else SUDO="sudo"; fi

# docker compose v2 (plugin) 或舊版 docker-compose (不含 sudo;啟動時用 sudo env 帶入變數)
if ${SUDO} docker compose version >/dev/null 2>&1; then
  DC_BIN="docker compose"
else
  DC_BIN="docker-compose"
fi

# 紀錄目前線上 image，供回滾使用 (容器不存在則為空 = 首次部署)
PREV_IMAGE="$(${SUDO} docker inspect --format '{{.Config.Image}}' "${SERVICE}" 2>/dev/null || true)"
log "目前線上版本: ${PREV_IMAGE:-<無，首次部署>}"
log "目標部署版本: ${IMAGE}"

# 用 VM 附掛服務帳戶的 access token 登入 Artifact Registry
log "向 ${REGION_HOST} 認證 (VM 服務帳戶)..."
ACCESS_TOKEN="$(curl -s -H 'Metadata-Flavor: Google' "${METADATA_TOKEN_URL}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')"
echo "${ACCESS_TOKEN}" | ${SUDO} docker login -u oauth2accesstoken --password-stdin "https://${REGION_HOST}"

log "拉取 image..."
${SUDO} docker pull "${IMAGE}"

health_check() {
  log "健康檢查 ${HEALTH_URL} (最多 $((HEALTH_RETRIES * HEALTH_INTERVAL))s)..."
  for i in $(seq 1 "${HEALTH_RETRIES}"); do
    if curl -fsS --max-time 5 "${HEALTH_URL}" >/dev/null 2>&1; then
      log "健康檢查通過 (第 ${i} 次)"
      return 0
    fi
    sleep "${HEALTH_INTERVAL}"
  done
  return 1
}

bring_up() {
  # 用 sudo env 帶入 IMAGE，避免 sudo 清掉前置環境變數導致 compose 取不到 ${IMAGE}
  ${SUDO} env IMAGE="$1" ${DC_BIN} -f "${COMPOSE_FILE}" up -d --remove-orphans
}

log "啟動新版本..."
bring_up "${IMAGE}"

if health_check; then
  log "✅ 部署成功: ${IMAGE}"
  ${SUDO} docker image prune -f >/dev/null 2>&1 || true
  exit 0
fi

# ---- 健康檢查失敗 -> 回滾 ----
log "❌ 健康檢查失敗，開始回滾..."
${SUDO} docker logs --tail 50 "${SERVICE}" 2>&1 || true

if [ -n "${PREV_IMAGE}" ]; then
  log "回滾到上一版本: ${PREV_IMAGE}"
  bring_up "${PREV_IMAGE}"
  if health_check; then
    log "↩️  已回滾並恢復服務 (${PREV_IMAGE})"
  else
    log "⚠️  回滾後健康檢查仍失敗，需人工介入"
  fi
else
  log "⚠️  無上一版本可回滾 (首次部署)；保留容器以利除錯"
fi
exit 1
