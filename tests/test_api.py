"""API 層測試 (不需網路)。

涵蓋:
  - screen_service: attach_ranker + publish 的快照建立 (用 EOD 路徑 + 自製歷史)。
  - 純 dict 序列化 (stock/breakout/meta)。
  - FastAPI 端點 (若已安裝 fastapi 才跑，否則自動略過)。

均線/起漲條件用一段精心設計的歷史，讓單一檔在 EOD 模式下確定通過 6 條件。
模擬 run_intraday --serve: ranker.tick() 算完 → screen_breakout() → service.publish()。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from stock_quant.breakout_screen import screen_breakout
from stock_quant.breakout_screen import BreakoutConfig
from stock_quant.domain import DailyQuote, Market
from stock_quant.intraday import IntradayRanker, RankRow
from stock_quant.screen_service import (ApiConfig, ScreenService, Snapshot,
                                         breakout_to_dict, meta_to_dict, stock_to_dict)

_SAT = datetime(2026, 6, 13, 15, 0, 0)   # 週六 → 非交易時間 → EOD 路徑，不抓即時


def _quote(sym, d, close, *, o=None, h=None, low=None, vol=1_000_000):
    return DailyQuote.normalize(symbol=sym, name="測試股", market=Market.TWSE,
                                trade_date=d, open=o if o is not None else close,
                                high=h if h is not None else close,
                                low=low if low is not None else close,
                                close=close, volume=vol)


def _engineered_history(sym="2330"):
    """30 根日K，設計成 EOD 模式下單一檔通過起漲 6 條件 + 落在漲幅池。"""
    base = date(2026, 5, 1)
    closes = [round(90 + 0.4 * i, 2) for i in range(28)]   # 緩升 → 20MA 上彎
    closes.append(99.0)                                     # 昨日(index28): 跌破自己的5MA
    closes.append(103.95)                                   # 今日(index29): +5% 跳上、突破
    quotes = []
    for i, c in enumerate(closes):
        d = base + timedelta(days=i)
        if i == 28:        # 昨日: 設定昨高與昨量 (條件2/3 基準)
            quotes.append(_quote(sym, d, c, h=100.0, vol=1_000_000))
        elif i == 29:      # 今日: 紅K (open<close)、量增
            quotes.append(_quote(sym, d, c, o=99.5, h=104.0, low=99.0, vol=2_000_000))
        else:
            quotes.append(_quote(sym, d, c, vol=1_000_000))
    return {sym: quotes}


def _ranker(min_change=3.0):
    hist = _engineered_history()
    pairs = [(s, q[-1].market) for s, q in hist.items()]
    r = IntradayRanker(pairs, apply_filter=True, min_change_pct=min_change,
                       exclude_limit_up=True)
    r.preload_history(hist)
    return r


def _tick_and_publish(svc, min_change=3.0):
    """模擬 run_intraday --serve 一個 cycle: tick → screen_breakout → publish。"""
    rows = svc.ranker.tick(_SAT)
    scored = screen_breakout(svc.ranker, rows, _SAT, BreakoutConfig())
    return svc.publish(_SAT, rows, scored), rows, scored


def _service():
    svc = ScreenService(ApiConfig(allowed_origins=("*",)))
    svc.attach_ranker(_ranker())
    return svc


# ---------------- service: attach_ranker + publish ----------------

def test_attach_ranker_marks_ready():
    svc = ScreenService()
    assert svc.ready is False and svc.snapshot() is None
    r = _ranker()
    svc.attach_ranker(r)
    assert svc.ready is True and svc.ranker is r


def test_publish_builds_snapshot_eod():
    svc = _service()
    snap, rows, scored = _tick_and_publish(svc)
    assert snap.source == "eod"                  # 週六走最後交易日
    assert snap.universe == 1
    assert snap.pool_size == len(snap.pool) == len(rows) == 1
    row = snap.pool[0]
    assert row.symbol == "2330"
    assert abs(row.change_pct - 5.0) < 0.01       # (103.95-99)/99*100 = 5
    assert len(snap.breakout) == len(scored) == 1  # 設計上通過起漲 6 條件
    assert svc.snapshot() is snap


def test_publish_scored_none_safe():
    """--no-breakout 時 scored=None，publish 仍要安全 (breakout 空、pool 照常)。"""
    svc = _service()
    rows = svc.ranker.tick(_SAT)
    snap = svc.publish(_SAT, rows, None)
    assert snap.breakout == [] and snap.pool_size == 1


def test_publish_requires_ranker():
    svc = ScreenService()
    try:
        svc.publish(_SAT, [], [])
    except RuntimeError:
        return
    raise AssertionError("未 attach_ranker 應丟 RuntimeError")


def test_pool_filter_min_change():
    """min_change 拉到 6% 時，+5% 的個股應被濾掉 (pool 空)。"""
    svc = ScreenService()
    svc.attach_ranker(_ranker(min_change=6.0))
    rows = svc.ranker.tick(_SAT)
    scored = screen_breakout(svc.ranker, rows, _SAT, BreakoutConfig())
    snap = svc.publish(_SAT, rows, scored)
    assert snap.pool_size == 0 and len(snap.breakout) == 0


# ---------------- 純 dict 序列化 ----------------

def test_stock_to_dict_shape():
    r = RankRow(symbol="2330", name="台積電", market=Market.TWSE, close=103.95,
                prev_close=99.0, change=4.95, change_pct=5.0, volume=2_000_000,
                open=99.5, high=104.0, low=99.0)
    d = stock_to_dict(r)
    assert d["symbol"] == "2330" and d["name"] == "台積電"
    assert d["market"] == "上市" and d["market_code"] == "TWSE"
    assert d["lots"] == 2000.0                   # 2_000_000 股 / 1000
    assert set(d) >= {"symbol", "name", "market", "market_code", "close",
                      "prev_close", "change", "change_pct", "volume", "lots",
                      "open", "high", "low"}


def test_breakout_and_meta_dict():
    svc = _service()
    snap, _rows, _scored = _tick_and_publish(svc)
    bd = breakout_to_dict(snap.breakout[0])
    assert bd["symbol"] == "2330"
    assert isinstance(bd["reasons"], list) and bd["reasons"]
    assert "ma20_up" in bd and bd["ma20_up"] is True
    md = meta_to_dict(snap, svc, count=len(snap.breakout))
    assert md["source"] == "eod" and md["count"] == 1 and md["pool_size"] == 1
    assert md["generated_at"] is not None and md["age_seconds"] is not None
    # 無快照時 meta 也要安全
    empty = meta_to_dict(None, ScreenService(), 0)
    assert empty["generated_at"] is None and empty["count"] == 0


# ---------------- FastAPI 端點 (有裝 fastapi 才跑) ----------------

def test_endpoints_if_fastapi_available():
    try:
        from fastapi.testclient import TestClient

        from stock_quant.api import create_app
    except Exception:
        return  # 沒裝 fastapi/httpx → 略過 (CLI/排程環境不需要)

    svc = _service()
    _tick_and_publish(svc)                       # 模擬 run_intraday --serve 已 publish 一次
    app = create_app(service=svc)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        r = client.get("/api/screen")
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["source"] == "eod"
        assert body["results"] and body["results"][0]["symbol"] == "2330"
        assert body["results"][0]["market"] == "上市"

        assert len(client.get("/api/screen?top=1").json()["results"]) <= 1

        pool = client.get("/api/pool").json()
        assert pool["results"][0]["symbol"] == "2330"


def test_503_before_snapshot_if_fastapi_available():
    try:
        from fastapi.testclient import TestClient

        from stock_quant.api import create_app
    except Exception:
        return
    svc = ScreenService()                         # 從未 publish → 無快照
    svc.attach_ranker(_ranker())
    app = create_app(service=svc)
    with TestClient(app) as client:
        r = client.get("/api/screen")
        assert r.status_code == 503
        assert r.json()["results"] == []
