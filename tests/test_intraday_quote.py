"""盤中即時價端點測試 (不需網路)。

涵蓋:
  - history_api._merge_today_candle: 附加 / 覆寫今日盤中K、重算MA、不污染快取。
  - history_api.get_intraday_quote: 有/無即時價兩種情形的回傳結構。
  - history_api.get_history_candles(intraday=True): 接上今日K、快取只存歷史。
  - FastAPI /api/quote、/api/history?intraday=true (有裝 fastapi 才跑)。

即時報價來源 (_fetch_live_quote / get_history) 一律以替身函式注入，不打網路。
"""
from __future__ import annotations

from datetime import date, datetime

import stock_quant.history_api as h
from stock_quant.domain import DailyQuote, Market

# 2026-06-22 為週一 10:00 → 交易時間
_TRADING = datetime(2026, 6, 22, 10, 0, 0)


def _q(d: date, close: float, change=None, vol: int = 1_000_000) -> DailyQuote:
    return DailyQuote.normalize(
        symbol="2330", name="台積電", market=Market.TWSE, trade_date=d,
        open=close, high=close, low=close, close=close, change=change, volume=vol)


def _today_quote() -> DailyQuote:
    # 今日盤中: 現價 107、漲 3 → 昨收應推得 104
    return DailyQuote.normalize(
        symbol="2330", name="台積電", market=Market.TWSE, trade_date=date(2026, 6, 22),
        open=104, high=108, low=103, close=107, change=3, volume=500_000)


# ---------------- _merge_today_candle ----------------

def test_merge_appends_today_and_recomputes_ma():
    hist = [_q(date(2026, 6, 10 + i), 100 + i) for i in range(6)]  # 6 根 → ma5 可算
    candles = h.quotes_to_candles(hist)
    today = h._quote_to_candle(_today_quote())
    merged = h._merge_today_candle(candles, today)
    assert len(merged) == 7
    assert merged[-1]["date"] == "2026-06-22" and merged[-1]["close"] == 107.0
    # ma5 = 後五根收盤平均 [102,103,104,105,107]
    assert merged[-1]["ma5"] == round((102 + 103 + 104 + 105 + 107) / 5, 2)
    # 不可改到原始 candles (快取安全)
    assert len(candles) == 6


def test_merge_overwrites_when_same_day():
    hist = [_q(date(2026, 6, 10 + i), 100 + i) for i in range(5)]
    hist.append(_q(date(2026, 6, 22), 106))   # 已有今日一根 (例如盤後完成日K)
    candles = h.quotes_to_candles(hist)
    today = h._quote_to_candle(_today_quote())  # close 107
    merged = h._merge_today_candle(candles, today)
    assert len(merged) == 6 and merged[-1]["close"] == 107.0


# ---------------- get_intraday_quote ----------------

def test_get_intraday_quote_live():
    orig = h._fetch_live_quote
    h._fetch_live_quote = lambda symbol, market: _today_quote()
    try:
        out = h.get_intraday_quote("2330", "TWSE", now=_TRADING)
    finally:
        h._fetch_live_quote = orig
    assert out["trading"] is True and out["source"] == "live"
    assert out["close"] == 107.0 and out["prev_close"] == 104.0
    assert abs(out["change_pct"] - 3 / 104 * 100) < 0.01
    assert out["candle"]["date"] == "2026-06-22" and out["candle"]["close"] == 107.0


def test_get_intraday_quote_none():
    orig = h._fetch_live_quote
    h._fetch_live_quote = lambda symbol, market: None
    try:
        out = h.get_intraday_quote("9999", "TWSE", now=_TRADING)
    finally:
        h._fetch_live_quote = orig
    assert out["candle"] is None and out["close"] is None
    assert out["trading"] is True and out["source"] == "live"


# ---------------- get_history_candles(intraday=True) ----------------

def test_history_intraday_appends_and_cache_is_clean():
    hist = [_q(date(2026, 6, 17), 101), _q(date(2026, 6, 18), 102),
            _q(date(2026, 6, 19), 104)]   # 歷史到上週五 (非今日)
    orig_get, orig_live = h.get_history, h._fetch_live_quote
    h.get_history = lambda symbol, market, months=6: hist
    h._fetch_live_quote = lambda symbol, market: _today_quote()
    h._CACHE._store.clear()
    try:
        data = h.get_history_candles("2330", "TWSE", months=6, intraday=True, now=_TRADING)
    finally:
        h.get_history, h._fetch_live_quote = orig_get, orig_live

    assert data["intraday"] is True and data["source"] == "live"
    assert data["count"] == 4
    assert data["candles"][-1]["date"] == "2026-06-22" and data["candles"][-1]["close"] == 107.0
    # 快取只存歷史 3 根，不含盤中那根
    cached = h._CACHE.get(("2330", "TWSE", 6))
    assert cached is not None and len(cached) == 3


def test_history_without_intraday_unchanged():
    hist = [_q(date(2026, 6, 18), 102), _q(date(2026, 6, 19), 104)]
    orig_get = h.get_history
    h.get_history = lambda symbol, market, months=6: hist
    h._CACHE._store.clear()
    try:
        data = h.get_history_candles("2330", "TWSE", months=6, intraday=False)
    finally:
        h.get_history = orig_get
    assert data["intraday"] is False and data["count"] == 2
    assert data["candles"][-1]["date"] == "2026-06-19"


# ---------------- FastAPI 端點 (有裝 fastapi 才跑) ----------------

def test_quote_and_history_endpoints_if_fastapi_available():
    try:
        from fastapi.testclient import TestClient

        import stock_quant.api as api
    except Exception:
        return  # 沒裝 fastapi/httpx → 略過

    quote_dict = {
        "symbol": "2330", "market": "上市", "market_code": "TWSE",
        "trading": True, "source": "live", "as_of": "2026-06-22T10:00:00",
        "prev_close": 104.0, "close": 107.0, "change": 3.0, "change_pct": 2.88,
        "candle": {"date": "2026-06-22", "open": 104.0, "high": 108.0, "low": 103.0,
                   "close": 107.0, "volume": 500_000, "lots": 500.0, "change": 3.0,
                   "ma5": None, "ma20": None, "ma60": None},
    }
    history_dict = {
        "symbol": "2330", "market": "上市", "market_code": "TWSE", "months": 6,
        "count": 1, "cached": False, "intraday": True, "source": "live",
        "as_of": "2026-06-22T10:00:00", "candles": [quote_dict["candle"]],
    }
    orig_q, orig_h = api.get_intraday_quote, api.get_history_candles
    api.get_intraday_quote = lambda symbol, market_code=None: quote_dict
    api.get_history_candles = (
        lambda symbol, market_code=None, months=6, intraday=False: history_dict)
    try:
        app = api.create_app()
        with TestClient(app) as client:
            r = client.get("/api/quote/2330")
            assert r.status_code == 200
            b = r.json()
            assert b["close"] == 107.0 and b["trading"] is True
            assert b["candle"]["date"] == "2026-06-22"

            r2 = client.get("/api/history/2330?intraday=true")
            assert r2.status_code == 200
            b2 = r2.json()
            assert b2["intraday"] is True and b2["source"] == "live"
            assert b2["candles"][-1]["close"] == 107.0
    finally:
        api.get_intraday_quote, api.get_history_candles = orig_q, orig_h
