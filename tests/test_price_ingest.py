"""全市場收盤價上傳 (price_ingest)。

重點在「什麼該送、什麼不該送」——上傳本身是一次 HTTP POST，用假的 poster 攔下來看
payload 就夠了；真正會出錯的是資料的選取:盤中的即時價被誤當收盤價入庫、只送最後
一天導致昨天失敗就永久缺一天、以及沒有收盤價的個股塞進 NOT NULL 欄位。
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from stock_quant.domain import DailyQuote, Market
from stock_quant.ingest import IngestConfig
from stock_quant.price_ingest import (
    DailyPriceIngestor,
    collect_daily_bars,
    quote_to_price_item,
)


def q(symbol, d, close, market=Market.TWSE, **kw):
    return DailyQuote(
        symbol=symbol, name=f"股{symbol}", market=market, trade_date=d,
        close=None if close is None else Decimal(str(close)), **kw
    )


class _FakeRanker:
    """只提供 price_ingest 會讀的介面: pairs / history() / last_source。"""

    def __init__(self, history, source="eod"):
        self._h = history
        self.pairs = [(s, Market.TWSE) for s in history]
        self.last_source = source

    def history(self, symbol):
        return self._h.get(symbol, [])


@pytest.fixture(name="posted")
def posted_fixture(monkeypatch):
    """攔下 post_daily_prices，記錄每次呼叫的 (交易日, 檔數)。"""
    calls = []

    def fake_post(cfg, trade_date, quotes, source="eod"):
        calls.append((trade_date, [x.symbol for x in quotes], source))
        return True, f"{trade_date} 共 {len(quotes)} 檔"

    monkeypatch.setattr("stock_quant.price_ingest.post_daily_prices", fake_post)
    return calls


def _cfg():
    return IngestConfig(base_url="http://backend:8100", token="t0ken")


# --------------------------------------------------------------------------


def test_quote_to_price_item_serialises_decimals_as_floats():
    item = quote_to_price_item(
        q("2330", date(2026, 6, 1), 100.5, open=Decimal("99"), volume=1234)
    )
    assert item["symbol"] == "2330"
    assert item["market_code"] == "TWSE"
    assert item["close"] == 100.5 and isinstance(item["close"], float)
    assert item["open"] == 99.0
    assert item["volume"] == 1234


def test_collect_daily_bars_groups_by_day_and_keeps_only_the_newest():
    hist = {
        "2330": [q("2330", date(2026, 6, d), 100 + d) for d in (1, 2, 3)],
        "2317": [q("2317", date(2026, 6, d), 50 + d) for d in (1, 2, 3)],
    }
    by_day = collect_daily_bars(hist, days=2)
    assert sorted(by_day) == [date(2026, 6, 2), date(2026, 6, 3)]
    assert sorted(x.symbol for x in by_day[date(2026, 6, 3)]) == ["2317", "2330"]


def test_collect_daily_bars_drops_quotes_without_a_close():
    """A suspended stock has no close; the column is NOT NULL server-side."""
    hist = {"9999": [q("9999", date(2026, 6, 1), None)],
            "2330": [q("2330", date(2026, 6, 1), 100)]}
    by_day = collect_daily_bars(hist, days=1)
    assert [x.symbol for x in by_day[date(2026, 6, 1)]] == ["2330"]


def test_collect_daily_bars_tolerates_symbols_with_shorter_history():
    """A newly listed stock has fewer bars — it must not shorten everyone else's."""
    hist = {
        "2330": [q("2330", date(2026, 6, d), 100) for d in (1, 2, 3)],
        "6666": [q("6666", date(2026, 6, 3), 20)],   # listed today
    }
    by_day = collect_daily_bars(hist, days=3)
    assert len(by_day[date(2026, 6, 1)]) == 1
    assert len(by_day[date(2026, 6, 3)]) == 2


# --------------------------------------------------------------------------


def test_ingestor_does_not_upload_intraday_prices(posted):
    """盤中的「今日K」是 MIS 即時價，還會再變 —— 存成收盤價會讓回測拿到錯的數字。"""
    ranker = _FakeRanker(
        {"2330": [q("2330", date(2026, 6, 1), 100)]}, source="live"
    )
    DailyPriceIngestor(_cfg()).process(datetime(2026, 6, 1, 11, 0), ranker)
    assert posted == []


def test_ingestor_uploads_recent_days_after_close(posted):
    ranker = _FakeRanker(
        {"2330": [q("2330", date(2026, 6, d), 100 + d) for d in (1, 2, 3)]}
    )
    DailyPriceIngestor(_cfg(), days=2).process(datetime(2026, 6, 3, 15, 0), ranker)
    assert [d for d, _syms, _src in posted] == [date(2026, 6, 2), date(2026, 6, 3)]


def test_ingestor_sends_each_trading_day_only_once(posted):
    """每分鐘 tick 一次，但同一天只該送一次。"""
    ranker = _FakeRanker({"2330": [q("2330", date(2026, 6, 3), 100)]})
    ing = DailyPriceIngestor(_cfg(), days=1)
    for minute in range(3):
        ing.process(datetime(2026, 6, 3, 15, minute), ranker)
    assert len(posted) == 1


def test_ingestor_retries_a_day_whose_upload_failed(monkeypatch):
    """失敗的那天不標記為已送，下個 tick 會再試一次。"""
    attempts = []

    def flaky(cfg, trade_date, quotes, source="eod"):
        attempts.append(trade_date)
        ok = len(attempts) > 1        # 第一次失敗，第二次成功
        return ok, "ok" if ok else "boom"

    monkeypatch.setattr("stock_quant.price_ingest.post_daily_prices", flaky)
    ranker = _FakeRanker({"2330": [q("2330", date(2026, 6, 3), 100)]})
    ing = DailyPriceIngestor(_cfg(), days=1)
    ing.process(datetime(2026, 6, 3, 15, 0), ranker)
    ing.process(datetime(2026, 6, 3, 15, 1), ranker)
    ing.process(datetime(2026, 6, 3, 15, 2), ranker)   # 已成功 -> 不再送
    assert attempts == [date(2026, 6, 3), date(2026, 6, 3)]


def test_ingestor_is_a_no_op_without_credentials(posted):
    ranker = _FakeRanker({"2330": [q("2330", date(2026, 6, 3), 100)]})
    DailyPriceIngestor(IngestConfig()).process(datetime(2026, 6, 3, 15, 0), ranker)
    assert posted == []
