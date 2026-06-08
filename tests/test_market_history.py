"""全市場歷史日K 組裝的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.datasource import market_history as mh
from stock_quant.datasource import history as hist_mod
from stock_quant.datasource.history import TpexHistoryDataSource
from stock_quant.domain import Market


# ---- 上市 MI_INDEX (逐日整批) 解析 + 個股過濾 --------------------------
def test_fetch_twse_day_parses(monkeypatch):
    payload = {"stat": "OK", "tables": [
        {"fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
                    "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差"],
         "data": [
             ["2330", "台積電", "30,000,000", "40000", "3.0e10", "1000.00", "1010.00", "995.00", "1005.00", "+", "5.00"],
             ["0050", "元大台灣50", "1,000", "10", "1.8e5", "180", "181", "179", "180", "+", "1"],  # ETF
         ]},
    ]}
    monkeypatch.setattr(mh, "get_json", lambda *a, **k: payload)
    quotes = mh.fetch_twse_day(date(2026, 6, 4))
    assert [q.symbol for q in quotes] == ["2330"]
    assert float(quotes[0].close) == 1005.0 and quotes[0].market == Market.TWSE


def test_fetch_twse_day_holiday_returns_empty(monkeypatch):
    monkeypatch.setattr(mh, "get_json", lambda *a, **k: {"stat": "很抱歉，沒有符合條件的資料"})
    assert mh.fetch_twse_day(date(2026, 1, 1)) == []


# ---- 上櫃 tradingStock (tables[0].data) 解析 (新版端點) ----------------
def test_tpex_history_parses(monkeypatch):
    payload = {"tables": [{"data": [
        ["115/06/03", "1,000", "5,000", "500.00", "510.00", "498.00", "505.00", "5.00", "300"],
        ["115/06/04*", "1,100", "5,500", "505.00", "515.00", "500.00", "511.00", "6.00", "320"],
    ]}]}
    monkeypatch.setattr(hist_mod, "get_json", lambda *a, **k: payload)
    quotes = TpexHistoryDataSource(polite_delay=0).fetch_history("6488", months=1)
    assert len(quotes) >= 2
    assert quotes[0].trade_date == date(2026, 6, 3)        # 升冪、民國轉西元
    assert float(quotes[0].close) == 505.0
    assert quotes[0].market == Market.TPEX


# ---- 上市逐日累積成序列 ------------------------------------------------
def test_load_market_history_twse_series(monkeypatch):
    def fake_twse(d, timeout=10.0):
        return [mh.DailyQuote.normalize(symbol="2330", name="", market=Market.TWSE,
                                        trade_date=d, open=100, high=101, low=99, close=100)]
    monkeypatch.setattr(mh, "fetch_twse_day", fake_twse)
    hist = mh.load_market_history(("twse",), days=5, delay=0, today=date(2026, 6, 8))
    assert len(hist["2330"]) == 5
    assert hist["2330"] == sorted(hist["2330"], key=lambda q: q.trade_date)


# ---- 上櫃路徑: OTC清單 + 逐檔 (processes=1) -----------------------------
def test_load_market_history_tpex_path(monkeypatch):
    monkeypatch.setattr(mh, "_otc_universe", lambda timeout=20.0: ["6488"])

    def fake_get_history(symbol, market=None, months=5):
        return [mh.DailyQuote.normalize(symbol=symbol, name="", market=Market.TPEX,
                                        trade_date=date(2026, 6, 3), open=500, high=510, low=498, close=505)]
    # _otc_worker 內部 from .history import get_history -> patch 該模組屬性
    monkeypatch.setattr(hist_mod, "get_history", fake_get_history)
    hist = mh.load_market_history(("tpex",), days=5, delay=0, processes=1, today=date(2026, 6, 8))
    assert "6488" in hist and hist["6488"][0].market == Market.TPEX


# ---- 快取 ---------------------------------------------------------------
def test_load_market_history_cache(monkeypatch):
    seen = {"n": 0}

    def fake_twse(d, timeout=10.0):
        seen["n"] += 1
        return [mh.DailyQuote.normalize(symbol="2330", name="", market=Market.TWSE,
                                        trade_date=d, open=1, high=1, low=1, close=1)]
    monkeypatch.setattr(mh, "fetch_twse_day", fake_twse)
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "h.pkl")
        mh.load_market_history(("twse",), days=3, delay=0, cache_path=cache, today=date(2026, 6, 8))
        n1 = seen["n"]
        mh.load_market_history(("twse",), days=3, delay=0, cache_path=cache, today=date(2026, 6, 8))
        assert seen["n"] == n1        # 第二次走快取


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---- (由 test_trend 移入) 上市逐檔 STOCK_DAY 解析 ----------------------
def test_twse_history_parses(monkeypatch):
    from stock_quant.datasource.history import TwseHistoryDataSource
    payload = {"stat": "OK", "data": [
        ["115/06/03", "30,000,000", "3.0e10", "1000.00", "1010.00", "995.00", "1005.00", "+5.00", "40000"],
        ["115/06/04", "31,000,000", "3.1e10", "1005.00", "1015.00", "1000.00", "1012.00", "+7.00", "42000"],
    ]}
    monkeypatch.setattr(hist_mod, "get_json", lambda *a, **k: payload)
    quotes = TwseHistoryDataSource(polite_delay=0).fetch_history("2330", months=1)
    assert len(quotes) >= 2 and quotes[0].trade_date == date(2026, 6, 3)
    assert quotes[0].market == Market.TWSE
    assert quotes[0].volume == 30000000          # 股 (×1)


# ---- (由 test_trend 移入) universe 組裝 --------------------------------
def test_load_universe(monkeypatch):
    from stock_quant.datasource import twse as twse_mod
    from stock_quant.datasource import tpex as tpex_mod
    from stock_quant.universe import load_individual_universe
    monkeypatch.setattr(twse_mod, "get_json", lambda *a, **k: [
        {"Code": "2330", "Name": "台積電", "ClosingPrice": "1005"},
        {"Code": "0050", "Name": "ETF", "ClosingPrice": "180"},
    ])
    monkeypatch.setattr(tpex_mod, "get_json", lambda *a, **k: [
        {"Date": "1150605", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "Close": "505"},
    ])
    pairs = load_individual_universe(("twse", "tpex"))
    assert ("2330", Market.TWSE) in pairs and ("6488", Market.TPEX) in pairs
    assert all(sym != "0050" for sym, _ in pairs)
