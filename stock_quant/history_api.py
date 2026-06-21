"""歷史日K → API 用的純 dict 序列化 (含均線計算 + TTL 記憶體快取)。

供 stock_quant/api.py 的 GET /api/history/{symbol} 端點使用。

設計重點:
- 直接呼叫 datasource.history.get_history() 抓逐月歷史日K (TWSE STOCK_DAY / TPEx)。
- 在這層算好 MA5/20/60 (簡單移動平均)，前端只負責畫。
- TTL 記憶體快取: 同一檔在 cache_ttl 秒內重複查詢直接回快取，避免逐月請求把
  證交所打到限流 (盤後資料一天才變一次，TTL 給長一點很安全)。

只用標準函式庫 + 既有 stock_quant 模型；沒裝 fastapi 也能 import/測試。
⚠️ 資訊參考，非投資建議。
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Optional

from .datasource.history import get_history
from .domain import DailyQuote, Market

# ============================================================
# 均線
# ============================================================

def _sma(values: list[Optional[float]], n: int) -> list[Optional[float]]:
    """對齊輸入長度的 n 日簡單移動平均；前 n-1 根或視窗內有缺值時為 None。"""
    out: list[Optional[float]] = []
    window: list[float] = []
    for v in values:
        if v is None:
            # 遇到缺值: 視窗無法保證連續 n 根有效值，保守清空
            window = []
            out.append(None)
            continue
        window.append(v)
        if len(window) > n:
            window.pop(0)
        out.append(round(sum(window) / n, 2) if len(window) == n else None)
    return out


def _f(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


# ============================================================
# 序列化
# ============================================================

def quotes_to_candles(quotes: list[DailyQuote], ma_windows=(5, 20, 60)) -> list[dict]:
    """list[DailyQuote] (時間升冪) → list[candle dict]，附 ma5/ma20/ma60。

    candle 欄位: date(YYYY-MM-DD), open, high, low, close, volume(股), lots(張),
                 change, ma5, ma20, ma60
    """
    closes = [_f(q.close) for q in quotes]
    ma_series = {n: _sma(closes, n) for n in ma_windows}
    candles: list[dict] = []
    for i, q in enumerate(quotes):
        vol = q.volume
        row = {
            "date": q.trade_date.isoformat(),
            "open": _f(q.open),
            "high": _f(q.high),
            "low": _f(q.low),
            "close": _f(q.close),
            "volume": vol,
            "lots": round(vol / 1000, 1) if vol is not None else None,
            "change": _f(q.change),
        }
        for n in ma_windows:
            row[f"ma{n}"] = ma_series[n][i]
        candles.append(row)
    return candles


# ============================================================
# TTL 記憶體快取
# ============================================================

class _HistoryCache:
    """執行緒安全的 (symbol, market, months) → candles TTL 快取。"""

    def __init__(self, ttl_seconds: float = 1800.0):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[tuple, tuple[float, list[dict]]] = {}

    def get(self, key: tuple) -> Optional[list[dict]]:
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            ts, data = hit
            if time.time() - ts > self.ttl:
                self._store.pop(key, None)
                return None
            return data

    def put(self, key: tuple, data: list[dict]) -> None:
        with self._lock:
            self._store[key] = (time.time(), data)


_CACHE = _HistoryCache()


def _resolve_market(market_code: Optional[str]) -> Optional[Market]:
    if not market_code:
        return None
    code = market_code.strip().upper()
    if code in ("TWSE", "上市", "TSE"):
        return Market.TWSE
    if code in ("TPEX", "上櫃", "OTC"):
        return Market.TPEX
    return None


def get_history_candles(
    symbol: str,
    market_code: Optional[str] = None,
    months: int = 6,
    ma_windows=(5, 20, 60),
) -> dict:
    """抓歷史日K並序列化成 API 回應 dict (含 TTL 快取)。

    回傳: {"symbol", "market", "market_code", "months", "count", "cached", "candles"[]}
    """
    symbol = (symbol or "").strip()
    market = _resolve_market(market_code)
    key = (symbol, market.value if market else "AUTO", int(months))

    cached = _CACHE.get(key)
    if cached is not None:
        return _wrap(symbol, market, months, cached, cached=True)

    quotes = get_history(symbol, market, months=months)
    candles = quotes_to_candles(quotes, ma_windows=ma_windows)
    _CACHE.put(key, candles)
    return _wrap(symbol, market, months, candles, cached=False)


def _wrap(symbol, market, months, candles, *, cached) -> dict:
    return {
        "symbol": symbol,
        "market": getattr(market, "zh", "") if market else "",
        "market_code": getattr(market, "value", "") if market else "",
        "months": int(months),
        "count": len(candles),
        "cached": cached,
        "candles": candles,
    }
