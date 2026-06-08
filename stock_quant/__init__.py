"""stock_quant - 台股個股趨勢分析 (盤後 + 盤中即時)。

兩種模式，都用多指標綜合評分判斷 多頭/空頭/盤整，並用多進程平行:
  - 盤後 (run.py)        : 列全市場個股 -> 逐檔抓歷史日K判斷趨勢 -> print。
  - 盤中即時 (run_intraday.py): 盤中每分鐘用 MIS 即時價接成今日K重算趨勢 -> print。

分層 (依賴反轉，上層只依賴抽象):
    domain      - DailyQuote / Market / is_individual_stock
    datasource  - 個股清單(EOD) + 歷史日K(history) + 盤中即時(mis)
    analysis    - 技術指標 + 趨勢分類器
    scanner     - 盤後多進程逐檔掃描趨勢
    intraday    - 盤中監控 (快取歷史 + 每分鐘即時重算)
    scheduler   - 盤中時段判斷 + 每分鐘迴圈
    universe    - 全市場個股清單
"""

__version__ = "0.5.0"
