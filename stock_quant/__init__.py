"""stock_quant - 台股個股漲幅排行 (盤中即時 + 盤後)。

對全市場個股算漲幅% = (今收 - 昨收)/昨收 ×100，由大到小排序，並用多進程平行抓即時價。

單一進入點 run_intraday.py，資料來源依時間自動切換:
  - 交易時間 (09:00–13:30): 每分鐘用 MIS 即時價當「今日K」算漲幅 -> print + 寫 Excel，可選 LINE 推播。
  - 非交易時間            : 用最後一個交易日的完成日K算漲幅。

分層 (依賴反轉，上層只依賴抽象):
    domain        - DailyQuote / Market / is_individual_stock
    datasource    - 個股清單(EOD) + 歷史日K(history/逐日整批,增量快取) + 盤中即時(mis)
    intraday      - 漲幅排行引擎 IntradayRanker (快取歷史 + 盤中即時/盤後EOD 自動切換)
    excel_export  - 把漲幅排行寫進 .xlsx
    scheduler     - 盤中時段判斷 + 常駐迴圈
    notify        - LINE 推播 (每日彙整 / 即時) + .env 載入
    universe      - 全市場個股清單
"""

__version__ = "0.7.0"
