"""domain + datasource(個股清單來源) 的單元測試 (不需網路)。"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_quant.datasource import twse as twse_mod
from stock_quant.datasource import tpex as tpex_mod
from stock_quant.datasource import TwseDataSource, TpexDataSource
from stock_quant.datasource.dates import parse_roc_date
from stock_quant.domain import DailyQuote, Market, is_individual_stock


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


def test_is_individual_stock():
    assert is_individual_stock("2330") and is_individual_stock("6488")
    assert not is_individual_stock("0050")     # ETF
    assert not is_individual_stock("030123")   # 權證
    assert not is_individual_stock("2887A")    # 特別股
    assert not is_individual_stock("911616")   # TDR/KY
    assert not is_individual_stock("")


def test_parse_roc_date():
    assert parse_roc_date("1130607") == date(2024, 6, 7)
    assert parse_roc_date("--") is None


def test_twse_universe_filters_non_stock(monkeypatch):
    sample = [
        {"Code": "2330", "Name": "台積電", "ClosingPrice": "1005"},
        {"Code": "0050", "Name": "元大台灣50", "ClosingPrice": "180"},   # ETF -> 濾掉
        {"Code": "030123", "Name": "某購01", "ClosingPrice": "1.5"},     # 權證 -> 濾掉
        {"Code": "", "Name": "壞資料"},
    ]
    monkeypatch.setattr(twse_mod, "get_json", lambda *a, **k: sample)
    src = TwseDataSource(trade_date=date(2026, 6, 5))
    quotes = src.fetch(src.list_fetch_units()[0])
    assert [q.symbol for q in quotes] == ["2330"]
    assert quotes[0].market == Market.TWSE


def test_tpex_universe_filters_non_stock(monkeypatch):
    sample = [
        {"Date": "1150605", "SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "Close": "505"},
        {"Date": "1150605", "SecuritiesCompanyCode": "00679B", "CompanyName": "債券ETF", "Close": "30"},
    ]
    monkeypatch.setattr(tpex_mod, "get_json", lambda *a, **k: sample)
    src = TpexDataSource()
    quotes = src.fetch(src.list_fetch_units()[0])
    assert [q.symbol for q in quotes] == ["6488"]
    assert quotes[0].market == Market.TPEX


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
