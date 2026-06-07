"""上市 (TWSE) 資料來源 — 使用臺灣證券交易所官方 OpenAPI。

端點: https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
      「上市個股日成交資訊」— 一次回傳『最後一個交易日』全部上市證券。

預設 only_individual=True: 只保留普通個股，濾掉 ETF/權證/特別股等非個股商品。
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from ..domain import DailyQuote, Market, is_individual_stock
from .base import FetchUnit, IDataSource
from .dates import latest_trading_day
from .http import get_json


def _pick(row: dict, *candidates: str):
    """從一筆 row 依序嘗試多個可能的欄位名，回傳第一個存在的值。"""
    for key in candidates:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


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
            symbol = _pick(row, "Code", "code")
            if not symbol:
                continue
            symbol = str(symbol).strip()
            if self._only_individual and not is_individual_stock(symbol):
                continue  # 濾掉 ETF / 權證 / 特別股 等非個股
            q = DailyQuote.normalize(
                symbol=symbol,
                name=_pick(row, "Name", "name") or "",
                market=self.market,
                trade_date=self._trade_date,
                open=_pick(row, "OpeningPrice"),
                high=_pick(row, "HighestPrice"),
                low=_pick(row, "LowestPrice"),
                close=_pick(row, "ClosingPrice"),
                change=_pick(row, "Change"),
                volume=_pick(row, "TradeVolume"),
                turnover=_pick(row, "TradeValue"),
                transactions=_pick(row, "Transaction"),
            )
            if q.is_valid():
                quotes.append(q)
        return quotes
