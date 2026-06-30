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
from stock_quant.intraday import IntradayRanker
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
    # 今日(6/5)完成K已在歷史中 -> EOD 不需另抓 (回空，避免測試打網路)
    monkeypatch.setattr(intraday_mod, "fetch_market_eod", lambda *a, **k: {})

    base = date(2026, 6, 5)
    rk = IntradayRanker([("2330", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["2330"] = [_bar("2330", base - timedelta(days=1), 100, 1000),
                           _bar("2330", base, 108, 1500)]
    rows = rk.tick(datetime(2026, 6, 5, 15, 0))
    assert rk.last_source == "eod"
    assert len(rows) == 1 and rows[0].change_pct == 8.0
    assert rows[0].stable is True


# ---- 盤後: 補抓今日完成日K併入歷史，使 history[-1] = 今日 (修正顯示昨日的問題) ----
def test_ranker_eod_merges_today_close(monkeypatch):
    """收盤後 history 只到昨日(6/22)時，應補抓今日(6/23)完成K併入並用它算漲幅。"""
    monkeypatch.setattr(intraday_mod, "fetch_realtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該抓即時價")))
    yday, today = date(2026, 6, 22), date(2026, 6, 23)
    rk = IntradayRanker([("8028", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["8028"] = [_bar("8028", yday, 311.5, 9000, name="昇陽半導體")]  # 只有昨日

    captured = {}
    def fake_eod(markets, day, **k):
        captured["markets"], captured["day"] = markets, day
        return {"8028": _bar("8028", today, 322.5, 11432, name="昇陽半導體")}
    monkeypatch.setattr(intraday_mod, "fetch_market_eod", fake_eod)

    rows = rk.tick(datetime(2026, 6, 23, 15, 0))
    assert rk.last_source == "eod"
    assert captured["day"] == today and captured["markets"] == ("twse",)
    assert len(rows) == 1
    r = rows[0]
    assert r.close == 322.5 and r.prev_close == 311.5
    assert r.change == 11.0 and r.trade_date == today      # 顯示的是今日(6/23)資料
    assert rk._history["8028"][-1].trade_date == today     # 已併入歷史
    assert len(rk._history["8028"]) == 2


def test_ranker_eod_no_today_data_fallback(monkeypatch):
    """今日尚未公布 (fetch_market_eod 回空) -> fallback 用最後一個已完成交易日，不亂併。"""
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda *a, **k: [])
    monkeypatch.setattr(intraday_mod, "fetch_market_eod", lambda *a, **k: {})
    d1, d2 = date(2026, 6, 19), date(2026, 6, 20)
    rk = IntradayRanker([("8028", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["8028"] = [_bar("8028", d1, 100, 1000), _bar("8028", d2, 108, 1500)]
    rows = rk.tick(datetime(2026, 6, 23, 15, 0))
    assert len(rows) == 1 and rows[0].change_pct == 8.0    # 仍用 d2 vs d1
    assert rows[0].trade_date == d2
    assert len(rk._history["8028"]) == 2                   # 未被亂加
    assert rk._eod_day is None                             # 沒抓到不標記，下次會再試


def test_ranker_eod_no_duplicate_merge(monkeypatch):
    """今日已在歷史中時，再抓到同一天也不重複併入。"""
    monkeypatch.setattr(intraday_mod, "fetch_realtime", lambda *a, **k: [])
    yday, today = date(2026, 6, 22), date(2026, 6, 23)
    rk = IntradayRanker([("8028", Market.TWSE)], processes=1, apply_filter=False)
    rk._history["8028"] = [_bar("8028", yday, 311.5, 9000),
                           _bar("8028", today, 322.5, 11432)]   # 今日已在
    monkeypatch.setattr(intraday_mod, "fetch_market_eod",
                        lambda *a, **k: {"8028": _bar("8028", today, 999, 1)})
    rows = rk.tick(datetime(2026, 6, 23, 15, 0))
    assert len(rk._history["8028"]) == 2                   # 沒被加成 3 根
    assert rows[0].close == 322.5                          # 用原本的今日K，不是 999


def test_ranker_eod_partial_market_publish_retries(monkeypatch):
    """一個市場(上櫃)已公布、另一個(上市)尚未公布/被限流時，不可整天停在前一交易日。

    應：先併入已公布的市場，但 **不標記本日完成**；下個 cycle 只重試仍缺今日的市場，
    待補上後才標記完成。(回歸先前『部分市場公布即整天不再補抓』的 bug。)
    """
    monkeypatch.setattr(intraday_mod, "fetch_realtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該抓即時價")))
    yday, today = date(2026, 6, 22), date(2026, 6, 23)

    def _tpex_bar(d, c, v=1000):
        return DailyQuote.normalize(symbol="6488", name="環球晶", market=Market.TPEX,
                                    trade_date=d, open=c, high=c, low=c, close=c, volume=v)

    rk = IntradayRanker([("2330", Market.TWSE), ("6488", Market.TPEX)],
                        processes=1, apply_filter=False)
    rk._history["2330"] = [_bar("2330", yday, 100, 1000)]            # 上市只有昨日
    rk._history["6488"] = [_tpex_bar(yday, 200, 1000)]              # 上櫃只有昨日

    calls = []
    def fake_eod(markets, day, **k):
        calls.append(tuple(markets))
        if len(calls) == 1:                                         # 第一次只有上櫃公布
            return {"6488": _tpex_bar(today, 220, 1500)}
        return {"2330": _bar("2330", today, 108, 1500)}            # 第二次上市才公布
    monkeypatch.setattr(intraday_mod, "fetch_market_eod", fake_eod)

    # 第一次 tick：上櫃併入今日，上市仍停昨日，且因仍有市場未補 -> 不可標記本日完成
    rk.tick(datetime(2026, 6, 23, 15, 0))
    assert rk._history["6488"][-1].trade_date == today
    assert rk._history["2330"][-1].trade_date == yday
    assert rk._eod_day is None
    assert calls[0] == ("tpex", "twse")                            # 兩市場都缺 -> 一起抓

    # 第二次 tick：只重試仍缺的上市，補上今日後才標記完成
    rk.tick(datetime(2026, 6, 23, 15, 1))
    assert rk._history["2330"][-1].trade_date == today
    assert rk._eod_day == today
    assert calls[1] == ("twse",)                                   # 只重抓仍缺的市場


def test_ranker_eod_partial_symbols_within_market_retries(monkeypatch):
    """同一市場只先公布『部分個股』時，落後的個股不可被永久卡在前一交易日。

    回歸先前 bug：『該市場有任一檔到今日就把整個市場標記完成』→ 後續陸續公布的個股
    再也不會被補抓。改用覆蓋率門檻後，部分公布時市場仍視為未完成，下個 cycle 會把
    落後的個股一併補上，且未併齊期間會寫進 last_warning。
    """
    monkeypatch.setattr(intraday_mod, "fetch_realtime",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不該抓即時價")))
    yday, today = date(2026, 6, 22), date(2026, 6, 23)
    rk = IntradayRanker([("1101", Market.TWSE), ("1102", Market.TWSE),
                         ("1103", Market.TWSE)], processes=1, apply_filter=False)
    for s in ("1101", "1102", "1103"):
        rk._history[s] = [_bar(s, yday, 100, 1000)]                # 三檔都只有昨日

    calls = []
    def fake_eod(markets, day, **k):
        calls.append(tuple(markets))
        if len(calls) == 1:                                        # 第一次只公布 1101
            return {"1101": _bar("1101", today, 110, 1500)}
        return {s: _bar(s, today, 110, 1500) for s in ("1101", "1102", "1103")}
    monkeypatch.setattr(intraday_mod, "fetch_market_eod", fake_eod)

    # 第一次 tick：只有 1101 到今日 (覆蓋 1/3 < 門檻) -> 市場未完成、1102/1103 仍昨日
    rk.tick(datetime(2026, 6, 23, 15, 0))
    assert rk._history["1101"][-1].trade_date == today
    assert rk._history["1102"][-1].trade_date == yday
    assert rk._history["1103"][-1].trade_date == yday
    assert rk._eod_day is None                                     # 未併齊不可標記完成
    assert rk.last_warning and "TWSE" in rk.last_warning           # 明確提示仍沿用昨日

    # 第二次 tick：其餘個股補上 -> 落後的 1102/1103 被補齊、才標記完成
    rk.tick(datetime(2026, 6, 23, 15, 1))
    assert rk._history["1102"][-1].trade_date == today
    assert rk._history["1103"][-1].trade_date == today
    assert rk._eod_day == today
    assert len(calls) == 2                                         # 第一次未完成 -> 有重試


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
