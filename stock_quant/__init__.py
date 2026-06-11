"""stock_quant - 台股個股選股 (盤中即時 + 盤後)。

用 7 條規則 (BreakoutScreen) 篩出符合的個股，並用多進程平行:
  紅K、漲幅3%~漲停前一檔、上影線≤1%、量增1.2倍(同時段)、前一日收盤<MA5、
  今日現價>昨日最高、多頭趨勢閘門(站上月線+月線≥季線+頭頭高底底高)。

單一進入點 run_intraday.py，資料來源依時間自動切換:
  - 交易時間 (09:00–13:30): 每分鐘用 MIS 即時價當「今日K」篩選 -> print，可選 LINE 推播。
  - 非交易時間            : 用最後一個交易日的完成日K篩選。

分層 (依賴反轉，上層只依賴抽象):
    domain      - DailyQuote / Market / is_individual_stock
    datasource  - 個股清單(EOD) + 歷史日K(history/逐日整批,增量快取) + 盤中即時(mis)
    analysis    - 選股篩選器 BreakoutScreen (7 條規則)
    intraday    - 選股引擎 (快取歷史 + 盤中即時/盤後EOD 自動切換 + 穩定層)
    scheduler   - 盤中時段判斷 + 常駐迴圈
    notify      - LINE 推播 (每日彙整 / 即時) + .env 載入
    universe    - 全市場個股清單
"""

__version__ = "0.6.0"
