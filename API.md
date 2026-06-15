# HTTP API — 盤中起漲篩選 (給前端用)

把盤中篩選結果以 JSON 提供給 **另一個 repo 的前端**。框架用 **FastAPI**，
即時做法是 **背景定時刷新 + 前端輪詢**：`run_intraday.py --serve` 的每分鐘迴圈跑一次
`IntradayRanker.tick()` + 起漲 6 條件篩選後，把同一份結果 publish 成「最新快照」；前端
`GET /api/screen` 直接讀這份快照（毫秒回應），**不會每次請求都打爆 MIS 即時 API，
也不會和 CLI 重複爬取**。

資料來源沿用既有邏輯自動切換：交易時間（週一至五 09:00–13:30）抓 MIS 即時價；
非交易時間用最後一個交易日完成日K。

> ⚠️ 篩選為機率性資訊參考，非投資建議。

## 啟動

先裝 API 相依（CLI/排程不需要）：

```bash
poetry install --with api          # 或: pip install fastapi "uvicorn[standard]"
```

用 `run_intraday.py --serve` 在同一進程啟動：CLI 每分鐘 tick 一次，
**同一份結果同時供 console/Excel/LINE 與 API**，所以即時價只會被抓一次（不會重複爬）。
前端輪詢 `GET /api/screen` 讀本進程 publish 的快照，API 自己不抓即時價。

```bash
python run_intraday.py --serve                       # 篩選 + 寫 Excel + 內嵌 API:8000
python run_intraday.py --serve --api-port 8080
python run_intraday.py --serve --notify line         # 再加 LINE 推播
python run_intraday.py --serve --api-origins https://my-frontend.app

# Docker：預設 service 就是這個模式 (已含 --serve，對外 8000)
docker compose up -d --build
```

啟動後開 <http://localhost:8000/docs> 看 Swagger UI（互動式測試 + 完整 schema）。
首次篩選完成前 `GET /api/screen` 會回 `503`，請輪詢重試。

## 端點

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/health` | 存活檢查；回 `ready` / `has_snapshot` |
| GET | `/api/meta` | 只回中繼資訊（資料時間、資料源、檔數、警告） |
| GET | `/api/screen` | **主端點**：最新起漲個股（6 條件），依強度分排序 |
| GET | `/api/pool` | 第一層漲幅池（3%~漲停前一檔），除錯/備用 |

`/api/screen` 查詢參數：`top`（只取前 N 名，0=全部）、`min_score`（強度分下限）。
`/api/pool` 查詢參數：`top`。

### `GET /api/screen` 回應範例

```jsonc
{
  "meta": {
    "generated_at": "2026-06-12T11:30:00",
    "age_seconds": 7.3,
    "source": "live",          // live=盤中即時 / eod=最後交易日
    "universe": 1955,           // 全市場掃描檔數
    "quotable": 1900,           // 可算漲幅檔數
    "pool_size": 12,            // 通過漲幅池檔數
    "count": 3,                 // 本次回傳筆數
    "warning": null,            // 即時報價限流摘要 (若有)
    "last_error": null
  },
  "results": [
    {
      "symbol": "2330", "name": "台積電",
      "market": "上市", "market_code": "TWSE",
      "close": 109.5, "prev_close": 100.0,
      "change": 9.5, "change_pct": 9.5,
      "volume": 25000, "lots": 25.0,
      "open": 101.0, "high": 110.0, "low": 100.5,
      "score": 86.4, "prev_high": 102.0, "vol_ratio": 1.8,
      "ma5": 98.2, "ma20": 95.1, "ma20_up": true,
      "reasons": ["紅K", "突破昨高102", "量比1.80", "站上5MA", "站上月線↑", "昨收<昨日5MA"]
    }
  ]
}
```

資料未備妥時回 `503`，body 仍是同結構（`results: []` + `detail`）。

## 前端串接（另一個 repo）

跨網域要設 CORS：用 `--api-origins`（或環境變數 `ALLOWED_ORIGINS`）指定前端網址
（逗號分隔多個），預設 `*` 方便開發。

```bash
python run_intraday.py --serve --api-origins https://my-frontend.app,http://localhost:5173
```

前端輪詢範例（每 30 秒拉一次，盤中刷新間隔預設 60s，30s 輪詢足夠即時）：

```js
const API = "http://localhost:8000";

async function fetchScreen() {
  const res = await fetch(`${API}/api/screen?top=50`);
  if (res.status === 503) return null;        // 後端仍在備資料，稍後再試
  const { meta, results } = await res.json();
  return { meta, results };
}

setInterval(async () => {
  const data = await fetchScreen();
  if (data) render(data.results, data.meta);   // meta.age_seconds 可顯示「N 秒前更新」
}, 30_000);
```

## 設定

篩選參數（市場、漲幅下限、量比、間隔…）一律用 `run_intraday.py` 既有的 CLI 旗標
與 API 旗標控制，見 `python run_intraday.py --help`：

| 旗標 | 預設 | 說明 |
| --- | --- | --- |
| `--serve` | 關 | 啟動內嵌 API |
| `--api-host` | `0.0.0.0` | API 監聽位址 |
| `--api-port` | `8000` | API 監聽埠 |
| `--api-origins` | `*` | CORS 允許來源，逗號分隔（也可用環境變數 `ALLOWED_ORIGINS`） |

API 層唯一讀的環境變數是 `ALLOWED_ORIGINS`（CORS，預設 `*`）；`--api-origins` 會覆蓋它。

## 架構備註

- `stock_quant/screen_service.py`：`ScreenService` 是**純快照持有者**
  （`attach_ranker` 沿用 CLI 的 ranker、`publish` 發佈每分鐘結果、`snapshot` 給端點讀），
  自己不抓資料、不跑迴圈 → 不會和 CLI 重複爬。另含純 dict 序列化。
- `stock_quant/api.py`：`create_app(service)` 建 FastAPI app（CORS、端點、pydantic 回應模型）。
- `run_intraday.py --serve`：`_start_api_server()` 在背景 thread 起 uvicorn，
  `_cycle()` 每次 tick 後 `service.publish(...)`。
- 篩選/漲幅/資料源邏輯完全沿用既有模組，API 只是「讀快照 → 序列化 JSON」這一層。
