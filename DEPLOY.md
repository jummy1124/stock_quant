# 部署指南 — Docker + Poetry

用 **Poetry** 管理 Python 環境、**Docker** 打包部署。映像為多階段建置：builder 階段用
Poetry 把相依套件裝進獨立 `.venv`，runtime 階段只帶 venv 與原始碼，體積小且不含建置工具。

相依極精簡：標準函式庫 + `openpyxl`（寫 Excel）。Python 3.10。

---

## 一、環境準備

需要主機安裝 Docker（含 Docker Compose v2，`docker compose`）。

```bash
# 在專案根目錄
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
```

`.env` 內填 LINE 推播用的 `LINE_CHANNEL_TOKEN` / `LINE_USER_ID`（不用 LINE 可留空）。
`.env` 已被 `.gitignore` / `.dockerignore` 忽略，不會進 git、也不會被烤進映像，而是在容器
啟動時由 compose 的 `env_file` 注入。

---

## 二、用 Docker Compose（建議）

```bash
docker compose up -d --build      # 建置並背景啟動 (常駐每分鐘篩選)
docker compose logs -f            # 看即時輸出 (排行表)
docker compose down               # 停止
```

啟動後：

- 歷史日K 快取存在 named volume `stock-cache`（`/app/.cache`），重啟不必重抓全市場。
- Excel 輸出 `ranking.xlsx` 落到主機 **`./data/`**，可直接開啟。
- `restart: unless-stopped`：當機 / 重開機自動拉回。

### 改變執行行為

編輯 `docker-compose.yml` 的 `command`，例如開啟每日 13:00 LINE 推播：

```yaml
command: ["--excel", "/app/data/ranking.xlsx", "--notify", "line"]
```

其餘參數（`--min-change`、`--top`、`--no-breakout`、`--once` …）見 `README.md` 的「用法」。

---

## 三、用純 Docker（不透過 Compose）

```bash
docker build -t stock-quant:latest .

# 常駐
docker run -d --name stock-quant \
  -e TZ=Asia/Taipei \
  --env-file .env \
  -v stock-cache:/app/.cache \
  -v "$(pwd)/data:/app/data" \
  --restart unless-stopped \
  stock-quant:latest

# 立刻跑一次就結束 (覆寫 CMD 帶自訂參數)
docker run --rm \
  -e TZ=Asia/Taipei --env-file .env \
  -v stock-cache:/app/.cache -v "$(pwd)/data:/app/data" \
  stock-quant:latest --once --excel /app/data/ranking.xlsx
```

> 時區很重要：程式用系統本地時間判斷盤中時段（09:00–13:30），映像已固定
> `TZ=Asia/Taipei` 並安裝 `tzdata`。

---

## 四、本機開發（不用 Docker，直接用 Poetry）

```bash
poetry install                    # 依 pyproject.toml 建 venv 並裝相依
poetry run python run_intraday.py --once
poetry run stock-quant --once     # 等同上一行 (已註冊 console script)
poetry run python tests/run_tests.py   # 53/53 通過
```

---

## 檔案說明

| 檔案 | 作用 |
|------|------|
| `pyproject.toml` | Poetry 專案定義：Python 3.10、`openpyxl`、`stock-quant` console script |
| `poetry.lock` | 相依鎖定（首次 `poetry install` 或 build 時自動產生，建議 commit 以求可重現）|
| `Dockerfile` | 多階段建置：Poetry 裝套件 → 精簡 runtime（非 root、台北時區）|
| `docker-compose.yml` | 一鍵啟停：env_file 注入密鑰、快取與輸出 volume、自動重啟 |
| `.dockerignore` | 排除 git/快取/密鑰/輸出，縮小建置脈絡與映像 |

## 持久化資料

| 路徑（容器內） | 對應 | 用途 |
|----------------|------|------|
| `/app/.cache` | named volume `stock-cache` | 歷史日K 增量快取（`history.pkl`）|
| `/app/data` | 主機 `./data` | Excel 輸出 `ranking.xlsx` |
