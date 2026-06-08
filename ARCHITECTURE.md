# 台股個股選股 — 架構設計 (盤後 + 盤中即時)

把原本的「趨勢分類」改成 **4 規則選股篩選 (BreakoutScreen)**。分層、依賴反轉 (DIP)。

## 分層

```
   run.py (盤後)                  run_intraday.py (盤中即時, 每分鐘)
      │                               │
      │                          intraday (快取歷史 + 每分鐘即時篩選)
      │                            │        │
      │                       scheduler   datasource.mis (即時, 批次+多進程)
      │                                     │
      └────────────── analysis.BreakoutScreen ─────────────
                              │
   universe → datasource  個股清單(EOD) + 歷史日K + 即時(mis)
                              │
                          domain  DailyQuote / Market / is_individual_stock
```

## 選股篩選 (analysis/screen.py)

`BreakoutScreen.check(today, prev)` 對「當日K」與「前一交易日」檢查 4 條規則：
紅K、漲幅 3%~漲停前一檔（用台股升降單位 `tick_size`/`limit_up_price` 計算）、
上影線 ≤ 1%、量增 ≥ 1.2 倍。回傳 `ScreenResult`(passed + 漲幅%/上影線%/量比/收盤)。

## 成交量單位 (重要)

不同來源量的單位不一致：MIS 即時與 TPEx `tradingStock` 是「張」，TWSE 是「股」。
為讓「量比」正確，統一在資料來源轉成「股」(張 ×1000)：MIS 與 TPEx 歷史皆 ×1000。

## 盤中怎麼做到「即時 + 每分鐘 + 多進程」

- 啟動：歷史日K盤中不變，取得一次並快取（全市場逐日整批 / 指定個股逐檔，皆可多進程）。
- 每分鐘 `IntradayScreener.tick`：`fetch_realtime`(MIS 批次 + 多進程)抓現價當今日K，
  與昨日(`history[-1]`)比對做篩選；拿不到即時價時退用最後完整日K。
- `scheduler.run_market_loop` 負責盤中(09:00–13:30)每 interval 秒呼叫 tick。

## 擴充性

- 改規則 / 加條件：調整 `BreakoutScreen` 參數或在 `check()` 增加判斷。
- 換即時/歷史來源：新增對應實作 (都輸出同一個 `DailyQuote`)。
- 存每分鐘命中清單/回測：加 storage 層落地 `ScreenResult`。

## 已驗證

`tests/`（test_core + test_screen + test_intraday + test_market_history）共 30 個不需網路測試：
升降單位/漲停價、4 條規則各自命中與不命中、缺資料、MIS 即時解析(量轉股)、
盤中 tick(即時/退歷史/無歷史略過)、prepare、逐日整批與逐檔歷史解析、universe、快取。
執行 `python tests/run_tests.py`，30/30 通過。
