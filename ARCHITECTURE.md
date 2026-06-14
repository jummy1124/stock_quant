# 台股個股漲幅排行 — 架構設計 (盤後 + 盤中即時)

篩出「漲幅 3%~漲停前一檔」的個股、依漲幅排序，盤中每分鐘更新，print (含名稱) 並寫進 Excel。分層、依賴反轉 (DIP)。

## 分層

```
   run_intraday.py (盤中即時, 每分鐘 / 盤後 EOD)
      │
   intraday.IntradayRanker (快取歷史取昨收 + 每分鐘抓即時價算漲幅 + 排序)
      │        │
      │   datasource.mis (即時, 批次 + 多進程)
      │
   excel_export.save_ranking (openpyxl，覆蓋寫入 ranking.xlsx)
      │
   universe → datasource  個股清單(EOD) + 歷史日K + 即時(mis)
      │
   domain  DailyQuote / Market / is_individual_stock
```

## 漲幅計算 + 篩選 (intraday.py)

`IntradayRanker.tick(now)` 對每檔有「昨收(歷史)」與「今日現價(即時)」的個股計算：

    漲幅% = (今收 − 昨收) / 昨收 × 100

預設篩選 (apply_filter=True): 漲幅 ≥ min_change_pct (預設 3%) 且 收盤 ≤ 漲停前一檔
(limits.limit_up_prev_tick；exclude_limit_up 可關)。通過者回傳 `RankRow`
(代號/名稱/市場/現價/昨收/漲跌/漲幅%/量…) 並依漲幅由大到小排序。
沒拿到即時價或沒有昨收的個股當次跳過。
名稱以 name_map ({代號:名稱}，由 universe.load_name_map 建表) 優先補上。

## 資料來源自動切換

- 交易時間 (09:00–13:30)：抓 MIS 即時價當「今日K」，與歷史最後一日(昨收)比對。
- 非交易時間：用最後一個交易日的完成日K (`history[-1]`) 與前一日 (`history[-2]`) 算漲幅
  (可用 `--no-screen-when-closed` 關閉、非盤中就純休眠)。
- `scheduler.run_market_loop` 負責盤中每 interval 秒呼叫 tick。

## 輸出

- 畫面：`run_intraday._print_ranking` 印出排行表 (可用 `--top N` 只看前 N 名)。
- Excel：`excel_export.save_ranking` 用 openpyxl 把「全部」排行覆蓋寫入同一個 `ranking.xlsx`
  (可用 `--excel PATH` 改路徑、`--no-excel` 關閉)。檔案被 Excel 開著鎖住時略過該次、不中斷迴圈。
- LINE：`--notify line` 時把漲幅前幾名 (`--notify-top`) 推到 LINE，預設每日 13:00 彙整一次。

## 成交量單位 (重要)

不同來源量的單位不一致：MIS 即時與 TPEx `tradingStock` 是「張」，TWSE 是「股」。
統一在資料來源轉成「股」(張 ×1000)；顯示時 `RankRow.lots` 再換回「張」。

## 擴充性

- 換即時/歷史來源：新增對應實作 (都輸出同一個 `DailyQuote`)。
- 加欄位/改排序：調整 `RankRow` 與 `IntradayRanker._sort`。
- 落地每分鐘排行：擴充 `excel_export` 或加 storage 層。

## 已驗證

`tests/`（test_core + test_intraday + test_market_history + test_notify）共 45 個不需網路測試：
即時解析(量轉股)、漲幅計算與排序、live/eod 切換、缺資料跳過、prepare、逐日整批與逐檔歷史解析、
universe、快取、LINE 推播彙整。執行 `python tests/run_tests.py`，45/45 通過。

依賴：標準函式庫 + `openpyxl` (寫 Excel)。
