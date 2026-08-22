"""全市場收盤價上傳 (price_ingest) 的單元測試 (合成資料，不需網路)。

重點在「什麼該送、什麼不該送」——上傳本身只是一次 HTTP POST，用假的 poster 攔下來看
參數就夠了；真正會出錯的是資料的選取:盤中的即時價被誤當收盤價入庫、只送最後一天導致
昨天失敗就永久缺一天、以及沒有收盤價的個股被塞進後端的 NOT NULL 欄位。

與本專案其他測試一樣不依賴 pytest —— tests/run_tests.py 會自動發現 test_* 函式，
並在簽章裡有 monkeypatch 參數時注入一個最小版的替身。
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant import price_ingest as price_ingest_mod
from stock_quant.domain import DailyQuote, Market
from stock_quant.ingest import IngestConfig
from stock_quant.price_ingest import (
    DailyPriceIngestor,
    collect_daily_bars,
    quote_to_price_item,
)


def _q(symbol, d, close, market=Market.TWSE, **kw):
    return DailyQuote(
        symbol=symbol, name=f"股{symbol}", market=market, trade_date=d,
        close=None if close is None else Decimal(str(close)), **kw
    )


def _cfg():
    return IngestConfig(base_url="http://backend:8100", token="t0ken")


def _ingestor(cfg=None, days=5):
    """DailyPriceIngestor 預設用 print 當 log；測試裡吞掉，免得污染 runner 的輸出。"""
    return DailyPriceIngestor(cfg or _cfg(), days=days, log=lambda _m: None)


class _FakeRanker:
    """只提供 price_ingest 會讀的介面: pairs / history() / last_source。"""

    def __init__(self, history, source="eod"):
        self._h = history
        self.pairs = [(s, Market.TWSE) for s in history]
        self.last_source = source

    def history(self, symbol):
        return self._h.get(symbol, [])


def _capture_posts(monkeypatch, outcomes=None):
    """把 post_daily_prices 換成假的，回傳記錄每次呼叫的 list。

    outcomes: 依序回傳的 (ok, msg)；用完之後一律成功。用來模擬「第一次失敗、
    第二次才成功」這種需要重試的情況。
    """
    calls = []
    pending = list(outcomes or [])

    def fake_post(cfg, trade_date, quotes, source="eod"):
        calls.append((trade_date, [q.symbol for q in quotes], source))
        if pending:
            return pending.pop(0)
        return True, f"{trade_date} 共 {len(quotes)} 檔"

    monkeypatch.setattr(price_ingest_mod, "post_daily_prices", fake_post)
    return calls


# ---- 序列化 ------------------------------------------------------------

def test_quote_to_price_item_serialises_decimals_as_floats():
    item = quote_to_price_item(
        _q("2330", date(2026, 6, 1), 100.5, open=Decimal("99"), volume=1234)
    )
    assert item["symbol"] == "2330"
    assert item["market_code"] == "TWSE"
    assert item["close"] == 100.5 and isinstance(item["close"], float)
    assert item["open"] == 99.0
    assert item["volume"] == 1234


# ---- 逐日整理 ----------------------------------------------------------

def test_collect_daily_bars_groups_by_day_and_keeps_only_the_newest():
    hist = {
        "2330": [_q("2330", date(2026, 6, d), 100 + d) for d in (1, 2, 3)],
        "2317": [_q("2317", date(2026, 6, d), 50 + d) for d in (1, 2, 3)],
    }
    by_day = collect_daily_bars(hist, days=2)
    assert sorted(by_day) == [date(2026, 6, 2), date(2026, 6, 3)]
    assert sorted(q.symbol for q in by_day[date(2026, 6, 3)]) == ["2317", "2330"]


def test_collect_daily_bars_drops_quotes_without_a_close():
    """暫停交易的個股沒有收盤價；後端那一欄是 NOT NULL，送過去只會整批失敗。"""
    hist = {"9999": [_q("9999", date(2026, 6, 1), None)],
            "2330": [_q("2330", date(2026, 6, 1), 100)]}
    by_day = collect_daily_bars(hist, days=1)
    assert [q.symbol for q in by_day[date(2026, 6, 1)]] == ["2330"]


def test_collect_daily_bars_tolerates_symbols_with_shorter_history():
    """新上市的個股歷史比較短，不該讓其他檔的天數也跟著被截掉。"""
    hist = {
        "2330": [_q("2330", date(2026, 6, d), 100) for d in (1, 2, 3)],
        "6666": [_q("6666", date(2026, 6, 3), 20)],   # 今天才上市
    }
    by_day = collect_daily_bars(hist, days=3)
    assert len(by_day[date(2026, 6, 1)]) == 1
    assert len(by_day[date(2026, 6, 3)]) == 2


# ---- 上傳觸發條件 ------------------------------------------------------

def test_ingestor_does_not_upload_intraday_prices(monkeypatch):
    """盤中的「今日K」是 MIS 即時價，還會再變 —— 存成收盤價會讓回測拿到錯的數字。"""
    posted = _capture_posts(monkeypatch)
    ranker = _FakeRanker({"2330": [_q("2330", date(2026, 6, 1), 100)]}, source="live")
    _ingestor().process(datetime(2026, 6, 1, 11, 0), ranker)
    assert posted == []


def test_ingestor_uploads_recent_days_after_close(monkeypatch):
    posted = _capture_posts(monkeypatch)
    ranker = _FakeRanker(
        {"2330": [_q("2330", date(2026, 6, d), 100 + d) for d in (1, 2, 3)]}
    )
    _ingestor(days=2).process(datetime(2026, 6, 3, 15, 0), ranker)
    assert [d for d, _syms, _src in posted] == [date(2026, 6, 2), date(2026, 6, 3)]


def test_ingestor_sends_each_trading_day_only_once(monkeypatch):
    """每分鐘 tick 一次，但同一個交易日只該送一次。"""
    posted = _capture_posts(monkeypatch)
    ranker = _FakeRanker({"2330": [_q("2330", date(2026, 6, 3), 100)]})
    ing = _ingestor(days=1)
    for minute in range(3):
        ing.process(datetime(2026, 6, 3, 15, minute), ranker)
    assert len(posted) == 1


def test_ingestor_retries_a_day_whose_upload_failed(monkeypatch):
    """失敗的那天不標記為已送，下個 tick 會再試一次；成功後就不再送。"""
    posted = _capture_posts(monkeypatch, outcomes=[(False, "boom")])
    ranker = _FakeRanker({"2330": [_q("2330", date(2026, 6, 3), 100)]})
    ing = _ingestor(days=1)
    for minute in range(3):
        ing.process(datetime(2026, 6, 3, 15, minute), ranker)
    assert [d for d, _syms, _src in posted] == [date(2026, 6, 3), date(2026, 6, 3)]


def test_ingestor_is_a_no_op_without_credentials(monkeypatch):
    posted = _capture_posts(monkeypatch)
    ranker = _FakeRanker({"2330": [_q("2330", date(2026, 6, 3), 100)]})
    _ingestor(IngestConfig()).process(datetime(2026, 6, 3, 15, 0), ranker)
    assert posted == []
