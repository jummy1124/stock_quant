"""盤中即時趨勢監控的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.analysis import Trend
from stock_quant.datasource import mis as mis_mod
from stock_quant.datasource.mis import fetch_realtime
from stock_quant.domain import DailyQuote, Market
from stock_quant import intraday as intraday_mod
from stock_quant.intraday import IntradayTrendMonitor
from stock_quant.scheduler import MarketClock, run_market_loop


def test_fetch_realtime_parses(monkeypatch):
    payload = {"rtcode": "0000", "msgArray": [
        {"c": "2330", "n": "台積電", "ex": "tse", "d": "20260605",
         "o": "1000", "h": "1010", "l": "995", "z": "1005", "y": "1000", "v": "30000"},
        {"c": "6488", "n": "環球晶", "ex": "otc", "d": "20260605",
         "o": "500", "h": "510", "l": "498", "z": "505", "y": "500", "v": "1000"},
    ]}
    monkeypatch.setattr(mis_mod, "get_json", lambda *a, **k: payload)
    quotes = fetch_realtime([("2330", Market.TWSE), ("6488", Market.TPEX)], processes=1)
    by = {q.symbol: q for q in quotes}
    assert by["2330"].market == Market.TWSE and float(by["2330"].close) == 1005.0
    assert by["6488"].market == Market.TPEX


def test_fetch_realtime_no_trade_uses_prev_close(monkeypatch):
    # 尚未成交: z='-'，應改用昨收 y 當現價，不可被丟掉
    payload = {"rtcode": "0000", "msgArray": [
        {"c": "1234", "n": "某冷門股", "ex": "tse", "d": "20260605",
         "o": "-", "h": "-", "l": "-", "z": "-", "y": "50.0", "v": "0"},
    ]}
    monkeypatch.setattr(mis_mod, "get_json", lambda *a, **k: payload)
    quotes = fetch_realtime([("1234", Market.TWSE)], processes=1)
    assert len(quotes) == 1 and float(quotes[0].close) == 50.0   # 用昨收


def test_market_clock():
    clk = MarketClock()
    assert clk.is_trading(datetime(2026, 6, 5, 9, 30))
    assert not clk.is_trading(datetime(2026, 6, 5, 14, 0))
    assert not clk.is_trading(datetime(2026, 6, 6, 10, 0))


def test_run_market_loop_runs_then_skips():
    calls = []
    ran = run_market_loop(lambda now: calls.append(now), interval=60, max_iterations=2,
                          now_fn=lambda: datetime(2026, 6, 5, 10, 0),
                          sleep_fn=lambda s: None, log_fn=lambda m: None)
    assert ran == 2 and len(calls) == 2
    ran2 = run_market_loop(lambda now: calls.append(now), interval=60, max_iterations=2,
                           now_fn=lambda: datetime(2026, 6, 6, 10, 0),
                           sleep_fn=lambda s: None, log_fn=lambda m: None)
    assert ran2 == 0


def _history(symbol, closes, end):
    out = []
    for i, c in enumerate(closes):
        d = end - timedelta(days=len(closes) - i)
        out.append(DailyQuote.normalize(symbol=symbol, name="X", market=Market.TWSE,
                                        trade_date=d, open=c, high=c + 1, low=c - 1, close=c))
    return out


def test_monitor_tick_recomputes_with_live(monkeypatch):
    today = date(2026, 6, 5)
    mon = IntradayTrendMonitor([("2330", Market.TWSE)], processes=1)
    mon._history["2330"] = _history("2330", [100 + i for i in range(79)], today)
    live = DailyQuote.normalize(symbol="2330", name="台積電", market=Market.TWSE,
                                trade_date=today, open=179, high=181, low=178, close=180)
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [live])
    res = mon.tick(datetime(2026, 6, 5, 10, 0))
    assert res[0].trend == Trend.BULLISH
    assert res[0].details["bars"] == 80          # 79 歷史 + 1 今日即時K


def test_monitor_tick_no_live_falls_back_to_history(monkeypatch):
    # 拿不到即時價時，改用歷史判趨勢，不應標資料不足
    today = date(2026, 6, 5)
    mon = IntradayTrendMonitor([("2330", Market.TWSE)], processes=1)
    mon._history["2330"] = _history("2330", [100 + i for i in range(80)], today)  # 上升
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])    # 無即時價
    res = mon.tick()
    assert res[0].ok and res[0].trend == Trend.BULLISH       # 由歷史判出多頭
    assert res[0].details["bars"] == 80


def test_monitor_tick_no_history_is_unknown(monkeypatch):
    mon = IntradayTrendMonitor([("9999", Market.TWSE)], processes=1)   # 沒塞歷史
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])
    res = mon.tick()
    assert res[0].trend == Trend.UNKNOWN and "歷史" in res[0].error


def test_monitor_prepare(monkeypatch):
    def fake_history(symbol, market=None, months=5):
        return _history(symbol, [100 + i for i in range(60)], date(2026, 6, 5))
    monkeypatch.setattr(intraday_mod, "get_history", fake_history)
    mon = IntradayTrendMonitor([("2330", Market.TWSE), ("2317", Market.TWSE)], processes=1)
    assert mon.prepare() == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
