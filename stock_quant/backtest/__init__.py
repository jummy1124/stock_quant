"""回測模組 — 重用 analysis.BreakoutScreen 的選股邏輯做歷史回測。

分層維持與專案一致 (依賴反轉、零第三方核心邏輯)：
  - dataset : 取得/快取「全市場日K」，組成 {代號: 升冪日K} 與交易日序列。
  - engine  : 逐日以「收盤價」篩選 + 模擬下單 (庫存上限五檔、每檔一張、跌破MA5出場)。
  - report  : 把回測結果寫成 Excel 績效報告 (需要 openpyxl)。
"""
from .engine import Backtester, BacktestConfig, BacktestResult, Trade

__all__ = ["Backtester", "BacktestConfig", "BacktestResult", "Trade"]
