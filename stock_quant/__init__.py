"""stock_quant - 台股爬蟲 (第一階段)。

目前範圍: 只做「多進程爬蟲」，抓取台股上市/上櫃最後交易日資訊並印出，
尚未接資料庫與技術分析。

分層 (保持可擴充，未來再往上加):
    domain      - 核心資料模型 (DailyQuote / Market)，零外部依賴
    datasource  - 官方 OpenAPI 資料來源抽象與實作 (TWSE 上市 / TPEx 上櫃)
    crawler     - 多進程抓取編排

設計原則: 依賴反轉 (上層依賴抽象介面)。未來要加 storage / analysis / api，
只需新增對應模組，既有程式不用改。
"""

__version__ = "0.2.0"
