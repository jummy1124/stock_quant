"""盤中選股的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.datasource import mis as mis_mod
from stock_quant.datasource.mis import fetch_realtime
from stock_quant.domain import DailyQuote, Market
from stock_quant import intraday as intraday_mod
from stock_quant.intraday import IntradayScreener
from stock_quant.scheduler import MarketClock, run_market_loop


def test_fetch_realtime_parses(monkeypatch):
    payload = {"rtcode": "0000", "msgArray": [
        {"c": "2330", "n": "台積電", "ex": "tse", "d": "20260605",
         "o": "1000", "h": "1010", "l": "995", "z": "1005", "y": "1000", "v": "30"},
    ]}
    monkeypatch.setattr(mis_mod, "get_json", lambda *a, **k: payload)
    quotes = fetch_realtime([("2330", Market.TWSE)], processes=1)
    assert float(quotes[0].close) == 1005.0
    assert quotes[0].volume == 30000        # 30 張 -> 30000 股


def test_fetch_realtime_no_trade_uses_prev_close(monkeypatch):
    payload = {"rtcode": "0000", "msgArray": [
        {"c": "1234", "n": "x", "ex": "tse", "d": "20260605",
         "o": "-", "h": "-", "l": "-", "z": "-", "y": "50.0", "v": "0"},
    ]}
    monkeypatch.setattr(mis_mod, "get_json", lambda *a, **k: payload)
    quotes = fetch_realtime([("1234", Market.TWSE)], processes=1)
    assert len(quotes) == 1 and float(quotes[0].close) == 50.0


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
    assert ran == 2
    ran2 = run_market_loop(lambda now: calls.append(now), interval=60, max_iterations=2,
                           now_fn=lambda: datetime(2026, 6, 6, 10, 0),
                           sleep_fn=lambda s: None, log_fn=lambda m: None)
    assert ran2 == 0


def _bar(d, o, h, l, c, v):
    return DailyQuote.normalize(symbol="2330", name="X", market=Market.TWSE,
                                trade_date=d, open=o, high=h, low=l, close=c, volume=v)


# ---- 盤中 tick: 即時價當今日K做篩選 ------------------------------------
def test_screener_tick_with_live_pass(monkeypatch):
    today = date(2026, 6, 5)
    scr = IntradayScreener([("2330", Market.TWSE)], processes=1)
    scr._history["2330"] = [_bar(today - timedelta(days=1), 99, 100, 98, 100, 1000)]  # 昨收100 量1000
    live = _bar(today, 100, 105.2, 100, 105, 1500)   # +5%, 上影0.19%, 量1.5x
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [live])
    rows = scr.tick()
    assert len(rows) == 1
    sym, market, res = rows[0]
    assert sym == "2330" and res.passed


def test_screener_tick_no_live_uses_history(monkeypatch):
    today = date(2026, 6, 5)
    scr = IntradayScreener([("2330", Market.TWSE)], processes=1)
    scr._history["2330"] = [
        _bar(today - timedelta(days=2), 99, 100, 98, 100, 1000),
        _bar(today - timedelta(days=1), 100, 105.2, 100, 105, 1500),   # 最後完整日K符合
    ]
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])  # 無即時價
    rows = scr.tick()
    assert len(rows) == 1 and rows[0][2].passed       # 退用最後完整日K，仍判定


def test_screener_no_history_skipped(monkeypatch):
    scr = IntradayScreener([("9999", Market.TWSE)], processes=1)
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])
    assert scr.tick() == []                            # 無歷史 -> 不納入


def test_screener_prepare(monkeypatch):
    def fake_history(symbol, market=None, months=5):
        return [_bar(date(2026, 6, 4), 99, 100, 98, 100, 1000)]
    monkeypatch.setattr(intraday_mod, "get_history", fake_history)
    scr = IntradayScreener([("2330", Market.TWSE), ("2317", Market.TWSE)], processes=1)
    assert scr.prepare() == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
