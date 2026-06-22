#!/usr/bin/env bash
# =============================================================================
# 一次性 GCP 環境建置 (在你本機，已安裝並登入 gcloud 後執行一次)。
# 建立：API 啟用、Artifact Registry、Workload Identity Federation(無金鑰)、
#       部署用服務帳戶與 IAM 綁定、VM 附掛服務帳戶的 AR 讀取權限。
#
# 用法:
#   1) 編輯下方變數區，填入你的專案 / repo / VM 資訊
#   2) gcloud auth login && gcloud config set project <PROJECT_ID>
#   3) bash deploy/setup_gcp.sh
#   4) 依輸出，把列出的值設到 GitHub repo 的 Variables (Settings > Secrets and
#      variables > Actions > Variables)
# =============================================================================
set -euo pipefail

# ----------------------------- 變數區 (請修改) --------------------------------
PROJECT_ID="${PROJECT_ID:-your-gcp-project-id}"
REGION="${REGION:-asia-east1}"                 # Artifact Registry / VM 所在區域
AR_REPOSITORY="${AR_REPOSITORY:-stock-quant}"  # Artifact Registry repo 名稱
IMAGE_NAME="${IMAGE_NAME:-stock-quant}"        # image 名稱

GITHUB_OWNER="${GITHUB_OWNER:-jummy1124}"      # GitHub 帳號/組織
GITHUB_REPO="${GITHUB_REPO:-stock_quant}"      # GitHub repo 名

VM_NAME="${VM_NAME:-stock-quant-vm}"           # 目標 GCE VM 名稱
VM_ZONE="${VM_ZONE:-asia-east1-b}"             # VM 所在 zone

DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-gha-deployer}"     # GitHub Actions 用的 SA
POOL_ID="${POOL_ID:-github-pool}"
PROVIDER_ID="${PROVIDER_ID:-github-provider}"
# -----------------------------------------------------------------------------

DEPLOY_SA_EMAIL="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "${PROJECT_ID}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"

echo "==> 1. 啟用必要 API"
gcloud services enable \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com \
  iam.googleapis.com \
  sts.googleapis.com \
  compute.googleapis.com \
  iap.googleapis.com

echo "==> 2. 建立 Artifact Registry (docker) repo: ${AR_REPOSITORY}"
gcloud artifacts repositories create "${AR_REPOSITORY}" \
  --repository-format=docker --location="${REGION}" \
  --description="stock-quant images" 2>/dev/null || echo "   (已存在，略過)"

echo "==> 3. 建立部署用服務帳戶: ${DEPLOY_SA_EMAIL}"
gcloud iam service-accounts create "${DEPLOY_SA_NAME}" \
  --display-name="GitHub Actions deployer" 2>/dev/null || echo "   (已存在，略過)"

echo "==> 4. 授權部署 SA"
# 4a. 推送 image + 移動 deployed tag。
#     用 repoAdmin（含 tags.delete）：deploy 以 `tags add` 移動 `deployed` 需刪舊 tag；
#     純 writer 會在第二次部署移動 tag 時報 artifactregistry.tags.delete PERMISSION_DENIED。
gcloud artifacts repositories add-iam-policy-binding "${AR_REPOSITORY}" \
  --location="${REGION}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/artifactregistry.repoAdmin" >/dev/null
# 4b. 透過 IAP 隧道 SSH/SCP 到 VM
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/iap.tunnelResourceAccessor" --condition=None >/dev/null
# 4c. 解析 VM、執行 SSH (OS Login，含 sudo 以便操作 docker)
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/compute.osAdminLogin" --condition=None >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${DEPLOY_SA_EMAIL}" \
  --role="roles/compute.viewer" --condition=None >/dev/null

echo "==> 5. 建立 Workload Identity Federation (無金鑰 OIDC)"
gcloud iam workload-identity-pools create "${POOL_ID}" \
  --location="global" --display-name="GitHub Actions pool" 2>/dev/null || echo "   (pool 已存在)"

gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_ID}" \
  --location="global" --workload-identity-pool="${POOL_ID}" \
  --display-name="GitHub provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner=='${GITHUB_OWNER}'" \
  2>/dev/null || echo "   (provider 已存在)"

echo "==> 6. 綁定：只有此 GitHub repo 可冒用部署 SA"
gcloud iam service-accounts add-iam-policy-binding "${DEPLOY_SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}" >/dev/null

echo "==> 7. 確保 VM 啟用 OS Login，且其附掛 SA 可讀 Artifact Registry"
gcloud compute instances add-metadata "${VM_NAME}" --zone="${VM_ZONE}" \
  --metadata=enable-oslogin=TRUE 2>/dev/null || echo "   (略過：請確認 VM 存在且已啟用 OS Login)"
VM_SA="$(gcloud compute instances describe "${VM_NAME}" --zone="${VM_ZONE}" \
  --format='value(serviceAccounts[0].email)' 2>/dev/null || true)"
if [ -n "${VM_SA}" ]; then
  gcloud artifacts repositories add-iam-policy-binding "${AR_REPOSITORY}" \
    --location="${REGION}" \
    --member="serviceAccount:${VM_SA}" \
    --role="roles/artifactregistry.reader" >/dev/null
  echo "   VM 附掛 SA: ${VM_SA} 已獲 AR Reader"
else
  echo "   ⚠️ 找不到 VM 附掛 SA，請手動授予該 SA roles/artifactregistry.reader"
fi

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"

cat <<EOF

============================================================================
✅ 完成。請到 GitHub repo > Settings > Secrets and variables > Actions
   > Variables 新增以下 Repository variables：

  GCP_PROJECT_ID   = ${PROJECT_ID}
  GCP_REGION       = ${REGION}
  AR_REPOSITORY    = ${AR_REPOSITORY}
  IMAGE_NAME       = ${IMAGE_NAME}
  GCP_WIF_PROVIDER = ${WIF_PROVIDER}
  GCP_DEPLOY_SA    = ${DEPLOY_SA_EMAIL}
  GCE_VM_NAME      = ${VM_NAME}
  GCE_VM_ZONE      = ${VM_ZONE}
  VM_APP_DIR       = /opt/stock-quant   # 或你在 VM 上放 .env / data 的目錄

VM 端前置 (一次性，見 CICD.md「VM 前置準備」)：
  - 安裝 Docker + Compose v2
  - 建立 \${VM_APP_DIR}，放入 .env (LINE 密鑰) 與 data/ 目錄
============================================================================
EOF
