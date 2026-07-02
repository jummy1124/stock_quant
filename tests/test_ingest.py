"""SnapshotIngestor 觸發時機測試 (合成資料，不需網路)。

重點回歸：盤後(eod)快照只在「該交易日確實收盤後」才送，不會在盤前/收盤前
就替今天造出一筆盤後快照 (否則前端會提早出現盤後下載選項)。
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant import ingest as ingest_mod
from stock_quant.domain import Market
from stock_quant.ingest import IngestConfig, SnapshotIngestor


class _Bar:
    def __init__(self, d: date):
        self.trade_date = d


class _Ranker:
    """最小假 ranker：只提供 ingest 需要的介面。"""

    def __init__(self, source: str, last_date):
        self.last_source = source
        self.pairs = [("2330", Market.TWSE)]
        self._last = last_date
        self.last_quoted = 1
        self.last_warning = None

    def history(self, _sym):
        return [_Bar(self._last)] if self._last is not None else []


def _ingestor(monkeypatch, calls):
    cfg = IngestConfig(base_url="http://x", token="t")
    ing = SnapshotIngestor(cfg, fire_time=time(13, 0), close_time=time(13, 30),
                           log=lambda _m: None)
    monkeypatch.setattr(ingest_mod, "post_snapshot",
                        lambda _cfg, payload: (calls.append(payload) or (True, "ok")))
    return ing


def test_eod_not_sent_for_today_before_close(monkeypatch):
    """最後交易日=今天、但現在還沒收盤 -> 不送 (回歸『盤後選項提早出現』)。"""
    calls: list = []
    ing = _ingestor(monkeypatch, calls)
    rk = _Ranker("eod", date(2026, 6, 23))
    ing.process(datetime(2026, 6, 23, 8, 30), None, [], rk)   # 今天盤前
    assert calls == []


def test_eod_sent_for_today_after_close(monkeypatch):
    """今天且已過收盤 (13:30) -> 正常送當日盤後快照。"""
    calls: list = []
    ing = _ingestor(monkeypatch, calls)
    rk = _Ranker("eod", date(2026, 6, 23))
    ing.process(datetime(2026, 6, 23, 15, 0), None, [], rk)
    assert len(calls) == 1
    assert calls[0]["trade_date"] == "2026-06-23" and calls[0]["session"] == "eod"


def test_eod_sent_for_previous_trading_day(monkeypatch):
    """最後交易日=昨天 -> 盤前也可送 (昨天早已收盤)。"""
    calls: list = []
    ing = _ingestor(monkeypatch, calls)
    rk = _Ranker("eod", date(2026, 6, 22))
    ing.process(datetime(2026, 6, 23, 8, 0), None, [], rk)
    assert len(calls) == 1 and calls[0]["trade_date"] == "2026-06-22"


def test_eod_not_sent_when_history_empty(monkeypatch):
    """歷史未就緒 (無最後交易日) -> 不可退回用今天日期造假快照。"""
    calls: list = []
    ing = _ingestor(monkeypatch, calls)
    rk = _Ranker("eod", None)
    ing.process(datetime(2026, 6, 23, 15, 0), None, [], rk)
    assert calls == []


def test_intraday_1300_still_sends(monkeypatch):
    """盤中到 13:00 後仍正常送 intraday_1300 (未被本次修改影響)。"""
    calls: list = []
    ing = _ingestor(monkeypatch, calls)
    rk = _Ranker("live", date(2026, 6, 23))
    ing.process(datetime(2026, 6, 23, 13, 0), None, [], rk)
    assert len(calls) == 1 and calls[0]["session"] == "intraday_1300"
    assert calls[0]["trade_date"] == "2026-06-23"
