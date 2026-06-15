# syntax=docker/dockerfile:1

###############################################################################
# Stage 1 — builder：用 Poetry 把相依套件裝進獨立的 .venv
###############################################################################
FROM python:3.10-slim AS builder

ENV POETRY_VERSION=1.8.3 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=true \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 只裝 Poetry 本體 (隔離在 /opt/poetry，不污染專案 venv)
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# 先只複製相依宣告，最大化 Docker layer 快取：原始碼變動時不必重裝套件。
# poetry.lock 用萬用字元複製：有就用 (可重現)，沒有也不會 build 失敗。
COPY pyproject.toml poetry.lock* ./

# 沒有 lock 就先產生一份，再只裝 runtime 相依 (不裝 dev、不裝專案本身)
# 裝 main + api 群組 (api 為 optional 群組，含 fastapi/uvicorn)，讓 image 能跑
# CLI 與內嵌 HTTP API (run_intraday.py --serve)。
RUN if [ ! -f poetry.lock ]; then poetry lock; fi \
    && poetry install --only main,api --no-root

# 複製原始碼。容器以 `python run_intraday.py` 直接執行 (PYTHONPATH=/app 即可 import
# stock_quant)，不需把專案本身裝進 venv；故不再 poetry install root，
# 也避免 poetry 打包時讀 README 觸發編碼錯誤。相依已在上一步 (--no-root) 裝好。
COPY . .

###############################################################################
# Stage 2 — runtime：只帶 venv + 原始碼，體積小、無建置工具
###############################################################################
FROM python:3.10-slim AS runtime

# 台股時間判斷依賴系統本地時間，固定為台北時區
ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# tzdata 讓 TZ 生效；建立非 root 使用者
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && useradd --create-home --uid 1000 appuser

WORKDIR /app

# 從 builder 帶入已裝好的虛擬環境與原始碼
COPY --from=builder /app /app

# 快取與輸出目錄 (對應 compose / -v 掛載的 volume)，並交給非 root 使用者
RUN mkdir -p /app/.cache /app/data && chown -R appuser:appuser /app

USER appuser

# 持久化：歷史日K 快取 + Excel 輸出
VOLUME ["/app/.cache", "/app/data"]

# 預設常駐每分鐘篩選 + 內嵌 API；Excel 寫到掛載的 /app/data。
# 一次性執行：docker run ... --once；其餘參數見 README / API.md。
ENTRYPOINT ["python", "run_intraday.py"]
CMD ["--excel", "/app/data/ranking.xlsx", "--serve", "--api-port", "8000"]
