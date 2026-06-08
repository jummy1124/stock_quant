"""analysis(指標+分類器) + 歷史日K + 多進程掃描 的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.analysis import TrendClassifier, Trend, indicators as ind
from stock_quant.datasource import history as hist_mod
from stock_quant.datasource import twse as twse_mod
from stock_quant.datasource import tpex as tpex_mod
from stock_quant.datasource import TwseHistoryDataSource
from stock_quant.domain import DailyQuote, Market
from stock_quant.scanner import TrendScanner
from stock_quant.universe import load_individual_universe


def _mk(closes):
    base = date(2026, 1, 1)
    return [DailyQuote.normalize(symbol="T", name="T", market=Market.TWSE,
                                 trade_date=base + timedelta(days=i),
                                 open=c, high=c + 1, low=c - 1, close=c)
            for i, c in enumerate(closes)]


# ---- 指標 --------------------------------------------------------------
def test_sma_known():
    s = ind.sma([10, 11, 12, 13, 14], 3)
    assert s[0] is None and s[2] == 11.0 and s[4] == 13.0


def test_slope_sign():
    assert ind.slope([1, 2, 3, 4, 5], 5) > 0 and ind.slope([5, 4, 3, 2, 1], 5) < 0


def test_adx_strong_uptrend():
    closes = list(range(100, 200))
    adx_val, pdi, mdi = ind.adx([c + 1 for c in closes], [c - 1 for c in closes], closes)
    assert adx_val is not None and adx_val > 25 and pdi > mdi


# ---- 分類器 ------------------------------------------------------------
def test_classify_bullish():
    res = TrendClassifier().classify(_mk([100 + i for i in range(80)]))
    assert res.trend == Trend.BULLISH and res.score >= 2


def test_classify_bearish():
    res = TrendClassifier().classify(_mk([200 - i for i in range(80)]))
    assert res.trend == Trend.BEARISH and res.score <= -2


def test_classify_ranging():
    res = TrendClassifier().classify(_mk([100 + (2 if i % 2 == 0 else -2) for i in range(80)]))
    assert res.trend == Trend.RANGING


def test_classify_insufficient():
    assert TrendClassifier().classify(_mk([100, 101, 102])).trend == Trend.UNKNOWN


# ---- 歷史日K來源 -------------------------------------------------------
def test_twse_history_parses(monkeypatch):
    payload = {"stat": "OK", "data": [
        ["115/06/03", "30,000,000", "30,000,000,000", "1000.00", "1010.00", "995.00", "1005.00", "+5.00", "40000"],
        ["115/06/04", "31,000,000", "31,000,000,000", "1005.00", "1015.00", "1000.00", "1012.00", "+7.00", "42000"],
    ]}
    monkeypatch.setattr(hist_mod, "get_json", lambda *a, **k: payload)
    quotes = TwseHistoryDataSource(polite_delay=0).fetch_history("2330", months=1)
    assert len(quotes) >= 2 and quotes[0].trade_date == date(2026, 6, 3)
    assert quotes[0].market == Market.TWSE


# ---- universe (由 EOD 組個股清單) --------------------------------------
def test_load_universe(monkeypatch):
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


# ---- 多進程掃描: 逐檔抓歷史 -> 判斷趨勢 (processes=1 套用 monkeypatch) ---
def test_scanner_classifies_each(monkeypatch):
    def fake_history(symbol, market=None, months=5):
        base = date(2026, 1, 1)
        closes = [100 + i for i in range(80)] if symbol == "2330" else [200 - i for i in range(80)]
        return [DailyQuote.normalize(symbol=symbol, name="X", market=Market.TWSE,
                                     trade_date=base + timedelta(days=i),
                                     open=c, high=c + 1, low=c - 1, close=c)
                for i, c in enumerate(closes)]
    # scanner 透過 datasource.get_history 取資料，這裡換掉它
    import stock_quant.scanner as sc
    monkeypatch.setattr(sc, "get_history", fake_history)
    results = TrendScanner(processes=1).scan([("2330", Market.TWSE), ("2317", Market.TWSE)])
    by = {r.symbol: r for r in results}
    assert by["2330"].trend == Trend.BULLISH
    assert by["2317"].trend == Trend.BEARISH


def test_scanner_isolates_errors(monkeypatch):
    def boom(symbol, market=None, months=5):
        raise ValueError("network down")
    import stock_quant.scanner as sc
    monkeypatch.setattr(sc, "get_history", boom)
    results = TrendScanner(processes=1).scan([("9999", Market.TWSE)])
    assert not results[0].ok and "network down" in results[0].error


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
