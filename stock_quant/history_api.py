"""歷史日K → API 用的純 dict 序列化 (含均線計算 + TTL 記憶體快取)。

供 stock_quant/api.py 的 GET /api/history/{symbol} 端點使用。

設計重點:
- 直接呼叫 datasource.history.get_history() 抓逐月歷史日K (TWSE STOCK_DAY / TPEx)。
- 在這層算好 MA5/20/60 (簡單移動平均)，前端只負責畫。
- TTL 記憶體快取: 同一檔在 cache_ttl 秒內重複查詢直接回快取，避免逐月請求把
  證交所打到限流 (盤後資料一天才變一次，TTL 給長一點很安全)。
- 盤中即時: get_intraday_quote() 抓單檔 MIS 即時價；get_history_candles(intraday=True)
  會把今日盤中K接到日K尾端 (這根不進快取)。

只用標準函式庫 + 既有 stock_quant 模型；沒裝 fastapi 也能 import/測試。
⚠️ 資訊參考，非投資建議。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .datasource.history import get_history
from .datasource.mis import fetch_realtime
from .domain import DailyQuote, Market
from .scheduler import MarketClock

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


def _apply_ma(candles: list[dict], ma_windows=(5, 20, 60)) -> None:
    """就地（重新）計算 candle 串的 MA 欄位；合併今日盤中K後重算用。"""
    closes = [c.get("close") for c in candles]
    for n in ma_windows:
        series = _sma(closes, n)
        for i, c in enumerate(candles):
            c[f"ma{n}"] = series[i]


# ============================================================
# 盤中即時報價（單檔）— 給「今日K」與 /api/quote 用
# ============================================================

_CLOCK = MarketClock()


def _quote_to_candle(q: DailyQuote) -> dict:
    vol = q.volume
    return {
        "date": q.trade_date.isoformat(),
        "open": _f(q.open),
        "high": _f(q.high),
        "low": _f(q.low),
        "close": _f(q.close),
        "volume": vol,
        "lots": round(vol / 1000, 1) if vol is not None else None,
        "change": _f(q.change),
    }


def _fetch_live_quote(symbol: str, market: Optional[Market]) -> Optional[DailyQuote]:
    """抓單一檔的 MIS 即時報價；market 未知時先試上市再試上櫃。查無回 None。"""
    markets = [market] if market else [Market.TWSE, Market.TPEX]
    for m in markets:
        try:
            res = fetch_realtime([(symbol, m)], batch_size=1, processes=1)
        except Exception:  # noqa: BLE001 — 即時價失敗不應拖垮歷史端點
            res = []
        if res:
            return res[0]
    return None


def get_intraday_quote(
    symbol: str,
    market_code: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """取單一檔的「目前最新價」。

    交易時間回 MIS 盤中即時價 (source=live)，非交易時間回 MIS 最後成交價 (source=eod)。
    回傳: {symbol, market, market_code, trading, source, as_of,
           prev_close, close, change, change_pct, candle|None}
    candle 為「今日(或最後交易日)」的一根，可直接接到歷史日K尾端。
    """
    now = now or datetime.now()
    symbol = (symbol or "").strip()
    market = _resolve_market(market_code)
    trading = _CLOCK.is_trading(now)

    q = _fetch_live_quote(symbol, market)
    if q is None:
        return {
            "symbol": symbol,
            "market": getattr(market, "zh", "") if market else "",
            "market_code": getattr(market, "value", "") if market else "",
            "trading": trading,
            "source": "live" if trading else "eod",
            "as_of": now.isoformat(timespec="seconds"),
            "prev_close": None, "close": None, "change": None, "change_pct": None,
            "candle": None,
        }

    close = _f(q.close)
    change = _f(q.change)
    prev_close = round(close - change, 4) if (close is not None and change is not None) else None
    change_pct = (
        round(change / prev_close * 100.0, 2)
        if (change is not None and prev_close)
        else None
    )
    return {
        "symbol": symbol or q.symbol,
        "market": q.market.zh,
        "market_code": q.market.value,
        "trading": trading,
        "source": "live" if trading else "eod",
        "as_of": now.isoformat(timespec="seconds"),
        "prev_close": prev_close,
        "close": close,
        "change": change,
        "change_pct": change_pct,
        "candle": _quote_to_candle(q),
    }


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


def _merge_today_candle(
    candles: list[dict], today: dict, ma_windows=(5, 20, 60)
) -> list[dict]:
    """把今日盤中K接到歷史日K尾端 (不污染快取: 回傳新串)。

    - 末根日期 == 今日 → 用即時OHLC覆寫 (保留原本算好的 MA，僅換價/量)。
    - 否則 → 附加一根今日K，並重算整串 MA 使今日均線點正確。
    """
    merged = [dict(c) for c in candles]  # 淺拷貝每根，避免改到快取內容
    if merged and merged[-1].get("date") == today.get("date"):
        last = merged[-1]
        for k in ("open", "high", "low", "close", "volume", "lots", "change"):
            last[k] = today.get(k)
        _apply_ma(merged, ma_windows)
    else:
        new_row = dict(today)
        for n in ma_windows:
            new_row.setdefault(f"ma{n}", None)
        merged.append(new_row)
        _apply_ma(merged, ma_windows)
    return merged


def get_history_candles(
    symbol: str,
    market_code: Optional[str] = None,
    months: int = 6,
    ma_windows=(5, 20, 60),
    intraday: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    """抓歷史日K並序列化成 API 回應 dict (含 TTL 快取)。

    intraday=True 時，於回傳前接上「今日盤中即時K」(不寫入快取，因為盤中會變動)。
    回傳: {"symbol","market","market_code","months","count","cached","candles"[],
           "intraday","source","as_of"}
    """
    symbol = (symbol or "").strip()
    market = _resolve_market(market_code)
    key = (symbol, market.value if market else "AUTO", int(months))

    cached = _CACHE.get(key)
    if cached is not None:
        candles = cached
        is_cached = True
    else:
        quotes = get_history(symbol, market, months=months)
        candles = quotes_to_candles(quotes, ma_windows=ma_windows)
        _CACHE.put(key, candles)
        is_cached = False

    intraday_applied = False
    source = None
    as_of = None
    if intraday:
        quote = get_intraday_quote(symbol, market_code, now=now)
        source = quote.get("source")
        as_of = quote.get("as_of")
        today = quote.get("candle")
        if today is not None:
            candles = _merge_today_candle(candles, today, ma_windows)
            intraday_applied = True

    return _wrap(
        symbol, market, months, candles, cached=is_cached,
        intraday=intraday_applied, source=source, as_of=as_of,
    )


def _wrap(symbol, market, months, candles, *, cached,
          intraday=False, source=None, as_of=None) -> dict:
    return {
        "symbol": symbol,
        "market": getattr(market, "zh", "") if market else "",
        "market_code": getattr(market, "value", "") if market else "",
        "months": int(months),
        "count": len(candles),
        "cached": cached,
        "intraday": intraday,
        "source": source,
        "as_of": as_of,
        "candles": candles,
    }
