# CI/CD 流程說明 — 每日自動部署到 GCP VM

本文件說明 `stock-quant` 的 CI/CD：每次 push 自動測試與建置 image，每日固定時段
(08:40 Asia/Taipei，台股開盤前) 檢查 `main` 是否有尚未上線的新版本，有才部署到 GCP VM。

## 架構總覽

```
 開發者 push 到 main
        │
        ▼
 ┌──────────────────────────── CI (ci.yml) ────────────────────────────┐
 │ 1. test       Python 3.10 + Poetry，跑 tests/run_tests.py            │
 │ 2. build-push 建 Docker image，推 Artifact Registry                  │
 │               tag = <commit-sha> (不可變) + latest                   │
 └─────────────────────────────────────────────────────────────────────┘
        │  (image 已就緒於 Artifact Registry)
        ▼
 ┌─────────────────── 每日排程 CD (deploy.yml) ─────────────────────────┐
 │ 00:40 UTC = 08:40 台北                                               │
 │ 1. 更新檢查  比對 main 最新 sha 的 image digest 與 `deployed` tag    │
 │              相同 → 無更新，結束；不同 → 繼續                        │
 │ 2. 部署      scp 部署資產到 VM → 經 IAP SSH 執行 deploy_on_vm.sh：   │
 │              docker pull → compose up -d → /health 健康檢查          │
 │              失敗自動回滾到前一版本                                  │
 │ 3. 標記      成功後把 `deployed` tag 移到本次版本 (線上真相/回滾基準)│
 └─────────────────────────────────────────────────────────────────────┘
```

身分驗證全程使用 **Workload Identity Federation (無金鑰 OIDC)**，GitHub 不保存任何
GCP 長期金鑰。

## 檔案

| 檔案 | 作用 |
|------|------|
| `.github/workflows/ci.yml` | push/PR 測試；push 到 main 時 build+push image |
| `.github/workflows/deploy.yml` | 每日排程 + 手動：更新檢查 → 部署 → 標記 |
| `deploy/docker-compose.deploy.yml` | VM 端正式部署 compose（用預建 image，不在 VM build） |
| `deploy/deploy_on_vm.sh` | VM 上執行：pull + 重啟 + 健康檢查 + 失敗回滾 |
| `deploy/setup_gcp.sh` | 一次性建置 GCP（API/AR/WIF/SA/IAM） |

## 「有更新才部署」如何判定

以 Artifact Registry 的 `deployed` tag 作為「目前線上版本」的單一真相：

- **target**：`main` 最新 commit 的 image（`:<sha>`，由 CI 建好推上）。
- **current**：`deployed` tag 指向的 image。
- 兩者 **digest 相同** → 線上已是最新 → 跳過部署。
- 不同（或首次部署無 `deployed`）→ 部署，成功後把 `deployed` 移到 target。

好處：建置與部署解耦、線上版本可追溯、回滾只需把 `deployed` 指回舊 image。

## 一次性設定

### 1. GCP 環境

編輯 `deploy/setup_gcp.sh` 變數區後執行一次：

```bash
gcloud auth login
gcloud config set project <PROJECT_ID>
bash deploy/setup_gcp.sh
```

它會啟用 API、建立 Artifact Registry、Workload Identity Federation、部署服務帳戶與
IAM 綁定，並把 VM 附掛服務帳戶授予 Artifact Registry Reader，最後印出要填到 GitHub 的值。

### 2. GitHub Repository Variables

到 repo → **Settings → Secrets and variables → Actions → Variables**，新增（非機密，
用 Variables 即可；WIF 不需任何 Secret）：

| Variable | 範例 |
|----------|------|
| `GCP_PROJECT_ID` | `my-project` |
| `GCP_REGION` | `asia-east1` |
| `AR_REPOSITORY` | `stock-quant` |
| `IMAGE_NAME` | `stock-quant` |
| `GCP_WIF_PROVIDER` | `projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| `GCP_DEPLOY_SA` | `gha-deployer@my-project.iam.gserviceaccount.com` |
| `GCE_VM_NAME` | `stock-quant-vm` |
| `GCE_VM_ZONE` | `asia-east1-b` |
| `VM_APP_DIR` | `/opt/stock-quant` |

### 3. VM 前置準備（一次性）

VM 需具備：

```bash
# Docker + Compose v2（Debian/Ubuntu 範例）
curl -fsSL https://get.docker.com | sudo sh

# 應用目錄，放 .env(LINE 密鑰) 與 data/
sudo mkdir -p /opt/stock-quant/data
sudo cp /path/to/your/.env /opt/stock-quant/.env      # 內含 LINE_CHANNEL_TOKEN / LINE_USER_ID
```

同時確認：VM 已啟用 **OS Login**（`enable-oslogin=TRUE`，setup 腳本會設）、其
**附掛服務帳戶**具 Artifact Registry Reader（setup 腳本會授）、防火牆允許
**IAP 來源 `35.235.240.0/20`** 連 TCP 22。

> 部署 image 認證走 VM 附掛服務帳戶（經 metadata token），所以 VM 上不需安裝 gcloud。

## 日常運作

- **改程式 → push 到 main**：CI 跑測試並把新 image 推到 Artifact Registry。
- **隔天 08:40（台北）**：deploy.yml 自動偵測到新版本並部署；當天若沒有新 commit 則跳過。
- **想立即部署**：到 Actions → *Daily Deploy to GCP VM* → **Run workflow**，
  需要強制重部署可勾 `force`。

> GitHub 排程為 best-effort，尖峰時可能延後幾分鐘，屬正常。

## 回滾

1. 找上一版的 commit SHA（或 `:deployed` 之前指向的 image）。
2. 把 `deployed` 指回該版本並手動於 VM 重啟：

```bash
# 在本機（已登入 gcloud）
REG=asia-east1-docker.pkg.dev/<PROJECT>/stock-quant/stock-quant
gcloud artifacts docker tags add ${REG}:<good-sha> ${REG}:deployed
# 在 VM 上
cd /opt/stock-quant
IMAGE=${REG}:<good-sha> REGION_HOST=asia-east1-docker.pkg.dev bash deploy_on_vm.sh
```

`deploy_on_vm.sh` 本身在健康檢查失敗時也會**自動回滾**到部署前的容器版本。

## 故障排除

| 症狀 | 可能原因 / 處置 |
|------|----------------|
| deploy.yml 報 `找不到 image ...:<sha>` | 該 commit 的 CI build-and-push 未成功；先看 CI 是否綠燈 |
| WIF 認證失敗 | 檢查 `GCP_WIF_PROVIDER` / `GCP_DEPLOY_SA` 變數、provider 的 `repository_owner` 條件、SA 的 workloadIdentityUser 綁定 |
| SSH/SCP 逾時 | 確認防火牆允許 `35.235.240.0/20` 連 22、VM 已開機、`iap.tunnelResourceAccessor` 權限 |
| VM 上 `docker pull` 403 | VM 附掛服務帳戶缺 Artifact Registry Reader |
| 健康檢查失敗自動回滾 | 看 Actions log 內 `docker logs` 片段；多為新版程式啟動錯誤 |

## 安全性

- 無金鑰：WIF 換發短期憑證，GitHub 不存任何 GCP service account 金鑰。
- 最小範圍：provider 限定 `repository_owner`，SA 綁定再限定到單一 repo。
- 密鑰隔離：LINE token 只存在 VM 的 `.env`，不進 git、不烤進 image。
