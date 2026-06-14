"""盤中漲幅排行 + 篩選的單元測試 (合成資料，不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.datasource import mis as mis_mod
from stock_quant.datasource.mis import fetch_realtime
from stock_quant.domain import DailyQuote, Market
from stock_quant import intraday as intraday_mod
from stock_quant.intraday import IntradayRanker, RankRow
from stock_quant.limits import limit_up_price, limit_up_prev_tick, tick_size
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


def test_fetch_realtime_batch_error_isolated(monkeypatch):
    ok = {"rtcode": "0000", "msgArray": [
        {"c": "2330", "n": "台積電", "ex": "tse", "d": "20260605",
         "o": "1000", "h": "1010", "l": "995", "z": "1005", "y": "1000", "v": "30"}]}

    def fake_get_json(url, **k):
        if "9999" in url:
            raise RuntimeError("MIS rtcode=0009 流量限制")
        return ok
    monkeypatch.setattr(mis_mod, "get_json", fake_get_json)
    warnings = []
    quotes = fetch_realtime([("2330", Market.TWSE), ("9999", Market.TWSE)],
                            batch_size=1, processes=1, on_error=warnings.append)
    assert [q.symbol for q in quotes] == ["2330"]
    assert warnings and "1/2" in warnings[0]


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


def test_session_fraction():
    clk = MarketClock()
    assert abs(clk.session_fraction(datetime(2026, 6, 5, 10, 30)) - 90 / 270) < 1e-9
    assert clk.session_fraction(datetime(2026, 6, 5, 14, 0)) == 1.0
    assert 0 < clk.session_fraction(datetime(2026, 6, 5, 9, 0)) <= 1


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


def test_run_market_loop_run_when_closed():
    calls = []
    ran = run_market_loop(lambda now: calls.append(now), interval=60, idle_interval=300,
                          run_when_closed=True, max_iterations=2,
                          now_fn=lambda: datetime(2026, 6, 6, 10, 0),
                          sleep_fn=lambda s: None, log_fn=lambda m: None)
    assert ran == 2 and len(calls) == 2


def _bar(sym, d, c, v=1000, name="X"):
    return DailyQuote.normalize(symbol=sym, name=name, market=Market.TWSE,
                                trade_date=d, open=c, high=c, low=c, close=c, volume=v)


# ---- 升降單位 / 漲停價 -------------------------------------------------
def test_limits():
    assert tick_size(8) == 0.01 and tick_size(80) == 0.1 and tick_size(800) == 1.0
    assert limit_up_price(100) == 110.0 and limit_up_prev_tick(100) == 109.5
    assert limit_up_price(17.5) == 19.25 and limit_up_prev_tick(17.5) == 19.2


# ---- 盤中 tick: 即時價當今日K算漲幅 (不篩選) ----------------------------
def test_ranker_tick_live_change_pct(monkeypatch):
    today = date(2026, 6, 5)
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["2330"] = [_bar("2330", today - timedelta(days=1), 100, 1000)]
    live = _bar("2330", today, 105, 1500, name="台積電")
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [live])
    rows = rk.tick(datetime(2026, 6, 5, 13, 25))
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "2330" and r.name == "台積電"
    assert r.prev_close == 100.0 and r.close == 105.0
    assert r.change == 5.0 and r.change_pct == 5.0
    assert r.lots == 1.5
    assert rk.last_source == "live"


def test_ranker_sorts_by_change_desc(monkeypatch):
    today = date(2026, 6, 5)
    pairs = [("1111", Market.TWSE), ("2222", Market.TWSE), ("3333", Market.TWSE)]
    rk = IntradayRanker(pairs, processes=1, apply_filter=False)
    for s in ("1111", "2222", "3333"):
        rk._history[s] = [_bar(s, today - timedelta(days=1), 100, 1000)]
    live = [_bar("1111", today, 101), _bar("2222", today, 110), _bar("3333", today, 95)]
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: live)
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert [r.symbol for r in rows] == ["2222", "1111", "3333"]
    assert [r.change_pct for r in rows] == [10.0, 1.0, -5.0]


def test_ranker_tick_no_live_skipped(monkeypatch):
    today = date(2026, 6, 5)
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["2330"] = [_bar("2330", today - timedelta(days=1), 100, 1000)]
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])
    assert rk.tick(datetime(2026, 6, 5, 10, 0)) == []


def test_ranker_no_history_skipped(monkeypatch):
    rk = IntradayRanker([("9999", Market.TWSE)], processes=1, apply_filter=False)
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])
    assert rk.tick(datetime(2026, 6, 5, 10, 0)) == []


# ---- 篩選: 漲幅 3% ~ 漲停前一檔 ----------------------------------------
def _setup_filter_ranker(monkeypatch, **kw):
    today = date(2026, 6, 5)
    pairs = [("A", Market.TWSE), ("B", Market.TWSE), ("C", Market.TWSE), ("D", Market.TWSE)]
    rk = IntradayRanker(pairs, processes=1, **kw)
    for s in ("A", "B", "C", "D"):
        rk._history[s] = [_bar(s, today - timedelta(days=1), 100, 1000)]
    # 昨收 100 -> 漲停 110、漲停前一檔 109.5
    live = [_bar("A", today, 102),      # +2%  (低於 3% -> 剔除)
            _bar("B", today, 103),      # +3%  (剛好入選)
            _bar("C", today, 109.5),    # +9.5% == 漲停前一檔 (入選)
            _bar("D", today, 110)]      # +10% 鎖漲停 (>漲停前一檔 -> 剔除)
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: live)
    return rk


def test_ranker_filter_3pct_to_pre_limit(monkeypatch):
    rk = _setup_filter_ranker(monkeypatch)        # 預設 apply_filter=True, min 3%, 排除漲停
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert [r.symbol for r in rows] == ["C", "B"]      # 依漲幅: 9.5%, 3%
    assert rk.last_matched == 2


def test_ranker_filter_off_keeps_all(monkeypatch):
    rk = _setup_filter_ranker(monkeypatch, apply_filter=False)
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert [r.symbol for r in rows] == ["D", "C", "B", "A"]   # 全部、依漲幅


def test_ranker_filter_include_limit_up(monkeypatch):
    rk = _setup_filter_ranker(monkeypatch, exclude_limit_up=False)
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert [r.symbol for r in rows] == ["D", "C", "B"]        # 連漲停 D 也納入


def test_ranker_filter_min_change(monkeypatch):
    rk = _setup_filter_ranker(monkeypatch, min_change_pct=9.0)
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert [r.symbol for r in rows] == ["C"]                  # 只剩 ≥9% 且未鎖漲停


# ---- 非交易時間: 用最後交易日完成日K ----------------------------------
def test_ranker_tick_eod_when_closed(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("非交易時間不該抓即時價")
    monkeypatch.setattr(intraday_mod, "fetch_realtime", _boom)

    base = date(2026, 6, 5)
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["2330"] = [_bar("2330", base - timedelta(days=1), 100, 1000),
                           _bar("2330", base, 108, 1500)]
    rows = rk.tick(datetime(2026, 6, 5, 15, 0))
    assert rk.last_source == "eod"
    assert len(rows) == 1 and rows[0].change_pct == 8.0
    assert rows[0].stable is True


def test_ranker_tick_eod_can_be_disabled(monkeypatch):
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, eod_when_closed=False)
    rk._history["2330"] = [_bar("2330", date(2026, 6, 4), 100, 1000),
                           _bar("2330", date(2026, 6, 5), 108, 1500)]
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [])
    assert rk.tick(datetime(2026, 6, 5, 15, 0)) == []
    assert rk.last_source == "live"


# ---- 名稱補完: name_map 優先 ------------------------------------------
def test_ranker_name_map_fills_name(monkeypatch):
    today = date(2026, 6, 5)
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, apply_filter=False,
                        name_map={"2330": "台積電"})
    rk._history["2330"] = [_bar("2330", today - timedelta(days=1), 100, 1000, name="")]
    live = _bar("2330", today, 105, 1500, name="")          # 即時資料無名稱
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda pairs, **k: [live])
    rows = rk.tick(datetime(2026, 6, 5, 11, 0))
    assert rows[0].name == "台積電"                          # 由 name_map 補上


def test_ranker_prepare(monkeypatch):
    def fake_history(symbol, market=None, months=5):
        return [_bar(symbol, date(2026, 6, 4), 100, 1000)]
    monkeypatch.setattr(intraday_mod, "get_history", fake_history)
    rk = IntradayRanker([("2330", Market.TWSE), ("2317", Market.TWSE)], processes=1)
    assert rk.prepare() == 2


def test_rankrow_notify_compat():
    r = RankRow("2330", "台積電", Market.TWSE, close=105.0, prev_close=100.0,
                change=5.0, change_pct=5.0, volume=1500)
    assert r.result.change_pct == 5.0 and r.result.close == 105.0
    assert r.stable is True and r.market.zh == "上市"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
