"""不需網路的單元測試: 驗證解析、正規化、個股過濾、與多進程爬蟲串接。

執行: python tests/run_tests.py   (零依賴)
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.crawler import MultiProcessCrawler
from stock_quant.datasource import twse as twse_mod
from stock_quant.datasource import tpex as tpex_mod
from stock_quant.datasource import TwseDataSource, TpexDataSource
from stock_quant.datasource.base import FetchUnit, IDataSource
from stock_quant.datasource.dates import parse_roc_date
from stock_quant.domain import DailyQuote, Market, is_individual_stock


# ---- 1. 髒值正規化 ------------------------------------------------------
def test_normalize_dirty_values():
    q = DailyQuote.normalize(
        symbol=" 2330 ", name="台積電", market=Market.TWSE, trade_date=date(2026, 6, 5),
        open="1,000.00", high="1,010", low="--", close="1,005.00",
        change="+5.00", volume="32,123,456", turnover="32,000,000,000", transactions="50000",
    )
    assert q.symbol == "2330"
    assert q.open == Decimal("1000.00")
    assert q.low is None
    assert q.change == Decimal("5.00")
    assert q.volume == 32123456
    assert q.is_valid()


# ---- 2. 個股判斷 (核心: 只保留個股) -------------------------------------
def test_is_individual_stock():
    # 個股
    assert is_individual_stock("2330")
    assert is_individual_stock("1101")
    assert is_individual_stock("6488")
    # 非個股 -> 應排除
    assert not is_individual_stock("0050")     # ETF (開頭 0)
    assert not is_individual_stock("00878")    # ETF
    assert not is_individual_stock("030123")   # 權證 (6 碼)
    assert not is_individual_stock("2887A")    # 特別股 (含字母)
    assert not is_individual_stock("911616")   # TDR/KY
    assert not is_individual_stock("")         # 空字串


# ---- 3. 民國日期解析 ----------------------------------------------------
def test_parse_roc_date():
    assert parse_roc_date("1130607") == date(2024, 6, 7)
    assert parse_roc_date("113/06/07") == date(2024, 6, 7)
    assert parse_roc_date("--") is None


# ---- 4. TWSE 解析 + 個股過濾 (monkeypatch，無網路) ----------------------
def test_twse_fetch_filters_non_stock(monkeypatch):
    sample = [
        {"Code": "2330", "Name": "台積電", "OpeningPrice": "1000", "HighestPrice": "1010",
         "LowestPrice": "995", "ClosingPrice": "1005", "Change": "5",
         "TradeVolume": "30000000", "TradeValue": "30000000000", "Transaction": "40000"},
        {"Code": "0050", "Name": "元大台灣50", "ClosingPrice": "180"},   # ETF -> 濾掉
        {"Code": "030123", "Name": "某某購01", "ClosingPrice": "1.5"},   # 權證 -> 濾掉
        {"Code": "2887A", "Name": "台新戊特", "ClosingPrice": "55"},     # 特別股 -> 濾掉
        {"Code": "", "Name": "壞資料"},                                  # 無代號 -> 略過
    ]
    monkeypatch.setattr(twse_mod, "get_json", lambda *a, **k: sample)
    src = TwseDataSource(trade_date=date(2026, 6, 5))
    quotes = src.fetch(src.list_fetch_units()[0])
    assert [q.symbol for q in quotes] == ["2330"]      # 只剩個股
    assert quotes[0].market == Market.TWSE
    assert quotes[0].close == Decimal("1005")


def test_twse_can_include_non_stock_when_flag_off(monkeypatch):
    sample = [
        {"Code": "2330", "Name": "台積電", "ClosingPrice": "1005"},
        {"Code": "0050", "Name": "元大台灣50", "ClosingPrice": "180"},
    ]
    monkeypatch.setattr(twse_mod, "get_json", lambda *a, **k: sample)
    src = TwseDataSource(trade_date=date(2026, 6, 5), only_individual=False)
    quotes = src.fetch(src.list_fetch_units()[0])
    assert sorted(q.symbol for q in quotes) == ["0050", "2330"]   # 關閉過濾就全保留


# ---- 5. TPEx 解析 + 個股過濾 -------------------------------------------
def test_tpex_fetch_filters_non_stock(monkeypatch):
    sample = [
        {"Date": "1150605", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶",
         "Open": "500", "High": "510", "Low": "498", "Close": "505", "Change": "5",
         "TradingShares": "1000000", "TransactionAmount": "500000000", "TransactionNumber": "3000"},
        {"Date": "1150605", "SecuritiesCompanyCode": "00679B", "CompanyName": "某債券ETF",
         "Close": "30"},                                            # ETF -> 濾掉
    ]
    monkeypatch.setattr(tpex_mod, "get_json", lambda *a, **k: sample)
    src = TpexDataSource()
    quotes = src.fetch(src.list_fetch_units()[0])
    assert [q.symbol for q in quotes] == ["6488"]
    assert quotes[0].trade_date == date(2026, 6, 5)
    assert quotes[0].market == Market.TPEX


# ---- 6. 多進程 crawler 串接 (假資料源，無網路) -------------------------
class _FakeSource(IDataSource):
    name = "fake"
    market = Market.TWSE

    def list_fetch_units(self):
        return [FetchUnit(source_name=self.name, market=self.market, label="上市個股"),
                FetchUnit(source_name=self.name, market=self.market, label="上櫃個股")]

    def fetch(self, unit):
        return [DailyQuote.normalize(symbol="2330", name=unit.label, market=Market.TWSE,
                                     trade_date=date(2026, 6, 5), close="100")]


def test_multiprocess_crawler_end_to_end():
    results = MultiProcessCrawler([_FakeSource()], processes=2).crawl()
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert sum(len(r.quotes) for r in results) == 2


class _BadSource(_FakeSource):
    name = "bad"

    def fetch(self, unit):
        raise ValueError("boom")


def test_crawler_isolates_errors():
    results = MultiProcessCrawler([_BadSource()], processes=2).crawl()
    assert all(not r.ok for r in results)
    assert "boom" in results[0].error


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
