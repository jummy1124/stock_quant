"""上市 (TWSE) 資料來源 — 使用臺灣證券交易所官方 OpenAPI。

端點: https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
      「上市個股日成交資訊」— 一次回傳『最後一個交易日』全部上市證券。

預設 only_individual=True: 只保留普通個股，濾掉 ETF/權證/特別股等非個股商品。
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from ..domain import DailyQuote, Market, is_individual_stock
from .base import FetchUnit, IDataSource, pick
from .dates import latest_trading_day
from .http import get_json


class TwseDataSource(IDataSource):
    name = "twse"
    market = Market.TWSE

    URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

    def __init__(self, trade_date: Optional[date] = None, timeout: float = 30.0,
                 only_individual: bool = True):
        self._trade_date = trade_date or latest_trading_day()
        self._timeout = timeout
        self._only_individual = only_individual

    def list_fetch_units(self) -> Sequence[FetchUnit]:
        return [FetchUnit(source_name=self.name, market=self.market, label="上市個股")]

    def fetch(self, unit: FetchUnit) -> list[DailyQuote]:
        rows = get_json(self.URL, timeout=self._timeout)
        quotes: list[DailyQuote] = []
        for row in rows:
            symbol = pick(row, "Code", "code")
            if not symbol:
                continue
            symbol = str(symbol).strip()
            if self._only_individual and not is_individual_stock(symbol):
                continue  # 濾掉 ETF / 權證 / 特別股 等非個股
            q = DailyQuote.normalize(
                symbol=symbol,
                name=pick(row, "Name", "name") or "",
                market=self.market,
                trade_date=self._trade_date,
                open=pick(row, "OpeningPrice"),
                high=pick(row, "HighestPrice"),
                low=pick(row, "LowestPrice"),
                close=pick(row, "ClosingPrice"),
                change=pick(row, "Change"),
                volume=pick(row, "TradeVolume"),
                turnover=pick(row, "TradeValue"),
                transactions=pick(row, "Transaction"),
            )
            if q.is_valid():
                quotes.append(q)
        return quotes
