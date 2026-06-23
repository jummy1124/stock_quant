# HTTP API — 盤中起漲篩選 (給前端用)

把盤中篩選結果以 JSON 提供給 **另一個 repo 的前端**。框架用 **FastAPI**，
即時做法是 **背景定時刷新 + 前端輪詢**：`run_intraday.py --serve` 的每分鐘迴圈跑一次
`IntradayRanker.tick()` + 起漲 6 條件篩選後，把同一份結果 publish 成「最新快照」；前端
`GET /api/screen` 直接讀這份快照（毫秒回應），**不會每次請求都打爆 MIS 即時 API，
也不會和 CLI 重複爬取**。

快照同時保留**未過濾的原始報價列**（`raw`），因此 `GET /api/screen` / `GET /api/pool`
可接受篩選參數，在每次請求時**依使用者參數即時重算**漲幅池與起漲 6 條件——只是對同一份
快照重新計算，不會重抓即時價，所以**各使用者的參數互不影響、也不增加任何爬取**。所有參數
省略時即等於原專案預設（見 `GET /api/screen-defaults`）。

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
| GET | `/api/screen` | **主端點**：最新起漲個股（6 條件，參數可調） |
| GET | `/api/screen-defaults` | 篩選參數預設值（前端「恢復預設」用） |
| GET | `/api/pool` | 第一層漲幅池（預設 3%~漲停前一檔，參數可調），除錯/備用 |
| GET | `/api/history/{symbol}` | **個股歷史日K**：OHLC + 成交量 + MA5/20/60（給 K 線圖用） |
| GET | `/api/quote/{symbol}` | **個股盤中最新價**（單檔即時）：給圖表輪詢更新「今日K + 現價線」 |

`/api/screen` 查詢參數：`top`（只取前 N 名，0=全部）＋下表「篩選參數」（省略即用預設）。
`/api/pool` 查詢參數：`top` ＋漲幅池參數 `min_change`、`exclude_limit_up`。
`/api/screen-defaults` 無查詢參數，回傳下表所有參數的預設值。
`/api/history/{symbol}` 查詢參數：`months`（抓取月數 1~24，預設 6）、`market`（`TWSE`/`TPEX`，省略則自動判別）、`intraday`（`true` 時把今日盤中即時價接到日K尾端，交易時間有效；這根不進快取）。
`/api/quote/{symbol}` 查詢參數：`market`（`TWSE`/`TPEX`，省略則自動判別）。

### `GET /api/quote/{symbol}` 說明與回應範例

單檔盤中即時報價，搭配 `/api/history/{symbol}?intraday=true` 使用：開圖時用 history 帶今日K，
之後前端每 ~30 秒輪詢本端點更新最右邊那根 K 棒與「現價線」。交易時間（週一至五 09:00–13:30）
回 MIS 盤中即時價（`source=live`）；非交易時間回最後成交價（`source=eod`）。查無資料回 `200` + `candle=null`。

```jsonc
{
  "symbol": "2330", "market": "上市", "market_code": "TWSE",
  "trading": true, "source": "live", "as_of": "2026-06-22T10:32:00",
  "prev_close": 104.0, "close": 107.0, "change": 3.0, "change_pct": 2.88,
  "candle": { "date": "2026-06-22", "open": 104.0, "high": 108.0, "low": 103.0,
              "close": 107.0, "volume": 500000, "lots": 500.0, "change": 3.0 }
}
```

### `GET /api/history/{symbol}` 說明與回應範例

與 `/api/screen` 讀「每分鐘快照」不同，歷史端點是**即時向交易所逐月抓取**
（上市 TWSE `STOCK_DAY`、上櫃 TPEx），在後端算好 5/20/60 日均線後回傳，並做
TTL 記憶體快取（盤後資料一天才變一次，預設 30 分鐘）。所以第一次查某檔較慢
（要逐月請求），之後同檔同區間都走快取、毫秒回應。`candles` 為**時間升冪**。
查無資料時回 `200` + `candles: []`（不是錯誤）；抓取失敗回 `502`。

```jsonc
{
  "symbol": "2330",
  "market": "上市",
  "market_code": "TWSE",
  "months": 6,
  "count": 122,
  "cached": false,
  "candles": [
    {
      "date": "2026-01-05",
      "open": 100.0, "high": 103.5, "low": 99.5, "close": 102.0,
      "volume": 25000000, "lots": 25000.0, "change": 2.0,
      "ma5": null, "ma20": null, "ma60": null   // 視窗未滿時為 null
    }
    // ...（升冪到最近交易日）
  ]
}
```

> 均線採簡單移動平均（SMA）；視窗未滿 N 根（或視窗內有缺值）該日均線為 `null`，
> 前端畫線時需略過 `null` 點。

### 篩選參數（`/api/screen` 可調，預設＝原專案設定）

所有參數皆為查詢字串、可省略；省略時用下表預設值（與 `run_intraday.py` CLI / `BreakoutConfig`
原始設定一致）。前端可先打 `GET /api/screen-defaults` 取得這份預設，做為「恢復預設」依據。

| 參數 | 型別 | 預設 | 層級 | 說明 |
| --- | --- | --- | --- | --- |
| `min_change` | float | `3.0` | 漲幅池 | 漲幅下限 %（≥ 此值才入池） |
| `exclude_limit_up` | bool | `true` | 漲幅池 | 排除已鎖漲停（收盤 ≤ 漲停前一檔） |
| `vol_ratio` | float | `1.2` | 起漲(條件3) | 當日量 / 昨量 下限（量增倍數），需 > 0 |
| `ma_short` | int | `5` | 起漲(條件4) | 短均線天數（站上 5MA），≥ 1 |
| `ma_mid` | int | `20` | 起漲(條件5) | 月均線天數（站上月線），≥ 1 |
| `ma_slope_lookback` | int | `5` | 起漲(條件5) | 月線上彎回看天數（今日 20MA > N 日前），≥ 0 |
| `vol_projection` | bool | `false` | 起漲(條件3) | 量能改用「全日預估量」比昨量（盤中早盤較公允） |

> 起漲篩選的其餘 3 條件為**固定**、不可調：紅K（現價 > 開盤）、突破前一交易日最高、
> 昨日仍在短均線下（鎖定當日才轉強上穿）。歷史K不足以算某條均線時，該條件自動略過（不誤殺）。

```bash
# 範例：漲幅 ≥ 2%、量增 ≥ 1.5 倍、月線改用 60 日季線
curl "http://localhost:8000/api/screen?min_change=2&vol_ratio=1.5&ma_mid=60&top=50"
```

`GET /api/screen-defaults` 回應：

```jsonc
{
  "min_change": 3.0, "exclude_limit_up": true,
  "vol_ratio": 1.2, "ma_short": 5, "ma_mid": 20,
  "ma_slope_lookback": 5, "vol_projection": false
}
```

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
      "prev_high": 102.0, "vol_ratio": 1.8,
      "ma5": 98.2, "ma20": 95.1, "ma20_up": true
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

篩選參數分兩種：

- **後端全域**（市場、歷史天數、刷新間隔、CLI 顯示/Excel/LINE 用的預設池）由 `run_intraday.py`
  的 CLI 旗標控制，見 `python run_intraday.py --help`。這也決定了 console/Excel/LINE 輸出與
  快照 `pool` / `breakout` 欄位（採預設參數）。
- **前端可調（每請求）**：`GET /api/screen` / `/api/pool` 的查詢參數（見上方「篩選參數」表），
  在請求時對同一份快照即時重算，**預設值＝原專案設定**，不影響後端全域與其他使用者。

啟動/API 旗標：

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
