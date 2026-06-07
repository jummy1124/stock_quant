"""盤中即時相關的單元測試 (不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal

from stock_quant.datasource import mis as mis_mod
from stock_quant.datasource import twse as twse_mod
from stock_quant.datasource import tpex as tpex_mod
from stock_quant.datasource import MisRealtimeDataSource
from stock_quant.domain import Market
from stock_quant.scheduler import MarketClock, run_market_loop
from stock_quant.universe import load_individual_universe


# ---- MIS 批次解析 (tse + otc 混合) --------------------------------------
def test_mis_batch_parses(monkeypatch):
    payload = {"rtcode": "0000", "msgArray": [
        {"c": "2330", "n": "台積電", "ex": "tse", "d": "20260605",
         "o": "1000", "h": "1010", "l": "995", "z": "1005", "y": "1000", "v": "30000"},
        {"c": "6488", "n": "環球晶", "ex": "otc", "d": "20260605",
         "o": "500", "h": "510", "l": "498", "z": "505", "y": "500", "v": "1000"},
    ]}
    monkeypatch.setattr(mis_mod, "get_json", lambda *a, **k: payload)
    src = MisRealtimeDataSource([("2330", Market.TWSE), ("6488", Market.TPEX)], batch_size=40)
    units = src.list_fetch_units()
    assert len(units) == 1                        # 2 檔 < batch 40 -> 一批
    quotes = src.fetch(units[0])
    by = {q.symbol: q for q in quotes}
    assert by["2330"].market == Market.TWSE and by["2330"].close == Decimal("1005")
    assert by["2330"].change == Decimal("5")      # z - y
    assert by["6488"].market == Market.TPEX       # ex=otc 自動判定


# ---- 分批: 90 檔 / batch 40 -> 3 批 ------------------------------------
def test_mis_batching():
    pairs = [(str(1000 + i), Market.TWSE) for i in range(90)]
    src = MisRealtimeDataSource(pairs, batch_size=40)
    units = src.list_fetch_units()
    assert len(units) == 3
    assert sum(len(u.params["pairs"]) for u in units) == 90


# ---- 盤中時段判斷 -------------------------------------------------------
def test_market_clock():
    clk = MarketClock()
    assert clk.is_trading(datetime(2026, 6, 5, 9, 30))      # 週五 09:30 盤中
    assert clk.is_trading(datetime(2026, 6, 5, 13, 30))     # 收盤瞬間仍算
    assert not clk.is_trading(datetime(2026, 6, 5, 8, 59))  # 開盤前
    assert not clk.is_trading(datetime(2026, 6, 5, 14, 0))  # 收盤後
    assert not clk.is_trading(datetime(2026, 6, 6, 10, 0))  # 週六


# ---- 常駐迴圈: 盤中會跑、非盤中不跑 (注入假時鐘) -----------------------
def test_run_market_loop_runs_during_trading():
    calls = []
    trading = datetime(2026, 6, 5, 10, 0)        # 盤中
    ran = run_market_loop(
        task=lambda now: calls.append(now),
        interval=60, max_iterations=3,
        now_fn=lambda: trading, sleep_fn=lambda s: None, log_fn=lambda m: None,
    )
    assert ran == 3 and len(calls) == 3


def test_run_market_loop_skips_when_closed():
    calls = []
    closed = datetime(2026, 6, 6, 10, 0)         # 週六
    ran = run_market_loop(
        task=lambda now: calls.append(now),
        interval=60, max_iterations=3,
        now_fn=lambda: closed, sleep_fn=lambda s: None, log_fn=lambda m: None,
    )
    assert ran == 0 and len(calls) == 0


# ---- universe: 由 EOD 來源組出 (代號,市場) 清單 (monkeypatch) ----------
def test_load_universe(monkeypatch):
    monkeypatch.setattr(twse_mod, "get_json", lambda *a, **k: [
        {"Code": "2330", "Name": "台積電", "ClosingPrice": "1005"},
        {"Code": "0050", "Name": "ETF", "ClosingPrice": "180"},   # 非個股 -> 濾掉
    ])
    monkeypatch.setattr(tpex_mod, "get_json", lambda *a, **k: [
        {"Date": "1150605", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "Close": "505"},
    ])
    pairs = load_individual_universe(("twse", "tpex"))
    assert ("2330", Market.TWSE) in pairs
    assert ("6488", Market.TPEX) in pairs
    assert all(sym != "0050" for sym, _ in pairs)     # ETF 不在 universe
