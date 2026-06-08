"""個股歷史日K (逐檔) — 給「指定少數個股」與「上櫃全市場」用。

上市: 臺灣證交所 STOCK_DAY (逐月)。回傳 {"stat":"OK","data":[[日期,量,額,開,高,低,收,漲跌,筆數],...]}
上櫃: 證券櫃買中心 tradingStock (逐月)。回傳 {"tables":[{"data":[[日期,量,額,開,高,低,收,漲跌,筆數],...]}]}
      (端點與欄位順序依 twstock 維護版本確認)

回傳『時間升冪』的 list[DailyQuote]。get_history() 可自動偵測上市/上櫃。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from ..domain import DailyQuote, Market
from .dates import parse_roc_date
from .http import get_json

def _recent_month_firsts(months: int, today: Optional[date] = None) -> list[date]:
    d = today or date.today()
    y, m = d.year, d.month
    firsts = []
    for _ in range(months):
        firsts.append(date(y, m, 1))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(firsts))   # 升冪


def _row_to_quote(symbol, market, raw_date, r) -> Optional[DailyQuote]:
    """共用: 由一列 [日期,量,額,開,高,低,收,漲跌,筆數] 組 DailyQuote。"""
    d = parse_roc_date(raw_date)
    if d is None:
        return None
    q = DailyQuote.normalize(
        symbol=symbol, name="", market=market, trade_date=d,
        open=r[3] if len(r) > 3 else None,
        high=r[4] if len(r) > 4 else None,
        low=r[5] if len(r) > 5 else None,
        close=r[6] if len(r) > 6 else None,
        change=r[7] if len(r) > 7 else None,
        volume=r[1] if len(r) > 1 else None,
        turnover=r[2] if len(r) > 2 else None,
        transactions=r[8] if len(r) > 8 else None,
    )
    return q if q.is_valid() else None


class IHistoryDataSource(ABC):
    market: Market

    @abstractmethod
    def fetch_history(self, symbol: str, months: int = 5) -> list[DailyQuote]:
        ...


class TwseHistoryDataSource(IHistoryDataSource):
    market = Market.TWSE
    BASE = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"

    def __init__(self, timeout: float = 20.0, polite_delay: float = 0.3):
        self._timeout = timeout
        self._delay = polite_delay

    def fetch_history(self, symbol: str, months: int = 5) -> list[DailyQuote]:
        quotes: list[DailyQuote] = []
        for first in _recent_month_firsts(months):
            url = f"{self.BASE}?response=json&date={first:%Y%m%d}&stockNo={symbol}"
            try:
                data = get_json(url, timeout=self._timeout)
            except Exception:
                continue
            if str(data.get("stat", "")).upper() != "OK":
                continue
            for r in data.get("data", []):
                q = _row_to_quote(symbol, self.market, r[0] if r else None, r)
                if q:
                    quotes.append(q)
            if self._delay:
                time.sleep(self._delay)
        quotes.sort(key=lambda q: q.trade_date)
        return quotes


class TpexHistoryDataSource(IHistoryDataSource):
    market = Market.TPEX
    # 新版 TPEx 端點 (依 twstock 確認)；回傳 tables[0].data
    BASE = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"

    def __init__(self, timeout: float = 20.0, polite_delay: float = 0.3):
        self._timeout = timeout
        self._delay = polite_delay

    def fetch_history(self, symbol: str, months: int = 5) -> list[DailyQuote]:
        quotes: list[DailyQuote] = []
        for first in _recent_month_firsts(months):
            url = f"{self.BASE}?date={first.year}/{first.month:02d}/01&code={symbol}&response=json"
            try:
                data = get_json(url, timeout=self._timeout)
            except Exception:
                continue
            tables = data.get("tables") or []
            rows = tables[0].get("data", []) if tables else data.get("aaData", [])
            for r in rows:
                raw_date = str(r[0]).replace("*", "").strip() if r else None
                q = _row_to_quote(symbol, self.market, raw_date, r)
                if q:
                    quotes.append(q)
            if self._delay:
                time.sleep(self._delay)
        quotes.sort(key=lambda q: q.trade_date)
        return quotes


def get_history(symbol: str, market: Optional[Market] = None, months: int = 5) -> list[DailyQuote]:
    """取得個股歷史日K。market 未指定時，先試上市再試上櫃。"""
    if market == Market.TWSE:
        return TwseHistoryDataSource().fetch_history(symbol, months)
    if market == Market.TPEX:
        return TpexHistoryDataSource().fetch_history(symbol, months)
    q = TwseHistoryDataSource().fetch_history(symbol, months)
    return q if q else TpexHistoryDataSource().fetch_history(symbol, months)
