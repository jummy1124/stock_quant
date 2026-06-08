# 台股個股趨勢分析 — 架構設計 (盤後 + 盤中即時)

兩種模式都用「多指標綜合評分」判斷趨勢、都用多進程平行。分層、依賴反轉 (DIP)。

## 分層

```
   run.py (盤後)                 run_intraday.py (盤中即時, 每分鐘)
      │                              │
   scanner (多進程逐檔)           intraday (快取歷史 + 每分鐘即時重算)
      │                            │   │   │
      │                       scheduler │ datasource.mis (即時, 批次+多進程)
      │                                 │
      └──────────── analysis (indicators + TrendClassifier) ───────────
                              │
   universe → datasource  個股清單(EOD) + 歷史日K(history) + 即時(mis)
                              │
                          domain  DailyQuote / Market / is_individual_stock
```

## EOD / 歷史 / 即時 三種資料，各司其職

- **個股清單 (EOD OpenAPI)**：`TwseDataSource`/`TpexDataSource` 列出「有哪些個股」。
- **歷史日K (history)**：判斷趨勢的基底序列（MA60 等需要約 60+ 根）。盤中不變。
- **盤中即時 (mis)**：MIS API，盤中數秒更新。`fetch_realtime` 批次 + 多進程。

官方 OpenAPI 是盤後 EOD（盤中不更新），所以盤中即時必須用 MIS。三者都正規化成同一個
`DailyQuote`，分析層不在乎資料怎麼來 —— 依賴反轉的好處。

## 盤中監控怎麼做到「即時 + 每分鐘 + 多進程」

取得歷史日K (盤中不變，啟動取得一次)：
- **全市場**：`datasource.market_history.load_market_history` 用『逐日整批』端點
  （上市 MI_INDEX、上櫃每日收盤）—— 一天一次請求拿到當天全市場，抓 ~75 個交易日
  只要 ~75 次請求，對 TWSE 限流友善；並本地快取 (`.cache/history.pkl`)。
  ❗ 逐檔抓全市場歷史 (STOCK_DAY) 要近萬次請求且會被限流而卡住，故全市場不走逐檔。
- **指定少數個股**：`IntradayTrendMonitor.prepare()` 逐檔 `get_history`（多進程，檔數少 OK）。

每分鐘 `tick(now)`：用 `fetch_realtime`（MIS 批次 + 多進程）抓現價，
把現價當「今日即時K」接在歷史後面（`series = 歷史(今日前) + 即時K`），
再用 `TrendClassifier` 重算趨勢 → 趨勢隨盤中分鐘級變動。
`scheduler.run_market_loop` 負責盤中(09:00–13:30)每 interval 秒呼叫 `tick`。

多進程用在 I/O 瓶頸：每分鐘抓即時價（逐批平行）、指定個股時逐檔抓歷史。

## 趨勢判斷 (analysis)

`indicators`：SMA/EMA/MACD/ADX(+DI/-DI)/線性斜率。
`TrendClassifier`：五訊號投票加總 + ADX 趨勢強度過濾 → 多頭/空頭/盤整/資料不足 + 信心度。

## 擴充性

- 加趨勢訊號：`TrendClassifier.classify` 加一筆 vote。
- 換即時/歷史來源：新增對應實作。
- 存每分鐘快照/回測：加 storage 層，把 `ScanResult` 落地。

## 已驗證

`tests/`（test_core + test_trend + test_intraday）共 30 個不需網路的單元測試：
指標、分類器(多頭/空頭/盤整/資料不足)、EOD 解析與過濾、歷史日K解析、universe、
盤後多進程掃描、MIS 即時解析、盤中時段、常駐迴圈、盤中 tick（即時價接今日K重算趨勢、
bars=歷史+1）、prepare 快取。執行 `python tests/run_tests.py`，30/30 通過。
