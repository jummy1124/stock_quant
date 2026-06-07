"""核心領域模型。

這一層刻意「乾淨」：不 import requests / sqlite / fastapi 等任何外部框架，
只用標準函式庫。如此一來模型可以被任何上層自由使用，也最容易測試。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional


class Market(str, Enum):
    """市場別。繼承 str 方便序列化。"""

    TWSE = "TWSE"   # 上市 (臺灣證券交易所)
    TPEX = "TPEX"   # 上櫃 (證券櫃檯買賣中心)

    @property
    def zh(self) -> str:
        return {"TWSE": "上市", "TPEX": "上櫃"}[self.value]


def is_individual_stock(symbol: str) -> bool:
    """判斷代號是否為『普通個股』，用來濾掉非個股商品。

    台股代號慣例:
      - 普通個股: 4 碼純數字，且開頭非 0  (e.g. 2330, 1101, 6488)
      - ETF / ETN: 開頭為 0 (e.g. 0050, 00878, 020019)         -> 排除
      - 權證       : 6 碼 (e.g. 030123, 712345)                  -> 排除
      - 特別股     : 4 碼數字 + 英文字母 (e.g. 2887A, 1101B)      -> 排除
      - TDR / KY 等: 多為 911xxx / 5/6 碼或開頭 0                 -> 排除

    期貨不在本專案的現股 OpenAPI 端點內，天生就不會出現。
    """
    s = (symbol or "").strip()
    return len(s) == 4 and s.isdigit() and s[0] != "0"


def _to_decimal(raw) -> Optional[Decimal]:
    """把 API 回傳的字串(可能含逗號、'-'、'--'、空白)安全轉成 Decimal。

    無法解析時回傳 None，而不是讓整批抓取爆掉。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
    s = str(raw).strip().replace(",", "")
    if s in ("", "-", "--", "---", "X", "N/A", "null"):
        return None
    s = s.lstrip("+").lstrip("X")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _to_int(raw) -> Optional[int]:
    d = _to_decimal(raw)
    return int(d) if d is not None else None


@dataclass(frozen=True, slots=True)
class DailyQuote:
    """單一個股某一交易日的收盤資訊 (不可變值物件)。

    系統內部的「正規化」格式 — 不論資料來自 TWSE 還是 TPEx，最終都轉成這個統一結構。
    """

    symbol: str                     # 股票代號, e.g. "2330"
    name: str                       # 股票名稱, e.g. "台積電"
    market: Market
    trade_date: date                # 交易日
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    change: Optional[Decimal] = None       # 漲跌價差
    volume: Optional[int] = None           # 成交股數
    turnover: Optional[Decimal] = None     # 成交金額
    transactions: Optional[int] = None     # 成交筆數

    @classmethod
    def normalize(
        cls,
        *,
        symbol: str,
        name: str,
        market: Market,
        trade_date: date,
        open=None,
        high=None,
        low=None,
        close=None,
        change=None,
        volume=None,
        turnover=None,
        transactions=None,
    ) -> "DailyQuote":
        """以原始(髒)值建立一筆乾淨的 DailyQuote。"""
        return cls(
            symbol=str(symbol).strip(),
            name=str(name).strip(),
            market=market,
            trade_date=trade_date,
            open=_to_decimal(open),
            high=_to_decimal(high),
            low=_to_decimal(low),
            close=_to_decimal(close),
            change=_to_decimal(change),
            volume=_to_int(volume),
            turnover=_to_decimal(turnover),
            transactions=_to_int(transactions),
        )

    def is_valid(self) -> bool:
        """基本健全性檢查: 至少要有代號與收盤價。"""
        return bool(self.symbol) and self.close is not None
