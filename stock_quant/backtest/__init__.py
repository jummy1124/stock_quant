"""回測模組 — 重用專案最新的『兩段式選股』(IntradayRanker 漲幅池 + breakout_screen 起漲篩選) 做歷史回測。

  - dataset : 取得/快取全市場完成日K，組成 {代號: 升冪日K} 與交易日序列 (含髒價清洗)。
  - engine  : 逐日以收盤完成K，複製『漲幅 3%~漲停前一檔』池 + screen_breakout 6 條件起漲篩選
              + 強度分排序選股，再模擬下單 (庫存上限、部位大小、跌破5MA/停損出場、漲停不買跌停不賣)。
  - report  : 把回測結果寫成 Excel 績效報告 (需要 openpyxl)。
"""
from .engine import Backtester, BacktestConfig, BacktestResult, Trade

__all__ = ["Backtester", "BacktestConfig", "BacktestResult", "Trade"]
