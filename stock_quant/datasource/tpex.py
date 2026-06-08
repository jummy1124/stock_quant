"""上櫃 (TPEx) 資料來源 — 使用證券櫃檯買賣中心官方 OpenAPI。

端點: https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
      「上櫃股票每日收盤行情」— 一次回傳『最後一個交易日』全部上櫃個股。

預設 only_individual=True: 只保留普通個股，濾掉 ETF/權證/特別股等非個股商品。
TPEx 欄位名歷年調整較多，故用多候選名 (base.pick) 防禦式解析。
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from ..domain import DailyQuote, Market, is_individual_stock
from .base import FetchUnit, IDataSource, pick
from .dates import latest_trading_day, parse_roc_date
from .http import get_json


class TpexDataSource(IDataSource):
    name = "tpex"
    market = Market.TPEX

    URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

    def __init__(self, trade_date: Optional[date] = None, timeout: float = 30.0,
                 only_individual: bool = True):
        self._trade_date = trade_date
        self._timeout = timeout
        self._only_individual = only_individual

    def list_fetch_units(self) -> Sequence[FetchUnit]:
        return [FetchUnit(source_name=self.name, market=self.market, label="上櫃個股")]

    def fetch(self, unit: FetchUnit) -> list[DailyQuote]:
        rows = get_json(self.URL, timeout=self._timeout)
        quotes: list[DailyQuote] = []
        for row in rows:
            symbol = pick(row, "SecuritiesCompanyCode", "Code", "code", "股票代號")
            if not symbol:
                continue
            symbol = str(symbol).strip()
            if self._only_individual and not is_individual_stock(symbol):
                continue  # 濾掉 ETF / 權證 / 特別股 等非個股
            row_date = (
                self._trade_date
                or parse_roc_date(pick(row, "Date", "date"))
                or latest_trading_day()
            )
            q = DailyQuote.normalize(
                symbol=symbol,
                name=pick(row, "CompanyName", "Name", "name", "名稱") or "",
                market=self.market,
                trade_date=row_date,
                open=pick(row, "Open", "OpeningPrice"),
                high=pick(row, "High", "HighestPrice"),
                low=pick(row, "Low", "LowestPrice"),
                close=pick(row, "Close", "ClosingPrice"),
                change=pick(row, "Change"),
                volume=pick(row, "TradingShares", "TradeVolume"),
                turnover=pick(row, "TransactionAmount", "TradeValue"),
                transactions=pick(row, "TransactionNumber", "Transaction"),
            )
            if q.is_valid():
                quotes.append(q)
        return quotes
