# 台股個股爬蟲 — 架構設計

提供「盤後 EOD」與「盤中即時」兩種爬法，都只保留普通個股、都只 print（不接資料庫）。
分層與介面按可擴充原則設計；核心原則：**上層只依賴抽象介面（依賴反轉，DIP）**。

## 分層

```
   run_crawl.py (EOD)            run_intraday.py (盤中即時, 每分鐘常駐迴圈)
        │                              │              │
        │                         scheduler       universe (由 EOD 取得個股清單)
        │                              │              │
        └──────────────┬──────────────┴──────────────┘
                  crawler        MultiProcessCrawler 多進程抓取
                       │
                  datasource     TWSE/TPEx (EOD OpenAPI) + MIS (盤中即時, 批次)
                       │
                  domain         DailyQuote / Market / is_individual_stock
```

## EOD vs 盤中即時 — 為何需要兩種來源

官方 OpenAPI（`STOCK_DAY_ALL`、`tpex_mainboard_daily_close_quotes`）是**盤後 EOD**，
盤中不更新，無法做即時。盤中即時改用證交所 **MIS API**（盤中數秒更新）。
兩者都實作同一個 `IDataSource` 介面、都輸出同一個 `DailyQuote`，所以
crawler 與下游完全共用，只是換了資料來源 —— 這就是依賴反轉的好處。

## 各層職責

**domain**　`DailyQuote` 不可變值物件 + `normalize()` 髒值清洗；
`is_individual_stock(code)` 是「只保留個股」的單一判斷來源。

**datasource**
- `TwseDataSource` / `TpexDataSource`：EOD OpenAPI 全市場快照，解析後用
  `is_individual_stock` 過濾，下游只看得到個股。
- `MisRealtimeDataSource`：盤中即時。接 (代號, 市場) 清單，**切成 batch_size 一批**，
  每批一個 `FetchUnit`，MIS 一次可帶多檔 (ex_ch 用 `|` 串接)。

**crawler**　`MultiProcessCrawler` 把所有 `FetchUnit` fan-out 到多進程平行抓。
EOD 是每市場一個 unit；盤中是每『一批個股』一個 unit —— 同一個 crawler，不同切分粒度。

**universe**　盤中即時需先知道要抓哪些檔。`load_individual_universe()` 用 EOD 來源
取得全市場個股清單 (代號, 市場)，餵給 MIS。

**scheduler**　`MarketClock.is_trading(now)` 判斷是否盤中 (09:00–13:30, 週一至五，
未含國定假日，假設系統為台北時間)。`run_market_loop()` 是常駐迴圈：盤中每
interval 秒呼叫 task 一次，非盤中休眠；可注入假時鐘 / sleep 以便測試。

## 多進程設計重點

平行化的關鍵是 `FetchUnit`。盤中把上千檔切成數十檔一批、每批一個 unit，
多進程同時打 MIS，全市場一分鐘內抓得完。切分粒度由資料來源決定，crawler 通用。

## 流量與限制 (盤中)

MIS 有流量限制，全市場每分鐘輪詢量大，可能被限流/暫時封鎖。可調 `interval`、
`batch_size`，或縮小 universe。盤中才有即時價。

## 未來擴充路徑（皆不需動爬蟲程式）

- **存每分鐘快照**：新增 storage 層（`IQuoteRepository` + SQLite/PostgreSQL/CSV），
  在 task 內把 `crawler.crawl()` 結果寫入即可。
- **技術分析**：新增 analysis 層，定義 `IIndicator` 策略介面。
- **API / 前端**：加 service 編排層 + FastAPI，前端消費 API。

## 已驗證

`tests/`（test_core + test_intraday）共 14 個不需網路的單元測試，涵蓋髒值正規化、
個股判斷、EOD 解析與過濾、MIS 批次解析與分批、盤中時段判斷、常駐迴圈（假時鐘）、
universe 組裝、多進程端到端與錯誤隔離。執行 `python tests/run_tests.py`，目前 14/14 通過。
